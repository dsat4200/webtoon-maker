from __future__ import annotations

import copy
import math
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, ChapterDocument
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.three_d.documents import (
    BlenderChapterDocument, DrawingMaterial3D,
)
from comic_editor.three_d.repository import BlenderSidecarData
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.three_d import ThreeDToolKind, ThreeDViewportController
from comic_editor.ui.tree_model import HierarchyModel
from comic_editor.three_d.renderer.primitives import cube_mesh
from comic_editor.three_d.renderer.scene import SceneData, SceneNode


class FakeRenderService(QObject):
    result_ready = Signal(object)
    render_failed = Signal(int, str)

    available = True
    reason = ""

    def __init__(self) -> None:
        super().__init__()
        self.requests = []

    def submit(self, request) -> int:
        self.requests.append(request)
        return int(request.generation_id)

    def shutdown(self) -> None:
        pass


class UnavailableRenderService(FakeRenderService):
    available = False
    reason = "OpenGL 3.3 unavailable"


def build_three_d_document(*, with_catalog: bool = False):
    chapter = ChapterDocument(height=420)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, chapter.width, 400)
    )
    ordinary = chapter.add_layer(
        page.layer_id, "2D Layer",
        BoundGeometry.rectangle(10, 10, 100, 100),
    )
    blender = chapter.add_blender_layer(
        page.layer_id, "Linked View",
        BoundGeometry.circle(360, 190, 120),
    )
    document = BlenderChapterDocument(chapter_id=chapter.chapter_id)
    if with_catalog:
        document.collection_catalog = {
            "collection": {"name": "Characters"},
        }
        document.object_catalog = {
            "cube": {
                "name": "Cube", "type": "mesh",
                "collection_ids": ["collection"],
            },
        }
    sidecar = BlenderSidecarData(document)
    sidecar.create_frame(
        frame_id=blender.comic_frame_id,
        included_collection_ids=["collection"] if with_catalog else [],
    )
    return chapter, page, ordinary, blender, sidecar


def solid_image(width: int, height: int, color: str) -> QImage:
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    return image


def test_monotonic_sidecar_restore_never_rewinds_blender_source_watermark(qapp):
    chapter, _page, _ordinary, _blender, sidecar = build_three_d_document()
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    sidecar.document.revision = 8
    sidecar.document.source_revision = 5
    sidecar.document.extensions["accepted_source_digest"] = "a" * 64
    older = copy.deepcopy(sidecar)
    older.document.revision = 3
    older.document.source_revision = 2
    older.document.extensions["accepted_source_digest"] = "b" * 64

    controller.replace_sidecar(older, monotonic=True)

    assert controller.sidecar.document.revision == 9
    assert controller.sidecar.document.source_revision == 5
    assert controller.sidecar.document.extensions[
        "accepted_source_digest"
    ] == "a" * 64
    controller.shutdown()


def test_main_window_exposes_exact_six_3d_ribbon_pages_and_aa_defaults_off(qapp):
    chapter, _page, ordinary, blender, sidecar = build_three_d_document()
    window = MainWindow()
    fake = FakeRenderService()
    window.three_d_controller._render_service = fake
    window._set_chapter(chapter, TileStore(), blender_sidecar=sidecar)

    window.canvas.set_selection(
        "layer", blender.layer_id, activate_default_tool=False
    )

    expected = {
        "three_d_view": "View",
        "three_d_rendering": "Rendering",
        "three_d_outline": "Outline Settings",
        "three_d_materials": "Materials",
        "three_d_object": "Object Properties",
        "three_d_tool_settings": "Tool Settings",
    }
    assert window.ribbon.page_keys(visible_only=True) == list(expected)
    assert {
        key: window.ribbon.page(key).title for key in expected
    } == expected
    assert window.three_d_outline_page.groups() == []
    assert not window.three_d_antialiasing.isChecked()
    assert not window.three_d_volume_grid.isChecked()
    assert not window.three_d_multi_select.isChecked()

    window.canvas.set_selection(
        "layer", ordinary.layer_id, activate_default_tool=False
    )
    assert not any(
        window.ribbon.is_page_visible(key) for key in expected
    )
    window.close()
    window.deleteLater()


