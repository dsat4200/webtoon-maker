"""Native current source pixels during held transformed Pencil/Brush contacts."""
from dataclasses import replace
from threading import Event
import copy
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.models import BoundGeometry, ChildRef
from comic_editor.core.tools import ToolKind
from comic_editor.render.raster_feedback import feedback_chain
from test_raster_contact_feedback import scene, pixels, ready_paint, native_reference as native_artwork_reference


def native_reference(canvas):
    # Compare the complete presented buffer, including the document boundary
    # that the ordinary presenter always draws after the native artwork.
    from comic_editor.ui.document_presentation import draw_document_border
    image = native_artwork_reference(canvas)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    try:
        draw_document_border(painter, canvas._render_document_state().bounds,
                             canvas.camera_transform(), canvas.size(), owner=canvas)
    finally:
        painter.end()
    return image


def configure_transforms(canvas, selected):
    page = canvas.chapter.layers[selected.parent_layer_id]
    page.children = [ref for ref in page.children if ref.entity_id != selected.object_id]
    layer = canvas.chapter.add_layer(page.layer_id, 'Mapped ink',
        BoundGeometry.rectangle(0, 0, 128, 128), index=1)
    layer.fill_color, layer.border_width = None, 0
    layer.transform_frame = (0., 0., 128., 128.)
    layer.transform_quad = [(3.25, -4.5), (156.85, -4.5), (156.85, 149.1), (3.25, 149.1)]
    layer.children = [ChildRef('object', selected.object_id)]
    selected.parent_layer_id = layer.layer_id
    selected.transform_frame = (0., 0., 256., 256.)
    selected.transform_quad = [(-.25, 20.5), (204.55, 20.5), (204.55, 225.3), (-.25, 225.3)]
    canvas._invalidate_scene_cache()
    return layer


def finish(canvas, tool):
    if tool == ToolKind.BRUSH:
        canvas._finish_paint_brush()
    else:
        canvas._end_stroke()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.BRUSH, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('height', [128, 70000])
def test_transformed_contact_publishes_every_held_position_with_exact_ordered_native_pixels(
        scene, tool, height, wait_scene, monkeypatch, qapp):
    canvas, selected, _front = scene
    configure_transforms(canvas, selected)
    canvas.chapter.height = height
    if tool == ToolKind.RASTER_ERASER:
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id, (0, 0), image)
    canvas.set_tool(tool)
    wait_scene(canvas)
    prepared = canvas._scene_controller.feedback
    assert prepared is not None and prepared.source_transform is not None
    assert prepared.origin == (0, 0)
    assert all(tile.source_keys for tile in prepared.tiles)
    assert prepared.byte_count <= 32 * 1024 * 1024
    native_before = {key: bytes(image.constBits()) for key, image in
        canvas.tiles.object_tiles(selected.object_id).items()}
    history_revision = canvas.command_stack.revision
    gate, entered = Event(), Event()
    scheduler = canvas._scene_controller.scheduler
    original = scheduler._evaluate_admitted
    def blocked(demand, token):
        entered.set()
        assert gate.wait(20), 'test barrier was not released'
        return original(demand, token)
    monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
    before = ready_paint(canvas)
    try:
        if tool == ToolKind.BRUSH:
            canvas._begin_paint_brush(QPointF(32, 64), 1.,
                _definition=BrushDefinition(size=16, antialiasing=0, spacing=.08))
        else:
            canvas._begin_stroke(QPointF(32, 64), 1.)
        first = ready_paint(canvas)
        assert canvas._drawing and canvas._raster_contact_active
        assert first.pixelColor(32, 64) != before.pixelColor(32, 64)
        assert first.pixelColor(32, 64) == QColor('blue' if tool == ToolKind.RASTER_ERASER else 'red')
        np.testing.assert_array_equal(pixels(first), pixels(native_reference(canvas)))
        deadline = time.monotonic() + 3.
        while not entered.is_set() and time.monotonic() < deadline:
            ready_paint(canvas)
            qapp.processEvents()
            time.sleep(.002)
        assert entered.is_set() and scheduler.busy
        previous = first
        for x in (72, 88, 104):
            if tool == ToolKind.BRUSH:
                canvas._continue_paint_brush(QPointF(x, 64), 1.)
            else:
                canvas._continue_stroke(QPointF(x, 64), 1.)
            actual = ready_paint(canvas)
            assert canvas._drawing and canvas._raster_contact_active and not gate.is_set()
            assert canvas._scene_controller.feedback is prepared
            assert actual.pixelColor(x, 64) != previous.pixelColor(x, 64)
            assert actual.pixelColor(x, 64) == QColor('blue' if tool == ToolKind.RASTER_ERASER else 'red')
            assert actual.pixelColor(56, 64) == QColor('green'), 'foreground must retain its scene order'
            assert actual.pixelColor(4, 64) == QColor('#242428'), 'page clipping must remain exact'
            np.testing.assert_array_equal(pixels(actual), pixels(native_reference(canvas)))
            native_current = {key: bytes(image.constBits()) for key, image in
                canvas.tiles.object_tiles(selected.object_id).items()}
            with monkeypatch.context() as patch:
                patch.setattr(canvas, '_render_scene_layers', lambda *_a, **_k: pytest.fail('GUI scene evaluation'))
                patch.setattr(canvas.tiles.residency, 'get', lambda *_a, **_k: pytest.fail('GUI source decode'))
                np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(actual))
            assert native_current == {key: bytes(image.constBits()) for key, image in
                canvas.tiles.object_tiles(selected.object_id).items()}, 'feedback may not mutate source pixels'
            previous = actual
        finish(canvas, tool)
        np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(previous))
        assert canvas.command_stack.revision == history_revision + 1
    finally:
        if canvas._drawing:
            finish(canvas, tool)
        gate.set()
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_reference(canvas)))
    canvas.command_stack.undo()
    assert native_before == {key: bytes(image.constBits()) for key, image in
        canvas.tiles.object_tiles(selected.object_id).items()}


