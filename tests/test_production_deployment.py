import json
import sqlite3

import pytest
from pydantic import SecretStr, ValidationError
from fastapi.testclient import TestClient

from config.settings import Settings, settings, environment_values
from core.state.run_store import RunStore
from dashboard.backend.main import create_app
from scripts.backup_state import backup, restore
from services.health import HealthService
from services.providers.business import (
    JSONBusinessProvider,
    fetch_business,
    production_source_errors,
)


def test_production_rejects_mock_and_unattested(monkeypatch, tmp_path, business):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "BUSINESS_SOURCE_TRUST", "UNVERIFIED")
    assert production_source_errors(business) == ["BUSINESS_SOURCE_NOT_AUTHORITATIVE"]
    monkeypatch.setattr(settings, "BUSINESS_SOURCE_TRUST", "AUTHORITATIVE")
    monkeypatch.setattr(settings, "BUSINESS_SOURCE_ID", "ERP-PURCHASING")
    assert production_source_errors(business) == ["BUSINESS_MOCK_SOURCE_FORBIDDEN"]
    path = tmp_path / "mock_replenishment.json"
    path.write_text(business.model_dump_json())
    result = fetch_business(JSONBusinessProvider(path))
    assert result.quality.value == "INVALID" and result.payload is None


def test_production_settings_fail_closed():
    options = dict(
        ENVIRONMENT="production",
        OPERATOR_API_TOKEN="a" * 32,
        APPROVER_API_TOKEN="b" * 32,
        CORS_ORIGINS="https://engine.example.com",
        TRUSTED_HOSTS="engine.example.com",
    )
    assert Settings(**options).ENVIRONMENT == "production"
    for override in (
        {"ERP_MODE": "simulation"},
        {"TRUSTED_HOSTS": "*"},
        {"CORS_ORIGINS": "http://engine.example.com"},
        {"APPROVER_API_TOKEN": "a" * 32},
    ):
        with pytest.raises(ValidationError):
            Settings(**(options | override))


def test_file_secrets_and_conflicts(monkeypatch, tmp_path):
    path = tmp_path / "secret"
    path.write_text("secret-value\n")
    monkeypatch.delenv("ERP_TOKEN", raising=False)
    monkeypatch.setenv("ERP_TOKEN_FILE", str(path))
    assert environment_values()["ERP_TOKEN"] == "secret-value"
    monkeypatch.setenv("ERP_TOKEN", "inline")
    with pytest.raises(ValueError, match="Configure either"):
        environment_values()


def test_backup_restores_execution_identity(tmp_path):
    store = RunStore(tmp_path / "original.db")
    with store.connect() as db:
        db.execute(
            "INSERT INTO executions VALUES(?,?,?,?,?)",
            ("key", "run", "hash", "UNKNOWN", json.dumps({"status": "UNKNOWN"})),
        )
        db.execute("INSERT INTO execution_commands VALUES(?,?)", ("key", "{}"))
    backup({"decision_runs": store.path}, tmp_path / "backup")
    restore(tmp_path / "backup", tmp_path / "restored")
    with sqlite3.connect(tmp_path / "restored/decision_runs.db") as db:
        assert db.execute(
            "SELECT idempotency_key,plan_hash,status FROM executions"
        ).fetchone() == ("key", "hash", "UNKNOWN")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    with pytest.raises(FileExistsError):
        restore(tmp_path / "backup", tmp_path / "restored")
    (tmp_path / "backup/decision_runs.db").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        restore(tmp_path / "backup", tmp_path / "invalid")


def test_newer_schema_rejected(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="newer"):
        RunStore(path)


def test_readiness_unconfigured_is_not_ready(store, registry, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "BUSINESS_DATA_PATH", "")
    monkeypatch.setattr(settings, "BUSINESS_API_URL", "")
    health = HealthService(registry, store)
    monkeypatch.setattr(
        health,
        "check",
        lambda: {
            "dependencies": dict.fromkeys(
                ("database", "optimization", "redis", "kafka"), "HEALTHY"
            )
            | {"erp": "UNAVAILABLE"}
        },
    )
    result = health.readiness()
    assert result["status"] == "NOT_READY"
    assert result["dependencies"]["business"] == "UNAVAILABLE"


def test_production_api_requires_reader_and_trusted_host(
    store, registry, cluster, monkeypatch
):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr("operator-test"))
    monkeypatch.setattr(settings, "TRUSTED_HOSTS", "testserver")
    client = TestClient(create_app(store, registry, cluster))
    assert client.get("/livez").status_code == 200
    assert client.get("/api/v1/telemetry").status_code == 401
    assert (
        client.get(
            "/api/v1/telemetry", headers={"Authorization": "Bearer operator-test"}
        ).status_code
        == 200
    )
    assert client.get("/livez", headers={"Host": "attacker.invalid"}).status_code == 400
    monkeypatch.setattr(settings, "CORS_ORIGINS", "https://dashboard.example.com")
    cors = TestClient(create_app(store, registry, cluster))
    assert (
        cors.options(
            "/api/v1/telemetry",
            headers={
                "Origin": "https://dashboard.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        ).status_code
        == 200
    )


def test_json_source_mutation_during_capture(tmp_path, business, monkeypatch):
    path = tmp_path / "source.json"
    path.write_text(business.model_dump_json())
    original = type(path).read_text

    def mutated(target, *args, **kwargs):
        content = original(target, *args, **kwargs)
        target.write_text(content + " ")
        return content

    monkeypatch.setattr(type(path), "read_text", mutated)
    result = fetch_business(JSONBusinessProvider(path))
    assert result.quality.value == "UNAVAILABLE" and result.payload is None


def test_weather_collection_deadline_is_explicit(monkeypatch):
    from types import SimpleNamespace
    import agents.data.weather_agent as module

    clock = [0.0]
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(settings, "PROVIDER_TIMEOUT_SECONDS", 20)
    agent = module.WeatherDataAgent()

    def fetch(points):
        clock[0] += 21
        return [{"current": {"time": "2026-09-30T00:00"}} for _ in points]

    monkeypatch.setattr(agent, "_fetch_batch", fetch)
    vessels = [
        SimpleNamespace(
            mmsi=str(i), position=SimpleNamespace(latitude=i / 4, longitude=0)
        )
        for i in range(100)
    ]
    result = agent.execute(SimpleNamespace(get_active_vessels=lambda: vessels))
    assert result["grid_points_requested"] == 100
    assert result["grid_points_received"] == 50
    assert "WEATHER_COLLECTION_DEADLINE_EXCEEDED" in result["errors"]
