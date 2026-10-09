"""Warm authored Brush contacts begin with the already prepared native owner."""
from dataclasses import replace
from threading import get_ident
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.brush_features import _PreparedBrushMaterialCache
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from test_brush_input_admission import close, pixels, wait_for
from test_brush_material_ownership import definition


def canvas_for(brush, *, cache=True):
    chapter = ChapterDocument(width=256, height=256, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 256, 256))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 256, 256)))
    tiles = TileStore()
    original = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    original.fill(QColor('#76335981'))
    tiles.set_tile(raster.object_id, (0, 0), original)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', snap_to_grid=False,
                                        grid_overlay_visible=False))
    canvas.set_document(chapter, tiles)
    canvas.set_selection('object', raster.object_id)
    canvas.set_tool(ToolKind.BRUSH)
    canvas.primary_color = '#a0dd3311'
    canvas.settings.active_paint_brush = lambda: brush
    if not cache:
        canvas._paint_brush_material_cache.budget = 0
    return canvas, raster


def stroke(canvas, *, y=30, begin_only=False):
    canvas._paint_brush_packet_time = 10.
    canvas._begin_paint_brush(QPointF(25, y), .3)
    if begin_only:
        return
    for x, pressure, timestamp in ((49, .8, 10.016), (76, .5, 10.033)):
        canvas._paint_brush_packet_time = timestamp
        canvas._continue_paint_brush(QPointF(x, y), pressure)
    canvas._finish_paint_brush()
    wait_for(lambda: canvas._paint_brush_tile_input is None)


@pytest.mark.parametrize('mode', ['stamp', 'spray', 'replay'])
def test_warm_contact_applies_first_dab_without_worker_and_retains_exact_native_pixels(
        qapp, monkeypatch, mode):
    from comic_editor.ui import brush_features
    brush = definition()
    brush = replace(brush, dual=replace(brush.dual, direction='fixed'))
    if mode == 'spray':
        brush = replace(brush, spray=True, particle_size=7, particle_density=3)
    elif mode == 'replay':
        brush = replace(brush, post_correction=.4, taper_end=8)
    expected, expected_raster = canvas_for(brush, cache=False)
    actual, actual_raster = canvas_for(brush)
    gui, preparation_threads = get_ident(), []
    prepare = brush_features.prepare_material_resources
    def record_prepare(*args):
        preparation_threads.append(get_ident())
        assert get_ident() != gui, 'Material preparation ran on the GUI'
        return prepare(*args)
    record_prepare.admission_priority = prepare.admission_priority
    record_prepare.working_bytes = prepare.working_bytes
    monkeypatch.setattr(brush_features, 'prepare_material_resources', record_prepare)
    try:
        stroke(expected)
        stroke(expected, y=70)
        stroke(actual)
        assert preparation_threads and all(thread != gui for thread in preparation_threads)
        owner = next(iter(actual._paint_brush_material_cache.values.values()))[0]
        preparation_count = len(preparation_threads)
        before = pixels(actual, actual_raster)
        # Pen-down must publish its native dab in this call, before any event
        # pump or detached result becomes available.
        stroke(actual, y=70, begin_only=True)
        gate = actual._paint_brush_tile_input
        assert not gate.pending and not gate.queue and gate.current()
        assert actual._paint_brush_stroke._materials is owner
        assert pixels(actual, actual_raster) != before
        for x, pressure, timestamp in ((49, .8, 10.016), (76, .5, 10.033)):
            actual._paint_brush_packet_time = timestamp
            actual._continue_paint_brush(QPointF(x, 70), pressure)
        actual._finish_paint_brush()
        wait_for(lambda: actual._paint_brush_tile_input is None)
        assert len(preparation_threads) == preparation_count
        assert pixels(actual, actual_raster) == pixels(expected, expected_raster)
        assert all(image.format() == QImage.Format_RGBA64_Premultiplied
                   for image in actual.tiles.object_tiles(actual_raster.object_id).values())
        final = pixels(actual, actual_raster)
        actual.command_stack.undo()
        assert pixels(actual, actual_raster) == before
        actual.command_stack.redo()
        assert pixels(actual, actual_raster) == final
    finally:
        close(actual)
        close(expected)


def test_material_identity_reuses_only_complete_requests_without_hashing_pngs():
    class NoHash(str):
        def __hash__(self):
            pytest.fail('Pen-down hashed an authored PNG')
    brush = definition()
    source = NoHash(brush.tips[0].png)
    brush = replace(brush, tips=(replace(brush.tips[0], png=source),), texture=None, dual=None)
    owner = SimpleNamespace(_values={0: SimpleNamespace(nbytes=32)})
    cache = _PreparedBrushMaterialCache()
    cache.put(brush, owner)
    assert cache.get(replace(brush, size=250, opacity=.3, name='Renamed')) is owner
    assert cache.get(replace(brush, antialiasing=0)) is None
    assert cache.get(replace(brush, tips=(replace(brush.tips[0], width=64, height=64),))) is None
    replacement = NoHash(brush.tips[0].png)
    assert replacement is not source
    assert cache.get(replace(brush, tips=(replace(brush.tips[0], png=replacement),))) is None
    assert next(iter(cache.values.values()))[1] == (source,)


def test_material_reuse_observes_byte_budget_entry_limit_and_lru():
    brush = replace(definition(), texture=None, dual=None)
    brushes = [replace(brush, tips=(replace(brush.tips[0], png=str(index)),)) for index in range(4)]
    owner = SimpleNamespace(_values={0: SimpleNamespace(nbytes=20)})
    cache = _PreparedBrushMaterialCache(budget=63, limit=2)
    cache.put(brushes[0], owner)
    cache.put(brushes[1], owner)
    assert cache.get(brushes[0]) is owner  # this owner becomes most recent
    cache.put(brushes[2], owner)
    assert cache.get(brushes[1]) is None
    assert cache.get(brushes[0]) is owner and cache.get(brushes[2]) is owner
    assert cache.bytes == 42 and len(cache.values) == 2
    cache.put(brushes[3], SimpleNamespace(_values={0: SimpleNamespace(nbytes=64)}))
    assert cache.get(brushes[3]) is None and cache.bytes <= cache.budget

