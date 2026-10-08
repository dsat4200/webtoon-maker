"""Owned rigs keep current native pixels when a detached scene first previews them."""
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from comic_editor.core.commands import CallbackCommand
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, DistortModifier, ImageObject,
    MirrorModifier, RadialBlurModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import (
    DocumentRenderService, RenderQuality, RenderRequest, RenderStatus,
)
from comic_editor.ui.canvas import RasterCanvasWidget, ToolKind
from comic_editor.ui.transform_modifier_preview import effective_preview_modifier, transform_modifier_rig


@pytest.fixture(scope="session")
def owned_rig_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def owned_rig_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("comic_editor.core.settings.settings_path", lambda: tmp_path / "settings.json")


def _close(canvas):
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
    consumers = getattr(canvas, "_scene_consumers", None)
    if consumers is not None:
        consumers.shutdown()
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    for timer in canvas.findChildren(QTimer):
        timer.stop()
    canvas.close()
    canvas.deleteLater()


def _encoded_pattern():
    image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#c0d0e0"))
    painter = QPainter(image)
    for y in range(0, 128, 16):
        for x in range(0, 128, 16):
            if (x // 16 + y // 16) % 2:
                painter.fillRect(x, y, 16, 16, QColor("#ff6c20"))
    painter.fillRect(13, 19, 25, 31, QColor("#143bb5"))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.WriteOnly) and image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def _rig(kind):
    if kind == "deform":
        modifier = DistortModifier(modifier_type="distort_deform", frame=(80, 90, 144, 136),
            center=(152, 158), radius=85,
            points=[(.03, .02), (.96, -.02), (.92, .94), (.05, .97)],
            source_points=[(0., 0.), (1., 0.), (1., 1.), (0., 1.)],
            parameters={"amount": 100., "mode": "rigid", "interpolation": "bilinear", "edges": "transparent"})
    elif kind == "twirl":
        modifier = DistortModifier(modifier_type="distort_twirl", frame=(80, 90, 144, 136),
            center=(152, 158), radius=85,
            parameters={"angle": 140., "interpolation": "bilinear", "edges": "transparent"})
    elif kind == "mirror":
        modifier = MirrorModifier(axis_start=(246., 20.), axis_end=(246., 350.))
    elif kind == "radial":
        modifier = RadialBlurModifier(center=(152., 158.), angle=22.)
    else:
        raise AssertionError("Unknown owned-rig fixture")
    modifier.validate()
    return modifier


def _canvas(chapter, images, settings):
    canvas = RasterCanvasWidget(settings)
    canvas.setUpdatesEnabled(False)
    canvas.resize(384, 384)
    canvas.set_document(chapter, TileStore(), images)
    canvas.center_x = canvas.center_y = 192.
    canvas.scale, canvas.rotation = 1., 0.
    return canvas


def _scene(kind, mode, parent_kind="identity"):
    chapter = ChapterDocument(width=1080, height=512)
    page = chapter.add_page("Native rig", BoundGeometry.rectangle(0, 0, 384, 384))
    page.fill_color, page.border_width = None, 0
    parent = page
    if parent_kind != "identity":
        parent = chapter.add_layer(page.layer_id, "Mapped source parent")
        parent.transform_frame = (0., 0., 384., 384.)
        parent.transform_quad = ([(20., 25.), (404., 40.), (389., 424.), (5., 409.)]
            if parent_kind == "affine" else [(20., 25.), (384., 20.), (395., 390.), (12., 405.)])
    obj = chapter.add_object(parent.layer_id, ImageObject(pixel_width=128, pixel_height=128,
        source_filename="pattern.png", source_mime_type="image/png",
        transform_frame=(0., 0., 128., 128.),
        transform_quad=[(80., 90.), (224., 90.), (224., 226.), (80., 226.)]))
    modifier = _rig(kind)
    images = ImageStore()
    images.put(obj.object_id, "pattern.png", _encoded_pattern(), "image/png")
    settings = EditorSettings(canvas_renderer="raster", snap_to_grid=False,
        grid_overlay_visible=False, transform_mode="free" if mode == "warp" else "uniform")
    canvas = _canvas(chapter, images, settings)
    if parent_kind != "identity":
        mapping = canvas.layer_world_transform(parent.layer_id)
        assert mapping.isAffine() is (parent_kind == "affine")
        transform_modifier_rig(canvas, modifier, mapping)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    canvas.set_selection("object", obj.object_id, activate_default_tool=False)
    assert canvas.selected_object_id == obj.object_id
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.command_stack.push(CallbackCommand("Prior owned-rig command", lambda: None, lambda: None), already_done=True)
    return canvas, obj.object_id, modifier.modifier_id


