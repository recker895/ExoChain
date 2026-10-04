from datetime import timedelta

import pytest
from pydantic import ValidationError

from core.schemas.contracts import (
    BusinessInputs, DataQualityReport, DataSnapshot, ProviderEnvelope,
    Quality, RunRequest, VesselReference, utcnow,
)
from core.schemas.vessel import VesselState
from services.providers.registry import ProviderRegistry
from services.voyage_assessment import assess_voyage
from orchestrator.data_cluster import DataCluster
from tests.test_voyage_assessment import scenario


def reference():
    return VesselReference(
        shipment_id="s", mmsi="563275100", imo="9502946",
        vessel_name="MAERSK ENSHI",
    )


def observed_vessel(*, age_minutes=1, name="MAERSK ENSHI"):
    at = utcnow() - timedelta(minutes=age_minutes)
    return VesselState(
        mmsi="563275100", vessel_name=name,
        position={"latitude": 1.2, "longitude": 103.8, "timestamp": at},
        last_updated=at, source="TEST_AIS_OBSERVATION",
        provenance=[{"source": "TEST_AIS_OBSERVATION", "reference": "563275100", "observed_at": at.isoformat()}],
    )


def test_reference_requires_exact_single_transport_shipment():
    with pytest.raises(ValidationError):
        RunRequest(operation="ASSESSMENT", vessel_reference=reference())
    with pytest.raises(ValidationError):
        RunRequest(operation="TRANSPORT", shipment_ids=["other"], vessel_reference=reference())
    with pytest.raises(ValidationError):
        VesselReference(shipment_id="s", mmsi="123", imo="9502946")


def test_exact_redis_lookup_preserves_freshness_and_name_check():
    class Store:
        def __init__(self, vessel):
            self.vessel = vessel

        def get_vessel(self, mmsi):
            assert mmsi == "563275100"
            return self.vessel

    registry = ProviderRegistry(Store(observed_vessel()))
    result = registry.selected_vessel(reference())
    assert result.quality == Quality.VALID
    assert result.payload["identity_checks"] == {
        "mmsi": "MATCH", "name": "MATCH", "imo": "NOT_VERIFIED_BY_AIS",
    }
    registry.store = Store(observed_vessel(age_minutes=90))
    assert registry.selected_vessel(reference()).quality == Quality.STALE
    registry.store = Store(observed_vessel(name="ANOTHER SHIP"))
    assert registry.selected_vessel(reference()).quality == Quality.PARTIAL
    registry.store = Store(None)
    assert registry.selected_vessel(reference()).quality == Quality.UNAVAILABLE


def test_voyage_assessment_uses_only_captured_selected_vessel():
    route, shipment, network, _ = scenario()
    shipment.vessel_id = None
    registry = ProviderRegistry(type("Store", (), {"get_vessel": lambda self, mmsi: observed_vessel()})())
    selected = registry.selected_vessel(reference())
    data = DataSnapshot(
        sources={"selected_vessel": selected},
        quality=DataQualityReport(status=Quality.PARTIAL),
        business=BusinessInputs(shipments=[shipment], route_network=network),
    )
    result = assess_voyage(route, shipment, network, data,
                           fetch_forecasts=False, vessel_reference=reference())
    assert result["vessel_id"] == "563275100"
    assert result["summary"]["vessel_state"] == "VALID"
    assert result["vessel_state"]["position"]["latitude"] == 1.2
    assert result["vessel_state"]["identity_checks"]["imo"] == "NOT_VERIFIED_BY_AIS"
    assert result["summary"]["fuel"] == "UNAVAILABLE"
    assert result["summary"]["draft"] == "DATA_REQUIRED"
    shipment.vessel_id = "999999999"
    mismatch = assess_voyage(route, shipment, network, data,
                             fetch_forecasts=False, vessel_reference=reference())
    assert mismatch["vessel_state"]["reason"] == "VESSEL_SHIPMENT_IDENTITY_MISMATCH"
    assert mismatch["summary"]["fuel"] == "UNAVAILABLE"


def test_selected_vessel_enters_immutable_snapshot_even_when_fleet_sample_empty(registry):
    vessel = observed_vessel()
    selected = ProviderEnvelope(
        source="TEST_AIS_OBSERVATION", entity_id=vessel.mmsi,
        observed_at=vessel.last_updated,
        valid_until=vessel.last_updated + timedelta(minutes=30),
        quality=Quality.VALID,
        payload={"vessel": vessel.model_dump(mode="json"),
                 "identity_checks": {"mmsi": "MATCH", "imo": "NOT_VERIFIED_BY_AIS"}},
    )
    registry.selected_vessel = lambda requested: selected
    snapshot = DataCluster(registry).execute(
        BusinessInputs(), vessel_reference=reference(),
    )
    assert [v["mmsi"] for v in snapshot.vessels] == ["563275100"]
    assert snapshot.sources["selected_vessel"].quality == Quality.VALID
    assert snapshot.sources["ais"].payload["vessel_ids"] == ["563275100"]
