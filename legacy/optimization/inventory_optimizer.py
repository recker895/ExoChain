from __future__ import annotations

from typing import Any

from ortools.sat.python import cp_model


class InventoryOptimizer:
    """
    Inventory optimization layer.

    Determines replenishment quantities for SKUs while balancing:

        - expected demand
        - safety stock
        - warehouse capacity
        - ordering cost
        - holding cost
        - shortage penalty
        - procurement budget

    The optimizer produces a plan only.
    It does not place purchase orders.
    """

    optimizer_id = "optimization.inventory"
    version = "1.0.0"

    def optimize(
        self,
        items: list[dict[str, Any]],
        *,
        max_budget_usd: float | None = None,
        warehouse_capacity_units: int | None = None,
        objective_weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:

        if not items:
            return {
                "status": "INFEASIBLE",
                "optimizer_id": self.optimizer_id,
                "version": self.version,
                "reason": "No inventory items supplied.",
                "plan": [],
            }

        weights = {
            "ordering_cost": 1.0,
            "holding_cost": 1.0,
            "shortage_penalty": 10.0,
        }

        if objective_weights:
            weights.update(objective_weights)

        normalized: list[dict[str, Any]] = []

        for index, item in enumerate(items):

            try:
                demand = int(
                    item.get(
                        "expected_demand_units",
                        0,
                    )
                )

                current = int(
                    item.get(
                        "current_inventory_units",
                        0,
                    )
                )

                safety = int(
                    item.get(
                        "safety_stock_units",
                        0,
                    )
                )

                max_stock = int(
                    item.get(
                        "max_stock_units",
                        max(
                            demand + safety,
                            current + demand,
                        ),
                    )
                )

                unit_cost = float(
                    item.get(
                        "unit_cost_usd",
                        0.0,
                    )
                )

                holding_cost = float(
                    item.get(
                        "holding_cost_per_unit_usd",
                        0.0,
                    )
                )

                shortage_penalty = float(
                    item.get(
                        "shortage_penalty_usd",
                        unit_cost * 5.0,
                    )
                )

                if demand < 0:
                    continue

                if current < 0:
                    continue

                if safety < 0:
                    continue

                if max_stock < current:
                    max_stock = current

                if unit_cost < 0:
                    continue

                normalized.append(
                    {
                        **item,
                        "item_id": item.get(
                            "item_id",
                            f"SKU-{index + 1}",
                        ),
                        "expected_demand_units": demand,
                        "current_inventory_units": current,
                        "safety_stock_units": safety,
                        "max_stock_units": max_stock,
                        "unit_cost_usd": unit_cost,
                        "holding_cost_per_unit_usd": holding_cost,
                        "shortage_penalty_usd": shortage_penalty,
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
                "reason": "No valid inventory items.",
                "plan": [],
            }

        model = cp_model.CpModel()

        order_vars = []
        shortage_vars = []
        ending_inventory_vars = []

        for index, item in enumerate(normalized):

            max_order = max(
                0,
                item["max_stock_units"]
                - item["current_inventory_units"],
            )

            order = model.NewIntVar(
                0,
                max_order,
                f"order_{index}",
            )

            shortage = model.NewIntVar(
                0,
                item["expected_demand_units"],
                f"shortage_{index}",
            )

            ending = model.NewIntVar(
                0,
                item["max_stock_units"],
                f"ending_inventory_{index}",
            )

            order_vars.append(order)
            shortage_vars.append(shortage)
            ending_inventory_vars.append(ending)

            # ------------------------------------------------------
            # INVENTORY BALANCE
            # ------------------------------------------------------
            #
            # current + order - demand + shortage = ending
            #
            model.Add(
                item["current_inventory_units"]
                + order
                - item["expected_demand_units"]
                + shortage
                == ending
            )

            # ------------------------------------------------------
            # SAFETY STOCK
            # ------------------------------------------------------
            #
            # If demand can be covered, maintain safety stock.
            #
            model.Add(
                ending
                >= item["safety_stock_units"]
            )

        # ----------------------------------------------------------
        # WAREHOUSE CAPACITY
        # ----------------------------------------------------------

        if warehouse_capacity_units is not None:

            model.Add(
                sum(
                    item[
                        "current_inventory_units"
                    ]
                    + order
                    for item, order in zip(
                        normalized,
                        order_vars,
                    )
                )
                <= warehouse_capacity_units
            )

        # ----------------------------------------------------------
        # PROCUREMENT BUDGET
        # ----------------------------------------------------------

        if max_budget_usd is not None:

            model.Add(
                sum(
                    int(
                        item["unit_cost_usd"]
                        * 100
                    )
                    * order
                    for item, order in zip(
                        normalized,
                        order_vars,
                    )
                )
                <= int(
                    max_budget_usd * 100
                )
            )

        # ----------------------------------------------------------
        # OBJECTIVE
        # ----------------------------------------------------------

        # Rebuild objective using linear CP-SAT expressions.
        objective_expression = []

        for (
            item,
            order,
            shortage,
            ending,
        ) in zip(
            normalized,
            order_vars,
            shortage_vars,
            ending_inventory_vars,
        ):

            ordering_cost = int(
                weights["ordering_cost"]
                * item["unit_cost_usd"]
                * 100
            )

            holding_cost = int(
                weights["holding_cost"]
                * item[
                    "holding_cost_per_unit_usd"
                ]
                * 100
            )

            shortage_cost = int(
                weights["shortage_penalty"]
                * item[
                    "shortage_penalty_usd"
                ]
                * 100
            )

            objective_expression.extend(
                [
                    ordering_cost * order,
                    holding_cost * ending,
                    shortage_cost * shortage,
                ]
            )

        model.Minimize(
            sum(objective_expression)
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
                    "No inventory plan satisfies "
                    "the supplied constraints."
                ),
                "plan": [],
            }

        plan = []

        total_ordered = 0
        total_shortage = 0
        total_procurement_cost = 0.0
        total_holding_cost = 0.0
        total_shortage_cost = 0.0

        for (
            item,
            order,
            shortage,
            ending,
        ) in zip(
            normalized,
            order_vars,
            shortage_vars,
            ending_inventory_vars,
        ):

            order_units = int(
                solver.Value(order)
            )

            shortage_units = int(
                solver.Value(shortage)
            )

            ending_units = int(
                solver.Value(ending)
            )

            procurement_cost = (
                order_units
                * item["unit_cost_usd"]
            )

            holding_cost = (
                ending_units
                * item[
                    "holding_cost_per_unit_usd"
                ]
            )

            shortage_cost = (
                shortage_units
                * item[
                    "shortage_penalty_usd"
                ]
            )

            total_ordered += order_units
            total_shortage += shortage_units

            total_procurement_cost += (
                procurement_cost
            )

            total_holding_cost += (
                holding_cost
            )

            total_shortage_cost += (
                shortage_cost
            )

            plan.append(
                {
                    "item_id": item["item_id"],
                    "current_inventory_units": item[
                        "current_inventory_units"
                    ],
                    "expected_demand_units": item[
                        "expected_demand_units"
                    ],
                    "safety_stock_units": item[
                        "safety_stock_units"
                    ],
                    "order_units": order_units,
                    "ending_inventory_units": ending_units,
                    "shortage_units": shortage_units,
                    "procurement_cost_usd": round(
                        procurement_cost,
                        2,
                    ),
                    "holding_cost_usd": round(
                        holding_cost,
                        2,
                    ),
                    "shortage_cost_usd": round(
                        shortage_cost,
                        2,
                    ),
                }
            )

        return {
            "status": (
                "OPTIMAL"
                if status == cp_model.OPTIMAL
                else "FEASIBLE"
            ),
            "optimizer_id": self.optimizer_id,
            "version": self.version,
            "plan": plan,
            "summary": {
                "total_ordered_units": total_ordered,
                "total_shortage_units": total_shortage,
                "procurement_cost_usd": round(
                    total_procurement_cost,
                    2,
                ),
                "holding_cost_usd": round(
                    total_holding_cost,
                    2,
                ),
                "shortage_cost_usd": round(
                    total_shortage_cost,
                    2,
                ),
                "total_cost_usd": round(
                    total_procurement_cost
                    + total_holding_cost
                    + total_shortage_cost,
                    2,
                ),
            },
            "constraints": {
                "max_budget_usd": max_budget_usd,
                "warehouse_capacity_units": (
                    warehouse_capacity_units
                ),
            },
            "objective_weights": weights,
            "solver": {
                "name": "OR-Tools CP-SAT",
                "status": int(status),
                "wall_time_seconds": solver.WallTime(),
            },
        }


inventory_optimizer = InventoryOptimizer()


__all__ = [
    "InventoryOptimizer",
    "inventory_optimizer",
]
