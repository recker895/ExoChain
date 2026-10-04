from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class IntelligenceType(str, Enum):
    FORECAST = "forecast"
    ETA = "eta"
    RISK = "risk"
    CONGESTION = "congestion"
    SUPPLY_RISK = "supply_risk"
    COST = "cost"
    FUEL = "fuel"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ForecastPoint(BaseModel):
    timestamp: datetime
    value: float
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None


class RiskAssessment(BaseModel):
    risk_type: str
    probability: float = Field(ge=0.0, le=1.0)
    impact: float = Field(ge=0.0, le=1.0)
    level: RiskLevel
    factors: List[str] = Field(default_factory=list)


class IntelligenceResult(BaseModel):
    result_id: str
    agent_id: str
    intelligence_type: IntelligenceType

    timestamp: datetime

    confidence: float = Field(ge=0.0, le=1.0)

    prediction: Optional[float] = None
    forecast: List[ForecastPoint] = Field(default_factory=list)

    risk: Optional[RiskAssessment] = None

    metrics: Dict[str, float] = Field(default_factory=dict)
    features: Dict[str, Any] = Field(default_factory=dict)

    explanation: Optional[str] = None

    model_name: Optional[str] = None
    model_version: Optional[str] = None
