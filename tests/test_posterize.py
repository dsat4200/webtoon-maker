from __future__ import annotations

import json

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QTransform
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox, QInputDialog

from comic_editor.core.assets import extract_asset
from comic_editor.core.models import (
    POSTERIZE_MIN_SPAN, BoundGeometry, ChapterDocument,
    HueSaturationLightnessModifier, ImageObject, ParameterMaskBinding,
    PosterizeModifier, PosterizeValueModifier, PosterizeRange, RasterObject, ShapeStyle,
    modifier_from_dict,
)
from comic_editor.core.posterize import HueStatistics, ValueStatistics, range_indices, rgb_hues, rgb_values
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.baking import apply_raster_modifiers
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_rendering import apply_modifier_stack
from comic_editor.ui.posterize_controls import PosterizeControls, PosterizeSampler, SimplifyColorsControls
from comic_editor.ui.posterize_value_controls import ValueRangeMap


def image_of(colors):
    image = QImage(len(colors), 1, QImage.Format_ARGB32_Premultiplied)
    for i, color in enumerate(colors):
        image.setPixelColor(i, 0, QColor(color))
    return image


@pytest.fixture
def editor(qapp):
    chapter = ChapterDocument(name="Posterize test", height=256)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 256, 256))
    layer = chapter.add_layer(page.layer_id, "Art", BoundGeometry.rectangle(0, 0, 256, 256))
    raster = chapter.add_object(layer.layer_id, RasterObject(interaction_rect=(0, 0, 256, 256)))
    tiles = TileStore()
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor("red"))
    tiles.set_tile(raster.object_id, (0, 0), tile)
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    canvas.set_selection("object", raster.object_id)
    controls = ModifierControls(canvas)
    yield canvas, controls, raster
    controls.deleteLater()
    canvas.deleteLater()


def test_posterize_round_trip_and_invalid_boundaries():
    modifier = PosterizeModifier(ranges=[PosterizeRange(300, "#8000ff00"), PosterizeRange(40, "#112233")],
                                intensity=75, muted=True, expanded=False,
                                parameter_masks={"intensity": ParameterMaskBinding("mask", 0, 100)})
    serialized = modifier.to_dict()
    assert modifier_from_dict(json.loads(json.dumps(serialized))).to_dict() == serialized
    assert serialized["ranges"][0]["color"] == "#FF112233"
    for ranges in ([], [PosterizeRange(0), PosterizeRange(0)],
                   [PosterizeRange(2), PosterizeRange(359)], [PosterizeRange(float("nan"))]):
        with pytest.raises(ValueError):
            PosterizeModifier(ranges=ranges).validate()


def test_wraparound_mapping_alpha_intensity_and_mute():
    modifier = PosterizeModifier(ranges=[PosterizeRange(60, "#FF0000FF"), PosterizeRange(300, "#8000FF00")])
    source = image_of(["#80FF0000", "#FFFFFF00", "#FFFF00FF", "#00FFFFFF"])
    result = apply_modifier_stack(source, [modifier], (0, 0))
    assert result.pixelColor(0, 0).getRgb() == (0, 255, 0, 64)
    assert result.pixelColor(1, 0).name() == "#0000ff"
    assert result.pixelColor(2, 0).green() == 255
    assert result.pixelColor(3, 0).alpha() == 0
    modifier.intensity = 0
    assert apply_modifier_stack(source, [modifier], (0, 0)) == source
    modifier.intensity, modifier.muted = 100, True
    assert apply_modifier_stack(source, [modifier], (0, 0)) == source
    modifier.muted, modifier.intensity = False, 50
    result = apply_modifier_stack(image_of(["red"]), [modifier], (0, 0)).pixelColor(0, 0)
    assert 180 <= result.alpha() <= 193 and result.red() > result.green() > 70


def test_intensity_mask_and_stack_order():
    modifier = PosterizeModifier(ranges=[PosterizeRange(60, "#FF0000FF"), PosterizeRange(300, "#FF00FF00")])
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("mask", 0, 100)
    source = image_of(["red", "red"])
    result = apply_modifier_stack(source, [modifier], (0, 0),
        {(modifier.modifier_id, "intensity"): np.array([[0., 1.]])})
    assert result.pixelColor(0, 0).name() == "#ff0000"
    assert result.pixelColor(1, 0).name() == "#00ff00"
    modifier.parameter_masks.clear()
    result = apply_modifier_stack(source, [HueSaturationLightnessModifier(hue=120), modifier], (0, 0))
    assert result.pixelColor(0, 0).name() == "#0000ff"


