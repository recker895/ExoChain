"""Explicit provider fixtures verify plumbing, not claims of live weather."""
import copy
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from config.settings import settings
from core.schemas.contracts import RunRequest, DataSnapshot, utcnow
from dashboard.backend.main import create_app
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.policy import validate
from services.route_evidence import evaluate_route_metrics


ROUTE_INPUT = {"origin_locode": "SGSIN", "destination_locode": "NLRTM",
               "shipment_id": "PUBLIC-ROUTE-DEMO", "planning_speed_knots": 14}


@pytest.fixture
def public_provider_fixture(monkeypatch):
    """Never used outside tests; deliberately distinct route weather for assertions."""
    def fetch(self, points, *, domain="weather"):
        records = []
        for index, (lat, lon) in enumerate(points):
            current = {"time": utcnow().isoformat()}
            units = {}
            if domain == "weather":
                current.update(wind_speed_10m=20 if index < 9 else 2, wind_direction_10m=90, temperature_2m=25)
                units = {"wind_speed_10m": "m/s", "wind_direction_10m": "°", "temperature_2m": "°C"}
            elif domain == "waves":
                current.update(wave_height=1, wave_direction=90, wave_period=8)
                units = {"wave_height": "m", "wave_direction": "°", "wave_period": "s"}
            else:
                current.update(ocean_current_velocity=0.4, ocean_current_direction=45)
                units = {"ocean_current_velocity": "m/s", "ocean_current_direction": "°"}
            records.append({"latitude": lat, "longitude": lon, "current": current, "current_units": units})
        return records
    monkeypatch.setattr("agents.data.weather_agent.WeatherDataAgent.fetch_route_snapshot", fetch)
    return fetch


def request(**changes):
    options = dict(operation="TRANSPORT", demo_mode=True, required_components=["route"],
                   shipment_ids=[ROUTE_INPUT["shipment_id"]], require_human_approval=False, max_risk=1,
                   vessel_reference={"shipment_id": ROUTE_INPUT["shipment_id"], "mmsi": "563275100",
                                     "imo": "9502946", "vessel_name": "MAERSK ENSHI"})
    options.update(changes)
    return RunRequest(**options)


def run(cluster, **changes):
    return build_exochain_graph(data_cluster=cluster).invoke(initial_state(request(**changes), route_input=ROUTE_INPUT))


def test_corridor_evidence_reaches_solver_and_all_functions_execute(cluster, public_provider_fixture, monkeypatch):
    from agents.intelligence import intelligence_agents
    from agents.decision import decision_agents
    called = []
    for domains, prefix in ((intelligence_agents.DOMAINS, "intelligence"), (decision_agents.DOMAINS, "decision")):
        for name, original in list(domains.items()):
            def spy(*args, _function=original, _id=f"{prefix}.{name}", **kwargs):
                called.append(_id)
                return _function(*args, **kwargs)
            monkeypatch.setitem(domains, name, spy)
    state = run(cluster)
    assert len(called) == 13 and len(set(called)) == 13
    records = [a for stage in ("data", "intelligence", "decisions") for a in state[stage]["executions"]]
    assert len(records) == 19 and all(a["started_at"] and a["completed_at"] for a in records)
    assert state["status"] == "ROUTE_READY", state["validation"]
    assert state["approval"] is None and state["execution"] is None
    component = state["optimization"]["components"]["route"]
    assert component["solver"] == "OR-Tools CP-SAT" and component["candidate_count"] == 2
    assert "weather" in component["explanation"]["used_metrics"]
    assert "fuel" in component["explanation"]["excluded_metrics"]
    assert component["selected"][0]["weather_penalty"] is not None
    assert component["selected"][0]["fuel_litres"] is None
    assert component["selected"][0]["metric_evidence"]["weather_penalty"]["references"]
    assert state["data"]["sources"]["weather"]["payload"]["requested_points"] == 18
    assert len(state["decisions"]["routes"]) == 2


