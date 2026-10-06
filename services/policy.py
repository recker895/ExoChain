"""Authorization is bound to the full immutable input/plan, never a status flag."""

import hashlib
import json
import math
from datetime import datetime

from config.settings import settings
from core.schemas.contracts import (
    BusinessInputs,
    BusinessSnapshotIdentity,
    OptimizationSolution,
    RouteCandidate,
    POLICY_VERSION,
    RunRequest,
    ValidationResult,
    utcnow,
)
from services.providers.business import business_hash, production_source_errors
from agents.intelligence.intelligence_agents import supply_restrictions


def plan_hash(state):
    body = {
        key: state.get(key)
        for key in (
            "run_id", "request", "business_inputs", "data",
            "intelligence", "decisions", "optimization",
        )
    }
    body["policy_version"] = POLICY_VERSION
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def validate(state):
    reasons = []
    request = RunRequest.model_validate(state["request"])
    plan = OptimizationSolution.model_validate(state["optimization"])
    business = BusinessInputs.model_validate(state["business_inputs"])
    if request.demo_mode:
        if request.operation != "TRANSPORT":
            reasons = []
            if settings.ENVIRONMENT == "production":
                reasons.append("DEMO_MODE_DISABLED_IN_PRODUCTION")
            if request.operation == "REPLENISHMENT":
                if plan.status not in {"OPTIMAL", "FEASIBLE", "NO_ACTION_REQUIRED"}:
                    reasons.extend(plan.constraint_violations or ["NO_FEASIBLE_DEMO_PLAN"])
                else:
                    reasons.extend(validate_replenishment(request, business, plan))
            if plan.run_id != state["run_id"]:
                reasons.append("PLAN_RUN_MISMATCH")
            if business.model_dump(mode="json") != state.get("data", {}).get("business"):
                reasons.append("BUSINESS_SNAPSHOT_MISMATCH")
            return ValidationResult(
                valid=not reasons, status="DEMO_REVIEWED" if not reasons else "DEMO_WARNINGS",
                reasons=sorted(set(reasons)), plan_hash=plan_hash(state),
            )
        return validate_transport_demo(state, request, plan, business)
    now = utcnow()
    if plan.run_id != state["run_id"]:
        reasons.append("PLAN_RUN_MISMATCH")
    if business.model_dump(mode="json") != state.get("data", {}).get("business"):
        reasons.append("BUSINESS_SNAPSHOT_MISMATCH")
    identity = state.get("data", {}).get("business_identity")
    if request.operation == "TRANSPORT" and settings.ENVIRONMENT == "production":
        reasons.extend(production_source_errors(business))
        if not identity or not identity.get("configured"):
            reasons.append("AUTHORITATIVE_BUSINESS_SOURCE_REQUIRED")
    if request.operation == "REPLENISHMENT":
        reasons.extend(production_source_errors(business))
        if settings.ENVIRONMENT == "production" and (not identity or not identity.get("configured")):
            reasons.append("AUTHORITATIVE_BUSINESS_SOURCE_REQUIRED")
        if settings.ENVIRONMENT == "production" and request.simulation:
            reasons.append("PRODUCTION_SIMULATION_FORBIDDEN")
        reservations = state.get("data", {}).get("reservations", {})
        if set(request.inventory_ids).intersection(reservations.get("inventory_ids", [])):
            reasons.append("INVENTORY_VERSION_ALREADY_COMMITTED")
        reserved_units = reservations.get("quote_units", {})
        for quote in business.supplier_quotes:
            selected_units = sum(
                a.get("quantity", 0) for a in plan.selected_actions
                if a.get("quote_id") == quote.id
            )
            if selected_units + reserved_units.get(quote.id, 0) > quote.capacity_units:
                reasons.append("SUPPLIER_CAPACITY_ALREADY_COMMITTED")
        if not identity:
            reasons.append("BUSINESS_SNAPSHOT_IDENTITY_REQUIRED")
        else:
            identity = BusinessSnapshotIdentity.model_validate(identity)
            if identity.snapshot_hash != business_hash(business):
                reasons.append("BUSINESS_SNAPSHOT_HASH_MISMATCH")
        source = state.get("data", {}).get("sources", {}).get("business", {})
        if source.get("quality") != "VALID":
            reasons.append("VALID_BUSINESS_SOURCE_REQUIRED")
    required = set(request.required_components)
    if request.operation == "REPLENISHMENT":
        required.update(("inventory", "procurement"))
        allowed = {"inventory", "procurement"}
    else:
        required = required or {"route"}
        allowed = {"route", "modal"}
    if request.operation == "ASSESSMENT":
        reasons.append("BUSINESS_DECISION_REQUEST_REQUIRED")
    if required - allowed or {"route", "modal"} <= required:
        reasons.append("OPERATION_COMPONENT_SCOPE_MISMATCH")
    if set(plan.components) != required:
        reasons.append("REQUIRED_COMPONENTS_MISMATCH")
    if (
        plan.status == "NO_ACTION_REQUIRED"
        and not plan.selected_actions
        and plan.total_cost_usd == 0
        and not reasons
    ):
        return ValidationResult(
            valid=False, status="NO_ACTION_REQUIRED",
            reasons=["NO_REPLENISHMENT_REQUIRED"], plan_hash=plan_hash(state),
        )
    if request.what_if:
        reasons.append("WHAT_IF_RUN_NOT_EXECUTABLE")
    if plan.status not in {"OPTIMAL", "FEASIBLE"}:
        reasons.append(f"OPTIMIZATION_{plan.status}")
    if not plan.components or not plan.selected_actions:
        reasons.append("EXECUTABLE_PLAN_REQUIRED")
    if plan.total_cost_usd is None or not math.isfinite(plan.total_cost_usd) or plan.total_cost_usd <= 0:
        reasons.append("KNOWN_POSITIVE_COST_REQUIRED")
    elif request.budget_usd is None or plan.total_cost_usd > min(
        request.budget_usd, settings.PROCUREMENT_MAX_BUDGET_USD
    ) + 1e-8:
        reasons.append("BUDGET_LIMIT")
    for name, component in plan.components.items():
        if component.component != name:
            reasons.append(f"COMPONENT_IDENTITY_MISMATCH:{name}")
        if component.status not in {"OPTIMAL", "FEASIBLE", "NOT_REQUIRED"}:
            reasons.append(f"COMPONENT_NOT_FEASIBLE:{name}")
        if component.status == "NOT_REQUIRED" and plan.selected_actions:
            reasons.append(f"REQUIRED_COMPONENT_SKIPPED:{name}")
        if component.status in {"OPTIMAL", "FEASIBLE"} and not component.selected:
            reasons.append(f"COMPONENT_SELECTION_REQUIRED:{name}")
        if component.constraint_violations:
            reasons.extend(component.constraint_violations)
        if component.total_cost_usd is None:
            reasons.append(f"COMPONENT_COST_UNKNOWN:{name}")
        for selected in component.selected:
            if name in {"route", "modal", "procurement"}:
                risk = selected.get("risk_score")
                if type(risk) not in (int, float) or not math.isfinite(risk):
                    reasons.append(f"KNOWN_RISK_REQUIRED:{name}")
                elif not 0 <= risk <= request.max_risk:
                    reasons.append(f"RISK_LIMIT:{name}")
    selected_shipments = [s for s in business.shipments if s.id in request.shipment_ids]
    selected_inventory = [i for i in business.inventory if i.id in request.inventory_ids]
    if request.operation == "TRANSPORT" and (
        not request.shipment_ids or len(selected_shipments) != len(set(request.shipment_ids))
    ):
        reasons.append("SHIPMENT_REQUIREMENT_UNAVAILABLE")
    if request.operation == "REPLENISHMENT" and (
        not request.inventory_ids or len(selected_inventory) != len(set(request.inventory_ids))
    ):
        reasons.append("INVENTORY_UNAVAILABLE")
    # Imported entities include quote/network dependencies in freshness checks.
    entities = selected_shipments + selected_inventory
    if request.operation == "TRANSPORT":
        selected_vessel_ids = {s.vessel_id for s in selected_shipments if s.vessel_id}
        entities += [p for p in business.vessel_profiles if p.id in selected_vessel_ids]
    if request.operation == "REPLENISHMENT":
        skus = {s.id: s for s in business.skus}
        for position in selected_inventory:
            if position.sku_id not in skus:
                reasons.append(f"SKU_UNAVAILABLE:{position.sku_id}")
            else:
                entities.append(skus[position.sku_id])
            references = {p.reference for p in position.provenance} | {d.id for d in business.demand}
            if position.demand_reference not in references:
                reasons.append(f"DEMAND_PROVENANCE_UNAVAILABLE:{position.id}")
            for demand in business.demand:
                if demand.id == position.demand_reference:
                    entities.append(demand)
                    if (demand.sku_id, demand.location_id) != (position.sku_id, position.location_id):
                        reasons.append(f"DEMAND_SCOPE_MISMATCH:{position.id}")
        entities += business.disruptions
    if "route" in plan.components:
        if business.route_network is None:
            reasons.append("ROUTE_NETWORK_UNAVAILABLE")
        else:
            entities += [business.route_network] + business.route_network.edges
            if business.route_network.planning_classification == "GEOGRAPHIC_PLANNING_ONLY":
                reasons.append("GEOGRAPHIC_PLANNING_ONLY_NOT_APPROVED_FOR_NAVIGATION")
    if "modal" in plan.components:
        entities += business.carrier_options + business.carriers
    if "procurement" in plan.components:
        entities += business.supplier_quotes + business.suppliers
    for entity in entities:
        if entity.data_quality != "VALID":
            reasons.append(f"INVALID_QUALITY:{entity.id}")
        if (
            entity.observed_at > now or entity.valid_until < now
            or (now - entity.observed_at).total_seconds() > settings.BUSINESS_MAX_AGE_SECONDS
        ):
            reasons.append(f"STALE_OR_FUTURE_EVIDENCE:{entity.id}")
        if any(p.observed_at > now for p in entity.provenance):
            reasons.append(f"FUTURE_PROVENANCE:{entity.id}")
    if request.operation == "REPLENISHMENT" and plan.selected_actions:
        selected_quote_ids = {a.get("quote_id") for a in plan.selected_actions}
        referenced = selected_inventory + [
            q for q in business.supplier_quotes if q.id in selected_quote_ids
        ]
        if not {p.reference for e in referenced for p in e.provenance} <= set(plan.source_references):
            reasons.append("PLAN_PROVENANCE_MISMATCH")
    # Vessel freshness authorizes navigation only.
    vessels = {str(v["mmsi"]): v for v in state.get("data", {}).get("vessels", [])}
    if "route" in plan.components:
        if request.vessel_reference:
            reference = request.vessel_reference
            selected = state.get("data", {}).get("sources", {}).get("selected_vessel", {})
            if selected.get("entity_id") != reference.mmsi or selected.get("quality") != "VALID":
                reasons.append(f"FRESH_SELECTED_VESSEL_REQUIRED:{reference.mmsi}")
        for shipment in selected_shipments:
            if request.vessel_reference and shipment.vessel_id != request.vessel_reference.mmsi:
                reasons.append(f"VESSEL_REFERENCE_NOT_BOUND_TO_SHIPMENT:{shipment.id}")
            if shipment.vessel_id:
                vessel = vessels.get(shipment.vessel_id)
                try:
                    observed = datetime.fromisoformat(
                        vessel["last_updated"].replace("Z", "+00:00")
                    ) if vessel else None
                    fresh = observed is not None and (
                        0 <= (now - observed).total_seconds() <= settings.AIS_MAX_AGE_SECONDS
                    )
                except (KeyError, ValueError, TypeError):
                    fresh = False
                if not fresh or vessel.get("quality") != "VALID":
                    reasons.append(f"FRESH_VESSEL_STATE_REQUIRED:{shipment.vessel_id}")
    reasons.extend(plan.constraint_violations)
    # Independently reconstruct the executable ledger.
    if request.operation == "REPLENISHMENT" and plan.selected_actions:
        reasons.extend(validate_replenishment(request, business, plan))
        quotes = {q.id: q for q in business.supplier_quotes}
        allocated = {}
        purchase_cost = 0.0
        for action in plan.selected_actions:
            quote = quotes.get(action.get("quote_id"))
            quantity = action.get("quantity")
            if (
                action.get("type") != "PURCHASE_ORDER" or quote is None
                or type(quantity) is not int or quantity <= 0
            ):
                reasons.append("INVALID_PROCUREMENT_ACTION")
                continue
            if any(
                action.get(k) != getattr(quote, k)
                for k in ("supplier_id", "sku_id", "location_id", "unit_cost_usd")
            ):
                reasons.append("ACTION_QUOTE_MISMATCH")
            allocated[quote.id] = allocated.get(quote.id, 0) + quantity
            expected_cost = quote.unit_cost_usd * quantity
            if not math.isclose(action.get("total_cost_usd", -1), expected_cost, abs_tol=0.001):
                reasons.append("ACTION_COST_MISMATCH")
            purchase_cost += expected_cost
        if any(units > quotes[qid].capacity_units for qid, units in allocated.items()):
            reasons.append("SUPPLIER_CAPACITY_EXCEEDED")
        inventory = plan.components.get("inventory")
        if inventory:
            fixed_cost = sum(i.get("ordering_cost_usd_committed", 0) for i in inventory.selected)
            if plan.total_cost_usd is None or not math.isclose(
                plan.total_cost_usd, purchase_cost + fixed_cost, abs_tol=0.001
            ):
                reasons.append("PLAN_COST_LEDGER_MISMATCH")
            for item in inventory.selected:
                units = sum(
                    a.get("quantity", 0) for a in plan.selected_actions
                    if (a.get("sku_id"), a.get("location_id")) == (item["sku_id"], item["location_id"])
                )
                required = sum(
                    i["order_units"] for i in inventory.selected
                    if (i["sku_id"], i["location_id"]) == (item["sku_id"], item["location_id"])
                )
                if units != required:
                    reasons.append("PROCUREMENT_QUANTITY_MISMATCH")
    return ValidationResult(
        valid=not reasons, status="VALID" if not reasons else "BLOCKED",
        reasons=sorted(set(reasons)), plan_hash=plan_hash(state),
    )


