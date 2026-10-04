from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class AgentResult(BaseModel):
    agent_id: str
    agent_type: str

    status: AgentStatus = AgentStatus.SUCCESS

    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    outputs: Dict[str, Any] = Field(default_factory=dict)

    confidence: float = 1.0

    evidence: List[Dict[str, Any]] = Field(
        default_factory=list
    )

    errors: List[str] = Field(
        default_factory=list
    )

    metadata: Dict[str, Any] = Field(
        default_factory=dict
    )

    latency_ms: Optional[float] = None
