"""Thumbnail work stays bounded and cannot replace exact document pixels."""
import copy

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, CageTransformModifier, ChapterDocument, DistortModifier, HalftoneModifier,
    ImageObject, MirrorModifier, OutlineModifier, ParameterMaskBinding,
    PixelateModifier, RadialBlurModifier, RasterObject, ToneMask, WobbleModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.preview import ChapterPreview
from comic_editor.ui.thumbnail_effects import capture_scale, scaled_modifiers
from comic_editor.render.pixels import (
    FLOAT_PIXELS, LEGACY_PIXELS, current_contract, pixel_scope,
    premultiplied_pixels, working_image,
)


@pytest.fixture(params=[LEGACY_PIXELS, FLOAT_PIXELS], ids=["rgba8", "float"])
def spatial_precision(request):
    with pixel_scope(request.param):
        yield request.param


@pytest.fixture
def canvas(qapp):
    document = ChapterDocument(width=1200, height=900)
    page = document.add_page("Page", BoundGeometry.rectangle(0, 0, 1200, 900))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    canvas.set_document(document, TileStore())
    yield canvas
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def preview(canvas, navigator=True):
    canvas._interactive_render = navigator
    canvas._effect_preview_channel = "navigator" if navigator else "canvas"
    result = QImage(300, 225, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(result)
    return result


def source_image():
    result = QImage(640, 240, QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor("red"))
    painter = QPainter(result)
    painter.fillRect(320, 0, 320, 240, QColor("blue"))
    painter.end()
    return result


@pytest.mark.parametrize("kind", ["image", "raster"])
@pytest.mark.parametrize("effect", ["mirror", "pixelate", "halftone"])
def test_compact_captures_keep_placement_palette_and_exact_cache_separate(canvas, kind, effect):
    chapter = canvas.chapter
    parent = chapter.add_layer(chapter.root_page_ids[0], "Parent", BoundGeometry.rectangle(0, 0, 1200, 900))
    parent.fill_color, parent.border_width = None, 0
    parent.translate_x, parent.translate_y = 25, 40
    if kind == "image":
        obj = chapter.add_object(parent.layer_id, ImageObject(x=100, y=200, pixel_width=640, pixel_height=240))
        canvas.images.put_decoded(obj.object_id, "colors.png", b"", source_image())
    else:
        obj = chapter.add_object(parent.layer_id, RasterObject(x=100, y=200, interaction_rect=(0, 0, 640, 240)))
        image = source_image()
        canvas.tiles.replace_object_tiles(obj.object_id, {
            (x, y): image.copy(x*256, y*256, 256, 256) for x in range(3) for y in range(1)
        })
    modifier = {
        "mirror": MirrorModifier(axis_start=(900, 0), axis_end=(900, 900)),
        "pixelate": PixelateModifier(pixel_size=20),
        "halftone": HalftoneModifier(blur=0, foreground="#FF00FF00", background="#FFFFFF00",
                                     base_resolution=200, spacing=40, scale_factor=0),
    }[effect]
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    exact = preview(canvas, False)
    # Warm exact results must not hide the navigator capture path in this test.
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    result = preview(canvas)
    compact = [value for key, value in canvas._modifier_source_cache.items() if key[0] == "navigator-source"]
    assert compact and all(max(image.width(), image.height()) <= 258 for image in compact)
    assert not canvas._effect_jobs.submitted
    if effect != "halftone":
        # Interior points retain source placement through parent translation.
        assert result.pixelColor(45, 85) == QColor("red")
        assert result.pixelColor(155, 85) == QColor("blue")
    else:
        assert result.pixelColor(45, 85).green() > 200
        assert result.pixelColor(155, 85).blue() < 10
    assert preview(canvas, False) == exact


@pytest.mark.parametrize("angle", [0, 19])
def test_compact_output_opacity_mask_uses_original_world_coordinates(canvas, angle, monkeypatch):
    chapter = canvas.chapter
    parent = chapter.add_layer(chapter.root_page_ids[0], "Parent", BoundGeometry.rectangle(0, 0, 1200, 900))
    parent.fill_color, parent.border_width = None, 0
    transform = QTransform().translate(180, 20).rotate(angle)
    parent.transform_frame = (0, 0, 1200, 900)
    parent.transform_quad = [transform.map(QPointF(x, y)).toTuple()
                             for x, y in ((0, 0), (1200, 0), (1200, 900), (0, 900))]
    obj = chapter.add_object(parent.layer_id, ImageObject(x=100, y=200, pixel_width=640, pixel_height=240))
    canvas.images.put_decoded(obj.object_id, "colors.png", b"", source_image())
    chapter.add_modifier(PixelateModifier(pixel_size=20), [("object", obj.object_id)])
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    obj.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    mapping = canvas.layer_world_transform(parent.layer_id)
    red = mapping.map(QPointF(180, 340))
    blue = mapping.map(QPointF(620, 340))
    canvas.tiles.paint_dab(mask.mask_id, red, 180, QColor("white"))
    dimensions = []
    original = canvas.render_tone_mask_field
    def fields(*args, **kwargs):
        dimensions.append((args[1], args[2]))
        return original(*args, **kwargs)
    monkeypatch.setattr(canvas, "render_tone_mask_field", fields)
    actual = preview(canvas)
    assert dimensions and all(max(size) <= 258 for size in dimensions)
    assert actual.pixelColor(int(red.x()/4), int(red.y()/4)) == QColor("red")
    assert actual.pixelColor(int(blue.x()/4), int(blue.y()/4)) == actual.pixelColor(0, 0)


@pytest.mark.parametrize("channel", ["navigator", "contact"])
def test_interactive_pattern_samples_masks_at_compact_resolution_in_world_frame(canvas, monkeypatch, channel):
    canvas._interactive_render = True
    canvas._effect_preview_channel = "navigator" if channel == "navigator" else "canvas"
    canvas._stroke_projection_active = channel == "contact"
    source = source_image()
    bounds = QRectF(1000, 2000, 640, 240)
    mapping = QTransform().translate(53, 71).rotate(31)
    modifier = PixelateModifier(pixel_size=20, brightness=30)
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("mask", 0, 100)
    calls = []
    def fields(modifiers, width, height, world_to_image, visible):
        calls.append((width, height))
        assert max(width, height) <= 128
        assert width * height <= 8192
        assert world_to_image.map(mapping.map(bounds.topLeft())).x() == pytest.approx(0, abs=1e-8)
        assert world_to_image.map(mapping.map(bounds.bottomRight())).y() == pytest.approx(height)
        field = np.ones((height, width), dtype=np.float32)
        field[:, :width//2] = 0
        return {(modifier.modifier_id, "intensity"): field}
    monkeypatch.setattr(canvas, "_modifier_mask_fields", fields)
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for",
                        lambda _: pytest.fail("Interactive draft must not start full-size GPU work"))
    result, actual_bounds = render_stages(canvas, source, bounds, [modifier], mapping,
        required=QRectF(1100, 2020, 440, 180), source_key=("frame",))
    assert actual_bounds == QRectF(1100, 2020, 440, 180)
    assert result.pixelColor(10, 60) == QColor("red")
    assert result.pixelColor(400, 60).red() > 60
    assert calls == [(128, 48)]
    assert not any(key[0] == "stage" for key in canvas._modifier_render_cache)
    render_stages(canvas, source, bounds, [modifier], mapping,
        required=QRectF(1100, 2020, 440, 180), source_key=("frame",))
    assert len(calls) == 1


def test_target_layer_colors_are_captured_small_and_follow_pixel_revisions(canvas, monkeypatch):
    from comic_editor.ui import halftone_source
    parent = canvas.chapter.root_page_ids[0]
    target = canvas.chapter.add_object(parent, ImageObject(x=200, y=300, pixel_width=640, pixel_height=240))
    colors = source_image()
    colors.fill(QColor("lime"))
    canvas.images.put_decoded(target.object_id, "source.png", b"", colors)
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=target.object_id,
        blur=0, base_resolution=200, spacing=40, scale_factor=0, size=2)
    incoming = source_image()
    incoming.fill(QColor("gray"))
    canvas._interactive_render = True
    canvas._effect_preview_channel = "navigator"
    captures = []
    original = halftone_source.render_color_source
    def capture(*args):
        captures.append(args[2].size())
        assert max(args[2].width(), args[2].height()) <= 96
        return original(*args)
    monkeypatch.setattr(halftone_source, "render_color_source", capture)
    def render():
        return render_stages(canvas, incoming, QRectF(100, 200, 640, 240), [modifier],
            QTransform.fromTranslate(100, 100), source_key=("same incoming pixels",))[0]
    green = render()
    assert green.pixelColor(200, 100).green() > 200
    colors.fill(QColor("blue"))
    canvas.images.put_decoded(target.object_id, "source.png", b"", colors)
    blue = render()
    assert blue != green and blue.pixelColor(200, 100).blue() > 200
    assert len(captures) == 2


