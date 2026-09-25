"""Viewport locality must preserve opacity masks, captures, and pan recovery."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import BoundGeometry, ChapterDocument, ParameterMaskBinding, ToneMask
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.viewport_masking import mask_output


@pytest.fixture
def scene(qapp):
    document = ChapterDocument(height=1800)
    page = document.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 1800))
    page.fill_color, page.border_width = None, 0
    mask = ToneMask()
    document.masks[mask.mask_id] = mask
    tiles = TileStore()
    for y in range(50, 1700, 75):
        tiles.paint_dab(mask.mask_id, QPointF(400, y), 120, QColor(255, 255, 255, 170))
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.set_document(document, tiles)
    yield canvas, mask
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).copy()


@pytest.mark.parametrize("scale,angle", [(1., 0), (.25, 0), (2., 31), (.6, -18)])
def test_cropped_mask_matches_full_render_while_panning(scene, scale, angle, monkeypatch):
    canvas, mask = scene
    source = QImage(800, 1800, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(37, 105, 213, 178))
    binding = ParameterMaskBinding(mask.mask_id, .13, .83)
    calls = []
    original = canvas.render_tone_mask_field
    def field(*args, **kwargs):
        calls.append((args[1], args[2]))
        return original(*args, **kwargs)
    monkeypatch.setattr(canvas, "render_tone_mask_field", field)
    for y in (200, 650, 1250, 200):
        transform = QTransform().translate(180, 140).rotate(angle).scale(scale, scale).translate(-400, -y)
        inverse, valid = transform.inverted()
        assert valid
        visible = inverse.mapRect(QRectF(0, 0, 360, 280))
        rendered = []
        for interactive in (False, True):
            canvas._interactive_render = interactive
            output = QImage(360, 280, QImage.Format_ARGB32_Premultiplied)
            output.fill(Qt.transparent)
            painter = QPainter(output)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.setTransform(transform)
            result, bounds = mask_output(canvas, source, QRectF(0, 0, 800, 1800),
                                         QTransform(), binding, visible, painter)
            assert result is not None
            painter.drawImage(bounds.topLeft(), result)
            painter.end()
            rendered.append(pixels(output))
        np.testing.assert_array_equal(*rendered)
    if angle == 0:
        assert any(width * height < source.width() * source.height() / 2 for width, height in calls)
    else:
        assert calls == [(source.width(), source.height())]


def test_cache_invalidates_for_mask_paint_and_binding_edit(scene):
    canvas, mask = scene
    canvas._interactive_render = True
    source = QImage(800, 1800, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("red"))
    binding = ParameterMaskBinding(mask.mask_id, 0, 1)
    target = QImage(400, 400, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(target)
    args = (canvas, source, QRectF(0, 0, 800, 1800), QTransform(), binding, QRectF(200, 0, 400, 400), painter)
    first, _ = mask_output(*args)
    repeated, _ = mask_output(*args)
    assert first.cacheKey() == repeated.cacheKey()
    canvas.tiles.paint_dab(mask.mask_id, QPointF(350, 200), 180, QColor("white"))
    mask.touch()
    changed, _ = mask_output(*args)
    assert not np.array_equal(pixels(first), pixels(changed))
    binding.white_value = .5
    bound, _ = mask_output(*args)
    assert not np.array_equal(pixels(changed), pixels(bound))
    painter.end()


@pytest.mark.parametrize("capture", ["source", "mask", "projective", "export"])
def test_captures_and_projective_placement_keep_full_frame(scene, capture):
    canvas, mask = scene
    canvas._interactive_render = capture != "export"
    if capture == "source":
        canvas._render_modifier_sources.add(("layer", "capture"))
    if capture == "mask":
        canvas._rendering_mask_contributor = 1
    mapping = QTransform(1, 0, .0001, 0, 1, 0, 0, 0, 1) if capture == "projective" else QTransform()
    source = QImage(800, 1800, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("red"))
    output = QImage(50, 50, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(output)
    result, bounds = mask_output(canvas, source, QRectF(0, 0, 800, 1800), mapping,
                                 ParameterMaskBinding(mask.mask_id, 0, 1), QRectF(0, 0, 50, 50), painter)
    painter.end()
    assert result.size() == source.size()
    assert bounds == QRectF(0, 0, 800, 1800)


def test_navigator_band_culls_unrelated_artwork_and_matches_full_preview(scene, monkeypatch):
    canvas, _ = scene
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    for y in (0, 650, 1300):
        layer = canvas.chapter.add_layer(page.layer_id, str(y), BoundGeometry.rectangle(0, y, 600, 400))
        layer.fill_color, layer.border_width = "#c04783", 5
    canvas._interactive_render = True
    complete = QImage(108, 180, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(complete)
    partial = QImage(complete)
    clip = QRect(0, 64, 108, 15)
    visited = []
    original = canvas._render_layer
    def track(painter, layer, opacity, visible):
        visited.append(QRectF(visible))
        return original(painter, layer, opacity, visible)
    monkeypatch.setattr(canvas, "_render_layer", track)
    canvas.render_preview(partial, clip)
    np.testing.assert_array_equal(pixels(complete), pixels(partial))
    assert visited and max(r.height() for r in visited) < 300
