"""Evidence-driven intelligence domains. Missing models remain unavailable."""

import statistics
from collections import defaultdict
from core.schemas.contracts import (
    IntelligenceResult,
    Quality,
    RiskAssessment,
    DemandForecast,
    utcnow,
    ReplenishmentRestrictions,
)


def result(
    name,
    status,
    output,
    explanation,
    refs=(),
    model="deterministic-observation",
    quality=None,
    provenance=(),
):
    return IntelligenceResult(
        agent=f"intelligence.{name}",
        status=status,
        output=output,
        explanation=explanation,
        input_references=list(refs),
        model_source=model,
        data_quality=quality
        or (Quality.VALID if status == "SUCCESS" else Quality.UNAVAILABLE),
        provenance=list(provenance),
    )


def risk(data, upstream=None):
    if data.route_context:
        from services.route_evidence import evaluate_route_metrics, METHOD
        reports = evaluate_route_metrics(data)
        usable = any(r["metrics"]["risk_score"] is not None for r in reports.values())
        return result("risk", "PARTIAL" if usable else "UNAVAILABLE",
                      {"route_metrics": reports}, METHOD, list(reports), quality=Quality.PARTIAL if usable else Quality.UNAVAILABLE)
    assessments = []
    for vessel in data.vessels:
        weather, ocean = vessel.get("weather", {}), vessel.get("ocean", {})
        wind = weather.get("wind_speed_ms")
        components = {
            "wind": min(1, wind / 25) if wind is not None else None,
            "waves": None,
            "current": min(1, ocean["speed_ms"] / 2)
            if ocean.get("speed_ms") is not None
            else None,
        }
        missing = [k for k, v in components.items() if v is None]
        if vessel.get("quality") != "VALID":
            missing.append("fresh_ais")
        assessments.append(
            RiskAssessment(
                entity_id=str(vessel["mmsi"]),
                score=None if missing else max(components.values()),
                components=components,
                missing_inputs=missing,
                method="Uncalibrated operational hazard indices; not failure probabilities",
            ).model_dump(mode="json")
        )
    return result(
        "risk",
        "PARTIAL" if assessments else "UNAVAILABLE",
        {"assessments": assessments},
        "Unknown hazards are not assigned zero risk. Commercial risk limits require a complete, sourced candidate assessment.",
        [str(v["mmsi"]) for v in data.vessels],
        quality=Quality.PARTIAL if assessments else Quality.UNAVAILABLE,
    )


def demand(data, upstream=None):
    if data.route_context and not data.business.demand:
        return result("demand", "NOT_APPLICABLE", {"demand_records_checked": 0},
                      "Checked business scope: fixed port-to-port voyage; cargo demand forecasting is not needed to compare sea routes.")
    groups = defaultdict(list)
    for item in data.business.demand:
        if item.valid_until >= utcnow() and item.data_quality == Quality.VALID:
            groups[(item.sku_id, item.location_id)].append(item)
    forecasts = []
    for (sku, location), records in groups.items():
        records.sort(key=lambda x: x.period_start)
        if len(records) < 3:
            continue
        widths = [
            (r.period_end - r.period_start).total_seconds() / 86400 for r in records
        ]
        if min(widths) <= 0 or max(widths) - min(widths) > 1e-6:
            continue
        if any(a.period_end != b.period_start for a, b in zip(records, records[1:])):
            continue
        window = records[-12:]
        values = [r.quantity for r in window]
        forecasts.append(
            DemandForecast(
                sku_id=sku,
                location_id=location,
                prediction=max(
                    0,
                    statistics.fmean(values)
                    + (values[-1] - values[0]) / (len(values) - 1),
                ),
                horizon_days=widths[-1],
                method="Recent mean plus linear trend baseline; not a customer order",
                input_references=[r.id for r in window],
            ).model_dump()
        )
    return result(
        "demand",
        "SUCCESS" if forecasts else "UNAVAILABLE",
        {
            "forecasts": forecasts,
            "execution_outcomes": [f.model_dump(mode="json") for f in data.feedback],
            "feedback_use": "Acknowledged orders are evidence only; receipt and inventory changes require the authoritative source.",
        },
        "Forecasts require at least three contiguous equal-duration observations; shipment quantity comes from a requirement.",
        [r.id for r in data.business.demand],
        model="recent-mean-linear-trend-v1",
        provenance=[p for r in data.business.demand for p in r.provenance],
    )


