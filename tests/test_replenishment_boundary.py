"""Production boundary verification with explicit fixtures and HTTP transport doubles.

No test endpoint is configured or called by application code outside these tests.
"""

import csv
import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from fastapi.testclient import TestClient
from pydantic import SecretStr

from config.settings import settings
from core.schemas.contracts import (
    ApprovalDecision,
    Disruption,
    ExecutionResult,
    RunRequest,
    utcnow,
)
from orchestrator.data_cluster import DataCluster
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.approval_service import decide, execute
from services.providers.business import (
    JSONBusinessProvider,
    CSVBusinessProvider,
    APIBusinessProvider,
    fetch_business,
)
from services.reconciliation import reconcile


@pytest.fixture
def source(tmp_path, business, monkeypatch):
    path = tmp_path / "authoritative-test-only.json"
    path.write_text(business.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "BUSINESS_DATA_PATH", str(path))
    monkeypatch.setattr(settings, "BUSINESS_API_URL", "")
    monkeypatch.setattr(settings, "ERP_MODE", "http")
    monkeypatch.setattr(settings, "ERP_URL", "https://erp.test.invalid")
    monkeypatch.setattr(settings, "ERP_TOKEN", SecretStr("TEST_ONLY"))
    monkeypatch.setattr(settings, "ALLOW_AUTOMATIC_APPROVAL", False)
    return path


def run(store, registry, source, **changes):
    options = dict(
        operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
    )
    options.update(changes)
    return build_exochain_graph(
        store, DataCluster(registry, JSONBusinessProvider(source))
    ).invoke(initial_state(RunRequest(**options)))


def approve(store, state):
    return decide(
        store,
        state["run_id"],
        ApprovalDecision(
            plan_hash=state["approval"]["plan_hash"],
            decision="APPROVED",
            reason="TEST_ONLY",
        ),
        "approver:test",
    )


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def acknowledgement(command):
    return ExecutionResult(
        status="EXECUTED",
        idempotency_key=command["idempotency_key"],
        external_reference="TEST-ERP-PO",
        message="test acknowledgement",
        plan_hash=command["plan_hash"],
        acknowledged_actions=command["actions"],
        acknowledged_cost_usd=command["total_cost_usd"],
    ).model_dump(mode="json")


def test_complete_source_to_erp_reconciliation_feedback(
    source, store, registry, monkeypatch
):
    submitted = []

    def post(url, **kwargs):
        assert url == "https://erp.test.invalid/purchase-orders"
        assert kwargs["allow_redirects"] is False
        submitted.append(kwargs["json"])
        assert kwargs["headers"]["Idempotency-Key"] == submitted[-1]["idempotency_key"]
        return Response(acknowledgement(submitted[-1]))

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(
        requests, "get", lambda url, **kw: Response(acknowledgement(submitted[0]))
    )
    state = run(store, registry, source)
    assert (
        sum(len(state[k]["executions"]) for k in ("data", "intelligence", "decisions"))
        == 19
    )
    assert state["data"]["business_identity"]["configured"]
    assert state["optimization"]["status"] == "OPTIMAL"
    assert (
        state["optimization"]["components"]["inventory"]["selected"][0]["order_units"]
        == 15
    )
    assert state["validation"]["valid"]
    approve(store, state)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda _: execute(store, state["run_id"], "operator:test"), range(3)
            )
        )
    assert len(submitted) == 1
    assert all(r["status"] in {"EXECUTED", "IN_PROGRESS"} for r in results)
    command = submitted[0]
    assert command["actions"] == state["optimization"]["selected_actions"]
    assert command["request_id"] == state["request"]["request_id"]
    assert (
        command["business_snapshot_hash"]
        == state["data"]["business_identity"]["snapshot_hash"]
    )
    assert command["purchase_cost_usd"] == 75 and command["ordering_cost_usd"] == 2
    assert reconcile(store, state["run_id"], "operator:test")["status"] == "EXECUTED"
    assert store.get(state["run_id"])["execution_lifecycle"]["stage"] == "RECONCILED"
    assert len(store.feedback()) == 1
    feedback = store.feedback()[0]
    assert (
        feedback.ordered_quantity == 15 and feedback.actual_order_lead_time_days is None
    )
    next_run = run(store, registry, source)
    assert (
        next_run["intelligence"]["results"]["demand"]["output"]["execution_outcomes"][
            0
        ]["id"]
        == feedback.id
    )
    assert next_run["approval"] is None
    assert "INVENTORY_VERSION_ALREADY_COMMITTED" in next_run["validation"]["reasons"]
    with pytest.raises(ValueError):
        execute(store, next_run["run_id"], "operator:test")
    assert len(submitted) == 1


