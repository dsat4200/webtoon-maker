"""Promotion preserves hierarchy appearance and agrees with picking/captures."""
from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainterPath

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, ParameterMaskBinding,
    RasterObject, ShapeStyle, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.eyedropper_sampling import EyedropperSampler, sample_color


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=240)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 240))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(320, 240)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 160, 120, 1
    yield canvas, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def layer(canvas, parent, name="Group", bounds=(20, 20, 200, 180), color=None, index=None):
    return canvas.chapter.add_layer(
        parent.layer_id, name, BoundGeometry.rectangle(*bounds), index=index,
        style=ShapeStyle(primary_color=color, outline_thickness=0),
    )


def ink(canvas, parent, color="red", center=(100, 100), width=80, index=None, alpha=1):
    obj = canvas.chapter.add_object(parent.layer_id, RasterObject(), index=index)
    canvas.tiles.paint_dab(obj.object_id, QPointF(*center), width, QColor(color), opacity=alpha)
    return obj


def preview(canvas):
    canvas.documentChanged.emit(QRectF())
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def pixels(image):
    return np.frombuffer(image.constBits(), dtype=np.uint8).copy()


def rgb(image, x=100, y=100):
    return image.pixelColor(x, y).getRgb()[:3]


def viewport(canvas):
    canvas.documentChanged.emit(QRectF())
    canvas._invalidate_scene_cache()
    canvas._ensure_scene_cache()
    return QImage(canvas._scene_cache)


def viewport_rgb(canvas, image, x=100, y=100):
    point = canvas.camera_transform().map(QPointF(x, y))
    ratio = image.devicePixelRatioF()
    return rgb(image, round(point.x() * ratio), round(point.y() * ratio))


@pytest.mark.parametrize("target_kind", ["object", "layer"])
def test_promoted_item_covers_ordinary_foreground_in_preview_and_viewport(scene, target_kind):
    canvas, page = scene
    back = layer(canvas, page)
    red = ink(canvas, back)
    front = layer(canvas, page, index=0)
    ink(canvas, front, "blue")
    assert rgb(preview(canvas)) == (0, 0, 255)
    target = red if target_kind == "object" else back
    target.show_on_top = True
    assert rgb(preview(canvas)) == (255, 0, 0)
    assert viewport_rgb(canvas, viewport(canvas)) == (255, 0, 0)
    # New ordinary foreground entries still cannot cover promoted content.
    foreground = layer(canvas, page, index=0)
    ink(canvas, foreground, "lime")
    assert rgb(preview(canvas)) == (255, 0, 0)


def test_promoted_object_keeps_parent_transform_clip_and_opacity(scene):
    canvas, page = scene
    group = layer(canvas, page, bounds=(20, 20, 80, 100))
    group.translate_x, group.translate_y, group.opacity = 100, 30, .5
    red = ink(canvas, group, center=(90, 70), width=80)
    red.show_on_top = True
    front = layer(canvas, page, bounds=(0, 0, 300, 240), color="#0000ff", index=0)
    result = preview(canvas)
    assert rgb(result, 185, 100) == pytest.approx((128, 0, 127), abs=1)
    assert rgb(result, 215, 100) == (0, 0, 255)  # Outside the translated clip.
    assert rgb(result, 85, 70) == (0, 0, 255)  # No untransformed duplicate.
    red.ignore_parent_mask = True
    assert rgb(preview(canvas), 215, 100) == pytest.approx((128, 0, 127), abs=1)
    assert front.visible


def test_nested_promoted_layer_and_object_are_painted_once(scene):
    canvas, page = scene
    group = layer(canvas, page)
    group.opacity = .5
    child = layer(canvas, group)
    child.opacity = .5
    obj = ink(canvas, child)
    obj.opacity, obj.opacity_locked = .5, False
    baseline = preview(canvas)
    group.show_on_top = child.show_on_top = obj.show_on_top = True
    result = preview(canvas)
    np.testing.assert_array_equal(pixels(result), pixels(baseline))
    assert result.pixelColor(100, 100).alpha() == pytest.approx(32, abs=1)
    assert result.pixelColor(100, 100).red() == 255


def test_multiple_promoted_items_keep_their_relative_outliner_order(scene):
    canvas, page = scene
    red = ink(canvas, page, "red")
    blue = ink(canvas, page, "blue", index=0)
    ink(canvas, page, "lime", index=0)
    red.show_on_top = blue.show_on_top = True
    assert rgb(preview(canvas)) == (0, 0, 255)
    hits = canvas.hit_test_objects(QPointF(100, 100))
    assert hits[:2] == [blue.object_id, red.object_id]


