from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("moderngl")

from PySide6.QtGui import QImage

from comic_editor.three_d.renderer.camera import CameraState, quaternion_identity
from comic_editor.three_d.renderer.materials import DrawingMaterial, SurfaceMaterial
from comic_editor.three_d.renderer.mesh import MeshData, MeshPrimitive, SourceMaterial
from comic_editor.three_d.renderer.offscreen import OffscreenRenderer, RenderOptions, RendererUnavailable
from comic_editor.three_d.renderer.primitives import cube_mesh
from comic_editor.three_d.renderer.projection import FisheyeMapping, ProjectionMode
from comic_editor.three_d.renderer.scene import LightType, SceneData, SceneLight, SceneNode


def _renderer() -> OffscreenRenderer:
    try:
        return OffscreenRenderer()
    except RendererUnavailable as error:
        pytest.skip(str(error))


def _scene() -> SceneData:
    light_matrix = np.identity(4)
    light_matrix[:3, 3] = (3.0, 4.0, 5.0)
    scene = SceneData(
        nodes={
            "cube": SceneNode("cube", "Cube", mesh_index=0),
            "light": SceneNode("light", "Light", light_matrix, light_index=0),
        },
        root_node_ids=("cube", "light"),
        meshes=(cube_mesh(),),
        source_materials=(SourceMaterial("default"),),
        lights=(SceneLight("light-data", light_type=LightType.SUN, casts_shadow=False),),
    )
    scene.shadows.enabled = False
    scene.active_camera.frame_bounds(*scene.bounds(), 1.0, 50.0, True)
    return scene


def _triangle_scene(
    *,
    mirrored: bool = False,
    reversed_winding: bool = False,
    double_sided: bool = False,
    outline: bool = False,
) -> SceneData:
    positions = np.array(
        [[-0.9, -0.7, 0.0], [0.9, -0.7, 0.0], [0.0, 0.9, 0.0]],
        dtype=np.float32,
    )
    indices = np.array([[0, 2, 1] if reversed_winding else [0, 1, 2]], dtype=np.uint32)
    mesh = MeshData("triangle-mesh", "Triangle", (MeshPrimitive(positions, indices),))
    transform = np.identity(4)
    if mirrored:
        transform[0, 0] = -1.0
    source = SourceMaterial(
        "triangle-source",
        base_color_factor=np.array([1.0, 0.1, 0.05, 1.0], dtype=np.float32),
        double_sided=double_sided,
    )
    drawing = DrawingMaterial(
        "triangle-drawing",
        surface=SurfaceMaterial.UNSHADED,
        outline_enabled=outline,
        outline_color=(0.0, 1.0, 0.0, 1.0),
        outline_thickness_px=8.0,
    )
    scene = SceneData(
        nodes={"triangle": SceneNode("triangle", "Triangle", transform, mesh_index=0)},
        root_node_ids=("triangle",),
        meshes=(mesh,),
        source_materials=(source,),
        drawing_materials={drawing.material_id: drawing},
        material_mappings={source.material_id: drawing.material_id},
        active_camera=CameraState(distance=3.0, orientation=quaternion_identity()),
    )
    scene.shadows.enabled = False
    return scene


