"""CPU ID/depth buffer used for deterministic rectangle and lasso selection."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .projection import clip_planes
from .scene import SceneData


@dataclass(frozen=True, slots=True)
class SceneIdBuffer:
    ids: NDArray[np.uint32]
    depth: NDArray[np.float64]
    id_to_node: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.ids.shape != self.depth.shape or self.ids.ndim != 2:
            raise ValueError("ID and depth buffers must be matching 2D arrays")


def rasterize_scene_ids(
    scene: SceneData, size: tuple[int, int],
) -> SceneIdBuffer:
    """Rasterize visible mesh nodes with a stable integer ID and depth.

    This is a selection pass, not a color render.  It follows every supported
    projection (including fisheye), exact node matrices, and morph weights.  A
    bounded CPU pass keeps selection available even when Qt and ModernGL cannot
    share a context; the color renderer remains on its dedicated worker.
    """

    width, height = int(size[0]), int(size[1])
    if (
        width <= 0 or height <= 0 or width > 4096 or height > 4096
        or width * height > 4_194_304
    ):
        raise ValueError("selection ID buffer exceeds its 4-megapixel limit")
    ids = np.zeros((height, width), dtype=np.uint32)
    depths = np.full((height, width), np.inf, dtype=np.float64)
    low, high = scene.bounds()
    radius = max(0.001, float(np.linalg.norm(high - low)) * 0.5)
    near, far = clip_planes(scene.active_camera.distance, radius)
    context = scene.projection.context((width, height), near, far)
    view = scene.active_camera.view_matrix()
    nodes = tuple(sorted(scene.visible_nodes(), key=lambda node: node.node_id))
    id_to_node = ("", *(node.node_id for node in nodes))

    for integer_id, node in enumerate(nodes, 1):
        mesh = scene.meshes[node.mesh_index]  # type: ignore[index]
        weights = node.morph_weights or mesh.default_morph_weights
        model_view = view @ node.world_matrix
        for primitive in mesh.primitives:
            positions = primitive.evaluated_positions(weights).astype(np.float64)
            if (
                node.skin_index is not None
                and primitive.joints is not None
                and primitive.weights is not None
            ):
                skin = scene.skins[node.skin_index]
                inverse_node = np.linalg.pinv(node.world_matrix)
                joint_matrices = np.asarray([
                    inverse_node @ scene.nodes[joint_id].world_matrix @ bind
                    for joint_id, bind in zip(
                        skin.joint_node_ids, skin.inverse_bind_matrices
                    )
                ])
                homogeneous = np.column_stack((
                    positions, np.ones(len(positions), dtype=np.float64)
                ))
                deformed = np.zeros((len(positions), 4), dtype=np.float64)
                for influence in range(primitive.joints.shape[1]):
                    matrices = joint_matrices[
                        primitive.joints[:, influence]
                    ]
                    deformed += (
                        np.einsum("nij,nj->ni", matrices, homogeneous)
                        * primitive.weights[:, influence, None]
                    )
                positions = deformed[:, :3]
            camera = (
                model_view
                @ np.column_stack((positions, np.ones(len(positions)))).T
            ).T[:, :3]
            projected = [
                scene.projection.project_cpu(point, context)
                for point in camera
            ]
            screen = np.asarray(
                [value.screen_px for value in projected], dtype=np.float64
            )
            vertex_depth = np.asarray(
                [value.camera_depth for value in projected], dtype=np.float64
            )
            valid = np.asarray(
                [value.lens_valid for value in projected], dtype=bool
            )
            for triangle in primitive.indices:
                a, b, c = (int(value) for value in triangle)
                if not (valid[a] and valid[b] and valid[c]):
                    continue
                points = screen[[a, b, c]]
                minimum = np.floor(points.min(axis=0)).astype(int)
                maximum = np.ceil(points.max(axis=0)).astype(int)
                left = max(0, int(minimum[0]))
                top = max(0, int(minimum[1]))
                right = min(width - 1, int(maximum[0]))
                bottom = min(height - 1, int(maximum[1]))
                if left > right or top > bottom:
                    continue
                p0, p1, p2 = points
                denominator = (
                    (p1[1] - p2[1]) * (p0[0] - p2[0])
                    + (p2[0] - p1[0]) * (p0[1] - p2[1])
                )
                if abs(float(denominator)) <= 1.0e-12:
                    continue
                xs, ys = np.meshgrid(
                    np.arange(left, right + 1, dtype=np.float64) + 0.5,
                    np.arange(top, bottom + 1, dtype=np.float64) + 0.5,
                )
                w0 = (
                    (p1[1] - p2[1]) * (xs - p2[0])
                    + (p2[0] - p1[0]) * (ys - p2[1])
                ) / denominator
                w1 = (
                    (p2[1] - p0[1]) * (xs - p2[0])
                    + (p0[0] - p2[0]) * (ys - p2[1])
                ) / denominator
                w2 = 1.0 - w0 - w1
                inside = (
                    (w0 >= -1.0e-8) & (w1 >= -1.0e-8)
                    & (w2 >= -1.0e-8)
                )
                depth = (
                    w0 * vertex_depth[a]
                    + w1 * vertex_depth[b]
                    + w2 * vertex_depth[c]
                )
                target_depth = depths[top:bottom + 1, left:right + 1]
                update = inside & (depth >= near) & (depth <= far) & (
                    depth < target_depth
                )
                target_depth[update] = depth[update]
                target_ids = ids[top:bottom + 1, left:right + 1]
                target_ids[update] = integer_id
    return SceneIdBuffer(ids, depths, id_to_node)


def _polygon_mask(
    shape: tuple[int, int], polygon: NDArray[np.float64],
) -> NDArray[np.bool_]:
    height, width = shape
    result = np.zeros(shape, dtype=bool)
    if polygon.ndim != 2 or polygon.shape[0] < 3 or polygon.shape[1] != 2:
        return result
    minimum = np.floor(polygon.min(axis=0)).astype(int)
    maximum = np.ceil(polygon.max(axis=0)).astype(int)
    left, top = max(0, minimum[0]), max(0, minimum[1])
    right, bottom = min(width - 1, maximum[0]), min(height - 1, maximum[1])
    if left > right or top > bottom:
        return result
    xs, ys = np.meshgrid(
        np.arange(left, right + 1, dtype=np.float64) + 0.5,
        np.arange(top, bottom + 1, dtype=np.float64) + 0.5,
    )
    inside = np.zeros_like(xs, dtype=bool)
    previous = polygon[-1]
    for current in polygon:
        crosses = (current[1] > ys) != (previous[1] > ys)
        denominator = previous[1] - current[1]
        intersection = (
            (previous[0] - current[0]) * (ys - current[1])
            / (denominator if abs(float(denominator)) > 1e-12 else 1e-12)
            + current[0]
        )
        inside ^= crosses & (xs < intersection)
        previous = current
    result[top:bottom + 1, left:right + 1] = inside
    return result


def select_region_ids(
    buffer: SceneIdBuffer,
    polygon: NDArray[np.float64],
    *, multi_select: bool,
) -> tuple[str, ...]:
    """Resolve visible pixels inside a rectangle/lasso, nearest first."""

    mask = _polygon_mask(buffer.ids.shape, np.asarray(polygon, dtype=np.float64))
    values = np.unique(buffer.ids[mask])
    ranked: list[tuple[float, str]] = []
    for raw_id in values:
        integer_id = int(raw_id)
        if integer_id <= 0 or integer_id >= len(buffer.id_to_node):
            continue
        pixels = mask & (buffer.ids == integer_id)
        ranked.append((
            float(np.min(buffer.depth[pixels], initial=np.inf)),
            buffer.id_to_node[integer_id],
        ))
    ranked.sort()
    if not multi_select:
        ranked = ranked[:1]
    return tuple(node_id for _depth, node_id in ranked)


__all__ = ["SceneIdBuffer", "rasterize_scene_ids", "select_region_ids"]
