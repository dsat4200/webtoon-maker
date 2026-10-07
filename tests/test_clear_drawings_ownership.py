"""Whole drawing clears own cold originals and affected history only."""
import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QPointF
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QMessageBox

from comic_editor.core.models import (ChapterDocument, RasterObject, VectorDrawingObject,
                                      VectorStroke, VectorStrokePoint)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore
from comic_editor.core.pixel_arrays import native_rgba_pixels
from comic_editor.ui.main_window import MainWindow


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.002)
    assert predicate()


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    repository = SeriesRepository(tmp_path / 'project')
    series = repository.create('Clear drawings')
    repository.create_chapter(series, 'Chapter')
    owner = MainWindow()
    assert owner.open_series(repository.root)
    chapter = owner.chapter
    raster = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    vector = chapter.add_object(raster.parent_layer_id, VectorDrawingObject(strokes=[
        VectorStroke(points=[VectorStrokePoint(x=10, y=10), VectorStrokePoint(x=35, y=35)])]))
    unrelated = chapter.add_object(raster.parent_layer_id, RasterObject(name='Unrelated'))
    native = QImage(256, 256, QImage.Format_RGBA64)
    native.fill(QColor.fromRgbF(.310049, .530049, .790049, .410049))
    original = TileStore()
    original.set_tile(raster.object_id, (0, 0), native)
    source = tmp_path / 'cold'
    original.save_directory(source, {raster.object_id}, complete=True)
    cold = TileStore()
    cold.load_directory(source, {raster.object_id})
    owner.canvas.set_document(chapter, cold, owner.active_session.images)
    owner.active_session.tiles = cold
    owner._refresh_hierarchy()
    owner.canvas.set_selection_set([('object', raster.object_id), ('object', vector.object_id)],
                                   primary=('object', vector.object_id))
    owner.active_session.dirty = owner._dirty = False
    yield owner, raster, vector, unrelated, native
    consumers = getattr(owner.canvas, '_scene_consumers', None)
    if consumers is not None:
        consumers.shutdown()
        consumers.executor.shutdown(wait=True, cancel_futures=True)
    for session in owner.sessions.values():
        session.dirty = False
    owner._dirty = False
    owner.close()
    owner.deleteLater()


def test_clear_prepares_native_sources_off_gui_and_preserves_unrelated_rows(window, monkeypatch):
    owner, raster, vector, unrelated, native = window
    chapter, canvas = owner.chapter, owner.canvas
    gui = threading.get_ident()
    original = TileStore._alpha_bbox
    def worker_bounds(image):
        assert threading.get_ident() != gui, 'GUI clear/history alpha scan'
        return original(image)
    monkeypatch.setattr(TileStore, '_alpha_bbox', staticmethod(worker_bounds))
    monkeypatch.setattr(ChapterDocument, 'to_dict', lambda *_a, **_k:
        pytest.fail('Clear serialized the whole chapter'))
    monkeypatch.setattr(unrelated, 'to_dict', lambda *_a, **_k:
        pytest.fail('Clear serialized an unrelated drawing'))
    resets = QSignalSpy(owner.hierarchy_model.modelReset)
    owner._clear_canvas()
    jobs = canvas._scene_consumers
    assert jobs.contains(('clear-canvas',)) and vector.strokes
    wait_for(lambda: not jobs.contains(('clear-canvas',)))
    assert not vector.strokes and not canvas.tiles._tiles.get(raster.object_id)
    assert canvas.tiles.residency.decodes == 0
    assert chapter.objects[unrelated.object_id] is unrelated
    assert len(canvas.command_stack._undo) == 1 and resets.count() == 0
    canvas.command_stack.undo()
    assert vector.strokes and chapter.objects[vector.object_id] is vector
    restored = canvas.tiles.tile(raster.object_id, (0, 0))
    assert restored.format() == native.format()
    np.testing.assert_array_equal(native_rgba_pixels(restored)[0], native_rgba_pixels(native)[0])
    assert chapter.objects[unrelated.object_id] is unrelated and resets.count() == 0
    canvas.command_stack.redo()
    assert not vector.strokes and not canvas.tiles._tiles.get(raster.object_id)
    assert chapter.objects[unrelated.object_id] is unrelated and resets.count() == 0


@pytest.mark.parametrize('operation', ['save', 'close'])
def test_accepted_clear_settles_before_save_or_close_dirty_snapshot(window, monkeypatch, operation):
    from comic_editor.render import bake_sources
    owner, raster, vector, unrelated, _native = window
    session = owner.active_session
    entered, release = threading.Event(), threading.Event()
    original = bake_sources.clear_drawings_history
    def held(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(bake_sources, 'clear_drawings_history', held)
    prompts = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *_a: prompts.append(True) or QMessageBox.Save)
    owner._clear_canvas()
    wait_for(entered.is_set)
    assert not session.dirty
    timer = threading.Timer(.05, release.set)
    timer.start()
    try:
        if operation == 'close':
            assert owner.close()
            assert prompts == [True]
        else:
            assert owner.save(wait=True)
            assert not session.dirty and not prompts
    finally:
        release.set()
        timer.join()
    saved, saved_tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert not saved.objects[vector.object_id].strokes
    assert not saved_tiles._tiles.get(raster.object_id)
    assert unrelated.object_id in saved.objects


def test_clear_waits_for_original_cold_brush_transaction_before_capturing_history(window, monkeypatch):
    from comic_editor.ui import tile_input
    owner, raster, vector, unrelated, native = window
    canvas = owner.canvas
    entered, release = threading.Event(), threading.Event()
    original = tile_input.prepare_input_tiles
    def held(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', held)
    canvas.set_selection('object', raster.object_id)
    canvas._begin_paint_brush(QPointF(32, 32), 1)
    wait_for(entered.is_set)
    canvas.set_selection_set([('object', raster.object_id), ('object', vector.object_id)],
                             primary=('object', vector.object_id))
    timer = threading.Timer(.05, release.set)
    timer.start()
    try:
        owner._clear_canvas()
    finally:
        release.set()
        timer.join()
    wait_for(lambda: not canvas._scene_consumers.contains(('clear-canvas',)))
    assert len(canvas.command_stack._undo) == 2
    assert not vector.strokes and not canvas.tiles._tiles.get(raster.object_id)
    canvas.command_stack.undo()
    assert vector.strokes
    restored = canvas.tiles.tile(raster.object_id, (0, 0))
    assert restored != native  # Clear undo retains the accepted brush result.
    canvas.command_stack.undo()
    restored = canvas.tiles.tile(raster.object_id, (0, 0))
    np.testing.assert_array_equal(native_rgba_pixels(restored)[0], native_rgba_pixels(native)[0])
    assert owner.chapter.objects[unrelated.object_id] is unrelated
