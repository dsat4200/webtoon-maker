from __future__ import annotations

import copy
import math

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, ColorGradientRamp,
    ColorGradientStop, LineGradientField, PathNode, RadialGradientField, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def gradient_canvas(qapp):
    chapter = ChapterDocument(height=900)
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 200, 200))
    page.fill_color, page.border_width = None, 0
    obj = ColorFillGradientObject(
        gradient_shape="circular",
        line_field=LineGradientField(BoundGeometry.path([
            PathNode(x=100, y=100), PathNode(x=150, y=100),
        ])),
        ramp=ColorGradientRamp(stops=[
            ColorGradientStop(position=0, color="#FFFF0000"),
            ColorGradientStop(position=1, color="#FF0000FF"),
        ]),
    )
    chapter.add_object(page.layer_id, obj)
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    yield canvas, obj
    canvas.close()
    canvas.deleteLater()


def _image(canvas, obj, bounds=QRectF(0, 0, 200, 200)):
    result = canvas._line_gradient_image(
        obj, canvas.bound_path(obj.line_field.geometry), bounds,
    )
    assert result is not None
    return result[0]


def _sample(image, x, y):
    return image.pixelColor(int(x * image.width() / 200), int(y * image.height() / 200))


def _pixels(image):
    return bytes(image.constBits())


def test_circular_center_radius_outside_and_reverse(gradient_canvas):
    canvas, obj = gradient_canvas
    image = _image(canvas, obj)
    assert _sample(image, 100, 100).red() > 250
    for point in [(150, 100), (100, 150), (50, 100), (100, 50), (190, 190)]:
        assert _sample(image, *point).blue() > 250
    middle = _sample(image, 125, 100)
    assert middle.red() == pytest.approx(127, abs=2)
    assert middle.blue() == pytest.approx(128, abs=2)
    obj.line_field.reverse_direction = True
    reversed_image = _image(canvas, obj)
    assert _sample(reversed_image, 100, 100).blue() > 250
    assert _sample(reversed_image, 190, 190).red() == 255


def test_circular_alpha_hard_stops_and_parent_clip(gradient_canvas):
    canvas, obj = gradient_canvas
    obj.ramp = ColorGradientRamp(stops=[
        ColorGradientStop(position=0, color="#00FF0000"),
        ColorGradientStop(position=.5, color="#00FF0000"),
        ColorGradientStop(position=.5, color="#800000FF"),
        ColorGradientStop(position=1, color="#800000FF"),
    ])
    image = _image(canvas, obj)
    assert _sample(image, 124, 100).alpha() == 0
    assert _sample(image, 126, 100).alpha() == 128
    assert _sample(image, 126, 100).blue() == 255
    rendered = QImage(canvas.chapter.width, canvas.chapter.height,
                      QImage.Format.Format_ARGB32_Premultiplied)
    canvas.render_preview(rendered)
    assert rendered.pixelColor(190, 190).alpha() == 128
    assert rendered.pixelColor(220, 190).alpha() == 0


def test_shape_radius_and_colors_invalidate_only_dependent_cache(gradient_canvas, monkeypatch):
    canvas, obj = gradient_canvas
    calls = []
    original = canvas._gradient_ramp_lut
    monkeypatch.setattr(canvas, "_gradient_ramp_lut", lambda ramp, size=1024: (
        calls.append(size), original(ramp, size)
    )[1])
    first = _image(canvas, obj)
    assert _image(canvas, obj).cacheKey() == first.cacheKey()
    obj.line_field.geometry.nodes[-1].x += 10
    larger = _image(canvas, obj)
    assert larger.cacheKey() != first.cacheKey()
    assert calls == [1024]
    geometry_keys = set(canvas._gradient_geometry_cache)
    scalar_keys = set(canvas._gradient_scalar_cache)
    obj.ramp.stops[0].color = "#FF00FF00"
    recolored = _image(canvas, obj)
    assert recolored.cacheKey() != larger.cacheKey()
    assert set(canvas._gradient_geometry_cache) == geometry_keys
    assert set(canvas._gradient_scalar_cache) == scalar_keys
    assert calls == [1024, 1024]
    obj.gradient_shape = "linear"
    linear = _image(canvas, obj)
    assert _pixels(linear) != _pixels(recolored)
    obj.gradient_shape = "circular"
    assert _image(canvas, obj).cacheKey() == recolored.cacheKey()


