import base64
import time
from io import BytesIO

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QImage, QTransform, QPainter
from PySide6.QtNetwork import QNetworkAccessManager
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ShapeStyle, RasterObject, ImageObject,
    BlenderComicViewSourceDescriptor, TextureModifier, SolidColorOverlayModifier,
    HueSaturationLightnessModifier, MirrorModifier, VectorDrawingObject,
    TextObject, TilingModifier, ParameterMaskBinding, PathNode, WobbleModifier, DotDashModifier, modifier_from_dict,
    OVERLAY_BLEND_MODES,
)
from comic_editor.core.modifier_presets import preset_from_modifier, apply_modifier_preset
from comic_editor.core.settings import EditorSettings, load_settings, save_settings
from comic_editor.core.texture_library import DEFAULT_TEXTURE_DIRECTORY, import_texture, texture_categories
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_rendering import apply_modifier_stack, modifier_render_settings
from comic_editor.ui.overlay_controls import OverlayControls
from comic_editor.ui.settings_dialog import SettingsDialog
from comic_editor.ui.texture_picker import TextureCombo


def encoded(color=(220, 80, 40, 255), size=(8, 8)):
    stream = BytesIO()
    Image.new("RGBA", size, color).save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode("ascii")


def pixels(image):
    converted = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(converted.constBits(), np.uint8).reshape(converted.height(), converted.width(), 4).copy()


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda parent: QNetworkAccessManager(parent))
    chapter = ChapterDocument(height=200, background="#FFFFFFFF")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 200))
    page.fill_color, page.border_width = None, 0
    style = ShapeStyle(primary_color="#FFB0B0B0", outline_color="#FF112233", outline_thickness=8)
    shape = chapter.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(30, 30, 160, 120), style=style)
    raster = chapter.add_object(shape.layer_id, RasterObject(interaction_rect=(0, 0, 240, 200)))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(640, 480)
    canvas.set_document(chapter, TileStore())
    canvas.tiles.paint_dab(raster.object_id, QPointF(100, 90), 20, QColor("blue"), square=True, antialias=False)
    yield canvas, chapter, shape, raster
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return pixels(image)


@pytest.mark.parametrize("factory", [TextureModifier, SolidColorOverlayModifier])
def test_overlay_roundtrip_presets_and_eligibility(scene, factory):
    _, chapter, shape, raster = scene
    modifier = factory(intensity=47, blend_mode="replace", apply_to_outline=True)
    if isinstance(modifier, TextureModifier):
        modifier.texture_data, modifier.texture_name, modifier.texture_category = encoded(), "sample.png", "Paper"
        modifier.hue, modifier.saturation, modifier.lightness = 73, -28, 19
    image = chapter.add_object(shape.layer_id, ImageObject())
    blender = chapter.add_object(shape.layer_id, ImageObject(source=BlenderComicViewSourceDescriptor(
        project_uuid="a" * 32, view_uuid="b" * 32)))
    refs = [("layer", shape.layer_id), ("object", raster.object_id), ("object", image.object_id), ("object", blender.object_id)]
    assert not chapter.incompatible_modifier_targets(modifier, refs)
    chapter.add_modifier(modifier, refs)
    restored = ChapterDocument.from_dict(chapter.to_dict()).modifiers[modifier.modifier_id]
    assert restored.to_dict() == modifier.to_dict()
    preset = preset_from_modifier("Overlay", modifier)
    fresh = factory()
    applied = apply_modifier_preset(fresh, preset)
    assert applied.modifier_id == fresh.modifier_id
    assert applied.intensity == 47 and applied.apply_to_outline
    if isinstance(applied, TextureModifier):
        assert (applied.hue, applied.saturation, applied.lightness) == (73, -28, 19)
    for obj in (TextObject(), VectorDrawingObject()):
        chapter.add_object(shape.layer_id, obj)
        assert chapter.incompatible_modifier_targets(modifier, [("object", obj.object_id)])
    assert chapter.incompatible_modifier_targets(modifier, [("layer", shape.parent_id)])


@pytest.mark.parametrize("mode", [mode for mode, _ in OVERLAY_BLEND_MODES])
def test_solid_overlay_blending_preserves_coverage(mode):
    from comic_editor.core.blend_modes import blend_rgb
    image = QImage(2, 1, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    image.setPixelColor(0, 0, QColor(80, 140, 200, 128))
    overlay = SolidColorOverlayModifier(color="#FFFF8040", intensity=50, blend_mode=mode)
    result = pixels(apply_modifier_stack(image, [overlay], (0, 0)))
    back = pixels(image)[0, 0, :3] / 255.
    front = np.array([1., 128 / 255., 64 / 255.])
    blended = front if mode == "replace" else blend_rgb(back, front, mode)
    assert np.max(np.abs(result[0, 0, :3] - (back + blended) * .5 * 255)) <= 3
    assert result[0, 0, 3] == 128
    assert np.array_equal(result[0, 1], [0, 0, 0, 0])


def test_texture_replace_raw_pixels_alpha_and_mask():
    image = QImage(2, 1, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("blue"))
    modifier = TextureModifier(texture_data=encoded((255, 0, 0, 128), (2, 1)), blend_mode="replace")
    full = pixels(apply_modifier_stack(image, [modifier], (0, 0)))
    assert np.array_equal(full[0, 0], [255, 0, 0, 128])
    modifier.intensity = 0
    assert np.array_equal(pixels(apply_modifier_stack(image, [modifier], (0, 0))), pixels(image))
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask_id="mask", black_value=0, white_value=100)
    masked = pixels(apply_modifier_stack(image, [modifier], (0, 0),
                    {(modifier.modifier_id, "intensity"): np.array([[0., 1.]])}))
    assert np.array_equal(masked[0, 0], [0, 0, 255, 255])
    assert np.array_equal(masked[0, 1], [255, 0, 0, 128])


