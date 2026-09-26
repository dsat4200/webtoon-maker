"""Native retained-scene integration in a widget that never appears on screen.

Run with QT_QPA_PLATFORM=windows to use the installed GL driver. Artwork is
synthetic and all framebuffer readback belongs to this verification code.
"""
import ctypes

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QSurfaceFormat
from shiboken6 import delete, isValid

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import GpuCanvasWidget


class CapturedGpuCanvas(GpuCanvasWidget):
    """Capture the actual paintEvent frame after ordinary editor painting."""

    def __init__(self):
        super().__init__(EditorSettings(canvas_renderer="gpu", grid_overlay_visible=False,
                                        snap_to_grid=False, predictive_ink=False))
        self.setAttribute(Qt.WA_DontShowOnScreen)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMinimumSize(1, 1)
        self.resize(512, 512)
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)
        self.frames = 0
        self.frame_pixels = None
        self.dirty_regions = []

    def _draw_tablet_hover(self, painter):
        # A deterministic UI overlay in the same post-presentation phase as the
        # production brush cursor; distinguish it from retained artwork pixels.
        painter.fillRect(8, 8, 16, 16, QColor("lime"))

    def paintEvent(self, event):
        super().paintEvent(event)
        self.dirty_regions.append(event.region().boundingRect())
        width = round(self.width() * self.devicePixelRatioF())
        height = round(self.height() * self.devicePixelRatioF())
        output = np.zeros((height, width, 4), dtype=np.uint8)
        convention = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        read = convention(None, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(
            self.context().getProcAddress(b"glReadPixels")
        )
        read(0, 0, width, height, 0x1908, 0x1401, output.ctypes.data)
        self.frame_pixels = output[::-1].copy()
        self.frames += 1


@pytest.fixture
def native_scene(qapp, monkeypatch):
    if QGuiApplication.platformName() in {"offscreen", "minimal"}:
        pytest.skip("Native OpenGL widget requires a desktop Qt backend")
    # The scene is entirely synthetic; external-image downloading is unrelated
    # and Windows plugin discovery can encounter unrelated installed TLS DLLs.
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda _parent: None)
    chapter = ChapterDocument(width=1024, height=1024, document_kind="asset",
                              background="#FFFFFFFF")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1024, 1024))
    page.fill_color, page.border_width = None, 0
    artwork = chapter.add_object(page.layer_id, ImageObject(pixel_width=1024, pixel_height=1024))
    source = QImage(1024, 1024, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(255, 0, 0, 128))
    painter = QPainter(source)
    painter.fillRect(400, 400, 100, 100, QColor("blue"))
    painter.end()
    images = ImageStore()
    images.put_decoded(artwork.object_id, "synthetic.png", b"", source)
    canvas = CapturedGpuCanvas()
    canvas.set_document(chapter, TileStore(), images)
    canvas.center_x = canvas.center_y = 512.
    canvas.scale = .5
    canvas.show()
    qapp.processEvents()
    if not canvas.isValid():
        canvas.close()
        canvas.deleteLater()
        pytest.skip("No valid native OpenGL widget")
    yield canvas
    if isValid(canvas):
        canvas._effect_jobs.cancel()
        presenter = getattr(canvas, "_document_tile_presenter", None)
        if presenter is not None:
            presenter.close()
        canvas.close()
        canvas.deleteLater()
    qapp.processEvents()


def assert_pixel(canvas, x, y, color):
    ratio = canvas.devicePixelRatioF()
    np.testing.assert_allclose(canvas.frame_pixels[round(y * ratio), round(x * ratio)],
                               color, atol=1)


def test_native_canvas_displays_retained_artwork_and_reuses_navigation(native_scene, qapp):
    canvas = native_scene
    assert canvas._document_presentation_stats.backend == "gpu"
    assert canvas._scene_cache.isNull(), "Native display must bypass the screen-sized CPU image"
    assert_pixel(canvas, 100, 100, [255, 127, 127, 255])
    assert_pixel(canvas, 225, 225, [0, 0, 255, 255])
    assert_pixel(canvas, 12, 12, [0, 255, 0, 255])
    presenter = canvas._document_tile_presenter
    uploads, renders = presenter.uploads, canvas._document_projection.renders
    assert uploads > 0 and renders > 0
    for center_x, center_y, scale, rotation in (
        (512., 512., 1., 0.), (490., 515., .9, 0.),
        (530., 490., .8, 12.), (512., 512., 1., -8.),
    ):
        canvas.center_x, canvas.center_y = center_x, center_y
        # Stay within the warmed native-density level on HiDPI as well. Crossing
        # into greater physical detail legitimately requires new document tiles.
        canvas.scale, canvas.rotation = scale / max(1., canvas.devicePixelRatioF()), rotation
        before = canvas.frames
        canvas.update()
        qapp.processEvents()
        assert canvas.frames > before
        assert presenter.uploads == uploads
        assert canvas._document_projection.renders == renders
        assert canvas._document_presentation_stats.uploads == 0
        point = canvas.document_to_widget(QPointF(450, 450))
        assert_pixel(canvas, point.x(), point.y(), [0, 0, 255, 255])
        assert_pixel(canvas, 12, 12, [0, 255, 0, 255])