def test_circular_ignores_dormant_line_options_and_path_sampling(gradient_canvas, monkeypatch):
    canvas, obj = gradient_canvas
    monkeypatch.setattr(canvas, "_path_projection_arrays", lambda *args: pytest.fail(
        "Circular gradients must not project every sample onto a path"
    ))
    first = _image(canvas, obj)
    obj.line_field.direction_mode = "perpendicular"
    obj.line_field.perpendicular_distance = -17
    assert _image(canvas, obj).cacheKey() == first.cacheKey()
    # Radius is Euclidean and orientation-independent, even for diagonal handles.
    obj.line_field.geometry.nodes[-1].x = 130
    obj.line_field.geometry.nodes[-1].y = 140
    assert _pixels(_image(canvas, obj)) == _pixels(first)


def test_ramp_cache_reuses_preset_copies_and_is_bounded(gradient_canvas):
    canvas, obj = gradient_canvas
    first = canvas._cached_gradient_ramp_lut(obj.ramp)
    copied = copy.deepcopy(obj.ramp)
    for stop in copied.stops:
        stop.stop_id += "-copy"
    assert canvas._cached_gradient_ramp_lut(copied) is first
    for value in range(40):
        copied.stops[0].color = f"#FF{value:02X}00FF"
        canvas._cached_gradient_ramp_lut(copied)
    assert len(canvas._gradient_ramp_cache) <= 32


