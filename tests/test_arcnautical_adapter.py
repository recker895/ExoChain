import pytest
import time
from fastapi.testclient import TestClient
from pydantic import SecretStr

from config.settings import settings
from core.schemas.contracts import ObjectiveWeights
from dashboard.backend.main import create_app
from optimization.navigation import generate_routes
from services.providers.arcnautical_adapter import (
    adapt_arcnautical_route,
    compute_arcnautical_route,
)


def test_arcnautical_singapore_rotterdam_adapter():
    raw = compute_arcnautical_route("SGSIN", "NLRTM")
    business = adapt_arcnautical_route(
        raw,
        shipment_id="ARC-SGSIN-NLRTM-TEST",
        cost_usd=1000,
        fuel_litres=1000,
        risk_score=0.2,
        vessel_draft_m=8,
        max_speed_knots=20,
        assumption_source="TEST_ONLY_OPERATOR_ASSUMPTIONS",
    )
    network = business.route_network
    edge = network.edges[0]
    assert network.planning_classification == "GEOGRAPHIC_PLANNING_ONLY"
    assert network.navigational_authority.startswith("NONE")
    assert len(edge.geometry) > 100
    assert edge.geometry[0] == network.nodes[0].position
    assert edge.geometry[-1] == network.nodes[1].position
    assert edge.distance_km == pytest.approx(raw["result"]["distance_nm"] * 1.852)
    assert edge.duration_hours == raw["result"]["duration_hours"]
    assert edge.hazard_zones_crossed == raw["result"]["hazard_zones_crossed"]
    assert edge.provenance[0].source.startswith("ArcNautical")
    assert edge.provenance[1].source == "TEST_ONLY_OPERATOR_ASSUMPTIONS"
    routes, errors, _ = generate_routes(network, business.shipments[0], ObjectiveWeights())
    assert not routes and errors == ["ROUTE_METRICS_DATA_REQUIRED"]
    assert edge.weather_penalty is None and edge.current_penalty is None
    assert edge.max_draft_m is None


def test_public_geographic_alternative_is_rankable_but_not_executable():
    primary = compute_arcnautical_route("SGSIN", "NLRTM")
    cape = compute_arcnautical_route("SGSIN", "NLRTM", via_cape=True)
    business = adapt_arcnautical_route(
        primary, shipment_id="PLANNING-ONLY", alternative_payloads=[cape],
    )
    network = business.route_network
    assert len(network.edges) == 2
    fastest, alternative = sorted(network.edges, key=lambda edge: edge.duration_hours)
    assert fastest.planning_label == "Default geographic route"
    assert alternative.planning_label == "Via Cape of Good Hope"
    assert fastest.duration_hours < alternative.duration_hours
    assert fastest.distance_km < alternative.distance_km
    assert all(edge.cost_usd is None and edge.max_draft_m is None for edge in network.edges)
    assert network.planning_classification == "GEOGRAPHIC_PLANNING_ONLY"


def test_arcnautical_requires_explicit_assumptions():
    raw = {
        "package_version": "1.0.1",
        "result": {
            "origin_locode": "SGSIN", "dest_locode": "NLRTM",
            "distance_nm": 10, "duration_hours": 1,
            "hazard_zones_crossed": [],
            "route_geojson": {"features": [{"geometry": {
                "type": "LineString", "coordinates": [[103, 1], [4, 51]]
            }}]},
        },
    }
    with pytest.raises(ValueError, match="positive and finite"):
        adapt_arcnautical_route(
            raw, shipment_id="test", cost_usd=0, fuel_litres=1,
            risk_score=0.2, vessel_draft_m=8, max_speed_knots=20,
            assumption_source="TEST_ONLY",
        )