@pytest.mark.parametrize("quad", [False, True])
@pytest.mark.parametrize("mode", ["normal", "replace"])
@pytest.mark.parametrize("adjustments, expected", [
    ({"hue": 120}, [0, 127, 127]),
    ({"saturation": -100}, [63, 63, 191]),
    ({"lightness": 50}, [127, 63, 191]),
])
def test_texture_hsl_adjusts_overlay_before_blending(quad, mode, adjustments, expected):
    image = QImage(4, 2, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("blue"))
    modifier = TextureModifier(texture_data=encoded((255, 0, 0, 255)),
        blend_mode=mode, intensity=50, **adjustments)
    if quad:
        modifier.texture_quad = [(1, 0), (3, 0), (3, 2), (1, 2)]
    result = pixels(apply_modifier_stack(image, [modifier], (0, 0)))
    np.testing.assert_allclose(result[:, 1:3, :3], np.broadcast_to(expected, (2, 2, 3)), atol=1)
    assert np.all(result[..., 3] == 255)
    if quad:
        np.testing.assert_array_equal(result[:, [0, 3]], pixels(image)[:, [0, 3]])


def test_texture_hsl_preserves_alpha_and_supports_parameter_masks():
    image = QImage(2, 1, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("blue"))
    modifier = TextureModifier(texture_data=encoded((255, 0, 0, 128), (2, 1)),
        blend_mode="replace", hue=120, saturation=-100, lightness=50)
    result = pixels(apply_modifier_stack(image, [modifier], (0, 0)))
    np.testing.assert_allclose(result[0, 0], [191, 191, 191, 128], atol=2)
    modifier.saturation = modifier.lightness = 0
    modifier.parameter_masks["hue"] = ParameterMaskBinding("hue-mask", 0, 120)
    modifier.validate()
    masked = pixels(apply_modifier_stack(image, [modifier], (0, 0),
        {(modifier.modifier_id, "hue"): np.array([[0., 1.]], np.float32)}))
    np.testing.assert_array_equal(masked[0, 0], [255, 0, 0, 128])
    np.testing.assert_array_equal(masked[0, 1], [0, 255, 0, 128])
    restored = modifier_from_dict(modifier.to_dict())
    assert restored.parameter_masks["hue"].white_value == 120


def test_texture_hsl_legacy_defaults_and_validation():
    legacy = modifier_from_dict({"type": "texture", "texture_data": encoded()})
    assert (legacy.hue, legacy.saturation, legacy.lightness) == (0, 0, 0)
    modifier = TextureModifier(hue=400, saturation=-200, lightness=200)
    modifier.validate()
    assert (modifier.hue, modifier.saturation, modifier.lightness) == (180, -100, 100)
    for attribute in ("hue", "saturation", "lightness"):
        invalid = TextureModifier(**{attribute: float("nan")})
        with pytest.raises(ValueError, match="finite"):
            invalid.validate()


def test_texture_hsl_updates_cached_shape_preview_without_recoloring_outline(scene):
    canvas, chapter, shape, _ = scene
    baseline = render(canvas)
    modifier = TextureModifier(texture_data=encoded((255, 0, 0, 255)), blend_mode="replace")
    chapter.add_modifier(modifier, [("layer", shape.layer_id)])
    assert np.array_equal(render(canvas)[60, 90], [255, 0, 0, 255])
    modifier.hue = 120
    changed = render(canvas)
    assert np.array_equal(changed[60, 90], [0, 255, 0, 255])
    assert np.array_equal(changed[32, 90], baseline[32, 90])
    modifier.apply_to_outline = True
    assert np.array_equal(render(canvas)[32, 90], [0, 255, 0, 255])


@pytest.mark.parametrize("texture", [False, True])
@pytest.mark.parametrize("compound", [False, True])
def test_shape_overlay_preserves_outline_by_default_and_toggle(scene, texture, compound):
    canvas, chapter, shape, _ = scene
    shape.compound_enabled = compound
    baseline = render(canvas)
    modifier = (TextureModifier(texture_data=encoded((255, 0, 0, 255)), blend_mode="replace")
                if texture else SolidColorOverlayModifier(color="#FFFF0000"))
    chapter.add_modifier(modifier, [("layer", shape.layer_id)])
    result = render(canvas)
    assert np.array_equal(result[32, 90], baseline[32, 90])
    assert np.array_equal(result[90, 100], [255, 0, 0, 255])
    assert np.array_equal(result[60, 90], [255, 0, 0, 255])
    modifier.apply_to_outline = True
    assert np.array_equal(render(canvas)[32, 90], [255, 0, 0, 255])
    modifier.muted = True
    assert np.array_equal(render(canvas), baseline)


