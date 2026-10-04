import hashlib
import json
import requests
from datetime import timedelta

from config.settings import settings
from core.schemas.contracts import (
    ApprovalRequest,
    ApprovalDecision,
    AuditEvent,
    ExecutionCommand,
    ExecutionResult,
    utcnow,
)
from services.policy import plan_hash, validate
from services.erp_client import configured_erp, HTTPERP
from services.providers.business import verify_source
from services.feedback.outcomes import transition, persist_feedback


def audit(state, stage, status, actor, reason):
    event = AuditEvent(
        run_id=state["run_id"],
        trace_id=state["trace_id"],
        stage=stage,
        status=status,
        actor=actor,
        reason=reason,
    )
    state.setdefault("audit", []).append(event.model_dump(mode="json"))
    state.setdefault("timestamps", {})[stage] = event.timestamp.isoformat()
    return event


def request_approval(state):
    validation = validate(state)
    if not validation.valid:
        return None
    if not state["request"].get("demo_mode"):
        try:
            verify_source(state)
        except ValueError as exc:
            state["validation"].update(valid=False, status="BLOCKED", reasons=[str(exc)])
            return None
    plan = state["optimization"]
    automatic = automatic_approval_allowed(state)
    now = utcnow()
    return ApprovalRequest(
        run_id=state["run_id"],
        plan_id=plan["plan_id"],
        plan_version=plan["plan_version"],
        plan_hash=validation.plan_hash,
        cost_usd=plan["total_cost_usd"],
        selected_actions=plan["selected_actions"],
        status="APPROVED" if automatic else "PENDING_APPROVAL",
        created_at=now,
        expires_at=now + timedelta(seconds=settings.APPROVAL_TTL_SECONDS),
        mode="POLICY" if automatic else "HUMAN",
        approver="policy:automatic-replenishment-v1" if automatic else None,
        decided_at=now if automatic else None,
        policy_reason="Explicit automatic approval policy; within threshold"
        if automatic
        else "Human approval required by request or configured policy",
    )


def automatic_approval_allowed(state):
    return (
        settings.ALLOW_AUTOMATIC_APPROVAL
        and state["request"]["operation"] == "REPLENISHMENT"
        and not state["request"].get("require_human_approval", True)
        and not state["request"].get("what_if")
        and state["optimization"]["total_cost_usd"] <= settings.APPROVAL_THRESHOLD_USD
    )


def verify_approval_binding(state, approval):
    """The approval snapshot must describe exactly the hashed execution plan."""
    plan = state["optimization"]
    if (
        approval.run_id != state["run_id"]
        or approval.plan_id != plan["plan_id"]
        or approval.plan_version != plan["plan_version"]
        or approval.cost_usd != plan["total_cost_usd"]
        or json.dumps(approval.selected_actions, sort_keys=True, allow_nan=False)
        != json.dumps(plan["selected_actions"], sort_keys=True, allow_nan=False)
        or approval.plan_hash != plan_hash(state)
    ):
        raise ValueError("approval does not match current plan; new approval required")


def decide(store, run_id, decision: ApprovalDecision, actor):
    if decision.decision == "APPROVED" and not store.get(run_id)["request"].get("demo_mode"):
        verify_source(store.get(run_id))

    def update(db, state):
        approval = ApprovalRequest.model_validate(state.get("approval"))
        if (
            actor
            and approval.status == decision.decision
            and state["status"] == decision.decision
            and approval.approver == actor
            and decision.plan_hash == approval.plan_hash
        ):
            verify_approval_binding(state, approval)
            return state, None
        if not actor or state["status"] != "PENDING_APPROVAL":
            raise ValueError("authenticated pending approval required")
        if approval.status != "PENDING_APPROVAL":
            raise ValueError("approval is not pending")
        verify_approval_binding(state, approval)
        if approval.expires_at < utcnow():
            approval.status = "EXPIRED"
            state["approval"] = approval.model_dump(mode="json")
            state["status"] = "EXPIRED"
            return state, audit(state, "approval", "EXPIRED", actor, "Approval expired")
        if (
            decision.plan_hash != approval.plan_hash
            or plan_hash(state) != approval.plan_hash
        ):
            raise ValueError("plan changed; new approval required")
        if decision.decision == "APPROVED" and not validate(state).valid:
            raise ValueError("plan no longer satisfies policy")
        approval.status = decision.decision
        approval.approver = actor
        approval.decided_at = utcnow()
        state["approval"] = approval.model_dump(mode="json")
        state["status"] = decision.decision
        transition(state, decision.decision)
        return state, audit(
            state, "approval", decision.decision, actor, decision.reason
        )

    return store.transaction(run_id, update)


