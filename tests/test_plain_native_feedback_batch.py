"""Guarded plain contact uses one coherent native presentation batch."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.ui.raster_feedback import replace_raster_feedback_batch
from comic_editor.ui.document_presentation import draw_document_tiles, draw_document_border
from test_raster_contact_feedback import scene, ready_paint, pixels
from test_raster_contact_feedback_boundaries import begin, finish
from test_raster_feedback_bootstrap import drain_native
from test_fractional_raster_feedback_native import fixed_native_reference
from comic_editor.ui.canvas import ToolKind


def setup_contact(scene, wait_scene, qapp, tool):
    canvas, selected, _front = scene
    canvas.chapter.width = 768
    canvas.setFixedSize(768, 128)
    canvas.center_x, canvas.center_y, canvas.scale = 384., 64., .57
    for x in range(3):
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor.fromRgbF(.1837, .5139, .8271, .7213))
        canvas.tiles.set_tile(selected.object_id, (x, 0), image)
    canvas.set_tool(tool)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    ready_paint(canvas)
    begin(canvas, tool, QPointF(224., 64.))
    drain_native(canvas, qapp)
    document = canvas._render_document_state()
    visible = canvas.visible_document_rect().intersected(document.bounds)
    requests = tuple(canvas._document_projection.requests(visible, 1.))
    return canvas, selected, document, requests, canvas._projection_completed_view[1]


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_owned_complete_native_batch_matches_fresh_source_and_refreshes_context(
        scene, wait_scene, qapp, tool):
    canvas, selected, document, requests, base = setup_contact(scene, wait_scene, qapp, tool)
    original = [(tile.image.cacheKey(), bytes(tile.image.constBits())) for _phase, tiles in base for tile in tiles]
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#242428'))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        batch, count, context = replace_raster_feedback_batch(canvas, painter, document, requests, base)
        assert count == len(requests) and context[0] == 'raster-contact'
        draw_document_tiles(painter, batch[0][1], canvas.camera_transform(), canvas.size(), owner=canvas,
            native_context=context, native_bounds=document.bounds)
        draw_document_border(painter, document.bounds, canvas.camera_transform(), canvas.size(), owner=canvas)
    finally:
        painter.end()
    np.testing.assert_array_equal(pixels(image), pixels(fixed_native_reference(canvas)))
    assert [(tile.image.cacheKey(), bytes(tile.image.constBits())) for _phase, tiles in base for tile in tiles] == original
    before = context
    (canvas._continue_paint_brush if tool == ToolKind.BRUSH else canvas._continue_stroke)(QPointF(250., 64.), .7)
    drain_native(canvas, qapp)
    painter = QPainter(image)
    try:
        batch, count, context = replace_raster_feedback_batch(canvas, painter,
            canvas._render_document_state(), requests, base)
        assert count and context != before
    finally:
        painter.end()
    assert canvas._scene_controller.feedback_patches.byte_count <= 32 * 1024 * 1024
    finish(canvas, tool)


@pytest.mark.parametrize('case', ['released', 'busy', 'selection', 'configuration', 'identity',
    'filtered', 'missing-resident', 'preview', 'geometry', 'density', 'missing-source', 'coverage'])
def test_unowned_or_incomplete_plain_batches_preserve_ordinary_fallback(
        scene, wait_scene, qapp, monkeypatch, case):
    canvas, selected, document, requests, base = setup_contact(scene, wait_scene, qapp, ToolKind.RASTER_PENCIL)
    controller = canvas._scene_controller
    supplied = base
    if case == 'released':
        monkeypatch.setattr(canvas._raster_tile_input, 'released', True)
    elif case == 'busy':
        monkeypatch.setattr(controller, '_covered_contact_gate',
            lambda *_a: SimpleNamespace(busy=True))
    elif case == 'selection':
        monkeypatch.setattr(canvas, 'selected_id', 'another-source')
    elif case in {'configuration', 'identity'}:
        modified = replace(controller.feedback.document, **{case: ('different-owner',)})
        monkeypatch.setattr(controller, 'feedback', replace(controller.feedback, document=modified))
    elif case == 'filtered':
        monkeypatch.setattr(controller, '_filtered_state', SimpleNamespace(close=lambda: None))
    elif case == 'missing-resident':
        owner = canvas.tiles._tiles[selected.object_id]
        for key in controller.feedback_dirty:
            monkeypatch.delitem(owner.residency.entries, (owner, key), raising=False)
    elif case in {'preview', 'geometry', 'density', 'missing-source'}:
        tiles = list(base[0][1])
        tile = tiles[0]
        if case == 'preview':
            tiles[0] = replace(tile, key=('preview', 1))
        elif case == 'geometry':
            tiles[0] = replace(tile, source_rect=QRectF(1., 2., 256., 256.))
        elif case == 'density':
            image = tile.image.copy()
            image.setDevicePixelRatio(2.)
            tiles[0] = replace(tile, image=image)
        else:
            tiles[0] = replace(tile, image=QImage(16, 16, QImage.Format_ARGB32_Premultiplied))
        supplied = [(None, tiles)]
    elif case == 'coverage':
        monkeypatch.setattr(controller, 'feedback', replace(controller.feedback, tiles=()))
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        batch, count, context = replace_raster_feedback_batch(canvas, painter, document, requests, supplied)
        assert batch is supplied and count == 0 and context is None
    finally:
        painter.end()
