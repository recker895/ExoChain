"""Deterministic optimization over immutable, validated commercial evidence."""

from __future__ import annotations

import math
import time
from importlib.metadata import version

from ortools.sat.python import cp_model

from config.settings import settings
from core.schemas.contracts import ComponentSolution, OptimizationSolution
from optimization.navigation import score_routes, score_demo_routes
from optimization.monte_carlo import MonteCarloSimulator


def cents(value):
    return math.ceil(value * 100 - 1e-8)


def solver():
    instance = cp_model.CpSolver()
    instance.parameters.max_time_in_seconds = settings.SOLVER_TIMEOUT_SECONDS
    instance.parameters.num_search_workers = 1
    instance.parameters.random_seed = 42
    return instance


def finish(name, instance, status, start, count):
    return ComponentSolution(
        component=name,
        status="OPTIMAL"
        if status == cp_model.OPTIMAL
        else "FEASIBLE"
        if status == cp_model.FEASIBLE
        else "INFEASIBLE"
        if status == cp_model.INFEASIBLE
        else "FAILED",
        solver="OR-Tools CP-SAT",
        solver_version=version("ortools"),
        solve_time_ms=(time.perf_counter() - start) * 1000,
        objective_value=instance.ObjectiveValue()
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        else None,
        candidate_count=count,
        explanation={
            "random_seed": 42,
            "workers": 1,
            "time_limit_seconds": settings.SOLVER_TIMEOUT_SECONDS,
            "best_bound": instance.BestObjectiveBound()
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
            else None,
        },
    )


def unavailable(name, reason):
    return ComponentSolution(
        component=name, status="DATA_REQUIRED", constraint_violations=[reason]
    )


def route_optimize(request, business, decisions):
    start = time.perf_counter()
    model = cp_model.CpModel()
    ranked = []
    rejected = []
    for shipment_id in request.shipment_ids:
        routes = [r for r in decisions.routes if r.shipment_id == shipment_id]
        if not routes:
            return unavailable(
                "route", f"ROUTE_NETWORK_UNAVAILABLE_OR_NO_FEASIBLE_ROUTE:{shipment_id}"
            )
        scorer = score_demo_routes if request.demo_mode else score_routes
        for record in scorer(routes, request.weights):
            if request.demo_mode and not record["used_metrics"]:
                return unavailable("route", "NO_AVAILABLE_METRIC_HAS_A_POSITIVE_WEIGHT")
            reasons = []
            if record["risk_score"] is not None and record["risk_score"] > request.max_risk:
                reasons.append("RISK_LIMIT")
            if (
                request.max_duration_hours
                and record["duration_hours"] > request.max_duration_hours
            ):
                reasons.append("DURATION_LIMIT")
            if reasons:
                rejected.append(
                    {"candidate_id": record["candidate_id"], "reasons": reasons}
                )
            else:
                ranked.append(record)
    if not ranked:
        return ComponentSolution(
            component="route",
            status="INFEASIBLE",
            rejected=rejected,
            constraint_violations=["NO_ROUTE_SATISFIES_LIMITS"],
        )
    variables = [model.NewBoolVar(r["candidate_id"]) for r in ranked]
    for shipment in request.shipment_ids:
        model.Add(
            sum(v for r, v in zip(ranked, variables) if r["shipment_id"] == shipment)
            == 1
        )
    priced = [(r, v) for r, v in zip(ranked, variables) if r["cost_usd"] is not None]
    if priced and request.budget_usd is not None:
        model.Add(
            sum(cents(r["cost_usd"]) * v for r, v in priced)
            <= math.floor(request.budget_usd * 100)
        )
    model.Minimize(
        sum(
            round(r["objective_value"] * 1_000_000) * v
            for r, v in zip(ranked, variables)
        )
    )
    instance = solver()
    status = instance.Solve(model)
    out = finish("route", instance, status, start, len(ranked) + len(rejected))
    out.rejected = rejected
    out.explanation.update(
        {
            "generation": decisions.explanation.get("route_generation", {}),
            "weights": request.weights.model_dump(),
            "objective": "sum(weight * metric / maximum metric in shipment candidate set)",
            "objective_scale": 1_000_000,
            "guarantee": "Optimal only over generated candidates when CP-SAT returns OPTIMAL; bounded search is not a global navigational optimum.",
            "demo_geographic_only": request.demo_mode,
            "used_metrics": ranked[0].get("used_metrics", []) if request.demo_mode else None,
            "excluded_metrics": ranked[0].get("excluded_metrics", []) if request.demo_mode else None,
            "unchecked_constraints": (["Risk limit cannot be checked: risk evidence unavailable"]
                if any(r["risk_score"] is None for r in ranked) else []) +
                (["Budget not checked: no evidenced route cost"] if not priced else []),
        }
    )
    if out.status in {"OPTIMAL", "FEASIBLE"}:
        out.selected = [r for r, v in zip(ranked, variables) if instance.Value(v)]
        out.alternatives = [
            r for r, v in zip(ranked, variables) if not instance.Value(v)
        ]
        out.total_cost_usd = (sum(r["cost_usd"] for r in out.selected)
                              if all(r["cost_usd"] is not None for r in out.selected) else None)
        out.explanation["recommendation"] = {
            "selected": [{"candidate_id": r["candidate_id"], "label": r.get("planning_label"),
                          "distance_nm": r["distance_km"] / 1.852, "duration_hours": r["duration_hours"],
                          "objective_value": r["objective_value"]} for r in out.selected],
            "why": "Lowest weighted normalized objective among generated routes satisfying the checkable input limits.",
            "used_metrics": out.selected[0].get("used_metrics", []),
            "not_a_global_navigation_optimum": True,
        }
    return out


