import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import docker

from factory_worker.config import ContainerConfig

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    task_id: int
    status: str  # "done" or "failed"
    output: str
    branch_name: str
    pr_url: str


class ContainerManager:
    def __init__(self, config: ContainerConfig):
        self._config = config
        self._docker = docker.from_env()
        self._running: dict[int, dict] = {}

    @property
    def running_count(self) -> int:
        return len(self._running)

    def start(
        self, task_id: int, worktree_path: str, prompt: str,
        system_prompt: str, allowed_tools: list[str], image: str,
        env: dict[str, str],
    ) -> None:
        container_name = f"factory-agent-{task_id}"
        cmd = ["--print", "--output-format", "json"]
        if self._config.dangerous_mode:
            cmd.append("--dangerously-skip-permissions")
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])
        cmd.append(prompt)

        volumes = {worktree_path: {"bind": "/workspace", "mode": "rw"}}
        if self._config.ssh_dir:
            volumes[self._config.ssh_dir] = {"bind": "/root/.ssh", "mode": "ro"}

        container = self._docker.containers.run(
            image, cmd, name=container_name, detach=True,
            volumes=volumes,
            environment=env, working_dir="/workspace",
            cpu_count=int(self._config.cpu_limit),
            mem_limit=self._config.memory_limit,
        )
        self._running[task_id] = {
            "container": container, "task_id": task_id,
            "started_at": datetime.now(timezone.utc),
        }
        logger.info("Started container %s for task %d", container_name, task_id)

    def collect_finished(self) -> list[AgentResult]:
        results = []
        finished_ids = []
        for task_id, info in self._running.items():
            container = info["container"]
            try:
                container.reload()
            except Exception:
                finished_ids.append(task_id)
                results.append(AgentResult(task_id=task_id, status="failed",
                    output="Container disappeared", branch_name="", pr_url=""))
                continue
            if container.status != "exited":
                continue
            exit_code = container.attrs["State"]["ExitCode"]
            output = container.logs().decode("utf-8", errors="replace")
            branch_name, pr_url = _parse_output(output)
            results.append(AgentResult(
                task_id=task_id, status="done" if exit_code == 0 else "failed",
                output=output[-50000:], branch_name=branch_name, pr_url=pr_url,
            ))
            try:
                container.remove()
            except Exception:
                pass
            finished_ids.append(task_id)
        for tid in finished_ids:
            del self._running[tid]
        return results

    def kill_all(self) -> None:
        for info in list(self._running.values()):
            try:
                info["container"].kill()
                info["container"].remove(force=True)
            except Exception:
                pass
        self._running.clear()

    def cleanup_orphans(self) -> int:
        count = 0
        for container in self._docker.containers.list(all=True):
            if container.name.startswith("factory-agent-"):
                try:
                    container.remove(force=True)
                    count += 1
                except Exception:
                    pass
        return count

    def check_timeouts(self) -> list[int]:
        killed = []
        timeout_seconds = self._config.timeout_minutes * 60
        now = datetime.now(timezone.utc)
        for task_id, info in list(self._running.items()):
            elapsed = (now - info["started_at"]).total_seconds()
            if elapsed > timeout_seconds:
                logger.warning("Task %d timed out after %ds", task_id, int(elapsed))
                try:
                    info["container"].kill()
                    info["container"].remove(force=True)
                except Exception:
                    pass
                killed.append(task_id)
        for tid in killed:
            del self._running[tid]
        return killed


def _parse_output(output: str) -> tuple[str, str]:
    branch = ""
    pr_url = ""
    pr_match = re.search(r"https://github\.com/[^\s]+/pull/\d+", output)
    if pr_match:
        pr_url = pr_match.group(0)
    branch_match = re.search(r"agent/task-\d+-[\w-]+", output)
    if branch_match:
        branch = branch_match.group(0)
    return branch, pr_url
