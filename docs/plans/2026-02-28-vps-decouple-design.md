# Design: Make Factory VPS-Agnostic

**Date:** 2026-02-28
**Branch:** `feature/vps-decouple`
**Goal:** Remove hardcoded VPS-specific values so Factory can deploy to any VPS or local machine without code changes.

## Problem

Four hardcoded values tie Factory to a specific VPS at `/opt/factory` on `reitti.6a.fi`:

1. `FACTORY_ROOT = Path("/opt/factory")` in `orchestrator.py` — used for repos, worktrees, and prompt loading
2. `/opt/factory/config.yml` and `/opt/factory/factory.db` in `main.py`
3. `PREVIEW_DOMAIN = "preview.factory.6a.fi"` in `docker_toolkit.py`
4. `systemd-run --scope ... /opt/factory/deploy.sh` in `api.py`
5. All paths hardcoded in `deploy.sh`

## Design

### 1. Environment-based root path (`main.py`)

Replace hardcoded paths with `FACTORY_HOME` env var, defaulting to cwd:

```python
factory_home = Path(os.environ.get("FACTORY_HOME", ".")).resolve()
config_path = factory_home / "config.yml"
db_path = factory_home / "factory.db"
```

### 2. Use `self.base_dir` consistently (`orchestrator.py`)

The `Orchestrator.__init__` already accepts `base_dir` and stores it as `self.base_dir`. Remove the `FACTORY_ROOT` constant and replace all 5 references with `self.base_dir`:

- Lines 40-41: `repos_dir` and `worktrees_dir`
- Lines 680, 809, 1188: worktree path construction

### 3. Configurable Docker preview domain (`docker_toolkit.py`, `config.py`)

Add `DockerConfig` to `config.py`:

```python
class DockerConfig(BaseModel):
    preview_domain: str = "preview.factory.6a.fi"
    network: str = "factory-preview"
```

Pass these values through to `DockerEnvironment` instead of using module-level constants.

### 4. Configurable deploy command (`api.py`, `config.py`)

Add `DeployConfig` to `config.py`:

```python
class DeployConfig(BaseModel):
    command: list[str] = []
```

When empty (default), the deploy endpoint returns 404. When configured, it runs the specified command. This replaces the hardcoded `systemd-run` + `/opt/factory/deploy.sh`.

### 5. Parameterize `deploy.sh`

Replace hardcoded paths with env vars that default to current values:

```bash
FACTORY_DIR="${FACTORY_HOME:-/opt/factory}"
LOCKFILE="$FACTORY_DIR/deploy.lock"
LOGFILE="$FACTORY_DIR/deploy.log"
ORCH_DIR="$FACTORY_DIR/orchestrator"
VENV="$FACTORY_DIR/.venv"
```

## Constraints

- All defaults match current values — existing VPS deployment works without changes
- No new dependencies
- No behavioral changes
- Backward compatible with existing `config.yml` files