def test_ordered_overlays_preserve_preceding_outline_effects(scene):
    canvas, chapter, shape, _ = scene
    hsl = HueSaturationLightnessModifier(lightness=30)
    chapter.add_modifier(hsl, [("layer", shape.layer_id)])
    baseline = render(canvas)
    overlay = SolidColorOverlayModifier(color="#FFFF0000")
    chapter.add_modifier(overlay, [("layer", shape.layer_id)])
    assert np.array_equal(render(canvas)[32, 90], baseline[32, 90])
    assert np.array_equal(render(canvas)[60, 90], [255, 0, 0, 255])
    chapter.add_modifier(SolidColorOverlayModifier(color="#FF00FF00", intensity=50), [("layer", shape.layer_id)])
    assert np.max(np.abs(render(canvas)[60, 90, :3] - [127, 127, 0])) <= 1


@pytest.mark.parametrize("shape_type", ["open", "tiled", "wobble", "dots"])
def test_overlay_preserves_shape_outline_through_other_shape_stages(scene, shape_type):
    canvas, chapter, shape, _ = scene
    if shape_type == "open":
        shape.bound = BoundGeometry.path([PathNode(x=30, y=90), PathNode(x=190, y=90)])
        shape.layer_kind = "open_shape"
        shape.shape_style.base_thickness = 45
    elif shape_type == "tiled":
        chapter.add_modifier(TilingModifier(center=(110, 90), side=60), [("layer", shape.layer_id)])
    elif shape_type == "wobble":
        chapter.add_modifier(WobbleModifier(position=6, strength=0), [("layer", shape.layer_id)])
    else:
        chapter.add_modifier(DotDashModifier(distance=10), [("layer", shape.layer_id)])
    baseline = render(canvas)
    outline = np.all(baseline == [17, 34, 51, 255], axis=-1)
    fill = np.all(baseline == [176, 176, 176, 255], axis=-1)
    assert outline.any() and fill.any()
    overlay = SolidColorOverlayModifier(color="#FFFF0000")
    chapter.add_modifier(overlay, [("layer", shape.layer_id)])
    result = render(canvas)
    # Separately warped premultiplied channels can round one level differently.
    assert np.abs(result[outline].astype(int) - baseline[outline]).max() <= 1
    assert np.all(result[fill] == [255, 0, 0, 255])
    overlay.apply_to_outline = True
    assert np.abs(render(canvas)[outline].astype(int) - [255, 0, 0, 255]).max() <= 1


def test_raster_overlay_baking_undo_and_saved_texture(scene, tmp_path):
    from comic_editor.ui.baking import apply_raster_modifiers
    from comic_editor.core.persistence import SeriesRepository
    canvas, chapter, _, raster = scene
    path = tmp_path / "raw.png"
    path.write_bytes(base64.b64decode(encoded((250, 40, 20, 255))))
    modifier = TextureModifier(texture_data=import_texture(path), texture_name=path.name, blend_mode="replace")
    chapter.add_modifier(modifier, [("object", raster.object_id)])
    path.unlink()
    repository = SeriesRepository(tmp_path / "series")
    repository.create("Overlays")
    repository.save_chapter(chapter, canvas.tiles)
    restored, restored_tiles = repository.load_chapter(chapter.chapter_id)
    assert restored.modifiers[modifier.modifier_id].texture_data == modifier.texture_data
    expected = render(canvas)
    assert np.array_equal(expected[90, 100], [250, 40, 20, 255])
    canvas.set_selection("object", raster.object_id)
    apply_raster_modifiers(canvas, modifier.modifier_id)
    assert not raster.modifier_ids
    assert np.array_equal(render(canvas), expected)
    canvas.command_stack.undo()
    assert modifier.modifier_id in canvas.chapter.modifiers
    assert np.array_equal(render(canvas), expected)


@pytest.mark.parametrize("blender", [False, True])
def test_image_and_blender_overlay_render(scene, blender):
    canvas, chapter, shape, _ = scene
    source = (BlenderComicViewSourceDescriptor(project_uuid="a" * 32, view_uuid="b" * 32) if blender else None)
    image = ImageObject(x=50, y=50, pixel_width=40, pixel_height=40, **({"source": source} if source else {}))
    chapter.add_object(shape.layer_id, image)
    canvas.images.put(image.object_id, "image.png", base64.b64decode(encoded((0, 255, 0, 255))))
    chapter.add_modifier(TextureModifier(texture_data=encoded((255, 0, 0, 255)), blend_mode="replace"),
                         [("object", image.object_id)])
    assert np.array_equal(render(canvas)[65, 65], [255, 0, 0, 255])


