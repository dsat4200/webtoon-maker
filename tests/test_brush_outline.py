"""Material brush outlines retain contours, masks and portable brush artwork."""
import base64
from dataclasses import replace
import io
import json
from types import SimpleNamespace

import numpy as np
from PIL import Image
from PySide6.QtWidgets import QComboBox, QPushButton

from comic_editor.core.brushes import BrushDefinition, BrushTip
from comic_editor.core.brush_outline import alpha_contours
from comic_editor.core.models import OutlineModifier, ParameterMaskBinding, modifier_from_dict
from comic_editor.core.modifier_presets import apply_modifier_preset, preset_from_modifier
from comic_editor.core.settings import EditorSettings
from comic_editor.ui.modifier_rendering import (
    _premultiplied_qimage, _qimage_premultiplied, apply_modifier_stack, modifier_render_settings,
)
from comic_editor.ui.outline_brush_controls import OutlineBrushControls


def source():
    pixels = np.zeros((96, 128, 4), np.float32)
    pixels[30:66, 32:96] = (.1, .6, .2, 1)
    return _premultiplied_qimage(pixels)


def stamp_brush():
    pixels = np.zeros((12, 12, 4), np.uint8)
    pixels[2:10, 4:8] = (255, 0, 80, 255)
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, "PNG")
    tip = BrushTip(name="Pink dash", width=12, height=12,
                   png=base64.b64encode(stream.getvalue()).decode(), shape="image", mode="color")
    return BrushDefinition(name="Pink material", size=20, spacing=.65, tips=(tip,))


def render(modifier, fields=None):
    return _qimage_premultiplied(apply_modifier_stack(source(), [modifier], (0, 0), fields))


def test_alpha_contours_preserve_holes_and_diagonal_components():
    alpha = np.zeros((18, 20), np.float32)
    alpha[1:8, 1:8] = 1
    alpha[3:6, 3:6] = 0
    alpha[8:12, 8:12] = 1
    contours = alpha_contours(alpha)
    assert len(contours) == 3
    assert all(np.array_equal(path[0], path[-1]) for path in contours)
    assert all(len(path) <= 6 for path in contours)
    assert alpha_contours(alpha.copy()) is contours


def test_material_tip_colors_and_gaps_are_used_and_source_is_preserved():
    modifier = OutlineModifier(style="brush", thickness=10, brush=stamp_brush().to_dict())
    pixels = render(modifier)
    original = _qimage_premultiplied(source())
    np.testing.assert_array_equal(pixels[original[..., 3] == 1], original[original[..., 3] == 1])
    outside = (original[..., 3] == 0) & (pixels[..., 3] > .01)
    assert outside.sum() > 30
    assert np.all(pixels[outside, 0] > pixels[outside, 1])
    solid = render(OutlineModifier(thickness=10))
    assert np.count_nonzero(pixels[..., 3]) < np.count_nonzero(solid[..., 3])
    np.testing.assert_array_equal(pixels, render(modifier))
    modifier.brush_spacing = 200
    assert not np.array_equal(pixels, render(modifier))


def test_thickness_mask_controls_stroke_size_and_zero_region():
    modifier = OutlineModifier(style="brush", thickness=9)
    modifier.parameter_masks["thickness"] = ParameterMaskBinding("mask", 0, 12)
    field = np.zeros((96, 128), np.float32)
    field[:, 64:] = 1
    pixels = render(modifier, {(modifier.modifier_id, "thickness"): field})
    original = _qimage_premultiplied(source())
    np.testing.assert_array_equal(pixels[:, :63], original[:, :63])
    assert pixels[25, 82, 3] > .9
    assert pixels[30, 103, 3] > .1
    assert np.all(pixels[..., :3] <= pixels[..., 3:4]+1e-6)


def test_material_brush_roundtrips_presets_and_cache_dependencies():
    modifier = OutlineModifier(style="brush", thickness=7, brush=stamp_brush().to_dict(),
                               brush_spacing=140, brush_angle=32, brush_seed=97)
    modifier.parameter_masks["thickness"] = ParameterMaskBinding("mask", 1, 10)
    restored = modifier_from_dict(json.loads(json.dumps(modifier.to_dict())))
    assert restored.to_dict() == modifier.to_dict()
    np.testing.assert_array_equal(render(restored), render(modifier))
    preset = preset_from_modifier("Pattern outline", modifier)
    target = OutlineModifier()
    applied = apply_modifier_preset(target, preset)
    assert applied.modifier_id == target.modifier_id
    assert applied.brush == modifier.brush
    assert applied.brush is not modifier.brush
    np.testing.assert_array_equal(render(applied), render(modifier))
    before = modifier_render_settings(modifier)
    modifier.brush = replace(stamp_brush(), spacing=.2).to_dict()
    assert modifier_render_settings(modifier) != before


