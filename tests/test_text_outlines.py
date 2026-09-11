"""Canvas regressions for text outline rendering, editing, and cache reuse."""
from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, OutlineModifier, ParameterMaskBinding, TextObject, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.baking import rasterize
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def canvas(qapp, text_outline_font_family):
    chapter = ChapterDocument(height=480)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 480))
    page.fill_color, page.border_width = None, 0
    widget = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    widget.resize(700, 500)
    widget.set_document(chapter, TileStore())
    widget._test_outline_font_family = text_outline_font_family
    widget.center_x, widget.center_y, widget.scale = 350, 240, 1
    widget.set_selection("layer", page.layer_id)
    yield widget
    widget._effect_jobs.cancel()
    widget.hide()
    widget.deleteLater()


def add_text(canvas, target_kind="object", *, strict=False):
    parent = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    if strict:
        parent = canvas.chapter.add_layer(
            parent.layer_id, "Bubble", BoundGeometry.rectangle(60, 60, 340, 160))
        parent.fill_color, parent.border_width = None, 0
    elif target_kind == "container":
        parent = canvas.chapter.add_layer(parent.layer_id, "Text", layer_kind="text_container")
    rect = QRectF(80, 80, 280, 110)
    obj = canvas.chapter.add_object(parent.layer_id, TextObject(
        text="Outline me", font_family=canvas._test_outline_font_family,
        font_size=38, margin=12 if strict else 0,
        layout_mode="strict" if strict else "free",
        x=rect.x(), y=rect.y(), width=rect.width(), height=rect.height(),
        transform_quad=None if strict else canvas._rect_quad(rect),
    ))
    target = ("layer", parent.layer_id) if target_kind == "container" else ("object", obj.object_id)
    return obj, parent, target


def outline(canvas, target, **kwargs):
    modifier = OutlineModifier(thickness=5, color="#FFFF0000", **kwargs)
    canvas.chapter.add_modifier(modifier, [target])
    return modifier


def render(canvas, image=None, clip=None):
    if image is None:
        image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image, clip)
    return image


