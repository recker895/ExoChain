from __future__ import annotations

from typing import Any

from ortools.sat.python import cp_model


class ProcurementOptimizer:
    """
    Supplier/procurement optimization layer.

    Allocates required procurement quantity across suppliers while
    respecting supplier capacity, budget, lead time, and risk.

    Produces a procurement plan only.
    It does NOT create or submit purchase orders.
    """

    optimizer_id = "optimization.procurement"
    version = "1.0.0"

    def optimize(
        self,
        suppliers: list[dict[str, Any]],
        *,
        required_units: int,
        max_budget_usd: float | None = None,
        max_average_risk: float = 1.0,
        max_lead_time_days: float | None = None,
        objective_weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:

        if required_units <= 0:
            return {
                "status": "INVALID",
                "optimizer_id": self.optimizer_id,
                "version": self.version,
                "reason": "required_units must be greater than zero.",
                "allocation": [],
            }

        if not suppliers:
            return {
                "status": "INFEASIBLE",
                "optimizer_id": self.optimizer_id,
                "version": self.version,
                "reason": "No suppliers supplied.",
                "allocation": [],
            }

        weights = {
            "cost": 1.0,
            "risk": 1.0,
            "lead_time": 1.0,
        }

        if objective_weights:
            weights.update(objective_weights)

        normalized: list[dict[str, Any]] = []

        for index, supplier in enumerate(suppliers):

            try:
                capacity = int(
                    supplier.get(
                        "capacity_units",
                        0,
                    )
                )

                unit_cost = float(
                    supplier.get(
                        "unit_cost_usd",
                        0.0,
                    )
                )

                risk = float(
                    supplier.get(
                        "risk_score",
                        0.0,
                    )
                )

                lead_time = float(
                    supplier.get(
                        "lead_time_days",
                        0.0,
                    )
                )

                if capacity <= 0:
                    continue

                if unit_cost < 0:
                    continue

                if lead_time < 0:
                    continue

                normalized.append(
                    {
                        **supplier,
                        "supplier_id": supplier.get(
                            "supplier_id",
                            f"SUPPLIER-{index + 1}",
                        ),
                        "capacity_units": capacity,
                        "unit_cost_usd": unit_cost,
                        "risk_score": max(
                            0.0,
                            min(1.0, risk),
                        ),
                        "lead_time_days": lead_time,
                    }
                )

            except (
                TypeError,
                ValueError,
            ):
                continue

        if not normalized:
            return {
                "status": "INFEASIBLE",
                "optimizer_id": self.optimizer_id,
                "version": self.version,
                "reason": "No valid supplier records.",
                "allocation": [],
            }

        model = cp_model.CpModel()

        quantities = []

        for index, supplier in enumerate(
            normalized
        ):

            quantity = model.NewIntVar(
                0,
                supplier["capacity_units"],
                f"supplier_quantity_{index}",
            )

            quantities.append(quantity)

        # ----------------------------------------------------------
        # REQUIRED QUANTITY
        # ----------------------------------------------------------

        model.Add(
            sum(quantities)
            == required_units
        )

        # ----------------------------------------------------------
        # BUDGET
        # ----------------------------------------------------------

        if max_budget_usd is not None:

            model.Add(
                sum(
                    int(
                        supplier["unit_cost_usd"]
                        * 100
                    )
                    * quantity
                    for supplier, quantity
                    in zip(
                        normalized,
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
                        supplier["risk_score"]
                        * 10000
                    )
                    * quantity
                    for supplier, quantity
                    in zip(
                        normalized,
                        quantities,
                    )
                )
                <= risk_limit
                * sum(quantities)
            )

        # ----------------------------------------------------------
        # LEAD TIME
        # ----------------------------------------------------------
        #
        # Conservative aggregate service-time constraint.
        #

        if max_lead_time_days is not None:

            model.Add(
                sum(
                    int(
                        supplier["lead_time_days"]
                        * 100
                    )
                    * quantity
                    for supplier, quantity
                    in zip(
                        normalized,
                        quantities,
                    )
                )
                <= int(
                    max_lead_time_days
                    * required_units
                    * 100
                )
            )

        # ----------------------------------------------------------
        # OBJECTIVE
        # ----------------------------------------------------------

        objective_terms = []

        for supplier, quantity in zip(
            normalized,
            quantities,
        ):

            cost_component = (
                weights["cost"]
                * supplier["unit_cost_usd"]
            )

            risk_component = (
                weights["risk"]
                * supplier["risk_score"]
                * 100.0
            )

            lead_time_component = (
                weights["lead_time"]
                * supplier["lead_time_days"]
            )

            score = (
                cost_component
                + risk_component
                + lead_time_component
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
                    "No supplier allocation satisfies "
                    "the supplied constraints."
                ),
                "allocation": [],
            }

        allocation = []

        total_units = 0
        total_cost = 0.0
        weighted_risk = 0.0
        weighted_lead_time = 0.0

        for supplier, quantity in zip(
            normalized,
            quantities,
        ):

            units = int(
                solver.Value(quantity)
            )

            if units <= 0:
                continue

            cost = (
                units
                * supplier["unit_cost_usd"]
            )

            total_units += units
            total_cost += cost

            weighted_risk += (
                units
                * supplier["risk_score"]
            )

            weighted_lead_time += (
                units
                * supplier["lead_time_days"]
            )

            allocation.append(
                {
                    "supplier_id": supplier[
                        "supplier_id"
                    ],
                    "units": units,
                    "capacity_units": supplier[
                        "capacity_units"
                    ],
                    "unit_cost_usd": supplier[
                        "unit_cost_usd"
                    ],
                    "risk_score": supplier[
                        "risk_score"
                    ],
                    "lead_time_days": supplier[
                        "lead_time_days"
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

        average_lead_time = (
            weighted_lead_time / total_units
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
            "required_units": required_units,
            "allocated_units": total_units,
            "unmet_units": max(
                0,
                required_units - total_units,
            ),
            "total_cost_usd": round(
                total_cost,
                2,
            ),
            "average_risk": round(
                average_risk,
                6,
            ),
            "average_lead_time_days": round(
                average_lead_time,
                4,
            ),
            "allocation": allocation,
            "constraints": {
                "max_budget_usd": max_budget_usd,
                "max_average_risk": max_average_risk,
                "max_lead_time_days": max_lead_time_days,
            },
            "objective_weights": weights,
            "solver": {
                "name": "OR-Tools CP-SAT",
                "status": int(status),
                "wall_time_seconds": solver.WallTime(),
            },
        }


procurement_optimizer = ProcurementOptimizer()


__all__ = [
    "ProcurementOptimizer",
    "procurement_optimizer",
]
