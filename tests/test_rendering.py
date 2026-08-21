from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, GridSettings, RasterObject, VectorDrawingObject,
    VectorStroke, VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


def test_user_document_and_layer_grids_resolve_for_drawing_and_snapping(
    qapp, monkeypatch,
):
    settings = EditorSettings(
        snap_to_grid=True,
        grid_overlay_visible=True,
        grid_size_px=40,
        grid_divisions=2,
        grid_color="#000000",
        grid_opacity=1.0,
    )
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 1080, 1080)
    )
    layer = chapter.add_layer(
        page.layer_id, "Layer", BoundGeometry.rectangle(0, 0, 500, 500)
    )
    canvas = CanvasWidget(settings)
    canvas.set_document(chapter, TileStore())

    inherited = canvas.resolved_grid(layer.layer_id)
    assert (inherited.size, inherited.divisions) == (40, 2)
    assert canvas._snap(QPointF(23, 23), layer.layer_id) == QPointF(20, 20)

    chapter.grid_override_enabled = True
    chapter.grid = GridSettings(size=60, divisions=2, color="#ff0000", opacity=0.5)
    document_grid = canvas.resolved_grid(layer.layer_id)
    assert (document_grid.size, document_grid.divisions) == (60, 2)
    assert canvas._snap(QPointF(23, 23), layer.layer_id) == QPointF(30, 30)

    layer.grid_override = GridSettings(
        size=80, divisions=1, color="#00ff00", opacity=0.75
    )
    layer_grid = canvas.resolved_grid(layer.layer_id)
    assert (layer_grid.size, layer_grid.divisions) == (80, 1)
    assert layer_grid.color == "#00ff00"
    assert canvas._snap(QPointF(43, 43), layer.layer_id) == QPointF(80, 80)

    chapter.grid_override_enabled = False
    layer.grid_override = None
    image = QImage(81, 81, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("white"))
    painter = QPainter(image)
    canvas._draw_grid(painter, QRectF(0, 0, 80, 80))
    painter.end()
    assert image.pixelColor(20, 10).lightness() < 20
    assert image.pixelColor(10, 10).lightness() > 240

    settings.grid_overlay_visible = False
    hidden = QImage(81, 81, QImage.Format_ARGB32_Premultiplied)
    hidden.fill(QColor("white"))
    painter = QPainter(hidden)
    canvas._draw_grid(painter, QRectF(0, 0, 80, 80))
    painter.end()
    assert hidden.pixelColor(20, 10).lightness() > 240

    preview_grid_calls = 0

    def counted_grid(*args):
        nonlocal preview_grid_calls
        preview_grid_calls += 1

    monkeypatch.setattr(canvas, "_draw_grid", counted_grid)
    preview = QImage(108, 108, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(preview)
    assert preview_grid_calls == 0


def _scene(bound: BoundGeometry):
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 1080, 1080))
    layer = chapter.add_layer(page.layer_id, "Masked", bound)
    raster = chapter.add_object(layer.layer_id, RasterObject())
    tiles = TileStore()
    return chapter, layer, raster, tiles


