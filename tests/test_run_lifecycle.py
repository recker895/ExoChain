"""API scheduling, recoverable drafts and infeasible replenishment regression."""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from config.settings import settings
from core.schemas.contracts import RunRequest
from dashboard.backend.main import create_app
from orchestrator.supervisor import initial_state, build_exochain_graph


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr("test-operator"))
    monkeypatch.setattr(settings, "APPROVER_API_TOKEN", SecretStr("test-approver"))
    return {"Authorization": "Bearer test-operator"}


def terminal(store, rid):
    for _ in range(200):
        state = store.get(rid)
        if state["status"] in {"PENDING_APPROVAL", "BLOCKED", "FAILED"}:
            return state
        time.sleep(0.02)
    pytest.fail("Workflow did not terminate")


@pytest.mark.parametrize("recover", [False, True])
def test_automatic_start_and_draft_recovery(
    store, registry, cluster, business, operator, recover
):
    entered, release = threading.Event(), threading.Event()
    original = cluster.execute

    def gated(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    cluster.execute = gated
    api = TestClient(create_app(store, registry, cluster))
    request = RunRequest(
        operation="REPLENISHMENT",
        inventory_ids=["inv-test"],
        required_components=["inventory", "procurement"],
        budget_usd=1000,
        require_human_approval=True,
        simulation=True,
    )
    if recover:
        draft = initial_state(request, business)
        rid = draft["run_id"]
        store.save(draft)
        response = api.post(f"/api/v1/runs/{rid}/start")
    else:
        response = api.post(
            "/api/v1/runs",
            headers=operator,
            json={
                "request": request.model_dump(mode="json"),
                "business_inputs": business.model_dump(mode="json"),
            },
        )
        rid = response.json()["run_id"]
    try:
        assert response.status_code == 202 and response.json()["scheduled"]
        assert entered.wait(2)
        assert store.get(rid)["status"] == "RUNNING"
        assert (
            api.post(f"/api/v1/runs/{rid}/start", headers=operator).status_code == 409
        )
        assert (
            api.post(f"/api/v1/runs/{rid}/execute", headers=operator).status_code == 409
        )
    finally:
        release.set()
    state = terminal(store, rid)
    assert state["status"] == "PENDING_APPROVAL"
    assert state["optimization"]["status"] == "OPTIMAL"
    assert set(state["optimization"]["components"]) == {"inventory", "procurement"}
    assert state["validation"]["valid"] and state["execution"] is None
    assert state["execution_lifecycle"]["stage"] == "PENDING_APPROVAL"
    assert api.post(f"/api/v1/runs/{rid}/start", headers=operator).status_code == 409
    claims = [
        e
        for e in store.events(rid)
        if e["stage"] == "workflow" and e["status"] == "RUNNING"
    ]
    assert len(claims) == 1 and claims[0]["actor"] == "local-demo:operator"
    assert (
        len(state["data"]["executions"])
        + len(state["intelligence"]["executions"])
        + len(state["decisions"]["executions"])
        == 19
    )


def test_workflow_failure_is_durable(store, registry, cluster, business, operator):
    def failed(*args, **kwargs):
        raise RuntimeError("TEST_ONLY_SECRET")

    cluster.execute = failed
    api = TestClient(create_app(store, registry, cluster))
    result = api.post(
        "/api/v1/runs",
        headers=operator,
        json={"request": {}, "business_inputs": business.model_dump(mode="json")},
    )
    state = terminal(store, result.json()["run_id"])
    assert state["status"] == "FAILED"
    assert state["errors"][-1] == {"stage": "workflow", "error": "RuntimeError"}
    assert state["approval"] is None and state["execution"] is None
    assert api.post("/api/v1/runs/absent/start", headers=operator).status_code == 404


def test_two_sku_budget_feasible_partial_preserves_validation_boundary(
    cluster, business
):
    first = business.inventory[0]
    first.current_inventory_units = 420
    first.expected_demand_units = 900
    first.safety_stock_units = 250
    first.max_stock_units = 1500
    first.unit_cost_usd = 42.5
    first.ordering_cost_usd = 250
    first.service_level = 0.98
    second = first.model_copy(
        update={
            "id": "second-inventory",
            "sku_id": "second-sku",
            "current_inventory_units": 120,
            "expected_demand_units": 650,
            "safety_stock_units": 180,
            "max_stock_units": 1000,
            "unit_cost_usd": 125,
            "ordering_cost_usd": 300,
            "service_level": 0.97,
        }
    )
    business.inventory.append(second)
    business.skus.append(business.skus[0].model_copy(update={"id": "second-sku"}))
    quote = business.supplier_quotes[0]
    business.supplier_quotes = [
        quote.model_copy(
            update={
                "id": name,
                "sku_id": sku,
                "unit_cost_usd": price,
                "capacity_units": cap,
            }
        )
        for name, sku, price, cap in [
            ("a", first.sku_id, 42.5, 700),
            ("b", first.sku_id, 46, 1000),
            ("c", second.sku_id, 125, 800),
            ("d", second.sku_id, 132, 600),
        ]
    ]
    result = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT",
                inventory_ids=[first.id, second.id],
                required_components=["inventory", "procurement"],
                budget_usd=35000,
                warehouse_capacity_units=5000,
                require_human_approval=True,
                simulation=True,
            ),
            business,
        )
    )
    plan = result["optimization"]
    assert plan["status"] == "OPTIMAL" and not plan["constraint_violations"]
    assert plan["total_cost_usd"] == 34992.5 <= 35000
    explanation = plan["components"]["inventory"]["explanation"]
    assert explanation["plan_type"] == "BUDGET_CONSTRAINED_PARTIAL_REPLENISHMENT"
    assert explanation["shortage_units"] == 418
    assert explanation["safety_stock_shortfall_units"] == 429
    assert explanation["remaining_budget_usd"] == 7.5
    assert [
        r["full_demand_replenishment_units"] for r in explanation["requirements"]
    ] == [730, 710]
    assert [r["target_order_units"] for r in explanation["requirements"]] == [712, 691]
    assert [i["order_units"] for i in plan["components"]["inventory"]["selected"]] == [
        481,
        112,
    ]
    assert sum(a["quantity"] for a in plan["selected_actions"]) == 593
    assert result["status"] == "BLOCKED" and result["approval"] is None
    assert "INVENTORY_CONSTRAINT_VIOLATION" in result["validation"]["reasons"]
    assert result["execution"] is None


