"""Compatibility exports of the single canonical contract. Legacy models are archived."""

from core.schemas.contracts import ApprovalRequest, ApprovalDecision

ApprovalResponse = ApprovalDecision

__all__ = ["ApprovalRequest", "ApprovalDecision", "ApprovalResponse"]
