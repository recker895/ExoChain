"""Spatiotemporal environmental joins and physically explicit current projection."""

import math

from config.settings import settings
from core.schemas.contracts import utcnow
from services.providers.registry import timestamp


def distance_km(a, b):
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def current_effect(u_ms, v_ms, heading_deg, through_water_speed_knots=None):
    heading = math.radians(heading_deg)
    along = u_ms * math.sin(heading) + v_ms * math.cos(heading)
    cross = u_ms * math.cos(heading) - v_ms * math.sin(heading)
    return {
        "speed_ms": math.hypot(u_ms, v_ms),
        "direction_deg": math.degrees(math.atan2(u_ms, v_ms)) % 360,
        "relative_heading_deg": (
            (math.degrees(math.atan2(u_ms, v_ms)) - heading_deg + 180) % 360
        )
        - 180,
        "along_track_ms": along,
        "cross_track_ms": cross,
        "estimated_ground_speed_knots": through_water_speed_knots + along * 1.943844
        if through_water_speed_knots is not None
        else None,
        "assumption": "Vector projection; observed AIS speed over ground is not speed through water.",
    }


def join_environment(snapshot):
    """Return enriched copies. Grid joins require matching space and observation time."""
    ais = snapshot.get("ais")
    vessels = (ais.payload or {}).get("vessels", []) if ais else []
    weather = snapshot.get("weather")
    ocean = snapshot.get("ocean")
    grids = (weather.payload or {}).get("weather", {}) if weather else {}
    currents = (ocean.payload or {}).get("currents", {}) if ocean else {}
    now = utcnow()
    enriched = []
    for original in vessels:
        import copy

        vessel = copy.deepcopy(original)
        pos = vessel["position"]
        location = (pos["latitude"], pos["longitude"])
        at = timestamp(pos["timestamp"])
        vessel["weather"], vessel["ocean"], vessel["waves"] = {}, {}, {}
        vessel["missing_environment"] = ["WAVE_DATA_UNAVAILABLE"]
        for grid in grids.values():
            current = grid.get("current", {})
            observed = timestamp(current.get("time"))
            if (
                not observed
                or observed > now
                or abs((at - observed).total_seconds())
                > settings.WEATHER_MAX_AGE_SECONDS
                or (now - observed).total_seconds() > settings.WEATHER_MAX_AGE_SECONDS
            ):
                continue
            if distance_km(location, (grid["latitude"], grid["longitude"])) > 25:
                continue
            units = grid.get("current_units", {})
            speed = current.get("wind_speed_10m")
            if speed is not None:
                unit = units.get("wind_speed_10m")
                speed = (
                    speed / 3.6
                    if unit == "km/h"
                    else speed
                    if unit == "m/s"
                    else speed / 1.943844
                    if unit == "kn"
                    else None
                )
            vessel["weather"] = {
                "source": "Open-Meteo",
                "latitude": grid["latitude"],
                "longitude": grid["longitude"],
                "observed_at": observed.isoformat(),
                "query_grid_degrees": 0.25,
                "spatial_resolution_degrees": None,
                "wind_speed_ms": speed,
                "wind_direction_deg": current.get("wind_direction_10m"),
                "precipitation_mm": current.get("precipitation"),
                "temperature_c": current.get("temperature_2m"),
                "weather_code": current.get("weather_code"),
                "visibility_m": current.get("visibility"),
            }
            break
        current = currents.get(str(vessel["mmsi"]))
        if (
            current
            and current.get("valid")
            and ocean.observed_at
            and ocean.valid_until
            and ocean.valid_until >= now
        ):
            if (
                abs((at - ocean.observed_at).total_seconds())
                <= settings.OCEAN_MAX_AGE_SECONDS
                and distance_km(location, (current["latitude"], current["longitude"]))
                <= 15
            ):
                vessel["ocean"] = {
                    **current,
                    "source": "Copernicus Marine",
                    "observed_at": ocean.observed_at.isoformat(),
                    "spatial_resolution_degrees": 1 / 12,
                }
                heading = pos.get("course_over_ground")
                if heading is not None and 0 <= heading < 360:
                    vessel["ocean"]["effect"] = current_effect(
                        current["u_ms"], current["v_ms"], heading
                    )
        if not vessel["weather"]:
            vessel["missing_environment"].append("WEATHER_UNAVAILABLE")
        if not vessel["ocean"]:
            vessel["missing_environment"].append("OCEAN_CURRENT_UNAVAILABLE")
        enriched.append(vessel)
    return enriched
