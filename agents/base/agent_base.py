from __future__ import annotations

import time
import uuid
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, ConfigDict


logger = logging.getLogger("exochain.agents")

T = TypeVar("T")


class AgentExecution(BaseModel, Generic[T]):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    agent_name: str
    agent_version: str
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    latency_ms: float = 0.0

    input_references: list[str] = Field(default_factory=list)
    output: T | None = None

    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    data_quality: str = "UNKNOWN"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class AgentContext(BaseModel):
    """
    Shared execution context passed between ExoChain agents.

    The context intentionally stores references and structured state rather
    than arbitrary hidden agent state.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    state: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)

    metadata: dict[str, Any] = Field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.outputs:
            return self.outputs[key]

        if key in self.inputs:
            return self.inputs[key]

        return self.state.get(key, default)

    def set_output(self, key: str, value: Any) -> None:
        self.outputs[key] = value


class BaseAgent(ABC, Generic[T]):
    """
    Production base contract for every ExoChain agent.

    Agents must:
    - validate inputs
    - execute deterministically where possible
    - return structured outputs
    - expose health information
    - expose execution metrics
    - never fabricate unavailable data
    """

    agent_id: str
    agent_name: str
    agent_type: str
    version: str = "1.0.0"
    dependencies: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._execution_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._total_latency_ms = 0.0

    @abstractmethod
    def validate_input(self, context: AgentContext) -> None:
        """Raise ValueError when required inputs are invalid."""

    @abstractmethod
    def run(self, context: AgentContext) -> T:
        """Execute the agent's actual domain logic."""

    def validate_output(self, output: T) -> None:
        if output is None:
            raise ValueError(
                f"{self.agent_id} returned None. "
                "Agents must return structured output."
            )

    def execute(self, context: AgentContext) -> AgentExecution[T]:
        started = datetime.now(timezone.utc)
        start_perf = time.perf_counter()

        self._execution_count += 1

        execution = AgentExecution[T](
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            agent_version=self.version,
            started_at=started,
            status="RUNNING",
            input_references=[],
        )

        try:
            self.validate_input(context)

            output = self.run(context)

            self.validate_output(output)

            latency_ms = (time.perf_counter() - start_perf) * 1000.0

            self._success_count += 1
            self._total_latency_ms += latency_ms

            execution.completed_at = datetime.now(timezone.utc)
            execution.status = "SUCCESS"
            execution.latency_ms = latency_ms
            execution.output = output
            execution.confidence = self._extract_confidence(output)
            execution.data_quality = self._extract_data_quality(output)

            logger.info(
                "agent_execution_success",
                extra={
                    "agent_id": self.agent_id,
                    "execution_id": execution.execution_id,
                    "latency_ms": latency_ms,
                },
            )

            return execution

        except Exception as exc:
            latency_ms = (time.perf_counter() - start_perf) * 1000.0

            self._failure_count += 1
            self._total_latency_ms += latency_ms

            execution.completed_at = datetime.now(timezone.utc)
            execution.status = "FAILED"
            execution.latency_ms = latency_ms
            execution.errors.append(str(exc))

            logger.exception(
                "agent_execution_failed",
                extra={
                    "agent_id": self.agent_id,
                    "execution_id": execution.execution_id,
                },
            )

            return execution

    def health_check(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "agent_type": self.agent_type,
            "version": self.version,
            "status": "HEALTHY",
            "dependencies": list(self.dependencies),
        }

    def metrics(self) -> dict[str, Any]:
        average_latency = (
            self._total_latency_ms / self._execution_count
            if self._execution_count
            else 0.0
        )

        success_rate = (
            self._success_count / self._execution_count
            if self._execution_count
            else 0.0
        )

        return {
            "agent_id": self.agent_id,
            "executions": self._execution_count,
            "successes": self._success_count,
            "failures": self._failure_count,
            "success_rate": success_rate,
            "average_latency_ms": average_latency,
        }

    @staticmethod
    def _extract_confidence(output: Any) -> float | None:
        value = getattr(output, "confidence", None)

        if isinstance(value, (int, float)):
            return float(value)

        return None

    @staticmethod
    def _extract_data_quality(output: Any) -> str:
        value = getattr(output, "data_quality", None)

        if value is None:
            return "UNKNOWN"

        if hasattr(value, "value"):
            return str(value.value)

        return str(value)
