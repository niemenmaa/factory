import pytest
from unittest.mock import MagicMock, patch

from factory_worker.config import ContainerConfig
from factory_worker.container import ContainerManager, AgentResult, _parse_output


@pytest.fixture
def config():
    return ContainerConfig()


@patch("factory_worker.container.docker.from_env")
def test_running_count(mock_docker_env, config):
    mock_docker_env.return_value = MagicMock()
    manager = ContainerManager(config)
    assert manager.running_count == 0


@patch("factory_worker.container.docker.from_env")
def test_start_creates_container(mock_docker_env, config):
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    mock_container = MagicMock()
    mock_client.containers.run.return_value = mock_container

    manager = ContainerManager(config)
    manager.start(
        task_id=1, worktree_path="/tmp/wt", prompt="Fix the bug",
        system_prompt="You are helpful", allowed_tools=["Bash", "Read"],
        image="factory-agent:latest", env={"API_KEY": "secret"},
    )

    mock_client.containers.run.assert_called_once()
    call_args = mock_client.containers.run.call_args
    assert call_args[0][0] == "factory-agent:latest"
    assert "Fix the bug" in call_args[0][1]
    assert "--dangerously-skip-permissions" in call_args[0][1]
    assert "--system-prompt" in call_args[0][1]
    assert "--allowedTools" in call_args[0][1]
    assert call_args[1]["name"] == "factory-agent-1"
    assert call_args[1]["detach"] is True
    assert manager.running_count == 1


@patch("factory_worker.container.docker.from_env")
def test_collect_finished_running(mock_docker_env, config):
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client

    manager = ContainerManager(config)

    mock_container = MagicMock()
    mock_container.status = "running"
    manager._running[1] = {
        "container": mock_container,
        "task_id": 1,
        "started_at": MagicMock(),
    }

    results = manager.collect_finished()
    assert results == []
    assert manager.running_count == 1


@patch("factory_worker.container.docker.from_env")
def test_collect_finished_exited(mock_docker_env, config):
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client

    manager = ContainerManager(config)

    mock_container = MagicMock()
    mock_container.status = "exited"
    mock_container.attrs = {"State": {"ExitCode": 0}}
    mock_container.logs.return_value = b"Done. Created https://github.com/org/repo/pull/42 on branch agent/task-1-fix-bug"

    manager._running[1] = {
        "container": mock_container,
        "task_id": 1,
        "started_at": MagicMock(),
    }

    results = manager.collect_finished()
    assert len(results) == 1
    assert results[0].task_id == 1
    assert results[0].status == "done"
    assert results[0].pr_url == "https://github.com/org/repo/pull/42"
    assert results[0].branch_name == "agent/task-1-fix-bug"
    assert manager.running_count == 0


def test_parse_output():
    output = (
        "Working on task...\n"
        "Created PR: https://github.com/myorg/myrepo/pull/123\n"
        "Branch: agent/task-42-add-feature\n"
        "Done."
    )
    branch, pr_url = _parse_output(output)
    assert pr_url == "https://github.com/myorg/myrepo/pull/123"
    assert branch == "agent/task-42-add-feature"


def test_parse_output_no_match():
    output = "Just some regular output with no links"
    branch, pr_url = _parse_output(output)
    assert pr_url == ""
    assert branch == ""


@patch("factory_worker.container.docker.from_env")
def test_start_without_ssh_dir(mock_docker_env):
    """When ssh_dir is empty, only workspace volume is mounted."""
    config = ContainerConfig(ssh_dir="")
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    mock_client.containers.run.return_value = MagicMock()

    manager = ContainerManager(config)
    manager.start(
        task_id=1, worktree_path="/tmp/wt", prompt="Fix bug",
        system_prompt="", allowed_tools=[], image="img:latest", env={},
    )

    call_kwargs = mock_client.containers.run.call_args[1]
    assert call_kwargs["volumes"] == {
        "/tmp/wt": {"bind": "/workspace", "mode": "rw"},
    }


@patch("factory_worker.container.docker.from_env")
def test_start_with_ssh_dir(mock_docker_env):
    """When ssh_dir is set, SSH directory is mounted read-only alongside workspace."""
    config = ContainerConfig(ssh_dir="/home/user/.ssh")
    mock_client = MagicMock()
    mock_docker_env.return_value = mock_client
    mock_client.containers.run.return_value = MagicMock()

    manager = ContainerManager(config)
    manager.start(
        task_id=2, worktree_path="/tmp/wt", prompt="Fix bug",
        system_prompt="", allowed_tools=[], image="img:latest", env={},
    )

    call_kwargs = mock_client.containers.run.call_args[1]
    assert call_kwargs["volumes"] == {
        "/tmp/wt": {"bind": "/workspace", "mode": "rw"},
        "/home/user/.ssh": {"bind": "/root/.ssh", "mode": "ro"},
    }
