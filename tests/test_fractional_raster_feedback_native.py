"""Native live pixels for Free rasters placed by fractional parent translations."""
from dataclasses import replace
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.ui.themes import theme_color
from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.models import BoundGeometry, ChildRef
from comic_editor.core.tools import ToolKind
from comic_editor.render.outputs import capture_document
from comic_editor.render.raster_feedback import feedback_chain
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, TileBatchPolicy
from comic_editor.ui.document_presentation import (
    PresentedTile, draw_document_border, draw_document_tiles,
)
from test_raster_contact_feedback import (
    scene, pixels, ready_paint, _queue_blocked_native_scene,
)


def configure_fractional_scene(canvas, selected, front, placement, format):
    canvas.chapter.width, canvas.chapter.height = 768, 70000
    canvas.setFixedSize(768, 128)
    canvas.center_x, canvas.center_y, canvas.scale = 384., 50064., 1.
    page = canvas.chapter.layers[selected.parent_layer_id]
    page.translate_y = 50000.
    page.bound = BoundGeometry.rectangle(8.25, 4.25, 751.5, 118.)
    page.children = [ref for ref in page.children if ref.entity_id != selected.object_id]
    layer = canvas.chapter.add_layer(page.layer_id, 'Fractional parent',
        BoundGeometry.rectangle(-15.25, -2.25, 798.5, 136.), index=1)
    layer.fill_color, layer.border_width = None, 0
    layer.children = [ChildRef('object', selected.object_id)]
    selected.parent_layer_id = layer.layer_id
    if placement == 'object':
        selected.x, selected.y = -.75, .3125
    else:
        layer.translate_x, layer.translate_y = -.75, .3125
        if placement == 'cancelling':
            selected.x, selected.y = .75, -.3125
    selected.opacity = .43
    selected.opacity_locked = False
    assert canvas.chapter.effective_object_opacity(selected.object_id) == .43
    # Default Free mode has no authored quad. Every ancestor also stays plain.
    assert selected.transform_frame is None and selected.transform_quad is None
    assert page.transform_quad is None and layer.transform_quad is None
    colors = (QColor.fromRgbF(.0713, .2817, .7891, .3527),
              QColor.fromRgbF(.7831, .3173, .0719, .7243),
              QColor.fromRgbF(.0371, .7139, .8293, .4527))
    for x, color in enumerate(colors):
        image = QImage(256, 256, format)
        image.fill(color)
        painter = QPainter(image)
        try:
            # Distinct one-pixel alpha/color edges make wrong tile clearance,
            # sampling phase, and neighboring gutter reuse visible in bytes.
            for column in (0, 1, 254, 255):
                painter.fillRect(column, 0, 1, 128, colors[(x + column) % 3])
            painter.setCompositionMode(QPainter.CompositionMode_Source)
            painter.fillRect(252, 30, 4, 4, Qt.transparent)
        finally:
            painter.end()
        canvas.tiles.set_tile(selected.object_id, (x, 0), image)
    back = canvas.chapter.objects[page.children[-1].entity_id]
    for x in range(3):
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('blue'))
        canvas.tiles.set_tile(back.object_id, (x, 0), image)
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.fillRect(36, 20, 12, 90, QColor('green'))
    painter.end()
    empty = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    empty.fill(Qt.transparent)
    canvas.tiles.set_tile(front.object_id, (0, 0), empty)
    canvas.tiles.set_tile(front.object_id, (1, 0), image)
    canvas.primary_color = '#67e22caf'
    canvas.settings.pencil_size_px = dict.fromkeys(('small', 'medium', 'large'), 16)
    canvas.settings.eraser_size_px = dict.fromkeys(('small', 'medium', 'large'), 16)
    canvas._invalidate_scene_cache()


def fixed_native_reference(canvas):
    """Fresh ordinary 4x4 native blocks, independent of feedback plane edits."""
    snapshot = capture_document(canvas.chapter, canvas.tiles, canvas.images)
    backend = DetachedSceneBackend(snapshot)
    service = DocumentRenderService(backend)
    service.projection.revision = snapshot.document.revision
    visible = canvas.visible_document_rect().intersected(snapshot.document.bounds)
    requests = tuple(service.projection.requests(visible, 1.))
    try:
        result = service.render_tiles(snapshot.document, requests,
            TileBatchPolicy(visible.center().toTuple()))
        assert not result.error and not result.pending and not result.yielded
        assert all(exact for _image, exact in result.tiles.values())
        batch = [PresentedTile((None, request.address), result.tiles[request.address][0],
            request.world_rect, request.source_rect, snapshot.document.pixel_contract,
            snapshot.pixel_environment) for request in requests]
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(theme_color('canvas', '#242428'))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        try:
            draw_document_tiles(painter, batch, canvas.camera_transform(), canvas.size(), owner=canvas)
            draw_document_border(painter, snapshot.document.bounds, canvas.camera_transform(), canvas.size(), owner=canvas)
        finally:
            painter.end()
        return image
    finally:
        backend.close()


