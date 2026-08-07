from __future__ import annotations

import math

import numpy as np
import pytest

from comic_editor.three_d.renderer.camera import CameraState
from comic_editor.three_d.renderer.cubemap import CubeFace, direction_to_face_uv, face_view_matrix, required_cube_faces
from comic_editor.three_d.renderer.gizmo import rotation_delta, scale_delta, trackball_delta, translation_delta
from comic_editor.three_d.renderer.grid import build_floor_grid, build_volume_grid, nice_spacing
from comic_editor.three_d.renderer.offscreen import OffscreenRenderer
from comic_editor.three_d.renderer.picking import WorldRay, pick_scene
from comic_editor.three_d.renderer.primitives import cube_mesh, cylinder_mesh
from comic_editor.three_d.renderer.projection import (
    FisheyeCrop,
    FisheyeMapping,
    ProjectionContext,
    ProjectionMode,
    ProjectionSettings,
)
from comic_editor.three_d.renderer.scene import LightType, SceneData, SceneLight, SceneNode, TransformSpace


@pytest.mark.parametrize("mapping", list(FisheyeMapping))
def test_all_four_fisheye_mappings_round_trip_screen_ray(mapping: FisheyeMapping) -> None:
    settings = ProjectionSettings(
        mode=ProjectionMode.FISHEYE,
        fisheye_mapping=mapping,
        fisheye_crop=FisheyeCrop.CIRCULAR,
        fisheye_fov_deg=170.0,
    )
    context = ProjectionContext((800, 600), 0.01, 100.0)
    for point in (np.array([400.0, 300.0]), np.array([500.0, 300.0]), np.array([350.0, 390.0])):
        ray = settings.screen_to_ray_cpu(point, context)
        assert ray.lens_valid
        projected = settings.project_cpu(ray.direction * 5.0, context)
        assert projected.lens_valid
        np.testing.assert_allclose(projected.screen_px, point, atol=1e-8)


def test_perspective_and_orthographic_center_and_switch_scale() -> None:
    context = ProjectionContext((1600, 900), 0.1, 100.0)
    settings = ProjectionSettings(vertical_fov_deg=60.0)
    projected = settings.project_cpu(np.array([0.0, 0.0, -5.0]), context)
    np.testing.assert_allclose(projected.screen_px, [800.0, 450.0])
    settings.switch_mode(ProjectionMode.ORTHOGRAPHIC, 5.0)
    assert settings.ortho_height == pytest.approx(2.0 * 5.0 * math.tan(math.radians(30.0)))
    ray = settings.screen_to_ray_cpu(np.array([800.0, 450.0]), context)
    np.testing.assert_allclose(ray.origin, 0.0)
    np.testing.assert_allclose(ray.direction, [0.0, 0.0, -1.0])


def test_blender_camera_shift_projection_and_rays_round_trip() -> None:
    context = ProjectionContext((1600, 900), 0.1, 100.0)
    settings = ProjectionSettings(
        vertical_fov_deg=60.0,
        shift_x=0.1,
        shift_y=0.05,
        sensor_fit="HORIZONTAL",
    )
    np.testing.assert_allclose(settings.ndc_shift(context.aspect), [0.2, 0.17777777777777778])
    optical_axis = settings.project_cpu(np.array([0.0, 0.0, -5.0]), context)
    np.testing.assert_allclose(optical_axis.screen_px, [640.0, 530.0])
    ray = settings.screen_to_ray_cpu(optical_axis.screen_px, context)
    np.testing.assert_allclose(ray.origin, 0.0)
    np.testing.assert_allclose(ray.direction, [0.0, 0.0, -1.0], atol=1.0e-12)

    settings.switch_mode(ProjectionMode.ORTHOGRAPHIC, 5.0)
    optical_axis = settings.project_cpu(np.array([0.0, 0.0, -5.0]), context)
    np.testing.assert_allclose(optical_axis.screen_px, [640.0, 530.0])
    ray = settings.screen_to_ray_cpu(optical_axis.screen_px, context)
    np.testing.assert_allclose(ray.origin, 0.0, atol=1.0e-12)
    np.testing.assert_allclose(ray.direction, [0.0, 0.0, -1.0])

    settings.sensor_fit = "VERTICAL"
    np.testing.assert_allclose(
        settings.ndc_shift(context.aspect),
        [0.1125, 0.1],
    )


def test_cubemap_conventions_and_required_rear_face() -> None:
    samples = {
        CubeFace.POSITIVE_X: [1, 0, 0], CubeFace.NEGATIVE_X: [-1, 0, 0],
        CubeFace.POSITIVE_Y: [0, 1, 0], CubeFace.NEGATIVE_Y: [0, -1, 0],
        CubeFace.POSITIVE_Z: [0, 0, 1], CubeFace.NEGATIVE_Z: [0, 0, -1],
    }
    for face, direction in samples.items():
        sample = direction_to_face_uv(np.asarray(direction, dtype=np.float64))
        assert sample.face is face
        np.testing.assert_allclose(sample.uv, [0.5, 0.5])
    settings = ProjectionSettings(mode=ProjectionMode.FISHEYE, fisheye_fov_deg=210.0)
    assert CubeFace.POSITIVE_Z in required_cube_faces(settings)
    assert np.all(np.isfinite(face_view_matrix(CameraState(), CubeFace.NEGATIVE_Z)))


