"""Radial handle edits stay interactive through exact GPU projection work."""
import math
import time
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPointingDevice, QTabletEvent
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ImageObject, ParameterMaskBinding, RadialBlurModifier, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls


@pytest.fixture
def scene(qapp):
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False,
                                        grid_overlay_visible=False))
    chapter = ChapterDocument(width=180, height=160, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 180, 160))
    page.fill_color, page.border_width = None, 0
    obj = chapter.add_object(page.layer_id, ImageObject(pixel_width=180, pixel_height=160,
        placement_mode="free", transform_quad=[(0, 0), (180, 0), (180, 160), (0, 160)]))
    modifier = RadialBlurModifier(center=(90, 80), angle=24)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    canvas.setMinimumSize(1, 1)
    canvas.resize(360, 320)
    canvas.set_document(chapter, TileStore())
    image = QImage(180, 160, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.fillRect(QRectF(110, 40, 24, 50), QColor("#ff238a"))
    painter.end()
    canvas.images.put_decoded(obj.object_id, "source.png", b"", image)
    canvas.center_x, canvas.center_y, canvas.scale = 90, 80, 1
    canvas.set_selection("object", obj.object_id)
    canvas.modifier_mode, canvas.active_modifier_id = True, modifier.modifier_id
    canvas._document_projection_enabled = True
    yield canvas, modifier
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def frame(canvas, deferred=False):
    canvas._projection_defer_effects = deferred
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#242428"))
    painter = QPainter(image)
    try:
        canvas._paint_projection_frame(painter, None, live_ink=False)
    finally:
        painter.end()
    return image


@pytest.mark.parametrize("pen", [False, True])
def test_changed_handle_and_release_defer_exact_work_and_publish_final_pixels(scene, monkeypatch, pen):
    from comic_editor.ui import radial_blur
    canvas, modifier = scene
    before = frame(canvas)
    center, end = canvas._radial_handle_points(modifier)
    assert canvas._begin_modifier_handle(end)
    canvas._pen_contact_active = pen
    target = center + QPointF(72 / math.sqrt(2), 72 / math.sqrt(2))
    assert canvas._move_modifier_handle(target)
    canvas._flush_radial_handle()
    assert modifier.angle == pytest.approx(90)
    assert canvas._projection_can_defer_effects(), "A changed radial edit must not run on the UI thread"

    entered, release = Event(), Event()
    gui = get_ident()
    original = radial_blur.radial_blur
    calls = []

    def blocked(*args, **kwargs):
        assert get_ident() != gui, "Radial integration blocked the UI thread"
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(radial_blur, "radial_blur", blocked)
    try:
        assert frame(canvas, canvas._projection_can_defer_effects()) == before
        assert entered.wait(2)
        assert canvas._projection_frame_pending
        assert canvas._finish_modifier_handle()
        canvas._pen_contact_active = False
        assert canvas._projection_can_defer_effects(), "Release must also stay asynchronous"
        assert frame(canvas, canvas._projection_can_defer_effects()) == before
        assert len(calls) == 1
    finally:
        release.set()
    canvas._effect_jobs.running[3].result(timeout=10)
    canvas._effect_jobs.poll()
    after = frame(canvas, canvas._projection_can_defer_effects())
    assert after != before and not canvas._projection_frame_pending
    assert frame(canvas) == after
    assert len(calls) == 1
    monkeypatch.setattr(radial_blur, "radial_blur", original)
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].angle == 24
    assert frame(canvas) == before
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[modifier.modifier_id].angle == pytest.approx(90)
    assert frame(canvas) == after


