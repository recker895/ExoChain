"""Transactional audit outbox publisher. Kafka failures never erase audit events."""

import json
from kafka import KafkaProducer
from config.settings import settings


class OutboxPublisher:
    def __init__(self, store):
        self.store, self.producer = store, None

    def flush(self, limit=100):
        if self.producer is None:
            self.producer = KafkaProducer(
                bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
                acks="all",
                retries=3,
                max_block_ms=3000,
                request_timeout_ms=5000,
                value_serializer=lambda value: json.dumps(value).encode(),
            )
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT sequence,body FROM events WHERE published=0 ORDER BY sequence LIMIT ?",
                (limit,),
            ).fetchall()
        for row in rows:
            event = json.loads(row[1])
            # At-least-once publication; consumers deduplicate using event.id.
            self.producer.send(
                "agent-decision-logs", key=event["run_id"].encode(), value=event
            ).get(timeout=5)
            with self.store.connect() as db:
                db.execute("UPDATE events SET published=1 WHERE sequence=?", (row[0],))
        return len(rows)

    def close(self):
        if self.producer:
            self.producer.close(timeout=5)