def test_expired_worker_is_failed_and_fenced_but_draft_is_recoverable(store):
    from datetime import timedelta
    from core.schemas.contracts import utcnow

    state = initial_state(RunRequest())
    store.save(state)
    draft = initial_state(RunRequest())
    store.save(draft)

    def claim(db, current):
        current["status"] = "RUNNING"
        db.execute(
            "INSERT INTO workflow_workers VALUES(?,?,?)",
            (
                current["run_id"],
                "dead-worker",
                (utcnow() - timedelta(minutes=5)).isoformat(),
            ),
        )
        return None, None

    store.transaction(state["run_id"], claim)
    assert store.recover_interrupted_workflows() == [state["run_id"]]
    assert store.get(state["run_id"])["status"] == "FAILED"
    assert store.get(draft["run_id"])["status"] == "DRAFT"
    assert store.events(state["run_id"])[-1]["reason"].startswith(
        "Worker lease expired"
    )
    with pytest.raises(ValueError, match="fenced"):
        store.save(state)
    assert store.recover_interrupted_workflows() == []


def test_live_worker_heartbeat_prevents_recovery(store):
    from datetime import timedelta
    from core.schemas.contracts import utcnow

    state = initial_state(RunRequest())
    state["status"] = "RUNNING"
    store.save(state)
    with store.connect() as db:
        db.execute(
            "INSERT INTO workflow_workers VALUES(?,?,?)",
            (
                state["run_id"],
                "live-worker",
                (utcnow() - timedelta(minutes=5)).isoformat(),
            ),
        )
    store.heartbeat_workflows("live-worker")
    assert store.recover_interrupted_workflows() == []
    assert store.get(state["run_id"])["status"] == "RUNNING"


def test_noop_transaction_does_not_reorder_run_history(store):
    state = initial_state(RunRequest())
    store.save(state)
    with store.connect() as db:
        before = db.execute(
            "SELECT updated_at FROM runs WHERE run_id=?", (state["run_id"],)
        ).fetchone()[0]
    store.transaction(state["run_id"], lambda db, current: (None, None))
    with store.connect() as db:
        assert (
            db.execute(
                "SELECT updated_at FROM runs WHERE run_id=?", (state["run_id"],)
            ).fetchone()[0]
            == before
        )