def test_statistics_weight_alpha_ignore_transparency_and_average_rgb():
    statistics = HueStatistics()
    statistics.add(np.array([[[1., 0., 0., 1.], [.5, 0., 0., .5], [0., 1., 0., 0.]]]))
    assert statistics.counts.sum() == 1.5
    assert statistics.counts[0] == 1.5
    ranges = statistics.initialize(1)
    assert ranges[0].color == "#FFD40000"


def test_initialization_keeps_rare_distinct_hues_and_red_seam():
    statistics = HueStatistics()
    statistics.add(np.array([[[1., 0., 0., 1.]]] * 100 + [[[0., 1., 0., 1.]], [[0., 0., 1., 1.]]]))
    ranges = statistics.initialize(3)
    PosterizeModifier(ranges=ranges).validate()
    mapped = [ranges[index].color for index in range_indices(np.array([0., 120., 240.]), ranges)]
    assert mapped == ["#FFFF0000", "#FF00FF00", "#FF0000FF"]
    colors = np.array([QColor.fromHsv(hue, 255, 255).getRgbF() for hue in [1, 359, 120]])
    statistics = HueStatistics()
    statistics.add(colors)
    ranges = statistics.initialize(2)
    indices = range_indices(rgb_hues(colors[:, :3]), ranges)
    assert indices[0] == indices[1] != indices[2]


@pytest.mark.parametrize("count", [1, 2, 6, 24])
def test_empty_and_monochrome_initialization_has_requested_count(count):
    for pixels in (np.zeros((1, 1, 4)), np.array([[[.25, .25, .25, 1.]]])):
        statistics = HueStatistics()
        statistics.add(pixels)
        ranges = statistics.initialize(count)
        PosterizeModifier(ranges=ranges).validate()
        assert len(ranges) == count
        assert len({item.range_id for item in ranges}) == count
        assert len({item.color for item in ranges}) == 1


def test_sampler_reads_upstream_not_own_output_and_caches(editor):
    canvas, controls, raster = editor
    upstream = HueSaturationLightnessModifier(hue=120)
    modifier = PosterizeModifier(ranges=[PosterizeRange(0, "#FF0000FF")])
    downstream = HueSaturationLightnessModifier(hue=120)
    targets = controls.targets()
    for item in (upstream, modifier, downstream):
        canvas.chapter.add_modifier(item, targets)
    sampler = PosterizeSampler()
    before = canvas.chapter.to_dict()
    first = sampler.sample(canvas, targets, modifier.modifier_id)
    assert np.argmax(first.counts) == 120
    assert first.average(0, 360) == "#FF00FF00"
    assert canvas.chapter.to_dict() == before
    modifier.ranges[0].color = "#FFFFFF00"
    assert sampler.sample(canvas, targets, modifier.modifier_id) is first
    upstream.hue = -120
    assert np.argmax(sampler.sample(canvas, targets, modifier.modifier_id).counts) == 240


def test_sampler_isolates_shapes_and_restores_state_on_error(editor, monkeypatch):
    canvas, controls, raster = editor
    chapter = canvas.chapter
    layer = chapter.layers[raster.parent_layer_id]
    shape = chapter.add_layer(layer.parent_id, "Green shape", BoundGeometry.rectangle(10, 10, 100, 100),
                              style=ShapeStyle(primary_color="#FF00FF00", outline_thickness=0))
    statistics = PosterizeSampler().sample(canvas, [("layer", shape.layer_id)])
    assert statistics.average(0, 360) == "#FF00FF00"
    modifier = PosterizeModifier()
    chapter.add_modifier(modifier, controls.targets())
    before = chapter.to_dict()
    def fail(*_args):
        raise ValueError("test render failure")
    monkeypatch.setattr(canvas, "_render_object", fail)
    with pytest.raises(ValueError, match="test render failure"):
        PosterizeSampler().sample(canvas, controls.targets(), modifier.modifier_id)
    assert chapter.to_dict() == before
    assert canvas._interactive_render is False


