"""Convert ArcNautical geographic output into the existing business route contract.

The package supplies geometry, distance, duration and crossed zones. It does
not supply verified voyage cost, fuel, risk or draft clearance. Callers must
state those separately; the resulting network is always non-executable.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import timedelta
from pathlib import Path

from core.schemas.contracts import BusinessInputs, Coordinate, utcnow
from services.geography import normalize_longitude


BRIDGE = Path(__file__).with_name("arcnautical_route.mjs")
PROJECT_ROOT = BRIDGE.parents[2]
SOURCE = "ArcNautical @arcnautical/maritime-routing"


def compute_arcnautical_route(origin: str, destination: str, *, via_cape: bool = False,
                             speed_knots: float | None = None) -> dict:
    if not all(code.isascii() and code.isalnum() and len(code) == 5 for code in (origin, destination)):
        raise ValueError("Two five-character UN/LOCODEs are required")
    from services.maritime_catalog import validate_ports
    validate_ports(origin, destination)
    options = {"via_waypoints": [{"lat": -34.4, "lon": 18.5}]} if via_cape else {}
    if speed_knots is not None:
        if not math.isfinite(speed_knots) or not 1 <= speed_knots <= 40:
            raise ValueError("Planning speed must be between 1 and 40 knots")
    try:
        completed = subprocess.run(
            ["node", str(BRIDGE), origin.upper(), destination.upper(), json.dumps(options)],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60, check=True,
        )
    except subprocess.CalledProcessError as exc:
        reason = next((line.removeprefix("Error: ") for line in (exc.stderr or "").splitlines()
                       if line.startswith("Error: ")), "No supported sea route could be generated")
        raise ValueError(reason[:300]) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Geographic route generation timed out; try another supported port pair") from exc
    payload = json.loads(completed.stdout)
    if speed_knots is not None:
        payload["package_duration_hours"] = payload["result"]["duration_hours"]
        payload["planning_speed_knots"] = speed_knots
        payload["result"]["duration_hours"] = payload["result"]["distance_nm"] / speed_knots
    return payload


def adapt_arcnautical_route(
    payload: dict,
    *,
    shipment_id: str,
    cost_usd: float | None = None,
    fuel_litres: float | None = None,
    risk_score: float | None = None,
    vessel_draft_m: float | None = None,
    max_speed_knots: float | None = None,
    assumption_source: str | None = None,
    alternative_payloads: list[dict] | None = None,
) -> BusinessInputs:
    """Build a planning-only network; never infer missing business facts."""
    values = (cost_usd, fuel_litres, vessel_draft_m, max_speed_knots)
    if not all(v is None or (math.isfinite(v) and v > 0) for v in values):
        raise ValueError("Cost, fuel, draft and speed assumptions must be positive and finite")
    if risk_score is not None and (not math.isfinite(risk_score) or not 0 <= risk_score <= 1):
        raise ValueError("Risk assumption must be between 0 and 1")
    if not shipment_id:
        raise ValueError("Shipment identity is required")
    if any(value is not None for value in (*values, risk_score)) and not assumption_source:
        raise ValueError("Assumption source is required when assumptions are supplied")
    route = payload["result"]
    origin, destination = route["origin_locode"], route["dest_locode"]
    if origin == destination:
        raise ValueError("Origin and destination must differ")
    now = utcnow()
    route_payloads = [payload] + (alternative_payloads or [])
    fingerprint = hashlib.sha256(json.dumps(route, sort_keys=True).encode()).hexdigest()
    reference = f"{origin}-{destination}:{fingerprint}"
    route_provenance = {
        "source": SOURCE,
        "reference": reference,
        "observed_at": now.isoformat(),
        "transformation": f"GeoJSON [longitude,latitude] to route_network; package {payload['package_version']}; route fingerprint {fingerprint}; duration is distance / planning speed ({payload.get('planning_speed_knots', 14)} knots), not an observed ETA; environmental penalties assessed separately",
    }
    assumption_provenance = {
        "source": assumption_source,
        "reference": f"{shipment_id}:cost-fuel-risk-draft-speed",
        "observed_at": now.isoformat(),
        "transformation": "Explicit planning assumptions; not ArcNautical measurements",
    }
    provenance = [route_provenance] + ([assumption_provenance] if assumption_source else [])
    metadata = {
        "source": SOURCE,
        "created_at": now.isoformat(),
        "observed_at": now.isoformat(),
        "valid_until": now.isoformat(),
        "version": payload["package_version"],
        "data_quality": "VALID",
        "provenance": provenance,
    }
    edges = []
    max_duration = 0.0
    for index, item in enumerate(route_payloads):
        option = item["result"]
        if (option["origin_locode"], option["dest_locode"]) != (origin, destination):
            raise ValueError("Alternative route endpoints must match")
        geometry = option["route_geojson"]["features"][0]["geometry"]
        if geometry["type"] != "LineString":
            raise ValueError("ArcNautical must return a LineString")
        # The provider unwraps Pacific routes beyond +/-180 for continuity.
        # Store equivalent canonical coordinates without modifying its geometry.
        coords = [Coordinate(latitude=lat, longitude=normalize_longitude(lon))
                  for lon, lat in geometry["coordinates"]]
        if len(coords) < 2:
            raise ValueError("ArcNautical route has fewer than two coordinates")
        distance_km = float(option["distance_nm"]) * 1.852
        duration_hours = float(option["duration_hours"])
        if not all(math.isfinite(v) and v > 0 for v in (distance_km, duration_hours)):
            raise ValueError("ArcNautical route distance and duration must be positive")
        max_duration = max(max_duration, duration_hours)
        option_fingerprint = hashlib.sha256(json.dumps(option, sort_keys=True).encode()).hexdigest()
        option_reference = f"{origin}-{destination}:{option_fingerprint}"
        option_provenance = {
            **route_provenance,
            "reference": option_reference,
            "transformation": (
                f"GeoJSON [longitude,latitude] to route_network; package {item['package_version']}; "
                f"original route fingerprint {option_fingerprint}; longitudes wrapped to [-180,180] (same positions); weather/current penalties not modeled; "
                + ("via Cape of Good Hope" if index else "default route")
            ),
        }
        edges.append({
            "id": f"ARCNAUTICAL-EDGE-{option_fingerprint[:16]}",
            "origin_id": origin, "destination_id": destination,
            "geometry": [c.model_dump() for c in coords],
            "distance_km": distance_km, "duration_hours": duration_hours,
            "cost_usd": cost_usd, "fuel_litres": fuel_litres,
            "risk_score": risk_score, "weather_penalty": None,
            "current_penalty": None, "max_draft_m": None,
            "max_speed_knots": max_speed_knots, "open": True,
            "hazard_zones_crossed": list(option.get("hazard_zones_crossed", [])),
            "environmental_references": [],
            "planning_label": "Via Cape of Good Hope" if index else "Default geographic route",
            "provenance": [option_provenance] + ([assumption_provenance] if assumption_source else []),
        })
    expires = now + timedelta(hours=max_duration + 72)
    metadata["valid_until"] = expires.isoformat()
    for edge in edges:
        edge.update({key: metadata[key] for key in ("source", "created_at", "observed_at", "valid_until", "version", "data_quality")})
    hazards = list(route.get("hazard_zones_crossed", []))
    return BusinessInputs.model_validate({
        "shipments": [{
            **metadata, "id": shipment_id, "source": "TEST_ONLY_ROUTE_DERIVED_SHIPMENT",
            "sku_id": "UNSPECIFIED_CARGO", "quantity": 1,
            "origin_id": origin, "destination_id": destination,
            "departure_at": (now + timedelta(hours=1)).isoformat(),
            "due_at": (now + timedelta(hours=max_duration + 24)).isoformat(),
            "draft_m": vessel_draft_m, "max_speed_knots": max_speed_knots,
        }],
        "route_network": {
            **metadata, "id": f"ARCNAUTICAL-{fingerprint[:16]}",
            "navigational_authority": "NONE - geographic planning only; not for navigation",
            "planning_classification": "GEOGRAPHIC_PLANNING_ONLY",
            "hazard_zones_crossed": hazards,
            "nodes": [
                {"id": origin, "position": edges[0]["geometry"][0]},
                {"id": destination, "position": edges[0]["geometry"][-1]},
            ],
            "edges": edges,
        },
    })


def generate_arcnautical_business(route_input, business=None):
    """Execute real routing within the instrumented run, not before submission."""
    origin, destination = route_input["origin_locode"], route_input["destination_locode"]
    speed = route_input.get("planning_speed_knots", 14)
    primary = compute_arcnautical_route(origin, destination, speed_knots=speed)
    alternatives = []
    warnings = []
    crossed = set(primary["result"].get("hazard_zones_crossed", []))
    if {"Red Sea", "Suez Canal"} & crossed:
        try:
            cape = compute_arcnautical_route(origin, destination, via_cape=True, speed_knots=speed)
            if cape["result"]["route_geojson"] != primary["result"]["route_geojson"]:
                alternatives.append(cape)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            warnings.append(f"CAPE_ALTERNATIVE_UNAVAILABLE:{type(exc).__name__}")
    generated = adapt_arcnautical_route(
        primary, shipment_id=route_input["shipment_id"], alternative_payloads=alternatives,
        cost_usd=route_input.get("assumed_cost_usd"),
        fuel_litres=route_input.get("assumed_fuel_litres"),
        risk_score=route_input.get("assumed_risk_score"),
        vessel_draft_m=route_input.get("assumed_vessel_draft_m"),
        max_speed_knots=route_input.get("assumed_max_speed_knots"),
        assumption_source=route_input.get("assumption_source"),
    )
    if business is not None:
        generated = business.model_copy(update={"route_network": generated.route_network})
    return generated, {"origin_locode": origin.upper(), "destination_locode": destination.upper(),
                       "planning_speed_knots": speed, "speed_source": "OPERATOR_PLANNING_ASSUMPTION",
                       "alternative_warnings": warnings}
