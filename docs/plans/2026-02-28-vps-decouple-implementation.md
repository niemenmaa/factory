# VPS Decoupling Implementation Plan

**Goal:** Remove all hardcoded VPS-specific values (`/opt/factory`, `preview.factory.6a.fi`, `systemd-run`) so Factory can deploy to any VPS or local machine via configuration.

**Architecture:** Replace module-level constants with config-driven values. The `FACTORY_HOME` env var sets the root directory (defaults to cwd). Docker domain/network move to `config.yml`. Deploy command becomes configurable. All existing defaults match current values for backward compatibility.

**Tech Stack:** Python 3.12, Pydantic, FastAPI, pytest

---

### Task 1: Add `DockerConfig` and `DeployConfig` to config model

**Files:**
- Modify: `orchestrator/src/factory/config.py`
- Modify: `orchestrator/tests/test_config.py`

**Step 1: Write the failing tests**

In `orchestrator/tests/test_config.py`, add:

```python
def test_docker_config_defaults():
    cfg_text = """
orchestrator:
  port: 8100
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cfg_text)
        f.flush()
        config = load_config(Path(f.name))

    assert config.docker.preview_domain == "preview.factory.6a.fi"
    assert config.docker.network == "factory-preview"


def test_docker_config_custom():
    cfg_text = """
docker:
  preview_domain: "preview.myserver.com"
  network: "my-network"
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cfg_text)
        f.flush()
        config = load_config(Path(f.name))

    assert config.docker.preview_domain == "preview.myserver.com"
    assert config.docker.network == "my-network"


def test_deploy_config_defaults():
    cfg_text = """
orchestrator:
  port: 8100
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cfg_text)
        f.flush()
        config = load_config(Path(f.name))

    assert config.deploy.command == []


def test_deploy_config_custom():
    cfg_text = """
deploy:
  command: ["systemd-run", "--scope", "/opt/factory/deploy.sh"]
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(cfg_text)
        f.flush()
        config = load_config(Path(f.name))

    assert config.deploy.command == ["systemd-run", "--scope", "/opt/factory/deploy.sh"]
```

**Step 2: Run tests to verify they fail**

Run: `cd orchestrator && python -m pytest tests/test_config.py -v`
Expected: FAIL — `Config` has no `docker` or `deploy` attribute

**Step 3: Add the config models**

In `orchestrator/src/factory/config.py`, add before the `Config` class:

```python
class DockerConfig(BaseModel):
    preview_domain: str = "preview.factory.6a.fi"
    network: str = "factory-preview"


class DeployConfig(BaseModel):
    command: list[str] = []
```

And add to the `Config` class:

```python
    docker: DockerConfig = DockerConfig()
    deploy: DeployConfig = DeployConfig()
```

**Step 4: Run tests to verify they pass**

Run: `cd orchestrator && python -m pytest tests/test_config.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/config.py orchestrator/tests/test_config.py
git commit -m "feat: add DockerConfig and DeployConfig to config model"
```

---

### Task 2: Make `main.py` use `FACTORY_HOME` env var

**Files:**
- Modify: `orchestrator/src/factory/main.py`
- Modify: `orchestrator/tests/test_health.py` (verify existing test still passes)

**Step 1: Write the failing test**

In `orchestrator/tests/test_health.py`, add (or create a new `test_main.py`):

```python
def test_factory_home_defaults_to_cwd(monkeypatch, tmp_path):
    """FACTORY_HOME env var controls config and db paths."""
    monkeypatch.delenv("FACTORY_HOME", raising=False)
    # Just verify the import and env var reading works
    import factory.main as m
    import importlib
    monkeypatch.chdir(tmp_path)
    # Write a minimal config
    (tmp_path / "config.yml").write_text("orchestrator:\n  port: 8100\n")
    # The factory_home resolution is tested via the lifespan
```

Actually, `main.py` is the FastAPI entrypoint and the lifespan function is what uses the paths. The simplest approach: just make the change and verify the existing health test still passes. The config/db paths are injected into `init_services` which is already tested.

**Step 1: Modify `main.py`**

Replace the hardcoded paths in `main.py`:

```python
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from factory.api import router
from factory.deps import init_services, shutdown_services

STATIC_DIR = Path(__file__).parent / "static"

# Resolve factory home directory from env or cwd
_factory_home = Path(os.environ.get("FACTORY_HOME", ".")).resolve()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_services(
        config_path=str(_factory_home / "config.yml"),
        db_path=str(_factory_home / "factory.db"),
    )
    yield
    await shutdown_services()
```

Everything else in the file stays the same.

**Step 2: Run existing tests to verify nothing breaks**

Run: `cd orchestrator && python -m pytest tests/test_health.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add orchestrator/src/factory/main.py
git commit -m "feat: use FACTORY_HOME env var instead of hardcoded /opt/factory in main"
```

---

### Task 3: Replace `FACTORY_ROOT` with `self.base_dir` in orchestrator

**Files:**
- Modify: `orchestrator/src/factory/orchestrator.py`
- Modify: `orchestrator/tests/test_orchestrator.py`

**Step 1: Write the failing test**

In `orchestrator/tests/test_orchestrator.py`, add:

```python
@patch("factory.orchestrator.RepoManager")
@patch("factory.orchestrator.AgentRunner")
async def test_orchestrator_uses_base_dir(MockRunner, MockRepoMgr):
    """Orchestrator should use base_dir, not hardcoded /opt/factory."""
    db = Database(":memory:")
    await db.initialize()

    config = Config()
    custom_dir = Path("/tmp/my-factory")

    orch = Orchestrator(db=db, config=config, base_dir=custom_dir)

    # RepoManager should receive base_dir-relative paths
    MockRepoMgr.assert_called_once_with(
        repos_dir=custom_dir / "repos",
        worktrees_dir=custom_dir / "worktrees",
    )

    await db.close()
```

**Step 2: Run test to verify it fails**

Run: `cd orchestrator && python -m pytest tests/test_orchestrator.py::test_orchestrator_uses_base_dir -v`
Expected: FAIL — `RepoManager` called with `/opt/factory/repos` instead of `/tmp/my-factory/repos`

**Step 3: Make the changes**

In `orchestrator/src/factory/orchestrator.py`:

1. Remove line 24: `FACTORY_ROOT = Path("/opt/factory")`

2. Change the default parameter on line 34 from `base_dir: Path = FACTORY_ROOT` to `base_dir: Path | None = None`. Handle the default inside `__init__`:

```python
def __init__(self, db: Database, config: Config, memory: AgentMemory | None = None, base_dir: Path | None = None):
    self.db = db
    self.config = config
    self.memory = memory
    self.base_dir = base_dir or Path(".").resolve()
    self.repo_manager = RepoManager(
        repos_dir=self.base_dir / "repos",
        worktrees_dir=self.base_dir / "worktrees",
    )
```

3. Replace all remaining `FACTORY_ROOT` usages with `self.base_dir`:

   - Line 680: `wt_path = self.base_dir / "worktrees" / task.branch_name.replace("/", "-")`
   - Line 809: `wt_path = self.base_dir / "worktrees" / task.branch_name.replace("/", "-")`
   - Line 1188: `wt_path = self.base_dir / "worktrees" / task.branch_name.replace("/", "-")`

**Step 4: Run all orchestrator tests**

Run: `cd orchestrator && python -m pytest tests/test_orchestrator.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/orchestrator.py orchestrator/tests/test_orchestrator.py
git commit -m "feat: replace hardcoded FACTORY_ROOT with configurable base_dir"
```

---

### Task 4: Make Docker Toolkit use config values

**Files:**
- Modify: `orchestrator/src/factory/docker_toolkit.py`
- Modify: `orchestrator/tests/test_docker_toolkit.py`

**Step 1: Write the failing test**

In `orchestrator/tests/test_docker_toolkit.py`, add:

```python
class TestCustomDomain:
    def test_custom_preview_domain(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp", preview_domain="preview.myserver.com")
        assert env.get_url() == "https://task-42.preview.myserver.com"

    def test_custom_domain_in_traefik_labels(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp", preview_domain="preview.myserver.com")
        labels = env.get_traefik_labels(service_port=3000)
        assert labels["traefik.http.routers.task-42.rule"] == "Host(`task-42.preview.myserver.com`)"

    def test_custom_network(self):
        env = DockerEnvironment(task_id=42, repo="acme/webapp", network="my-network")
        assert env.network == "my-network"

    def test_defaults_match_current_values(self):
        env = DockerEnvironment(task_id=1, repo="r")
        assert env.preview_domain == "preview.factory.6a.fi"
        assert env.network == "factory-preview"
```

**Step 2: Run test to verify it fails**

Run: `cd orchestrator && python -m pytest tests/test_docker_toolkit.py::TestCustomDomain -v`
Expected: FAIL — `DockerEnvironment.__init__` doesn't accept `preview_domain` or `network`

**Step 3: Modify `DockerEnvironment`**

In `orchestrator/src/factory/docker_toolkit.py`:

1. Add `preview_domain` and `network` parameters to `__init__`:

```python
def __init__(
    self,
    task_id: int,
    repo: str,
    pr_number: Optional[int] = None,
    preview_domain: str = PREVIEW_DOMAIN,
    network: str = FACTORY_NETWORK,
):
    self.task_id = task_id
    self.repo = repo
    self.pr_number = pr_number
    self.project_name = f"factory-task-{task_id}"
    self.env_type = "preview" if pr_number is not None else "test"
    self.preview_domain = preview_domain
    self.network = network
```

2. Replace `PREVIEW_DOMAIN` with `self.preview_domain` in `_get_hostname`:

```python
def _get_hostname(self) -> str:
    if self.pr_number is not None:
        return f"pr-{self.pr_number}.{self.preview_domain}"
    return f"task-{self.task_id}.{self.preview_domain}"
```

3. Replace `FACTORY_NETWORK` with `self.network` in `_connect_to_network`:

```python
def _connect_to_network(self) -> None:
    result = subprocess.run(
        [
            "docker", "ps", "-q",
            "--filter", f"label=com.docker.compose.project={self.project_name}",
        ],
        capture_output=True, text=True,
    )
    container_ids = result.stdout.strip().split("\n")
    for cid in container_ids:
        if cid:
            subprocess.run(
                ["docker", "network", "connect", self.network, cid],
                capture_output=True,
            )
```

4. Update `spin_up` to pass `self.network` to `_ensure_network`:

```python
_ensure_network(self.network)
```

5. Update `_ensure_network` to accept a `network` parameter:

```python
def _ensure_network(network: str = FACTORY_NETWORK) -> None:
    result = subprocess.run(
        ["docker", "network", "ls", "--filter", f"name=^{network}$", "--format", "{{.Name}}"],
        capture_output=True, text=True,
    )
    if network not in result.stdout:
        logger.info("Creating Docker network: %s", network)
        subprocess.run(["docker", "network", "create", network], check=True, capture_output=True)
```

**Step 4: Run all docker toolkit tests**

Run: `cd orchestrator && python -m pytest tests/test_docker_toolkit.py -v`
Expected: All PASS (existing tests use defaults, new tests verify custom values)

**Step 5: Commit**

```bash
git add orchestrator/src/factory/docker_toolkit.py orchestrator/tests/test_docker_toolkit.py
git commit -m "feat: make Docker Toolkit domain and network configurable"
```

---

### Task 5: Make deploy webhook use config

**Files:**
- Modify: `orchestrator/src/factory/api.py`
- Modify: `orchestrator/tests/test_api.py`

**Step 1: Write the failing test**

In `orchestrator/tests/test_api.py`, find or add a test for the deploy webhook. Add:

```python
@patch("factory.api.subprocess.Popen")
async def test_github_webhook_uses_config_deploy_command(mock_popen, client, monkeypatch):
    """Deploy webhook should use command from config, not hardcoded systemd-run."""
    # Configure a custom deploy command
    from factory.deps import get_orchestrator
    orch = get_orchestrator()
    orch.config.deploy.command = ["/usr/local/bin/deploy.sh"]

    payload = {"ref": "refs/heads/main"}
    secret = "test-secret"
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)

    import hmac, hashlib, json
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    resp = await client.post(
        "/api/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert cmd == ["/usr/local/bin/deploy.sh"]
```

