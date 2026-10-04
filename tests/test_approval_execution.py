from concurrent.futures import ThreadPoolExecutor
import json
import pytest
from core.schemas.contracts import ApprovalDecision, ExecutionResult, RunRequest
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.approval_service import decide, execute
from services.erp_client import SimulationERP


def pending(store, cluster, business, simulation=True):
    return build_exochain_graph(store, cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT",
                inventory_ids=["inv-test"],
                budget_usd=1000,
                simulation=simulation,
            ),
            business,
        )
    )


def approve(store, state):
    return decide(
        store,
        state["run_id"],
        ApprovalDecision(
            plan_hash=state["approval"]["plan_hash"],
            decision="APPROVED",
            reason="Test approval",
        ),
        "test-approver",
    )


def test_pending_never_rejected_and_cannot_execute(store, cluster, business):
    state = pending(store, cluster, business)
    with pytest.raises(ValueError):
        execute(store, state["run_id"], "test", SimulationERP())
    assert store.get(state["run_id"])["approval"]["status"] == "PENDING_APPROVAL"


def test_simulation_and_idempotency(store, cluster, business):
    state = approve(store, pending(store, cluster, business))

    class Counting(SimulationERP):
        calls = 0

        def submit_purchase_order(self, command):
            self.calls += 1
            return super().submit_purchase_order(command)

    client = Counting()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: execute(store, state["run_id"], "test", client), range(4)
            )
        )
    assert all(r["status"] in {"EXECUTION_SIMULATED", "IN_PROGRESS"} for r in results)
    assert client.calls == 1
    result = execute(store, state["run_id"], "test", client)
    assert result["status"] == "EXECUTION_SIMULATED" and result["simulation"] is True
    assert client.calls == 1


@pytest.mark.parametrize(
    "response", ['{"status":"EXECUTED"}', None, {"status": "APPROVED"}]
)
def test_malformed_erp_cannot_succeed(store, cluster, business, response):
    state = approve(store, pending(store, cluster, business))

    class Bad:
        def submit_purchase_order(self, c):
            return response

    assert execute(store, state["run_id"], "test", Bad())["status"] == "FAILED"


def test_rejected_erp_stays_rejected(store, cluster, business):
    state = approve(store, pending(store, cluster, business))

    class Reject:
        def submit_purchase_order(self, c):
            return ExecutionResult(
                status="REJECTED",
                idempotency_key=c.idempotency_key,
                message="Rejected by test ERP",
            )

    assert execute(store, state["run_id"], "test", Reject())["status"] == "REJECTED"


def test_changed_plan_invalidates_approval(store, cluster, business):
    state = approve(store, pending(store, cluster, business))
    state["optimization"]["selected_actions"][0]["quantity"] += 1
    # Deliberate DB tamper models a changed plan, bypassing immutable save.
    with store.connect() as db:
        db.execute(
            "UPDATE runs SET state=? WHERE run_id=?",
            (json.dumps(state), state["run_id"]),
        )
    with pytest.raises(ValueError):
        execute(store, state["run_id"], "test", SimulationERP())


@pytest.mark.parametrize("stage", ["decision", "execution"])
@pytest.mark.parametrize(
    "field", ["run_id", "plan_id", "plan_version", "cost_usd", "selected_actions"]
)
def test_approval_snapshot_tamper_never_calls_erp(
    store, cluster, business, stage, field
):
    state = pending(store, cluster, business)
    if stage == "execution":
        state = approve(store, state)
    if field == "selected_actions":
        state["approval"][field][0]["quantity"] += 1
    elif field in {"cost_usd", "plan_version"}:
        state["approval"][field] += 1
    else:
        state["approval"][field] = "another-identity"
    with store.connect() as db:
        db.execute(
            "UPDATE runs SET state=? WHERE run_id=?",
            (json.dumps(state), state["run_id"]),
        )

    class NeverCalled(SimulationERP):
        calls = 0

        def submit_purchase_order(self, command):
            self.calls += 1
            return super().submit_purchase_order(command)

    client = NeverCalled()
    with pytest.raises(ValueError, match="approval does not match"):
        if stage == "decision":
            approve(store, state)
        else:
            execute(store, state["run_id"], "test", client)
    assert client.calls == 0
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM executions").fetchone()[0] == 0


@pytest.mark.parametrize(
    "requested_simulation,status,ack_simulation",
    [
        (True, "EXECUTED", False),
        (True, "EXECUTION_SIMULATED", False),
        (False, "EXECUTION_SIMULATED", True),
        (False, "EXECUTED", True),
    ],
)
def test_erp_acknowledgement_cannot_change_execution_mode(
    store, cluster, business, requested_simulation, status, ack_simulation
):
    state = approve(store, pending(store, cluster, business, requested_simulation))

    class WrongMode:
        calls = 0

        def submit_purchase_order(self, command):
            self.calls += 1
            return ExecutionResult(
                status=status,
                simulation=ack_simulation,
                idempotency_key=command.idempotency_key,
                external_reference="TEST_ONLY_ACK",
                message="Test mode mismatch",
            )

    client = WrongMode()
    result = execute(store, state["run_id"], "test", client)
    assert result["status"] == "FAILED"
    assert store.get(state["run_id"])["approval"]["status"] == "FAILED"
    assert execute(store, state["run_id"], "test", client) == result
    assert client.calls == 1


def test_graph_completion_cannot_overwrite_concurrent_approval(
    store, cluster, business, monkeypatch
):
    save = store.save
    approved = []

    def approve_when_published(state, event=None):
        save(state, event)
        if state.get("approval") and not approved:
            approved.append(state["run_id"])
            approve(store, state)

    monkeypatch.setattr(store, "save", approve_when_published)
    stale = pending(store, cluster, business)
    assert store.get(stale["run_id"])["approval"]["status"] == "APPROVED"
    with pytest.raises(ValueError, match="transitions require a transaction"):
        save(stale)
