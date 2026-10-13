"""UI surround stays consistent while native feedback and exact tiles alternate."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF
from PySide6.QtGui import QColor

from comic_editor.render.raster_feedback import prepare_raster_feedback
from comic_editor.ui.raster_feedback import RasterFeedbackPatchCache, _raster_feedback_tiles
from test_raster_contact_feedback import scene, ready_paint, _queue_blocked_native_scene
from test_raster_feedback_solo import freeze


@pytest.fixture
def palette(qapp):
    previous = qapp.property('ui_theme_palette')
    def set_color(color, canvas):
        qapp.setProperty('ui_theme_palette', {**(previous or {}), 'canvas': color})
        QCoreApplication.sendEvent(canvas, QEvent(QEvent.PaletteChange))
    yield set_color
    qapp.setProperty('ui_theme_palette', previous)


@pytest.mark.parametrize('rotation', [0., -.663008407605119])
def test_theme_surround_matches_idle_live_release_and_exact(scene, palette, wait_scene, monkeypatch, rotation):
    canvas, selected, _front = scene
    canvas.set_solo_entities({('object', selected.object_id)})
    canvas.rotation = rotation
    palette('#282828', canvas)
    wait_scene(canvas)
    assert ready_paint(canvas).pixelColor(4, 64) == QColor('#282828')
    blocker = Event()
    scheduler = canvas._scene_controller.scheduler
    original = scheduler._evaluate_admitted
    def blocked(demand, token):
        assert blocker.wait(10)
        return original(demand, token)
    monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
    _queue_blocked_native_scene(canvas)
    try:
        canvas._begin_stroke(QPointF(28, 64), 1.)
        assert ready_paint(canvas).pixelColor(4, 64) == QColor('#282828')
        palette('#e8f1f0', canvas)
        assert ready_paint(canvas).pixelColor(4, 64) == QColor('#e8f1f0')
        canvas._end_stroke()
        assert ready_paint(canvas).pixelColor(4, 64) == QColor('#e8f1f0')
    finally:
        blocker.set()
    wait_scene(canvas)
    assert ready_paint(canvas).pixelColor(4, 64) == QColor('#e8f1f0')


def test_flattened_patch_color_changes_without_changing_native_sources(scene, palette):
    canvas, selected, _front = scene
    canvas.tiles._object_tiles(selected.object_id)
    canvas.set_solo_entities({('object', selected.object_id)})
    snapshot = freeze(canvas)
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared = worker.submit(prepare_raster_feedback, snapshot, selected.object_id,
            (0., 0., 128., 128.)).result(timeout=10)
    assert prepared is not None
    source_key = prepared.tiles[0].source.cacheKey()
    cache = RasterFeedbackPatchCache()
    dirty = {(0, 0)}
    palette('#282828', canvas)
    first = _raster_feedback_tiles(canvas, prepared, dirty, cache, False)
    view = canvas._scene_controller.presentation_view()
    palette('#e8f1f0', canvas)
    second = _raster_feedback_tiles(canvas, prepared, dirty, cache, False)
    assert first and second
    assert first[0].image.pixelColor(4, 64) == QColor('#282828')
    assert second[0].image.pixelColor(4, 64) == QColor('#e8f1f0')
    assert prepared.tiles[0].source.cacheKey() == source_key
    assert view != canvas._scene_controller.presentation_view()
