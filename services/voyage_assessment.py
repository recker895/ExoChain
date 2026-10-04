"""Evidence-bound route assessment; never grants navigational clearance."""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from agents.data.weather_agent import WeatherDataAgent
from data_sources.copernicus.ocean_currents import CopernicusOceanCurrentSource
from core.schemas.contracts import Quality, utcnow
from services.feature_engineering.environment import current_effect, distance_km


def _unavailable(reason: str) -> dict:
    return {"status": "UNAVAILABLE", "reason": reason}


def _bearing(a, b) -> float:
    lat1, lat2 = math.radians(a.latitude), math.radians(b.latitude)
    delta = math.radians(b.longitude - a.longitude)
    y = math.sin(delta) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta)
    return math.degrees(math.atan2(y, x)) % 360


def route_samples(route, shipment, count=9):
    """Interpolate time at evenly spaced cumulative-distance positions."""
    points = route.geometry
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + distance_km(
            (a.latitude, a.longitude), (b.latitude, b.longitude)
        ))
    total = cumulative[-1]
    if total <= 0:
        raise ValueError("route geometry has no length")
    result = []
    for i in range(count):
        target = total * i / (count - 1)
        right = next((j for j in range(1, len(cumulative)) if cumulative[j] >= target), len(points) - 1)
        left = right - 1
        fraction = (target - cumulative[left]) / (cumulative[right] - cumulative[left]) if cumulative[right] > cumulative[left] else 0
        a, b = points[left], points[right]
        lat = a.latitude + (b.latitude - a.latitude) * fraction
        lon = a.longitude + (b.longitude - a.longitude) * fraction
        result.append({
            "latitude": lat, "longitude": lon,
            "expected_at": (shipment.departure_at + timedelta(hours=route.duration_hours * i / (count - 1))).isoformat(),
            "heading_deg": _bearing(a, b),
            "distance_fraction": i / (count - 1),
        })
    return result


def _forecast_record(response, sample, *, marine, received_at):
    hourly = response.get("hourly") or {}
    expected = datetime.fromisoformat(sample["expected_at"])
    times = hourly.get("time") or []
    parsed = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc) for t in times]
    if not parsed:
        return _unavailable("FORECAST_UNAVAILABLE")
    index = min(range(len(parsed)), key=lambda i: abs((parsed[i] - expected).total_seconds()))
    valid_at = parsed[index]
    if abs((valid_at - expected).total_seconds()) > 1800:
        return _unavailable("OUTSIDE_FORECAST_HORIZON")
    variables = (
        ("wave_height", "wave_direction", "wave_period") if marine else
        ("wind_speed_10m", "wind_direction_10m", "temperature_2m", "precipitation", "weather_code")
    )
    values = {key: hourly.get(key, [])[index] if len(hourly.get(key, [])) > index else None for key in variables}
    present = sum(value is not None for value in values.values())
    if not present:
        return _unavailable("FORECAST_VALUES_UNAVAILABLE")
    source = "Open-Meteo Marine" if marine else "Open-Meteo Weather"
    return {
        "status": "VALID" if present == len(values) else "PARTIAL",
        "classification": "MODEL_DERIVED",
        "source": source,
        "reference": f"{source}:{response.get('latitude')},{response.get('longitude')}:{valid_at.isoformat()}",
        "observed_at": received_at.isoformat(),
        "valid_until": (valid_at + timedelta(minutes=30)).isoformat(),
        "forecast_valid_at": valid_at.isoformat(),
        "transformation": "Nearest hourly forecast at interpolated route position/time; not an observation or navigational forecast",
        "values": values,
        "units": {key: (response.get("hourly_units") or {}).get(key) for key in variables},
    }


