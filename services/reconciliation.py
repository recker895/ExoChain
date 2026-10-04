"""Read-only ERP reconciliation of durable commands, including interrupted submissions."""

from core.schemas.contracts import (
    ApprovalRequest,
    ExecutionCommand,
    ExecutionResult,
    utcnow,
)
from services.approval_service import (
    audit,
    verify_approval_binding,
    verify_command_binding,
)
from services.erp_client import HTTPERP, configured_erp
from services.feedback.outcomes import transition, persist_feedback


def reconcile(store, run_id, actor, client=None):
    if not actor:
        raise ValueError("authenticated operator required")
    state = store.get(run_id)
    approval = ApprovalRequest.model_validate(state.get("approval"))
    verify_approval_binding(state, approval)
    lifecycle = state.get("execution_lifecycle") or {}
    if lifecycle.get("stage") == "RECONCILED":
        return lifecycle["reconciliation"]
    if state["request"].get("simulation"):
        raise ValueError("Simulated orders cannot produce production feedback")
    if lifecycle.get("stage") in {"SUBMITTED", "EXECUTION_IN_PROGRESS"}:
        from datetime import datetime

        if (
            utcnow() - datetime.fromisoformat(lifecycle["updated_at"])
        ).total_seconds() < 60:
            raise ValueError(
                "Submission may still be active; reconcile after its timeout"
            )
    with store.connect() as db:
        row = db.execute(
            "SELECT body FROM execution_commands WHERE idempotency_key IN (SELECT idempotency_key FROM executions WHERE run_id=?)",
            (run_id,),
        ).fetchone()
    if not row:
        raise ValueError("No durable ERP submission to reconcile")
    command = ExecutionCommand.model_validate_json(row[0])
    verify_command_binding(state, command)
    adapter = client or configured_erp()
    try:
        result = adapter.reconcile_purchase_order(command)
        if not isinstance(result, ExecutionResult):
            raise ValueError("Malformed reconciliation")
        result = HTTPERP.validate_acknowledgement(
            command, result.model_dump(mode="json")
        )
    except Exception as exc:
        result = ExecutionResult(
            status="UNKNOWN",
            idempotency_key=command.idempotency_key,
            message=f"ERP reconciliation unavailable: {type(exc).__name__}",
        )

    def complete(db, current):
        verify_approval_binding(
            current, ApprovalRequest.model_validate(current["approval"])
        )
        prior = current.get("execution_lifecycle") or {}
        if prior.get("stage") == "RECONCILED":
            return prior["reconciliation"], None
        # An inconclusive lookup cannot undo an already acknowledged submission.
        terminal = result.status in {"EXECUTED", "REJECTED"}
        if terminal:
            db.execute(
                "UPDATE executions SET status=?,result=? WHERE idempotency_key=?",
                (result.status, result.model_dump_json(), command.idempotency_key),
            )
            current["execution"] = result.model_dump(mode="json")
            current["status"] = result.status
            current["approval"]["status"] = (
                "EXECUTED" if result.status == "EXECUTED" else "FAILED"
            )
            if result.status == "REJECTED":
                db.execute(
                    "DELETE FROM replenishment_commitments WHERE idempotency_key=?",
                    (command.idempotency_key,),
                )
                db.execute(
                    "DELETE FROM quote_commitments WHERE idempotency_key=?",
                    (command.idempotency_key,),
                )
        transition(
            current,
            "RECONCILED" if terminal else "REQUIRES_RECONCILIATION",
            result=result,
            reconciled=True,
            error=None if terminal else result.message,
        )
        persist_feedback(db, current, command, result, reconciled=True)
        return result.model_dump(mode="json"), audit(
            current,
            "reconciliation",
            "RECONCILED" if terminal else "REQUIRES_RECONCILIATION",
            actor,
            result.message,
        )

    return store.transaction(run_id, complete)