@pytest.mark.parametrize("mirror", [False, True])
def test_shape_overlay_order_and_transformed_outline(scene, mirror):
    canvas, chapter, shape, _ = scene
    shape.translate_x = 15
    shape.translate_y = 10
    if mirror:
        chapter.add_modifier(MirrorModifier(axis_start=(300, 0), axis_end=(300, 200)), [("layer", shape.layer_id)])
    baseline = render(canvas)
    chapter.add_modifier(SolidColorOverlayModifier(color="#FFFF0000"), [("layer", shape.layer_id)])
    result = render(canvas)
    assert np.array_equal(result[42, 100], baseline[42, 100])
    assert np.array_equal(result[80, 100], [255, 0, 0, 255])
    if mirror:
        assert np.array_equal(result[42, 500], baseline[42, 500])
        assert np.array_equal(result[80, 500], [255, 0, 0, 255])


def test_texture_full_frame_sampling_survives_viewport_crop(scene):
    from comic_editor.ui.effect_pipeline import render_stages
    canvas, _, _, _ = scene
    texture = Image.new("RGBA", (64, 1))
    for x in range(64):
        texture.putpixel((x, 0), (x * 4, 0, 0, 255))
    stream = BytesIO()
    texture.save(stream, "PNG")
    effect = TextureModifier(texture_data=base64.b64encode(stream.getvalue()).decode("ascii"), blend_mode="replace")
    image = QImage(640, 50, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("white"))
    frame = QRectF(0, 0, 640, 50)
    full, _ = render_stages(canvas, image, frame, [effect], QTransform(), source_key=("test",))
    canvas._interactive_render = True
    canvas._effect_region_requests = True
    canvas._projection_exact = True
    for left in (0, 256, 512):
        cropped, bounds = render_stages(canvas, image, frame, [effect], QTransform(), source_key=("test",),
            required=QRectF(left, 0, 128, 50), request_scope=("test", left))
        expected = full.copy(bounds.toAlignedRect())
        assert np.array_equal(pixels(cropped), pixels(expected))


def test_texture_quad_moves_scales_and_free_warps_without_moving_pixels():
    image = QImage(100, 80, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("blue"))
    effect = TextureModifier(texture_data=encoded((255, 0, 0, 255)), blend_mode="replace",
                             texture_quad=[(20, 10), (60, 10), (60, 50), (20, 50)])
    result = pixels(apply_modifier_stack(image, [effect], (0, 0)))
    assert np.array_equal(result[20, 30], [255, 0, 0, 255])
    assert np.array_equal(result[20, 70], [0, 0, 255, 255])
    assert np.array_equal(result[60, 30], [0, 0, 255, 255])
    effect.texture_quad = [(30, 20), (80, 15), (65, 65), (15, 55)]
    assert modifier_from_dict(effect.to_dict()).texture_quad == effect.texture_quad
    warped = pixels(apply_modifier_stack(image, [effect], (0, 0)))
    assert np.array_equal(warped[35, 50], [255, 0, 0, 255])
    assert np.array_equal(warped[70, 50], [0, 0, 255, 255])
    # Mapping a world-space quad into a cropped image must keep its placement.
    cropped = apply_modifier_stack(image.copy(10, 10, 70, 60), [effect], (10, 10),
                                   world_to_image=QTransform.fromTranslate(-10, -10))
    assert np.array_equal(pixels(cropped), warped[10:70, 10:80])


@pytest.mark.parametrize("mode", ["uniform", "free"])
@pytest.mark.parametrize("handle", range(8))
def test_eight_texture_gizmo_handles_transform_only_modifier_with_undo(scene, mode, handle):
    canvas, chapter, shape, _ = scene
    effect = TextureModifier(texture_data=encoded(), transform_mode=mode,
                             texture_quad=[(30, 30), (190, 30), (190, 150), (30, 150)])
    chapter.add_modifier(effect, [("layer", shape.layer_id)])
    canvas.set_selection("layer", shape.layer_id)
    canvas.modifier_mode, canvas.active_modifier_id = True, effect.modifier_id
    original = list(effect.texture_quad)
    layer_before = shape.to_dict()
    position = canvas.document_to_widget(QPointF(*canvas._quad_handles(original)[handle]))
    assert canvas._texture_handle_hit(position) == handle
    assert canvas._begin_modifier_handle(position)
    assert canvas._move_modifier_handle(position + QPointF(12, 10))
    assert effect.texture_quad != original
    assert shape.to_dict() == layer_before
    if mode == "uniform":
        start_width, start_height = np.linalg.norm(np.subtract(original[1], original[0])), np.linalg.norm(np.subtract(original[3], original[0]))
        width, height = np.linalg.norm(np.subtract(effect.texture_quad[1], effect.texture_quad[0])), np.linalg.norm(np.subtract(effect.texture_quad[3], effect.texture_quad[0]))
        assert width / height == pytest.approx(start_width / start_height)
    else:
        changed = {i for i in range(4) if effect.texture_quad[i] != original[i]}
        assert changed == ({handle} if handle < 4 else {handle - 4, (handle - 3) % 4})
    transformed = list(effect.texture_quad)
    assert canvas._finish_modifier_handle()
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[effect.modifier_id].texture_quad == original
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[effect.modifier_id].texture_quad == transformed