def modal_optimize(request, business, decisions):
    start = time.perf_counter()
    model = cp_model.CpModel()
    options = [c.parameters for c in decisions.candidates if c.decision_type == "modal"]
    shipments = {s.id: s for s in business.shipments if s.id in request.shipment_ids}
    if set(shipments) != set(request.shipment_ids) or not shipments:
        return unavailable("modal", "SHIPMENT_REQUIREMENT_UNAVAILABLE")
    if not options:
        return unavailable("modal", "FREIGHT_RATE_UNAVAILABLE")
    options = [
        o
        for o in options
        if o["shipment_id"] in shipments
        and o["risk_score"] <= request.max_risk
        and o["duration_hours"]
        <= min(
            request.max_duration_hours or math.inf,
            (
                shipments[o["shipment_id"]].due_at
                - shipments[o["shipment_id"]].departure_at
            ).total_seconds()
            / 3600,
        )
    ]
    quantities = [model.NewIntVar(0, o["capacity_units"], o["id"]) for o in options]
    for sid, shipment in shipments.items():
        model.Add(
            sum(q for o, q in zip(options, quantities) if o["shipment_id"] == sid)
            == shipment.quantity
        )
    model.Add(
        sum(cents(o["cost_per_unit_usd"]) * q for o, q in zip(options, quantities))
        <= math.floor(request.budget_usd * 100)
    )
    scales = {
        k: max((o[k] for o in options), default=1) or 1
        for k in ("cost_per_unit_usd", "duration_hours", "risk_score")
    }
    coefficients = [
        round(
            1_000_000
            * (
                request.weights.cost
                * o["cost_per_unit_usd"]
                / scales["cost_per_unit_usd"]
                + request.weights.time * o["duration_hours"] / scales["duration_hours"]
                + request.weights.risk * o["risk_score"] / scales["risk_score"]
            )
        )
        for o in options
    ]
    model.Minimize(sum(c * q for c, q in zip(coefficients, quantities)))
    instance = solver()
    status = instance.Solve(model)
    out = finish("modal", instance, status, start, len(options))
    out.explanation.update(
        {
            "weights": request.weights.model_dump(),
            "normalization_scales": scales,
            "quantity_constraint": "Exact fulfillment per shipment; per-option capacity; per-mode service/risk limits",
            "objective_scale": 1_000_000,
        }
    )
    if out.status in {"OPTIMAL", "FEASIBLE"}:
        out.selected = [
            {
                **o,
                "units": instance.Value(q),
                "total_cost_usd": o["cost_per_unit_usd"] * instance.Value(q),
            }
            for o, q in zip(options, quantities)
            if instance.Value(q) > 0
        ]
        out.total_cost_usd = sum(o["total_cost_usd"] for o in out.selected)
    return out


