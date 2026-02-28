import pytest
from unittest.mock import AsyncMock, patch

import httpx

from factory_worker.client import OrchestratorClient


def _make_response(status_code: int, json: dict) -> httpx.Response:
    """Create an httpx.Response with a dummy request so raise_for_status works."""
    request = httpx.Request("POST", "http://test")
    return httpx.Response(status_code, json=json, request=request)


@pytest.fixture
def client():
    return OrchestratorClient(base_url="http://localhost:8100/api", auth_token="test-token")


@pytest.mark.asyncio
async def test_heartbeat(client):
    mock_response = _make_response(200, {})
    client._http.post = AsyncMock(return_value=mock_response)

    await client.heartbeat(worker_id="w1", max_agents=4, running=2)

    client._http.post.assert_called_once_with("/workers/heartbeat", json={
        "worker_id": "w1",
        "max_agents": 4,
        "running": 2,
    })


@pytest.mark.asyncio
async def test_claim_tasks(client):
    mock_response = _make_response(200, {"tasks": [{"id": 1}, {"id": 2}]})
    client._http.post = AsyncMock(return_value=mock_response)

    tasks = await client.claim_tasks(worker_id="w1", count=2)

    assert tasks == [{"id": 1}, {"id": 2}]
    client._http.post.assert_called_once_with("/tasks/claim", json={
        "worker_id": "w1",
        "count": 2,
    })


@pytest.mark.asyncio
async def test_claim_tasks_empty(client):
    mock_response = _make_response(200, {"tasks": []})
    client._http.post = AsyncMock(return_value=mock_response)

    tasks = await client.claim_tasks(worker_id="w1", count=3)

    assert tasks == []


@pytest.mark.asyncio
async def test_claim_task_success(client):
    mock_response = _make_response(200, {"task": {"id": 5, "prompt": "do stuff"}})
    client._http.post = AsyncMock(return_value=mock_response)

    task = await client.claim_task(task_id=5, worker_id="w1")

    assert task == {"id": 5, "prompt": "do stuff"}


@pytest.mark.asyncio
async def test_claim_task_conflict(client):
    mock_response = _make_response(409, {"error": "already claimed"})
    client._http.post = AsyncMock(return_value=mock_response)

    task = await client.claim_task(task_id=5, worker_id="w1")

    assert task is None


@pytest.mark.asyncio
async def test_report_result(client):
    mock_response = _make_response(200, {})
    client._http.post = AsyncMock(return_value=mock_response)

    await client.report_result(
        task_id=1, worker_id="w1", status="done",
        output="all good", branch_name="agent/task-1-feat", pr_url="https://github.com/org/repo/pull/42",
    )

    client._http.post.assert_called_once_with("/tasks/1/report", json={
        "worker_id": "w1",
        "status": "done",
        "output": "all good",
        "branch_name": "agent/task-1-feat",
        "pr_url": "https://github.com/org/repo/pull/42",
    })


@pytest.mark.asyncio
async def test_report_in_progress(client):
    mock_response = _make_response(200, {})
    client._http.post = AsyncMock(return_value=mock_response)

    await client.report_in_progress(task_id=3, worker_id="w1")

    client._http.post.assert_called_once_with("/tasks/3/report", json={
        "worker_id": "w1",
        "status": "in_progress",
        "output": "",
        "branch_name": "",
        "pr_url": "",
    })