def test_texture_gizmo_selected_only_move_rotate_mode_and_escape(scene):
    canvas, chapter, shape, raster = scene
    canvas.scale = 1
    effect = TextureModifier(texture_data=encoded(), texture_quad=[(30, 30), (190, 30), (190, 150), (30, 150)])
    chapter.add_modifier(effect, [("layer", shape.layer_id)])
    canvas.set_selection("layer", shape.layer_id)
    canvas.active_modifier_id = effect.modifier_id
    point = canvas.document_to_widget(QPointF(30, 30))
    assert canvas._texture_handle_hit(point) is None
    canvas.modifier_mode = True
    assert canvas._texture_handle_hit(point) == 0
    image = QImage(640, 480, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    canvas._draw_selection(painter)
    assert not pixels(image)[..., 3].any()
    assert canvas._draw_texture_handles(painter)
    painter.end()
    effect.muted = True
    assert canvas._texture_handle_hit(point) is None
    effect.muted = False
    canvas.active_modifier_id = ""
    assert canvas._texture_handle_hit(point) is None
    canvas.active_modifier_id = effect.modifier_id
    canvas.set_selection("object", raster.object_id)
    assert canvas._texture_handle_hit(point) is None
    canvas.set_selection("layer", shape.layer_id)
    canvas.active_modifier_id = effect.modifier_id
    mode_point = canvas._texture_mode_rect(effect).center()
    assert canvas._begin_modifier_handle(mode_point)
    assert effect.transform_mode == "free"
    middle = canvas.document_to_widget(QPointF(110, 90))
    assert canvas._begin_modifier_handle(middle)
    canvas._move_modifier_handle(middle + QPointF(20, 10))
    assert effect.texture_quad[0] == pytest.approx((50, 40))
    canvas._finish_modifier_handle()
    before_rotate = list(effect.texture_quad)
    corner = canvas.document_to_widget(QPointF(*effect.texture_quad[0])) - QPointF(15, 15)
    assert canvas._texture_handle_hit(corner) == "rotate"
    assert canvas._begin_modifier_handle(corner)
    canvas._move_modifier_handle(corner + QPointF(30, -20))
    assert effect.texture_quad != before_rotate
    QTest.keyClick(canvas, Qt.Key_Escape)
    assert canvas._modifier_handle_drag is None
    assert canvas.chapter.modifiers[effect.modifier_id].texture_quad == before_rotate


def test_texture_selection_hides_and_disables_layer_geometry_controls(scene):
    canvas, chapter, shape, _ = scene
    effect = TextureModifier(texture_data=encoded(),
                             texture_quad=[(70, 60), (150, 60), (150, 120), (70, 120)])
    chapter.add_modifier(effect, [("layer", shape.layer_id)])
    canvas.set_selection("layer", shape.layer_id)
    canvas.modifier_mode, canvas.active_modifier_id = True, effect.modifier_id
    canvas.scale, canvas.center_x, canvas.center_y = 1, 110, 90
    original = chapter.to_dict()
    # A hidden shape corner must not resize the artwork during texture editing.
    point = canvas.document_to_widget(QPointF(30, 30)).toPoint()
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, point)
    QTest.mouseMove(canvas, point + QPointF(-10, -10).toPoint())
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, point + QPointF(-10, -10).toPoint())
    assert chapter.to_dict() == original


def test_texture_mouse_gizmo_takes_priority_over_brush_and_shift_preserves_proportions(scene):
    canvas, chapter, shape, _ = scene
    effect = TextureModifier(texture_data=encoded(), transform_mode="free",
                             texture_quad=[(30, 30), (190, 30), (190, 150), (30, 150)])
    chapter.add_modifier(effect, [("layer", shape.layer_id)])
    canvas.set_selection("layer", shape.layer_id)
    canvas.modifier_mode, canvas.active_modifier_id = True, effect.modifier_id
    canvas.set_tool(ToolKind.BRUSH)
    canvas.scale, canvas.center_x, canvas.center_y = 1, 110, 90
    point = canvas.document_to_widget(QPointF(190, 150)).toPoint()
    QTest.mousePress(canvas, Qt.LeftButton, Qt.ShiftModifier, point)
    assert not canvas._nav_mode
    assert canvas._paint_brush_stroke is None
    assert canvas._modifier_handle_drag["uniform"]
    QTest.mouseMove(canvas, point + QPointF(30, 5).toPoint())
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.ShiftModifier, point + QPointF(30, 5).toPoint())
    # Release the simulated key too, so later tree-selection tests don't
    # inherit Shift from this uniform-transform gesture.
    QTest.keyRelease(canvas, Qt.Key_Shift)
    assert canvas._modifier_handle_drag is None
    assert effect.texture_quad != [(30, 30), (190, 30), (190, 150), (30, 150)]
    width = np.linalg.norm(np.subtract(effect.texture_quad[1], effect.texture_quad[0]))
    height = np.linalg.norm(np.subtract(effect.texture_quad[3], effect.texture_quad[0]))
    assert width / height == pytest.approx(4 / 3)