def test_circular_render_does_not_build_dormant_curve(gradient_canvas, monkeypatch):
    canvas, obj = gradient_canvas
    original = canvas.bound_path

    def checked(geometry, *args, **kwargs):
        assert geometry is not obj.line_field.geometry
        return original(geometry, *args, **kwargs)

    monkeypatch.setattr(canvas, "bound_path", checked)
    image = QImage(canvas.chapter.width, canvas.chapter.height,
                   QImage.Format.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    assert image.pixelColor(100, 100).red() > 250


def _mask_reference(canvas, obj, width, height, transform):
    inverse, valid = transform.inverted()
    assert valid
    x = np.arange(width, dtype=np.float32)[None, :] + .5
    y = np.arange(height, dtype=np.float32)[:, None] + .5
    divisor = inverse.m13() * x + inverse.m23() * y + inverse.m33()
    with np.errstate(divide="ignore", invalid="ignore"):
        world_x = (inverse.m11() * x + inverse.m21() * y + inverse.dx()) / divisor
        world_y = (inverse.m12() * x + inverse.m22() * y + inverse.dy()) / divisor
        first, second = obj.line_field.geometry.nodes
        dx, dy = second.x - first.x, second.y - first.y
        if obj.gradient_shape == "circular":
            scalar = np.clip(np.hypot(world_x - first.x, world_y - first.y)
                             / max(math.hypot(dx, dy), 1e-6), 0, 1)
        else:
            scalar = np.clip(((world_x - first.x) * dx + (world_y - first.y) * dy)
                             / max(dx * dx + dy * dy, 1e-12), 0, 1)
    if obj.line_field.reverse_direction:
        scalar = 1 - scalar
    lut = canvas._gradient_ramp_lut(obj.ramp)
    indices = np.rint(np.nan_to_num(scalar) * (len(lut) - 1)).astype(np.int32)
    coverage = ((world_x >= 0) & (world_x < canvas.chapter.width)
                & (world_y >= 0) & (world_y < canvas.chapter.height))
    return np.where(coverage, lut[indices, 3] / np.float32(255), 0)


@pytest.mark.parametrize("shape", ["linear", "circular"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("transform", [
    QTransform(), QTransform(0, 1, -1, 0, 400, 0),
    QTransform(1.2, .1, .0002, -.2, .8, -.0003, 37, -29, 1),
])
def test_mask_chunks_match_full_resolution_sampling_exactly(
    gradient_canvas, shape, reverse, transform,
):
    canvas, obj = gradient_canvas
    obj.gradient_shape = shape
    obj.line_field.reverse_direction = reverse
    obj.ramp.stops[0].color = "#00334455"
    obj.ramp.stops[1].color = "#FF778899"
    expected = _mask_reference(canvas, obj, 1100, 600, transform)
    actual = canvas._render_mask_gradient_field(obj, 1100, 600, transform)
    np.testing.assert_array_equal(actual, expected)


def test_mask_circular_hard_stops_and_bounded_temporaries(gradient_canvas, monkeypatch):
    canvas, obj = gradient_canvas
    obj.ramp = ColorGradientRamp(stops=[
        ColorGradientStop(position=0, color="#00000000"),
        ColorGradientStop(position=.5, color="#00000000"),
        ColorGradientStop(position=.5, color="#FFFFFFFF"),
        ColorGradientStop(position=1, color="#FFFFFFFF"),
    ])
    sizes = []
    original = np.hypot
    def counted(x, y):
        sizes.append(np.broadcast_shapes(x.shape, y.shape))
        return original(x, y)
    monkeypatch.setattr(np, "hypot", counted)
    actual = canvas._render_mask_gradient_field(obj, 1100, 600, QTransform())
    assert actual[100, 123] == 0
    assert actual[100, 126] == 1
    assert len(sizes) > 1
    assert max(math.prod(shape) for shape in sizes) <= 262144


def test_tone_mask_cache_tracks_shape_radius_and_ramp(gradient_canvas):
    canvas, obj = gradient_canvas
    obj.ramp.stops[0].color = "#00FFFFFF"
    mask = ToneMask(gradient=copy.deepcopy(obj))
    canvas.chapter.masks[mask.mask_id] = mask
    def render():
        return canvas.render_tone_mask_field(
            mask.mask_id, 200, 200, QTransform(), QRectF(0, 0, 200, 200),
        )
    circle = render()
    assert circle[150, 100] == 1
    mask.gradient.gradient_shape = "linear"
    linear = render()
    assert linear[150, 100] < .02
    mask.gradient.gradient_shape = "circular"
    np.testing.assert_array_equal(render(), circle)
    mask.gradient.line_field.geometry.nodes[-1].x = 200
    bigger = render()
    assert bigger[150, 100] == pytest.approx(.505, abs=.005)
    mask.gradient.ramp.stops[1].color = "#00FFFFFF"
    assert not render().any()


def test_degenerate_radius_and_invalid_mask_transform_are_finite(gradient_canvas):
    canvas, obj = gradient_canvas
    obj.line_field.geometry.nodes[-1].x = 100
    obj.line_field.geometry.nodes[-1].y = 100
    image = _image(canvas, obj)
    assert _sample(image, 150, 100).blue() == 255
    field = canvas._render_mask_gradient_field(obj, 200, 200, QTransform())
    assert np.isfinite(field).all()
    invalid = canvas._render_mask_gradient_field(obj, 200, 200, QTransform(0, 0, 0, 0, 0, 0))
    assert not invalid.any()


def test_legacy_radial_field_ignores_new_shape(gradient_canvas):
    canvas, obj = gradient_canvas
    obj.field_type = "radial"
    obj.radial_field = RadialGradientField(
        origin_x=100, origin_y=100, radius_x=50, radius_y=50,
    )
    before = QImage(canvas.chapter.width, canvas.chapter.height,
                    QImage.Format.Format_ARGB32_Premultiplied)
    after = QImage(before.size(), before.format())
    obj.gradient_shape = "linear"
    canvas.render_preview(before)
    obj.gradient_shape = "circular"
    canvas.render_preview(after)
    assert _pixels(before) == _pixels(after)
