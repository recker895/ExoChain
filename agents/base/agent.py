import time
from abc import ABC, abstractmethod
from typing import Any, Dict

from agents.base.context import AgentContext
from agents.base.result import AgentResult, AgentStatus


class BaseAgent(ABC):

    def __init__(
        self,
        agent_id: str,
        agent_type: str,
    ) -> None:

        self.agent_id = agent_id
        self.agent_type = agent_type

    @abstractmethod
    def execute(
        self,
        context: AgentContext,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def run(
        self,
        context: AgentContext,
    ) -> AgentResult:

        start = time.perf_counter()

        try:

            outputs = self.execute(context)

            latency_ms = (
                time.perf_counter() - start
            ) * 1000

            return AgentResult(
                agent_id=self.agent_id,
                agent_type=self.agent_type,
                status=AgentStatus.SUCCESS,
                outputs=outputs,
                latency_ms=latency_ms,
            )

        except Exception as exc:

            latency_ms = (
                time.perf_counter() - start
            ) * 1000

            return AgentResult(
                agent_id=self.agent_id,
                agent_type=self.agent_type,
                status=AgentStatus.FAILED,
                outputs={},
                errors=[str(exc)],
                confidence=0.0,
                latency_ms=latency_ms,
            )