def test_transformed_pruned_native_source_reveals_prefix_without_repainting_the_scene(
        scene, wait_scene, monkeypatch):
    canvas, selected, _front = scene
    configure_transforms(canvas, selected)
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    local = canvas._raster_local_point(selected, QPointF(32, 64))
    painter = QPainter(image)
    painter.fillRect(round(local.x()) - 2, round(local.y()) - 2, 4, 4, QColor('red'))
    painter.end()
    canvas.tiles.set_tile(selected.object_id, (0, 0), image)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    wait_scene(canvas)
    assert ready_paint(canvas).pixelColor(32, 64) == QColor('red')
    canvas._begin_stroke(QPointF(32, 64), 1.)
    canvas._end_stroke()
    assert (0, 0) not in canvas.tiles._tiles[selected.object_id].entries
    expected = native_reference(canvas)
    with monkeypatch.context() as patch:
        patch.setattr(canvas, '_render_scene_layers', lambda *_a, **_k: pytest.fail('GUI scene evaluation'))
        np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(expected))
    assert ready_paint(canvas).pixelColor(32, 64) == QColor('blue')


@pytest.mark.parametrize('case', ['rotated', 'perspective', 'collapsed', 'mirror', 'nan'])
def test_unsupported_mapping_keeps_native_detached_evaluation_required(scene, case, wait_scene):
    canvas, selected, _front = scene
    wait_scene(canvas)
    snapshot = canvas._scene_controller.snapshot
    chapter = copy.copy(snapshot.chapter)
    chapter.objects = dict(chapter.objects)
    obj = chapter.objects[selected.object_id] = copy.copy(chapter.objects[selected.object_id])
    obj.transform_frame = (0., 0., 128., 128.)
    obj.transform_quad = {
        'rotated': [(0., 0.), (128., 1.), (127., 129.), (-1., 128.)],
        'perspective': [(0., 0.), (128., 0.), (127., 128.), (0., 128.)],
        'collapsed': [(0., 0.), (0., 0.), (0., 128.), (0., 128.)],
        'mirror': [(128., 0.), (0., 0.), (0., 128.), (128., 128.)],
        'nan': [(float('nan'), 0.), (128., 0.), (128., 128.), (0., 128.)],
    }[case]
    assert feedback_chain(replace(snapshot, chapter=chapter), selected.object_id) is None
