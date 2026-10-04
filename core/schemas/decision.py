"""Compatibility exports of the single canonical contract. Legacy models are archived."""

from core.schemas.contracts import DecisionCandidate

DecisionProposal = DecisionCandidate

__all__ = ["DecisionCandidate", "DecisionProposal"]
