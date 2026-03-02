import logging
import os
from pathlib import Path

from fastapi import Depends, HTTPException, Request

from factory.config import Config, load_config
from factory.db import Database
from factory.memory import AgentMemory
from factory.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

_db: Database | None = None
_orchestrator: Orchestrator | None = None
_memory: AgentMemory | None = None


async def _init_memory() -> AgentMemory | None:
    url = os.environ.get("SURREALDB_URL", "")
    user = os.environ.get("SURREALDB_USER", "")
    password = os.environ.get("SURREALDB_PASS", "")
    if not (url and user and password):
        logger.info("SurrealDB env vars not set, agent memory disabled")
        return None
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    memory = AgentMemory(url=url, user=user, password=password, openai_api_key=openai_api_key)
    try:
        await memory.initialize()
        return memory
    except Exception as e:
        logger.warning("Failed to initialize agent memory: %s", e)
        return None


async def init_services(config_path: str, db_path: str):
    global _db, _orchestrator, _memory
    config_path = Path(config_path)
    config = load_config(config_path)
    _db = Database(db_path)
    await _db.initialize()
    _memory = await _init_memory()
    _orchestrator = Orchestrator(db=_db, config=config, memory=_memory, base_dir=config_path.parent)
    await _orchestrator.recover_orphaned_tasks()
    await _orchestrator.recover_orphaned_workflows()
    _orchestrator.start_polling()


async def shutdown_services():
    global _db, _orchestrator, _memory
    if _orchestrator:
        await _orchestrator.close()
    if _memory:
        await _memory.close()
    if _db:
        await _db.close()


def get_db() -> Database:
    assert _db is not None, "Database not initialized"
    return _db


def get_orchestrator() -> Orchestrator:
    assert _orchestrator is not None, "Orchestrator not initialized"
    return _orchestrator


def get_memory() -> AgentMemory | None:
    return _memory


async def verify_worker_token(
    request: Request,
    orch: "Orchestrator" = Depends(get_orchestrator),
) -> None:
    """Validate Bearer token on worker protocol endpoints."""
    expected = orch.config.orchestrator.auth_token
    if not expected:
        return  # No token configured — auth disabled
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth.removeprefix("Bearer ")
    if token != expected:
        raise HTTPException(status_code=403, detail="Invalid token")
