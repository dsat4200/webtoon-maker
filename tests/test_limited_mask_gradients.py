"""Private bounded ramps feed the same mask field as contributors and paint."""
from __future__ import annotations

import json

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QTransform
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, ColorGradientRamp,
    ColorGradientStop, LimitedMaskGradient, LineGradientField, PathNode,
    ParameterMaskBinding, SeriesDocument, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow


def gradient(shape="linear", **kwargs):
    return LimitedMaskGradient(
        gradient=ColorFillGradientObject(
            gradient_shape=shape,
            line_field=LineGradientField(BoundGeometry.path([
                PathNode(x=100, y=200), PathNode(x=300, y=200),
            ], False)),
            ramp=ColorGradientRamp(stops=[
                ColorGradientStop(position=0, color="#00FFFFFF"),
                ColorGradientStop(position=1, color="#FFFFFFFF"),
            ]),
        ), half_width=50, **kwargs,
    )


@pytest.fixture
def canvas(qapp):
    widget = CanvasWidget(EditorSettings(snap_to_grid=False))
    chapter = ChapterDocument(height=400)
    chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 500, 400))
    mask = ToneMask(saved=True, name="Focal detail")
    chapter.masks[mask.mask_id] = mask
    widget.resize(500, 400)
    widget.set_document(chapter, TileStore())
    widget.scale = 1
    widget.center_x, widget.center_y = 250, 200
    widget.set_tone_mask_mode(mask.mask_id)
    widget.set_tool(ToolKind.GRADIENT)
    yield widget
    widget._effect_jobs.cancel()
    widget.close()
    widget.deleteLater()


def field(canvas, transform=QTransform(), width=500, height=400):
    return canvas.render_tone_mask_field(canvas.active_tone_mask_id, width, height,
        transform, QRectF(0, 0, 500, 400))


def set_gradient(canvas, limited):
    mask = canvas.chapter.masks[canvas.active_tone_mask_id]
    mask.limited_gradients = [limited]
    mask.validate()
    canvas._active_mask_gradient_id = limited.gradient.object_id
    return mask


def test_linear_gradient_bounds_clip_both_axes_and_feather(canvas):
    limited = gradient(start_bound=.25, end_bound=1.25)
    set_gradient(canvas, limited)
    values = field(canvas)
    assert values[200, [140, 200, 320, 360]] == pytest.approx([0, .5, 1, 0], abs=.01)
    assert values[[140, 200, 260], 200] == pytest.approx([0, .5, 0], abs=.01)
    limited.feather = 20
    values = field(canvas)
    assert values[200, 340] == pytest.approx(.475, abs=.01)
    assert values[240, 250] == pytest.approx(.475*.7525, abs=.01)


def test_circular_inner_outer_bounds_and_rotated_export(canvas):
    limited = gradient("circular", start_bound=.2, end_bound=.75)
    set_gradient(canvas, limited)
    values = field(canvas)
    assert values[200, [100, 150, 200, 275]] == pytest.approx([0, .2525, .5025, 0], abs=.01)
    assert values[[100, 200, 300], 100] == pytest.approx([.4975, 0, .5025], abs=.01)
    rotated = field(canvas, QTransform(0, 1, -1, 0, 400, 0), 400, 500)
    np.testing.assert_allclose(rotated, np.rot90(values, -1), atol=.001)


def test_multiple_gradients_subtract_after_contributors_and_round_trip(canvas):
    first = gradient(end_bound=2)
    first.gradient.ramp.stops[0].color = "#FFFFFFFF"
    mask = set_gradient(canvas, first)
    second = gradient("circular", operation="subtract", end_bound=.5)
    second.gradient.line_field.reverse_direction = True
    mask.limited_gradients.append(second)
    values = field(canvas)
    assert values[200, [100, 150, 250, 450]] == pytest.approx([0, .25, 1, 1], abs=.01)
    assert mask.gradient is None
    assert not canvas.chapter.objects
    restored = ChapterDocument.from_dict(json.loads(json.dumps(canvas.chapter.to_dict())))
    assert restored.masks[mask.mask_id].to_dict() == mask.to_dict()
    for entry in restored.masks[mask.mask_id].limited_gradients:
        assert entry.gradient.mask_only
        assert entry.gradient.parent_layer_id == ""


