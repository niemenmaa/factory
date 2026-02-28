# Local-Remote Work Handover Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add distributed worker support so local machines can claim and execute Factory tasks inside sandboxed Docker containers.

**Architecture:** VPS orchestrator gains a worker protocol API (heartbeat, claim, report). A new `factory-worker` Python package runs locally as a daemon, polls the VPS for tasks, runs agents in Docker containers, and reports results back. Tasks always live on VPS.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, httpx, Docker SDK, click (CLI), pydantic, pytest, pytest-asyncio

---

## Phase 1: VPS Orchestrator — Worker Protocol

### Task 1: Extend Task model with worker fields

**Files:**
- Modify: `orchestrator/src/factory/models.py:25-41`
- Modify: `orchestrator/src/factory/db.py` (schema + row converter)
- Test: `orchestrator/tests/test_db.py`

**Step 1: Write failing test for new task fields**

```python
# In orchestrator/tests/test_db.py — add at end of file
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
```

**Step 2: Run test to verify it fails**

Run: `cd orchestrator && python -m pytest tests/test_db.py::test_task_has_worker_fields -v`
Expected: FAIL — `Task` model has no `claimed_by` field

**Step 3: Add fields to Task model**

In `orchestrator/src/factory/models.py`, add to `Task` class after `completed_at`:

```python
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    execution_mode: str = "local"
```

**Step 4: Add columns to DB schema**

In `orchestrator/src/factory/db.py`, add to the `tasks` CREATE TABLE statement:

```sql
claimed_by TEXT DEFAULT NULL,
claimed_at TEXT DEFAULT NULL,
execution_mode TEXT DEFAULT 'local'
```

Add a migration to the `_MIGRATIONS` list:

```python
"ALTER TABLE tasks ADD COLUMN claimed_by TEXT DEFAULT NULL",
"ALTER TABLE tasks ADD COLUMN claimed_at TEXT DEFAULT NULL",
"ALTER TABLE tasks ADD COLUMN execution_mode TEXT DEFAULT 'local'",
```

**Step 5: Update `_row_to_task` to include new fields**

```python
claimed_by=row["claimed_by"] if "claimed_by" in row.keys() else None,
claimed_at=datetime.fromisoformat(row["claimed_at"]) if row.get("claimed_at") else None,
execution_mode=row["execution_mode"] if "execution_mode" in row.keys() else "local",
```

**Step 6: Run test to verify it passes**

Run: `cd orchestrator && python -m pytest tests/test_db.py::test_task_has_worker_fields -v`
Expected: PASS

**Step 7: Commit**

```bash
git add orchestrator/src/factory/models.py orchestrator/src/factory/db.py orchestrator/tests/test_db.py
git commit -m "feat: add worker fields to Task model (claimed_by, claimed_at, execution_mode)"
```

---

### Task 2: Add claim_task and release_claim DB methods

**Files:**
- Modify: `orchestrator/src/factory/db.py`
- Test: `orchestrator/tests/test_db.py`

**Step 1: Write failing tests**

```python
# In orchestrator/tests/test_db.py
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
```

**Step 2: Run tests to verify they fail**

Run: `cd orchestrator && python -m pytest tests/test_db.py -k "claim" -v`
Expected: FAIL — methods don't exist

**Step 3: Implement claim methods in db.py**

```python
async def claim_task(self, task_id: int, worker_id: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    cursor = await self._db.execute(
        "UPDATE tasks SET claimed_by = ?, claimed_at = ?, execution_mode = 'remote' "
        "WHERE id = ? AND claimed_by IS NULL AND status = 'queued'",
        (worker_id, now, task_id),
    )
    await self._db.commit()
    return cursor.rowcount > 0

async def release_claim(self, task_id: int) -> None:
    await self._db.execute(
        "UPDATE tasks SET claimed_by = NULL, claimed_at = NULL, execution_mode = 'local' "
        "WHERE id = ?",
        (task_id,),
    )
    await self._db.commit()

async def claim_tasks(self, worker_id: str, count: int) -> list[Task]:
    cursor = await self._db.execute(
        "SELECT id FROM tasks WHERE status = 'queued' AND claimed_by IS NULL "
        "ORDER BY created_at ASC LIMIT ?",
        (count,),
    )
    rows = await cursor.fetchall()
    claimed = []
    for row in rows:
        if await self.claim_task(row["id"], worker_id):
            task = await self.get_task(row["id"])
            if task:
                claimed.append(task)
    return claimed

async def release_worker_claims(self, worker_id: str) -> int:
    """Release all claims held by a worker. Returns count released."""
    cursor = await self._db.execute(
        "UPDATE tasks SET claimed_by = NULL, claimed_at = NULL, execution_mode = 'local' "
        "WHERE claimed_by = ? AND status = 'queued'",
        (worker_id,),
    )
    await self._db.commit()
    return cursor.rowcount

async def fail_worker_tasks(self, worker_id: str) -> int:
    """Fail all in-progress tasks for a dead worker. Returns count failed."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = await self._db.execute(
        "UPDATE tasks SET status = 'failed', error = 'Worker disconnected', "
        "completed_at = ? WHERE claimed_by = ? AND status = 'in_progress'",
        (now, worker_id),
    )
    await self._db.commit()
    return cursor.rowcount
```

**Step 4: Run tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_db.py -k "claim" -v`
Expected: PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/db.py orchestrator/tests/test_db.py
git commit -m "feat: add task claim/release DB methods for worker protocol"
```

---

### Task 3: Add execution config to Config model

**Files:**
- Modify: `orchestrator/src/factory/config.py`
- Modify: `orchestrator/src/factory/models.py` (add ClaimPayload model)
- Test: `orchestrator/tests/test_config.py` (create if needed)

**Step 1: Write failing test**

```python
# orchestrator/tests/test_config.py
from factory.config import Config, ExecutionConfig, RepoConfig


def test_execution_config_defaults():
    config = Config()
    assert config.execution.prefer_workers is True
    assert config.execution.local_fallback is True
    assert config.execution.worker_claim_window_seconds == 30
    assert config.execution.worker_heartbeat_ttl_seconds == 60
    assert config.execution.claim_lease_ttl_seconds == 300


def test_repo_config_image_field():
    repo = RepoConfig(url="https://github.com/test/repo.git", image="factory-agent:php")
    assert repo.image == "factory-agent:php"


def test_repo_config_image_default():
    repo = RepoConfig(url="https://github.com/test/repo.git")
    assert repo.image == ""
```

