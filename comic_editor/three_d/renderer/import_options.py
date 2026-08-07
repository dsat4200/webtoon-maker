"""Internal OBJ/glTF import placement and normal policies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np
from numpy.typing import NDArray


class UpAxis(str, Enum):
    Y = "Y up"
    Z = "Z up"


class PlacementMode(str, Enum):
    FLOOR_CENTER = "Center X/Z and place on floor"
    CENTER_ORIGIN = "Center at origin"
    PRESERVE_ORIGIN = "Preserve authored origin"


class NormalPolicy(str, Enum):
    AUTO = "Auto"
    REQUIRE_AUTHORED = "Require authored"
    RECOMPUTE_FLAT = "Recompute flat"
    RECOMPUTE_SMOOTH = "Recompute smooth"


@dataclass(frozen=True, slots=True)
class ImportOptions:
    up_axis: UpAxis = UpAxis.Y
    unit_scale: float = 1.0
    placement: PlacementMode = PlacementMode.PRESERVE_ORIGIN
    normals: NormalPolicy = NormalPolicy.AUTO
    smooth_crease_angle_deg: float = 60.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "up_axis", UpAxis(self.up_axis))
        object.__setattr__(self, "placement", PlacementMode(self.placement))
        object.__setattr__(self, "normals", NormalPolicy(self.normals))
        if not 1e-9 <= self.unit_scale <= 1e9:
            raise ValueError("unit scale must be between 1e-9 and 1e9")
        if not 0.0 <= self.smooth_crease_angle_deg <= 180.0:
            raise ValueError("smooth crease angle must be between 0 and 180 degrees")


def import_model_matrix(
    bounds_min: NDArray[np.float64],
    bounds_max: NDArray[np.float64],
    options: ImportOptions,
) -> NDArray[np.float64]:
    rotation = np.identity(4, dtype=np.float64)
    if options.up_axis is UpAxis.Z:
        rotation[:3, :3] = np.array([[1,0,0],[0,0,1],[0,-1,0]], dtype=np.float64)
    rotation[:3, :3] *= options.unit_scale
    corners = np.asarray(
        [[x,y,z,1.0] for x in (bounds_min[0],bounds_max[0]) for y in (bounds_min[1],bounds_max[1]) for z in (bounds_min[2],bounds_max[2])]
    )
    transformed = (rotation @ corners.T).T[:, :3]
    low, high = transformed.min(axis=0), transformed.max(axis=0)
    translation = np.zeros(3)
    if options.placement is PlacementMode.FLOOR_CENTER:
        center = (low + high) * 0.5
        translation = np.array([-center[0], -low[1], -center[2]])
    elif options.placement is PlacementMode.CENTER_ORIGIN:
        translation = -(low + high) * 0.5
    result = rotation.copy()
    result[:3, 3] = translation
    return result

