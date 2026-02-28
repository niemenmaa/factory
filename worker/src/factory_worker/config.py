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
    if "auth_token" in data and isinstance(data["auth_token"], str) and data["auth_token"].startswith("${"):
        var_name = data["auth_token"].strip("${}")
        data["auth_token"] = os.environ.get(var_name, "")
    return WorkerConfig(**data)