def test_arcnautical_api_routes_through_existing_decision_flow(
    store, registry, cluster, monkeypatch
):
    monkeypatch.setattr(
        "services.voyage_assessment.route_forecasts",
        lambda samples: (
            [{"status": "UNAVAILABLE", "reason": "TEST_NO_FORECAST"} for _ in samples],
            [{"status": "UNAVAILABLE", "reason": "TEST_NO_WAVES"} for _ in samples],
        ),
    )
    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr("arc-test-operator"))
    monkeypatch.setattr(settings, "APPROVER_API_TOKEN", SecretStr("arc-test-approver"))
    api = TestClient(create_app(store, registry, cluster))
    response = api.post(
        "/api/v1/runs",
        headers={"Authorization": "Bearer arc-test-operator"},
        json={
            "request": {
                "operation": "TRANSPORT", "shipment_ids": ["ARC-SGSIN-NLRTM-TEST"],
                "required_components": ["route"], "budget_usd": 2000,
                "vessel_reference": {
                    "shipment_id": "ARC-SGSIN-NLRTM-TEST",
                    "mmsi": "563275100", "imo": "9502946",
                    "vessel_name": "MAERSK ENSHI",
                },
            },
            "arcnautical_route": {
                "origin_locode": "SGSIN", "destination_locode": "NLRTM",
                "shipment_id": "ARC-SGSIN-NLRTM-TEST",
            },
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    for _ in range(200):
        state = store.get(run_id)
        if state.get("validation", {}).get("reasons") and state["status"] in {"BLOCKED", "FAILED", "PENDING_APPROVAL"}:
            break
        time.sleep(0.05)
    assert state["status"] == "BLOCKED"
    assert state["optimization"]["components"]["route"]["status"] == "DATA_REQUIRED"
    assessment = state["decisions"]["explanation"]["route_generation"]["ARC-SGSIN-NLRTM-TEST"]["voyage_assessment"]
    assert assessment["summary"]["fuel"] == "UNAVAILABLE"
    assert assessment["summary"]["vessel_state"] == "UNAVAILABLE"
    assert state["request"]["vessel_reference"]["mmsi"] == "563275100"
    assert assessment["summary"]["draft"] == "DATA_REQUIRED"
    assert "GEOGRAPHIC_PLANNING_ONLY_NOT_APPROVED_FOR_NAVIGATION" in state["validation"]["reasons"]
    assert state["approval"] is None
    time.sleep(0.2)


def test_demo_transport_optimizes_without_optional_maritime_evidence(
    store, registry, cluster, monkeypatch,
):
    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr("demo-operator"))
    monkeypatch.setattr(settings, "APPROVER_API_TOKEN", SecretStr("demo-approver"))
    api = TestClient(create_app(store, registry, cluster))
    response = api.post(
        "/api/v1/runs",
        headers={"Authorization": "Bearer demo-operator"},
        json={
            "request": {
                "operation": "TRANSPORT", "demo_mode": True,
                "shipment_ids": ["ARC-SGSIN-NLRTM-DEMO"],
                "required_components": ["route"], "budget_usd": 2000,
                "max_risk": 0.5, "require_human_approval": True,
                "vessel_reference": {
                    "shipment_id": "ARC-SGSIN-NLRTM-DEMO",
                    "mmsi": "563275100", "imo": "9502946",
                    "vessel_name": "MAERSK ENSHI",
                },
            },
            "arcnautical_route": {
                "origin_locode": "SGSIN", "destination_locode": "NLRTM",
                "shipment_id": "ARC-SGSIN-NLRTM-DEMO",
            },
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    for _ in range(500):
        state = store.get(run_id)
        if state["status"] in {"PENDING_APPROVAL", "BLOCKED", "FAILED"}:
            break
        time.sleep(0.05)
    assert state["status"] == "PENDING_APPROVAL", state.get("validation")
    assert state["validation"]["valid"] is True
    assert state["approval"]["cost_usd"] is None
    assert state["execution"] is None
    assert state["optimization"]["status"] == "OPTIMAL"
    component = state["optimization"]["components"]["route"]
    assert component["status"] == "OPTIMAL"
    assert component["solver"] == "OR-Tools CP-SAT"
    assert component["candidate_count"] >= 1
    assert component["selected"][0]["fuel_litres"] is None
    assert component["selected"][0]["cost_usd"] is None
    assert component["selected"][0]["distance_km"] > 0
    assert len(component["selected"][0]["geometry"]) > 100
    assert state["optimization"]["total_cost_usd"] is None
    assert state["decisions"]["routes"]
    assert state["data"]["sources"]["selected_vessel"]["quality"] == "UNAVAILABLE"
    dashboard = api.get(
        f"/api/v1/runs/{run_id}",
        headers={"Authorization": "Bearer demo-operator"},
    )
    assert dashboard.status_code == 200
    shown = dashboard.json()
    assert shown["request"]["vessel_reference"]["vessel_name"] == "MAERSK ENSHI"
    assert shown["optimization"]["components"]["route"]["selected"][0]["geometry"]
    approved = api.post(
        f"/api/v1/runs/{run_id}/approval",
        headers={"Authorization": "Bearer demo-approver"},
        json={"plan_hash": state["approval"]["plan_hash"],
              "decision": "APPROVED", "reason": "Demo review"},
    )
    assert approved.status_code == 200, approved.text
    refused = api.post(
        f"/api/v1/runs/{run_id}/execute",
        headers={"Authorization": "Bearer demo-operator"},
    )
    assert refused.status_code == 409
    assert store.get(run_id)["execution"] is None