**Step 2: Run test to verify it fails**

Run: `cd orchestrator && python -m pytest tests/test_config.py -v`
Expected: FAIL — `ExecutionConfig` doesn't exist

**Step 3: Add ExecutionConfig and update Config and RepoConfig**

In `orchestrator/src/factory/config.py`:

```python
class ExecutionConfig(BaseModel):
    prefer_workers: bool = True
    local_fallback: bool = True
    worker_claim_window_seconds: int = 30
    worker_heartbeat_ttl_seconds: int = 60
    claim_lease_ttl_seconds: int = 300
```

Add to `RepoConfig`:

```python
class RepoConfig(BaseModel):
    url: str
    default_agent: str = "coder"
    image: str = ""  # Docker image for containerized execution
```

Add to `Config`:

```python
    execution: ExecutionConfig = ExecutionConfig()
```

**Step 4: Add ClaimPayload model to models.py**

```python
class ClaimPayload(BaseModel):
    task_id: int
    title: str
    description: str = ""
    repo: str
    repo_url: str
    branch_name: str
    image: str
    prompt: str
    system_prompt: str
    allowed_tools: list[str]
    timeout_minutes: int
```

**Step 5: Run tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_config.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add orchestrator/src/factory/config.py orchestrator/src/factory/models.py orchestrator/tests/test_config.py
git commit -m "feat: add ExecutionConfig, ClaimPayload model, image field on RepoConfig"
```

---

### Task 4: Add worker tracking to Orchestrator

**Files:**
- Modify: `orchestrator/src/factory/orchestrator.py`
- Test: `orchestrator/tests/test_worker_tracking.py` (new)

**Step 1: Write failing tests**

```python
# orchestrator/tests/test_worker_tracking.py
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock
from factory.orchestrator import Orchestrator, WorkerInfo


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
```

**Step 2: Run tests to verify they fail**

Run: `cd orchestrator && python -m pytest tests/test_worker_tracking.py -v`
Expected: FAIL — `WorkerInfo` not importable

**Step 3: Add WorkerInfo dataclass and worker tracking to Orchestrator**

At the top of `orchestrator/src/factory/orchestrator.py`, add:

```python
from dataclasses import dataclass, field

@dataclass
class WorkerInfo:
    worker_id: str
    max_agents: int
    running: int
    last_heartbeat: datetime

    @property
    def is_alive(self) -> bool:
        elapsed = (datetime.now(timezone.utc) - self.last_heartbeat).total_seconds()
        return elapsed < 60

    @property
    def has_capacity(self) -> bool:
        return self.running < self.max_agents
```

In `Orchestrator.__init__`, add:

```python
self._workers: dict[str, WorkerInfo] = {}
```

Add methods to `Orchestrator`:

```python
def register_heartbeat(self, worker_id: str, max_agents: int, running: int) -> None:
    self._workers[worker_id] = WorkerInfo(
        worker_id=worker_id,
        max_agents=max_agents,
        running=running,
        last_heartbeat=datetime.now(timezone.utc),
    )

def _has_active_workers(self) -> bool:
    self._prune_dead_workers()
    return any(w.has_capacity for w in self._workers.values())

def _prune_dead_workers(self) -> list[str]:
    dead = [wid for wid, w in self._workers.items() if not w.is_alive]
    for wid in dead:
        del self._workers[wid]
    return dead

def get_workers(self) -> list[WorkerInfo]:
    self._prune_dead_workers()
    return list(self._workers.values())
```

**Step 4: Run tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_worker_tracking.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/orchestrator.py orchestrator/tests/test_worker_tracking.py
git commit -m "feat: add WorkerInfo dataclass and worker tracking to Orchestrator"
```

---

### Task 5: Add worker API endpoints

**Files:**
- Modify: `orchestrator/src/factory/api.py`
- Test: `orchestrator/tests/test_worker_api.py` (new)

**Step 1: Write failing tests**

```python
# orchestrator/tests/test_worker_api.py
import pytest
from unittest.mock import MagicMock, AsyncMock
from httpx import AsyncClient, ASGITransport
from factory.main import app
from factory.deps import get_db, get_orchestrator
from factory.db import Database
from factory.models import TaskCreate, TaskStatus


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def mock_orchestrator(db):
    orch = MagicMock()
    orch.db = db
    orch.config = MagicMock()
    orch.config.repos = {
        "myapp": MagicMock(url="https://github.com/test/myapp.git", image="factory-agent:php"),
    }
    orch.config.agent_templates = {
        "coder": MagicMock(
            system_prompt_file="prompts/coder.md",
            allowed_tools=["Read", "Edit", "Bash"],
            timeout_minutes=60,
        ),
    }
    orch.register_heartbeat = MagicMock()
    orch.get_workers = MagicMock(return_value=[])
    orch._build_prompt = MagicMock(return_value="test prompt")
    orch.plane = None
    orch.memory = None
    orch.runner = MagicMock()
    orch.runner.get_running_agents.return_value = {}
    return orch


@pytest.fixture
async def client(db, mock_orchestrator):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_heartbeat(client, mock_orchestrator):
    resp = await client.post("/api/workers/heartbeat", json={
        "worker_id": "test-worker",
        "max_agents": 8,
        "running": 2,
    })
    assert resp.status_code == 200
    mock_orchestrator.register_heartbeat.assert_called_once_with("test-worker", 8, 2)


async def test_claim_tasks(client, db):
    t1 = await db.create_task(TaskCreate(title="Task 1", repo="myapp"))
    t2 = await db.create_task(TaskCreate(title="Task 2", repo="myapp"))

    resp = await client.post("/api/tasks/claim", json={
        "worker_id": "test-worker",
        "count": 2,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tasks"]) == 2
    assert data["tasks"][0]["task_id"] == t1.id
    assert data["tasks"][0]["image"] == "factory-agent:php"


async def test_claim_specific_task(client, db):
    task = await db.create_task(TaskCreate(title="Specific task", repo="myapp"))

    resp = await client.post(f"/api/tasks/{task.id}/claim", json={
        "worker_id": "test-worker",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["task"]["task_id"] == task.id


async def test_claim_already_claimed(client, db):
    task = await db.create_task(TaskCreate(title="Taken", repo="myapp"))
    await db.claim_task(task.id, "other-worker")

    resp = await client.post(f"/api/tasks/{task.id}/claim", json={
        "worker_id": "test-worker",
    })
    assert resp.status_code == 409


async def test_report_success(client, db, mock_orchestrator):
    task = await db.create_task(TaskCreate(title="Done", repo="myapp"))
    await db.claim_task(task.id, "test-worker")

    mock_orchestrator._handle_success = AsyncMock()

    resp = await client.post(f"/api/tasks/{task.id}/report", json={
        "worker_id": "test-worker",
        "status": "done",
        "output": "All done",
        "branch_name": "agent/task-1-done",
        "pr_url": "https://github.com/test/myapp/pull/1",
    })
    assert resp.status_code == 200

    fetched = await db.get_task(task.id)
    assert fetched.branch_name == "agent/task-1-done"
    assert fetched.pr_url == "https://github.com/test/myapp/pull/1"


async def test_list_workers(client, mock_orchestrator):
    resp = await client.get("/api/workers")
    assert resp.status_code == 200
    assert resp.json() == []
```

