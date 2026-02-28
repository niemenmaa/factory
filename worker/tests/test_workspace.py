from pathlib import Path

from factory_worker.workspace import WorkspaceManager


def test_repo_cache_path():
    manager = WorkspaceManager(cache_dir=Path("/tmp/factory-test"))
    path = manager._repo_path("myorg/myrepo")
    assert path == Path("/tmp/factory-test/repos/myorg/myrepo")


def test_worktree_path():
    manager = WorkspaceManager(cache_dir=Path("/tmp/factory-test"))
    path = manager._worktree_path("myorg/myrepo", "agent/task-1-fix-bug")
    assert path == Path("/tmp/factory-test/worktrees/agent/task-1-fix-bug")