def test_timeout_requires_lookup_never_retry(source, store, registry, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        raise requests.Timeout("TEST_ONLY")

    monkeypatch.setattr(requests, "post", post)
    state = approve(store, run(store, registry, source))
    first = execute(store, state["run_id"], "operator:test")
    assert first["status"] == "UNKNOWN"
    assert store.get(state["run_id"])["execution_lifecycle"]["requires_reconciliation"]
    assert execute(store, state["run_id"], "operator:test") == first
    assert len(calls) == 1 and not store.feedback()
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: Response(acknowledgement(calls[0]))
    )
    assert reconcile(store, state["run_id"], "operator:test")["status"] == "EXECUTED"
    assert execute(store, state["run_id"], "operator:test")["status"] == "EXECUTED"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "allow,human,threshold,expected",
    [
        (False, False, 100, "PENDING_APPROVAL"),
        (True, True, 100, "PENDING_APPROVAL"),
        (True, False, 76, "PENDING_APPROVAL"),
        (True, False, 77, "APPROVED"),
        (True, False, 100, "APPROVED"),
    ],
)
def test_explicit_approval_policy(
    source, store, registry, monkeypatch, allow, human, threshold, expected
):
    monkeypatch.setattr(settings, "ALLOW_AUTOMATIC_APPROVAL", allow)
    monkeypatch.setattr(settings, "APPROVAL_THRESHOLD_USD", threshold)
    state = run(store, registry, source, require_human_approval=human)
    assert state["status"] == expected
    if expected == "APPROVED":
        assert state["approval"]["mode"] == "POLICY"
        assert state["approval"]["approver"].startswith("policy:")
        monkeypatch.setattr(settings, "ALLOW_AUTOMATIC_APPROVAL", False)
        with pytest.raises(ValueError, match="policy changed"):
            execute(store, state["run_id"], "operator:test")


@pytest.mark.parametrize("stage", ["approval", "execution"])
def test_source_changed_requires_new_run(source, business, store, registry, stage):
    state = run(store, registry, source)
    if stage == "execution":
        approve(store, state)
    business.inventory[0].current_inventory_units += 1
    source.write_text(business.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        approve(store, state) if stage == "approval" else execute(
            store, state["run_id"], "operator:test"
        )


def test_unconfigured_source_has_no_plan(store, registry, monkeypatch):
    monkeypatch.setattr(settings, "BUSINESS_DATA_PATH", "")
    monkeypatch.setattr(settings, "BUSINESS_API_URL", "")
    state = build_exochain_graph(store, DataCluster(registry)).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["absent"], budget_usd=1000
            )
        )
    )
    assert not state["optimization"]["selected_actions"] and state["approval"] is None
    assert state["data"]["sources"]["business"]["errors"] == [
        "BUSINESS_SOURCE_NOT_CONFIGURED"
    ]


def test_unconfigured_erp_is_unavailable(source, store, registry, monkeypatch):
    state = approve(store, run(store, registry, source))
    monkeypatch.setattr(settings, "ERP_MODE", "unavailable")
    assert (
        execute(store, state["run_id"], "operator:test")["status"] == "ERP_UNAVAILABLE"
    )
    assert not store.feedback()


def test_reservation_created_after_snapshot_still_blocks_execution(
    source, store, registry, monkeypatch
):
    first = approve(store, run(store, registry, source))
    second = approve(store, run(store, registry, source))
    commands = []

    def post(*args, **kwargs):
        commands.append(kwargs["json"])
        return Response(acknowledgement(commands[-1]))

    monkeypatch.setattr(requests, "post", post)
    assert execute(store, first["run_id"], "operator:test")["status"] == "EXECUTED"
    with pytest.raises(ValueError, match="ALREADY_COMMITTED"):
        execute(store, second["run_id"], "operator:test")
    assert len(commands) == 1


def test_typed_disruption_blocks_supply(source, business, metadata, store, registry):
    business.disruptions = [
        Disruption(
            id="closure",
            event_type="SUPPLIER_CLOSURE",
            severity=1,
            affected_entities=["supplier-test"],
            supplier_ids=["supplier-test"],
            supply_effect="UNAVAILABLE",
            explanation="TEST_ONLY",
            **metadata,
        )
    ]
    source.write_text(business.model_dump_json(), encoding="utf-8")
    state = run(store, registry, source)
    assert state["intelligence"]["results"]["disruption"]["output"][
        "replenishment_restrictions"
    ]["supplier_ids"] == ["supplier-test"]
    assert not [
        c for c in state["decisions"]["candidates"] if c["decision_type"] == "supplier"
    ]
    assert state["approval"] is None and state["status"] == "BLOCKED"


