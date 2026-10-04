"""
EXOCHAIN REAL AIS ST-GNN DATASET BUILDER

Builds a supervised spatio-temporal dataset from historical AIS events.

Output:
    X         [samples, INPUT_WINDOW, MAX_NODES, FEATURES]
    Y         [samples, PREDICTION_HORIZON, MAX_NODES, 2]
    node_mask [samples, TOTAL_STEPS, MAX_NODES]
    vessel_ids[samples, MAX_NODES]

Features:
    0 latitude
    1 longitude
    2 speed_knots
    3 course_sin
    4 course_cos
    5 heading_sin
    6 heading_cos
    7 delta_time_seconds

Y is future displacement in degrees:
    [delta_latitude, delta_longitude]
relative to the last input observation.

Important:
    - Input gaps may be interpolated when sufficiently short.
    - Future target observations must be real AIS observations.
    - Samples are padded/truncated to MAX_NODES so np.stack is always valid.
"""

from __future__ import annotations

import math
import os
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np


# ============================================================================
# CONFIGURATION
# ============================================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DB_PATH = PROJECT_ROOT / os.getenv(
    "AIS_HISTORY_DB_PATH",
    "data/ais_history.db",
)

OUTPUT_PATH = PROJECT_ROOT / os.getenv(
    "STGNN_DATASET_PATH",
    "data/stgnn/ais_stgnn_dataset.npz",
)

TIME_BIN_SECONDS = int(
    os.getenv("STGNN_TIME_BIN_SECONDS", "60")
)

INPUT_WINDOW = int(
    os.getenv("STGNN_TEMPORAL_WINDOW", "12")
)

PREDICTION_HORIZON = int(
    os.getenv("STGNN_PREDICTION_HORIZON", "6")
)

TOTAL_STEPS = INPUT_WINDOW + PREDICTION_HORIZON

MAX_NODES = int(
    os.getenv(
        "STGNN_MAX_NODES",
        os.getenv("STGNN_NUM_NODES", "50"),
    )
)

MIN_VESSEL_STEPS = int(
    os.getenv("STGNN_MIN_VESSEL_STEPS", "12")
)

MAX_BIN_GAP = int(
    os.getenv("STGNN_MAX_BIN_GAP", "3")
)

PRINT_EVERY = 10

FEATURE_DIM = 8
TARGET_DIM = 2


# ============================================================================
# TYPE DEFINITIONS
# ============================================================================

Observation = Dict[str, Any]

BinMap = Dict[
    int,
    Dict[str, Observation],
]


# ============================================================================
# GENERAL HELPERS
# ============================================================================

def safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        result = float(value)

        if math.isfinite(result):
            return result

    except (TypeError, ValueError):
        pass

    return default


def safe_int(
    value: Any,
    default: int = 0,
) -> int:
    try:
        return int(value)

    except (TypeError, ValueError):
        return default


def normalize_angle(
    angle: float,
) -> float:
    return angle % 360.0


def circular_interpolate(
    a: float,
    b: float,
    alpha: float,
) -> float:
    """
    Interpolate an angle using the shortest circular path.
    """

    a = normalize_angle(a)
    b = normalize_angle(b)

    delta = (
        (b - a + 180.0)
        % 360.0
        - 180.0
    )

    return normalize_angle(
        a + alpha * delta
    )


def circular_mean(
    values: Sequence[float],
) -> float:
    if not values:
        return 0.0

    radians = np.deg2rad(
        [
            normalize_angle(
                safe_float(value)
            )
            for value in values
        ]
    )

    sin_mean = float(
        np.mean(
            np.sin(radians)
        )
    )

    cos_mean = float(
        np.mean(
            np.cos(radians)
        )
    )

    if (
        abs(sin_mean) < 1e-12
        and abs(cos_mean) < 1e-12
    ):
        return 0.0

    return normalize_angle(
        math.degrees(
            math.atan2(
                sin_mean,
                cos_mean,
            )
        )
    )


# ============================================================================
# DATABASE
# ============================================================================

def open_database() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            "AIS history database not found:\n"
            f"{DB_PATH}"
        )

    conn = sqlite3.connect(
        str(DB_PATH)
    )

    conn.row_factory = sqlite3.Row

    return conn


