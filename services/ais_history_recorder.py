"""
ExoChain AIS Historical Recorder

Purpose
-------
Consumes every AIS event from Kafka and persists it permanently into SQLite.

Architecture
------------
AISStream
   ↓
Kafka: telemetry-ais
   ├──→ Redis consumer       → live state / inference buffer
   │
   └──→ THIS CONSUMER        → permanent historical store
                                  ↓
                              ST-GNN dataset
                                  ↓
                              model training

Important
---------
- Uses a separate Kafka consumer group.
- Does NOT interfere with the existing maritime consumer.
- Kafka offsets are committed only after SQLite commit succeeds.
- SQLite WAL mode is enabled for durability/concurrent reads.
- Duplicate events are ignored using a deterministic event ID.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from kafka import KafkaConsumer


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

KAFKA_BOOTSTRAP_SERVERS = os.getenv(
    "KAFKA_BOOTSTRAP_SERVERS",
    "localhost:9092",
)

KAFKA_TOPIC = os.getenv(
    "KAFKA_TOPIC_MARITIME",
    "telemetry-ais",
)

KAFKA_CONSUMER_GROUP = os.getenv(
    "KAFKA_AIS_HISTORY_CONSUMER_GROUP",
    "exochain-ais-history",
)

DB_PATH = Path(
    os.getenv(
        "AIS_HISTORY_DB_PATH",
        str(BASE_DIR / "data" / "ais_history.db"),
    )
)

BATCH_SIZE = int(
    os.getenv(
        "AIS_HISTORY_BATCH_SIZE",
        "100",
    )
)

BATCH_TIMEOUT_SECONDS = float(
    os.getenv(
        "AIS_HISTORY_BATCH_TIMEOUT_SECONDS",
        "2.0",
    )
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("exochain.ais_history")


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown_requested = False


def _handle_shutdown(signum: int, frame: Any) -> None:
    global _shutdown_requested

    logger.info(
        "Shutdown signal received | signal=%s",
        signum,
    )

    _shutdown_requested = True


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ais_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    event_id TEXT NOT NULL UNIQUE,

    mmsi INTEGER NOT NULL,

    vessel_name TEXT,

    latitude REAL NOT NULL,
    longitude REAL NOT NULL,

    speed_knots REAL,
    course_over_ground REAL,
    heading REAL,

    event_timestamp TEXT NOT NULL,

    source TEXT,
    event_type TEXT,

    ingested_at TEXT NOT NULL
);
"""


CREATE_INDEXES_SQL = [
    """
    CREATE INDEX IF NOT EXISTS idx_ais_mmsi_timestamp
    ON ais_positions (mmsi, event_timestamp);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ais_timestamp
    ON ais_positions (event_timestamp);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_ais_mmsi
    ON ais_positions (mmsi);
    """,
]


INSERT_SQL = """
INSERT OR IGNORE INTO ais_positions (
    event_id,
    mmsi,
    vessel_name,
    latitude,
    longitude,
    speed_knots,
    course_over_ground,
    heading,
    event_timestamp,
    source,
    event_type,
    ingested_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


def create_database() -> sqlite3.Connection:
    """
    Create/open the historical AIS database.

    WAL allows the dataset builder or other readers to access the database
    while this recorder is still running.
    """

    DB_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.info(
        "Opening AIS historical database | path=%s",
        DB_PATH,
    )

    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
        isolation_level=None,
    )

    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA synchronous=NORMAL;")
    connection.execute("PRAGMA busy_timeout=30000;")
    connection.execute("PRAGMA foreign_keys=ON;")

    connection.execute(CREATE_TABLE_SQL)

    for index_sql in CREATE_INDEXES_SQL:
        connection.execute(index_sql)

    logger.info("AIS historical database ready")

    return connection


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Normalize Kafka AIS event into the database schema.

    Expected event format from kafka_pipeline.publish_ais_position():

    {
        "event_type": "ais_position",
        "mmsi": ...,
        "vessel_name": ...,
        "lat": ...,
        "lon": ...,
        "speed_knots": ...,
        "course_over_ground": ...,
        "heading": ...,
        "timestamp": ...,
        "source": "aisstream"
    }
    """

    if event.get("event_type") == "AIS_STATIC_REPORT":
        return (
            None  # Position history deliberately excludes non-position type-5 reports.
        )
    mmsi = _safe_int(event.get("mmsi"))

    latitude = _safe_float(event.get("lat", event.get("latitude")))

    longitude = _safe_float(event.get("lon", event.get("longitude")))

    timestamp = event.get("timestamp")

    if mmsi is None:
        logger.warning("Skipping AIS event without MMSI")
        return None

    if latitude is None or longitude is None:
        logger.warning(
            "Skipping AIS event without valid coordinates | MMSI=%s",
            mmsi,
        )
        return None

    if not timestamp:
        logger.warning(
            "Skipping AIS event without timestamp | MMSI=%s",
            mmsi,
        )
        return None

    normalized = {
        "mmsi": mmsi,
        "vessel_name": event.get("vessel_name"),
        "latitude": latitude,
        "longitude": longitude,
        "speed_knots": _safe_float(event.get("speed_knots")),
        "course_over_ground": _safe_float(event.get("course_over_ground")),
        "heading": _safe_float(event.get("heading")),
        "event_timestamp": str(timestamp),
        "source": event.get("source"),
        "event_type": event.get(
            "event_type",
            "ais_position",
        ),
    }

    return normalized


# ---------------------------------------------------------------------------
# Deterministic event ID
# ---------------------------------------------------------------------------


