"""Ribbon topology and source registration, independent of CSP pixel parity."""
import base64
import io
import math
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDefinition, BrushInput, BrushTip
from comic_editor.core.brush_raster import RasterBrushStroke, image_pixels
from comic_editor.core.tiles import TileStore


def _brush(pixels, **settings):
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, "PNG")
    height, width = pixels.shape[:2]
    tip = BrushTip("Registered rectangle", width, height,
                   base64.b64encode(stream.getvalue()).decode(), shape="image")
    return BrushDefinition(size=32, tips=(tip,), ribbon=True,
                           antialiasing=0, **settings)


def _render(brush, points=((32, 64), (224, 64))):
    store = TileStore(32)
    stroke = RasterBrushStroke(store, "paint", brush, QColor("black"), {},
                               defer_flush=True)
    stroke.begin(BrushInput(*points[0]))
    for index, point in enumerate(points[1:], 1):
        stroke.add(BrushInput(*point, time=index*.02))
    stroke.finish()
    pixels = np.zeros((128, 256, 4), np.float32)
    for (x, y), tile in store.iter_tiles("paint"):
        if 0 <= x < 8 and 0 <= y < 4:
            pixels[y*32:(y+1)*32, x*32:(x+1)*32] = image_pixels(tile)
    return pixels


def test_cardinal_ribbon_keeps_registered_side_and_longitudinal_padding():
    pixels = np.zeros((32, 32, 4), np.uint8)
    pixels[4:28, 8:24] = (0, 0, 0, 255)
    alpha = _render(_brush(pixels, angle=0))[..., 3]
    # Registered transparent sides stay transparent along the entire strip.
    assert alpha[44:56, 48:216].max() == 0
    assert alpha[72:84, 48:216].max() == 0
    # Blank leading/trailing rows stay blank at every repetition; they are
    # neither cropped nor filled just to make an opaque-looking ribbon.
    assert alpha[63:65, 62:66].max() == 0
    assert alpha[63:65, 40:56].min() == 1


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_cardinal_ribbons_preserve_long_thin_registered_aspect(angle):
    pixels = np.zeros((32, 8, 4), np.uint8)
    pixels[:, 2:6] = (0, 0, 0, 255)
    if angle % 180:
        pixels = np.rot90(pixels).copy()
    alpha = _render(_brush(pixels, angle=angle))[..., 3]
    assert alpha[63:65, 40:216].min() == 1
    assert np.count_nonzero(alpha[:, 128]) == 4


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("antialiasing", [0, 2])
def test_cardinal_ribbon_pixels_match_previous_registered_rotation(monkeypatch, angle, antialiasing):
    pixels = np.zeros((24, 12, 4), np.uint8)
    pixels[2:22, 2:10] = (0, 0, 0, 255)
    pixels[8:16, 5:8, 3] = 83
    brush = replace(_brush(pixels, angle=angle), antialiasing=antialiasing)
    actual = _render(brush)

    def old_coordinates(u, v, width, height, angle):
        rotation = math.radians(angle)
        c, s = math.cos(rotation), math.sin(rotation)
        rotated_width = width*abs(c)+height*abs(s)
        rotated_height = width*abs(s)+height*abs(c)
        xx, yy = (u-.5)*rotated_width, (v-.5)*rotated_height
        return (c*xx+s*yy)/width+.5, (-s*xx+c*yy)/height+.5

    monkeypatch.setattr(RasterBrushStroke, "_ribbon_coordinates", staticmethod(old_coordinates))
    np.testing.assert_array_equal(actual, _render(brush))


def test_slanted_repeat_once_stops_after_one_registered_image():
    pixels = np.full((32, 32, 4), 255, np.uint8)
    alpha = _render(_brush(pixels, angle=26, repeat_mode="once"))[..., 3]
    assert alpha[60:68, 42:56].min() == 1
    assert alpha[:, 76:].max() == 0


def test_angle_crossing_forty_five_does_not_switch_axes_or_tip_count():
    pixels = np.zeros((24, 32, 4), np.uint8)
    pixels[3:21, 4:28] = (0, 0, 0, 255)
    pixels[9:15, 4:16, 3] = 0
    brush = _brush(pixels, angle=44.9, repeat_mode="once")
    brush = replace(brush, antialiasing=2)
    before = _render(brush)
    after = _render(replace(brush, angle=45.1))
    assert np.mean(np.abs(before-after)) < .001
    assert before[:, 80:, 3].max() == after[:, 80:, 3].max() == 0


def test_slanted_curved_strip_matches_input_subdivision():
    pixels = np.full((32, 32, 4), 255, np.uint8)
    brush = _brush(pixels, angle=37.1)
    coarse = ((32, 64), (96, 48), (160, 80), (224, 64))
    fine = []
    for start, end in zip(coarse, coarse[1:]):
        for part in range(8):
            fine.append(tuple(a+(b-a)*part/8 for a, b in zip(start, end)))
    fine.append(coarse[-1])
    np.testing.assert_array_equal(_render(brush, coarse), _render(brush, fine))
