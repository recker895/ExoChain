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
    from services.forecasting.stgnn import validated_forecast

    return validated_forecast(data)


def disruption(data, upstream=None):
    records = [
        r
        for r in data.business.disruptions
        if r.observed_at <= utcnow() <= r.valid_until
        and r.data_quality == Quality.VALID
    ]
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
    observations = []
    for p in records:
        observations.append(
            {
                "id": p.get("portid"),
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
