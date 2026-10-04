import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict

from agents.base.agent import BaseAgent
from agents.base.context import AgentContext
from config.settings import settings
from data_sources.copernicus.ocean_currents import (
    CopernicusOceanCurrentSource,
)


class OceanCurrentDataAgent(BaseAgent):
    """
    Ocean-current telemetry agent.

    Architecture:

        Redis vessel state
                ↓
        geographic tile grouping
                ↓
        Redis tile cache
           ↙          ↘
       HIT             MISS
        ↓                ↓
    reuse          Copernicus Marine
                         ↓
                    Redis cache
                         ↓
                  nearest-current
                         ↓
                    agent result

    The agent does NOT perform routing or optimization.
    """

    TILE_SIZE_DEGREES = float(
        os.getenv(
            "COPERNICUS_CURRENT_TILE_SIZE_DEGREES",
            "2.0",
        )
    )

    CACHE_TTL_SECONDS = int(
        os.getenv(
            "COPERNICUS_CURRENT_CACHE_TTL_SECONDS",
            "21600",
        )
    )

    CACHE_PREFIX = os.getenv(
        "COPERNICUS_CURRENT_CACHE_PREFIX",
        "exochain:ocean_current:tile",
    )

    def __init__(self) -> None:
        super().__init__(
            agent_id="data.ocean_current",
            agent_type="OCEAN_CURRENT",
        )

        self.source = (
            CopernicusOceanCurrentSource()
        )

    # ============================================================
    # TILE MANAGEMENT
    # ============================================================

    @classmethod
    def _tile_key(
        cls,
        latitude: float,
        longitude: float,
    ) -> tuple[int, int]:

        return (
            int(
                latitude
                // cls.TILE_SIZE_DEGREES
            ),
            int(
                longitude
                // cls.TILE_SIZE_DEGREES
            ),
        )

    @classmethod
    def _tile_bounds(
        cls,
        tile: tuple[int, int],
    ) -> tuple[
        float,
        float,
        float,
        float,
    ]:

        lat_index, lon_index = tile

        min_latitude = (
            lat_index
            * cls.TILE_SIZE_DEGREES
        )

        max_latitude = (
            min_latitude
            + cls.TILE_SIZE_DEGREES
        )

        min_longitude = (
            lon_index
            * cls.TILE_SIZE_DEGREES
        )

        max_longitude = (
            min_longitude
            + cls.TILE_SIZE_DEGREES
        )

        return (
            min_longitude,
            max_longitude,
            min_latitude,
            max_latitude,
        )

    @classmethod
    def _cache_key(
        cls,
        tile: tuple[int, int],
        date: str,
    ) -> str:

        lat_index, lon_index = tile

        return (
            f"{cls.CACHE_PREFIX}:"
            f"{date}:"
            f"{lat_index}:"
            f"{lon_index}"
        )

    # ============================================================
    # DATASET SERIALIZATION
    # ============================================================

    @staticmethod
    def _serialize_dataset(
        dataset,
    ) -> Dict[str, Any]:
        """
        Convert the tiny single-time/surface dataset
        into a Redis-compatible JSON structure.
        """

        latitude_values = [
            float(value)
            for value in dataset.latitude.values
        ]

        longitude_values = [
            float(value)
            for value in dataset.longitude.values
        ]

        uo_values = (
            dataset["uo"]
            .isel(time=0, depth=0)
            .values
        )

        vo_values = (
            dataset["vo"]
            .isel(time=0, depth=0)
            .values
        )

        def clean(value):
            value = float(value)

            if value != value:
                return None

            return value

        return {
            "latitude": latitude_values,
            "longitude": longitude_values,
            "uo": [
                [
                    clean(value)
                    for value in row
                ]
                for row in uo_values
            ],
            "vo": [
                [
                    clean(value)
                    for value in row
                ]
                for row in vo_values
            ],
        }

    @staticmethod
    def _nearest_cached_current(
        cached: Dict[str, Any],
        latitude: float,
        longitude: float,
    ) -> Dict[str, Any]:
        """
        Find the nearest cached grid point without
        requiring xarray.
        """

        latitudes = cached["latitude"]
        longitudes = cached["longitude"]

        nearest_lat_index = min(
            range(len(latitudes)),
            key=lambda index: abs(
                latitudes[index]
                - latitude
            ),
        )

        nearest_lon_index = min(
            range(len(longitudes)),
            key=lambda index: abs(
                longitudes[index]
                - longitude
            ),
        )

        uo = cached["uo"][
            nearest_lat_index
        ][
            nearest_lon_index
        ]

        vo = cached["vo"][
            nearest_lat_index
        ][
            nearest_lon_index
        ]

        if (
            uo is None
            or vo is None
        ):
            return {
                "valid": False,
                "status": "LAND_MASKED",
                "u_ms": None,
                "v_ms": None,
                "speed_ms": None,
                "speed_knots": None,
                "direction_deg": None,
                "latitude": latitudes[
                    nearest_lat_index
                ],
                "longitude": longitudes[
                    nearest_lon_index
                ],
            }

        result = (
            CopernicusOceanCurrentSource
            .current_vector(
                float(uo),
                float(vo),
            )
        )

        result["latitude"] = (
            latitudes[
                nearest_lat_index
            ]
        )

        result["longitude"] = (
            longitudes[
                nearest_lon_index
            ]
        )

        return result

    # ============================================================
    # CACHE OPERATIONS
    # ============================================================

    def _get_cached_tile(
        self,
        context: AgentContext,
        tile: tuple[int, int],
        date: str,
    ) -> Any:

        key = self._cache_key(
            tile,
            date,
        )

        redis_client = (
            context.state_store.redis
        )

        raw = redis_client.get(key)

        if raw is None:
            return None

        try:
            return json.loads(raw)
        except (
            json.JSONDecodeError,
            TypeError,
        ):
            redis_client.delete(key)
            return None

    def _save_cached_tile(
        self,
        context: AgentContext,
        tile: tuple[int, int],
        date: str,
        dataset,
    ) -> None:

        key = self._cache_key(
            tile,
            date,
        )

        payload = (
            self._serialize_dataset(
                dataset
            )
        )

        redis_client = (
            context.state_store.redis
        )

        redis_client.setex(
            key,
            self.CACHE_TTL_SECONDS,
            json.dumps(
                payload,
                separators=(
                    ",",
                    ":",
                ),
            ),
        )

    # ============================================================
    # MAIN EXECUTION
    # ============================================================

    def execute(
        self,
        context: AgentContext,
    ) -> Dict[str, Any]:

        start = time.perf_counter()

        # --------------------------------------------------------
        # 1. Load active vessels
        # --------------------------------------------------------

        max_vessels = (
            context.config.get(
                "max_vessels"
            )
        )

        if max_vessels is not None:
            max_vessels = int(
                max_vessels
            )

        vessels = (
            context.get_active_vessels(
                limit=max_vessels
            )
        )

        # --------------------------------------------------------
        # 2. Group vessels by tile
        # --------------------------------------------------------

        tiles: Dict[
            tuple[int, int],
            list[Any],
        ] = {}

        for vessel in vessels:

            tile = self._tile_key(
                latitude=vessel.position.latitude,
                longitude=vessel.position.longitude,
            )

            tiles.setdefault(
                tile,
                [],
            ).append(vessel)

        # --------------------------------------------------------
        # 3. Current date
        # --------------------------------------------------------

        date = datetime.now(
            timezone.utc
        ).strftime(
            "%Y-%m-%d"
        )

        currents = {}

        errors = []

        cache_hits = 0
        cache_misses = 0
        copernicus_requests = 0

        # --------------------------------------------------------
        # 4. Process each tile
        # --------------------------------------------------------

        for tile, tile_vessels in (
            tiles.items()
        ):

            try:

                # -----------------------------------------------
                # Try Redis cache first
                # -----------------------------------------------

                cached = (
                    self._get_cached_tile(
                        context=context,
                        tile=tile,
                        date=date,
                    )
                )

                if cached is not None:

                    cache_hits += 1

                    for vessel in (
                        tile_vessels
                    ):

                        current = (
                            self._nearest_cached_current(
                                cached=cached,
                                latitude=(
                                    vessel
                                    .position
                                    .latitude
                                ),
                                longitude=(
                                    vessel
                                    .position
                                    .longitude
                                ),
                            )
                        )

                        currents[
                            vessel.mmsi
                        ] = {
                            "mmsi": (
                                vessel.mmsi
                            ),
                            "vessel_name": (
                                vessel
                                .vessel_name
                            ),
                            "latitude": (
                                vessel
                                .position
                                .latitude
                            ),
                            "longitude": (
                                vessel
                                .position
                                .longitude
                            ),
                            **current,
                        }

                    continue

                # -----------------------------------------------
                # Cache miss → Copernicus
                # -----------------------------------------------

                cache_misses += 1
                if time.perf_counter() - start >= settings.PROVIDER_TIMEOUT_SECONDS:
                    errors.append("OCEAN_COLLECTION_DEADLINE_EXCEEDED")
                    continue
                copernicus_requests += 1

                (
                    min_lon,
                    max_lon,
                    min_lat,
                    max_lat,
                ) = self._tile_bounds(
                    tile
                )

                dataset = (
                    self.source.fetch_tile(
                        minimum_longitude=min_lon,
                        maximum_longitude=max_lon,
                        minimum_latitude=min_lat,
                        maximum_latitude=max_lat,
                        date=date,
                    )
                )

                try:

                    # Store serialized tile
                    # in Redis.
                    self._save_cached_tile(
                        context=context,
                        tile=tile,
                        date=date,
                        dataset=dataset,
                    )

                    # Use the freshly downloaded
                    # dataset for this invocation.
                    for vessel in (
                        tile_vessels
                    ):

                        current = (
                            self.source
                            .nearest_current(
                                dataset=dataset,
                                latitude=(
                                    vessel
                                    .position
                                    .latitude
                                ),
                                longitude=(
                                    vessel
                                    .position
                                    .longitude
                                ),
                            )
                        )

                        if (
                            current.get(
                                "valid"
                            )
                            is False
                        ):
                            current[
                                "status"
                            ] = "LAND_MASKED"

                        currents[
                            vessel.mmsi
                        ] = {
                            "mmsi": (
                                vessel.mmsi
                            ),
                            "vessel_name": (
                                vessel
                                .vessel_name
                            ),
                            "latitude": (
                                vessel
                                .position
                                .latitude
                            ),
                            "longitude": (
                                vessel
                                .position
                                .longitude
                            ),
                            **current,
                        }

                finally:

                    dataset.close()

            except Exception as exc:

                errors.append(
                    {
                        "tile": tile,
                        "vessel_count": (
                            len(
                                tile_vessels
                            )
                        ),
                        "error": str(exc),
                    }
                )

        # --------------------------------------------------------
        # 5. Statistics
        # --------------------------------------------------------

        valid = 0
        land_masked = 0
        missing = 0

        for current in (
            currents.values()
        ):

            status = current.get(
                "status"
            )

            if status == "VALID":
                valid += 1

            elif status == "LAND_MASKED":
                land_masked += 1

            else:
                missing += 1

        latency_ms = (
            time.perf_counter()
            - start
        ) * 1000.0

        # --------------------------------------------------------
        # 6. Return structured result
        # --------------------------------------------------------

        return {
            "source": (
                "COPERNICUS_MARINE"
            ),

            "dataset_id": (
                self.source.DATASET_ID
            ),

            "date": date,

            "variables": [
                "uo",
                "vo",
            ],

            "surface_depth_m": (
                self.source.SURFACE_DEPTH
            ),

            "vessel_count": (
                len(vessels)
            ),

            "tiles_requested": (
                len(tiles)
            ),

            "cache_hits": cache_hits,

            "cache_misses": cache_misses,

            "copernicus_requests": (
                copernicus_requests
            ),

            "vessels_with_current": valid,

            "vessels_land_masked": (
                land_masked
            ),

            "vessels_without_current": (
                missing
            ),

            "currents": currents,

            "errors": errors,

            "latency_ms": latency_ms,
        }
