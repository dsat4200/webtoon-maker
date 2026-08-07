"""Neutral CPU mesh, texture, skin, and morph data."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum

import numpy as np
from numpy.typing import NDArray


class AlphaMode(str, Enum):
    OPAQUE = "OPAQUE"
    MASK = "MASK"
    BLEND = "BLEND"


class TextureFilter(IntEnum):
    NEAREST = 9728
    LINEAR = 9729
    NEAREST_MIPMAP_NEAREST = 9984
    LINEAR_MIPMAP_NEAREST = 9985
    NEAREST_MIPMAP_LINEAR = 9986
    LINEAR_MIPMAP_LINEAR = 9987


class TextureWrap(IntEnum):
    CLAMP_TO_EDGE = 33071
    MIRRORED_REPEAT = 33648
    REPEAT = 10497


@dataclass(frozen=True, slots=True)
class TextureData:
    texture_id: str
    name: str
    pixels: NDArray[np.uint8]
    mag_filter: TextureFilter = TextureFilter.LINEAR
    min_filter: TextureFilter = TextureFilter.LINEAR
    wrap_s: TextureWrap = TextureWrap.REPEAT
    wrap_t: TextureWrap = TextureWrap.REPEAT

    def __post_init__(self) -> None:
        pixels = np.asarray(self.pixels, dtype=np.uint8)
        if not self.texture_id or pixels.ndim != 3 or pixels.shape[2] != 4 or min(pixels.shape[:2]) <= 0:
            raise ValueError("texture must have an id and non-empty uint8 RGBA pixels")
        object.__setattr__(self, "pixels", np.ascontiguousarray(pixels))
        object.__setattr__(self, "mag_filter", TextureFilter(self.mag_filter))
        object.__setattr__(self, "min_filter", TextureFilter(self.min_filter))
        object.__setattr__(self, "wrap_s", TextureWrap(self.wrap_s))
        object.__setattr__(self, "wrap_t", TextureWrap(self.wrap_t))

    @property
    def size(self) -> tuple[int, int]:
        return int(self.pixels.shape[1]), int(self.pixels.shape[0])


@dataclass(frozen=True, slots=True)
class SourceMaterial:
    material_id: str
    name: str = "Default"
    base_color_factor: NDArray[np.float32] = field(default_factory=lambda: np.ones(4, dtype=np.float32))
    base_color_texture: int | None = None
    alpha_mode: AlphaMode = AlphaMode.OPAQUE
    alpha_cutoff: float = 0.5
    double_sided: bool = False
    base_color_texcoord: int = 0

    def __post_init__(self) -> None:
        factor = np.asarray(self.base_color_factor, dtype=np.float32)
        if not self.material_id or factor.shape != (4,) or not np.all(np.isfinite(factor)):
            raise ValueError("source material id/factor is invalid")
        if self.base_color_texture is not None and self.base_color_texture < 0:
            raise ValueError("texture index must be non-negative")
        if self.base_color_texcoord < 0:
            raise ValueError("texture-coordinate set must be non-negative")
        if not 0.0 <= self.alpha_cutoff <= 1.0:
            raise ValueError("alpha cutoff must be between zero and one")
        object.__setattr__(self, "base_color_factor", factor)
        object.__setattr__(self, "alpha_mode", AlphaMode(self.alpha_mode))


@dataclass(frozen=True, slots=True)
class MorphTarget:
    name: str
    position_deltas: NDArray[np.float32] | None = None
    normal_deltas: NDArray[np.float32] | None = None

    def __post_init__(self) -> None:
        for attribute in ("position_deltas", "normal_deltas"):
            value = getattr(self, attribute)
            if value is not None:
                array = np.asarray(value, dtype=np.float32)
                if array.ndim != 2 or array.shape[1] != 3:
                    raise ValueError("morph target arrays must have shape (N, 3)")
                object.__setattr__(self, attribute, np.ascontiguousarray(array))


@dataclass(frozen=True, slots=True)
class MeshPrimitive:
    positions: NDArray[np.float32]
    indices: NDArray[np.uint32]
    material_index: int = 0
    normals: NDArray[np.float32] | None = None
    texcoords: NDArray[np.float32] | None = None
    colors: NDArray[np.float32] | None = None
    joints: NDArray[np.uint16] | None = None
    weights: NDArray[np.float32] | None = None
    morph_targets: tuple[MorphTarget, ...] = ()

    def __post_init__(self) -> None:
        positions = np.asarray(self.positions, dtype=np.float32)
        indices = np.asarray(self.indices, dtype=np.uint32)
        if positions.ndim != 2 or positions.shape[1] != 3 or not len(positions):
            raise ValueError("positions must have shape (N, 3)")
        if indices.ndim == 1:
            if len(indices) % 3:
                raise ValueError("triangle index count must be divisible by three")
            indices = indices.reshape((-1, 3))
        if indices.ndim != 2 or indices.shape[1] != 3 or not len(indices):
            raise ValueError("indices must have shape (T, 3)")
        if int(indices.max(initial=0)) >= len(positions):
            raise ValueError("triangle index references a missing vertex")
        object.__setattr__(self, "positions", np.ascontiguousarray(positions))
        object.__setattr__(self, "indices", np.ascontiguousarray(indices))
        lengths = {len(positions)}
        for name, width, dtype in (
            ("normals", 3, np.float32), ("texcoords", 2, np.float32),
            ("colors", 4, np.float32), ("joints", 4, np.uint16), ("weights", 4, np.float32),
        ):
            value = getattr(self, name)
            if value is None:
                continue
            array = np.asarray(value, dtype=dtype)
            if array.shape != (len(positions), width):
                raise ValueError(f"{name} must have shape ({len(positions)}, {width})")
            object.__setattr__(self, name, np.ascontiguousarray(array))
            lengths.add(len(array))
        if (self.joints is None) != (self.weights is None):
            raise ValueError("joint indices and weights must be provided together")
        if any(target.position_deltas is not None and len(target.position_deltas) != len(positions) for target in self.morph_targets):
            raise ValueError("morph position count must match vertex count")

    @property
    def bounds(self) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        values = self.positions.astype(np.float64)
        return values.min(axis=0), values.max(axis=0)

    def evaluated_positions(self, morph_weights: tuple[float, ...] = ()) -> NDArray[np.float32]:
        result = self.positions.copy()
        for target, weight in zip(self.morph_targets, morph_weights):
            if target.position_deltas is not None and weight:
                result += target.position_deltas * np.float32(weight)
        return result


@dataclass(frozen=True, slots=True)
class MeshData:
    mesh_id: str
    name: str
    primitives: tuple[MeshPrimitive, ...]
    default_morph_weights: tuple[float, ...] = ()
    extras: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.mesh_id or not self.primitives:
            raise ValueError("mesh requires an id and at least one primitive")

    @property
    def bounds(self) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        bounds = [primitive.bounds for primitive in self.primitives]
        return np.min([item[0] for item in bounds], axis=0), np.max([item[1] for item in bounds], axis=0)

    @property
    def triangle_count(self) -> int:
        return sum(len(primitive.indices) for primitive in self.primitives)


@dataclass(frozen=True, slots=True)
class SkinData:
    skin_id: str
    name: str
    joint_node_ids: tuple[str, ...]
    inverse_bind_matrices: NDArray[np.float64]
    skeleton_root_id: str | None = None

    def __post_init__(self) -> None:
        matrices = np.asarray(self.inverse_bind_matrices, dtype=np.float64)
        if not self.skin_id or matrices.shape != (len(self.joint_node_ids), 4, 4):
            raise ValueError("skin inverse-bind matrices must match its joints")
        object.__setattr__(self, "inverse_bind_matrices", matrices)


def compute_vertex_normals(positions: NDArray[np.float32], indices: NDArray[np.uint32]) -> NDArray[np.float32]:
    normals = np.zeros_like(positions, dtype=np.float64)
    triangles = positions[indices].astype(np.float64)
    face_normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    for corner in range(3):
        np.add.at(normals, indices[:, corner], face_normals)
    lengths = np.linalg.norm(normals, axis=1)
    valid = lengths > 1e-12
    normals[valid] /= lengths[valid, None]
    normals[~valid] = (0.0, 1.0, 0.0)
    return normals.astype(np.float32)