def test_selecting_3d_layer_resets_then_exactly_restores_2d_camera(qapp):
    chapter, _page, ordinary, blender, sidecar = build_three_d_document()
    service = FakeRenderService()
    controller = ThreeDViewportController(render_service=service)
    controller.set_documents(chapter, sidecar)
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(900, 620)
    canvas.set_three_d_controller(controller)
    canvas.set_document(chapter, TileStore())
    original = (173.25, 829.75, 1.375, -27.5)
    canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = original

    canvas.set_selection(
        "layer", blender.layer_id, activate_default_tool=False
    )

    assert canvas.in_three_d_mode
    assert canvas.rotation == 0
    assert (canvas.center_x, canvas.center_y) == pytest.approx((360, 190))
    assert canvas.scale != original[2]
    assert controller.active_layer_id == blender.layer_id

    canvas.set_selection(
        "layer", ordinary.layer_id, activate_default_tool=False
    )
    assert not canvas.in_three_d_mode
    assert (
        canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation
    ) == original
    controller.shutdown()
    canvas.deleteLater()


def test_virtual_blender_rows_are_locked_but_owner_layer_renames_and_reorders(qapp):
    chapter, page, ordinary, blender, sidecar = build_three_d_document(
        with_catalog=True
    )
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    model = HierarchyModel(chapter)
    model.set_blender_hierarchy(controller.virtual_hierarchy())

    layer_index = model.index_for_entity("layer", blender.layer_id)
    blender_root = model.index(0, 0, layer_index)
    collection = model.index(0, 0, blender_root)
    cube = model.index(0, 0, collection)

    layer_flags = model.flags(layer_index)
    assert layer_flags & Qt.ItemIsEditable
    assert layer_flags & Qt.ItemIsDragEnabled
    assert not layer_flags & Qt.ItemIsDropEnabled
    for virtual_index in (blender_root, collection, cube):
        flags = model.flags(virtual_index)
        assert flags & Qt.ItemIsSelectable
        assert not flags & Qt.ItemIsEditable
        assert not flags & Qt.ItemIsDragEnabled
        assert not flags & Qt.ItemIsDropEnabled
        assert not model.setData(
            virtual_index, "Cannot rename", Qt.EditRole
        )
        assert not model.mimeData([virtual_index]).hasFormat(model.MIME)

    assert model.setData(layer_index, "Renamed 3D View", Qt.EditRole)
    assert chapter.layers[blender.layer_id].name == "Renamed 3D View"

    layer_mime = model.mimeData([layer_index])
    assert not model.canDropMimeData(
        layer_mime, Qt.MoveAction, 0, 0, collection
    )
    page_index = model.index_for_entity("layer", page.layer_id)
    assert model.dropMimeData(
        layer_mime, Qt.MoveAction, 0, 0, page_index
    )
    assert page.children[0].entity_id == blender.layer_id
    assert page.children[1].entity_id == ordinary.layer_id


def test_blender_image_is_shape_clipped_stacked_and_has_default_outline(qapp):
    chapter = ChapterDocument(height=300)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, chapter.width, 300)
    )
    overlay = chapter.add_layer(
        page.layer_id, "Overlay",
        BoundGeometry.rectangle(185, 135, 30, 30),
    )
    overlay.fill_color = "#FF00FF00"
    blender = chapter.add_blender_layer(
        page.layer_id, "3D",
        BoundGeometry.circle(200, 150, 100),
    )
    backdrop = chapter.add_layer(
        page.layer_id, "Backdrop",
        BoundGeometry.rectangle(0, 0, chapter.width, 300),
    )
    backdrop.fill_color = "#FFFF0000"
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_cached_image(
        blender.layer_id, solid_image(160, 120, "#FF0000FF")
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_three_d_controller(controller)
    canvas.set_document(chapter, TileStore())
    result = solid_image(chapter.width, chapter.height, "#00000000")

    canvas.render_preview(result)

    # The rectangular render target is clipped to the ellipse.
    assert result.pixelColor(105, 55).red() > 220
    assert result.pixelColor(120, 150).blue() > 220
    # The first outliner row is frontmost and remains above the 3D image.
    center = result.pixelColor(200, 150)
    assert center.green() > 220 and center.red() < 40 and center.blue() < 40
    # The independent 4 px black boundary is drawn over the rendered image.
    outline = result.pixelColor(102, 150)
    assert outline.red() < 60 and outline.green() < 60 and outline.blue() < 60
    assert blender.border_width == 4


def test_offline_placeholder_is_bounded_by_the_arbitrary_shape(qapp):
    chapter = ChapterDocument(height=300)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, chapter.width, 300)
    )
    blender = chapter.add_blender_layer(
        page.layer_id, "Offline 3D",
        BoundGeometry.circle(200, 150, 100),
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    result = solid_image(chapter.width, chapter.height, "#00000000")

    canvas.render_preview(result)

    inside = result.pixelColor(200, 150)
    outside = result.pixelColor(105, 55)
    assert inside.lightness() < 100
    assert outside.lightness() > 220
    assert result.pixelColor(102, 150).lightness() < 60
    assert blender.bound.primitive == "ellipse"


def test_controller_discards_stale_render_generations(qapp):
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    service = FakeRenderService()
    controller = ThreeDViewportController(render_service=service)
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id, (320, 240))
    first = service.requests[-1]
    controller.request_render((400, 300))
    latest = service.requests[-1]
    assert latest.generation_id > first.generation_id

    old_image = solid_image(4, 4, "#FFFF0000")
    new_image = solid_image(4, 4, "#FF0000FF")
    controller._accept_result(SimpleNamespace(
        request=first, generation_id=first.generation_id,
        image=old_image, error=None,
    ))
    assert controller.image_for_layer(blender.layer_id) is None

    controller._accept_result(SimpleNamespace(
        request=latest, generation_id=latest.generation_id,
        image=new_image, error=None,
    ))
    accepted = controller.image_for_layer(blender.layer_id)
    assert accepted is not None
    assert accepted.pixelColor(1, 1).blue() > 220
    assert not latest.antialiasing

    controller.request_render((420, 310))
    unavailable = service.requests[-1]
    controller._accept_result(SimpleNamespace(
        request=unavailable, generation_id=unavailable.generation_id,
        image=solid_image(4, 4, "#FFFF0000"),
        error="simulated context loss", available=False,
    ))
    retained = controller.image_for_layer(blender.layer_id)
    assert retained is not None
    assert retained.pixelColor(1, 1).blue() > 220


