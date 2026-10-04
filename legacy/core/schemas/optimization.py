from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class OptimizationObjective(str, Enum):
    COST = "cost"
    TIME = "time"
    RISK = "risk"
    FUEL = "fuel"
    EMISSIONS = "emissions"
    BALANCED = "balanced"


class OptimizationConstraint(BaseModel):
    name: str
    constraint_type: str
    value: Any
    operator: str = "eq"
    hard: bool = True


class OptimizationRequest(BaseModel):
    request_id: str
    timestamp: datetime

    objective: OptimizationObjective = OptimizationObjective.BALANCED

    variables: Dict[str, Any] = Field(default_factory=dict)
    constraints: List[OptimizationConstraint] = Field(default_factory=list)

    candidate_decisions: List[Dict[str, Any]] = Field(default_factory=list)

    budget: Optional[float] = None
    deadline: Optional[datetime] = None

    metadata: Dict[str, Any] = Field(default_factory=dict)


class OptimizationSolution(BaseModel):
    solution_id: str
    request_id: str

    timestamp: datetime

    status: str

    objective_value: Optional[float] = None

    total_cost: Optional[float] = None
    total_time_hours: Optional[float] = None
    total_fuel: Optional[float] = None
    total_risk: Optional[float] = None
    total_emissions: Optional[float] = None

    decisions: List[Dict[str, Any]] = Field(default_factory=list)

    constraint_violations: List[str] = Field(default_factory=list)

    solver: Optional[str] = None
    solver_version: Optional[str] = None

    metadata: Dict[str, Any] = Field(default_factory=dict)