def test_brush_outline_blur_and_intensity_keep_source_sharp():
    base = OutlineModifier(style="brush", thickness=7, brush=stamp_brush().to_dict())
    original = _qimage_premultiplied(source())
    full = render(base)
    half = render(replace(base, intensity=50))
    np.testing.assert_allclose(half, original+(full-original)*.5, atol=1/255)
    blurred = render(replace(base, blur_radius=4, blur_strength=100))
    np.testing.assert_array_equal(blurred[original[..., 3] == 1], original[original[..., 3] == 1])
    assert np.any(blurred[..., 3] > full[..., 3]+.01)


def test_outline_brush_controls_snapshot_library_without_mutating_it(qapp):
    settings = EditorSettings()
    calls = []
    modifier = OutlineModifier()
    def set_parameter(_identifier, name, value, _commit):
        calls.append(name)
        setattr(modifier, name, value)
        modifier.validate()
    owner = SimpleNamespace(canvas=SimpleNamespace(settings=settings), set_parameter=set_parameter)
    controls = OutlineBrushControls(modifier, owner)
    controls.findChild(QComboBox, "outlineStyle").setCurrentIndex(1)
    controls.findChild(QComboBox, "outlineBrush").setCurrentIndex(1)
    assert modifier.style == "brush"
    assert modifier.brush == settings.brush_presets[0]
    assert modifier.brush is not settings.brush_presets[0]
    controls.findChild(QPushButton, "outlineUseCurrentBrush").click()
    assert modifier.brush == settings.active_paint_brush().to_dict()
    assert calls == ["style", "brush", "brush"]
    controls.deleteLater()


def test_limited_gradient_drives_outline_thickness_and_bakes(qapp):
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QColor, QImage, QTransform
    from comic_editor.core.models import (
        BoundGeometry, ChapterDocument, ColorFillGradientObject, ColorGradientRamp,
        ColorGradientStop, LimitedMaskGradient, LineGradientField, PathNode,
        RasterObject, ToneMask,
    )
    from comic_editor.core.tiles import TileStore
    from comic_editor.ui.baking import apply_raster_modifiers
    from comic_editor.ui.canvas import CanvasWidget
    chapter = ChapterDocument(width=128, height=128, document_kind="image")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 128, 128))
    page.fill_color, page.border_width = None, 0
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    tiles = TileStore()
    tiles.paint_dab(obj.object_id, QPointF(64, 64), 48, QColor("red"), square=True, antialias=False)
    gradient = ColorFillGradientObject(
        line_field=LineGradientField(BoundGeometry.path([PathNode(x=32, y=64), PathNode(x=96, y=64)], False)),
        ramp=ColorGradientRamp(stops=[ColorGradientStop(position=0, color="#00FFFFFF"),
                                      ColorGradientStop(position=1, color="#FFFFFFFF")]))
    mask = ToneMask(limited_gradients=[LimitedMaskGradient(gradient=gradient, half_width=50)])
    chapter.masks[mask.mask_id] = mask
    modifier = OutlineModifier(style="brush", thickness=10, brush=stamp_brush().to_dict())
    modifier.parameter_masks["thickness"] = ParameterMaskBinding(mask.mask_id, 0, 12)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    canvas.set_selection("object", obj.object_id)
    field = canvas.render_tone_mask_field(mask.mask_id, 128, 128, QTransform(), QRectF(0, 0, 128, 128))
    assert field[64, 48] < field[64, 80]
    assert field[64, 10] == 0
    def preview():
        image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(image)
        return _qimage_premultiplied(image)
    before = preview()
    assert before[35:40, 64:86, 3].sum() > before[35:40, 42:64, 3].sum()
    apply_raster_modifiers(canvas, modifier.modifier_id)
    np.testing.assert_allclose(preview(), before, atol=2/255)
    canvas.command_stack.undo()
    np.testing.assert_array_equal(preview(), before)
    canvas.deleteLater()