def _center_color(image: QImage):
    return image.pixelColor(image.width() // 2, image.height() // 2)


def _alpha_centroid_x(image: QImage) -> float:
    weighted = 0.0
    total = 0.0
    for y in range(image.height()):
        for x in range(image.width()):
            alpha = image.pixelColor(x, y).alpha()
            weighted += x * alpha
            total += alpha
    assert total > 0.0
    return weighted / total


def test_offscreen_perspective_and_fisheye_return_premultiplied_images() -> None:
    renderer = _renderer()
    try:
        scene = _scene()
        perspective = renderer.render(scene, (64, 64), RenderOptions(draw_floor=False, draw_grid=False, draw_axes=False))
        assert perspective.size().toTuple() == (64, 64)
        assert perspective.format() == QImage.Format.Format_RGBA8888_Premultiplied
        assert renderer.last_metrics.triangles == 12
        assert renderer.last_metrics.draw_submit_ms >= 0.0
        assert renderer.last_metrics.readback_ms >= 0.0
        assert (
            renderer.last_metrics.draw_submit_ms
            + renderer.last_metrics.readback_ms
        ) == pytest.approx(renderer.last_metrics.elapsed_ms, abs=0.2)

        scene.projection.mode = ProjectionMode.FISHEYE
        scene.projection.fisheye_mapping = FisheyeMapping.EQUISOLID_ANGLE
        fisheye = renderer.render(scene, (64, 48), RenderOptions(antialiasing=True, draw_floor=False, draw_grid=False, draw_axes=False))
        assert fisheye.size().toTuple() == (64, 48)
        assert renderer.last_metrics.used_cubemap
    finally:
        renderer.release()


def test_negative_determinant_keeps_front_surface_and_winding() -> None:
    renderer = _renderer()
    try:
        image = renderer.render(
            _triangle_scene(mirrored=True),
            (96, 96),
            RenderOptions(draw_floor=False, draw_grid=False, draw_axes=False),
        )
        center = _center_color(image)
        assert center.alpha() > 240
        assert center.red() > 220
        assert center.green() < 80
    finally:
        renderer.release()


def test_double_sided_material_disables_only_base_surface_culling() -> None:
    renderer = _renderer()
    options = RenderOptions(draw_floor=False, draw_grid=False, draw_axes=False)
    try:
        culled = renderer.render(
            _triangle_scene(reversed_winding=True),
            (96, 96),
            options,
        )
        visible = renderer.render(
            _triangle_scene(reversed_winding=True, double_sided=True),
            (96, 96),
            options,
        )
        assert _center_color(culled).alpha() == 0
        assert _center_color(visible).alpha() > 240

        # A front-facing, double-sided surface must still cull the expanded
        # front hull during the silhouette pass.  If outline culling leaked
        # off, the green hull would cover the red base at the center.
        outlined = renderer.render(
            _triangle_scene(double_sided=True, outline=True),
            (96, 96),
            options,
        )
        center = _center_color(outlined)
        assert center.red() > 220
        assert center.green() < 80
    finally:
        renderer.release()


def test_camera_shift_moves_gpu_projection_with_cpu_convention() -> None:
    renderer = _renderer()
    options = RenderOptions(draw_floor=False, draw_grid=False, draw_axes=False)
    try:
        centered_scene = _triangle_scene()
        centered = renderer.render(centered_scene, (96, 96), options)
        shifted_scene = _triangle_scene()
        shifted_scene.projection.shift_x = 0.2
        shifted = renderer.render(shifted_scene, (96, 96), options)
        assert _alpha_centroid_x(shifted) < _alpha_centroid_x(centered) - 12.0
    finally:
        renderer.release()


def test_volume_grid_renders_and_point_shadows_are_explicitly_disabled() -> None:
    renderer = _renderer()
    try:
        volume_scene = SceneData()
        volume_scene.shadows.enabled = False
        image = renderer.render(
            volume_scene,
            (96, 96),
            RenderOptions(
                draw_floor=False,
                draw_grid=False,
                draw_volume_grid=True,
                draw_axes=False,
            ),
        )
        assert renderer.last_metrics.draw_calls == 1
        assert any(
            image.pixelColor(x, y).alpha() > 0
            for y in range(image.height())
            for x in range(image.width())
        )

        point_scene = _scene()
        point_scene.lights = (
            SceneLight(
                "point-data",
                "Fill",
                LightType.POINT,
                casts_shadow=True,
            ),
        )
        point_scene.shadows.enabled = True
        point_scene.shadows.resolution = 256
        renderer.render(
            point_scene,
            (64, 64),
            RenderOptions(draw_floor=False, draw_grid=False, draw_axes=False),
        )
        assert renderer.last_metrics.shadow_casters == 0
        assert len(renderer.last_warnings) == 1
        assert "omnidirectional shadows" in renderer.last_warnings[0]
        assert renderer.last_warnings[0] in point_scene.warnings
    finally:
        renderer.release()
