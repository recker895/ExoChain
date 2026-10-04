"""Directed maritime network search. Never derives navigability from a geodesic.

Search enumerates bounded loop-free paths in nonnegative additive score order.
The reported guarantee is selection optimality over the generated candidate set,
not a global hydrodynamic or time-dependent routing optimum.
"""

import hashlib
import heapq
import itertools
from datetime import timedelta

from core.schemas.contracts import NavigationNetwork, RouteCandidate, Shipment, utcnow


METRICS = {
    "cost": "cost_usd",
    "time": "duration_hours",
    "fuel": "fuel_litres",
    "risk": "risk_score",
    "weather": "weather_penalty",
    "current": "current_penalty",
}


def generate_routes(
    network: NavigationNetwork | None,
    shipment: Shipment,
    weights,
    limit=8,
    max_expansions=20000,
    demo_mode=False,
):
    if network is None:
        return [], ["ROUTE_NETWORK_UNAVAILABLE"], {"algorithm": None}
    now = utcnow()
    if network.valid_until < now or network.data_quality != "VALID":
        return [], ["ROUTE_NETWORK_STALE_OR_INVALID"], {"algorithm": None}
    nodes = {n.id for n in network.nodes}
    if shipment.origin_id not in nodes or shipment.destination_id not in nodes:
        return [], ["SHIPMENT_ENDPOINT_NOT_IN_NETWORK"], {"algorithm": None}
    if demo_mode and network.planning_classification == "GEOGRAPHIC_PLANNING_ONLY":
        candidates = []
        rejected = []
        for edge in network.edges:
            if (edge.origin_id, edge.destination_id) != (shipment.origin_id, shipment.destination_id):
                continue
            reason = None
            if not edge.open or edge.data_quality != "VALID" or edge.valid_until < now:
                reason = "EDGE_CLOSED_OR_STALE"
            elif shipment.departure_at + timedelta(hours=edge.duration_hours) > shipment.due_at:
                reason = "SHIPMENT_DUE_AT_EXCEEDED"
            elif shipment.draft_m is not None and edge.max_draft_m is not None and shipment.draft_m > edge.max_draft_m:
                reason = "DRAFT_RESTRICTION"
            elif shipment.max_speed_knots is not None and edge.max_speed_knots is not None and edge.distance_km / 1.852 / edge.duration_hours > min(shipment.max_speed_knots, edge.max_speed_knots):
                reason = "SPEED_RESTRICTION"
            elif edge.allowed_vessel_ids and shipment.vessel_id not in edge.allowed_vessel_ids:
                reason = "VESSEL_RESTRICTION"
            if reason:
                rejected.append({"edge_id": edge.id, "reason": reason})
                continue
            candidates.append(RouteCandidate(
                candidate_id=hashlib.sha256(
                    (shipment.id + network.id + network.version + edge.id).encode()
                ).hexdigest()[:24],
                shipment_id=shipment.id, network_id=network.id,
                edge_ids=[edge.id], geometry=edge.geometry,
                distance_km=edge.distance_km, duration_hours=edge.duration_hours,
                cost_usd=edge.cost_usd, fuel_litres=edge.fuel_litres,
                risk_score=edge.risk_score, weather_penalty=edge.weather_penalty,
                current_penalty=edge.current_penalty,
                provenance=edge.provenance, segments=[edge],
            ))
        return candidates[:limit], ([] if candidates else ["NO_USABLE_GEOGRAPHIC_ROUTE"]), {
            "algorithm": "ArcNautical geographic candidates; optional metrics remain unknown",
            "network_id": network.id, "rejected_edges": rejected,
        }
    if shipment.draft_m is None or shipment.max_speed_knots is None:
        return [], ["VESSEL_CONSTRAINTS_UNAVAILABLE"], {"algorithm": None}
    missing_metrics = [edge.id for edge in network.edges
                       if any(getattr(edge, field) is None for field in
                              ("cost_usd", "fuel_litres", "risk_score", "weather_penalty", "current_penalty", "max_draft_m", "max_speed_knots"))]
    if missing_metrics:
        return [], ["ROUTE_METRICS_DATA_REQUIRED"], {
            "algorithm": None, "network_id": network.id,
            "edges_missing_metrics": missing_metrics,
        }
    scales = {
        key: max(getattr(edge, attr) for edge in network.edges) or 1
        for key, attr in METRICS.items()
    }
    graph, rejected = {}, []
    for edge in network.edges:
        reason = None
        if not edge.open:
            reason = "EDGE_CLOSED"
        elif edge.data_quality != "VALID" or edge.valid_until < now:
            reason = "EDGE_STALE_OR_INVALID"
        elif shipment.draft_m > edge.max_draft_m:
            reason = "DRAFT_RESTRICTION"
        elif edge.distance_km / 1.852 / edge.duration_hours > min(
            edge.max_speed_knots, shipment.max_speed_knots
        ):
            reason = "SPEED_RESTRICTION"
        elif (
            edge.allowed_vessel_ids
            and shipment.vessel_id not in edge.allowed_vessel_ids
        ):
            reason = "VESSEL_RESTRICTION"
        if reason:
            rejected.append({"edge_id": edge.id, "reason": reason})
            continue
        graph.setdefault(edge.origin_id, []).append(edge)
    paths = {}
    expansions = 0
    # Include shortest distance/time/fuel/risk and environmental objectives as
    # well as the operator's composite objective; deduplicate identical paths.
    profiles = (
        [weights.model_dump()] + [{key: 1} for key in METRICS] + [{"distance": 1}]
    )
    counter = itertools.count()
    for profile in profiles:
        queue = [(0, next(counter), shipment.origin_id, (), (shipment.origin_id,), 0.0)]
        found = 0
        while queue and expansions < max_expansions and found < limit:
            score, _, node, path, visited, hours = heapq.heappop(queue)
            if node == shipment.destination_id:
                if path:
                    paths[tuple(e.id for e in path)] = path
                found += 1
                continue
            expansions += 1
            for edge in sorted(graph.get(node, []), key=lambda e: e.id):
                if edge.destination_id in visited:
                    continue
                arrival = shipment.departure_at + timedelta(
                    hours=hours + edge.duration_hours
                )
                if arrival > shipment.due_at or arrival > edge.valid_until:
                    continue
                edge_score = sum(
                    weight
                    * (
                        edge.distance_km
                        if key == "distance"
                        else getattr(edge, METRICS[key]) / scales[key]
                    )
                    for key, weight in profile.items()
                )
                heapq.heappush(
                    queue,
                    (
                        score + edge_score,
                        next(counter),
                        edge.destination_id,
                        path + (edge,),
                        visited + (edge.destination_id,),
                        hours + edge.duration_hours,
                    ),
                )
    candidates = []
    for ids, path in sorted(paths.items()):
        geometry = list(path[0].geometry)
        for edge in path[1:]:
            geometry.extend(edge.geometry[1:])
        distance = sum(e.distance_km for e in path)
        candidates.append(
            RouteCandidate(
                candidate_id=hashlib.sha256(
                    (shipment.id + network.id + network.version + "|".join(ids)).encode()
                ).hexdigest()[:24],
                shipment_id=shipment.id,
                network_id=network.id,
                edge_ids=list(ids),
                geometry=geometry,
                distance_km=distance,
                duration_hours=sum(e.duration_hours for e in path),
                cost_usd=sum(e.cost_usd for e in path),
                fuel_litres=sum(e.fuel_litres for e in path),
                risk_score=sum(e.risk_score * e.distance_km for e in path) / distance,
                weather_penalty=sum(e.weather_penalty for e in path),
                current_penalty=sum(e.current_penalty for e in path),
                provenance=[p for e in path for p in e.provenance],
                segments=list(path),
            )
        )
    return (
        candidates,
        [] if candidates else ["NO_FEASIBLE_NAVIGATIONAL_PATH"],
        {
            "algorithm": "Bounded multi-objective Dijkstra on supplied directed navigational graph",
            "network_id": network.id,
            "network_version": network.version,
            "expansions": expansions,
            "search_limit_reached": expansions >= max_expansions,
            "rejected_edges": rejected,
            "environment_method": "Source-supplied segment costs and penalties; no invented weather or fuel model.",
        },
    )