def route_forecasts(samples):
    """Reuse the existing Open-Meteo weather agent for route-timed weather/waves."""
    coords = [(s["latitude"], s["longitude"]) for s in samples]
    def fetch(marine):
        try:
            received = utcnow()
            responses = WeatherDataAgent().fetch_route_forecast(coords, marine=marine)
            return [_forecast_record(value, sample, marine=marine, received_at=received)
                    for value, sample in zip(responses, samples)]
        except Exception as exc:
            return [_unavailable(f"FORECAST_PROVIDER_ERROR:{type(exc).__name__}") for _ in samples]
    with ThreadPoolExecutor(max_workers=2) as pool:
        weather = pool.submit(fetch, False)
        waves = pool.submit(fetch, True)
        return weather.result(), waves.result()


def route_currents(samples, envelope):
    """Use only spatially and temporally matching captured Copernicus evidence."""
    if any(sample["expected_at"] is None for sample in samples):
        return [{"status": "DATA_REQUIRED", "reason": "SHIPMENT_TIMING_REQUIRED"} for _ in samples]
    if not envelope or envelope.quality != Quality.VALID or not envelope.observed_at or (envelope.valid_until and envelope.valid_until < utcnow()):
        return [_unavailable("COPERNICUS_CURRENT_UNAVAILABLE_OR_STALE") for _ in samples]
    records = list(((envelope.payload or {}).get("currents") or {}).values())
    output = []
    for sample in samples:
        if sample["expected_at"] is None:
            output.append({"status": "DATA_REQUIRED", "reason": "SHIPMENT_TIMING_REQUIRED"})
            continue
        expected = datetime.fromisoformat(sample["expected_at"])
        matches = [r for r in records if r.get("valid") and r.get("u_ms") is not None
                   and r.get("v_ms") is not None and r.get("latitude") is not None
                   and r.get("longitude") is not None
                   and abs((expected - envelope.observed_at).total_seconds()) <= 12 * 3600
                   and (envelope.valid_until is None or expected <= envelope.valid_until)
                   and distance_km((sample["latitude"], sample["longitude"]),
                                   (r["latitude"], r["longitude"])) <= 15]
        if not matches:
            output.append(_unavailable("NO_ROUTE_TIME_MATCHED_COPERNICUS_CURRENT"))
            continue
        record = min(matches, key=lambda r: distance_km(
            (sample["latitude"], sample["longitude"]), (r["latitude"], r["longitude"])))
        effect = current_effect(record["u_ms"], record["v_ms"], sample["heading_deg"])
        along = effect["along_track_ms"]
        output.append({
            "status": "VALID", "classification": "MODEL_DERIVED",
            "source": envelope.source, "reference": envelope.entity_id,
            "observed_at": envelope.observed_at.isoformat(),
            "valid_until": envelope.valid_until.isoformat() if envelope.valid_until else None,
            "transformation": "Nearest observed current within 15 km and 12 h, projected onto route heading; no fuel/time saving inferred",
            "u_ms": record["u_ms"], "v_ms": record["v_ms"],
            "speed_ms": effect["speed_ms"], "direction_deg": effect["direction_deg"],
            "along_track_ms": along,
            "effect": "ASSISTS" if along > .05 else "OPPOSES" if along < -.05 else "NEGLIGIBLE",
        })
    return output


