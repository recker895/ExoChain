import copy
import pytest

from core.schemas.contracts import RunRequest
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.policy import validate


def solve(cluster, business, **kwargs):
    values = dict(
        operation="REPLENISHMENT",
        inventory_ids=["inv-test"],
        budget_usd=1000,
        required_components=["inventory", "procurement"],
        require_human_approval=True,
    )
    values.update(kwargs)
    return build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(RunRequest(**values), business)
    )


def test_reference_price_does_not_falsely_block_affordable_quotes(cluster, business):
    business.inventory[0].unit_cost_usd = 1000
    state = solve(cluster, business, budget_usd=100)
    assert state["status"] == "PENDING_APPROVAL"
    assert state["optimization"]["total_cost_usd"] == 77
    assert state["validation"]["valid"]


@pytest.mark.parametrize(
    "constraint,expected_order,expected_shortage,expected_shortfall",
    [
        ("budget", 9, 1, 5),
        ("capacity", 12, 0, 3),
        ("economics", 15, 0, 0),
        ("warehouse", 10, 0, 5),
    ],
)
def test_joint_model_uses_permitted_shortage(
    cluster, business, constraint, expected_order, expected_shortage, expected_shortfall
):
    item = business.inventory[0]
    item.service_level = 0.5
    options = {}
    if constraint == "budget":
        business.supplier_quotes[0].unit_cost_usd = 10
        item.unit_cost_usd = 1
        options["budget_usd"] = 100
    elif constraint == "capacity":
        business.supplier_quotes[0].capacity_units = 12
    elif constraint == "economics":
        item.shortage_penalty_usd = 1
    else:
        options["warehouse_capacity_units"] = 20
    state = solve(cluster, business, **options)
    assert state["optimization"]["status"] == "OPTIMAL"
    assert state["status"] == ("BLOCKED" if expected_shortfall else "PENDING_APPROVAL")
    chosen = state["optimization"]["components"]["inventory"]["selected"][0]
    assert chosen["order_units"] == expected_order
    assert chosen["shortage_units"] == expected_shortage
    assert chosen["ending_units"] == item.safety_stock_units - expected_shortfall
    assert chosen["safety_stock_shortfall_units"] == expected_shortfall
    assert (
        chosen["achieved_service_level"]
        == 1 - expected_shortage / item.expected_demand_units
    )
    assert state["optimization"]["total_cost_usd"] <= options.get("budget_usd", 1000)
    assert (
        sum(a["quantity"] for a in state["optimization"]["selected_actions"])
        == expected_order
    )
    tampered = copy.deepcopy(state)
    tampered["optimization"]["components"]["inventory"]["selected"][0][
        "ending_units"
    ] += 1
    assert "INVENTORY_CONSTRAINT_VIOLATION" in validate(tampered).reasons


@pytest.mark.parametrize("defect", ["risk", "lead_time", "warehouse", "capacity"])
def test_hard_constraints_remain_binding(cluster, business, defect):
    options = {}
    if defect == "risk":
        business.supplier_quotes[0].risk_score = 0.9
    elif defect == "lead_time":
        business.supplier_quotes[0].lead_time_days = 3
    elif defect == "capacity":
        business.supplier_quotes[0].capacity_units = 14
    else:
        options["warehouse_capacity_units"] = 24
    state = solve(cluster, business, **options)
    assert state["status"] == "BLOCKED"
    assert state["approval"] is None
    if defect in {"risk", "lead_time"}:
        assert not state["optimization"]["selected_actions"]
    else:
        assert state["optimization"]["status"] == "OPTIMAL"
        assert (
            sum(a["quantity"] for a in state["optimization"]["selected_actions"]) == 14
        )
        assert "INVENTORY_CONSTRAINT_VIOLATION" in state["validation"]["reasons"]


