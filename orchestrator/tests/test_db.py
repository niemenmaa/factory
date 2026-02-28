from factory.db import Database
from factory.models import TaskCreate, TaskStatus


async def test_create_and_get_task():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(
        title="Fix login bug",
        description="The login timeout is too short",
        repo="myapp",
        agent_type="coder",
        plane_issue_id="issue-123",
    ))

    assert task.id is not None
    assert task.title == "Fix login bug"
    assert task.status == TaskStatus.QUEUED

    fetched = await db.get_task(task.id)
    assert fetched is not None
    assert fetched.title == "Fix login bug"

    await db.close()


async def test_list_tasks():
    db = Database(":memory:")
    await db.initialize()

    await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    await db.create_task(TaskCreate(title="Task 2", repo="myapp", agent_type="coder"))

    tasks = await db.list_tasks()
    assert len(tasks) == 2

    await db.close()


async def test_update_task_status():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    updated = await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    assert updated.status == TaskStatus.IN_PROGRESS
    assert updated.started_at is not None

    await db.close()


async def test_list_tasks_by_status():
    db = Database(":memory:")
    await db.initialize()

    await db.create_task(TaskCreate(title="Task 1", repo="myapp", agent_type="coder"))
    t2 = await db.create_task(TaskCreate(title="Task 2", repo="myapp", agent_type="coder"))
    await db.update_task_status(t2.id, TaskStatus.IN_PROGRESS)

    queued = await db.list_tasks(status=TaskStatus.QUEUED)
    assert len(queued) == 1
    assert queued[0].title == "Task 1"

    in_progress = await db.list_tasks(status=TaskStatus.IN_PROGRESS)
    assert len(in_progress) == 1
    assert in_progress[0].title == "Task 2"

    await db.close()


async def test_task_has_worker_fields():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(
        title="Test worker fields",
        repo="myapp",
    ))

    assert task.claimed_by is None
    assert task.claimed_at is None
    assert task.execution_mode == "local"

    await db.close()


async def test_claim_task():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Claim me", repo="myapp"))

    # Claim succeeds
    claimed = await db.claim_task(task.id, "worker-1")
    assert claimed is True

    fetched = await db.get_task(task.id)
    assert fetched.claimed_by == "worker-1"
    assert fetched.claimed_at is not None
    assert fetched.execution_mode == "remote"

    # Double claim fails
    claimed_again = await db.claim_task(task.id, "worker-2")
    assert claimed_again is False

    # Verify still claimed by worker-1
    fetched = await db.get_task(task.id)
    assert fetched.claimed_by == "worker-1"

    await db.close()


async def test_claim_task_only_queued():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="In progress", repo="myapp"))
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    claimed = await db.claim_task(task.id, "worker-1")
    assert claimed is False

    await db.close()


async def test_release_claim():
    db = Database(":memory:")
    await db.initialize()

    task = await db.create_task(TaskCreate(title="Release me", repo="myapp"))
    await db.claim_task(task.id, "worker-1")

    await db.release_claim(task.id)

    fetched = await db.get_task(task.id)
    assert fetched.claimed_by is None
    assert fetched.claimed_at is None
    assert fetched.execution_mode == "local"

    await db.close()


async def test_claim_batch():
    db = Database(":memory:")
    await db.initialize()

    t1 = await db.create_task(TaskCreate(title="Task 1", repo="myapp"))
    t2 = await db.create_task(TaskCreate(title="Task 2", repo="myapp"))
    t3 = await db.create_task(TaskCreate(title="Task 3", repo="myapp"))

    claimed = await db.claim_tasks("worker-1", count=2)
    assert len(claimed) == 2

    # Third task still available
    remaining = await db.claim_tasks("worker-2", count=5)
    assert len(remaining) == 1

    await db.close()
