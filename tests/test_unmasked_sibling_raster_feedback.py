"""Unrelated unmasked siblings retain their native foreground ordering."""
from threading import Event
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import BoundGeometry, RasterObject
from comic_editor.ui.canvas import ToolKind
from test_raster_contact_feedback import scene, pixels, ready_paint, _queue_blocked_native_scene
from test_raster_contact_feedback_boundaries import native_decorated
from test_fractional_raster_feedback_native import fixed_native_reference


@pytest.mark.parametrize('kind', ['object', 'layer'])
@pytest.mark.parametrize('position', ['before', 'after'])
@pytest.mark.parametrize('erasing', [False, True])
@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied, QImage.Format_RGBA64_Premultiplied])
def test_unmasked_sibling_matches_native_full_buffer_during_held_contact(
        scene, wait_scene, monkeypatch, kind, position, erasing, nested, format):
    canvas, selected, _front = scene
    chapter = canvas.chapter
    page = chapter.layers[selected.parent_layer_id]
    if nested:
        index = next(i for i, ref in enumerate(page.children) if ref.entity_id == selected.object_id)
        branch = page.children.pop(index)
        layer = chapter.add_layer(page.layer_id, 'Selected branch', BoundGeometry.rectangle(0, 0, 128, 128))
        layer.fill_color, layer.border_width = None, 0
        page.children.insert(index, page.children.pop())
        layer.children = [branch]
        selected.parent_layer_id = layer.layer_id
    if kind == 'layer':
        sibling = chapter.add_layer(page.layer_id, 'Unmasked', BoundGeometry.rectangle(0, 0, 128, 128))
        sibling.fill_color, sibling.border_width = None, 0
        obj = chapter.add_object(sibling.layer_id, RasterObject())
    else:
        sibling = obj = chapter.add_object(page.layer_id, RasterObject())
    sibling.ignore_parent_mask = True
    reference = page.children.pop()
    page.children.insert(0 if position == 'before' else len(page.children), reference)
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.fillRect(76, 0, 16, 128, QColor('#a040c020'))
    painter.end()
    canvas.tiles.set_tile(obj.object_id, (0, 0), image)
    image = QImage(256, 256, format)
    image.fill(QColor.fromRgbF(.2713, .1837, .8291, .7423))
    canvas.tiles.set_tile(selected.object_id, (0, 0), image)
    if erasing:
        canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    assert canvas._scene_controller.feedback is not None
    original_sources = {key: bytes(image.constBits())
        for key, image in canvas.tiles.object_tiles(selected.object_id).items()}
    history = canvas.command_stack.revision
    gate = Event()
    scheduler = canvas._scene_controller.scheduler
    original = scheduler._evaluate_admitted

    def blocked(demand, token):
        assert gate.wait(10)
        return original(demand, token)

    monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
    _queue_blocked_native_scene(canvas)
    try:
        canvas._begin_stroke(QPointF(32, 64), 1.)
        canvas._continue_stroke(QPointF(100, 64), 1.)
        actual = ready_paint(canvas)
        expected = native_decorated(canvas)
        np.testing.assert_array_equal(pixels(actual), pixels(expected))
        assert actual.pixelColor(80, 4) != QColor('#242428'), 'Unmasked foreground was clipped to the page'
        canvas._end_stroke()
    finally:
        gate.set()
    wait_scene(canvas)
    assert canvas.command_stack.revision == history + 1
    canvas.command_stack.undo()
    wait_scene(canvas)
    assert {key: bytes(image.constBits())
        for key, image in canvas.tiles.object_tiles(selected.object_id).items()} == original_sources


@pytest.mark.skipif(not os.environ.get('DOODLES_CONTACT_PROJECT_COPY'),
    reason='Set DOODLES_CONTACT_PROJECT_COPY to the private Doodles recovery copy')
@pytest.mark.parametrize('erasing', [False, True])
def test_captured_doodles_scene_matches_cold_native_pixels_and_undo(qapp, wait_scene, monkeypatch, erasing):
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.core.settings import EditorSettings
    from comic_editor.ui.canvas import CanvasWidget

    project = Path(os.environ['DOODLES_CONTACT_PROJECT_COPY']).resolve()
    root = Path(__file__).resolve().parents[1]
    assert project.is_relative_to(root / '.artifacts')

    def manifest():
        return {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in project.rglob('*') if p.is_file()}

    files = manifest()
    chapter, tiles, images = SeriesRepository(project).load_chapter(
        '49354b12f0aa4f09a3522a623d520fc1', recover=True, include_images=True)
    assert (len(chapter.layers), len(chapter.objects), len(chapter.modifiers), len(chapter.masks)) == (28, 75, 11, 5)
    selected = chapter.objects['572d37134a904b8ab7220c8d4febf507']
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', predictive_ink=False,
        grid_overlay_visible=False, snap_to_grid=False))
    canvas.resize(951, 937)
    canvas.set_document(chapter, tiles, images)
    canvas.center_x, canvas.center_y, canvas.scale = 664.2173700481578, 8226.007869070449, .6581012198770725
    canvas.primary_color = '#ffff0000'
    canvas.set_selection('object', selected.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER if erasing else ToolKind.RASTER_PENCIL)
    gate = Event()
    try:
        wait_scene(canvas)
        assert canvas._scene_controller.feedback is not None
        original_sources = {key: bytes(image.constBits()) for key, image in tiles.object_tiles(selected.object_id).items()}
        original = canvas._scene_controller.scheduler._evaluate_admitted

        def blocked(demand, token):
            assert gate.wait(20)
            return original(demand, token)

        monkeypatch.setattr(canvas._scene_controller.scheduler, '_evaluate_admitted', blocked)
        _queue_blocked_native_scene(canvas)
        mapping = canvas._drawing_local_to_world_transform(selected)
        canvas._begin_stroke(mapping.map(QPointF(300, 400)), 1.)
        canvas._continue_stroke(mapping.map(QPointF(650, 400)), 1.)
        actual = ready_paint(canvas)
        expected = fixed_native_reference(canvas)
        difference = int(np.count_nonzero(pixels(actual) != pixels(expected)))
        assert difference == 0, f'{difference} displayed channels differ from cold native tiles'
        canvas._end_stroke()
        gate.set()
        wait_scene(canvas)
        canvas.command_stack.undo()
        wait_scene(canvas)
        assert {key: bytes(image.constBits()) for key, image in tiles.object_tiles(selected.object_id).items()} == original_sources
        assert files == manifest()
        evidence = root / '.artifacts/drawing-eraser-20261011'
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / f'native-pixels-{"eraser" if erasing else "pencil"}.json').write_text(json.dumps({
            'chapter': chapter.chapter_id, 'selected': selected.object_id,
            'compared_pixels': actual.width() * actual.height(), 'different_channels': difference,
            'undo_restored_source_bytes': True, 'project_files_unchanged': True}, indent=2), encoding='utf-8')
    finally:
        gate.set()
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()