def test_thumbnail_scaling_copies_bindings_and_preserves_world_axes(canvas):
    canvas._interactive_render = True
    canvas._effect_preview_channel = "navigator"
    outline = OutlineModifier(thickness=8)
    outline.parameter_masks["thickness"] = ParameterMaskBinding("mask", 2, 12)
    mirror = MirrorModifier(axis_start=(2000, 3000), axis_end=(2000, 5000))
    original = copy.deepcopy(outline.to_dict())
    result = scaled_modifiers([outline, mirror], .25)
    assert result[0].thickness == 2
    assert result[0].parameter_masks["thickness"].white_value == 3
    assert result[1].axis_start == mirror.axis_start
    assert outline.to_dict() == original
    bounds = QRectF(1000, 2000, 3000, 4000)
    assert capture_scale(canvas, bounds, [outline, mirror]) < 1
    assert capture_scale(canvas, bounds, [DistortModifier(modifier_type="distort_twirl")]) < 1
    for unsupported in (WobbleModifier(), BlurModifier(mode="focal")):
        assert capture_scale(canvas, bounds, [unsupported]) == 1
    canvas._effect_preview_channel = "canvas"
    assert capture_scale(canvas, bounds, [outline, mirror]) == 1


@pytest.mark.parametrize("kind,effect", [
    (kind, effect) for kind in ("image", "raster")
    for effect in ("twirl", "mesh", "smudge", "radial", "generic")
] + [("image", "cage")])
def test_spatial_thumbnail_bounds_mask_work_and_preserves_native_output(canvas, monkeypatch, kind, effect, spatial_precision):
    chapter = canvas.chapter
    parent = chapter.add_layer(chapter.root_page_ids[0], "Parent", BoundGeometry.rectangle(0, 0, 1200, 900))
    parent.fill_color, parent.border_width = None, 0
    parent.translate_x, parent.translate_y = 25, 40
    image = source_image()
    if spatial_precision.floating:
        pixels = premultiplied_pixels(image)
        pixels[..., :3] *= 1.7  # Native HDR values cannot pass through RGBA8.
        image = working_image(pixels)
    if kind == "image":
        obj = chapter.add_object(parent.layer_id, ImageObject(x=100, y=200, pixel_width=640, pixel_height=240))
        canvas.images.put_decoded(obj.object_id, "colors.png", b"", image)
    else:
        obj = chapter.add_object(parent.layer_id, RasterObject(x=100, y=200, interaction_rect=(0, 0, 640, 240)))
        canvas.tiles.replace_object_tiles(obj.object_id, {
            (x, 0): image.copy(x*256, 0, 256, 256) for x in range(3)
        })
    world_frame = (125, 240, 640, 240)
    if effect == "cage":
        modifier = CageTransformModifier(frame=world_frame, columns=2, rows=2,
            points=[(125, 240), (765, 255), (125, 480), (765, 465)], smoothness=0)
    elif effect == "radial":
        modifier = RadialBlurModifier(center=(445, 360), angle=15)
    elif effect == "generic":
        modifier = OutlineModifier(thickness=8)
    else:
        parameters = {"interpolation": "bilinear", "edges": "transparent"}
        if effect == "twirl":
            parameters["angle"] = 30
        elif effect == "mesh":
            parameters.update(rows=2, columns=2, smoothness=0)
        if effect == "smudge":
            from comic_editor.core.smudge import default_tool_settings, validate_strokes
            parameters["strokes"] = validate_strokes([{"id": "stroke", "points": [
                {"position": point, "handle": point, "point_type": "vector",
                 "radius": 25, "flow": 100, "strength": 100}
                for point in [(250, 350), (650, 350)]
            ], "pressure_settings": default_tool_settings()}])
        modifier = DistortModifier(modifier_type="distort_" + ("mesh_warp" if effect == "mesh" else effect),
            frame=world_frame, center=(445, 360), radius=200, parameters=parameters)
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    for x in range(4):
        for y in range(3):
            tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
            tile.fill(QColor("white"))
            canvas.tiles.set_tile(mask.mask_id, (x, y), tile)
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    obj.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    def exact():
        canvas._interactive_render = False
        canvas._effect_preview_channel = "canvas"
        image = QImage(chapter.width, chapter.height, current_contract().image_format)
        canvas.render_preview(image)
        return image
    expected = exact()
    model = chapter.to_dict()
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    fields = []
    original = canvas.render_tone_mask_field
    def field(mask_id, width, height, *args, **kwargs):
        fields.append(width*height)
        return original(mask_id, width, height, *args, **kwargs)
    monkeypatch.setattr(canvas, "render_tone_mask_field", field)
    result = preview(canvas)
    assert not result.isNull()
    assert fields and max(fields) < 100_000
    compact = [image for key, image in canvas._modifier_source_cache.items() if key[0] == "navigator-source"]
    assert compact and all(max(image.width(), image.height()) <= 258 for image in compact)
    assert chapter.to_dict() == model, "Thumbnail scaling changed source rigs or mask bindings"
    assert canvas._effect_jobs.submitted == 0
    assert exact() == expected, "Navigator drafts contaminated native output"


