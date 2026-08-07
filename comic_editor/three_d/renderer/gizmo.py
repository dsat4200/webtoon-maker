"""World/local transform gizmo and free-trackball math."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

import numpy as np
from numpy.typing import NDArray

from .camera import quaternion_from_axis_angle, quaternion_matrix
from .scene import TransformSpace


class GizmoMode(str, Enum):
    MOVE = "move"
    ROTATE = "rotate"
    SCALE = "scale"
    TRACKBALL = "trackball"


@dataclass(frozen=True, slots=True)
class GizmoAxes:
    origin: NDArray[np.float64]
    axes: NDArray[np.float64]


def gizmo_axes(world_matrix: NDArray[np.float64], space: TransformSpace) -> GizmoAxes:
    matrix = np.asarray(world_matrix, dtype=np.float64)
    axes = np.identity(3, dtype=np.float64)
    if TransformSpace(space) is TransformSpace.LOCAL:
        linear = matrix[:3, :3]
        lengths = np.linalg.norm(linear, axis=0)
        axes = linear / np.maximum(lengths, 1e-12)
    return GizmoAxes(matrix[:3, 3].copy(), axes)


def translation_delta(axis: NDArray[np.float64], amount: float) -> NDArray[np.float64]:
    result = np.identity(4, dtype=np.float64)
    value = np.asarray(axis, dtype=np.float64)
    result[:3, 3] = value / max(float(np.linalg.norm(value)), 1e-12) * float(amount)
    return result


def rotation_delta(axis: NDArray[np.float64], radians: float) -> NDArray[np.float64]:
    return quaternion_matrix(quaternion_from_axis_angle(np.asarray(axis, dtype=np.float64), radians))


def scale_delta(axis_index: int | None, factor: float) -> NDArray[np.float64]:
    if not math.isfinite(factor) or abs(factor) <= 1e-9:
        raise ValueError("scale factor must be finite and nonzero")
    result = np.identity(4, dtype=np.float64)
    if axis_index is None:
        result[:3, :3] *= factor
    else:
        if axis_index not in (0, 1, 2):
            raise ValueError("scale axis must be 0, 1, 2, or None")
        result[axis_index, axis_index] = factor
    return result


def trackball_delta(previous_ndc: NDArray[np.float64], current_ndc: NDArray[np.float64]) -> NDArray[np.float64]:
    def sphere(point: NDArray[np.float64]) -> NDArray[np.float64]:
        x, y = np.asarray(point, dtype=np.float64)
        result = np.array([x, y, math.sqrt(max(0.0, 1.0 - x*x - y*y))])
        return result / max(float(np.linalg.norm(result)), 1e-12)
    start, end = sphere(previous_ndc), sphere(current_ndc)
    cross = np.cross(start, end)
    if float(np.linalg.norm(cross)) <= 1e-12:
        return np.identity(4)
    angle = math.acos(float(np.clip(np.dot(start, end), -1.0, 1.0)))
    return rotation_delta(cross, angle)

