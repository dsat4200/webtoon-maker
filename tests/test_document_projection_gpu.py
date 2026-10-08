"""Native retained-scene integration in a widget that never appears on screen.

Run with QT_QPA_PLATFORM=windows to use the installed GL driver. Artwork is
synthetic and all framebuffer readback belongs to this verification code.
"""
import ctypes
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QOpenGLContext, QPainter, QSurfaceFormat
from shiboken6 import delete, isValid

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ImageObject, ParameterMaskBinding, RasterObject, RadialBlurModifier, ToneMask,
)
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
        self.frame_capture = None

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
        context = QOpenGLContext.currentContext()
        assert context is self.context(), 'Capture requires actual owning current GL context'
        gl = context.functions()
        assert int(gl.glGetIntegerv(0x8CAA)) == int(self.defaultFramebufferObject()), 'Capture requires owned read FBO'
        assert int(gl.glGetIntegerv(0x0C02)) == 0x8CE0, 'Capture requires actual color attachment0'
        read(0, 0, width, height, 0x1908, 0x1401, output.ctypes.data)
        self.frame_pixels = output[::-1].copy()
        self.frames += 1
        document = self._render_document_state()
        controller = self._scene_controller
        completed = self._projection_completed_view
        keys = tuple((phase, tuple((tile.key, tuple(tile.world_rect.getRect()), int(tile.image.cacheKey()))
                                   for tile in tiles)) for phase, tiles in completed[1]) if completed is not None else ()
        assert sum(len(row[1]) for row in keys) <= 64, 'Fixture metadata bound exceeded'
        camera = self.camera_transform()
        presented_initialized = '_projection_presented_revision' in vars(self)
        presented_revision = getattr(self, '_projection_presented_revision', None)
        self.frame_capture = {
            'frame': self.frames, 'document': document, 'source_keys': keys,
            'completed_revision': completed[2] if completed is not None else None,
            'presented_revision': presented_revision,
            'presented_revision_initialized': presented_initialized,
            'pending': self._projection_frame_pending,
            'camera': tuple(getattr(camera, f'm{i}{j}')() for i in range(1, 4) for j in range(1, 4)),
            'dpr': self.devicePixelRatioF(), 'size': (width, height),
            'current': bool(completed is not None and completed[0] == document.configuration
                and completed[2] == document.revision and presented_initialized
                and presented_revision is not None and presented_revision == document.revision
                and not self._projection_frame_pending and not controller.scheduler.busy
                and controller.snapshot is not None and controller.snapshot.document == document),
        }


@pytest.fixture
def native_scene(qapp, monkeypatch, wait_scene):
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
    wait_scene(canvas)
    # wait_scene's QImage render can execute paintGL without this fixture's
    # paintEvent reader. Require a subsequent actual owned current frame.
    before_frame = canvas.frames
    deadline = time.monotonic() + 10.
    while time.monotonic() < deadline:
        canvas.update()
        qapp.processEvents()
        if canvas.frames > before_frame and canvas.frame_capture and canvas.frame_capture['current']:
            break
        time.sleep(.002)
    else:
        pytest.fail(f'No current owned capture after ordinary settlement: {canvas.frame_capture!r}')
    yield canvas
    if isValid(canvas):
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
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


def wait_native_mask_frame(canvas, qapp, wait_scene):
    """Capture the matching detached artwork and mask presentation resources."""
    wait_scene(canvas, timeout=60)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        frames = canvas.frames
        canvas.update()
        qapp.processEvents()
        controller = getattr(canvas, '_mask_overlay_controller', None)
        assert controller is None or controller.error is None, controller.error
        if (controller is not None and controller.ready_key == controller.key(controller.context())
                and not controller.jobs.pending and controller.jobs.active is None
                and canvas.frames > frames and not canvas._projection_frame_pending
                and canvas._projection_presented_revision == canvas._document_projection.revision):
            return
        time.sleep(.002)
    pytest.fail('Matching native mask presentation did not finish')


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
    initial_capture = canvas.frame_capture
    assert initial_capture["current"]
    before = canvas.frames
    canvas.update(QRect(420, 420, 12, 12))
    qapp.processEvents()
    assert canvas.frames > before
    assert canvas.frame_capture['current'], canvas.frame_capture
    for name in ('document', 'source_keys', 'completed_revision', 'presented_revision', 'camera', 'dpr', 'size'):
        assert canvas.frame_capture[name] == initial_capture[name], name
    assert_pixel(canvas, 12, 12, [0, 255, 0, 255])
    np.testing.assert_array_equal(canvas.frame_pixels, initial)
    assert canvas._document_presentation_stats.uploads == 0