def verify_command_binding(state, command):
    approval = ApprovalRequest.model_validate(state["approval"])
    verify_approval_binding(state, approval)
    identity = state["data"]["business_identity"]
    if (
        command.run_id != state["run_id"]
        or command.plan_id != approval.plan_id
        or command.approval_id != approval.id
        or command.plan_hash != approval.plan_hash
        or command.request_id != state["request"].get("request_id")
        or command.idempotency_key
        != hashlib.sha256((state["run_id"] + approval.plan_hash).encode()).hexdigest()
        or command.actions != approval.selected_actions
        or command.total_cost_usd != approval.cost_usd
        or command.business_snapshot_hash != identity["snapshot_hash"]
        or command.source_versions != identity["versions"]
        or command.purchase_cost_usd
        != sum(a["total_cost_usd"] for a in command.actions)
        or command.ordering_cost_usd
        != state["optimization"]["explanation"]["cost_ledger"]["ordering_cost_usd"]
    ):
        raise ValueError("Durable command differs from approved plan")


def execute(store, run_id, actor, client=None):
    if not actor:
        raise ValueError("authenticated operator required")
    captured = store.get(run_id)
    if captured["request"].get("demo_mode"):
        raise ValueError("Demo geographic route cannot be executed")
    # Source reads are outside the SQLite write lock. The transaction below
    # checks the exact snapshot/plan again before reserving the submission.
    if captured["status"] == "APPROVED":
        verify_source(
            captured,
            require_configured=not captured["request"].get("simulation")
            and isinstance(client or configured_erp(False), HTTPERP),
        )

    def reserve(db, state):
        approval = ApprovalRequest.model_validate(state.get("approval"))
        # Return a previous result before considering re-execution.
        key = hashlib.sha256((run_id + approval.plan_hash).encode()).hexdigest()
        existing = db.execute(
            "SELECT result,status FROM executions WHERE idempotency_key=?", (key,)
        ).fetchone()
        if existing:
            result = (
                json.loads(existing[0])
                if existing[0]
                else ExecutionResult(
                    status="IN_PROGRESS",
                    idempotency_key=key,
                    message="Submission reserved; reconcile interrupted calls before retrying",
                ).model_dump(mode="json")
            )
            return (None, result), None
        if (
            approval.status != "APPROVED"
            or state["status"] != "APPROVED"
            or not actor
            or not approval.approver
            or approval.decided_at is None
        ):
            raise ValueError("authenticated approval required")
        verify_approval_binding(state, approval)
        if plan_hash(captured) != plan_hash(state):
            raise ValueError("Plan changed during source verification")
        if approval.mode == "POLICY" and not automatic_approval_allowed(state):
            raise ValueError(
                "Automatic approval policy changed; human approval required"
            )
        now = utcnow()
        if not approval.created_at <= approval.decided_at <= now < approval.expires_at:
            raise ValueError("approval expired or plan changed")
        validation = validate(state)
        if not validation.valid:
            raise ValueError(
                "execution policy failed: " + ", ".join(validation.reasons)
            )
        if any(a["type"] != "PURCHASE_ORDER" for a in approval.selected_actions):
            result = ExecutionResult(
                status="ERP_UNAVAILABLE",
                idempotency_key=key,
                message="Transport booking connector unavailable; no purchase-order substitution",
            )
            state["execution"] = result.model_dump(mode="json")
            state["status"] = result.status
            return (None, state["execution"]), audit(
                state, "execution", result.status, actor, result.message
            )
        command = ExecutionCommand(
            run_id=run_id,
            plan_id=approval.plan_id,
            plan_hash=approval.plan_hash,
            idempotency_key=key,
            approval_id=approval.id,
            actions=state["optimization"]["selected_actions"],
            total_cost_usd=state["optimization"]["total_cost_usd"],
            request_id=state["request"].get("request_id"),
            business_snapshot_hash=state["data"]["business_identity"]["snapshot_hash"],
            source_versions=state["data"]["business_identity"]["versions"],
            purchase_cost_usd=sum(
                a["total_cost_usd"] for a in approval.selected_actions
            ),
            ordering_cost_usd=state["optimization"]
            .get("explanation", {})
            .get("cost_ledger", {})
            .get("ordering_cost_usd", 0),
        )
        if not state["request"].get("simulation"):
            for item in state["optimization"]["components"]["inventory"]["selected"]:
                if item["order_units"] <= 0:
                    continue
                identity = hashlib.sha256(
                    json.dumps([item["source"], item["id"], item["version"]]).encode()
                ).hexdigest()
                if db.execute(
                    "SELECT 1 FROM replenishment_commitments WHERE identity=?",
                    (identity,),
                ).fetchone():
                    raise ValueError(
                        "INVENTORY_VERSION_ALREADY_COMMITTED; refresh authoritative inventory"
                    )
                db.execute(
                    "INSERT INTO replenishment_commitments VALUES(?,?)", (identity, key)
                )
            quotes = {q["id"]: q for q in state["business_inputs"]["supplier_quotes"]}
            quantities = {}
            for action in command.actions:
                quantities[action["quote_id"]] = (
                    quantities.get(action["quote_id"], 0) + action["quantity"]
                )
            for quote_id, quantity in quantities.items():
                quote = quotes[quote_id]
                identity = hashlib.sha256(
                    json.dumps([quote["source"], quote_id, quote["version"]]).encode()
                ).hexdigest()
                committed = db.execute(
                    "SELECT COALESCE(SUM(units),0) FROM quote_commitments WHERE identity=?",
                    (identity,),
                ).fetchone()[0]
                if committed + quantity > quote["capacity_units"]:
                    raise ValueError(
                        "SUPPLIER_CAPACITY_ALREADY_COMMITTED; refresh authoritative quote"
                    )
                db.execute(
                    "INSERT INTO quote_commitments VALUES(?,?,?)",
                    (identity, key, quantity),
                )
        db.execute(
            "INSERT INTO executions VALUES(?,?,?,?,NULL)",
            (key, run_id, approval.plan_hash, "IN_PROGRESS"),
        )
        state["status"] = "EXECUTING"
        db.execute(
            "INSERT INTO execution_commands VALUES(?,?)",
            (key, command.model_dump_json()),
        )
        transition(state, "EXECUTION_IN_PROGRESS", command=command)
        transition(state, "SUBMITTED")
        return (command, state["request"]), audit(
            state, "execution", "IN_PROGRESS", actor, "Idempotent submission reserved"
        )

    command, info = store.transaction(run_id, reserve)
    if command is None:
        return info
    if client is None:
        client = configured_erp(info.get("simulation", False))
    try:
        result = client.submit_purchase_order(command)
        if (
            not isinstance(result, ExecutionResult)
            or result.idempotency_key != command.idempotency_key
        ):
            raise ValueError("malformed acknowledgement")
        if result.status == "EXECUTED" and (
            info.get("simulation") or result.simulation or not result.external_reference
        ):
            raise ValueError("invalid execution acknowledgement")
        if result.status == "EXECUTED":
            result = HTTPERP.validate_acknowledgement(
                command, result.model_dump(mode="json")
            )
        if result.status == "EXECUTION_SIMULATED" and (
            not info.get("simulation") or not result.simulation
        ):
            raise ValueError("simulation acknowledgement does not match requested mode")
    except (TimeoutError, ConnectionError, requests.RequestException):
        result = ExecutionResult(
            status="UNKNOWN",
            idempotency_key=command.idempotency_key,
            message="Submission outcome unknown; reconciliation required",
        )
    except Exception:
        result = ExecutionResult(
            status="FAILED",
            idempotency_key=command.idempotency_key,
            message="Invalid execution acknowledgement; manual reconciliation required",
        )

    def complete(db, state):
        if (state.get("execution_lifecycle") or {}).get("stage") == "RECONCILED":
            return state["execution"], None
        db.execute(
            "UPDATE executions SET status=?,result=? WHERE idempotency_key=?",
            (result.status, result.model_dump_json(), command.idempotency_key),
        )
        state["execution"] = result.model_dump(mode="json")
        state["status"] = result.status
        if result.status == "EXECUTED":
            state["approval"]["status"] = "EXECUTED"
        elif result.status in {"FAILED", "REJECTED", "UNKNOWN"}:
            state["approval"]["status"] = "FAILED"
        stage = {
            "EXECUTED": "ACKNOWLEDGED",
            "IN_PROGRESS": "REQUIRES_RECONCILIATION",
            "FAILED": "REQUIRES_RECONCILIATION",
            "ERP_UNAVAILABLE": "FAILED",
            "EXECUTION_SIMULATED": "ACKNOWLEDGED",
        }.get(result.status, result.status)
        transition(
            state,
            stage,
            result=result,
            error=result.message
            if result.status in {"FAILED", "UNKNOWN", "ERP_UNAVAILABLE"}
            else None,
        )
        persist_feedback(db, state, command, result)
        if result.status in {"REJECTED", "ERP_UNAVAILABLE"}:
            db.execute(
                "DELETE FROM replenishment_commitments WHERE idempotency_key=?",
                (command.idempotency_key,),
            )
            db.execute(
                "DELETE FROM quote_commitments WHERE idempotency_key=?",
                (command.idempotency_key,),
            )
        return state["execution"], audit(
            state, "execution", result.status, actor, result.message
        )

    return store.transaction(run_id, complete)
