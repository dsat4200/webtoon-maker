"""Projection-aware ray and triangle picking."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .camera import CameraState
from .mesh import MeshPrimitive
from .projection import ProjectionContext, ProjectionSettings
from .scene import SceneData


@dataclass(frozen=True, slots=True)
class WorldRay:
    origin: NDArray[np.float64]
    direction: NDArray[np.float64]
    lens_valid: bool = True


@dataclass(frozen=True, slots=True)
class PickHit:
    node_id: str
    distance: float
    point: NDArray[np.float64]
    normal: NDArray[np.float64]
    triangle_index: int


def screen_to_world_ray(screen_px: NDArray[np.float64], camera: CameraState, projection: ProjectionSettings, context: ProjectionContext) -> WorldRay:
    ray = projection.screen_to_ray_cpu(screen_px, context)
    direction = camera.right * ray.direction[0] + camera.up * ray.direction[1] - camera.forward * ray.direction[2]
    origin = camera.position + camera.right * ray.origin[0] + camera.up * ray.origin[1] - camera.forward * ray.origin[2]
    length = float(np.linalg.norm(direction))
    return WorldRay(origin, direction / max(length, 1e-12), ray.lens_valid)


def _pick_primitive(ray: WorldRay, primitive: MeshPrimitive, matrix: NDArray[np.float64], node_id: str) -> PickHit | None:
    inverse = np.linalg.pinv(matrix)
    local_origin = (inverse @ np.append(ray.origin, 1.0))[:3]
    local_direction = (inverse @ np.append(ray.direction, 0.0))[:3]
    positions = primitive.positions.astype(np.float64)
    triangles = positions[primitive.indices]
    edge1, edge2 = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    pvec = np.cross(np.broadcast_to(local_direction, edge2.shape), edge2)
    determinant = np.einsum("ij,ij->i", edge1, pvec)
    valid = np.abs(determinant) > 1e-10
    inverse_det = np.zeros_like(determinant)
    inverse_det[valid] = 1.0 / determinant[valid]
    tvec = local_origin - triangles[:, 0]
    u = np.einsum("ij,ij->i", tvec, pvec) * inverse_det
    qvec = np.cross(tvec, edge1)
    v = qvec @ local_direction * inverse_det
    t = np.einsum("ij,ij->i", edge2, qvec) * inverse_det
    valid &= (u >= 0) & (v >= 0) & (u + v <= 1) & (t >= 0)
    candidates = np.flatnonzero(valid)
    if not len(candidates):
        return None
    local_index = int(candidates[np.argmin(t[candidates])])
    local_point = local_origin + local_direction * t[local_index]
    world_point = (matrix @ np.append(local_point, 1.0))[:3]
    local_normal = np.cross(edge1[local_index], edge2[local_index])
    world_normal = np.linalg.pinv(matrix[:3, :3]).T @ local_normal
    world_normal /= max(float(np.linalg.norm(world_normal)), 1e-12)
    # Keep placement consistently on the camera-facing side of two-sided
    # geometry, even when an imported object has a signed transform.
    if float(np.dot(world_normal, ray.direction)) > 0.0:
        world_normal *= -1.0
    return PickHit(
        node_id, float(np.linalg.norm(world_point - ray.origin)),
        world_point, world_normal, local_index,
    )


def pick_scene(scene: SceneData, ray: WorldRay) -> PickHit | None:
    if not ray.lens_valid:
        return None
    hits: list[PickHit] = []
    for node in scene.visible_nodes():
        mesh = scene.meshes[node.mesh_index]  # type: ignore[index]
        for primitive in mesh.primitives:
            hit = _pick_primitive(ray, primitive, node.world_matrix, node.node_id)
            if hit is not None:
                hits.append(hit)
    return min(hits, key=lambda hit: hit.distance) if hits else None


def pick_region_ids(id_pixels: NDArray[np.integer], *, multi_select: bool) -> tuple[int, ...]:
    values, counts = np.unique(np.asarray(id_pixels), return_counts=True)
    candidates = [(int(count), int(value)) for value, count in zip(values, counts) if value > 0]
    if not candidates:
        return ()
    candidates.sort(reverse=True)
    return tuple(value for _count, value in candidates) if multi_select else (candidates[0][1],)
