import os
import json
import logging
from dotenv import load_dotenv
from kafka import KafkaProducer
from kafka.errors import KafkaError

load_dotenv()

logger = logging.getLogger("ExoChain-Kafka")

KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

KAFKA_TOPIC_MARITIME = os.getenv("KAFKA_TOPIC_MARITIME", "telemetry-ais")

KAFKA_TOPIC_AVIATION = os.getenv("KAFKA_TOPIC_AVIATION", "telemetry-opensky")

KAFKA_TOPIC_GRAPH = os.getenv("KAFKA_TOPIC_GRAPH_EVENTS", "stgnn-graph-events")

KAFKA_TOPIC_AGENT = "agent-decision-logs"


class ExoChainKafkaPipeline:
    def __init__(self):

        logger.info("Initializing Kafka producer: %s", KAFKA_BOOTSTRAP_SERVERS)

        self.producer = KafkaProducer(
            bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
            # JSON serialization
            value_serializer=lambda value: json.dumps(value).encode("utf-8"),
            # Production-oriented delivery settings
            acks="all",
            retries=10,
            # Batching
            linger_ms=10,
            batch_size=32768,
            # Compression
            compression_type="gzip",
            # Timeouts
            request_timeout_ms=30000,
            delivery_timeout_ms=120000,
            # Connection resilience
            reconnect_backoff_ms=100,
            reconnect_backoff_max_ms=5000,
            # Avoid oversized messages
            max_request_size=1048576,
            # Don't block forever
            max_block_ms=10000,
        )

        logger.info("Kafka producer connected successfully.")

    # --------------------------------------------------------------
    # Generic publisher
    # --------------------------------------------------------------

    def _publish(self, topic: str, payload: dict, key: str | None = None):

        try:
            future = self.producer.send(
                topic=topic,
                key=(key.encode("utf-8") if key is not None else None),
                value=payload,
            )

            # Wait for Kafka acknowledgement.
            metadata = future.get(timeout=30)

            logger.info(
                "Kafka delivery confirmed | topic=%s | partition=%s | offset=%s",
                metadata.topic,
                metadata.partition,
                metadata.offset,
            )

            return {
                "success": True,
                "topic": metadata.topic,
                "partition": metadata.partition,
                "offset": metadata.offset,
            }

        except KafkaError as exc:
            logger.error("Kafka delivery failed | topic=%s | error=%s", topic, exc)

            raise

        except Exception as exc:
            logger.error(
                "Unexpected Kafka publishing error | topic=%s | error=%s", topic, exc
            )

            raise

    # --------------------------------------------------------------
    # Maritime AIS
    # --------------------------------------------------------------

    def publish_ais_position(self, vessel_data: dict):

        mmsi = vessel_data.get("mmsi")

        return self._publish(
            topic=KAFKA_TOPIC_MARITIME,
            payload=vessel_data,
            key=str(mmsi) if mmsi else None,
        )

    # --------------------------------------------------------------
    # Aviation
    # --------------------------------------------------------------

    def publish_aviation_position(self, aircraft_data: dict):

        icao24 = aircraft_data.get("icao24")

        return self._publish(
            topic=KAFKA_TOPIC_AVIATION,
            payload=aircraft_data,
            key=str(icao24) if icao24 else None,
        )

    # --------------------------------------------------------------
    # ST-GNN graph events
    # --------------------------------------------------------------

    def publish_risk_event(self, risk_data: dict):

        node = risk_data.get("node")

        return self._publish(
            topic=KAFKA_TOPIC_GRAPH, payload=risk_data, key=str(node) if node else None
        )

    # --------------------------------------------------------------
    # Agent decisions
    # --------------------------------------------------------------

    def publish_agent_decision(self, agent_output: dict):

        agent = agent_output.get("agent")

        return self._publish(
            topic=KAFKA_TOPIC_AGENT,
            payload=agent_output,
            key=str(agent) if agent else None,
        )

    # --------------------------------------------------------------
    # Shutdown
    # --------------------------------------------------------------

    def close(self):

        if self.producer:
            logger.info("Flushing Kafka producer...")

            self.producer.flush()

            logger.info("Closing Kafka producer...")

            self.producer.close()


# ------------------------------------------------------------------
# Global pipeline
# ------------------------------------------------------------------


class LazyKafkaPipeline:
    """No broker connection at import time; preserve the existing publisher API."""

    def __init__(self):
        import threading

        self._instance = None
        self._lock = threading.Lock()

    def __getattr__(self, name):
        with self._lock:
            if self._instance is None:
                self._instance = ExoChainKafkaPipeline()
        return getattr(self._instance, name)


kafka_pipeline = LazyKafkaPipeline()
