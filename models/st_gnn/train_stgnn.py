# models/st_gnn/train_stgnn.py

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset


# ============================================================
# PATHS / CONFIGURATION
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATASET_PATH = (
    PROJECT_ROOT
    / "data"
    / "stgnn"
    / "ais_stgnn_dataset.npz"
)

CHECKPOINT_DIR = (
    PROJECT_ROOT
    / "data"
    / "stgnn"
    / "checkpoints"
)

BEST_CHECKPOINT = (
    CHECKPOINT_DIR
    / "exochain_stgnn_best.pt"
)

LAST_CHECKPOINT = (
    CHECKPOINT_DIR
    / "exochain_stgnn_last.pt"
)

NORMALIZATION_PATH = (
    CHECKPOINT_DIR
    / "normalization.json"
)

HISTORY_PATH = (
    CHECKPOINT_DIR
    / "training_history.json"
)

METADATA_PATH = (
    CHECKPOINT_DIR
    / "model_metadata.json"
)


# Reproducibility
SEED = 42

# Training
BATCH_SIZE = 4
EPOCHS = 150

LEARNING_RATE = 1e-3
WEIGHT_DECAY = 1e-4

HIDDEN_DIM = 64

GRAPH_LAYERS = 2
GRU_LAYERS = 2

K_NEIGHBORS = 5
GRAPH_RADIUS_KM = 50.0

EARLY_STOPPING_PATIENCE = 20

GRADIENT_CLIP_NORM = 1.0

HUBER_DELTA = 1.0

TRAIN_RATIO = 0.70
VAL_RATIO = 0.15
TEST_RATIO = 0.15


# Dataset channels
LAT_CHANNEL = 0
LON_CHANNEL = 1


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

TARGET_NAMES = [
    "delta_latitude",
    "delta_longitude",
]


# ============================================================
# REPRODUCIBILITY
# ============================================================

def set_seed(seed: int = SEED) -> None:

    random.seed(seed)
    np.random.seed(seed)

    torch.manual_seed(seed)

    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


# ============================================================
# DEVICE
# ============================================================

def get_device() -> torch.device:

    if torch.cuda.is_available():

        device = torch.device("cuda")

        print("CUDA device: AVAILABLE")

        print(
            f"GPU: "
            f"{torch.cuda.get_device_name(0)}"
        )

        print(
            "CUDA capability: "
            f"{torch.cuda.get_device_capability(0)}"
        )

        return device

    print("CUDA device: NOT AVAILABLE")
    print("Using CPU.")

    return torch.device("cpu")


# ============================================================
# DATASET
# ============================================================

class STGNNDataset(Dataset):

    """
    Returns:

        x_normalized:
            [T, N, F]

        y_normalized:
            [H, N, 2]

        mask:
            [T+H, N]

        raw_coordinates:
            [T, N, 2]

    IMPORTANT:

    x_normalized is used by the neural network.

    raw_coordinates contains the ORIGINAL latitude/longitude
    values in degrees and is used exclusively for graph
    construction.
    """

    def __init__(
        self,
        x_normalized: np.ndarray,
        y_normalized: np.ndarray,
        mask: np.ndarray,
        raw_coordinates: np.ndarray,
    ):

        self.x = torch.from_numpy(
            x_normalized.astype(
                np.float32
            )
        )

        self.y = torch.from_numpy(
            y_normalized.astype(
                np.float32
            )
        )

        self.mask = torch.from_numpy(
            mask.astype(
                np.float32
            )
        )

        self.raw_coordinates = torch.from_numpy(
            raw_coordinates.astype(
                np.float32
            )
        )

    def __len__(self) -> int:

        return self.x.shape[0]

    def __getitem__(
        self,
        index: int,
    ):

        return (
            self.x[index],
            self.y[index],
            self.mask[index],
            self.raw_coordinates[index],
        )


# ============================================================
# NORMALIZATION
# ============================================================

@dataclass
class NormalizationStats:

    x_mean: np.ndarray
    x_std: np.ndarray

    y_mean: np.ndarray
    y_std: np.ndarray

    def to_json(self) -> Dict:

        return {
            "features": {
                name: {
                    "mean": float(
                        self.x_mean[i]
                    ),
                    "std": float(
                        self.x_std[i]
                    ),
                }
                for i, name in enumerate(
                    FEATURE_NAMES
                )
            },
            "targets": {
                name: {
                    "mean": float(
                        self.y_mean[i]
                    ),
                    "std": float(
                        self.y_std[i]
                    ),
                }
                for i, name in enumerate(
                    TARGET_NAMES
                )
            },
        }


