"""Editing a compound child keeps its live subtree and culls unrelated bubbles."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, ChapterDocument, HalftoneModifier, ImageObject, PathNode, PixelateModifier, ShapeStyle, TextObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def scene(qapp, text_outline_font_family):
    chapter = ChapterDocument(height=4000)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 4000))
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.resize(480, 480)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 240, 240, 1
    bubbles = []
    for y in (70, 900, 1800):
        bubble = chapter.add_layer(page.layer_id, "Bubble", BoundGeometry.rectangle(80, y, 200, 100),
            style=ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF000000", outline_thickness=4))
        bubble.compound_enabled = True
        tail = chapter.add_layer(bubble.layer_id, "Bent tail", BoundGeometry.path([
            PathNode(x=160, y=y+80, width_multiplier=4),
            PathNode(x=170, y=y+200, width_multiplier=2),
            PathNode(x=270, y=y+170, width_multiplier=.1),
        ], closed=False), layer_kind="open_shape", style=ShapeStyle(
            primary_color="#FFFFFFFF", base_thickness=12, outline_thickness=4))
        text = chapter.add_object(bubble.layer_id, TextObject(text="Visible", font_family=text_outline_font_family))
        bubbles.append((bubble, tail, text))
    yield canvas, chapter, bubbles
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).copy()


def render(canvas, culling=True):
    prepare = canvas._render_bounds.prepare
    if not culling:
        def disabled():
            prepare()
            canvas._render_bounds.enabled = False
        canvas._render_bounds.prepare = disabled
    try:
        canvas._invalidate_scene_cache()
        canvas._ensure_scene_cache()
        return QImage(canvas._scene_cache)
    finally:
        canvas._render_bounds.prepare = prepare


@pytest.mark.parametrize("gesture", ["node", "transform"])
def test_live_child_edits_skip_offscreen_compound_outlines(scene, monkeypatch, gesture):
    canvas, _, bubbles = scene
    bubble, tail, _ = bubbles[0]
    render(canvas)
    canvas.set_selection("layer", tail.layer_id)
    if gesture == "node":
        tail.bound.nodes[1].x += 20
        canvas.documentChanged.emit(QRectF())
    else:
        x, y, width, height = tail.bound.bbox()
        canvas._geometry_transform_target = ("layer_group", tail.layer_id)
        canvas._transform_preview_quad = [(x+20, y), (x+width+20, y),
                                          (x+width+20, y+height), (x+20, y+height)]
        canvas._compound_path_cache.clear()
    expected = pixels(render(canvas, False))
    calls = []
    original = canvas._render_compound_layer_contents

    def observe(painter, layer, *args):
        calls.append(layer.layer_id)
        return original(painter, layer, *args)

    monkeypatch.setattr(canvas, "_render_compound_layer_contents", observe)
    np.testing.assert_array_equal(pixels(render(canvas)), expected)
    assert bubble.layer_id in calls
    assert all(other.layer_id not in calls for other, _, _ in bubbles[1:])


@pytest.mark.parametrize("projective", [False, True])
def test_live_group_transform_keeps_descendants_with_stale_offscreen_bounds(scene, projective):
    canvas, _, bubbles = scene
    bubble, tail, text = bubbles[1]
    render(canvas)
    # Cache each descendant outside the viewport before the preview begins.
    for kind, identifier in (("layer", tail.layer_id), ("object", text.object_id)):
        canvas._render_bounds.entity_bounds(kind, identifier)
    canvas.set_selection("layer", bubble.layer_id)
    canvas._geometry_transform_target = ("layer_group", bubble.layer_id)
    canvas._transform_preview_quad = [(250, 40), (450, 40),
                                      (430 if projective else 450, 140), (250, 140)]
    canvas._compound_path_cache.clear()
    expected = pixels(render(canvas, False))
    np.testing.assert_array_equal(pixels(render(canvas)), expected)
    assert ("object", text.object_id) in canvas._render_bounds.live_branches
    assert ("layer", tail.layer_id) in canvas._render_bounds.live_branches
    # Canceling restores the original scene without retaining preview bounds.
    canvas._geometry_transform_target = None
    canvas._transform_preview_quad = None
    canvas._compound_path_cache.clear()
    np.testing.assert_array_equal(pixels(render(canvas)), pixels(render(canvas, False)))


def test_offscreen_compound_keeps_escaped_child_pixels(scene):
    canvas, chapter, bubbles = scene
    bubble, _, _ = bubbles[2]
    obj = chapter.add_object(bubble.layer_id, ImageObject(
        x=20, y=20, pixel_width=30, pixel_height=30, ignore_parent_mask=True))
    image = QImage(30, 30, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    canvas.images.put_decoded(obj.object_id, "sample.png", b"", image)
    result = render(canvas)
    assert result.pixelColor(30, 30) == QColor("red")
    np.testing.assert_array_equal(pixels(result), pixels(render(canvas, False)))


def test_group_preview_before_prepare_does_not_use_stale_visibility(scene):
    canvas, _, bubbles = scene
    render(canvas)
    bubble, _, text = bubbles[1]
    canvas._geometry_transform_target = ("layer_group", bubble.layer_id)
    canvas._transform_preview_quad = [(0, 0), (200, 0), (200, 100), (0, 100)]
    canvas._interactive_render = True
    try:
        assert not canvas._render_bounds.usable()
        assert canvas._render_bounds.object_visible(text, QRectF(0, 0, 480, 480))
    finally:
        canvas._interactive_render = False


@pytest.mark.parametrize("gesture", ["node", "transform"])
@pytest.mark.parametrize("nested", [False, True])
def test_operand_edit_retains_fitted_image_sibling_with_stale_bounds(scene, gesture, nested):
    canvas, chapter, bubbles = scene
    bubble, tail, _ = bubbles[1]
    owner = tail if nested else bubble
    if nested:
        tail.layer_kind = "bounded"
        tail.bound = BoundGeometry.rectangle(100, 980, 100, 100)
        tail.compound_enabled = True
    operand = chapter.add_layer(owner.layer_id, "Moving operand",
                                BoundGeometry.rectangle(80, 1000, 100, 100))
    image_object = chapter.add_object(bubble.layer_id, ImageObject(
        pixel_width=30, pixel_height=30, placement_mode="fit_parent", fit_mode="stretch"))
    image = QImage(30, 30, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    canvas.images.put_decoded(image_object.object_id, "sample.png", b"", image)
    render(canvas)
    cached = canvas._render_bounds.entity_bounds("object", image_object.object_id)
    assert cached.top() >= 900
    canvas.set_selection("layer", operand.layer_id)
    if gesture == "transform":
        canvas._geometry_transform_target = ("layer_group", operand.layer_id)
        canvas._transform_preview_quad = [(350, 100), (450, 100), (450, 200), (350, 200)]
    else:
        operand.bound = BoundGeometry.rectangle(350, 100, 100, 100)
        canvas.documentChanged.emit(QRectF())
    canvas._compound_path_cache.clear()
    actual = render(canvas)
    expected = render(canvas, False)
    assert expected.pixelColor(390, 150) == QColor("red")
    np.testing.assert_array_equal(pixels(actual), pixels(expected))
    assert ("object", image_object.object_id) in canvas._render_bounds.live_branches


@pytest.mark.parametrize("modifier", [HalftoneModifier(size=5), PixelateModifier(pixel_size=9)])
def test_unrelated_patterned_images_stay_culled_while_editing_child(scene, monkeypatch, modifier):
    canvas, chapter, bubbles = scene
    _, tail, _ = bubbles[0]
    page_id = chapter.root_page_ids[0]
    obj = chapter.add_object(page_id, ImageObject(x=30, y=1500, pixel_width=50, pixel_height=50))
    image = QImage(50, 50, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("blue"))
    canvas.images.put_decoded(obj.object_id, "sample.png", b"", image)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    canvas.set_selection("layer", tail.layer_id)
    x, y, width, height = tail.bound.bbox()
    canvas._geometry_transform_target = ("layer_group", tail.layer_id)
    canvas._transform_preview_quad = [(x+10, y), (x+width+10, y),
                                      (x+width+10, y+height), (x+10, y+height)]
    expected = pixels(render(canvas, False))
    original = canvas._render_modified_object

    def checked(painter, target, *args):
        assert target.object_id != obj.object_id, "Offscreen image rebuilt its pattern during shape editing"
        return original(painter, target, *args)

    with monkeypatch.context() as patch:
        patch.setattr(canvas, "_render_modified_object", checked)
        np.testing.assert_array_equal(pixels(render(canvas)), expected)
    # Panning to that image still renders its complete effect.
    canvas.center_y = 1500
    np.testing.assert_array_equal(pixels(render(canvas)), pixels(render(canvas, False)))
