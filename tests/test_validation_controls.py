"""Mutate solver output at the boundary; invalid plans must never reach approval."""

import pytest

from core.schemas.contracts import RunRequest
from optimization.engine import optimize
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.approval_service import execute
from services.erp_client import SimulationERP


@pytest.mark.parametrize(
    "defect",
    [
        "FAILED",
        "PARTIAL",
        "INVALID",
        "INFEASIBLE",
        "missing_component",
        "wrong_run",
        "missing_risk",
        "inventory_balance",
        "inventory_source",
        "ordering_cost",
        "allocation",
        "action",
        "quote_risk",
        "component_skipped",
    ],
)
def test_invalid_optimization_has_no_approval_or_execution(
    store, cluster, business, monkeypatch, defect
):
    def broken(*args):
        plan = optimize(*args)
        assert plan.status == "OPTIMAL"
        if defect in {"FAILED", "PARTIAL", "INVALID", "INFEASIBLE"}:
            plan.status = defect
        elif defect == "missing_component":
            del plan.components["inventory"]
        elif defect == "wrong_run":
            plan.run_id = "other-run"
        elif defect == "missing_risk":
            del plan.components["procurement"].selected[0]["risk_score"]
        elif defect == "inventory_balance":
            plan.components["inventory"].selected[0]["ending_units"] += 1
        elif defect == "inventory_source":
            plan.components["inventory"].selected[0]["expected_demand_units"] += 1
        elif defect == "ordering_cost":
            plan.components["inventory"].selected[0]["ordering_cost_usd_committed"] = 0
            plan.total_cost_usd -= 2
        elif defect == "allocation":
            plan.components["procurement"].selected[0]["units"] += 1
        elif defect == "action":
            plan.selected_actions[0]["quantity"] += 1
        elif defect == "quote_risk":
            plan.components["procurement"].selected[0]["risk_score"] = 0
        elif defect == "component_skipped":
            plan.components["procurement"].status = "NOT_REQUIRED"
        return plan

    monkeypatch.setattr("orchestrator.supervisor.optimize", broken)
    result = build_exochain_graph(store, cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
            ),
            business,
        )
    )
    assert result["status"] == "BLOCKED"
    assert not result["validation"]["valid"]
    assert result["approval"] is None and result["execution"] is None
    assert store.get(result["run_id"]) == result

    class NeverCalled(SimulationERP):
        calls = 0

        def submit_purchase_order(self, command):
            self.calls += 1
            return super().submit_purchase_order(command)

    client = NeverCalled()
    with pytest.raises(ValueError):
        execute(store, result["run_id"], "test", client)
    assert client.calls == 0
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0
