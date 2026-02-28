import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock
from factory.db import Database
from factory.models import TaskCreate, TaskStatus
from factory.config import Config, ExecutionConfig, RepoConfig, AgentTemplateConfig
from factory.orchestrator import Orchestrator


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

    # Task should still be queued (not in_progress)
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