def test_promoted_objects_have_global_cross_page_picking_priority(scene):
    canvas, front_page = scene
    blue = ink(canvas, front_page, "blue")
    back_page = canvas.chapter.add_page("Back page", BoundGeometry.rectangle(0, 0, 1080, 240))
    back_page.fill_color, back_page.border_width = None, 0
    canvas.chapter.root_page_ids = [front_page.layer_id, back_page.layer_id]
    red = ink(canvas, back_page, "red")
    red.show_on_top = True
    assert rgb(preview(canvas)) == (255, 0, 0)
    assert canvas.hit_test_object(QPointF(100, 100)) == red.object_id
    hits = canvas.hit_test_entities(QPointF(100, 100))
    assert next(hit["id"] for hit in hits if hit["kind"] == "object") == red.object_id
    assert canvas.hit_test_objects(QPointF(100, 100)) == [red.object_id, blue.object_id]


@pytest.mark.parametrize("top_kind", ["object", "layer"])
def test_promotion_remains_visible_during_solo_without_showing_ancestors(scene, top_kind):
    canvas, page = scene
    group = layer(canvas, page, color="#00ff00")
    red = ink(canvas, group)
    top = red if top_kind == "object" else group
    top.show_on_top = True
    other_group = layer(canvas, page, bounds=(0, 0, 300, 230), index=0)
    solo = ink(canvas, other_group, "blue", center=(250, 100))
    canvas.set_solo_entities({("object", solo.object_id)})
    result = preview(canvas)
    assert rgb(result) == (255, 0, 0)
    assert rgb(result, 250, 100) == (0, 0, 255)
    if top_kind == "object":
        assert result.pixelColor(40, 40).alpha() == 0
    else:
        assert rgb(result, 40, 40) == (0, 255, 0)
    assert canvas.hit_test_object(QPointF(100, 100)) == red.object_id


@pytest.mark.parametrize("hidden", ["object", "parent", "page"])
def test_explicit_eye_visibility_still_hides_promoted_content(scene, hidden):
    canvas, page = scene
    group = layer(canvas, page)
    red = ink(canvas, group)
    red.show_on_top = True
    target = {"object": red, "parent": group, "page": page}[hidden]
    target.visible = False
    assert preview(canvas).pixelColor(100, 100).alpha() == 0
    assert not canvas.hit_test_objects(QPointF(100, 100))


def test_parent_effect_caches_keep_ordinary_and_promoted_content_separate(scene):
    canvas, page = scene
    group = layer(canvas, page, bounds=(10, 10, 280, 200))
    red = ink(canvas, group, "red", center=(70, 100))
    ink(canvas, group, "blue", center=(220, 100))
    canvas.chapter.add_modifier(BlurModifier(strength=2), [("layer", group.layer_id)])
    layer(canvas, page, bounds=(0, 0, 300, 230), color="#00ff00", index=0)
    assert rgb(preview(canvas), 70, 100) == (0, 255, 0)
    red.show_on_top = True
    for _ in range(2):
        result = preview(canvas)
        assert result.pixelColor(70, 100).red() > 250
        assert rgb(result, 220, 100) == (0, 255, 0)
    red.show_on_top = False
    assert rgb(preview(canvas), 70, 100) == (0, 255, 0)
    red.show_on_top = True
    assert preview(canvas).pixelColor(70, 100).red() > 250


