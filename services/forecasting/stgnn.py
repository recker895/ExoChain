"""Admit only approved, integrity-checked deployment artifacts and complete AIS windows.

Shares the actual training architecture, aggregation, features, normalization and
adjacency code. No random weights, temporal interpolation or invented predictions.
"""

import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config.settings import settings
from core.schemas.contracts import IntelligenceResult, Quality, utcnow

FEATURES = [
    "latitude",
    "longitude",
    "speed_knots",
    "course_sin",
    "course_cos",
    "heading_sin",
    "heading_cos",
    "delta_time_seconds",
]
PREPROCESSING_VERSION = "ais-bin-mean-circular-v1-complete-window"


def prepare_inputs(data, manifest):
    import numpy as np
    from services.stgnn_dataset_builder import (
        aggregate_bin_observations,
        observation_to_features,
        TIME_BIN_SECONDS,
        INPUT_WINDOW,
        MAX_NODES,
    )

    if (manifest["bin_seconds"], manifest["input_steps"], manifest["max_nodes"]) != (
        TIME_BIN_SECONDS,
        INPUT_WINDOW,
        MAX_NODES,
    ):
        raise ValueError("training preprocessing configuration mismatch")
    now = utcnow().timestamp()
    end = int(now // TIME_BIN_SECONDS) - 1
    window = list(range(end - INPUT_WINDOW + 1, end + 1))
    selected = []
    for vessel in sorted(data.vessels, key=lambda v: str(v["mmsi"])):
        if vessel.get("quality") != "VALID":
            continue
        groups = defaultdict(list)
        for point in vessel.get("history", []):
            if any(
                point.get(k) is None
                for k in (
                    "latitude",
                    "longitude",
                    "speed_knots",
                    "course_over_ground",
                    "heading",
                    "timestamp",
                )
            ):
                continue
            observed = datetime.fromisoformat(
                point["timestamp"].replace("Z", "+00:00")
            ).timestamp()
            bucket = int(observed // TIME_BIN_SECONDS)
            if bucket not in window:
                continue
            groups[bucket].append(
                {
                    "mmsi": str(vessel["mmsi"]),
                    "vessel_name": vessel.get("vessel_name"),
                    "lat": point["latitude"],
                    "lon": point["longitude"],
                    "speed_knots": point["speed_knots"],
                    "course": point["course_over_ground"],
                    "heading": point["heading"],
                    "timestamp": observed,
                }
            )
        if all(b in groups for b in window):
            observations = [aggregate_bin_observations(groups[b]) for b in window]
            selected.append((str(vessel["mmsi"]), observations))
        if len(selected) == MAX_NODES:
            break
    if not selected:
        raise ValueError("complete fresh feature windows unavailable")
    raw = np.zeros((1, INPUT_WINDOW, MAX_NODES, 8), dtype=np.float32)
    mask = np.zeros((1, INPUT_WINDOW, MAX_NODES), dtype=np.float32)
    for node, (_, observations) in enumerate(selected):
        for step, point in enumerate(observations):
            raw[0, step, node] = observation_to_features(
                point, observations[step - 1]["timestamp"] if step else None
            )
            mask[0, step, node] = 1
    return raw, mask, selected, end


def validated_forecast(data):
    def unavailable(reason):
        return IntelligenceResult(
            agent="intelligence.forecast",
            status="MODEL_UNAVAILABLE",
            output={"trajectories": []},
            model_source="ST-GNN admission gate",
            data_quality=Quality.UNAVAILABLE,
            explanation=reason,
        )
    if not settings.STGNN_MODEL_MANIFEST:
        return unavailable("No approved deployment manifest configured")
    try:
        location = Path(settings.STGNN_MODEL_MANIFEST).resolve()
        manifest = json.loads(location.read_text(encoding="utf-8"))
        required = {
            "model_version",
            "checkpoint",
            "checkpoint_sha256",
            "normalization",
            "normalization_sha256",
            "preprocessing_version",
            "feature_names",
            "validation_report",
            "validation_report_sha256",
            "approved",
            "bin_seconds",
            "input_steps",
            "max_nodes",
        }
        if (
            not required <= manifest.keys()
            or manifest["approved"] is not True
            or not manifest["model_version"]
        ):
            return unavailable("Deployment manifest incomplete or unapproved")
        if (
            manifest["feature_names"] != FEATURES
            or manifest["preprocessing_version"] != PREPROCESSING_VERSION
        ):
            return unavailable("Model feature/preprocessing contract mismatch")
        paths = {}
        for name in ("checkpoint", "normalization", "validation_report"):
            path = (location.parent / manifest[name]).resolve()
            if (
                hashlib.sha256(path.read_bytes()).hexdigest()
                != manifest[name + "_sha256"]
            ):
                return unavailable("Deployment artifact integrity mismatch")
            paths[name] = path
        report = json.loads(paths["validation_report"].read_text(encoding="utf-8"))
        if (
            report.get("model_version") != manifest["model_version"]
            or report.get("preprocessing_version") != PREPROCESSING_VERSION
            or report.get("approved_for_trajectory_inference") is not True
        ):
            return unavailable(
                "Independent deployment validation is missing or mismatched"
            )
        import numpy as np
        import torch
        from models.st_gnn.train_stgnn import ExoChainSTGNN, build_dynamic_adjacency

        raw, mask, selected, end = prepare_inputs(data, manifest)
        norms = json.loads(paths["normalization"].read_text(encoding="utf-8"))
        means = np.array(
            [norms["features"][n]["mean"] for n in FEATURES], dtype=np.float32
        )
        stds = np.array(
            [norms["features"][n]["std"] for n in FEATURES], dtype=np.float32
        )
        if (
            not np.isfinite(means).all()
            or not np.isfinite(stds).all()
            or (stds <= 0).any()
        ):
            return unavailable("Invalid training normalization")
        checkpoint = torch.load(
            paths["checkpoint"], map_location="cpu", weights_only=True
        )
        config = checkpoint["config"]
        if config["input_features"] != 8 or config["output_features"] != 2:
            return unavailable("Checkpoint architecture mismatch")
        model = ExoChainSTGNN(
            **{
                k: config[k]
                for k in (
                    "input_features",
                    "hidden_dim",
                    "graph_layers",
                    "gru_layers",
                    "prediction_horizon",
                    "output_features",
                )
            }
        )
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
        model.eval()
        edge_index, edge_attr = build_dynamic_adjacency(
            torch.from_numpy(raw[..., :2]),
            torch.from_numpy(mask),
            k_neighbors=config["k_neighbors"],
            radius_km=config["graph_radius_km"],
        )
        with torch.inference_mode():
            prediction = model(
                torch.from_numpy((raw - means) / stds), edge_index, edge_attr
            ).numpy()[0]
        targets = ["delta_latitude", "delta_longitude"]
        prediction = prediction * np.array(
            [norms["targets"][n]["std"] for n in targets]
        ) + np.array([norms["targets"][n]["mean"] for n in targets])
        trajectories = []
        for i, (mmsi, observations) in enumerate(selected):
            points = prediction[:, i] + raw[0, -1, i, :2]
            if (
                not np.isfinite(points).all()
                or (np.abs(points[:, 0]) > 90).any()
                or (np.abs(points[:, 1]) > 180).any()
            ):
                return unavailable("Inference produced invalid coordinates")
            trajectories.append(
                {
                    "entity_id": mmsi,
                    "origin_observed_at": observations[-1]["timestamp"],
                    "positions": [
                        {
                            "latitude": float(p[0]),
                            "longitude": float(p[1]),
                            "forecast_bin_start_unix": (end + h + 1)
                            * manifest["bin_seconds"],
                        }
                        for h, p in enumerate(points)
                    ],
                }
            )
        return IntelligenceResult(
            agent="intelligence.forecast",
            status="SUCCESS",
            input_references=[m for m, _ in selected],
            output={
                "trajectories": trajectories,
                "prediction_type": "MODEL_TRAJECTORY_NOT_NAVIGABLE_ROUTE",
                "checkpoint_sha256": manifest["checkpoint_sha256"],
                "preprocessing_version": PREPROCESSING_VERSION,
            },
            model_source="ExoChainSTGNN",
            model_version=manifest["model_version"],
            data_quality=Quality.VALID,
            explanation="Approved trajectory model on complete observed time bins. Confidence is uncalibrated and remains unavailable; no routing or congestion prediction implied.",
        )
    except Exception as exc:
        return unavailable(
            "Deployment artifacts or inference unavailable: " + type(exc).__name__
        )