@pytest.mark.parametrize("smudge_preview", [False, True], ids=["retained-tiles", "smudge-preview"])
def test_collected_previous_frame_cannot_end_current_native_painter(native_scene, qapp, monkeypatch,
                                                                  smudge_preview, wait_scene):
    """Caught effect tracebacks may outlive their frame and be collected mid-paint."""
    import gc

    canvas = native_scene
    if smudge_preview:
        from comic_editor.core.models import DistortModifier
        artwork = next(iter(canvas.chapter.objects.values()))
        modifier = DistortModifier(modifier_type="distort_smudge", frame=(0, 0, 1024, 1024))
        canvas.chapter.add_modifier(modifier, [("object", artwork.object_id)])
        canvas._smudge_parameter_drag_id = modifier.modifier_id
        canvas.documentChanged.emit(None)
        wait_scene(canvas)
    frame = canvas._paint_canvas_frame
    retained = []
    old_painters = []

    def capture_traceback(painter, started):
        # Retain the actual native painter through the frame's traceback, as
        # happens during deferred captures. Its Python destructor is delayed.
        old_painters.append(painter)
        frame(painter, started)
        try:
            raise RuntimeError("Simulated deferred capture")
        except RuntimeError as error:
            retained.append(error)

    monkeypatch.setattr(canvas, "_paint_canvas_frame", capture_traceback)
    canvas.update()
    qapp.processEvents()
    assert old_painters and all(not painter.isActive() for painter in old_painters)

    def collect_during_paint(painter, started):
        assert painter.isActive()
        old_painters.clear()
        retained.clear()
        gc.collect()
        # This enters Qt native painting and then draws ordinary UI overlays.
        # Ending an old, still-active painter here used to crash Qt's engine.
        frame(painter, started)
        assert painter.isActive()

    monkeypatch.setattr(canvas, "_paint_canvas_frame", collect_during_paint)
    before = canvas.frames
    canvas.update()
    qapp.processEvents()
    assert canvas.frames > before
    assert_pixel(canvas, 225, 225, [0, 0, 255, 255])
    assert_pixel(canvas, 12, 12, [0, 255, 0, 255])


def test_native_radial_handle_stays_responsive_until_final_exact_frame(native_scene, qapp, monkeypatch, wait_scene):
    from threading import Event, get_ident
    from comic_editor.ui import radial_blur

    canvas = native_scene
    artwork = next(iter(canvas.chapter.objects.values()))
    modifier = RadialBlurModifier(center=(512, 512), angle=0)
    canvas.chapter.add_modifier(modifier, [("object", artwork.object_id)])
    canvas.set_selection("object", artwork.object_id)
    canvas.modifier_mode, canvas.active_modifier_id = True, modifier.modifier_id
    canvas.documentChanged.emit(None)
    canvas.update()
    wait_scene(canvas)
    before = canvas.frame_pixels.copy()
    center, end = canvas._radial_handle_points(modifier)
    assert canvas._begin_modifier_handle(end)
    entered, release = Event(), Event()
    gui, original = get_ident(), radial_blur.radial_blur

    def blocked(*args, **kwargs):
        assert get_ident() != gui, "Native handle drag ran radial integration on the GUI thread"
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(radial_blur, "radial_blur", blocked)
    try:
        canvas._move_modifier_handle(center + QPointF(72, 1))
        canvas._flush_radial_handle()
        frames = canvas.frames
        canvas.update()
        deadline = time.monotonic()+2
        while not entered.is_set() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.002)
        assert entered.is_set()
        assert canvas.frames > frames and canvas._projection_frame_pending
        assert canvas._scene_controller.scheduler.busy
        canvas._finish_modifier_handle()
        canvas.update()
        qapp.processEvents()
        assert canvas._projection_frame_pending
    finally:
        release.set()
    wait_scene(canvas, timeout=60)
    assert not canvas._projection_frame_pending
    assert canvas._projection_presented_revision == canvas._document_projection.revision
    assert np.any(canvas.frame_pixels != before)


def test_native_intensity_gradient_pen_edit_reuses_full_quality_blur(native_scene, qapp, monkeypatch, wait_scene):
    from comic_editor.ui import radial_blur
    canvas = native_scene
    artwork = next(iter(canvas.chapter.objects.values()))
    modifier = RadialBlurModifier(center=(512, 512), angle=1)
    mask = ToneMask(saved=True)
    canvas.chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    canvas.chapter.add_modifier(modifier, [("object", artwork.object_id)])
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas._mask_gradient_press(QPointF(100, 512))
    canvas._mask_gradient_move(QPointF(900, 512))
    canvas._finish_mask_gradient()
    # Warm exact artwork before testing the actual interactive paintEvent.
    canvas.update()
    wait_native_mask_frame(canvas, qapp, wait_scene)
    before = canvas.frame_pixels.copy()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Intensity-gradient edit repeated angular integration")
    monkeypatch.setattr(radial_blur, "radial_blur", forbidden)
    canvas._mask_gradient_press(QPointF(900, 512))
    canvas._pen_contact_active = True
    canvas._mask_gradient_move(QPointF(600, 512))
    canvas.update()
    wait_native_mask_frame(canvas, qapp, wait_scene)
    assert canvas.active_mask_gradient().line_field.geometry.nodes[-1].position == (600., 512.)
    assert np.any(canvas.frame_pixels != before)
    canvas._finish_mask_gradient()
    canvas._pen_contact_active = False
    canvas.update()
    wait_native_mask_frame(canvas, qapp, wait_scene)
    assert not canvas._projection_frame_pending
    assert canvas._projection_presented_revision == canvas._document_projection.revision
    assert canvas._effect_jobs.running is None and not canvas._effect_jobs.pending


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