@pytest.mark.parametrize("shape,key,delta", [
    ("linear", "end_bound", QPointF(50, 0)),
    ("linear", "half_width", QPointF(0, 25)),
    ("circular", "end_bound", QPointF(40, -40)),
])
def test_bound_gizmos_commit_undo_and_cancel(canvas, shape, key, delta):
    limited = gradient(shape)
    set_gradient(canvas, limited)
    start = canvas._limited_gradient_controls(limited)[key]
    before = canvas.chapter.to_dict()
    canvas._mask_gradient_press(start)
    canvas._mask_gradient_move(start+delta)
    canvas._finish_mask_gradient()
    assert getattr(canvas.active_limited_mask_gradient(), key) > getattr(LimitedMaskGradient.from_dict(before["masks"][0]["limited_gradients"][0]), key)
    after = canvas.chapter.to_dict()
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == after
    current = canvas.active_limited_mask_gradient()
    start = canvas._limited_gradient_controls(current)[key]
    canvas._mask_gradient_press(start)
    canvas._mask_gradient_move(start+delta)
    canvas._finish_mask_gradient(False)
    assert canvas.chapter.to_dict() == after


def test_mask_only_creation_multiple_selection_endpoint_edit_and_delete(canvas):
    first = canvas.add_limited_mask_gradient("linear")
    second = canvas.add_limited_mask_gradient("circular")
    assert canvas.active_mask_gradient() is second.gradient
    canvas.select_mask_gradient(first.gradient.object_id)
    assert canvas.active_limited_mask_gradient() is first
    start = QPointF(*first.gradient.line_field.geometry.nodes[-1].position)
    canvas._mask_gradient_press(start)
    canvas._mask_gradient_move(start+QPointF(20, 30))
    canvas._finish_mask_gradient()
    assert first.gradient.line_field.geometry.nodes[-1].position == (start+QPointF(20, 30)).toTuple()
    canvas.remove_active_mask_gradient()
    assert canvas.active_mask_gradient() is second.gradient
    canvas.command_stack.undo()
    assert len(canvas.chapter.masks[canvas.active_tone_mask_id].limited_gradients) == 2
    canvas.set_tone_mask_mode("")
    before = canvas.chapter.to_dict()
    assert canvas.add_limited_mask_gradient("linear") is None
    assert canvas.active_mask_gradient() is None
    assert canvas.chapter.to_dict() == before


def test_default_circular_focal_mask_reaches_detail_inside_and_large_artifacts_outside(canvas):
    limited = canvas.add_limited_mask_gradient("circular")
    limited.gradient.line_field.geometry = BoundGeometry.path([
        PathNode(x=150.5, y=200.5), PathNode(x=250.5, y=200.5),
    ], False)
    values = field(canvas)
    assert values[200, [150, 200, 350]] == pytest.approx([1, .5, 0], abs=.01)
    for black, white in ((20, 0), (8, 1)):
        binding = ParameterMaskBinding(canvas.active_tone_mask_id, black, white)
        parameter = binding.black_value + values * (binding.white_value-binding.black_value)
        assert parameter[200, [150, 350]] == pytest.approx([white, black])


def test_mask_ui_lists_gradients_and_edits_bounds_with_ramp(qapp):
    window = MainWindow()
    chapter = ChapterDocument(height=400)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 500, 400))
    mask = ToneMask(saved=True, name="Detail")
    chapter.masks[mask.mask_id] = mask
    window.series = SeriesDocument()
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", page.layer_id)
    controls = window.gradient_tools_controls
    try:
        assert controls.mask_gradients_widget.isHidden()
        window._enter_mask_mode(mask.mask_id)
        window._activate_tool(ToolKind.GRADIENT)
        assert not controls.mask_gradients_widget.isHidden()
        controls.add_limited_circular.click()
        limited = window.canvas.active_limited_mask_gradient()
        assert limited is not None
        assert not window.gradient_create_group.isHidden()
        assert not controls.limited_bounds_widget.isHidden()
        controls.limited_bounds_controls["end_bound"].setValue(250)
        assert limited.end_bound == 2.5
        controls._add_stop()
        assert len(limited.gradient.ramp.stops) == 3
        controls.add_limited_linear.click()
        assert controls.mask_gradient_selector.count() == 2
        controls.mask_gradient_selector.setCurrentIndex(0)
        assert window.canvas.active_limited_mask_gradient() is limited
        # Changing tools must keep the selected gradient without assuming it is displayed.
        window._activate_tool(ToolKind.RASTER_PENCIL)
        controls.refresh()
        assert controls.limited_bounds_widget.isHidden()
        window._finish_mask_mode(True)
        assert controls.mask_gradients_widget.isHidden()
    finally:
        window._dirty = False
        window.close()
        window.deleteLater()
