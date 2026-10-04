"""Optional integration against real Redis, confined to unique test-only keys."""

import json
import os
from uuid import uuid4

import pytest
import redis

from config.settings import settings
from services.maritime_consumer import process_ais_event


@pytest.mark.skipif(
    os.getenv("REDIS_INTEGRATION") != "1",
    reason="Set REDIS_INTEGRATION=1 for isolated real Redis verification",
)
def test_atomic_replay_missing_navigation_and_static_preservation():
    client = redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        decode_responses=True,
    )
    prefix = "exochain-test:" + uuid4().hex + ":"
    keys = [
        prefix + "exochain:vessel:test-mmsi:state",
        prefix + "exochain:vessel:test-mmsi:history",
        prefix + "exochain:vessels:recent",
    ]

    class Isolated:
        def eval(self, script, count, *args):
            return client.eval(
                script, count, *[prefix + key for key in args[:count]], *args[count:]
            )

        def hset(self, key, **kwargs):
            return client.hset(prefix + key, **kwargs)

    writer = Isolated()
    position = dict(
        mmsi="test-mmsi",
        lat=1,
        lon=2,
        timestamp=1000,
        speed_knots=5,
        course_over_ground=90,
        heading=90,
    )
    try:
        process_ais_event(position, writer)
        process_ais_event(position, writer)
        assert client.llen(keys[1]) == 1
        process_ais_event(dict(position, timestamp=999), writer)
        assert client.llen(keys[1]) == 1
        process_ais_event(
            dict(
                event_type="AIS_STATIC_REPORT",
                mmsi="test-mmsi",
                destination="TEST_ONLY",
                characteristics={"draft_m": 5},
                timestamp=1001,
            ),
            writer,
        )
        process_ais_event(
            dict(position, timestamp=1002, speed_knots=None, heading=None), writer
        )
        state = client.hgetall(keys[0])
        assert state["destination"] == "TEST_ONLY"
        assert "heading" not in state and "speed_knots" not in state
        assert json.loads(state["characteristics"])["draft_m"] == 5
        assert client.llen(keys[1]) == 2
        assert client.zscore(keys[2], "test-mmsi") == 1002
    finally:
        client.delete(*keys)
        client.close()
