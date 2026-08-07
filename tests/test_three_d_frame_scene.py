from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from comic_editor.three_d.frame_scene import (
    apply_blender_camera_state,
    apply_pose_and_shape_state,
)
from comic_editor.three_d.renderer.mesh import (
    MeshData, MeshPrimitive, MorphTarget,
)
from comic_editor.three_d.renderer.scene import (
    SceneCamera, SceneData, SceneNode,
)
from comic_editor.three_d.renderer.projection import ProjectionMode


def _flat(matrix: np.ndarray) -> list[float]:
    return matrix.reshape(16, order="F").tolist()


def test_pose_matrices_become_parent_relative_and_shape_overrides_apply():
    positions = np.array(
        [[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32
    )
    primitive = MeshPrimitive(
        positions, np.array([[0, 1, 2]], dtype=np.uint32),
        morph_targets=(MorphTarget(
            "Smile", np.full((3, 3), 0.25, dtype=np.float32)
        ),),
    )
    mesh = MeshData("mesh", "Mesh", (primitive,), (0.1,))
    root = SceneNode("armature", "Armature", child_ids=("root-bone",))
    root_bone = SceneNode(
        "root-bone", "Root", parent_id="armature", child_ids=("child-bone",)
    )
    child_bone = SceneNode(
        "child-bone", "Child", parent_id="root-bone"
    )
    face = SceneNode("face", "Face", mesh_index=0)
    scene = SceneData(
        nodes={node.node_id: node for node in (root, root_bone, child_bone, face)},
        root_node_ids=("armature", "face"), meshes=(mesh,),
    )
    root_pose = np.identity(4)
    root_pose[0, 3] = 2.0
    child_pose = root_pose.copy()
    child_pose[1, 3] = 3.0
    frame = SimpleNamespace(
        source_state={
            "poses": {"armature": {
                "root-bone": {"matrix": _flat(root_pose)},
                "child-bone": {"matrix": _flat(child_pose)},
            }},
            "shape_keys": {"face": {
                "shape-uuid": {"name": "Smile", "value": 0.25},
            }},
        },
        presentation_overrides={
            "shape_keys": {"face": {
                "shape-uuid": {"value": 0.8},
            }},
        },
        baked_variant_hashes={},
    )

    apply_pose_and_shape_state(scene, frame)

    np.testing.assert_allclose(scene.nodes["root-bone"].local_matrix, root_pose)
    np.testing.assert_allclose(
        scene.nodes["child-bone"].local_matrix,
        np.linalg.inv(root_pose) @ child_pose,
    )
    assert scene.nodes["face"].morph_weights == pytest.approx((0.8,))


def test_baked_fallback_disables_pose_and_shape_playback():
    primitive = MeshPrimitive(
        np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32),
        np.array([[0, 1, 2]], dtype=np.uint32),
        morph_targets=(MorphTarget(
            "Smile", np.ones((3, 3), dtype=np.float32)
        ),),
    )
    scene = SceneData(
        nodes={"face": SceneNode("face", "Face", mesh_index=0)},
        root_node_ids=("face",),
        meshes=(MeshData("mesh", "Mesh", (primitive,), (0.2,)),),
    )
    frame = SimpleNamespace(
        source_state={"shape_keys": {"face": {
            "key": {"name": "Smile", "value": 1.0},
        }}}, presentation_overrides={},
        baked_variant_hashes={"face": "a" * 64},
    )
    apply_pose_and_shape_state(scene, frame)
    assert scene.nodes["face"].morph_weights == ()


def test_active_blender_camera_sets_exact_view_until_navigation_override():
    matrix = np.identity(4)
    matrix[:3, 3] = [4.0, 5.0, 6.0]
    scene = SceneData(
        nodes={"camera-object": SceneNode(
            "camera-object", "Camera", local_matrix=matrix, camera_index=0
        )},
        root_node_ids=("camera-object",),
        cameras=(SceneCamera("camera-data"),),
    )
    frame = SimpleNamespace(
        source_state={"cameras": {"camera-object": {
            "type": "ORTHO", "ortho_scale": 7.5,
            "fov_y_radians": 0.75, "clip_start": 0.2, "clip_end": 500.0,
            "shift_x": 0.125, "shift_y": -0.25, "sensor_fit": "VERTICAL",
        }}},
        presentation_overrides={},
        extensions={"active_camera_id": "camera-object"},
    )

    assert apply_blender_camera_state(scene, frame, {})
    np.testing.assert_allclose(scene.active_camera.position, [4.0, 5.0, 6.0])
    assert scene.projection.mode is ProjectionMode.ORTHOGRAPHIC
    assert scene.projection.ortho_height == pytest.approx(7.5)
    assert scene.projection.shift_x == pytest.approx(0.125)
    assert scene.projection.shift_y == pytest.approx(-0.25)
    assert scene.projection.sensor_fit == "VERTICAL"
    assert scene.cameras[0].shift_x == pytest.approx(0.125)
    assert scene.cameras[0].shift_y == pytest.approx(-0.25)
    assert scene.cameras[0].near == pytest.approx(0.2)
