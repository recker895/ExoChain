from datetime import datetime, timedelta

from core.schemas.contracts import (
    BusinessInputs, Coordinate, DataQualityReport, DataSnapshot,
    NavigationEdge, NavigationNetwork, NavigationNode, ProviderEnvelope,
    PortQueueObservation, Quality, RouteCandidate, Shipment, VesselProfile, utcnow,
)
from services.voyage_assessment import (
    _forecast_record, _wave_safety, assess_voyage, route_currents, route_draft,
    route_forecasts, route_fuel, route_ports, route_samples,
)


def scenario():
    now = utcnow()
    meta = dict(source="TEST_ONLY", created_at=now, observed_at=now,
                valid_until=now + timedelta(days=5), version="1",
                data_quality="VALID", provenance=[{
                    "source": "TEST_ONLY", "reference": "fixture", "observed_at": now,
                }])
    a, b = Coordinate(latitude=0, longitude=0), Coordinate(latitude=0, longitude=1)
    edge = NavigationEdge(id="e", origin_id="a", destination_id="b",
                          geometry=[a, b], distance_km=111.12,
                          duration_hours=6, cost_usd=100, fuel_litres=10,
                          risk_score=.2, weather_penalty=0, current_penalty=0,
                          max_draft_m=12, max_speed_knots=20, open=True, **meta)
    network = NavigationNetwork(id="n", navigational_authority="NONE - TEST",
                                planning_classification="GEOGRAPHIC_PLANNING_ONLY",
                                nodes=[NavigationNode(id="a", position=a), NavigationNode(id="b", position=b)],
                                edges=[edge], **meta)
    shipment = Shipment(id="s", sku_id="cargo", quantity=1, origin_id="a",
                        destination_id="b", departure_at=now + timedelta(hours=1),
                        due_at=now + timedelta(days=1), draft_m=8,
                        max_speed_knots=20, vessel_id="v", **meta)
    candidate = RouteCandidate(candidate_id="c", shipment_id="s", network_id="n",
                               edge_ids=["e"], geometry=[a, b], distance_km=111.12,
                               duration_hours=6, cost_usd=100, fuel_litres=10,
                               risk_score=.2, weather_penalty=0, current_penalty=0,
                               provenance=edge.provenance, segments=[edge])
    profile = VesselProfile(id="v", draft_m=8, max_speed_knots=20,
                            fuel_consumption_curve=[
                                {"speed_knots": 5, "fuel_litres_per_hour": 10},
                                {"speed_knots": 15, "fuel_litres_per_hour": 30},
                            ], fuel_curve_classification="AUTHORITATIVE", **meta)
    return candidate, shipment, network, profile


def test_route_timing_and_forecast_horizon():
    route, shipment, _, _ = scenario()
    samples = route_samples(route, shipment, count=3)
    assert samples[0]["longitude"] == 0
    assert samples[-1]["longitude"] == 1
    assert samples[1]["distance_fraction"] == .5
    at = (datetime.fromisoformat(samples[0]["expected_at"]) + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0).isoformat()[:16]
    response = {"latitude": 0, "longitude": 0, "hourly": {
        "time": [at], "wave_height": [2.3], "wave_direction": [90],
        "wave_period": [8],
    }, "hourly_units": {"wave_height": "m"}}
    assert _forecast_record(response, samples[0], marine=True, received_at=utcnow())["values"]["wave_height"] == 2.3
    assert _forecast_record(response, samples[-1], marine=True, received_at=utcnow())["status"] == "UNAVAILABLE"


def test_fuel_is_model_derived_only_with_curve():
    route, _, _, profile = scenario()
    result = route_fuel(route, profile)
    assert result["status"] == "VALID"
    assert result["classification"] == "MODEL_DERIVED"
    assert result["curve_classification"] == "AUTHORITATIVE"
    assert result["total_fuel_litres"] > 0
    assert route_fuel(route, None)["reason"] == "FUEL_DATA_UNAVAILABLE"


def test_draft_never_passes_on_geographic_route():
    route, shipment, network, profile = scenario()
    assert route_draft(route, shipment, network, profile)["status"] == "DATA_REQUIRED"
    route.segments[0].available_depth_m = 7
    assert route_draft(route, shipment, network, profile)["status"] == "FAIL"
    route.segments[0].available_depth_m = 12
    route.segments[0].depth_evidence_reference = "chart-1"
    route.segments[0].restrictions_verified = True
    assert route_draft(route, shipment, network, profile)["status"] == "DATA_REQUIRED"
    network.planning_classification = "NAVIGATION_AUTHORITY"
    assert route_draft(route, shipment, network, profile)["status"] == "PASS"


