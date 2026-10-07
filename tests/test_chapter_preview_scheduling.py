"""Navigator renders are deferred without dropping changes or gesture state."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from comic_editor.ui.preview import ChapterPreview


class PreviewCanvas(QWidget):
    documentChanged = Signal(object)
    visualChanged = Signal(object)
    derivedResultReady = Signal()
    hierarchyChanged = Signal()
    cameraChanged = Signal()
    interactionFinished = Signal()

    def __init__(self):
        super().__init__()
        self.chapter = SimpleNamespace(width=100, height=1000)
        self._interactive_render = False
        self._effect_preview_channel = "canvas"
        self._wheel_zoom_timer = QTimer(self)
        self.calls = []
        self.color = QColor("red")
        self.fraction = (0., .1)

    def viewport_fraction(self):
        return self.fraction

    def render_preview(self, image, clip):
        self.calls.append(None if clip is None else QRect(clip))
        assert self._interactive_render
        assert self._effect_preview_channel == "navigator"
        painter = QPainter(image)
        painter.fillRect(image.rect() if clip is None else clip, self.color)
        painter.end()


@pytest.fixture
def navigator(qapp):
    canvas = PreviewCanvas()
    preview = ChapterPreview(canvas)
    # Single-band fixtures isolate scheduling from the chunking tests below.
    preview.REFRESH_BAND_HEIGHT = 1000
    preview.resize(92, 300)
    preview.show()
    qapp.processEvents()
    yield canvas, preview
    preview.close()
    preview.deleteLater()
    canvas.deleteLater()


def test_paint_does_not_render_dirty_chapter_and_idle_timer_catches_up(navigator):
    canvas, preview = navigator
    assert not preview.grab().isNull()
    assert canvas.calls == []
    for _ in range(100):
        QTest.qWait(10)
        if canvas.calls:
            break
    assert canvas.calls == [None]
    assert not preview._cache.isNull()
    assert not canvas._interactive_render
    assert canvas._effect_preview_channel == "canvas"

    for y in range(300, 450):
        canvas.visualChanged.emit(QRectF(20, y, 10, 1))
        preview.grab()
    assert len(canvas.calls) == 1
    assert len(preview._dirty_bands) == 1
    for _ in range(100):
        QTest.qWait(10)
        if len(canvas.calls) == 2:
            break
    assert len(canvas.calls) == 2
    assert canvas.calls[-1].height() < preview._cache.height()


@pytest.mark.parametrize("field,value", [
    ("_drawing", True), ("_pen_contact_active", True),
    ("_nav_mode", "pan"), ("_touch_points", [1, 2]),
    ("_vector_gesture_mode", "pencil"), ("_transform_drag_mode", "translate"),
    ("_bound_drag_mode", "translate"), ("_selection_transform_mode", "scale"),
    ("_fill_gesture_active", True), ("_cage_drag", {"mode": "point"}),
    ("_modifier_handle_drag", {"handle": "center"}),
    ("_mask_gradient_drag", {"handle": "end"}),
    ("_text_property_drag", {"property": "size"}),
    ("_shape_property_drag", {"property": "width"}),
    ("_active_gradient_control", ("point", "end")),
    ("_text_editing", True), ("_text_dragging", True),
    ("_free_text_drag", {"mode": "handle"}), ("_text_placement", {"new": True}),
    ("_modifier_parameter_drag_id", "keyboard-parameter"),
    ("_overlay_color_preview", {"color": "#FF123456"}),
])
def test_held_gesture_defers_even_without_further_packets(navigator, field, value):
    canvas, preview = navigator
    setattr(canvas, field, value)
    preview._refresh_cache()
    assert not canvas.calls
    assert preview._dirty_full and preview._refresh_timer.isActive()
    setattr(canvas, field, None)
    canvas.interactionFinished.emit()
    assert not canvas.calls, "Gesture release must not synchronously render the navigator"
    preview._refresh_cache()
    assert canvas.calls == [None]


def test_wheel_zoom_waits_for_settle(navigator):
    canvas, preview = navigator
    canvas._wheel_zoom_timer.start(1000)
    preview._refresh_cache()
    assert not canvas.calls
    canvas._wheel_zoom_timer.stop()
    preview._refresh_cache()
    assert canvas.calls == [None]


def test_all_canvas_live_previews_defer_navigator_work_without_mouse_contact(navigator):
    canvas, preview = navigator
    canvas._projection_has_live_preview = lambda: True
    preview._refresh_cache()
    assert not canvas.calls and preview._dirty_full
    canvas._projection_has_live_preview = lambda: False
    canvas.interactionFinished.emit()
    assert not canvas.calls
    preview._refresh_cache()
    assert canvas.calls == [None]


def test_hidden_navigator_keeps_latest_changes_without_running_timer(navigator):
    canvas, preview = navigator
    preview._refresh_cache()
    preview.hide()
    canvas.visualChanged.emit(QRectF(10, 300, 20, 30))
    canvas.visualChanged.emit(QRectF(10, 700, 20, 30))
    assert len(preview._dirty_bands) == 1
    assert not preview._refresh_timer.isActive()
    preview._refresh_cache()
    assert len(canvas.calls) == 1
    preview.show()
    assert preview._refresh_timer.isActive()
    preview._refresh_cache()
    assert len(canvas.calls) == 2
    assert not preview._dirty_bands


def test_partial_refresh_preserves_untouched_pixels_and_resize_rebuilds(navigator):
    canvas, preview = navigator
    preview._refresh_cache()
    before = preview._cache.copy()
    canvas.color = QColor("blue")
    canvas.documentChanged.emit(QRectF(0, 350, 100, 50))
    clip = QRect(preview._dirty_bands[0])
    preview._refresh_cache()
    for y in range(before.height()):
        assert preview._cache.pixelColor(0, y) == (
            QColor("blue") if clip.contains(0, y) else before.pixelColor(0, y)
        )
    preview.resize(92, 400)
    preview.grab()
    assert len(canvas.calls) == 2
    preview._refresh_cache()
    assert canvas.calls[-1] is None
    assert preview._cache.size() == preview.content_rect().size()


def test_camera_handle_moves_during_drawing_without_thumbnail_render(navigator):
    canvas, preview = navigator
    preview._refresh_cache()
    before = preview.grab().toImage()
    canvas._drawing = True
    canvas.visualChanged.emit(QRectF(0, 200, 100, 50))
    canvas.fraction = (.7, .1)
    canvas.cameraChanged.emit()
    after = preview.grab().toImage()
    assert before != after
    assert len(canvas.calls) == 1
    assert preview._dirty_bands


def test_change_during_refresh_is_retained(navigator):
    canvas, preview = navigator
    original = canvas.render_preview
    def render(image, clip):
        original(image, clip)
        canvas.documentChanged.emit(QRectF(10, 500, 20, 30))
    canvas.render_preview = render
    preview._refresh_cache()
    assert preview._dirty_full
    assert preview._cache.isNull(), "A stale in-progress frame must not be published"
    assert preview._refresh_timer.isActive()


def finish_build(preview):
    for _ in range(100):
        preview._refresh_cache()
        if preview._pending_image.isNull():
            return
    pytest.fail("Navigator refresh did not finish")


def test_full_build_yields_between_bands_and_publishes_only_when_complete(navigator):
    canvas, preview = navigator
    preview._refresh_cache()
    original = preview._cache.copy()
    canvas.color = QColor("blue")
    preview.REFRESH_BAND_HEIGHT = 16
    canvas.documentChanged.emit(None)
    canvas.calls.clear()
    preview._refresh_cache()
    assert len(canvas.calls) == 1 and canvas.calls[0].height() == 16
    assert preview._cache == original
    assert not preview._pending_image.isNull()
    assert preview._refresh_timer.isActive()
    canvas._drawing = True
    preview._refresh_cache()
    assert len(canvas.calls) == 1
    canvas._drawing = False
    canvas.interactionFinished.emit()
    finish_build(preview)
    expected = QImage(original.size(), original.format())
    expected.fill(QColor("blue"))
    assert preview._cache == expected
    assert all(clip.height() <= 16 for clip in canvas.calls)


def test_invalidated_build_restarts_without_mixed_revision_pixels(navigator):
    canvas, preview = navigator
    preview.REFRESH_BAND_HEIGHT = 16
    preview._refresh_cache()
    assert not preview._pending_image.isNull()
    canvas.color = QColor("green")
    canvas.visualChanged.emit(QRectF(0, 0, 100, 20))
    assert preview._pending_image.isNull() and preview._dirty_full
    finish_build(preview)
    assert all(preview._cache.pixelColor(0, y) == QColor("green")
               for y in range(preview._cache.height()))


def test_document_swap_cancels_old_chunks_even_without_a_signal(navigator):
    canvas, preview = navigator
    preview._refresh_cache()
    old = canvas.chapter
    preview.REFRESH_BAND_HEIGHT = 16
    canvas.documentChanged.emit(None)
    preview._refresh_cache()
    canvas.chapter = SimpleNamespace(width=200, height=1200)
    canvas.color = QColor("blue")
    preview._refresh_cache()
    assert preview._pending_chapter is canvas.chapter
    assert preview._cache_chapter is old
    finish_build(preview)
    assert preview._cache_chapter is canvas.chapter
    assert preview._cache.size() == preview.content_rect().size()
    assert all(preview._cache.pixelColor(0, y) == QColor("blue")
               for y in range(preview._cache.height()))


def test_document_swap_during_render_restarts_without_publishing(navigator):
    canvas, preview = navigator
    original = canvas.render_preview
    def render(image, clip):
        original(image, clip)
        canvas.chapter = SimpleNamespace(width=200, height=1200)
        canvas.color = QColor("blue")
        canvas.render_preview = original
    canvas.render_preview = render
    preview._refresh_cache()
    assert preview._cache.isNull() and preview._pending_image.isNull()
    assert preview._dirty_full and preview._refresh_timer.isActive()
    finish_build(preview)
    assert preview._cache_chapter is canvas.chapter


def test_partial_build_keeps_every_invalidated_band_when_restarted(navigator):
    canvas, preview = navigator
    preview._refresh_cache()
    before = preview._cache.copy()
    preview.REFRESH_BAND_HEIGHT = 16
    canvas.color = QColor("blue")
    canvas.visualChanged.emit(QRectF(0, 200, 100, 300))
    first = QRect(preview._dirty_bands[0])
    preview._refresh_cache()
    assert preview._cache == before
    canvas.visualChanged.emit(QRectF(0, 750, 100, 30))
    assert preview._dirty_bands[0].contains(first)
    affected = QRect(preview._dirty_bands[0])
    finish_build(preview)
    for y in range(before.height()):
        assert preview._cache.pixelColor(0, y) == (
            QColor("blue") if affected.contains(0, y) else before.pixelColor(0, y))
