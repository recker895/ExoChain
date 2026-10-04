from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any

from agents.base import BaseAgent, AgentContext
from legacy.core.schemas.intelligence import (
    IntelligenceResult,
    IntelligenceType,
    RiskAssessment,
    RiskLevel,
)
from core.state.state_store import StateStore


class RiskIntelligenceAgent(BaseAgent[dict[str, Any]]):
    """
    Production risk-intelligence agent.

    Uses canonical Redis vessel state and derives explainable risk
    components from observable vessel/environmental features.

    Important:
    - no fabricated telemetry
    - no arbitrary global risk score
    - missing data lowers confidence rather than being treated as safe
    - every vessel receives explicit risk assessments
    """

    agent_id = "intelligence.risk"
    agent_name = "Risk Intelligence Agent"
    agent_type = "INTELLIGENCE"
    version = "1.0.0"

    dependencies = (
        "Redis:StateStore",
        "AIS",
        "OceanCurrent",
        "ST-GNN",
        "Weather",
        "PortState",
        "DisruptionEvents",
    )

    def __init__(self, state_store: StateStore | None = None) -> None:
        super().__init__()
        self.state_store = state_store or StateStore()

    def validate_input(self, context: AgentContext) -> None:
        if context is None:
            raise ValueError("AgentContext is required")

    def run(self, context: AgentContext) -> dict[str, Any]:
        vessels = self.state_store.get_active_vessels()

        if not vessels:
            return {
                "status": "UNAVAILABLE",
                "data_quality": "UNAVAILABLE",
                "agent_id": self.agent_id,
                "vessel_count": 0,
                "results": [],
                "reason": "No active vessel state available in Redis",
            }

        results: list[dict[str, Any]] = []

        for vessel in vessels:
            result = self._assess_vessel(vessel, context)
            results.append(result)

        valid_count = sum(
            1
            for result in results
            if result["data_quality"] == "VALID"
        )

        partial_count = sum(
            1
            for result in results
            if result["data_quality"] == "PARTIAL"
        )

        overall_quality = (
            "VALID"
            if valid_count == len(results)
            else "PARTIAL"
            if valid_count + partial_count > 0
            else "UNKNOWN"
        )

        return {
            "status": "SUCCESS",
            "data_quality": overall_quality,
            "agent_id": self.agent_id,
            "vessel_count": len(results),
            "valid_count": valid_count,
            "partial_count": partial_count,
            "results": results,
        }

    def _assess_vessel(
        self,
        vessel: Any,
        context: AgentContext,
    ) -> dict[str, Any]:

        now = datetime.now(timezone.utc)

        position = vessel.position

        speed = float(
            vessel.current_speed_knots
            if vessel.current_speed_knots is not None
            else position.speed_knots or 0.0
        )

        course = (
            vessel.position.course_over_ground
            if vessel.position.course_over_ground is not None
            else vessel.position.heading
        )

        weather = vessel.weather or {}
        ocean = vessel.ocean or {}
        waves = vessel.waves or {}

        factors: list[str] = []
        component_scores: dict[str, float] = {}
        missing_inputs: list[str] = []

        # --------------------------------------------------------------
        # 1. DATA QUALITY
        # --------------------------------------------------------------

        if vessel.data_quality < 0.75:
            factors.append(
                "AIS/state data quality below preferred threshold"
            )

        if vessel.data_quality < 0.5:
            missing_inputs.append("vessel_data_quality")

        # --------------------------------------------------------------
        # 2. SPEED / KINEMATIC RISK
        # --------------------------------------------------------------

        speed_risk = self._speed_risk(
            speed=speed,
            vessel=vessel,
        )

        component_scores["operational"] = speed_risk

        if speed_risk > 0.7:
            factors.append(
                f"elevated vessel speed: {speed:.2f} knots"
            )

        # --------------------------------------------------------------
        # 3. COURSE / POSITION QUALITY
        # --------------------------------------------------------------

        if course is None:
            missing_inputs.append("course_over_ground")
            route_risk = 0.35
        else:
            route_risk = 0.0

            if course < 0.0 or course > 360.0:
                route_risk = 0.8
                factors.append("invalid course value")
            else:
                route_risk = 0.05

        component_scores["route_deviation"] = route_risk

        # --------------------------------------------------------------
        # 4. WEATHER
        # --------------------------------------------------------------

        weather_risk, weather_factors = self._weather_risk(weather)

        component_scores["weather"] = weather_risk
        factors.extend(weather_factors)

        if not weather:
            missing_inputs.append("weather")

        # --------------------------------------------------------------
        # 5. WAVES
        # --------------------------------------------------------------

        wave_risk, wave_factors = self._wave_risk(waves)

        component_scores["operational_wave"] = wave_risk
        factors.extend(wave_factors)

        if not waves:
            missing_inputs.append("waves")

        # --------------------------------------------------------------
        # 6. OCEAN CURRENT
        # --------------------------------------------------------------

        current_risk, current_factors = self._current_risk(ocean)

        component_scores["current"] = current_risk
        factors.extend(current_factors)

        if not ocean:
            missing_inputs.append("ocean_current")

        # --------------------------------------------------------------
        # 7. CONTEXT FROM OTHER INTELLIGENCE AGENTS
        # --------------------------------------------------------------

        disruption_data = context.get("disruptions", [])

        if disruption_data is None:
            disruption_data = []

        disruption_risk = self._disruption_risk(
            disruption_data,
            vessel.mmsi,
        )

        component_scores["supply_disruption"] = disruption_risk

        if disruption_risk > 0.0:
            factors.append(
                "relevant disruption intelligence detected"
            )

        # --------------------------------------------------------------
        # 8. COMPOSITE RISK
        # --------------------------------------------------------------

        weights = {
            "operational": 0.18,
            "route_deviation": 0.12,
            "weather": 0.22,
            "operational_wave": 0.16,
            "current": 0.12,
            "supply_disruption": 0.20,
        }

        weighted_score = sum(
            component_scores[name] * weight
            for name, weight in weights.items()
        )

        weighted_score = max(
            0.0,
            min(1.0, weighted_score),
        )

        # Missing environmental data must not silently imply safety.
        completeness = 1.0 - min(
            0.75,
            len(missing_inputs) * 0.12,
        )

        confidence = max(
            0.05,
            min(
                1.0,
                completeness * max(0.5, vessel.data_quality),
            ),
        )

        level = self._risk_level(weighted_score)

        assessment = RiskAssessment(
            risk_type="maritime_operational_risk",
            probability=weighted_score,
            impact=self._impact_score(vessel, weighted_score),
            level=level,
            factors=factors,
        )

        result = IntelligenceResult(
            result_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            intelligence_type=IntelligenceType.RISK,
            timestamp=now,
            confidence=confidence,
            prediction=weighted_score,
            risk=assessment,
            metrics={
                "composite_risk": weighted_score,
                "operational_risk": component_scores["operational"],
                "route_risk": component_scores["route_deviation"],
                "weather_risk": component_scores["weather"],
                "wave_risk": component_scores["operational_wave"],
                "current_risk": component_scores["current"],
                "disruption_risk": component_scores["supply_disruption"],
            },
            features={
                "mmsi": vessel.mmsi,
                "vessel_name": vessel.vessel_name,
                "latitude": position.latitude,
                "longitude": position.longitude,
                "speed_knots": speed,
                "course_degrees": course,
                "data_quality": vessel.data_quality,
                "missing_inputs": missing_inputs,
                "component_scores": component_scores,
            },
            explanation=self._build_explanation(
                weighted_score,
                level,
                factors,
                missing_inputs,
            ),
            model_name="ExoChainRiskEngine",
            model_version=self.version,
        )

        self.state_store.save_intelligence(
            self.agent_id,
            str(vessel.mmsi),
            result,
        )

        return {
            "mmsi": vessel.mmsi,
            "vessel_name": vessel.vessel_name,
            "data_quality": (
                "VALID"
                if not missing_inputs
                else "PARTIAL"
            ),
            "confidence": confidence,
            "intelligence": result.model_dump(),
        }

    @staticmethod
    def _speed_risk(
        speed: float,
        vessel: Any,
    ) -> float:

        if speed < 0:
            return 1.0

        characteristics = vessel.characteristics

        max_speed = getattr(
            characteristics,
            "max_speed_knots",
            None,
        )

        cruising_speed = getattr(
            characteristics,
            "cruising_speed_knots",
            None,
        )

        if max_speed and max_speed > 0:
            utilization = speed / max_speed

            if utilization >= 0.95:
                return min(1.0, 0.9 + (utilization - 0.95) * 2.0)

            if utilization >= 0.85:
                return 0.65

            return min(0.45, utilization * 0.45)

        if cruising_speed and cruising_speed > 0:
            utilization = speed / cruising_speed

            if utilization >= 1.25:
                return 0.85

            if utilization >= 1.10:
                return 0.65

            return min(0.4, utilization * 0.25)

        # Without vessel-specific performance limits we cannot assert
        # that a particular speed is unsafe.
        return 0.1

    @staticmethod
    def _weather_risk(
        weather: dict[str, Any],
    ) -> tuple[float, list[str]]:

        if not weather:
            return 0.0, []

        score = 0.0
        factors: list[str] = []

        wind = RiskIntelligenceAgent._first_numeric(
            weather,
            (
                "wind_speed_knots",
                "wind_speed",
                "wind_speed_ms",
            ),
        )

        if wind is not None:
            if "wind_speed_ms" in weather:
                wind_knots = wind * 1.94384
            else:
                wind_knots = wind

            if wind_knots >= 40:
                score = max(score, 0.9)
                factors.append(
                    f"high wind speed: {wind_knots:.1f} knots"
                )
            elif wind_knots >= 30:
                score = max(score, 0.65)
                factors.append(
                    f"elevated wind speed: {wind_knots:.1f} knots"
                )
            elif wind_knots >= 20:
                score = max(score, 0.35)

        visibility = RiskIntelligenceAgent._first_numeric(
            weather,
            ("visibility_km", "visibility"),
        )

        if visibility is not None:
            if visibility < 1:
                score = max(score, 0.85)
                factors.append(
                    f"very low visibility: {visibility:.2f} km"
                )
            elif visibility < 3:
                score = max(score, 0.55)

        return min(score, 1.0), factors

    @staticmethod
    def _wave_risk(
        waves: dict[str, Any],
    ) -> tuple[float, list[str]]:

        if not waves:
            return 0.0, []

        height = RiskIntelligenceAgent._first_numeric(
            waves,
            (
                "wave_height_m",
                "significant_wave_height_m",
                "height_m",
            ),
        )

        if height is None:
            return 0.0, []

        factors: list[str] = []

        if height >= 6:
            factors.append(
                f"very high significant wave height: {height:.2f} m"
            )
            return 0.95, factors

        if height >= 4:
            factors.append(
                f"high significant wave height: {height:.2f} m"
            )
            return 0.75, factors

        if height >= 2.5:
            factors.append(
                f"elevated wave height: {height:.2f} m"
            )
            return 0.45, factors

        return min(height / 6.0, 0.25), factors

    @staticmethod
    def _current_risk(
        ocean: dict[str, Any],
    ) -> tuple[float, list[str]]:

        if not ocean:
            return 0.0, []

        speed = RiskIntelligenceAgent._first_numeric(
            ocean,
            (
                "speed_knots",
                "current_speed_knots",
            ),
        )

        if speed is None:
            u = RiskIntelligenceAgent._first_numeric(
                ocean,
                ("u_ms", "uo"),
            )

            v = RiskIntelligenceAgent._first_numeric(
                ocean,
                ("v_ms", "vo"),
            )

            if u is not None and v is not None:
                speed = math.sqrt(u * u + v * v) * 1.94384

        if speed is None:
            return 0.0, []

        factors: list[str] = []

        if speed >= 2.0:
            factors.append(
                f"strong ocean current: {speed:.2f} knots"
            )
            return 0.85, factors

        if speed >= 1.0:
            factors.append(
                f"moderate ocean current: {speed:.2f} knots"
            )
            return 0.45, factors

        return min(speed / 4.0, 0.25), factors

    @staticmethod
    def _disruption_risk(
        disruptions: Any,
        mmsi: str,
    ) -> float:

        if not isinstance(disruptions, list):
            return 0.0

        highest = 0.0

        for event in disruptions:
            if not isinstance(event, dict):
                continue

            affected = event.get("affected_entities", [])

            if affected and mmsi not in affected:
                continue

            severity = event.get("severity")

            if isinstance(severity, str):
                mapping = {
                    "low": 0.2,
                    "medium": 0.5,
                    "high": 0.75,
                    "critical": 1.0,
                }
                severity = mapping.get(
                    severity.lower(),
                    0.0,
                )

            if isinstance(severity, (int, float)):
                highest = max(
                    highest,
                    min(1.0, float(severity)),
                )

        return highest

    @staticmethod
    def _impact_score(
        vessel: Any,
        probability: float,
    ) -> float:

        capacity = getattr(
            vessel.characteristics,
            "cargo_capacity_tonnes",
            None,
        )

        if capacity is None:
            return probability

        # This is an impact proxy, not a probability.
        # It only increases when cargo exposure is known.
        exposure = min(
            1.0,
            math.log10(max(capacity, 1.0) + 1.0) / 7.0,
        )

        return min(
            1.0,
            0.65 * probability + 0.35 * exposure,
        )

    @staticmethod
    def _risk_level(score: float) -> RiskLevel:
        if score >= 0.8:
            return RiskLevel.CRITICAL

        if score >= 0.6:
            return RiskLevel.HIGH

        if score >= 0.3:
            return RiskLevel.MEDIUM

        return RiskLevel.LOW

    @staticmethod
    def _first_numeric(
        values: dict[str, Any],
        keys: tuple[str, ...],
    ) -> float | None:

        for key in keys:
            value = values.get(key)

            if isinstance(value, (int, float)):
                return float(value)

        return None

    @staticmethod
    def _build_explanation(
        score: float,
        level: RiskLevel,
        factors: list[str],
        missing_inputs: list[str],
    ) -> str:

        parts = [
            f"Composite maritime risk is {score:.3f} "
            f"({level.value})."
        ]

        if factors:
            parts.append(
                "Observed factors: " +
                "; ".join(factors[:8]) +
                "."
            )

        if missing_inputs:
            parts.append(
                "Confidence is limited because the following "
                "inputs were unavailable: " +
                ", ".join(missing_inputs) +
                "."
            )

        return " ".join(parts)


__all__ = ["RiskIntelligenceAgent"]