**Step 2: Run tests to verify they fail**

Run: `cd orchestrator && python -m pytest tests/test_worker_api.py -v`
Expected: FAIL — endpoints don't exist

**Step 3: Implement worker API endpoints**

In `orchestrator/src/factory/api.py`, add request models at top:

```python
from factory.models import ClaimPayload

class HeartbeatRequest(BaseModel):
    worker_id: str
    max_agents: int
    running: int

class ClaimRequest(BaseModel):
    worker_id: str
    count: int = 1

class SingleClaimRequest(BaseModel):
    worker_id: str

class ReportRequest(BaseModel):
    worker_id: str
    status: str  # "done" or "failed"
    output: str = ""
    branch_name: str = ""
    pr_url: str = ""
```

Add endpoints:

```python
@router.post("/workers/heartbeat")
async def worker_heartbeat(
    body: HeartbeatRequest,
    orch: Orchestrator = Depends(get_orchestrator),
):
    orch.register_heartbeat(body.worker_id, body.max_agents, body.running)
    return {"status": "ok"}


@router.get("/workers")
async def list_workers(orch: Orchestrator = Depends(get_orchestrator)):
    workers = orch.get_workers()
    return [
        {
            "worker_id": w.worker_id,
            "max_agents": w.max_agents,
            "running": w.running,
            "last_heartbeat": w.last_heartbeat.isoformat(),
            "is_alive": w.is_alive,
            "has_capacity": w.has_capacity,
        }
        for w in workers
    ]


@router.post("/tasks/claim")
async def claim_tasks(
    body: ClaimRequest,
    db: Database = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
):
    tasks = await db.claim_tasks(body.worker_id, body.count)
    payloads = [_build_claim_payload(t, orch) for t in tasks]
    return {"tasks": payloads}


@router.post("/tasks/{task_id}/claim")
async def claim_specific_task(
    task_id: int,
    body: SingleClaimRequest,
    db: Database = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    success = await db.claim_task(task_id, body.worker_id)
    if not success:
        raise HTTPException(status_code=409, detail="Task already claimed or not queued")
    task = await db.get_task(task_id)
    return {"task": _build_claim_payload(task, orch)}


@router.post("/tasks/{task_id}/report")
async def report_task_result(
    task_id: int,
    body: ReportRequest,
    db: Database = Depends(get_db),
    orch: Orchestrator = Depends(get_orchestrator),
):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Update task fields from worker report
    fields = {}
    if body.branch_name:
        fields["branch_name"] = body.branch_name
    if body.pr_url:
        fields["pr_url"] = body.pr_url
    if fields:
        await db.update_task_fields(task_id, **fields)

    # Route to orchestrator completion handlers
    if body.status == "done":
        await orch._handle_success(task_id, body.output)
    else:
        await orch._handle_failure(task_id, body.output)

    return {"status": "ok"}


def _build_claim_payload(task, orch) -> dict:
    """Build the full execution payload for a claimed task."""
    from factory.prompts import load_prompt

    repo_config = orch.config.repos.get(task.repo)
    template = orch.config.agent_templates.get(task.agent_type)

    repo_url = repo_config.url if repo_config else ""
    image = (repo_config.image if repo_config and repo_config.image
             else _detect_image(task.repo))

    prompt = orch._build_prompt(task.title, task.description)
    system_prompt = ""
    if template and template.system_prompt_file:
        try:
            system_prompt = load_prompt(template.system_prompt_file, orch.base_dir)
        except Exception:
            pass

    branch_name = task.branch_name or f"agent/task-{task.id}-{_slugify(task.title)}"
    allowed_tools = template.allowed_tools if template else []
    timeout = template.timeout_minutes if template else 60

    return ClaimPayload(
        task_id=task.id,
        title=task.title,
        description=task.description,
        repo=task.repo,
        repo_url=repo_url,
        branch_name=branch_name,
        image=image,
        prompt=prompt,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        timeout_minutes=timeout,
    ).model_dump()


def _detect_image(repo: str) -> str:
    """Auto-detect image from repo files. Fallback to base."""
    # NOTE: Full auto-detect implemented in Task 10.
    # For now, return base image.
    return "factory-agent:base"


def _slugify(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]
```

**Step 4: Run tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_worker_api.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/api.py orchestrator/tests/test_worker_api.py
git commit -m "feat: add worker protocol API endpoints (heartbeat, claim, report)"
```

---

### Task 6: Modify process_task() for execution routing

**Files:**
- Modify: `orchestrator/src/factory/orchestrator.py`
- Test: `orchestrator/tests/test_execution_routing.py` (new)

**Step 1: Write failing tests**

```python
# orchestrator/tests/test_execution_routing.py
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch
from factory.db import Database
from factory.models import TaskCreate, TaskStatus
from factory.config import Config, ExecutionConfig, RepoConfig, AgentTemplateConfig
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
        execution=ExecutionConfig(prefer_workers=True, local_fallback=True),
        repos={"myapp": RepoConfig(url="https://github.com/test/myapp.git")},
        agent_templates={"coder": AgentTemplateConfig(
            system_prompt_file="prompts/coder.md",
            allowed_tools=["Read", "Edit", "Bash"],
        )},
    )


async def test_process_task_skips_claimed(db, config):
    orch = Orchestrator(db=db, config=config, memory=None, base_dir="/opt/factory")
    task = await db.create_task(TaskCreate(title="Claimed", repo="myapp"))
    await db.claim_task(task.id, "worker-1")

    result = await orch.process_task(task.id)
    assert result is True  # returns True but doesn't start agent

    # Task should still be queued, not in_progress
    fetched = await db.get_task(task.id)
    assert fetched.status == TaskStatus.QUEUED


