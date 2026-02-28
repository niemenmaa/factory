import asyncio
import logging
import os
import signal
import socket
from pathlib import Path

import click

from factory_worker.client import OrchestratorClient
from factory_worker.config import WorkerConfig, load_worker_config, DEFAULT_CONFIG_PATH
from factory_worker.container import ContainerManager
from factory_worker.workspace import WorkspaceManager

logger = logging.getLogger(__name__)

_shutdown = False


async def launch_agent(task: dict, client: OrchestratorClient,
                       containers: ContainerManager, workspace: WorkspaceManager,
                       config: WorkerConfig) -> None:
    task_id = task["task_id"]
    try:
        wt_path = await workspace.create_worktree(
            repo=task["repo"], repo_url=task["repo_url"],
            branch_name=task["branch_name"],
        )
        await client.report_in_progress(task_id, config.worker_id)
        env = {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
        }
        containers.start(
            task_id=task_id, worktree_path=str(wt_path),
            prompt=task["prompt"], system_prompt=task["system_prompt"],
            allowed_tools=task["allowed_tools"], image=task["image"], env=env,
        )
        logger.info("Launched agent for task %d (%s)", task_id, task["title"])
    except Exception as e:
        logger.exception("Failed to launch agent for task %d", task_id)
        await client.report_result(
            task_id=task_id, worker_id=config.worker_id, status="failed",
            output=f"Worker launch error: {e}", branch_name="", pr_url="",
        )


async def run_loop(config: WorkerConfig, client: OrchestratorClient,
                   containers: ContainerManager, workspace: WorkspaceManager) -> None:
    global _shutdown
    while not _shutdown:
        try:
            await client.heartbeat(config.worker_id, max_agents=config.max_agents,
                                   running=containers.running_count)
            available = config.max_agents - containers.running_count
            if available > 0:
                tasks = await client.claim_tasks(config.worker_id,
                    count=min(available, config.claim_batch_size))
                for task in tasks:
                    await launch_agent(task, client, containers, workspace, config)
            for result in containers.collect_finished():
                await client.report_result(
                    task_id=result.task_id, worker_id=config.worker_id,
                    status=result.status, output=result.output,
                    branch_name=result.branch_name, pr_url=result.pr_url,
                )
                logger.info("Task %d %s", result.task_id, result.status)
            killed = containers.check_timeouts()
            for task_id in killed:
                await client.report_result(
                    task_id=task_id, worker_id=config.worker_id,
                    status="failed", output="Agent timed out", branch_name="", pr_url="",
                )
        except Exception:
            logger.exception("Error in worker loop")
        await asyncio.sleep(config.poll_interval_seconds)


async def _async_start(config: WorkerConfig) -> None:
    global _shutdown
    _shutdown = False
    client = OrchestratorClient(config.orchestrator_url, config.auth_token)
    containers = ContainerManager(config.container)
    workspace = WorkspaceManager()
    cleaned = containers.cleanup_orphans()
    if cleaned:
        logger.info("Cleaned up %d orphaned containers", cleaned)

    def handle_signal(sig, frame):
        global _shutdown
        _shutdown = True
        logger.info("Shutdown signal received")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    click.echo(f"Worker {config.worker_id} connected to {config.orchestrator_url}")
    click.echo(f"Max agents: {config.max_agents}")
    try:
        await run_loop(config, client, containers, workspace)
    finally:
        click.echo("Shutting down...")
        await client.close()


@click.group()
def cli():
    """Factory Worker - local agent execution daemon."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


@cli.command()
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
def start(config_path):
    """Start the worker daemon."""
    path = Path(config_path) if config_path else None
    config = load_worker_config(path)
    if not config.worker_id:
        config.worker_id = socket.gethostname()
    asyncio.run(_async_start(config))


@cli.command()
@click.argument("task_id", type=int)
@click.option("--config", "config_path", type=click.Path(exists=True), default=None)
def claim(task_id, config_path):
    """Manually claim a specific task."""
    path = Path(config_path) if config_path else None
    config = load_worker_config(path)
    if not config.worker_id:
        config.worker_id = socket.gethostname()

    async def _claim():
        client = OrchestratorClient(config.orchestrator_url, config.auth_token)
        task = await client.claim_task(task_id, config.worker_id)
        if task:
            click.echo(f"Claimed task #{task_id}: {task['title']}")
        else:
            click.echo(f"Could not claim task #{task_id}")
        await client.close()
    asyncio.run(_claim())


@cli.command()
@click.argument("stack", required=False)
def build(stack):
    """Build agent Docker images."""
    import subprocess
    worker_dir = Path(__file__).parent.parent.parent
    stacks = [stack] if stack else ["base", "php", "python", "node"]
    for s in stacks:
        dockerfile = worker_dir / f"Dockerfile.{s}"
        if not dockerfile.exists():
            click.echo(f"Dockerfile.{s} not found, skipping")
            continue
        tag = f"factory-agent:{s}"
        click.echo(f"Building {tag}...")
        subprocess.run(["docker", "build", "-t", tag, "-f", str(dockerfile), str(worker_dir)], check=True)
        click.echo(f"  {tag} built successfully")


if __name__ == "__main__":
    cli()