def forecast(data, upstream=None):
    if data.route_context:
        estimates = [{"edge_id": edge, "duration_hours": r["duration_hours"],
                      "distance_km": r["distance_km"], "speed_source": "OPERATOR_PLANNING_ASSUMPTION"}
                     for edge, r in data.route_context["routes"].items()]
        return result("forecast", "SUCCESS" if estimates else "UNAVAILABLE", {"transit_estimates": estimates},
                      "Estimated transit time = ArcNautical distance / planning speed; not a trained prediction or observed ETA.",
                      [e["edge_id"] for e in estimates], model="distance/speed planning estimate")
    from services.forecasting.stgnn import validated_forecast

    return validated_forecast(data)


def disruption(data, upstream=None):
    records = [
        r
        for r in data.business.disruptions
        if r.observed_at <= utcnow() <= r.valid_until
        and r.data_quality == Quality.VALID
    ]
    if data.route_context:
        hazards = {edge: route["hazards"] for edge, route in data.route_context["routes"].items()}
        return result("disruption", "SUCCESS" if records else "PARTIAL", {
            "events": [r.model_dump(mode="json") for r in records], "geographic_chokepoints": hazards,
            "live_event_feed_available": bool(records)},
            "Cross-checked supplied events and actual route chokepoints. Crossing Suez or the Red Sea is not proof of a current closure; no news events are invented.", list(hazards))
    return result(
        "disruption",
        "SUCCESS" if records else "UNAVAILABLE",
        {
            "events": [r.model_dump(mode="json") for r in records],
            "replenishment_restrictions": supply_restrictions(records).model_dump(
                mode="json"
            ),
        },
        "Only supplied, attributed and unexpired disruptions are actionable.",
        [r.id for r in records],
        provenance=[p for r in records for p in r.provenance],
    )


def supply_restrictions(records):
    events = [r for r in records if r.supply_effect == "UNAVAILABLE"]
    return ReplenishmentRestrictions(
        supplier_ids=sorted({x for r in events for x in r.supplier_ids}),
        location_ids=sorted({x for r in events for x in r.location_ids}),
        quote_ids=sorted({x for r in events for x in r.quote_ids}),
        event_ids=sorted(r.id for r in events),
    )


def anomaly(data, upstream=None):
    if data.route_context:
        checked = []
        for edge, route in data.route_context["routes"].items():
            checked.append({"edge_id": edge, "distance_km": route["distance_km"],
                            "duration_hours": route["duration_hours"],
                            "geometry_sample_count": len(route["samples"]),
                            "positive_distance_and_duration": route["distance_km"] > 0 and route["duration_hours"] > 0})
        return result("anomaly", "SUCCESS" if checked else "UNAVAILABLE", {"route_checks": checked},
                      "Checked positive route metrics, sampled geometry and source availability; no vessel speed anomaly claim without AIS history.", [r["edge_id"] for r in checked])
    anomalies = []
    evaluated = 0
    for vessel in data.vessels:
        values = [
            h["speed_knots"]
            for h in vessel.get("history", [])
            if h.get("speed_knots") is not None
        ]
        speed = vessel["position"].get("speed_knots")
        if len(values) < 6 or speed is None:
            continue
        baseline = values[:-1]
        std = statistics.pstdev(baseline)
        evaluated += 1
        if std and abs(speed - statistics.fmean(baseline)) / std >= 3:
            anomalies.append(
                {
                    "entity_id": vessel["mmsi"],
                    "speed_knots": speed,
                    "z_score": abs(speed - statistics.fmean(baseline)) / std,
                }
            )
    return result(
        "anomaly",
        "SUCCESS" if evaluated else "UNAVAILABLE",
        {"anomalies": anomalies, "evaluated_vessels": evaluated},
        "Speed z-score using prior observed history; no anomaly does not prove safety.",
    )


