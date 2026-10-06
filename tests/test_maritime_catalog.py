"""Sourced static identity/routing checks; only test functions use provider fixtures."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.schemas.contracts import ProviderEnvelope, Quality, RunRequest
from dashboard.backend.main import create_app
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.maritime_catalog import catalog, validate_ports
from services.providers.arcnautical_adapter import compute_arcnautical_route
from services.route_evidence import _record


def test_catalog_is_sourced_and_imo_is_stable_identity():
    values = catalog()
    assert len(values["ports"]) == 20 and len(values["vessels"]) >= 10
    assert len({v["imo"] for v in values["vessels"]}) == len(values["vessels"])
    for vessel in values["vessels"]:
        assert vessel["source"].startswith("https://") and vessel["verified_at"]
        assert len(vessel["imo"]) == 7 and len(vessel["mmsi"]) == 9
        assert not {"position", "speed", "fuel_curve"} & vessel.keys()
    for port in values["ports"]:
        assert -90 <= port["lat"] <= 90 and -180 <= port["lon"] <= 180
        assert port["source"].startswith("https://")


@pytest.mark.parametrize("origin,destination", [("CNSHG", "KRPUS"), ("HKHKG", "LKCMB"), ("USLAX", "USNYC")])
def test_real_routing_package_uses_selected_port_coordinates(origin, destination):
    result = compute_arcnautical_route(origin, destination, speed_knots=16)["result"]
    assert result["origin_locode"] == origin and result["dest_locode"] == destination
    assert result["duration_hours"] == pytest.approx(result["distance_nm"] / 16)
    points = result["route_geojson"]["features"][0]["geometry"]["coordinates"]
    ports = {p["locode"]: p for p in catalog()["ports"]}
    assert points[0] == pytest.approx([ports[origin]["lon"], ports[origin]["lat"]])
    assert points[-1] == pytest.approx([ports[destination]["lon"], ports[destination]["lat"]])
    assert len(points) > 2


@pytest.mark.parametrize("origin,destination", [("SGSIN", "SGSIN"), ("ZZZZZ", "NLRTM"), ("CNSHA", "NLRTM"), ("CNNGB", "SGSIN")])
def test_bad_or_unsupported_ports_are_not_faked(origin, destination):
    with pytest.raises(ValueError):
        validate_ports(origin, destination)


@pytest.mark.parametrize("unit,value,expected", [("m/s", 0.9, 0.9), ("km/h", 3.6, 1), ("kn", 1, 1852 / 3600), ("knots", 2, 3704 / 3600)])
def test_current_units_are_converted_and_original_provider_value_retained(unit, value, expected):
    now = datetime.now(timezone.utc)
    row = _record({"latitude": 1, "longitude": 2,
        "current": {"time": now.isoformat(), "ocean_current_velocity": value, "ocean_current_direction": 180},
        "current_units": {"ocean_current_velocity": unit, "ocean_current_direction": "°"}},
        {"latitude": 1, "longitude": 2, "heading_deg": 0}, "ocean", now)
    assert row["status"] == "VALID"
    assert row["values"]["ocean_current_velocity"] == pytest.approx(expected)
    assert row["along_track_ms"] == pytest.approx(-expected)
    assert row["original_values"]["ocean_current_velocity"] == value
    assert row["original_units"]["ocean_current_velocity"] == unit
    assert row["units"]["ocean_current_velocity"] == "m/s"


def test_unknown_current_units_stay_unavailable():
    now = datetime.now(timezone.utc)
    row = _record({"latitude": 1, "longitude": 2, "current": {"time": now.isoformat(),
        "ocean_current_velocity": 3, "ocean_current_direction": 180}, "current_units": {"ocean_current_velocity": "unknown"}},
        {"latitude": 1, "longitude": 2, "heading_deg": 0}, "ocean", now)
    assert row["status"] == "UNAVAILABLE" and "along_track_ms" not in row


def test_selected_vessel_ports_and_corridor_reach_actual_agents(cluster, registry, monkeypatch):
    vessels, endpoints, samples = [], [], []
    def lookup(reference):
        vessels.append(reference.model_dump())
        return ProviderEnvelope(source="TEST_ONLY_MISSING_AIS", entity_id=reference.mmsi, quality=Quality.UNAVAILABLE)
    def ports(locodes):
        endpoints.append(locodes)
        return ProviderEnvelope(source="TEST_ONLY_MISSING_PORTS", entity_id="endpoints", quality=Quality.UNAVAILABLE)
    def environment(self, points, *, domain):
        samples.append((domain, points))
        raise ConnectionError("TEST_ONLY_OFFLINE")
    monkeypatch.setattr(registry, "selected_vessel", lookup, raising=False)
    monkeypatch.setattr(registry, "route_ports", ports, raising=False)
    monkeypatch.setattr("agents.data.weather_agent.WeatherDataAgent.fetch_route_snapshot", environment)
    for vessel, origin, destination in [(catalog()["vessels"][0], "CNSHG", "KRPUS"), (catalog()["vessels"][2], "HKHKG", "LKCMB")]:
        shipment = f"MARITIME-{origin}-{destination}-{vessel['imo']}"
        request = RunRequest(operation="TRANSPORT", demo_mode=True, required_components=["route"],
            shipment_ids=[shipment], max_risk=1, require_human_approval=False,
            vessel_reference={"shipment_id": shipment, "imo": vessel["imo"], "mmsi": vessel["mmsi"], "vessel_name": vessel["name"]})
        state = build_exochain_graph(data_cluster=cluster).invoke(initial_state(request, route_input={
            "origin_locode": origin, "destination_locode": destination, "shipment_id": shipment, "planning_speed_knots": 14}))
        assert state["status"] == "ROUTE_READY", state["validation"]
        assert vessels[-1]["mmsi"] == vessel["mmsi"] and vessels[-1]["imo"] == vessel["imo"]
        assert set(endpoints[-1]) == {origin, destination}
        context = state["data"]["route_context"]
        assert context["selection"]["departure"]["locode"] == origin
        assert context["selection"]["vessel"]["imo"] == vessel["imo"]
        assert len([r for stage in ("data", "intelligence", "decisions") for r in state[stage]["executions"]]) == 19
        assert state["data"]["sources"]["selected_vessel"]["quality"] == "UNAVAILABLE"
        assert state["optimization"]["components"]["route"]["selected"][0]["weather_penalty"] is None
        assert state["execution"] is None
    assert vessels[0]["mmsi"] != vessels[1]["mmsi"]
    assert samples[0][1] != samples[-1][1]
    assert {domain for domain, _ in samples} == {"weather", "waves", "ocean"}


def test_api_rejects_unsupported_port_before_scheduling(store, registry, cluster):
    api = TestClient(create_app(store, registry, cluster))
    request = RunRequest(operation="TRANSPORT", demo_mode=True, required_components=["route"], shipment_ids=["custom"])
    response = api.post("/api/v1/runs", json={"request": request.model_dump(mode="json"),
        "arcnautical_route": {"origin_locode": "ZZZZZ", "destination_locode": "SGSIN", "shipment_id": "custom"}})
    assert response.status_code == 422 and "not supported" in response.json()["detail"]