def fetch_copernicus_route_currents(samples):
    """Use the existing Copernicus source at bounded route points when configured.

    Daily tiles do not support precise hour-of-passage claims; evidence is
    PARTIAL even when a valid current vector is returned.
    """
    if not (os.getenv("COPERNICUSMARINE_SERVICE_USERNAME") and os.getenv("COPERNICUSMARINE_SERVICE_PASSWORD")):
        return [_unavailable("COPERNICUS_CREDENTIALS_REQUIRED") for _ in samples]
    source = CopernicusOceanCurrentSource()
    output = [_unavailable("COPERNICUS_ROUTE_SAMPLE_NOT_FETCHED") for _ in samples]
    eligible = [i for i, sample in enumerate(samples)
                if utcnow() <= datetime.fromisoformat(sample["expected_at"]) <= utcnow() + timedelta(days=7)]
    for index in eligible[:3]:
        sample = samples[index]
        lat, lon = sample["latitude"], sample["longitude"]
        date = datetime.fromisoformat(sample["expected_at"]).date().isoformat()
        try:
            dataset = source.fetch_tile(
                minimum_longitude=max(-180, lon - .1), maximum_longitude=min(180, lon + .1),
                minimum_latitude=max(-90, lat - .1), maximum_latitude=min(90, lat + .1),
                date=date,
            )
            try:
                valid_at = datetime.fromisoformat(str(dataset.time.values[0])[:19]).replace(tzinfo=timezone.utc)
                if valid_at.date().isoformat() != date:
                    raise ValueError("COPERNICUS_TIME_MISMATCH")
                current = source.nearest_current(dataset, lat, lon)
            finally:
                dataset.close()
            if not current.get("valid") or current.get("u_ms") is None or current.get("v_ms") is None:
                output[index] = _unavailable("COPERNICUS_CURRENT_LAND_MASKED_OR_MISSING")
                continue
            effect = current_effect(current["u_ms"], current["v_ms"], sample["heading_deg"])
            along = effect["along_track_ms"]
            output[index] = {
                "status": "PARTIAL", "classification": "MODEL_DERIVED",
                "source": "Copernicus Marine", "reference": source.DATASET_ID,
                "observed_at": utcnow().isoformat(),
                "valid_until": (valid_at + timedelta(days=1)).isoformat(),
                "forecast_valid_at": valid_at.isoformat(),
                "transformation": "Nearest daily surface-current grid at route coordinate; passage hour may differ; projection onto route heading; no exact fuel/time saving inferred",
                "u_ms": current["u_ms"], "v_ms": current["v_ms"],
                "speed_ms": effect["speed_ms"], "direction_deg": effect["direction_deg"],
                "along_track_ms": along,
                "effect": "ASSISTS" if along > .05 else "OPPOSES" if along < -.05 else "NEGLIGIBLE",
            }
        except Exception as exc:
            output[index] = _unavailable(f"COPERNICUS_ROUTE_ERROR:{type(exc).__name__}")
    return output


def route_fuel(route, profile):
    if not profile or not profile.fuel_consumption_curve or profile.data_quality != Quality.VALID:
        return _unavailable("FUEL_DATA_UNAVAILABLE")
    if profile.observed_at > utcnow() or profile.valid_until < utcnow():
        return {"status": "STALE", "reason": "FUEL_CURVE_STALE"}
    curve = sorted(profile.fuel_consumption_curve, key=lambda p: p.speed_knots)
    segments = []
    for edge in route.segments:
        speed = edge.distance_km / 1.852 / edge.duration_hours
        if speed < curve[0].speed_knots or speed > curve[-1].speed_knots:
            return _unavailable("FUEL_CURVE_SPEED_OUT_OF_RANGE")
        upper = next((i for i, p in enumerate(curve) if p.speed_knots >= speed), None)
        if upper is None:
            return _unavailable("FUEL_CURVE_SPEED_OUT_OF_RANGE")
        if upper == 0 or curve[upper].speed_knots == speed:
            rate = curve[upper].fuel_litres_per_hour
        else:
            low, high = curve[upper - 1], curve[upper]
            ratio = (speed - low.speed_knots) / (high.speed_knots - low.speed_knots)
            rate = low.fuel_litres_per_hour + ratio * (high.fuel_litres_per_hour - low.fuel_litres_per_hour)
        segments.append({"edge_id": edge.id, "speed_knots": speed,
                         "fuel_litres": rate * edge.duration_hours})
    return {
        "status": "VALID", "classification": "MODEL_DERIVED",
        "curve_classification": profile.fuel_curve_classification,
        "source": profile.source, "reference": profile.id,
        "observed_at": profile.observed_at.isoformat(),
        "valid_until": profile.valid_until.isoformat(),
        "transformation": "Linear speed-curve interpolation per route segment; constant speed; no weather or current fuel correction",
        "segments": segments, "total_fuel_litres": sum(s["fuel_litres"] for s in segments),
    }


