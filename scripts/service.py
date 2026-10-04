"""Load file-backed worker secrets and propagate termination to existing workers."""

import os
import runpy
import signal
import sys
from config.settings import settings

if __name__ == "__main__":
    for key in (
        "AISSTREAM_API_KEY",
        "COPERNICUSMARINE_SERVICE_USERNAME",
        "COPERNICUSMARINE_SERVICE_PASSWORD",
    ):
        value = getattr(settings, key, None)
        if value is not None:
            os.environ[key] = (
                value.get_secret_value()
                if hasattr(value, "get_secret_value")
                else str(value)
            )
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    runpy.run_module(sys.argv[1], run_name="__main__")