def _start(canvas, object_id, mode):
    obj = canvas.chapter.objects[object_id]
    quad = canvas.object_world_quad(object_id)
    press = (canvas.layer_world_transform(obj.parent_layer_id).map(QPointF(111., 126.))
        if mode == "translate" else QPointF(*quad[2]))
    expected_mode = "translate" if mode == "translate" else "handle"
    assert canvas._selected_object_transform_hit(obj, quad, press)[0] == expected_mode
    move = press + (QPointF(31., 23.) if mode == "translate" else QPointF(28., 19.))
    canvas._tool_press(canvas.document_to_widget(press), 1.)
    canvas._tool_move(canvas.document_to_widget(move), 1.)
    assert canvas._transform_drag_mode == expected_mode and canvas._geometry_transform_target is None
    assert canvas._transform_preview_quad and canvas._transform_start_quad
    assert canvas._transform_preview_quad != canvas._transform_start_quad
    return press, move


def _pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine()).copy()


def _capture(canvas, *, rig_id=None, expect_live=False, require_cold=False):
    document = canvas._render_document_state()
    assert document.live_preview is expect_live
    capture = canvas._scene_snapshot_compiler.capture(canvas, document)
    while not capture.advance(.004):
        pass
    assert not capture.stale and capture.result.document == document
    backend = DetachedSceneBackend(capture.result)
    backend.native_preview, backend.artwork_scale = True, 1.
    backend.scene._effect_preview_channel = "canvas"
    service = DocumentRenderService(backend)
    service.projection.revision = document.revision
    request = RenderRequest((0., 0., 384., 384.), 1., (384, 384), ("owned-rig-native-proof",),
        document.revision, quality=RenderQuality.INTERACTIVE, defer_effects=False)
    try:
        assert backend.scene._transform_modifier_preview_cache is None
        assert not backend.scene._modifier_render_cache and not backend.scene._modifier_source_cache
        if require_cold:
            assert not backend.scene.images._decoded
        if rig_id is not None:
            stored = backend.scene.chapter.modifiers[rig_id]
            saved = deepcopy(stored.to_dict())
            effective = effective_preview_modifier(backend.scene, stored)
            assert effective is not stored and effective.to_dict() != saved
            cache = backend.scene._transform_modifier_preview_cache
            assert isinstance(cache, tuple) and len(cache) == 4 and rig_id in cache[3]
            assert effective_preview_modifier(backend.scene, stored) is effective
            assert backend.scene.chapter.modifiers[rig_id].to_dict() == saved
            assert "_transform_modifier_preview_cache" not in backend.scene.cache_state()
        result = service.render_region(document, request)
        assert result.status in (RenderStatus.EXACT, RenderStatus.PROVISIONAL) and not result.image.isNull()
        assert result.image.size().width() == result.image.size().height() == 384
        assert result.image.format() == document.pixel_contract.image_format
        assert service.current(document, request) and backend.scene._vector_render_scale_override == 1.
        if require_cold:
            assert backend.scene.images._decoded
        return QImage(result.image)
    finally:
        backend.close()


def _cold_committed_reference(canvas, before, object_id, mode):
    images = canvas.images.clone()
    assert not images._decoded
    reference = _canvas(ChapterDocument.from_dict(deepcopy(before)), images,
        EditorSettings(**asdict(canvas.settings)))
    reference.set_selection("object", object_id, activate_default_tool=False)
    assert reference.selected_object_id == object_id
    reference.set_tool(ToolKind.TRANSFORM)
    try:
        _start(reference, object_id, mode)
        reference._tool_release()
        assert len(reference.command_stack._undo) == 1 and reference._transform_preview_quad is None
        model = deepcopy(reference.chapter.to_dict())
        return _capture(reference, require_cold=True), model
    finally:
        _close(reference)