def test_transform_components_round_trip_signed_scale_and_residual_shear(qapp):
    del qapp
    matrix = np.array([
        [-1.8, 0.35, 0.12, 4.0],
        [0.2, 2.4, -0.18, -3.0],
        [0.1, 0.22, 0.65, 8.0],
        [0.0, 0.0, 0.0, 1.0],
    ], dtype=np.float64)
    translation, rotation, scale, residual = (
        ThreeDViewportController._matrix_components(matrix)
    )
    restored = ThreeDViewportController._compose_matrix(
        translation, rotation, scale, residual
    )
    np.testing.assert_allclose(restored, matrix, atol=1e-10)


@pytest.mark.parametrize("tool, kind", [
    (ThreeDToolKind.DRAW_CUBE, "cube"),
    (ThreeDToolKind.DRAW_CYLINDER, "cylinder"),
])
def test_primitive_tools_place_surface_aligned_entities(qapp, tool, kind):
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id, (320, 240))
    scene = SceneData(
        nodes={"source": SceneNode("source", "Source", mesh_index=0)},
        root_node_ids=("source",), meshes=(cube_mesh(),),
    )
    scene.active_camera.frame_bounds(
        *scene.bounds(), 320 / 240, 50.0, True
    )
    controller._active_scene = scene
    controller.set_tool(tool)
    center = QPointF(160.0, 120.0)

    assert controller.pointer_press(center, Qt.LeftButton, Qt.NoModifier)
    assert controller.pointer_release(center, Qt.LeftButton, Qt.NoModifier)

    local = sidecar.frames[blender.comic_frame_id].local_entities[-1]
    assert local["type"] == kind
    matrix = np.asarray(
        local["transform"]["matrix"], dtype=np.float64
    ).reshape((4, 4), order="F")
    assert np.all(np.isfinite(matrix))
    assert np.linalg.norm(matrix[:3, 3]) > 0.5


@pytest.mark.parametrize("light_type", ["sun", "point", "rectangle", "spot"])
def test_add_light_supports_all_four_frame_local_types(qapp, light_type):
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id)
    controller.set_pending_light_type(light_type)

    assert controller.pointer_press(
        QPointF(20, 20), Qt.LeftButton, Qt.NoModifier
    )
    assert controller.pointer_release(
        QPointF(20, 20), Qt.LeftButton, Qt.NoModifier
    )
    entity = sidecar.frames[blender.comic_frame_id].local_entities[-1]
    assert entity["type"] == light_type