def test_texture_placement_stays_with_saved_asset(scene):
    from comic_editor.core.assets import extract_asset, instantiate_asset
    canvas, chapter, shape, _ = scene
    effect = TextureModifier(texture_data=encoded((255, 0, 0, 255)), blend_mode="replace",
                             texture_quad=[(30, 30), (110, 30), (110, 150), (30, 150)])
    chapter.add_modifier(effect, [("layer", shape.layer_id)])
    manifest, source_tiles = extract_asset(chapter, canvas.tiles, "layer", shape.layer_id, "Textured shape")
    target = ChapterDocument(height=200, background="#FFFFFFFF")
    page = target.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 200))
    page.fill_color, page.border_width = None, 0
    tiles = TileStore()
    _, layer_id, _ = instantiate_asset(manifest, source_tiles, target, tiles, page.layer_id, 400, 90)
    canvas.set_document(target, tiles)
    result = render(canvas)
    assert np.array_equal(result[70, 350], [255, 0, 0, 255])
    assert np.array_equal(result[70, 450], [176, 176, 176, 255])
    assert np.array_equal(result[32, 350], [17, 34, 51, 255])
    loaded = ChapterDocument.from_dict(target.to_dict())
    placed = loaded.modifiers[loaded.layers[layer_id].modifier_ids[-1]]
    assert placed.texture_quad[0] == pytest.approx((320, 30))


def test_missing_or_corrupt_texture_leaves_pixels_unchanged():
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    for data in ("", "not an image"):
        result = apply_modifier_stack(image, [TextureModifier(texture_data=data)], (0, 0))
        assert np.array_equal(pixels(result), pixels(image))


def test_render_key_uses_content_not_label():
    modifier = TextureModifier(texture_data=encoded(), texture_name="one.png")
    key = modifier_render_settings(modifier)
    assert len(key["texture_data"]) == 64
    modifier.texture_name = "renamed.png"
    assert key == modifier_render_settings(modifier)
    modifier.texture_data = encoded((0, 0, 0, 255))
    assert key != modifier_render_settings(modifier)


@pytest.mark.parametrize("target_kind, attribute, value", [
    (kind, attribute, value) for kind in ("layer", "object")
    for attribute, value in (("color", "#FF00FF00"), ("intensity", 35),
                             ("blend_mode", "multiply"), ("apply_to_outline", True))
    if kind == "layer" or attribute != "apply_to_outline"
])
def test_solid_overlay_edits_refresh_warm_canvas(scene, target_kind, attribute, value):
    canvas, chapter, shape, raster = scene
    identifier = shape.layer_id if target_kind == "layer" else raster.object_id
    canvas.set_selection(target_kind, identifier)
    modifier = SolidColorOverlayModifier(color="#FFFF8040")
    chapter.add_modifier(modifier, [(target_kind, identifier)])
    owner = ModifierControls(canvas)
    canvas.center_x, canvas.center_y, canvas.scale = 150, 100, 1
    canvas._ensure_scene_cache()
    before = pixels(canvas._scene_cache)
    source_keys = set(canvas._modifier_source_cache)
    owner.set_parameter(modifier.modifier_id, attribute, value, False)
    canvas._ensure_scene_cache()
    after = pixels(canvas._scene_cache)
    assert not np.array_equal(before, after)
    assert source_keys == set(canvas._modifier_source_cache)
    # A fully cold render must agree without moving any artwork or the camera.
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._effect_jobs.cancel(clear_retained=True)
    canvas._document_projection.clear()
    canvas._invalidate_scene_cache()
    canvas._ensure_scene_cache()
    np.testing.assert_array_equal(after, pixels(canvas._scene_cache))
    owner.deleteLater()


@pytest.mark.parametrize("accept", [False, True])
@pytest.mark.parametrize("target_kind", ["layer", "object"])
def test_solid_overlay_picker_previews_before_apply_and_cancels(scene, qapp, accept, target_kind):
    canvas, chapter, shape, raster = scene
    identifier = shape.layer_id if target_kind == "layer" else raster.object_id
    canvas.set_selection(target_kind, identifier)
    modifier = SolidColorOverlayModifier(color="#FFFF0000")
    chapter.add_modifier(modifier, [(target_kind, identifier)])
    owner = ModifierControls(canvas)
    owner.refresh()
    controls = owner._cards[modifier.modifier_id].findChild(OverlayControls)
    canvas.center_x, canvas.center_y, canvas.scale = 150, 100, 1
    canvas._ensure_scene_cache()
    before = pixels(canvas._scene_cache)
    revision = canvas.command_stack.revision
    controls.color.click()
    popup = controls._popup
    qapp.processEvents()
    popup.workspace.panel.apply_color("#FF00FF00")
    wait(qapp, lambda: canvas._overlay_color_preview is not None
         and canvas._overlay_color_preview[2].color == "#FF00FF00")
    canvas._ensure_scene_cache()
    during = pixels(canvas._scene_cache)
    assert modifier.color == "#FFFF0000"  # Draft is absent from saved artwork.
    assert not np.array_equal(before, during)
    assert canvas.command_stack.revision == revision
    if accept:
        popup.accept()
    else:
        popup.reject()
    canvas._ensure_scene_cache()
    assert canvas._overlay_color_preview is None
    assert modifier.color == ("#FF00FF00" if accept else "#FFFF0000")
    np.testing.assert_array_equal(during if accept else before, pixels(canvas._scene_cache))
    assert canvas.command_stack.revision == revision + int(accept)
    if accept:
        canvas.command_stack.undo()
        canvas._ensure_scene_cache()
        np.testing.assert_array_equal(before, pixels(canvas._scene_cache))
        canvas.command_stack.redo()
        canvas._ensure_scene_cache()
        np.testing.assert_array_equal(during, pixels(canvas._scene_cache))
    owner.deleteLater()


