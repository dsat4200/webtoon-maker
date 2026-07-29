from __future__ import annotations

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


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
