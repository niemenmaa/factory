# Local-Remote Work Handover Design

## Overview

Add distributed worker support to Factory. Tasks remain on the VPS orchestrator (source of truth), but a local worker process can connect, claim tasks, and execute them inside sandboxed Docker containers. The local machine typically has more capacity than the VPS and can run agents in "dangerous mode" safely.

## Architecture

```
VPS (orchestrator)                    Local Machine
┌──────────────┐                      ┌──────────────────┐
│ Orchestrator  │◄── HTTP polling ────│  factory-worker   │
│ (FastAPI)     │                      │  (Python daemon)  │
│              │── claim response ───►│                    │
│  SQLite DB    │                      │  ┌──────────────┐ │
│  (tasks,      │◄── status updates ──│  │ Container 1  │ │
│   workers)    │                      │  │ (Claude agent)│ │
└──────────────┘                      │  └──────────────┘ │
                                       │  ┌──────────────┐ │
                                       │  │ Container 2  │ │
                                       │  │ (Claude agent)│ │
                                       │  └──────────────┘ │
                                       │  ...               │
                                       └──────────────────┘
```

Tasks always live on the VPS. Workers are "powerful remotes" that claim and execute tasks. VPS handles state management, Plane sync, notifications, workflow advancement. Workers handle execution only.

## 1. Worker Protocol (VPS API Additions)

### New Task Fields

```python
class Task(BaseModel):
    # ... existing fields ...
    claimed_by: str | None = None      # worker_id that claimed this task
    claimed_at: datetime | None = None  # when claimed (for lease TTL)
    execution_mode: str = "local"       # "local" (VPS subprocess) or "remote" (worker)
```

### Worker Heartbeat

Workers register on connect and send periodic heartbeats. Tracked in-memory on the orchestrator with TTL.

```
POST /api/workers/heartbeat
Body: { "worker_id": "anttoni-mbp", "max_agents": 8, "running": 3 }
→ 200 OK
```

If no heartbeat for 60s, worker is considered dead. Its unclaimed tasks return to queue; in-progress tasks are marked failed.

```python
@dataclass
class WorkerInfo:
    worker_id: str
    max_agents: int
    running: int
    last_heartbeat: datetime

    @property
    def has_capacity(self) -> bool:
        return self.running < self.max_agents

    @property
    def is_alive(self) -> bool:
        return (datetime.utcnow() - self.last_heartbeat).seconds < 60
```

### Task Claiming

```
POST /api/tasks/claim
Body: { "worker_id": "anttoni-mbp", "count": 3 }
→ { "tasks": [{ ... full execution payload ... }] }

POST /api/tasks/{id}/claim
Body: { "worker_id": "anttoni-mbp" }
→ { "task": { ... full execution payload ... } }
```

Claiming sets `claimed_by`, `claimed_at`, and `execution_mode = "remote"`. Claims have a **lease TTL of 5 minutes** — if the worker doesn't report `in_progress` within that window, the claim expires and the task returns to `QUEUED`.

Atomic claim via DB transaction prevents race conditions:

```python
async def claim_task(self, task_id: int, worker_id: str) -> bool:
    result = await self.execute(
        "UPDATE tasks SET claimed_by = ?, claimed_at = ? "
        "WHERE id = ? AND claimed_by IS NULL AND status = 'queued'",
        (worker_id, utcnow(), task_id)
    )
    return result.rowcount > 0
```

### Claim Payload

The orchestrator builds the full execution package at claim time (prompt with handoff context, memories, system prompt). The worker receives everything it needs:

```json
{
    "task_id": 42,
    "title": "Fix checkout timeout",
    "description": "Users report timeouts...",
    "repo": "legacy-app",
    "repo_url": "git@github.com:you/legacy-app.git",
    "branch_name": "agent/task-42-fix-checkout-timeout",
    "image": "factory-agent:php",
    "prompt": "...full built prompt...",
    "system_prompt": "...loaded from prompts/coder.md...",
    "allowed_tools": ["Read", "Edit", "Bash", "Glob", "Grep"],
    "timeout_minutes": 60
}
```

### Result Reporting

```
POST /api/tasks/{id}/report
Body: {
    "worker_id": "anttoni-mbp",
    "status": "done",
    "output": "...",
    "branch_name": "agent/task-42-fix-checkout-timeout",
    "pr_url": "https://github.com/..."
}
```

The orchestrator then runs its normal completion logic — updates Plane, sends notifications, stores handoffs, advances workflows.

