import pytest
from datetime import datetime, timezone, timedelta
from factory.db import Database
from factory.models import TaskCreate, TaskStatus
from factory.config import Config, ExecutionConfig, RepoConfig
from factory.orchestrator import Orchestrator, WorkerInfo


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config():
    return Config(
        execution=ExecutionConfig(),
        repos={"myapp": RepoConfig(url="https://github.com/test/myapp.git")},
    )


def test_worker_info_is_alive():
    worker = WorkerInfo(
        worker_id="test-worker",
        max_agents=8,
        running=3,
        last_heartbeat=datetime.now(timezone.utc),
    )
    assert worker.is_alive is True


def test_worker_info_is_dead():
    worker = WorkerInfo(
        worker_id="test-worker",
        max_agents=8,
        running=3,
        last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    assert worker.is_alive is False


def test_worker_info_has_capacity():
    worker = WorkerInfo(worker_id="w", max_agents=8, running=3,
                        last_heartbeat=datetime.now(timezone.utc))
    assert worker.has_capacity is True


def test_worker_info_no_capacity():
    worker = WorkerInfo(worker_id="w", max_agents=3, running=3,
                        last_heartbeat=datetime.now(timezone.utc))
    assert worker.has_capacity is False


async def test_prune_dead_workers_releases_claims(db, config):
    """When a worker dies, its unclaimed tasks go back to queue."""
    orch = Orchestrator(db=db, config=config, memory=None, base_dir="/opt/factory")

    task = await db.create_task(TaskCreate(title="Orphaned", repo="myapp"))
    await db.claim_task(task.id, "dead-worker")

    # Simulate dead worker (heartbeat 120s ago)
    orch._workers["dead-worker"] = WorkerInfo(
        worker_id="dead-worker", max_agents=8, running=1,
        last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=120),
    )

    dead = await orch.prune_and_cleanup_dead_workers()
    assert dead == ["dead-worker"]

    fetched = await db.get_task(task.id)
    assert fetched.claimed_by is None
    assert fetched.status == TaskStatus.QUEUED


async def test_prune_dead_workers_fails_in_progress(db, config):
    """When a worker dies, its in-progress tasks are failed."""
    orch = Orchestrator(db=db, config=config, memory=None, base_dir="/opt/factory")

    task = await db.create_task(TaskCreate(title="Running", repo="myapp"))
    await db.claim_task(task.id, "dead-worker")
    await db.update_task_status(task.id, TaskStatus.IN_PROGRESS)

    orch._workers["dead-worker"] = WorkerInfo(
        worker_id="dead-worker", max_agents=8, running=1,
        last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=120),
    )

    dead = await orch.prune_and_cleanup_dead_workers()
    assert dead == ["dead-worker"]

    fetched = await db.get_task(task.id)
    assert fetched.status == TaskStatus.FAILED
    assert "Worker disconnected" in fetched.error