def test_native_prediction_between_retained_phases_keeps_top_art_and_ui(native_scene, qapp, monkeypatch, wait_scene):
    canvas = native_scene
    top = next(iter(canvas.chapter.objects.values()))
    top.show_on_top = True
    drawing = canvas.chapter.add_object(top.parent_layer_id, RasterObject())
    canvas.set_selection("object", drawing.object_id)
    canvas.settings.predictive_ink = True
    canvas._predictive = (QPointF(350, 450), QPointF(600, 450), 30, QColor("green"))
    canvas._invalidate_scene_cache()
    canvas.update()
    wait_scene(canvas)
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


@pytest.mark.parametrize('erasing', [False, True])
@pytest.mark.parametrize('scale,rotation', [(1., 0.), (.73, 17.)])
def test_prepared_native_contact_batch_matches_qpainter_images_and_reuses_textures(
        native_scene, qapp, monkeypatch, wait_scene, erasing, scale, rotation):
    """Only the display edge changes; native composed patch bytes are retained."""
    from threading import Event
    from comic_editor.ui.canvas import ToolKind
    import comic_editor.ui.raster_feedback as feedback_ui

    canvas = native_scene
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    page.bound = BoundGeometry.circle(512.25, 512.75, 490.5)
    page.border_width, page.border_color = 4., '#ffcc8800'
    drawing = canvas.chapter.add_object(page.layer_id, RasterObject(x=-239., y=-227., opacity=.5))
    # A retained native source spanning odd/negative placement and tile seams.
    for y in range(1, 4):
        for x in range(1, 4):
            tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
            tile.fill(QColor('#7f800080') if erasing else Qt.transparent)
            canvas.tiles.set_tile(drawing.object_id, (x, y), tile)
    canvas.set_selection('object', drawing.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER if erasing else ToolKind.RASTER_PENCIL)
    canvas.settings.predictive_ink = not erasing
    canvas.primary_color = '#ff00aa33'
    canvas.settings.brush_size = 24
    canvas.scale, canvas.rotation = scale / canvas.devicePixelRatioF(), rotation
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    assert canvas._scene_controller.feedback is not None
    release = Event()
    scheduler = canvas._scene_controller.scheduler
    evaluate = scheduler._evaluate_admitted
    def blocked(*args):
        assert release.wait(20)
        return evaluate(*args)
    monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
    try:
        canvas._begin_stroke(QPointF(500, 512), 1.)
        canvas._continue_stroke(QPointF(570, 512), .7)
        before = canvas.frames
        canvas.update()
        deadline = time.monotonic() + 2
        while canvas.frames == before and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.002)
        assert canvas.frames > before and canvas._raster_feedback_contact_covered
        batch = canvas.frame_pixels.copy()
        presenter = canvas._document_tile_presenter
        feedback_textures = [key for key in presenter._textures
                             if isinstance(key[0], tuple) and key[0][0] == 'raster-feedback']
        assert feedback_textures, 'Current contact must use retained native presentation'
        uploads = presenter.uploads
        canvas.update()
        qapp.processEvents()
        assert presenter.uploads == uploads
        np.testing.assert_array_equal(canvas.frame_pixels, batch)
        # Original native Qt image presentation, using exactly the same patch
        # buffers/gutters. Filtering may differ by one display byte; artwork
        # bytes and the final exact document path are unchanged.
        def qt_images(painter, tiles, camera, size, *, owner, smooth, clip_world):
            painter.save()
            try:
                painter.setTransform(camera)
                painter.setClipRect(clip_world, Qt.IntersectClip)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
                for tile in tiles:
                    painter.drawImage(tile.world_rect, tile.image, tile.source_rect)
            finally:
                painter.restore()
        monkeypatch.setattr(feedback_ui, 'draw_document_tiles', qt_images)
        canvas.update()
        qapp.processEvents()
        np.testing.assert_allclose(canvas.frame_pixels, batch, atol=1)
        assert canvas._raster_feedback_contact_covered
        canvas._end_stroke()
    finally:
        release.set()
    wait_scene(canvas)
    assert not canvas._projection_frame_pending
    assert not canvas._projection_provisional_visible
