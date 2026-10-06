"""Real telemetry adapters with bounded execution, caching and failure isolation."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from config.settings import settings
from core.schemas.contracts import ProviderEnvelope, Quality, utcnow
from core.schemas.vessel import VesselState
from core.state.state_store import StateStore


class Provider(Protocol):
    def fetch(self) -> ProviderEnvelope: ...


def timestamp(value):
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(
            value / 1000 if value > 1e11 else value, timezone.utc
        )
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return value


class CachedProvider:
    def __init__(self, source: str, loader: Callable, ttl: int):
        self.source, self.loader, self.ttl = source, loader, ttl
        self.lock = threading.Lock()
        self.cached = None
        self.last_result = None
        self.last_fetch = 0.0
        self.failures = 0
        self.retry_at = 0.0

    def fetch(self) -> ProviderEnvelope:
        with self.lock:
            start = time.perf_counter()
            now = time.monotonic()
            if self.cached and now - self.last_fetch < self.ttl:
                value = self.cached.model_copy(deep=True)
                if value.valid_until and value.valid_until < utcnow():
                    value.quality = Quality.STALE
                return value
            if now < self.retry_at:
                return self._unavailable("CIRCUIT_OPEN")
            try:
                value = self.loader()
                if value.valid_until and value.valid_until < utcnow():
                    value.quality = Quality.STALE
                if value.quality in {Quality.UNAVAILABLE, Quality.INVALID}:
                    self.failures += 1
                    self.retry_at = now + min(300, 2 ** min(self.failures, 8))
                else:
                    self.failures = 0
                    self.cached, self.last_fetch = value, now
                value.latency_ms = (time.perf_counter() - start) * 1000
                self.last_result = value
                return value
            except Exception as exc:
                # Exception text can include credential-bearing URLs; expose type only.
                self.failures += 1
                self.retry_at = now + min(300, 2 ** min(self.failures, 8))
                result = self._unavailable(f"PROVIDER_ERROR:{type(exc).__name__}")
                result.latency_ms = (time.perf_counter() - start) * 1000
                self.last_result = result
                return result

    def _unavailable(self, reason):
        if self.cached:
            value = self.cached.model_copy(deep=True)
            value.quality = Quality.STALE
            value.last_known_data = True
            value.errors = [reason]
            value.impact = "Cached evidence cannot authorize a new action until freshness is validated."
            return value
        return ProviderEnvelope(
            source=self.source,
            entity_id="fleet",
            quality=Quality.UNAVAILABLE,
            errors=[reason],
        )


class ProviderRegistry:
    def __init__(self, store=None):
        self.store = store or StateStore()
        self.environment_cache = {}
        self.environment_lock = threading.Lock()
        self.providers = {
            "ais": CachedProvider("AISStream/Kafka/Redis", self.ais, 5),
            "aviation": CachedProvider("OpenSky", self.aviation, 60),
            "weather": CachedProvider("Open-Meteo", self.weather, 600),
            "ocean": CachedProvider("Copernicus Marine", self.ocean, 1800),
            "ports": CachedProvider("IMF PortWatch", self.ports, 3600),
        }

    def for_ais_snapshot(self, envelope):
        """Environmental requests use the exact captured entities, never a moving fleet query."""
        records = (envelope.payload or {}).get("vessels", [])
        vessels = [VesselState.model_validate(v) for v in records]
        signature = hashlib.sha256(
            json.dumps(
                [
                    (
                        v.mmsi,
                        round(v.position.latitude, 3),
                        round(v.position.longitude, 3),
                    )
                    for v in vessels
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with self.environment_lock:
            if signature in self.environment_cache:
                return self.environment_cache[signature]
            live_store = self.store

            class SnapshotStore:
                def get_active_vessels(self, limit=None):
                    return vessels[:limit]

                def __getattr__(self, name):
                    return getattr(live_store, name)

            scoped = ProviderRegistry(SnapshotStore())
            result = {name: scoped.providers[name] for name in ("weather", "ocean")}
            # The bounded cache retains failures/circuit state per immutable fleet
            # and source. Copernicus tiles remain shared in the real Redis cache.
            if len(self.environment_cache) >= 16:
                self.environment_cache.pop(next(iter(self.environment_cache)))
            self.environment_cache[signature] = result
            return result

    def vessels(self):
        return self.store.get_active_vessels(limit=settings.TELEMETRY_MAX_VESSELS)

    def selected_vessel(self, reference):
        """Read the exact MMSI from Redis, independently of the displayed fleet limit."""
        source = "AISStream/Kafka/Redis"
        try:
            vessel = self.store.get_vessel(reference.mmsi)
        except Exception as exc:
            return ProviderEnvelope(
                source=source, entity_id=reference.mmsi,
                quality=Quality.UNAVAILABLE,
                errors=[f"SELECTED_VESSEL_LOOKUP_ERROR:{type(exc).__name__}"],
            )
        if vessel is None or vessel.mmsi != reference.mmsi:
            return ProviderEnvelope(
                source=source, entity_id=reference.mmsi,
                quality=Quality.UNAVAILABLE,
                errors=["SELECTED_VESSEL_NOT_OBSERVED"],
            )
        observed = vessel.last_updated
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        valid_until = observed + timedelta(seconds=settings.AIS_MAX_AGE_SECONDS)
        name_status = "UNVERIFIED"
        if reference.vessel_name and vessel.vessel_name:
            name_status = (
                "MATCH" if reference.vessel_name.strip().casefold()
                == vessel.vessel_name.strip().casefold() else "MISMATCH"
            )
        quality = (
            Quality.STALE if observed > utcnow() or valid_until < utcnow()
            else Quality.PARTIAL if name_status == "MISMATCH" or vessel.data_quality < 0.5
            else Quality.VALID
        )
        return ProviderEnvelope(
            source=source, entity_id=reference.mmsi,
            observed_at=observed, valid_until=valid_until, quality=quality,
            payload={
                "vessel": vessel.model_dump(mode="json"),
                "reference": reference.model_dump(mode="json"),
                "identity_checks": {
                    "mmsi": "MATCH", "name": name_status,
                    "imo": "NOT_VERIFIED_BY_AIS" if reference.imo else "NOT_SUPPLIED",
                },
            },
            errors=["VESSEL_NAME_MISMATCH"] if name_status == "MISMATCH" else [],
        )

    def ais(self):
        vessels = self.vessels()
        now = utcnow()
        data = []
        for vessel in vessels:
            record = vessel.model_dump(mode="json")
            record["age_seconds"] = max(0, (now - vessel.last_updated).total_seconds())
            record["quality"] = (
                "STALE"
                if record["age_seconds"] > settings.AIS_MAX_AGE_SECONDS
                else "VALID"
            )
            data.append(record)
        oldest = min((v.last_updated for v in vessels), default=None)
        quality = (
            Quality.UNAVAILABLE
            if not vessels
            else Quality.VALID
            if all(v["quality"] == "VALID" for v in data)
            else Quality.STALE
        )
        return ProviderEnvelope(
            source="AISStream/Kafka/Redis",
            entity_id="fleet",
            observed_at=oldest,
            valid_until=oldest + timedelta(seconds=settings.AIS_MAX_AGE_SECONDS)
            if oldest
            else None,
            quality=quality,
            payload={"vessels": data, "sample_limit": settings.TELEMETRY_MAX_VESSELS},
            errors=[] if vessels else ["AIS_UNAVAILABLE"],
        )

    def context(self):
        registry = self

        class LegacyContext:
            state_store = registry.store
            config = {"max_vessels": settings.TELEMETRY_MAX_VESSELS}

            def get_active_vessels(self, limit=None):
                return registry.store.get_active_vessels(
                    limit=limit or settings.TELEMETRY_MAX_VESSELS
                )

        return LegacyContext()

    def aviation(self):
        from agents.data.aviation_agent import AviationDataAgent
        from agents.base import AgentContext

        output = AviationDataAgent().run(AgentContext())
        aircraft = output["aircraft"]
        observed = min(
            (timestamp(x["last_contact"]) for x in aircraft if x.get("last_contact")),
            default=None,
        )
        return ProviderEnvelope(
            source="OpenSky",
            entity_id="aircraft",
            observed_at=observed,
            valid_until=observed + timedelta(minutes=5) if observed else None,
            quality=Quality.VALID if aircraft else Quality.UNAVAILABLE,
            payload=output,
        )

    def weather(self):
        from agents.data.weather_agent import WeatherDataAgent

        output = WeatherDataAgent().execute(self.context())
        points = output.get("weather", {})
        times = [
            timestamp(p["current"]["time"])
            for p in points.values()
            if p.get("current", {}).get("time")
        ]
        observed = min(times, default=None)
        quality = (
            Quality.VALID
            if observed and not output.get("errors")
            else Quality.PARTIAL
            if observed
            else Quality.UNAVAILABLE
        )
        return ProviderEnvelope(
            source="Open-Meteo",
            entity_id="weather-grids",
            observed_at=observed,
            valid_until=observed + timedelta(seconds=settings.WEATHER_MAX_AGE_SECONDS)
            if observed
            else None,
            quality=quality,
            payload={"weather": points},
            errors=["WEATHER_BATCH_FAILED"] if output.get("errors") else [],
        )

    def ocean(self):
        from agents.data.ocean_current_agent import OceanCurrentDataAgent

        output = OceanCurrentDataAgent().execute(self.context())
        observed = timestamp(output["date"])
        return ProviderEnvelope(
            source="Copernicus Marine",
            entity_id="ocean-grids",
            observed_at=observed,
            valid_until=observed + timedelta(seconds=settings.OCEAN_MAX_AGE_SECONDS),
            quality=Quality.VALID
            if output["vessels_with_current"] == output["vessel_count"]
            and output["vessel_count"] > 0
            and not output["errors"]
            else Quality.PARTIAL
            if output["vessels_with_current"]
            else Quality.UNAVAILABLE,
            payload={k: v for k, v in output.items() if k != "errors"},
            errors=["OCEAN_TILE_FAILED"] if output["errors"] else [],
        )

    def ports(self):
        from agents.data.port_infrastructure_agent import PortInfrastructureDataAgent
        from agents.base import AgentContext

        output = PortInfrastructureDataAgent().run(AgentContext())
        location_error = False
        if output.get("ports"):
            from services.providers.ports import port_locations, join_port_locations

            try:
                output["ports"] = join_port_locations(
                    output["ports"], port_locations(self.store)
                )
            except Exception:
                location_error = True
        observed = timestamp(output.get("date"))
        return ProviderEnvelope(
            source="IMF PortWatch",
            entity_id="ports",
            observed_at=observed,
            valid_until=observed + timedelta(seconds=settings.PORT_MAX_AGE_SECONDS)
            if observed
            else None,
            quality=Quality.PARTIAL
            if location_error
            else Quality.VALID
            if output.get("port_count")
            else Quality.UNAVAILABLE,
            payload={k: v for k, v in output.items() if k != "error"},
            errors=["PORT_LOCATIONS_UNAVAILABLE"]
            if location_error
            else []
            if output.get("port_count")
            else ["PORT_DATA_UNAVAILABLE"],
        )

    def route_ports(self, locodes):
        """Bounded real PortWatch lookup of just the requested endpoints."""
        from agents.data.port_infrastructure_agent import PortInfrastructureDataAgent
        from services.providers.ports import port_locations, join_port_locations
        agent = PortInfrastructureDataAgent()
        locations = port_locations(self.store)
        ids = [key for key, value in locations["ports"].items()
               if str(value.get("locode", "")).upper() in set(locodes)]
        if not ids:
            return ProviderEnvelope(source="IMF PortWatch", entity_id="route-ports",
                quality=Quality.UNAVAILABLE, errors=["PORTWATCH_ENDPOINT_IDS_UNAVAILABLE"])
        date = agent._latest_date()
        quoted = ",".join("'" + str(key).replace("'", "''") + "'" for key in ids)
        payload = agent._query({"where": f"date = DATE '{date}' AND portid IN ({quoted})",
            "outFields": "*", "returnGeometry": "false", "resultRecordCount": 1000, "f": "json"})
        records = [item["attributes"] for item in payload.get("features", [])]
        observed = timestamp(date)
        expires = observed + timedelta(seconds=settings.PORT_MAX_AGE_SECONDS) if observed else None
        return ProviderEnvelope(source="IMF PortWatch", entity_id="route-ports",
            observed_at=observed, valid_until=expires,
            quality=Quality.STALE if records and expires and expires < utcnow()
            else Quality.VALID if records else Quality.UNAVAILABLE,
            payload={"ports": join_port_locations(records, locations), "date": date,
                     "port_count": len(records), "requested_locodes": locodes},
            errors=[] if records else ["PORTWATCH_ENDPOINT_ACTIVITY_UNAVAILABLE"])

    def snapshot(self):
        with ThreadPoolExecutor(max_workers=settings.PROVIDER_WORKERS) as pool:
            values = list(
                pool.map(
                    lambda pair: (pair[0], pair[1].fetch()), self.providers.items()
                )
            )
        result = dict(values)
        result["waves"] = ProviderEnvelope(
            source="unconfigured",
            entity_id="waves",
            quality=Quality.UNAVAILABLE,
            errors=["WAVE_DATA_UNAVAILABLE"],
        )
        return result
