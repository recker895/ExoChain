"""Persist acknowledged commercial outcomes; never infer receipt or alter inventory."""

import hashlib

from core.schemas.contracts import (
    ExecutionFeedback,
    ExecutionLifecycle,
    Provenance,
    utcnow,
)


def transition(
    state, stage, *, command=None, result=None, reconciled=False, error=None
):
    lifecycle = ExecutionLifecycle.model_validate(
        state.get("execution_lifecycle") or {"stage": stage}
    )
    lifecycle.stage = stage
    lifecycle.updated_at = utcnow()
    lifecycle.history.append(
        {"stage": stage, "timestamp": lifecycle.updated_at.isoformat()}
    )
    if command is not None:
        lifecycle.command = command
    if result is not None:
        if reconciled:
            lifecycle.reconciliation = result
        else:
            lifecycle.acknowledgement = result
    lifecycle.requires_reconciliation = stage in {"UNKNOWN", "REQUIRES_RECONCILIATION"}
    lifecycle.error = error
    state["execution_lifecycle"] = lifecycle.model_dump(mode="json")


def persist_feedback(db, state, command, result, *, reconciled=False):
    if (
        result.status != "EXECUTED"
        or result.simulation
        or not result.external_reference
    ):
        return
    quotes = {q["id"]: q for q in state["business_inputs"]["supplier_quotes"]}
    for index, action in enumerate(command.actions):
        quote = quotes[action["quote_id"]]
        identity = hashlib.sha256(
            f"{command.idempotency_key}:{index}".encode()
        ).hexdigest()
        feedback = ExecutionFeedback(
            id=identity,
            run_id=command.run_id,
            plan_hash=command.plan_hash,
            idempotency_key=command.idempotency_key,
            erp_po_id=result.external_reference,
            quote_id=action["quote_id"],
            supplier_id=action["supplier_id"],
            sku_id=action["sku_id"],
            location_id=action["location_id"],
            ordered_quantity=action["quantity"],
            planned_cost_usd=action["total_cost_usd"],
            acknowledged_order_cost_usd=result.acknowledged_cost_usd,
            planned_lead_time_days=quote["lead_time_days"],
            actual_order_lead_time_days=result.actual_lead_time_days,
            status="RECONCILED" if reconciled else "ACKNOWLEDGED",
            observed_at=result.timestamp,
            provenance=[Provenance.model_validate(p) for p in quote["provenance"]]
            + [
                Provenance(
                    source="ERP",
                    reference=result.external_reference,
                    observed_at=result.timestamp,
                    transformation="Acknowledgement; not proof of goods receipt",
                )
            ],
        )
        db.execute(
            "INSERT INTO execution_feedback VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
            (identity, command.run_id, feedback.model_dump_json()),
        )