@pytest.mark.parametrize(
    "field", ["plan_hash", "acknowledged_actions", "idempotency_key"]
)
def test_wrong_erp_ack_never_succeeds(source, store, registry, monkeypatch, field):
    def post(url, **kwargs):
        payload = acknowledgement(kwargs["json"])
        payload[field] = [] if field == "acknowledged_actions" else "wrong"
        return Response(payload)

    monkeypatch.setattr(requests, "post", post)
    state = approve(store, run(store, registry, source))
    assert execute(store, state["run_id"], "operator:test")["status"] == "FAILED"
    assert not store.feedback()


def test_source_csv_and_https_contracts(tmp_path, business, monkeypatch):
    for domain, records in business.model_dump(mode="json").items():
        if not isinstance(records, list) or not records:
            continue
        with (tmp_path / (domain + ".csv")).open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(
                {
                    k: json.dumps(v) if isinstance(v, (dict, list)) else v
                    for k, v in row.items()
                }
                for row in records
            )
    assert CSVBusinessProvider(tmp_path).fetch().payload == business
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: Response(business.model_dump(mode="json"))
    )
    assert (
        APIBusinessProvider("https://business.test.invalid", "TEST_ONLY")
        .fetch()
        .payload
        == business
    )

    def timeout(*a, **kw):
        raise requests.Timeout("secret-must-not-appear")

    monkeypatch.setattr(requests, "get", timeout)
    result = fetch_business(
        APIBusinessProvider("https://business.test.invalid", "TEST_ONLY")
    )
    assert result.quality == "UNAVAILABLE" and "secret" not in result.model_dump_json()


def test_reconciliation_api_needs_run_not_operator_token(source, store, registry, monkeypatch):
    from dashboard.backend.main import create_app

    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr("test-operator"))
    monkeypatch.setattr(settings, "APPROVER_API_TOKEN", SecretStr("test-approver"))
    api = TestClient(create_app(store, registry, DataCluster(registry)))
    assert api.post("/api/v1/runs/absent/reconcile").status_code == 404
    assert (
        api.post(
            "/api/v1/runs/absent/reconcile",
            headers={"Authorization": "Bearer test-approver"},
        ).status_code
        == 404
    )
    assert api.get(
        "/api/v1/business/source", headers={"Authorization": "Bearer test-operator"}
    ).json()["identity"]["configured"]
    assert (
        api.get(
            "/api/v1/feedback", headers={"Authorization": "Bearer test-operator"}
        ).json()
        == []
    )


def test_api_execution_timeout_reconcile_feedback(source, store, registry, monkeypatch):
    import time
    from dashboard.backend.main import create_app

    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr("test-operator"))
    monkeypatch.setattr(settings, "APPROVER_API_TOKEN", SecretStr("test-approver"))
    operator = {"Authorization": "Bearer test-operator"}
    approver = {"Authorization": "Bearer test-approver"}
    commands = []

    def post(*args, **kwargs):
        commands.append(kwargs["json"])
        raise requests.Timeout("TEST_ONLY")

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: Response(acknowledgement(commands[0]))
    )
    app = create_app(store, registry, DataCluster(registry))
    api = TestClient(app)
    accepted = api.post(
        "/api/v1/runs",
        headers=operator,
        json={
            "request": {
                "operation": "REPLENISHMENT",
                "inventory_ids": ["inv-test"],
                "budget_usd": 1000,
            }
        },
    )
    assert accepted.status_code == 202
    rid = accepted.json()["run_id"]
    for _ in range(100):
        state = api.get(f"/api/v1/runs/{rid}", headers=operator).json()
        if state["status"] == "PENDING_APPROVAL":
            break
        time.sleep(0.02)
    assert state["status"] == "PENDING_APPROVAL"
    assert api.post(f"/api/v1/runs/{rid}/execute", headers=operator).status_code == 409
    assert not commands
    result = api.post(
        f"/api/v1/runs/{rid}/approval",
        headers=approver,
        json={
            "decision": "APPROVED",
            "reason": "TEST_ONLY",
            "plan_hash": state["approval"]["plan_hash"],
        },
    )
    assert result.status_code == 200
    assert (
        api.post(f"/api/v1/runs/{rid}/execute", headers=operator).json()["status"]
        == "UNKNOWN"
    )
    assert (
        api.post(f"/api/v1/runs/{rid}/execute", headers=operator).json()["status"]
        == "UNKNOWN"
    )
    assert (
        api.post(f"/api/v1/runs/{rid}/reconcile", headers=operator).json()["status"]
        == "EXECUTED"
    )
    state = api.get(f"/api/v1/runs/{rid}", headers=operator).json()
    assert state["execution_lifecycle"]["stage"] == "RECONCILED"
    assert state["feedback"][0]["ordered_quantity"] == 15
    assert len(commands) == 1