def replenishment_optimize(request, business, decisions, reservations=None):
    """One coupled CP-SAT model, published through the existing two components."""
    start = time.perf_counter()
    items = [
        c.parameters for c in decisions.candidates if c.decision_type == "inventory"
    ]
    quotes = [
        c.parameters for c in decisions.candidates if c.decision_type == "supplier"
    ]
    if not items or {i["id"] for i in items} != set(request.inventory_ids):
        return {
            "inventory": unavailable("inventory", "INVENTORY_UNAVAILABLE"),
            "procurement": unavailable("procurement", "VALID_INVENTORY_PLAN_REQUIRED"),
        }
    reservations = reservations or {}
    reserved_ids = set(reservations.get("inventory_ids", []))
    reserved_quotes = reservations.get("quote_units", {})
    if reserved_ids.intersection(request.inventory_ids):
        return {
            k: unavailable(k, "INVENTORY_VERSION_ALREADY_COMMITTED")
            for k in ("inventory", "procurement")
        }
    if not quotes:
        return {
            "inventory": unavailable("inventory", "PROCUREMENT_UNAVAILABLE"),
            "procurement": unavailable("procurement", "PROCUREMENT_UNAVAILABLE"),
        }
    model = cp_model.CpModel()
    rows, allocations, requirements, safety_shortfalls = [], [], [], []
    for item in items:
        maximum = max(0, item["max_stock_units"] - item["current_inventory_units"])
        order = model.NewIntVar(0, maximum, "order_" + item["id"])
        shortage_limit = math.floor(
            item["expected_demand_units"] * (1 - item["service_level"]) + 1e-8
        )
        shortage = model.NewIntVar(
            0, item["expected_demand_units"], "shortage_" + item["id"]
        )
        end = model.NewIntVar(0, item["max_stock_units"], "ending_" + item["id"])
        placed = model.NewBoolVar("placed_" + item["id"])
        model.Add(order <= maximum * placed)
        model.Add(order >= placed)
        model.Add(
            item["current_inventory_units"]
            + order
            - item["expected_demand_units"]
            + shortage
            == end
        )
        safety_shortfall = model.NewIntVar(
            0, item["safety_stock_units"], "safety_shortfall_" + item["id"]
        )
        model.AddMaxEquality(safety_shortfall, [0, item["safety_stock_units"] - end])
        safety_shortfalls.append(safety_shortfall)
        if item["lead_time_days"] > item["horizon_days"]:
            model.Add(order == 0)
        rows.append((item, order, shortage, end, placed))
        for quote in quotes:
            if (quote["sku_id"], quote["location_id"]) != (
                item["sku_id"],
                item["location_id"],
            ):
                continue
            if quote["risk_score"] > request.max_risk or quote["lead_time_days"] > min(
                item["lead_time_days"], item["horizon_days"]
            ):
                continue
            capacity = max(
                0, quote["capacity_units"] - reserved_quotes.get(quote["id"], 0)
            )
            quantity = model.NewIntVar(0, capacity, item["id"] + ":" + quote["id"])
            allocations.append((item, quote, quantity))
        model.Add(sum(q for i, _, q in allocations if i["id"] == item["id"]) == order)
        full = max(
            0,
            item["expected_demand_units"]
            + item["safety_stock_units"]
            - item["current_inventory_units"],
        )
        minimum = max(0, full - shortage_limit)
        requirements.append(
            {
                "inventory_id": item["id"],
                "sku_id": item["sku_id"],
                "full_demand_replenishment_units": full,
                "configured_shortage_allowance_units": shortage_limit,
                "target_order_units": minimum,
                "reference_inventory_commitment_usd": item["unit_cost_usd"] * minimum
                + (item["ordering_cost_usd"] if minimum else 0),
            }
        )
    for quote in quotes:
        model.Add(
            sum(q for _, s, q in allocations if s["id"] == quote["id"])
            <= max(0, quote["capacity_units"] - reserved_quotes.get(quote["id"], 0))
        )
    if request.warehouse_capacity_units is not None:
        model.Add(
            sum(i["current_inventory_units"] + q for i, q, _, _, _ in rows)
            <= request.warehouse_capacity_units
        )
    purchase = sum(cents(s["unit_cost_usd"]) * q for _, s, q in allocations)
    fixed = sum(cents(i["ordering_cost_usd"]) * p for i, _, _, _, p in rows)
    commitment = purchase + fixed
    economic = commitment + sum(
        cents(i["holding_cost_per_unit_usd"]) * e + cents(i["shortage_penalty_usd"]) * s
        for i, _, s, e, _ in rows
    )
    # Budget is hard; service level and safety stock are reported soft targets.
    # The diagnostic retains every physical/supplier/reservation constraint.
    diagnostic = model.Clone()
    diagnostic.Minimize(commitment)
    model.Add(commitment <= math.floor(request.budget_usd * 100))
    total_shortage = sum(s for _, _, s, _, _ in rows)
    total_safety_shortfall = sum(safety_shortfalls)
    model.Minimize(total_shortage)
    instance = solver()
    status = instance.Solve(model)
    components = {
        k: finish(
            k, instance, status, start, len(items) if k == "inventory" else len(quotes)
        )
        for k in ("inventory", "procurement")
    }
    explanation = {
        "formulation": "COUPLED_REPLENISHMENT_V3_SOFT_TARGETS",
        "balance": "current + order - demand + shortage = ending",
        "safety_stock": "Soft target: shortfall = max(0, safety_stock - ending)",
        "service_level": "Soft target; shortage may exceed floor(demand * (1 - service_level))",
        "warehouse": "Peak receipt stock: sum(current + order) <= supplied capacity",
        "objective": "Lexicographic: total demand shortage, then safety-stock shortfall, then actual purchase + ordering + holding + shortage penalty",
        "objective_unit": "First two priorities: units; economic priority: USD cents, with monetary coefficients rounded upward to cents",
        "requirements": requirements,
        "budget_usd": request.budget_usd,
        "reference_price_role": "Inventory unit_cost_usd is informational; supplier quotes determine committed spend",
        "quote_preferences": "Risk and lead time are binding limits; quality/reliability and request weights break equal-economic-cost ties only",
    }
    for component in components.values():
        component.explanation.update(explanation)
    if status == cp_model.INFEASIBLE:
        probe = solver()
        probe_status = probe.Solve(diagnostic)
        proof = {
            "status": probe.StatusName(probe_status),
            "scope": "Diagnostic only; budget removed, all other constraints retained",
        }
        if probe_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            proof["feasible_budget_usd"] = probe.Value(commitment) / 100
            proof["minimum_feasible_budget_usd"] = (
                probe.Value(commitment) / 100
                if probe_status == cp_model.OPTIMAL
                else None
            )
            proof["lower_bound_usd"] = probe.BestObjectiveBound() / 100
            proof["requirements"] = [
                {
                    "inventory_id": i["id"],
                    "order_units": probe.Value(q),
                    "shortage_units": probe.Value(s),
                    "ending_units": probe.Value(e),
                }
                for i, q, s, e, _ in rows
            ]
            proof["allocations"] = [
                {
                    "inventory_id": i["id"],
                    "quote_id": s["id"],
                    "units": probe.Value(q),
                    "unit_cost_usd": s["unit_cost_usd"],
                }
                for i, s, q in allocations
                if probe.Value(q)
            ]
        reason = (
            "REPLENISHMENT_MINIMUM_COMMITMENT_EXCEEDS_BUDGET"
            if probe_status == cp_model.OPTIMAL
            and probe.Value(commitment) > math.floor(request.budget_usd * 100)
            else "REPLENISHMENT_CONSTRAINTS_INFEASIBLE"
        )
        for component in components.values():
            component.explanation["feasibility_analysis"] = proof
            component.constraint_violations.append(reason)
        return components
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return components
    # Sequential solves give exact priorities without oversized/overflowing
    # penalty weights. Only a PROVEN optimum is fixed for the next priority.
    # On timeout retain the last feasible allocation and never claim optimality.
    stages = [
        {
            "objective": "demand_shortage_units",
            "status": instance.StatusName(status),
            "value": instance.Value(total_shortage),
        }
    ]
    previous = total_shortage
    for name, objective in (
        ("safety_stock_shortfall_units", total_safety_shortfall),
        ("economic_cost_cents", economic),
    ):
        if status != cp_model.OPTIMAL:
            break
        model.Add(previous == instance.Value(previous))
        model.Minimize(objective)
        next_instance = solver()
        next_status = next_instance.Solve(model)
        stages.append(
            {
                "objective": name,
                "status": next_instance.StatusName(next_status),
                "value": next_instance.Value(objective)
                if next_status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
                else None,
            }
        )
        if next_status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            status = cp_model.FEASIBLE
            break
        instance, status, previous = next_instance, next_status, objective
    for component in components.values():
        component.status = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"
        component.explanation["lexicographic_stages"] = stages
        component.explanation["best_bound"] = (
            instance.BestObjectiveBound()
            if len(stages) == 3 and stages[-1]["value"] is not None
            else None
        )
    economic_value = instance.Value(economic)
    if status == cp_model.OPTIMAL and allocations:
        model.Add(economic == economic_value)
        scales = {
            k: max((s[k] for _, s, _ in allocations), default=1) or 1
            for k in ("unit_cost_usd", "lead_time_days")
        }
        model.Minimize(
            sum(
                round(
                    1_000_000
                    * (
                        request.weights.cost
                        * s["unit_cost_usd"]
                        / scales["unit_cost_usd"]
                        + request.weights.time
                        * s["lead_time_days"]
                        / scales["lead_time_days"]
                        + request.weights.risk
                        * (
                            s["risk_score"]
                            + 2
                            - s["supplier_quality"]
                            - s["supplier_reliability"]
                        )
                        / 3
                    )
                )
                * q
                for _, s, q in allocations
            )
        )
        tie = solver()
        tie_status = tie.Solve(model)
        if tie_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            instance = tie
        for component in components.values():
            component.explanation["tie_break_status"] = tie.StatusName(tie_status)
    inventory, procurement = components["inventory"], components["procurement"]
    inventory.selected = [
        {
            **i,
            "order_units": instance.Value(q),
            "shortage_units": instance.Value(s),
            "ending_units": instance.Value(e),
            "configured_service_level": i["service_level"],
            "achieved_service_level": 1 - instance.Value(s) / i["expected_demand_units"]
            if i["expected_demand_units"]
            else 1.0,
            "service_level_shortfall_units": max(
                0,
                instance.Value(s)
                - math.floor(
                    i["expected_demand_units"] * (1 - i["service_level"]) + 1e-8
                ),
            ),
            "safety_stock_shortfall_units": instance.Value(shortfall),
            "ordering_cost_usd_committed": i["ordering_cost_usd"] * instance.Value(p),
            "holding_cost_usd_estimated": i["holding_cost_per_unit_usd"]
            * instance.Value(e),
            "purchase_estimate_usd": i["unit_cost_usd"] * instance.Value(q),
        }
        for (i, q, s, e, p), shortfall in zip(rows, safety_shortfalls)
    ]
    procurement.selected = [
        {
            **s,
            "inventory_id": i["id"],
            "units": instance.Value(q),
            "total_cost_usd": s["unit_cost_usd"] * instance.Value(q),
        }
        for i, s, q in allocations
        if instance.Value(q) > 0
    ]
    procurement.total_cost_usd = sum(s["total_cost_usd"] for s in procurement.selected)
    inventory.total_cost_usd = procurement.total_cost_usd + sum(
        i["ordering_cost_usd_committed"] for i in inventory.selected
    )
    for selection in inventory.selected:
        selection["committed_cost_usd"] = selection[
            "ordering_cost_usd_committed"
        ] + sum(
            s["total_cost_usd"]
            for s in procurement.selected
            if s["inventory_id"] == selection["id"]
        )
    if not procurement.selected:
        procurement.status = "NOT_REQUIRED"
    unmet = any(
        i["service_level_shortfall_units"] or i["safety_stock_shortfall_units"]
        for i in inventory.selected
    )
    total_demand = sum(i["expected_demand_units"] for i in inventory.selected)
    shortage_units = sum(i["shortage_units"] for i in inventory.selected)
    for component in components.values():
        component.objective_value = economic_value
        component.solve_time_ms = (time.perf_counter() - start) * 1000
        component.explanation["economic_cost_cents"] = economic_value
        component.explanation.update(
            {
                "plan_type": "BUDGET_CONSTRAINED_PARTIAL_REPLENISHMENT"
                if unmet
                else "TARGET_SATISFYING_REPLENISHMENT",
                "shortage_units": shortage_units,
                "achieved_service_level": 1 - shortage_units / total_demand
                if total_demand
                else 1.0,
                "safety_stock_shortfall_units": sum(
                    i["safety_stock_shortfall_units"] for i in inventory.selected
                ),
                "committed_cost_usd": inventory.total_cost_usd,
                "remaining_budget_usd": request.budget_usd - inventory.total_cost_usd,
                "targets_met": not unmet,
                "governance": "Optimization feasibility does not override independent validation or approval policy",
            }
        )
    return components