## 2. Local Worker Process

A standalone Python daemon (`factory-worker`) that runs on the local machine. Thin client — no SQLite, no FastAPI, just a polling loop and container manager.

### Project Structure

```
factory/
├── orchestrator/              # existing VPS code
└── worker/                    # new local worker
    ├── pyproject.toml
    ├── Dockerfile.base        # shared base image
    ├── Dockerfile.php
    ├── Dockerfile.python
    ├── Dockerfile.node
    └── src/factory_worker/
        ├── main.py            # CLI entry point + polling loop
        ├── client.py          # HTTP client for VPS orchestrator API
        ├── container.py       # Docker container lifecycle
        ├── config.py          # Local worker config
        └── workspace.py       # Git clone + worktree setup locally
```

### Configuration

```yaml
# ~/.factory/worker-config.yml
worker_id: "anttoni-mbp"
orchestrator_url: "https://factory.6a.fi/api"
auth_token: "${FACTORY_AUTH_TOKEN}"

max_agents: 8
poll_interval_seconds: 10
claim_batch_size: 3

container:
  runtime: "docker"
  dangerous_mode: true
  cpu_limit: "2"
  memory_limit: "4g"
  timeout_minutes: 60

repos_filter: []               # empty = claim anything
```

### Main Loop

```python
async def run(config):
    client = OrchestratorClient(config.orchestrator_url, config.auth_token)
    containers = ContainerManager(config.container)

    while True:
        # 1. Heartbeat
        await client.heartbeat(
            worker_id=config.worker_id,
            max_agents=config.max_agents,
            running=containers.running_count,
        )

        # 2. Claim tasks if we have capacity
        available = config.max_agents - containers.running_count
        if available > 0:
            tasks = await client.claim_tasks(
                worker_id=config.worker_id,
                count=min(available, config.claim_batch_size),
            )
            for task in tasks:
                await launch_agent(task, client, containers, config)

        # 3. Check completed containers, report results
        for result in containers.collect_finished():
            await client.report_result(
                task_id=result.task_id,
                worker_id=config.worker_id,
                status=result.status,
                output=result.output,
                branch_name=result.branch_name,
                pr_url=result.pr_url,
            )

        await asyncio.sleep(config.poll_interval_seconds)
```

## 3. Container Runtime

### Per-Stack Docker Images

All images share a base layer. Per-stack images add language tooling:

```
factory-agent:base    → Node.js 22 + Claude Code CLI + git + gh
factory-agent:php     → base + PHP 8.4 + Composer
factory-agent:python  → base + Python 3.12 + pip + uv
factory-agent:node    → base (Node.js already there)
```

### Base Image

```dockerfile
# Dockerfile.base
FROM node:22-bookworm-slim
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ssh-client curl && \
    rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
ENTRYPOINT ["claude"]
```

### PHP Image

```dockerfile
# Dockerfile.php
FROM factory-agent:base
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      php8.4-cli php8.4-xml php8.4-mbstring php8.4-curl unzip && \
    rm -rf /var/lib/apt/lists/*
RUN curl -sS https://getcomposer.org/installer | php -- \
      --install-dir=/usr/local/bin --filename=composer
```

### Image Selection

Detection order when no explicit image set in repo config:

1. Repo config `image` field (explicit)
2. `composer.json` in repo root → `factory-agent:php`
3. `pyproject.toml` or `requirements.txt` → `factory-agent:python`
4. `package.json` → `factory-agent:node`
5. Fallback → `factory-agent:base`

Configured per-repo:

```yaml
repos:
  legacy-app:
    url: "git@github.com:you/legacy-app.git"
    image: "factory-agent:php"        # explicit
  new-project:
    url: "git@github.com:you/new-project.git"
    # no image → auto-detect from repo files
```

### Container Launch

```bash
docker run -d \
  --name factory-agent-42 \
  --cpus 2 --memory 4g \
  -v /path/to/worktree:/workspace \
  -e ANTHROPIC_API_KEY=... \
  -e GITHUB_TOKEN=... \
  factory-agent:php \
  --print \
  --output-format json \
  --dangerously-skip-permissions \
  --system-prompt "..." \
  --allowedTools "Read,Edit,Bash,Glob,Grep" \
  "the task prompt here"
```

GitHub auth via `GITHUB_TOKEN` env var (used by `gh` CLI and git credential helper). No SSH key mounting needed.

### Timeout Enforcement

Worker runs a watchdog that checks container elapsed time every 30 seconds. Kills containers that exceed the configured timeout.