@pytest.mark.parametrize(
    "defect", ["missing_sku", "stale", "provenance", "capacity", "budget"]
)
def test_bad_business_never_approves(source, business, store, registry, defect):
    options = {}
    if defect == "missing_sku":
        business.skus = []
    elif defect == "stale":
        business.inventory[0].observed_at = utcnow() - timedelta(days=3)
    elif defect == "provenance":
        business.inventory[0].demand_reference = "unattributed"
    elif defect == "capacity":
        business.supplier_quotes[0].capacity_units = 1
    else:
        options["budget_usd"] = 1
    source.write_text(business.model_dump_json(), encoding="utf-8")
    state = run(store, registry, source, **options)
    assert state["approval"] is None
    assert not state["validation"]["valid"]


def test_quote_capacity_reserved_across_inventory_versions(
    source, business, store, registry, monkeypatch
):
    business.supplier_quotes[0].capacity_units = 20
    source.write_text(business.model_dump_json(), encoding="utf-8")
    commands = []

    def post(url, **kwargs):
        commands.append(kwargs["json"])
        return Response(acknowledgement(commands[-1]))

    monkeypatch.setattr(requests, "post", post)
    first = approve(store, run(store, registry, source))
    execute(store, first["run_id"], "operator:test")
    business.inventory[0].version = "test-2"
    source.write_text(business.model_dump_json(), encoding="utf-8")
    second = run(store, registry, source)
    assert second["approval"] is None and second["optimization"]["status"] == "OPTIMAL"
    assert second["status"] == "BLOCKED"
    assert sum(a["quantity"] for a in second["optimization"]["selected_actions"]) == 5
    assert second["data"]["reservations"]["quote_units"]["quote-test"] == 15
    with pytest.raises(ValueError):
        execute(store, second["run_id"], "operator:test")
    assert len(commands) == 1


def test_inconclusive_lookup_does_not_claim_execution(
    source, store, registry, monkeypatch
):
    def unavailable(*a, **kw):
        raise requests.Timeout("TEST_ONLY")

    monkeypatch.setattr(requests, "post", unavailable)
    monkeypatch.setattr(requests, "get", unavailable)
    state = approve(store, run(store, registry, source))
    execute(store, state["run_id"], "operator:test")
    assert reconcile(store, state["run_id"], "operator:test")["status"] == "UNKNOWN"
    assert (
        store.get(state["run_id"])["execution_lifecycle"]["stage"]
        == "REQUIRES_RECONCILIATION"
    )
    assert not store.feedback()


def test_interrupted_submission_is_recoverable_after_restart(
    source, store, registry, monkeypatch
):
    from core.state.run_store import RunStore

    commands = []

    def interrupted(*a, **kw):
        commands.append(kw["json"])
        raise SystemExit("TEST_PROCESS_CRASH")

    monkeypatch.setattr(requests, "post", interrupted)
    state = approve(store, run(store, registry, source))
    with pytest.raises(SystemExit):
        execute(store, state["run_id"], "operator:test")
    restarted = RunStore(store.path)
    assert (
        execute(restarted, state["run_id"], "operator:test")["status"] == "IN_PROGRESS"
    )
    with pytest.raises(ValueError, match="still be active"):
        reconcile(restarted, state["run_id"], "operator:test")

    def age(db, current):
        current["execution_lifecycle"]["updated_at"] = (
            utcnow() - timedelta(seconds=61)
        ).isoformat()
        return None, None

    restarted.transaction(state["run_id"], age)
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: Response(acknowledgement(commands[0]))
    )
    assert (
        reconcile(restarted, state["run_id"], "operator:test")["status"] == "EXECUTED"
    )
    assert len(commands) == 1 and len(restarted.feedback()) == 1


