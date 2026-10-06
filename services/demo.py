"""Repeatable college demonstrations with explicitly simulated commercial data."""

from datetime import timedelta

from config.settings import settings
from core.schemas.contracts import (
    ApprovalRequest, BusinessInputs, ExecutionResult, Provenance, RunRequest, utcnow,
)


def replenishment_sample():
    """Small self-contained dataset, available even on a clean GitHub checkout."""
    now = utcnow() - timedelta(seconds=1)
    metadata = dict(
        source="COLLEGE_DEMO", created_at=now, observed_at=now,
        valid_until=now + timedelta(days=30), version="college-demo-v1",
        data_quality="VALID",
        provenance=[dict(source="COLLEGE_DEMO", reference="sample-inventory",
                         observed_at=now, transformation="Simulated classroom dataset")],
    )
    skus, inventory, suppliers, quotes = [], [], [], []
    for index, (sku, description, stock, demand, safety, maximum, cost, lead) in enumerate([
        ("SKU-ENGINE-BEARING-001", "Engine bearing assembly", 420, 900, 250, 1500, 42.5, 10),
        ("SKU-HYDRAULIC-PUMP-002", "Hydraulic pump assembly", 120, 650, 180, 1000, 125, 14),
    ], 1):
        skus.append(dict(id=sku, unit="unit", description=description, **metadata))
        inventory.append(dict(
            id=f"INV-{index:03d}", sku_id=sku, location_id="WH-MUMBAI-001",
            current_inventory_units=stock, expected_demand_units=demand,
            demand_reference="sample-inventory", horizon_days=30, lead_time_days=lead,
            safety_stock_units=safety, service_level=1, max_stock_units=maximum,
            unit_cost_usd=cost, holding_cost_per_unit_usd=1.5,
            ordering_cost_usd=250, shortage_penalty_usd=250, **metadata,
        ))
        for option in range(2):
            supplier = f"DEMO-SUP-{index}-{option}"
            suppliers.append(dict(
                id=supplier, name=f"Demo supplier {index}-{option}",
                location_id="DEMO-SUPPLIER-LOCATION", approved=True,
                quality_score=0.9, reliability=0.95, **metadata,
            ))
            quotes.append(dict(
                id=f"DEMO-QUOTE-{index}-{option}", supplier_id=supplier,
                sku_id=sku, location_id="WH-MUMBAI-001", capacity_units=1000,
                unit_cost_usd=cost * (1 + option * 0.08),
                lead_time_days=lead - option * 3, risk_score=0.1 + option * 0.05,
                **metadata,
            ))
    return BusinessInputs(skus=skus, inventory=inventory, suppliers=suppliers,
                          supplier_quotes=quotes)


def demo_business(business):
    """Replay imported/sample facts on today's demo clock; retain original provenance."""
    business = (business or replenishment_sample()).model_copy(deep=True)
    now = utcnow() - timedelta(seconds=1)
    for field in type(business).model_fields:
        records = getattr(business, field)
        for record in records if isinstance(records, list) else [records]:
            if record is None:
                continue
            record.created_at = min(record.created_at, now)
            record.valid_until = now + timedelta(days=30)
            record.observed_at = now
            record.data_quality = "VALID"
            record.source = "COLLEGE_DEMO"
            record.provenance.append(Provenance(
                source="COLLEGE_DEMO", reference=record.id, observed_at=now,
                transformation="Demo clock replay; original provenance retained",
            ))
    return business


def prepare_demo(request, business=None):
    if not request.demo_mode:
        return request, business
    if settings.ENVIRONMENT == "production":
        raise ValueError("College demo is disabled in production")
    if request.what_if:
        return request, business
    changes = {"simulation": True}
    if request.operation == "REPLENISHMENT":
        business = demo_business(business)
        changes["required_components"] = ["inventory", "procurement"]
        changes["inventory_ids"] = request.inventory_ids or [i.id for i in business.inventory]
        changes["budget_usd"] = request.budget_usd or 125000
    return RunRequest.model_validate({**request.model_dump(), **changes}), business


def complete_demo(state):
    """A local simulation only: no ERP call, commitment, or production feedback."""
    if settings.ENVIRONMENT == "production":
        raise ValueError("College demo is disabled in production")
    if state["request"].get("what_if"):
        return {"status": "WHAT_IF_COMPLETE", "approval": None, "execution": None}
    plan = state["optimization"]
    if not state["validation"]["valid"] or not plan["selected_actions"]:
        return {"status": "DEMO_COMPLETE", "approval": None, "execution": None}
    now = utcnow()
    approval = ApprovalRequest(
        run_id=state["run_id"], plan_id=plan["plan_id"], plan_version=plan["plan_version"],
        plan_hash=state["validation"]["plan_hash"], cost_usd=plan["total_cost_usd"],
        selected_actions=plan["selected_actions"], status="APPROVED", mode="POLICY",
        created_at=now, decided_at=now, expires_at=now + timedelta(hours=1),
        approver="college-demo-simulator", policy_reason="Classroom simulation approval",
    )
    execution = ExecutionResult(
        status="EXECUTION_SIMULATED", idempotency_key=f"demo:{state['run_id']}",
        external_reference=f"DEMO-{state['run_id'][:8]}", simulation=True,
        message="College demo completed. Order/booking simulated locally.",
        plan_hash=approval.plan_hash, acknowledged_actions=plan["selected_actions"],
        acknowledged_cost_usd=plan["total_cost_usd"],
    )
    from services.feedback.outcomes import transition
    transition(state, "EXECUTION_SIMULATED", result=execution)
    return {"status": "EXECUTION_SIMULATED", "approval": approval.model_dump(mode="json"),
            "execution": execution.model_dump(mode="json")}
