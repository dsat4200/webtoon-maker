"""Prospective guards: ordinary live contacts defer only disk status validation.

Unrun artifact payload. Reuses the real disk-cache editor fixture, original
semantic keys and production tool handlers; no renderer/provider replacement.
"""
from collections import deque
from concurrent.futures import Future
import copy
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from comic_editor.core.models import ParameterMaskBinding, ToneMask, RasterObject
from comic_editor.core.tools import ToolKind
from test_disk_render_cache import editor  # real MainWindow/private repository fixture


def wait(qapp, predicate):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.002)
    assert predicate(), "Ordinary contact/cancellation did not finish"


def begin_contact(window, qapp, kind):
    canvas = window.canvas
    canvas.settings.snap_to_grid = False
    canvas.setFixedSize(640, 480)
    canvas.scale = .4
    canvas.center_x, canvas.center_y = 540, 384
    window.show()
    if kind == "mask":
        raster = next(obj for obj in canvas.chapter.objects.values() if isinstance(obj, RasterObject))
        mask = ToneMask(saved=True, name="Status contact mask")
        canvas.chapter.masks[mask.mask_id] = mask
        raster.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
        canvas.set_tone_mask_mode(mask.mask_id)
        canvas.set_tool(ToolKind.RASTER_PENCIL)
        start, end = QPointF(90, 80), QPointF(115, 95)
    else:
        page_id = canvas.chapter.root_page_ids[0]
        canvas.set_selection("layer", page_id)
        canvas.set_tool(ToolKind.TRANSFORM)
        start, end = QPointF(540, 384), QPointF(585, 414)
    qapp.processEvents()
    window.disk_cache.timer.stop()
    before = copy.deepcopy(canvas.chapter.to_dict())
    history = tuple(canvas.command_stack._undo)
    first = canvas.document_to_widget(start).toPoint()
    last = canvas.document_to_widget(end).toPoint()
    QTest.mousePress(canvas, Qt.LeftButton, pos=first)
    QTest.mouseMove(canvas, last)
    wait(qapp, lambda: canvas._drawing if kind == "mask" else canvas._projection_has_live_preview())
    if kind == "mask":
        assert canvas._drawing and canvas._mask_tile_input is not None
    else:
        assert canvas._geometry_transform_target is not None and canvas._transform_preview_quad
        assert canvas.chapter.to_dict() == before
    return last, before, history


def observe_status(cache, monkeypatch):
    observed, polls = [], []
    original_row, original_poll = cache.row_ready, cache.backing.poll
    def row_ready(row):
        canvas = cache.canvas
        assert not canvas._drawing and not canvas._projection_has_live_preview()
        observed.append((row, canvas._document_projection.revision))
        return original_row(row)
    def poll():
        polls.append(True)
        return original_poll()
    monkeypatch.setattr(cache, "row_ready", row_ready)
    monkeypatch.setattr(cache.backing, "poll", poll)
    cache._revision = cache.canvas._document_projection.revision
    cache._status_queue = deque((0, 1, 2))
    return observed, polls


@pytest.mark.parametrize("kind", ["mask", "transform"])
@pytest.mark.parametrize("outcome", ["commit", "cancel"])
def test_real_contact_preserves_status_queue_then_current_semantic_retry(editor, qapp, monkeypatch, kind, outcome):
    window, _, _ = editor
    cache, canvas = window.disk_cache, window.canvas
    last, before, history = begin_contact(window, qapp, kind)
    observed, polls = observe_status(cache, monkeypatch)
    queued = tuple(cache._status_queue)
    entries = copy.deepcopy(cache.backing.entries)
    for _ in range(4):
        cache.tick()
    assert tuple(cache._status_queue) == queued and not observed
    assert len(polls) == 4 and cache.backing.entries == entries
    assert not cache.building and tuple(canvas.command_stack._undo) == history
    if outcome == "commit":
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=last)
    else:
        QTest.keyClick(canvas, Qt.Key_Escape)
    wait(qapp, lambda: not canvas._drawing and not canvas._projection_has_live_preview())
    if outcome == "cancel":
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == history
    else:
        assert len(canvas.command_stack._undo) == len(history) + 1
        if kind == "mask":
            assert canvas.tiles.object_tiles(canvas.active_tone_mask_id)
        else:
            assert canvas.chapter.to_dict() != before
    cache.tick()
    assert observed and all(revision == canvas._document_projection.revision for _, revision in observed)
    assert cache.backing.entries == entries  # status validation is never a disk writer


@pytest.mark.parametrize("completion", ["maintenance", "scheduler"])
def test_pending_native_contact_does_not_hide_completed_storage_ownership(editor, qapp, monkeypatch, completion):
    window, _, _ = editor
    cache, canvas = window.disk_cache, window.canvas
    _last, before, history = begin_contact(window, qapp, "mask")
    observed, polls = observe_status(cache, monkeypatch)
    entries, sources = dict(cache.backing.entries), dict(cache.backing.source_digests)
    if completion == "maintenance":
        future = Future()
        future.set_result((entries, sources))
        cache._maintenance = future, cache._serial, "clear"
    else:
        # Metadata handoff only: the original serial/current committed manifest
        # format, no scene/image substitution and no invented pixel result.
        monkeypatch.setattr(cache.scheduler, "poll", lambda: [SimpleNamespace(
            demand=SimpleNamespace(serial=cache._serial), cache_manifest=(entries,sources),
            recorded_rows=(0,), error="", done=True, recorded=True)])
    cache.tick()
    assert not observed and polls and cache._status_queue
    if completion == "maintenance":
        assert cache._maintenance is None and cache.message == "Cache cleared"
    else:
        assert cache._evaluation_done and 0 in cache._pending_rows
    QTest.keyClick(canvas, Qt.Key_Escape)
    wait(qapp, lambda: not canvas._drawing and not canvas._projection_has_live_preview())
    assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == history
    cache.tick()
    assert observed
