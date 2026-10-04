from concurrent.futures import ThreadPoolExecutor

from core.schemas.contracts import AuditEvent
from core.state.run_store import RunStore


def test_parallel_agent_events_wait_for_sqlite_writer(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    with store.connect() as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000

    def append(index):
        store.append_event(
            AuditEvent(
                run_id="run-1",
                trace_id="trace-1",
                stage=f"data.agent-{index}",
                status="RUNNING",
                actor=f"agent-{index}",
                reason="Agent started",
            )
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(append, range(48)))

    events = store.events("run-1", limit=100)
    assert len(events) == 48
    assert len({event["id"] for event in events}) == 48