@pytest.mark.parametrize("kind", ["image", "raster"])
@pytest.mark.parametrize("effect", ["none", "outline", "mirror", "halftone"])
def test_chunked_full_and_dirty_thumbnail_match_direct_render(canvas, kind, effect):
    from PySide6.QtTest import QTest
    canvas.chapter.height = 9000
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    page.bound = BoundGeometry.rectangle(0, 0, 1200, 9000)
    if kind == "image":
        obj = canvas.chapter.add_object(page.layer_id, ImageObject(x=100, y=3000, pixel_width=640, pixel_height=240))
        canvas.images.put_decoded(obj.object_id, "colors.png", b"", source_image())
    else:
        obj = canvas.chapter.add_object(page.layer_id, RasterObject(x=100, y=3000, interaction_rect=(0, 0, 640, 240)))
        image = source_image()
        canvas.tiles.replace_object_tiles(obj.object_id, {(x, 0): image.copy(x*256, 0, 256, 256) for x in range(3)})
    if effect != "none":
        modifier = {"outline": OutlineModifier(thickness=8),
                    "mirror": MirrorModifier(axis_start=(800, 0), axis_end=(800, 9000)),
                    "halftone": HalftoneModifier(blur=0, base_resolution=200, spacing=40)}[effect]
        canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    navigator = ChapterPreview(canvas)
    navigator.resize(92, 700)
    navigator.show()
    try:
        reference = QImage(navigator.content_rect().size(), QImage.Format_ARGB32_Premultiplied)
        navigator._render_live_preview(reference)
        # Rebuild cold sources and pixels, not just the final widget image.
        canvas._modifier_source_cache.clear()
        canvas._modifier_source_cache_bytes = 0
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        for _ in range(1000):
            navigator._refresh_cache()
            QTest.qWait(10)
            if not navigator._cache.isNull():
                break
        assert navigator._cache == reference, (navigator._navigator_jobs.error,
            navigator._navigator_jobs.document, navigator._navigator_jobs.capture,
            navigator._navigator_jobs.sent, navigator._navigator_jobs.scheduler.busy)
        obj.x += 41
        canvas.documentChanged.emit(QRectF(50, 2900, 1150, 500))
        for _ in range(1000):
            navigator._refresh_cache()
            QTest.qWait(10)
            if not navigator._dirty_full and not navigator._dirty_bands:
                break
        navigator._render_live_preview(reference)
        assert navigator._cache == reference
    finally:
        navigator.close()
        navigator.deleteLater()


