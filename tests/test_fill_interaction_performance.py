"""Large fill gestures submit quickly and commit detached, current results only."""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, QThreadPool, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def fill_canvas(qapp):
    chapter = ChapterDocument(height=1400)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 1400))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 1080, 1100)))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    canvas.settings.active_fill_profile().update({"close_gap": False, "antialiasing": False})
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.FILL)
    canvas.set_active_colors("#FF2266CC", "#FFFFFFFF")
    yield canvas, raster
    canvas._cancel_fill_job()
    canvas._clear_fill_replay()
    canvas._effect_jobs.cancel()
    QThreadPool.globalInstance().waitForDone(5000)
    qapp.processEvents()
    canvas.deleteLater()


def wait_until(qapp, predicate, timeout=10):
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert predicate(), "Fill did not finish before the timeout"


def wait_fill(qapp, canvas):
    wait_until(qapp, lambda: not canvas._fill_workers and canvas._fill_job_cancel is None
               and canvas._fill_replay_cancel is None)
    assert canvas._fill_job_error is None


def gate_worker(monkeypatch):
    original = TileStore.advanced_fill
    started, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()
    calls = []

    def delayed(store, *args, **kwargs):
        calls.append(threading.get_ident())
        if threading.get_ident() != main_thread and not started.is_set():
            started.set()
            release.wait(3)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(TileStore, "advanced_fill", delayed)
    return original, started, release, calls


def assert_tiles_equal(first, second, object_id):
    actual, expected = first.object_tiles(object_id), second.object_tiles(object_id)
    assert actual.keys() == expected.keys()
    assert all(actual[key] == expected[key] for key in actual)


def test_large_drag_returns_before_fill_and_preserves_seeds_one_undo(fill_canvas, qapp, monkeypatch):
    canvas, obj = fill_canvas
    canvas.settings.active_fill_profile()["opacity"] = 50
    original, started, release, calls = gate_worker(monkeypatch)
    points = [QPointF(50, 50), QPointF(500, 500), QPointF(950, 900)]
    start = time.perf_counter()
    canvas._begin_fill_gesture(obj, points[0])
    for point in points[1:]:
        canvas._continue_fill_gesture(obj, point)
    canvas._finish_fill_gesture(obj)
    assert time.perf_counter() - start < .15
    wait_until(qapp, started.is_set)
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    qapp.processEvents()
    assert ticks and not canvas.tiles.object_tiles(obj.object_id)
    assert not canvas.command_stack.can_undo
    assert calls and all(thread != threading.get_ident() for thread in calls)
    release.set()
    wait_fill(qapp, canvas)

    expected = TileStore()
    for point in points:
        original(expected, obj.object_id, point, QRectF(*obj.interaction_rect),
                 QColor("#FF2266CC"), canvas.settings.active_fill_profile(), {})
    assert_tiles_equal(canvas.tiles, expected, obj.object_id)
    assert len(canvas.command_stack._undo) == 1
    assert len(canvas._fill_replay_state.steps) == len(points)
    canvas.command_stack.undo()
    assert not canvas.tiles.object_tiles(obj.object_id)
    canvas.command_stack.redo()
    assert_tiles_equal(canvas.tiles, expected, obj.object_id)


@pytest.mark.parametrize("change", ["cancel", "pixels", "selection", "model"])
def test_large_fill_discards_cancelled_or_stale_results(fill_canvas, qapp, monkeypatch, change):
    canvas, obj = fill_canvas
    _original, started, release, _calls = gate_worker(monkeypatch)
    canvas._begin_fill_gesture(obj, QPointF(50, 50))
    canvas._finish_fill_gesture(obj)
    wait_until(qapp, started.is_set)
    if change == "cancel":
        canvas.set_tool(ToolKind.RASTER_PENCIL)
    elif change == "pixels":
        canvas.tiles.paint_dab(obj.object_id, QPointF(700, 700), 4, QColor("red"))
    elif change == "selection":
        selection = QPainterPath()
        selection.addRect(QRectF(600, 600, 100, 100))
        canvas._drawing_selection_path = selection
    else:
        obj.x += 30
    before = canvas._snapshot_fill_object_tiles(obj.object_id)
    release.set()
    wait_fill(qapp, canvas)
    actual = canvas.tiles.object_tiles(obj.object_id)
    assert actual.keys() == before.keys()
    assert all(actual[key] == before[key] for key in actual)
    assert not canvas.command_stack.can_undo


