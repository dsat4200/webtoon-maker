import copy
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QTransform
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QSpinBox

from comic_editor.core.assets import extract_asset, instantiate_asset
from comic_editor.core.effect_geometry import array_indices, array_transform, effect_bounds
from comic_editor.core.models import (
    ArrayModifier, BoundGeometry, ChapterDocument, HueSaturationLightnessModifier,
    OutlineModifier, ParameterMaskBinding, RasterObject, ToneMask, modifier_from_dict,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.baking import apply_raster_modifiers
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.modifier_controls import ModifierControls


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=360)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 480, 360))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(640, 480)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 240, 180, 1
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 480, 360)))
    canvas.tiles.paint_dab(obj.object_id, QPointF(180, 100), 10, QColor("red"), square=True, antialias=False)
    canvas.set_selection("object", obj.object_id)
    yield canvas, chapter, page, obj
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def pixels(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine()).copy()


def attach(scene, **kwargs):
    canvas, chapter, page, obj = scene
    modifier = ArrayModifier(axis_start=(180, 140), axis_end=(220, 140), center=(180, 100), **kwargs)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    return modifier


@pytest.mark.parametrize("mode,count,expected", [
    ("first", 3, [1, 2, 3]), ("last", 3, [-3, -2, -1]),
    ("center", 4, [-2, -1, 1, 2]), ("center", 3, [-1, 1, 2]),
    ("center", 1, [1]), ("center", 0, []),
])
def test_repeat_counts_modes_and_source_preservation(scene, mode, count, expected):
    canvas, chapter, page, obj = scene
    original = copy.deepcopy(obj.to_dict())
    before = pixels(render(canvas))
    modifier = attach(scene, count=count, repeat_type=mode)
    assert list(array_indices(modifier)) == expected
    image = render(canvas)
    for step in range(-4, 5):
        color = image.pixelColor(180 + 40*step, 100)
        assert color.alpha() == (255 if step in [0, *expected] else 0)
    assert np.count_nonzero(pixels(image)[:, 3::4]) == (count+1)*np.count_nonzero(before[:, 3::4])
    current = obj.to_dict()
    current["modifier_ids"] = original["modifier_ids"]
    assert current == original
    assert len(chapter.objects) == 1


def test_roundtrip_validation_and_chapter_persistence(scene):
    canvas, chapter, page, obj = scene
    modifier = attach(scene, count=7, repeat_type="center", angle_offset=-17.5, scale_offset=10.5)
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.modifiers[modifier.modifier_id].to_dict() == modifier.to_dict()
    assert modifier_from_dict(modifier.to_dict()).to_dict() == modifier.to_dict()
    for attribute in ("count", "angle_offset", "scale_offset", "intensity"):
        bad = modifier.to_dict()
        bad[attribute] = float("nan")
        with pytest.raises(ValueError, match="finite"):
            modifier_from_dict(bad)
    with pytest.raises(ValueError, match="repeat type"):
        modifier_from_dict({"type": "array", "repeat_type": "unknown"})


def test_cumulative_rotation_scale_and_independent_pivot():
    modifier = ArrayModifier(center=(10, 20), axis_start=(0, 0), axis_end=(40, 10),
                             angle_offset=90, scale_offset=100)
    point = QPointF(12, 20)
    assert array_transform(modifier, 0).map(point).toTuple() == pytest.approx((12, 20))
    assert array_transform(modifier, 1).map(point).toTuple() == pytest.approx((50, 34))
    assert array_transform(modifier, 2).map(point).toTuple() == pytest.approx((82, 40))
    assert array_transform(modifier, -1).map(point).toTuple() == pytest.approx((-30, 9))
    # The translated pivot stays on a straight line at every iteration.
    for step in (-2, -1, 1, 2, 3):
        assert array_transform(modifier, step).map(QPointF(*modifier.center)).toTuple() == pytest.approx((10+40*step, 20+10*step))


def test_diagonal_axis_rotation_and_scale_pixels(scene):
    canvas, chapter, page, obj = scene
    modifier = attach(scene, count=2, angle_offset=90, scale_offset=100)
    modifier.axis_end = (220, 180)
    image = render(canvas)
    assert image.pixelColor(180, 100) == QColor("red")
    assert image.pixelColor(220, 140) == QColor("red")
    assert image.pixelColor(260, 180) == QColor("red")
    assert image.pixelColor(278, 180) == QColor("red")
    assert image.pixelColor(231, 140).alpha() == 0


