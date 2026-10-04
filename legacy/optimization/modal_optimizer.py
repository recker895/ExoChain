from __future__ import annotations

from typing import Any

from ortools.sat.python import cp_model


class ModalOptimizer:
    """
    Multi-modal transport optimization.

    Selects quantities across available transport modes while
    satisfying demand and respecting capacity, budget, time,
    and risk constraints.

    This layer only produces an optimized plan.
    It does not execute transportation or ERP actions.
    """

    optimizer_id = "optimization.modal"
    version = "1.0.0"

    def _normalize_modes(
        self,
        modes: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:

        normalized = []

        for index, mode in enumerate(modes):

            try:
                capacity = float(
                    mode.get("capacity_units", 0)
                )

                cost = float(
                    mode.get("cost_per_unit_usd", 0)
                )

                duration = float(
                    mode.get("duration_hours", 0)
                )

                risk = float(
                    mode.get("risk_score", 0)
                )

                if capacity <= 0:
                    continue

                if cost < 0:
                    continue

                if duration <= 0:
                    continue

                normalized.append(
                    {
                        **mode,
                        "mode_id": mode.get(
                            "mode_id",
                            f"MODE-{index + 1}",
                        ),
                        "capacity_units": capacity,
                        "cost_per_unit_usd": cost,
                        "duration_hours": duration,
                        "risk_score": max(
                            0.0,
                            min(1.0, risk),
                        ),
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
        modes: list[dict[str, Any]],
        *,
        demand_units: int,
        max_budget_usd: float | None = None,
        max_duration_hours: float | None = None,
        max_average_risk: float = 1.0,
        objective_weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:

        if demand_units <= 0:
            return {
                "status": "INVALID",
                "optimizer_id": self.optimizer_id,
                "version": self.version,
                "reason": "demand_units must be greater than zero.",
            }

        modes = self._normalize_modes(modes)

        if not modes:
            return {
                "status": "INFEASIBLE",
                "optimizer_id": self.optimizer_id,
                "version": self.version,
                "reason": "No valid transport modes.",
                "allocation": [],
            }

        weights = {
            "cost": 1.0,
            "time": 1.0,
            "risk": 1.0,
        }

        if objective_weights:
            weights.update(
                objective_weights
            )

        model = cp_model.CpModel()

        quantities = []

        for index, mode in enumerate(modes):

            max_capacity = int(
                mode["capacity_units"]
            )

            variable = model.NewIntVar(
                0,
                max_capacity,
                f"units_{index}",
            )

            quantities.append(variable)

        # ----------------------------------------------------------
        # DEMAND
        # ----------------------------------------------------------

        model.Add(
            sum(quantities)
            == demand_units
        )

        # ----------------------------------------------------------
        # BUDGET
        # ----------------------------------------------------------

        if max_budget_usd is not None:

            model.Add(
                sum(
                    int(
                        mode["cost_per_unit_usd"]
                        * 100
                    )
                    * quantity
                    for mode, quantity
                    in zip(
                        modes,
                        quantities,
                    )
                )
                <= int(
                    max_budget_usd * 100
                )
            )

        # ----------------------------------------------------------
        # AVERAGE RISK
        # ----------------------------------------------------------

        if max_average_risk < 1.0:

            risk_limit = int(
                max_average_risk * 10000
            )

            model.Add(
                sum(
                    int(
                        mode["risk_score"]
                        * 10000
                    )
                    * quantity
                    for mode, quantity
                    in zip(
                        modes,
                        quantities,
                    )
                )
                <= risk_limit
                * sum(quantities)
            )

        # ----------------------------------------------------------
        # TIME
        # ----------------------------------------------------------
        #
        # This is a conservative aggregate service-time constraint.
        # Each mode contributes its duration to its allocated units.
        #

        if max_duration_hours is not None:

            model.Add(
                sum(
                    int(
                        mode["duration_hours"]
                        * 100
                    )
                    * quantity
                    for mode, quantity
                    in zip(
                        modes,
                        quantities,
                    )
                )
                <= int(
                    max_duration_hours
                    * demand_units
                    * 100
                )
            )

        # ----------------------------------------------------------
        # OBJECTIVE
        # ----------------------------------------------------------

        objective_terms = []

        for mode, quantity in zip(
            modes,
            quantities,
        ):

            cost_component = (
                weights["cost"]
                * mode["cost_per_unit_usd"]
            )

            time_component = (
                weights["time"]
                * mode["duration_hours"]
            )

            risk_component = (
                weights["risk"]
                * mode["risk_score"]
                * 100.0
            )

            score = (
                cost_component
                + time_component
                + risk_component
            )

            objective_terms.append(
                int(score * 100)
                * quantity
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
                "reason": (
                    "No modal allocation satisfies "
                    "the supplied constraints."
                ),
                "allocation": [],
            }

        allocation = []

        total_units = 0
        total_cost = 0.0
        weighted_risk = 0.0
        weighted_duration = 0.0

        for mode, quantity in zip(
            modes,
            quantities,
        ):

            units = int(
                solver.Value(quantity)
            )

            if units <= 0:
                continue

            cost = (
                units
                * mode["cost_per_unit_usd"]
            )

            total_units += units
            total_cost += cost

            weighted_risk += (
                units
                * mode["risk_score"]
            )

            weighted_duration += (
                units
                * mode["duration_hours"]
            )

            allocation.append(
                {
                    "mode_id": mode["mode_id"],
                    "units": units,
                    "capacity_units": mode[
                        "capacity_units"
                    ],
                    "cost_per_unit_usd": mode[
                        "cost_per_unit_usd"
                    ],
                    "duration_hours": mode[
                        "duration_hours"
                    ],
                    "risk_score": mode[
                        "risk_score"
                    ],
                    "total_cost_usd": round(
                        cost,
                        2,
                    ),
                }
            )

        average_risk = (
            weighted_risk / total_units
            if total_units
            else 0.0
        )

        average_duration = (
            weighted_duration / total_units
            if total_units
            else 0.0
        )

        return {
            "status": (
                "OPTIMAL"
                if status == cp_model.OPTIMAL
                else "FEASIBLE"
            ),
            "optimizer_id": self.optimizer_id,
            "version": self.version,
            "demand_units": demand_units,
            "allocated_units": total_units,
            "unmet_demand_units": max(
                0,
                demand_units - total_units,
            ),
            "total_cost_usd": round(
                total_cost,
                2,
            ),
            "average_risk": round(
                average_risk,
                6,
            ),
            "average_duration_hours": round(
                average_duration,
                4,
            ),
            "allocation": allocation,
            "constraints": {
                "max_budget_usd": max_budget_usd,
                "max_duration_hours": max_duration_hours,
                "max_average_risk": max_average_risk,
            },
            "objective_weights": weights,
            "solver": {
                "name": "OR-Tools CP-SAT",
                "status": int(status),
                "wall_time_seconds": solver.WallTime(),
            },
        }


modal_optimizer = ModalOptimizer()


__all__ = [
    "ModalOptimizer",
    "modal_optimizer",
]