def compute_normalization(
    X: np.ndarray,
    Y: np.ndarray,
    mask: np.ndarray,
) -> NormalizationStats:

    """
    Compute normalization statistics ONLY from
    the training dataset.

    X:
        [S, T, N, F]

    Y:
        [S, H, N, 2]

    mask:
        [S, T+H, N]
    """

    input_steps = X.shape[1]

    input_mask = mask[
        :,
        :input_steps,
        :
    ].astype(bool)

    target_mask = mask[
        :,
        input_steps:,
        :
    ].astype(bool)

    x_mean = np.zeros(
        X.shape[-1],
        dtype=np.float64,
    )

    x_std = np.ones(
        X.shape[-1],
        dtype=np.float64,
    )

    y_mean = np.zeros(
        Y.shape[-1],
        dtype=np.float64,
    )

    y_std = np.ones(
        Y.shape[-1],
        dtype=np.float64,
    )

    # --------------------------------------------------------
    # Input normalization
    # --------------------------------------------------------

    for feature_idx in range(
        X.shape[-1]
    ):

        values = X[
            ...,
            feature_idx
        ][input_mask]

        if values.size == 0:
            continue

        mean = values.mean()
        std = values.std()

        if std < 1e-8:
            std = 1.0

        x_mean[feature_idx] = mean
        x_std[feature_idx] = std

    # --------------------------------------------------------
    # Target normalization
    # --------------------------------------------------------

    for target_idx in range(
        Y.shape[-1]
    ):

        values = Y[
            ...,
            target_idx
        ][target_mask]

        if values.size == 0:
            continue

        mean = values.mean()
        std = values.std()

        if std < 1e-8:
            std = 1.0

        y_mean[target_idx] = mean
        y_std[target_idx] = std

    return NormalizationStats(
        x_mean=x_mean,
        x_std=x_std,
        y_mean=y_mean,
        y_std=y_std,
    )


def normalize_X(
    X: np.ndarray,
    stats: NormalizationStats,
) -> np.ndarray:

    return (
        X
        - stats.x_mean.reshape(
            1,
            1,
            1,
            -1,
        )
    ) / stats.x_std.reshape(
        1,
        1,
        1,
        -1,
    )


def normalize_Y(
    Y: np.ndarray,
    stats: NormalizationStats,
) -> np.ndarray:

    return (
        Y
        - stats.y_mean.reshape(
            1,
            1,
            1,
            -1,
        )
    ) / stats.y_std.reshape(
        1,
        1,
        1,
        -1,
    )


# ============================================================
# HAVERSINE
# ============================================================

def haversine_distance_km(
    lat1: torch.Tensor,
    lon1: torch.Tensor,
    lat2: torch.Tensor,
    lon2: torch.Tensor,
) -> torch.Tensor:

    """
    Haversine distance.

    INPUTS MUST BE RAW LAT/LON IN DEGREES.

    Returns distance in kilometers.
    """

    earth_radius_km = 6371.0088

    lat1_rad = torch.deg2rad(lat1)
    lon1_rad = torch.deg2rad(lon1)

    lat2_rad = torch.deg2rad(lat2)
    lon2_rad = torch.deg2rad(lon2)

    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = (
        torch.sin(dlat / 2.0) ** 2
        + torch.cos(lat1_rad)
        * torch.cos(lat2_rad)
        * torch.sin(dlon / 2.0) ** 2
    )

    a = torch.clamp(
        a,
        min=0.0,
        max=1.0,
    )

    c = (
        2.0
        * torch.asin(
            torch.sqrt(a)
        )
    )

    return (
        earth_radius_km
        * c
    )


# ============================================================
# DYNAMIC GRAPH CONSTRUCTION
# ============================================================

