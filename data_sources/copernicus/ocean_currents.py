import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict

import copernicusmarine
import xarray as xr


class CopernicusOceanCurrentSource:
    """
    Adapter for Copernicus Marine ocean-current data.

    Retrieves:
        uo = eastward sea-water velocity
        vo = northward sea-water velocity

    The source itself is deliberately stateless.
    Caching is handled by the OceanCurrentDataAgent / Redis layer.
    """

    DATASET_ID = os.getenv(
        "COPERNICUS_CURRENT_DATASET",
        "cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m",
    )

    SURFACE_DEPTH = 0.49402499198913574

    def fetch_tile(
        self,
        minimum_longitude: float,
        maximum_longitude: float,
        minimum_latitude: float,
        maximum_latitude: float,
        date: str,
    ) -> xr.Dataset:
        """
        Download one spatial ocean-current tile for one date.
        """

        output_dir = Path(
            tempfile.mkdtemp(
                prefix="exochain_cmems_"
            )
        )

        try:
            result = copernicusmarine.subset(
                dataset_id=self.DATASET_ID,
                variables=["uo", "vo"],
                minimum_longitude=minimum_longitude,
                maximum_longitude=maximum_longitude,
                minimum_latitude=minimum_latitude,
                maximum_latitude=maximum_latitude,
                start_datetime=f"{date}T00:00:00",
                end_datetime=f"{date}T00:00:00",
                minimum_depth=self.SURFACE_DEPTH,
                maximum_depth=self.SURFACE_DEPTH,
                output_directory=str(output_dir),
                output_filename="current.nc",
                disable_progress_bar=True,
            )

            # Load the complete dataset into memory before removing
            # the temporary directory.
            with xr.open_dataset(result.file_path) as dataset:
                return dataset.load()

        finally:
            shutil.rmtree(
                output_dir,
                ignore_errors=True,
            )

    @staticmethod
    def current_vector(
        uo: float,
        vo: float,
    ) -> Dict[str, Any]:
        """
        Convert u/v current components into a normalized vector.
        """

        if (
            not math.isfinite(uo)
            or not math.isfinite(vo)
        ):
            return {
                "valid": False,
                "status": "MISSING",
                "u_ms": None,
                "v_ms": None,
                "speed_ms": None,
                "speed_knots": None,
                "direction_deg": None,
            }

        speed_ms = math.sqrt(
            uo ** 2 + vo ** 2
        )

        direction_deg = (
            math.degrees(
                math.atan2(uo, vo)
            )
            + 360.0
        ) % 360.0

        return {
            "valid": True,
            "status": "VALID",
            "u_ms": float(uo),
            "v_ms": float(vo),
            "speed_ms": float(speed_ms),
            "speed_knots": float(
                speed_ms * 1.943844
            ),
            "direction_deg": float(
                direction_deg
            ),
        }

    @staticmethod
    def nearest_current(
        dataset: xr.Dataset,
        latitude: float,
        longitude: float,
    ) -> Dict[str, Any]:
        """
        Find the nearest Copernicus grid point.
        """

        point = dataset.sel(
            latitude=latitude,
            longitude=longitude,
            method="nearest",
        )

        uo = float(
            point["uo"]
            .isel(time=0)
            .squeeze()
            .values
        )

        vo = float(
            point["vo"]
            .isel(time=0)
            .squeeze()
            .values
        )

        result = (
            CopernicusOceanCurrentSource
            .current_vector(
                uo,
                vo,
            )
        )

        result["latitude"] = float(
            point.latitude.values
        )

        result["longitude"] = float(
            point.longitude.values
        )

        return result