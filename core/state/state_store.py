from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis

from core.schemas.vessel import VesselState
from config.settings import settings
from core.schemas.intelligence import IntelligenceResult
from core.schemas.decision import DecisionProposal


def optional_navigation(value, limit):
    try:
        number = float(value)
        return number if 0 <= number < limit else None
    except (TypeError, ValueError):
        return None


class StateStore:
    """
    Redis-backed state store for ExoChain.

    Responsibilities
    ----------------
    1. Current vessel state
    2. Vessel temporal history
    3. Intelligence results
    4. Decision proposals
    5. Ocean-current tile cache

    Redis is used as the operational state layer.
    """

    def __init__(
        self,
        redis_host: Optional[str] = None,
        redis_port: Optional[int] = None,
        redis_db: Optional[int] = None,
    ) -> None:

        self.redis_host = redis_host or os.getenv(
            "REDIS_HOST",
            "localhost",
        )

        self.redis_port = int(
            redis_port
            if redis_port is not None
            else os.getenv(
                "REDIS_PORT",
                "6379",
            )
        )

        self.redis_db = int(
            redis_db
            if redis_db is not None
            else os.getenv(
                "REDIS_DB",
                "0",
            )
        )

        self.redis = redis.Redis(
            host=self.redis_host,
            port=self.redis_port,
            db=self.redis_db,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )

    # ============================================================
    # REDIS CONNECTION
    # ============================================================

    def ping(self) -> bool:
        """Check whether Redis is reachable."""

        try:
            return bool(self.redis.ping())

        except Exception:
            return False

    def close(self) -> None:
        """Close the Redis connection."""

        try:
            self.redis.close()

        except Exception:
            pass

    # ============================================================
    # KEY HELPERS
    # ============================================================

    @staticmethod
    def _vessel_state_key(
        mmsi: str,
    ) -> str:
        return f"exochain:vessel:{mmsi}:state"

    @staticmethod
    def _vessel_history_key(
        mmsi: str,
    ) -> str:
        return f"exochain:vessel:{mmsi}:history"

    @staticmethod
    def _intelligence_key(
        agent_name: str,
        entity_id: str,
    ) -> str:
        return f"exochain:intelligence:{agent_name}:{entity_id}"

    @staticmethod
    def _decision_key(
        agent_name: str,
        entity_id: str,
    ) -> str:
        return f"exochain:decision:{agent_name}:{entity_id}"

    # ============================================================
    # TIMESTAMP
    # ============================================================

    @staticmethod
    def _parse_timestamp(
        value: Any,
    ) -> datetime:
        """
        Convert Redis timestamp representation into
        timezone-aware UTC datetime.

        Supports:
        - Unix timestamps
        - ISO-8601 strings
        - datetime objects
        """

        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)

            return value

        if value is None:
            raise ValueError("missing or invalid observation timestamp")

        value = str(value).strip()

        if not value:
            raise ValueError("missing or invalid observation timestamp")

        # --------------------------------------------------------
        # Unix timestamp
        # --------------------------------------------------------

        try:
            return datetime.fromtimestamp(
                float(value),
                tz=timezone.utc,
            )

        except (
            ValueError,
            TypeError,
            OverflowError,
        ):
            pass

        # --------------------------------------------------------
        # ISO-8601 timestamp
        # --------------------------------------------------------

        try:
            parsed = datetime.fromisoformat(
                value.replace(
                    "Z",
                    "+00:00",
                )
            )

            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)

            return parsed

        except ValueError:
            raise ValueError("missing or invalid observation timestamp")

    # ============================================================
    # VESSEL STATE
    # ============================================================

    def get_vessel(
        self,
        mmsi: str,
    ) -> Optional[VesselState]:

        key = self._vessel_state_key(mmsi)

        data = self.redis.hgetall(key)

        if not data:
            return None

        try:
            timestamp = self._parse_timestamp(data.get("timestamp"))

            def decoded(name, default):
                value = data.get(name)
                if not value:
                    return default
                try:
                    return json.loads(value)
                except (TypeError, ValueError):
                    return default

            vessel = VesselState(
                mmsi=str(
                    data.get(
                        "mmsi",
                        mmsi,
                    )
                ),
                vessel_name=data.get(
                    "vessel_name",
                    "",
                ),
                position={
                    "latitude": float(data["lat"]),
                    "longitude": float(data["lon"]),
                    "speed_knots": optional_navigation(data.get("speed_knots"), 102.3),
                    "course_over_ground": optional_navigation(
                        data.get("course_over_ground"), 360
                    ),
                    "heading": optional_navigation(data.get("heading"), 360),
                    "timestamp": timestamp,
                },
                last_updated=timestamp,
                destination=data.get("destination") or None,
                eta=data.get("eta") or None,
                characteristics=decoded("characteristics", {}),
                weather=decoded("weather", {}),
                ocean=decoded("ocean", {}),
                waves=decoded("waves", {}),
                provenance=decoded("provenance", []),
                data_quality=min(float(data.get("data_quality", 1)), 0.4)
                if (datetime.now(timezone.utc) - timestamp).total_seconds()
                > settings.AIS_MAX_AGE_SECONDS
                else float(data.get("data_quality", 1)),
                history=[
                    {
                        "latitude": point.get("lat", point.get("latitude")),
                        "longitude": point.get("lon", point.get("longitude")),
                        "speed_knots": optional_navigation(
                            point.get("speed_knots"), 102.3
                        ),
                        "course_over_ground": optional_navigation(
                            point.get("course_over_ground"), 360
                        ),
                        "heading": optional_navigation(point.get("heading"), 360),
                        "timestamp": self._parse_timestamp(point.get("timestamp")),
                    }
                    for point in self.get_vessel_history(mmsi)
                    if point.get("lat", point.get("latitude")) is not None
                    and point.get("lon", point.get("longitude")) is not None
                ],
            )

            return vessel

        except Exception as exc:
            print(f"StateStore.get_vessel({mmsi}) failed: {type(exc).__name__}: {exc}")

            return None

    # ------------------------------------------------------------

    def save_vessel(
        self,
        vessel: VesselState,
    ) -> bool:
        """
        Save current vessel state to Redis.

        Redis representation intentionally remains compatible
        with the existing AIS consumer.
        """

        try:
            position = vessel.position

            timestamp = vessel.last_updated

            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)

            key = self._vessel_state_key(str(vessel.mmsi))

            data = {
                "mmsi": str(vessel.mmsi),
                "vessel_name": (vessel.vessel_name or ""),
                "lat": str(position.latitude),
                "lon": str(position.longitude),
                "speed_knots": str(position.speed_knots),
                "course_over_ground": str(position.course_over_ground),
                "heading": str(position.heading),
                "timestamp": str(timestamp.timestamp()),
            }

            for field in ("characteristics", "weather", "ocean", "waves", "provenance"):
                value = getattr(vessel, field)
                data[field] = json.dumps(
                    value.model_dump(mode="json")
                    if hasattr(value, "model_dump")
                    else value,
                    default=str,
                )
            if vessel.destination:
                data["destination"] = vessel.destination
            if vessel.eta:
                data["eta"] = vessel.eta.isoformat()
            data["data_quality"] = str(vessel.data_quality)
            for field in ("speed_knots", "course_over_ground", "heading"):
                if data[field] == "None":
                    data.pop(field)
            self.redis.hset(key, mapping=data)

            return True

        except Exception as exc:
            print(f"StateStore.save_vessel failed: {type(exc).__name__}: {exc}")

            return False

    # ------------------------------------------------------------

    def get_active_vessels(
        self,
        limit: Optional[int] = None,
    ) -> List[VesselState]:
        """
        Retrieve vessel states currently stored in Redis.

        Uses SCAN instead of KEYS so the operation does not
        block Redis when the fleet grows.
        """

        # Newly ingested observations use a sorted index. Preserve SCAN fallback
        # for pre-migration Redis data; no cache rewrite is required.
        recent = self.redis.zrevrange(
            "exochain:vessels:recent", 0, limit - 1 if limit else -1
        )
        if recent:
            return [v for mmsi in recent if (v := self.get_vessel(mmsi)) is not None]

        # Legacy caches have no recent index. Batch timestamp reads then select
        # newest vessels; SCAN order is not freshness order.
        keys = list(self.redis.scan_iter(match="exochain:vessel:*:state", count=500))
        ranked = []
        for offset in range(0, len(keys), 500):
            batch = keys[offset : offset + 500]
            pipe = self.redis.pipeline(transaction=False)
            for key in batch:
                pipe.hget(key, "timestamp")
            for key, value in zip(batch, pipe.execute()):
                try:
                    ranked.append((self._parse_timestamp(value), key.split(":")[2]))
                except (ValueError, TypeError, IndexError):
                    continue
        ranked.sort(reverse=True)
        return [
            v for _, mmsi in ranked[:limit] if (v := self.get_vessel(mmsi)) is not None
        ]

    # ============================================================
    # VESSEL HISTORY
    # ============================================================

    def get_vessel_history(
        self,
        mmsi: str,
        limit: int = 720,
    ) -> List[Dict[str, Any]]:

        key = self._vessel_history_key(mmsi)

        values = self.redis.lrange(
            key,
            -limit,
            -1,
        )

        history: List[Dict[str, Any]] = []

        for value in values:
            try:
                if isinstance(
                    value,
                    str,
                ):
                    history.append(json.loads(value))

                else:
                    history.append(value)

            except Exception:
                continue

        return history

    # ------------------------------------------------------------

    def save_vessel_history(
        self,
        mmsi: str,
        event: Dict[str, Any],
        max_length: int = 720,
    ) -> bool:

        try:
            key = self._vessel_history_key(mmsi)

            self.redis.rpush(
                key,
                json.dumps(
                    event,
                    default=str,
                ),
            )

            self.redis.ltrim(
                key,
                -max_length,
                -1,
            )

            return True

        except Exception as exc:
            print(f"StateStore.save_vessel_history failed: {type(exc).__name__}: {exc}")

            return False

    # ============================================================
    # INTELLIGENCE
    # ============================================================

    def save_intelligence(
        self,
        agent_name: str,
        entity_id: str,
        result: IntelligenceResult,
    ) -> bool:

        try:
            key = self._intelligence_key(
                agent_name,
                entity_id,
            )

            payload = result.model_dump(mode="json")

            self.redis.set(
                key,
                json.dumps(payload),
            )

            return True

        except Exception as exc:
            print(f"StateStore.save_intelligence failed: {type(exc).__name__}: {exc}")

            return False

    # ------------------------------------------------------------

    def get_intelligence(
        self,
        agent_name: str,
        entity_id: str,
    ) -> Optional[IntelligenceResult]:

        key = self._intelligence_key(
            agent_name,
            entity_id,
        )

        value = self.redis.get(key)

        if not value:
            return None

        try:
            payload = json.loads(value)

            return IntelligenceResult.model_validate(payload)

        except Exception as exc:
            print(f"StateStore.get_intelligence failed: {type(exc).__name__}: {exc}")

            return None

    # ============================================================
    # DECISIONS
    # ============================================================

    def save_decision(
        self,
        agent_name: str,
        entity_id: str,
        decision: DecisionProposal,
    ) -> bool:

        try:
            key = self._decision_key(
                agent_name,
                entity_id,
            )

            payload = decision.model_dump(mode="json")

            self.redis.set(
                key,
                json.dumps(payload),
            )

            return True

        except Exception as exc:
            print(f"StateStore.save_decision failed: {type(exc).__name__}: {exc}")

            return False

    # ------------------------------------------------------------

    def get_decision(
        self,
        agent_name: str,
        entity_id: str,
    ) -> Optional[DecisionProposal]:

        key = self._decision_key(
            agent_name,
            entity_id,
        )

        value = self.redis.get(key)

        if not value:
            return None

        try:
            payload = json.loads(value)

            return DecisionProposal.model_validate(payload)

        except Exception as exc:
            print(f"StateStore.get_decision failed: {type(exc).__name__}: {exc}")

            return None

    # ============================================================
    # OCEAN CURRENT TILE CACHE
    # ============================================================

    def get_ocean_current_tile(
        self,
        key: str,
    ) -> Optional[Dict[str, Any]]:

        try:
            value = self.redis.get(key)

            if not value:
                return None

            return json.loads(value)

        except Exception as exc:
            print(
                f"StateStore.get_ocean_current_tile failed: {type(exc).__name__}: {exc}"
            )

            return None

    # ------------------------------------------------------------

    def set_ocean_current_tile(
        self,
        key: str,
        value: Dict[str, Any],
        ttl_seconds: int,
    ) -> bool:

        try:
            self.redis.set(
                key,
                json.dumps(
                    value,
                    default=str,
                ),
                ex=int(ttl_seconds),
            )

            return True

        except Exception as exc:
            print(
                f"StateStore.set_ocean_current_tile failed: {type(exc).__name__}: {exc}"
            )

            return False

    # ============================================================
    # GENERIC REDIS HELPERS
    # ============================================================

    def exists(
        self,
        key: str,
    ) -> bool:

        try:
            return bool(self.redis.exists(key))

        except Exception:
            return False

    # ------------------------------------------------------------

    def delete(
        self,
        key: str,
    ) -> bool:

        try:
            return bool(self.redis.delete(key))

        except Exception:
            return False