def test_mask_intensity_mute_and_source_cache(scene):
    canvas, chapter, page, obj = scene
    modifier = attach(scene, count=2, intensity=50)
    image = render(canvas)
    assert image.pixelColor(180, 100).alpha() == 255
    assert image.pixelColor(220, 100).alpha() == pytest.approx(128, abs=1)
    keys = set(canvas._modifier_source_cache)
    modifier.axis_end = (230, 140)
    assert render(canvas).pixelColor(230, 100).alpha() == pytest.approx(128, abs=1)
    assert set(canvas._modifier_source_cache) == keys
    mask = ToneMask(name="Empty")
    chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask_id=mask.mask_id, black_value=0, white_value=100)
    image = render(canvas)
    assert image.pixelColor(180, 100).alpha() == 255
    assert image.pixelColor(230, 100).alpha() == 0
    modifier.parameter_masks.clear()
    modifier.muted = True
    assert render(canvas).pixelColor(230, 100).alpha() == 0


def test_offscreen_source_and_prior_effects_are_not_culled(scene):
    canvas, chapter, page, obj = scene
    source = QImage(10, 10, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("red"))
    bounds = QRectF(10, 10, 10, 10)
    array = ArrayModifier(axis_end=(100, 0), count=2)
    stack = [HueSaturationLightnessModifier(hue=120), array, OutlineModifier(thickness=2)]
    full, full_bounds = render_stages(canvas, source, bounds, stack, QTransform())
    crop, crop_bounds = render_stages(canvas, source, bounds, stack, QTransform(), required=QRectF(208, 8, 14, 14))
    offset = crop_bounds.topLeft()-full_bounds.topLeft()
    expected = full.copy(int(offset.x()), int(offset.y()), crop.width(), crop.height())
    np.testing.assert_array_equal(pixels(crop), pixels(expected))
    assert crop.pixelColor(7, 7).green() > 200


@pytest.mark.parametrize("mode", ["first", "last", "center"])
def test_apply_and_undo_preserve_transformed_raster(scene, mode):
    canvas, chapter, page, obj = scene
    obj.x, obj.y = 15, 20
    obj.transform_frame = (0, 0, 480, 360)
    obj.transform_quad = [(10, 10), (470, 20), (460, 340), (20, 350)]
    modifier = attach(scene, count=2, repeat_type=mode, angle_offset=20, scale_offset=15)
    expected = pixels(render(canvas))
    transform = (obj.x, obj.y, copy.deepcopy(obj.transform_quad))
    apply_raster_modifiers(canvas, modifier.modifier_id)
    assert modifier.modifier_id not in canvas.chapter.objects[obj.object_id].modifier_ids
    assert (obj.x, obj.y, obj.transform_quad) == transform
    np.testing.assert_array_equal(pixels(render(canvas)), expected)
    canvas.command_stack.undo()
    np.testing.assert_array_equal(pixels(render(canvas)), expected)
    assert modifier.modifier_id in canvas.chapter.modifiers
    canvas.command_stack.redo()
    np.testing.assert_array_equal(pixels(render(canvas)), expected)


def test_layer_array_and_nested_transform(scene):
    canvas, chapter, page, obj = scene
    layer = chapter.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(30, 30, 20, 10))
    layer.fill_color, layer.border_width = "#FF0000FF", 0
    layer.transform_frame = (30, 30, 20, 10)
    layer.transform_quad = [(40, 40), (80, 40), (80, 60), (40, 60)]
    modifier = ArrayModifier(axis_start=(0, 0), axis_end=(70, 20), center=(60, 50), count=2)
    chapter.add_modifier(modifier, [("layer", layer.layer_id)])
    image = render(canvas)
    for x, y in ((60, 50), (130, 70), (200, 90)):
        assert image.pixelColor(x, y) == QColor("blue")


def test_asset_copies_all_rig_points_and_visual_bounds(scene):
    canvas, chapter, page, obj = scene
    modifier = attach(scene, count=3, repeat_type="center", angle_offset=15, scale_offset=5)
    manifest, tiles = extract_asset(chapter, canvas.tiles, "object", obj.object_id, "Array")
    copied = next(m for m in manifest.document.modifiers.values() if isinstance(m, ArrayModifier))
    assert manifest.visual_bounds[2] > 120
    assert copied.modifier_id != modifier.modifier_id
    _, identifier, _ = instantiate_asset(manifest, tiles, chapter, canvas.tiles, page.layer_id, 250, 230)
    placed = chapter.modifiers[chapter.objects[identifier].modifier_ids[0]]
    bx, by, bw, bh = manifest.visual_bounds
    shift = QPointF(250-bx-bw/2, 230-by-bh/2)
    for name in ("axis_start", "axis_end", "center"):
        assert getattr(placed, name) == pytest.approx((QPointF(*getattr(copied, name))+shift).toTuple())


