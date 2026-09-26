"""Canvas pickup remains paintable when fresh paint density is zero."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDab, BrushDefinition, BrushDynamics, BrushInput
from comic_editor.core.brush_raster import RasterBrushStroke, image_pixels, pixels_image
from comic_editor.core.tiles import TileStore


@pytest.mark.parametrize("mode", ["blend", "running", "smear"])
@pytest.mark.parametrize("existing_alpha", [.25, 1.])
def test_wet_response_clamps_normalized_controls_after_large_tilt_factor(mode,existing_alpha):
    brush = BrushDefinition(size=16,mixing_mode=mode,paint_amount=.5,paint_density=.5)
    stroke = RasterBrushStroke(TileStore(16),"paint",brush,QColor("gray"),{})
    stroke.main.wet_pickup = np.empty((2,2,4),np.float32)
    stroke.main.wet_pickup[:] = (0,0,existing_alpha,existing_alpha)
    dab = BrushDab(8,8,16,paint_amount=3,paint_density=3)
    coordinate = np.array([[8.5]],np.float32)
    rgb = np.full((1,1,3),.5,np.float32)
    alpha = np.full((1,1),.4,np.float32)
    color,coverage = stroke._wet_pixels(stroke.main,dab,coordinate,coordinate,rgb,alpha)
    # A response above 100% saturates a normalized control. It must not turn
    # gray fresh paint into yellow by subtracting blue, or create alpha > .4.
    np.testing.assert_allclose(color,rgb,atol=1e-7)
    np.testing.assert_allclose(coverage,alpha,atol=1e-7)
    assert coverage.max() <= alpha.max()


def _patches():
    pixels = np.zeros((64, 256, 4), np.float32)
    pixels[:, :64] = (0, 0, 1, 1)
    pixels[:, 128:192] = (1, 1, 0, 1)
    return pixels


def _draw(brush, *, pixels=None, points=None, color=None, budget=None):
    pixels = _patches() if pixels is None else pixels
    store, before = TileStore(64), {}
    for x in range(pixels.shape[1]//64):
        store.set_tile("paint", (x, 0), pixels_image(pixels[:, x*64:(x+1)*64]))
    original = {key: image.copy() for key, image in store.iter_tiles("paint")}
    stroke = RasterBrushStroke(store, "paint", brush, color or QColor("red"), before, seed=12)
    if budget is not None:
        stroke.main.budget = budget
    points = points or [BrushInput(30, 32), BrushInput(230, 32, time=1)]
    stroke.begin(points[0])
    for point in points[1:]:
        stroke.add(point)
    stroke.finish()
    result = np.zeros_like(pixels)
    for (x, y), image in store.iter_tiles("paint"):
        if y == 0 and 0 <= x < pixels.shape[1]//64:
            result[:, x*64:(x+1)*64] = image_pixels(image)
    return result, before, original, stroke


def _blender(mode="running", **changes):
    return replace(BrushDefinition(size=18, spacing=.12, mixing_mode=mode,
                                   paint_amount=0, paint_density=0, color_stretch=.7), **changes)


@pytest.mark.parametrize("mode", ["blend", "running"])
def test_zero_amount_and_density_carries_canvas_pigment_across_empty_regions(mode):
    result, before, original, _ = _draw(_blender(mode))
    assert result[32, 95, 3] > .4
    assert result[32, 95, 2] > result[32, 95, 0] + .4  # transported blue
    assert result[32, 215, 3] > .4
    assert result[32, 215, 0] > .05 and result[32, 215, 1] > .05  # second pickup
    assert len(before) > 1
    for key, image in before.items():
        assert image == original.get(key)  # exact undo baseline, not wet intermediates


@pytest.mark.parametrize("mode", ["blend", "running"])
def test_empty_canvas_does_not_invent_pigment_for_a_zero_load_blender(mode):
    result, before, _, _ = _draw(_blender(mode), pixels=np.zeros((64, 256, 4), np.float32))
    assert not result.any()
    assert not before


@pytest.mark.parametrize("amount", [0, .5, 1])
def test_zero_density_keeps_color_mixing_separate_from_the_picked_up_alpha(amount):
    result, _, _, _ = _draw(_blender(paint_amount=amount), points=[BrushInput(30, 32)])
    assert result[32, 30, 0] == pytest.approx(amount, abs=1/255)
    assert result[32, 30, 2] == pytest.approx(1-amount, abs=1/255)
    assert result[32, 30, 3] == 1


def test_partial_alpha_pickup_survives_without_becoming_a_full_opacity_dab():
    pixels = np.zeros((64, 256, 4), np.float32)
    pixels[:, :64] = (0, 0, 1, .25)
    brush = _blender(spacing=2, size=18)
    result, _, _, _ = _draw(brush, pixels=pixels,
                            points=[BrushInput(48, 32), BrushInput(84, 32, time=.2)])
    assert .05 < result[32, 84, 3] < .4
    assert result[32, 84, 2] > .99


def test_zero_density_dynamic_retains_pickup_but_removes_fresh_empty_canvas_load():
    brush = _blender(paint_amount=.5, paint_density=1,
                      dynamics={"paint_density": BrushDynamics(pressure=True)})
    zero, _, _, _ = _draw(brush, points=[BrushInput(30, 32, 0), BrushInput(230, 32, 0, time=1)])
    assert zero[32, 95, 3] > .4
    empty = np.zeros((64, 256, 4), np.float32)
    no_load, _, _, _ = _draw(brush, pixels=empty,
                              points=[BrushInput(30, 32, 0), BrushInput(230, 32, 0, time=1)])
    full_load, _, _, _ = _draw(brush, pixels=empty)
    assert not no_load.any()
    assert full_load[32, 95, 3] > .9


@pytest.mark.parametrize("disabled", ["color", "opacity", "density"])
def test_zero_coverage_remains_noop_even_when_canvas_pigment_is_loaded(disabled):
    brush = _blender(**({disabled: 0} if disabled != "color" else {}))
    color = QColor(255, 0, 0, 0) if disabled == "color" else QColor("red")
    result, before, _, _ = _draw(brush, color=color)
    np.testing.assert_array_equal(result, _patches())
    assert not before


def test_zero_paint_density_is_not_an_eraser_and_explicit_dry_erase_still_erases():
    result, _, _, _ = _draw(_blender())
    assert result[32, 30, 3] == 1
    eraser = _blender(mixing_mode="none", blending_mode="erase", opacity=.5)
    result, _, _, _ = _draw(eraser)
    assert result[32, 30, 3] == pytest.approx(.5, abs=1/255)


def test_zero_density_transport_keeps_packet_invariance_and_bounded_working_storage():
    brush = _blender()
    coarse, _, _, _ = _draw(brush)
    fine, _, _, stroke = _draw(brush,
        points=[BrushInput(x, 32, time=(x-30)/200) for x in range(30, 231)],
        budget=64*64*20)
    np.testing.assert_array_equal(coarse, fine)
    assert stroke.main.wet_pickup is None