@pytest.mark.parametrize("accept", [False, True])
def test_solid_overlay_picker_coalesces_and_flushes_last_color(scene, qapp, accept):
    from comic_editor.ui.cache_dependencies import exact_cache_allowed
    canvas, chapter, shape, _ = scene
    canvas.set_selection("layer", shape.layer_id)
    modifier = SolidColorOverlayModifier(color="#FFFF0000")
    chapter.add_modifier(modifier, [("layer", shape.layer_id)])
    owner = ModifierControls(canvas)
    owner.refresh()
    controls = owner._cards[modifier.modifier_id].findChild(OverlayControls)
    controls.color.click()
    popup = controls._popup
    qapp.processEvents()
    changes = []
    canvas.visualChanged.connect(lambda _region: changes.append(1))
    for color in ("#FF00FF00", "#FF112233", "#FF556677"):
        popup.workspace.panel.apply_color(color)
    assert not changes
    assert chapter.modifiers[modifier.modifier_id].color == "#FFFF0000"
    wait(qapp, lambda: bool(changes))
    assert len(changes) == 1
    assert canvas._overlay_color_preview[2].color == "#FF556677"
    # Preview captures use the normal kernels and cannot enter disk backing.
    canvas._projection_exact = True
    assert not exact_cache_allowed(canvas, ("stage-stack", "source"))
    canvas._projection_exact = False
    canvas._ensure_scene_cache()
    source_keys = set(canvas._modifier_source_cache)
    popup.workspace.panel.apply_color("#FF8899AA")
    wait(qapp, lambda: canvas._overlay_color_preview[2].color == "#FF8899AA")
    canvas._ensure_scene_cache()
    assert source_keys == set(canvas._modifier_source_cache)
    # Apply/Cancel while the final update is still queued must retire the timer.
    popup.workspace.panel.apply_color("#FFABCDEF")
    revision = canvas.command_stack.revision
    if accept:
        popup.accept()
    else:
        popup.reject()
    QTest.qWait(20)
    assert canvas._overlay_color_preview is None
    assert not controls._color_timer.isActive()
    assert modifier.color == ("#FFABCDEF" if accept else "#FFFF0000")
    assert canvas.command_stack.revision == revision + int(accept)
    owner.deleteLater()


def test_solid_overlay_picker_drag_repaints_visible_canvas(scene, qapp):
    canvas, chapter, shape, _ = scene
    canvas.set_selection("layer", shape.layer_id)
    modifier = SolidColorOverlayModifier(color="#FFFF0000")
    chapter.add_modifier(modifier, [("layer", shape.layer_id)])
    owner = ModifierControls(canvas, canvas)
    owner.refresh()
    controls = owner._cards[modifier.modifier_id].findChild(OverlayControls)
    canvas.center_x, canvas.center_y, canvas.scale = 150, 100, 1
    canvas.show()
    qapp.processEvents()
    # The cold widget may return a pending frame; cancellation compares
    # against completed artwork rather than that loading presentation.
    canvas._ensure_scene_cache()
    assert not canvas._projection_frame_pending
    before = pixels(canvas.grab().toImage())
    controls.color.click()
    popup = controls._popup
    qapp.processEvents()
    QTest.mousePress(popup.picker, Qt.LeftButton, pos=popup.picker.sv_rect().center().toPoint())
    wait(qapp, lambda: canvas._overlay_color_preview is not None)
    assert not np.array_equal(before, pixels(canvas.grab().toImage()))
    assert modifier.color == "#FFFF0000"
    QTest.mouseRelease(popup.picker, Qt.LeftButton)
    popup.reject()
    np.testing.assert_array_equal(before, pixels(canvas.grab().toImage()))


def test_solid_overlay_inspector_deletion_retires_preview(scene, qapp):
    canvas, chapter, shape, _ = scene
    canvas.set_selection("layer", shape.layer_id)
    modifier = SolidColorOverlayModifier(color="#FFFF0000")
    chapter.add_modifier(modifier, [("layer", shape.layer_id)])
    owner = ModifierControls(canvas, canvas)
    owner.refresh()
    controls = owner._cards[modifier.modifier_id].findChild(OverlayControls)
    controls.color.click()
    popup = controls._popup
    qapp.processEvents()
    popup.workspace.panel.apply_color("#FF00FF00")
    wait(qapp, lambda: canvas._overlay_color_preview is not None)
    owner.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert canvas._overlay_color_preview is None
    assert modifier.color == "#FFFF0000"
    assert not canvas.command_stack.can_undo


