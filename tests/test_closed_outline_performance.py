"""Complex panel outlines retain full-resolution edges without Boolean clipping."""
import numpy as np
import pytest
from scipy.ndimage import maximum_filter, minimum_filter
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import BoundGeometry, PathNode
from comic_editor.ui import compound_outline_painting as painting, shape_outline
from comic_editor.ui.shape_contours import bound_path


@pytest.fixture
def outline(qapp):
    bound = BoundGeometry.path([
        PathNode(x=-100, y=11510, roundness=15, outline_multiplier=.4),
        PathNode(x=1220, y=10980, roundness=56, outline_multiplier=3.2),
        PathNode(x=1275, y=12470, outline_multiplier=2.4),
        PathNode(x=-135, y=12245, outline_multiplier=.5),
    ], closed=True)
    return bound, bound_path(bound)


def render(outline, *, direct, scale, dpr=1, opacity=1, antialias=True,
           image_format=QImage.Format_ARGB32_Premultiplied):
    bound, fill = outline
    image = QImage(900, 700, image_format)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setOpacity(opacity)
    painter.setRenderHint(QPainter.Antialiasing, antialias)
    mapping = (QTransform.fromTranslate(-550, -11720)
               * QTransform().rotate(9).scale(scale, scale)
               * QTransform.fromTranslate(450/dpr, 350/dpr))
    painter.setTransform(mapping)
    painter.setClipPath(fill)
    before = (painter.transform(), painter.clipPath(), painter.opacity(),
              painter.compositionMode(), painter.renderHints())
    if direct:
        path = shape_outline.outline_mesh(bound, 38, fill, tolerance=.125/scale)
        painter.fillPath(path, QColor(32, 80, 150, 197))
    else:
        painting.paint_closed_shape_outline(painter, bound, 38, fill,
            QColor(32, 80, 150, 197), tolerance=.125/scale)
    assert before == (painter.transform(), painter.clipPath(), painter.opacity(),
                      painter.compositionMode(), painter.renderHints())
    painter.end()
    image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    return np.frombuffer(image.constBits(), np.uint8).reshape(700, 900, 4).copy()


@pytest.mark.parametrize("scale,dpr,opacity,antialias", [
    (.4, 1, 1., True), (.7, 1, .53, True), (1.2, 1, 1., True),
    (.4, 2, .53, True),
])
def test_complex_outline_preserves_coverage_and_parent_opacity(
        outline, scale, dpr, opacity, antialias, monkeypatch):
    expected = render(outline, direct=True, scale=scale, dpr=dpr,
                      opacity=opacity, antialias=antialias)
    monkeypatch.setattr(shape_outline, "clip_coverage", lambda *_a, **_k:
                        pytest.fail("Interactive outline used geometric Boolean clipping"))
    actual = render(outline, direct=False, scale=scale, dpr=dpr,
                    opacity=opacity, antialias=antialias)
    delta = abs(expected.astype(int) - actual.astype(int))
    alpha = expected[:, :, 3]
    boundary = maximum_filter(alpha, 5) != minimum_filter(alpha, 5)
    assert not np.any((delta.max(axis=2) > 2) & ~boundary)
    assert not np.any((alpha > 190 * opacity) & (actual[:, :, 3] == 0))


@pytest.mark.parametrize("fallback", ["memory", "high_depth", "no_antialias"])
def test_unsupported_outline_outputs_keep_exact_geometric_clipping(outline, fallback, monkeypatch):
    kwargs = {"image_format": QImage.Format_RGBA64_Premultiplied} if fallback == "high_depth" else {}
    if fallback == "no_antialias":
        kwargs["antialias"] = False
    if fallback == "memory":
        monkeypatch.setattr(painting, "MAX_RASTER_PIXELS", 1)
    expected = render(outline, direct=True, scale=.5, **kwargs)
    assert np.array_equal(expected, render(outline, direct=False, scale=.5, **kwargs))


def test_simple_outlines_keep_identical_geometric_edges(qapp):
    bound = BoundGeometry.rectangle(450, 11650, 200, 200)
    item = bound, bound_path(bound)
    assert np.array_equal(render(item, direct=True, scale=.5),
                          render(item, direct=False, scale=.5))
