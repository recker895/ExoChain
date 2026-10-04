import json
import pytest
from pydantic import ValidationError
from core.schemas.contracts import (
    BusinessInputs,
    ObjectiveWeights,
    Quality,
    RunRequest,
    SupplierQuote,
)
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.providers.business import JSONBusinessProvider, CSVBusinessProvider


def test_missing_inputs_block_and_19_agents_are_real_records(cluster, store):
    state = initial_state(RunRequest(operation="TRANSPORT", budget_usd=1000))
    result = build_exochain_graph(store, cluster).invoke(state)
    assert result["status"] == "BLOCKED"
    assert result["approval"] is None and result["execution"] is None
    assert (
        sum(len(result[k]["executions"]) for k in ("data", "intelligence", "decisions"))
        == 19
    )
    assert result["run_id"] == state["run_id"] and result["request"] == state["request"]
    assert json.loads(json.dumps(result)) == result
    assert store.get(state["run_id"]) == result
    assert (
        result["intelligence"]["results"]["forecast"]["status"] == "MODEL_UNAVAILABLE"
    )


def test_schema_requires_business_facts(metadata):
    with pytest.raises(ValidationError):
        SupplierQuote(
            id="x", supplier_id="s", sku_id="sku", location_id="w", **metadata
        )
    with pytest.raises(ValidationError):
        ObjectiveWeights(cost=0, time=0, fuel=0, risk=0, weather=0, current=0)
    with pytest.raises(ValidationError):
        RunRequest(budget_usd=float("nan"))
    with pytest.raises(ValidationError):
        BusinessInputs.model_validate({"demand_units": 100})


def test_json_import_preserves_sources(tmp_path, business):
    path = tmp_path / "business.json"
    path.write_text(business.model_dump_json())
    envelope = JSONBusinessProvider(path).fetch()
    assert envelope.payload == business
    assert envelope.payload.inventory[0].source == "TEST_FIXTURE_ONLY"


def test_empty_csv_import_does_not_manufacture_data(tmp_path):
    envelope = CSVBusinessProvider(tmp_path).fetch()
    assert envelope.quality == Quality.UNAVAILABLE
    assert not envelope.payload.inventory


def test_immutable_snapshot_rejects_mutation(cluster, store, business):
    result = build_exochain_graph(store, cluster).invoke(
        initial_state(
            RunRequest(
                operation="REPLENISHMENT", inventory_ids=["inv-test"], budget_usd=1000
            ),
            business,
        )
    )
    result["data"]["business"]["inventory"][0]["current_inventory_units"] = 999
    with pytest.raises(ValueError):
        store.save(result)
