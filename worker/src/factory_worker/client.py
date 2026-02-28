import logging
import httpx

logger = logging.getLogger(__name__)


class OrchestratorClient:
    def __init__(self, base_url: str, auth_token: str):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {auth_token}"},
            timeout=30.0,
        )

    async def heartbeat(self, worker_id: str, max_agents: int, running: int) -> None:
        resp = await self._http.post("/workers/heartbeat", json={
            "worker_id": worker_id,
            "max_agents": max_agents,
            "running": running,
        })
        resp.raise_for_status()

    async def claim_tasks(self, worker_id: str, count: int) -> list[dict]:
        resp = await self._http.post("/tasks/claim", json={
            "worker_id": worker_id,
            "count": count,
        })
        resp.raise_for_status()
        return resp.json().get("tasks", [])

    async def claim_task(self, task_id: int, worker_id: str) -> dict | None:
        resp = await self._http.post(f"/tasks/{task_id}/claim", json={
            "worker_id": worker_id,
        })
        if resp.status_code == 409:
            return None
        resp.raise_for_status()
        return resp.json().get("task")

    async def report_result(
        self, task_id: int, worker_id: str, status: str,
        output: str, branch_name: str, pr_url: str,
    ) -> None:
        resp = await self._http.post(f"/tasks/{task_id}/report", json={
            "worker_id": worker_id,
            "status": status,
            "output": output,
            "branch_name": branch_name,
            "pr_url": pr_url,
        })
        resp.raise_for_status()

    async def report_in_progress(self, task_id: int, worker_id: str) -> None:
        resp = await self._http.post(f"/tasks/{task_id}/report", json={
            "worker_id": worker_id,
            "status": "in_progress",
            "output": "",
            "branch_name": "",
            "pr_url": "",
        })
        resp.raise_for_status()

    async def close(self) -> None:
        await self._http.aclose()
