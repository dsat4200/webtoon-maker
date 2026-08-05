from __future__ import annotations

import gc

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, VectorDrawingObject,
    VectorStroke, VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.vector_geometry import fit_freehand
from comic_editor.ui.canvas import (
    CanvasWidget, GpuCanvasWidget, RasterCanvasWidget, ToolKind,
)
from comic_editor.ui.preview import ChapterPreview


class _WheelEvent:
    def __init__(
        self, position: QPointF, delta: float,
        modifiers=Qt.ControlModifier,
    ) -> None:
        self._position = QPointF(position)
        self._delta = float(delta)
        self._modifiers = modifiers
        self.accepted = False

    def position(self) -> QPointF:
        return QPointF(self._position)

    def angleDelta(self) -> QPointF:  # noqa: N802
        return QPointF(0, self._delta)

    def modifiers(self):
        return self._modifiers

    def accept(self) -> None:
        self.accepted = True


def _document_canvas(settings: EditorSettings | None = None):
    chapter = ChapterDocument()
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1200, 1200)
    )
    layer = chapter.add_layer(
        page.layer_id, "Ink", BoundGeometry.rectangle(0, 0, 1200, 1200)
    )
    canvas = CanvasWidget(settings or EditorSettings(snap_to_grid=False))
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    canvas.center_x = 400
    canvas.center_y = 300
    canvas.scale = 1.0
    return canvas, chapter, layer


def test_paint_segment_batches_variable_width_and_skips_empty_erase():
    store = TileStore()
    before: dict = {}
    dirty = store.paint_segment(
        "ink", QPointF(20, 80), QPointF(180, 80),
        4, 24, QColor("#111111"), 0.3, 1.0,
        before=before,
    )
    assert not dirty.isEmpty()
    assert before == {(0, 0): None}
    image = store.tile("ink", (0, 0))

    def painted_height(x: int) -> int:
        values = [
            y for y in range(image.height())
            if image.pixelColor(x, y).alpha() > 0
        ]
        return max(values) - min(values) + 1 if values else 0

    assert painted_height(170) > painted_height(35)
    assert image.pixelColor(170, 80).alpha() > image.pixelColor(35, 80).alpha()

    original = QImage(image)
    changed_before: dict = {}
    store.paint_segment(
        "ink", QPointF(80, 140), QPointF(160, 140),
        12, 12, QColor("#222222"), before=changed_before,
    )
    assert changed_before[(0, 0)] is not None
    assert changed_before[(0, 0)].cacheKey() == original.cacheKey()

    empty = TileStore()
    erased_before: dict = {}
    erased = empty.paint_segment(
        "missing", QPointF(0, 0), QPointF(200, 200),
        40, 40, QColor("#000000"), erase=True,
        before=erased_before,
    )
    assert erased.isEmpty()
    assert erased_before == {}
    assert empty.object_tiles("missing") == {}


def test_round_and_square_erasers_keep_their_distinct_hit_shapes():
    source = TileStore()
    source.paint_dab(
        "ink", QPointF(60, 60), 100, QColor("#111111"), square=True
    )
    round_store = TileStore()
    square_store = TileStore()
    round_store.replace_object_tiles("ink", source.object_tiles("ink"))
    square_store.replace_object_tiles("ink", source.object_tiles("ink"))

    round_store.paint_dab(
        "ink", QPointF(60, 60), 40, QColor("#000000"),
        erase=True, square=False, antialias=False,
    )
    square_store.paint_dab(
        "ink", QPointF(60, 60), 40, QColor("#000000"),
        erase=True, square=True, antialias=False,
    )
    assert round_store.tile("ink", (0, 0)).pixelColor(78, 78).alpha() > 0
    assert square_store.tile("ink", (0, 0)).pixelColor(78, 78).alpha() == 0