def test_pending_fill_tolerance_uses_captured_color_and_one_undo(fill_canvas, qapp, monkeypatch):
    canvas, obj = fill_canvas
    canvas.settings.active_fill_profile()["tolerance"] = 0
    for key in canvas.tiles.keys_for_rect(QRectF(*obj.interaction_rect)):
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor("#FF101010" if key[0] < 2 else "#FF202020"))
        canvas.tiles.set_tile(obj.object_id, key, image)
    before = canvas._snapshot_fill_object_tiles(obj.object_id)
    _original, started, release, _calls = gate_worker(monkeypatch)
    canvas._begin_fill_gesture(obj, QPointF(40, 40))
    canvas._finish_fill_gesture(obj)
    wait_until(qapp, started.is_set)
    canvas.request_fill_tolerance_replay(20, immediate=True)
    canvas._fill_operation_color = QColor("red")
    release.set()
    wait_fill(qapp, canvas)
    assert canvas.tiles.tile(obj.object_id, (3, 1)).pixelColor(20, 20) == QColor("#FF2266CC")
    assert len(canvas.command_stack._undo) == 1
    assert canvas._fill_replay_state.profile["tolerance"] == 20
    canvas.command_stack.undo()
    assert all(canvas.tiles.tile(obj.object_id, key) == image for key, image in before.items())


def test_composite_reference_capture_yields_and_includes_morphology_halo(fill_canvas, qapp, monkeypatch):
    canvas, obj = fill_canvas
    reference = canvas.chapter.add_object(obj.parent_layer_id, RasterObject())
    reference.fill_reference = True
    profile = canvas.settings.active_fill_profile()
    profile.update({"reference_mode": "reference", "close_gap": True, "gap_threshold": 8,
                    "fill_narrow_areas": False, "area_scaling": True, "area_amount": 3})
    captured = {}
    original = canvas._fill_reference_tile

    def slow_capture(*args, **kwargs):
        image = original(*args, **kwargs)
        captured[args[1]] = image
        time.sleep(.003)
        return image

    monkeypatch.setattr(canvas, "_fill_reference_tile", slow_capture)
    start = time.perf_counter()
    canvas._begin_fill_gesture(obj, QPointF(80, 80))
    canvas._finish_fill_gesture(obj)
    assert time.perf_counter() - start < .15
    assert not captured
    ticks = []
    timer = QTimer()
    timer.setInterval(1)
    timer.timeout.connect(lambda: ticks.append(len(captured)))
    timer.start()
    wait_fill(qapp, canvas)
    timer.stop()
    assert len(set(ticks)) > 2
    assert (-1, -1) in captured
    expected = TileStore()
    expected.advanced_fill(obj.object_id, QPointF(80, 80), QRectF(*obj.interaction_rect),
                           QColor("#FF2266CC"), profile, {}, reference_tile=captured.get)
    assert_tiles_equal(canvas.tiles, expected, obj.object_id)
    assert canvas._fill_replay_state.reference_tiles.keys() == captured.keys()
    replay_references = []
    original_fill = TileStore.advanced_fill

    def record_replay(store, *args, **kwargs):
        replay_references.append(kwargs["reference_tile"].__self__)
        return original_fill(store, *args, **kwargs)

    monkeypatch.setattr(TileStore, "advanced_fill", record_replay)
    canvas.request_fill_tolerance_replay(20, immediate=True)
    wait_fill(qapp, canvas)
    assert replay_references and replay_references[0].keys() == captured.keys()


def test_reference_descendant_change_invalidates_pending_snapshot(fill_canvas, qapp, monkeypatch):
    canvas, obj = fill_canvas
    folder = canvas.chapter.add_layer(obj.parent_layer_id, "Reference", BoundGeometry.rectangle(0, 0, 1080, 1100))
    folder.fill_reference = True
    child = canvas.chapter.add_object(folder.layer_id, RasterObject())
    canvas.settings.active_fill_profile()["reference_mode"] = "reference"
    _original, started, release, _calls = gate_worker(monkeypatch)
    canvas._begin_fill_gesture(obj, QPointF(50, 50))
    canvas._finish_fill_gesture(obj)
    wait_until(qapp, started.is_set)
    canvas.tiles.paint_dab(child.object_id, QPointF(600, 600), 7, QColor("red"))
    release.set()
    wait_fill(qapp, canvas)
    assert not canvas.tiles.object_tiles(obj.object_id)
    assert not canvas.command_stack.can_undo


def test_fill_retains_unrelated_modifier_source_and_outline_distance(fill_canvas, qapp):
    canvas, obj = fill_canvas
    image = QImage(30, 30, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("blue"))
    canvas._modifier_source_cache_put(("unrelated",), image)
    alpha = np.zeros((30, 30), np.float32)
    alpha[10:20, 10:20] = 1
    distance = canvas._outline_distance_cache.distance(alpha)
    canvas._begin_fill_gesture(obj, QPointF(50, 50))
    canvas._finish_fill_gesture(obj)
    wait_fill(qapp, canvas)
    assert canvas._modifier_source_cache_get(("unrelated",)) == image
    assert canvas._outline_distance_cache.distance(alpha) is distance
    assert canvas._outline_distance_cache.computations == 1


