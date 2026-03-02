import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient

from factory.db import Database
from factory.deps import get_db, get_orchestrator
from factory.main import app
from factory.models import TaskStatus
from factory.orchestrator import Orchestrator

TEST_AUTH_TOKEN = "test-secret-token"


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def mock_orchestrator():
    orch = MagicMock(spec=Orchestrator)

    # Worker tracking
    orch.register_heartbeat = MagicMock()
    orch.get_workers = MagicMock(return_value=[])

    # Prompt building
    orch._build_prompt = MagicMock(return_value="Generated prompt")

    # Completion handlers
    orch._handle_success = AsyncMock()
    orch._handle_failure = AsyncMock()

    # Config: repos
    repo_config = MagicMock()
    repo_config.url = "https://github.com/test/myapp.git"
    repo_config.image = "factory-agent:myapp"
    orch.config = MagicMock()
    orch.config.repos = {"myapp": repo_config}

    # Config: agent_templates
    template = MagicMock()
    template.system_prompt_file = ""
    template.allowed_tools = ["bash", "read"]
    template.timeout_minutes = 45
    orch.config.agent_templates = {"coder": template}

    # Config: plane (needed by existing create_task endpoint)
    orch.config.plane.default_repo = "myapp"

    # Config: orchestrator auth
    orch.config.orchestrator.auth_token = TEST_AUTH_TOKEN

    # Base dir
    orch.base_dir = Path("/tmp/factory-test")

    # Other mocks needed by existing endpoints
    orch.cancel_task = AsyncMock()
    orch.process_task = AsyncMock(return_value=True)
    orch.runner = MagicMock()
    orch.runner.get_running_agents.return_value = {}
    orch.plane = None

    return orch


@pytest.fixture
async def client(db, mock_orchestrator):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_AUTH_TOKEN}"},
    ) as c:
        yield c
    app.dependency_overrides.clear()


# ── Helper to create a queued task via the API ────────────────────────


async def _create_task(client, title="Fix login bug", repo="myapp"):
    resp = await client.post(
        "/api/tasks", json={"title": title, "repo": repo, "agent_type": "coder"}
    )
    assert resp.status_code == 201
    return resp.json()


# ── Heartbeat tests ──────────────────────────────────────────────────


