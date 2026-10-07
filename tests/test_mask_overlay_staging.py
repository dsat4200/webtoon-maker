"""Mask widget paint consumes prepared resources, including native cold inputs."""
from threading import Event, get_ident
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, ToneMask
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tile_backing import TileResidency
from comic_editor.core.tiles import TileStore
from comic_editor.render.scene_kernels import SceneKernels
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.002)
    assert predicate()


def overlay(canvas, density=1., smooth=False):
    image = QImage(round(canvas.width()*density), round(canvas.height()*density), QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(density)
    image.fill(QColor('#4f314d'))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
    try:
        canvas._draw_tone_mask_preview(painter)
    finally:
        painter.end()
    return image


def reference(canvas, density=1., smooth=False):
    """Original split presentation, independent of the new worker adapter."""
    mask = canvas.chapter.masks[canvas.active_tone_mask_id]
    width, height = canvas.width(), canvas.height()
    transform, visible = canvas.camera_transform(), canvas.visible_document_rect()
    field = canvas.render_tone_mask_field(mask.mask_id, width, height, transform, visible,
                                          include_paint=mask.paint_has_subtractions)
    image = QImage(round(width*density), round(height*density), QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(density)
    image.fill(QColor('#4f314d'))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
    painter.drawImage(0, 0, canvas._blue_mask_image(field))
    if not mask.paint_has_subtractions:
        painter.setCompositionMode(QPainter.CompositionMode_Plus)
        painter.setTransform(transform)
        painter.setClipRect(QRectF(0, 0, canvas.chapter.width, canvas.chapter.height))
        painter.translate(*mask.paint_offset)
        for key, tile in canvas.tiles.iter_tiles(mask.mask_id, visible.translated(
                -mask.paint_offset[0], -mask.paint_offset[1])):
            painter.drawImage(key[0]*canvas.tiles.tile_size, key[1]*canvas.tiles.tile_size,
                              canvas._blue_mask_image(canvas._image_alpha_array(tile)))
    painter.end()
    return image


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).copy()


@pytest.mark.parametrize('density', [1., 2.])
@pytest.mark.parametrize('smooth', [False, True])
def test_bounded_zoomed_out_paint_frame_keeps_original_rotated_sampling(scene, density, smooth, monkeypatch):
    canvas, _mask, _contributor = scene
    # Force the same bounded presentation used for hundreds of visible tiles.
    monkeypatch.setattr('comic_editor.render.mask_overlay.NATIVE_TILE_LIMIT', 0)
    expected = reference(canvas, density, smooth)
    canvas._invalidate_tone_mask_overlay()
    overlay(canvas, density, smooth)
    wait_for(lambda: canvas._mask_overlay_controller.ready is not None)
    prepared = canvas._mask_overlay_controller.ready
    assert prepared.paint == ()
    assert prepared.paint_frame.devicePixelRatioF() == density
    assert prepared.paint_frame.sizeInBytes() == round(canvas.width()*density)*round(canvas.height()*density)*4
    np.testing.assert_array_equal(pixels(overlay(canvas, density, smooth)), pixels(expected))


@pytest.fixture
def scene(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    chapter = ChapterDocument(width=128, height=128, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 128, 128))
    contributor = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    mask = ToneMask(saved=True, contributors=[('object', contributor.object_id)], paint_offset=(3.25, 1.5))
    chapter.masks[mask.mask_id] = mask
    tiles = TileStore()
    source = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    source.fill(QColor.fromRgbF(.27, .48, .63, .37))
    paint = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    paint.fill(Qt.transparent)
    painter = QPainter(paint)
    painter.fillRect(12, 16, 40, 55, QColor.fromRgbF(1., 1., 1., .51))
    painter.fillRect(72, 15, 25, 62, QColor.fromRgbF(0., 0., 0., .73))
    painter.end()
    for identifier, image in ((contributor.object_id, source), (mask.mask_id, paint)):
        path = tmp_path / f'{identifier}.png'
        assert image.save(str(path))
        tiles._object_tiles(identifier).register((0, 0), path)
        tiles._alpha_bounds[identifier] = {(0, 0): TileStore._alpha_bbox(image)}
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', snap_to_grid=False,
                                         grid_overlay_visible=False))
    canvas.setFixedSize(128, 128)
    canvas.set_document(chapter, tiles)
    canvas.scale = 1.15
    canvas.rotation = 13.
    canvas.center_x = canvas.center_y = 64.
    canvas.set_tone_mask_mode(mask.mask_id)
    qapp.processEvents()
    yield canvas, mask, contributor
    for name in ('_mask_tile_input', '_raster_tile_input'):
        gate = getattr(canvas, name, None)
        if gate is not None:
            gate.cancel()
    controller = getattr(canvas, '_mask_overlay_controller', None)
    if controller is not None:
        controller.jobs.shutdown()
    canvas._scene_controller.reset()
    canvas.deleteLater()