def test_add_dialog_cancel_initialization_and_undo(editor, monkeypatch):
    canvas, controls, _raster = editor
    before = canvas.chapter.to_dict()
    monkeypatch.setattr(QInputDialog, "getInt", lambda *_args: (4, False))
    controls.add_modifier("posterize")
    assert canvas.chapter.to_dict() == before
    monkeypatch.setattr(QInputDialog, "getInt", lambda *_args: (4, True))
    controls.add_modifier("posterize")
    modifier = next(iter(canvas.chapter.modifiers.values()))
    assert len(modifier.ranges) == 4
    assert all(item.color == "#FFFF0000" for item in modifier.ranges)
    canvas.command_stack.undo()
    assert not canvas.chapter.modifiers
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[modifier.modifier_id].to_dict() == modifier.to_dict()


def move_wheel(wheel, point):
    event = QMouseEvent(QMouseEvent.MouseMove, point, wheel.mapToGlobal(point.toPoint()),
                       Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    wheel.mouseMoveEvent(event)


def test_drag_clamps_at_neighbors_crosses_zero_and_is_one_undo(editor, qapp):
    canvas, controls, _raster = editor
    modifier = PosterizeModifier(ranges=[PosterizeRange(30), PosterizeRange(150), PosterizeRange(270)])
    canvas.chapter.add_modifier(modifier, controls.targets())
    controls.refresh()
    panel = controls.findChild(PosterizeControls)
    wheel = panel.wheel
    controls.show()
    qapp.processEvents()
    _, radius = wheel.geometry_values()
    before = modifier.to_dict()
    depth = len(canvas.command_stack._undo)
    QTest.mousePress(wheel, Qt.LeftButton, pos=wheel.point(30, radius - 16).toPoint())
    for hue in (5, 355, 300, 260):
        move_wheel(wheel, wheel.point(hue, radius - 16))
    QTest.mouseRelease(wheel, Qt.LeftButton, pos=wheel.point(260, radius - 16).toPoint())
    moved = next(item for item in modifier.ranges if item.range_id == before["ranges"][0]["id"])
    assert moved.start == pytest.approx(270 + POSTERIZE_MIN_SPAN)
    assert len(canvas.command_stack._undo) == depth + 1
    modifier.validate()
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].to_dict() == before


def test_add_remove_ranges_and_existing_picker(editor, monkeypatch):
    canvas, controls, _raster = editor
    modifier = PosterizeModifier(ranges=[PosterizeRange(0, "#FF112233"), PosterizeRange(180, "#FF445566")])
    canvas.chapter.add_modifier(modifier, controls.targets())
    controls.refresh()
    panel = controls.findChild(PosterizeControls)
    original = modifier.to_dict()
    panel.add_range()
    assert len(modifier.ranges) == 3
    assert modifier.ranges[0].color == "#FF112233"
    selected = panel.wheel.selected_id
    from comic_editor.ui import color_picker
    calls = []
    def picker(parent, color, callback, title):
        calls.append((color, title))
        callback("#804488CC")
    monkeypatch.setattr(color_picker, "choose_color", picker)
    panel.choose_color(selected)
    assert calls
    assert next(item for item in modifier.ranges if item.range_id == selected).color == "#804488CC"
    panel.remove_range()
    assert modifier.to_dict() == original
    panel.remove_range()
    panel.remove_range()
    assert len(modifier.ranges) == 1


def test_raster_apply_and_asset_clone_preserve_posterize(editor):
    canvas, controls, raster = editor
    modifier = PosterizeModifier(ranges=[PosterizeRange(0, "#FF0000FF")])
    canvas.chapter.add_modifier(modifier, controls.targets())
    manifest, asset_tiles = extract_asset(canvas.chapter, canvas.tiles, "object", raster.object_id, "Posterized")
    cloned = next(iter(manifest.document.modifiers.values()))
    assert isinstance(cloned, PosterizeModifier)
    assert cloned.modifier_id != modifier.modifier_id
    assert cloned.ranges[0].color == "#FF0000FF"
    apply_raster_modifiers(canvas, modifier.modifier_id)
    assert not canvas.chapter.modifiers
    assert canvas.tiles.object_tiles(raster.object_id)[(0, 0)].pixelColor(20, 20).name() == "#0000ff"
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].ranges[0].color == "#FF0000FF"
    assert canvas.tiles.object_tiles(raster.object_id)[(0, 0)].pixelColor(20, 20).name() == "#ff0000"


