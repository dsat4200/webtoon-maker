"""Curves adjusts every current renderable target without widening other effects."""
import json
import uuid

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage

from comic_editor.core.fill_migration import materialize_legacy_fills
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    ArrayModifier, BlenderComicViewSourceDescriptor, BlurModifier, BoundGeometry,
    BrightnessContrastModifier, CageTransformModifier, ChapterDocument,
    ColorFillGradientObject, ColorGradientRamp, ColorGradientStop, CurvesModifier,
    DistortModifier, DocumentObject, DotDashModifier, HalftoneModifier,
    HueSaturationLightnessModifier, ImageObject, LayerNode, LineGradientField,
    MirrorModifier, OutlineModifier, PathNode, PixelateModifier, PosterizeModifier,
    PosterizeValueModifier, RadialBlurModifier, RadialGradientField, RasterObject,
    ScreamModifier, ShapeGradientField, TextObject, TilingModifier,
    VectorDrawingObject, VectorStroke, VectorStrokePoint, WobbleModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_rendering import _qimage_premultiplied


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=180, height=160, document_kind="asset", background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 180, 160))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    yield canvas, chapter, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def adjustment():
    return CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .8), (1, 1)]})


def render(canvas):
    output = QImage(180, 160, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(output)
    return output


def raster(canvas, chapter, parent, **kwargs):
    obj = chapter.add_object(parent.layer_id, RasterObject(**kwargs))
    canvas.tiles.paint_dab(obj.object_id, QPointF(80, 80), 45, QColor(86, 107, 128), opacity=.7)
    return obj


def target(scene, kind, font):
    canvas, chapter, page = scene
    if kind == "page":
        raster(canvas, chapter, page)
        page.fill_color = "#80505050"
        return "layer", page.layer_id
    if kind in {"shape", "open_shape", "compound", "compound_operand", "text_container", "mask_only_layer"}:
        bound = BoundGeometry.rectangle(30, 30, 110, 100)
        if kind == "open_shape":
            bound = BoundGeometry.path([PathNode(x=30, y=80), PathNode(x=140, y=80)], closed=False)
        layer = chapter.add_layer(page.layer_id, kind, bound,
            layer_kind="text_container" if kind == "text_container" else "open_shape" if kind == "open_shape" else "bounded")
        layer.fill_color, layer.border_width = "#B4606060", 0
        layer.mask_only = kind == "mask_only_layer"
        if kind == "text_container":
            layer.fill_color = None
            chapter.add_object(layer.layer_id, TextObject(x=30, y=40, width=110, height=70,
                text="Curve", font_family=font, font_size=27, margin=0, layout_mode="free", text_color="#B4606060"))
        elif kind == "open_shape":
            layer.shape_style.base_thickness = 20
        elif kind in {"compound", "compound_operand"}:
            layer.compound_enabled = True
            child = chapter.add_layer(layer.layer_id, "Operand", BoundGeometry.rectangle(45, 45, 110, 100))
            child.fill_color, child.border_width = None, 0
            raster(canvas, chapter, child)
            if kind == "compound_operand":
                return "layer", child.layer_id
        return "layer", layer.layer_id
    if kind in {"image", "blender"}:
        obj = ImageObject(x=30, y=30, pixel_width=110, pixel_height=100)
        if kind == "blender":
            obj.source = BlenderComicViewSourceDescriptor(project_uuid=uuid.UUID(int=1).hex, view_uuid=uuid.UUID(int=2).hex)
        chapter.add_object(page.layer_id, obj)
        source = QImage(110, 100, QImage.Format_ARGB32_Premultiplied)
        source.fill(QColor(86, 107, 128, 180))
        canvas.images.put_decoded(obj.object_id, "source.png", b"", source)
    elif kind in {"text", "text_strict"}:
        obj = chapter.add_object(page.layer_id, TextObject(x=30, y=40, width=120, height=70,
            text="Curve", font_family=font, font_size=27, margin=0,
            layout_mode="strict" if kind == "text_strict" else "free", text_color="#B4606060"))
    elif kind == "vector":
        obj = chapter.add_object(page.layer_id, VectorDrawingObject(strokes=[VectorStroke(color="#B4606060",
            points=[VectorStrokePoint(x=40, y=60, width=22), VectorStrokePoint(x=135, y=95, width=28)])]))
    elif kind.startswith("gradient_"):
        style = kind[len("gradient_"):]
        parent = chapter.add_layer(page.layer_id, "Field", BoundGeometry.rectangle(35, 35, 105, 85))
        parent.fill_color, parent.border_width = None, 0
        obj = chapter.add_object(parent.layer_id, ColorFillGradientObject(
            field_type="line" if style == "line" else "radial" if "radial" in style else "parent_shape",
            line_field=LineGradientField(BoundGeometry.path([PathNode(x=35, y=80), PathNode(x=140, y=80)])),
            radial_field=RadialGradientField(origin_x=87, origin_y=77, radius_x=28, radius_y=19,
                ellipse_enabled=True, rotation=23, reverse_direction=style == "outward_radial",
                uniform=style == "uniform_radial", distance=18),
            shape_field=ShapeGradientField(reverse_direction=style == "outward_shape", distance=18),
            ramp=ColorGradientRamp(stops=[ColorGradientStop(position=0, color="#80662299"),
                                         ColorGradientStop(position=1, color="#C0339966")]),
        ))
    else:
        obj = raster(canvas, chapter, page, mask_only=kind == "mask_only")
    return "object", obj.object_id


KINDS = ["raster", "image", "blender", "text", "text_strict", "vector", "shape", "open_shape",
         "compound", "compound_operand", "text_container", "page", "mask_only", "mask_only_layer",
         "gradient_line", "gradient_radial", "gradient_uniform_radial", "gradient_shape",
         "gradient_outward_radial", "gradient_outward_shape"]


@pytest.mark.parametrize("kind", KINDS)
def test_every_current_target_changes_pixels_preserves_coverage_and_reloads(scene, text_outline_font_family, kind):
    canvas, chapter, _page = scene
    ref = target(scene, kind, text_outline_font_family)
    if kind.startswith("mask_only"):
        # Mask-only content stays hidden in normal export, but is still an
        # editable/renderable source when explicitly captured as a contributor.
        assert not np.any(_qimage_premultiplied(render(canvas)))
        canvas._rendering_mask_contributor = 1
    before = _qimage_premultiplied(render(canvas))
    modifier = adjustment()
    chapter.add_modifier(modifier, [ref])
    after = _qimage_premultiplied(render(canvas))
    assert np.count_nonzero(after[..., :3] - before[..., :3] > .02) > 50
    np.testing.assert_allclose(after[..., 3], before[..., 3], atol=1/255)
    restored = ChapterDocument.from_dict(json.loads(json.dumps(chapter.to_dict())))
    assert restored.modifier_target(*ref).modifier_ids == [modifier.modifier_id]
    canvas.set_document(restored, canvas.tiles, canvas.images)
    if kind.startswith("mask_only"):
        canvas._rendering_mask_contributor = 1
    np.testing.assert_array_equal(_qimage_premultiplied(render(canvas)), after)


@pytest.mark.parametrize("factory", [ArrayModifier, BlurModifier, BrightnessContrastModifier,
    CageTransformModifier, DistortModifier, DotDashModifier, HalftoneModifier,
    HueSaturationLightnessModifier, MirrorModifier, OutlineModifier, PixelateModifier,
    PosterizeModifier, PosterizeValueModifier, RadialBlurModifier, ScreamModifier,
    TilingModifier, WobbleModifier])
def test_exposing_pages_does_not_enable_any_existing_modifier(scene, factory):
    _canvas, chapter, page = scene
    ref = ("layer", page.layer_id)
    assert chapter.modifier_target(*ref) is page
    assert not chapter.stroke_modifier_target(*ref)
    with pytest.raises(ValueError, match="Pages support Curves"):
        chapter.add_modifier(factory(), [ref])
    assert not page.modifier_ids


def test_page_load_preserves_curves_but_still_removes_unsupported_legacy_links(scene):
    _canvas, chapter, page = scene
    curves, blur = adjustment(), BlurModifier()
    chapter.modifiers.update({m.modifier_id: m for m in (curves, blur)})
    page.modifier_ids = [curves.modifier_id, blur.modifier_id]
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.layers[page.layer_id].modifier_ids == [curves.modifier_id]


def test_text_gradient_and_nonrendered_targets_keep_existing_restrictions(scene, text_outline_font_family):
    _canvas, chapter, page = scene
    for kind in ("text", "gradient_line"):
        ref = target(scene, kind, text_outline_font_family)
        with pytest.raises(ValueError):
            chapter.add_modifier(HueSaturationLightnessModifier(), [ref])
    invisible = chapter.add_object(page.layer_id, DocumentObject())
    assert chapter.incompatible_modifier_targets(adjustment(), [("object", invisible.object_id)])


def test_layer_curves_keeps_escaped_child_pixels_outside_shape_bounds(scene):
    canvas, chapter, page = scene
    layer = chapter.add_layer(page.layer_id, "Small shape", BoundGeometry.rectangle(20, 20, 25, 25))
    layer.fill_color, layer.border_width = None, 0
    obj = raster(canvas, chapter, layer, ignore_parent_mask=True)
    before = render(canvas)
    chapter.add_modifier(adjustment(), [("layer", layer.layer_id)])
    after = render(canvas)
    assert after.pixelColor(80, 80).red() > before.pixelColor(80, 80).red()
    assert after.pixelColor(80, 80).alpha() == before.pixelColor(80, 80).alpha()
    assert chapter.objects[obj.object_id].ignore_parent_mask


def test_legacy_boundless_fill_migrates_to_supported_raster_and_renders_curves(scene):
    canvas, chapter, page = scene
    payload = chapter.to_dict()
    old = LayerNode(layer_id="legacy-fill", parent_id=page.layer_id).to_dict()
    old.update(layer_kind="fill", bound=None, shape_style={"primary_color": "#FF606060", "outline_thickness": 0})
    payload["layers"].append(old)
    payload["layers"][0]["children"].append({"kind": "layer", "id": "legacy-fill"})
    restored = ChapterDocument.from_dict(payload)
    materialize_legacy_fills(restored, canvas.tiles)
    assert isinstance(restored.objects["legacy-fill"], RasterObject)
    canvas.set_document(restored, canvas.tiles, canvas.images)
    before = render(canvas)
    restored.add_modifier(adjustment(), [("object", "legacy-fill")])
    after = render(canvas)
    assert after.pixelColor(80, 80).red() > before.pixelColor(80, 80).red()


def test_curves_has_bounded_render_culling(scene):
    canvas, chapter, page = scene
    obj = raster(canvas, chapter, page)
    chapter.add_modifier(adjustment(), [("object", obj.object_id)])
    canvas._interactive_render = True
    canvas._render_bounds.prepare()
    assert canvas._render_bounds.entity_bounds("object", obj.object_id) is not None
    assert not canvas._render_bounds.object_visible(obj, QRectF(1000, 1000, 20, 20))