@pytest.mark.parametrize('subtractions', [False, True])
def test_cold_overlay_worker_preserves_scalar_and_split_presentation(scene, subtractions, monkeypatch):
    canvas, mask, _ = scene
    mask.paint_has_subtractions = subtractions
    expected = reference(canvas)
    canvas.tiles.residency.clear(discard=True)
    canvas._invalidate_tone_mask_overlay()
    entered, release, owner = Event(), Event(), get_ident()
    original = SceneKernels.render_tone_mask_field
    def field(*args, **kwargs):
        assert get_ident() != owner, 'widget evaluated a cold scalar mask'
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(SceneKernels, 'render_tone_mask_field', field)
    monkeypatch.setattr(canvas, 'render_tone_mask_field', lambda *_a, **_k: pytest.fail('GUI mask evaluation'))
    iter_tiles = canvas.tiles.iter_tiles
    monkeypatch.setattr(canvas.tiles, 'iter_tiles', lambda *_a, **_k: pytest.fail('GUI cold mask iteration'))
    try:
        untouched = overlay(canvas)
        wait_for(entered.is_set)
        assert canvas._mask_overlay_controller.ready is None
        np.testing.assert_array_equal(pixels(overlay(canvas)), pixels(untouched))
        assert canvas.tiles.residency.decodes == 2  # Only the independent oracle decoded live originals.
        release.set()
        wait_for(lambda: canvas._mask_overlay_controller.ready is not None)
        np.testing.assert_array_equal(pixels(overlay(canvas)), pixels(expected))
        assert canvas._mask_overlay_controller.error is None
    finally:
        release.set()
        monkeypatch.setattr(canvas.tiles, 'iter_tiles', iter_tiles)


@pytest.mark.parametrize('retire', ['camera', 'mask', 'document'])
def test_mask_overlay_never_publishes_retired_viewport_or_document(scene, retire, monkeypatch):
    canvas, mask, _ = scene
    from comic_editor.ui import mask_overlay
    entered, release = Event(), Event()
    original = mask_overlay.prepare_mask_overlay
    def prepare(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(mask_overlay, 'prepare_mask_overlay', prepare)
    try:
        overlay(canvas)
        wait_for(entered.is_set)
        controller = canvas._mask_overlay_controller
        if retire == 'camera':
            canvas.center_x += 20
        elif retire == 'mask':
            canvas.set_tone_mask_mode('')
        else:
            other = ChapterDocument(width=64, height=64)
            other.add_page()
            canvas.set_document(other, TileStore())
        release.set()
        wait_for(lambda: controller.jobs.active is None)
        assert controller.ready is None
        assert controller.ready_key is None
    finally:
        release.set()


@pytest.mark.parametrize('erasing', [False, True])
def test_warm_mask_contact_and_release_present_resident_pixels_without_mask_evaluation(scene, erasing, monkeypatch):
    canvas, mask, _ = scene
    overlay(canvas)
    wait_for(lambda: canvas._mask_overlay_controller.ready is not None)
    canvas.tiles.tile(mask.mask_id, (0, 0))
    canvas.set_tool(ToolKind.RASTER_ERASER if erasing else ToolKind.RASTER_PENCIL)
    # Tool switching clears a completed overlay if it retires another gesture.
    overlay(canvas)
    wait_for(lambda: canvas._mask_overlay_controller.ready is not None)
    before = overlay(canvas)
    with monkeypatch.context() as patch:
        patch.setattr(canvas, 'render_tone_mask_field', lambda *_a, **_k: pytest.fail('GUI scalar evaluation'))
        patch.setattr(canvas.tiles, 'iter_tiles', lambda *_a, **_k: pytest.fail('GUI mask iteration'))
        canvas._begin_mask_stroke(QPointF(25, 35), .3)
        assert overlay(canvas) != before
        canvas._continue_mask_stroke(QPointF(45, 42), .9)
        canvas._end_mask_stroke()
        finished = overlay(canvas)
    expected = reference(canvas)
    np.testing.assert_array_equal(pixels(finished), pixels(expected))
    canvas.command_stack.undo()
    overlay(canvas)
    wait_for(lambda: canvas._mask_overlay_controller.ready_key == canvas._mask_overlay_controller.key(
        canvas._mask_overlay_controller.context()))
    np.testing.assert_array_equal(pixels(overlay(canvas)), pixels(before))


@pytest.mark.parametrize('cancel', [False, True])
def test_signed_mask_last_release_cancel_and_undo_publish_matching_scalar_resources(scene, cancel, monkeypatch):
    canvas, mask, _ = scene
    mask.paint_has_subtractions = True
    before = reference(canvas)
    canvas._invalidate_tone_mask_overlay()
    overlay(canvas)
    wait_for(lambda: canvas._mask_overlay_controller.ready is not None)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    baseline = canvas.command_stack.revision
    entered, release, owner = Event(), Event(), get_ident()
    original = SceneKernels.render_tone_mask_field
    def field(*args, **kwargs):
        assert get_ident() != owner, 'signed mask was evaluated in widget paint'
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(SceneKernels, 'render_tone_mask_field', field)
        patch.setattr(canvas, 'render_tone_mask_field', lambda *_a, **_k: pytest.fail('GUI signed scalar'))
        try:
            canvas._begin_mask_stroke(QPointF(82, 42), .4)
            canvas._continue_mask_stroke(QPointF(87, 55), .9)
            if cancel:
                assert canvas._cancel_mask_stroke()
            else:
                canvas._end_mask_stroke()
            overlay(canvas)
            wait_for(entered.is_set)
            release.set()
            wait_for(lambda: canvas._mask_overlay_controller.ready_key == canvas._mask_overlay_controller.key(
                canvas._mask_overlay_controller.context()))
        finally:
            release.set()
    monkeypatch.setattr(canvas, 'render_tone_mask_field', original.__get__(canvas, type(canvas)))
    expected = before if cancel else reference(canvas)
    np.testing.assert_array_equal(pixels(overlay(canvas)), pixels(expected))
    assert canvas.command_stack.revision == baseline + (0 if cancel else 1)
    if not cancel:
        assert expected != before
        canvas.command_stack.undo()
        overlay(canvas)
        wait_for(lambda: canvas._mask_overlay_controller.ready is not None)
        np.testing.assert_array_equal(pixels(overlay(canvas)), pixels(before))