def score_routes(candidates, weights):
    scales = {
        key: max((getattr(c, attr) for c in candidates), default=0) or 1
        for key, attr in METRICS.items()
    }
    scored = []
    for c in candidates:
        components = {
            key: weight * getattr(c, METRICS[key]) / scales[key]
            for key, weight in weights.model_dump().items()
        }
        total = sum(components.values())
        scored.append(
            {
                **c.model_dump(mode="json"),
                "objective_value": total,
                "objective_components": components,
                "objective_percentages": {
                    k: 100 * v / total if total else 0 for k, v in components.items()
                },
                "normalization_scales": scales,
            }
        )
    return sorted(scored, key=lambda c: (c["objective_value"], c["candidate_id"]))


def score_demo_routes(candidates, weights):
    """Compare only observed attributes shared by every geographic candidate."""
    attributes = {"distance": "distance_km", "time": "duration_hours", **METRICS}
    attributes.pop("cost", None)  # Added below only when a commercial cost exists.
    enabled = {"distance": 1.0, "time": 1.0}
    preferences = weights.model_dump()
    for key in ("cost", "fuel", "risk", "weather", "current"):
        field = METRICS[key]
        if preferences[key] > 0 and all(getattr(c, field) is not None for c in candidates):
            enabled[key] = preferences[key]
    scales = {
        key: max(getattr(c, METRICS[key] if key in METRICS else attributes[key])
                 for c in candidates) or 1
        for key in enabled
    }
    scored = []
    for candidate in candidates:
        parts = {
            key: weight * getattr(candidate, METRICS[key] if key in METRICS else attributes[key]) / scales[key]
            for key, weight in enabled.items()
        }
        total = sum(parts.values())
        scored.append({
            **candidate.model_dump(mode="json"),
            "objective_value": total,
            "objective_components": parts,
            "objective_percentages": {key: 100 * part / total for key, part in parts.items()},
            "normalization_scales": scales,
            "used_metrics": list(enabled),
            "excluded_metrics": [key for key in ("cost", "fuel", "risk", "weather", "current") if key not in enabled],
        })
    return sorted(scored, key=lambda route: (route["objective_value"], route["candidate_id"]))
