"""Candidate generation only. These agents cannot approve or execute actions."""

from types import SimpleNamespace

from core.schemas.contracts import (
    DecisionCandidate,
    Quality,
    utcnow,
    ReplenishmentRestrictions,
)
from optimization.navigation import generate_routes
from services.voyage_assessment import assess_voyage


def usable(entity):
    return (
        entity.data_quality == Quality.VALID
        and entity.observed_at <= utcnow() <= entity.valid_until
    )


def candidate(kind, entity, action, parameters):
    return DecisionCandidate(
        candidate_id=f"{kind}:{entity.id}:{entity.version}",
        decision_type=kind,
        entity_id=entity.id,
        action=action,
        parameters=parameters,
        constraints={"valid_until": entity.valid_until.isoformat()},
        expected_effect={"status": "REQUIRES_OPTIMIZATION"},
        provenance=entity.provenance,
    )


def route(data, intel, request):
    routes = []
    missing = []
    metadata = {}
    shipments = [
        s for s in data.business.shipments if s.id in request.shipment_ids and usable(s)
    ]
    if request.operation == "TRANSPORT" and not shipments:
        missing.append("SHIPMENT_REQUIREMENT_UNAVAILABLE")
    for shipment in shipments:
        network = data.business.route_network
        generated, errors, details = generate_routes(
            network, shipment, request.weights, demo_mode=request.demo_mode
        )
        if network is not None:
            for candidate in generated:
                candidate.voyage_assessment = assess_voyage(
                    candidate, shipment, network, data,
                    vessel_reference=request.vessel_reference,
                    fetch_forecasts=(network.source.startswith("ArcNautical")
                                     and shipment.source != "TEST_ONLY_ROUTE_DERIVED_SHIPMENT"),
                )
            if (not generated and network.planning_classification == "GEOGRAPHIC_PLANNING_ONLY"):
                options = sorted(
                    (edge for edge in network.edges
                     if edge.origin_id == shipment.origin_id
                     and edge.destination_id == shipment.destination_id),
                    key=lambda edge: (edge.duration_hours, edge.distance_km, edge.id),
                )
                details["planning_options"] = [
                    {"edge_id": edge.id, "label": edge.planning_label or "Geographic route",
                     "distance_km": edge.distance_km, "duration_hours": edge.duration_hours,
                     "source": edge.source, "hazard_zones_crossed": edge.hazard_zones_crossed}
                    for edge in options
                ]
                if options:
                    edge = options[0]
                    preview = SimpleNamespace(
                        candidate_id=f"planning-preview:{edge.id}",
                        geometry=edge.geometry, segments=[edge],
                        duration_hours=edge.duration_hours,
                    )
                    details["voyage_assessment"] = assess_voyage(
                        preview, shipment, network, data,
                        vessel_reference=request.vessel_reference,
                        fetch_forecasts=shipment.source != "TEST_ONLY_ROUTE_DERIVED_SHIPMENT",
                    )
        routes.extend(generated)
        missing.extend(errors)
        metadata[shipment.id] = details
    return {
        "status": "SUCCESS" if routes else "UNAVAILABLE",
        "routes": [r.model_dump(mode="json") for r in routes],
        "missing": missing,
        "metadata": metadata,
        "candidates": [],
    }


def modal(data, intel, request):
    carriers = {c.id: c for c in data.business.carriers if usable(c)}
    options = [
        c
        for c in data.business.carrier_options
        if c.shipment_id in request.shipment_ids
        and usable(c)
        and c.carrier_id in carriers
        and c.mode in carriers[c.carrier_id].modes
    ]
    return {
        "status": "SUCCESS" if options else "UNAVAILABLE",
        "candidates": [
            candidate(
                "modal", c, "ALLOCATE_TRANSPORT", c.model_dump(mode="json")
            ).model_dump(mode="json")
            for c in options
        ],
        "missing": [] if options else ["FREIGHT_RATE_UNAVAILABLE"],
    }


def inventory(data, intel, request):
    records = [
        r
        for r in data.business.inventory
        if r.id in request.inventory_ids and usable(r)
    ]
    return {
        "status": "SUCCESS" if records else "UNAVAILABLE",
        "candidates": [
            candidate(
                "inventory", r, "REPLENISH", r.model_dump(mode="json")
            ).model_dump(mode="json")
            for r in records
        ],
        "missing": [] if records else ["INVENTORY_UNAVAILABLE"],
    }


