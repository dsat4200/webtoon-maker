"""Finished outline pixels are reused only when every pixel input is unchanged."""
from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QTransform

from comic_editor.core.models import BoundGeometry, PathNode
from comic_editor.ui import compound_outline_painting as painting
from comic_editor.ui.shape_contours import bound_path
from comic_editor.ui.shape_outline import OutlineCache, _size


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).reshape(
        image.height(), image.bytesPerLine()).copy()


def paths():
    fill = QPainterPath()
    fill.addEllipse(20, 25, 160, 150)
    fill.addEllipse(70, 80, 30, 40)
    coverage = QPainterPath()
    coverage.setFillRule(Qt.WindingFill)
    coverage.addRect(0, 0, 130, 220)
    coverage.addRect(60, 0, 140, 220)
    return fill, coverage


def render(cache, *, fill=None, coverage=None, color=None, mapping=None, clip=None,
           opacity=1., ratio=1., size=(260, 240), antialias=True):
    default_fill, default_coverage = paths()
    fill = default_fill if fill is None else fill
    coverage = default_coverage if coverage is None else coverage
    image = QImage(*size, QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setOpacity(opacity)
    painter.setRenderHint(QPainter.Antialiasing, antialias)
    painter.setTransform(QTransform() if mapping is None else mapping)
    if clip is not None:
        painter.setClipPath(clip)
    target = painting.prepare_outline_raster(painter, fill, cache=cache)
    assert target is not None and target.image.isNull() and target.mask.isNull()
    state = painter.transform(), painter.clipPath(), painter.opacity(), painter.renderHints()
    assert target.paint(painter, coverage, fill, QColor(31, 84, 143, 153) if color is None else color)
    assert state == (painter.transform(), painter.clipPath(), painter.opacity(), painter.renderHints())
    painter.end()
    return image


@pytest.mark.parametrize("ratio,mapping", [
    (1., QTransform()), (1.5, QTransform().translate(.375, .625)),
    (2., QTransform().rotate(12).scale(.7, .7)),
    (1., QTransform(1.1, .2, .00025, -.1, .85, -.00012, 15, 0, 1)),
])
def test_exact_raster_cache_hit_skips_all_scratch_painting(qapp, monkeypatch, ratio, mapping):
    cache = OutlineCache()
    expected = render(None, ratio=ratio, mapping=mapping)
    first = render(cache, ratio=ratio, mapping=mapping)
    monkeypatch.setattr(painting.OutlineRaster, "_render", lambda *_a:
                        pytest.fail("A completed outline allocated or repainted scratch pixels"))
    reused = render(cache, ratio=ratio, mapping=mapping)
    np.testing.assert_array_equal(pixels(first), pixels(expected))
    np.testing.assert_array_equal(pixels(reused), pixels(expected))
    assert cache.builds["outline_raster"] == 1
    assert cache.hits["outline_raster"] == 1


@pytest.mark.parametrize("changed", ["fill", "coverage", "color", "antialias",
                                    "fractional_translation", "rotation", "scale",
                                    "perspective", "ratio", "capture_size"])
def test_pixel_dependencies_invalidate_finished_outline(qapp, changed):
    cache = OutlineCache()
    original = render(cache)
    kwargs = {}
    if changed in {"fill", "coverage"}:
        fill, coverage = paths()
        if changed == "fill":
            fill.addEllipse(115, 95, 45, 45)
            kwargs["fill"] = fill
        else:
            coverage = QPainterPath()
            coverage.addRect(0, 0, 100, 220)
            kwargs["coverage"] = coverage
    elif changed == "color":
        kwargs["color"] = QColor(140, 30, 80, 197)
    elif changed == "antialias":
        kwargs["antialias"] = False
    elif changed == "ratio":
        kwargs["ratio"] = 1.5
    elif changed == "capture_size":
        kwargs["size"] = (100, 110)
    else:
        kwargs["mapping"] = {
            "fractional_translation": QTransform.fromTranslate(.375, .625),
            "rotation": QTransform().rotate(12), "scale": QTransform.fromScale(.7, .8),
            "perspective": QTransform(1.1, .2, .00025, -.1, .85, -.00012, 15, 0, 1),
        }[changed]
    expected = render(None, **kwargs)
    actual = render(cache, **kwargs)
    np.testing.assert_array_equal(pixels(actual), pixels(expected))
    assert not np.array_equal(pixels(original), pixels(actual))
    assert cache.builds["outline_raster"] == 2


def test_parent_opacity_and_clip_shape_apply_after_cached_pixels(qapp, monkeypatch):
    cache = OutlineCache()
    rectangle, ellipse = QPainterPath(), QPainterPath()
    rectangle.addRect(30, 35, 145, 135)
    ellipse.addEllipse(30, 35, 145, 135)
    render(cache, clip=rectangle)
    expected = render(None, clip=ellipse, opacity=.41)
    monkeypatch.setattr(painting.OutlineRaster, "_render", lambda *_a:
                        pytest.fail("Outer opacity and equal-bounds clip should reuse pixels"))
    actual = render(cache, clip=ellipse, opacity=.41)
    np.testing.assert_array_equal(pixels(actual), pixels(expected))
    assert cache.hits["outline_raster"] == 1


def test_outline_cache_counts_image_memory_and_rejects_oversize_entries(qapp):
    image = QImage(100, 50, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.white)
    assert _size(image) >= 20_000
    small = OutlineCache(budget=8_000)
    assert small.get(("raster", 1), lambda: image) is image
    assert not small.entries and small.bytes == 0
    bounded = OutlineCache(budget=45_000)
    for index in range(10):
        bounded.get(("raster", index), image.copy)
        assert bounded.bytes <= bounded.budget
    assert len(bounded.entries) == 2
    assert bounded.bytes >= 40_000


@pytest.mark.parametrize("changed", ["geometry", "width", "style", "fill"])
def test_closed_outline_semantic_signature_preserves_pixel_dependencies(qapp, changed):
    bound = BoundGeometry.path([
        PathNode(x=15, y=80, roundness=15, outline_multiplier=.4),
        PathNode(x=260, y=20, roundness=25, outline_multiplier=3.2),
        PathNode(x=270, y=260, outline_multiplier=2.4),
        PathNode(x=10, y=210, outline_multiplier=.5),
    ], closed=True)
    cache = OutlineCache()

    def paint(cache, bound, width, fill):
        image = QImage(300, 300, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painting.paint_closed_shape_outline(painter, bound, width, fill,
                                             QColor(30, 60, 100, 190), cache=cache)
        painter.end()
        return pixels(image)

    initial = paint(cache, bound, 12, bound_path(bound))
    np.testing.assert_array_equal(initial, paint(cache, bound, 12, bound_path(bound)))
    assert cache.builds["outline_raster"] == 1
    revised = deepcopy(bound)
    width = 12
    if changed == "geometry":
        revised.nodes[1].x -= 35
    elif changed == "width":
        width = 18
    elif changed == "style":
        revised.nodes[1].outline_multiplier = .3
    fill = bound_path(revised)
    if changed == "fill":
        fill.addEllipse(QRectF(200, 20, 30, 30))
    expected = paint(None, revised, width, fill)
    actual = paint(cache, revised, width, fill)
    np.testing.assert_array_equal(actual, expected)
    assert cache.builds["outline_raster"] == 2
    assert not np.array_equal(actual, initial)


def test_failed_scratch_allocation_is_not_retained(qapp, monkeypatch):
    cache = OutlineCache()
    fill, coverage = paths()
    image = QImage(260, 240, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    target = painting.prepare_outline_raster(painter, fill, cache=cache)
    original = target._render
    monkeypatch.setattr(target, "_render", lambda *_a: None)
    assert not target.paint(painter, coverage, fill, Qt.white)
    assert not cache.entries
    monkeypatch.setattr(target, "_render", original)
    assert target.paint(painter, coverage, fill, Qt.white)
    assert cache.builds["outline_raster"] == 1
    painter.end()
