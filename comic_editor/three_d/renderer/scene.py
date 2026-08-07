"""Neutral scene graph with exact authored local/world matrices."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import math

import numpy as np
from numpy.typing import NDArray

from .camera import CameraState
from .materials import DrawingMaterial
from .mesh import MeshData, SkinData, SourceMaterial, TextureData
from .projection import ProjectionSettings


def _matrix(value: NDArray[np.floating]) -> NDArray[np.float64]:
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (4, 4) or not np.all(np.isfinite(result)):
        raise ValueError("transform must be a finite 4x4 matrix")
    return result.copy()


class TransformSpace(str, Enum):
    LOCAL = "local"
    GLOBAL = "global"


class LightType(str, Enum):
    SUN = "sun"
    POINT = "point"
    RECTANGLE = "rectangle"
    SPOT = "spot"


@dataclass(slots=True)
class SceneLight:
    light_id: str
    name: str = "Light"
    light_type: LightType = LightType.SUN
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    energy: float = 1.0
    range: float = 0.0
    area_size: tuple[float, float] = (1.0, 1.0)
    spot_outer_angle: float = math.radians(45.0)
    spot_inner_angle: float = 0.0
    casts_shadow: bool = True
    visible: bool = True
    raw_source: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.light_type = LightType(self.light_type)
        if not self.light_id or len(self.color) != 3 or any(not math.isfinite(v) or v < 0 for v in self.color):
            raise ValueError("light id/color is invalid")
        if self.energy < 0.0 or self.range < 0.0:
            raise ValueError("light energy/range cannot be negative")


@dataclass(slots=True)
class SceneCamera:
    camera_id: str
    name: str = "Camera"
    perspective: bool = True
    yfov: float = math.radians(50.0)
    ortho_height: float = 10.0
    near: float = 0.01
    far: float = 1000.0
    raw_source: dict[str, object] = field(default_factory=dict)
    shift_x: float = 0.0
    shift_y: float = 0.0
    sensor_fit: str = "AUTO"


@dataclass(slots=True)
class SceneNode:
    node_id: str
    name: str
    local_matrix: NDArray[np.float64] = field(default_factory=lambda: np.identity(4, dtype=np.float64))
    world_matrix: NDArray[np.float64] = field(default_factory=lambda: np.identity(4, dtype=np.float64))
    parent_id: str | None = None
    child_ids: tuple[str, ...] = ()
    mesh_index: int | None = None
    skin_index: int | None = None
    camera_index: int | None = None
    light_index: int | None = None
    morph_weights: tuple[float, ...] = ()
    visible: bool = True
    extras: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("node id must be non-empty")
        self.local_matrix = _matrix(self.local_matrix)
        self.world_matrix = _matrix(self.world_matrix)

    @property
    def determinant(self) -> float:
        return float(np.linalg.det(self.world_matrix[:3, :3]))

    @property
    def world_origin(self) -> NDArray[np.float64]:
        return self.world_matrix[:3, 3].copy()

    @property
    def normal_matrix(self) -> NDArray[np.float64]:
        return np.linalg.pinv(self.world_matrix[:3, :3]).T


@dataclass(slots=True)
class OverlaySettings:
    grid_visible: bool = True
    volume_grid_visible: bool = False
    axes_visible: bool = True
    floor_visible: bool = True
    grid_extent: float = 20.0


@dataclass(slots=True)
class ShadowSettings:
    enabled: bool = True
    resolution: int = 1024
    bias: float = 0.0015
    opacity: float = 0.55

    def __post_init__(self) -> None:
        if self.resolution not in (256, 512, 1024, 2048, 4096):
            raise ValueError("shadow resolution must be a supported power-of-two size")


@dataclass(slots=True)
class SceneData:
    scene_id: str = "scene"
    nodes: dict[str, SceneNode] = field(default_factory=dict)
    root_node_ids: tuple[str, ...] = ()
    meshes: tuple[MeshData, ...] = ()
    textures: tuple[TextureData, ...] = ()
    source_materials: tuple[SourceMaterial, ...] = ()
    drawing_materials: dict[str, DrawingMaterial] = field(default_factory=dict)
    material_mappings: dict[str, str] = field(default_factory=dict)
    skins: tuple[SkinData, ...] = ()
    cameras: tuple[SceneCamera, ...] = ()
    lights: tuple[SceneLight, ...] = ()
    active_camera: CameraState = field(default_factory=CameraState)
    projection: ProjectionSettings = field(default_factory=ProjectionSettings)
    overlays: OverlaySettings = field(default_factory=OverlaySettings)
    shadows: ShadowSettings = field(default_factory=ShadowSettings)
    ambient_color: tuple[float, float, float] = (0.18, 0.18, 0.18)
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.scene_id:
            raise ValueError("scene id must be non-empty")
        self.validate()
        self.recompute_world_matrices()

    def validate(self) -> None:
        for key, node in self.nodes.items():
            if key != node.node_id:
                raise ValueError("scene node dictionary keys must match node ids")
            if node.parent_id is not None and node.parent_id not in self.nodes:
                raise ValueError(f"node {node.node_id!r} has a missing parent")
            for child_id in node.child_ids:
                if child_id not in self.nodes or self.nodes[child_id].parent_id != node.node_id:
                    raise ValueError("scene parent/child links are inconsistent")
            if node.mesh_index is not None and not 0 <= node.mesh_index < len(self.meshes):
                raise ValueError("node references a missing mesh")
            if node.skin_index is not None and not 0 <= node.skin_index < len(self.skins):
                raise ValueError("node references a missing skin")
        expected_roots = {node.node_id for node in self.nodes.values() if node.parent_id is None}
        if self.root_node_ids and set(self.root_node_ids) != expected_roots:
            raise ValueError("root node list does not match hierarchy")
        if not self.root_node_ids:
            self.root_node_ids = tuple(node_id for node_id in self.nodes if node_id in expected_roots)
        for material in self.source_materials:
            if material.base_color_texture is not None and material.base_color_texture >= len(self.textures):
                raise ValueError("material references a missing texture")

    def recompute_world_matrices(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str, parent_world: NDArray[np.float64]) -> None:
            if node_id in visiting:
                raise ValueError("scene hierarchy contains a cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            node = self.nodes[node_id]
            node.world_matrix = parent_world @ node.local_matrix
            for child_id in node.child_ids:
                visit(child_id, node.world_matrix)
            visiting.remove(node_id)
            visited.add(node_id)

        for root_id in self.root_node_ids:
            visit(root_id, np.identity(4, dtype=np.float64))
        if len(visited) != len(self.nodes):
            raise ValueError("scene hierarchy has unreachable nodes")

    def set_local_matrix(self, node_id: str, matrix: NDArray[np.floating]) -> None:
        self.nodes[node_id].local_matrix = _matrix(matrix)
        self.recompute_world_matrices()

    def set_world_matrix(self, node_id: str, matrix: NDArray[np.floating]) -> None:
        world = _matrix(matrix)
        node = self.nodes[node_id]
        if node.parent_id is None:
            node.local_matrix = world
        else:
            node.local_matrix = np.linalg.pinv(self.nodes[node.parent_id].world_matrix) @ world
        self.recompute_world_matrices()

    def apply_transform(self, node_id: str, delta: NDArray[np.floating], space: TransformSpace) -> None:
        node = self.nodes[node_id]
        value = _matrix(delta)
        if TransformSpace(space) is TransformSpace.LOCAL:
            self.set_local_matrix(node_id, node.local_matrix @ value)
        else:
            self.set_world_matrix(node_id, value @ node.world_matrix)

    def visible_nodes(self) -> tuple[SceneNode, ...]:
        result: list[SceneNode] = []

        def visit(node_id: str, parent_visible: bool) -> None:
            node = self.nodes[node_id]
            visible = parent_visible and node.visible
            if visible and node.mesh_index is not None:
                result.append(node)
            for child in node.child_ids:
                visit(child, visible)

        for root in self.root_node_ids:
            visit(root, True)
        return tuple(result)

    def bounds(self) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        corners: list[NDArray[np.float64]] = []
        for node in self.visible_nodes():
            low, high = self.meshes[node.mesh_index].bounds  # type: ignore[index]
            local_corners = np.asarray(
                [[x, y, z, 1.0] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]
            )
            corners.append((node.world_matrix @ local_corners.T).T[:, :3])
        if not corners:
            return np.array([-1.0, -1.0, -1.0]), np.array([1.0, 1.0, 1.0])
        values = np.concatenate(corners)
        return values.min(axis=0), values.max(axis=0)

    @property
    def enabled_lights(self) -> tuple[tuple[SceneNode, SceneLight], ...]:
        pairs = []
        for node in self.nodes.values():
            if node.visible and node.light_index is not None:
                light = self.lights[node.light_index]
                if light.visible:
                    pairs.append((node, light))
        return tuple(pairs[:8])

    def copy(self) -> "SceneData":
        return replace(self, nodes={key: replace(value) for key, value in self.nodes.items()})