def test_real_count_dialog_and_swatch_picker_apply_cancel(editor, qapp):
    canvas, controls, _raster = editor
    observed = []
    def accept_count():
        dialog = qapp.activeModalWidget()
        observed.append(isinstance(dialog, QInputDialog))
        dialog.setIntValue(2)
        dialog.accept()
    QTimer.singleShot(0, accept_count)
    controls.add_modifier("posterize")
    assert observed == [True]
    modifier = next(iter(canvas.chapter.modifiers.values()))
    assert len(modifier.ranges) == 2
    controls.show()
    qapp.processEvents()
    panel = controls.findChild(PosterizeControls)
    item, position, _anchor = panel.wheel.swatches()[0]
    QTest.mouseClick(panel.wheel, Qt.LeftButton, pos=position.toPoint())
    popup = panel._color_popup
    popup.workspace.panel.apply_color("#FF123456")
    QTest.mouseClick(popup.buttons.button(QDialogButtonBox.Cancel), Qt.LeftButton)
    assert modifier.ranges[0].color == "#FFFF0000"
    QTest.mouseClick(panel.color_button, Qt.LeftButton)
    popup = panel._color_popup
    popup.workspace.panel.apply_color("#804488CC")
    QTest.mouseClick(popup.buttons.button(QDialogButtonBox.Apply), Qt.LeftButton)
    assert next(value for value in modifier.ranges if value.range_id == item.range_id).color == "#804488CC"


