"""Curves pixels survive destructive baking, export previews, and portable copies."""
import json

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.assets import AssetManifest, entity_visual_bounds, extract_asset, instantiate_asset
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (BoundGeometry, BrightnessContrastModifier,
    ChapterDocument, CurvesModifier, ImageObject, ModifierPreset, RasterObject, TextObject)
from comic_editor.core.modifier_presets import apply_modifier_preset, preset_from_modifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.baking import apply_raster_modifiers, rasterize
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _qimage_premultiplied


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=256, height=256, document_kind="asset", background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 256, 256))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    yield canvas, chapter, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def adjustment():
    return CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .78), (1, 1)],
                                 "rgb:blue": [(0, 0), (.45, .3), (1, 1)]})


def render(canvas):
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def pixels(image):
    return (_qimage_premultiplied(image) * 255).round().astype(np.int16)


def artwork(values):
    y, x = np.nonzero(values[..., 3])
    return values[y.min():y.max()+1, x.min():x.max()+1]


def add_raster(canvas, chapter, parent):
    obj = chapter.add_object(parent.layer_id, RasterObject())
    canvas.tiles.paint_dab(obj.object_id, QPointF(100, 105), 80,
                           QColor(81, 115, 143), opacity=.7)
    canvas.set_selection("object", obj.object_id)
    return obj