def build_dynamic_adjacency(
    raw_coordinates: torch.Tensor,
    input_mask: torch.Tensor,
    k_neighbors: int = K_NEIGHBORS,
    radius_km: float = GRAPH_RADIUS_KM,
) -> Tuple[
    torch.Tensor,
    torch.Tensor,
]:

    """
    Build a dynamic spatial graph.

    raw_coordinates:
        [B, T, N, 2]

        [:, :, :, 0] = RAW latitude
        [:, :, :, 1] = RAW longitude

    input_mask:
        [B, T, N]

    Returns:

        edge_index:
            [2, E]

        edge_attr:
            [E, 1]

    CRITICAL:

    Graph construction NEVER receives normalized
    latitude/longitude.
    """

    batch_size = raw_coordinates.shape[0]
    num_nodes = raw_coordinates.shape[2]

    device = raw_coordinates.device

    # --------------------------------------------------------
    # Last valid coordinate of every node
    # --------------------------------------------------------

    coordinates = torch.zeros(
        batch_size,
        num_nodes,
        2,
        device=device,
        dtype=raw_coordinates.dtype,
    )

    valid_nodes = torch.zeros(
        batch_size,
        num_nodes,
        dtype=torch.bool,
        device=device,
    )

    for batch_idx in range(
        batch_size
    ):

        for node_idx in range(
            num_nodes
        ):

            valid = input_mask[
                batch_idx,
                :,
                node_idx,
            ]

            valid_indices = torch.where(
                valid
            )[0]

            if valid_indices.numel() == 0:
                continue

            last_index = (
                valid_indices[-1]
            )

            coordinates[
                batch_idx,
                node_idx
            ] = raw_coordinates[
                batch_idx,
                last_index,
                node_idx
            ]

            valid_nodes[
                batch_idx,
                node_idx
            ] = True

    all_edges = []
    all_distances = []

    total_edges = 0
    total_active_nodes = 0

    # --------------------------------------------------------
    # Construct each graph independently
    # --------------------------------------------------------

    for batch_idx in range(
        batch_size
    ):

        active_indices = torch.where(
            valid_nodes[
                batch_idx
            ]
        )[0]

        active_count = (
            active_indices.numel()
        )

        total_active_nodes += int(
            active_count
        )

        if active_count <= 1:
            continue

        coords = coordinates[
            batch_idx,
            active_indices,
        ]

        lat = coords[:, 0]
        lon = coords[:, 1]

        # ----------------------------------------------------
        # Pairwise raw geographic coordinates
        # ----------------------------------------------------

        lat_i = lat[:, None]
        lon_i = lon[:, None]

        lat_j = lat[None, :]
        lon_j = lon[None, :]

        distance_matrix = (
            haversine_distance_km(
                lat_i,
                lon_i,
                lat_j,
                lon_j,
            )
        )

        # Remove self edges.
        distance_matrix.fill_diagonal_(
            float("inf")
        )

        # ----------------------------------------------------
        # Radius graph
        # ----------------------------------------------------

        radius_edges = (
            distance_matrix
            <= radius_km
        )

        # ----------------------------------------------------
        # KNN graph
        # ----------------------------------------------------

        k = min(
            k_neighbors,
            active_count - 1,
        )

        knn_edges = torch.zeros_like(
            radius_edges
        )

        if k > 0:

            _, nearest = torch.topk(
                distance_matrix,
                k=k,
                dim=1,
                largest=False,
            )

            knn_edges.scatter_(
                1,
                nearest,
                True,
            )

        adjacency = (
            radius_edges
            | knn_edges
        )

        src, dst = torch.where(
            adjacency
        )

        if src.numel() == 0:
            continue

        # ----------------------------------------------------
        # Convert local node IDs into
        # global batch-node IDs.
        # ----------------------------------------------------

        global_src = (
            batch_idx * num_nodes
            + active_indices[src]
        )

        global_dst = (
            batch_idx * num_nodes
            + active_indices[dst]
        )

        edges = torch.stack(
            [
                global_src,
                global_dst,
            ],
            dim=0,
        )

        distances = distance_matrix[
            src,
            dst,
        ]

        all_edges.append(edges)

        all_distances.append(
            distances.unsqueeze(-1)
        )

        total_edges += int(
            src.numel()
        )

    # --------------------------------------------------------
    # Empty graph
    # --------------------------------------------------------

    if not all_edges:

        edge_index = torch.empty(
            (2, 0),
            dtype=torch.long,
            device=device,
        )

        edge_attr = torch.empty(
            (0, 1),
            dtype=raw_coordinates.dtype,
            device=device,
        )

        print(
            "GRAPH | "
            f"active_nodes={total_active_nodes} | "
            "edges=0"
        )

        return (
            edge_index,
            edge_attr,
        )

    edge_index = torch.cat(
        all_edges,
        dim=1,
    )

    edge_attr = torch.cat(
        all_distances,
        dim=0,
    )

    if total_active_nodes > 0:

        avg_edges_per_node = (
            total_edges
            / total_active_nodes
        )

    else:

        avg_edges_per_node = 0.0

    print(
        "GRAPH | "
        f"active_nodes={total_active_nodes} | "
        f"edges={total_edges} | "
        f"edges/active_node="
        f"{avg_edges_per_node:.2f}"
    )

    return (
        edge_index,
        edge_attr,
    )


# ============================================================
# SPATIAL GRAPH CONVOLUTION
# ============================================================

class SpatialGraphConv(
    nn.Module
):

    def __init__(
        self,
        in_features: int,
        out_features: int,
    ):

        super().__init__()

        self.message_mlp = nn.Sequential(
            nn.Linear(
                in_features + 1,
                out_features,
            ),
            nn.ReLU(),
            nn.Linear(
                out_features,
                out_features,
            ),
        )

        self.self_projection = nn.Linear(
            in_features,
            out_features,
        )

        self.norm = nn.LayerNorm(
            out_features
        )

    def forward(
        self,
        x: torch.Tensor,
        edge_index: torch.Tensor,
        edge_attr: torch.Tensor,
    ) -> torch.Tensor:

        """
        x:
            [B*N, F]

        edge_index:
            [2, E]

        edge_attr:
            [E, 1]
        """

        num_nodes = x.shape[0]

        output = (
            self.self_projection(x)
        )

        if edge_index.shape[1] == 0:

            return self.norm(
                torch.relu(output)
            )

        src = edge_index[0]
        dst = edge_index[1]

        source_features = x[src]

        messages = torch.cat(
            [
                source_features,
                edge_attr,
            ],
            dim=-1,
        )

        messages = (
            self.message_mlp(
                messages
            )
        )

        aggregated = torch.zeros(
            num_nodes,
            messages.shape[-1],
            device=x.device,
            dtype=x.dtype,
        )

        aggregated.index_add_(
            0,
            dst,
            messages,
        )

        degree = torch.zeros(
            num_nodes,
            device=x.device,
            dtype=x.dtype,
        )

        degree.index_add_(
            0,
            dst,
            torch.ones_like(
                dst,
                dtype=x.dtype,
            ),
        )

        degree = (
            degree.clamp_min(1.0)
        )

        aggregated = (
            aggregated
            / degree.unsqueeze(-1)
        )

        output = (
            output
            + aggregated
        )

        output = torch.relu(
            output
        )

        return self.norm(
            output
        )


# ============================================================
# EXOCHAIN ST-GNN
# ============================================================