def test_promoted_object_mask_keeps_independent_contributors(scene):
    canvas, page = scene
    group = layer(canvas, page)
    red = ink(canvas, group)
    contributor = ink(canvas, group, "black")
    contributor.mask_only = True
    mask = ToneMask(contributors=[("object", contributor.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    red.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    red.show_on_top = True
    front = layer(canvas, page, index=0)
    solo = ink(canvas, front, "blue")
    canvas.set_solo_entities({("object", solo.object_id)})
    assert rgb(preview(canvas)) == (255, 0, 0)


def test_export_and_eyedropper_match_promoted_scene_order(scene):
    canvas, page = scene
    red = ink(canvas, page)
    blue = ink(canvas, page, "blue", index=0)
    red.show_on_top = True
    canvas.set_solo_entities({("object", blue.object_id)})
    assert rgb(preview(canvas)) == (255, 0, 0)
    assert rgb(canvas.render_export_image()) == (255, 0, 0)
    assert QColor(sample_color(canvas, QPointF(100, 100))) == QColor("red")


def test_eyedropper_reads_finished_promoted_projection(scene, monkeypatch):
    canvas, page = scene
    back = layer(canvas, page)
    red = ink(canvas, back, alpha=.5)
    front = layer(canvas, page, index=0)
    ink(canvas, front, "blue")
    red.show_on_top = True
    expected = sample_color(canvas, QPointF(100, 100))
    canvas._document_projection_enabled = True
    canvas._projection_phase_batch(("base", "top"))
    assert canvas._projection_completed_view is not None
    import comic_editor.ui.eyedropper_sampling as sampling
    monkeypatch.setattr(sampling, "render_sample_region", lambda *_a, **_k:
                        pytest.fail("A finished promoted composite was rendered again"))
    assert EyedropperSampler(canvas).sample(QPointF(100, 100)) == expected


def test_independent_layer_capture_keeps_promoted_descendants_and_omits_other_tops(scene):
    canvas, page = scene
    group = layer(canvas, page, bounds=(20, 20, 200, 180))
    red = ink(canvas, group)
    red.show_on_top = True
    outside = ink(canvas, page, "blue", index=0)
    outside.show_on_top = True
    assert rgb(preview(canvas)) == (0, 0, 255)
    crop = canvas._render_entity_crop(canvas.chapter, canvas.tiles, "layer", group.layer_id)
    assert crop.pixelColor(80, 80) == QColor("red")
    assert rgb(preview(canvas)) == (0, 0, 255)


def test_compound_parent_promotion_moves_its_whole_appearance(scene):
    canvas, page = scene
    root = layer(canvas, page, bounds=(30, 40, 100, 100), color="#ff0000")
    root.compound_enabled, root.show_on_top = True, True
    layer(canvas, root, bounds=(100, 60, 80, 70), color="#0000ff")
    layer(canvas, page, bounds=(0, 0, 250, 200), color="#00ff00", index=0)
    image = preview(canvas)
    assert rgb(image, 70, 90) == (255, 0, 0)
    assert rgb(image, 160, 90) == (255, 0, 0)


def test_promoted_compound_contributor_paints_own_style_once(scene):
    canvas, page = scene
    root = layer(canvas, page, bounds=(30, 40, 100, 100), color="#ff0000")
    root.compound_enabled = True
    child = layer(canvas, root, bounds=(100, 60, 80, 70), color="#0000ff")
    child.show_on_top = True
    layer(canvas, page, bounds=(0, 0, 250, 200), color="#00ff00", index=0)
    image = preview(canvas)
    assert rgb(image, 70, 90) == (0, 255, 0)
    assert rgb(image, 160, 90) == (0, 0, 255)
    # Promotion must not remove the child's operand from the compound shape.
    assert canvas.layer_effective_path(root.layer_id).contains(QPointF(160, 90))


def test_overflow_view_promotes_artwork_but_keeps_inner_layer_clipping(scene):
    canvas, page = scene
    page.bound = BoundGeometry.rectangle(0, 0, 140, 200)
    canvas.chapter.view_overflow = 1
    back = layer(canvas, page, bounds=(0, 0, 300, 200))
    red = ink(canvas, back, "red", center=(220, 100))
    front = layer(canvas, page, bounds=(0, 0, 300, 200), index=0)
    ink(canvas, front, "blue", center=(220, 100))
    assert viewport_rgb(canvas, viewport(canvas), 220, 100) == (0, 0, 255)
    red.show_on_top = True
    assert viewport_rgb(canvas, viewport(canvas), 220, 100) == (255, 0, 0)
    back.bound = BoundGeometry.rectangle(0, 0, 180, 200)
    assert viewport_rgb(canvas, viewport(canvas), 220, 100) == (0, 0, 255)
    page.visible = False
    assert viewport_rgb(canvas, viewport(canvas), 220, 100) == (36, 36, 40)


def test_promoted_page_keeps_its_opacity_above_other_pages(scene):
    canvas, front = scene
    front.fill_color = "#0000ff"
    back = canvas.chapter.add_page("Back", BoundGeometry.rectangle(0, 0, 1080, 240))
    back.fill_color, back.border_width, back.opacity = "#ff0000", 0, .5
    canvas.chapter.root_page_ids = [front.layer_id, back.layer_id]
    assert rgb(preview(canvas)) == (0, 0, 255)
    back.show_on_top = True
    assert rgb(preview(canvas)) == pytest.approx((128, 0, 127), abs=1)
    assert rgb(canvas.render_export_image()) == pytest.approx((128, 0, 127), abs=1)
    back.visible = False
    assert rgb(preview(canvas)) == (0, 0, 255)


@pytest.mark.parametrize("target_kind", ["object", "layer"])
def test_promoted_artwork_retains_parent_blur_opacity_and_opacity_mask(scene, target_kind):
    canvas, page = scene
    group = layer(canvas, page)
    group.opacity = .5
    red = ink(canvas, group)
    canvas.chapter.add_modifier(BlurModifier(strength=2), [("layer", group.layer_id)])
    contributor = ink(canvas, page, "black", width=120, alpha=.5)
    contributor.mask_only = True
    mask = ToneMask(contributors=[("object", contributor.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    group.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    layer(canvas, page, bounds=(0, 0, 300, 230), color="#0000ff", index=0)
    assert rgb(preview(canvas)) == (0, 0, 255)
    target = red if target_kind == "object" else group
    target.show_on_top = True
    first = preview(canvas)
    assert rgb(first) == pytest.approx((64, 0, 191), abs=2)
    # Blur extends the dab's edge while its inherited alpha remains bounded.
    edge = first.pixelColor(141, 100)
    assert 0 < edge.red() < 50
    assert edge.blue() > 200
    np.testing.assert_array_equal(pixels(preview(canvas)), pixels(first))


def test_live_raster_object_transform_stays_below_promoted_artwork(scene, monkeypatch, wait_scene):
    canvas, page = scene
    back = layer(canvas, page, bounds=(0, 0, 300, 230))
    red = ink(canvas, back, center=(80, 100), width=60)
    top = layer(canvas, page, bounds=(140, 50, 120, 110), color="#0000ff", index=0)
    top.show_on_top = True
    canvas.set_selection("object", red.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    # The selection cage is UI; inspect the actual canvas artwork beneath it.
    monkeypatch.setattr(canvas, "_draw_selection", lambda *_args: None)
    start = QPointF(80, 100)
    assert canvas._begin_selected_raster_transform(start)
    canvas._transform_drag_mode = "translate"
    canvas._update_transform_preview(start + QPointF(100, 0))
    assert canvas._transform_preview_quad != canvas._transform_start_quad
    wait_scene(canvas)
    image = canvas.grab().toImage()
    assert viewport_rgb(canvas, image, 180, 100) == (0, 0, 255)
    top.visible = False
    canvas.documentChanged.emit(QRectF())
    wait_scene(canvas)
    assert viewport_rgb(canvas, canvas.grab().toImage(), 180, 100) == (255, 0, 0)
    canvas._clear_transform_preview()


def test_live_raster_lasso_selection_stays_below_promoted_artwork(scene, monkeypatch, wait_scene):
    canvas, page = scene
    back = layer(canvas, page, bounds=(0, 0, 300, 230))
    red = ink(canvas, back, center=(80, 100), width=60)
    top = layer(canvas, page, bounds=(140, 50, 120, 110), color="#0000ff", index=0)
    top.show_on_top = True
    canvas.set_selection("object", red.object_id)
    canvas.set_tool(ToolKind.DRAW_SELECT_LASSO)
    monkeypatch.setattr(canvas, "_draw_selection", lambda *_args: None)
    source = QPainterPath()
    source.addRect(QRectF(40, 60, 80, 80))
    canvas._drawing_selection_path = source
    canvas._selection_before_tiles = canvas.tiles.object_tiles(red.object_id)
    canvas._selection_transform_start_quad = canvas._rect_quad(source.boundingRect())
    canvas._selection_transform_quad = [
        (x + 100, y) for x, y in canvas._selection_transform_start_quad
    ]
    canvas.documentChanged.emit(QRectF())
    wait_scene(canvas)
    assert viewport_rgb(canvas, canvas.grab().toImage(), 180, 100) == (0, 0, 255)
    top.visible = False
    canvas.documentChanged.emit(QRectF())
    wait_scene(canvas)
    assert viewport_rgb(canvas, canvas.grab().toImage(), 180, 100) == (255, 0, 0)
    # The moved pixels exist only in the preview until selection commit.
    assert canvas.tiles.tile(red.object_id, (0, 0)).pixelColor(80, 100) == QColor("red")
    assert canvas.tiles.tile(red.object_id, (0, 0)).pixelColor(180, 100).alpha() == 0


def test_live_raster_predictive_ink_stays_below_top_and_clears_after_stroke(scene, monkeypatch, wait_scene):
    canvas, page = scene
    back = layer(canvas, page, bounds=(0, 0, 350, 230))
    drawing = canvas.chapter.add_object(back.layer_id, RasterObject())
    top = layer(canvas, page, bounds=(150, 50, 120, 110), color="#0000ff", index=0)
    top.show_on_top = True
    canvas.set_selection("object", drawing.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.primary_color = "#FF0000"
    canvas.settings.predictive_ink = True
    monkeypatch.setattr(canvas, "_draw_selection", lambda *_args: None)
    wait_scene(canvas)
    canvas._begin_stroke(QPointF(70, 100), 1)
    try:
        canvas._continue_stroke(QPointF(170, 100), 1)
        assert canvas._predictive is not None
        wait_scene(canvas)
        image = canvas.grab().toImage()
        assert viewport_rgb(canvas, image, 195, 100) == (0, 0, 255)
        # Removing the occluder shows the predictor, proving it was drawn.
        top.visible = False
        canvas.documentChanged.emit(QRectF())
        wait_scene(canvas)
        predicted = viewport_rgb(canvas, canvas.grab().toImage(), 195, 100)
        assert predicted[0] > predicted[1] + 50
    finally:
        canvas._end_stroke()
    wait_scene(canvas)
    ended = viewport_rgb(canvas, canvas.grab().toImage(), 195, 100)
    assert ended == (36, 36, 40)