def route_draft(route, shipment, network, profile):
    profile_current = bool(profile and profile.data_quality == Quality.VALID
                           and profile.observed_at <= utcnow() <= profile.valid_until)
    draft = profile.draft_m if profile_current and profile.draft_m is not None else shipment.draft_m
    if draft is None:
        return {"status": "DATA_REQUIRED", "reason": "VESSEL_DRAFT_UNAVAILABLE"}
    findings = []
    for edge in route.segments:
        if edge.available_depth_m is not None and edge.available_depth_m < draft:
            status = "FAIL"
        elif (network.planning_classification != "NAVIGATION_AUTHORITY"
              or edge.available_depth_m is None or not edge.depth_evidence_reference
              or not edge.restrictions_verified):
            status = "UNKNOWN"
        else:
            status = "PASS"
        findings.append({"edge_id": edge.id, "status": status,
                         "available_depth_m": edge.available_depth_m,
                         "depth_evidence_reference": edge.depth_evidence_reference})
    overall = "FAIL" if any(x["status"] == "FAIL" for x in findings) else "PASS" if all(x["status"] == "PASS" for x in findings) else "DATA_REQUIRED"
    return {"status": overall, "vessel_draft_m": draft, "segments": findings,
            "reason": "Geographic routing alone does not establish depth, tide or navigational clearance" if overall == "DATA_REQUIRED" else None}


def route_ports(shipment, envelope, queue_observations=()):
    fresh_portwatch = bool(envelope and envelope.quality in {Quality.VALID, Quality.PARTIAL}
                           and (not envelope.valid_until or envelope.valid_until >= utcnow()))
    records = (envelope.payload or {}).get("ports", []) if fresh_portwatch else []
    output = {}
    for label, locode in (("origin", shipment.origin_id), ("destination", shipment.destination_id)):
        record = next((r for r in records if str(r.get("locode", "")).upper() == locode.upper()), None)
        if not record:
            reason = "PORTWATCH_PORT_NOT_MATCHED" if fresh_portwatch else "PORTWATCH_STALE" if envelope and envelope.quality == Quality.STALE else "PORTWATCH_UNAVAILABLE"
            output[label] = {"status": "STALE" if reason.endswith("STALE") else "UNAVAILABLE", "reason": reason}
        else:
            output[label] = {
                "status": "PARTIAL", "classification": "AUTHORITATIVE",
                "source": envelope.source, "reference": str(record.get("portid")),
                "observed_at": envelope.observed_at.isoformat() if envelope.observed_at else None,
                "valid_until": envelope.valid_until.isoformat() if envelope.valid_until else None,
                "transformation": "UN/LOCODE join; raw PortWatch activity only, not physical congestion",
                "observed_port_activity": {key: record.get(key) for key in ("portcalls", "import", "export")},
            }
        queue = next((q for q in queue_observations if q.port_locode.upper() == locode.upper()), None)
        if queue and queue.data_quality == Quality.VALID and queue.observed_at <= utcnow() <= queue.valid_until:
            output[label]["berth_queue"] = {
                "status": "VALID", "classification": "AUTHORITATIVE",
                "source": queue.source, "reference": queue.id,
                "observed_at": queue.observed_at.isoformat(),
                "valid_until": queue.valid_until.isoformat(),
                "queued_vessels": queue.queued_vessels,
                "provenance": [p.model_dump(mode="json") for p in queue.provenance],
            }
            output[label]["waiting_time_hours"] = (
                {"status": "VALID", "classification": "AUTHORITATIVE",
                 "value": queue.observed_waiting_hours, "source": queue.source,
                 "reference": queue.id}
                if queue.observed_waiting_hours is not None
                else _unavailable("WAITING_TIME_MODEL_OR_OBSERVATION_REQUIRED")
            )
            if output[label]["status"] == "UNAVAILABLE":
                output[label]["status"] = "PARTIAL"
        else:
            output[label]["berth_queue"] = _unavailable("EXACT_BERTH_QUEUE_PROVIDER_REQUIRED")
            output[label]["waiting_time_hours"] = _unavailable("WAITING_TIME_MODEL_OR_OBSERVATION_REQUIRED")
    return output