class ExoChainSTGNN(
    nn.Module
):

    def __init__(
        self,
        input_features: int = 8,
        hidden_dim: int = HIDDEN_DIM,
        graph_layers: int = GRAPH_LAYERS,
        gru_layers: int = GRU_LAYERS,
        prediction_horizon: int = 6,
        output_features: int = 2,
    ):

        super().__init__()

        self.input_features = (
            input_features
        )

        self.hidden_dim = (
            hidden_dim
        )

        self.prediction_horizon = (
            prediction_horizon
        )

        self.output_features = (
            output_features
        )

        # ----------------------------------------------------
        # Input projection
        # ----------------------------------------------------

        self.input_projection = (
            nn.Sequential(
                nn.Linear(
                    input_features,
                    hidden_dim,
                ),
                nn.ReLU(),
            )
        )

        # ----------------------------------------------------
        # Temporal encoder
        # ----------------------------------------------------

        self.temporal_encoder = (
            nn.GRU(
                input_size=hidden_dim,
                hidden_size=hidden_dim,
                num_layers=gru_layers,
                batch_first=True,
                dropout=(
                    0.1
                    if gru_layers > 1
                    else 0.0
                ),
            )
        )

        # ----------------------------------------------------
        # Spatial layers
        # ----------------------------------------------------

        self.graph_layers = (
            nn.ModuleList()
        )

        for _ in range(
            graph_layers
        ):

            self.graph_layers.append(
                SpatialGraphConv(
                    hidden_dim,
                    hidden_dim,
                )
            )

        # ----------------------------------------------------
        # Temporal decoder
        # ----------------------------------------------------

        self.temporal_decoder = (
            nn.GRU(
                input_size=hidden_dim,
                hidden_size=hidden_dim,
                num_layers=gru_layers,
                batch_first=True,
                dropout=(
                    0.1
                    if gru_layers > 1
                    else 0.0
                ),
            )
        )

        # ----------------------------------------------------
        # Prediction head
        # ----------------------------------------------------

        self.prediction_head = (
            nn.Sequential(
                nn.Linear(
                    hidden_dim,
                    hidden_dim,
                ),
                nn.ReLU(),
                nn.Linear(
                    hidden_dim,
                    output_features,
                ),
            )
        )

    def forward(
        self,
        x: torch.Tensor,
        edge_index: torch.Tensor,
        edge_attr: torch.Tensor,
    ) -> torch.Tensor:

        """
        x:
            [B,T,N,F]

        output:
            [B,H,N,2]
        """

        (
            batch_size,
            time_steps,
            num_nodes,
            _,
        ) = x.shape

        # ----------------------------------------------------
        # Feature projection
        # ----------------------------------------------------

        x = self.input_projection(
            x
        )

        # ----------------------------------------------------
        # Arrange each vessel as an independent
        # temporal sequence.
        # ----------------------------------------------------

        x = x.permute(
            0,
            2,
            1,
            3,
        )

        x = x.reshape(
            batch_size * num_nodes,
            time_steps,
            self.hidden_dim,
        )

        temporal_output, _ = (
            self.temporal_encoder(x)
        )

        vessel_embeddings = (
            temporal_output[:, -1, :]
        )

        # ----------------------------------------------------
        # Spatial message passing
        # ----------------------------------------------------

        spatial = (
            vessel_embeddings
        )

        for graph_layer in (
            self.graph_layers
        ):

            spatial = graph_layer(
                spatial,
                edge_index,
                edge_attr,
            )

        # ----------------------------------------------------
        # Future decoder
        # ----------------------------------------------------

        decoder_input = (
            spatial.unsqueeze(1)
        )

        decoder_input = (
            decoder_input.repeat(
                1,
                self.prediction_horizon,
                1,
            )
        )

        decoded, _ = (
            self.temporal_decoder(
                decoder_input
            )
        )

        prediction = (
            self.prediction_head(
                decoded
            )
        )

        prediction = prediction.reshape(
            batch_size,
            num_nodes,
            self.prediction_horizon,
            self.output_features,
        )

        prediction = prediction.permute(
            0,
            2,
            1,
            3,
        )

        return prediction


# ============================================================
# LOSS FUNCTIONS
# ============================================================

def masked_huber_loss(
    prediction: torch.Tensor,
    target: torch.Tensor,
    mask: torch.Tensor,
    delta: float = HUBER_DELTA,
) -> torch.Tensor:

    """
    prediction:
        [B,H,N,2]

    target:
        [B,H,N,2]

    mask:
        [B,H,N]
    """

    error = (
        prediction
        - target
    )

    abs_error = (
        error.abs()
    )

    delta_tensor = torch.tensor(
        delta,
        device=error.device,
        dtype=error.dtype,
    )

    quadratic = torch.minimum(
        abs_error,
        delta_tensor,
    )

    linear = (
        abs_error
        - quadratic
    )

    loss = (
        0.5 * quadratic ** 2
        + delta * linear
    )

    loss = loss.mean(
        dim=-1
    )

    mask = mask.float()

    numerator = (
        loss * mask
    ).sum()

    denominator = (
        mask.sum()
        .clamp_min(1.0)
    )

    return (
        numerator
        / denominator
    )


def masked_mae(
    prediction: torch.Tensor,
    target: torch.Tensor,
    mask: torch.Tensor,
) -> torch.Tensor:

    error = (
        prediction
        - target
    ).abs()

    error = error.mean(
        dim=-1
    )

    mask = mask.float()

    # IMPORTANT:
    # Keep the complete division inside parentheses.
    return (
        (error * mask).sum()
        / mask.sum().clamp_min(1.0)
    )


