"""Compatibility exports of the single canonical contract. Legacy models are archived."""

from core.schemas.contracts import (
    OptimizationRequest,
    OptimizationSolution,
    ComponentSolution,
)

__all__ = ["OptimizationRequest", "OptimizationSolution", "ComponentSolution"]
