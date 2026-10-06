"""Serializable LangGraph workflow. Dependencies live in closures, never state."""

from __future__ import annotations
import json
from typing import TypedDict
from uuid import uuid4
from langgraph.graph import StateGraph, END
from core.schemas.contracts import (
    AuditEvent,
    BusinessInputs,
    DataSnapshot,
    IntelligenceBundle,
    DecisionBundle,
    RunRequest,
    utcnow,
)
from orchestrator.data_cluster import DataCluster
from orchestrator.intelligence_cluster import intelligence_cluster
from orchestrator.decision_cluster import decision_cluster
from optimization.engine import optimize
from services.policy import validate
from services.approval_service import request_approval, audit
from services.feedback.outcomes import transition
from services.demo import prepare_demo, complete_demo


class ExoChainOSState(TypedDict):
    run_id: str
    trace_id: str
    request: dict
    business_inputs: dict
    telemetry: dict
    data: dict
    intelligence: dict
    decisions: dict
    optimization: dict
    validation: dict
    approval: dict | None
    execution: dict | None
    audit: list[dict]
    errors: list[dict]
    status: str
    timestamps: dict[str, str]
    execution_lifecycle: dict | None
    route_input: dict | None


def initial_state(
    request: RunRequest, business: BusinessInputs | None = None, run_id=None, route_input=None
):
    request, business = prepare_demo(request, business)
    return ExoChainOSState(
        run_id=run_id or str(uuid4()),
        trace_id=str(uuid4()),
        request=request.model_dump(mode="json"),
        business_inputs=business.model_dump(mode="json")
        if business is not None
        else {},
        telemetry={},
        data={},
        intelligence={},
        decisions={},
        optimization={},
        validation={},
        approval=None,
        execution=None,
        execution_lifecycle=None,
        route_input=route_input,
        audit=[],
        errors=[],
        status="DRAFT",
        timestamps={"created_at": utcnow().isoformat()},
    )