# ============================================================
# EPOCH
# ============================================================

def run_epoch(
    model: nn.Module,
    loader: DataLoader,
    optimizer,
    device: torch.device,
    training: bool,
    input_steps: int,
) -> Tuple[
    float,
    float,
]:

    if training:

        model.train()

    else:

        model.eval()

    total_loss = 0.0
    total_mae = 0.0
    batches = 0

    for (
        x,
        y,
        mask,
        raw_coordinates,
    ) in loader:

        x = x.to(
            device,
            non_blocking=True,
        )

        y = y.to(
            device,
            non_blocking=True,
        )

        mask = mask.to(
            device,
            non_blocking=True,
        )

        raw_coordinates = (
            raw_coordinates.to(
                device,
                non_blocking=True,
            )
        )

        # ----------------------------------------------------
        # Input and target masks
        # ----------------------------------------------------

        input_mask = mask[
            :,
            :input_steps,
            :,
        ]

        target_mask = mask[
            :,
            input_steps:,
            :,
        ]

        # ----------------------------------------------------
        # CRITICAL:
        #
        # normalized X -> neural network
        #
        # raw_coordinates -> graph builder
        # ----------------------------------------------------

        (
            edge_index,
            edge_attr,
        ) = build_dynamic_adjacency(
            raw_coordinates=(
                raw_coordinates
            ),
            input_mask=input_mask,
            k_neighbors=(
                K_NEIGHBORS
            ),
            radius_km=(
                GRAPH_RADIUS_KM
            ),
        )

        if training:

            optimizer.zero_grad(
                set_to_none=True
            )

        with torch.set_grad_enabled(
            training
        ):

            prediction = model(
                x,
                edge_index,
                edge_attr,
            )

            loss = (
                masked_huber_loss(
                    prediction,
                    y,
                    target_mask,
                )
            )

            mae = (
                masked_mae(
                    prediction,
                    y,
                    target_mask,
                )
            )

            if training:

                loss.backward()

                torch.nn.utils.clip_grad_norm_(
                    model.parameters(),
                    GRADIENT_CLIP_NORM,
                )

                optimizer.step()

        total_loss += float(
            loss.detach().cpu()
        )

        total_mae += float(
            mae.detach().cpu()
        )

        batches += 1

    if batches == 0:

        return (
            0.0,
            0.0,
        )

    return (
        total_loss / batches,
        total_mae / batches,
    )


# ============================================================
# CHECKPOINT
# ============================================================

def save_checkpoint(
    path: Path,
    model: nn.Module,
    optimizer,
    epoch: int,
    best_val_loss: float,
    history: Dict,
):

    torch.save(
        {
            "epoch": epoch,
            "model_state_dict": (
                model.state_dict()
            ),
            "optimizer_state_dict": (
                optimizer.state_dict()
            ),
            "best_val_loss": (
                best_val_loss
            ),
            "history": history,
            "config": {
                "input_features": 8,
                "hidden_dim": HIDDEN_DIM,
                "graph_layers": GRAPH_LAYERS,
                "gru_layers": GRU_LAYERS,
                "prediction_horizon": 6,
                "output_features": 2,
                "k_neighbors": K_NEIGHBORS,
                "graph_radius_km": (
                    GRAPH_RADIUS_KM
                ),
            },
        },
        path,
    )


# ============================================================
# DATASET DIAGNOSTICS
# ============================================================

def print_dataset_diagnostics(
    X: np.ndarray,
    Y: np.ndarray,
    mask: np.ndarray,
):

    input_steps = X.shape[1]

    input_mask = mask[
        :,
        :input_steps,
        :,
    ].astype(bool)

    target_mask = mask[
        :,
        input_steps:,
        :,
    ].astype(bool)

    active_nodes = (
        input_mask
        .any(axis=1)
        .sum(axis=1)
    )

    target_nodes = (
        target_mask
        .any(axis=1)
        .sum(axis=1)
    )

    print()
    print("=" * 60)
    print("DATASET DIAGNOSTICS")
    print("=" * 60)

    print(
        f"Samples: {X.shape[0]}"
    )

    print(
        f"Input sequence: {X.shape[1]}"
    )

    print(
        f"Prediction horizon: {Y.shape[1]}"
    )

    print(
        f"Nodes: {X.shape[2]}"
    )

    print(
        f"Features: {X.shape[3]}"
    )

    print(
        "Input active nodes/sample: "
        f"mean={active_nodes.mean():.2f} "
        f"min={active_nodes.min()} "
        f"max={active_nodes.max()}"
    )

    print(
        "Target active nodes/sample: "
        f"mean={target_nodes.mean():.2f} "
        f"min={target_nodes.min()} "
        f"max={target_nodes.max()}"
    )

    empty_samples = (
        active_nodes == 0
    ).sum()

    if empty_samples > 0:

        print(
            "WARNING: "
            f"{empty_samples} samples have "
            "ZERO input nodes."
        )

    print("=" * 60)


# ============================================================
# MAIN
# ============================================================

