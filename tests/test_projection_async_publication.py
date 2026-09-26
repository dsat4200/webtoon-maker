"""Pending exact work must not publish half an edit or leak canvas state."""
import time
from threading import Event
import pytest

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from test_projection_invalidation import scene


def frame(canvas):
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#242428"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    try:
        canvas._paint_projection_frame(painter, None, live_ink=False)
    finally:
        painter.end()
    return image


def test_cross_block_edit_publishes_only_when_all_regions_finish(scene, monkeypatch):
    canvas, obj, _ = scene
    canvas._projection_defer_effects = True
    before = frame(canvas)
    obj.x += 180
    canvas._invalidate_scene_cache()
    original = canvas._render_scene_layers
    def pending_second_block(*args, **kwargs):
        if canvas._projection_tile_key[1][1] == 1:
            raise ProjectionPending("deferred-block", "exact")
        return original(*args, **kwargs)
    monkeypatch.setattr(canvas, "_render_scene_layers", pending_second_block)
    during = frame(canvas)
    assert canvas._projection_frame_pending
    assert during == before
    # One block completed but it must not replace half the previous view.
    assert any(tile.valid for tile in canvas._document_projection.tiles.values())
    assert any(not tile.valid for tile in canvas._document_projection.tiles.values())
    assert not canvas._projection_exact
    assert canvas._effect_viewport_world is None
    assert canvas._show_on_top_phase is None
    assert canvas._active_top_plan is None
    monkeypatch.setattr(canvas, "_render_scene_layers", original)
    after = frame(canvas)
    assert not canvas._projection_frame_pending
    assert after != before
    canvas._document_projection.clear()
    canvas._projection_defer_effects = False
    assert frame(canvas) == after


def test_frame_budget_yields_without_falling_back_to_individual_tiles(scene, monkeypatch):
    canvas, _, _ = scene
    canvas._projection_defer_effects = True
    canvas._projection_render_deadline = time.perf_counter() - 1
    canvas._projection_blocks_started = 1
    original = canvas._render_document_region
    def forbidden(*args, **kwargs):
        raise AssertionError("Expired frame budget must return to the event loop")
    monkeypatch.setattr(canvas, "_render_document_region", forbidden)
    frame(canvas)
    assert canvas._projection_frame_pending and canvas._projection_yielded
    assert not canvas._effect_jobs.running and not canvas._effect_jobs.pending
    monkeypatch.setattr(canvas, "_render_document_region", original)
    canvas._projection_render_deadline = None
    frame(canvas)
    assert not canvas._projection_frame_pending


def test_failed_worker_keeps_finished_view_without_retrying_same_revision(scene, monkeypatch):
    canvas, _, _ = scene
    before = frame(canvas)
    canvas._projection_defer_effects = True
    canvas._invalidate_scene_cache()
    calls = []
    def failed(*args, **kwargs):
        calls.append(1)
        raise ProjectionFailed("bad-stage", "revision", "fixture failure")
    monkeypatch.setattr(canvas, "_render_scene_layers", failed)
    assert frame(canvas) == before
    assert canvas._projection_render_error == "fixture failure"
    assert frame(canvas) == before
    assert len(calls) == 1


def test_document_reset_drops_previous_presentation_and_errors(scene):
    canvas, _, _ = scene
    frame(canvas)
    assert canvas._projection_completed_view is not None
    canvas._projection_render_error = "old failure"
    canvas._clear_detached_input_state()
    assert canvas._projection_completed_view is None
    assert canvas._projection_render_error is None
    assert not canvas._projection_frame_pending


def test_canceled_running_job_does_not_block_current_capture(scene, monkeypatch):
    canvas, obj, _ = scene
    before = frame(canvas)
    obj.x += 180
    canvas._invalidate_scene_cache()
    canvas._projection_defer_effects = True
    canvas._projection_work_waiting = True
    token = Event()
    token.set()
    with monkeypatch.context() as patch:
        patch.setattr(canvas._effect_jobs, "running", ("old", "old", token, None, 0, True))
        assert frame(canvas) != before
        assert not canvas._projection_frame_pending


@pytest.mark.parametrize("gesture", ["_drawing", "_pen_contact_active", "_text_editing",
                                     "_transform_preview_quad", "_vector_gesture_mode"])
def test_active_edit_uses_immediate_exact_publication(scene, monkeypatch, gesture):
    canvas, obj, _ = scene
    assert not canvas._projection_can_defer_effects()
    before = frame(canvas)
    assert canvas._projection_can_defer_effects()
    original = getattr(canvas, gesture)
    monkeypatch.setattr(canvas, gesture, True)
    assert not canvas._projection_can_defer_effects()
    # In particular, a continuous stroke does not wait behind unrelated jobs
    # before its changed pixels enter the visible finished frame.
    monkeypatch.setattr(canvas, gesture, original)
    obj.x += 180
    canvas._invalidate_scene_cache()
    canvas._projection_work_waiting = True
    canvas._projection_defer_effects = False
    assert frame(canvas) != before
    assert not canvas._projection_frame_pending


def test_changed_view_configuration_has_no_compatible_fallback(scene):
    canvas, _, _ = scene
    frame(canvas)
    assert canvas._projection_can_defer_effects()
    canvas.chapter.background = "#ff555555"
    assert not canvas._projection_can_defer_effects()


def test_committed_edit_does_not_wait_for_background_publication(scene):
    canvas, _, _ = scene
    frame(canvas)
    assert canvas._projection_can_defer_effects()
    canvas._invalidate_scene_cache()
    assert not canvas._drawing
    assert not canvas._projection_can_defer_effects()
    frame(canvas)
    assert canvas._projection_can_defer_effects()


def test_retained_fallback_is_bounded_and_configuration_specific(scene):
    canvas, _, _ = scene
    frame(canvas)
    assert canvas._projection_completed_view is not None
    canvas.chapter.background = "#ff555555"
    canvas._projection_defer_effects = True
    canvas._projection_render_deadline = time.perf_counter() - 1
    canvas._projection_blocks_started = 1
    frame(canvas)
    assert canvas._projection_completed_view is None
    canvas._projection_render_deadline = None
    canvas._document_projection.budget = 1
    frame(canvas)
    assert canvas._projection_completed_view is None
