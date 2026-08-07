from __future__ import annotations

import numpy as np

from comic_editor.three_d.renderer.id_buffer import (
    rasterize_scene_ids, select_region_ids,
)
from comic_editor.three_d.renderer.primitives import cube_mesh
from comic_editor.three_d.renderer.scene import SceneData, SceneNode


def _scene() -> SceneData:
    front_matrix = np.identity(4)
    front_matrix[:3, 3] = [0.0, 0.0, 1.0]
    rear_matrix = np.identity(4)
    rear_matrix[:3, 3] = [0.8, 0.0, 0.0]
    scene = SceneData(
        nodes={
            "front": SceneNode(
                "front", "Front", local_matrix=front_matrix, mesh_index=0
            ),
            "rear": SceneNode(
                "rear", "Rear", local_matrix=rear_matrix, mesh_index=0
            ),
        },
        root_node_ids=("front", "rear"), meshes=(cube_mesh(),),
    )
    scene.active_camera.target = np.zeros(3)
    scene.active_camera.distance = 5.0
    scene.active_camera.orientation = np.array([1.0, 0.0, 0.0, 0.0])
    return scene


def test_id_buffer_keeps_frontmost_pixels_and_region_can_find_partial_objects():
    result = rasterize_scene_ids(_scene(), (160, 120))
    center_id = int(result.ids[60, 80])
    assert result.id_to_node[center_id] == "front"
    polygon = np.array([[65, 42], [125, 42], [125, 78], [65, 78]])
    selected = select_region_ids(result, polygon, multi_select=True)
    assert selected[0] == "front"
    assert set(selected) == {"front", "rear"}


def test_single_region_selection_returns_nearest_visible_id():
    result = rasterize_scene_ids(_scene(), (160, 120))
    polygon = np.array([[50, 35], [130, 35], [130, 85], [50, 85]])
    assert select_region_ids(
        result, polygon, multi_select=False
    ) == ("front",)