## 4. Execution Routing

### Routing Logic

When a task is ready to execute:

1. If task already claimed by a worker → skip, worker handles it
2. If workers are connected with capacity → leave QUEUED, let workers claim
3. No workers connected → run locally (existing subprocess path)

Workers get priority over local execution. VPS local subprocess is the fallback.

### Configuration

```yaml
# config.yml (VPS)
execution:
  prefer_workers: true
  local_fallback: true
  worker_claim_window_seconds: 30
```

### Changes to `process_task()`

Minimal change to existing method:

```python
async def process_task(self, task_id: int) -> bool:
    task = await self.db.get_task(task_id)

    # If already claimed by a remote worker, don't run locally
    if task.claimed_by:
        return True

    # If workers are connected and we prefer them, defer
    if self.config.execution.prefer_workers and self._has_active_workers():
        return True  # stays QUEUED, workers will claim it

    # Existing local execution path (unchanged)
    ...
```

## 5. Failure Handling

### Worker Dies Mid-Task

No heartbeat for 60s → worker marked dead:

- Unclaimed tasks (QUEUED + claimed_by = worker): clear claim, return to pool
- In-progress tasks (IN_PROGRESS + claimed_by = worker): mark FAILED
- Notify via Telegram

Optional auto-retry:

```yaml
execution:
  retry_on_worker_death: true
  max_retries: 1
```

### Claim Lease Expiry

Worker claims but doesn't report IN_PROGRESS within 5 minutes → claim expires, task returns to QUEUED.

### Worker Reconnects

Treated as fresh connection. Worker startup cleans up orphaned `factory-agent-*` containers from previous runs.

### Race Conditions

- **Two workers claim same task**: DB transaction, first writer wins
- **VPS local vs worker claim**: `process_task()` checks `claimed_by` before local execution
- **Late result after orchestrator timeout**: Accepted — late success updates task status

### Network Partitions

- Worker side: buffer completed results, report when connection restores
- VPS side: worker appears dead after 60s, may fail tasks. Reconciles on reconnect.

### Summary

| Failure | Detection | Recovery |
|---|---|---|
| Worker crash | No heartbeat 60s | Fail tasks, return unclaimed to queue |
| Claim expires | No IN_PROGRESS 5m | Return task to queue |
| Container OOM | Non-zero exit code | Report failure, preserve worktree |
| Network partition | Heartbeat timeout | Buffer locally, reconcile on reconnect |
| Duplicate claim | DB transaction | First writer wins |
| Late result | Status mismatch | Accept and update |

## 6. CLI Interface

### Commands

| Command | Description |
|---|---|
| `factory-worker init` | First-time setup, create config |
| `factory-worker start` | Connect and start claiming tasks |
| `factory-worker start -d` | Daemonized mode |
| `factory-worker status` | Show running/completed agents |
| `factory-worker claim <id>` | Manually claim specific task |
| `factory-worker pause` / `resume` | Pause/resume auto-claiming |
| `factory-worker stop` | Graceful shutdown (wait for agents) |
| `factory-worker stop --force` | Kill all containers immediately |
| `factory-worker logs <id>` | Stream agent container logs |
| `factory-worker build [stack]` | Build agent Docker images |

### Typical Session

```bash
$ factory-worker start
Worker anttoni-mbp connected to factory.6a.fi
5 tasks queued, claiming 3...
Starting factory-agent:php for task #42 (Fix checkout timeout)
Starting factory-agent:python for task #43 (Add API endpoint)
Starting factory-agent:php for task #44 (Update migrations)
Running: 3/8 agents

$ factory-worker status
Worker: anttoni-mbp | factory.6a.fi | uptime: 2h 14m

RUNNING (3/8)
  #42  Fix checkout timeout       factory-agent:php     12m elapsed
  #43  Add API endpoint           factory-agent:python   8m elapsed
  #44  Update migrations          factory-agent:php      3m elapsed

COMPLETED TODAY
  #38  Fix login bug              ✓ done   PR #127
  #39  Add user search            ✓ done   PR #128
  #40  Update dependencies        ✗ failed (timeout)
```

## Future Enhancements (Out of Scope)

- **Task decomposition**: Split large tasks into subtasks across multiple agents
- **VPS containerized execution**: Run VPS agents in containers too (same runtime)
- **Multiple workers**: Support multiple remote workers (different machines)
- **Vagrant/macOS container runtimes**: Alternative isolation backends
