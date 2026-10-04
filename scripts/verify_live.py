"""Read real providers and persist a non-executable assessment. No business fixtures."""

import argparse
import json
import time
from pathlib import Path

from config.settings import settings
from core.schemas.contracts import RunRequest
from core.state.run_store import RunStore
from orchestrator.data_cluster import DataCluster
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.health import HealthService
from services.providers.registry import ProviderRegistry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vessels", type=int, default=8)
    args = parser.parse_args()
    settings.TELEMETRY_MAX_VESSELS = max(1, min(args.vessels, 200))
    registry = ProviderRegistry()
    store = RunStore(settings.RUN_DB_PATH)
    start = time.perf_counter()
    state = build_exochain_graph(store, DataCluster(registry)).invoke(
        initial_state(RunRequest())
    )
    report = {
        "run_id": state["run_id"],
        "status": state["status"],
        "elapsed_seconds": time.perf_counter() - start,
        "telemetry": state["telemetry"],
        "providers": {
            k: {
                field: v.get(field)
                for field in (
                    "source",
                    "quality",
                    "observed_at",
                    "valid_until",
                    "latency_ms",
                    "errors",
                )
            }
            for k, v in state["data"]["sources"].items()
        },
        "agents": [
            {k: a[k] for k in ("agent_id", "status", "latency_ms")}
            for domain in ("data", "intelligence", "decisions")
            for a in state[domain]["executions"]
        ],
        "validation": state["validation"],
        "approval": state["approval"],
        "execution": state["execution"],
        "health": HealthService(registry, store).check(),
    }
    Path("data").mkdir(exist_ok=True)
    Path("data/live_verification.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    assert (
        state["status"] == "BLOCKED"
        and state["approval"] is None
        and state["execution"] is None
    )


if __name__ == "__main__":
    main()
