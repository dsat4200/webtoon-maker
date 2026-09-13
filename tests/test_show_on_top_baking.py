"""Converting promoted artwork to an image retains its tier and undo state."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ImageObject, RasterObject, ShapeStyle,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.baking import rasterize
from comic_editor.ui.canvas import CanvasWidget


def render(canvas):
    image = QImage(320, 240, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image, source_rect=QRectF(0, 0, 320, 240))
    return image


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).reshape(
        image.height(), image.bytesPerLine()//4, 4).copy()


@pytest.mark.parametrize("kind", ["layer", "object"])
@pytest.mark.parametrize("visible", [False, True])
def test_rasterize_retains_show_on_top_visibility_and_original_on_undo(qapp, kind, visible):
    chapter = ChapterDocument(height=240)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 320, 240))
    page.fill_color, page.border_width = None, 0
    tiles = TileStore()
    if kind == "layer":
        target = chapter.add_layer(page.layer_id, "Promoted shape",
            BoundGeometry.rectangle(40, 40, 120, 100),
            style=ShapeStyle(primary_color="#FFFF0000", outline_thickness=0))
        detail = chapter.add_object(target.layer_id, RasterObject(name="Detail"))
        tiles.paint_dab(detail.object_id, QPointF(105, 100), 12, QColor("yellow"),
                        square=True, antialias=False)
        identifier = target.layer_id
    else:
        target = chapter.add_object(page.layer_id, RasterObject(name="Promoted ink",
            interaction_rect=(40, 40, 120, 100)))
        tiles.paint_dab(target.object_id, QPointF(100, 90), 80, QColor("red"),
                        square=True, antialias=False)
        identifier = target.object_id
    target.show_on_top, target.visible = True, visible
    covering = chapter.add_layer(page.layer_id, "Ordinary front layer",
        BoundGeometry.rectangle(20, 20, 220, 160), index=0,
        style=ShapeStyle(primary_color="#FF0000FF", outline_thickness=0))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, tiles)
    canvas.set_selection(kind, identifier)
    try:
        before_model = chapter.to_dict()
        covering_before = covering.to_dict()
        before = render(canvas)
        assert before.pixelColor(80, 60) == QColor("red" if visible else "blue")
        rasterize(canvas, kind, identifier)
        replacement = canvas.chapter.objects[identifier]
        assert isinstance(replacement, ImageObject)
        assert replacement.show_on_top is True
        assert replacement.visible is visible
        assert canvas.chapter.layers[covering.layer_id].to_dict() == covering_before
        if kind == "layer":
            assert identifier not in canvas.chapter.layers
            assert detail.object_id not in canvas.chapter.objects
        captured = pixels(canvas.images.image(identifier))
        assert np.any((captured[:, :, 2] == 255) & (captured[:, :, 0] == 0))
        assert not np.any((captured[:, :, 0] == 255) & (captured[:, :, 2] == 0))
        after = render(canvas)
        assert after.pixelColor(80, 60) == before.pixelColor(80, 60)
        assert np.max(abs(pixels(before).astype(int)-pixels(after).astype(int))) <= 2
        after_model = canvas.chapter.to_dict()
        assert len(canvas.command_stack._undo) == 1
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before_model
        assert canvas.images.source(identifier) is None
        assert np.array_equal(pixels(render(canvas)), pixels(before))
        restored = (canvas.chapter.layers if kind == "layer" else canvas.chapter.objects)[identifier]
        assert type(restored) is type(target)
        assert restored.show_on_top is True
        canvas.command_stack.redo()
        assert canvas.chapter.to_dict() == after_model
        assert canvas.chapter.objects[identifier].show_on_top is True
        assert np.array_equal(pixels(render(canvas)), pixels(after))
    finally:
        canvas._effect_jobs.cancel()
        canvas.deleteLater()