def test_radial_drag_cancels_obsolete_work_and_noop_keeps_revision(scene):
    canvas, modifier = scene
    frame(canvas)
    center, end = canvas._radial_handle_points(modifier)
    assert canvas._begin_modifier_handle(end)
    revision = canvas._document_projection.revision
    canvas._move_modifier_handle(end)
    canvas._flush_radial_handle()
    assert canvas._document_projection.revision == revision
    gate, entered = Event(), Event()

    def old(cancelled):
        entered.set()
        gate.wait(5)
        return QImage(1, 1, QImage.Format_ARGB32_Premultiplied)

    try:
        canvas._effect_jobs.request("old", "old", old, 4, require_exact=True)
        assert entered.wait(2)
        job = canvas._effect_jobs.running
        canvas._projection_work_waiting = True
        canvas._move_modifier_handle(center + QPointF(0, 72))
        canvas._flush_radial_handle()
        assert job[2].is_set()
        assert not canvas._projection_work_waiting
        assert modifier.angle == 180
        assert canvas._projection_can_defer_effects()
    finally:
        gate.set()
    job[3].result(timeout=5)
    canvas._effect_jobs.poll()
    assert canvas._effect_jobs.result("old", "old") is None
    canvas._finish_modifier_handle()
    canvas._invalidate_scene_cache()
    assert not canvas._projection_can_defer_effects(), "Unrelated edits retain their publication policy"


def test_angle_card_sync_and_escape_restore_pending_drag(scene):
    canvas, modifier = scene
    controls = ModifierControls(canvas)
    controls.refresh()
    controls.activate_modifier(modifier.modifier_id)
    card = controls._cards[modifier.modifier_id]
    center, end = canvas._radial_handle_points(modifier)
    assert canvas._begin_modifier_handle(end)
    canvas._move_modifier_handle(center + QPointF(0, 72))
    canvas._finish_modifier_handle()
    assert all(control.value() == 180 for control in card._parameter_controls["angle"])
    assert canvas.command_stack.top_undo_command.label == "Edit radial blur"
    center, end = canvas._radial_handle_points(modifier)
    assert canvas._begin_modifier_handle(end)
    canvas._move_modifier_handle(center + QPointF(-72, 0))
    QTest.keyClick(canvas, Qt.Key_Escape)
    assert canvas.chapter.modifiers[modifier.modifier_id].angle == 180
    assert canvas._radial_handle_pending is None
    assert not canvas._radial_handle_timer.isActive()
    assert canvas._radial_preview_current()
    controls.deleteLater()


def test_handle_gesture_and_cancel_do_not_serialize_unrelated_document(scene, monkeypatch):
    from comic_editor.core.models import RasterObject
    canvas, modifier = scene
    unrelated = canvas.chapter.add_object(canvas.chapter.root_page_ids[0], RasterObject(visible=False))
    monkeypatch.setattr(canvas.chapter, 'to_dict', lambda: pytest.fail('Full chapter copied for a handle'))
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated object copied for a handle'))
    center, end = canvas._radial_handle_points(modifier)
    assert canvas._begin_modifier_handle(end)
    canvas._move_modifier_handle(center + QPointF(0, 72))
    assert canvas._finish_modifier_handle()
    assert modifier.angle == 180
    assert len(canvas.command_stack._undo) == 1
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].angle == 24
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[modifier.modifier_id].angle == 180
    assert canvas.chapter.objects[unrelated.object_id] is unrelated
    canvas.active_modifier_id = modifier.modifier_id
    center, end = canvas._radial_handle_points(canvas.chapter.modifiers[modifier.modifier_id])
    assert canvas._begin_modifier_handle(end)
    canvas._move_modifier_handle(center + QPointF(-72, 0))
    QTest.keyClick(canvas, Qt.Key_Escape)
    assert canvas.chapter.modifiers[modifier.modifier_id].angle == 180
    assert len(canvas.command_stack._undo) == 1


@pytest.mark.parametrize("pen", [False, True])
@pytest.mark.parametrize("handle", [0, 1], ids=["center", "angle"])
def test_actual_pointer_release_applies_last_position_once(scene, pen, handle):
    canvas, modifier = scene
    start = canvas._radial_handle_points(modifier)[handle].toPoint()
    center = canvas._radial_handle_points(modifier)[0]
    end = (start + QPointF(20, 10).toPoint() if handle == 0 else
           (center + QPointF(0, 72)).toPoint())

    def tablet(kind, point, pressed):
        event = QTabletEvent(kind, QPointingDevice.primaryPointingDevice(), QPointF(point),
            QPointF(canvas.mapToGlobal(point)), 1. if pressed else 0.,
            0., 0., 0., 0., 0., Qt.NoModifier, Qt.LeftButton,
            Qt.LeftButton if pressed else Qt.NoButton)
        QCoreApplication.sendEvent(canvas, event)

    if pen:
        tablet(QEvent.TabletPress, start, True)
        tablet(QEvent.TabletRelease, end, False)
    else:
        QTest.mousePress(canvas, Qt.LeftButton, pos=start)
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=end)
    assert modifier.center == ((110, 90) if handle == 0 else (90, 80))
    assert modifier.angle == (24 if handle == 0 else 180)
    assert canvas._modifier_handle_drag is None
    assert canvas._radial_handle_pending is None
    assert canvas.command_stack.top_undo_command.label == "Edit radial blur"
    canvas.command_stack.undo()
    restored = canvas.chapter.modifiers[modifier.modifier_id]
    assert restored.center == (90, 80) and restored.angle == 24


