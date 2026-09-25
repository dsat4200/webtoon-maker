"""Curves pickers edit input tones without painting, changing tools, or leaking undo."""
import copy

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, CurvesModifier, ImageObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.curves import apply_curves, evaluate_curve
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.curves_features import _point
from comic_editor.ui.curves_controls import CurvesControls
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_rendering import _qimage_premultiplied


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=160, height=160, background="#00000000", document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 160, 160))
    page.fill_color, page.border_width = None, 0
    obj = chapter.add_object(page.layer_id, ImageObject(x=40, y=40, pixel_width=80, pixel_height=80))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.resize(320, 320)
    canvas.set_document(chapter, TileStore(), ImageStore())
    image = QImage(80, 80, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(128, 128, 128, 180))
    canvas.images.put_decoded(obj.object_id, "fixture.png", b"", image)
    mod = CurvesModifier()
    chapter.add_modifier(mod, [("object", obj.object_id)])
    canvas.set_selection("object", obj.object_id)
    yield canvas, mod, image, obj
    canvas.cancel_curves_picker()
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def position(canvas):
    return canvas.document_to_widget(QPointF(80, 80))


def test_add_point_drag_has_one_undo_and_uses_release_coordinate(scene):
    canvas, mod, source, obj = scene
    tool, selection = canvas.tool, list(canvas.selected_entities)
    revision = canvas.command_stack.revision
    canvas.start_curves_picker(mod.modifier_id, "add_point", "rgb", "master")
    where = position(canvas)
    QTest.mousePress(canvas, Qt.LeftButton, pos=where.toPoint())
    QTest.mouseMove(canvas, (where - QPointF(0, 20)).toPoint())
    QTest.mouseRelease(canvas, Qt.LeftButton, pos=(where - QPointF(0, 40)).toPoint())
    points = canvas.chapter.modifiers[mod.modifier_id].curves["rgb:master"]
    assert len(points) == 3
    assert points[1][1] == pytest.approx(points[1][0] + .2)
    assert canvas.command_stack.revision == revision + 1
    assert canvas.tool == tool and canvas.selected_entities == selection
    assert canvas._curves_picker.state is None
    np.testing.assert_array_equal(_qimage_premultiplied(canvas.images.image(obj.object_id)), _qimage_premultiplied(source))
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[mod.modifier_id].curves == {}
    canvas.command_stack.redo()
    assert len(canvas.chapter.modifiers[mod.modifier_id].curves["rgb:master"]) == 3


@pytest.mark.parametrize("picker_mode,selected_channel", [("add_point", "master"), ("white_balance", "red")])
def test_picker_selects_its_node_in_the_live_modifier_card(scene, picker_mode, selected_channel):
    canvas, mod, _, _ = scene
    owner = ModifierControls(canvas)
    owner.refresh()
    controls = owner._cards[mod.modifier_id].findChild(CurvesControls)
    revision = canvas.command_stack.revision
    try:
        controls.picker_buttons[picker_mode].click()
        where = position(canvas)
        QTest.mousePress(canvas, Qt.LeftButton, pos=where.toPoint())
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=(where - QPointF(0, 40)).toPoint())
        points = canvas.chapter.modifiers[mod.modifier_id].curves[f"rgb:{selected_channel}"]
        assert controls.channel == selected_channel
        assert controls.graph.selected_index == 1
        assert controls.graph.points == points
        assert controls.x_value.value() == pytest.approx(points[1][0], abs=.0001)
        assert controls.y_value.value() == pytest.approx(points[1][1], abs=.0001)
        assert canvas.command_stack.revision == revision + 1
    finally:
        owner.deleteLater()


@pytest.mark.parametrize("cancel", ["escape", "tool", "selection", "chapter"])
def test_cancel_rolls_back_uncommitted_drag(scene, cancel):
    canvas, mod, _, _ = scene
    before = copy.deepcopy(mod.curves)
    revision = canvas.command_stack.revision
    canvas.start_curves_picker(mod.modifier_id, "add_point", "rgb", "red")
    picker = canvas._curves_picker
    where = position(canvas)
    picker.press(where)
    picker.move(where - QPointF(0, 40))
    assert mod.curves != before
    if cancel == "escape":
        QTest.keyClick(canvas, Qt.Key_Escape)
    elif cancel == "tool":
        canvas.toolChanged.emit(ToolKind.OBJECT_SELECT)
    elif cancel == "selection":
        canvas.set_selection_set([])
    else:
        canvas.replace_chapter(picker.state["before"])
    assert picker.state is None
    assert canvas.chapter.modifiers[mod.modifier_id].curves == before
    assert canvas.command_stack.revision == revision