@pytest.mark.parametrize("subtool", ["lasso_fill", "enclose_fill"])
def test_large_enclosed_gesture_preserves_clip_and_atomic_undo(fill_canvas, qapp, subtool):
    canvas, obj = fill_canvas
    canvas.settings.active_fill_subtool = subtool
    canvas.settings.active_fill_profile().update({"reference_mode": "editing", "close_gap": False, "antialiasing": False})
    selection = QPainterPath()
    selection.addRect(QRectF(40, 40, 400, 400))
    canvas._drawing_selection_path = selection
    canvas._begin_fill_gesture(obj, QPointF(100, 100))
    for point in (QPointF(700, 100), QPointF(700, 700), QPointF(100, 700)):
        canvas._continue_fill_gesture(obj, point)
    canvas._finish_fill_gesture(obj)
    assert not canvas.tiles.object_tiles(obj.object_id)
    wait_fill(qapp, canvas)
    assert canvas.tiles.tile(obj.object_id, (0, 0)).pixelColor(200, 200) == QColor("#FF2266CC")
    assert canvas.tiles.tile(obj.object_id, (0, 0)).pixelColor(60, 60).alpha() == 0
    assert canvas.tiles.tile(obj.object_id, (1, 1)).pixelColor(220, 220).alpha() == 0
    assert len(canvas.command_stack._undo) == 1
    canvas.command_stack.undo()
    assert not canvas.tiles.object_tiles(obj.object_id)


def test_reference_cache_budget_does_not_limit_fill_or_replay_size(fill_canvas, qapp, monkeypatch):
    canvas, obj = fill_canvas
    canvas.settings.active_fill_profile()["reference_mode"] = "all_visible"
    canvas._fill_reference_tile_cache_budget = 1024
    _original, started, release, calls = gate_worker(monkeypatch)
    canvas._begin_fill_gesture(obj, QPointF(50, 50))
    canvas._finish_fill_gesture(obj)
    wait_until(qapp, started.is_set)
    release.set()
    wait_fill(qapp, canvas)
    assert canvas.command_stack.can_undo
    canvas.request_fill_tolerance_replay(10, immediate=True)
    wait_fill(qapp, canvas)
    assert len(calls) == 2 and all(thread != threading.get_ident() for thread in calls)
    assert len(canvas.command_stack._undo) == 1


def test_tall_painted_layer_small_region_submits_without_pixel_scan(fill_canvas, qapp, monkeypatch):
    canvas, obj = fill_canvas
    obj.interaction_rect = (0, 0, 1080, 20000)
    black = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    black.fill(QColor("black"))
    for key in canvas.tiles.keys_for_rect(QRectF(*obj.interaction_rect)):
        canvas.tiles.set_tile(obj.object_id, key, black)
    spot = QImage(black)
    painter = QPainter(spot)
    painter.fillRect(QRectF(60, 40, 30, 30), QColor("white"))
    painter.end()
    canvas.tiles.set_tile(obj.object_id, (2, 74), spot)
    before = canvas._snapshot_fill_object_tiles(obj.object_id)
    original_bbox = TileStore._alpha_bbox
    scanning_on_gui = []
    main_thread = threading.get_ident()
    submitting = True

    def record_scan(image):
        if submitting and threading.get_ident() == main_thread:
            scanning_on_gui.append(True)
        return original_bbox(image)

    monkeypatch.setattr(TileStore, "_alpha_bbox", staticmethod(record_scan))
    started = time.perf_counter()
    canvas._begin_fill_gesture(obj, QPointF(580, 19000))
    canvas._finish_fill_gesture(obj)
    submitting = False
    assert time.perf_counter() - started < .15
    assert not scanning_on_gui
    wait_fill(qapp, canvas)
    assert canvas.tiles.tile(obj.object_id, (2, 74)).pixelColor(68, 56) == QColor("#FF2266CC")
    changed = [key for key, image in before.items() if canvas.tiles.tile(obj.object_id, key) != image]
    assert changed == [(2, 74)]
    canvas.command_stack.undo()
    assert all(canvas.tiles.tile(obj.object_id, key) == image for key, image in before.items())