def test_cached_bounds_prune_erased_tile_and_visible_lookup_is_direct():
    store = TileStore()
    store.paint_dab("ink", QPointF(20, 20), 20, QColor("#111111"))
    store.paint_dab("ink", QPointF(600, 20), 20, QColor("#111111"))
    assert store.content_bounds("ink") is not None
    visible = list(store.iter_tiles("ink", QRectF(0, 0, 100, 100)))
    assert [key for key, _image in visible] == [(0, 0)]

    before: dict = {}
    store.paint_dab(
        "ink", QPointF(20, 20), 80, QColor("#000000"),
        erase=True, before=before,
    )
    store.prune_empty("ink", set(before))
    assert store.tile("ink", (0, 0)) is None
    assert store.tile("ink", (2, 0)) is not None


def test_raster_stroke_coalesces_visual_updates_and_commits_once(qapp):
    canvas, chapter, layer = _document_canvas()
    raster = chapter.add_object(
        layer.layer_id, RasterObject(interaction_rect=(0, 0, 500, 500))
    )
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    visual: list[QRectF] = []
    committed: list[QRectF] = []
    canvas.visualChanged.connect(visual.append)
    canvas.documentChanged.connect(committed.append)

    canvas._begin_stroke(QPointF(100.25, 100.75), 0.4)
    canvas._continue_stroke(QPointF(180.5, 130.25), 0.8)
    qapp.processEvents()
    assert visual and committed == []
    canvas._end_stroke()
    qapp.processEvents()
    assert len(committed) == 1
    assert committed[0].contains(QPointF(180.5, 130.25))
    assert canvas.command_stack.can_undo
    painted = canvas.tiles.content_bounds(raster.object_id)
    assert painted is not None
    committed_frame = raster.interaction_rect
    canvas.command_stack.undo()
    assert canvas.tiles.content_bounds(raster.object_id) is None
    canvas.command_stack.redo()
    assert canvas.tiles.content_bounds(raster.object_id) is not None
    assert raster.interaction_rect == committed_frame


def test_detaching_document_restores_gc_after_active_stroke(qapp):
    was_enabled = gc.isenabled()
    gc.enable()
    canvas, chapter, layer = _document_canvas()
    raster = chapter.add_object(layer.layer_id, RasterObject())
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_stroke(QPointF(30, 30), 1.0)
    assert not gc.isenabled()
    canvas.clear_document()
    assert gc.isenabled()
    if not was_enabled:
        gc.disable()


def test_raster_paint_error_restores_tiles_and_gc(qapp, monkeypatch):
    was_enabled = gc.isenabled()
    gc.enable()
    try:
        canvas, chapter, layer = _document_canvas()
        raster = chapter.add_object(layer.layer_id, RasterObject())
        canvas.set_selection("object", raster.object_id)
        canvas.set_tool(ToolKind.RASTER_PENCIL)
        canvas._begin_stroke(QPointF(30, 30), 1.0)
        assert not gc.isenabled()

        def fail_segment(*_args, **_kwargs):
            raise RuntimeError("synthetic paint failure")

        monkeypatch.setattr(canvas.tiles, "paint_segment", fail_segment)
        with pytest.raises(RuntimeError, match="synthetic paint failure"):
            canvas._continue_stroke(QPointF(60, 30), 1.0)
        assert gc.isenabled()
        assert not canvas._drawing
        assert canvas.tiles.object_tiles(raster.object_id) == {}
    finally:
        if not was_enabled:
            gc.disable()


def test_vector_preview_error_restores_gc_and_transient_state(
    qapp, monkeypatch,
):
    was_enabled = gc.isenabled()
    gc.enable()
    try:
        canvas, chapter, layer = _document_canvas()
        drawing = chapter.add_object(layer.layer_id, VectorDrawingObject())
        canvas.set_selection("object", drawing.object_id)
        canvas._begin_vector_pencil(drawing, QPointF(30, 30), 1.0)
        assert not gc.isenabled()

        def fail_segment(*_args, **_kwargs):
            raise RuntimeError("synthetic preview failure")

        monkeypatch.setattr(
            canvas._vector_preview_tiles, "paint_segment", fail_segment
        )
        with pytest.raises(RuntimeError, match="synthetic preview failure"):
            canvas._continue_vector_gesture(QPointF(60, 30), 1.0)
        assert gc.isenabled()
        assert not canvas._drawing
        assert canvas._vector_gesture_mode is None
        assert drawing.strokes == []
    finally:
        if not was_enabled:
            gc.disable()


