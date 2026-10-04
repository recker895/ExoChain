"""Idempotently provision required local Kafka topics without deleting data."""

from kafka.admin import KafkaAdminClient, NewTopic
from config.settings import settings


def main():
    client = KafkaAdminClient(bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS)
    try:
        names = {
            settings.KAFKA_TOPIC_MARITIME,
            settings.KAFKA_TOPIC_AVIATION,
            settings.KAFKA_TOPIC_GRAPH_EVENTS,
            "agent-decision-logs",
        }
        missing = names - set(client.list_topics())
        if missing:
            client.create_topics(
                [
                    NewTopic(name, num_partitions=6, replication_factor=1)
                    for name in sorted(missing)
                ]
            )
        print({"topics": sorted(names), "created": sorted(missing)})
    finally:
        client.close()


if __name__ == "__main__":
    main()
