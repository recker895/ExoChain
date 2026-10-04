"""Explicit test fixtures only. Never imported by application/demo paths."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import timedelta
from types import SimpleNamespace
import pytest
from core.schemas.contracts import (
    BusinessInputs,
    InventoryPosition,
    ProviderEnvelope,
    Quality,
    Supplier,
    SupplierQuote,
    SKU,
    utcnow,
)
from core.state.run_store import RunStore
from orchestrator.data_cluster import DataCluster


@pytest.fixture
def metadata():
    now = utcnow() - timedelta(minutes=1)
    return dict(
        source="TEST_FIXTURE_ONLY",
        created_at=now,
        observed_at=now,
        valid_until=now + timedelta(days=1),
        version="test-1",
        data_quality="VALID",
        provenance=[
            dict(
                source="TEST_FIXTURE_ONLY",
                reference="unit-test-evidence",
                observed_at=now,
            )
        ],
    )


@pytest.fixture
def business(metadata):
    return BusinessInputs(
        skus=[SKU(id="sku-test", unit="units", **metadata)],
        inventory=[
            InventoryPosition(
                id="inv-test",
                sku_id="sku-test",
                location_id="warehouse-test",
                current_inventory_units=10,
                expected_demand_units=20,
                demand_reference="unit-test-evidence",
                horizon_days=7,
                lead_time_days=2,
                safety_stock_units=5,
                service_level=1,
                max_stock_units=50,
                unit_cost_usd=5,
                holding_cost_per_unit_usd=1,
                ordering_cost_usd=2,
                shortage_penalty_usd=50,
                **metadata,
            )
        ],
        suppliers=[
            Supplier(
                id="supplier-test",
                name="Test fixture",
                location_id="origin-test",
                approved=True,
                quality_score=0.9,
                reliability=0.9,
                **metadata,
            )
        ],
        supplier_quotes=[
            SupplierQuote(
                id="quote-test",
                supplier_id="supplier-test",
                sku_id="sku-test",
                location_id="warehouse-test",
                capacity_units=50,
                unit_cost_usd=5,
                lead_time_days=1,
                risk_score=0.2,
                **metadata,
            )
        ],
    )


@pytest.fixture
def registry():
    class Provider:
        cached = None

        def fetch(self):
            return ProviderEnvelope(
                source="TEST_FIXTURE_ONLY",
                entity_id="none",
                quality=Quality.UNAVAILABLE,
                errors=["TEST_PROVIDER_UNAVAILABLE"],
            )

    return SimpleNamespace(
        providers={
            k: Provider() for k in ("ais", "aviation", "weather", "ocean", "ports")
        },
        store=SimpleNamespace(ping=lambda: False),
    )


@pytest.fixture
def cluster(registry):
    return DataCluster(registry)


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs.db")