async def test_heartbeat(client, mock_orchestrator):
    resp = await client.post(
        "/api/workers/heartbeat",
        json={"worker_id": "w-1", "max_agents": 4, "running": 1},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_orchestrator.register_heartbeat.assert_called_once_with("w-1", 4, 1)


# ── List workers tests ───────────────────────────────────────────────


async def test_list_workers(client, mock_orchestrator):
    now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    worker = MagicMock()
    worker.worker_id = "w-1"
    worker.max_agents = 4
    worker.running = 2
    worker.last_heartbeat = now
    worker.is_alive = True
    worker.has_capacity = True
    mock_orchestrator.get_workers.return_value = [worker]

    resp = await client.get("/api/workers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["worker_id"] == "w-1"
    assert data[0]["max_agents"] == 4
    assert data[0]["running"] == 2
    assert data[0]["is_alive"] is True
    assert data[0]["has_capacity"] is True
    assert data[0]["last_heartbeat"] == now.isoformat()


async def test_list_workers_empty(client, mock_orchestrator):
    mock_orchestrator.get_workers.return_value = []
    resp = await client.get("/api/workers")
    assert resp.status_code == 200
    assert resp.json() == []


# ── Claim tasks tests ────────────────────────────────────────────────


async def test_claim_tasks(client, db, mock_orchestrator):
    task_data = await _create_task(client)
    task_id = task_data["id"]

    resp = await client.post(
        "/api/tasks/claim", json={"worker_id": "w-1", "count": 1}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tasks"]) == 1
    payload = data["tasks"][0]
    assert payload["task_id"] == task_id
    assert payload["title"] == "Fix login bug"
    assert payload["repo"] == "myapp"
    assert payload["repo_url"] == "https://github.com/test/myapp.git"
    assert payload["image"] == "factory-agent:myapp"
    assert payload["prompt"] == "Generated prompt"
    assert payload["allowed_tools"] == ["bash", "read"]
    assert payload["timeout_minutes"] == 45
    assert "agent/task-" in payload["branch_name"]


async def test_claim_tasks_empty(client, mock_orchestrator):
    """No queued tasks means empty list returned."""
    resp = await client.post(
        "/api/tasks/claim", json={"worker_id": "w-1", "count": 5}
    )
    assert resp.status_code == 200
    assert resp.json()["tasks"] == []


# ── Claim specific task tests ────────────────────────────────────────


async def test_claim_specific_task(client, db, mock_orchestrator):
    task_data = await _create_task(client)
    task_id = task_data["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/claim", json={"worker_id": "w-1"}
    )
    assert resp.status_code == 200
    payload = resp.json()["task"]
    assert payload["task_id"] == task_id
    assert payload["title"] == "Fix login bug"
    assert payload["repo_url"] == "https://github.com/test/myapp.git"


async def test_claim_already_claimed(client, db, mock_orchestrator):
    task_data = await _create_task(client)
    task_id = task_data["id"]

    # First claim succeeds
    resp1 = await client.post(
        f"/api/tasks/{task_id}/claim", json={"worker_id": "w-1"}
    )
    assert resp1.status_code == 200

    # Second claim should fail with 409
    resp2 = await client.post(
        f"/api/tasks/{task_id}/claim", json={"worker_id": "w-2"}
    )
    assert resp2.status_code == 409
    assert "already claimed" in resp2.json()["detail"]


async def test_claim_specific_task_not_found(client, mock_orchestrator):
    resp = await client.post(
        "/api/tasks/9999/claim", json={"worker_id": "w-1"}
    )
    assert resp.status_code == 404


# ── Report tests ─────────────────────────────────────────────────────


async def test_report_success(client, db, mock_orchestrator):
    task_data = await _create_task(client)
    task_id = task_data["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/report",
        json={
            "worker_id": "w-1",
            "status": "done",
            "output": "All tests pass",
            "branch_name": "agent/task-1-fix-login",
            "pr_url": "https://github.com/test/myapp/pull/42",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_orchestrator._handle_success.assert_awaited_once_with(task_id, "All tests pass")

    # Verify fields were updated in DB
    task = await db.get_task(task_id)
    assert task.branch_name == "agent/task-1-fix-login"
    assert task.pr_url == "https://github.com/test/myapp/pull/42"


async def test_report_failure(client, db, mock_orchestrator):
    task_data = await _create_task(client)
    task_id = task_data["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/report",
        json={
            "worker_id": "w-1",
            "status": "failed",
            "output": "Compilation error",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    mock_orchestrator._handle_failure.assert_awaited_once_with(task_id, "Compilation error")


async def test_report_in_progress(client, db, mock_orchestrator):
    task_data = await _create_task(client)
    task_id = task_data["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/report",
        json={"worker_id": "w-1", "status": "in_progress"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    # Neither success nor failure handler should be called
    mock_orchestrator._handle_success.assert_not_awaited()
    mock_orchestrator._handle_failure.assert_not_awaited()

    # Task status should be in_progress
    task = await db.get_task(task_id)
    assert task.status == TaskStatus.IN_PROGRESS


async def test_report_rejects_wrong_worker(client, db, mock_orchestrator):
    """A worker that doesn't own the task cannot report results."""
    task_data = await _create_task(client)
    task_id = task_data["id"]

    # Claim as worker w-1
    await client.post(f"/api/tasks/{task_id}/claim", json={"worker_id": "w-1"})

    # Try to report as worker w-2
    resp = await client.post(
        f"/api/tasks/{task_id}/report",
        json={"worker_id": "w-2", "status": "done", "output": "hacked"},
    )
    assert resp.status_code == 403
    assert "w-1" in resp.json()["detail"]


async def test_report_task_not_found(client, mock_orchestrator):
    resp = await client.post(
        "/api/tasks/9999/report",
        json={"worker_id": "w-1", "status": "done", "output": ""},
    )
    assert resp.status_code == 404


# ── Authentication tests ────────────────────────────────────────────


@pytest.fixture
async def unauthed_client(db, mock_orchestrator):
    """Client without auth headers for testing rejection."""
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


async def test_heartbeat_rejects_no_token(unauthed_client):
    resp = await unauthed_client.post(
        "/api/workers/heartbeat",
        json={"worker_id": "w-1", "max_agents": 4, "running": 1},
    )
    assert resp.status_code == 401


async def test_claim_rejects_wrong_token(db, mock_orchestrator):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": "Bearer wrong-token"},
    ) as c:
        resp = await c.post(
            "/api/tasks/claim", json={"worker_id": "w-1", "count": 1}
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


async def test_report_rejects_no_token(unauthed_client):
    resp = await unauthed_client.post(
        "/api/tasks/1/report",
        json={"worker_id": "w-1", "status": "done", "output": ""},
    )
    assert resp.status_code == 401


async def test_worker_auth_disabled_when_no_token(db, mock_orchestrator):
    """When auth_token is empty, all requests pass through."""
    mock_orchestrator.config.orchestrator.auth_token = ""
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_orchestrator] = lambda: mock_orchestrator
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post(
            "/api/workers/heartbeat",
            json={"worker_id": "w-1", "max_agents": 4, "running": 1},
        )
    app.dependency_overrides.clear()
    assert resp.status_code == 200
