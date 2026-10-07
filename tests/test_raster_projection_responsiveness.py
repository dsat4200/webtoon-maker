"""Raster presentation yields pending exact work without weakening captures."""
import time
from threading import Event, get_ident

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import OutlineModifier
from comic_editor.ui import interactive_effects
from test_projection_invalidation import scene


def widget_frame(canvas):
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#242428"))
    painter = QPainter(image)
    try:
        canvas._paint_canvas_frame(painter, None)
    finally:
        painter.end()
    return image


def test_raster_widget_yields_blocked_effect_and_publishes_exact_result(scene, qapp, monkeypatch):
    canvas, obj, _ = scene
    canvas.chapter.add_modifier(
        OutlineModifier(thickness=3, blur_radius=4, blur_strength=50),
        [("object", obj.object_id)],
    )
    canvas._invalidate_scene_cache()
    # Detached/reference callers remain synchronous, providing an exact oracle.
    canvas._ensure_scene_cache()
    expected = canvas._scene_cache.copy()
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._effect_jobs.cancel()
    canvas._document_projection.clear()
    canvas._projection_completed_view = None
    canvas._projection_completed_pixel_contract = None
    canvas._projection_last_exact_metadata = None
    canvas._projection_presented_revision = -1
    canvas._invalidate_scene_cache(projection=False)
    gui = get_ident()
    started, release = Event(), Event()
    original = interactive_effects.apply_modifier_stack
    calls, gui_drafts = [], []

    def gated(*args, **kwargs):
        thread = get_ident()
        calls.append((thread, canvas._projection_exact if thread == gui else True))
        if thread == gui:
            gui_drafts.append(args[0].size())
        if get_ident() != gui:
            started.set()
            assert release.wait(5), "test worker was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", gated)
    try:
        widget_frame(canvas)
        assert started.wait(2)
        assert not any(thread == gui and exact for thread, exact in calls), "Raster presentation evaluated the exact filter inline"
        # Cold screen feedback may run a bounded transient kernel while the
        # native exact job is blocked; it cannot use the full source grid.
        assert all(size.width() * size.height() <= 32768 for size in gui_drafts)
        assert canvas._projection_interaction_preview is not None
        assert canvas._projection_frame_pending
        assert canvas._scene_dirty_full
        assert canvas._projection_presented_revision == -1
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qapp.processEvents()
        assert heartbeat, "Raster presentation did not return to the GUI event loop"
        # Another paint while the worker is blocked must also return promptly.
        widget_frame(canvas)
        assert canvas._projection_frame_pending
    finally:
        release.set()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        canvas._effect_jobs.poll()
        widget_frame(canvas)
        if not canvas._projection_frame_pending:
            break
        qapp.processEvents()
        time.sleep(.001)
    assert not canvas._projection_frame_pending
    assert canvas._scene_cache == expected
    assert not canvas._scene_dirty_full
    assert canvas._projection_presented_revision == canvas._document_projection.revision


def test_raster_frame_budget_continues_cached_image_on_next_paint(scene, monkeypatch):
    canvas, _, _ = scene
    original = canvas._render_document_tiles
    captures = []

    def budgeted(requests):
        captures.append(len(requests))
        canvas._projection_render_deadline = time.perf_counter() - 1
        canvas._projection_blocks_started = 1
        return original(requests)

    monkeypatch.setattr(canvas, "_render_document_tiles", budgeted)
    widget_frame(canvas)
    assert canvas._projection_frame_pending
    assert canvas._scene_dirty_full
    assert canvas._projection_yielded
    monkeypatch.setattr(canvas, "_render_document_tiles", original)
    deadline = time.monotonic() + 5
    while canvas._scene_dirty_full and time.monotonic() < deadline:
        widget_frame(canvas)
    assert not canvas._projection_frame_pending
    assert not canvas._scene_dirty_full
    assert captures

