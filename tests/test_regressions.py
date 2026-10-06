import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from config.settings import settings, Settings
from core.schemas.contracts import (
    ApprovalDecision,
    BusinessInputs,
    Carrier,
    DataQualityReport,
    DataSnapshot,
    InventoryPosition,
    ModalCandidate,
    ProviderEnvelope,
    RunRequest,
    Shipment,
    utcnow,
)
from dashboard.backend.main import create_app
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.approval_service import decide, execute
from services.forecasting.stgnn import validated_forecast
from services.policy import validate
from services.providers.registry import CachedProvider


def test_missing_demand_is_rejected_at_boundary(business):
    record = business.inventory[0].model_dump()
    del record["expected_demand_units"]
    with pytest.raises(ValidationError):
        InventoryPosition.model_validate(record)


def test_sufficient_stock_requires_no_order(cluster, business):
    business.inventory[0].current_inventory_units = 40
    state = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
            ),
            business,
        )
    )
    assert state["status"] == "NO_ACTION_REQUIRED"
    assert state["approval"] is None and state["execution"] is None


def test_missing_inventory_blocks(cluster):
    state = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["absent"], budget_usd=1000
            )
        )
    )
    assert state["status"] == "BLOCKED"
    assert "INVENTORY_UNAVAILABLE" in state["optimization"]["constraint_violations"]
    assert state["approval"] is None and state["execution"] is None


def test_solver_exception_does_not_request_approval(cluster, business, monkeypatch):
    def fail(*args):
        raise RuntimeError("test solver failure")

    monkeypatch.setattr("optimization.engine.solver", fail)
    state = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
            ),
            business,
        )
    )
    assert state["optimization"]["status"] == "FAILED"
    assert state["approval"] is None and state["execution"] is None


def test_modal_allocation_uses_actual_quote_capacity(cluster, metadata):
    now = utcnow()
    business = BusinessInputs(
        shipments=[
            Shipment(
                id="shipment-test",
                sku_id="sku-test",
                quantity=12,
                origin_id="a",
                destination_id="b",
                departure_at=now,
                due_at=now + timedelta(hours=48),
                **metadata,
            )
        ],
        carriers=[
            Carrier(
                id="carrier-test", name="TEST_ONLY", modes=["AIR", "SEA"], **metadata
            )
        ],
        carrier_options=[
            ModalCandidate(
                id="sea-test",
                shipment_id="shipment-test",
                carrier_id="carrier-test",
                mode="SEA",
                capacity_units=8,
                cost_per_unit_usd=5,
                duration_hours=20,
                risk_score=0.1,
                **metadata,
            ),
            ModalCandidate(
                id="air-test",
                shipment_id="shipment-test",
                carrier_id="carrier-test",
                mode="AIR",
                capacity_units=10,
                cost_per_unit_usd=10,
                duration_hours=4,
                risk_score=0.1,
                **metadata,
            ),
        ],
    )
    request = RunRequest(
        operation="TRANSPORT",
        shipment_ids=["shipment-test"],
        required_components=["modal"],
        budget_usd=1000,
    )
    state = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(request, business)
    )
    selection = state["optimization"]["components"]["modal"]["selected"]
    assert sum(s["units"] for s in selection) == 12
    assert all(s["units"] <= s["capacity_units"] for s in selection)
    assert state["status"] == "PENDING_APPROVAL" and state["execution"] is None


def test_expired_approval_is_expired_never_rejected(cluster, store, business):
    state = build_exochain_graph(store, cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
            ),
            business,
        )
    )
    state["approval"]["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
    with store.connect() as db:
        db.execute(
            "UPDATE runs SET state=? WHERE run_id=?",
            (json.dumps(state), state["run_id"]),
        )
    result = decide(
        store,
        state["run_id"],
        ApprovalDecision(
            plan_hash=state["approval"]["plan_hash"],
            decision="APPROVED",
            reason="TEST_ONLY",
        ),
        "test-approver",
    )
    assert result["status"] == "EXPIRED"
    with pytest.raises(ValueError):
        execute(store, state["run_id"], "test-operator")


def test_independent_validation_detects_price_mutation(cluster, business):
    state = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
            ),
            business,
        )
    )
    state["optimization"]["selected_actions"][0]["unit_cost_usd"] = 1
    assert "ACTION_QUOTE_MISMATCH" in validate(state).reasons


