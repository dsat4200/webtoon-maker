"""Portable six-face cubemap helpers used by fisheye rendering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math

import numpy as np
from numpy.typing import NDArray

from .camera import CameraState, quaternion_rotate
from .projection import CubemapQuality, FisheyeCrop, ProjectionMode, ProjectionSettings


class CubeFace(IntEnum):
    POSITIVE_X = 0
    NEGATIVE_X = 1
    POSITIVE_Y = 2
    NEGATIVE_Y = 3
    POSITIVE_Z = 4
    NEGATIVE_Z = 5


FACE_BASES = np.asarray(
    [
        [[1, 0, 0], [0, 0, 1], [0, 1, 0]],
        [[-1, 0, 0], [0, 0, -1], [0, 1, 0]],
        [[0, 1, 0], [1, 0, 0], [0, 0, 1]],
        [[0, -1, 0], [1, 0, 0], [0, 0, -1]],
        [[0, 0, 1], [-1, 0, 0], [0, 1, 0]],
        [[0, 0, -1], [1, 0, 0], [0, 1, 0]],
    ],
    dtype=np.float64,
)


@dataclass(frozen=True, slots=True)
class FaceSample:
    face: CubeFace
    uv: NDArray[np.float64]


def face_gutter_tangent(face_size: int) -> float:
    return 1.0 + 2.0 / max(int(face_size), 1)


def direction_to_face_uv(direction: NDArray[np.float64], gutter_tangent: float = 1.0) -> FaceSample:
    ray = np.asarray(direction, dtype=np.float64)
    length = float(np.linalg.norm(ray))
    if length <= 1.0e-12:
        raise ValueError("cubemap direction must be nonzero")
    ray /= length
    face_index = int(np.argmax(FACE_BASES[:, 0, :] @ ray))
    forward, right, up = FACE_BASES[face_index]
    depth = float(np.dot(ray, forward))
    uv = np.array(
        [
            0.5 + 0.5 * float(np.dot(ray, right)) / depth / gutter_tangent,
            0.5 + 0.5 * float(np.dot(ray, up)) / depth / gutter_tangent,
        ],
        dtype=np.float64,
    )
    return FaceSample(CubeFace(face_index), uv)


def face_view_matrix(camera: CameraState, face: CubeFace) -> NDArray[np.float64]:
    forward, right, up = FACE_BASES[int(face)]
    world_forward = quaternion_rotate(camera.orientation, forward)
    world_right = quaternion_rotate(camera.orientation, right)
    world_up = quaternion_rotate(camera.orientation, up)
    position = camera.position
    matrix = np.identity(4, dtype=np.float64)
    matrix[0, :3], matrix[1, :3], matrix[2, :3] = world_right, world_up, -world_forward
    matrix[0, 3] = -float(np.dot(world_right, position))
    matrix[1, 3] = -float(np.dot(world_up, position))
    matrix[2, 3] = float(np.dot(world_forward, position))
    return matrix


def required_cube_faces(settings: ProjectionSettings) -> tuple[CubeFace, ...]:
    if settings.mode is not ProjectionMode.FISHEYE:
        return ()
    faces = [CubeFace.NEGATIVE_Z]
    if settings.effective_fisheye_fov_deg * 0.5 >= 45.0 - 1e-7:
        faces.extend((CubeFace.POSITIVE_X, CubeFace.NEGATIVE_X, CubeFace.POSITIVE_Y, CubeFace.NEGATIVE_Y))
    if settings.effective_fisheye_fov_deg > 180.0:
        faces.append(CubeFace.POSITIVE_Z)
    return tuple(faces)


_QUALITY = {
    CubemapQuality.PERFORMANCE: (0.75, 768, 384),
    CubemapQuality.BALANCED: (1.0, 1024, 512),
    CubemapQuality.HIGH: (1.5, 2048, 1024),
    CubemapQuality.ADAPTIVE: (1.0, 1024, 512),
}

ADAPTIVE_CUBEMAP_TIERS = (256, 384, 512, 768, 1024, 1536, 2048)


@dataclass(slots=True)
class AdaptiveCubemapController:
    refresh_rate_hz: float = 60.0
    current_size: int = 0
    elapsed_ms: float | None = None
    _sample_count: int = 0

    @property
    def target_ms(self) -> float:
        return 0.7 * 1000.0 / max(self.refresh_rate_hz, 1.0)

    def choose_size(self, base: float, max_texture_size: int) -> int:
        useful_cap = min(max(int(math.ceil(max(base * 2.0, 256.0))), 256), 2048, max(int(max_texture_size), 64))
        tiers = [tier for tier in ADAPTIVE_CUBEMAP_TIERS if tier <= useful_cap] or [max(64, min(useful_cap, max_texture_size))]
        if self.current_size not in tiers:
            self.current_size = next((tier for tier in tiers if tier >= base), tiers[-1])
        return self.current_size

    def record(self, elapsed_ms: float, base: float, max_texture_size: int) -> bool:
        value = max(float(elapsed_ms), 0.0)
        self.elapsed_ms = value if self.elapsed_ms is None else self.elapsed_ms * 0.75 + value * 0.25
        self._sample_count += 1
        if self._sample_count < 8:
            return False
        self._sample_count = 0
        current = self.choose_size(base, max_texture_size)
        tiers = [tier for tier in ADAPTIVE_CUBEMAP_TIERS if tier <= max_texture_size]
        if current not in tiers:
            return False
        index = tiers.index(current)
        next_index = index + 1 if self.elapsed_ms < self.target_ms * 0.55 and index + 1 < len(tiers) else index - 1 if self.elapsed_ms > self.target_ms * 0.9 and index > 0 else index
        if next_index == index:
            return False
        self.current_size = tiers[next_index]
        return True


def cubemap_base_size(settings: ProjectionSettings, viewport_size: tuple[int, int]) -> float:
    if settings.mode is not ProjectionMode.FISHEYE:
        return 0.0
    width, height = max(viewport_size[0], 1), max(viewport_size[1], 1)
    reference = min(width, height) if settings.fisheye_crop is FisheyeCrop.CIRCULAR else math.hypot(width, height)
    return reference * 90.0 / settings.effective_fisheye_fov_deg


def cubemap_face_size(
    settings: ProjectionSettings,
    viewport_size: tuple[int, int],
    *,
    interactive: bool,
    max_texture_size: int,
) -> int:
    base = cubemap_base_size(settings, viewport_size)
    if base <= 0.0:
        return 0
    scale, settled_cap, interactive_cap = _QUALITY[settings.cubemap_quality]
    desired = base * scale * (0.5 if interactive else 1.0)
    desired = min(max(desired, 128.0), interactive_cap if interactive else settled_cap, max_texture_size)
    return max(64, int(math.ceil(desired / 64.0)) * 64)


class CubemapTarget:
    """Six portable 2D face targets (avoids cubemap-layer requirements)."""

    def __init__(self, context: object, size: int, moderngl_module: object) -> None:
        self.context, self.size = context, int(size)
        self.framebuffers: list[object] = []
        self.color_textures: list[object] = []
        self.depth_textures: list[object] = []
        try:
            for _face in CubeFace:
                color = context.texture((self.size, self.size), 4, dtype="f1")  # type: ignore[attr-defined]
                color.filter = (moderngl_module.LINEAR, moderngl_module.LINEAR)  # type: ignore[attr-defined]
                color.repeat_x = color.repeat_y = False
                depth = context.depth_texture((self.size, self.size))  # type: ignore[attr-defined]
                depth.filter = (moderngl_module.NEAREST, moderngl_module.NEAREST)  # type: ignore[attr-defined]
                depth.repeat_x = depth.repeat_y = False
                depth.compare_func = ""
                self.color_textures.append(color); self.depth_textures.append(depth)
                self.framebuffers.append(context.framebuffer([color], depth))  # type: ignore[attr-defined]
        except Exception:
            self.release()
            raise

    def release(self) -> None:
        for group in (self.framebuffers, self.color_textures, self.depth_textures):
            for resource in group:
                resource.release()  # type: ignore[attr-defined]
            group.clear()