def test_local_primitive_and_light_properties_are_single_undoable_edits(qapp):
    del qapp
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id)
    events = []
    controller.frameEditCommitted.connect(
        lambda frame_id, before, after, label:
        events.append((frame_id, before, after, label))
    )

    cube_id = controller.add_local_entity("cube", size=[1.0, 1.0, 1.0])
    events.clear()
    before_revision = controller._frame().revision
    assert controller.set_selected_entity_properties({
        "size_x": 2.0, "size_y": 3.0, "size_z": 4.0,
    })
    assert len(events) == 1
    assert events[0][3] == "Set 3D object properties"
    assert controller._frame().revision == before_revision + 1
    cube = next(
        item for item in controller._frame().local_entities
        if item["id"] == cube_id
    )
    assert cube["parameters"]["size"] == [2.0, 3.0, 4.0]
    controller.apply_frame_payload(events[0][0], events[0][1], monotonic=False)
    assert controller.selected_entity_properties()["properties"] == {
        "size_x": 1.0, "size_y": 1.0, "size_z": 1.0,
    }

    light_id = controller.add_local_entity(
        "point", color="#FFFFFFFF", energy=1000.0, range=10.0,
    )
    events.clear()
    assert controller.set_selected_entity_properties({
        "light_type": "spot", "color": "#FF4080FF",
        "energy": 250.0, "range": 18.0,
        "area_width": 2.0, "area_height": 3.0,
        "spot_angle": 36.0, "casts_shadow": False,
    })
    assert len(events) == 1
    light = next(
        item for item in controller._frame().local_entities
        if item["id"] == light_id
    )
    assert light["type"] == "spot"
    assert light["parameters"] == {
        "color": "#FF4080FF", "energy": 250.0, "range": 18.0,
        "size": [2.0, 3.0], "spot_size": 36.0,
        "casts_shadow": False,
    }
    controller.apply_frame_payload(events[0][0], events[0][1], monotonic=False)
    restored = controller.selected_entity_properties()
    assert restored["properties"]["light_type"] == "point"
    assert restored["properties"]["energy"] == 1000.0


def test_source_light_and_camera_properties_use_sparse_overrides_and_undo(qapp):
    del qapp
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    sidecar.document.object_catalog = {
        "source-light": {"name": "Key", "type": "LIGHT"},
        "source-camera": {"name": "Shot", "type": "CAMERA"},
    }
    frame = sidecar.frames[blender.comic_frame_id]
    frame.source_state.update({
        "lights": {"source-light": {
            "type": "POINT", "color": [1.0, 1.0, 1.0],
            "energy": 1000.0, "use_custom_distance": False,
            "cutoff_distance": 25.0, "use_shadow": True,
        }},
        "cameras": {"source-camera": {
            "type": "PERSP", "fov_y_radians": math.radians(50.0),
            "ortho_scale": 10.0, "clip_start": 0.1,
            "clip_end": 1000.0,
        }},
    })
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id)
    events = []
    controller.frameEditCommitted.connect(
        lambda frame_id, before, after, label:
        events.append((frame_id, before, after, label))
    )

    controller.selected_entity_ids = {"source-light"}
    assert controller.set_selected_entity_properties({
        "light_type": "rectangle", "color": "#FFFF8000",
        "energy": 500.0, "range": 12.0,
        "area_width": 4.0, "area_height": 2.0,
        "spot_angle": 30.0, "casts_shadow": False,
    })
    assert len(events) == 1
    override = frame.presentation_overrides["lights"]["source-light"]
    assert override["type"] == "AREA"
    assert override["use_custom_distance"] is True
    assert override["cutoff_distance"] == 12.0
    assert override["size"] == 4.0 and override["size_y"] == 2.0
    assert override["use_shadow"] is False
    controller.apply_frame_payload(events[0][0], events[0][1], monotonic=False)
    assert "lights" not in controller._frame().presentation_overrides

    controller.selected_entity_ids = {"source-camera"}
    events.clear()
    assert controller.set_selected_entity_properties({
        "camera_type": "orthographic", "fov": 35.0,
        "ortho_scale": 7.5, "clip_start": 0.25, "clip_end": 800.0,
    })
    assert len(events) == 1
    override = controller._frame().presentation_overrides[
        "cameras"
    ]["source-camera"]
    assert override["type"] == "ORTHO"
    assert override["fov_y_radians"] == pytest.approx(math.radians(35.0))
    assert override["ortho_scale"] == 7.5
    assert override["clip_start"] == 0.25
    assert override["clip_end"] == 800.0
    controller.apply_frame_payload(events[0][0], events[0][1], monotonic=False)
    assert "cameras" not in controller._frame().presentation_overrides