def get_database_stats(
    conn: sqlite3.Connection,
) -> Tuple[int, int]:

    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_rows,
            COUNT(DISTINCT mmsi) AS unique_vessels
        FROM ais_positions
        """
    ).fetchone()

    if row is None:
        return 0, 0

    return (
        safe_int(
            row["total_rows"]
        ),
        safe_int(
            row["unique_vessels"]
        ),
    )


def load_events(
    conn: sqlite3.Connection,
) -> List[Observation]:
    """
    Load valid AIS position events.

    Events are ordered chronologically.
    """

    query = """
        SELECT
            mmsi,
            vessel_name,
            latitude,
            longitude,
            speed_knots,
            course_over_ground,
            heading,
            event_timestamp
        FROM ais_positions
        WHERE
            mmsi IS NOT NULL
            AND event_timestamp IS NOT NULL
            AND latitude IS NOT NULL
            AND longitude IS NOT NULL
        ORDER BY event_timestamp ASC
    """

    events: List[Observation] = []

    invalid = 0

    cursor = conn.execute(query)

    for row in cursor:

        mmsi = str(
            row["mmsi"]
        ).strip()

        lat = safe_float(
            row["latitude"],
            float("nan"),
        )

        lon = safe_float(
            row["longitude"],
            float("nan"),
        )

        timestamp = safe_float(
            row["event_timestamp"],
            float("nan"),
        )

        if (
            not mmsi
            or not math.isfinite(lat)
            or not math.isfinite(lon)
            or not math.isfinite(timestamp)
            or not -90.0 <= lat <= 90.0
            or not -180.0 <= lon <= 180.0
        ):
            invalid += 1
            continue

        speed = safe_float(
            row["speed_knots"],
            0.0,
        )

        course = safe_float(
            row["course_over_ground"],
            0.0,
        )

        heading = safe_float(
            row["heading"],
            course,
        )

        events.append(
            {
                "mmsi": mmsi,

                "vessel_name": str(
                    row["vessel_name"] or ""
                ),

                "lat": lat,

                "lon": lon,

                "speed_knots": max(
                    0.0,
                    speed,
                ),

                "course": normalize_angle(
                    course
                ),

                "heading": normalize_angle(
                    heading
                ),

                "timestamp": timestamp,
            }
        )

    print(
        f"Raw events                 : "
        f"{len(events) + invalid:,}"
    )

    print(
        f"Invalid rows skipped       : "
        f"{invalid:,}"
    )

    print(
        f"Valid AIS events           : "
        f"{len(events):,}"
    )

    return events


# ============================================================================
# TEMPORAL BINNING
# ============================================================================

def bin_timestamp(
    timestamp: float,
) -> int:

    return int(
        math.floor(
            timestamp
            / TIME_BIN_SECONDS
        )
    )


def aggregate_bin_observations(
    observations: Sequence[Observation],
) -> Observation:
    """
    Aggregate all AIS observations for one vessel
    inside one temporal bin.
    """

    observations = sorted(
        observations,
        key=lambda item: item["timestamp"],
    )

    latest = observations[-1]

    return {
        "mmsi": latest["mmsi"],

        "vessel_name": latest[
            "vessel_name"
        ],

        "lat": float(
            np.mean(
                [
                    item["lat"]
                    for item in observations
                ]
            )
        ),

        "lon": float(
            np.mean(
                [
                    item["lon"]
                    for item in observations
                ]
            )
        ),

        "speed_knots": float(
            np.mean(
                [
                    item["speed_knots"]
                    for item in observations
                ]
            )
        ),

        "course": circular_mean(
            [
                item["course"]
                for item in observations
            ]
        ),

        "heading": circular_mean(
            [
                item["heading"]
                for item in observations
            ]
        ),

        "timestamp": float(
            np.mean(
                [
                    item["timestamp"]
                    for item in observations
                ]
            )
        ),

        "real": True,
    }


def create_temporal_bins(
    events: Sequence[Observation],
) -> BinMap:
    """
    Creates:

        bins[time_bin][mmsi] = observation
    """

    raw_groups: Dict[
        int,
        Dict[
            str,
            List[Observation],
        ],
    ] = defaultdict(
        lambda: defaultdict(list)
    )

    for event in events:

        time_bin = bin_timestamp(
            event["timestamp"]
        )

        raw_groups[
            time_bin
        ][
            event["mmsi"]
        ].append(event)

    bins: BinMap = {}

    for time_bin in sorted(
        raw_groups
    ):

        bins[time_bin] = {}

        for (
            mmsi,
            observations,
        ) in raw_groups[
            time_bin
        ].items():

            bins[time_bin][mmsi] = (
                aggregate_bin_observations(
                    observations
                )
            )

    total_states = sum(
        len(v)
        for v in bins.values()
    )

    print(
        f"Unique time bins           : "
        f"{len(bins):,}"
    )

    print(
        f"Time-bin vessel states     : "
        f"{total_states:,}"
    )

    return bins


# ============================================================================
# WINDOW DISCOVERY
# ============================================================================

def find_candidate_windows(
    bins: BinMap,
) -> List[List[int]]:
    """
    Discover groups of TOTAL_STEPS temporal bins.

    The AIS stream can contain short global gaps.

    MAX_BIN_GAP allows those short gaps without
    accepting arbitrarily discontinuous windows.
    """

    sorted_bins = sorted(
        bins.keys()
    )

    windows: List[
        List[int]
    ] = []

    if len(sorted_bins) < TOTAL_STEPS:
        return windows

    for start in range(
        0,
        len(sorted_bins)
        - TOTAL_STEPS
        + 1,
    ):

        candidate = sorted_bins[
            start:
            start + TOTAL_STEPS
        ]

        valid = True

        for (
            left,
            right,
        ) in zip(
            candidate,
            candidate[1:],
        ):

            gap = right - left

            if (
                gap <= 0
                or gap > MAX_BIN_GAP
            ):
                valid = False
                break

        if valid:
            windows.append(
                candidate
            )

    return windows


# ============================================================================
# INTERPOLATION
# ============================================================================

def interpolate_observation(
    previous: Observation,
    following: Observation,
    target_timestamp: float,
) -> Observation:
    """
    Interpolate an observation between two real AIS states.

    Used only for input reconstruction.

    Future labels are never generated by interpolation.
    """

    t0 = previous[
        "timestamp"
    ]

    t1 = following[
        "timestamp"
    ]

    if t1 <= t0:
        return dict(previous)

    alpha = (
        target_timestamp - t0
    ) / (t1 - t0)

    alpha = max(
        0.0,
        min(
            1.0,
            alpha,
        ),
    )

    return {
        "mmsi": previous[
            "mmsi"
        ],

        "vessel_name": (
            previous["vessel_name"]
            or following["vessel_name"]
        ),

        "lat": (
            previous["lat"]
            + alpha
            * (
                following["lat"]
                - previous["lat"]
            )
        ),

        "lon": (
            previous["lon"]
            + alpha
            * (
                following["lon"]
                - previous["lon"]
            )
        ),

        "speed_knots": (
            previous["speed_knots"]
            + alpha
            * (
                following["speed_knots"]
                - previous["speed_knots"]
            )
        ),

        "course": circular_interpolate(
            previous["course"],
            following["course"],
            alpha,
        ),

        "heading": circular_interpolate(
            previous["heading"],
            following["heading"],
            alpha,
        ),

        "timestamp": target_timestamp,

        "real": False,
    }


# ============================================================================
# VESSEL TRAJECTORY RECONSTRUCTION
# ============================================================================

def reconstruct_vessel_window(
    mmsi: str,
    bins: BinMap,
    window_bins: Sequence[int],
) -> Optional[
    List[Observation]
]:
    """
    Reconstruct one vessel over the full
    input + prediction window.

    Rules:

        1. Future observations MUST be real.
        2. Input internal gaps may be interpolated.
        3. Vessel needs MIN_VESSEL_STEPS real observations.
        4. Interpolation cannot cross MAX_BIN_GAP.
    """

    observations: List[
        Optional[Observation]
    ] = [
        bins[time_bin].get(mmsi)
        for time_bin in window_bins
    ]

    input_observations = (
        observations[
            :INPUT_WINDOW
        ]
    )

    future_observations = (
        observations[
            INPUT_WINDOW:
        ]
    )

    # ------------------------------------------------------------------
    # FUTURE MUST BE REAL
    # ------------------------------------------------------------------

    if any(
        obs is None
        for obs in future_observations
    ):
        return None

    real_count = sum(
        obs is not None
        for obs in observations
    )

    if (
        real_count
        < MIN_VESSEL_STEPS
    ):
        return None

    if all(
        obs is None
        for obs in input_observations
    ):
        return None

    reconstructed: List[
        Observation
    ] = []

    # ------------------------------------------------------------------
    # INPUT WINDOW
    # ------------------------------------------------------------------

    for index, obs in enumerate(
        input_observations
    ):

        if obs is not None:

            reconstructed.append(
                dict(obs)
            )

            continue

        previous_index = None
        following_index = None

        # Search backward.
        for j in range(
            index - 1,
            -1,
            -1,
        ):

            if (
                input_observations[j]
                is not None
            ):

                previous_index = j
                break

        # Search forward.
        for j in range(
            index + 1,
            INPUT_WINDOW,
        ):

            if (
                input_observations[j]
                is not None
            ):

                following_index = j
                break

        if (
            previous_index is None
            or following_index is None
        ):
            return None

        previous = (
            input_observations[
                previous_index
            ]
        )

        following = (
            input_observations[
                following_index
            ]
        )

        assert previous is not None
        assert following is not None

        bin_gap = (
            window_bins[
                following_index
            ]
            - window_bins[
                previous_index
            ]
        )

        if bin_gap > MAX_BIN_GAP:
            return None

        target_timestamp = (
            window_bins[index]
            * TIME_BIN_SECONDS
            + TIME_BIN_SECONDS / 2.0
        )

        reconstructed.append(
            interpolate_observation(
                previous,
                following,
                target_timestamp,
            )
        )

    # ------------------------------------------------------------------
    # FUTURE WINDOW
    # ------------------------------------------------------------------

    for obs in future_observations:

        assert obs is not None

        reconstructed.append(
            dict(obs)
        )

    if (
        len(reconstructed)
        != TOTAL_STEPS
    ):
        return None

    return reconstructed


# ============================================================================
# NODE SELECTION
# ============================================================================

def select_vessels_for_window(
    bins: BinMap,
    window_bins: Sequence[int],
) -> List[str]:
    """
    Select the strongest vessels for this graph.

    Priority:

        1. Future coverage
        2. Total real observations
        3. Input coverage
        4. MMSI deterministic tie-break
    """

    candidate_ids = set()

    for time_bin in window_bins:

        candidate_ids.update(
            bins[time_bin].keys()
        )

    scored: List[
        Tuple[
            Tuple[int, int, int, str],
            str,
        ]
    ] = []

    for mmsi in candidate_ids:

        observations = [
            bins[time_bin].get(mmsi)
            for time_bin in window_bins
        ]

        real_total = sum(
            obs is not None
            for obs in observations
        )

        input_real = sum(
            obs is not None
            for obs in observations[
                :INPUT_WINDOW
            ]
        )

        future_real = sum(
            obs is not None
            for obs in observations[
                INPUT_WINDOW:
            ]
        )

        # We require a complete future trajectory.
        if (
            future_real
            != PREDICTION_HORIZON
        ):
            continue

        if (
            real_total
            < MIN_VESSEL_STEPS
        ):
            continue

        score = (
            future_real,
            real_total,
            input_real,
            mmsi,
        )

        scored.append(
            (
                score,
                mmsi,
            )
        )

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return [
        mmsi
        for _, mmsi in scored[
            :MAX_NODES
        ]
    ]


# ============================================================================
# FEATURE ENGINEERING
# ============================================================================

def observation_to_features(
    observation: Observation,
    previous_timestamp: Optional[float],
) -> np.ndarray:
    """
    Convert AIS state to the 8-dimensional ST-GNN input vector.
    """

    course_rad = math.radians(
        normalize_angle(
            observation["course"]
        )
    )

    heading_rad = math.radians(
        normalize_angle(
            observation["heading"]
        )
    )

    if previous_timestamp is None:

        delta_time = 0.0

    else:

        delta_time = max(
            0.0,
            observation["timestamp"]
            - previous_timestamp,
        )

    return np.asarray(
        [
            observation["lat"],
            observation["lon"],
            observation["speed_knots"],
            math.sin(course_rad),
            math.cos(course_rad),
            math.sin(heading_rad),
            math.cos(heading_rad),
            delta_time,
        ],
        dtype=np.float32,
    )


# ============================================================================
# SAMPLE BUILDING
# ============================================================================

def build_sample_arrays(
    bins: BinMap,
    window_bins: Sequence[int],
    vessel_ids: Sequence[str],
) -> Optional[
    Tuple[
        np.ndarray,
        np.ndarray,
        np.ndarray,
    ]
]:
    """
    Build one fixed-size graph sample.

    Returns:

        X:
            [INPUT_WINDOW, MAX_NODES, FEATURE_DIM]

        Y:
            [PREDICTION_HORIZON, MAX_NODES, TARGET_DIM]

        node_mask:
            [TOTAL_STEPS, MAX_NODES]
    """

    x = np.zeros(
        (
            INPUT_WINDOW,
            MAX_NODES,
            FEATURE_DIM,
        ),
        dtype=np.float32,
    )

    y = np.zeros(
        (
            PREDICTION_HORIZON,
            MAX_NODES,
            TARGET_DIM,
        ),
        dtype=np.float32,
    )

    node_mask = np.zeros(
        (
            TOTAL_STEPS,
            MAX_NODES,
        ),
        dtype=np.float32,
    )

    valid_nodes = 0

    for node_index, mmsi in enumerate(
        vessel_ids
    ):

        if node_index >= MAX_NODES:
            break

        trajectory = (
            reconstruct_vessel_window(
                mmsi,
                bins,
                window_bins,
            )
        )

        if trajectory is None:
            continue

        valid_nodes += 1

        # --------------------------------------------------------------
        # INPUT FEATURES
        # --------------------------------------------------------------

        previous_timestamp: Optional[
            float
        ] = None

        for t in range(
            INPUT_WINDOW
        ):

            obs = trajectory[t]

            x[
                t,
                node_index,
                :,
            ] = observation_to_features(
                obs,
                previous_timestamp,
            )

            node_mask[
                t,
                node_index,
            ] = 1.0

            previous_timestamp = (
                obs["timestamp"]
            )

        # --------------------------------------------------------------
        # FUTURE TARGET
        # --------------------------------------------------------------

        reference = trajectory[
            INPUT_WINDOW - 1
        ]

        reference_lat = (
            reference["lat"]
        )

        reference_lon = (
            reference["lon"]
        )

        for h in range(
            PREDICTION_HORIZON
        ):

            obs = trajectory[
                INPUT_WINDOW + h
            ]

            y[
                h,
                node_index,
                0,
            ] = (
                obs["lat"]
                - reference_lat
            )

            y[
                h,
                node_index,
                1,
            ] = (
                obs["lon"]
                - reference_lon
            )

            node_mask[
                INPUT_WINDOW + h,
                node_index,
            ] = 1.0

    if valid_nodes <= 0:
        return None

    return (
        x,
        y,
        node_mask,
    )


# ============================================================================
# SHAPE NORMALIZATION
# ============================================================================

def pad_or_truncate_nodes(
    array: np.ndarray,
    target_nodes: int,
    fill_value: float = 0.0,
) -> np.ndarray:
    """
    Enforce a fixed node dimension.

    3D arrays:

        [T, N, F]

    2D arrays:

        [T, N]
    """

    array = np.asarray(
        array
    )

    if array.ndim == 3:

        current_nodes = (
            array.shape[1]
        )

        if (
            current_nodes
            == target_nodes
        ):
            return array

        if (
            current_nodes
            > target_nodes
        ):

            return array[
                :,
                :target_nodes,
                :,
            ]

        return np.pad(
            array,
            (
                (0, 0),
                (
                    0,
                    target_nodes
                    - current_nodes,
                ),
                (0, 0),
            ),
            mode="constant",
            constant_values=fill_value,
        )

    if array.ndim == 2:

        current_nodes = (
            array.shape[1]
        )

        if (
            current_nodes
            == target_nodes
        ):
            return array

        if (
            current_nodes
            > target_nodes
        ):

            return array[
                :,
                :target_nodes,
            ]

        return np.pad(
            array,
            (
                (0, 0),
                (
                    0,
                    target_nodes
                    - current_nodes,
                ),
            ),
            mode="constant",
            constant_values=fill_value,
        )

    raise ValueError(
        "Unsupported sample shape: "
        f"{array.shape}"
    )


def normalize_sample_shapes(
    x_samples: List[np.ndarray],
    y_samples: List[np.ndarray],
    mask_samples: List[np.ndarray],
    vessel_id_samples: List[List[str]],
) -> Tuple[
    List[np.ndarray],
    List[np.ndarray],
    List[np.ndarray],
    List[List[str]],
]:

    normalized_x = [
        pad_or_truncate_nodes(
            x,
            MAX_NODES,
            0.0,
        )
        for x in x_samples
    ]

    normalized_y = [
        pad_or_truncate_nodes(
            y,
            MAX_NODES,
            0.0,
        )
        for y in y_samples
    ]

    normalized_masks = [
        pad_or_truncate_nodes(
            mask,
            MAX_NODES,
            0.0,
        )
        for mask in mask_samples
    ]

    normalized_ids: List[
        List[str]
    ] = []

    for ids in vessel_id_samples:

        ids = list(
            ids[:MAX_NODES]
        )

        if len(ids) < MAX_NODES:

            ids.extend(
                [
                    ""
                ]
                * (
                    MAX_NODES
                    - len(ids)
                )
            )

        normalized_ids.append(
            ids
        )

    return (
        normalized_x,
        normalized_y,
        normalized_masks,
        normalized_ids,
    )


# ============================================================================
# DATASET BUILD
# ============================================================================

def build_dataset(
    bins: BinMap,
) -> Tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    int,
    int,
]:

    windows = (
        find_candidate_windows(
            bins
        )
    )

    print()
    print("-" * 72)
    print("TEMPORAL WINDOW ANALYSIS")
    print("-" * 72)

    print(
        f"Time bins                    : "
        f"{len(bins):,}"
    )

    print(
        f"Candidate {TOTAL_STEPS}-bin windows     : "
        f"{len(windows):,}"
    )

    x_samples: List[
        np.ndarray
    ] = []

    y_samples: List[
        np.ndarray
    ] = []

    mask_samples: List[
        np.ndarray
    ] = []

    vessel_id_samples: List[
        List[str]
    ] = []

    rejected = 0

    for (
        window_index,
        window_bins,
    ) in enumerate(
        windows,
        start=1,
    ):

        vessel_ids = (
            select_vessels_for_window(
                bins,
                window_bins,
            )
        )

        if not vessel_ids:

            rejected += 1

            continue

        # --------------------------------------------------------------
        # FINAL VALIDATION OF SELECTED NODES
        # --------------------------------------------------------------

        valid_vessels: List[
            str
        ] = []

        for mmsi in vessel_ids:

            trajectory = (
                reconstruct_vessel_window(
                    mmsi,
                    bins,
                    window_bins,
                )
            )

            if trajectory is not None:

                valid_vessels.append(
                    mmsi
                )

        if not valid_vessels:

            rejected += 1

            continue

        # --------------------------------------------------------------
        # BUILD SAMPLE
        # --------------------------------------------------------------

        sample = (
            build_sample_arrays(
                bins,
                window_bins,
                valid_vessels,
            )
        )

        if sample is None:

            rejected += 1

            continue

        x, y, mask = sample

        x_samples.append(x)

        y_samples.append(y)

        mask_samples.append(mask)

        vessel_id_samples.append(
            list(valid_vessels)
        )

        if (
            len(x_samples)
            % PRINT_EVERY
            == 0
        ):

            print(
                f"Valid samples               : "
                f"{len(x_samples)}"
            )

    print()
    print("-" * 72)
    print("DATASET RESULT")
    print("-" * 72)

    print(
        f"Candidate windows             : "
        f"{len(windows):,}"
    )

    print(
        f"Valid graph samples           : "
        f"{len(x_samples):,}"
    )

    print(
        f"Rejected windows              : "
        f"{rejected:,}"
    )

    if not x_samples:

        raise RuntimeError(
            "No valid ST-GNN samples "
            "were produced."
        )

    # ==================================================================
    # CRITICAL FIX
    #
    # Every sample MUST have exactly MAX_NODES nodes before np.stack().
    # ==================================================================

    (
        x_samples,
        y_samples,
        mask_samples,
        vessel_id_samples,
    ) = normalize_sample_shapes(
        x_samples,
        y_samples,
        mask_samples,
        vessel_id_samples,
    )

    # ==================================================================
    # DEFENSIVE SHAPE VALIDATION
    # ==================================================================

    x_shapes = {
        tuple(
            x.shape
        )
        for x in x_samples
    }

    y_shapes = {
        tuple(
            y.shape
        )
        for y in y_samples
    }

    mask_shapes = {
        tuple(
            mask.shape
        )
        for mask in mask_samples
    }

    if len(x_shapes) != 1:

        raise RuntimeError(
            "X samples still have "
            "inconsistent shapes: "
            f"{sorted(x_shapes)}"
        )

    if len(y_shapes) != 1:

        raise RuntimeError(
            "Y samples still have "
            "inconsistent shapes: "
            f"{sorted(y_shapes)}"
        )

    if len(mask_shapes) != 1:

        raise RuntimeError(
            "Node-mask samples still "
            "have inconsistent shapes: "
            f"{sorted(mask_shapes)}"
        )

    # ==================================================================
    # STACK
    # ==================================================================

    X = np.stack(
        x_samples,
        axis=0,
    ).astype(
        np.float32
    )

    Y = np.stack(
        y_samples,
        axis=0,
    ).astype(
        np.float32
    )

    node_mask = np.stack(
        mask_samples,
        axis=0,
    ).astype(
        np.float32
    )

    vessel_ids = np.asarray(
        vessel_id_samples,
        dtype=str,
    )

    return (
        X,
        Y,
        node_mask,
        vessel_ids,
        len(windows),
        rejected,
    )


# ============================================================================
# DATASET STATISTICS
# ============================================================================

def print_statistics(
    X: np.ndarray,
    Y: np.ndarray,
    node_mask: np.ndarray,
) -> None:

    print()
    print("-" * 72)
    print("FINAL DATASET")
    print("-" * 72)

    print(
        f"X shape                      : "
        f"{X.shape}"
    )

    print(
        f"Y shape                      : "
        f"{Y.shape}"
    )

    print(
        f"Node mask shape              : "
        f"{node_mask.shape}"
    )

    print()

    print(
        f"X dtype                      : "
        f"{X.dtype}"
    )

    print(
        f"Y dtype                      : "
        f"{Y.dtype}"
    )

    valid_input_nodes = (
        node_mask[
            :,
            :INPUT_WINDOW,
            :,
        ]
    )

    valid_future_nodes = (
        node_mask[
            :,
            INPUT_WINDOW:,
            :,
        ]
    )

    print(
        f"Input observations           : "
        f"{int(valid_input_nodes.sum()):,}"
    )

    print(
        f"Future observations          : "
        f"{int(valid_future_nodes.sum()):,}"
    )

    if X.size:

        print(
            f"Latitude range               : "
            f"{float(X[:, :, :, 0].min()):.6f} "
            f"to "
            f"{float(X[:, :, :, 0].max()):.6f}"
        )

        print(
            f"Longitude range              : "
            f"{float(X[:, :, :, 1].min()):.6f} "
            f"to "
            f"{float(X[:, :, :, 1].max()):.6f}"
        )

        print(
            f"Speed range (knots)          : "
            f"{float(X[:, :, :, 2].min()):.3f} "
            f"to "
            f"{float(X[:, :, :, 2].max()):.3f}"
        )

    if Y.size:

        print(
            f"Target Δlat range            : "
            f"{float(Y[:, :, :, 0].min()):.8f} "
            f"to "
            f"{float(Y[:, :, :, 0].max()):.8f}"
        )

        print(
            f"Target Δlon range            : "
            f"{float(Y[:, :, :, 1].min()):.8f} "
            f"to "
            f"{float(Y[:, :, :, 1].max()):.8f}"
        )

    nonzero_nodes = (
        node_mask[
            :,
            :INPUT_WINDOW,
            :,
        ].sum(axis=1)
    )

    print(
        f"Average input nodes/sample  : "
        f"{float(nonzero_nodes.mean()):.2f}"
    )

    print(
        f"Min input nodes/sample      : "
        f"{int(nonzero_nodes.min())}"
    )

    print(
        f"Max input nodes/sample      : "
        f"{int(nonzero_nodes.max())}"
    )


# ============================================================================
# SAVE DATASET
# ============================================================================

def save_dataset(
    X: np.ndarray,
    Y: np.ndarray,
    node_mask: np.ndarray,
    vessel_ids: np.ndarray,
) -> None:

    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    np.savez_compressed(
        OUTPUT_PATH,

        X=X,

        Y=Y,

        node_mask=node_mask,

        vessel_ids=vessel_ids,

        input_window=np.asarray(
            INPUT_WINDOW,
            dtype=np.int32,
        ),

        prediction_horizon=np.asarray(
            PREDICTION_HORIZON,
            dtype=np.int32,
        ),

        time_bin_seconds=np.asarray(
            TIME_BIN_SECONDS,
            dtype=np.int32,
        ),

        feature_dim=np.asarray(
            FEATURE_DIM,
            dtype=np.int32,
        ),

        max_nodes=np.asarray(
            MAX_NODES,
            dtype=np.int32,
        ),
    )

    print()

    print(
        f"Dataset saved to             : "
        f"{OUTPUT_PATH}"
    )

    size_mb = (
        OUTPUT_PATH.stat().st_size
        / (
            1024.0
            * 1024.0
        )
    )

    print(
        f"Dataset file size            : "
        f"{size_mb:.2f} MB"
    )


# ============================================================================
# SAVED DATASET VALIDATION
# ============================================================================

def validate_saved_dataset() -> None:

    if not OUTPUT_PATH.exists():

        raise FileNotFoundError(
            "Dataset was not created: "
            f"{OUTPUT_PATH}"
        )

    data = np.load(
        OUTPUT_PATH,
        allow_pickle=False,
    )

    required = {
        "X",
        "Y",
        "node_mask",
        "vessel_ids",
    }

    missing = (
        required
        .difference(
            data.files
        )
    )

    if missing:

        raise RuntimeError(
            "Saved dataset is missing "
            f"keys: {sorted(missing)}"
        )

    X = data["X"]

    Y = data["Y"]

    mask = data[
        "node_mask"
    ]

    vessel_ids = data[
        "vessel_ids"
    ]

    expected_x = (
        X.shape[0],
        INPUT_WINDOW,
        MAX_NODES,
        FEATURE_DIM,
    )

    expected_y = (
        X.shape[0],
        PREDICTION_HORIZON,
        MAX_NODES,
        TARGET_DIM,
    )

    expected_mask = (
        X.shape[0],
        TOTAL_STEPS,
        MAX_NODES,
    )

    expected_ids = (
        X.shape[0],
        MAX_NODES,
    )

    if X.shape != expected_x:

        raise RuntimeError(
            f"Invalid X shape: {X.shape}; "
            f"expected {expected_x}"
        )

    if Y.shape != expected_y:

        raise RuntimeError(
            f"Invalid Y shape: {Y.shape}; "
            f"expected {expected_y}"
        )

    if mask.shape != expected_mask:

        raise RuntimeError(
            "Invalid node_mask shape: "
            f"{mask.shape}; "
            f"expected {expected_mask}"
        )

    if vessel_ids.shape != expected_ids:

        raise RuntimeError(
            "Invalid vessel_ids shape: "
            f"{vessel_ids.shape}; "
            f"expected {expected_ids}"
        )

    if not np.isfinite(X).all():

        raise RuntimeError(
            "X contains NaN or "
            "infinite values."
        )

    if not np.isfinite(Y).all():

        raise RuntimeError(
            "Y contains NaN or "
            "infinite values."
        )

    if not np.isfinite(mask).all():

        raise RuntimeError(
            "node_mask contains NaN "
            "or infinite values."
        )

    print()
    print("-" * 72)
    print("DATASET VALIDATION")
    print("-" * 72)

    print(
        "Dataset integrity             : PASS"
    )

    print(
        f"X                            : "
        f"{X.shape}"
    )

    print(
        f"Y                            : "
        f"{Y.shape}"
    )

    print(
        f"Node mask                    : "
        f"{mask.shape}"
    )

    print(
        f"Vessel IDs                   : "
        f"{vessel_ids.shape}"
    )


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:

    print()
    print("=" * 72)
    print(
        "EXOCHAIN REAL AIS ST-GNN DATASET BUILDER"
    )
    print("=" * 72)

    print(
        f"Database                   : "
        f"{DB_PATH}"
    )

    print(
        f"Time bin                   : "
        f"{TIME_BIN_SECONDS} seconds"
    )

    print(
        f"Input window               : "
        f"{INPUT_WINDOW} bins"
    )

    print(
        f"Prediction horizon         : "
        f"{PREDICTION_HORIZON} bins"
    )

    print(
        f"Total temporal steps       : "
        f"{TOTAL_STEPS}"
    )

    print(
        f"Maximum graph nodes        : "
        f"{MAX_NODES}"
    )

    print(
        f"Minimum vessel steps       : "
        f"{MIN_VESSEL_STEPS}"
    )

    print(
        f"Maximum internal bin gap   : "
        f"{MAX_BIN_GAP}"
    )

    # ------------------------------------------------------------------
    # CONFIG VALIDATION
    # ------------------------------------------------------------------

    if INPUT_WINDOW <= 0:

        raise ValueError(
            "INPUT_WINDOW must be "
            "greater than zero."
        )

    if PREDICTION_HORIZON <= 0:

        raise ValueError(
            "PREDICTION_HORIZON must "
            "be greater than zero."
        )

    if MAX_NODES <= 0:

        raise ValueError(
            "MAX_NODES must be "
            "greater than zero."
        )

    if MIN_VESSEL_STEPS <= 0:

        raise ValueError(
            "MIN_VESSEL_STEPS must "
            "be greater than zero."
        )

    if MAX_BIN_GAP <= 0:

        raise ValueError(
            "MAX_BIN_GAP must be "
            "greater than zero."
        )

    # ------------------------------------------------------------------
    # DATABASE
    # ------------------------------------------------------------------

    conn = open_database()

    try:

        (
            total_rows,
            unique_vessels,
        ) = get_database_stats(
            conn
        )

        print(
            f"Raw database rows       : "
            f"{total_rows:,}"
        )

        print(
            f"Unique database vessels : "
            f"{unique_vessels:,}"
        )

        print()

        print(
            "Creating synchronized "
            "temporal bins..."
        )

        events = load_events(
            conn
        )

        if not events:

            raise RuntimeError(
                "No valid AIS events "
                "found in the database."
            )

        bins = (
            create_temporal_bins(
                events
            )
        )

    finally:

        conn.close()

    # ------------------------------------------------------------------
    # BUILD
    # ------------------------------------------------------------------

    (
        X,
        Y,
        node_mask,
        vessel_ids,
        candidate_count,
        rejected,
    ) = build_dataset(
        bins
    )

    # ------------------------------------------------------------------
    # STATISTICS
    # ------------------------------------------------------------------

    print_statistics(
        X,
        Y,
        node_mask,
    )

    # ------------------------------------------------------------------
    # SAVE
    # ------------------------------------------------------------------

    save_dataset(
        X,
        Y,
        node_mask,
        vessel_ids,
    )

    # ------------------------------------------------------------------
    # VALIDATE
    # ------------------------------------------------------------------

    validate_saved_dataset()

    # ------------------------------------------------------------------
    # COMPLETE
    # ------------------------------------------------------------------

    print()

    print("=" * 72)

    print(
        "ST-GNN HISTORICAL DATASET BUILD COMPLETE"
    )

    print("=" * 72)

    print(
        f"Samples                      : "
        f"{X.shape[0]:,}"
    )

    print(
        f"Candidate windows            : "
        f"{candidate_count:,}"
    )

    print(
        f"Rejected windows             : "
        f"{rejected:,}"
    )

    print(
        f"Input tensor                 : "
        f"{X.shape}"
    )

    print(
        f"Target tensor                : "
        f"{Y.shape}"
    )

    print(
        f"Node mask                    : "
        f"{node_mask.shape}"
    )

    print(
        f"Saved                        : "
        f"{OUTPUT_PATH}"
    )

    print("=" * 72)


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()