@pytest.mark.parametrize("effect", ["mirror", "halftone", "parent_outline"])
def test_small_rotated_raster_thumbnail_does_not_poison_exact_sources(canvas, effect):
    chapter = canvas.chapter
    parent = chapter.add_layer(chapter.root_page_ids[0], "Parent", BoundGeometry.rectangle(0, 0, 1200, 900))
    parent.fill_color, parent.border_width = None, 0
    obj = chapter.add_object(parent.layer_id, RasterObject(interaction_rect=(0, 0, 32, 32)))
    transform = QTransform().translate(315.25, 241.75).rotate(31)
    obj.transform_frame = (0, 0, 32, 32)
    obj.transform_quad = [transform.map(QPointF(x, y)).toTuple()
                          for x, y in ((0, 0), (32, 0), (32, 32), (0, 32))]
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(Qt.transparent)
    painter = QPainter(tile)
    for y in range(32):
        for x in range(32):
            painter.fillRect(x, y, 1, 1, QColor("red" if (x+y)%2 else "blue"))
    painter.end()
    canvas.tiles.replace_object_tiles(obj.object_id, {(0, 0): tile})
    if effect == "parent_outline":
        chapter.add_modifier(OutlineModifier(thickness=3), [("layer", parent.layer_id)])
    else:
        modifier = (MirrorModifier(axis_start=(370, 0), axis_end=(370, 900)) if effect == "mirror"
                    else HalftoneModifier(blur=0, spacing=15))
        chapter.add_modifier(modifier, [("object", obj.object_id)])
    exact = preview(canvas, False)
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    preview(canvas)
    if effect != "parent_outline":
        keys = [key for key in canvas._modifier_source_cache if key[0] == "navigator-source"]
        assert keys and any(key[1] == 1. for key in keys)
    assert preview(canvas, False) == exact
