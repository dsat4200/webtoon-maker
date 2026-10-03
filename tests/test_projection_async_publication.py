"""Pending exact work must not publish half an edit or leak canvas state."""
import time
from threading import Event
import pytest

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from comic_editor.ui.document_presentation import PresentedTile
from comic_editor.ui.document_projection import ProjectionAddress, ProjectionRequest
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


def test_dirty_stroke_seed_requires_complete_camera_coverage_in_every_pass():
    from comic_editor.ui.document_projection_features import DocumentProjectionFeatures
    covers = DocumentProjectionFeatures._completed_projection_covers
    image = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
    def tile(x, width):
        return PresentedTile(x, image, QRectF(x, 0, width, 20))
    complete = ('config', [(None, [tile(0, 10), tile(10, 10)])], 7)
    assert covers(complete, QRectF(0, 0, 20, 20))
    assert covers(complete, QRectF(3, 2, 15, 10))
    assert not covers(complete, QRectF(5, 0, 20, 20))
    gap = ('config', [(None, [tile(0, 9), tile(10, 10)])], 7)
    assert not covers(gap, QRectF(0, 0, 20, 20))
    passes = ('config', [('base', complete[1][0][1]), ('top', [tile(0, 10)])], 7)
    assert not covers(passes, QRectF(0, 0, 20, 20))


def test_first_stroke_after_camera_pan_captures_new_view_instead_of_blank_seed(scene, monkeypatch):
    canvas, obj, _ = scene
    frame(canvas)
    completed = canvas._projection_completed_view
    canvas.set_selection('object', obj.object_id)
    canvas.center_x += 300
    canvas._stroke_dirty_world = QRectF(obj.x, obj.y, 5, 5)
    seen = []
    original = canvas._render_service.render_region
    def capture(document, request):
        seen.append(request.region)
        return original(document, request)
    monkeypatch.setattr(canvas._render_service, 'render_region', capture)
    assert canvas._capture_stroke_projection_preview()
    assert seen == [canvas.visible_document_rect().getRect()]
    assert canvas._projection_completed_view is completed


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


def test_pan_presents_finished_new_tiles_while_other_blocks_wait(scene, monkeypatch):
    canvas, _, _ = scene
    frame(canvas)
    previous = canvas._projection_completed_view
    assert previous is not None
    address = ProjectionAddress(0, 7, 0)
    assert all(tile.key[1] != address for tile in previous[1][0][1])
    request = ProjectionRequest(address)
    image = QImage(request.pixel_size, request.pixel_size, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    finished = PresentedTile((None, address), image, request.world_rect, request.source_rect)
    canvas.center_x = 1700.
    canvas._projection_defer_effects = True

    def partial(_phase):
        canvas._projection_collection_complete = False
        return [finished]

    monkeypatch.setattr(canvas, "_collect_document_projection", partial)
    batch = canvas._projection_phase_batch((None,))
    assert canvas._projection_frame_pending
    assert finished in batch[0][1]
    assert any(tile.key != finished.key for tile in batch[0][1])
    assert canvas._projection_completed_view is previous

    # Waiting for an effect keeps the new tile visible without collecting it again.
    monkeypatch.setattr(canvas, "_collect_document_projection", lambda _phase: [])
    again = canvas._projection_phase_batch((None,))
    assert finished in again[0][1]

    # A content edit must not mix pixels from different revisions.
    canvas._document_projection.invalidate()
    after_edit = canvas._projection_phase_batch((None,))
    assert finished not in after_edit[0][1]


def test_initial_load_presents_finished_tiles_before_full_view(scene, monkeypatch):
    canvas, _, _ = scene
    assert canvas._projection_can_defer_effects()
    address = ProjectionAddress(0, 3, 0)
    request = ProjectionRequest(address)
    image = QImage(request.pixel_size, request.pixel_size, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("blue"))
    finished = PresentedTile((None, address), image, request.world_rect, request.source_rect)
    canvas._projection_defer_effects = True

    def partial(_phase):
        canvas._projection_collection_complete = False
        return [finished]

    monkeypatch.setattr(canvas, "_collect_document_projection", partial)
    batch = canvas._projection_phase_batch((None,))
    assert batch == [(None, [finished])]
    assert canvas._projection_frame_pending
    assert canvas._projection_completed_view is None

    monkeypatch.setattr(canvas, "_collect_document_projection", lambda _phase: [])
    assert canvas._projection_phase_batch((None,)) == batch


def test_missing_capture_blocks_start_near_view_center(scene, monkeypatch):
    canvas, _, _ = scene
    canvas.center_x = 1400.
    requests = canvas._document_projection.requests(QRectF(0, 0, 2048, 256), 1.)
    order = []

    def capture(_bounds, _scale, size, key, **_kwargs):
        order.append(key)
        image = QImage(size, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor("white"))
        return image, True

    monkeypatch.setattr(canvas, "_render_document_region", capture)
    canvas._render_document_tiles(requests)
    assert [key[1] for key in order] == [1, 0]


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
    assert canvas._projection_stroke_preview is None


def test_stroke_preview_shows_fresh_ink_without_waiting_or_publishing_draft(scene, qapp, monkeypatch):
    from threading import get_ident
    from comic_editor.core.models import OutlineModifier
    from comic_editor.ui import interactive_effects
    canvas, obj, _ = scene
    effect = OutlineModifier(thickness=3, blur_radius=4, blur_strength=50)
    canvas.chapter.add_modifier(effect, [('object', obj.object_id)])
    before = frame(canvas)
    completed = canvas._projection_completed_view
    gui, started, release = get_ident(), Event(), Event()
    original = interactive_effects.apply_modifier_stack
    def gated(*args, **kwargs):
        if get_ident() != gui:
            started.set()
            assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(interactive_effects, 'apply_modifier_stack', gated)
    try:
        canvas._begin_stroke(QPointF(900, 220), 1.)
        assert canvas._capture_stroke_projection_preview()
        assert started.wait(2)
        assert canvas._projection_completed_view is completed
        assert canvas._projection_presented_revision != canvas._document_projection.revision
        preview = canvas._projection_stroke_preview
        canvas._projection_frame_pending = True
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('#242428'))
        painter = QPainter(image)
        try:
            canvas._paint_projection_frame(painter, None, live_ink=False, stroke_only=True)
        finally:
            painter.end()
        assert image != before
        assert canvas._projection_stroke_preview is preview
        # Export/synchronous requests retain the full-quality reference.
        exact = frame(canvas)
        assert not canvas._projection_frame_pending
        assert canvas._projection_stroke_preview is None
        assert exact != before
    finally:
        release.set()
        canvas._end_stroke()
    # The final tail dab needs a fresh preview, then may finish asynchronously.
    assert canvas._capture_stroke_projection_preview()
    assert canvas._projection_can_defer_effects()
    canvas._projection_defer_effects = False
    final = frame(canvas)
    canvas._document_projection.clear()
    assert frame(canvas) == final


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
        patch.setattr(canvas._effect_jobs, "_running", {0: ("old", "old", token, None, 0, True)})
        assert frame(canvas) != before
        assert not canvas._projection_frame_pending


@pytest.mark.parametrize("gesture", ["_drawing", "_pen_contact_active", "_text_editing",
                                     "_transform_preview_quad", "_vector_gesture_mode"])
def test_active_edit_uses_immediate_exact_publication(scene, monkeypatch, gesture):
    canvas, obj, _ = scene
    assert canvas._projection_can_defer_effects()
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