def validate_transport_demo(state, request, plan, business):
    """Structural approval of a geographic demo, never a navigation clearance."""
    reasons = []
    if settings.ENVIRONMENT == "production":
        reasons.append("DEMO_MODE_DISABLED_IN_PRODUCTION")
    network = business.route_network
    if request.operation != "TRANSPORT" or set(request.required_components) != {"route"}:
        reasons.append("DEMO_ROUTE_SCOPE_REQUIRED")
    if request.what_if:
        reasons.append("WHAT_IF_RUN_NOT_APPROVABLE")
    if business.model_dump(mode="json") != state.get("data", {}).get("business"):
        reasons.append("BUSINESS_SNAPSHOT_MISMATCH")
    if plan.run_id != state["run_id"] or plan.status not in {"OPTIMAL", "FEASIBLE"}:
        reasons.append("OPTIMIZATION_NOT_FEASIBLE")
    if network is None or network.planning_classification != "GEOGRAPHIC_PLANNING_ONLY":
        reasons.append("GEOGRAPHIC_ROUTE_NETWORK_REQUIRED")
    if not request.shipment_ids or not business.shipments:
        reasons.append("SHIPMENT_REQUIREMENT_UNAVAILABLE")
    if set(plan.components) != {"route"}:
        reasons.append("ROUTE_COMPONENT_REQUIRED")
    component = plan.components.get("route")
    generated = {
        c.candidate_id: c for c in (
            RouteCandidate.model_validate(item)
            for item in state.get("decisions", {}).get("routes", [])
        )
    }
    if not generated or component is None or component.status not in {"OPTIMAL", "FEASIBLE"}:
        reasons.append("ROUTE_CANDIDATE_REQUIRED")
    elif len(component.selected) != len(request.shipment_ids):
        reasons.append("ONE_SELECTED_ROUTE_PER_SHIPMENT_REQUIRED")
    elif network is not None:
        edges = {edge.id: edge for edge in network.edges}
        shipments = {shipment.id: shipment for shipment in business.shipments}
        for selected in component.selected:
            candidate = generated.get(selected.get("candidate_id"))
            shipment = shipments.get(selected.get("shipment_id"))
            if candidate is None or shipment is None or shipment.id not in request.shipment_ids:
                reasons.append("SELECTED_ROUTE_NOT_GENERATED_FOR_SHIPMENT")
                continue
            if candidate.network_id != network.id or len(candidate.edge_ids) != 1:
                reasons.append("SELECTED_ROUTE_NETWORK_MISMATCH")
                continue
            edge = edges.get(candidate.edge_ids[0])
            if edge is None or not edge.open or edge.origin_id != shipment.origin_id or edge.destination_id != shipment.destination_id:
                reasons.append("SELECTED_ROUTE_ENDPOINT_MISMATCH")
                continue
            if (len(candidate.geometry) < 2 or candidate.geometry != edge.geometry
                    or candidate.distance_km != edge.distance_km
                    or candidate.duration_hours != edge.duration_hours
                    or candidate.distance_km <= 0 or candidate.duration_hours <= 0):
                reasons.append("SELECTED_ROUTE_GEOMETRY_OR_METRICS_INVALID")
            if (selected.get("geometry") != [point.model_dump(mode="json") for point in candidate.geometry]
                    or selected.get("distance_km") != candidate.distance_km
                    or selected.get("duration_hours") != candidate.duration_hours):
                reasons.append("SELECTED_ROUTE_DIFFERS_FROM_CANDIDATE")
            metric_fields = ["cost_usd", "fuel_litres", "risk_score", "weather_penalty", "current_penalty"]
            if state.get("data", {}).get("route_context"):
                from core.schemas.contracts import DataSnapshot
                from services.route_evidence import evaluate_route_metrics
                from services.voyage_assessment import route_fuel
                captured = DataSnapshot.model_validate(state["data"])
                reports = evaluate_route_metrics(captured)
                values = reports.get(edge.id, {}).get("metrics", {})
                metric_fields += ["wave_penalty", "port_penalty"]
                reference = request.vessel_reference
                profile = next((p for p in captured.business.vessel_profiles if
                                p.id == (reference.mmsi if reference else shipment.vessel_id)), None)
                fuel = route_fuel(candidate, profile)
                if fuel["status"] == "VALID":
                    values["fuel_litres"] = fuel["total_fuel_litres"]
            else:
                values = {}
            for field in metric_fields:
                expected = values.get(field) if values.get(field) is not None else getattr(edge, field, None)
                if selected.get(field) != getattr(candidate, field) or getattr(candidate, field) != expected:
                    reasons.append(f"SELECTED_ROUTE_METRIC_MISMATCH:{field}")
            if candidate.risk_score is not None and candidate.risk_score > request.max_risk:
                reasons.append("RISK_LIMIT")
            if request.max_duration_hours and candidate.duration_hours > request.max_duration_hours:
                reasons.append("DURATION_LIMIT")
            if candidate.cost_usd is not None and request.budget_usd is not None and candidate.cost_usd > request.budget_usd:
                reasons.append("BUDGET_LIMIT")
    if component and component.constraint_violations:
        reasons.extend(component.constraint_violations)
    reasons.extend(plan.constraint_violations)
    selected_routes = component.selected if component else []
    if (len(plan.selected_actions) != len(selected_routes)
            or any(action.get("type") != "TRANSPORT_PLAN"
                   or action.get("component") != "route"
                   or action.get("selection") != selected
                   for action, selected in zip(plan.selected_actions, selected_routes))):
        reasons.append("SELECTED_TRANSPORT_PLAN_REQUIRED")
    known_costs = [selected.get("cost_usd") for selected in selected_routes]
    expected_cost = sum(known_costs) if known_costs and all(cost is not None for cost in known_costs) else None
    if plan.total_cost_usd != expected_cost or (component and component.total_cost_usd != expected_cost):
        reasons.append("DEMO_COST_LEDGER_MISMATCH")
    return ValidationResult(
        valid=not reasons, status="VALID" if not reasons else "BLOCKED",
        reasons=sorted(set(reasons)), plan_hash=plan_hash(state),
    )


