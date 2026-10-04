import json
import logging
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "run_id": getattr(record, "run_id", None),
                "trace_id": getattr(record, "trace_id", None),
            }
        )


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    logger = logging.getLogger("exochain.runtime")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
