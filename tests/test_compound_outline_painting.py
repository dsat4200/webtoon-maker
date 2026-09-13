"""Full-resolution raster clipping preserves attributed ink and painter state."""
import copy

import numpy as np
import pytest
from scipy.ndimage import maximum_filter, minimum_filter
from PySide6.QtCore import QBuffer, QIODevice, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPdfWriter, QTransform

from comic_editor.core.models import BoundGeometry, ChapterDocument, PathNode, ShapeStyle
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import compound_outline_painting as painting, compound_strokes
from comic_editor.ui.shape_outline_compound import compound_outline


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=23200)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 23200))
    style = ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF203D65", outline_thickness=4)
    root = chapter.add_layer(page.layer_id, "Bubble", BoundGeometry.rectangle(
        150, 22707.050112752455, 420, 212.94988724754512), style=style)
    root.compound_enabled = True
    for node in root.bound.nodes:
        node.roundness, node.roundness_enabled = 106.47494362377256, True
    tail = chapter.add_layer(root.layer_id, "Tail", BoundGeometry.path([
        PathNode(x=360, y=22800, width_multiplier=10, outline_multiplier=1),
        PathNode(x=437.95839514474926, y=23085.813649243533, width_multiplier=3.2,
                 outline_multiplier=1.3566307516629306),
        PathNode(x=646.9583951447493, y=23022.813649243533, width_multiplier=.1,
                 outline_multiplier=1.5223502053141933),
    ]), layer_kind="open_shape", style=copy.deepcopy(style))
    tail.shape_style.base_thickness = 12
    hole = chapter.add_layer(root.layer_id, "Hole", BoundGeometry.circle(320, 22800, 25),
                             style=copy.deepcopy(style))
    hole.compound_operation = "subtract"
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    yield canvas, root, tail
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).reshape(
        image.height(), image.bytesPerLine()//4, 4)[:, :image.width()].copy()


def meshes(scene, tolerance):
    canvas, root, _ = scene
    canvas._clear_compound_path_cache()
    fill = canvas.layer_effective_path(root.layer_id)
    sources = canvas._compound_outline_mesh(root, fill, tolerance, sources_only=True)
    raw = compound_outline(fill, 4, sources, canvas._outline_cache, tolerance, clip=False)
    clipped = compound_outline(fill, 4, sources, canvas._outline_cache, tolerance)
    return fill, sources, raw, clipped


def render(fill, coverage, mapping, *, raster, dpr=1, opacity=1, alpha=255,
           antialias=True, tile=None):
    image = QImage(1100, 1000, QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, antialias)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.setOpacity(opacity)
    if tile is not None:
        painter.scale(1/dpr, 1/dpr)
        painter.setClipRect(tile)
    painter.setTransform(mapping)
    state = (painter.transform(), painter.clipPath(), painter.opacity(),
             painter.compositionMode(), painter.renderHints())
    if raster:
        target = painting.prepare_outline_raster(painter, fill)
        assert target is not None
        target.paint(painter, coverage, fill, QColor(31, 84, 143, alpha))
    else:
        painter.fillPath(coverage, QColor(31, 84, 143, alpha))
    assert state == (painter.transform(), painter.clipPath(), painter.opacity(),
                     painter.compositionMode(), painter.renderHints())
    painter.end()
    return image


@pytest.mark.parametrize("variant", ["stress", "hidden-reflected"])
@pytest.mark.parametrize("tolerance", [.0625, .125])
@pytest.mark.parametrize("transform,dpr", [("subpixel", 1), ("rotate", 2), ("perspective", 1)])
def test_raw_coverage_only_changes_boundary_antialiasing(scene, variant, tolerance, transform, dpr):
    _, _, tail = scene
    if variant == "stress":
        tail.bound.nodes[1].x -= 5
        tail.bound.nodes[1].y -= 5
    else:
        tail.bound.nodes[1].outline_enabled = False
        bounds = QRectF(*tail.bound.bbox())
        tail.transform_frame = tail.bound.bbox()
        tail.transform_quad = [point.toTuple() for point in (
            bounds.topRight(), bounds.topLeft(), bounds.bottomLeft(), bounds.bottomRight())]
    fill, _, raw, clipped = meshes(scene, tolerance)
    mapping = QTransform.fromTranslate(-100, -22670)
    if transform == "subpixel":
        mapping *= QTransform.fromTranslate(.375, .625)
    elif transform == "rotate":
        mapping *= QTransform().scale(.8, .8).rotate(-12).translate(0, 160)
    else:
        mapping *= QTransform(1.1, .2, .00025, -.1, .85, -.00012, 70, 0, 1)
    # Qt's projective QPainter path conversion can fill a boolean path's
    # holes despite contains() returning False. Mapping first preserves the
    # geometric reference's actual fill rule and transparent hole semantics.
    expected = pixels(render(fill, mapping.map(clipped), QTransform(),
                             raster=False, dpr=dpr))[:, :, 3]
    actual = pixels(render(fill, raw, mapping, raster=True, dpr=dpr))[:, :, 3]
    delta = abs(expected.astype(int)-actual.astype(int))
    boundary = maximum_filter(expected, size=5) != minimum_filter(expected, size=5)
    # Qt's raw winding rasterizer can differ by two alpha levels at overlap
    # joins. Larger changes must stay within two output pixels of an edge.
    assert not np.any((delta > 2) & ~boundary)
    assert not np.any((expected == 255) & (actual == 0))
    assert not np.any((expected == 0) & (actual == 255))
    # A subtracted contour remains transparent, including after reflection.
    center = mapping.map(320, 22800)
    x, y = round(center[0]*dpr), round(center[1]*dpr)
    assert not actual[y-2:y+3, x-2:x+3].any()


@pytest.mark.parametrize("dpr", [1, 2])
@pytest.mark.parametrize("antialias", [False, True])
def test_cropped_device_tiles_match_whole_render_without_seams(scene, dpr, antialias):
    fill, _, raw, _ = meshes(scene, .0625)
    mapping = QTransform.fromTranslate(-100, -22670) * QTransform().rotate(-13).translate(.3, 160.7)
    whole = pixels(render(fill, raw, mapping, raster=True, dpr=dpr,
                          alpha=103, opacity=.57, antialias=antialias))
    assembled = np.zeros_like(whole)
    for tile in (QRect(0, 0, 551, 503), QRect(551, 0, 549, 503),
                 QRect(0, 503, 551, 497), QRect(551, 503, 549, 497)):
        value = pixels(render(fill, raw, mapping, raster=True, dpr=dpr, tile=tile,
                              alpha=103, opacity=.57, antialias=antialias))
        assembled[tile.top():tile.bottom()+1, tile.left():tile.right()+1] = value[
            tile.top():tile.bottom()+1, tile.left():tile.right()+1]
    assert np.array_equal(whole, assembled)


def test_overlaps_apply_color_and_parent_opacity_once_and_preserve_holes(qapp):
    fill = QPainterPath()
    fill.addRect(10, 10, 180, 180)
    fill.addEllipse(80, 80, 30, 30)
    raw = QPainterPath()
    raw.setFillRule(Qt.WindingFill)
    raw.addRect(0, 0, 140, 200)
    raw.addRect(60, 0, 140, 200)
    actual = render(fill, raw, QTransform(), raster=True, dpr=2, alpha=103, opacity=.5)
    reference = render(fill, fill, QTransform(), raster=False, dpr=2, alpha=103, opacity=.5)
    a, b = pixels(actual), pixels(reference)
    assert np.max(abs(a.astype(int)-b.astype(int))) <= 1
    assert np.array_equal(a[30:150, 30:350], b[30:150, 30:350])
    assert actual.pixelColor(50, 50).alpha() == actual.pixelColor(150, 50).alpha()
    assert actual.pixelColor(190, 190).alpha() == 0
    assert actual.pixelColor(5, 5).alpha() == 0


def test_large_document_capture_is_bounded_by_device_and_existing_clip(qapp):
    image = QImage(400, 300, QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(2)
    painter = QPainter(image)
    painter.setClipRect(QRectF(25, 30, 10, 15))
    huge = QPainterPath()
    huge.addRect(-1e8, -1e8, 2e8, 2e8)
    target = painting.prepare_outline_raster(painter, huge)
    assert target is not None
    assert target.bounds == QRect(49, 59, 22, 32)
    painter.end()


@pytest.mark.parametrize("dpr", [1, 2])
def test_custom_window_viewport_and_transformed_clip_remain_device_aligned(qapp, dpr):
    fill = QPainterPath()
    fill.addEllipse(20, 30, 130, 90)
    fill.addEllipse(70, 60, 20, 20)
    raw = QPainterPath()
    raw.addRect(-1000, -1000, 2000, 2000)
    outputs = []
    for use_raster in (False, True):
        image = QImage(640, 520, QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setWindow(QRect(-10, -20, 220, 200))
        painter.setViewport(QRect(17, 29, 320, 260))
        painter.rotate(7)
        clip = QPainterPath()
        clip.addEllipse(5, 10, 110, 160)
        painter.setClipPath(clip)
        state = painter.window(), painter.viewport(), painter.transform(), painter.clipPath()
        if use_raster:
            target = painting.prepare_outline_raster(painter, fill)
            assert target is not None
            target.paint(painter, raw, fill, Qt.white)
        else:
            painter.fillPath(fill, Qt.white)
        assert state == (painter.window(), painter.viewport(), painter.transform(), painter.clipPath())
        painter.end()
        outputs.append(pixels(image))
    assert np.array_equal(*outputs)


@pytest.mark.parametrize("reason", ["budget", "composition", "high-depth", "pdf"])
def test_unsupported_output_uses_geometric_outline_before_raw_build(scene, monkeypatch, reason):
    canvas, root, _ = scene
    fill, sources, _, _ = meshes(scene, .125)
    buffer = QBuffer()
    if reason == "pdf":
        buffer.open(QIODevice.WriteOnly)
        device = QPdfWriter(buffer)
    else:
        format = QImage.Format_RGBA64_Premultiplied if reason == "high-depth" else QImage.Format_ARGB32_Premultiplied
        device = QImage(700, 500, format)
    painter = QPainter(device)
    painter.translate(-100, -22670)
    if reason == "budget":
        monkeypatch.setattr(painting, "MAX_RASTER_PIXELS", 1)
    if reason == "composition":
        painter.setCompositionMode(QPainter.CompositionMode_Source)
    called = []
    original = compound_strokes.compound_outline

    def checked(*args, **kwargs):
        called.append(kwargs.get("clip", True))
        return original(*args, **kwargs)

    monkeypatch.setattr(compound_strokes, "compound_outline", checked)
    compound_strokes.paint_outline(canvas, painter, root, fill, sources)
    assert called and all(called)
    painter.end()


def test_uniform_complex_bubbles_keep_cached_geometry_without_image_allocations(scene, monkeypatch):
    canvas, root, tail = scene
    for node in tail.bound.nodes:
        node.outline_multiplier = 1
    fill, sources, _, _ = meshes(scene, .125)
    assert fill.elementCount() > 128
    monkeypatch.setattr(compound_strokes, "prepare_outline_raster",
                        lambda *args: pytest.fail("Uniform outline must keep its cached path"))
    image = QImage(700, 500, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    painter.translate(-100, -22670)
    compound_strokes.paint_outline(canvas, painter, root, fill, sources)
    painter.end()