def test_large_fill_shows_exact_preview_while_held_then_commits_one_undo(fill_canvas, qapp):
    canvas, obj = fill_canvas
    profile = canvas.settings.active_fill_profile()
    profile["opacity"] = 50
    expected = TileStore()
    points = (QPointF(50, 50), QPointF(600, 600))
    canvas._begin_fill_gesture(obj, points[0])
    wait_fill(qapp, canvas)
    expected.advanced_fill(obj.object_id, points[0], QRectF(*obj.interaction_rect),
                           QColor("#FF2266CC"), profile, {})
    assert canvas._fill_gesture_active and not canvas.command_stack.can_undo
    assert_tiles_equal(canvas.tiles, expected, obj.object_id)
    canvas._continue_fill_gesture(obj, points[1])
    wait_fill(qapp, canvas)
    expected.advanced_fill(obj.object_id, points[1], QRectF(*obj.interaction_rect),
                           QColor("#FF2266CC"), profile, {})
    assert_tiles_equal(canvas.tiles, expected, obj.object_id)
    assert not canvas.command_stack.can_undo
    canvas._finish_fill_gesture(obj)
    assert len(canvas.command_stack._undo) == 1
    assert len(canvas._fill_replay_state.steps) == 2
    canvas.command_stack.undo()
    assert not canvas.tiles.object_tiles(obj.object_id)


@pytest.mark.parametrize("external_edit", [False, True])
def test_cancel_live_fill_preview_restores_only_its_own_tiles(fill_canvas, qapp, monkeypatch, external_edit):
    canvas, obj = fill_canvas
    canvas.settings.active_fill_profile()["opacity"] = 50
    canvas._begin_fill_gesture(obj, QPointF(50, 50))
    wait_fill(qapp, canvas)
    assert canvas.tiles.object_tiles(obj.object_id)
    _original, started, release, _calls = gate_worker(monkeypatch)
    canvas._continue_fill_gesture(obj, QPointF(600, 600))
    wait_until(qapp, started.is_set)
    edited = None
    if external_edit:
        canvas.tiles.paint_dab(obj.object_id, QPointF(20, 20), 4, QColor("red"))
        edited = QImage(canvas.tiles.tile(obj.object_id, (0, 0)))
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    release.set()
    wait_fill(qapp, canvas)
    assert not canvas.command_stack.can_undo
    if external_edit:
        assert canvas.tiles.object_tiles(obj.object_id).keys() == {(0, 0)}
        assert canvas.tiles.tile(obj.object_id, (0, 0)) == edited
    else:
        assert not canvas.tiles.object_tiles(obj.object_id)


def test_switching_documents_discards_live_fill_preview_and_late_result(fill_canvas, qapp, monkeypatch):
    canvas, obj = fill_canvas
    canvas.settings.active_fill_profile()["opacity"] = 50
    canvas._begin_fill_gesture(obj, QPointF(50, 50))
    wait_fill(qapp, canvas)
    original_tiles = canvas.tiles
    assert original_tiles.object_tiles(obj.object_id)
    _original, started, release, _calls = gate_worker(monkeypatch)
    canvas._continue_fill_gesture(obj, QPointF(600, 600))
    canvas._finish_fill_gesture(obj)
    wait_until(qapp, started.is_set)
    replacement = ChapterDocument()
    replacement_tiles = TileStore()
    canvas.set_document(replacement, replacement_tiles)
    assert not original_tiles.object_tiles(obj.object_id)
    release.set()
    wait_fill(qapp, canvas)
    assert canvas.chapter is replacement and canvas.tiles is replacement_tiles
    assert not replacement_tiles._tiles and not canvas.command_stack.can_undo


def test_selection_fill_replaces_pending_gesture_without_baking_preview(fill_canvas, qapp, monkeypatch):
    canvas, obj = fill_canvas
    canvas.settings.active_fill_profile()["opacity"] = 50
    canvas._begin_fill_gesture(obj, QPointF(50, 50))
    wait_fill(qapp, canvas)
    _original, started, release, _calls = gate_worker(monkeypatch)
    canvas._continue_fill_gesture(obj, QPointF(600, 600))
    canvas._finish_fill_gesture(obj)
    wait_until(qapp, started.is_set)
    selection = QPainterPath()
    selection.addRect(QRectF(10, 10, 20, 20))
    canvas._drawing_selection_path = selection
    assert canvas.fill_active_selection()
    release.set()
    wait_fill(qapp, canvas)
    assert canvas.tiles.object_tiles(obj.object_id).keys() == {(0, 0)}
    assert canvas.tiles.tile(obj.object_id, (0, 0)).pixelColor(50, 50).alpha() == 0
    assert len(canvas.command_stack._undo) == 1
    canvas.command_stack.undo()
    assert not canvas.tiles.object_tiles(obj.object_id)
