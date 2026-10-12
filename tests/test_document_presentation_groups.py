"""Ordered native presentation passes, isolated from editor and user artwork."""
from __future__ import annotations

import ctypes
import numpy as np
import pytest
from PySide6.QtCore import QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QPainter, QSurfaceFormat, QTransform

from comic_editor.ui.document_presentation import (
    PresentedTile, PresentedTileGroup, draw_document_tile_groups,
    draw_document_tiles,
)
from test_document_presentation import framebuffer, gl_context, pixels, solid


def groups():
    # A retained revision and a new revision of the same logical tile must
    # coexist until the ordered frame has finished presenting both of them.
    first = PresentedTile("same", solid(QColor(220, 40, 90, 117), 20),
                          QRectF(3, 4, 16, 16), QRectF(2, 2, 16, 16))
    second = PresentedTile("same", solid(QColor(30, 210, 70, 153), 20),
                           QRectF(9, 7, 16, 16), QRectF(2, 2, 16, 16))
    return (PresentedTileGroup((first,), QRectF(5, 0, 24, 25)),
            PresentedTileGroup((second,), QRectF(0, 10, 30, 20)))


@pytest.mark.parametrize("angle", [0., 17.25])
@pytest.mark.parametrize("ratio", [1., 1.25, 2.])
def test_gpu_groups_match_ordered_passes_with_one_state_and_geometry_upload(
        gl_context, monkeypatch, angle, ratio):
    presenter = gl_context
    target = framebuffer(presenter, 80, 80, (.1, .2, .3, 1.))
    camera, viewport = QTransform.fromTranslate(4.25, 5.75), QSizeF(80 / ratio, 80 / ratio)
    camera.rotate(angle)
    passes = groups()
    for group in passes:
        assert presenter.draw(group.tiles, camera, viewport,
                              clip_world=group.clip_world,
                              device_pixel_ratio=ratio, opacity=.73)
    expected = pixels(target.toImage())
    presenter.functions.glClear(0x4000)
    save, restore = presenter._save_state, presenter._restore_state
    boundaries = []
    monkeypatch.setattr(presenter, "_save_state", lambda: (boundaries.append("save"), save())[1])
    monkeypatch.setattr(presenter, "_restore_state",
                        lambda state: (boundaries.append("restore"), restore(state))[1])
    geometry_before = presenter.geometry_uploads
    assert presenter.draw_groups(passes, camera, viewport,
                                 device_pixel_ratio=ratio, opacity=.73)
    np.testing.assert_array_equal(pixels(target.toImage()), expected)
    assert boundaries == ["save", "restore"]
    assert presenter.geometry_uploads == geometry_before + 1
    assert len(presenter._textures) == 2
    assert presenter.texture_bytes == 2 * 20 * 20 * 4
    presenter.functions.glClear(0x4000)
    uploads_before = presenter.uploads
    assert presenter.draw_groups(passes, camera, viewport,
                                 device_pixel_ratio=ratio, opacity=.73)
    np.testing.assert_array_equal(pixels(target.toImage()), expected)
    assert presenter.geometry_uploads == geometry_before + 1
    assert presenter.uploads == uploads_before
    target.release()


def test_group_union_budget_rejects_before_any_draw(gl_context):
    presenter = gl_context
    presenter.byte_limit = 20 * 20 * 4
    target = framebuffer(presenter, color=(.1, .2, .3, 1.))
    expected = pixels(target.toImage())
    assert not presenter.draw_groups(groups(), QTransform(), QSizeF(64, 64))
    assert "budget" in presenter.reason
    assert presenter.uploads == presenter.draws == presenter.geometry_uploads == 0
    np.testing.assert_array_equal(pixels(target.toImage()), expected)
    target.release()


@pytest.mark.parametrize("unavailable", ["dimension", "fence"])
def test_every_group_resource_is_validated_before_upload_or_draw(gl_context, monkeypatch, unavailable):
    from comic_editor.render.device import DeviceImage
    from comic_editor.render.gpu.sync import GlSync

    presenter = gl_context
    target = framebuffer(presenter, color=(.1, .2, .3, 1.))
    expected = pixels(target.toImage())
    passes = groups()
    if unavailable == "dimension":
        # First pass fits, but a later texture cannot be presented.
        presenter._max_texture_size = 18
        first = PresentedTile("small", solid(size=16), QRectF(0, 0, 16, 16))
        passes = (PresentedTileGroup((first,)), passes[1])
    else:
        class Owner:
            closed, available, render_context = False, True, presenter.context
            def release(self, _token):
                pass
        owner = Owner()
        image = DeviceImage(owner, 987, "pending-fence", 16, 16, 0, 99)
        tile = PresentedTile("device", image, QRectF(0, 0, 16, 16))
        passes = (passes[0], PresentedTileGroup((tile,)))
        monkeypatch.setattr(GlSync, "ready", lambda _self, _fence: False)
    assert not presenter.draw_groups(passes, QTransform(), QSizeF(64, 64))
    assert presenter.uploads == presenter.draws == presenter.geometry_uploads == 0
    np.testing.assert_array_equal(pixels(target.toImage()), expected)
    target.release()


