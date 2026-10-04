from __future__ import annotations

import math
from typing import Any

from ortools.sat.python import cp_model


class RouteOptimizer:
    """
    Deterministic maritime route optimization layer.

    Agents propose candidate routing actions.
    This optimizer selects the feasible route using explicit
    operational, cost, time, and risk constraints.

    The optimizer does not execute the resulting plan.
    """

    optimizer_id = "optimization.route"
    version = "1.0.0"

    def _distance_km(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:

        radius_km = 6371.0

        p1 = math.radians(lat1)
        p2 = math.radians(lat2)

        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)

        a = (
            math.sin(dp / 2) ** 2
            + math.cos(p1)
            * math.cos(p2)
            * math.sin(dl / 2) ** 2
        )

        return (
            2
            * radius_km
            * math.atan2(
                math.sqrt(a),
                math.sqrt(1 - a),
            )
        )

    def _normalize_candidates(
        self,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:

        normalized = []

        for index, candidate in enumerate(candidates):

            try:
                distance = float(
                    candidate.get(
                        "distance_km",
                        0.0,
                    )
                )

                duration = float(
                    candidate.get(
                        "duration_hours",
                        0.0,
                    )
                )

                cost = float(
                    candidate.get(
                        "cost_usd",
                        0.0,
                    )
                )

                risk = float(
                    candidate.get(
                        "risk_score",
                        0.0,
                    )
                )

                fuel = float(
                    candidate.get(
                        "fuel_litres",
                        0.0,
                    )
                )

                if distance <= 0:
                    continue

                if duration <= 0:
                    continue

                if cost < 0:
                    continue

                risk = max(
                    0.0,
                    min(1.0, risk),
                )

                normalized.append(
                    {
                        **candidate,
                        "candidate_id": candidate.get(
                            "candidate_id",
                            f"ROUTE-{index + 1}",
                        ),
                        "distance_km": distance,
                        "duration_hours": duration,
                        "cost_usd": cost,
                        "risk_score": risk,
                        "fuel_litres": fuel,
                    }
                )

            except (
                TypeError,
                ValueError,
            ):
                continue

        return normalized

    def optimize(
        self,
        candidates: list[dict[str, Any]],
        *,
        max_cost_usd: float | None = None,
        max_duration_hours: float | None = None,
        max_risk_score: float = 1.0,
        objective_weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:

        candidates = self._normalize_candidates(
            candidates
        )

        if not candidates:
            return {
                "status": "INFEASIBLE",
                "optimizer_id": self.optimizer_id,
                "version": self.version,
                "reason": "No valid route candidates.",
                "selected_route": None,
                "candidate_count": 0,
            }

        weights = {
            "cost": 1.0,
            "time": 1.0,
            "risk": 1.0,
            "fuel": 0.25,
        }

        if objective_weights:
            weights.update(
                objective_weights
            )

        model = cp_model.CpModel()

        variables = [
            model.NewBoolVar(
                f"route_{index}"
            )
            for index in range(
                len(candidates)
            )
        ]

        # Exactly one route must be selected.
        model.Add(
            sum(variables) == 1
        )

        # ----------------------------------------------------------
        # CONSTRAINTS
        # ----------------------------------------------------------

        if max_cost_usd is not None:
            model.Add(
                sum(
                    int(
                        candidate["cost_usd"]
                        * 100
                    )
                    * variable
                    for candidate, variable
                    in zip(
                        candidates,
                        variables,
                    )
                )
                <= int(
                    max_cost_usd * 100
                )
            )

        if max_duration_hours is not None:
            model.Add(
                sum(
                    int(
                        candidate[
                            "duration_hours"
                        ]
                        * 100
                    )
                    * variable
                    for candidate, variable
                    in zip(
                        candidates,
                        variables,
                    )
                )
                <= int(
                    max_duration_hours * 100
                )
            )

        model.Add(
            sum(
                int(
                    candidate["risk_score"]
                    * 10000
                )
                * variable
                for candidate, variable
                in zip(
                    candidates,
                    variables,
                )
            )
            <= int(
                max_risk_score * 10000
            )
        )

        # ----------------------------------------------------------
        # OBJECTIVE
        # ----------------------------------------------------------

        objective_terms = []

        for candidate, variable in zip(
            candidates,
            variables,
        ):

            score = (
                weights["cost"]
                * candidate["cost_usd"]
                + weights["time"]
                * candidate["duration_hours"]
                * 100.0
                + weights["risk"]
                * candidate["risk_score"]
                * 10000.0
                + weights["fuel"]
                * candidate["fuel_litres"]
                / 100.0
            )

            objective_terms.append(
                int(score * 100)
                * variable
            )

        model.Minimize(
            sum(objective_terms)
        )

        solver = cp_model.CpSolver()

        solver.parameters.max_time_in_seconds = 10.0

        status = solver.Solve(model)

        if status not in (
            cp_model.OPTIMAL,
            cp_model.FEASIBLE,
        ):

            return {
                "status": "INFEASIBLE",
                "optimizer_id": self.optimizer_id,
                "version": self.version,
                "reason": "No route satisfies the supplied constraints.",
                "selected_route": None,
                "candidate_count": len(
                    candidates
                ),
            }

        selected_index = next(
            index
            for index, variable in enumerate(
                variables
            )
            if solver.Value(variable) == 1
        )

        selected = candidates[
            selected_index
        ]

        return {
            "status": (
                "OPTIMAL"
                if status == cp_model.OPTIMAL
                else "FEASIBLE"
            ),
            "optimizer_id": self.optimizer_id,
            "version": self.version,
            "candidate_count": len(
                candidates
            ),
            "selected_route": selected,
            "objective": {
                "cost_weight": weights["cost"],
                "time_weight": weights["time"],
                "risk_weight": weights["risk"],
                "fuel_weight": weights["fuel"],
            },
            "constraints": {
                "max_cost_usd": max_cost_usd,
                "max_duration_hours": max_duration_hours,
                "max_risk_score": max_risk_score,
            },
            "solver": {
                "name": "OR-Tools CP-SAT",
                "status": int(status),
                "wall_time_seconds": solver.WallTime(),
            },
        }


route_optimizer = RouteOptimizer()


__all__ = [
    "RouteOptimizer",
    "route_optimizer",
]