def test_object_properties_page_edits_selected_local_primitive_with_undo(qapp):
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    window = MainWindow()
    window.three_d_controller._render_service = FakeRenderService()
    window._set_chapter(chapter, TileStore(), blender_sidecar=sidecar)
    window.canvas.set_selection(
        "layer", blender.layer_id, activate_default_tool=False
    )
    entity_id = window.three_d_controller.add_local_entity(
        "cylinder", radius=0.5, depth=1.0, vertices=32,
    )
    window._three_d_selection_changed({entity_id})
    assert not window.three_d_entity_properties_widget.isHidden()
    assert not window.three_d_cylinder_radius.isHidden()
    assert window.three_d_light_energy.isHidden()

    command_count = len(window.canvas.command_stack._undo)
    window.three_d_cylinder_radius.setValue(1.75)
    window.three_d_cylinder_depth.setValue(6.0)
    window.three_d_cylinder_segments.setValue(48)
    window._edit_three_d_entity_properties()
    assert len(window.canvas.command_stack._undo) == command_count + 1
    entity = next(
        item for item in sidecar.frames[
            blender.comic_frame_id
        ].local_entities if item["id"] == entity_id
    )
    assert entity["parameters"]["radius"] == 1.75
    assert entity["parameters"]["depth"] == 6.0
    assert entity["parameters"]["vertices"] == 48
    window.canvas.command_stack.undo()
    restored = window.three_d_controller.selected_entity_properties()
    assert restored["properties"]["radius"] == 0.5
    assert restored["properties"]["depth"] == 1.0
    assert restored["properties"]["segments"] == 32

    window._dirty = False
    window.close()
    window.deleteLater()


def test_selection_claims_input_and_ctrl_wins_over_shift_in_multi_select(qapp):
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id)
    controller.set_tool(ThreeDToolKind.SELECT_RECT)

    assert not controller.pointer_press(
        QPointF(0, 0), Qt.RightButton, Qt.NoModifier
    )
    assert controller.pointer_press(
        QPointF(0, 0), Qt.LeftButton, Qt.NoModifier
    )
    assert controller.pointer_release(
        QPointF(1, 1), Qt.LeftButton, Qt.ShiftModifier,
        hit_ids=["front", "back"],
    )
    assert controller.selected_entity_ids == {"front"}
    assert controller.cursor_mode(Qt.ShiftModifier) == "select"

    controller.set_multi_select(True)
    assert controller.cursor_mode(Qt.ShiftModifier) == "add"
    assert controller.cursor_mode(Qt.ControlModifier) == "remove"
    assert controller.cursor_mode(
        Qt.ShiftModifier | Qt.ControlModifier
    ) == "remove"

    controller.selected_entity_ids = {"front"}
    assert controller.pointer_press(
        QPointF(0, 0), Qt.LeftButton, Qt.NoModifier
    )
    controller.pointer_release(
        QPointF(10, 10), Qt.LeftButton, Qt.ShiftModifier,
        hit_ids=["back"],
    )
    assert controller.selected_entity_ids == {"front", "back"}

    assert controller.pointer_press(
        QPointF(0, 0), Qt.LeftButton, Qt.NoModifier
    )
    controller.pointer_release(
        QPointF(10, 10), Qt.LeftButton,
        Qt.ShiftModifier | Qt.ControlModifier,
        hit_ids=["front"],
    )
    assert controller.selected_entity_ids == {"back"}
    assert controller.pointer_press(
        QPointF(0, 0), Qt.MiddleButton, Qt.NoModifier
    )
    assert controller.pointer_release(
        QPointF(0, 0), Qt.MiddleButton, Qt.NoModifier
    )


def test_region_selection_uses_visible_pixels_when_object_origin_is_outside(qapp):
    del qapp
    from comic_editor.three_d.renderer.id_buffer import rasterize_scene_ids

    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id, (160, 120))
    scene = SceneData(
        nodes={"mesh": SceneNode("mesh", "Mesh", mesh_index=0)},
        root_node_ids=("mesh",), meshes=(cube_mesh(),),
    )
    scene.active_camera.target = np.zeros(3)
    scene.active_camera.distance = 5.0
    scene.active_camera.orientation = np.array([1.0, 0.0, 0.0, 0.0])
    controller._active_scene = scene
    pixels = rasterize_scene_ids(scene, (160, 120)).ids
    ys, xs = np.where(pixels > 0)
    origin = np.array([80.0, 60.0])
    index = int(np.argmax((xs - origin[0]) ** 2 + (ys - origin[1]) ** 2))
    point = QPointF(float(xs[index]), float(ys[index]))
    start = QPointF(point.x() - 4.0, point.y() - 4.0)
    end = QPointF(point.x() + 4.0, point.y() + 4.0)
    assert not (
        min(start.x(), end.x()) <= origin[0] <= max(start.x(), end.x())
        and min(start.y(), end.y()) <= origin[1] <= max(start.y(), end.y())
    )
    controller.set_tool(ThreeDToolKind.SELECT_RECT)
    assert controller.pointer_press(start, Qt.LeftButton, Qt.NoModifier)
    assert controller.pointer_move(end, Qt.NoModifier)
    assert controller.pointer_release(end, Qt.LeftButton, Qt.NoModifier)
    assert controller.selected_entity_ids == {"mesh"}