def test_group_preflight_late_upload_failure_has_no_translucent_prefix(gl_context, monkeypatch):
    presenter = gl_context
    target = framebuffer(presenter, color=(.1, .2, .3, 1.))
    expected = pixels(target.toImage())
    upload = presenter._upload
    count = 0
    def fail_second(tile):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("controlled second upload failure")
        upload(tile)
    monkeypatch.setattr(presenter, "_upload", fail_second)
    assert not presenter.draw_groups(groups(), QTransform(), QSizeF(64, 64))
    assert presenter.draws == 0
    np.testing.assert_array_equal(pixels(target.toImage()), expected)
    target.release()


def test_submission_failure_restores_state_and_cannot_request_replay(gl_context, monkeypatch):
    presenter = gl_context
    target = framebuffer(presenter)
    gl = presenter.functions
    gl.glViewport(1, 2, 31, 27)
    gl.glDisable(0x0BE2)
    draw, count = gl.glDrawArrays, 0
    def fail_second(*arguments):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("controlled submission failure")
        draw(*arguments)
    monkeypatch.setattr(gl, "glDrawArrays", fail_second)
    with pytest.raises(RuntimeError, match="controlled submission failure"):
        presenter.draw_groups(groups(), QTransform(), QSizeF(64, 64))
    assert presenter._integer_vector(0x0BA2, 4) == [1, 2, 31, 27]
    assert not gl.glIsEnabled(0x0BE2)
    target.release()


def test_vector_query_resolution_is_cached_for_the_owned_context(gl_context, monkeypatch):
    presenter = gl_context
    assert len(presenter._integer_vector(0x0BA2, 4)) == 4
    query = presenter._integer_query
    monkeypatch.setattr(presenter.context, "getProcAddress",
                        lambda _name: pytest.fail("repeated vector entry point lookup"))
    assert len(presenter._integer_vector(0x0C23, 4)) == 4
    assert presenter._integer_query is query


@pytest.mark.parametrize("clipped", [False, True])
def test_public_raster_groups_keep_order_and_painter_state(clipped):
    camera = QTransform.fromTranslate(4.25, 5.75)
    passes = list(groups())
    passes.append(PresentedTileGroup((PresentedTile("replacement", solid(Qt.transparent, 8),
        QRectF(12, 13, 8, 8)),), replace_background=QColor("#242428")))
    results = []
    for grouped in (False, True):
        image = solid("black", 64)
        painter = QPainter(image)
        painter.setTransform(QTransform.fromTranslate(1, 2))
        painter.setOpacity(.73)
        if clipped:
            painter.setClipRect(QRectF(0, 0, 38, 40))
        transform, clip = painter.transform(), painter.clipBoundingRect()
        if grouped:
            stats = draw_document_tile_groups(painter, passes, camera, QSizeF(64, 64), smooth=False)
            assert stats.backend == "raster" and stats.tiles == 3 and stats.uploads == 0
        else:
            for group in passes:
                draw_document_tiles(painter, group.tiles, camera, QSizeF(64, 64),
                    smooth=False, clip_world=group.clip_world,
                    replace_background=group.replace_background)
        assert painter.transform() == transform and painter.opacity() == .73
        assert painter.clipBoundingRect() == clip
        painter.end()
        results.append(pixels(image))
    np.testing.assert_array_equal(*results)


