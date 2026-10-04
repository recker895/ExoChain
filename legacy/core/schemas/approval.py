from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalRequest(BaseModel):
    approval_id: str
    decision_id: Optional[str] = None
    solution_id: Optional[str] = None

    created_at: datetime

    status: ApprovalStatus = ApprovalStatus.PENDING

    requested_by: str

    reason: str

    risk_level: Optional[str] = None
    risk_score: Optional[float] = None

    proposed_action: Dict[str, Any] = Field(default_factory=dict)

    expires_at: Optional[datetime] = None

    metadata: Dict[str, Any] = Field(default_factory=dict)


class ApprovalResponse(BaseModel):
    approval_id: str

    status: ApprovalStatus

    approved_by: Optional[str] = None
    responded_at: Optional[datetime] = None

    comments: Optional[str] = None

    conditions: List[str] = Field(default_factory=list)
