"""Execution instrumentation shared by all canonical domains."""

import time
from core.schemas.contracts import AgentExecution, Quality, utcnow


def execute_agent(
    agent_id, domain, function, source, quality=Quality.UNAVAILABLE, observer=None
):
    started, clock = utcnow(), time.perf_counter()
    if observer:
        observer(agent_id, "RUNNING", None)
    try:
        output = function()
        payload = (
            output.model_dump(mode="json") if hasattr(output, "model_dump") else output
        )
        status = payload.get("status", payload.get("quality", "UNAVAILABLE"))
        if status == "VALID":
            status = "SUCCESS"
        record = AgentExecution(
            agent_id=agent_id,
            domain=domain,
            status=status,
            started_at=started,
            completed_at=utcnow(),
            latency_ms=(time.perf_counter() - clock) * 1000,
            input_quality=Quality(payload.get("quality", quality))
            if domain == "DATA"
            else quality,
            confidence=payload.get("confidence"),
            source=source,
            output=(
                {
                    **{
                        k: v
                        for k, v in payload.items()
                        if k not in {"payload", "sources"}
                    },
                    "output_reference": "data.sources",
                }
                if domain == "DATA"
                else payload
            ),
        )
        if observer:
            observer(agent_id, record.status, record.latency_ms)
        return output, record
    except Exception as exc:
        if observer:
            observer(agent_id, "FAILED", (time.perf_counter() - clock) * 1000)
        return None, AgentExecution(
            agent_id=agent_id,
            domain=domain,
            status="FAILED",
            started_at=started,
            completed_at=utcnow(),
            latency_ms=(time.perf_counter() - clock) * 1000,
            input_quality=quality,
            source=source,
            errors=[type(exc).__name__],
        )