def _check_owned_rig_pixels_and_history(kind, mode, parent_kind="identity"):
    canvas, object_id, rig_id = _scene(kind, mode, parent_kind)
    try:
        before = deepcopy(canvas.chapter.to_dict())
        source_sha = hashlib.sha256(canvas.images.source(object_id).data).hexdigest()
        prior, revision = tuple(canvas.command_stack._undo), canvas.command_stack.revision
        original = _capture(canvas)
        _start(canvas, object_id, mode)
        expected_quad = deepcopy(canvas._transform_preview_quad)
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        live = _capture(canvas, rig_id=rig_id, expect_live=True)
        assert np.count_nonzero(_pixels(live) != _pixels(original)) > 0
        cold, committed_model = _cold_committed_reference(canvas, before, object_id, mode)
        np.testing.assert_array_equal(_pixels(live), _pixels(cold))
        canvas._tool_release()
        assert canvas.chapter.to_dict() == committed_model and committed_model != before
        assert canvas.chapter.objects[object_id].transform_quad == expected_quad
        assert canvas._transform_preview_quad is None and canvas._transform_drag_mode is None
        own = canvas.command_stack.top_undo_command
        assert own.label == "Transform image" and len(canvas.command_stack._undo) == len(prior) + 1
        assert all(a is b for a, b in zip(prior, canvas.command_stack._undo[:-1]))
        assert canvas.command_stack.revision == revision + 1
        np.testing.assert_array_equal(_pixels(_capture(canvas)), _pixels(cold))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        np.testing.assert_array_equal(_pixels(_capture(canvas)), _pixels(original))
        canvas.command_stack.redo()
        assert canvas.chapter.to_dict() == committed_model and canvas.command_stack.top_undo_command is own
        np.testing.assert_array_equal(_pixels(_capture(canvas)), _pixels(cold))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before
        assert hashlib.sha256(canvas.images.source(object_id).data).hexdigest() == source_sha
    finally:
        _close(canvas)


@pytest.mark.parametrize("kind", ["deform", "twirl", "mirror", "radial"])
@pytest.mark.parametrize("mode", ["translate", "scale", "warp"])
def test_detached_owned_rig_native_preview_equals_cold_commit_and_undo(owned_rig_app, kind, mode):
    _check_owned_rig_pixels_and_history(kind, mode)


@pytest.mark.parametrize("parent_kind", ["affine", "projective"])
@pytest.mark.parametrize("kind", ["deform", "twirl", "mirror", "radial"])
@pytest.mark.parametrize("mode", ["translate", "scale", "warp"])
def test_detached_owned_rig_mapped_parent_native_preview_equals_cold_commit_and_undo(owned_rig_app, parent_kind, kind, mode):
    _check_owned_rig_pixels_and_history(kind, mode, parent_kind)


@pytest.mark.parametrize("parent_kind", ["identity", "affine", "projective"])
def test_live_free_image_bounds_match_committed_geometry_contract(owned_rig_app, parent_kind):
    from comic_editor.core.assets import entity_visual_bounds
    from comic_editor.ui.attached_translation import preview_object_bounds
    from comic_editor.render.scene import EvaluatedScene
    canvas, object_id, _rig_id = _scene("mirror", "warp", parent_kind)
    try:
        _start(canvas, object_id, "warp")
        obj = canvas.chapter.objects[object_id]
        document = canvas._render_document_state()
        capture = canvas._scene_snapshot_compiler.capture(canvas, document)
        while not capture.advance(.004):
            pass
        assert not capture.stale
        committed = ChapterDocument.from_dict(deepcopy(canvas.chapter.to_dict()))
        committed.objects[object_id].transform_quad = deepcopy(canvas._transform_preview_quad)
        cold_snapshot = replace(capture.result, chapter=committed,
            state={**capture.result.state, "_transform_preview_quad": None, "_transform_start_quad": None})
        cold_scene = EvaluatedScene(cold_snapshot)
        # The generic capture begins from the actual world quad; the mirror
        # source begins from the local AABB mapped conservatively by the parent.
        generic = preview_object_bounds(canvas, obj, canvas.object_world_rect(object_id))
        source = preview_object_bounds(canvas, obj,
            entity_visual_bounds(canvas.chapter, canvas.tiles, "object", object_id,
                layer_mapping=canvas.layer_world_transform), local_aabb=True)
        expected_generic = cold_scene.object_world_rect(object_id)
        expected_source = entity_visual_bounds(committed, canvas.tiles, "object", object_id,
            layer_mapping=cold_scene.layer_world_transform)
        np.testing.assert_array_equal(generic.getRect(), expected_generic.getRect())
        np.testing.assert_array_equal(source.getRect(), expected_source.getRect())
        if parent_kind == "projective":
            assert generic != source
        assert canvas.chapter.objects[object_id].transform_quad != committed.objects[object_id].transform_quad
    finally:
        _close(canvas)