def build_exochain_decision_os(store=None, data_cluster=None):
    cluster = data_cluster or DataCluster()

    def observer(state):
        def emit(agent, status, latency):
            if store:
                store.append_event(
                    AuditEvent(
                        run_id=state["run_id"],
                        trace_id=state["trace_id"],
                        stage=agent,
                        status=status,
                        actor=agent,
                        reason="Agent started"
                        if latency is None
                        else f"Agent completed in {latency:.2f} ms",
                    )
                )

        return emit

    def wrap(stage, function):
        def node(state):
            # LangGraph dictionaries are copied before every transition. No
            # object references, redis clients or model instances cross nodes.
            current = json.loads(json.dumps(state, allow_nan=False))
            event = audit(current, stage, "RUNNING", "system", f"{stage} started")
            if store:
                store.save(current, event)
            updates = function(current)
            current.update(updates)
            event = audit(
                current, stage, current["status"], "system", f"{stage} completed"
            )
            if store:
                current["audit"] = [
                    {k: v for k, v in event.items() if k != "sequence"}
                    for event in store.events(current["run_id"], limit=10000)
                ] + [event.model_dump(mode="json")]
                # Publish the completed state once. A second save after exposing
                # PENDING_APPROVAL could overwrite a concurrent human decision.
                store.save(current, event)
            json.dumps(current, allow_nan=False)
            return current

        return node

    def data_node(state):
        if state["request"].get("what_if") and state.get("data"):
            snapshot = DataSnapshot.model_validate(state["data"])
        else:
            business = (
                BusinessInputs.model_validate(state["business_inputs"])
                if state["business_inputs"]
                else None
            )
            request = RunRequest.model_validate(state["request"])
            snapshot = cluster.execute(
                business, observer=observer(state),
                vessel_reference=request.vessel_reference,
                request=request, route_input=state.get("route_input"),
            )
            if store:
                snapshot.feedback = store.feedback()
                if not request.demo_mode:
                    snapshot.reservations = store.reservations(snapshot.business)
        errors = [
            {"provider": k, "errors": v.errors, "quality": v.quality.value}
            for k, v in snapshot.sources.items()
            if v.errors
        ]
        return {
            "data": snapshot.model_dump(mode="json"),
            "business_inputs": snapshot.business.model_dump(mode="json"),
            "telemetry": {
                "vessel_count": len(snapshot.vessels),
                "sources": list(snapshot.sources),
            },
            "errors": errors,
            "status": "DATA_COLLECTED",
        }

    def intelligence_node(state):
        result = intelligence_cluster.execute(
            DataSnapshot.model_validate(state["data"]), observer=observer(state)
        )
        return {
            "intelligence": result.model_dump(mode="json"),
            "status": "INTELLIGENCE_COMPLETE",
        }

    def decisions_node(state):
        result = decision_cluster.execute(
            DataSnapshot.model_validate(state["data"]),
            IntelligenceBundle.model_validate(state["intelligence"]),
            RunRequest.model_validate(state["request"]),
            observer=observer(state),
        )
        return {
            "decisions": result.model_dump(mode="json"),
            "status": "CANDIDATES_GENERATED",
        }

    def optimization_node(state):
        result = optimize(
            state["run_id"],
            RunRequest.model_validate(state["request"]),
            DataSnapshot.model_validate(state["data"]),
            DecisionBundle.model_validate(state["decisions"]),
        )
        if state["request"].get("demo_mode"):
            if result.status == "BLOCKED":
                result.status = "DEMO_WARNINGS"
            result.explanation["demo"] = {
                "label": "COLLEGE DEMO",
                "business_data": "Public geographic route and external model evidence" if state["request"]["operation"] == "TRANSPORT" else "Simulated or replayed demo records",
                "note": "All agents are invoked; unavailable external feeds are optional.",
            }
        if result.status in {"OPTIMAL", "FEASIBLE"}:
            transition(state, "PLAN_CREATED")
        return {"optimization": result.model_dump(mode="json"), "status": result.status}

    def validation_node(state):
        result = validate(state)
        return {
            "validation": result.model_dump(mode="json"),
            "status": "VALIDATING" if result.valid else result.status,
        }

    def approval_node(state):
        if (state["request"].get("demo_mode") and state["request"]["operation"] == "TRANSPORT"
                and not state["request"].get("require_human_approval", True)):
            return {"status": "ROUTE_READY" if state["validation"]["valid"] else "NO_FEASIBLE_ROUTE",
                    "approval": None, "execution": None}
        if state["request"].get("demo_mode") and (
            state["request"]["operation"] != "TRANSPORT"
            or not state["request"].get("require_human_approval", True)
        ):
            return complete_demo(state)
        approval = request_approval(state) if state["validation"]["valid"] else None
        if approval:
            transition(state, approval.status)
        return {
            "approval": approval.model_dump(mode="json") if approval else None,
            "status": approval.status
            if approval
            else "WHAT_IF_COMPLETE"
            if state["request"]["what_if"]
            else "NO_ACTION_REQUIRED"
            if state["validation"]["status"] == "NO_ACTION_REQUIRED"
            else "BLOCKED",
        }

    workflow = StateGraph(ExoChainOSState)
    nodes = {
        "data": data_node,
        "intelligence": intelligence_node,
        "decisions": decisions_node,
        "optimization": optimization_node,
        "validation": validation_node,
        "approval": approval_node,
    }
    for name, function in nodes.items():
        workflow.add_node(name + "_stage", wrap(name, function))
    names = list(nodes)
    workflow.set_entry_point(names[0] + "_stage")
    for before, after in zip(names, names[1:]):
        workflow.add_edge(before + "_stage", after + "_stage")
    workflow.add_edge(names[-1] + "_stage", END)
    return workflow.compile()


build_exochain_graph = build_exochain_decision_os

if __name__ == "__main__":
    from config.settings import settings
    from core.state.run_store import RunStore

    result = build_exochain_decision_os(RunStore(settings.RUN_DB_PATH)).invoke(
        initial_state(RunRequest())
    )
    print(
        json.dumps(
            {
                "run_id": result["run_id"],
                "status": result["status"],
                "validation": result["validation"],
            },
            indent=2,
        )
    )