async def test_process_task_defers_to_workers(db, config):
    orch = Orchestrator(db=db, config=config, memory=None, base_dir="/opt/factory")
    orch.register_heartbeat("worker-1", max_agents=8, running=0)

    task = await db.create_task(TaskCreate(title="Defer me", repo="myapp"))

    result = await orch.process_task(task.id)
    assert result is True

    # Task stays queued for worker to claim
    fetched = await db.get_task(task.id)
    assert fetched.status == TaskStatus.QUEUED


async def test_process_task_local_when_no_workers(db, config):
    orch = Orchestrator(db=db, config=config, memory=None, base_dir="/opt/factory")
    orch.runner = MagicMock()
    orch.runner.can_accept_task = True
    orch.runner.start_agent = AsyncMock(return_value=True)
    orch.repo_manager = MagicMock()
    orch.repo_manager.ensure_repo = AsyncMock()
    orch.repo_manager.create_worktree = AsyncMock(return_value="/tmp/wt")

    task = await db.create_task(TaskCreate(title="Run locally", repo="myapp"))

    # No workers registered — should run locally
    result = await orch.process_task(task.id)
    assert result is True
    orch.runner.start_agent.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `cd orchestrator && python -m pytest tests/test_execution_routing.py -v`
Expected: FAIL — process_task doesn't check claimed_by

**Step 3: Add routing checks to process_task()**

In `orchestrator/src/factory/orchestrator.py`, at the top of `process_task()`, after fetching the task, add:

```python
    # Skip if claimed by a remote worker
    if task.claimed_by:
        logger.info("Task %d claimed by worker %s, skipping local execution", task_id, task.claimed_by)
        return True

    # Defer to workers if available and preferred
    if self.config.execution.prefer_workers and self._has_active_workers():
        logger.info("Task %d deferred to workers", task_id)
        return True
```

**Step 4: Run tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_execution_routing.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/orchestrator.py orchestrator/tests/test_execution_routing.py
git commit -m "feat: add execution routing — skip claimed tasks, defer to workers"
```

---

### Task 7: Add dead worker cleanup

**Files:**
- Modify: `orchestrator/src/factory/orchestrator.py`
- Test: `orchestrator/tests/test_worker_tracking.py`

**Step 1: Write failing tests**

```python
# Add to orchestrator/tests/test_worker_tracking.py
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
```

**Step 2: Run tests to verify they fail**

Run: `cd orchestrator && python -m pytest tests/test_worker_tracking.py -k "prune_dead" -v`
Expected: FAIL — method doesn't exist

**Step 3: Implement prune_and_cleanup_dead_workers**

In `Orchestrator`:

```python
async def prune_and_cleanup_dead_workers(self) -> list[str]:
    dead = self._prune_dead_workers()
    for worker_id in dead:
        released = await self.db.release_worker_claims(worker_id)
        failed = await self.db.fail_worker_tasks(worker_id)
        logger.warning(
            "Worker %s died: released %d claims, failed %d tasks",
            worker_id, released, failed,
        )
        if failed > 0:
            await self._notify(
                f"\u26a0\ufe0f Worker {worker_id} disconnected, {failed} task(s) failed"
            )
    return dead
```

Call this from the existing `_poll_loop` (or add to heartbeat processing).

**Step 4: Run tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_worker_tracking.py -k "prune_dead" -v`
Expected: PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/orchestrator.py orchestrator/tests/test_worker_tracking.py
git commit -m "feat: add dead worker cleanup — release claims, fail in-progress tasks"
```

---

## Phase 2: Local Worker Package

### Task 8: Create worker package scaffold

**Files:**
- Create: `worker/pyproject.toml`
- Create: `worker/src/factory_worker/__init__.py`
- Create: `worker/src/factory_worker/config.py`

**Step 1: Create directory structure**

Run: `mkdir -p worker/src/factory_worker`

**Step 2: Create pyproject.toml**

```toml
[project]
name = "factory-worker"
version = "0.1.0"
description = "Local worker daemon for Factory agent orchestrator"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.28.0",
    "pyyaml>=6.0",
    "pydantic>=2.10.0",
    "click>=8.0",
    "docker>=7.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.25.0",
]

[project.scripts]
factory-worker = "factory_worker.main:cli"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

**Step 3: Create config.py**

```python
from pathlib import Path
from pydantic import BaseModel
import os
import yaml


class ContainerConfig(BaseModel):
    runtime: str = "docker"
    dangerous_mode: bool = True
    cpu_limit: str = "2"
    memory_limit: str = "4g"
    timeout_minutes: int = 60


class WorkerConfig(BaseModel):
    worker_id: str = ""
    orchestrator_url: str = "http://localhost:8100/api"
    auth_token: str = ""
    max_agents: int = 8
    poll_interval_seconds: int = 10
    claim_batch_size: int = 3
    container: ContainerConfig = ContainerConfig()
    repos_filter: list[str] = []


DEFAULT_CONFIG_PATH = Path.home() / ".factory" / "worker-config.yml"


def load_worker_config(path: Path | None = None) -> WorkerConfig:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return WorkerConfig()
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    # Resolve env vars in auth_token
    if "auth_token" in data and data["auth_token"].startswith("${"):
        var_name = data["auth_token"].strip("${}")
        data["auth_token"] = os.environ.get(var_name, "")
    return WorkerConfig(**data)
```

**Step 4: Create __init__.py**

```python
"""Factory Worker — local agent execution daemon."""
```

**Step 5: Commit**

```bash
git add worker/
git commit -m "feat: create factory-worker package scaffold with config"
```

---

### Task 9: Implement orchestrator HTTP client

**Files:**
- Create: `worker/src/factory_worker/client.py`
- Create: `worker/tests/test_client.py`

**Step 1: Write failing tests**

```python
# worker/tests/test_client.py
import pytest
import httpx
from unittest.mock import AsyncMock, patch
from factory_worker.client import OrchestratorClient


@pytest.fixture
def client():
    return OrchestratorClient("http://test:8100/api", "test-token")


async def test_heartbeat(client):
    mock_response = httpx.Response(200, json={"status": "ok"})
    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=mock_response):
        await client.heartbeat("worker-1", max_agents=8, running=3)
        client._http.post.assert_called_once_with(
            "/workers/heartbeat",
            json={"worker_id": "worker-1", "max_agents": 8, "running": 3},
        )


async def test_claim_tasks(client):
    payload = {"tasks": [{"task_id": 1, "title": "Test", "repo": "myapp",
                          "repo_url": "", "branch_name": "b", "image": "factory-agent:base",
                          "prompt": "p", "system_prompt": "s", "allowed_tools": [],
                          "description": "", "timeout_minutes": 60}]}
    mock_response = httpx.Response(200, json=payload)
    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=mock_response):
        tasks = await client.claim_tasks("worker-1", count=3)
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == 1


async def test_report_result(client):
    mock_response = httpx.Response(200, json={"status": "ok"})
    with patch.object(client._http, "post", new_callable=AsyncMock, return_value=mock_response):
        await client.report_result(
            task_id=1, worker_id="worker-1", status="done",
            output="done", branch_name="b", pr_url="http://pr",
        )
        client._http.post.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `cd worker && pip install -e ".[dev]" && python -m pytest tests/test_client.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement client.py**

