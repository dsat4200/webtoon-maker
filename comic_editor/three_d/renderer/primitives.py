"""Frame-local cube, cylinder, and floor geometry."""

from __future__ import annotations

import math
import uuid

import numpy as np

from .mesh import MeshData, MeshPrimitive, compute_vertex_normals
from .scene import SceneNode


def _mesh(mesh_id: str, name: str, positions: np.ndarray, indices: np.ndarray) -> MeshData:
    positions = np.asarray(positions, dtype=np.float32)
    indices = np.asarray(indices, dtype=np.uint32).reshape((-1, 3))
    normals = compute_vertex_normals(positions, indices)
    return MeshData(mesh_id, name, (MeshPrimitive(positions, indices, normals=normals),))


def cube_mesh(mesh_id: str = "local:cube", size: tuple[float, float, float] = (1.0, 1.0, 1.0)) -> MeshData:
    sx, sy, sz = (max(abs(float(v)), 1e-6) * 0.5 for v in size)
    positions = np.array(
        [[x, y, z] for x, y, z in (
            (-sx,-sy,-sz),(sx,-sy,-sz),(sx,sy,-sz),(-sx,sy,-sz),
            (-sx,-sy,sz),(sx,-sy,sz),(sx,sy,sz),(-sx,sy,sz),
        )], dtype=np.float32
    )
    indices = np.array([
        0,2,1,0,3,2,4,5,6,4,6,7,0,1,5,0,5,4,
        3,7,6,3,6,2,0,4,7,0,7,3,1,2,6,1,6,5,
    ], dtype=np.uint32)
    return _mesh(mesh_id, "Cube", positions, indices)


def cylinder_mesh(
    mesh_id: str = "local:cylinder",
    radius: float = 0.5,
    height: float = 1.0,
    segments: int = 32,
) -> MeshData:
    radius, half_height = max(abs(float(radius)), 1e-6), max(abs(float(height)), 1e-6) * 0.5
    segments = max(3, min(int(segments), 256))
    positions: list[tuple[float, float, float]] = []
    for y in (-half_height, half_height):
        positions.extend((radius * math.cos(2*math.pi*i/segments), y, radius * math.sin(2*math.pi*i/segments)) for i in range(segments))
    positions.extend(((0.0, -half_height, 0.0), (0.0, half_height, 0.0)))
    bottom_center, top_center = 2 * segments, 2 * segments + 1
    triangles: list[tuple[int, int, int]] = []
    for i in range(segments):
        j = (i + 1) % segments
        triangles.extend(((i, segments+j, j), (i, segments+i, segments+j), (bottom_center, j, i), (top_center, segments+i, segments+j)))
    return _mesh(mesh_id, "Cylinder", np.asarray(positions), np.asarray(triangles))


def floor_mesh(mesh_id: str = "internal:floor", extent: float = 20.0) -> MeshData:
    value = max(float(extent), 0.1)
    return _mesh(mesh_id, "Floor", np.array([[-value,0,-value],[value,0,-value],[value,0,value],[-value,0,value]]), np.array([0,1,2,0,2,3]))


def create_local_node(kind: str, *, matrix: np.ndarray | None = None) -> tuple[SceneNode, MeshData]:
    identifier = f"local:{uuid.uuid4().hex}"
    if kind == "cube":
        mesh = cube_mesh(identifier + ":mesh")
    elif kind == "cylinder":
        mesh = cylinder_mesh(identifier + ":mesh")
    else:
        raise ValueError("local primitive kind must be cube or cylinder")
    node = SceneNode(identifier, kind.title(), np.identity(4) if matrix is None else matrix)
    return node, mesh


def surface_alignment_matrix(
    position: np.ndarray,
    normal: np.ndarray,
    forward_hint: np.ndarray = np.array([0.0, 0.0, -1.0]),
) -> np.ndarray:
    """Place a local primitive on a picked surface with local Y along normal."""
    up = np.asarray(normal, dtype=np.float64)
    up /= max(float(np.linalg.norm(up)), 1e-12)
    hint = np.asarray(forward_hint, dtype=np.float64)
    forward = hint - up * float(np.dot(hint, up))
    if float(np.linalg.norm(forward)) <= 1e-8:
        fallback = np.array([1.0, 0.0, 0.0]) if abs(up[0]) < 0.9 else np.array([0.0, 0.0, 1.0])
        forward = fallback - up * float(np.dot(fallback, up))
    forward /= max(float(np.linalg.norm(forward)), 1e-12)
    right = np.cross(up, forward)
    right /= max(float(np.linalg.norm(right)), 1e-12)
    forward = np.cross(right, up)
    result = np.identity(4, dtype=np.float64)
    result[:3, 0], result[:3, 1], result[:3, 2] = right, up, -forward
    result[:3, 3] = np.asarray(position, dtype=np.float64)
    return result