def test_exact_hierarchy_matrices_preserve_shear_and_negative_scale() -> None:
    parent_matrix = np.array(
        [[-2.0, 0.25, 0.0, 4.0], [0.0, 3.0, 0.0, 5.0], [0.0, 0.0, 0.5, 6.0], [0.0, 0.0, 0.0, 1.0]]
    )
    child_matrix = np.array(
        [[1.0, 0.0, 0.1, 1.0], [0.0, 1.0, 0.0, 2.0], [0.0, 0.0, 1.0, 3.0], [0.0, 0.0, 0.0, 1.0]]
    )
    parent = SceneNode("parent", "Parent", parent_matrix, child_ids=("child",))
    child = SceneNode("child", "Child", child_matrix, parent_id="parent", mesh_index=0)
    scene = SceneData(nodes={"parent": parent, "child": child}, root_node_ids=("parent",), meshes=(cube_mesh(),))
    np.testing.assert_array_equal(scene.nodes["parent"].local_matrix, parent_matrix)
    np.testing.assert_allclose(scene.nodes["child"].world_matrix, parent_matrix @ child_matrix)
    assert scene.nodes["child"].determinant < 0.0

    desired_world = scene.nodes["child"].world_matrix.copy()
    desired_world[0, 3] += 7.0
    scene.set_world_matrix("child", desired_world)
    np.testing.assert_allclose(scene.nodes["child"].world_matrix, desired_world, atol=2e-15)
    scene.apply_transform("child", translation_delta(np.array([0.0, 1.0, 0.0]), 2.0), TransformSpace.GLOBAL)
    assert scene.nodes["child"].world_matrix[1, 3] == pytest.approx(desired_world[1, 3] + 2.0)


def test_gizmo_and_trackball_deltas_are_finite_invertible_matrices() -> None:
    for matrix in (
        rotation_delta(np.array([0.0, 1.0, 0.0]), 0.7),
        scale_delta(None, -2.0),
        trackball_delta(np.array([-0.2, 0.1]), np.array([0.3, 0.5])),
    ):
        assert matrix.shape == (4, 4)
        assert np.all(np.isfinite(matrix))
        assert abs(np.linalg.det(matrix[:3, :3])) > 1e-8


def test_primitives_grid_and_triangle_picking() -> None:
    cube = cube_mesh(size=(2.0, 4.0, 6.0))
    cylinder = cylinder_mesh(radius=2.0, height=3.0, segments=12)
    np.testing.assert_allclose(cube.bounds[0], [-1.0, -2.0, -3.0])
    np.testing.assert_allclose(cube.bounds[1], [1.0, 2.0, 3.0])
    assert cylinder.triangle_count == 48
    assert nice_spacing(20.0) == 1.0
    assert build_floor_grid(20.0).vertices.shape[1] == 3
    volume = build_volume_grid(20.0)
    assert volume.vertices.shape == volume.colors.shape[:1] + (3,)
    assert volume.colors.shape[1] == 4
    assert len(volume.vertices) > len(build_floor_grid(20.0).vertices)

    node = SceneNode("cube", "Cube", mesh_index=0)
    scene = SceneData(nodes={"cube": node}, root_node_ids=("cube",), meshes=(cube,))
    hit = pick_scene(scene, WorldRay(np.array([0.0, 0.0, 10.0]), np.array([0.0, 0.0, -1.0])))
    assert hit is not None
    assert hit.node_id == "cube"
    assert hit.point[2] == pytest.approx(3.0)
    np.testing.assert_allclose(hit.normal, [0.0, 0.0, 1.0], atol=1e-12)


@pytest.mark.parametrize("light_type", [LightType.SPOT, LightType.RECTANGLE])
def test_spot_and_rectangle_shadow_views_follow_light_negative_z(light_type: LightType) -> None:
    matrix = np.array(
        [
            [0.0, 0.0, -1.0, 2.0],
            [0.0, 1.0, 0.0, 3.0],
            [1.0, 0.0, 0.0, 4.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    node = SceneNode(
        "light", "Light", matrix, world_matrix=matrix, light_index=0
    )
    light = SceneLight("light-data", light_type=light_type)
    view = OffscreenRenderer._shadow_view_matrix(
        node,
        light,
        np.array([-20.0, 8.0, -11.0]),
        5.0,
    )
    origin = view @ np.array([2.0, 3.0, 4.0, 1.0])
    along_negative_z = view @ np.array([3.0, 3.0, 4.0, 1.0])
    np.testing.assert_allclose(origin[:3], 0.0, atol=1.0e-12)
    np.testing.assert_allclose(along_negative_z[:3], [0.0, 0.0, -1.0], atol=1.0e-12)


def test_point_shadow_view_is_rejected_with_explicit_warning() -> None:
    node = SceneNode("point", "Point", light_index=0)
    light = SceneLight("point-data", "Fill", LightType.POINT)
    with pytest.raises(ValueError, match="omnidirectional"):
        OffscreenRenderer._shadow_view_matrix(node, light, np.zeros(3), 2.0)
    assert "shadow casting is disabled" in OffscreenRenderer._point_shadow_warning(light)