```python
import logging
import httpx

logger = logging.getLogger(__name__)


class OrchestratorClient:
    def __init__(self, base_url: str, auth_token: str):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=30.0,
        )

    async def heartbeat(self, worker_id: str, max_agents: int, running: int) -> None:
        resp = await self._http.post("/workers/heartbeat", json={
            "worker_id": worker_id,
            "max_agents": max_agents,
            "running": running,
        })
        resp.raise_for_status()

    async def claim_tasks(self, worker_id: str, count: int) -> list[dict]:
        resp = await self._http.post("/tasks/claim", json={
            "worker_id": worker_id,
            "count": count,
        })
        resp.raise_for_status()
        return resp.json().get("tasks", [])

    async def claim_task(self, task_id: int, worker_id: str) -> dict | None:
        resp = await self._http.post(f"/tasks/{task_id}/claim", json={
            "worker_id": worker_id,
        })
        if resp.status_code == 409:
            return None
        resp.raise_for_status()
        return resp.json().get("task")

    async def report_result(
        self, task_id: int, worker_id: str, status: str,
        output: str, branch_name: str, pr_url: str,
    ) -> None:
        resp = await self._http.post(f"/tasks/{task_id}/report", json={
            "worker_id": worker_id,
            "status": status,
            "output": output,
            "branch_name": branch_name,
            "pr_url": pr_url,
        })
        resp.raise_for_status()

    async def report_in_progress(self, task_id: int, worker_id: str) -> None:
        """Tell orchestrator the agent has started (refreshes claim lease)."""
        resp = await self._http.post(f"/tasks/{task_id}/report", json={
            "worker_id": worker_id,
            "status": "in_progress",
            "output": "",
            "branch_name": "",
            "pr_url": "",
        })
        resp.raise_for_status()

    async def close(self) -> None:
        await self._http.aclose()
```

**Step 4: Run tests to verify they pass**

Run: `cd worker && python -m pytest tests/test_client.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add worker/src/factory_worker/client.py worker/tests/test_client.py
git commit -m "feat: implement orchestrator HTTP client for worker"
```

---

### Task 10: Implement Docker container manager

**Files:**
- Create: `worker/src/factory_worker/container.py`
- Create: `worker/tests/test_container.py`

**Step 1: Write failing tests**

```python
# worker/tests/test_container.py
import pytest
from unittest.mock import MagicMock, patch
from factory_worker.container import ContainerManager, AgentResult
from factory_worker.config import ContainerConfig


@pytest.fixture
def config():
    return ContainerConfig(cpu_limit="2", memory_limit="4g", timeout_minutes=60)


def test_running_count(config):
    mgr = ContainerManager(config)
    assert mgr.running_count == 0


def test_start_creates_container(config):
    mgr = ContainerManager(config)
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container
    mgr._docker = mock_client

    mgr.start(
        task_id=42,
        worktree_path="/tmp/wt",
        prompt="fix the bug",
        system_prompt="you are a coder",
        allowed_tools=["Read", "Edit", "Bash"],
        image="factory-agent:php",
        env={"ANTHROPIC_API_KEY": "test"},
    )

    assert mgr.running_count == 1
    mock_client.containers.run.assert_called_once()


def test_collect_finished_running(config):
    mgr = ContainerManager(config)
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_container.reload = MagicMock()
    mgr._running = {42: {"container": mock_container, "task_id": 42}}

    results = mgr.collect_finished()
    assert len(results) == 0
    assert mgr.running_count == 1


def test_collect_finished_exited(config):
    mgr = ContainerManager(config)
    mock_container = MagicMock()
    mock_container.status = "exited"
    mock_container.attrs = {"State": {"ExitCode": 0}}
    mock_container.logs.return_value = b"output text"
    mock_container.reload = MagicMock()
    mock_container.remove = MagicMock()
    mgr._running = {42: {"container": mock_container, "task_id": 42}}

    results = mgr.collect_finished()
    assert len(results) == 1
    assert results[0].task_id == 42
    assert results[0].status == "done"
    assert mgr.running_count == 0
```

**Step 2: Run tests to verify they fail**

Run: `cd worker && python -m pytest tests/test_container.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement container.py**

```python
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import docker

from factory_worker.config import ContainerConfig

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    task_id: int
    status: str  # "done" or "failed"
    output: str
    branch_name: str
    pr_url: str


