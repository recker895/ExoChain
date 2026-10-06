"""Public-data corridor planning. No synthetic telemetry or commercial facts.

Compare current model conditions at equally spaced route fractions. This is a
planning snapshot, NOT weather at future passage times or nautical clearance.
Missing metrics stay null. Comparative indices are explicit, uncalibrated
educational scores, never probabilities of loss or vessel safety certificates.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import math
import statistics
from types import SimpleNamespace

from agents.data.weather_agent import WeatherDataAgent
from core.schemas.contracts import ProviderEnvelope, Quality, utcnow
from services.feature_engineering.environment import current_effect, distance_km
from services.voyage_assessment import route_samples, route_fuel, route_draft, route_ports, selected_vessel_evidence

METHOD = "Current model snapshot at common route fractions; wind/25 m/s, wave/8 m, adverse along-track current/2 m/s; bounded to [0,1]. Risk = mean of available indices; not a failure probability."


def planning_context(business, request, routing=None):
    network = business.route_network
    if not (request and request.demo_mode and request.operation == "TRANSPORT"
            and network and network.planning_classification == "GEOGRAPHIC_PLANNING_ONLY"):
        return {}
    now = utcnow()
    routes = {}
    shipments = {s.id: s for s in business.shipments if s.id in request.shipment_ids}
    for edge in network.edges:
        shipment = next((s for s in shipments.values() if
                        (s.origin_id, s.destination_id) == (edge.origin_id, edge.destination_id)), None)
        if shipment is None:
            continue
        samples = route_samples(SimpleNamespace(geometry=edge.geometry, duration_hours=edge.duration_hours), shipment)
        for sample in samples:
            sample["estimated_passage_at"] = sample["expected_at"]
            sample["expected_at"] = now.isoformat()
        routes[edge.id] = {"label": edge.planning_label, "shipment_id": shipment.id,
                          "samples": samples, "hazards": edge.hazard_zones_crossed,
                          "distance_km": edge.distance_km, "duration_hours": edge.duration_hours}
    from services.maritime_catalog import selection_snapshot
    return {"mode": "PUBLIC_DATA_ROUTE_PLANNING", "condition_mode": "CURRENT_MODEL_SNAPSHOT",
            "selection": selection_snapshot(business, request),
            "captured_at": now.isoformat(), "routes": routes, "routing": routing or {},
            "limitations": ["Not a certified navigational route", "Conditions are a current snapshot, not a full-voyage forecast",
                            "Fuel, freight prices, berth queues and depth clearance require separate evidence"]}


def _record(raw, sample, domain, received):
    source = "Open-Meteo Weather" if domain == "weather" else "Open-Meteo Marine (Copernicus/MeteoFrance model data)"
    values = raw.get("current") or {}
    units = raw.get("current_units") or {}
    at = values.get("time")
    if not at:
        return {"status": "UNAVAILABLE", "reason": "MODEL_VALID_TIME_MISSING"}
    valid_at = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if valid_at.tzinfo is None:
        valid_at = valid_at.replace(tzinfo=timezone.utc)
    # Future model valid-times are forecasts, not future observations.
    if abs((valid_at - received).total_seconds()) > 3600:
        return {"status": "STALE", "reason": "CURRENT_MODEL_TIME_OUT_OF_RANGE", "forecast_valid_at": valid_at.isoformat()}
    if raw.get("latitude") is None or raw.get("longitude") is None:
        return {"status": "UNAVAILABLE", "reason": "MODEL_GRID_LOCATION_MISSING"}
    separation = distance_km((sample["latitude"], sample["longitude"]), (raw["latitude"], raw["longitude"]))
    if separation > 100:
        return {"status": "UNAVAILABLE", "reason": "MODEL_GRID_TOO_DISTANT", "grid_distance_km": separation}
    numeric = {k: float(v) for k, v in values.items() if isinstance(v, (int, float)) and math.isfinite(v)}
    payload = {"status": "VALID", "classification": "PUBLIC_MODEL_DATA", "source": source,
               "reference": f"{source}:{raw['latitude']},{raw['longitude']}:{valid_at.isoformat()}",
               "observed_at": received.isoformat(), "forecast_valid_at": valid_at.isoformat(),
               "valid_until": (valid_at + timedelta(hours=1)).isoformat(),
               "latitude": raw["latitude"], "longitude": raw["longitude"], "grid_distance_km": separation,
               "values": numeric, "units": units, "transformation": "Provider current-condition numerical model at corridor coordinate; not a ship observation"}
    if domain == "weather":
        if units.get("wind_speed_10m") != "m/s" or numeric.get("wind_speed_10m", -1) < 0:
            return {**payload, "status": "UNAVAILABLE", "reason": "WIND_UNITS_OR_VALUE_UNAVAILABLE"}
    elif domain == "waves":
        if units.get("wave_height") != "m" or numeric.get("wave_height", -1) < 0:
            return {**payload, "status": "UNAVAILABLE", "reason": "WAVE_UNITS_OR_VALUE_UNAVAILABLE"}
    else:
        speed, direction = numeric.get("ocean_current_velocity"), numeric.get("ocean_current_direction")
        factor = {"m/s": 1.0, "km/h": 1 / 3.6, "kn": 1852 / 3600, "knots": 1852 / 3600}.get(units.get("ocean_current_velocity"))
        if factor is None or speed is None or speed < 0 or direction is None or not 0 <= direction <= 360:
            return {**payload, "status": "UNAVAILABLE", "reason": "CURRENT_UNITS_OR_VALUE_UNAVAILABLE"}
        payload["original_values"] = dict(numeric)
        payload["original_units"] = dict(units)
        speed *= factor
        payload["values"] = {**numeric, "ocean_current_velocity": speed}
        payload["units"] = {**units, "ocean_current_velocity": "m/s"}
        payload["transformation"] += f"; current velocity multiplied by {factor} from {units.get('ocean_current_velocity')} to m/s; direction is provider bearing"
        radians = math.radians(direction)
        payload.update(current_effect(speed * math.sin(radians), speed * math.cos(radians), sample["heading_deg"]))
    return payload


def collect_route_environment(context):
    """Three actual bounded parallel provider calls, within the environment agent."""
    flattened = [(edge, sample) for edge, route in context["routes"].items() for sample in route["samples"]]
    points = [(sample["latitude"], sample["longitude"]) for _, sample in flattened]

    def fetch(domain):
        received = utcnow()
        rows = {edge: [] for edge in context["routes"]}
        try:
            raw = WeatherDataAgent().fetch_route_snapshot(points, domain=domain)
            for (edge, sample), value in zip(flattened, raw):
                rows[edge].append(_record(value, sample, domain, received))
            errors = []
        except Exception as exc:
            errors = [f"ROUTE_PROVIDER_ERROR:{type(exc).__name__}"]
            rows = {edge: [{"status": "UNAVAILABLE", "reason": errors[0]} for _ in route["samples"]]
                    for edge, route in context["routes"].items()}
        usable = [r for records in rows.values() for r in records if r["status"] == "VALID"]
        count = sum(len(records) for records in rows.values())
        return domain, ProviderEnvelope(
            source="Open-Meteo Weather" if domain == "weather" else "Open-Meteo Marine (Copernicus/MeteoFrance)",
            entity_id="route-corridors", received_at=received,
            observed_at=min((datetime.fromisoformat(r["forecast_valid_at"]) for r in usable), default=None),
            valid_until=min((datetime.fromisoformat(r["valid_until"]) for r in usable), default=None),
            quality=Quality.VALID if len(usable) == count and count else Quality.PARTIAL if usable else Quality.UNAVAILABLE,
            payload={"routes": rows, "requested_points": len(points), "usable_points": len(usable),
                     "condition_mode": "CURRENT_MODEL_SNAPSHOT"}, errors=errors,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(fetch, ("weather", "waves", "ocean")))


def evaluate_route_metrics(data):
    """Only common sampled fractions enter comparisons; no asymmetric null filling."""
    context = data.route_context
    routes = context.get("routes", {})
    reports = {edge: {"metrics": {}, "evidence": {}, "raw": {}, "method": METHOD} for edge in routes}
    definitions = {"weather": ("weather_penalty", "wind_speed_10m", 25),
                   "waves": ("wave_penalty", "wave_height", 8),
                   "ocean": ("current_penalty", "along_track_ms", 2)}
    for domain, (metric, variable, scale) in definitions.items():
        source = data.sources.get(domain)
        rows = (source.payload or {}).get("routes", {}) if source else {}
        fresh = source and source.quality in {Quality.VALID, Quality.PARTIAL} and (
            source.valid_until is None or source.valid_until >= utcnow())
        sets = []
        for edge in routes:
            indices = {i for i, r in enumerate(rows.get(edge, [])) if fresh and r.get("status") == "VALID"
                       and (r.get(variable) if domain == "ocean" else r.get("values", {}).get(variable)) is not None
                       and r.get("valid_until") and datetime.fromisoformat(r["valid_until"]) >= utcnow()}
            sets.append(indices)
        common = sorted(set.intersection(*sets)) if sets else []
        for edge, report in reports.items():
            report["metrics"][metric] = None
            report["evidence"][metric] = {"source": source.source if source else "unconfigured",
                "status": "AVAILABLE" if len(common) >= 3 else "UNAVAILABLE",
                "used_sample_indices": common, "used_samples": len(common),
                "total_samples": len(routes[edge]["samples"]), "condition_mode": "CURRENT_MODEL_SNAPSHOT",
                "observed_at": source.observed_at.isoformat() if source and source.observed_at else None,
                "reason": None if len(common) >= 3 else "Fewer than three common valid route fractions"}
            if len(common) < 3:
                continue
            values = [rows[edge][i].get(variable) if domain == "ocean" else rows[edge][i]["values"][variable] for i in common]
            penalties = [min(1, max(0, -v if domain == "ocean" else v) / scale) for v in values]
            report["metrics"][metric] = statistics.fmean(penalties)
            report["raw"][variable] = statistics.fmean(values)
            report["evidence"][metric].update(references=[rows[edge][i]["reference"] for i in common],
                forecast_valid_times=[rows[edge][i]["forecast_valid_at"] for i in common],
                transformation=f"Mean of common-sample {'adverse along-track current' if domain == 'ocean' else variable} / {scale}; capped at 1")
    for report in reports.values():
        parts = [value for value in report["metrics"].values() if value is not None]
        report["metrics"].update(risk_score=statistics.fmean(parts) if parts else None,
                                 port_penalty=None)
        report["evidence"]["risk_score"] = {"source": "Evidence-derived comparative index", "method": METHOD,
            "included": [key for key, value in report["metrics"].items() if value is not None and key != "risk_score"],
            "unknown_hazards": ["piracy/conflict events", "bathymetry", "vessel-specific operating limits"],
            "classification": "PARTIAL_HAZARD_INDEX_NOT_PROBABILITY"}
        report["evidence"]["port_penalty"] = {"status": "UNAVAILABLE", "reason": "PortWatch activity is not congestion or waiting time"}
    return reports


def assess_public_route(route, shipment, data, request):
    edge = route.edge_ids[0]
    context = data.route_context["routes"][edge]
    profiles = {p.id: p for p in data.business.vessel_profiles}
    vessel_id = request.vessel_reference.mmsi if request.vessel_reference else shipment.vessel_id
    profile = profiles.get(vessel_id)
    fuel = route_fuel(route, profile)
    samples = []
    for i, sample in enumerate(context["samples"]):
        row = dict(sample)
        for domain, key in (("weather", "weather"), ("waves", "waves"), ("ocean", "current")):
            envelope = data.sources.get(domain)
            entries = (envelope.payload or {}).get("routes", {}).get(edge, []) if envelope else []
            row[key] = entries[i] if i < len(entries) else {"status": "UNAVAILABLE"}
        samples.append(row)
    vessel = selected_vessel_evidence(shipment, data, request.vessel_reference)
    ports = route_ports(shipment, data.sources.get("ports"), data.business.port_queues)
    draft = route_draft(route, shipment, data.business.route_network, profile)
    return {"classification": "PUBLIC_DATA_ROUTE_PLANNING", "condition_mode": "CURRENT_MODEL_SNAPSHOT",
        "route_id": route.candidate_id, "vessel_id": vessel_id, "vessel_state": vessel, "fuel": fuel,
        "draft": draft, "ports": ports, "samples": samples, "metric_evidence": route.metric_evidence,
        "summary": {"vessel_state": vessel["status"], "fuel": fuel["status"], "draft": draft["status"],
                    "weather": "PARTIAL" if route.weather_penalty is not None else "UNAVAILABLE",
                    "waves": "PARTIAL" if route.wave_penalty is not None else "UNAVAILABLE",
                    "currents": "PARTIAL" if route.current_penalty is not None else "UNAVAILABLE",
                    "port_activity": "PARTIAL" if any(v.get("observed_port_activity") for v in ports.values()) else "UNAVAILABLE"},
        "checks": {key: {"execution": "CHECKED"} for key in ("vessel_state", "fuel", "weather", "waves", "currents", "draft", "port_activity")},
        "limitations": data.route_context["limitations"]}
