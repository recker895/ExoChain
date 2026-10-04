from types import SimpleNamespace
from core.schemas.contracts import (
    ProviderEnvelope,
    DataQualityReport,
    DataSnapshot,
    utcnow,
)
from core.schemas.vessel import VesselState
from services.providers.registry import ProviderRegistry
from services.forecasting.stgnn import prepare_inputs, PREPROCESSING_VERSION


def test_environment_providers_are_bound_to_captured_ais_entities():
    now = utcnow()
    record = VesselState(
        mmsi="captured-test",
        position={"latitude": 1, "longitude": 2, "timestamp": now},
        last_updated=now,
    )
    moving_store = SimpleNamespace(get_active_vessels=lambda limit=None: [])
    registry = ProviderRegistry(moving_store)
    envelope = ProviderEnvelope(
        source="TEST_ONLY",
        entity_id="fleet",
        quality="VALID",
        payload={"vessels": [record.model_dump(mode="json")]},
    )
    scoped = registry.for_ais_snapshot(envelope)
    for name in ("ocean", "weather"):
        captured = scoped[name].loader.__self__.store.get_active_vessels()
        assert [v.mmsi for v in captured] == ["captured-test"]
        assert captured[0].position.latitude == 1


def test_forecast_requires_complete_observed_training_windows():
    import pytest

    with pytest.raises(ValueError, match="complete fresh"):
        prepare_inputs(
            DataSnapshot(quality=DataQualityReport(status="UNAVAILABLE")),
            {
                "preprocessing_version": PREPROCESSING_VERSION,
                "bin_seconds": 60,
                "input_steps": 12,
                "max_nodes": 50,
            },
        )


def test_port_locations_join_by_identity_never_guess_by_name():
    from services.providers.ports import join_port_locations

    records = join_port_locations(
        [
            {"portid": "known", "portname": "TEST"},
            {"portid": "unknown", "portname": "TEST"},
        ],
        {"source": "TEST_ONLY", "ports": {"known": {"latitude": 1, "longitude": 2}}},
    )
    assert records[0]["latitude"] == 1
    assert "latitude" not in records[1]
    assert records[1]["location_status"] == "PORT_LOCATION_UNAVAILABLE"
