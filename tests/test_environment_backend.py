from datetime import timedelta
from fastapi.testclient import TestClient
from pydantic import SecretStr
from config.settings import settings
from core.schemas.contracts import ProviderEnvelope, utcnow
from services.feature_engineering.environment import current_effect, join_environment


def test_current_projection_is_directional():
    aiding = current_effect(1, 0, 90, 10)
    opposing = current_effect(1, 0, 270, 10)
    assert aiding["estimated_ground_speed_knots"] > 10
    assert opposing["estimated_ground_speed_knots"] < 10
    assert current_effect(1, 0, 90)["estimated_ground_speed_knots"] is None


def test_weather_join_rejects_wrong_space_and_stale_time():
    now = utcnow()
    vessel = {
        "mmsi": "test",
        "position": {"latitude": 0, "longitude": 0, "timestamp": now.isoformat()},
        "quality": "VALID",
    }
    ais = ProviderEnvelope(
        source="TEST", entity_id="fleet", quality="VALID", payload={"vessels": [vessel]}
    )
    weather = ProviderEnvelope(
        source="TEST",
        entity_id="weather",
        quality="VALID",
        payload={
            "weather": {
                "far": {
                    "latitude": 45,
                    "longitude": 45,
                    "current": {"time": now.isoformat(), "wind_speed_10m": 80},
                    "current_units": {"wind_speed_10m": "km/h"},
                }
            }
        },
    )
    assert not join_environment({"ais": ais, "weather": weather})[0]["weather"]
    weather.payload["weather"]["far"].update(latitude=0, longitude=0)
    assert (
        abs(
            join_environment({"ais": ais, "weather": weather})[0]["weather"][
                "wind_speed_ms"
            ]
            - 80 / 3.6
        )
        < 1e-9
    )
    weather.payload["weather"]["far"]["current"]["time"] = (
        now - timedelta(days=1)
    ).isoformat()
    assert not join_environment({"ais": ais, "weather": weather})[0]["weather"]


def test_api_role_boundaries_and_schema(store, registry, cluster, monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_API_TOKEN", SecretStr("test-operator"))
    monkeypatch.setattr(settings, "APPROVER_API_TOKEN", SecretStr("test-approver"))
    from dashboard.backend.main import create_app

    client = TestClient(create_app(store, registry, cluster))
    assert client.get("/api/v1/runs").status_code == 401
    assert client.get("/api/v1/contracts/business").status_code == 200
    assert (
        client.post(
            "/api/v1/runs",
            json={"request": {}},
            headers={"Authorization": "Bearer test-approver"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/business/validate",
            json={"demand_units": 100},
            headers={"Authorization": "Bearer test-operator"},
        ).status_code
        == 422
    )