def noisy_color_blocks(height=96, width=192):
    pixels = np.ones((height, width, 4), dtype=np.float32)
    pixels[:, :width//2, :3] = [.7, .55, .5]
    pixels[:, width//2:, :3] = [.2, .4, .65]
    pixels[..., :3] += np.random.default_rng(81).normal(0, .014, pixels[..., :3].shape)
    return pixels


def pixel_image(pixels):
    data = np.ascontiguousarray(np.rint(np.clip(pixels, 0, 1)*255), dtype=np.uint8)
    return QImage(data.data, data.shape[1], data.shape[0], data.shape[1]*4, QImage.Format_RGBA8888).copy()


def test_simplify_settings_round_trip_legacy_default_and_validation():
    modifier = PosterizeModifier(simplify_enabled=True, simplify_radius=7,
        simplify_tolerance=42, simplify_strength=85)
    data = modifier.to_dict()
    assert modifier_from_dict(data).to_dict() == data
    for key in list(data):
        if key.startswith("simplify_"):
            del data[key]
    assert not modifier_from_dict(data).simplify_enabled
    for name in ("simplify_radius", "simplify_tolerance", "simplify_strength"):
        for invalid in (float("nan"), float("inf")):
            modifier = PosterizeModifier()
            setattr(modifier, name, invalid)
            with pytest.raises(ValueError, match="finite"):
                modifier.validate()


def test_guided_simplification_reduces_noise_and_keeps_shape_edges():
    from comic_editor.core.color_smoothing import simplify_colors
    pixels = noisy_color_blocks()
    result = simplify_colors(pixels, PosterizeModifier(simplify_enabled=True))
    interior = np.s_[12:-12, 12:75, :3]
    assert result[interior].std(axis=(0, 1)).max() < pixels[interior].std(axis=(0, 1)).min() * .25
    # A half-unit red edge remains sharp across adjacent pixels.
    assert result[20:75, 95, 0].mean() - result[20:75, 96, 0].mean() > .40
    np.testing.assert_array_equal(result[..., 3], pixels[..., 3])
    assert simplify_colors(pixels, PosterizeModifier()) is pixels
    assert simplify_colors(pixels, PosterizeModifier(simplify_enabled=True, simplify_strength=0)) is pixels


def test_simplify_ignores_hidden_rgb_and_preserves_partial_alpha():
    from comic_editor.core.color_smoothing import simplify_colors
    pixels = np.ones((32, 48, 4), dtype=np.float32)
    pixels[..., :3] = [.8, .2, .1]
    pixels[..., 3] = np.linspace(0, 1, 48)[None, :]
    pixels[:, :12, :3] = [0, 1, 0]
    pixels[:, :12, 3] = 0
    modifier = PosterizeModifier(simplify_enabled=True, simplify_radius=5)
    result = simplify_colors(pixels, modifier)
    np.testing.assert_array_equal(result[..., 3], pixels[..., 3])
    np.testing.assert_allclose(result[:, 12:, :3], np.broadcast_to([.8, .2, .1], (32, 36, 3)), atol=1/255)
    pixels[..., 3] = 0
    assert np.isfinite(simplify_colors(pixels, modifier)).all()


def test_simplify_tiling_matches_whole_image():
    from comic_editor.core.color_smoothing import _smooth, _guided_tile
    pixels = noisy_color_blocks(290, 520)
    expected = pixels.copy()
    for _ in range(2):
        expected[..., :3] = _guided_tile(expected.astype(np.float64), 3, (25/400)**2)
    expected[..., :3] = np.rint(expected[..., :3] * 255) / 255
    result = _smooth(pixels, 3, 25, 100)
    np.testing.assert_allclose(result, expected, atol=1e-6)


def test_simplify_cache_reuses_pixels_and_respects_budget():
    from comic_editor.core.color_smoothing import SimplifyColorCache
    pixels = noisy_color_blocks(24, 32)
    cache = SimplifyColorCache(budget=pixels.nbytes * 2)
    first = cache.smooth(pixels, 3, 25, 100)
    assert cache.smooth(pixels.copy(), 3, 25, 100) is first
    assert cache.computations == 1
    cache.smooth(pixels, 4, 25, 100)
    cache.smooth(pixels, 4, 40, 100)
    assert cache.computations == 3
    assert cache.bytes <= cache.budget
    assert not first.flags.writeable


def test_simplify_reduces_posterized_gray_speckles():
    rng = np.random.default_rng(31)
    pixels = np.ones((96, 96, 4), dtype=np.float32)
    pixels[..., :3] = .6 + rng.normal(0, .006, (96, 96, 3))
    modifier = PosterizeModifier(ranges=[PosterizeRange(hue, QColor.fromHsv(hue, 255, 255).name())
                                       for hue in range(0, 360, 60)])
    source = pixel_image(pixels)
    from comic_editor.ui.modifier_rendering import _qimage_premultiplied
    before = _qimage_premultiplied(apply_modifier_stack(source, [modifier], (0, 0)))
    modifier.simplify_enabled = True
    after = _qimage_premultiplied(apply_modifier_stack(source, [modifier], (0, 0)))
    before_changes = np.any(before[:, 1:] != before[:, :-1], axis=2).sum()
    after_changes = np.any(after[:, 1:] != after[:, :-1], axis=2).sum()
    assert after_changes < before_changes * .35


def test_simplify_histogram_changes_without_resampling_or_resetting_palette(editor, monkeypatch):
    canvas, controls, raster = editor
    canvas.tiles.set_tile(raster.object_id, (0, 0), pixel_image(noisy_color_blocks(256, 256)))
    modifier = PosterizeModifier()
    canvas.chapter.add_modifier(modifier, controls.targets())
    sampler = PosterizeSampler()
    before = sampler.sample(canvas, controls.targets(), modifier.modifier_id)
    palette = [item.to_dict() for item in modifier.ranges]
    modifier.simplify_enabled = True
    def unexpected(*_):
        raise AssertionError("Smoothing settings should reuse the raw sample")
    monkeypatch.setattr(canvas, "_render_object", unexpected)
    after = sampler.sample(canvas, controls.targets(), modifier.modifier_id)
    assert not np.array_equal(before.counts, after.counts)
    assert before.counts.sum() == pytest.approx(after.counts.sum())
    assert [item.to_dict() for item in modifier.ranges] == palette


def test_simplify_controls_precede_posterization_and_undo_one_drag(editor, qapp):
    canvas, controls, _ = editor
    modifier = PosterizeModifier()
    canvas.chapter.add_modifier(modifier, controls.targets())
    controls.refresh()
    controls.show()
    qapp.processEvents()
    simplify = controls.findChild(SimplifyColorsControls)
    panel = controls.findChild(PosterizeControls)
    assert simplify.mapTo(controls, QPointF().toPoint()).y() < panel.mapTo(controls, QPointF().toPoint()).y()
    assert not simplify.body.isVisible()
    QTest.mouseClick(simplify.enabled, Qt.LeftButton)
    assert modifier.simplify_enabled and simplify.body.isVisible()
    before = modifier.to_dict()
    depth = len(canvas.command_stack._undo)
    slider = simplify.sliders["simplify_radius"]
    slider.sliderPressed.emit()
    for value in (4, 5, 6, 7):
        slider.setValue(value)
    slider.sliderReleased.emit()
    assert modifier.simplify_radius == 7
    assert len(canvas.command_stack._undo) == depth + 1
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].to_dict() == before
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[modifier.modifier_id].simplify_radius == 7


def test_simplify_viewport_halo_matches_full_render_with_upstream_effect(editor):
    from comic_editor.ui.effect_pipeline import render_stages
    canvas, controls, _ = editor
    source = pixel_image(noisy_color_blocks(96, 192))
    upstream = HueSaturationLightnessModifier(hue=25)
    modifier = PosterizeModifier(simplify_enabled=True, simplify_radius=5,
        ranges=[PosterizeRange(hue, QColor.fromHsv(hue, 255, 255).name()) for hue in range(0, 360, 30)])
    for item in (upstream, modifier):
        canvas.chapter.add_modifier(item, controls.targets())
    bounds = QRectF(0, 0, 192, 96)
    full, full_bounds = render_stages(canvas, source, bounds, [upstream, modifier], QTransform())
    crop = QRectF(80, 15, 45, 60)
    partial, partial_bounds = render_stages(canvas, source, bounds, [upstream, modifier], QTransform(), required=crop)
    assert full_bounds == bounds and partial_bounds == crop
    assert partial == full.copy(crop.toRect())


def test_simplify_image_object_matches_pixels_when_zoomed(editor):
    canvas, _controls, raster = editor
    source = pixel_image(noisy_color_blocks())
    obj = canvas.chapter.add_object(raster.parent_layer_id, ImageObject(
        pixel_width=192, pixel_height=96, source_filename="noise.png",
        transform_frame=(0, 0, 192, 96),
        transform_quad=[(0, 0), (192, 0), (192, 96), (0, 96)]))
    canvas.images.put_decoded(obj.object_id, "noise.png", b"test", source, "image/png")
    modifier = PosterizeModifier(simplify_enabled=True,
        ranges=[PosterizeRange(hue, QColor.fromHsv(hue, 255, 255).name()) for hue in range(0, 360, 30)])
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    expected = apply_modifier_stack(source, [modifier], (0, 0))
    for zoom in (1, 3):
        output = QImage(192*zoom, 96*zoom, QImage.Format_ARGB32_Premultiplied)
        output.fill(Qt.transparent)
        painter = QPainter(output)
        painter.scale(zoom, zoom)
        canvas._render_object(painter, obj, 1., QRectF(0, 0, 192, 96))
        painter.end()
        for y in range(10, 86, 5):
            for x in range(10, 182, 5):
                assert output.pixelColor(x*zoom+zoom//2, y*zoom+zoom//2) == expected.pixelColor(x, y)


def test_value_model_round_trip_and_fixed_endpoint_validation():
    modifier = PosterizeValueModifier(simplify_enabled=True, simplify_radius=7,
        simplify_tolerance=40, simplify_strength=75, intensity=80,
        ranges=[PosterizeRange(0, "#FF123456"), PosterizeRange(128, "#804488CC")])
    data = json.loads(json.dumps(modifier.to_dict()))
    restored = modifier_from_dict(data)
    assert type(restored) is PosterizeValueModifier
    assert restored.to_dict() == data
    assert restored.name == "Posterize Value"
    for starts in ([], [1], [-1, 128], [0, 0], [0, 128, 130], [0, 255], [0, 270]):
        with pytest.raises(ValueError):
            PosterizeValueModifier(ranges=[PosterizeRange(value) for value in starts]).validate()


def test_value_maps_brightness_to_any_color_including_black_white_and_alpha():
    modifier = PosterizeValueModifier(ranges=[PosterizeRange(0, "#FF0000FF"),
        PosterizeRange(64, "#FF00FF00"), PosterizeRange(192, "#80FF0000")])
    source = image_of(["#000000", "#3f3f3f", "#404040", "#bfbfbf", "#c0c0c0", "#ffffff", "#80ffffff"])
    result = apply_modifier_stack(source, [modifier], (0, 0))
    assert [result.pixelColor(i, 0).name() for i in range(7)] == [
        "#0000ff", "#0000ff", "#00ff00", "#00ff00", "#ff0000", "#ff0000", "#ff0000"]
    assert result.pixelColor(5, 0).alpha() == 128
    assert result.pixelColor(6, 0).alpha() == 64
    modifier.intensity = 0
    assert apply_modifier_stack(source, [modifier], (0, 0)) == source
    modifier.intensity, modifier.muted = 100, True
    assert apply_modifier_stack(source, [modifier], (0, 0)) == source


@pytest.mark.parametrize("simplify", [False, True])
def test_value_source_is_grayscale_independent_of_hue_even_with_simplify(simplify):
    pixels = np.ones((32, 64, 4), dtype=np.float32)
    pixels[:, :32, :3] = [1., 0., 0.]
    pixels[:, 32:, :3] = [0., 76/255., 0.]
    assert np.all(rgb_values(pixels[..., :3]) == 54)
    modifier = PosterizeValueModifier(simplify_enabled=simplify,
        ranges=[PosterizeRange(0, "#FF0000FF"), PosterizeRange(54, "#FFFF00FF")])
    result = apply_modifier_stack(pixel_image(pixels), [modifier], (0, 0))
    assert all(result.pixelColor(x, 16).name() == "#ff00ff" for x in range(64))


def test_value_statistics_initialize_grayscale_averages_and_ignore_transparency():
    statistics = ValueStatistics()
    pixels = np.array([[[0., 0., 0., 1.], [1., 0., 0., 1.],
                        [0., 1., 0., 1.], [1., 1., 1., 1.], [0., 0., 1., 0.]]])
    statistics.add(pixels)
    assert statistics.counts.sum() == 4
    ranges = statistics.initialize(4)
    PosterizeValueModifier(ranges=ranges).validate()
    assert ranges[0].start == 0
    assert [item.color for item in ranges] == ["#FF000000", "#FF363636", "#FFB6B6B6", "#FFFFFFFF"]


@pytest.mark.parametrize("count", [1, 6, 24])
def test_value_initialization_covers_empty_and_monochrome_sources(count):
    for pixels in (np.zeros((1, 1, 4)), np.array([[[1., 1., 1., 1.]]])):
        statistics = ValueStatistics()
        statistics.add(pixels)
        ranges = statistics.initialize(count)
        PosterizeValueModifier(ranges=ranges).validate()
        assert len(ranges) == count
        assert ranges[0].start == 0
        assert len({item.color for item in ranges}) == 1


def test_value_add_dialog_linear_ui_picker_and_simplify_settings(editor, qapp):
    canvas, controls, _ = editor
    observed = []
    def accept_count():
        dialog = qapp.activeModalWidget()
        observed.append(dialog.windowTitle())
        dialog.setIntValue(4)
        dialog.accept()
    QTimer.singleShot(0, accept_count)
    controls.add_modifier("posterize_value")
    assert observed == ["Posterize Value"]
    modifier = next(iter(canvas.chapter.modifiers.values()))
    assert type(modifier) is PosterizeValueModifier
    assert len(modifier.ranges) == 4
    assert all(item.color == "#FF363636" for item in modifier.ranges)
    controls.show()
    qapp.processEvents()
    panel = controls.findChild(PosterizeControls)
    assert isinstance(panel.wheel, ValueRangeMap)
    simplify = controls.findChild(SimplifyColorsControls)
    assert simplify.mapTo(controls, QPointF().toPoint()).y() < panel.mapTo(controls, QPointF().toPoint()).y()
    QTest.mouseClick(simplify.enabled, Qt.LeftButton)
    assert modifier.simplify_enabled
    item, point, _ = panel.wheel.swatches()[1]
    QTest.mouseClick(panel.wheel, Qt.LeftButton, pos=point.toPoint())
    popup = panel._color_popup
    assert popup.windowTitle() == "Posterize Value range color"
    popup.workspace.panel.apply_color("#FF0088DD")
    QTest.mouseClick(popup.buttons.button(QDialogButtonBox.Apply), Qt.LeftButton)
    assert next(value for value in modifier.ranges if value.range_id == item.range_id).color == "#FF0088DD"


def test_value_cancel_does_not_create_modifier(editor, monkeypatch):
    canvas, controls, _ = editor
    before = canvas.chapter.to_dict()
    monkeypatch.setattr(QInputDialog, "getInt", lambda *_: (6, False))
    controls.add_modifier("posterize_value")
    assert canvas.chapter.to_dict() == before


def test_value_handles_clamp_without_wrapping_and_undo_atomically(editor, qapp):
    canvas, controls, _ = editor
    modifier = PosterizeValueModifier(ranges=[PosterizeRange(0), PosterizeRange(100), PosterizeRange(200)])
    canvas.chapter.add_modifier(modifier, controls.targets())
    controls.refresh()
    controls.show()
    qapp.processEvents()
    chart = controls.findChild(ValueRangeMap)
    before, depth = modifier.to_dict(), len(canvas.command_stack._undo)
    QTest.mousePress(chart, Qt.LeftButton, pos=chart.handle_point(1).toPoint())
    move_wheel(chart, QPointF(chart.x_at(400), 130))
    assert modifier.ranges[1].start == 196
    move_wheel(chart, QPointF(chart.x_at(-100), 130))
    assert modifier.ranges[1].start == 4
    QTest.mouseRelease(chart, Qt.LeftButton, pos=chart.handle_point(1).toPoint())
    assert len(canvas.command_stack._undo) == depth + 1
    assert modifier.ranges[0].start == 0
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].to_dict() == before
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[modifier.modifier_id].ranges[1].start == 4


def test_value_add_remove_first_and_last_ranges_keep_complete_domain(editor):
    canvas, controls, _ = editor
    modifier = PosterizeValueModifier(ranges=[PosterizeRange(0, "#FF0000FF"),
        PosterizeRange(128, "#FFFF0000")])
    canvas.chapter.add_modifier(modifier, controls.targets())
    controls.refresh()
    panel = controls.findChild(PosterizeControls)
    panel.add_range()
    assert [item.start for item in modifier.ranges] == [0, 64, 128]
    panel.remove_range()
    assert [item.start for item in modifier.ranges] == [0, 128]
    panel.wheel.select(modifier.ranges[0].range_id)
    panel.remove_range()
    assert len(modifier.ranges) == 1
    assert modifier.ranges[0].start == 0
    assert modifier.ranges[0].color == "#FFFF0000"
    panel.remove_range()
    assert len(modifier.ranges) == 1
    panel.add_range()
    assert [item.start for item in modifier.ranges] == [0, 128]
    modifier.validate()


def test_value_sampler_simplification_assets_and_raster_baking(editor):
    canvas, controls, raster = editor
    source = pixel_image(noisy_color_blocks(256, 256))
    canvas.tiles.set_tile(raster.object_id, (0, 0), source)
    modifier = PosterizeValueModifier(simplify_enabled=True, simplify_radius=5,
        ranges=[PosterizeRange(0, "#FF0077BB"), PosterizeRange(128, "#FFCC8844")])
    canvas.chapter.add_modifier(modifier, controls.targets())
    statistics = PosterizeSampler(value_mode=True).sample(canvas, controls.targets(), modifier.modifier_id)
    assert isinstance(statistics, ValueStatistics) and len(statistics.counts) == 256
    assert statistics.counts.sum() == 256*256
    manifest, _ = extract_asset(canvas.chapter, canvas.tiles, "object", raster.object_id, "Value")
    cloned = next(iter(manifest.document.modifiers.values()))
    assert type(cloned) is PosterizeValueModifier
    assert cloned.simplify_enabled and cloned.simplify_radius == 5
    expected = apply_modifier_stack(source, [modifier], (0, 0))
    apply_raster_modifiers(canvas, modifier.modifier_id)
    result = canvas.tiles.object_tiles(raster.object_id)[(0, 0)]
    assert result == expected
    canvas.command_stack.undo()
    assert type(canvas.chapter.modifiers[modifier.modifier_id]) is PosterizeValueModifier
