import os
import json
import math
import logging
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Any

import numpy as np
import redis
import torch
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

logger = logging.getLogger("ExoChain-STGNN-GraphBuilder")


# ============================================================
# CONFIGURATION
# ============================================================

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

NUM_NODES = int(os.getenv("STGNN_NUM_NODES", "50"))
TEMPORAL_WINDOW = int(os.getenv("STGNN_TEMPORAL_WINDOW", "12"))

# Maximum distance for spatial edges.
# Nautical-mile equivalent is converted from kilometers.
GRAPH_RADIUS_KM = float(os.getenv("STGNN_GRAPH_RADIUS_KM", "50.0"))

# Maximum number of spatial neighbors per vessel.
K_NEIGHBORS = int(os.getenv("STGNN_K_NEIGHBORS", "5"))

# Ignore vessels whose most recent AIS observation is older
# than this many seconds.
MAX_VESSEL_AGE_SECONDS = int(
    os.getenv("STGNN_MAX_VESSEL_AGE_SECONDS", "1800")
)


# ============================================================
# REDIS
# ============================================================

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    decode_responses=True
)


# ============================================================
# FEATURE DEFINITION
# ============================================================

FEATURE_NAMES = [
    "latitude",
    "longitude",
    "speed_knots",
    "course_sin",
    "course_cos",
    "heading_sin",
    "heading_cos",
    "delta_time_seconds",
]

NUM_FEATURES = len(FEATURE_NAMES)


# ============================================================
# GEO UTILITIES
# ============================================================

def haversine_km(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float
) -> float:

    earth_radius_km = 6371.0088

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)

    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_lat / 2) ** 2
        +
        math.cos(lat1_rad)
        * math.cos(lat2_rad)
        * math.sin(delta_lon / 2) ** 2
    )

    return 2 * earth_radius_km * math.asin(math.sqrt(a))


def normalize_angle(angle: float) -> Tuple[float, float]:
    """
    Convert heading/course from degrees into
    sine/cosine representation.
    """

    if angle is None:
        angle = 0.0

    radians = math.radians(float(angle))

    return math.sin(radians), math.cos(radians)


# ============================================================
# REDIS DISCOVERY
# ============================================================

def discover_active_vessels() -> List[Dict[str, Any]]:
    """
    Discover vessel state records currently stored in Redis.
    """

    vessels = []

    cursor = 0

    pattern = "exochain:vessel:*:state"

    while True:

        cursor, keys = redis_client.scan(
            cursor=cursor,
            match=pattern,
            count=500
        )

        for key in keys:

            state = redis_client.hgetall(key)

            if not state:
                continue

            try:
                mmsi = int(state["mmsi"])

                lat = float(state["lat"])
                lon = float(state["lon"])

            except (KeyError, TypeError, ValueError):
                continue

            timestamp = state.get("timestamp")

            if timestamp is None:
                continue

            try:
                timestamp_value = float(timestamp)
            except ValueError:
                continue

            # ------------------------------------------------
            # Reject stale vessels
            # ------------------------------------------------

            now = datetime.now(timezone.utc).timestamp()

            if now - timestamp_value > MAX_VESSEL_AGE_SECONDS:
                continue

            vessels.append(
                {
                    "mmsi": mmsi,
                    "vessel_name": state.get(
                        "vessel_name",
                        "UNKNOWN"
                    ),
                    "lat": lat,
                    "lon": lon,
                    "speed_knots": float(
                        state.get("speed_knots", 0.0)
                    ),
                    "course_over_ground": float(
                        state.get("course_over_ground", 0.0)
                    ),
                    "heading": float(
                        state.get("heading", 0.0)
                    ),
                    "timestamp": timestamp_value,
                }
            )

        if cursor == 0:
            break

    # Most recently updated vessels first.
    vessels.sort(
        key=lambda vessel: vessel["timestamp"],
        reverse=True
    )

    # Deterministic maximum graph size.
    vessels = vessels[:NUM_NODES]

    logger.info(
        "ACTIVE VESSELS | discovered=%d | selected=%d/%d",
        len(vessels),
        len(vessels),
        NUM_NODES
    )

    return vessels


# ============================================================
# TEMPORAL HISTORY
# ============================================================

def get_vessel_history(mmsi: int) -> List[Dict[str, Any]]:
    """
    Retrieve the temporal AIS history stored by
    maritime_consumer.py.
    """

    key = f"exochain:vessel:{mmsi}:history"

    raw_history = redis_client.lrange(
        key,
        -TEMPORAL_WINDOW,
        -1
    )

    history = []

    for raw_event in raw_history:

        try:
            event = json.loads(raw_event)
        except json.JSONDecodeError:
            continue

        try:
            history.append(
                {
                    "lat": float(event["lat"]),
                    "lon": float(event["lon"]),
                    "speed_knots": float(
                        event.get("speed_knots", 0.0)
                    ),
                    "course_over_ground": float(
                        event.get("course_over_ground", 0.0)
                    ),
                    "heading": float(
                        event.get("heading", 0.0)
                    ),
                    "timestamp": float(event["timestamp"]),
                }
            )

        except (KeyError, TypeError, ValueError):
            continue

    history.sort(
        key=lambda event: event["timestamp"]
    )

    return history[-TEMPORAL_WINDOW:]


# ============================================================
# FEATURE CONSTRUCTION
# ============================================================

