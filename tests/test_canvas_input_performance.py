from __future__ import annotations

import gc

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

import comic_editor.ui.canvas as canvas_module

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


def test_sparse_tile_lookup_filters_existing_tiles_for_extreme_bounds():
    store = TileStore()
    store.paint_dab("ink", QPointF(20, 20), 20, QColor("#111111"))
    store.paint_dab("ink", QPointF(600, 20), 20, QColor("#111111"))

    visible = list(store.iter_tiles(
        "ink", QRectF(-1.0e12, -1.0e12, 2.0e12, 2.0e12)
    ))

    assert {key for key, _image in visible} == {(0, 0), (2, 0)}


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


def test_vector_eraser_error_clears_live_background(qapp, monkeypatch):
    settings = EditorSettings(
        snap_to_grid=False, vector_eraser_mode="stroke",
    )
    canvas, chapter, layer = _document_canvas(settings)
    drawing = chapter.add_object(
        layer.layer_id,
        VectorDrawingObject(strokes=[VectorStroke(points=[
            VectorStrokePoint(x=50, y=100, width=10),
            VectorStrokePoint(x=350, y=100, width=10),
        ])]),
    )
    canvas.set_selection("object", drawing.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    monkeypatch.setattr(
        canvas, "_vector_stroke_touched",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("eraser failure")),
    )

    with pytest.raises(RuntimeError, match="eraser failure"):
        canvas._begin_vector_gesture(drawing, QPointF(200, 100), 1.0)

    assert canvas._vector_gesture_mode is None
    assert canvas._vector_eraser_background_cache.isNull()
    assert canvas._vector_eraser_preview_versions == {}


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