class ContainerManager:
    def __init__(self, config: ContainerConfig):
        self._config = config
        self._docker = docker.from_env()
        self._running: dict[int, dict] = {}

    @property
    def running_count(self) -> int:
        return len(self._running)

    def start(
        self,
        task_id: int,
        worktree_path: str,
        prompt: str,
        system_prompt: str,
        allowed_tools: list[str],
        image: str,
        env: dict[str, str],
    ) -> None:
        container_name = f"factory-agent-{task_id}"

        cmd = [
            "--print",
            "--output-format", "json",
        ]
        if self._config.dangerous_mode:
            cmd.append("--dangerously-skip-permissions")
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        cmd.append(prompt)

        container = self._docker.containers.run(
            image,
            cmd,
            name=container_name,
            detach=True,
            volumes={worktree_path: {"bind": "/workspace", "mode": "rw"}},
            environment=env,
            working_dir="/workspace",
            cpu_count=int(self._config.cpu_limit),
            mem_limit=self._config.memory_limit,
        )

        self._running[task_id] = {
            "container": container,
            "task_id": task_id,
            "started_at": datetime.now(timezone.utc),
        }
        logger.info("Started container %s for task %d", container_name, task_id)

    def collect_finished(self) -> list[AgentResult]:
        results = []
        finished_ids = []

        for task_id, info in self._running.items():
            container = info["container"]
            try:
                container.reload()
            except Exception:
                # Container gone
                finished_ids.append(task_id)
                results.append(AgentResult(
                    task_id=task_id, status="failed",
                    output="Container disappeared", branch_name="", pr_url="",
                ))
                continue

            if container.status != "exited":
                continue

            exit_code = container.attrs["State"]["ExitCode"]
            output = container.logs().decode("utf-8", errors="replace")

            branch_name, pr_url = _parse_output(output)

            results.append(AgentResult(
                task_id=task_id,
                status="done" if exit_code == 0 else "failed",
                output=output[-50000:],  # truncate to 50KB
                branch_name=branch_name,
                pr_url=pr_url,
            ))

            try:
                container.remove()
            except Exception:
                pass
            finished_ids.append(task_id)

        for tid in finished_ids:
            del self._running[tid]

        return results

    def kill_all(self) -> None:
        for task_id, info in list(self._running.items()):
            try:
                info["container"].kill()
                info["container"].remove(force=True)
            except Exception:
                pass
        self._running.clear()

    def cleanup_orphans(self) -> int:
        """Remove any leftover factory-agent-* containers from previous runs."""
        count = 0
        for container in self._docker.containers.list(all=True):
            if container.name.startswith("factory-agent-"):
                try:
                    container.remove(force=True)
                    count += 1
                except Exception:
                    pass
        return count

    def check_timeouts(self) -> list[int]:
        """Kill containers that exceeded timeout. Returns list of killed task IDs."""
        killed = []
        timeout_seconds = self._config.timeout_minutes * 60
        now = datetime.now(timezone.utc)

        for task_id, info in list(self._running.items()):
            elapsed = (now - info["started_at"]).total_seconds()
            if elapsed > timeout_seconds:
                logger.warning("Task %d timed out after %ds", task_id, int(elapsed))
                try:
                    info["container"].kill()
                except Exception:
                    pass
                killed.append(task_id)

        return killed


def _parse_output(output: str) -> tuple[str, str]:
    """Extract branch name and PR URL from agent output."""
    branch = ""
    pr_url = ""

    # Look for PR URL
    pr_match = re.search(r"https://github\.com/[^\s]+/pull/\d+", output)
    if pr_match:
        pr_url = pr_match.group(0)

    # Look for branch name
    branch_match = re.search(r"agent/task-\d+-[\w-]+", output)
    if branch_match:
        branch = branch_match.group(0)

    return branch, pr_url
```

**Step 4: Run tests to verify they pass**

Run: `cd worker && python -m pytest tests/test_container.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add worker/src/factory_worker/container.py worker/tests/test_container.py
git commit -m "feat: implement Docker container manager for worker agents"
```

---

### Task 11: Implement local workspace manager

**Files:**
- Create: `worker/src/factory_worker/workspace.py`
- Create: `worker/tests/test_workspace.py`

**Step 1: Write failing test**

```python
# worker/tests/test_workspace.py
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from factory_worker.workspace import WorkspaceManager


def test_repo_cache_path():
    mgr = WorkspaceManager(cache_dir=Path("/tmp/factory-test"))
    path = mgr._repo_path("my-org/my-app")
    assert path == Path("/tmp/factory-test/repos/my-org/my-app")


def test_worktree_path():
    mgr = WorkspaceManager(cache_dir=Path("/tmp/factory-test"))
    path = mgr._worktree_path("my-org/my-app", "agent/task-42-fix-bug")
    assert path == Path("/tmp/factory-test/worktrees/agent/task-42-fix-bug")
```

**Step 2: Run tests to verify they fail**

Run: `cd worker && python -m pytest tests/test_workspace.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement workspace.py**

```python
import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".factory"


class WorkspaceManager:
    def __init__(self, cache_dir: Path = DEFAULT_CACHE_DIR):
        self._cache_dir = cache_dir
        self._repos_dir = cache_dir / "repos"
        self._worktrees_dir = cache_dir / "worktrees"

    def _repo_path(self, repo: str) -> Path:
        return self._repos_dir / repo

    def _worktree_path(self, repo: str, branch_name: str) -> Path:
        return self._worktrees_dir / branch_name

    async def _run(self, *args: str, cwd: Path | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(args)}\n{stderr.decode()}")
        return stdout.decode().strip()

    async def ensure_repo(self, repo: str, repo_url: str) -> Path:
        repo_path = self._repo_path(repo)
        if repo_path.exists():
            await self._run("git", "fetch", "--all", cwd=repo_path)
            await self._run("git", "pull", "--ff-only", cwd=repo_path)
        else:
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            await self._run("git", "clone", repo_url, str(repo_path))
        return repo_path

    async def create_worktree(self, repo: str, repo_url: str, branch_name: str) -> Path:
        repo_path = await self.ensure_repo(repo, repo_url)
        wt_path = self._worktree_path(repo, branch_name)

        if wt_path.exists():
            logger.info("Worktree already exists at %s, reusing", wt_path)
            return wt_path

        wt_path.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            "git", "worktree", "add", "-b", branch_name, str(wt_path),
            cwd=repo_path,
        )
        logger.info("Created worktree at %s", wt_path)
        return wt_path

    async def remove_worktree(self, repo: str, branch_name: str) -> None:
        repo_path = self._repo_path(repo)
        wt_path = self._worktree_path(repo, branch_name)
        if wt_path.exists():
            await self._run("git", "worktree", "remove", str(wt_path), cwd=repo_path)
            logger.info("Removed worktree at %s", wt_path)
```

**Step 4: Run tests to verify they pass**

Run: `cd worker && python -m pytest tests/test_workspace.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add worker/src/factory_worker/workspace.py worker/tests/test_workspace.py
git commit -m "feat: implement local workspace manager for worker"
```

---

### Task 12: Implement main loop and CLI

**Files:**
- Create: `worker/src/factory_worker/main.py`
- Create: `worker/tests/test_main.py`

**Step 1: Write failing test for the run loop**

```python
# worker/tests/test_main.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from factory_worker.main import run_loop
from factory_worker.config import WorkerConfig


async def test_run_loop_single_iteration():
    config = WorkerConfig(
        worker_id="test-worker",
        orchestrator_url="http://test:8100/api",
        auth_token="token",
        max_agents=4,
        poll_interval_seconds=1,
    )

    mock_client = AsyncMock()
    mock_client.heartbeat = AsyncMock()
    mock_client.claim_tasks = AsyncMock(return_value=[])

    mock_containers = MagicMock()
    mock_containers.running_count = 0
    mock_containers.collect_finished.return_value = []
    mock_containers.check_timeouts.return_value = []

    mock_workspace = AsyncMock()

    # Run one iteration then break
    with patch("factory_worker.main.asyncio.sleep", side_effect=StopIteration):
        with pytest.raises(StopIteration):
            await run_loop(config, mock_client, mock_containers, mock_workspace)

    mock_client.heartbeat.assert_called_once_with("test-worker", max_agents=4, running=0)
    mock_client.claim_tasks.assert_called_once_with("test-worker", count=3)
```