def make_event_id(event: dict[str, Any]) -> str:
    """
    Create a deterministic ID.

    This protects the historical store against duplicate Kafka deliveries.

    Same:
        MMSI + timestamp + coordinates + navigation data

    => same event_id.
    """

    identity = {
        "mmsi": event["mmsi"],
        "timestamp": event["event_timestamp"],
        "latitude": event["latitude"],
        "longitude": event["longitude"],
        "speed_knots": event["speed_knots"],
        "course_over_ground": event["course_over_ground"],
        "heading": event["heading"],
        "event_type": event["event_type"],
    }

    canonical = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_batch(
    connection: sqlite3.Connection,
    events: list[dict[str, Any]],
) -> tuple[int, int]:
    """
    Persist one batch.

    Returns:
        inserted_count
        duplicate_count
    """

    if not events:
        return 0, 0

    rows = []

    ingested_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ",
        time.gmtime(),
    )

    for event in events:
        event_id = make_event_id(event)

        rows.append(
            (
                event_id,
                event["mmsi"],
                event["vessel_name"],
                event["latitude"],
                event["longitude"],
                event["speed_knots"],
                event["course_over_ground"],
                event["heading"],
                event["event_timestamp"],
                event["source"],
                event["event_type"],
                ingested_at,
            )
        )

    before_changes = connection.total_changes

    connection.execute("BEGIN")

    try:
        connection.executemany(
            INSERT_SQL,
            rows,
        )

        connection.execute("COMMIT")

    except Exception:
        connection.execute("ROLLBACK")
        raise

    inserted = connection.total_changes - before_changes

    duplicate = len(rows) - inserted

    return inserted, duplicate


# ---------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------


def create_consumer() -> KafkaConsumer:
    logger.info(
        "Connecting Kafka | bootstrap=%s | topic=%s | group=%s",
        KAFKA_BOOTSTRAP_SERVERS,
        KAFKA_TOPIC,
        KAFKA_CONSUMER_GROUP,
    )

    consumer = KafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=KAFKA_CONSUMER_GROUP,
        auto_offset_reset="latest",
        enable_auto_commit=False,
        key_deserializer=lambda key: key.decode("utf-8") if key else None,
        value_deserializer=lambda value: json.loads(value.decode("utf-8")),
        max_poll_records=BATCH_SIZE,
        session_timeout_ms=30000,
        heartbeat_interval_ms=10000,
        max_poll_interval_ms=300000,
        request_timeout_ms=40000,
        retry_backoff_ms=1000,
    )

    logger.info("Kafka consumer ready")

    return consumer


# ---------------------------------------------------------------------------
# Main recorder
# ---------------------------------------------------------------------------


def run() -> None:
    global _shutdown_requested

    connection = create_database()
    consumer = create_consumer()

    total_received = 0
    total_inserted = 0
    total_duplicates = 0
    total_invalid = 0

    batch: list[dict[str, Any]] = []

    last_flush = time.monotonic()

    logger.info(
        "AIS HISTORY RECORDER STARTED | topic=%s | group=%s | db=%s",
        KAFKA_TOPIC,
        KAFKA_CONSUMER_GROUP,
        DB_PATH,
    )

    try:
        while not _shutdown_requested:
            records = consumer.poll(
                timeout_ms=1000,
                max_records=BATCH_SIZE,
            )

            for _, messages in records.items():
                for message in messages:
                    total_received += 1

                    try:
                        event = normalize_event(message.value)

                        if event is None:
                            total_invalid += 1
                            continue

                        batch.append(event)

                    except Exception:
                        total_invalid += 1

                        logger.exception(
                            "Failed to normalize AIS event | partition=%s | offset=%s",
                            message.partition,
                            message.offset,
                        )

            now = time.monotonic()

            should_flush = len(batch) >= BATCH_SIZE or (
                batch and now - last_flush >= BATCH_TIMEOUT_SECONDS
            )

            if should_flush:
                try:
                    inserted, duplicates = persist_batch(
                        connection,
                        batch,
                    )

                    # IMPORTANT:
                    # Commit Kafka offsets only after SQLite
                    # successfully commits the batch.
                    consumer.commit()

                    total_inserted += inserted
                    total_duplicates += duplicates

                    logger.info(
                        "AIS HISTORY BATCH | "
                        "received=%d | inserted=%d | "
                        "duplicates=%d | invalid=%d | "
                        "total_rows=%d",
                        len(batch),
                        inserted,
                        duplicates,
                        total_invalid,
                        total_inserted,
                    )

                    batch.clear()
                    last_flush = now

                except Exception:
                    logger.exception(
                        "AIS history batch failed; Kafka offsets NOT committed"
                    )

                    # Do not clear batch.
                    #
                    # The consumer will eventually retry.
                    # The database transaction was rolled back
                    # if persistence failed.

                    time.sleep(2)

    finally:
        # Flush anything remaining before shutdown.
        if batch:
            try:
                inserted, duplicates = persist_batch(
                    connection,
                    batch,
                )

                consumer.commit()

                total_inserted += inserted
                total_duplicates += duplicates

                logger.info(
                    "FINAL AIS BATCH | inserted=%d | duplicates=%d",
                    inserted,
                    duplicates,
                )

            except Exception:
                logger.exception("Failed to persist final AIS batch")

        consumer.close()
        connection.close()

        logger.info(
            "AIS HISTORY RECORDER STOPPED | "
            "received=%d | inserted=%d | "
            "duplicates=%d | invalid=%d",
            total_received,
            total_inserted,
            total_duplicates,
            total_invalid,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    backoff = 1
    while not _shutdown_requested:
        try:
            run()
            break
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error(
                "Recorder unavailable: %s; reconnect in %ss",
                type(exc).__name__,
                backoff,
            )
            time.sleep(backoff)
            backoff = min(30, backoff * 2)