def test_collection_visibility_toggle_uses_collection_override(qapp):
    del qapp
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document(
        with_catalog=True
    )
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id)
    controller.set_virtual_visibility(blender.layer_id, "collection", False)
    frame = sidecar.frames[blender.comic_frame_id]
    assert frame.presentation_overrides["collection_visibility"] == {
        "collection": False
    }
    assert "collection" not in frame.presentation_overrides.get(
        "visibility", {}
    )
    assert not frame.collection_visible("collection")
    assert not controller.virtual_hierarchy()[blender.layer_id][0]["visible"]


def test_no_gl_mode_keeps_placeholder_view_but_rejects_3d_edits(qapp):
    del qapp
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    controller = ThreeDViewportController(
        render_service=UnavailableRenderService()
    )
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id)
    frame = sidecar.frames[blender.comic_frame_id]
    before = frame.to_dict()
    assert not controller.pointer_press(
        QPointF(20, 20), Qt.LeftButton, Qt.NoModifier
    )
    assert controller.add_local_entity("cube") == ""
    controller.set_renderer_setting("grid_visible", False)
    assert frame.to_dict() == before


def test_projected_global_rotate_handle_preserves_object_origin(qapp):
    del qapp
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    frame = sidecar.frames[blender.comic_frame_id]
    local = np.identity(4)
    local[:3, 3] = [2.0, 0.0, 0.0]
    frame.source_state["transforms"] = {
        "mesh": {"matrix_local": local.reshape(16, order="F").tolist()}
    }
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id, (320, 240))
    scene = SceneData(
        nodes={
            "mesh": SceneNode(
                "mesh", "Mesh", local_matrix=local, mesh_index=0
            )
        },
        root_node_ids=("mesh",), meshes=(cube_mesh(),),
    )
    scene.active_camera.target = np.array([1.0, 0.0, 0.0])
    scene.active_camera.distance = 8.0
    scene.recompute_world_matrices()
    controller._active_scene = scene
    controller.selected_entity_ids = {"mesh"}
    controller.set_transform_settings(space="global", mode="rotate")
    geometry = controller.gizmo_geometry()
    assert geometry is not None
    endpoint = geometry["axes"]["x"]
    center = geometry["center"]
    direction = endpoint - center
    tangent = QPointF(-direction.y(), direction.x())
    tangent_length = math.hypot(tangent.x(), tangent.y())
    movement = QPointF(
        tangent.x() / tangent_length * 18.0,
        tangent.y() / tangent_length * 18.0,
    )
    assert controller.pointer_press(endpoint, Qt.LeftButton, Qt.NoModifier)
    assert controller.pointer_move(endpoint + movement, Qt.NoModifier)
    assert controller.pointer_release(
        endpoint + movement, Qt.LeftButton, Qt.NoModifier
    )
    result = np.asarray(
        frame.presentation_overrides["transforms"]["mesh"]["matrix_local"],
        dtype=np.float64,
    ).reshape((4, 4), order="F")
    np.testing.assert_allclose(result[:3, 3], [2.0, 0.0, 0.0], atol=1e-10)
    assert not np.allclose(result[:3, :3], np.identity(3))


def test_source_camera_seeds_exact_navigation_and_ortho_wheel_changes_scale(qapp):
    del qapp
    from comic_editor.three_d.renderer.camera import quaternion_from_axis_angle
    from comic_editor.three_d.renderer.projection import ProjectionMode

    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    scene = SceneData()
    scene.active_camera.target = np.array([3.0, 4.0, 5.0])
    scene.active_camera.distance = 2.5
    scene.active_camera.orientation = quaternion_from_axis_angle(
        np.array([0.0, 0.0, 1.0]), 0.4
    )
    scene.projection.mode = ProjectionMode.ORTHOGRAPHIC
    scene.projection.ortho_height = 12.0
    controller = ThreeDViewportController(
        render_service=FakeRenderService(),
        scene_provider=lambda _layer, _frame, _sidecar: scene,
    )
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id, (320, 240))
    np.testing.assert_allclose(
        controller.navigation.orientation, scene.active_camera.orientation
    )
    assert controller.navigation.target == pytest.approx((3.0, 4.0, 5.0))
    distance = controller.navigation.distance
    assert controller.wheel(120, Qt.NoModifier)
    frame = sidecar.frames[blender.comic_frame_id]
    assert frame.presentation_overrides["renderer_settings"][
        "ortho_height"
    ] < 12.0
    assert controller.navigation.distance == pytest.approx(distance)