**Step 2: Run tests to verify they fail**

Run: `cd worker && python -m pytest tests/test_main.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement main.py**

```python
import asyncio
import logging
import os
import signal
import socket
from pathlib import Path

import click

from factory_worker.client import OrchestratorClient
from factory_worker.config import WorkerConfig, load_worker_config, DEFAULT_CONFIG_PATH
from factory_worker.container import ContainerManager, AgentResult
from factory_worker.workspace import WorkspaceManager

logger = logging.getLogger(__name__)

_shutdown = False


async def launch_agent(task: dict, client: OrchestratorClient,
                       containers: ContainerManager, workspace: WorkspaceManager,
                       config: WorkerConfig) -> None:
    task_id = task["task_id"]
    try:
        # Create local worktree
        wt_path = await workspace.create_worktree(
            repo=task["repo"],
            repo_url=task["repo_url"],
            branch_name=task["branch_name"],
        )

        # Report in_progress to refresh lease
        await client.report_in_progress(task_id, config.worker_id)

        # Start container
        env = {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
        }

        containers.start(
            task_id=task_id,
            worktree_path=str(wt_path),
            prompt=task["prompt"],
            system_prompt=task["system_prompt"],
            allowed_tools=task["allowed_tools"],
            image=task["image"],
            env=env,
        )
        logger.info("Launched agent for task %d (%s)", task_id, task["title"])

    except Exception as e:
        logger.exception("Failed to launch agent for task %d", task_id)
        await client.report_result(
            task_id=task_id,
            worker_id=config.worker_id,
            status="failed",
            output=f"Worker launch error: {e}",
            branch_name="",
            pr_url="",
        )


async def run_loop(config: WorkerConfig, client: OrchestratorClient,
                   containers: ContainerManager, workspace: WorkspaceManager) -> None:
    global _shutdown

    while not _shutdown:
        try:
            # 1. Heartbeat
            await client.heartbeat(
                config.worker_id,
                max_agents=config.max_agents,
                running=containers.running_count,
            )

            # 2. Claim tasks if capacity
            available = config.max_agents - containers.running_count
            if available > 0:
                tasks = await client.claim_tasks(
                    config.worker_id,
                    count=min(available, config.claim_batch_size),
                )
                for task in tasks:
                    await launch_agent(task, client, containers, workspace, config)

            # 3. Check completed containers
            for result in containers.collect_finished():
                await client.report_result(
                    task_id=result.task_id,
                    worker_id=config.worker_id,
                    status=result.status,
                    output=result.output,
                    branch_name=result.branch_name,
                    pr_url=result.pr_url,
                )
                logger.info("Task %d %s", result.task_id, result.status)

            # 4. Check timeouts
            killed = containers.check_timeouts()
            for task_id in killed:
                await client.report_result(
                    task_id=task_id,
                    worker_id=config.worker_id,
                    status="failed",
                    output="Agent timed out",
                    branch_name="",
                    pr_url="",
                )

        except Exception:
            logger.exception("Error in worker loop")

        await asyncio.sleep(config.poll_interval_seconds)


