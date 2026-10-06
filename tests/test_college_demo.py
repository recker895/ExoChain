"""College mode completes with solver results and local simulated orders."""

from datetime import timedelta

import pytest

from config.settings import settings
from core.schemas.contracts import RunRequest, utcnow
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.approval_service import execute
from services.demo import replenishment_sample
from services.policy import plan_hash


def run_demo(cluster, store, business=None, **changes):
    options = dict(operation="REPLENISHMENT", demo_mode=True,
                   budget_usd=125000, inventory_ids=["INV-001", "INV-002"])
    options.update(changes)
    return build_exochain_graph(store, cluster).invoke(
        initial_state(RunRequest(**options), business)
    )


def test_demo_runs_all_agents_and_simulates_without_erp(cluster, store, monkeypatch):
    import services.approval_service as approvals
    monkeypatch.setattr(approvals, "configured_erp", lambda *a: pytest.fail("Demo called ERP"))
    monkeypatch.setattr(settings, "PROCUREMENT_MAX_BUDGET_USD", 35000)
    for _ in range(2):
        state = run_demo(cluster, store)
        assert state["status"] == "EXECUTION_SIMULATED", state["validation"]
        assert state["optimization"]["status"] == "OPTIMAL"
        inventory = state["optimization"]["components"]["inventory"]["selected"]
        assert {i["id"]: i["order_units"] for i in inventory} == {"INV-001": 730, "INV-002": 710}
        assert state["optimization"]["total_cost_usd"] == 120275
        assert state["execution"]["simulation"]
        assert state["approval"]["plan_hash"] == plan_hash(state)
        assert sum(len(state[k]["executions"]) for k in ("data", "intelligence", "decisions")) == 19
        with pytest.raises(ValueError, match="cannot be executed"):
            execute(store, state["run_id"], "operator:demo")
    with store.connect() as db:
        for table in ("executions", "replenishment_commitments", "quote_commitments", "execution_feedback"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_stale_import_replays_only_in_demo(cluster, store):
    business = replenishment_sample()
    old = utcnow() - timedelta(days=60)
    for field in type(business).model_fields:
        records = getattr(business, field)
        for record in records if isinstance(records, list) else [records]:
            if record is not None:
                record.created_at = old
                record.observed_at = old
                record.valid_until = old + timedelta(days=1)
                record.data_quality = "STALE"
    demo = run_demo(cluster, store, business)
    assert demo["status"] == "EXECUTION_SIMULATED"
    assert business.inventory[0].observed_at == old
    normal = build_exochain_graph(store, cluster).invoke(initial_state(
        RunRequest(operation="REPLENISHMENT", inventory_ids=["INV-001"], budget_usd=125000), business,
    ))
    assert normal["status"] == "BLOCKED"
    assert normal["execution"] is None


def test_infeasible_demo_finishes_with_warnings_not_fake_order(cluster, store):
    state = run_demo(cluster, store, budget_usd=1)
    assert state["status"] == "DEMO_COMPLETE"
    assert state["optimization"]["status"] == "DEMO_WARNINGS"
    assert state["validation"]["reasons"]
    assert state["execution"] is None
    evidence = state["optimization"]["components"]["inventory"]["explanation"]
    assert evidence["remaining_budget_usd"] == 1
    assert evidence["shortage_units"] > 0


def test_assessment_demo_finishes_without_plan(cluster, store):
    state = run_demo(cluster, store, operation="ASSESSMENT", inventory_ids=[])
    assert state["status"] == "DEMO_COMPLETE"
    assert state["execution"] is None
    assert sum(len(state[k]["executions"]) for k in ("data", "intelligence", "decisions")) == 19


def test_demo_disabled_in_production(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="disabled in production"):
        initial_state(RunRequest(operation="REPLENISHMENT", demo_mode=True))


def test_api_demo_needs_no_upload_and_returns_simulated_order(cluster, registry, store, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from dashboard.backend.main import create_app

    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr("college-test-operator"))
    api = TestClient(create_app(store, registry, cluster))
    headers = {"Authorization": "Bearer college-test-operator"}
    response = api.post("/api/v1/runs", headers=headers, json={"request": {
        "operation": "REPLENISHMENT", "demo_mode": True,
    }})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    for _ in range(100):
        state = store.get(run_id)
        if state["status"] in {"EXECUTION_SIMULATED", "DEMO_COMPLETE", "FAILED"}:
            break
        time.sleep(0.05)
    assert state["status"] == "EXECUTION_SIMULATED", state.get("errors")
    assert state["request"]["required_components"] == ["inventory", "procurement"]
    assert state["request"]["inventory_ids"] == ["INV-001", "INV-002"]
    assert state["request"]["simulation"]
    assert api.get(f"/api/v1/runs/{run_id}", headers=headers).json()["execution"]["simulation"]