def test_freehand_pressure_knots_preserve_peak_without_centerline_change():
    fitted = fit_freehand(
        [(0, 0, 0.1), (50, 0, 1.0), (100, 0, 0.1)],
        error=50,
        resample_spacing=None,
        attribute_error=0.025,
    )
    assert len(fitted) >= 3
    assert max(point.pressure for point in fitted) == pytest.approx(1.0)
    assert all(point.y == pytest.approx(0.0) for point in fitted)
    assert all(
        control is None or control[1] == pytest.approx(0.0)
        for point in fitted
        for control in (point.incoming, point.outgoing)
    )


def test_vector_pencil_uses_tiled_preview_and_promotes_current_scale(
    qapp, monkeypatch,
):
    canvas, chapter, layer = _document_canvas()
    drawing = chapter.add_object(layer.layer_id, VectorDrawingObject())
    canvas.set_selection("object", drawing.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_vector_pencil(drawing, QPointF(50, 100), 0.5)
    for index in range(1, 120):
        canvas._continue_vector_gesture(
            QPointF(50 + index * 2, 100 + (index % 7)), 0.7
        )
    assert canvas._vector_preview_tiles.object_tiles(
        canvas._vector_preview_id
    )

    original_pressure_values = canvas._vector_pressure_values
    monkeypatch.setattr(
        canvas, "_vector_pressure_values",
        lambda *_args: pytest.fail("paint must use cached preview tiles"),
    )
    image = QImage(800, 600, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    canvas._draw_live_vector_gesture(painter)
    painter.end()
    monkeypatch.setattr(
        canvas, "_vector_pressure_values", original_pressure_values
    )

    canvas._finish_vector_pencil(drawing)
    assert drawing.strokes
    assert canvas._promoted_vector_preview["stroke_id"] == (
        drawing.strokes[-1].stroke_id
    )
    rasterized: list[str] = []
    original_stroke_image = canvas._vector_stroke_image

    def track_rasterization(*args, **kwargs):
        rasterized.append(args[1].stroke_id)
        return original_stroke_image(*args, **kwargs)

    monkeypatch.setattr(canvas, "_vector_stroke_image", track_rasterization)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    canvas._render_vector_drawing(
        painter, drawing, QRectF(0, 0, 800, 600)
    )
    painter.end()
    assert rasterized == []

    canvas.scale = 2.0
    image.fill(Qt.transparent)
    painter = QPainter(image)
    canvas._render_vector_drawing(
        painter, drawing, QRectF(0, 0, 800, 600)
    )
    painter.end()
    assert rasterized == [drawing.strokes[-1].stroke_id]


@pytest.mark.parametrize(
    ("mode", "remaining"),
    [("stroke", 1), ("point", 3), ("intersection", 1)],
)
def test_vector_eraser_previews_without_mutating_and_commits_once(
    qapp, mode, remaining,
):
    settings = EditorSettings(
        snap_to_grid=False, vector_eraser_mode=mode,
    )
    canvas, chapter, layer = _document_canvas(settings)
    target = VectorStroke(points=[
            VectorStrokePoint(x=50, y=150, width=10),
            VectorStrokePoint(x=350, y=150, width=10),
        ])
    untouched = VectorStroke(points=[
        VectorStrokePoint(x=50, y=450, width=10),
        VectorStrokePoint(x=350, y=450, width=10),
    ])
    drawing = chapter.add_object(
        layer.layer_id,
        VectorDrawingObject(strokes=[target, untouched]),
    )
    canvas.set_selection("object", drawing.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._set_vector_selection(
        drawing, {untouched.stroke_id}, {untouched.points[0].point_id}
    )
    before = drawing.to_dict()
    canvas._begin_vector_gesture(drawing, QPointF(200, 150), 1.0)
    canvas._continue_vector_gesture(QPointF(205, 150), 1.0)
    assert drawing.to_dict() == before
    assert drawing.strokes[0].stroke_id in canvas._vector_eraser_preview

    canvas._finish_vector_eraser(drawing)
    qapp.processEvents()
    assert len(drawing.strokes) == remaining
    assert drawing.strokes[-1].stroke_id == untouched.stroke_id
    assert canvas.selected_vector_stroke_ids == {untouched.stroke_id}
    assert canvas.selected_vector_point_ids == {untouched.points[0].point_id}
    if mode == "point":
        assert target.stroke_id in {
            stroke.stroke_id for stroke in drawing.strokes
        }
    assert canvas.command_stack.can_undo
    canvas.command_stack.undo()
    restored = canvas.chapter.objects[drawing.object_id]
    assert restored.to_dict() == before


@pytest.mark.parametrize("canvas_type", [RasterCanvasWidget, GpuCanvasWidget])
@pytest.mark.parametrize("ratio", [1.0, 2.0])
def test_scene_cache_is_device_pixel_aware_and_updates_partial_region(
    qapp, canvas_type, ratio,
):
    class TestCanvas(canvas_type):
        def devicePixelRatioF(self):  # noqa: N802
            return ratio

    chapter = ChapterDocument()
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1200, 1200)
    )
    chapter.add_layer(
        page.layer_id, "Ink", BoundGeometry.rectangle(0, 0, 1200, 1200)
    )
    canvas = TestCanvas(EditorSettings(snap_to_grid=False))
    canvas.resize(400, 300)
    canvas.set_document(chapter, TileStore())
    canvas.rotation = 23.0
    canvas._ensure_scene_cache()
    assert canvas._scene_cache.width() == canvas.width() * ratio
    assert canvas._scene_cache.height() == canvas.height() * ratio
    assert canvas.performance_snapshot()["renderer"] == (
        "gpu" if canvas_type is GpuCanvasWidget else "raster"
    )

    widget_dirty = canvas._mark_scene_dirty_world(QRectF(100, 100, 20, 20))
    assert not widget_dirty.isEmpty()
    assert widget_dirty != canvas.rect()
    canvas._ensure_scene_cache()
    assert canvas._scene_dirty_widget.isEmpty()


@pytest.mark.parametrize("ratio", [1.0, 2.0])
@pytest.mark.parametrize("rotation", [0.0, 37.0])
def test_ctrl_wheel_zoom_is_centered_and_pointer_independent(
    qapp, ratio, rotation,
):
    class TestCanvas(CanvasWidget):
        def devicePixelRatioF(self):  # noqa: N802
            return ratio

    def make_canvas():
        canvas, chapter, layer = _document_canvas()
        replacement = TestCanvas(EditorSettings(snap_to_grid=False))
        replacement.resize(canvas.size())
        replacement.set_document(chapter, TileStore())
        replacement.center_x = 431.125
        replacement.center_y = 517.875
        replacement.scale = 0.63
        replacement.rotation = rotation
        return replacement

    positions = [
        QPointF(0, 0), QPointF(799, 599), QPointF(113.25, 487.75),
    ]
    states = []
    for position in positions:
        canvas = make_canvas()
        viewport_center = QPointF(canvas.width() / 2, canvas.height() / 2)
        centered_document = canvas.widget_to_document(viewport_center)
        canvas._ensure_scene_cache()
        previous_key = canvas._scene_cache_key
        event = _WheelEvent(position, 120)
        canvas.wheelEvent(event)
        canvas._ensure_scene_cache()
        states.append((canvas.center_x, canvas.center_y, canvas.scale))
        assert event.accepted
        assert canvas.widget_to_document(viewport_center) == centered_document
        assert canvas._scene_cache_key == canvas._scene_key()
        assert canvas._scene_cache_key != previous_key
    assert states[0] == states[1] == states[2]


def test_centered_wheel_zoom_clamps_and_zero_delta_does_not_move_camera(qapp):
    canvas, _chapter, _layer = _document_canvas()
    canvas.center_x = 431.125
    canvas.center_y = 517.875
    center_before = (canvas.center_x, canvas.center_y)

    canvas.scale = 7.9
    canvas.wheelEvent(_WheelEvent(QPointF(10, 20), 10000))
    assert canvas.scale == 8.0
    assert (canvas.center_x, canvas.center_y) == center_before
    canvas.wheelEvent(_WheelEvent(QPointF(700, 500), 0))
    assert canvas.scale == 8.0
    assert (canvas.center_x, canvas.center_y) == center_before
    canvas.wheelEvent(_WheelEvent(QPointF(300, 200), -10000))
    assert canvas.scale == 0.05
    assert (canvas.center_x, canvas.center_y) == center_before


def test_drag_zoom_is_centered_and_start_position_independent(qapp):
    canvases = [_document_canvas()[0], _document_canvas()[0]]
    starts = [QPointF(40, 80), QPointF(620, 410)]
    states = []
    for canvas, start in zip(canvases, starts):
        canvas.center_x = 431.125
        canvas.center_y = 517.875
        canvas.scale = 0.75
        canvas.rotation = 31.0
        viewport_center = QPointF(canvas.width() / 2, canvas.height() / 2)
        centered_document = canvas.widget_to_document(viewport_center)
        canvas._begin_navigation("zoom", start)
        canvas._update_navigation(start + QPointF(80, 25))
        assert canvas.widget_to_document(viewport_center) == centered_document
        before_release = (canvas.center_x, canvas.center_y, canvas.scale)
        canvas._end_navigation()
        assert (canvas.center_x, canvas.center_y, canvas.scale) == before_release
        assert canvas.widget_to_document(viewport_center) == centered_document
        states.append(before_release)
    assert states[0] == states[1]


def test_non_zoom_wheel_scrolling_retains_camera_snapping(qapp):
    canvas, _chapter, _layer = _document_canvas()
    canvas.center_x = 431.125
    canvas.center_y = 517.875
    canvas.scale = 0.73
    canvas.wheelEvent(
        _WheelEvent(QPointF(10, 20), 120, Qt.NoModifier)
    )
    assert canvas.center_x * canvas.scale == pytest.approx(
        round(canvas.center_x * canvas.scale)
    )
    assert canvas.center_y * canvas.scale == pytest.approx(
        round(canvas.center_y * canvas.scale)
    )


def test_chapter_preview_uses_visual_change_dirty_bands(qapp):
    canvas, _chapter, _layer = _document_canvas()
    preview = ChapterPreview(canvas)
    preview._cache = QImage(60, 120, QImage.Format_ARGB32_Premultiplied)
    preview._cache.fill(Qt.transparent)
    preview._dirty_full = False

    canvas.visualChanged.emit(QRectF(100, 300, 50, 80))
    assert not preview._dirty_full
    assert len(preview._dirty_bands) == 1
    assert preview._dirty_bands[0].height() < preview._cache.height()


def test_restoring_session_fully_invalidates_scene_cache(qapp):
    canvas, _chapter, _layer = _document_canvas()
    state = canvas.capture_session_state()
    assert state is not None
    canvas._ensure_scene_cache()
    assert not canvas._scene_dirty_full

    canvas.restore_session_state(state)
    assert canvas._scene_dirty_full
    assert canvas._scene_cache_key is None


def test_performance_snapshot_has_stable_public_fields(qapp):
    canvas, _chapter, _layer = _document_canvas()
    canvas._performance.input_ms.extend([1.0, 2.0, 3.0])
    canvas._performance.submit_ms.extend([0.5, 1.0])
    canvas._performance.frame_ms.extend([4.0, 5.0])
    snapshot = canvas.performance_snapshot()
    assert set(snapshot) == {
        "renderer", "input_p50_ms", "input_p95_ms", "input_p99_ms",
        "submit_p95_ms", "frame_p95_ms", "samples",
    }
    assert snapshot["renderer"] == "raster"
    assert snapshot["samples"] == 3