def test_shared_quote_capacity_and_partial_budget(cluster, business):
    # Two independently sourced positions, one shared quote: capacity is not per row.
    business.inventory.append(
        business.inventory[0].model_copy(update={"id": "other-position"})
    )
    business.supplier_quotes[0].capacity_units = 25
    state = solve(cluster, business, inventory_ids=["inv-test", "other-position"])
    assert state["optimization"]["status"] == "OPTIMAL"
    assert sum(a["quantity"] for a in state["optimization"]["selected_actions"]) == 25
    assert (
        state["optimization"]["explanation"]["replenishment"][
            "safety_stock_shortfall_units"
        ]
        == 5
    )
    assert state["status"] == "BLOCKED" and state["approval"] is None
    business.supplier_quotes[0].capacity_units = 30
    state = solve(
        cluster, business, inventory_ids=["inv-test", "other-position"], budget_usd=100
    )
    assert state["optimization"]["status"] == "OPTIMAL"
    assert state["optimization"]["total_cost_usd"] == 99
    assert sum(a["quantity"] for a in state["optimization"]["selected_actions"]) == 19
    assert state["approval"] is None
    valid = solve(
        cluster, business, inventory_ids=["inv-test", "other-position"], budget_usd=154
    )
    assert valid["status"] == "PENDING_APPROVAL"


def test_soft_targets_preserve_true_physical_infeasibility(cluster, business):
    state = solve(cluster, business, warehouse_capacity_units=9)
    assert state["optimization"]["status"] == "INFEASIBLE"
    assert (
        "REPLENISHMENT_CONSTRAINTS_INFEASIBLE"
        in state["optimization"]["constraint_violations"]
    )
    assert state["approval"] is None and state["execution"] is None


def test_safety_target_does_not_create_artificial_demand_shortage(cluster, business):
    business.inventory[0].service_level = 0.1
    business.inventory[0].safety_stock_units = 15
    state = solve(cluster, business, budget_usd=52)
    selected = state["optimization"]["components"]["inventory"]["selected"][0]
    assert selected["order_units"] == 10 and selected["shortage_units"] == 0
    assert (
        selected["ending_units"] == 0 and selected["safety_stock_shortfall_units"] == 15
    )
    assert state["optimization"]["total_cost_usd"] == 52


def test_zero_demand_service_level_and_exact_safety_shortfall(cluster, business):
    position = business.inventory[0]
    position.expected_demand_units = 0
    position.safety_stock_units = 20
    state = solve(cluster, business, budget_usd=27)
    selected = state["optimization"]["components"]["inventory"]["selected"][0]
    assert selected["order_units"] == 5 and selected["ending_units"] == 15
    assert selected["achieved_service_level"] == 1.0
    assert selected["safety_stock_shortfall_units"] == 5
    assert selected["committed_cost_usd"] == 27


def test_secondary_timeout_retains_feasible_incumbent_without_false_optimal(
    cluster, business, monkeypatch
):
    import optimization.engine as engine
    from ortools.sat.python import cp_model

    original = engine.solver
    calls = []

    class Timeout:
        def Solve(self, model):
            return cp_model.UNKNOWN

        def StatusName(self, status):
            return "UNKNOWN"

    def limited():
        calls.append(1)
        return original() if len(calls) == 1 else Timeout()

    monkeypatch.setattr(engine, "solver", limited)
    state = solve(cluster, business)
    plan = state["optimization"]
    assert plan["status"] == "FEASIBLE" and plan["total_cost_usd"] <= 1000
    stages = plan["explanation"]["replenishment"]["lexicographic_stages"]
    assert stages[0]["status"] == "OPTIMAL" and stages[1]["status"] == "UNKNOWN"
    chosen = plan["components"]["inventory"]["selected"][0]
    assert chosen["shortage_units"] == 0
    assert sum(a["quantity"] for a in plan["selected_actions"]) == chosen["order_units"]


def test_approval_retry_is_idempotent_and_actor_bound(store, cluster, business):
    from core.schemas.contracts import ApprovalDecision
    from services.approval_service import decide

    state = build_exochain_graph(store, cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
            ),
            business,
        )
    )
    decision = ApprovalDecision(
        decision="APPROVED",
        reason="TEST_ONLY",
        plan_hash=state["approval"]["plan_hash"],
    )
    first = decide(store, state["run_id"], decision, "approver:test")
    event_count = len(store.events(state["run_id"]))
    second = decide(store, state["run_id"], decision, "approver:test")
    assert (
        first["approval"] == second["approval"]
        and len(store.events(state["run_id"])) == event_count
    )
    with pytest.raises(ValueError):
        decide(store, state["run_id"], decision, "approver:other")
