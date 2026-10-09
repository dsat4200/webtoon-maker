"""Exercise the retained path through real canvas paint/cache dispatch."""
import time

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest

from test_dirty_modifier_reuse import scene, pixels, exact_scene, watch_effect_work


def settled_projection(canvas, qapp, *, allow_native_contact=False):
    deadline = time.monotonic() + 8
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    while time.monotonic() < deadline:
        qapp.processEvents()
        image.fill(QColor("#242428"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        try:
            canvas._paint_ready_document_projection(painter)
        finally:
            painter.end()
        controller = canvas._scene_controller
        document = canvas._render_document_state()
        assert not controller.error, controller.error
        ready_preview = (document.live_preview and controller.preview is not None
                         and controller.preview[0] == document)
        # A prepared owned contact presents current native patches over retained
        # artwork while deliberately pausing detached preview work. Require the
        # actual presentation coverage and current gate, rather than accepting
        # an arbitrary pending frame or waiting for that paused preview.
        ready_contact = (allow_native_contact
                         and canvas._raster_feedback_contact_covered
                         and controller._contact_reuse_gate is not None
                         and controller._contact_reuse_gate is
                         controller._covered_contact_gate(document,
                             canvas.visible_document_rect().getRect()))
        if (controller.capture is None and not controller.scheduler.busy
                and (ready_contact or ready_preview or not canvas._projection_frame_pending)):
            return pixels(image)
        time.sleep(.002)
    raise AssertionError("Document projection did not reach exact pixels")


def test_projection_matches_existing_exact_scene(scene, qapp):
    canvas, _, _ = scene
    expected = exact_scene(canvas, qapp)
    canvas._document_projection_enabled = True
    canvas._invalidate_scene_cache()
    actual = settled_projection(canvas, qapp)
    np.testing.assert_array_equal(actual, expected)


def test_cached_camera_motion_never_requests_effect_work(scene, qapp, monkeypatch):
    canvas, _, _ = scene
    canvas._document_projection_enabled = True
    settled_projection(canvas, qapp)
    calls, requests, _ = watch_effect_work(canvas, monkeypatch)
    before = canvas._document_projection.renders
    for x, y, rotation, scale in ((194, 130, 0, 1), (194, 130, 15, 1),
                                  (194, 130, -30, .9), (192, 128, 0, 1)):
        canvas.center_x, canvas.center_y, canvas.rotation, canvas.scale = x, y, rotation, scale
        canvas._ensure_scene_cache()
    assert not calls
    assert not requests
    assert canvas._document_projection.renders == before


def test_paint_and_undo_update_projection_without_stale_pixels(scene, qapp):
    canvas, _, _ = scene
    canvas._document_projection_enabled = True
    expected = settled_projection(canvas, qapp)
    canvas._begin_stroke(QPointF(90, 100), 1.)
    canvas._continue_stroke(QPointF(140, 106), 1.)
    canvas._end_stroke()
    canvas._ensure_scene_cache()
    assert not canvas._effect_jobs.running and not canvas._effect_jobs.pending
    first = pixels(canvas._scene_cache)
    after = settled_projection(canvas, qapp)
    np.testing.assert_array_equal(first, after)
    assert np.any(after != expected)
    canvas.command_stack.undo()
    np.testing.assert_array_equal(settled_projection(canvas, qapp), expected)


def test_reset_rotation_reuses_document_tiles(scene, qapp):
    canvas, _, _ = scene
    canvas._document_projection_enabled = True
    canvas.rotation = 15
    settled_projection(canvas, qapp)
    before = canvas._document_projection.renders
    canvas.reset_rotation()
    canvas._ensure_scene_cache()
    assert canvas._document_projection.renders == before


def test_drawing_reuses_unchanged_effects_across_dirty_tile_subsets(scene, qapp, monkeypatch):
    canvas, reference, _ = scene
    # Make the unchanged reference straddle the 256px tile boundary. Different
    # brush positions then request different subsets of the same capture block.
    reference.x = 100
    canvas._document_projection_enabled = True
    before = settled_projection(canvas, qapp)
    calls, requests, _ = watch_effect_work(canvas, monkeypatch)
    unaffected = np.ones(before.shape[:2], dtype=bool)
    unaffected[80:130, 155:335] = False
    previous = before
    canvas._begin_stroke(QPointF(170, 100), 1.)
    for point in (QPointF(210, 102), QPointF(270, 105), QPointF(320, 108)):
        canvas._continue_stroke(point, 1.)
        actual = settled_projection(canvas, qapp, allow_native_contact=True)
        assert canvas._raster_feedback_contact_covered
        np.testing.assert_array_equal(actual[unaffected], before[unaffected])
        assert np.any(actual[~unaffected] != previous[~unaffected]), "Every native segment must be visible"
        previous = actual
    canvas._end_stroke()
    assert not calls, "An unchanged background effect was recomputed while drawing"
    assert not requests, "An unchanged background effect requested another worker"
    after = settled_projection(canvas, qapp)
    assert not canvas._projection_frame_pending
    assert not canvas._projection_provisional_visible
    np.testing.assert_array_equal(after, actual)
    canvas._document_projection.clear()
    canvas._invalidate_scene_cache(projection=False)
    np.testing.assert_array_equal(settled_projection(canvas, qapp), after)
