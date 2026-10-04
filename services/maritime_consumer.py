import os
import json
import logging
import time

import redis
from kafka import KafkaConsumer
from dotenv import load_dotenv

load_dotenv()

# ==============================================================
# Logging
# ==============================================================

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

logger = logging.getLogger("ExoChain-MaritimeConsumer")


# ==============================================================
# Configuration
# ==============================================================

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

KAFKA_TOPIC = os.getenv("KAFKA_TOPIC_MARITIME", "telemetry-ais")

KAFKA_GROUP_ID = os.getenv("KAFKA_MARITIME_CONSUMER_GROUP", "exochain-maritime-state")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")

REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

REDIS_DB = int(os.getenv("REDIS_DB", "0"))

TEMPORAL_WINDOW = int(os.getenv("AIS_HISTORY_LIMIT", "720"))


# ==============================================================
# Redis Connection
# ==============================================================

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    decode_responses=True,
    socket_connect_timeout=3,
    socket_timeout=5,
)


def create_consumer():
    return KafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=KAFKA_GROUP_ID,
        auto_offset_reset="latest",
        enable_auto_commit=False,
        value_deserializer=lambda value: json.loads(value.decode("utf-8")),
        key_deserializer=lambda key: key.decode("utf-8") if key else None,
    )


# ==============================================================
# Redis Key Helpers
# ==============================================================


def vessel_state_key(mmsi: int) -> str:
    return f"exochain:vessel:{mmsi}:state"


def vessel_history_key(mmsi: int) -> str:
    return f"exochain:vessel:{mmsi}:history"


# ==============================================================
# Process AIS Event
# ==============================================================


def process_ais_event(event: dict, client=None):
    client = client if client is not None else redis_client
    if not isinstance(event, dict):
        logger.warning("Discarding non-object telemetry event")
        return

    mmsi = event.get("mmsi")

    if not mmsi:
        logger.warning("Discarding AIS event without MMSI")
        return

    state_key = vessel_state_key(mmsi)
    if event.get("event_type") == "AIS_STATIC_REPORT":
        mapping = {
            key: json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            for key, value in {
                "destination": event.get("destination"),
                "characteristics": event.get("characteristics"),
                "eta_components": event.get("eta_components"),
                "static_observed_at": event.get("timestamp"),
            }.items()
            if value is not None
        }
        if mapping:
            client.hset(state_key, mapping=mapping)
        return
    if (
        event.get("lat") is None
        or event.get("lon") is None
        or event.get("timestamp") is None
    ):
        logger.warning("Rejected incomplete AIS position")
        return
    history_key = vessel_history_key(mmsi)
    state = {
        "mmsi": str(mmsi),
        "vessel_name": event.get("vessel_name") or "",
        "lat": event["lat"],
        "lon": event["lon"],
        "timestamp": event["timestamp"],
        "source": event.get("source", "AISSTREAM"),
        "provenance": json.dumps(
            [
                {
                    "source": "AISStream",
                    "reference": str(mmsi),
                    "observed_at": event["timestamp"],
                }
            ]
        ),
    }
    for key in ("speed_knots", "course_over_ground", "heading"):
        state[key] = event.get(key)
    # One atomic update covers ordering, missing-field removal, history, and
    # the recent-vessel index. Kafka replay cannot duplicate observation history.
    script = """
    local old = redis.call('HGET', KEYS[1], 'timestamp')
    if old and tonumber(old) >= tonumber(ARGV[1]) then return 0 end
    local state = cjson.decode(ARGV[2])
    for key,value in pairs(state) do
      if value == cjson.null then redis.call('HDEL', KEYS[1],key)
      else redis.call('HSET', KEYS[1],key,tostring(value)) end
    end
    redis.call('RPUSH', KEYS[2], ARGV[3])
    redis.call('LTRIM', KEYS[2], -tonumber(ARGV[4]), -1)
    redis.call('ZADD', KEYS[3], ARGV[1], ARGV[5])
    return 1
    """
    client.eval(
        script,
        3,
        state_key,
        history_key,
        "exochain:vessels:recent",
        event["timestamp"],
        json.dumps(state),
        json.dumps(event),
        TEMPORAL_WINDOW,
        str(mmsi),
    )


# ==============================================================
# Consumer Loop
# ==============================================================


def run():

    redis_client.ping()
    consumer = create_consumer()

    logger.info("Starting ExoChain Maritime State Consumer...")

    try:
        while True:
            batches = consumer.poll(timeout_ms=1000, max_records=2000)
            if not batches:
                continue
            # Redis validates event ordering atomically per MMSI. Commit a Kafka
            # batch only after every Redis command succeeds; replay is idempotent.
            pipe = redis_client.pipeline(transaction=False)
            for messages in batches.values():
                for message in messages:
                    process_ais_event(message.value, pipe)
            pipe.execute()
            consumer.commit()

    except KeyboardInterrupt:
        logger.info("Maritime consumer stopped.")

    finally:
        consumer.close()
        redis_client.close()

        logger.info("Maritime consumer shutdown complete.")


# ==============================================================
# Entry Point
# ==============================================================

if __name__ == "__main__":
    backoff = 1
    while True:
        try:
            run()
            break
        except KeyboardInterrupt:
            break
        except Exception as exc:
            logger.error(
                "Consumer unavailable: %s; retry in %ss", type(exc).__name__, backoff
            )
            time.sleep(backoff)
            backoff = min(30, backoff * 2)
