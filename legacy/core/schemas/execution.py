from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionCommand(BaseModel):
    execution_id: str

    solution_id: Optional[str] = None
    decision_id: Optional[str] = None

    command_type: str

    created_at: datetime

    target_system: str

    action: Dict[str, Any] = Field(default_factory=dict)

    requires_approval: bool = True
    approval_id: Optional[str] = None

    idempotency_key: str

    metadata: Dict[str, Any] = Field(default_factory=dict)


class ExecutionResult(BaseModel):
    execution_id: str

    status: ExecutionStatus

    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    target_system: str

    external_reference: Optional[str] = None

    response: Dict[str, Any] = Field(default_factory=dict)

    error: Optional[str] = None

    metadata: Dict[str, Any] = Field(default_factory=dict)
