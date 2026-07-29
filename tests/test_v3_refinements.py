from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QKeySequence

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, TextObject,
)
from comic_editor.core.pressure import BrushPreset, PressureCurve
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.preview import ChapterPreview


def _document_canvas(settings: EditorSettings | None = None):
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1080, 1080)
    )
    layer = chapter.add_layer(
        page.layer_id, "Layer 1",
        BoundGeometry.rectangle(0, 0, 1080, 1080),
    )
    canvas = CanvasWidget(settings or EditorSettings(snap_to_grid=False))
    canvas.resize(1080, 800)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", layer.layer_id, activate_default_tool=False)
    return canvas, chapter, page, layer


def test_fill_layer_is_a_boundless_leaf_and_round_trips():
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(page.layer_id)
    fill = chapter.add_fill_layer(layer.layer_id, "Backdrop", "#ff0080")
    chapter.validate()
    loaded = ChapterDocument.from_dict(chapter.to_dict())
    migrated = loaded.layers[fill.layer_id]
    assert migrated.layer_kind == "fill"
    assert migrated.bound is None
    assert migrated.fill_color == "#ff0080"
    with pytest.raises(ValueError, match="cannot contain"):
        loaded.add_layer(fill.layer_id)
    with pytest.raises(ValueError, match="directly"):
        loaded.add_object(fill.layer_id, RasterObject())


def test_layer_fill_border_and_radius_affect_actual_rendering(qapp):
    canvas, chapter, page, layer = _document_canvas()
    layer.bound = BoundGeometry.rectangle(100, 100, 400, 400)
    layer.fill_color = "#ff0000"
    layer.border_width = 20
    layer.border_color = "#0000ff"
    layer.vertex_radius = 80
    image = QImage(1080, 1080, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    assert image.pixelColor(300, 300).red() > 200
    assert image.pixelColor(110, 300).blue() > 180
    # The rounded corner is outside the actual clipping path.
    assert image.pixelColor(101, 101).lightness() > 200


def test_first_outliner_row_is_frontmost_for_hit_testing(qapp):
    canvas, chapter, page, layer = _document_canvas()
    front = chapter.add_object(
        layer.layer_id,
        TextObject(
            name="Front", layout_mode="free",
            transform_quad=[(100, 100), (300, 100), (300, 200), (100, 200)],
        ),
    )
    back = chapter.add_object(
        layer.layer_id,
        TextObject(
            name="Back", layout_mode="free",
            transform_quad=[(100, 100), (300, 100), (300, 200), (100, 200)],
        ),
    )
    assert canvas.hit_test_objects(QPointF(150, 150)) == [
        front.object_id, back.object_id
    ]


def test_raster_box_creation_and_frame_outside_selection(qapp):
    canvas, chapter, page, layer = _document_canvas()
    assert canvas.begin_raster_creation(layer.layer_id)
    first = canvas.document_to_widget(QPointF(100, 100))
    second = canvas.document_to_widget(QPointF(260, 220))
    canvas._tool_press(first, 1)
    canvas._tool_move(second, 1)
    canvas._tool_release()
    raster = chapter.objects[canvas.selected_object_id]
    assert isinstance(raster, RasterObject)
    assert raster.interaction_rect == pytest.approx((0, 0, 160, 120))
    assert canvas.tool == ToolKind.RASTER_PENCIL
    text = chapter.add_object(
        layer.layer_id,
        TextObject(
            layout_mode="free",
            transform_quad=[(400, 100), (600, 100), (600, 220), (400, 220)],
        ),
    )
    canvas._tool_press(canvas.document_to_widget(QPointF(450, 150)), 1)
    assert canvas.selected_object_id == text.object_id
    assert canvas.tool == ToolKind.TEXT_EDIT


def test_raster_bound_edit_cannot_shrink_inside_alpha(qapp):
    canvas, chapter, page, layer = _document_canvas()
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(interaction_rect=(0, 0, 300, 300)),
    )
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(220, 220), 40, QColor("black")
    )
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.BOUND_EDIT)
    top_left = canvas.document_to_widget(QPointF(0, 0))
    canvas._tool_press(top_left, 1)
    canvas._tool_move(canvas.document_to_widget(QPointF(250, 250)), 1)
    canvas._tool_release()
    content = canvas.tiles.content_bounds(raster.object_id)
    frame = QRectF(*raster.interaction_rect)
    assert frame.contains(content)
    assert canvas.tool == ToolKind.BOUND_EDIT


def test_preview_contains_and_centers_long_chapter(qapp):
    canvas, chapter, page, layer = _document_canvas()
    chapter.height = 100_000
    preview = ChapterPreview(canvas)
    preview.resize(92, 800)
    rect = preview.content_rect()
    assert rect.height() == 784
    assert rect.width() < 20
    assert abs(rect.width() / rect.height() - 1080 / 100_000) < 0.01
    assert abs(rect.center().x() - preview.rect().center().x()) <= 1


def test_pressure_preset_round_trip_and_independent_channels():
    preset = BrushPreset(
        name="Ink",
        size_curve=PressureCurve(minimum=0.2, maximum=0.8),
        opacity_curve=PressureCurve(minimum=0.4, maximum=1.0),
        pressure_size=False,
        pressure_opacity=True,
        density=1.7,
        stroke_start_ratio=0.3,
        stroke_end_ratio=0.5,
        antialiasing=False,
    )
    loaded = BrushPreset.from_dict(preset.to_dict())
    assert loaded.name == "Ink"
    assert loaded.pressure_size is False
    assert loaded.pressure_opacity is True
    assert loaded.density == pytest.approx(1.7)
    assert loaded.stroke_start_ratio == pytest.approx(0.3)
    assert loaded.antialiasing is False


def test_text_edit_shortcut_suppression_targets_letters_and_shift(qapp):
    window = MainWindow()
    try:
        window._set_text_shortcut_suppression(True)
        states = {
            sequence.toString(QKeySequence.PortableText): shortcut.isEnabled()
            for shortcut, sequence in window._shortcut_sequences
        }
        assert states["P"] is False
        assert states["Ctrl+S"] is False
        assert states["Ctrl+Shift+Z"] is False
        assert states["Ctrl+0"] is True
        assert states["Alt+Return"] is True
        window._set_text_shortcut_suppression(False)
        assert all(
            shortcut.isEnabled()
            for shortcut, _ in window._shortcut_sequences
        )
    finally:
        window.deleteLater()
