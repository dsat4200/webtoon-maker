"""Exercise the retained path through real canvas paint/cache dispatch."""
import time

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtTest import QTest

from test_dirty_modifier_reuse import scene, pixels, exact_scene, watch_effect_work


def settled_projection(canvas, qapp):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        qapp.processEvents()
        canvas._effect_jobs.poll()
        canvas._ensure_scene_cache()
        projection = canvas._document_projection
        if (not canvas._effect_jobs.running and not canvas._effect_jobs.pending
                and projection.tiles and all(t.valid for t in projection.tiles.values())):
            return pixels(canvas._scene_cache)
        QTest.qWait(2)
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
    canvas._begin_stroke(QPointF(170, 100), 1.)
    for point in (QPointF(210, 102), QPointF(270, 105), QPointF(320, 108)):
        canvas._continue_stroke(point, 1.)
        actual = settled_projection(canvas, qapp)
        np.testing.assert_array_equal(actual[unaffected], before[unaffected])
    canvas._end_stroke()
    assert not calls, "An unchanged background effect was recomputed while drawing"
    assert not requests, "An unchanged background effect requested another worker"
    after = settled_projection(canvas, qapp)
    canvas._document_projection.clear()
    canvas._invalidate_scene_cache(projection=False)
    np.testing.assert_array_equal(settled_projection(canvas, qapp), after)