def optimize(run_id, request, data, decisions):
    plan = OptimizationSolution(
        run_id=run_id,
        status="BLOCKED",
        explanation={
            "input_snapshot": "Run data is immutable; what-if runs have independent identities.",
            "decision": decisions.explanation,
        },
    )
    business = data.business
    if request.operation == "REPLENISHMENT":
        source = data.sources.get("business")
        if source is None or source.quality != "VALID":
            plan.constraint_violations = ["VALID_BUSINESS_SOURCE_REQUIRED"]
            if not business.inventory or not set(request.inventory_ids) <= {i.id for i in business.inventory}:
                plan.constraint_violations.append("INVENTORY_UNAVAILABLE")
            return plan
        skus = {s.id for s in business.skus}
        missing_skus = {
            i.sku_id for i in business.inventory if i.id in request.inventory_ids
        } - skus
        if missing_skus:
            plan.constraint_violations = [
                "SKU_UNAVAILABLE:" + s for s in sorted(missing_skus)
            ]
            return plan
    if request.operation == "ASSESSMENT":
        if request.demo_mode:
            plan.status = "ASSESSMENT_COMPLETE"
            return plan
        plan.constraint_violations = ["BUSINESS_DECISION_REQUEST_REQUIRED"]
        return plan
    if request.budget_usd is None and not (request.demo_mode and request.operation == "TRANSPORT"):
        plan.constraint_violations = ["BUDGET_REQUIRED"]
        return plan
    if not request.demo_mode and request.budget_usd > settings.PROCUREMENT_MAX_BUDGET_USD:
        plan.constraint_violations = ["BUDGET_EXCEEDS_POLICY"]
        return plan
    required = set(request.required_components)
    if request.operation == "REPLENISHMENT":
        required.update(("inventory", "procurement"))
    else:
        if not request.shipment_ids:
            plan.constraint_violations = ["SHIPMENT_REQUIREMENT_UNAVAILABLE"]
            return plan
        if not required:
            required.add("route")
    plan.explanation["required_components"] = sorted(required)
    if (
        request.operation == "TRANSPORT"
        and required - {"route", "modal"}
        or request.operation == "REPLENISHMENT"
        and required - {"inventory", "procurement"}
    ):
        plan.constraint_violations = ["OPERATION_COMPONENT_SCOPE_MISMATCH"]
        return plan
    if {"route", "modal"} <= required:
        plan.constraint_violations = [
            "TRANSPORT_COST_SCOPE_AMBIGUOUS: choose route operating plan or carrier allocation; do not double-book a shipment"
        ]
        return plan
    functions = {
        "route": route_optimize,
        "modal": modal_optimize,
    }
    if request.operation == "REPLENISHMENT":
        try:
            plan.components.update(
                replenishment_optimize(
                    request, business, decisions, data.reservations.model_dump()
                )
            )
        except Exception as exc:
            plan.components.update(
                {
                    k: ComponentSolution(
                        component=k,
                        status="FAILED",
                        constraint_violations=[f"SOLVER_ERROR:{type(exc).__name__}"],
                    )
                    for k in ("inventory", "procurement")
                }
            )
    for name in ("route", "modal", "inventory", "procurement"):
        if name not in required or name in plan.components:
            continue
        try:
            plan.components[name] = functions[name](request, business, decisions)
        except Exception as exc:
            plan.components[name] = ComponentSolution(
                component=name,
                status="FAILED",
                constraint_violations=[f"SOLVER_ERROR:{type(exc).__name__}"],
            )
    bad = [
        k
        for k, v in plan.components.items()
        if v.status not in {"OPTIMAL", "FEASIBLE", "NOT_REQUIRED"}
    ]
    if bad:
        plan.status = (
            "FAILED"
            if any(v.status == "FAILED" for v in plan.components.values())
            else "INFEASIBLE"
            if any(v.status == "INFEASIBLE" for v in plan.components.values())
            else "BLOCKED"
        )
        plan.constraint_violations = [
            reason
            for k in bad
            for reason in (
                plan.components[k].constraint_violations
                or [f"{k.upper()}_{plan.components[k].status}"]
            )
        ]
        return plan
    if "procurement" in plan.components:
        plan.explanation["replenishment"] = dict(
            plan.components["inventory"].explanation
        )
        plan.total_cost_usd = plan.components["procurement"].total_cost_usd + sum(
            i["ordering_cost_usd_committed"]
            for i in plan.components["inventory"].selected
        )
        plan.selected_actions = [
            {
                "type": "PURCHASE_ORDER",
                "quote_id": s["id"],
                "supplier_id": s["supplier_id"],
                "sku_id": s["sku_id"],
                "location_id": s["location_id"],
                "quantity": s["units"],
                "unit_cost_usd": s["unit_cost_usd"],
                "total_cost_usd": s["total_cost_usd"],
                "lead_time_days": s["lead_time_days"],
                "quote_version": s["version"],
            }
            for s in plan.components["procurement"].selected
        ]
        plan.explanation["cost_ledger"] = {
            "supplier_purchase_cost_usd": plan.components["procurement"].total_cost_usd,
            "ordering_cost_usd": sum(
                i["ordering_cost_usd_committed"]
                for i in plan.components["inventory"].selected
            ),
            "holding_and_shortage": "Scenario/economic objective only, not duplicated as ERP purchase commitments.",
        }
    else:
        plan.total_cost_usd = (sum(v.total_cost_usd for v in plan.components.values())
                               if all(v.total_cost_usd is not None for v in plan.components.values()) else None)
        plan.selected_actions = [
            {"type": "TRANSPORT_PLAN", "component": k, "selection": r}
            for k, v in plan.components.items()
            for r in v.selected
        ]
    plan.status = (
        "FEASIBLE"
        if any(v.status == "FEASIBLE" for v in plan.components.values())
        else "OPTIMAL"
    )
    if not plan.selected_actions:
        if request.operation == "REPLENISHMENT" and not plan.components[
            "inventory"
        ].explanation.get("targets_met", True):
            plan.status = "BLOCKED"
            plan.constraint_violations.append(
                "NO_AFFORDABLE_ELIGIBLE_PROCUREMENT_ACTION"
            )
        else:
            plan.status = "NO_ACTION_REQUIRED"
    plan.source_references = sorted(
        {p.reference for c in decisions.candidates for p in c.provenance}
        | {p.reference for r in decisions.routes for p in r.provenance}
    )
    if request.scenarios and plan.total_cost_usd is not None:
        assumptions = request.scenarios.model_dump()
        seed = assumptions.pop("seed")
        source = assumptions.pop("source")
        explanation = assumptions.pop("explanation")
        duration = max(
            (
                s.get("duration_hours", s.get("lead_time_days", 0) * 24)
                for c in plan.components.values()
                for s in c.selected
            ),
            default=0,
        )
        plan.scenario = MonteCarloSimulator(seed=seed).simulate(
            {"total_cost_usd": plan.total_cost_usd, "duration_hours": duration},
            **assumptions,
        )
        plan.scenario.update(
            {
                "evidence_type": "OPERATOR_SCENARIO_ASSUMPTIONS",
                "source": source,
                "explanation": explanation,
                "not_measured_uncertainty": True,
            }
        )
    else:
        plan.scenario = {"status": "ASSUMPTIONS_REQUIRED"}
    return plan