def test_rectangle_mask_is_non_destructive_and_reveals_data(qapp):
    chapter, layer, raster, tiles = _scene(BoundGeometry.rectangle(0, 0, 200, 200))
    tiles.paint_dab(raster.object_id, QPointF(500, 500), 80, QColor("black"))
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    preview = QImage(108, 108, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(preview)
    assert preview.pixelColor(50, 50).lightness() > 200
    layer.bound = BoundGeometry.rectangle(0, 0, 800, 800)
    canvas.render_preview(preview)
    assert preview.pixelColor(50, 50).lightness() < 80
    assert [key for key, _ in tiles.iter_tiles(raster.object_id)]


def test_circle_and_nested_masks_intersect(qapp):
    chapter, layer, raster, tiles = _scene(BoundGeometry.circle(540, 540, 300))
    nested = chapter.add_layer(
        layer.layer_id, "Nested", BoundGeometry.rectangle(440, 440, 200, 200)
    )
    layer.children = [item for item in layer.children if item.entity_id != raster.object_id]
    raster.parent_layer_id = nested.layer_id
    from comic_editor.core.models import ChildRef
    nested.children = [ChildRef("object", raster.object_id)]
    tiles.paint_dab(raster.object_id, QPointF(540, 540), 60, QColor("black"))
    tiles.paint_dab(raster.object_id, QPointF(750, 540), 60, QColor("black"))
    chapter.validate()
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    preview = QImage(108, 108, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(preview)
    assert preview.pixelColor(54, 54).lightness() < 80
    assert preview.pixelColor(75, 54).lightness() > 200


def test_preview_image_is_display_resolution_not_chapter_resolution(qapp):
    chapter, layer, raster, tiles = _scene(BoundGeometry.rectangle(0, 0, 1080, 100000))
    chapter.height = 100000
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    preview = QImage(76, 800, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(preview)
    assert preview.size().width() == 76
    assert preview.size().height() == 800


def test_ignore_direct_parent_renders_subtree_above_parent_and_keeps_page_mask(
    qapp,
):
    chapter = ChapterDocument(height=500)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 400)
    )
    parent = chapter.add_layer(
        page.layer_id, "Parent",
        BoundGeometry.rectangle(50, 50, 200, 200),
    )
    parent.border_width = 10
    parent.border_color = "#ff0000"
    child = chapter.add_layer(
        parent.layer_id, "Overlay",
        BoundGeometry.rectangle(220, 80, 130, 100),
    )
    child.fill_color = "#000000"
    child.ignore_parent_mask = True
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    image = QImage(
        chapter.width, chapter.height,
        QImage.Format_ARGB32_Premultiplied,
    )
    canvas.render_preview(image)

    assert image.pixelColor(245, 100).lightness() < 40
    assert image.pixelColor(280, 100).lightness() < 40
    assert image.pixelColor(320, 100).lightness() > 200
    assert canvas._shape_border_contains(
        child.layer_id, QPointF(280, 80)
    )


def test_ignore_direct_parent_raster_renders_and_hits_above_parent(qapp):
    chapter = ChapterDocument(height=400)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 350)
    )
    parent = chapter.add_layer(
        page.layer_id, "Parent",
        BoundGeometry.rectangle(50, 50, 200, 200),
    )
    parent.fill_color = "#ffffff"
    parent.border_width = 10
    parent.border_color = "#ff0000"
    raster = chapter.add_object(parent.layer_id, RasterObject())
    raster.ignore_parent_mask = True
    tiles = TileStore()
    tiles.paint_dab(
        raster.object_id, QPointF(270, 120), 36, QColor("black")
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    image = QImage(
        chapter.width, chapter.height,
        QImage.Format_ARGB32_Premultiplied,
    )
    canvas.render_preview(image)

    assert image.pixelColor(270, 120).lightness() < 40
    canvas.set_selection("layer", parent.layer_id, activate_default_tool=False)
    assert raster.object_id in canvas.hit_test_objects(QPointF(270, 120))


def test_selected_raster_underlay_is_live_only_and_bypasses_shape_mask(qapp):
    chapter = ChapterDocument(height=350)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 300)
    )
    layer = chapter.add_layer(
        page.layer_id, "Mask",
        BoundGeometry.rectangle(25, 25, 125, 200),
    )
    raster = chapter.add_object(layer.layer_id, RasterObject())
    raster.underlay_opacity = 0.5
    tiles = TileStore()
    tiles.paint_dab(
        raster.object_id, QPointF(75, 100), 24, QColor("black")
    )
    tiles.paint_dab(
        raster.object_id, QPointF(225, 100), 24, QColor("black")
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    canvas.set_selection("object", raster.object_id)

    preview = QImage(
        chapter.width, chapter.height, QImage.Format_ARGB32_Premultiplied
    )
    canvas.render_preview(preview)
    assert preview.pixelColor(225, 100).lightness() > 220

    live = QImage(
        chapter.width, chapter.height, QImage.Format_ARGB32_Premultiplied
    )
    live.fill(QColor(chapter.background))
    painter = QPainter(live)
    document_rect = QRectF(0, 0, chapter.width, chapter.height)
    canvas._set_live_underlay_context()
    for page_id in reversed(chapter.root_page_ids):
        canvas._render_layer(
            painter, chapter.layers[page_id], 1.0, document_rect
        )
    canvas._render_selected_drawing_underlay(painter, document_rect)
    canvas._clear_live_underlay_context()
    painter.end()

    assert 70 < live.pixelColor(225, 100).lightness() < 210
    assert live.pixelColor(75, 100).lightness() < 100
    exported = QImage(
        chapter.width, chapter.height, QImage.Format_ARGB32_Premultiplied
    )
    canvas.render_preview(exported)
    assert exported.pixelColor(225, 100).lightness() > 220


def test_vector_underlay_shows_selected_strokes_outside_parent_mask(qapp):
    chapter = ChapterDocument(height=320)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 300)
    )
    layer = chapter.add_layer(
        page.layer_id, "Mask",
        BoundGeometry.rectangle(0, 0, 120, 250),
    )
    drawing = chapter.add_object(
        layer.layer_id,
        VectorDrawingObject(strokes=[
            VectorStroke(
                color="#FF000000",
                points=[
                    VectorStrokePoint(x=180, y=100, width=10),
                    VectorStrokePoint(x=240, y=100, width=10),
                ],
            )
        ]),
    )
    drawing.underlay_opacity = 1.0
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", drawing.object_id)
    document_rect = QRectF(0, 0, chapter.width, chapter.height)

    live = QImage(
        chapter.width, chapter.height, QImage.Format_ARGB32_Premultiplied
    )
    live.fill(QColor(chapter.background))
    painter = QPainter(live)
    canvas._set_live_underlay_context()
    for page_id in reversed(chapter.root_page_ids):
        canvas._render_layer(
            painter, chapter.layers[page_id], 1.0, document_rect
        )
    canvas._render_selected_drawing_underlay(painter, document_rect)
    canvas._clear_live_underlay_context()
    painter.end()
    assert live.pixelColor(210, 100).lightness() < 80

    preview = QImage(
        chapter.width, chapter.height, QImage.Format_ARGB32_Premultiplied
    )
    canvas.render_preview(preview)
    assert preview.pixelColor(210, 100).lightness() > 220