def _coverage(records):
    statuses = [r["status"] for r in records]
    return "VALID" if statuses and all(s == "VALID" for s in statuses) else "PARTIAL" if any(s in {"VALID", "PARTIAL"} for s in statuses) else "DATA_REQUIRED" if "DATA_REQUIRED" in statuses else "UNAVAILABLE"


def _wave_safety(waves, profile):
    threshold = (profile.max_significant_wave_height_m if profile
                 and profile.data_quality == Quality.VALID
                 and profile.observed_at <= utcnow() <= profile.valid_until else None)
    if threshold is None:
        return {"status": "DATA_REQUIRED", "reason": "VESSEL_SPECIFIC_WAVE_THRESHOLD_UNAVAILABLE"}
    heights = [r.get("values", {}).get("wave_height") for r in waves]
    if any(height is not None and height > threshold for height in heights):
        return {"status": "FAIL", "threshold_m": threshold}
    if any(height is None for height in heights):
        return {"status": "DATA_REQUIRED", "reason": "WAVE_COVERAGE_INCOMPLETE", "threshold_m": threshold}
    return {"status": "PASS", "threshold_m": threshold, "classification": "MODEL_DERIVED"}


def selected_vessel_evidence(shipment, data, reference):
    """Describe the captured AIS observation, never turn it into a sailing order."""
    if reference is None:
        return _unavailable("VESSEL_REFERENCE_NOT_SUPPLIED")
    if shipment.id != reference.shipment_id or (
        shipment.vessel_id is not None and shipment.vessel_id != reference.mmsi
    ):
        return {"status": "DATA_REQUIRED", "reason": "VESSEL_SHIPMENT_IDENTITY_MISMATCH"}
    envelope = data.sources.get("selected_vessel")
    if envelope is None or envelope.entity_id != reference.mmsi:
        return _unavailable("SELECTED_VESSEL_NOT_CAPTURED")
    if not envelope.payload or not envelope.payload.get("vessel"):
        return {"status": envelope.quality.value, "reason": ",".join(envelope.errors) or "SELECTED_VESSEL_NOT_OBSERVED"}
    vessel = envelope.payload["vessel"]
    position = vessel.get("position") or {}
    return {
        "status": envelope.quality.value,
        "classification": "OBSERVED_AIS",
        "source": envelope.source,
        "reference": reference.mmsi,
        "observed_at": envelope.observed_at.isoformat() if envelope.observed_at else None,
        "valid_until": envelope.valid_until.isoformat() if envelope.valid_until else None,
        "identity_checks": envelope.payload.get("identity_checks", {}),
        "requested_imo": reference.imo,
        "vessel_name": vessel.get("vessel_name"),
        "position": {key: position.get(key) for key in ("latitude", "longitude", "speed_knots")},
        "reported_draft_m": (vessel.get("characteristics") or {}).get("draft_m"),
        "provenance": vessel.get("provenance", []),
        "transformation": "Exact-MMSI lookup of immutable AIS/Redis snapshot; AIS draft is not navigational clearance",
    }


