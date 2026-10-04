"""Compatibility entry point. Random-weight inference has been removed.

Training remains in train_stgnn.py; operational prediction requires a validated
DataSnapshot and deployment manifest through services.forecasting.stgnn.
"""
from services.forecasting.stgnn import validated_forecast


def run_inference(data=None):
    if data is None:
        raise RuntimeError("MODEL_UNAVAILABLE: pass a canonical DataSnapshot to validated_forecast")
    return validated_forecast(data)