def validate_replenishment(request, business, plan):
    """Independently check solver selections against authoritative input records."""
    reasons = []
    inventory = plan.components.get("inventory")
    procurement = plan.components.get("procurement")
    if inventory is None or procurement is None:
        return ["REQUIRED_COMPONENTS_MISMATCH"]
    positions = {i.id: i for i in business.inventory if i.id in request.inventory_ids}
    selected_ids = [i.get("id") for i in inventory.selected]
    if len(selected_ids) != len(set(selected_ids)) or set(selected_ids) != set(positions):
        return ["INVENTORY_SELECTION_MISMATCH"]
    orders = {}
    receipt_stock = 0
    for selection in inventory.selected:
        position = positions[selection["id"]]
        if any(selection.get(k) != v for k, v in position.model_dump(mode="json").items()):
            reasons.append("INVENTORY_SOURCE_MISMATCH")
        order, shortage, ending = (
            selection.get(k) for k in ("order_units", "shortage_units", "ending_units")
        )
        if any(type(v) is not int or v < 0 for v in (order, shortage, ending)):
            reasons.append("INVALID_INVENTORY_QUANTITY")
            continue
        orders[position.id] = order
        receipt_stock += position.current_inventory_units + order
        if (
            position.current_inventory_units + order - position.expected_demand_units + shortage != ending
            or not position.safety_stock_units <= ending <= position.max_stock_units
            or order > max(0, position.max_stock_units - position.current_inventory_units)
            or shortage > math.floor(position.expected_demand_units * (1 - position.service_level) + 1e-8)
            or (order > 0 and position.lead_time_days > position.horizon_days)
        ):
            reasons.append("INVENTORY_CONSTRAINT_VIOLATION")
        fixed = position.ordering_cost_usd if order > 0 else 0
        if selection.get("ordering_cost_usd_committed") != fixed:
            reasons.append("INVENTORY_ORDERING_COST_MISMATCH")
    if request.warehouse_capacity_units is not None and receipt_stock > request.warehouse_capacity_units:
        reasons.append("WAREHOUSE_CAPACITY_EXCEEDED")
    quotes = {q.id: q for q in business.supplier_quotes}
    suppliers = {s.id: s for s in business.suppliers}
    restrictions = supply_restrictions(business.disruptions)
    allocations = {}
    expected_actions = []
    for selection in procurement.selected:
        quote = quotes.get(selection.get("id"))
        position = positions.get(selection.get("inventory_id"))
        units = selection.get("units")
        if quote is None or position is None or type(units) is not int or units <= 0:
            reasons.append("INVALID_SUPPLIER_ALLOCATION")
            continue
        supplier = suppliers.get(quote.supplier_id)
        if supplier is None or not supplier.approved:
            reasons.append("APPROVED_SUPPLIER_REQUIRED")
        if supplier and (
            selection.get("supplier_quality") != supplier.quality_score
            or selection.get("supplier_reliability") != supplier.reliability
        ):
            reasons.append("SUPPLIER_ASSESSMENT_SOURCE_MISMATCH")
        if (
            quote.supplier_id in restrictions.supplier_ids
            or quote.id in restrictions.quote_ids
            or quote.location_id in restrictions.location_ids
            or (supplier and supplier.location_id in restrictions.location_ids)
        ):
            reasons.append("DISRUPTION_SUPPLY_UNAVAILABLE")
        if any(selection.get(k) != v for k, v in quote.model_dump(mode="json").items()):
            reasons.append("SUPPLIER_QUOTE_SOURCE_MISMATCH")
        if (
            (quote.sku_id, quote.location_id) != (position.sku_id, position.location_id)
            or quote.lead_time_days > min(position.lead_time_days, position.horizon_days)
            or quote.risk_score > request.max_risk
        ):
            reasons.append("SUPPLIER_CONSTRAINT_VIOLATION")
        allocations[position.id] = allocations.get(position.id, 0) + units
        expected_actions.append({
            "type": "PURCHASE_ORDER",
            "quote_id": quote.id, "supplier_id": quote.supplier_id,
            "sku_id": quote.sku_id, "location_id": quote.location_id,
            "quantity": units, "unit_cost_usd": quote.unit_cost_usd,
            "total_cost_usd": quote.unit_cost_usd * units,
            "lead_time_days": quote.lead_time_days, "quote_version": quote.version,
        })
    if any(allocations.get(pid, 0) != units for pid, units in orders.items()):
        reasons.append("INVENTORY_ALLOCATION_MISMATCH")
    if plan.selected_actions != expected_actions:
        reasons.append("EXECUTION_SELECTION_MISMATCH")
    if procurement.total_cost_usd != sum(a["total_cost_usd"] for a in expected_actions):
        reasons.append("PROCUREMENT_COST_MISMATCH")
    return reasons
