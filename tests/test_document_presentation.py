"""Camera-only tile presentation, including optional real-driver checks.

The GL tests use an isolated offscreen surface and test framebuffer. They never
open an editor, load user documents, or read back in the production draw path.
Run with QT_QPA_PLATFORM=windows to exercise the installed Windows driver.
"""
from __future__ import annotations

import ctypes
import numpy as np
import pytest
from PySide6.QtCore import QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QColor, QImage, QOffscreenSurface, QOpenGLContext, QPainter,
    QPolygonF, QSurfaceFormat, QTransform,
)
from PySide6.QtOpenGL import QOpenGLFramebufferObject

from comic_editor.ui.document_presentation import (
    GpuTilePresenter, PresentedTile, _border_patches, draw_document_border,
    draw_document_tiles, tile_vertices,
)


def solid(color="red", size=16):
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    return image


def pixels(image):
    image = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.frombuffer(image.constBits(), np.uint8).reshape(
        image.height(), image.bytesPerLine()
    )[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


def test_vertices_preserve_gutter_uv_and_document_clip():
    tile = PresentedTile("one", solid(size=20), QRectF(10, 20, 16, 16), QRectF(2, 2, 16, 16))
    camera = QTransform.fromTranslate(-10, -20)
    vertices = tile_vertices(tile, camera, QSizeF(16, 16), QRectF(18, 20, 8, 16))
    np.testing.assert_allclose(vertices[0], [0, 1, .5, .1])
    np.testing.assert_allclose(vertices[-1], [1, -1, .9, .9])


def test_camera_maps_before_float_conversion_at_large_document_offsets():
    tile = PresentedTile("far", solid(), QRectF(1e9, 1e9, 16, 16))
    camera = QTransform.fromTranslate(-1e9, -1e9)
    vertices = tile_vertices(tile, camera, QSizeF(16, 16))
    np.testing.assert_array_equal(vertices[0, :2], [-1, 1])
    np.testing.assert_array_equal(vertices[-1, :2], [1, -1])


def test_geometry_reuses_positions_across_pixel_edits_and_retires_camera_changes():
    presenter = GpuTilePresenter()
    presenter.geometry_limit = 3
    camera, viewport = QTransform(), QSizeF(64, 64)
    tile = PresentedTile('a', solid('red'), QRectF(10, 20, 16, 16))
    first = presenter._prepare_geometry([tile], camera, viewport, None)[0][1]
    changed = PresentedTile('a', solid('blue'), tile.world_rect)
    assert presenter._prepare_geometry([changed], camera, viewport, None)[0][1] is first
    assert presenter.geometry_builds == presenter.geometry_hits == 1
    camera.translate(3, 8)
    actual = presenter._prepare_geometry([changed], camera, viewport, None)[0][1]
    np.testing.assert_array_equal(actual, tile_vertices(changed, camera, viewport))
    assert not np.array_equal(actual, first)
    tile.world_rect.translate(7, 0)
    clipped = QRectF(20, 20, 10, 16)
    actual = presenter._prepare_geometry([tile], camera, viewport, clipped)[0][1]
    np.testing.assert_array_equal(actual, tile_vertices(tile, camera, viewport, clipped))
    resized = PresentedTile('a', solid(size=32), tile.world_rect, QRectF(2, 2, 16, 16))
    actual = presenter._prepare_geometry([resized], camera, QSizeF(128, 64), None)[0][1]
    np.testing.assert_array_equal(actual, tile_vertices(resized, camera, QSizeF(128, 64)))
    for index in range(8):
        moved = PresentedTile(index, tile.image, QRectF(index*16, 0, 16, 16))
        presenter._prepare_geometry([moved], camera, viewport, None)
    assert len(presenter._geometry) == presenter.geometry_limit


@pytest.mark.parametrize('angle', [0., 17.25])
def test_geometry_reuses_alternating_exact_and_feedback_clips_without_changing_vertices(angle):
    presenter = GpuTilePresenter()
    camera, viewport = QTransform.fromTranslate(9.25, 7.75), QSizeF(64, 64)
    camera.rotate(angle)
    tile = PresentedTile('edge', solid(size=20), QRectF(-3.25, 4.5, 16, 16), QRectF(2, 2, 16, 16))
    clips = [None, QRectF(0.25, 0.5, 48, 48)]
    first = [presenter._prepare_geometry([tile], camera, viewport, clip)[0][1] for clip in clips]
    assert not np.array_equal(*first)
    for _ in range(4):
        for index, clip in enumerate(clips):
            actual = presenter._prepare_geometry([tile], camera, viewport, clip)[0][1]
            assert actual is first[index]
            np.testing.assert_array_equal(actual, tile_vertices(tile, camera, viewport, clip))
    assert presenter.geometry_builds == 2 and presenter.geometry_hits == 8


def test_raster_fallback_uses_gutters_and_restores_painter():
    tile_image = solid("green", 20)
    painter = QPainter(tile_image)
    painter.fillRect(2, 2, 16, 16, QColor("red"))
    painter.end()
    tile = PresentedTile("one", tile_image, QRectF(20, 20, 16, 16), QRectF(2, 2, 16, 16))
    target = solid("black", 32)
    painter = QPainter(target)
    initial = QTransform.fromTranslate(7, 9)
    painter.setTransform(initial)
    stats = draw_document_tiles(painter, [tile], QTransform.fromTranslate(-20, -20),
                                QSizeF(32, 32), smooth=False, clip_world=QRectF(28, 20, 8, 16))
    assert painter.transform() == initial
    painter.end()
    assert stats.backend == "raster"
    assert target.pixelColor(7, 8) == QColor("black")
    assert target.pixelColor(8, 8) == QColor("red")
    assert target.pixelColor(15, 15) == QColor("red")
    assert target.pixelColor(16, 15) == QColor("black")


@pytest.mark.parametrize("ratio", [1., 1.25, 2.])
@pytest.mark.parametrize("angle", [0., 17., -33.])
def test_border_patches_match_full_raster_coverage_without_overlapping_corners(ratio, angle):
    size = QSizeF(384, 256)
    chapter = QRectF(20, 20, 260, 170)
    camera = QTransform.fromTranslate(55, 28)
    camera.rotate(angle)
    images = []
    for patched in (False, True):
        image = QImage(round(size.width() * ratio), round(size.height() * ratio),
                       QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(QColor("#242428"))
        painter = QPainter(image)
        if patched:
            patches = _border_patches(camera.map(QPolygonF(chapter)), size, ratio)
            for position, patch in patches:
                painter.drawImage(position, patch)
        else:
            draw_document_border(painter, chapter, camera, size)
        painter.end()
        images.append(pixels(image))
    np.testing.assert_allclose(images[0], images[1], atol=1)
    assert sum(image.sizeInBytes() for _, image in patches) < images[0].nbytes


@pytest.fixture
def gl_context(qapp):
    surface_format = QSurfaceFormat()
    surface_format.setVersion(3, 3)
    surface_format.setProfile(QSurfaceFormat.CoreProfile)
    context = QOpenGLContext()
    context.setFormat(surface_format)
    if not context.create():
        pytest.skip("No OpenGL context on this Qt platform")
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    if not surface.isValid() or not context.makeCurrent(surface):
        pytest.skip("No offscreen GL surface on this Qt platform")
    presenter = GpuTilePresenter()
    presenter._initialize()
    yield presenter
    presenter.close()
    context.doneCurrent()
    surface.destroy()


def framebuffer(presenter, width=64, height=64, color=(0., 0., 0., 0.)):
    target = QOpenGLFramebufferObject(width, height)
    assert target.isValid() and target.bind()
    presenter.functions.glClearColor(*color)
    presenter.functions.glClear(0x4000)
    return target


def test_gpu_camera_changes_reuse_uploads_and_preserve_premultiplied_alpha(gl_context):
    presenter = gl_context
    image = solid(QColor(200, 40, 80, 128), 16)
    tile = PresentedTile(("tile", 1), image, QRectF(10, 10, 16, 16))
    target = framebuffer(presenter)
    assert presenter.draw([tile], QTransform(), QSizeF(64, 64), smooth=False)
    first = pixels(target.toImage())
    np.testing.assert_allclose(first[12, 12], [100, 20, 40, 128], atol=1)
    assert not first[0, 0].any()
    assert presenter.uploads == 1
    assert presenter.geometry_uploads == 1
    camera = QTransform.fromTranslate(16, 4)
    camera.rotate(5)
    camera.scale(1.3, 1.3)
    assert presenter.draw([tile], camera, QSizeF(64, 64))
    assert presenter.uploads == 1
    assert presenter.geometry_uploads == 2
    changed = image.copy()
    changed.fill(QColor("blue"))
    assert presenter.draw([PresentedTile(tile.key, changed, tile.world_rect)],
                          camera, QSizeF(64, 64))
    assert presenter.uploads == 2
    assert presenter.geometry_uploads == 2
    target.release()


def test_gpu_repeated_frame_keeps_vertices_filters_and_context_limits(gl_context, monkeypatch):
    presenter = gl_context
    tile = PresentedTile('a', solid(), QRectF(0, 0, 16, 16))
    target = framebuffer(presenter)
    assert presenter.draw([tile], QTransform(), QSizeF(64,64), smooth=False)
    expected = pixels(target.toImage())
    entry = next(iter(presenter._textures.values()))
    filters = []
    set_filters = entry.texture.setMinMagFilters
    monkeypatch.setattr(entry.texture, 'setMinMagFilters', lambda *values: (filters.append(values), set_filters(*values))[1])
    get_integer = presenter.functions.glGetIntegerv
    queries = []
    def query(name):
        queries.append(name)
        return get_integer(name)
    monkeypatch.setattr(presenter.functions, 'glGetIntegerv', query)
    for _ in range(3):
        assert presenter.draw([tile], QTransform(), QSizeF(64,64), smooth=False)
        np.testing.assert_array_equal(pixels(target.toImage()), expected)
    assert presenter.geometry_uploads == 1 and not filters
    assert 0x0D33 not in queries
    assert presenter.draw([tile], QTransform(), QSizeF(64,64), smooth=True)
    assert len(filters) == 1 and presenter.geometry_uploads == 1
    assert presenter.draw([tile], QTransform(), QSizeF(64,64), smooth=False)
    assert len(filters) == 2
    target.release()


@pytest.mark.parametrize('angle', [0., 17.25])
def test_gpu_alternating_document_clips_reuse_geometry_and_keep_identical_frames(gl_context, angle):
    presenter = gl_context
    target = framebuffer(presenter)
    camera, viewport = QTransform.fromTranslate(9.25, 7.75), QSizeF(64, 64)
    camera.rotate(angle)
    tile = PresentedTile('edge', solid(QColor(231, 59, 93, 127), 20),
                         QRectF(-3.25, 4.5, 16, 16), QRectF(2, 2, 16, 16))
    clips = [None, QRectF(0.25, 0.5, 48, 48)]
    expected = []
    for _ in range(4):
        for index, clip in enumerate(clips):
            presenter.functions.glClear(0x4000)
            assert presenter.draw([tile], camera, viewport, clip_world=clip)
            actual = pixels(target.toImage())
            if len(expected) < 2:
                expected.append(actual)
            else:
                np.testing.assert_array_equal(actual, expected[index])
    assert not np.array_equal(*expected)
    assert presenter.geometry_builds == 2 and presenter.geometry_hits == 6
    assert presenter.uploads == 1
    target.release()


def test_gpu_budget_is_bounded_and_over_budget_frame_draws_nothing(gl_context):
    presenter = gl_context
    presenter.byte_limit = 16 * 16 * 4
    target = framebuffer(presenter)
    one = PresentedTile("a", solid("red"), QRectF(0, 0, 16, 16))
    two = PresentedTile("b", solid("blue"), QRectF(16, 0, 16, 16))
    assert presenter.draw([one], QTransform(), QSizeF(64, 64))
    assert presenter.draw([two], QTransform(), QSizeF(64, 64))
    assert presenter.texture_bytes == presenter.byte_limit
    before = pixels(target.toImage())
    assert not presenter.draw([one, two], QTransform(), QSizeF(64, 64))
    np.testing.assert_array_equal(before, pixels(target.toImage()))
    target.release()


def test_gpu_edit_retires_only_obsolete_revisions_of_its_logical_tile(gl_context):
    presenter = gl_context
    target = framebuffer(presenter)
    neighbor = PresentedTile("neighbor", solid("blue"), QRectF(16, 0, 16, 16))
    for revision in range(8):
        edited = PresentedTile("edited", solid(QColor(revision * 30, 0, 0)),
                               QRectF(0, 0, 16, 16))
        assert presenter.draw([edited, neighbor], QTransform(), QSizeF(64, 64))
        assert presenter.texture_bytes == 2 * 16 * 16 * 4
        assert len(presenter._textures) == 2
        assert presenter.uploads == revision + 2
        actual = pixels(target.toImage())
        np.testing.assert_array_equal(actual[8, 8], [revision * 30, 0, 0, 255])
        np.testing.assert_array_equal(actual[8, 24], [0, 0, 255, 255])
    target.release()


def test_gpu_restores_gl_state_and_honors_dpr(gl_context):
    presenter = gl_context
    target = framebuffer(presenter, 64, 64)
    gl = presenter.functions
    gl.glViewport(1, 2, 31, 27)
    gl.glEnable(0x0C11)
    gl.glScissor(1, 1, 4, 4)
    gl.glDisable(0x0BE2)
    gl.glActiveTexture(0x84C3)
    assert presenter.draw([PresentedTile("a", solid(), QRectF(0, 0, 16, 16))],
                          QTransform(), QSizeF(32, 32), device_pixel_ratio=2)
    assert presenter._integer_vector(0x0BA2, 4) == [1, 2, 31, 27]
    assert gl.glGetIntegerv(0x84E0) == 0x84C3
    assert gl.glIsEnabled(0x0C11)
    assert not gl.glIsEnabled(0x0BE2)
    actual = pixels(target.toImage())
    assert actual[30, 30, 0] == 255
    assert not actual[33, 33].any()
    gl.glDisable(0x0C11)
    target.release()


def test_gpu_adjacent_tiles_sample_gutters_without_seams(gl_context):
    presenter = gl_context
    # Both guttered images sample the same color across their shared boundary.
    tiles = [PresentedTile(index, solid(QColor(90, 130, 210, 173), 18),
                           QRectF(index * 16, 0, 16, 16), QRectF(1, 1, 16, 16))
             for index in range(2)]
    target = framebuffer(presenter)
    camera = QTransform.fromTranslate(10.4, 12.7)
    camera.rotate(12)
    assert presenter.draw(tiles, camera, QSizeF(64, 64))
    actual = pixels(target.toImage())
    # Sample on both sides of the transformed shared boundary, away from edges.
    for x in (14, 15, 16, 17):
        point = camera.map(QRectF(x, 8, 0, 0).topLeft())
        np.testing.assert_allclose(actual[int(point.y()), int(point.x())],
                                   [61, 88, 142, 173], atol=1)
    target.release()


def test_image_painter_with_widget_owner_never_touches_current_gl_framebuffer(gl_context):
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    # Captures can run inside a GPU widget's paintEvent while its GL context is
    # current. The painter's actual device, rather than the supplied owner or
    # ambient context, must determine whether native presentation is safe.
    owner = QOpenGLWidget()
    target = framebuffer(gl_context, color=(0., 0., 1., 1.))
    before = pixels(target.toImage())
    image = solid("black", 32)
    painter = QPainter(image)
    try:
        stats = draw_document_tiles(
            painter, [PresentedTile("one", solid("red"), QRectF(4, 4, 16, 16))],
            QTransform(), QSizeF(32, 32), owner=owner,
        )
    finally:
        painter.end()
    assert stats.backend == "raster" and stats.uploads == 0
    assert not hasattr(owner, "_document_tile_presenter")
    assert image.pixelColor(8, 8) == QColor("red")
    np.testing.assert_array_equal(before, pixels(target.toImage()))
    target.release()
    owner.deleteLater()


def test_presenter_cleanup_tolerates_already_deleted_context(qapp):
    from shiboken6 import delete

    presenter = GpuTilePresenter()
    context = QOpenGLContext()
    presenter.context = context
    delete(context)
    presenter.close()
    assert presenter.context is None
    assert presenter.texture_bytes == 0


@pytest.mark.parametrize("angle", [0., 17., -33.])
def test_native_border_matches_raster_and_reuses_camera_bound_cache(gl_context, qapp, angle):
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    class BorderCanvas(QOpenGLWidget):
        def __init__(self):
            super().__init__()
            self.setAttribute(Qt.WA_DontShowOnScreen)
            self.resize(384, 256)
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.CoreProfile)
            self.setFormat(fmt)
            self.chapter = QRectF(20, 20, 260, 170)
            self.camera = QTransform.fromTranslate(55, 28)
            self.camera.rotate(angle)

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("#242428"))
            draw_document_border(painter, self.chapter, self.camera, QSizeF(self.size()), owner=self)
            painter.end()
            ratio = self.devicePixelRatioF()
            width, height = round(self.width() * ratio), round(self.height() * ratio)
            output = np.zeros((height, width, 4), dtype=np.uint8)
            convention = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
            read = convention(None, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(
                self.context().getProcAddress(b"glReadPixels")
            )
            read(0, 0, width, height, 0x1908, 0x1401, output.ctypes.data)
            self.frame_pixels = output[::-1].copy()

    canvas = BorderCanvas()
    try:
        canvas.show()
        qapp.processEvents()
        ratio = canvas.devicePixelRatioF()
        expected = QImage(round(canvas.width() * ratio), round(canvas.height() * ratio),
                          QImage.Format_ARGB32_Premultiplied)
        expected.setDevicePixelRatio(ratio)
        expected.fill(QColor("#242428"))
        painter = QPainter(expected)
        draw_document_border(painter, canvas.chapter, canvas.camera, QSizeF(canvas.size()))
        painter.end()
        np.testing.assert_allclose(canvas.frame_pixels, pixels(expected), atol=1)
        cached = canvas._document_border_cache
        canvas.update()
        qapp.processEvents()
        assert canvas._document_border_cache is cached
        canvas.camera.translate(2, 3)
        canvas.update()
        qapp.processEvents()
        assert canvas._document_border_cache is not cached
    finally:
        canvas.close()
        canvas.deleteLater()
        qapp.processEvents()


def test_hidden_widget_native_presentation_keeps_qpainter_overlays_working(gl_context, qapp):
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    class IsolatedCanvas(QOpenGLWidget):
        def __init__(self):
            super().__init__()
            self.setAttribute(Qt.WA_DontShowOnScreen)
            self.resize(64, 64)
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.CoreProfile)
            self.setFormat(fmt)
            self.image = solid(QColor(255, 0, 0, 128), 16)
            self.stats = None

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor("blue"))
            self.stats = draw_document_tiles(
                painter, [PresentedTile("one", self.image, QRectF(8, 8, 16, 16))],
                QTransform(), QSizeF(self.size()), owner=self,
            )
            painter.fillRect(12, 12, 4, 4, QColor("green"))
            painter.end()
            # QOpenGLWidget.grabFramebuffer invokes paintGL, whereas this editor
            # uses a QPainter paintEvent. Read the test frame before Qt replaces
            # it; production presentation never performs this readback.
            size = round(self.width() * self.devicePixelRatioF())
            self.frame_pixels = np.zeros((size, size, 4), dtype=np.uint8)
            convention = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
            read = convention(None, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(
                self.context().getProcAddress(b"glReadPixels")
            )
            read(0, 0, size, size, 0x1908, 0x1401, self.frame_pixels.ctypes.data)
            self.frame_pixels = self.frame_pixels[::-1].copy()

    widget = IsolatedCanvas()
    try:
        widget.show()
        qapp.processEvents()
        assert widget.stats is not None and widget.stats.backend == "gpu"
        scale = widget.devicePixelRatioF()
        np.testing.assert_allclose(widget.frame_pixels[round(10 * scale), round(10 * scale)],
                                   [128, 0, 127, 255], atol=1)
        np.testing.assert_array_equal(widget.frame_pixels[round(13 * scale), round(13 * scale)],
                                      [0, 128, 0, 255])
        assert widget._document_tile_presenter.uploads == 1
        widget.update()
        qapp.processEvents()
        assert widget._document_tile_presenter.uploads == 1
    finally:
        widget._document_tile_presenter.close()
        widget.close()
        widget.deleteLater()
        qapp.processEvents()
