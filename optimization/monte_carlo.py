from __future__ import annotations

import math
import random
from statistics import mean
from typing import Any


class MonteCarloSimulator:
    """
    Monte Carlo stress-testing layer for optimized supply-chain plans.

    The simulator does not change the optimization decision.

    It evaluates how the selected plan behaves under uncertainty in:

        - cost
        - transit time
        - disruption occurrence

    Results are statistical estimates, not deterministic guarantees.
    """

    simulator_id = "optimization.monte_carlo"
    version = "1.0.0"

    def __init__(
        self,
        *,
        seed: int | None = 42,
    ) -> None:

        self.seed = seed

    @staticmethod
    def _percentile(
        values: list[float],
        percentile: float,
    ) -> float:

        if not values:
            return 0.0

        values = sorted(values)

        if len(values) == 1:
            return values[0]

        position = (
            percentile / 100.0
        ) * (len(values) - 1)

        lower = math.floor(position)
        upper = math.ceil(position)

        if lower == upper:
            return values[lower]

        fraction = position - lower

        return (
            values[lower]
            + (
                values[upper]
                - values[lower]
            )
            * fraction
        )

    @staticmethod
    def _normal_positive(
        rng: random.Random,
        mean_value: float,
        std_fraction: float,
    ) -> float:

        if mean_value <= 0:
            return 0.0

        value = rng.gauss(
            mean_value,
            mean_value * std_fraction,
        )

        return max(
            0.0,
            value,
        )

    def simulate(
        self,
        plan: dict[str, Any],
        *,
        iterations: int = 5000,
        cost_std_fraction: float = 0.10,
        duration_std_fraction: float = 0.10,
        disruption_probability: float = 0.05,
        disruption_cost_multiplier: float = 1.50,
        disruption_time_multiplier: float = 1.75,
        confidence_level: float = 0.95,
    ) -> dict[str, Any]:

        if iterations <= 0:
            raise ValueError(
                "iterations must be greater than zero"
            )

        if not 0 <= disruption_probability <= 1:
            raise ValueError(
                "disruption_probability must be between 0 and 1"
            )

        if cost_std_fraction < 0:
            raise ValueError(
                "cost_std_fraction cannot be negative"
            )

        if duration_std_fraction < 0:
            raise ValueError(
                "duration_std_fraction cannot be negative"
            )

        if disruption_cost_multiplier < 1:
            raise ValueError(
                "disruption_cost_multiplier must be >= 1"
            )

        if disruption_time_multiplier < 1:
            raise ValueError(
                "disruption_time_multiplier must be >= 1"
            )

        if not 0 < confidence_level < 1:
            raise ValueError(
                "confidence_level must be between 0 and 1"
            )

        # ----------------------------------------------------------
        # EXTRACT BASE PLAN
        # ----------------------------------------------------------

        total_cost = float(
            plan.get(
                "total_cost_usd",
                plan.get(
                    "cost_usd",
                    0.0,
                ),
            )
        )

        duration = float(
            plan.get(
                "duration_hours",
                plan.get(
                    "transit_time_hours",
                    0.0,
                ),
            )
        )

        if total_cost < 0:
            raise ValueError(
                "Plan cost cannot be negative"
            )

        if duration < 0:
            raise ValueError(
                "Plan duration cannot be negative"
            )

        rng = random.Random(
            self.seed
        )

        simulated_costs: list[float] = []
        simulated_durations: list[float] = []

        disruption_count = 0

        # ----------------------------------------------------------
        # SIMULATION
        # ----------------------------------------------------------

        for _ in range(iterations):

            cost = self._normal_positive(
                rng,
                total_cost,
                cost_std_fraction,
            )

            transit_time = self._normal_positive(
                rng,
                duration,
                duration_std_fraction,
            )

            disrupted = (
                rng.random()
                < disruption_probability
            )

            if disrupted:

                disruption_count += 1

                cost *= (
                    disruption_cost_multiplier
                )

                transit_time *= (
                    disruption_time_multiplier
                )

            simulated_costs.append(
                cost
            )

            simulated_durations.append(
                transit_time
            )

        # ----------------------------------------------------------
        # STATISTICS
        # ----------------------------------------------------------

        p_low = (
            1.0
            - confidence_level
        ) / 2.0

        p_high = (
            1.0
            - p_low
        )

        p_low_percentile = (
            p_low * 100.0
        )

        p_high_percentile = (
            p_high * 100.0
        )

        expected_cost = mean(
            simulated_costs
        )

        expected_duration = mean(
            simulated_durations
        )

        disruption_rate = (
            disruption_count
            / iterations
        )

        return {
            "status": "SUCCESS",
            "simulator_id": self.simulator_id,
            "version": self.version,

            "iterations": iterations,
            "seed": self.seed,

            "base_plan": {
                "total_cost_usd": total_cost,
                "duration_hours": duration,
            },

            "statistics": {
                "cost_usd": {
                    "mean": round(
                        expected_cost,
                        2,
                    ),
                    "p50": round(
                        self._percentile(
                            simulated_costs,
                            50,
                        ),
                        2,
                    ),
                    "p90": round(
                        self._percentile(
                            simulated_costs,
                            90,
                        ),
                        2,
                    ),
                    "p95": round(
                        self._percentile(
                            simulated_costs,
                            95,
                        ),
                        2,
                    ),
                    "confidence_interval": {
                        "lower": round(
                            self._percentile(
                                simulated_costs,
                                p_low_percentile,
                            ),
                            2,
                        ),
                        "upper": round(
                            self._percentile(
                                simulated_costs,
                                p_high_percentile,
                            ),
                            2,
                        ),
                    },
                },

                "duration_hours": {
                    "mean": round(
                        expected_duration,
                        2,
                    ),
                    "p50": round(
                        self._percentile(
                            simulated_durations,
                            50,
                        ),
                        2,
                    ),
                    "p90": round(
                        self._percentile(
                            simulated_durations,
                            90,
                        ),
                        2,
                    ),
                    "p95": round(
                        self._percentile(
                            simulated_durations,
                            95,
                        ),
                        2,
                    ),
                    "confidence_interval": {
                        "lower": round(
                            self._percentile(
                                simulated_durations,
                                p_low_percentile,
                            ),
                            2,
                        ),
                        "upper": round(
                            self._percentile(
                                simulated_durations,
                                p_high_percentile,
                            ),
                            2,
                        ),
                    },
                },

                "disruption_probability": round(
                    disruption_rate,
                    6,
                ),
            },

            "risk_parameters": {
                "cost_std_fraction": cost_std_fraction,
                "duration_std_fraction": duration_std_fraction,
                "disruption_probability": disruption_probability,
                "disruption_cost_multiplier": (
                    disruption_cost_multiplier
                ),
                "disruption_time_multiplier": (
                    disruption_time_multiplier
                ),
            },
        }


monte_carlo_simulator = MonteCarloSimulator()


__all__ = [
    "MonteCarloSimulator",
    "monte_carlo_simulator",
]
