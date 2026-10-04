from concurrent.futures import ThreadPoolExecutor
from core.schemas.contracts import (
    BusinessInputs,
    DataSnapshot,
    DataQualityReport,
    ProviderEnvelope,
    Quality,
    VesselReference,
)
from agents.base.runtime import execute_agent
from services.providers.business import (
    configured_business_provider,
    envelope,
    fetch_business,
    snapshot_identity,
)
from services.providers.registry import ProviderRegistry
from services.feature_engineering.environment import join_environment


class DataCluster:
    def __init__(self, registry=None, business_provider=None):
        self.registry = registry or ProviderRegistry()
        self.business_provider = business_provider or configured_business_provider()

    def execute(
        self, business: BusinessInputs | None = None, observer=None,
        vessel_reference: VesselReference | None = None,
    ) -> DataSnapshot:
        ais, ais_record = execute_agent(
            "data.maritime",
            "DATA",
            lambda: self.registry.providers["ais"].fetch(),
            "AISStream/Kafka/Redis",
            observer=observer,
        )
        ais = ais or ProviderEnvelope(
            source="AISStream/Kafka/Redis",
            entity_id="fleet",
            quality=Quality.UNAVAILABLE,
            errors=ais_record.errors,
        )
        selected_vessel = None
        if vessel_reference is not None:
            lookup = getattr(self.registry, "selected_vessel", None)
            selected_vessel = lookup(vessel_reference) if lookup else ProviderEnvelope(
                source="AISStream/Kafka/Redis", entity_id=vessel_reference.mmsi,
                quality=Quality.UNAVAILABLE,
                errors=["SELECTED_VESSEL_LOOKUP_UNAVAILABLE"],
            )
            record = (selected_vessel.payload or {}).get("vessel")
            if record is not None:
                ais = ais.model_copy(deep=True)
                payload = dict(ais.payload or {})
                vessels = list(payload.get("vessels") or [])
                vessels = [v for v in vessels if str(v.get("mmsi")) != vessel_reference.mmsi]
                vessels.append(record)
                payload["vessels"] = vessels
                ais.payload = payload
                if ais.quality == Quality.UNAVAILABLE:
                    ais.quality = Quality.PARTIAL
        scoped = (
            self.registry.for_ais_snapshot(ais)
            if hasattr(self.registry, "for_ais_snapshot")
            else self.registry.providers
        )

        def environment():
            with ThreadPoolExecutor(max_workers=3) as pool:
                sources = dict(
                    zip(
                        ("weather", "ocean"),
                        pool.map(
                            lambda k: scoped[k].fetch(),
                            ("weather", "ocean"),
                        ),
                    )
                )
            for name, value in sources.items():
                self.registry.providers[name].cached = value
                self.registry.providers[name].last_result = value
            sources["waves"] = ProviderEnvelope(
                source="unconfigured",
                entity_id="waves",
                quality=Quality.UNAVAILABLE,
                errors=["WAVE_DATA_UNAVAILABLE"],
            )
            return {
                "status": "PARTIAL"
                if any(v.quality != Quality.VALID for v in sources.values())
                else "SUCCESS",
                "sources": {k: v.model_dump(mode="json") for k, v in sources.items()},
            }

        work = {
            "aviation": lambda: self.registry.providers["aviation"].fetch(),
            "environment": environment,
            "ports": lambda: self.registry.providers["ports"].fetch(),
            "business": lambda: (
                envelope(business, "request-import")
                if business is not None
                else fetch_business(self.business_provider)
            ),
        }
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(
                pool.map(
                    lambda item: (
                        item[0],
                        execute_agent(
                            f"data.{item[0]}",
                            "DATA",
                            item[1],
                            item[0],
                            observer=observer,
                        ),
                    ),
                    work.items(),
                )
            )
        sources, executions = {"ais": ais}, [ais_record]
        if selected_vessel is not None:
            sources["selected_vessel"] = selected_vessel
        actual = business or BusinessInputs()
        for name, (value, record) in outcomes:
            executions.append(record)
            if name == "environment" and value:
                sources.update(
                    {
                        k: ProviderEnvelope.model_validate(v)
                        for k, v in value["sources"].items()
                    }
                )
            else:
                key = "ais" if name == "maritime" else name
                sources[key] = value or ProviderEnvelope(
                    source=name,
                    entity_id=name,
                    quality=Quality.UNAVAILABLE,
                    errors=record.errors,
                )
                if name == "business" and value and value.payload is not None:
                    actual = BusinessInputs.model_validate(value.payload)
        market, record = execute_agent(
            "data.market",
            "DATA",
            lambda: envelope(
                BusinessInputs(disruptions=actual.disruptions),
                "business-event-provider",
            ),
            "business",
            observer=observer,
        )
        executions.append(record)
        sources["market"] = market or ProviderEnvelope(
            source="business-event-provider",
            entity_id="market",
            quality=Quality.UNAVAILABLE,
            errors=record.errors,
        )
        missing = [
            k
            for k, v in sources.items()
            if v.quality in {Quality.UNAVAILABLE, Quality.INVALID}
        ]
        stale = [k for k, v in sources.items() if v.quality == Quality.STALE]
        enriched = join_environment(sources)
        # The immutable enriched vessel records retain original position/history.
        # Reference them instead of triplicating every AIS history in the run.
        sources["ais"] = sources["ais"].model_copy(deep=True)
        if sources["ais"].payload:
            sources["ais"].payload = {
                "vessel_ids": [v["mmsi"] for v in enriched],
                "vessels_reference": "data.vessels",
                "sample_limit": sources["ais"].payload.get("sample_limit"),
            }
        return DataSnapshot(
            sources=sources,
            vessels=enriched,
            business=actual,
            business_identity=snapshot_identity(
                actual,
                sources["business"].source,
                self.business_provider if business is None else None,
            ),
            quality=DataQualityReport(
                status=Quality.PARTIAL if missing or stale else Quality.VALID,
                missing=missing,
                stale=stale,
                sources=[v.source for v in sources.values()],
            ),
            executions=executions,
        )


data_cluster = DataCluster()