def test_provider_failure_retains_stale_evidence_without_secrets():
    provider = CachedProvider(
        "TEST",
        lambda: ProviderEnvelope(
            source="TEST", entity_id="x", quality="VALID", payload={"observed": True}
        ),
        0,
    )
    provider.fetch()

    def fail():
        raise RuntimeError("secret-must-not-appear")

    provider.loader = fail
    result = provider.fetch()
    assert result.quality == "STALE" and result.last_known_data
    assert "secret-must-not-appear" not in result.model_dump_json()
    assert provider.fetch().errors == ["CIRCUIT_OPEN"]


def test_legacy_operator_tokens_are_not_required():
    Settings(OPERATOR_API_TOKEN="", APPROVER_API_TOKEN="")
    Settings(OPERATOR_API_TOKEN="same", APPROVER_API_TOKEN="same")


def test_model_unavailable_without_approved_deployment(monkeypatch):
    monkeypatch.setattr(settings, "STGNN_MODEL_MANIFEST", "")
    output = validated_forecast(
        DataSnapshot(quality=DataQualityReport(status="UNAVAILABLE"))
    )
    assert output.status == "MODEL_UNAVAILABLE" and output.output["trajectories"] == []


def test_api_e2e_pending_approval_and_immutable_what_if(
    store, registry, cluster, business, monkeypatch
):
    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr("operator-test-only"))
    monkeypatch.setattr(settings, "APPROVER_API_TOKEN", SecretStr("approver-test-only"))
    monkeypatch.setattr(settings, "ERP_MODE", "simulation")
    client = TestClient(create_app(store, registry, cluster))
    operator = {"Authorization": "Bearer operator-test-only"}
    approver = {"Authorization": "Bearer approver-test-only"}
    request = RunRequest(
        operation="REPLENISHMENT",
        inventory_ids=["inv-test"],
        budget_usd=1000,
        simulation=True,
    ).model_dump(mode="json")
    response = client.post(
        "/api/v1/runs",
        headers=operator,
        json={"request": request, "business_inputs": business.model_dump(mode="json")},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = client.get(f"/api/v1/runs/{run_id}", headers=operator).json()
        if state["status"] == "PENDING_APPROVAL":
            break
        time.sleep(0.02)
    assert state["status"] == "PENDING_APPROVAL"
    assert (
        client.post(f"/api/v1/runs/{run_id}/execute", headers=operator).status_code
        == 409
    )
    decision = {
        "plan_hash": state["approval"]["plan_hash"],
        "decision": "APPROVED",
        "reason": "TEST_ONLY",
    }
    assert (
        client.post(
            f"/api/v1/runs/{run_id}/approval", json=decision
        ).status_code
        == 200
    )
    response = client.post(f"/api/v1/runs/{run_id}/execute", headers=operator)
    assert response.json()["status"] == "EXECUTION_SIMULATED"
    assert (
        client.post(f"/api/v1/runs/{run_id}/execute", headers=operator).json()
        == response.json()
    )
    child = client.post(
        f"/api/v1/runs/{run_id}/what-if", headers=operator, json={"request": request}
    )
    assert child.status_code == 202
    child_id = child.json()["run_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        replay = client.get(f"/api/v1/runs/{child_id}", headers=operator).json()
        if replay["status"] == "WHAT_IF_COMPLETE":
            break
        time.sleep(0.02)
    assert replay["data"] == state["data"]
    assert replay["approval"] is None and replay["execution"] is None
    assert store.get(run_id)["status"] == "EXECUTION_SIMULATED"
