import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from factory_worker.main import run_loop
from factory_worker.config import WorkerConfig


class _BreakLoop(Exception):
    pass


async def test_run_loop_single_iteration():
    config = WorkerConfig(worker_id="test-worker", orchestrator_url="http://test:8100/api",
                          auth_token="token", max_agents=4, poll_interval_seconds=1)
    mock_client = AsyncMock()
    mock_client.heartbeat = AsyncMock()
    mock_client.claim_tasks = AsyncMock(return_value=[])
    mock_containers = MagicMock()
    mock_containers.running_count = 0
    mock_containers.collect_finished.return_value = []
    mock_containers.check_timeouts.return_value = []
    mock_workspace = AsyncMock()

    with patch("factory_worker.main.asyncio.sleep", side_effect=_BreakLoop):
        with pytest.raises(_BreakLoop):
            await run_loop(config, mock_client, mock_containers, mock_workspace)

    mock_client.heartbeat.assert_called_once_with("test-worker", max_agents=4, running=0)
    mock_client.claim_tasks.assert_called_once_with("test-worker", count=3)
