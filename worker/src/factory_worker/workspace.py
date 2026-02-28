import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".factory"


class WorkspaceManager:
    def __init__(self, cache_dir: Path = DEFAULT_CACHE_DIR):
        self._cache_dir = cache_dir
        self._repos_dir = cache_dir / "repos"
        self._worktrees_dir = cache_dir / "worktrees"

    def _repo_path(self, repo: str) -> Path:
        return self._repos_dir / repo

    def _worktree_path(self, repo: str, branch_name: str) -> Path:
        return self._worktrees_dir / branch_name

    async def _run(self, *args: str, cwd: Path | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed: {' '.join(args)}\n{stderr.decode()}")
        return stdout.decode().strip()

    async def ensure_repo(self, repo: str, repo_url: str) -> Path:
        repo_path = self._repo_path(repo)
        if repo_path.exists():
            await self._run("git", "fetch", "--all", cwd=repo_path)
        else:
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            await self._run("git", "clone", repo_url, str(repo_path))
        return repo_path

    async def create_worktree(self, repo: str, repo_url: str, branch_name: str) -> Path:
        repo_path = await self.ensure_repo(repo, repo_url)
        wt_path = self._worktree_path(repo, branch_name)
        if wt_path.exists():
            logger.info("Worktree already exists at %s, reusing", wt_path)
            return wt_path
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        await self._run("git", "worktree", "add", "-b", branch_name, str(wt_path), cwd=repo_path)
        logger.info("Created worktree at %s", wt_path)
        return wt_path

    async def remove_worktree(self, repo: str, branch_name: str) -> None:
        repo_path = self._repo_path(repo)
        wt_path = self._worktree_path(repo, branch_name)
        if wt_path.exists():
            await self._run("git", "worktree", "remove", str(wt_path), cwd=repo_path)
            logger.info("Removed worktree at %s", wt_path)
