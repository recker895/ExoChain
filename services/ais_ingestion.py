import sys
import os
import asyncio
import json
import logging
import random
import time
import math
from datetime import datetime
from dotenv import load_dotenv
import websockets

# ------------------------------------------------------------------
# Project path
# ------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ------------------------------------------------------------------
# Environment
# ------------------------------------------------------------------
load_dotenv()

# ------------------------------------------------------------------
# Dependencies
# ------------------------------------------------------------------
from services.kafka_telemetry import kafka_pipeline  # noqa: E402 - after path/environment bootstrap

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

logger = logging.getLogger("ExoChain-AISIngestion")

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
AISSTREAM_URL = os.getenv("AISSTREAM_URL", "wss://stream.aisstream.io/v0/stream")

AISSTREAM_API_KEY = os.getenv("AISSTREAM_API_KEY")

# ------------------------------------------------------------------
# Geographic monitoring zones
#
# AISStream requires BoundingBoxes.
# Each box = [[south, west], [north, east]]
# ------------------------------------------------------------------
BOUNDING_BOXES = [
    # Suez / Red Sea approach
    [[28.0, 31.0], [31.5, 34.0]],
    # Mumbai / Arabian Sea
    [[17.0, 71.0], [20.5, 74.5]],
    # Rotterdam / North Sea
    [[50.5, 2.5], [53.5, 6.5]],
    # Singapore
    [[0.0, 103.0], [2.5, 105.0]],
    # Shanghai / East China Sea
    [[29.0, 120.0], [33.5, 123.5]],
]

# ------------------------------------------------------------------
# AIS subscription
# ------------------------------------------------------------------
SUBSCRIPTION = {
    "APIKey": AISSTREAM_API_KEY,
    "BoundingBoxes": BOUNDING_BOXES,
    "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
}


# ------------------------------------------------------------------
# Normalize AISStream event
# ------------------------------------------------------------------
def normalize_position_report(event: dict) -> dict | None:
    """
    Convert an AISStream PositionReport into ExoChain's
    canonical maritime telemetry schema.
    """

    if event.get("MessageType") != "PositionReport":
        return None

    metadata = event.get("MetaData", {})
    position = event.get("Message", {}).get("PositionReport", {})

    mmsi = metadata.get("MMSI")

    lat = metadata.get("Latitude")
    lon = metadata.get("Longitude")

    # Some AISStream payloads may contain position information
    # inside the PositionReport itself.
    if lat is None:
        lat = position.get("Latitude")

    if lon is None:
        lon = position.get("Longitude")

    if mmsi is None or lat is None or lon is None:
        logger.warning("Discarding incomplete AIS position event: %s", event)
        return None

    if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
        return None
    if not all(math.isfinite(float(x)) for x in (lat, lon)):
        return None
    if event.get("Message", {}).get("PositionReport", {}).get("Valid") is False:
        return None
    sog = position.get("Sog")
    cog = position.get("Cog")
    heading = position.get("TrueHeading")

    if sog is not None and (
        not math.isfinite(float(sog)) or not 0 <= float(sog) < 102.3
    ):
        sog = None
    if cog is not None and not 0 <= float(cog) < 360:
        cog = None
    if heading is not None and not 0 <= float(heading) < 360:
        heading = None
    timestamp = position.get("Timestamp")

    # AIS timestamp can be an AIS second value rather than Unix epoch.
    # Use ingestion time as the canonical event timestamp.
    ingestion_timestamp = time.time()
    observed_timestamp = ingestion_timestamp
    timestamp_source = "RECEIVED_AT_ONLY"
    provider_time = metadata.get("time_utc")
    if provider_time:
        try:
            text = provider_time.replace(" +0000 UTC", "+00:00").replace(
                " UTC", "+00:00"
            )
            observed_timestamp = datetime.fromisoformat(text).timestamp()
            timestamp_source = "AISSTREAM_METADATA"
        except ValueError:
            pass

    return {
        "event_type": "AIS_POSITION_REPORT",
        "source": "AISSTREAM",
        "mmsi": int(mmsi),
        "vessel_name": (metadata.get("ShipName") or "UNKNOWN").strip(),
        "lat": float(lat),
        "lon": float(lon),
        "speed_knots": (float(sog) if sog is not None else None),
        "course_over_ground": (float(cog) if cog is not None else None),
        "heading": (int(heading) if heading is not None else None),
        "ais_timestamp": timestamp,
        "timestamp": observed_timestamp,
        "received_at": ingestion_timestamp,
        "timestamp_source": timestamp_source,
    }


