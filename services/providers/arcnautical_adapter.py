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


BRIDGE = Path(__file__).with_name("arcnautical_route.mjs")
PROJECT_ROOT = BRIDGE.parents[2]
SOURCE = "ArcNautical @arcnautical/maritime-routing"


def compute_arcnautical_route(origin: str, destination: str, *, via_cape: bool = False) -> dict:
    if not all(code.isascii() and code.isalnum() and len(code) == 5 for code in (origin, destination)):
        raise ValueError("Two five-character UN/LOCODEs are required")
    completed = subprocess.run(
        ["node", str(BRIDGE), origin.upper(), destination.upper(),
         json.dumps({"via_waypoints": [{"lat": -34.4, "lon": 18.5}]}) if via_cape else "{}"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(completed.stdout)


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
        "transformation": f"GeoJSON [longitude,latitude] to route_network; package {payload['package_version']}; original route fingerprint {fingerprint}; weather/current penalties not modeled",
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
        coords = [Coordinate(latitude=lat, longitude=lon) for lon, lat in geometry["coordinates"]]
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
                f"original route fingerprint {option_fingerprint}; weather/current penalties not modeled; "
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