@pytest.mark.parametrize("mode", ["stroke", "point", "intersection"])
def test_vector_eraser_first_press_paints_complete_live_result(
    qapp, mode,
):
    settings = EditorSettings(
        snap_to_grid=False, vector_eraser_mode=mode,
    )
    canvas, chapter, layer = _document_canvas(settings)
    target = VectorStroke(color="#FF111111", points=[
        VectorStrokePoint(x=50, y=150, width=18),
        VectorStrokePoint(x=350, y=150, width=18),
    ])
    drawing = chapter.add_object(
        layer.layer_id, VectorDrawingObject(strokes=[target])
    )
    canvas.set_selection("object", drawing.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    before_model = drawing.to_dict()

    before = QImage(800, 600, QImage.Format_ARGB32_Premultiplied)
    before.fill(Qt.transparent)
    canvas.render(before)
    # Sample inside the ink but off its degenerate centerline selection cage.
    probe = canvas.document_to_widget(QPointF(200, 154)).toPoint()
    assert before.pixelColor(probe).red() < 100

    canvas._begin_vector_gesture(drawing, QPointF(200, 150), 1.0)
    assert drawing.to_dict() == before_model
    assert target.stroke_id in canvas._vector_eraser_preview
    assert not canvas._vector_eraser_background_cache.isNull()

    live = QImage(800, 600, QImage.Format_ARGB32_Premultiplied)
    live.fill(Qt.transparent)
    canvas.render(live)
    before_color = before.pixelColor(probe)
    live_color = live.pixelColor(probe)
    assert live_color.red() > 220, (
        before_color.getRgb(), live_color.getRgb()
    )

    canvas._cancel_vector_gesture(restore=True)
    assert canvas._vector_eraser_background_cache.isNull()
    assert canvas._vector_eraser_preview_versions == {}


def test_transformed_vector_eraser_requests_mapped_live_dirty_region(
    qapp, monkeypatch,
):
    settings = EditorSettings(
        snap_to_grid=False, vector_eraser_mode="stroke",
    )
    canvas, chapter, layer = _document_canvas(settings)
    stroke = VectorStroke(points=[
        VectorStrokePoint(x=0, y=50, width=12),
        VectorStrokePoint(x=100, y=50, width=12),
    ])
    drawing = chapter.add_object(
        layer.layer_id,
        VectorDrawingObject(
            strokes=[stroke],
            transform_frame=(0, 0, 100, 100),
            transform_quad=[
                (300, 200), (500, 180), (530, 390), (280, 410),
            ],
        ),
    )
    canvas.set_selection("object", drawing.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    local_probe = QPointF(50, 50)
    world_probe = canvas._drawing_object_transform(drawing).map(local_probe)
    updates = []
    monkeypatch.setattr(canvas, "update", lambda *args: updates.append(args))

    canvas._begin_vector_gesture(drawing, world_probe, 1.0)

    widget_probe = canvas.document_to_widget(world_probe).toPoint()
    assert any(
        args and args[0].contains(widget_probe)
        for args in updates
    )


def test_vector_eraser_reuses_unchanged_replacement_images(qapp):
    settings = EditorSettings(
        snap_to_grid=False, vector_eraser_mode="point",
    )
    canvas, chapter, layer = _document_canvas(settings)
    first = VectorStroke(points=[
        VectorStrokePoint(x=50, y=100, width=10),
        VectorStrokePoint(x=350, y=100, width=10),
    ])
    second = VectorStroke(points=[
        VectorStrokePoint(x=50, y=300, width=10),
        VectorStrokePoint(x=350, y=300, width=10),
    ])
    drawing = chapter.add_object(
        layer.layer_id, VectorDrawingObject(strokes=[first, second])
    )
    canvas.set_selection("object", drawing.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    image = QImage(800, 600, QImage.Format_ARGB32_Premultiplied)

    canvas._begin_vector_gesture(drawing, QPointF(200, 100), 1.0)
    canvas._continue_vector_gesture(QPointF(200, 200), 1.0)
    painter = QPainter(image)
    canvas._render_vector_drawing(
        painter, drawing, QRectF(0, 0, 800, 600)
    )
    painter.end()
    first_version = canvas._vector_eraser_preview_versions[first.stroke_id]
    first_keys = {
        key for key in canvas._vector_render_cache
        if len(key) >= 3
        and key[1] == first.stroke_id
        and isinstance(key[2], tuple)
        and key[2][0] == "eraser-preview"
    }
    assert first_keys

    canvas._continue_vector_gesture(QPointF(200, 300), 1.0)
    painter = QPainter(image)
    canvas._render_vector_drawing(
        painter, drawing, QRectF(0, 0, 800, 600)
    )
    painter.end()
    assert canvas._vector_eraser_preview_versions[first.stroke_id] == (
        first_version
    )
    assert first_keys <= set(canvas._vector_render_cache)


def test_vector_eraser_background_cache_is_device_pixel_aware(qapp):
    class TestCanvas(CanvasWidget):
        def devicePixelRatioF(self):  # noqa: N802
            return 2.0

    source, chapter, layer = _document_canvas()
    drawing = chapter.add_object(
        layer.layer_id,
        VectorDrawingObject(strokes=[VectorStroke(points=[
            VectorStrokePoint(x=50, y=100, width=10),
            VectorStrokePoint(x=350, y=100, width=10),
        ])]),
    )
    canvas = TestCanvas(EditorSettings(
        snap_to_grid=False, vector_eraser_mode="stroke",
    ))
    canvas.resize(source.size())
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 400, 300, 1.0
    canvas.set_selection("object", drawing.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER)

    canvas._begin_vector_gesture(drawing, QPointF(200, 100), 1.0)

    assert canvas._vector_eraser_background_cache.width() == 1600
    assert canvas._vector_eraser_background_cache.height() == 1200
    assert canvas._vector_eraser_background_cache.devicePixelRatio() == 2.0


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


def test_vector_cache_is_byte_budgeted_and_keeps_dense_warm_frames(
    qapp,
):
    canvas, chapter, layer = _document_canvas()
    strokes = [
        VectorStroke(points=[
            VectorStrokePoint(x=20, y=20 + index, width=2),
            VectorStrokePoint(x=40, y=20 + index, width=2),
        ])
        for index in range(500)
    ]
    drawing = chapter.add_object(
        layer.layer_id, VectorDrawingObject(strokes=strokes)
    )
    canvas.set_selection("object", drawing.object_id)
    image = QImage(800, 600, QImage.Format_ARGB32_Premultiplied)

    painter = QPainter(image)
    canvas._render_vector_drawing(
        painter, drawing, QRectF(0, 0, 800, 600)
    )
    painter.end()
    warm_keys = set(canvas._vector_render_cache)
    assert len(warm_keys) > 384
    assert canvas._vector_render_cache_bytes <= (
        canvas_module.VECTOR_RENDER_CACHE_BUDGET
    )

    canvas._begin_navigation("zoom", QPointF(300, 240))
    start_scale = canvas._vector_render_scale_override
    canvas._update_navigation(QPointF(390, 240))
    assert canvas._vector_render_scale_override == start_scale
    painter = QPainter(image)
    canvas._render_vector_drawing(
        painter, drawing, QRectF(0, 0, 800, 600)
    )
    painter.end()
    assert set(canvas._vector_render_cache) == warm_keys

    canvas._end_navigation()
    assert canvas._vector_render_scale_override is None
    painter = QPainter(image)
    canvas._render_vector_drawing(
        painter, drawing, QRectF(0, 0, 800, 600)
    )
    painter.end()
    assert len(canvas._vector_render_cache) > 384
    assert canvas._vector_render_cache_bytes <= (
        canvas_module.VECTOR_RENDER_CACHE_BUDGET
    )


def test_vector_cache_byte_accounting_oversize_invalidation_and_session(
    qapp, monkeypatch,
):
    canvas, chapter, layer = _document_canvas()
    monkeypatch.setattr(canvas_module, "VECTOR_RENDER_CACHE_BUDGET", 64)
    small = QImage(3, 3, QImage.Format_ARGB32_Premultiplied)
    small.fill(Qt.transparent)
    oversized = QImage(5, 5, QImage.Format_ARGB32_Premultiplied)
    oversized.fill(Qt.transparent)
    canvas._store_vector_render_cache(("drawing", "first"), (
        small, QRectF(0, 0, 3, 3),
    ))
    canvas._store_vector_render_cache(("drawing", "oversized"), (
        oversized, QRectF(0, 0, 5, 5),
    ))
    assert ("drawing", "oversized") not in canvas._vector_render_cache
    assert canvas._vector_render_cache_bytes == small.sizeInBytes()

    canvas._store_vector_render_cache(("drawing", "second"), (
        small, QRectF(0, 0, 3, 3),
    ))
    assert ("drawing", "first") not in canvas._vector_render_cache
    assert canvas._vector_render_cache_bytes <= 64
    canvas._invalidate_vector_cache_strokes({"second"})
    assert canvas._vector_render_cache == {}
    assert canvas._vector_render_cache_bytes == 0

    monkeypatch.setattr(
        canvas_module, "VECTOR_RENDER_CACHE_BUDGET", 64 * 1024 * 1024
    )
    stroke = VectorStroke(points=[VectorStrokePoint(x=20, y=20, width=2)])
    drawing = chapter.add_object(
        layer.layer_id, VectorDrawingObject(strokes=[stroke])
    )
    canvas._vector_stroke_image(drawing, stroke)
    state = canvas.capture_session_state()
    assert state is not None
    expected_cache = state.vector_cache
    expected_bytes = state.vector_cache_bytes
    canvas.restore_session_state(state)
    assert canvas._vector_render_cache is expected_cache
    assert canvas._vector_render_cache_bytes == expected_bytes

    cache_identity = canvas._vector_render_cache
    canvas._render_entity_crop(
        chapter, canvas.tiles, "object", drawing.object_id, maximum=64
    )
    assert canvas._vector_render_cache is cache_identity
    assert canvas._vector_render_cache_bytes == expected_bytes


def test_vector_spatial_index_preserves_order_and_rebuilds_on_revision(qapp):
    canvas, chapter, layer = _document_canvas()
    near_first = VectorStroke(points=[
        VectorStrokePoint(x=20, y=20, width=4),
        VectorStrokePoint(x=80, y=20, width=4),
    ])
    far = VectorStroke(points=[
        VectorStrokePoint(x=900, y=20, width=4),
        VectorStrokePoint(x=980, y=20, width=4),
    ])
    near_last = VectorStroke(points=[
        VectorStrokePoint(x=30, y=70, width=4),
        VectorStrokePoint(x=90, y=70, width=4),
    ])
    drawing = chapter.add_object(
        layer.layer_id,
        VectorDrawingObject(strokes=[near_first, far, near_last]),
    )
    visible = QRectF(0, 0, 200, 200)
    assert canvas._vector_stroke_indexes(drawing, visible) == [0, 2]

    for point in far.points:
        point.x -= 880
    far.touch_render_revision()
    drawing.touch_revision()
    assert canvas._vector_stroke_indexes(drawing, visible) == [0, 1, 2]


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
        start_render_scale = canvas._vector_render_scale_override
        canvas.wheelEvent(_WheelEvent(position, 120))
        assert canvas._vector_render_scale_override == start_render_scale
        canvas._settle_wheel_zoom()
        assert canvas._vector_render_scale_override is None
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


@pytest.mark.parametrize("rotation", [0.0, 31.0])
@pytest.mark.parametrize("start", [QPointF(40, 80), QPointF(620, 410)])
@pytest.mark.parametrize("drag_x", [-80.0, 80.0])
def test_drag_zoom_preserves_initial_screen_anchor(
    qapp, rotation, start, drag_x,
):
    canvas, _chapter, _layer = _document_canvas()
    canvas.center_x = 431.125
    canvas.center_y = 517.875
    canvas.scale = 0.75
    canvas.rotation = rotation
    anchor_document = canvas.widget_to_document(start)
    original_scale = canvas.scale

    canvas._begin_navigation("zoom", start)
    start_render_scale = canvas._vector_render_scale_override
    canvas._update_navigation(start + QPointF(drag_x, 25))
    assert canvas._vector_render_scale_override == start_render_scale

    anchored_widget = canvas.document_to_widget(anchor_document)
    assert anchored_widget.x() == pytest.approx(start.x())
    assert anchored_widget.y() == pytest.approx(start.y())
    if drag_x > 0:
        assert canvas.scale > original_scale
    else:
        assert canvas.scale < original_scale
    before_release = (
        canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation
    )
    canvas._end_navigation()
    assert canvas._vector_render_scale_override is None
    assert (
        canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation
    ) == before_release


def test_modifier_navigation_coalesces_to_latest_pointer_packet(qapp):
    canvas, _chapter, _layer = _document_canvas()
    reference, _chapter, _layer = _document_canvas()
    start = QPointF(120, 90)
    latest = QPointF(650, 430)
    changes = []
    canvas.cameraChanged.connect(lambda: changes.append(canvas.rotation))

    canvas._begin_navigation("rotate", start)
    reference._begin_navigation("rotate", start)
    for point in (
        QPointF(180, 120), QPointF(300, 200), latest,
    ):
        canvas._queue_navigation_update(point)
    assert canvas.rotation == 0.0

    reference._update_navigation(latest)
    qapp.processEvents()

    assert canvas.rotation == pytest.approx(reference.rotation)
    assert canvas.center_x == pytest.approx(reference.center_x)
    assert canvas.center_y == pytest.approx(reference.center_y)
    assert len(changes) == 1

    final = QPointF(700, 450)
    canvas._queue_navigation_update(QPointF(680, 440))
    canvas._end_navigation(final)
    reference._update_navigation(final)
    assert canvas.rotation == pytest.approx(reference.rotation)
    assert canvas.center_x == pytest.approx(reference.center_x)
    assert canvas.center_y == pytest.approx(reference.center_y)
    assert len(changes) == 2
    assert canvas._nav_mode is None
    assert canvas._nav_pending_point is None


def test_touch_pinch_reuses_starting_vector_scale_until_completion(qapp):
    canvas, _chapter, _layer = _document_canvas()
    start = [QPointF(250, 300), QPointF(550, 300)]
    canvas._rebase_touch_navigation(start)
    render_scale = canvas._vector_render_scale_override

    canvas._apply_touch_navigation([
        QPointF(150, 300), QPointF(650, 300),
    ])

    assert canvas.scale > 1.0
    assert canvas._vector_render_scale_override == render_scale
    canvas._cancel_touch_navigation()
    assert canvas._vector_render_scale_override is None


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