def rgba(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(
        image.height(), image.width(), 4).copy()


def clear_effect_images(canvas):
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._outline_distance_cache.clear()


@pytest.mark.parametrize("target_kind,strict", [("object", False), ("object", True), ("container", False)])
def test_text_outline_follows_glyphs_preserves_ink_and_mutes(canvas, target_kind, strict):
    obj, parent, target = add_text(canvas, target_kind, strict=strict)
    source = rgba(render(canvas))
    modifier = outline(canvas, target)
    result = rgba(render(canvas))
    opaque = source[..., 3] == 255
    np.testing.assert_array_equal(result[opaque], source[opaque])
    outside = (source[..., 3] == 0) & (result[..., 3] > 0)
    assert outside.sum() > 100
    assert np.all(result[outside, :3] == (255, 0, 0))
    # Whitespace in the text frame stays transparent, rather than a box outline.
    assert result[85, 85, 3] == 0
    modifier.muted = True
    np.testing.assert_array_equal(rgba(render(canvas)), source)


@pytest.mark.parametrize("target_kind", ["object", "container"])
def test_text_outline_slider_edits_reuse_source_and_distance(canvas, target_kind):
    _, _, target = add_text(canvas, target_kind)
    modifier = outline(canvas, target)
    initial = rgba(render(canvas))
    source_keys = set(canvas._modifier_source_cache)
    builds = canvas._outline_distance_cache.computations
    assert builds > 0
    for thickness in (1.25, 3.5, 8, 16, 25):
        modifier.thickness = thickness
        modifier.opacity = 75
        modifier.intensity = 80
        modifier.color = "#FF2080FF"
        current = rgba(render(canvas))
        assert not np.array_equal(initial, current)
        assert set(canvas._modifier_source_cache) == source_keys
        assert canvas._outline_distance_cache.computations == builds
    assert canvas._outline_distance_cache.bytes <= canvas._outline_distance_cache.budget
    assert canvas._modifier_source_cache_bytes <= canvas._modifier_source_cache_budget
    assert canvas._modifier_render_cache_bytes <= canvas._modifier_render_cache_budget


@pytest.mark.parametrize("target_kind", ["object", "container"])
@pytest.mark.parametrize("attribute,value", [
    ("text", "Different words"), ("font_size", 50), ("kerning", 3), ("bold", True),
])
def test_text_outline_content_edits_invalidate_cached_source(canvas, target_kind, attribute, value):
    obj, _, target = add_text(canvas, target_kind)
    outline(canvas, target)
    before = rgba(render(canvas))
    source_keys = set(canvas._modifier_source_cache)
    setattr(obj, attribute, value)
    updated = rgba(render(canvas))
    assert set(canvas._modifier_source_cache) != source_keys
    assert not np.array_equal(updated, before)
    clear_effect_images(canvas)
    np.testing.assert_array_equal(rgba(render(canvas)), updated)


def test_strict_text_outline_reflows_after_parent_geometry_edit(canvas):
    obj, parent, target = add_text(canvas, strict=True)
    obj.text = "Several words that should wrap onto another line"
    outline(canvas, target)
    before = rgba(render(canvas))
    parent.bound = BoundGeometry.rectangle(60, 60, 240, 230)
    canvas.documentChanged.emit(canvas.entity_world_rect("layer", parent.layer_id))
    updated = rgba(render(canvas))
    assert not np.array_equal(before, updated)
    clear_effect_images(canvas)
    np.testing.assert_array_equal(rgba(render(canvas)), updated)


@pytest.mark.parametrize("target_kind", ["object", "container"])
def test_text_outline_stack_order_and_partial_render(canvas, target_kind):
    _, parent, target = add_text(canvas, target_kind)
    first = outline(canvas, target)
    second = OutlineModifier(thickness=4, color="#FF0000FF")
    canvas.chapter.add_modifier(second, [target])
    target_model = canvas.chapter.modifier_target(*target)
    before = render(canvas)
    target_model.modifier_ids.reverse()
    after = render(canvas)
    assert before != after
    # Dirty-region rendering replaces old pixels with the complete new stack.
    render(canvas, before, QRect(40, 40, 400, 220))
    assert before == after
    first.muted = True
    only_second = rgba(render(canvas))
    target_model.modifier_ids.remove(first.modifier_id)
    np.testing.assert_array_equal(rgba(render(canvas)), only_second)


@pytest.mark.parametrize("target_kind", ["object", "container"])
def test_text_outline_live_caret_selection_typing_and_clean_export(canvas, qapp, target_kind):
    obj, _, target = add_text(canvas, target_kind)
    outline(canvas, target)
    canvas.set_selection("object", obj.object_id)
    canvas.show()
    canvas.activateWindow()
    canvas.setFocus()
    qapp.processEvents()

    def capture_without_caret():
        canvas._text_caret_timer.stop()
        canvas._text_caret_visible = False
        return canvas.grab().toImage()

    resting = capture_without_caret()
    export = render(canvas)
    assert canvas.start_text_edit()
    editing = capture_without_caret()
    assert editing == resting
    source_keys = set(canvas._modifier_source_cache)
    builds = canvas._outline_distance_cache.computations
    canvas._text_caret_visible = True
    assert canvas.grab().toImage() != editing
    QTest.keyClick(canvas, Qt.Key_A, Qt.ControlModifier)
    assert capture_without_caret() != editing
    assert render(canvas) == export
    assert set(canvas._modifier_source_cache) == source_keys
    assert canvas._outline_distance_cache.computations == builds
    QTest.keyClicks(canvas, "Live!")
    live = capture_without_caret()
    assert obj.text == "Live!"
    assert live != editing
    typed_export = render(canvas)
    assert typed_export != export
    canvas.commit_active_text_edit()
    assert capture_without_caret() == live
    assert render(canvas) == typed_export


@pytest.mark.parametrize("target_kind", ["object", "container"])
def test_transformed_text_outline_rasterization_preserves_pixels_and_undo(canvas, target_kind):
    obj, parent, target = add_text(canvas, target_kind)
    obj.transform_quad = [(80, 65), (370, 95), (355, 215), (65, 175)]
    if target_kind == "container":
        parent.translate_x, parent.translate_y = 30, 10
        parent.opacity = .7
    else:
        obj.opacity_locked, obj.opacity = False, .7
    outline(canvas, target)
    before = rgba(render(canvas))
    rasterize(canvas, *target)
    after = rgba(render(canvas))
    np.testing.assert_allclose(after.astype(int), before.astype(int), atol=2)
    canvas.command_stack.undo()
    assert isinstance(canvas.chapter.objects[obj.object_id], TextObject)
    np.testing.assert_array_equal(rgba(render(canvas)), before)


@pytest.mark.parametrize("target_kind", ["object", "container"])
def test_text_outline_stretch_preview_uses_current_geometry_before_release(canvas, target_kind):
    obj, _, target = add_text(canvas, target_kind)
    obj.transform_behavior = "stretch"
    outline(canvas, target)
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.commit_active_text_edit()
    before = rgba(render(canvas))
    _, frame, mapping, _ = canvas._text_frame_target()
    start = mapping.map(frame.bottomRight())
    assert canvas._begin_free_text_transform(start)
    canvas._queue_free_text_drag(start+QPointF(80, 35))
    canvas._flush_free_text_drag()
    assert canvas._free_text_drag is not None
    live = rgba(render(canvas))
    assert not np.array_equal(before, live)
    clear_effect_images(canvas)
    np.testing.assert_array_equal(rgba(render(canvas)), live)
    assert canvas._finish_free_text_drag()
    np.testing.assert_array_equal(rgba(render(canvas)), live)
    canvas.command_stack.undo()
    np.testing.assert_array_equal(rgba(render(canvas)), before)


@pytest.mark.parametrize("target_kind", ["object", "container"])
@pytest.mark.parametrize("attribute,white_value", [("thickness", 25), ("opacity", 100), ("intensity", 100)])
def test_text_outline_parameter_mask_paint_updates_without_source_rebuild(canvas, target_kind, attribute, white_value):
    _, parent, target = add_text(canvas, target_kind)
    source = rgba(render(canvas))
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    modifier = outline(canvas, target)
    modifier.parameter_masks[attribute] = ParameterMaskBinding(mask.mask_id, 0, white_value)
    canvas.tiles.paint_dab(mask.mask_id, QPointF(400, 150), 300, QColor("white"), square=True, antialias=False)
    masked = rgba(render(canvas))
    outside = source[..., 3] == 0
    assert np.any(masked[:, 260:, 3][outside[:, 260:]] > 0)
    assert not np.any(masked[:, :200, 3][outside[:, :200]] > 0)
    source_keys = set(canvas._modifier_source_cache)
    builds = canvas._outline_distance_cache.computations
    canvas.tiles.paint_dab(mask.mask_id, QPointF(120, 150), 230, QColor("white"), square=True, antialias=False)
    updated = rgba(render(canvas))
    assert np.any(updated[:, :200, 3][outside[:, :200]] > 0)
    assert set(canvas._modifier_source_cache) == source_keys
    assert canvas._outline_distance_cache.computations == builds
    clear_effect_images(canvas)
    np.testing.assert_array_equal(rgba(render(canvas)), updated)


@pytest.mark.parametrize("target_kind", ["object", "container"])
def test_outlined_text_edit_overlay_respects_ignore_parent_mask(canvas, target_kind):
    obj, _, target = add_text(canvas, target_kind)
    clip = canvas.chapter.add_layer(canvas.chapter.root_page_ids[0], "Small clip",
                                   BoundGeometry.rectangle(30, 30, 35, 35))
    clip.fill_color, clip.border_width = None, 0
    canvas.chapter.move_entity(*target, clip.layer_id, 0)
    canvas.chapter.modifier_target(*target).ignore_parent_mask = True
    outline(canvas, target)
    original = render(canvas)
    assert np.any(rgba(original)[100:200, 100:360, 3])
    canvas.set_selection("object", obj.object_id)
    assert canvas.start_text_edit(select_all=True)
    overlay = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    overlay.fill(Qt.transparent)
    painter = QPainter(overlay)
    try:
        canvas._draw_selected_text_edit_overlay(painter, obj)
    finally:
        painter.end()
    # The selected glyphs lie outside the intentionally ignored parent shape.
    assert np.any(rgba(overlay)[100:200, 100:360, 3])
    assert render(canvas) == original