@pytest.mark.parametrize("pen", [False, True])
@pytest.mark.parametrize("cache_budget", [64*1024*1024, 1], ids=["ordinary-cache", "evicted-lru"])
def test_intensity_gradient_drag_release_and_undo_reuse_integration(
        scene, monkeypatch, pen, cache_budget, wait_scene, qapp):
    from comic_editor.ui import radial_blur
    canvas, modifier = scene
    canvas._modifier_render_cache_budget = cache_budget
    mask = ToneMask(saved=True, name="Radial intensity")
    canvas.chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas._mask_gradient_press(QPointF(60, 80))
    canvas._mask_gradient_move(QPointF(150, 80))
    canvas._finish_mask_gradient()
    calls = []
    gui = get_ident()
    original = radial_blur.radial_blur
    def integration(*args, **kwargs):
        assert get_ident() != gui, 'Widget radial integration ran on the GUI'
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(radial_blur, 'radial_blur', integration)

    def ready_document_frame():
        # Exclude gradient handles/blue mask overlays: changed artwork must
        # prove intensity remixing, rather than merely moved editing gizmos.
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('#242428'))
        painter = QPainter(image)
        try:
            canvas._paint_ready_document_projection(painter, live_ink=False)
        finally:
            painter.end()
        return image

    wait_scene(canvas)
    before = ready_document_frame()
    assert len(calls) == 1
    entered, release = Event(), Event()
    scheduler = canvas._scene_controller.scheduler
    evaluate = scheduler._evaluate_admitted
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return evaluate(*args, **kwargs)
    with monkeypatch.context() as worker_patch:
        worker_patch.setattr(scheduler, '_evaluate_admitted', held)
        try:
            canvas._mask_gradient_press(QPointF(150, 80))
            canvas._pen_contact_active = pen
            canvas._mask_gradient_move(QPointF(100, 80))
            pending = ready_document_frame()
            deadline = time.monotonic() + 2
            while not entered.is_set() and time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(.002)
            assert entered.is_set() and scheduler.busy
            assert canvas._projection_frame_pending
            assert pending == before, 'Pending work must retain the last exact artwork'
            assert canvas.active_mask_gradient().line_field.geometry.nodes[-1].position == (100., 80.)
            assert len(calls) == 1
        finally:
            release.set()
        wait_scene(canvas)
    during = ready_document_frame()
    assert during != before and not canvas._projection_frame_pending
    assert len(calls) == 1, "Intensity gradient edits must only remix the cached angular integration"
    canvas._finish_mask_gradient()
    canvas._pen_contact_active = False
    wait_scene(canvas)
    assert ready_document_frame() == during
    assert len(calls) == 1
    canvas.command_stack.undo()
    wait_scene(canvas)
    assert ready_document_frame() == before
    assert len(calls) == 1
    canvas.command_stack.redo()
    wait_scene(canvas)
    assert ready_document_frame() == during
    assert len(calls) == 1


def test_regular_radial_settings_defer_but_ink_contact_does_not(scene):
    canvas, modifier = scene
    frame(canvas)
    modifier.angle = 50
    canvas.documentChanged.emit(None)
    assert canvas._projection_can_defer_effects()
    canvas._pen_contact_active = True
    assert not canvas._projection_can_defer_effects()
    canvas._pen_contact_active = False
    canvas._drawing = True
    assert not canvas._projection_can_defer_effects()