def test_texture_directory_settings_dialog_persistence(qapp, tmp_path, monkeypatch):
    from comic_editor.core import settings as settings_module
    monkeypatch.setattr(settings_module, "settings_path", lambda: tmp_path / "settings.json")
    settings = EditorSettings()
    assert settings.texture_directory == DEFAULT_TEXTURE_DIRECTORY
    dialog = SettingsDialog(settings)
    assert "Paths" in [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    dialog.texture_directory.setText(str(tmp_path))
    dialog.apply_user_settings(settings)
    save_settings(settings)
    assert load_settings().texture_directory == str(tmp_path)
    dialog.deleteLater()


def wait(qapp, predicate, seconds=4):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        qapp.processEvents()
        QTest.qWait(10)
    assert predicate()


def test_category_hover_thumbnails_selection_and_edge_scrolling(qapp, tmp_path):
    for name, count in (("Paper", 30), ("Wood", 1)):
        folder = tmp_path / name
        folder.mkdir()
        for i in range(count):
            (folder / f"Texture {i:02}.png").write_bytes(base64.b64decode(encoded()))
    (tmp_path / "ignore.txt").write_text("no", encoding="utf-8")
    assert len(texture_categories(str(tmp_path))["Paper"]) == 30
    combo = TextureCombo(lambda: str(tmp_path))
    combo.resize(240, 28)
    combo.show()
    combo.showPopup()
    menu = combo._menu
    wait(qapp, lambda: menu.grid.count() == 30)
    wait(qapp, lambda: not menu.grid.item(0).icon().isNull())
    assert menu.grid.verticalScrollBar().maximum() > 0
    QCursor.setPos(menu.grid.viewport().mapToGlobal(menu.grid.viewport().rect().bottomLeft()) + QPointF(100, -4).toPoint())
    wait(qapp, lambda: menu.grid.verticalScrollBar().value() > 0)
    menu.grid.verticalScrollBar().setValue(menu.grid.verticalScrollBar().maximum())
    QCursor.setPos(menu.grid.viewport().mapToGlobal(menu.grid.viewport().rect().topLeft()) + QPointF(100, 4).toPoint())
    before = menu.grid.verticalScrollBar().value()
    wait(qapp, lambda: menu.grid.verticalScrollBar().value() < before)
    menu.categories.itemEntered.emit(menu.categories.item(1))
    assert menu.grid.count() == 1
    chosen = []
    combo.textureSelected.connect(lambda path, category: chosen.append((path, category)))
    menu._select(menu.grid.item(0))
    assert chosen[0][1] == "Wood"
    assert not menu.isVisible()
    combo.deleteLater()


def test_modifier_picker_controls_and_async_selection_undo(scene, qapp, tmp_path):
    canvas, chapter, shape, raster = scene
    canvas.set_selection("object", raster.object_id)
    owner = ModifierControls(canvas)
    owner.refresh()
    assert owner.overlay_menu.menuAction().isVisible()
    owner.add_modifier("texture")
    modifier = chapter.modifiers[raster.modifier_ids[-1]]
    controls = owner._cards[modifier.modifier_id].findChild(OverlayControls)
    assert controls.blend.findData("replace") >= 0
    assert controls.outline.isHidden()
    controls.transform_mode.setCurrentIndex(controls.transform_mode.findData("free"))
    assert modifier.transform_mode == "free"
    modifier.texture_quad = [(0, 0), (10, 0), (10, 10), (0, 10)]
    controls.fit.click()
    assert modifier.texture_quad == canvas._texture_default_quad(modifier)
    path = tmp_path / "selected.png"
    path.write_bytes(base64.b64decode(encoded()))
    controls._import(path, "Paper")
    wait(qapp, lambda: bool(modifier.texture_data))
    assert modifier.texture_category == "Paper" and modifier.texture_name == path.name
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].texture_data == ""
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[modifier.modifier_id].texture_name == path.name
    canvas.set_selection("layer", shape.layer_id)
    owner.add_modifier("solid_color_overlay")
    solid = canvas.chapter.modifiers[canvas.chapter.layers[shape.layer_id].modifier_ids[-1]]
    controls = owner._cards[solid.modifier_id].findChild(OverlayControls)
    assert not controls.outline.isHidden() and not controls.outline.isChecked()
    controls.outline.setChecked(True)
    assert solid.apply_to_outline
    owner.deleteLater()


@pytest.mark.parametrize("attribute, value", [("hue", 120), ("saturation", -45), ("lightness", 30)])
def test_texture_color_controls_commit_one_undoable_change(scene, attribute, value):
    canvas, chapter, _, raster = scene
    canvas.set_selection("object", raster.object_id)
    modifier = TextureModifier(texture_data=encoded())
    chapter.add_modifier(modifier, [("object", raster.object_id)])
    owner = ModifierControls(canvas)
    owner.refresh()
    card = owner._cards[modifier.modifier_id]
    slider, number = card._parameter_controls[attribute]
    slider.sliderPressed.emit()
    slider.setValue(value)
    slider.sliderReleased.emit()
    assert getattr(modifier, attribute) == value
    assert number.value() == value
    assert modifier.texture_data == encoded()
    canvas.command_stack.undo()
    assert getattr(canvas.chapter.modifiers[modifier.modifier_id], attribute) == 0
    canvas.command_stack.redo()
    assert getattr(canvas.chapter.modifiers[modifier.modifier_id], attribute) == value
    owner.deleteLater()
