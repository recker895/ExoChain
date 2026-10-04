"""Exercise the running authenticated API with real providers and no business facts."""

import json
import time
from pathlib import Path
import requests
from config.settings import settings
from core.state.run_store import RunStore
from services.approval_service import execute


def main():
    base = "http://127.0.0.1:8000"
    headers = {
        "Authorization": "Bearer " + settings.OPERATOR_API_TOKEN.get_secret_value()
    }
    response = requests.post(
        base + "/api/v1/runs",
        headers=headers,
        json={"request": {"operation": "ASSESSMENT"}},
        timeout=10,
    )
    response.raise_for_status()
    run_id = response.json()["run_id"]
    start = time.monotonic()
    while time.monotonic() - start < 180:
        response = requests.get(
            base + "/api/v1/runs/" + run_id, headers=headers, timeout=15
        )
        response.raise_for_status()
        state = response.json()
        if state["status"] in {"BLOCKED", "FAILED"}:
            break
        time.sleep(1)
    rejected = requests.post(
        base + "/api/v1/runs/" + run_id + "/execute", headers=headers, timeout=10
    )
    class CountingERP:
        calls = 0

        def submit_purchase_order(self, command):
            self.calls += 1
            raise AssertionError("Blocked assessment reached ERP")

    probe = CountingERP()
    try:
        execute(RunStore(settings.RUN_DB_PATH), run_id, "verification", probe)
    except ValueError:
        pass
    else:
        raise AssertionError("Blocked execution was not rejected")
    report = {
        "blocked_execution_erp_calls": probe.calls,
        "run_id": run_id,
        "status": state["status"],
        "elapsed_seconds": time.monotonic() - start,
        "providers": {
            k: v["quality"] for k, v in state.get("data", {}).get("sources", {}).items()
        },
        "execution_attempt_http_status": rejected.status_code,
        "approval": state["approval"],
        "execution": state["execution"],
        "agent_records": sum(
            len(state.get(k, {}).get("executions", []))
            for k in ("data", "intelligence", "decisions")
        ),
    }
    Path("data/api_live_verification.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))
    assert (
        state["status"] == "BLOCKED"
        and state["approval"] is None
        and state["execution"] is None
        and rejected.status_code == 409
        and report["agent_records"] == 19
        and probe.calls == 0
    )


if __name__ == "__main__":
    main()
