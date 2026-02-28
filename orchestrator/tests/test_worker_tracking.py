from datetime import datetime, timezone, timedelta
from factory.orchestrator import WorkerInfo


def test_worker_info_is_alive():
    worker = WorkerInfo(
        worker_id="test-worker",
        max_agents=8,
        running=3,
        last_heartbeat=datetime.now(timezone.utc),
    )
    assert worker.is_alive is True


def test_worker_info_is_dead():
    worker = WorkerInfo(
        worker_id="test-worker",
        max_agents=8,
        running=3,
        last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    assert worker.is_alive is False


def test_worker_info_has_capacity():
    worker = WorkerInfo(worker_id="w", max_agents=8, running=3,
                        last_heartbeat=datetime.now(timezone.utc))
    assert worker.has_capacity is True


def test_worker_info_no_capacity():
    worker = WorkerInfo(worker_id="w", max_agents=3, running=3,
                        last_heartbeat=datetime.now(timezone.utc))
    assert worker.has_capacity is False