@pytest.mark.parametrize('busy_retry', [False, True])
def test_public_gpu_groups_return_to_qpainter_before_overlays(gl_context, qapp, monkeypatch, busy_retry):
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
            red = PresentedTile("same", solid(QColor(255, 0, 0, 128)), QRectF(8, 8, 16, 16))
            green = PresentedTile("same", solid(QColor(0, 255, 0, 128)), QRectF(12, 8, 16, 16))
            self.passes = (PresentedTileGroup((red,)), PresentedTileGroup((green,)))

        def paintGL(self):
            painter = QPainter(self)
            try:
                painter.fillRect(self.rect(), QColor("blue"))
                presenter = getattr(self, "_document_tile_presenter", None)
                before = presenter.draws if presenter is not None else 0
                self.stats = draw_document_tile_groups(painter, self.passes,
                    QTransform(), QSizeF(self.size()), owner=self, smooth=False,
                    pending_context=('owned-complete-scene',))
                self.frame_draws = self._document_tile_presenter.draws - before
                painter.fillRect(12, 12, 4, 4, QColor("yellow"))
            finally:
                painter.end()
            # Test-only readback validates the subsequent ordinary Qt overlay.
            side = round(self.width() * self.devicePixelRatioF())
            self.frame_pixels = np.zeros((side, side, 4), dtype=np.uint8)
            convention = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
            read = convention(None, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(
                    self.context().getProcAddress(b"glReadPixels"))
            read(0, 0, side, side, 0x1908, 0x1401, self.frame_pixels.ctypes.data)
            self.frame_pixels = self.frame_pixels[::-1].copy()

    widget = IsolatedCanvas()
    try:
        widget.show()
        qapp.processEvents()
        assert widget.stats.backend == "gpu" and widget.stats.tiles == 2
        assert widget.frame_draws == 1
        ratio = widget.devicePixelRatioF()
        np.testing.assert_allclose(widget.frame_pixels[round(10 * ratio), round(10 * ratio)],
                                   [128, 0, 127, 255], atol=1)
        np.testing.assert_allclose(widget.frame_pixels[round(10 * ratio), round(14 * ratio)],
                                   [64, 128, 63, 255], atol=1)
        np.testing.assert_array_equal(widget.frame_pixels[round(13 * ratio), round(13 * ratio)],
                                      [255, 255, 0, 255])
        assert widget._document_tile_presenter.uploads == 2
        widget.update()
        qapp.processEvents()
        assert widget.frame_draws == 1 and widget._document_tile_presenter.uploads == 2
        if busy_retry:
            presenter = widget._document_tile_presenter
            presenter.byte_limit = presenter.allocated_texture_bytes
            before = widget.frame_pixels.copy()
            ready = presenter._sync.ready
            monkeypatch.setattr(presenter._sync, 'ready', lambda _fence: False)
            def no_raster(*_args, **_kwargs):
                pytest.fail('Temporary owned native pressure entered Qt raster fallback')
            monkeypatch.setattr('comic_editor.ui.document_presentation._draw_document_tiles_raster', no_raster)
            widget.passes = (PresentedTileGroup((PresentedTile('same', solid('blue'), QRectF(8, 8, 16, 16)),)),
                PresentedTileGroup((PresentedTile('same', solid('white'), QRectF(12, 8, 16, 16)),)))
            widget.update()
            qapp.processEvents()
            assert widget.stats.backend == 'gpu-pending' and widget.stats.tiles == 0
            assert widget.stats.uploads == 0 and presenter.uploads == 2
            assert presenter.allocated_texture_bytes == presenter.byte_limit
            np.testing.assert_array_equal(widget.frame_pixels, before)
            monkeypatch.setattr(presenter._sync, 'ready', ready)
            widget.update()
            qapp.processEvents()
            assert widget.stats.backend == 'gpu' and widget.stats.tiles == 2
            assert presenter.uploads == 4 and presenter.texture_allocations == 2
            assert presenter.allocated_texture_bytes == presenter.byte_limit
            ratio = widget.devicePixelRatioF()
            np.testing.assert_array_equal(widget.frame_pixels[round(10 * ratio), round(14 * ratio)],
                [255, 255, 255, 255])
    finally:
        if hasattr(widget, "_document_tile_presenter"):
            widget._document_tile_presenter.close()
        widget.close()
        widget.deleteLater()
        qapp.processEvents()


def test_superseded_pixels_reuse_completed_storage_with_exact_alpha(gl_context):
    presenter = gl_context
    target = framebuffer(presenter)
    texture_id = None
    for revision, color in enumerate((QColor(200, 40, 80, 128), QColor(40, 180, 90, 173),
                                      QColor(20, 70, 220, 61))):
        image = solid(color)
        tile = PresentedTile("edited", image, QRectF(0, 0, 16, 16))
        presenter.functions.glClear(0x4000)
        assert presenter.draw((tile,), QTransform(), QSizeF(64, 64), smooth=False)
        actual = pixels(target.toImage())  # Completes the preceding GPU readers.
        np.testing.assert_array_equal(actual[8, 8], pixels(image)[8, 8])
        entry = next(iter(presenter._textures.values()))
        if texture_id is None:
            texture_id = entry.texture.textureId()
        assert entry.texture.textureId() == texture_id
        assert presenter.texture_allocations == 1
        assert presenter.texture_reuses == revision
        assert presenter.allocated_texture_bytes == 16 * 16 * 4
    target.release()


def test_busy_storage_uses_bounded_spares_without_waiting_or_overwriting(gl_context, monkeypatch):
    presenter = gl_context
    presenter.byte_limit = 3 * 16 * 16 * 4
    target = framebuffer(presenter)
    original_ready = presenter._sync.ready
    first = PresentedTile("edited", solid("red"), QRectF(0, 0, 16, 16))
    assert presenter.draw((first,), QTransform(), QSizeF(64, 64))
    pixels(target.toImage())
    polls = []
    monkeypatch.setattr(presenter._sync, "ready", lambda fence: (polls.append(fence), False)[1])
    for color in ("green", "blue"):
        assert presenter.draw((PresentedTile("edited", solid(color), first.world_rect),),
                              QTransform(), QSizeF(64, 64))
        pixels(target.toImage())
    assert presenter.texture_allocations == 3 and presenter.texture_reuses == 0
    assert presenter.allocated_texture_bytes == presenter.byte_limit
    expected = pixels(target.toImage())
    fourth = PresentedTile("edited", solid("yellow"), first.world_rect)
    assert not presenter.draw((fourth,), QTransform(), QSizeF(64, 64))
    assert "still in use" in presenter.reason
    assert presenter.texture_allocations == 3
    np.testing.assert_array_equal(pixels(target.toImage()), expected)
    assert len(set(polls)) >= 2
    monkeypatch.setattr(presenter._sync, "ready", original_ready)
    assert presenter.draw((fourth,), QTransform(), QSizeF(64, 64))
    assert presenter.texture_allocations == 3 and presenter.texture_reuses == 1
    assert presenter.allocated_texture_bytes == presenter.byte_limit
    target.release()


def test_full_budget_recycles_compatible_cold_lru_before_new_allocation(gl_context):
    presenter = gl_context
    presenter.byte_limit = 2 * 16 * 16 * 4
    target = framebuffer(presenter)
    first = PresentedTile("first", solid("red"), QRectF(0, 0, 16, 16))
    second = PresentedTile("second", solid("blue"), QRectF(16, 0, 16, 16))
    assert presenter.draw((first, second), QTransform(), QSizeF(64, 64))
    pixels(target.toImage())
    first_id = presenter._textures[presenter._key(first)].texture.textureId()
    third = PresentedTile("third", solid("green"), QRectF(0, 16, 16, 16))
    assert presenter.draw((second, third), QTransform(), QSizeF(64, 64))
    assert presenter._textures[presenter._key(third)].texture.textureId() == first_id
    assert presenter._key(second) in presenter._textures
    assert presenter.texture_allocations == 2 and presenter.texture_reuses == 1
    assert presenter.allocated_texture_bytes == presenter.byte_limit
    target.release()


def test_shared_frame_fences_are_released_once_and_pool_close_is_complete(gl_context, monkeypatch):
    presenter = gl_context
    target = framebuffer(presenter)
    insert, delete = presenter._sync.insert, presenter._sync.delete
    inserted, deleted = [], []
    def remember_insert():
        value = insert()
        inserted.append(value)
        return value
    monkeypatch.setattr(presenter._sync, "insert", remember_insert)
    monkeypatch.setattr(presenter._sync, "delete", lambda value: (deleted.append(value), delete(value))[1])
    first = PresentedTile("first", solid("red"), QRectF(0, 0, 16, 16))
    second = PresentedTile("second", solid("blue"), QRectF(16, 0, 16, 16))
    assert presenter.draw((first, second), QTransform(), QSizeF(64, 64))
    pixels(target.toImage())
    shared = presenter._textures[presenter._key(first)].fence
    assert shared is presenter._textures[presenter._key(second)].fence
    assert shared.references == 2 and not deleted
    changed = PresentedTile("first", solid("green"), first.world_rect)
    assert presenter.draw((changed,), QTransform(), QSizeF(64, 64))
    pixels(target.toImage())
    assert shared.references == 1 and not deleted
    target.release()
    presenter.close()
    assert sorted(deleted) == sorted(inserted)
    assert len(deleted) == len(set(deleted)) == 2
    assert presenter.allocated_texture_bytes == 0 and not presenter._texture_pool


def test_borrowed_device_leases_never_enter_the_allocation_pool(gl_context):
    from comic_editor.render.device import DeviceImage
    presenter = gl_context
    class Owner:
        closed, available, render_context = False, True, presenter.context
        released = []
        def release(self, token):
            self.released.append(token)
    owner = Owner()
    image = DeviceImage(owner, 77, "borrowed", 16, 16, 0, 0)
    tile = PresentedTile("borrowed", image, QRectF(0, 0, 16, 16))
    presenter._upload(tile)
    image.release()
    presenter._remove(presenter._key(tile))
    assert owner.released == [77]
    assert not presenter._texture_pool and presenter.allocated_texture_bytes == 0
    assert presenter.texture_allocations == presenter.texture_reuses == 0

