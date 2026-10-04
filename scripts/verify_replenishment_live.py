"""Verify a supplied real/local source via API; never approve or execute ERP."""

import argparse
import json
import time

import requests
from config.settings import settings


def agent_execution_evidence(state):
    """Count completed, distinct agents from run records or persisted audit events."""
    counts = {}
    sources = {}
    for cluster, prefix in (
        ("data", "data."),
        ("intelligence", "intelligence."),
        ("decisions", "decision."),
    ):
        records = state.get(cluster, {}).get("executions")
        if isinstance(records, list):
            agents = {
                record["agent_id"]
                for record in records
                if isinstance(record, dict)
                and isinstance(record.get("agent_id"), str)
                and record["agent_id"].startswith(prefix)
                and record.get("completed_at")
            }
            sources[cluster] = "executions"
        else:
            agents = {
                event["stage"]
                for event in state.get("audit", [])
                if isinstance(event, dict)
                and event.get("run_id") == state["run_id"]
                and isinstance(event.get("stage"), str)
                and event["stage"].startswith(prefix)
                and event.get("actor") == event["stage"]
                and event.get("reason", "").startswith("Agent completed in ")
            }
            sources[cluster] = "audit" if agents else "NOT_EXPOSED_OR_NOT_COMPLETED"
        counts[cluster] = len(agents)
    verified = counts == {"data": 6, "intelligence": 7, "decisions": 6}
    return counts, sources, verified


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--inventory-ids", nargs="+", required=True)
    parser.add_argument("--budget", type=float, required=True)
    parser.add_argument("--simulation", action="store_true")
    args = parser.parse_args()
    headers = {
        "Authorization": "Bearer " + settings.OPERATOR_API_TOKEN.get_secret_value()
    }
    response = requests.post(
        args.url + "/api/v1/runs",
        headers=headers,
        json={
            "request": {
                "operation": "REPLENISHMENT",
                "inventory_ids": args.inventory_ids,
                "required_components": ["inventory", "procurement"],
                "budget_usd": args.budget,
                "max_risk": 0.5,
                "warehouse_capacity_units": 5000,
                "require_human_approval": True,
                "simulation": args.simulation,
                "what_if": False,
            }
        },
        timeout=30,
        allow_redirects=False,
    )
    assert response.status_code == 202, response.status_code
    rid = response.json()["run_id"]
    for _ in range(240):
        response = requests.get(
            args.url + "/api/v1/runs/" + rid,
            headers=headers,
            timeout=30,
            allow_redirects=False,
        )
        response.raise_for_status()
        state = response.json()
        # BLOCKED is also published by intermediate graph nodes before validation.
        # Wait for the validation boundary rather than treating that snapshot as final.
        if state["status"] == "FAILED" or (
            state["status"] in {"BLOCKED", "PENDING_APPROVAL"}
            and type(state.get("validation", {}).get("valid")) is bool
        ):
            break
        time.sleep(1)
    else:
        raise TimeoutError("Run did not terminate")
    counts, evidence_sources, agents_verified = agent_execution_evidence(state)
    count = sum(counts.values())
    assert "execution" in state and state["execution"] is None, (
        "ERP execution detected or execution state missing"
    )
    validation = state.get("validation", {})
    optimization = state.get("optimization", {})
    if state["status"] == "BLOCKED":
        assert (
            "approval" in state
            and state["approval"] is None
            and validation.get("valid") is False
        ), "BLOCKED run must expose no approval and explicitly invalid validation"
    elif state["status"] == "PENDING_APPROVAL":
        assert validation.get("valid") is True and optimization.get(
            "selected_actions"
        ), "PENDING_APPROVAL requires valid validation and selected actions"
    print(
        json.dumps(
            {
                "run_id": rid,
                "status": state["status"],
                "agents": count,
                "agent_counts": counts,
                "agent_evidence_sources": evidence_sources,
                "all_19_agents_verified": agents_verified,
                "agent_verification": (
                    "VERIFIED"
                    if agents_verified
                    else "UNVERIFIED: API evidence does not prove all 19 agents completed"
                ),
                "business": state["data"]
                .get("sources", {})
                .get("business", {})
                .get("quality"),
                "optimization": optimization,
                "validation": validation,
                "approval": state["approval"],
                "execution": state["execution"],
                "errors": state.get("errors", []),
            },
            indent=2,
        )
    )
    if not agents_verified:
        raise SystemExit(
            "Verification failed: all 19 agent executions could not be proven."
        )
    if state["status"] == "FAILED":
        raise SystemExit(
            "Verification failed: workflow ended in FAILED; see reported errors."
        )


if __name__ == "__main__":
    main()
