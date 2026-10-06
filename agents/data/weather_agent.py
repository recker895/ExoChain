import time
from typing import Any, Dict, List, Tuple

import requests

from agents.base.agent import BaseAgent
from agents.base.context import AgentContext
from config.settings import settings


class WeatherDataAgent(BaseAgent):

    API_URL = "https://api.open-meteo.com/v1/forecast"
    MARINE_API_URL = "https://marine-api.open-meteo.com/v1/marine"
    BATCH_SIZE = 50

    def __init__(self) -> None:

        super().__init__(
            agent_id="data.weather",
            agent_type="WEATHER",
        )

        self.session = requests.Session()

    def fetch_route_snapshot(self, points, *, domain="weather"):
        """Real public model data at corridor points, independent of AIS coverage.

        This is a current-condition planning snapshot, not a month-long voyage
        forecast. Units and valid times are returned by the provider.
        """
        variables = {
            "weather": "wind_speed_10m,wind_direction_10m,temperature_2m",
            "waves": "wave_height,wave_direction,wave_period",
            "ocean": "ocean_current_velocity,ocean_current_direction",
        }
        if not points or len(points) > 50 or domain not in variables:
            raise ValueError("Invalid route snapshot request")
        params = {
            "latitude": ",".join(str(lat) for lat, _ in points),
            "longitude": ",".join(str(lon) for _, lon in points),
            "current": variables[domain], "timezone": "UTC",
        }
        if domain == "weather":
            params["wind_speed_unit"] = "ms"
        else:
            params.update(velocity_unit="ms", cell_selection="sea")
        response = self.session.get(
            self.API_URL if domain == "weather" else self.MARINE_API_URL,
            params=params, timeout=min(settings.PROVIDER_TIMEOUT_SECONDS, 10),
        )
        response.raise_for_status()
        payload = response.json()
        records = payload if isinstance(payload, list) else [payload]
        if len(records) != len(points):
            raise ValueError("Provider point count mismatch")
        return records

    def fetch_route_forecast(self, points: List[Tuple[float, float]], *, marine: bool = False) -> List[Dict[str, Any]]:
        """Hourly forecasts at explicit route sample coordinates, not AIS positions."""
        if not points or len(points) > 12:
            raise ValueError("Route forecast requires 1-12 sample points")
        params = {
            "latitude": ",".join(str(lat) for lat, _ in points),
            "longitude": ",".join(str(lon) for _, lon in points),
            "hourly": (
                "wave_height,wave_direction,wave_period"
                if marine else
                "wind_speed_10m,wind_direction_10m,temperature_2m,precipitation,weather_code"
            ),
            "forecast_days": 16,
            "timezone": "UTC",
        }
        response = self.session.get(
            self.MARINE_API_URL if marine else self.API_URL,
            params=params,
            timeout=min(settings.PROVIDER_TIMEOUT_SECONDS, 8),
        )
        response.raise_for_status()
        result = response.json()
        records = result if isinstance(result, list) else [result]
        if len(records) != len(points):
            raise ValueError("Route forecast point count mismatch")
        return records

    @staticmethod
    def _grid_key(
        latitude: float,
        longitude: float,
        resolution: float = 0.25,
    ) -> Tuple[float, float]:

        return (
            round(latitude / resolution) * resolution,
            round(longitude / resolution) * resolution,
        )

    def _fetch_batch(
        self,
        points: List[Tuple[float, float]],
    ) -> List[Dict[str, Any]]:

        params = {
            "latitude": ",".join(
                str(lat)
                for lat, _ in points
            ),

            "longitude": ",".join(
                str(lon)
                for _, lon in points
            ),

            "current": (
                "temperature_2m,"
                "relative_humidity_2m,"
                "surface_pressure,"
                "wind_speed_10m,"
                "wind_direction_10m,"
                "wind_gusts_10m,"
                "precipitation,"
                "weather_code"
            ),

            "timezone": "UTC",
        }

        response = self.session.get(
            self.API_URL,
            params=params,
            timeout=settings.PROVIDER_TIMEOUT_SECONDS,
        )

        response.raise_for_status()

        data = response.json()

        if isinstance(data, dict):
            return [data]

        return data

    def execute(
        self,
        context: AgentContext,
    ) -> Dict[str, Any]:

        start = time.perf_counter()

        vessels = context.get_active_vessels()

        grid_points: Dict[
            Tuple[float, float],
            List[str],
        ] = {}

        for vessel in vessels:

            key = self._grid_key(
                vessel.position.latitude,
                vessel.position.longitude,
            )

            grid_points.setdefault(
                key,
                [],
            ).append(vessel.mmsi)

        points = list(grid_points.keys())

        weather_points: Dict[
            str,
            Dict[str, Any],
        ] = {}

        errors: List[str] = []

        batches = [
            points[i:i + self.BATCH_SIZE]
            for i in range(
                0,
                len(points),
                self.BATCH_SIZE,
            )
        ]

        for batch in batches:
            if time.perf_counter() - start >= settings.PROVIDER_TIMEOUT_SECONDS:
                errors.append("WEATHER_COLLECTION_DEADLINE_EXCEEDED")
                break

            try:

                responses = self._fetch_batch(
                    batch
                )

                for point, data in zip(
                    batch,
                    responses,
                ):

                    latitude, longitude = point

                    weather_points[
                        f"{latitude:.2f},{longitude:.2f}"
                    ] = {
                        "latitude": latitude,
                        "longitude": longitude,
                        "current": data.get(
                            "current",
                            {},
                        ),
                        "current_units": data.get(
                            "current_units",
                            {},
                        ),
                        "vessel_count": len(
                            grid_points[point]
                        ),
                    }

            except requests.RequestException as exc:

                errors.append(
                    f"batch_size={len(batch)}: {type(exc).__name__}"
                )

        latency_ms = (
            time.perf_counter() - start
        ) * 1000

        return {
            "source": "OPEN_METEO",
            "vessel_count": len(vessels),

            "grid_resolution_degrees": 0.25,

            "grid_points_requested": len(
                grid_points
            ),

            "grid_points_received": len(
                weather_points
            ),

            "batches": len(batches),

            "weather": weather_points,

            "errors": errors,

            "latency_ms": latency_ms,
        }