def test_small_native_update_preserves_overlay_outside_dirty_region(native_scene, qapp):
    canvas = native_scene
    initial = canvas.frame_pixels.copy()
    before = canvas.frames
    canvas.update(QRect(420, 420, 12, 12))
    qapp.processEvents()
    assert canvas.frames > before
    assert_pixel(canvas, 12, 12, [0, 255, 0, 255])
    np.testing.assert_array_equal(canvas.frame_pixels, initial)
    assert canvas._document_presentation_stats.uploads == 0


def test_widget_destruction_releases_presenter_without_explicit_close(native_scene):
    canvas = native_scene
    presenter = canvas._document_tile_presenter
    assert presenter.texture_bytes > 0
    canvas._effect_jobs.cancel()
    delete(canvas)
    assert presenter.context is None
    assert presenter.texture_bytes == 0


def test_rotated_grid_preserves_raster_antialiasing_without_rebuilding_artwork(native_scene, qapp):
    canvas = native_scene
    canvas.rotation = 17.
    canvas.update()
    qapp.processEvents()
    base = canvas.frame_pixels.copy()
    expected = QImage(base.data, base.shape[1], base.shape[0], base.strides[0],
                      QImage.Format_RGBA8888).copy()
    expected.setDevicePixelRatio(canvas.devicePixelRatioF())
    canvas.settings.grid_overlay_visible = True
    canvas.settings.grid_size_px = 64
    canvas.settings.grid_divisions = 4
    canvas.settings.grid_opacity = .4
    reference = QPainter(expected)
    reference.setRenderHint(QPainter.Antialiasing, True)
    reference.setTransform(canvas.camera_transform())
    reference.setClipRect(QRectF(0, 0, canvas.chapter.width, canvas.chapter.height))
    canvas._draw_grid(reference, canvas.visible_document_rect())
    reference.end()
    renders = canvas._document_projection.renders
    canvas.refresh_grid_settings()
    qapp.processEvents()
    actual = canvas.frame_pixels
    comparison = np.frombuffer(expected.constBits(), np.uint8).reshape(actual.shape).copy()
    # The chapter border and cursor are above the grid in the real frame but
    # already present in the reference base. Compare chapter interior pixels.
    ratio = canvas.devicePixelRatioF()
    yy, xx = np.indices(actual.shape[:2], dtype=float)
    inverse, valid = canvas.camera_transform().inverted()
    assert valid
    x = inverse.m11() * (xx + .5) / ratio + inverse.m21() * (yy + .5) / ratio + inverse.dx()
    y = inverse.m12() * (xx + .5) / ratio + inverse.m22() * (yy + .5) / ratio + inverse.dy()
    interior = (x > 8) & (x < canvas.chapter.width - 8) & (y > 8) & (y < canvas.chapter.height - 8)
    interior[round(8 * ratio):round(24 * ratio), round(8 * ratio):round(24 * ratio)] = False
    np.testing.assert_allclose(actual[interior], comparison[interior], atol=1)
    assert canvas._document_projection.renders == renders
    image_key = canvas._projection_grid_cache[1].cacheKey()
    canvas.update()
    qapp.processEvents()
    assert canvas._projection_grid_cache[1].cacheKey() == image_key


def test_native_prediction_between_retained_phases_keeps_top_art_and_ui(native_scene, qapp, monkeypatch):
    canvas = native_scene
    top = next(iter(canvas.chapter.objects.values()))
    top.show_on_top = True
    drawing = canvas.chapter.add_object(top.parent_layer_id, RasterObject())
    canvas.set_selection("object", drawing.object_id)
    canvas.settings.predictive_ink = True
    canvas._predictive = (QPointF(350, 450), QPointF(600, 450), 30, QColor("green"))
    canvas._invalidate_scene_cache()
    canvas.update()
    qapp.processEvents()
    assert canvas._document_presentation_stats.backend == "gpu"
    assert canvas._scene_cache.isNull()
    assert_pixel(canvas, 225, 225, [0, 0, 255, 255])
    assert_pixel(canvas, 12, 12, [0, 255, 0, 255])
    presenter = canvas._document_tile_presenter
    uploads, renders = presenter.uploads, canvas._document_projection.renders
    def forbidden(*args, **kwargs):
        raise AssertionError("Native prediction must only present retained artwork")
    monkeypatch.setattr(canvas, "_render_scene_cache_rect", forbidden)
    monkeypatch.setattr(canvas, "_render_document_region", forbidden)
    for end in (570, 590, 620):
        canvas._predictive = (QPointF(350, 450), QPointF(end, 450), 30, QColor("green"))
        canvas.update()
        qapp.processEvents()
        assert_pixel(canvas, 225, 225, [0, 0, 255, 255])
        assert_pixel(canvas, 12, 12, [0, 255, 0, 255])
        assert presenter.uploads == uploads
        assert canvas._document_projection.renders == renders
        assert canvas._document_presentation_stats.uploads == 0