@pytest.mark.parametrize("mode,expected", [("black_point", 0.), ("white_point", 1.), ("gray_point", .5)])
def test_tonal_anchor_picker_maps_the_sampled_tone(scene, mode, expected):
    canvas, mod, _, _ = scene
    canvas.start_curves_picker(mod.modifier_id, mode, "rgb", "master")
    picker = canvas._curves_picker
    picker.press(position(canvas))
    picker.release(position(canvas))
    points = canvas.chapter.modifiers[mod.modifier_id].curves["rgb:master"]
    sampled = picker.sampler.pixel(canvas, mod.modifier_id, QPointF(80, 80))[0].mean()
    assert float(evaluate_curve(points, sampled)) == pytest.approx(expected, abs=1e-5)


def test_white_balance_neutralizes_selected_pixel_without_changing_alpha(scene):
    canvas, mod, _, obj = scene
    source = QImage(80, 80, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(180, 110, 65, 180))
    canvas.images.put_decoded(obj.object_id, "cast.png", b"", source)
    canvas.start_curves_picker(mod.modifier_id, "white_balance", "rgb", "master")
    picker = canvas._curves_picker
    picker.press(position(canvas))
    picker.release(position(canvas))
    original = _qimage_premultiplied(source)
    actual = apply_curves(original, canvas.chapter.modifiers[mod.modifier_id])
    assert np.ptp(actual[40, 40, :3]) < 1 / 255
    np.testing.assert_array_equal(actual[..., 3], original[..., 3])


@pytest.mark.parametrize("color_mode", ["rgb", "gray", "lab", "cmyk"])
@pytest.mark.parametrize("picker_mode,expected", [("black_point", 0.), ("white_point", 1.)])
@pytest.mark.parametrize("color", [(180, 110, 65), (255, 80, 0)])
def test_master_black_white_anchors_neutralize_colored_samples(scene, color_mode, picker_mode, expected, color):
    canvas, mod, _, obj = scene
    source = QImage(80, 80, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(*color))
    canvas.images.put_decoded(obj.object_id, "colored.png", b"", source)
    mod.color_mode = color_mode
    canvas.start_curves_picker(mod.modifier_id, picker_mode, color_mode, "master")
    picker = canvas._curves_picker
    picker.press(position(canvas))
    picker.release(position(canvas))
    adjusted = apply_curves(_qimage_premultiplied(source), canvas.chapter.modifiers[mod.modifier_id])
    if color_mode == "gray" and expected == 1:
        # Gray changes luminance while preserving hue, so only its luminance
        # curve anchor is neutral; clipping cannot create missing RGB channels.
        assert adjusted[40, 40, :3].max() == 1.
    else:
        np.testing.assert_allclose(adjusted[40, 40, :3], expected, atol=2 / 255)


def test_alpha_picker_is_independent_of_master_and_transparent_click_is_noop(scene):
    canvas, mod, _, _ = scene
    mod.curves = {"rgb:master": [(0, 1), (1, 0)]}
    canvas.start_curves_picker(mod.modifier_id, "add_point", "rgb", "alpha")
    picker = canvas._curves_picker
    picker.press(canvas.document_to_widget(QPointF(5, 5)))
    assert picker.state["before"] is None
    assert "rgb:alpha" not in mod.curves
    picker.press(position(canvas))
    picker.release(position(canvas) - QPointF(0, 20))
    assert mod.curves["rgb:alpha"][1] == pytest.approx((180 / 255, 180 / 255 + .1))
    assert mod.curves["rgb:master"] == [(0., 1.), (1., 0.)]


def test_point_limit_and_endpoint_reuse_keep_valid_distinct_inputs():
    curves = {"rgb:master": [(x, x) for x in np.linspace(0, 1, 256)]}
    index = _point(curves, "rgb:master", .413, .8)
    assert len(curves["rgb:master"]) == 256
    assert curves["rgb:master"][index][1] == .8
    modifier = CurvesModifier(curves=curves)
    modifier.validate()
