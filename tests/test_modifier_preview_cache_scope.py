"""A live shape transform invalidates only dependent modifier captures."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ImageObject, MirrorModifier, PathNode,
    PixelateModifier, ShapeStyle,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=480, height=320)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 480, 320))
    page.fill_color, page.border_width = None, 0
    bubble = chapter.add_layer(page.layer_id, "Bubble", BoundGeometry.rectangle(50, 40, 160, 90),
        style=ShapeStyle(primary_color="#FFFFFFFF", outline_thickness=4))
    bubble.compound_enabled = True
    tail = chapter.add_layer(bubble.layer_id, "Tail", BoundGeometry.path([
        PathNode(x=150, y=110, width_multiplier=3),
        PathNode(x=180, y=190, width_multiplier=.1),
    ], closed=False), layer_kind="open_shape", style=ShapeStyle(
        primary_color="#FFFFFFFF", base_thickness=12, outline_thickness=4))
    obj = chapter.add_object(page.layer_id, ImageObject(
        x=300, y=45, pixel_width=80, pixel_height=70))
    image = QImage(80, 70, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#FF2040D0"))
    painter = QPainter(image)
    painter.fillRect(4, 6, 27, 43, QColor("#FFF04020"))
    painter.end()
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(480, 320)
    canvas.set_document(chapter, TileStore(), reset_view=False)
    canvas.scale = 1
    canvas.images.put_decoded(obj.object_id, "sample.png", b"", image)
    yield canvas, bubble, tail, obj
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


def render(canvas):
    image = QImage(480, 320, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return np.frombuffer(image.constBits(), np.uint8).copy()


def clear_captures(canvas):
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._effect_jobs.cancel()


@pytest.mark.parametrize("effect", ["mirror", "pixelate"])
def test_child_transform_reuses_unrelated_visible_effect_source(scene, monkeypatch, effect):
    from comic_editor.ui import modifier_rendering

    canvas, _, tail, obj = scene
    modifier = (MirrorModifier(axis_start=(380, 0), axis_end=(380, 320))
                if effect == "mirror" else PixelateModifier(pixel_size=7))
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    before = render(canvas)
    captures = []
    original = canvas._render_object_content

    def capture(painter, target, *args):
        if target.object_id == obj.object_id:
            captures.append(target.object_id)
        return original(painter, target, *args)

    monkeypatch.setattr(canvas, "_render_object_content", capture)
    effects = []
    apply_pattern = modifier_rendering.apply_pattern_modifier

    def apply(*args, **kwargs):
        effects.append(True)
        return apply_pattern(*args, **kwargs)

    monkeypatch.setattr(modifier_rendering, "apply_pattern_modifier", apply)
    canvas.set_selection("layer", tail.layer_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.TRANSFORM)
    start = QPointF(160, 135)
    assert canvas._begin_geometry_transform(start)
    assert canvas._transform_drag_mode == "translate"
    for delta in (QPointF(8, 5), QPointF(17, 11)):
        canvas._update_geometry_transform_preview(start + delta)
        actual = render(canvas)
    assert not captures, "Unrelated modified artwork was captured again during the tail drag"
    assert not effects, "Unrelated patterned artwork reran its effect during the tail drag"
    assert not np.array_equal(actual, before)
    clear_captures(canvas)
    np.testing.assert_array_equal(render(canvas), actual)


@pytest.mark.parametrize("primary", [True, False], ids=["primary", "secondary"])
def test_selected_image_preview_changes_capture_with_unchanged_bounds(scene, primary):
    canvas, _, _, obj = scene
    canvas.chapter.add_modifier(PixelateModifier(pixel_size=7), [("object", obj.object_id)])
    if primary:
        canvas.set_selection("object", obj.object_id, activate_default_tool=False)
    else:
        other = canvas.chapter.add_object(obj.parent_layer_id, ImageObject(
            x=20, y=270, pixel_width=1, pixel_height=1))
        refs = [("object", other.object_id), ("object", obj.object_id)]
        canvas.set_selection_set(refs, primary=refs[0])
    before = render(canvas)
    original = canvas.object_world_quad(obj.object_id)
    destination = [original[1], original[0], original[3], original[2]]
    if primary:
        canvas._transform_start_quad = list(original)
        canvas._transform_preview_quad = destination
    else:
        canvas._multi_transform_preview_quads[obj.object_id] = destination
    actual = render(canvas)
    assert not np.array_equal(actual, before)
    clear_captures(canvas)
    np.testing.assert_array_equal(render(canvas), actual)


def test_modified_parent_recaptures_transformed_child(scene):
    canvas, bubble, tail, _ = scene
    canvas.chapter.add_modifier(
        MirrorModifier(axis_start=(230, 0), axis_end=(230, 320)),
        [("layer", bubble.layer_id)])
    canvas.set_selection("layer", tail.layer_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.TRANSFORM)
    before = render(canvas)
    start = QPointF(160, 135)
    assert canvas._begin_geometry_transform(start)
    canvas._update_geometry_transform_preview(start + QPointF(20, 14))
    actual = render(canvas)
    assert not np.array_equal(actual, before)
    clear_captures(canvas)
    np.testing.assert_array_equal(render(canvas), actual)