def image_pattern():
    y, x = np.indices((256, 256))
    rgba = np.empty((256, 256, 4), np.uint8)
    rgba[..., 0] = np.where((x+y) % 2, 55, 190)
    rgba[..., 1] = (x // 2 + 40).astype(np.uint8)
    rgba[..., 2] = (y // 2 + 50).astype(np.uint8)
    rgba[..., 3] = 180
    return QImage(rgba.data, 256, 256, 256*4, QImage.Format_RGBA8888).copy().convertToFormat(QImage.Format_ARGB32_Premultiplied)


def put_image(canvas, identifier, image):
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    canvas.images.put_decoded(identifier, "source.png", bytes(payload), image)


def test_apply_raster_curves_prefix_preserves_later_effect_and_one_undo(scene):
    canvas, chapter, page = scene
    obj = add_raster(canvas, chapter, page)
    first, later = adjustment(), BrightnessContrastModifier(brightness=-6, contrast=23)
    for modifier in (first, later):
        chapter.add_modifier(modifier, [("object", obj.object_id)])
    before = pixels(render(canvas))
    # Raster interaction frames are non-clipping. Curves must capture these
    # painted pixels beyond the default frame's right edge at x=120.
    assert before[105, 135, 3] > 0
    revision = canvas.command_stack.revision
    apply_raster_modifiers(canvas, first.modifier_id)
    assert chapter.objects[obj.object_id].modifier_ids == [later.modifier_id]
    assert canvas.command_stack.revision == revision + 1
    baked = pixels(render(canvas))
    # The stored prefix adds one 8-bit rounding before the remaining contrast.
    np.testing.assert_allclose(baked, before, atol=2)
    np.testing.assert_array_equal(baked[..., 3], before[..., 3])
    canvas.command_stack.undo()
    assert canvas.chapter.objects[obj.object_id].modifier_ids == [first.modifier_id, later.modifier_id]
    np.testing.assert_array_equal(pixels(render(canvas)), before)
    canvas.command_stack.redo()
    assert canvas.chapter.objects[obj.object_id].modifier_ids == [later.modifier_id]
    np.testing.assert_array_equal(pixels(render(canvas)), baked)


@pytest.mark.parametrize("kind", ["image", "text", "compound"])
def test_rasterize_curves_preserves_export_and_restores_editable_source(scene, text_outline_font_family, kind):
    canvas, chapter, page = scene
    if kind == "image":
        obj = chapter.add_object(page.layer_id, ImageObject(pixel_width=256, pixel_height=256))
        put_image(canvas, obj.object_id, image_pattern())
        ref = ("object", obj.object_id)
    elif kind == "text":
        obj = chapter.add_object(page.layer_id, TextObject(x=30, y=40, width=200, height=100,
            text="Curves", font_family=text_outline_font_family, font_size=40,
            margin=0, layout_mode="free", text_color="#B4606070"))
        ref = ("object", obj.object_id)
    else:
        layer = chapter.add_layer(page.layer_id, "Compound", BoundGeometry.rectangle(30, 30, 150, 135))
        layer.compound_enabled = True
        layer.fill_color, layer.border_width = "#B4606070", 0
        operand = chapter.add_layer(layer.layer_id, "Operand", BoundGeometry.rectangle(90, 90, 130, 130))
        operand.border_width = 0
        add_raster(canvas, chapter, operand)
        ref = ("layer", layer.layer_id)
    chapter.add_modifier(adjustment(), [ref])
    canvas.set_selection(*ref, activate_default_tool=False)
    before = pixels(render(canvas))
    original_model = chapter.to_dict()
    rasterize(canvas, *ref)
    baked = pixels(render(canvas))
    assert isinstance(canvas.chapter.objects[ref[1]], ImageObject)
    assert not canvas.chapter.objects[ref[1]].modifier_ids
    np.testing.assert_allclose(baked, before, atol=1)
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == original_model
    np.testing.assert_array_equal(pixels(render(canvas)), before)
    canvas.command_stack.redo()
    np.testing.assert_array_equal(pixels(render(canvas)), baked)


@pytest.mark.parametrize("channel", ["canvas", "navigator"])
def test_preview_draft_does_not_poison_full_quality_png_export(scene, monkeypatch, tmp_path, channel):
    canvas, chapter, page = scene
    obj = chapter.add_object(page.layer_id, ImageObject(pixel_width=256, pixel_height=256))
    source = image_pattern()
    put_image(canvas, obj.object_id, source)
    modifier = adjustment()
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    expected = pixels(apply_modifier_stack(source, [modifier], (0, 0)))
    requests = []
    monkeypatch.setattr(canvas._effect_jobs, "request", lambda *args, **kwargs: (requests.append(args), True)[1])
    canvas._interactive_render = True
    canvas._effect_preview_channel = channel
    revision = getattr(canvas, "_effect_provisional_revision", 0)
    draft = pixels(render(canvas))
    assert getattr(canvas, "_effect_provisional_revision", 0) > revision
    assert not np.array_equal(draft, expected)
    assert bool(requests) is (channel == "canvas")
    canvas._interactive_render = False
    exact = render(canvas)
    np.testing.assert_array_equal(pixels(exact), expected)
    destination = tmp_path / f"curves-{channel}.png"
    assert exact.save(str(destination))
    np.testing.assert_array_equal(pixels(QImage(str(destination))), expected)
    np.testing.assert_array_equal(pixels(render(canvas)), expected)


def test_asset_copy_and_preset_round_trip_preserve_curves_pixels(scene):
    canvas, chapter, page = scene
    obj = add_raster(canvas, chapter, page)
    modifier = adjustment()
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    expected = pixels(render(canvas))
    original_bounds = entity_visual_bounds(chapter, canvas.tiles, "object", obj.object_id)
    manifest, tiles = extract_asset(chapter, canvas.tiles, "object", obj.object_id, "Curve artwork")
    manifest = AssetManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    preset = ModifierPreset.from_dict(json.loads(json.dumps(preset_from_modifier("Lift", modifier).to_dict())))
    destination = ChapterDocument(width=256, height=256, document_kind="asset", background="#00000000")
    target_page = destination.add_page("Page", BoundGeometry.rectangle(0, 0, 256, 256))
    target_page.fill_color, target_page.border_width = None, 0
    target_tiles = TileStore()
    kind, identifier, _ = instantiate_asset(manifest, tiles, destination, target_tiles,
        target_page.layer_id, original_bounds.center().x(), original_bounds.center().y())
    clone = destination.objects[identifier]
    cloned_modifier = destination.modifiers[clone.modifier_ids[0]]
    assert cloned_modifier.modifier_id != modifier.modifier_id
    assert cloned_modifier.curves == modifier.curves
    canvas.set_document(destination, target_tiles, ImageStore())
    copied = pixels(render(canvas))
    # Asset placement centers its fitted frame on the destination point.
    np.testing.assert_array_equal(artwork(copied), artwork(expected))
    cloned_modifier.curves = {}
    assert not np.array_equal(pixels(render(canvas)), copied)
    destination.modifiers[cloned_modifier.modifier_id] = apply_modifier_preset(cloned_modifier, preset)
    assert kind == "object"
    np.testing.assert_array_equal(pixels(render(canvas)), copied)