def test_touch_style_pan_and_pinch_commit_one_camera_undo(qapp):
    del qapp
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id)
    events = []
    controller.frameEditCommitted.connect(
        lambda frame_id, before, after, label:
        events.append((frame_id, before, after, label))
    )
    assert controller.pointer_press(
        QPointF(50, 50), Qt.MiddleButton, Qt.NoModifier
    )
    assert controller.pointer_move(QPointF(60, 55), Qt.NoModifier)
    assert controller.wheel(40, Qt.NoModifier, commit=False)
    assert controller.wheel(40, Qt.NoModifier, commit=False)
    assert not events
    assert controller.pointer_release(
        QPointF(60, 55), Qt.MiddleButton, Qt.NoModifier
    )
    assert len(events) == 1
    assert events[0][3] == "Navigate 3D camera"


def test_boundary_edit_reselects_owner_and_returns_to_3d_after_commit(qapp):
    del qapp
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    canvas = CanvasWidget(EditorSettings())
    canvas.set_three_d_controller(controller)
    canvas.set_document(chapter, TileStore())
    assert canvas.set_blender_virtual_selection(
        blender.layer_id, "virtual:mesh", "mesh"
    )
    assert canvas.begin_blender_boundary_edit()
    assert canvas.selected_kind == "layer"
    assert canvas.selected_id == blender.layer_id
    assert canvas._three_d_boundary_edit
    assert not canvas.in_three_d_mode
    canvas._model_before = chapter.to_dict()
    canvas._tool_release()
    assert not canvas._three_d_boundary_edit
    assert canvas.in_three_d_mode


def test_read_only_metadata_includes_catalog_and_captured_source_state(qapp):
    del qapp
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document(
        with_catalog=True
    )
    frame = sidecar.frames[blender.comic_frame_id]
    frame.source_state["transforms"] = {
        "cube": {"matrix_local": np.identity(4).reshape(
            16, order="F"
        ).tolist()}
    }
    frame.source_state["visibility"] = {
        "cube": {"visible": True, "hide_render": False}
    }
    controller = ThreeDViewportController(render_service=FakeRenderService())
    controller.set_documents(chapter, sidecar)
    assert controller.activate(blender.layer_id)
    controller.selected_entity_ids = {"cube"}
    metadata = controller.selected_entity_metadata()
    assert metadata["ownership"] == "Blender"
    assert metadata["catalog"]["name"] == "Cube"
    assert set(metadata["captured"]) == {"transforms", "visibility"}