def normalize_static_report(event: dict) -> dict | None:
    """AISStream type 5: preserve observed voyage facts; never infer an ETA year.

    Reference: https://aisstream.io/documentation (ShipStaticData schema).
    """
    record = event.get("Message", {}).get("ShipStaticData", {})
    metadata = event.get("MetaData", {})
    mmsi = metadata.get("MMSI") or record.get("UserID")
    if not mmsi or record.get("Valid") is False:
        return None
    dimensions = record.get("Dimension", {})
    characteristics = {}
    for key, fields in (("length_m", ("A", "B")), ("beam_m", ("C", "D"))):
        if all(
            isinstance(dimensions.get(f), (int, float)) and dimensions[f] > 0
            for f in fields
        ):
            characteristics[key] = sum(dimensions[f] for f in fields)
    draft = record.get("MaximumStaticDraught")
    if isinstance(draft, (int, float)) and 0 < draft < 25.5:
        characteristics["draft_m"] = draft
    return {
        "event_type": "AIS_STATIC_REPORT",
        "source": "AISSTREAM",
        "mmsi": int(mmsi),
        "vessel_name": record.get("Name", "").replace("@", "").strip(),
        "destination": record.get("Destination", "").replace("@", "").strip() or None,
        "characteristics": characteristics,
        "eta_components": record.get("Eta"),
        "timestamp": time.time(),
        "timestamp_source": "RECEIVED_AT_ONLY",
    }


# ------------------------------------------------------------------
# Process incoming WebSocket message
# ------------------------------------------------------------------
async def process_message(raw_message):
    """
    AISStream sends binary WebSocket frames containing UTF-8 JSON.
    Decode bytes before parsing.
    """

    try:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")

        event = json.loads(raw_message)

    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("Invalid AISStream message: %s", exc)
        return

    message_type = event.get("MessageType")

    # --------------------------------------------------------------
    # Connection confirmation
    # --------------------------------------------------------------
    if message_type == "SubscriptionConfirmation":
        compression_enabled = event.get("Message", {}).get("CompressionEnabled")

        logger.info(
            "AISStream subscription confirmed. Compression enabled=%s",
            compression_enabled,
        )

        return

    # --------------------------------------------------------------
    # Position report
    # --------------------------------------------------------------
    if message_type == "ShipStaticData":
        packet = normalize_static_report(event)
        if packet:
            await asyncio.to_thread(kafka_pipeline.publish_ais_position, packet)
        return
    if message_type != "PositionReport":
        return

    ais_packet = normalize_position_report(event)

    if ais_packet is None:
        return

    # --------------------------------------------------------------
    # Kafka
    # --------------------------------------------------------------
    await asyncio.to_thread(kafka_pipeline.publish_ais_position, ais_packet)

    logger.info(
        "AIS POSITION | MMSI=%s | %s | LAT=%.5f | LON=%.5f | SOG=%s | COG=%s",
        ais_packet["mmsi"],
        ais_packet["vessel_name"],
        ais_packet["lat"],
        ais_packet["lon"],
        ais_packet["speed_knots"],
        ais_packet["course_over_ground"],
    )


# ------------------------------------------------------------------
# AISStream connection
# ------------------------------------------------------------------
async def connect_and_stream():
    """
    Maintain a long-lived AISStream connection.

    AISStream requires the subscription to be sent immediately
    after connection establishment.
    """

    logger.info("Connecting to AISStream: %s", AISSTREAM_URL)

    async with websockets.connect(
        AISSTREAM_URL,
        compression="deflate",
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=None,
    ) as websocket:
        logger.info("AISStream WebSocket connected.")

        # IMPORTANT:
        # Subscription must be sent immediately after connecting.
        await websocket.send(json.dumps(SUBSCRIPTION))

        logger.info(
            "AISStream subscription sent. Monitoring %d geographic zones.",
            len(BOUNDING_BOXES),
        )

        # ----------------------------------------------------------
        # Continuously consume events
        # ----------------------------------------------------------
        async for message in websocket:
            await process_message(message)


# ------------------------------------------------------------------
# Reconnecting stream supervisor
# ------------------------------------------------------------------
async def stream_live_ais_telemetry():
    """
    Production AIS ingestion supervisor.

    Reconnects automatically using exponential backoff + jitter.
    """

    logger.info("Starting ExoChain real-time AIS ingestion...")

    retry_count = 0

    while True:
        try:
            await connect_and_stream()

            # If connection closes normally,
            # restart from a clean state.
            retry_count = 0

            logger.warning("AISStream connection closed. Reconnecting...")

        except asyncio.CancelledError:
            logger.info("AIS ingestion task cancelled.")
            raise

        except Exception as exc:
            retry_count += 1

            # Exponential backoff capped at 60 seconds
            base_delay = min(2 ** min(retry_count, 6), 60)

            # Random jitter prevents synchronized reconnects
            jitter = random.uniform(0, min(base_delay * 0.25, 5))

            delay = base_delay + jitter

            logger.error("AISStream connection error: %s", type(exc).__name__)

            logger.info("Reconnecting in %.2f seconds...", delay)

            await asyncio.sleep(delay)


# ------------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------------
if __name__ == "__main__":
    if not AISSTREAM_API_KEY:
        raise RuntimeError("AISSTREAM_API_KEY is not configured")

    try:
        asyncio.run(stream_live_ais_telemetry())

    except KeyboardInterrupt:
        logger.info("ExoChain AIS ingestion stopped.")
    finally:
        if kafka_pipeline._instance is not None:
            kafka_pipeline.close()