def main():

    # --------------------------------------------------------
    # Reproducibility
    # --------------------------------------------------------

    set_seed()

    # --------------------------------------------------------
    # Create checkpoint directory
    # --------------------------------------------------------

    CHECKPOINT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("=" * 60)
    print("EXOCHAIN REAL AIS ST-GNN TRAINING")
    print("=" * 60)

    print(
        f"Dataset: "
        f"{DATASET_PATH}"
    )

    print(
        f"Batch size "
        f"{BATCH_SIZE}"
    )

    print(
        f"Epochs "
        f"{EPOCHS}"
    )

    print(
        f"Learning rate "
        f"{LEARNING_RATE}"
    )

    print(
        f"Hidden dimension "
        f"{HIDDEN_DIM}"
    )

    print(
        f"Graph layers "
        f"{GRAPH_LAYERS}"
    )

    print(
        f"GRU layers "
        f"{GRU_LAYERS}"
    )

    print(
        f"K nearest neighbors "
        f"{K_NEIGHBORS}"
    )

    print(
        f"Graph radius "
        f"{GRAPH_RADIUS_KM} km"
    )

    print(
        f"Early stopping patience "
        f"{EARLY_STOPPING_PATIENCE}"
    )

    # --------------------------------------------------------
    # Device
    # --------------------------------------------------------

    device = get_device()

    # --------------------------------------------------------
    # Dataset existence
    # --------------------------------------------------------

    if not DATASET_PATH.exists():

        raise FileNotFoundError(
            "Dataset not found: "
            f"{DATASET_PATH}"
        )

    # --------------------------------------------------------
    # Load
    # --------------------------------------------------------

    dataset = np.load(
        DATASET_PATH,
        allow_pickle=True,
    )

    X = dataset[
        "X"
    ].astype(
        np.float32
    )

    Y = dataset[
        "Y"
    ].astype(
        np.float32
    )

    mask = dataset[
        "node_mask"
    ].astype(
        np.float32
    )

    print()
    print("DATASET")

    print(
        f"X shape "
        f"{X.shape}"
    )

    print(
        f"Y shape "
        f"{Y.shape}"
    )

    print(
        f"Mask shape "
        f"{mask.shape}"
    )

    print_dataset_diagnostics(
        X,
        Y,
        mask,
    )

    # --------------------------------------------------------
    # Validate dimensions
    # --------------------------------------------------------

    if X.ndim != 4:

        raise ValueError(
            "X must have shape "
            "[S,T,N,F]"
        )

    if Y.ndim != 4:

        raise ValueError(
            "Y must have shape "
            "[S,H,N,2]"
        )

    if mask.ndim != 3:

        raise ValueError(
            "node_mask must have shape "
            "[S,T+H,N]"
        )

    samples = X.shape[0]

    input_steps = X.shape[1]

    prediction_horizon = Y.shape[1]

    num_nodes = X.shape[2]

    num_features = X.shape[3]

    if num_features != 8:

        raise ValueError(
            "Expected 8 input features, "
            f"got {num_features}"
        )

    if Y.shape[-1] != 2:

        raise ValueError(
            "Expected 2 target features."
        )

    if mask.shape[1] != (
        input_steps
        + prediction_horizon
    ):

        raise ValueError(
            "Mask temporal dimension "
            "does not match X + Y."
        )

    # --------------------------------------------------------
    # Chronological split
    # --------------------------------------------------------

    train_end = int(
        samples
        * TRAIN_RATIO
    )

    val_end = int(
        samples
        * (
            TRAIN_RATIO
            + VAL_RATIO
        )
    )

    train_end = max(
        train_end,
        1,
    )

    val_end = max(
        val_end,
        train_end + 1,
    )

    val_end = min(
        val_end,
        samples - 1,
    )

    X_train = X[
        :train_end
    ]

    X_val = X[
        train_end:val_end
    ]

    X_test = X[
        val_end:
    ]

    Y_train = Y[
        :train_end
    ]

    Y_val = Y[
        train_end:val_end
    ]

    Y_test = Y[
        val_end:
    ]

    mask_train = mask[
        :train_end
    ]

    mask_val = mask[
        train_end:val_end
    ]

    mask_test = mask[
        val_end:
    ]

    print()
    print("CHRONOLOGICAL SPLIT")

    print(
        f"Train samples "
        f"{len(X_train)}"
    )

    print(
        f"Validation samples "
        f"{len(X_val)}"
    )

    print(
        f"Test samples "
        f"{len(X_test)}"
    )

    if len(X_train) < 100:

        print()

        print(
            "WARNING: dataset is small "
            "for serious ST-GNN training."
        )

    # --------------------------------------------------------
    # Normalization
    #
    # TRAIN ONLY
    # --------------------------------------------------------

    stats = compute_normalization(
        X_train,
        Y_train,
        mask_train,
    )

    print()
    print("NORMALIZATION")

    for i, name in enumerate(
        FEATURE_NAMES
    ):

        print(
            f"{name} mean "
            f"{stats.x_mean[i]:.6f} "
            f"std "
            f"{stats.x_std[i]:.6f}"
        )

    for i, name in enumerate(
        TARGET_NAMES
    ):

        print(
            f"{name} mean "
            f"{stats.y_mean[i]:.8f} "
            f"std "
            f"{stats.y_std[i]:.8f}"
        )

    # --------------------------------------------------------
    # Normalize inputs/targets
    # --------------------------------------------------------

    X_train_norm = normalize_X(
        X_train,
        stats,
    )

    X_val_norm = normalize_X(
        X_val,
        stats,
    )

    X_test_norm = normalize_X(
        X_test,
        stats,
    )

    Y_train_norm = normalize_Y(
        Y_train,
        stats,
    )

    Y_val_norm = normalize_Y(
        Y_val,
        stats,
    )

    Y_test_norm = normalize_Y(
        Y_test,
        stats,
    )

    # --------------------------------------------------------
    # RAW coordinates
    #
    # DO NOT normalize these.
    # --------------------------------------------------------

    raw_train_coordinates = X_train[
        ...,
        [
            LAT_CHANNEL,
            LON_CHANNEL,
        ],
    ]

    raw_val_coordinates = X_val[
        ...,
        [
            LAT_CHANNEL,
            LON_CHANNEL,
        ],
    ]

    raw_test_coordinates = X_test[
        ...,
        [
            LAT_CHANNEL,
            LON_CHANNEL,
        ],
    ]

    # --------------------------------------------------------
    # Dataset objects
    # --------------------------------------------------------

    train_dataset = STGNNDataset(
        X_train_norm,
        Y_train_norm,
        mask_train,
        raw_train_coordinates,
    )

    val_dataset = STGNNDataset(
        X_val_norm,
        Y_val_norm,
        mask_val,
        raw_val_coordinates,
    )

    test_dataset = STGNNDataset(
        X_test_norm,
        Y_test_norm,
        mask_test,
        raw_test_coordinates,
    )

    # --------------------------------------------------------
    # Data loaders
    # --------------------------------------------------------

    pin_memory = (
        device.type == "cuda"
    )

    train_loader = DataLoader(
        train_dataset,
        batch_size=BATCH_SIZE,
        shuffle=False,
        num_workers=0,
        pin_memory=pin_memory,
    )

    val_loader = DataLoader(
        val_dataset,
        batch_size=BATCH_SIZE,
        shuffle=False,
        num_workers=0,
        pin_memory=pin_memory,
    )

    test_loader = DataLoader(
        test_dataset,
        batch_size=BATCH_SIZE,
        shuffle=False,
        num_workers=0,
        pin_memory=pin_memory,
    )

    # --------------------------------------------------------
    # Model
    # --------------------------------------------------------

    model = ExoChainSTGNN(
        input_features=num_features,
        hidden_dim=HIDDEN_DIM,
        graph_layers=GRAPH_LAYERS,
        gru_layers=GRU_LAYERS,
        prediction_horizon=(
            prediction_horizon
        ),
        output_features=2,
    )

    model = model.to(
        device
    )

    parameter_count = sum(
        parameter.numel()
        for parameter in model.parameters()
    )

    trainable_parameter_count = sum(
        parameter.numel()
        for parameter in model.parameters()
        if parameter.requires_grad
    )

    print()
    print("MODEL")

    print(
        "Architecture "
        "Dynamic Graph + GRU"
    )

    print(
        f"Total parameters "
        f"{parameter_count}"
    )

    print(
        f"Trainable parameters "
        f"{trainable_parameter_count}"
    )

    print(
        "Input shape "
        f"[B,{input_steps},"
        f"{num_nodes},"
        f"{num_features}]"
    )

    print(
        "Output shape "
        f"[B,{prediction_horizon},"
        f"{num_nodes},2]"
    )

    # --------------------------------------------------------
    # Optimizer
    # --------------------------------------------------------

    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=LEARNING_RATE,
        weight_decay=WEIGHT_DECAY,
    )

    scheduler = (
        torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer,
            mode="min",
            factor=0.5,
            patience=5,
            min_lr=1e-6,
        )
    )

    # --------------------------------------------------------
    # History
    # --------------------------------------------------------

    history = {
        "train_loss": [],
        "val_loss": [],
        "val_mae": [],
        "learning_rate": [],
    }

    best_val_loss = float(
        "inf"
    )

    best_epoch = 0

    patience_counter = 0

    # --------------------------------------------------------
    # Training
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("TRAINING")
    print("=" * 60)

    for epoch in range(
        1,
        EPOCHS + 1,
    ):

        train_loss, train_mae = (
            run_epoch(
                model=model,
                loader=train_loader,
                optimizer=optimizer,
                device=device,
                training=True,
                input_steps=input_steps,
            )
        )

        val_loss, val_mae = (
            run_epoch(
                model=model,
                loader=val_loader,
                optimizer=None,
                device=device,
                training=False,
                input_steps=input_steps,
            )
        )

        scheduler.step(
            val_loss
        )

        current_lr = (
            optimizer.param_groups[0][
                "lr"
            ]
        )

        history[
            "train_loss"
        ].append(
            float(train_loss)
        )

        history[
            "val_loss"
        ].append(
            float(val_loss)
        )

        history[
            "val_mae"
        ].append(
            float(val_mae)
        )

        history[
            "learning_rate"
        ].append(
            float(current_lr)
        )

        improved = (
            val_loss
            < best_val_loss
        )

        if improved:

            best_val_loss = (
                val_loss
            )

            best_epoch = (
                epoch
            )

            patience_counter = 0

            save_checkpoint(
                BEST_CHECKPOINT,
                model,
                optimizer,
                epoch,
                best_val_loss,
                history,
            )

            marker = " BEST"

        else:

            patience_counter += 1

            marker = ""

        save_checkpoint(
            LAST_CHECKPOINT,
            model,
            optimizer,
            epoch,
            best_val_loss,
            history,
        )

        print(
            f"Epoch {epoch:03d} | "
            f"train {train_loss:.6f} | "
            f"val {val_loss:.6f} | "
            f"MAE {val_mae:.6f} | "
            f"lr {current_lr:.7f}"
            f"{marker}"
        )

        if (
            patience_counter
            >= EARLY_STOPPING_PATIENCE
        ):

            print()
            print(
                "Early stopping triggered."
            )

            break

    # --------------------------------------------------------
    # Load best checkpoint
    # --------------------------------------------------------

    if BEST_CHECKPOINT.exists():

        checkpoint = torch.load(
            BEST_CHECKPOINT,
            map_location=device,
        )

        model.load_state_dict(
            checkpoint[
                "model_state_dict"
            ]
        )

    # --------------------------------------------------------
    # Test
    # --------------------------------------------------------

    test_loss, test_mae = (
        run_epoch(
            model=model,
            loader=test_loader,
            optimizer=None,
            device=device,
            training=False,
            input_steps=input_steps,
        )
    )

    # --------------------------------------------------------
    # Final results
    # --------------------------------------------------------

    print()
    print("=" * 60)
    print("FINAL")
    print("=" * 60)

    print(
        f"Best epoch "
        f"{best_epoch}"
    )

    print(
        f"Best validation loss "
        f"{best_val_loss:.6f}"
    )

    print(
        f"Test Huber "
        f"{test_loss:.6f}"
    )

    print(
        f"Test normalized MAE "
        f"{test_mae:.6f}"
    )

    # --------------------------------------------------------
    # Save normalization
    # --------------------------------------------------------

    with open(
        NORMALIZATION_PATH,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            stats.to_json(),
            file,
            indent=2,
        )

    # --------------------------------------------------------
    # Save history
    # --------------------------------------------------------

    history_output = {
        **history,
        "best_epoch": int(
            best_epoch
        ),
        "best_validation_loss": float(
            best_val_loss
        ),
        "test_loss": float(
            test_loss
        ),
        "test_mae_normalized": float(
            test_mae
        ),
    }

    with open(
        HISTORY_PATH,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            history_output,
            file,
            indent=2,
        )

    # --------------------------------------------------------
    # Save metadata
    # --------------------------------------------------------

    metadata = {

        "model": "ExoChainSTGNN",

        "architecture": {

            "temporal_encoder": "GRU",

            "spatial_encoder":
                "Dynamic Haversine "
                "KNN + radius graph",

            "temporal_decoder": "GRU",
        },

        "input": {

            "features":
                FEATURE_NAMES,

            "shape": [
                "batch",
                input_steps,
                num_nodes,
                num_features,
            ],
        },

        "target": {

            "features":
                TARGET_NAMES,

            "shape": [
                "batch",
                prediction_horizon,
                num_nodes,
                2,
            ],
        },

        "graph": {

            "coordinate_source":
                "RAW latitude/longitude",

            "distance_metric":
                "Haversine",

            "radius_km":
                GRAPH_RADIUS_KM,

            "k_neighbors":
                K_NEIGHBORS,
        },

        "training": {

            "seed":
                SEED,

            "batch_size":
                BATCH_SIZE,

            "epochs":
                EPOCHS,

            "learning_rate":
                LEARNING_RATE,

            "weight_decay":
                WEIGHT_DECAY,

            "hidden_dim":
                HIDDEN_DIM,

            "graph_layers":
                GRAPH_LAYERS,

            "gru_layers":
                GRU_LAYERS,

            "train_ratio":
                TRAIN_RATIO,

            "validation_ratio":
                VAL_RATIO,

            "test_ratio":
                TEST_RATIO,
        },

        "dataset": {

            "path":
                str(DATASET_PATH),

            "samples":
                int(samples),

            "train_samples":
                int(len(X_train)),

            "validation_samples":
                int(len(X_val)),

            "test_samples":
                int(len(X_test)),
        },

        "results": {

            "best_epoch":
                int(best_epoch),

            "best_validation_loss":
                float(best_val_loss),

            "test_loss":
                float(test_loss),

            "test_mae_normalized":
                float(test_mae),
        },

        "device":
            str(device),

        "parameter_count":
            int(parameter_count),

        "trainable_parameter_count":
            int(trainable_parameter_count),
    }

    with open(
        METADATA_PATH,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            metadata,
            file,
            indent=2,
        )

    # --------------------------------------------------------
    # Output
    # --------------------------------------------------------

    print()
    print("CHECKPOINTS")

    print(
        f"Best checkpoint "
        f"{BEST_CHECKPOINT}"
    )

    print(
        f"Last checkpoint "
        f"{LAST_CHECKPOINT}"
    )

    print(
        f"Normalization "
        f"{NORMALIZATION_PATH}"
    )

    print(
        f"Training history "
        f"{HISTORY_PATH}"
    )

    print(
        f"Model metadata "
        f"{METADATA_PATH}"
    )

    print()
    print("=" * 60)
    print("ST-GNN TRAINING COMPLETE")
    print("=" * 60)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()