def port(data, upstream=None):
    source = data.sources.get("ports")
    records = (source.payload or {}).get("ports", []) if source else []
    if data.route_context:
        endpoints = {value for shipment in data.business.shipments for value in (shipment.origin_id, shipment.destination_id)}
        records = [r for r in records if str(r.get("locode", "")).upper() in endpoints]
    observations = []
    for p in records:
        observations.append(
            {
                "id": p.get("portid"),
                "locode": p.get("locode"),
                "name": p.get("portname"),
                "country": p.get("country"),
                "latitude": p.get("latitude"),
                "longitude": p.get("longitude"),
                "port_calls": p.get("portcalls"),
                "imports": p.get("import"),
                "exports": p.get("export"),
                "signal_type": "OBSERVED_PORT_ACTIVITY",
                "physical_waiting_hours": None,
                "location_source": p.get("location_source"),
                "location_status": p.get(
                    "location_status", "PORT_LOCATION_UNAVAILABLE"
                ),
                "attribution": "UN Global Platform; IMF PortWatch (portwatch.imf.org)",
            }
        )
    return result(
        "port",
        "SUCCESS" if observations else "UNAVAILABLE",
        {"ports": observations},
        "IMF PortWatch measures daily activity. Physical congestion and berth waiting time are unavailable.",
        model="IMF PortWatch",
        quality=source.quality if source else Quality.UNAVAILABLE,
    )


def scenario(data, upstream):
    if data.route_context:
        reports = upstream["risk"].output.get("route_metrics", {})
        routes = data.route_context["routes"]
        profiles = {}
        for name, include in (("Shortest distance", ["distance"]), ("Shortest time", ["time"]),
                              ("Available environment", ["weather", "current", "wave"])):
            fields = {"distance": "distance_km", "time": "duration_hours", "weather": "weather_penalty",
                      "current": "current_penalty", "wave": "wave_penalty"}
            values = {edge: {key: route.get(fields[key]) if key in {"distance", "time"}
                             else reports.get(edge, {}).get("metrics", {}).get(fields[key]) for key in include}
                      for edge, route in routes.items()}
            usable = [key for key in include if values and all(v[key] is not None for v in values.values())]
            scales = {key: max(v[key] for v in values.values()) or 1 for key in usable}
            scores = {edge: sum(v[key] / scales[key] for key in usable) for edge, v in values.items()} if usable else {}
            profiles[name] = {"used_metrics": usable, "scores": scores,
                              "leading_edge": min(scores, key=scores.get) if scores else None}
        return result("scenario", "PARTIAL", {"comparisons": profiles, "probabilities": None},
                      "Compared distance-first, time-first and environment-first preferences using the same evidence. These are deterministic what-if comparisons, not invented event probabilities.", list(routes), quality=Quality.PARTIAL)
    available = [
        key for key, value in upstream.items() if value.status in {"SUCCESS", "PARTIAL"}
    ]
    return result(
        "scenario",
        "PARTIAL" if available else "UNAVAILABLE",
        {"evidence_domains": available, "probabilities": None},
        "Scenario distributions must be explicitly supplied with the optimization request. No event probabilities are assumed.",
        quality=Quality.PARTIAL if available else Quality.UNAVAILABLE,
    )


DOMAINS = {
    "risk": risk,
    "demand": demand,
    "forecast": forecast,
    "disruption": disruption,
    "anomaly": anomaly,
    "port": port,
    "scenario": scenario,
}