**Step 2: Run test to verify it fails**

Run: `cd orchestrator && python -m pytest tests/test_api.py::test_github_webhook_uses_config_deploy_command -v`
Expected: FAIL

**Step 3: Modify `api.py`**

In the `github_webhook` handler, replace the hardcoded `subprocess.Popen` call. The handler needs access to the orchestrator's config. Import `get_orchestrator` from deps:

```python
    orch = get_orchestrator()
    if not orch.config.deploy.command:
        raise HTTPException(status_code=404, detail="Deploy not configured")

    subprocess.Popen(
        orch.config.deploy.command,
        start_new_session=True,
    )
    logger.info("Deploy triggered by push to main (spawned %s)", orch.config.deploy.command[0])
```

**Step 4: Run tests**

Run: `cd orchestrator && python -m pytest tests/test_api.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add orchestrator/src/factory/api.py orchestrator/tests/test_api.py
git commit -m "feat: make deploy webhook command configurable via config"
```

---

### Task 6: Parameterize `deploy.sh`

**Files:**
- Modify: `deploy.sh`

**Step 1: Replace hardcoded paths with env vars**

Replace the top of `deploy.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

FACTORY_DIR="${FACTORY_HOME:-/opt/factory}"
LOCKFILE="$FACTORY_DIR/deploy.lock"
LOGFILE="$FACTORY_DIR/deploy.log"
ORCH_DIR="$FACTORY_DIR/orchestrator"
VENV="$FACTORY_DIR/.venv"
API_URL="${FACTORY_API_URL:-http://localhost:8100/api}"
POLL_INTERVAL=15
POLL_TIMEOUT=2100  # 35 minutes
```

The `systemctl` commands at the bottom also need configuring. Replace the restart/verify section:

```bash
# Restart the service
SERVICE_NAME="${FACTORY_SERVICE:-factory-orchestrator}"
log "Restarting $SERVICE_NAME..."
systemctl restart "$SERVICE_NAME"

# Wait a moment and verify
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "=== Deploy successful ==="
else
    log "ERROR: $SERVICE_NAME failed to start!"
    systemctl status "$SERVICE_NAME" || true
    exit 1
fi
```

**Step 2: Verify the script is valid bash**

Run: `bash -n deploy.sh`
Expected: No errors

**Step 3: Commit**

```bash
git add deploy.sh
git commit -m "feat: parameterize deploy.sh with FACTORY_HOME, FACTORY_API_URL, FACTORY_SERVICE env vars"
```

---

### Task 7: Update `.env.example` and README

**Files:**
- Modify: `.env.example`
- Modify: `README.md` (update deployment section if it references hardcoded paths)

**Step 1: Add new env vars to `.env.example`**

Add at the top:

```bash
# Factory root directory (defaults to current working directory)
# FACTORY_HOME=/opt/factory

# Deploy script settings (optional)
# FACTORY_API_URL=http://localhost:8100/api
# FACTORY_SERVICE=factory-orchestrator
```

**Step 2: Update README deployment instructions**

Search for `/opt/factory` in README.md and replace with `$FACTORY_HOME` or mention the env var. Keep the default `/opt/factory` as an example.

**Step 3: Commit**

```bash
git add .env.example README.md
git commit -m "docs: document FACTORY_HOME and configurable paths"
```

---

### Task 8: Run full test suite and verify

**Step 1: Run all tests**

Run: `cd orchestrator && python -m pytest tests/ -v`
Expected: All PASS

**Step 2: Verify no remaining hardcoded `/opt/factory` in Python source**

Run: `grep -r "/opt/factory" orchestrator/src/`
Expected: No matches

**Step 3: Verify no remaining hardcoded `preview.factory.6a.fi` in Python source (outside defaults)**

Run: `grep -r "preview.factory.6a.fi" orchestrator/src/`
Expected: Only in `config.py` default value and `docker_toolkit.py` module constant (kept for backward compat)
