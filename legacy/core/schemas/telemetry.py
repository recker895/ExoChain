from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, ConfigDict


class TelemetrySource(str, Enum):
    AIS = "ais"
    WEATHER = "weather"
    OCEAN_CURRENT = "ocean_current"
    WAVE = "wave"
    PORT = "port"
    AVIATION = "aviation"
    MARKET = "market"
    NEWS = "news"
    UNKNOWN = "unknown"


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_id: str
    source: TelemetrySource
    event_type: str
    timestamp: datetime
    received_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    entity_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    payload: Dict[str, Any] = Field(default_factory=dict)

    source_sequence: Optional[str] = None
    schema_version: str = "1.0"
