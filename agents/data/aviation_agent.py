from __future__ import annotations

import time
from typing import Any

import requests

from agents.base import AgentContext, BaseAgent
from config.settings import settings


class AviationDataAgent(BaseAgent[dict[str, Any]]):
    """
    Real-time aviation telemetry agent backed by OpenSky Network.

    Fetches ADS-B state vectors and normalizes them for the ExoChain
    Data Cluster.
    """

    agent_id = "data.aviation"
    agent_name = "Aviation Data Agent"
    agent_type = "DATA"
    version = "1.0.0"

    dependencies = ("OpenSky",)

    def __init__(self) -> None:
        super().__init__()

        self.url = settings.OPENSKY_URL
        self.poll_interval = settings.OPENSKY_POLL_INTERVAL

    def validate_input(self, context: AgentContext) -> None:
        if context is None:
            raise ValueError("AgentContext is required")

    def run(self, context: AgentContext) -> dict[str, Any]:
        started = time.perf_counter()

        # Global bounding box by default.
        # If a narrower bbox is supplied through metadata, use it.
        bbox = context.metadata.get(
            "aviation_bbox",
            {
                "lamin": -90.0,
                "lomin": -180.0,
                "lamax": 90.0,
                "lomax": 180.0,
            },
        )

        params = {
            "lamin": bbox["lamin"],
            "lomin": bbox["lomin"],
            "lamax": bbox["lamax"],
            "lomax": bbox["lomax"],
        }

        response = requests.get(
            self.url,
            params=params,
            timeout=15,
        )

        response.raise_for_status()

        payload = response.json()

        states = payload.get("states") or []

        aircraft: list[dict[str, Any]] = []

        for state in states:
            if not state or len(state) < 11:
                continue

            icao24 = state[0]

            if not icao24:
                continue

            aircraft.append(
                {
                    "icao24": icao24,
                    "callsign": (
                        state[1].strip()
                        if state[1]
                        else "UNKNOWN"
                    ),
                    "origin_country": state[2],
                    "time_position": state[3],
                    "last_contact": state[4],
                    "longitude": state[5],
                    "latitude": state[6],
                    "baro_altitude_m": state[7],
                    "on_ground": state[8],
                    "velocity_ms": state[9],
                    "velocity_kmh": (
                        round(state[9] * 3.6, 2)
                        if state[9] is not None
                        else None
                    ),
                    "true_track_deg": state[10],
                    "vertical_rate_ms": (
                        state[11]
                        if len(state) > 11
                        else None
                    ),
                    "geo_altitude_m": (
                        state[13]
                        if len(state) > 13
                        else None
                    ),
                    "squawk": (
                        state[14]
                        if len(state) > 14
                        else None
                    ),
                    "position_source": (
                        state[16]
                        if len(state) > 16
                        else None
                    ),
                }
            )

        latency_ms = round(
            (time.perf_counter() - started) * 1000,
            2,
        )

        return {
            "status": "SUCCESS",
            "data_quality": "VALID" if aircraft else "EMPTY",
            "source": "OPENSKY",
            "endpoint": self.url,
            "aircraft_count": len(aircraft),
            "aircraft": aircraft,
            "bounding_box": bbox,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
        }