def supplier(data, intel, request):
    evidence = intel.results.get("disruption")
    restrictions = ReplenishmentRestrictions.model_validate(
        evidence.output.get("replenishment_restrictions", {}) if evidence else {}
    )
    suppliers = {s.id: s for s in data.business.suppliers if usable(s) and s.approved}
    quotes = [
        q
        for q in data.business.supplier_quotes
        if usable(q)
        and q.supplier_id in suppliers
        and q.supplier_id not in restrictions.supplier_ids
        and q.location_id not in restrictions.location_ids
        and suppliers[q.supplier_id].location_id not in restrictions.location_ids
        and q.id not in restrictions.quote_ids
    ]
    return {
        "status": "SUCCESS" if quotes else "UNAVAILABLE",
        "restrictions": restrictions.model_dump(mode="json"),
        "candidates": [
            candidate(
                "supplier",
                q,
                "PROCURE",
                {
                    **q.model_dump(mode="json"),
                    "supplier_quality": suppliers[q.supplier_id].quality_score,
                    "supplier_reliability": suppliers[q.supplier_id].reliability,
                },
            ).model_dump(mode="json")
            for q in quotes
        ],
        "missing": [] if quotes else ["PROCUREMENT_UNAVAILABLE"],
    }


def disruption_response(data, intel, request, peers):
    events = [e for e in data.business.disruptions if usable(e)]
    candidates = []
    for event in events:
        affected = set(event.affected_entities)
        for domain, output in peers.items():
            for option in output.get("routes", output.get("candidates", [])):
                params = option.get("parameters", option)
                identities = {
                    str(params.get(k, ""))
                    for k in (
                        "shipment_id",
                        "supplier_id",
                        "sku_id",
                        "location_id",
                        "id",
                    )
                }
                if not identities & affected:
                    continue
                action = {
                    "route": "REROUTE",
                    "modal": "MODAL_SWITCH",
                    "supplier": "SUPPLIER_SUBSTITUTION",
                    "inventory": "REPLAN_REPLENISHMENT",
                }[domain]
                candidate_id = option["candidate_id"]
                c = candidate(
                    "disruption_response",
                    event,
                    action,
                    {
                        "event_id": event.id,
                        "affected_entities": event.affected_entities,
                        "resource_candidate_id": candidate_id,
                        "resource_domain": domain,
                        "resource_parameters": params,
                        "status": "REQUIRES_OPTIMIZATION",
                    },
                )
                c.candidate_id += ":" + candidate_id
                candidates.append(c.model_dump(mode="json"))
    return {
        "status": "PARTIAL" if candidates else "UNAVAILABLE",
        "candidates": candidates,
        "missing": ["CONTINGENCY_SELECTION_REQUIRES_OPERATOR_SCOPE"]
        if candidates
        else ["DISRUPTION_RESOURCE_CANDIDATES_UNAVAILABLE"]
        if events
        else ["DISRUPTION_EVIDENCE_UNAVAILABLE"],
    }


def executive(data, intel, request, peers):
    return {
        "status": "SUCCESS",
        "candidates": [],
        "missing": [],
        "explanation": {
            "what_happened": request.operation,
            "known": {k: v.status for k, v in intel.results.items()},
            "options_generated": {
                k: len(v.get("routes", v.get("candidates", [])))
                for k, v in peers.items()
            },
            "missing_information": sorted(
                {m for v in peers.values() for m in v.get("missing", [])}
            ),
            "selection": "Deterministic optimization selects candidates; this agent has no override authority.",
            "assumptions": request.scenarios.model_dump() if request.scenarios else [],
            "remaining_risks": [
                "Forecast model unavailable"
                if intel.results["forecast"].status == "MODEL_UNAVAILABLE"
                else "See forecast evidence"
            ],
        },
    }


DOMAINS = {
    "route": route,
    "modal": modal,
    "inventory": inventory,
    "supplier": supplier,
    "disruption_response": disruption_response,
    "executive": executive,
}
