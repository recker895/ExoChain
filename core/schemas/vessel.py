from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict


class VesselType(str, Enum):
    CARGO = "cargo"
    CONTAINER = "container"
    TANKER = "tanker"
    BULK_CARRIER = "bulk_carrier"
    LNG = "lng"
    PASSENGER = "passenger"
    TUG = "tug"
    FISHING = "fishing"
    OTHER = "other"
    UNKNOWN = "unknown"


class VesselPosition(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    speed_knots: Optional[float] = Field(default=None, ge=0, lt=102.3)
    course_over_ground: Optional[float] = Field(default=None, ge=0, lt=360)
    heading: Optional[float] = Field(default=None, ge=0, lt=360)
    timestamp: datetime


class VesselCharacteristics(BaseModel):
    vessel_type: VesselType = VesselType.UNKNOWN

    length_m: Optional[float] = None
    beam_m: Optional[float] = None
    draft_m: Optional[float] = None

    max_speed_knots: Optional[float] = None
    cruising_speed_knots: Optional[float] = None

    cargo_capacity_tonnes: Optional[float] = None
    fuel_capacity_litres: Optional[float] = None

    fuel_type: Optional[str] = None


class VesselState(BaseModel):
    mmsi: str
    vessel_name: Optional[str] = None

    position: VesselPosition
    characteristics: VesselCharacteristics = Field(
        default_factory=VesselCharacteristics
    )

    current_speed_knots: Optional[float] = None
    current_direction_degrees: Optional[float] = None

    weather: Dict[str, Any] = Field(default_factory=dict)
    ocean: Dict[str, Any] = Field(default_factory=dict)
    waves: Dict[str, Any] = Field(default_factory=dict)
    source: str = "AISSTREAM"
    version: str = "1"
    provenance: List[Dict[str, Any]] = Field(default_factory=list)

    destination: Optional[str] = None
    eta: Optional[datetime] = None

    last_updated: datetime
    data_quality: float = Field(default=1.0, ge=0.0, le=1.0)

    history: List[VesselPosition] = Field(default_factory=list)
