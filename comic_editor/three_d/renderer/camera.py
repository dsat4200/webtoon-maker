"""Camera and quaternion math for the embedded 3D renderer.

The orbit convention is retained from Perspective Renderer commit
``d13b75fc437fad8e990c87a4e2c0d7e6bdf7e73d``: right handed, Y up, and a
camera looking down its local negative-Z axis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np
from numpy.typing import NDArray


Float64Array = NDArray[np.float64]


def normalize(vector: Float64Array) -> Float64Array:
    value = np.asarray(vector, dtype=np.float64)
    length = float(np.linalg.norm(value))
    if length <= 1.0e-12:
        raise ValueError("cannot normalize a zero-length vector")
    return value / length


def quaternion_identity() -> Float64Array:
    return np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float64)


def quaternion_normalize(quaternion: Float64Array) -> Float64Array:
    return normalize(np.asarray(quaternion, dtype=np.float64))


def quaternion_multiply(left: Float64Array, right: Float64Array) -> Float64Array:
    w1, x1, y1, z1 = np.asarray(left, dtype=np.float64)
    w2, x2, y2, z2 = np.asarray(right, dtype=np.float64)
    return np.array(
        [
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ],
        dtype=np.float64,
    )


def quaternion_from_axis_angle(axis: Float64Array, angle_radians: float) -> Float64Array:
    unit_axis = normalize(np.asarray(axis, dtype=np.float64))
    half = float(angle_radians) * 0.5
    return np.concatenate(([math.cos(half)], unit_axis * math.sin(half)))


def quaternion_rotate(quaternion: Float64Array, vector: Float64Array) -> Float64Array:
    q = quaternion_normalize(quaternion)
    w, xyz = q[0], q[1:]
    value = np.asarray(vector, dtype=np.float64)
    return (
        2.0 * float(np.dot(xyz, value)) * xyz
        + (w * w - float(np.dot(xyz, xyz))) * value
        + 2.0 * w * np.cross(xyz, value)
    )


def quaternion_from_rotation_matrix(matrix: Float64Array) -> Float64Array:
    """Convert a 3x3 local-to-world rotation matrix to a unit quaternion."""
    value = np.asarray(matrix, dtype=np.float64)
    if value.shape != (3, 3):
        raise ValueError("rotation matrix must have shape (3, 3)")
    trace = float(np.trace(value))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        result = np.array(
            [
                0.25 * scale,
                (value[2, 1] - value[1, 2]) / scale,
                (value[0, 2] - value[2, 0]) / scale,
                (value[1, 0] - value[0, 1]) / scale,
            ]
        )
    else:
        index = int(np.argmax(np.diag(value)))
        if index == 0:
            scale = math.sqrt(1.0 + value[0, 0] - value[1, 1] - value[2, 2]) * 2.0
            result = np.array(
                [
                    (value[2, 1] - value[1, 2]) / scale,
                    0.25 * scale,
                    (value[0, 1] + value[1, 0]) / scale,
                    (value[0, 2] + value[2, 0]) / scale,
                ]
            )
        elif index == 1:
            scale = math.sqrt(1.0 + value[1, 1] - value[0, 0] - value[2, 2]) * 2.0
            result = np.array(
                [
                    (value[0, 2] - value[2, 0]) / scale,
                    (value[0, 1] + value[1, 0]) / scale,
                    0.25 * scale,
                    (value[1, 2] + value[2, 1]) / scale,
                ]
            )
        else:
            scale = math.sqrt(1.0 + value[2, 2] - value[0, 0] - value[1, 1]) * 2.0
            result = np.array(
                [
                    (value[1, 0] - value[0, 1]) / scale,
                    (value[0, 2] + value[2, 0]) / scale,
                    (value[1, 2] + value[2, 1]) / scale,
                    0.25 * scale,
                ]
            )
    return quaternion_normalize(result)


def quaternion_matrix(quaternion: Float64Array) -> Float64Array:
    """Return an exact 4x4 rotation matrix for a ``(w, x, y, z)`` quaternion."""
    w, x, y, z = quaternion_normalize(quaternion)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0],
            [0, 0, 0, 1],
        ],
        dtype=np.float64,
    )


def default_orientation() -> Float64Array:
    yaw = quaternion_from_axis_angle(np.array([0.0, 1.0, 0.0]), math.radians(45.0))
    pitch = quaternion_from_axis_angle(np.array([1.0, 0.0, 0.0]), math.radians(-25.0))
    return quaternion_normalize(quaternion_multiply(yaw, pitch))


@dataclass(slots=True)
class CameraState:
    target: Float64Array = field(default_factory=lambda: np.zeros(3, dtype=np.float64))
    distance: float = 10.0
    orientation: Float64Array = field(default_factory=default_orientation)

    def __post_init__(self) -> None:
        self.target = np.asarray(self.target, dtype=np.float64)
        self.orientation = quaternion_normalize(self.orientation)
        if self.target.shape != (3,) or not np.all(np.isfinite(self.target)):
            raise ValueError("camera target must contain three finite values")
        if not math.isfinite(self.distance) or self.distance <= 0.0:
            raise ValueError("camera distance must be positive")

    @property
    def position(self) -> Float64Array:
        return self.target + quaternion_rotate(
            self.orientation, np.array([0.0, 0.0, self.distance], dtype=np.float64)
        )

    @property
    def right(self) -> Float64Array:
        return quaternion_rotate(self.orientation, np.array([1.0, 0.0, 0.0]))

    @property
    def up(self) -> Float64Array:
        return quaternion_rotate(self.orientation, np.array([0.0, 1.0, 0.0]))

    @property
    def forward(self) -> Float64Array:
        return quaternion_rotate(self.orientation, np.array([0.0, 0.0, -1.0]))

    def view_matrix(self) -> Float64Array:
        forward = normalize(self.target - self.position)
        right = normalize(np.cross(forward, self.up))
        up = np.cross(right, forward)
        matrix = np.identity(4, dtype=np.float64)
        matrix[0, :3] = right
        matrix[1, :3] = up
        matrix[2, :3] = -forward
        matrix[0, 3] = -float(np.dot(right, self.position))
        matrix[1, 3] = -float(np.dot(up, self.position))
        matrix[2, 3] = float(np.dot(forward, self.position))
        return matrix

    def orbit(self, delta_x: float, delta_y: float) -> None:
        yaw = quaternion_from_axis_angle(np.array([0.0, 1.0, 0.0]), -delta_x * 0.008)
        yawed = quaternion_multiply(yaw, self.orientation)
        pitch_axis = quaternion_rotate(yawed, np.array([1.0, 0.0, 0.0]))
        pitch = quaternion_from_axis_angle(pitch_axis, -delta_y * 0.008)
        self.orientation = quaternion_normalize(quaternion_multiply(pitch, yawed))

    def trackball(self, previous_ndc: Float64Array, current_ndc: Float64Array) -> None:
        """Free-trackball rotate between two normalized viewport coordinates."""
        def point(value: Float64Array) -> Float64Array:
            x, y = np.asarray(value, dtype=np.float64)
            length2 = x * x + y * y
            z = math.sqrt(max(0.0, 1.0 - length2))
            result = np.array([x, y, z], dtype=np.float64)
            return normalize(result)

        start, end = point(previous_ndc), point(current_ndc)
        axis_camera = np.cross(start, end)
        length = float(np.linalg.norm(axis_camera))
        if length <= 1.0e-12:
            return
        axis_camera /= length
        axis_world = (
            self.right * axis_camera[0]
            + self.up * axis_camera[1]
            - self.forward * axis_camera[2]
        )
        angle = math.acos(float(np.clip(np.dot(start, end), -1.0, 1.0)))
        rotation = quaternion_from_axis_angle(axis_world, angle)
        self.orientation = quaternion_normalize(
            quaternion_multiply(rotation, self.orientation)
        )

    def roll(self, delta: float) -> None:
        rotation = quaternion_from_axis_angle(self.forward, -delta * 0.008)
        self.orientation = quaternion_normalize(
            quaternion_multiply(rotation, self.orientation)
        )

    def pan(
        self,
        delta_x: float,
        delta_y: float,
        world_units_per_pixel_x: float,
        world_units_per_pixel_y: float | None = None,
    ) -> None:
        vertical = world_units_per_pixel_x if world_units_per_pixel_y is None else world_units_per_pixel_y
        self.target += -self.right * delta_x * world_units_per_pixel_x + self.up * delta_y * vertical

    def focus_point_preserve_position(self, point: Float64Array) -> None:
        position = self.position.copy()
        target = np.asarray(point, dtype=np.float64)
        offset = target - position
        distance = float(np.linalg.norm(offset))
        if distance <= 1e-9:
            return
        forward = offset / distance
        preferred_up = self.up
        right = np.cross(forward, preferred_up)
        if float(np.linalg.norm(right)) <= 1e-8:
            right = self.right
        right = normalize(right)
        up = normalize(np.cross(right, forward))
        self.target = target.copy()
        self.distance = distance
        self.orientation = quaternion_from_rotation_matrix(np.column_stack((right, up, -forward)))

    def dolly(self, wheel_steps: float, minimum_distance: float = 1.0e-4) -> None:
        self.distance = max(minimum_distance, self.distance * math.exp(-wheel_steps * 0.15))

    def frame_bounds(
        self,
        bounds_min: Float64Array,
        bounds_max: Float64Array,
        aspect: float,
        vertical_fov_deg: float,
        reset_orientation_value: bool = False,
    ) -> float:
        low = np.asarray(bounds_min, dtype=np.float64)
        high = np.asarray(bounds_max, dtype=np.float64)
        self.target = (low + high) * 0.5
        radius = max(float(np.linalg.norm((high - low) * 0.5)), 1.0e-6)
        if reset_orientation_value:
            self.orientation = default_orientation()
        vertical_half = math.radians(vertical_fov_deg) * 0.5
        horizontal_half = math.atan(math.tan(vertical_half) * max(aspect, 1.0e-6))
        fit_half = max(min(vertical_half, horizontal_half), math.radians(1.0))
        self.distance = 1.1 * radius / math.sin(fit_half)
        return radius
