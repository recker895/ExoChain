from datetime import timedelta
from core.schemas.contracts import (
    Coordinate,
    NavigationEdge,
    NavigationNetwork,
    NavigationNode,
    ObjectiveWeights,
    RunRequest,
    ScenarioAssumptions,
    Shipment,
    utcnow,
)
from orchestrator.supervisor import build_exochain_graph, initial_state
from optimization.navigation import generate_routes, score_routes


def test_inventory_feeds_procurement_exact_quantities(cluster, business):
    result = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
            ),
            business,
        )
    )
    plan = result["optimization"]
    assert plan["status"] == "OPTIMAL"
    assert plan["components"]["inventory"]["selected"][0]["order_units"] == 15
    assert sum(r["units"] for r in plan["components"]["procurement"]["selected"]) == 15
    assert plan["total_cost_usd"] == 77
    assert result["status"] == "PENDING_APPROVAL"
    assert result["execution"] is None


def test_no_supplier_blocks(cluster, business):
    business.supplier_quotes = []
    business.suppliers = []
    result = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
            ),
            business,
        )
    )
    assert result["status"] == "BLOCKED"
    assert "PROCUREMENT_UNAVAILABLE" in result["optimization"]["constraint_violations"]
    assert result["approval"] is None


def test_budget_below_minimum_positive_order_blocks_without_fake_no_action(
    cluster, business
):
    result = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1
            ),
            business,
        )
    )
    assert result["optimization"]["status"] == "BLOCKED"
    assert (
        "NO_AFFORDABLE_ELIGIBLE_PROCUREMENT_ACTION"
        in result["optimization"]["constraint_violations"]
    )
    assert result["optimization"]["components"]["inventory"]["status"] == "OPTIMAL"
    assert result["optimization"]["total_cost_usd"] == 0
    assert result["approval"] is None


def test_route_requires_network(metadata):
    now = utcnow()
    shipment = Shipment(
        id="ship",
        sku_id="sku",
        quantity=12,
        origin_id="a",
        destination_id="b",
        departure_at=now,
        due_at=now + timedelta(hours=12),
        draft_m=5,
        max_speed_knots=25,
        **metadata,
    )
    candidates, errors, _ = generate_routes(None, shipment, ObjectiveWeights())
    assert not candidates and errors == ["ROUTE_NETWORK_UNAVAILABLE"]


def test_route_geometry_restrictions_alternatives_and_explanation(metadata):
    now = utcnow()
    a = Coordinate(latitude=0, longitude=0)
    b = Coordinate(latitude=0, longitude=1)
    c = Coordinate(latitude=1, longitude=0)

    def edge(id, origin, dest, geometry, cost, draft=10):
        return NavigationEdge(
            id=id,
            origin_id=origin,
            destination_id=dest,
            geometry=geometry,
            distance_km=100,
            duration_hours=3,
            cost_usd=cost,
            fuel_litres=30,
            risk_score=0.1,
            weather_penalty=0.2,
            current_penalty=0.1,
            max_draft_m=draft,
            max_speed_knots=25,
            open=True,
            **metadata,
        )

    network = NavigationNetwork(
        id="net",
        navigational_authority="TEST_ONLY",
        nodes=[
            NavigationNode(id=id, position=p)
            for id, p in [("a", a), ("b", b), ("c", c)]
        ],
        edges=[
            edge("ab", "a", "b", [a, b], 200),
            edge("ac", "a", "c", [a, c], 40),
            edge("cb", "c", "b", [c, b], 40),
            edge("bad", "a", "b", [a, b], 1, draft=1),
        ],
        **metadata,
    )
    shipment = Shipment(
        id="ship",
        sku_id="sku",
        quantity=12,
        origin_id="a",
        destination_id="b",
        departure_at=now,
        due_at=now + timedelta(hours=12),
        draft_m=5,
        max_speed_knots=25,
        **metadata,
    )
    candidates, errors, meta = generate_routes(network, shipment, ObjectiveWeights())
    assert len(candidates) == 2 and not errors
    assert meta["rejected_edges"] == [{"edge_id": "bad", "reason": "DRAFT_RESTRICTION"}]
    ranked = score_routes(
        candidates,
        ObjectiveWeights(cost=10, time=0, fuel=0, risk=0, weather=0, current=0),
    )
    assert ranked[0]["edge_ids"] == ["ac", "cb"]
    assert abs(sum(ranked[0]["objective_percentages"].values()) - 100) < 1e-8
    assert ranked[0]["geometry"] == [a.model_dump(), c.model_dump(), b.model_dump()]


def test_scenario_assumptions_are_labeled(cluster, business):
    scenario = ScenarioAssumptions(
        source="TEST_ONLY",
        explanation="Test distribution",
        cost_std_fraction=0.1,
        duration_std_fraction=0.1,
        disruption_probability=0.1,
        disruption_cost_multiplier=1.5,
        disruption_time_multiplier=2,
        iterations=100,
    )
    request = RunRequest(
        operation="REPLENISHMENT",
        inventory_ids=["inv-test"],
        budget_usd=1000,
        scenarios=scenario,
    )
    result = build_exochain_graph(data_cluster=cluster).invoke(
        initial_state(request, business)
    )
    outcome = result["optimization"]["scenario"]
    assert outcome["not_measured_uncertainty"]
    assert (
        outcome["statistics"]["cost_usd"]["p95"]
        >= outcome["statistics"]["cost_usd"]["p50"]
    )