async def _async_start(config: WorkerConfig) -> None:
    global _shutdown
    _shutdown = False

    client = OrchestratorClient(config.orchestrator_url, config.auth_token)
    containers = ContainerManager(config.container)
    workspace = WorkspaceManager()

    # Cleanup orphaned containers from previous runs
    cleaned = containers.cleanup_orphans()
    if cleaned:
        logger.info("Cleaned up %d orphaned containers", cleaned)

    def handle_signal(sig, frame):
        global _shutdown
        _shutdown = True
        logger.info("Shutdown signal received, waiting for agents to finish...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    click.echo(f"Worker {config.worker_id} connected to {config.orchestrator_url}")
    click.echo(f"Max agents: {config.max_agents}")

    try:
        await run_loop(config, client, containers, workspace)
    finally:
        click.echo("Shutting down...")
        await client.close()


@click.group()
def cli():
    """Factory Worker — local agent execution daemon."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@cli.command()
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
def start(config_path):
    """Start the worker daemon."""
    path = Path(config_path) if config_path else None
    config = load_worker_config(path)
    if not config.worker_id:
        config.worker_id = socket.gethostname()
    asyncio.run(_async_start(config))


@cli.command()
@click.argument("task_id", type=int)
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
def claim(task_id, config_path):
    """Manually claim a specific task."""
    path = Path(config_path) if config_path else None
    config = load_worker_config(path)
    if not config.worker_id:
        config.worker_id = socket.gethostname()

    async def _claim():
        client = OrchestratorClient(config.orchestrator_url, config.auth_token)
        task = await client.claim_task(task_id, config.worker_id)
        if task:
            click.echo(f"Claimed task #{task_id}: {task['title']}")
        else:
            click.echo(f"Could not claim task #{task_id} (already claimed or not queued)")
        await client.close()

    asyncio.run(_claim())


@cli.command()
def status():
    """Show worker status (placeholder — requires running daemon)."""
    click.echo("Status command requires a running daemon. Use 'factory-worker start'.")


@cli.command()
@click.argument("stack", required=False)
def build(stack):
    """Build agent Docker images."""
    import subprocess

    worker_dir = Path(__file__).parent.parent.parent
    stacks = [stack] if stack else ["base", "php", "python", "node"]

    for s in stacks:
        dockerfile = worker_dir / f"Dockerfile.{s}"
        if not dockerfile.exists():
            click.echo(f"Dockerfile.{s} not found, skipping")
            continue
        tag = f"factory-agent:{s}"
        click.echo(f"Building {tag}...")
        subprocess.run(
            ["docker", "build", "-t", tag, "-f", str(dockerfile), str(worker_dir)],
            check=True,
        )
        click.echo(f"  {tag} built successfully")


if __name__ == "__main__":
    cli()
```

**Step 4: Run tests to verify they pass**

Run: `cd worker && python -m pytest tests/test_main.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add worker/src/factory_worker/main.py worker/tests/test_main.py
git commit -m "feat: implement worker main loop and CLI (start, claim, build, status)"
```

---

## Phase 3: Docker Images

### Task 13: Create agent Dockerfiles

**Files:**
- Create: `worker/Dockerfile.base`
- Create: `worker/Dockerfile.php`
- Create: `worker/Dockerfile.python`
- Create: `worker/Dockerfile.node`

**Step 1: Create Dockerfile.base**

```dockerfile
FROM node:22-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git ssh-client curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Git config for agent commits
RUN git config --global user.email "factory-agent@noreply.github.com" && \
    git config --global user.name "Factory Agent"

WORKDIR /workspace
ENTRYPOINT ["claude"]
```

**Step 2: Create Dockerfile.php**

```dockerfile
FROM factory-agent:base

# PHP 8.4 via Sury repo
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      lsb-release apt-transport-https && \
    curl -sSLo /tmp/debsuryorg-archive-keyring.deb \
      https://packages.sury.org/debsuryorg-archive-keyring.deb && \
    dpkg -i /tmp/debsuryorg-archive-keyring.deb && \
    echo "deb [signed-by=/usr/share/keyrings/deb.sury.org-php.gpg] https://packages.sury.org/php/ $(lsb_release -sc) main" \
      | tee /etc/apt/sources.list.d/sury-php.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      php8.4-cli php8.4-xml php8.4-mbstring php8.4-curl \
      php8.4-zip php8.4-sqlite3 php8.4-mysql unzip && \
    rm -rf /var/lib/apt/lists/* /tmp/*

# Composer
RUN curl -sS https://getcomposer.org/installer | php -- \
      --install-dir=/usr/local/bin --filename=composer
```

**Step 3: Create Dockerfile.python**

```dockerfile
FROM factory-agent:base

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

# uv (fast Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"
```

**Step 4: Create Dockerfile.node**

```dockerfile
# Node is already in the base image — this is just an alias
FROM factory-agent:base
```

**Step 5: Commit**

```bash
git add worker/Dockerfile.*
git commit -m "feat: add agent Docker images (base, php, python, node)"
```

---

## Phase 4: Integration & Smoke Test

### Task 14: Add image auto-detection to orchestrator

**Files:**
- Modify: `orchestrator/src/factory/api.py` (update `_detect_image`)
- Modify: `orchestrator/src/factory/workspace.py` (add detection helper)
- Test: `orchestrator/tests/test_image_detection.py` (new)

**Step 1: Write failing tests**

```python
# orchestrator/tests/test_image_detection.py
import pytest
from pathlib import Path
from unittest.mock import patch
from factory.api import _detect_image_from_path


def test_detect_php(tmp_path):
    (tmp_path / "composer.json").write_text("{}")
    assert _detect_image_from_path(tmp_path) == "factory-agent:php"


def test_detect_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]")
    assert _detect_image_from_path(tmp_path) == "factory-agent:python"


def test_detect_node(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert _detect_image_from_path(tmp_path) == "factory-agent:node"


def test_detect_fallback(tmp_path):
    assert _detect_image_from_path(tmp_path) == "factory-agent:base"


def test_detect_php_priority_over_node(tmp_path):
    (tmp_path / "composer.json").write_text("{}")
    (tmp_path / "package.json").write_text("{}")
    assert _detect_image_from_path(tmp_path) == "factory-agent:php"
```

**Step 2: Run test to verify it fails**

Run: `cd orchestrator && python -m pytest tests/test_image_detection.py -v`
Expected: FAIL — function doesn't exist

**Step 3: Implement _detect_image_from_path**

In `orchestrator/src/factory/api.py`:

```python
def _detect_image_from_path(repo_path: Path) -> str:
    if (repo_path / "composer.json").exists():
        return "factory-agent:php"
    if (repo_path / "pyproject.toml").exists() or (repo_path / "requirements.txt").exists():
        return "factory-agent:python"
    if (repo_path / "package.json").exists():
        return "factory-agent:node"
    return "factory-agent:base"
```

Update `_detect_image` to use the repo path when available (falls back to base if repo not cloned yet).

**Step 4: Run tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_image_detection.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/api.py orchestrator/tests/test_image_detection.py
git commit -m "feat: add image auto-detection from repo marker files"
```

---

### Task 15: Run full test suite and fix issues

**Step 1: Run all orchestrator tests**

Run: `cd orchestrator && python -m pytest tests/ -v`
Expected: All tests pass (existing + new)

**Step 2: Run all worker tests**

Run: `cd worker && python -m pytest tests/ -v`
Expected: All tests pass

**Step 3: Fix any failures**

Address any issues found. Likely candidates: import paths, missing fixtures in new tests, model field mismatches.

**Step 4: Commit fixes if any**

```bash
git commit -am "fix: resolve test failures from integration"
```

---

### Task 16: Build and test Docker images locally

**Step 1: Build all images**

Run: `cd worker && docker build -t factory-agent:base -f Dockerfile.base .`
Run: `cd worker && docker build -t factory-agent:php -f Dockerfile.php .`
Run: `cd worker && docker build -t factory-agent:python -f Dockerfile.python .`
Run: `cd worker && docker build -t factory-agent:node -f Dockerfile.node .`

**Step 2: Verify claude is available in base image**

Run: `docker run --rm factory-agent:base --version`
Expected: Claude Code version output

**Step 3: Verify PHP in php image**

Run: `docker run --rm --entrypoint php factory-agent:php --version`
Expected: PHP 8.4.x

**Step 4: Verify Python in python image**

Run: `docker run --rm --entrypoint python3 factory-agent:python --version`
Expected: Python 3.x

**Step 5: Commit any Dockerfile fixes**

```bash
git commit -am "fix: resolve Docker image build issues"
```

---

### Task 17: End-to-end smoke test

**Step 1: Start orchestrator locally for testing**

Run:
```bash
cd orchestrator
cp ../config.yml.example ./test-config.yml  # or create minimal config
python -m factory.main
```

**Step 2: Create a test task via API**

Run:
```bash
curl -X POST http://localhost:8100/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Test task", "repo": "factory", "agent_type": "coder"}'
```

**Step 3: Start worker and verify it claims the task**

Run:
```bash
cd worker
factory-worker start --config ./test-worker-config.yml
```

Expected: Worker connects, claims task, starts container

**Step 4: Verify task completion flow**

Check orchestrator logs for:
- Heartbeat received
- Task claimed
- Report received
- Task marked done/failed

**Step 5: Final commit**

```bash
git commit -am "test: verify end-to-end worker flow"
```
