"""Durable runs, append-only audit/outbox, and atomic execution reservation.

SQLite transactions serialize approval/execution races. An interrupted external
submission is never blindly retried: reconcile by its idempotency key first.
"""

from __future__ import annotations
import json
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import timedelta

from core.schemas.contracts import AuditEvent, utcnow


class RunStore:
    # Cluster agents append audit events concurrently while stage snapshots can
    # be large. Allow SQLite's single writer to finish before rejecting a run.
    BUSY_TIMEOUT_MS = 60_000

    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise ValueError("Database schema is newer than this application")
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, run_id TEXT NOT NULL, body TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS executions(idempotency_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, plan_hash TEXT NOT NULL, status TEXT NOT NULL, result TEXT);
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id,sequence);
            CREATE TABLE IF NOT EXISTS execution_commands(idempotency_key TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS replenishment_commitments(identity TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS execution_feedback(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS quote_commitments(identity TEXT NOT NULL, idempotency_key TEXT NOT NULL, units INTEGER NOT NULL, PRIMARY KEY(identity,idempotency_key));
            CREATE TABLE IF NOT EXISTS workflow_workers(run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, heartbeat TEXT NOT NULL);
            PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(
            self.path, timeout=self.BUSY_TIMEOUT_MS / 1000, isolation_level=None
        )
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={self.BUSY_TIMEOUT_MS}")
        try:
            yield db
        finally:
            db.close()

    def save(self, state, event: AuditEvent | None = None):
        encoded = json.dumps(state, allow_nan=False, separators=(",", ":"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT state FROM runs WHERE run_id=?", (state["run_id"],)
                ).fetchone()
                if existing:
                    old = json.loads(existing[0])
                    if any(
                        e.get("error") == "WORKFLOW_LEASE_EXPIRED"
                        for e in old.get("errors", [])
                    ):
                        raise ValueError(
                            "Interrupted workflow is fenced; create a new run"
                        )
                    for key in ("run_id", "request"):
                        if old[key] != state[key]:
                            raise ValueError("run identity/request is immutable")
                    if old.get("data") and old["data"] != state.get("data"):
                        raise ValueError("input snapshot is immutable")
                    if old.get("data") and old.get("business_inputs") != state.get(
                        "business_inputs"
                    ):
                        raise ValueError("business input snapshot is immutable")
                    if old.get("approval") and old.get("optimization") != state.get(
                        "optimization"
                    ):
                        raise ValueError("approved plan cannot be replaced")
                    if old.get("approval") and any(
                        old.get(key) != state.get(key)
                        for key in ("approval", "execution", "status")
                    ):
                        raise ValueError(
                            "approval/execution transitions require a transaction"
                        )
                db.execute(
                    "INSERT INTO runs VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at",
                    (state["run_id"], encoded, utcnow().isoformat()),
                )
                if event:
                    db.execute(
                        "INSERT OR IGNORE INTO events(event_id,run_id,body) VALUES(?,?,?)",
                        (event.id, event.run_id, event.model_dump_json()),
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def list(self, limit=30):
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT state FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
                )
            ]

    def feedback(self, limit=200):
        from core.schemas.contracts import ExecutionFeedback

        with self.connect() as db:
            return [
                ExecutionFeedback.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT body FROM execution_feedback ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                )
            ]

    def reservations(self, business):
        from core.schemas.contracts import ReservationSnapshot

        def identity(entity):
            return hashlib.sha256(
                json.dumps([entity.source, entity.id, entity.version]).encode()
            ).hexdigest()

        snapshot = ReservationSnapshot()
        with self.connect() as db:
            db.execute("BEGIN")
            for item in business.inventory:
                if db.execute(
                    "SELECT 1 FROM replenishment_commitments WHERE identity=?",
                    (identity(item),),
                ).fetchone():
                    snapshot.inventory_ids.append(item.id)
            for quote in business.supplier_quotes:
                units = db.execute(
                    "SELECT COALESCE(SUM(units),0) FROM quote_commitments WHERE identity=?",
                    (identity(quote),),
                ).fetchone()[0]
                if units:
                    snapshot.quote_units[quote.id] = units
            db.execute("COMMIT")
        return snapshot

    def heartbeat_workflows(self, owner):
        with self.connect() as db:
            db.execute(
                "UPDATE workflow_workers SET heartbeat=? WHERE owner=?",
                (utcnow().isoformat(), owner),
            )

    def recover_interrupted_workflows(self, lease_seconds=120):
        """Fail closed after worker loss; never replay a partly captured graph or ERP."""
        cutoff = (utcnow() - timedelta(seconds=lease_seconds)).isoformat()
        with self.connect() as db:
            rows = db.execute(
                "SELECT r.run_id FROM runs r LEFT JOIN workflow_workers w ON r.run_id=w.run_id WHERE COALESCE(w.heartbeat,r.updated_at)<?",
                (cutoff,),
            ).fetchall()
        recovered = []
        for row in rows:

            def recover(db, state):
                live = db.execute(
                    "SELECT heartbeat FROM workflow_workers WHERE run_id=?",
                    (state["run_id"],),
                ).fetchone()
                updated = db.execute(
                    "SELECT updated_at FROM runs WHERE run_id=?", (state["run_id"],)
                ).fetchone()[0]
                if (live and live[0] >= cutoff) or (not live and updated >= cutoff):
                    return False, None
                if (
                    state.get("approval")
                    or state.get("execution")
                    or state.get("timestamps", {}).get("approval")
                    or state["status"] in {"DRAFT", "FAILED"}
                ):
                    return False, None
                state["status"] = "FAILED"
                state.setdefault("errors", []).append(
                    {"stage": "workflow", "error": "WORKFLOW_LEASE_EXPIRED"}
                )
                event = AuditEvent(
                    run_id=state["run_id"],
                    trace_id=state["trace_id"],
                    stage="workflow",
                    status="FAILED",
                    actor="system",
                    reason="Worker lease expired; partial workflow fenced, create a new run",
                )
                state.setdefault("audit", []).append(event.model_dump(mode="json"))
                db.execute(
                    "DELETE FROM workflow_workers WHERE run_id=?", (state["run_id"],)
                )
                return True, event

            if self.transaction(row[0], recover):
                recovered.append(row[0])
        return recovered

    def append_event(self, event):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(event_id,run_id,body) VALUES(?,?,?)",
                (event.id, event.run_id, event.model_dump_json()),
            )

    def events(self, run_id=None, after=0, limit=200):
        with self.connect() as db:
            rows = db.execute(
                "SELECT sequence,body FROM events WHERE sequence>? AND (? IS NULL OR run_id=?) ORDER BY sequence LIMIT ?",
                (after, run_id, run_id, limit),
            ).fetchall()
        return [{"sequence": r[0], **json.loads(r[1])} for r in rows]

    def transaction(self, run_id, update):
        """Atomically read, authorize and persist a state change plus its event."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT state FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if not row:
                    raise KeyError(run_id)
                state = json.loads(row[0])
                before = json.dumps(state, sort_keys=True, allow_nan=False)
                result, event = update(db, state)
                if json.dumps(state, sort_keys=True, allow_nan=False) != before:
                    db.execute(
                        "UPDATE runs SET state=?,updated_at=? WHERE run_id=?",
                        (
                            json.dumps(state, allow_nan=False),
                            utcnow().isoformat(),
                            run_id,
                        ),
                    )
                if event:
                    db.execute(
                        "INSERT INTO events(event_id,run_id,body) VALUES(?,?,?)",
                        (event.id, run_id, event.model_dump_json()),
                    )
                db.execute("COMMIT")
                return result
            except Exception:
                db.execute("ROLLBACK")
                raise