def assess_voyage(route, shipment, network, data, *, fetch_forecasts=True,
                  vessel_reference=None):
    checked_at = utcnow().isoformat()
    samples = route_samples(route, shipment)
    timing_verified = shipment.source != "TEST_ONLY_ROUTE_DERIVED_SHIPMENT"
    if not timing_verified:
        for sample in samples:
            sample["expected_at"] = None
        fetch_forecasts = False
    if fetch_forecasts:
        weather, waves = route_forecasts(samples)
    else:
        reason = "SHIPMENT_TIMING_REQUIRED" if not timing_verified else "FORECAST_NOT_FETCHED"
        status = "DATA_REQUIRED" if not timing_verified else "UNAVAILABLE"
        weather = [{"status": status, "reason": reason} for _ in samples]
        waves = [{"status": status, "reason": reason} for _ in samples]
    currents = route_currents(samples, data.sources.get("ocean"))
    if fetch_forecasts:
        scoped = fetch_copernicus_route_currents(samples)
        currents = [new if new["status"] in {"VALID", "PARTIAL"} else old
                    for old, new in zip(currents, scoped)]
    profiles = {p.id: p for p in data.business.vessel_profiles}
    vessel_id = vessel_reference.mmsi if vessel_reference else shipment.vessel_id
    identity_matches = not vessel_reference or (
        shipment.id == vessel_reference.shipment_id and
        shipment.vessel_id in (None, vessel_reference.mmsi)
    )
    profile = profiles.get(vessel_id) if vessel_id and identity_matches else None
    fuel = route_fuel(route, profile) if identity_matches else _unavailable("VESSEL_SHIPMENT_IDENTITY_MISMATCH")
    draft = route_draft(route, shipment, network, profile) if identity_matches else {"status": "DATA_REQUIRED", "reason": "VESSEL_SHIPMENT_IDENTITY_MISMATCH"}
    ports = route_ports(shipment, data.sources.get("ports"), data.business.port_queues)
    wave_safety = _wave_safety(waves, profile)
    vessel_state = selected_vessel_evidence(shipment, data, vessel_reference)
    checks = {
        "vessel_state": {"execution": "CHECKED", "reason": vessel_state.get("reason")},
        "fuel": {"execution": "CHECKED", "reason": fuel.get("reason")},
        "weather": {"execution": "CHECKED", "lookup": "REQUESTED" if fetch_forecasts else "SKIPPED",
                    "reason": None if fetch_forecasts else reason},
        "currents": {"execution": "CHECKED", "lookup": "REQUESTED" if timing_verified else "SKIPPED",
                     "reason": None if timing_verified else "SHIPMENT_TIMING_REQUIRED"},
        "waves": {"execution": "CHECKED", "lookup": "REQUESTED" if fetch_forecasts else "SKIPPED",
                  "reason": None if fetch_forecasts else reason},
        "draft": {"execution": "CHECKED", "reason": draft.get("reason")},
        "port_activity": {"execution": "CHECKED", "reason": None},
    }
    return {
        "classification": "GEOGRAPHIC_PLANNING_ONLY" if network.planning_classification == "GEOGRAPHIC_PLANNING_ONLY" else "EVIDENCE_ASSESSMENT",
        "timing_status": "VALID" if timing_verified else "DATA_REQUIRED",
        "route_id": route.candidate_id,
        "vessel_id": vessel_id,
        "vessel_state": vessel_state,
        "fuel": fuel,
        "draft": draft,
        "ports": ports,
        "samples": [dict(sample, weather=w, waves=wv, current=c)
                    for sample, w, wv, c in zip(samples, weather, waves, currents)],
        "wave_safety": wave_safety,
        "checks_at": checked_at,
        "checks": checks,
        "summary": {
            "vessel_state": vessel_state["status"],
            "fuel": fuel["status"], "weather": _coverage(weather),
            "currents": _coverage(currents), "waves": _coverage(waves),
            "draft": draft["status"],
            "port_activity": _coverage(list(ports.values())),
            "berth_queue": _coverage([entry["berth_queue"] for entry in ports.values()]),
        },
        "depth_provider": "Authoritative bathymetry/nautical restrictions not configured",
        "berth_queue_provider": "Exact berth/queue provider not configured",
    }