def test_current_requires_space_and_time_match_and_portwatch_is_not_berth_queue():
    route, shipment, _, _ = scenario()
    samples = route_samples(route, shipment, count=3)
    now = utcnow()
    source = ProviderEnvelope(source="Copernicus Marine", entity_id="currents",
                              quality=Quality.VALID, observed_at=now,
                              valid_until=now + timedelta(days=1), payload={"currents": {
                                  "point": {"valid": True, "latitude": 0, "longitude": 0,
                                            "u_ms": .5, "v_ms": 0}
                              }})
    currents = route_currents(samples, source)
    assert currents[0]["effect"] == "ASSISTS"
    assert currents[-1]["status"] == "UNAVAILABLE"
    source.quality = Quality.STALE
    assert all(r["status"] == "UNAVAILABLE" for r in route_currents(samples, source))
    port_source = ProviderEnvelope(source="IMF PortWatch", entity_id="ports",
                                   quality=Quality.VALID, observed_at=now,
                                   valid_until=now + timedelta(days=1),
                                   payload={"ports": [{"locode": "a", "portid": "1", "portcalls": 12}]})
    ports = route_ports(shipment, port_source)
    assert ports["origin"]["observed_port_activity"]["portcalls"] == 12
    assert ports["origin"]["berth_queue"]["status"] == "UNAVAILABLE"
    assert ports["destination"]["status"] == "UNAVAILABLE"


def test_port_queue_requires_separate_observation_and_wave_threshold():
    route, shipment, _, profile = scenario()
    now = utcnow()
    queue = PortQueueObservation(
        id="queue-1", port_locode="a0000", queued_vessels=4,
        source="TEST_ONLY_PORT_OPERATOR", created_at=now, observed_at=now,
        valid_until=now + timedelta(hours=2), version="1", data_quality="VALID",
        provenance=[{"source": "TEST_ONLY_PORT_OPERATOR", "reference": "fixture", "observed_at": now}],
    )
    shipment.origin_id = "a0000"
    ports = route_ports(shipment, None, [queue])
    assert ports["origin"]["berth_queue"]["queued_vessels"] == 4
    assert ports["origin"]["waiting_time_hours"]["status"] == "UNAVAILABLE"
    assert _wave_safety([{"values": {"wave_height": 2.1}}], profile)["status"] == "DATA_REQUIRED"
    profile.max_significant_wave_height_m = 2
    assert _wave_safety([{"values": {"wave_height": 2.1}}], profile)["status"] == "FAIL"
    assert _wave_safety([{"status": "UNAVAILABLE"}], profile)["status"] == "DATA_REQUIRED"


def test_route_forecast_uses_existing_weather_agent(monkeypatch):
    route, shipment, _, _ = scenario()
    sample = route_samples(route, shipment, count=2)[:1]
    at = (datetime.fromisoformat(sample[0]["expected_at"]) + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0).isoformat()[:16]
    def fake_fetch(self, points, *, marine=False):
        variables = ("wave_height", "wave_direction", "wave_period") if marine else (
            "wind_speed_10m", "wind_direction_10m", "temperature_2m", "precipitation", "weather_code")
        return [{"latitude": points[0][0], "longitude": points[0][1],
                 "hourly": {"time": [at], **{name: [1] for name in variables}}}]
    monkeypatch.setattr("agents.data.weather_agent.WeatherDataAgent.fetch_route_forecast", fake_fetch)
    weather, waves = route_forecasts(sample)
    assert weather[0]["status"] == "VALID"
    assert waves[0]["status"] == "VALID"
    assert weather[0]["classification"] == "MODEL_DERIVED"


def test_assessment_reports_missing_evidence_without_zero_filling():
    route, shipment, network, _ = scenario()
    data = DataSnapshot(quality=DataQualityReport(status=Quality.UNAVAILABLE),
                        business=BusinessInputs(shipments=[shipment], route_network=network))
    report = assess_voyage(route, shipment, network, data, fetch_forecasts=False)
    assert report["summary"]["fuel"] == "UNAVAILABLE"
    assert report["summary"]["weather"] == "UNAVAILABLE"
    assert report["summary"]["currents"] == "UNAVAILABLE"
    assert report["summary"]["waves"] == "UNAVAILABLE"
    assert report["summary"]["draft"] == "DATA_REQUIRED"
    assert report["summary"]["berth_queue"] == "UNAVAILABLE"
    assert "total_fuel_litres" not in report["fuel"]
    assert report["checks_at"]
    assert report["checks"]["fuel"]["execution"] == "CHECKED"
    assert report["checks"]["weather"]["execution"] == "CHECKED"
    assert report["checks"]["weather"]["lookup"] == "SKIPPED"
    assert report["checks"]["waves"]["reason"] == "FORECAST_NOT_FETCHED"