def test_local_component_rotation_and_explicit_material_are_interpreted(qapp):
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document()
    frame = sidecar.frames[blender.comic_frame_id]
    frame.local_entities.append({
        "id": "local-cube", "name": "Local Cube", "type": "cube",
        "visible": True,
        "transform": {
            "translation": [1.0, 2.0, 3.0],
            "rotation": [0.0, 0.0, 90.0],
            "scale": [2.0, 1.0, 1.0],
        },
        "parameters": {"size": [1.0, 1.0, 1.0]},
    })
    window = MainWindow()
    window.three_d_controller._render_service = FakeRenderService()
    window._set_chapter(chapter, TileStore(), blender_sidecar=sidecar)
    window.active_session = SimpleNamespace(
        kind="series", context=SimpleNamespace(repository=None)
    )
    scene = window._three_d_scene_for_frame(
        blender.layer_id, frame, sidecar
    )
    node = scene.nodes["local-cube"]
    np.testing.assert_allclose(node.local_matrix[:3, 3], [1.0, 2.0, 3.0])
    np.testing.assert_allclose(
        node.local_matrix[:3, :3],
        [[0.0, -1.0, 0.0], [2.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
        atol=1.0e-10,
    )
    primitive = scene.meshes[node.mesh_index].primitives[0]
    material = scene.source_materials[primitive.material_index]
    assert material.material_id == "webtoon:local-material:local-cube"
    window._dirty = False
    window.active_session = None
    window.close()
    window.deleteLater()


def _window_with_material_sidecar(qapp):
    chapter, _page, _ordinary, blender, sidecar = build_three_d_document(
        with_catalog=True
    )
    sidecar.document.material_catalog = {
        "source-ink": {"name": "Blender Ink"},
        "source-paper": {"name": "Blender Paper"},
    }
    sidecar.document.extensions["source_material_assignments"] = {
        "cube": ["source-ink", None, "source-paper"],
    }
    sidecar.document.drawing_materials = [
        DrawingMaterial3D(material_id="drawing-ink", name="Comic Ink"),
        DrawingMaterial3D(material_id="drawing-paper", name="Comic Paper"),
    ]
    sidecar.document.material_mappings = {
        "source-paper": "drawing-paper",
    }
    window = MainWindow()
    window.three_d_controller._render_service = FakeRenderService()
    window._set_chapter(chapter, TileStore(), blender_sidecar=sidecar)
    # The regular application supplies this through its project-tab session.
    window.active_session = SimpleNamespace(
        kind="series", context=None, blender_sidecar=sidecar, dirty=False,
    )
    window.canvas.set_selection(
        "layer", blender.layer_id, activate_default_tool=False
    )
    window._refresh_three_d_materials()
    return window, sidecar


def _material_mapping_row(window, source_id: str) -> int:
    table = window.three_d_material_mapping_table
    for row in range(table.rowCount()):
        if table.item(row, 0).data(Qt.ItemDataRole.UserRole) == source_id:
            return row
    raise AssertionError(f"Missing material mapping row for {source_id}")


def test_material_table_maps_blender_assignments_as_one_undo_command(qapp):
    window, sidecar = _window_with_material_sidecar(qapp)
    table = window.three_d_material_mapping_table
    assert table.rowCount() == 2
    ink_row = _material_mapping_row(window, "source-ink")
    assert table.item(ink_row, 0).text() == "Blender Ink"
    assert "Cube [slot 1]" in table.item(ink_row, 1).text()
    ink_mapping = table.cellWidget(ink_row, 2)
    assert ink_mapping.currentData() == ""

    command_count = len(window.canvas.command_stack._undo)
    ink_mapping.setCurrentIndex(ink_mapping.findData("drawing-ink"))

    assert len(window.canvas.command_stack._undo) == command_count + 1
    assert sidecar.document.material_mappings["source-ink"] == "drawing-ink"
    drawing_ink = next(
        material for material in sidecar.document.drawing_materials
        if material.material_id == "drawing-ink"
    )
    assert drawing_ink.source_material_ids == ["source-ink"]
    window.canvas.command_stack.undo()
    assert "source-ink" not in sidecar.document.material_mappings

    window._dirty = False
    window.active_session.dirty = False
    window.close()
    window.deleteLater()


def test_drawing_material_editor_exposes_full_renderer_contract(qapp):
    window, sidecar = _window_with_material_sidecar(qapp)
    window.three_d_material_list.setCurrentRow(0)
    controls = (
        window.three_d_material_shader,
        window.three_d_material_tint,
        window.three_d_material_toon_ramp,
        window.three_d_material_outline,
        window.three_d_material_outline_color,
        window.three_d_material_outline_width,
    )
    for control in controls:
        control.blockSignals(True)
    window.three_d_material_shader.setCurrentIndex(
        window.three_d_material_shader.findData("toon")
    )
    window.three_d_material_tint.setText("#80402010")
    window.three_d_material_toon_ramp.setText(
        "0:#FF000000, 0.3:#FF00FF00, 1:#FFFFFFFF"
    )
    window.three_d_material_outline.setChecked(False)
    window.three_d_material_outline_color.setText("#FF102030")
    window.three_d_material_outline_width.setValue(2.75)
    for control in controls:
        control.blockSignals(False)

    command_count = len(window.canvas.command_stack._undo)
    window._edit_three_d_material()

    assert len(window.canvas.command_stack._undo) == command_count + 1
    material = sidecar.document.drawing_materials[0]
    assert material.shader == "toon"
    assert material.tint == "#80402010"
    assert material.toon_ramp == [
        (0.0, "#FF000000"), (0.3, "#FF00FF00"),
        (1.0, "#FFFFFFFF"),
    ]
    assert not material.outline_enabled
    assert material.outline_color == "#FF102030"
    assert material.outline_width == pytest.approx(2.75)
    window.canvas.command_stack.undo()
    restored = sidecar.document.drawing_materials[0]
    assert restored.shader == "diffuse"
    assert restored.tint == "#FFFFFFFF"
    assert restored.outline_enabled

    window._dirty = False
    window.active_session.dirty = False
    window.close()
    window.deleteLater()