def test_tampered_durable_command_cannot_reconcile(
    source, store, registry, monkeypatch
):
    def unavailable(*a, **kw):
        raise requests.Timeout("TEST_ONLY")

    monkeypatch.setattr(requests, "post", unavailable)
    state = approve(store, run(store, registry, source))
    result = execute(store, state["run_id"], "operator:test")
    with store.connect() as db:
        row = db.execute("SELECT body FROM execution_commands").fetchone()
        command = json.loads(row[0])
        command["total_cost_usd"] += 1
        db.execute("UPDATE execution_commands SET body=?", (json.dumps(command),))
    with pytest.raises(ValueError, match="differs"):
        reconcile(store, state["run_id"], "operator:test")
    assert result["status"] == "UNKNOWN" and not store.feedback()


def test_missing_erp_token_is_explicitly_unavailable(
    source, store, registry, monkeypatch
):
    state = approve(store, run(store, registry, source))
    monkeypatch.setattr(settings, "ERP_TOKEN", SecretStr(""))
    assert (
        execute(store, state["run_id"], "operator:test")["status"] == "ERP_UNAVAILABLE"
    )


def test_http_conflict_does_not_release_unknown_submission(
    source, store, registry, monkeypatch
):
    response = Response({})
    response.status_code = 409

    def conflict():
        raise requests.HTTPError("TEST_ONLY")

    response.raise_for_status = conflict
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    state = approve(store, run(store, registry, source))
    assert execute(store, state["run_id"], "operator:test")["status"] == "UNKNOWN"
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM replenishment_commitments").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("reason", ["expired", "rejected"])
def test_expired_or_rejected_approval_never_submits(
    source, store, registry, monkeypatch, reason
):
    state = run(store, registry, source)
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: calls.append(kw))
    if reason == "rejected":
        decide(
            store,
            state["run_id"],
            ApprovalDecision(
                decision="REJECTED",
                reason="TEST_ONLY",
                plan_hash=state["approval"]["plan_hash"],
            ),
            "approver:test",
        )
    else:
        approve(store, state)

        def expire(db, current):
            current["approval"]["expires_at"] = (
                utcnow() - timedelta(seconds=1)
            ).isoformat()
            return None, None

        store.transaction(state["run_id"], expire)
    with pytest.raises(ValueError):
        execute(store, state["run_id"], "operator:test")
    assert calls == [] and store.feedback() == []


def test_policy_approved_execution_is_bound(source, store, registry, monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_AUTOMATIC_APPROVAL", True)
    monkeypatch.setattr(settings, "APPROVAL_THRESHOLD_USD", 77)
    commands = []

    def post(*a, **kw):
        commands.append(kw["json"])
        return Response(acknowledgement(commands[-1]))

    monkeypatch.setattr(requests, "post", post)
    state = run(store, registry, source, require_human_approval=False)
    assert state["approval"]["mode"] == "POLICY" and state["status"] == "APPROVED"
    assert execute(store, state["run_id"], "operator:test")["status"] == "EXECUTED"
    assert len(commands) == 1 and commands[0]["approval_id"] == state["approval"]["id"]


def test_reconciled_rejection_releases_reservations(
    source, store, registry, monkeypatch
):
    commands = []

    def post(*a, **kw):
        commands.append(kw["json"])
        raise requests.Timeout("TEST_ONLY")

    monkeypatch.setattr(requests, "post", post)
    state = approve(store, run(store, registry, source))
    execute(store, state["run_id"], "operator:test")
    rejection = ExecutionResult(
        status="REJECTED",
        idempotency_key=commands[0]["idempotency_key"],
        message="TEST_ONLY",
    )
    monkeypatch.setattr(
        requests, "get", lambda *a, **kw: Response(rejection.model_dump(mode="json"))
    )
    assert reconcile(store, state["run_id"], "operator:test")["status"] == "REJECTED"
    assert execute(store, state["run_id"], "operator:test")["status"] == "REJECTED"
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM replenishment_commitments").fetchone()[0]
            == 0
        )
        assert db.execute("SELECT COUNT(*) FROM quote_commitments").fetchone()[0] == 0
    assert not store.feedback() and len(commands) == 1


def test_acknowledged_cost_cannot_exceed_authorization(
    source, store, registry, monkeypatch
):
    def post(*a, **kw):
        payload = acknowledgement(kw["json"])
        payload["acknowledged_cost_usd"] += 1
        return Response(payload)

    monkeypatch.setattr(requests, "post", post)
    state = approve(store, run(store, registry, source))
    assert execute(store, state["run_id"], "operator:test")["status"] == "FAILED"
    assert store.get(state["run_id"])["execution_lifecycle"]["requires_reconciliation"]
    assert not store.feedback()