@pytest.mark.parametrize('placement', ['object', 'ancestor', 'cancelling'])
def test_plain_free_fractional_placement_has_native_feedback_lane(scene, placement):
    canvas, selected, front = scene
    configure_fractional_scene(canvas, selected, front, placement,
        QImage.Format_ARGB32_Premultiplied)
    snapshot = capture_document(canvas.chapter, canvas.tiles, canvas.images)
    eligible = feedback_chain(snapshot, selected.object_id)
    assert eligible is not None
    assert eligible[2] == ((0, 50000) if placement == 'cancelling' else (0, 0))
    # Nonfinite positions still decline instead of coercing a bad native grid.
    chapter = snapshot.chapter
    selected_snapshot = chapter.objects[selected.object_id]
    import copy
    chapter = copy.copy(chapter)
    chapter.objects = dict(chapter.objects)
    chapter.objects[selected.object_id] = copy.copy(selected_snapshot)
    chapter.objects[selected.object_id].x = float('nan')
    assert feedback_chain(replace(snapshot, chapter=chapter), selected.object_id) is None


@pytest.mark.parametrize('placement', ['object', 'ancestor'])
@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied, QImage.Format_RGBA64_Premultiplied])
@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_fractional_plain_source_matches_full_native_buffers_during_held_contact(
        scene, placement, format, tool, wait_scene, monkeypatch):
    canvas, selected, front = scene
    configure_fractional_scene(canvas, selected, front, placement, format)
    canvas.set_tool(tool)
    wait_scene(canvas)
    prepared = canvas._scene_controller.feedback
    assert prepared is not None and prepared.source_transform is not None
    assert prepared.origin == (0, 0)
    assert prepared.source_transform[:4] == (1., 0., 0., 1.)
    original = {key: QImage(image) for key, image in canvas.tiles.object_tiles(selected.object_id).items()}
    history = canvas.command_stack.revision
    before = ready_paint(canvas)
    np.testing.assert_array_equal(pixels(before), pixels(fixed_native_reference(canvas)))
    gate, entered = Event(), Event()
    scheduler = canvas._scene_controller.scheduler
    evaluate = scheduler._evaluate_admitted
    def blocked(demand, token):
        entered.set()
        assert gate.wait(20), 'test barrier was not released'
        return evaluate(demand, token)
    monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
    try:
        _queue_blocked_native_scene(canvas)
        assert entered.wait(3.)
        previous = before
        with monkeypatch.context() as guarded:
            guarded.setattr(canvas._scene_snapshot_compiler, 'capture',
                lambda *_a, **_k: pytest.fail('Warm contact recaptured the scene'))
            for index, x in enumerate((240., 272., 328., 520.)):
                point = QPointF(x, 50064.)
                if index == 0:
                    if tool == ToolKind.BRUSH:
                        canvas._begin_paint_brush(point, 1., _definition=BrushDefinition(
                            size=16, antialiasing=0, spacing=.08))
                    else:
                        canvas._begin_stroke(point, 1.)
                elif tool == ToolKind.BRUSH:
                    canvas._continue_paint_brush(point, 1.)
                else:
                    canvas._continue_stroke(point, 1.)
                actual = ready_paint(canvas)
                assert canvas._drawing and canvas._raster_contact_active and scheduler.busy
                assert canvas._scene_controller.feedback is prepared and not gate.is_set()
                assert actual.pixelColor(int(x), 64) != previous.pixelColor(int(x), 64)
                assert actual.pixelColor(296, 64) == QColor('green')
                assert canvas._raster_feedback_contact_covered and not canvas._raster_feedback_pending_visible
                np.testing.assert_array_equal(pixels(actual), pixels(fixed_native_reference(canvas)))
                with monkeypatch.context() as presentation:
                    presentation.setattr(canvas, '_render_scene_layers', lambda *_a, **_k: pytest.fail('GUI scene traversal'))
                    presentation.setattr(canvas.tiles.residency, 'get', lambda *_a, **_k: pytest.fail('GUI native source decode'))
                    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(actual))
                for key, image in canvas.tiles.object_tiles(selected.object_id).items():
                    assert image.format() == format
                    assert bytes(image.constBits())[128 * image.bytesPerLine():] == bytes(original[key].constBits())[128 * original[key].bytesPerLine():]
                previous = actual
        (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
        assert canvas.command_stack.revision == history + 1
        np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(previous))
    finally:
        if canvas._drawing:
            (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
        gate.set()
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(fixed_native_reference(canvas)))
    canvas.command_stack.undo()
    assert original == canvas.tiles.object_tiles(selected.object_id)
