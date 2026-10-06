"""Coordinate representation helpers; never clamp or invent route positions."""
import math


def normalize_longitude(longitude: float) -> float:
    """Represent the same meridian in [-180, 180], preserving valid inputs."""
    if not math.isfinite(longitude):
        raise ValueError("Longitude must be finite")
    if -180 <= longitude <= 180:
        return longitude
    return (longitude + 180) % 360 - 180


def longitude_delta(start: float, end: float) -> float:
    """Shortest signed displacement, including across the Pacific date line."""
    delta = end - start
    return (delta + 180) % 360 - 180 if abs(delta) > 180 else delta