def test_preferences_change_selected_route_from_actual_candidate_metrics(cluster, public_provider_fixture):
    weights = {key: 0 for key in request().weights.model_dump()}
    shortest = run(cluster, weights={**weights, "distance": 1})
    calmer = run(cluster, weights={**weights, "weather": 1})
    assert shortest["optimization"]["components"]["route"]["selected"][0]["planning_label"] == "Default geographic route"
    assert calmer["optimization"]["components"]["route"]["selected"][0]["planning_label"] == "Via Cape of Good Hope"
    assert calmer["status"] == "ROUTE_READY"


def test_missing_data_is_excluded_not_zero_and_no_erp(cluster, monkeypatch):
    monkeypatch.setattr("agents.data.weather_agent.WeatherDataAgent.fetch_route_snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("TEST_OFFLINE")))
    state = run(cluster)
    assert state["status"] == "ROUTE_READY"
    selected = state["optimization"]["components"]["route"]["selected"][0]
    assert selected["risk_score"] is None and selected["weather_penalty"] is None
    assert selected["used_metrics"] == ["time"]
    assert state["execution"] is None


def test_real_user_duration_constraint_is_respected(cluster, public_provider_fixture):
    state = run(cluster, max_duration_hours=1)
    assert state["status"] == "NO_FEASIBLE_ROUTE"
    component = state["optimization"]["components"]["route"]
    assert component["status"] == "INFEASIBLE" and not component["selected"]
    assert all("DURATION_LIMIT" in r["reasons"] for r in component["rejected"])


def test_metric_tampering_is_rejected(cluster, public_provider_fixture):
    state = run(cluster)
    tampered = copy.deepcopy(state)
    selected = tampered["optimization"]["components"]["route"]["selected"]
    route = next(r for r in tampered["decisions"]["routes"]
                 if r["candidate_id"] == selected[0]["candidate_id"])
    route["weather_penalty"] = 0.001
    for item in selected:
        if item["candidate_id"] == route["candidate_id"]:
            item["weather_penalty"] = 0.001
    assert not validate(tampered).valid


def test_stale_or_asymmetric_evidence_never_fills_nulls(cluster, public_provider_fixture):
    state = run(cluster)
    snapshot = DataSnapshot.model_validate(state["data"])
    source = snapshot.sources["weather"]
    source.valid_until = utcnow() - timedelta(hours=1)
    reports = evaluate_route_metrics(snapshot)
    assert all(r["metrics"]["weather_penalty"] is None for r in reports.values())
    source.valid_until = utcnow() + timedelta(hours=1)
    first = next(iter(source.payload["routes"]))
    for row in source.payload["routes"][first][2:]:
        row["status"] = "UNAVAILABLE"
    reports = evaluate_route_metrics(snapshot)
    assert all(r["metrics"]["weather_penalty"] is None for r in reports.values())


def test_async_api_reports_route_ready_and_live_agent_events(store, registry, cluster, public_provider_fixture, monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr(""))
    api = TestClient(create_app(store, registry, cluster))
    headers = {}
    response = api.post("/api/v1/runs", headers=headers, json={"request": request().model_dump(mode="json"), "arcnautical_route": ROUTE_INPUT})
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = api.get(f"/api/v1/runs/{run_id}", headers=headers).json()
        if state["status"] in {"ROUTE_READY", "NO_FEASIBLE_ROUTE", "FAILED"}:
            break
        time.sleep(0.05)
    assert state["status"] == "ROUTE_READY", state.get("validation")
    events = state["audit"]
    for record in [a for stage in ("data", "intelligence", "decisions") for a in state[stage]["executions"]]:
        assert any(e["stage"] == record["agent_id"] and e["status"] == "RUNNING" for e in events)
        assert any(e["stage"] == record["agent_id"] and e["status"] == record["status"] for e in events)
    assert state["optimization"]["components"]["route"]["selected"][0]["geometry"]
    assert api.post(f"/api/v1/runs/{run_id}/execute", headers=headers).status_code == 409