def build_vessel_features(
    history: List[Dict[str, Any]]
) -> np.ndarray:

    features = []

    if not history:
        return np.zeros(
            (TEMPORAL_WINDOW, NUM_FEATURES),
            dtype=np.float32
        )

    for i, event in enumerate(history):

        if i == 0:
            delta_time = 0.0
        else:
            delta_time = (
                event["timestamp"]
                -
                history[i - 1]["timestamp"]
            )

        course_sin, course_cos = normalize_angle(
            event["course_over_ground"]
        )

        heading_sin, heading_cos = normalize_angle(
            event["heading"]
        )

        features.append(
            [
                event["lat"],
                event["lon"],
                event["speed_knots"],
                course_sin,
                course_cos,
                heading_sin,
                heading_cos,
                delta_time,
            ]
        )

    features = np.asarray(
        features,
        dtype=np.float32
    )

    # --------------------------------------------------------
    # Pad history at the beginning.
    # --------------------------------------------------------

    if len(features) < TEMPORAL_WINDOW:

        padding = np.repeat(
            features[0:1],
            TEMPORAL_WINDOW - len(features),
            axis=0
        )

        features = np.concatenate(
            [padding, features],
            axis=0
        )

    return features[-TEMPORAL_WINDOW:]


# ============================================================
# SPATIAL GRAPH
# ============================================================

def build_spatial_graph(
    vessels: List[Dict[str, Any]]
) -> Tuple[torch.Tensor, torch.Tensor]:

    edges = []
    edge_attributes = []

    num_vessels = len(vessels)

    # --------------------------------------------------------
    # Pairwise distances
    # --------------------------------------------------------

    distances = np.full(
        (num_vessels, num_vessels),
        np.inf,
        dtype=np.float32
    )

    for i in range(num_vessels):

        for j in range(num_vessels):

            if i == j:
                continue

            distance = haversine_km(
                vessels[i]["lat"],
                vessels[i]["lon"],
                vessels[j]["lat"],
                vessels[j]["lon"]
            )

            distances[i, j] = distance

    # --------------------------------------------------------
    # k-nearest spatial neighbors
    # --------------------------------------------------------

    for i in range(num_vessels):

        neighbors = np.argsort(
            distances[i]
        )[:K_NEIGHBORS]

        for j in neighbors:

            distance = distances[i, j]

            if distance > GRAPH_RADIUS_KM:
                continue

            edges.append([i, int(j)])

            # Edge feature = geographic distance.
            edge_attributes.append(
                [float(distance)]
            )

    if not edges:

        edge_index = torch.empty(
            (2, 0),
            dtype=torch.long
        )

        edge_attr = torch.empty(
            (0, 1),
            dtype=torch.float32
        )

    else:

        edge_index = torch.tensor(
            edges,
            dtype=torch.long
        ).t().contiguous()

        edge_attr = torch.tensor(
            edge_attributes,
            dtype=torch.float32
        )

    logger.info(
        "SPATIAL GRAPH | nodes=%d | edges=%d",
        num_vessels,
        edge_index.shape[1]
    )

    return edge_index, edge_attr


# ============================================================
# GRAPH BUILDER
# ============================================================

def build_graph() -> Dict[str, Any]:
    """
    Build the complete ST-GNN graph from Redis.
    """

    vessels = discover_active_vessels()

    if not vessels:

        logger.warning(
            "No active vessels available for ST-GNN graph."
        )

        return {
            "x": None,
            "edge_index": None,
            "edge_attr": None,
            "vessels": [],
            "mmsi_to_node": {},
        }

    node_features = []

    valid_vessels = []

    for vessel in vessels:

        history = get_vessel_history(
            vessel["mmsi"]
        )

        if not history:
            continue

        features = build_vessel_features(
            history
        )

        node_features.append(features)
        valid_vessels.append(vessel)

    if not valid_vessels:

        logger.warning(
            "No vessels have usable temporal history."
        )

        return {
            "x": None,
            "edge_index": None,
            "edge_attr": None,
            "vessels": [],
            "mmsi_to_node": {},
        }

    vessels = valid_vessels

    # --------------------------------------------------------
    # X = [T, N, F]
    # --------------------------------------------------------

    x = np.stack(
        node_features,
        axis=1
    )

    x = torch.tensor(
        x,
        dtype=torch.float32
    )

    # --------------------------------------------------------
    # Spatial graph
    # --------------------------------------------------------

    edge_index, edge_attr = build_spatial_graph(
        vessels
    )

    mmsi_to_node = {
        vessel["mmsi"]: index
        for index, vessel in enumerate(vessels)
    }

    logger.info(
        "ST-GNN GRAPH READY | X=%s | edge_index=%s | edge_attr=%s",
        tuple(x.shape),
        tuple(edge_index.shape),
        tuple(edge_attr.shape)
    )

    return {
        "x": x,
        "edge_index": edge_index,
        "edge_attr": edge_attr,
        "vessels": vessels,
        "mmsi_to_node": mmsi_to_node,
    }


# ============================================================
# STANDALONE TEST
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Starting ExoChain ST-GNN Graph Builder..."
    )

    redis_client.ping()

    graph = build_graph()

    if graph["x"] is None:

        logger.warning(
            "Graph could not be constructed."
        )

    else:

        print("\n" + "=" * 70)
        print("EXOCHAIN ST-GNN GRAPH")
        print("=" * 70)

        print(
            f"Temporal tensor : {tuple(graph['x'].shape)}"
        )

        print(
            f"Edge index      : {tuple(graph['edge_index'].shape)}"
        )

        print(
            f"Edge attributes : {tuple(graph['edge_attr'].shape)}"
        )

        print(
            f"Vessels         : {len(graph['vessels'])}"
        )

        print("\nVessel → Node Mapping")

        for vessel in graph["vessels"][:10]:

            print(
                f"  {vessel['mmsi']} | "
                f"{vessel['vessel_name']} → "
                f"node {graph['mmsi_to_node'][vessel['mmsi']]}"
            )