"""World-space mask changes must survive local modifier output/source reuse."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, HueSaturationLightnessModifier, ImageObject,
    ParameterMaskBinding, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=300, height=220, document_kind="asset",
                              background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 300, 220))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    yield canvas, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def layer(canvas, parent, name, rectangle=(0, 0, 100, 100)):
    result = canvas.chapter.add_layer(parent.layer_id, name, BoundGeometry.rectangle(*rectangle))
    result.fill_color, result.border_width = None, 0
    return result


def image_object(canvas, parent, x=0, y=0, size=100):
    result = canvas.chapter.add_object(parent.layer_id, ImageObject(
        x=x, y=y, pixel_width=size, pixel_height=size))
    source = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(150, 70, 25))
    canvas.images.put_decoded(result.object_id, "source.png", b"", source)
    return result


def painted_mask(canvas):
    result = ToneMask()
    canvas.chapter.masks[result.mask_id] = result
    canvas.tiles.paint_dab(result.mask_id, QPointF(25, 50), 90, QColor("white"))
    return result


def render(canvas):
    result = QImage(300, 220, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(result)
    return np.frombuffer(result.constBits(), np.uint8).reshape(220, 300, 4).copy()


def clear_cached_results(canvas, *, sources=False):
    canvas._effect_jobs.cancel()
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    if sources:
        canvas._modifier_source_cache.clear()
        canvas._modifier_source_cache_bytes = 0


def double_parent_size(parent):
    # Keeps both local capture bounds and the world origin unchanged.
    parent.transform_frame = (0, 0, 100, 100)
    parent.transform_quad = [(0, 0), (200, 0), (200, 200), (0, 200)]


@pytest.mark.parametrize("target_kind", ["object", "layer"])
@pytest.mark.parametrize("binding_kind", ["parameter", "opacity"])
def test_generic_output_follows_full_parent_mapping(scene, target_kind, binding_kind):
    canvas, page = scene
    parent = layer(canvas, page, "Transformed parent")
    target_layer = layer(canvas, parent, "Adjusted layer") if target_kind == "layer" else parent
    obj = image_object(canvas, target_layer)
    target = obj if target_kind == "object" else target_layer
    identifier = obj.object_id if target_kind == "object" else target_layer.layer_id
    modifier = HueSaturationLightnessModifier(hue=120)
    canvas.chapter.add_modifier(modifier, [(target_kind, identifier)])
    mask = painted_mask(canvas)
    if binding_kind == "parameter":
        modifier.parameter_masks["intensity"] = ParameterMaskBinding(
            mask_id=mask.mask_id, black_value=0, white_value=100)
    else:
        target.opacity_mask = ParameterMaskBinding(mask_id=mask.mask_id, black_value=0, white_value=1)
    render(canvas)
    double_parent_size(parent)
    reused = render(canvas)
    clear_cached_results(canvas, sources=True)
    expected = render(canvas)
    assert expected[..., 3].any()
    np.testing.assert_array_equal(reused, expected)


def test_generic_layer_source_recaptures_child_mask_after_parent_mapping_change(scene):
    canvas, page = scene
    parent = layer(canvas, page, "Transformed parent")
    adjusted = layer(canvas, parent, "Adjusted layer")
    obj = image_object(canvas, adjusted)
    mask = painted_mask(canvas)
    obj.opacity_mask = ParameterMaskBinding(mask_id=mask.mask_id, black_value=0, white_value=1)
    canvas.chapter.add_modifier(HueSaturationLightnessModifier(hue=120), [("layer", adjusted.layer_id)])
    render(canvas)
    double_parent_size(parent)
    # The outer effect must not re-use a capture containing the child's old
    # world-space mask, even when all completed output caches were discarded.
    clear_cached_results(canvas)
    reused_source = render(canvas)
    clear_cached_results(canvas, sources=True)
    expected = render(canvas)
    assert expected[..., 3].any()
    np.testing.assert_array_equal(reused_source, expected)


@pytest.mark.parametrize("contributor_kind", ["self_layer", "child_layer", "child_object"])
def test_mask_contributor_tracks_own_or_ancestor_live_layer_transform(scene, contributor_kind):
    canvas, page = scene
    moving = layer(canvas, page, "Moving group", (20, 20, 40, 40))
    if contributor_kind == "self_layer":
        moving.fill_color = "#ffffff"
        contributor = ("layer", moving.layer_id)
    elif contributor_kind == "child_layer":
        child = layer(canvas, moving, "Child mask", (20, 20, 40, 40))
        child.fill_color = "#ffffff"
        contributor = ("layer", child.layer_id)
    else:
        child = image_object(canvas, moving, 20, 20, 40)
        contributor = ("object", child.object_id)
    mask = ToneMask(contributors=[contributor])
    canvas.chapter.masks[mask.mask_id] = mask

    def field():
        return canvas.render_tone_mask_field(mask.mask_id, 300, 220,
            QTransform(), QRectF(0, 0, 300, 220))

    before_signature = canvas._tone_mask_signature(mask.mask_id)
    before = field()
    canvas._geometry_transform_target = ("layer_group", moving.layer_id)
    canvas._transform_preview_quad = [(90, 20), (130, 20), (130, 60), (90, 60)]
    assert canvas._tone_mask_signature(mask.mask_id) != before_signature
    reused = field()
    canvas._tone_mask_contributor_cache.clear()
    canvas._tone_mask_contributor_cache_bytes = 0
    expected = field()
    assert not np.array_equal(before, expected)
    np.testing.assert_array_equal(reused, expected)

    canvas._geometry_transform_target = None
    canvas._transform_preview_quad = None
    assert canvas._tone_mask_signature(mask.mask_id) == before_signature
    np.testing.assert_array_equal(field(), before)
