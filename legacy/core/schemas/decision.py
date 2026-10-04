from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class DecisionType(str, Enum):
    ROUTE = "route"
    MODAL_SPLIT = "modal_split"
    INVENTORY = "inventory"
    PROCUREMENT = "procurement"
    PORT_SELECTION = "port_selection"
    CONTINGENCY = "contingency"


class DecisionPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class DecisionProposal(BaseModel):
    decision_id: str
    agent_id: str
    decision_type: DecisionType

    timestamp: datetime

    priority: DecisionPriority = DecisionPriority.NORMAL

    recommendation: Dict[str, Any] = Field(default_factory=dict)

    alternatives: List[Dict[str, Any]] = Field(default_factory=list)

    expected_cost: Optional[float] = None
    expected_duration_hours: Optional[float] = None
    expected_risk: Optional[float] = None

    confidence: float = Field(ge=0.0, le=1.0)

    constraints: List[str] = Field(default_factory=list)

    rationale: Optional[str] = None

    intelligence_refs: List[str] = Field(default_factory=list)