def test_panel_gizmos_undo_cancel_and_original_transform(scene, qapp):
    canvas, chapter, page, obj = scene
    controls = ModifierControls(canvas)
    canvas.modifier_mode = True
    controls.add_modifier("array")
    modifier = chapter.modifiers[obj.modifier_ids[0]]
    assert controls.findChild(QSpinBox, "array_count").value() == 3
    spin = controls.findChild(QDoubleSpinBox, "array_scale_offset")
    spin.setValue(10)
    spin.editingFinished.emit()
    assert modifier.scale_offset == 10
    combo = controls.findChild(QComboBox, "array_repeat_type")
    combo.setCurrentIndex(combo.findData("center"))
    assert modifier.repeat_type == "center"
    original = obj.to_dict()
    canvas.scale = 1.7
    before = chapter.to_dict()
    end = canvas._array_handle_points(modifier)[1]
    assert canvas._begin_modifier_handle(end)
    destination = end + QPointF(34, -51)
    canvas._move_modifier_handle(destination)
    canvas._finish_modifier_handle()
    assert modifier.axis_end == pytest.approx((canvas.widget_to_document(destination)).toTuple())
    assert obj.to_dict() == original
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    canvas.command_stack.redo()
    modifier = canvas.chapter.modifiers[modifier.modifier_id]
    canvas.modifier_mode = True
    canvas.active_modifier_id = modifier.modifier_id
    old_center = modifier.center
    pivot = canvas._array_handle_points(modifier)[2]
    assert canvas._begin_modifier_handle(pivot)
    canvas._move_modifier_handle(pivot+QPointF(34, 17))
    assert modifier.center == pytest.approx((old_center[0]+20, old_center[1]+10))
    QTest.keyClick(canvas, Qt.Key_Escape)
    assert canvas.chapter.modifiers[modifier.modifier_id].center == old_center
    assert not canvas._modifier_handle_drag
    controls.deleteLater()


def test_bounds_and_dirty_region_include_rotated_scaled_copies(scene):
    canvas, chapter, page, obj = scene
    modifier = attach(scene, count=3, angle_offset=30, scale_offset=20)
    source = QRectF(175, 95, 10, 10)
    actual = effect_bounds(source, [modifier])
    dirty = canvas.modifier_expanded_dirty(obj.object_id, source)
    for step in (0, 1, 2, 3):
        assert actual.contains(array_transform(modifier, step).mapRect(source))
        assert dirty.contains(array_transform(modifier, step).mapRect(source))


def test_large_array_clips_to_viewport_and_rejects_oversized_bake(scene):
    canvas = scene[0]
    source = QImage(10, 10, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("red"))
    bounds = QRectF(10, 10, 10, 10)
    modifier = ArrayModifier(count=100, scale_offset=100)
    image, output = render_stages(canvas, source, bounds, [modifier], QTransform(),
                                  required=QRectF(0, 0, 480, 360))
    assert image.width() <= 480 and image.height() <= 360
    assert image.pixelColor(5, 5) == QColor("red")
    with pytest.raises(ValueError, match="too large"):
        render_stages(canvas, source, bounds, [modifier], QTransform())


def test_real_mouse_drag_and_keyboard_panel_entry(scene, qapp):
    canvas, chapter, page, obj = scene
    modifier = attach(scene)
    controls = ModifierControls(canvas)
    canvas.show()
    controls.show()
    canvas.modifier_mode = True
    canvas.active_modifier_id = modifier.modifier_id
    controls.refresh()
    qapp.processEvents()
    spin = controls.findChild(QSpinBox, "array_count")
    spin.setFocus()
    QTest.keyClick(spin, Qt.Key_A, Qt.ControlModifier)
    QTest.keyClicks(spin, "5")
    QTest.keyClick(spin, Qt.Key_Return)
    assert modifier.count == 5
    original = obj.to_dict()
    for handle, attribute in ((0, "axis_start"), (1, "axis_end"), (2, "center")):
        old = QPointF(*getattr(modifier, attribute))
        point = canvas._array_handle_points(modifier)[handle].toPoint()
        QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, point)
        assert canvas._modifier_handle_drag and "array" in canvas._modifier_handle_drag
        QTest.mouseMove(canvas, point + QPointF(20, 10).toPoint())
        QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, point + QPointF(20, 10).toPoint())
        assert getattr(modifier, attribute) == pytest.approx((old+QPointF(20, 10)).toTuple())
        assert not canvas._modifier_handle_drag
    assert obj.to_dict() == original
    canvas.hide()
    controls.close()
    controls.deleteLater()
