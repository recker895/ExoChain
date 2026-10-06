"""Real geographic routes and date-line regressions; no external feed fixtures."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.schemas.contracts import Coordinate
from services.geography import normalize_longitude
from services.maritime_catalog import catalog
from services.providers.arcnautical_adapter import compute_arcnautical_route, adapt_arcnautical_route
from services.voyage_assessment import route_samples


@pytest.mark.parametrize("value,expected", [(-180.025, 179.975), (180.025, -179.975),
    (-540.025, 179.975), (540.025, -179.975), (180, 180), (-180, -180), (72.84, 72.84)])
def test_wrapping_preserves_meridian_without_clamping(value, expected):
    assert normalize_longitude(value) == pytest.approx(expected)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_coordinates_still_fail(value):
    with pytest.raises(ValueError, match="finite"):
        normalize_longitude(value)


@pytest.mark.parametrize("first,last", [(179.9, -179.9), (-179.9, 179.9)])
def test_environment_samples_take_short_crossing_not_greenwich(first, last):
    points = [Coordinate(latitude=20, longitude=first), Coordinate(latitude=20, longitude=last)]
    route = SimpleNamespace(geometry=points, duration_hours=1)
    shipment = SimpleNamespace(departure_at=datetime.now(timezone.utc))
    samples = route_samples(route, shipment, count=3)
    assert abs(samples[1]["longitude"]) == pytest.approx(180)
    assert samples[0]["longitude"] == pytest.approx(first)
    assert samples[-1]["longitude"] == pytest.approx(last)


@pytest.mark.parametrize("origin,destination", [("USNYC", "HKHKG"), ("HKHKG", "USNYC")])
def test_actual_pacific_route_adapts_and_samples_without_coordinate_error(origin, destination):
    payload = compute_arcnautical_route(origin, destination, speed_knots=14)
    original = copy.deepcopy(payload)
    business = adapt_arcnautical_route(payload, shipment_id="dateline-regression")
    edge = business.route_network.edges[0]
    raw = payload["result"]["route_geojson"]["features"][0]["geometry"]["coordinates"]
    assert len(edge.geometry) == len(raw)
    assert all(-180 <= p.longitude <= 180 for p in edge.geometry)
    assert edge.distance_km == pytest.approx(payload["result"]["distance_nm"] * 1.852)
    assert edge.duration_hours == pytest.approx(payload["result"]["distance_nm"] / 14)
    assert payload == original  # Original provider geometry/provenance is retained.
    samples = route_samples(edge, business.shipments[0], count=101)
    assert all(-180 <= p["longitude"] <= 180 for p in samples)
    assert all(-90 <= p["latitude"] <= 90 for p in samples)
    assert any(abs(p["longitude"]) > 175 for p in samples)


@pytest.mark.parametrize("origin", ["INBOM", "INNSA", "INMAA", "INMUN", "INVTZ", "INTUT"])
def test_indian_dropdown_ports_generate_actual_routes(origin):
    payload = compute_arcnautical_route(origin, "SGSIN", speed_knots=14)
    business = adapt_arcnautical_route(payload, shipment_id=f"india-{origin}")
    edge = business.route_network.edges[0]
    port = next(p for p in catalog()["ports"] if p["locode"] == origin)
    assert len(edge.geometry) > 2
    assert edge.geometry[0].longitude == pytest.approx(port["lon"])
    assert edge.geometry[0].latitude == pytest.approx(port["lat"])
    assert edge.origin_id == origin and edge.destination_id == "SGSIN"
    assert edge.distance_km > 0 and edge.duration_hours > 0
