"""Auto QWidget presentation preserves actual GPU bake work and native history."""
import ctypes
import threading

import numpy as np
import pytest
from PySide6.QtGui import QImage, QPainter

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.device import DeviceImage
from comic_editor.render.gpu import worker as worker_module
from comic_editor.ui import canvas as canvas_module, point_lut
from comic_editor.ui.baking import request_apply_raster_modifiers
from comic_editor.ui.canvas_presentation_policy import prefer_raster_presentation
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _qimage_premultiplied
from test_gpu_worker import wait_until
from test_point_chain import effects, source_image


@pytest.mark.parametrize('blend', ['normal', 'soft_light'])
def test_auto_raster_presentation_keeps_native_gpu_bake_and_history(qapp, monkeypatch, blend):
    if (qapp.platformName() != 'windows' or not canvas_module.gpu_available()
            or not prefer_raster_presentation(canvas_module._GPU_PRESENTATION_IDENTITY,
                                             qapp.primaryScreen().name())):
        pytest.skip('Requires the measured native cross-adapter presentation build')
    monkeypatch.setattr(canvas_module, 'create_network_manager', lambda *_: None)
    owner = canvas_module.create_canvas(EditorSettings(canvas_renderer='auto',
        predictive_ink=False, grid_overlay_visible=False, snap_to_grid=False))
    assert isinstance(owner, canvas_module.RasterCanvasWidget)
    assert not hasattr(owner, 'context')
    owner.setUpdatesEnabled(False)
    gui_thread = threading.get_ident()
    operations, readbacks, identity = [], [], {}
    original_request = worker_module._admitted_request
    original_materialize = DeviceImage.materialize

    def observed_request(service, renderer, helpers, helper_sizes, leases, request):
        if renderer.available and not identity:
            with renderer._current():
                gl = ctypes.WinDLL('opengl32')
                gl.glGetString.argtypes = [ctypes.c_uint]
                gl.glGetString.restype = ctypes.c_char_p
                identity['renderer'] = gl.glGetString(0x1F01).decode()
        operations.append((request.operation, threading.get_ident()))
        return original_request(service, renderer, helpers, helper_sizes, leases, request)

    def observed_materialize(image):
        readbacks.append(threading.get_ident())
        assert readbacks[-1] != gui_thread
        return original_materialize(image)

    monkeypatch.setattr(worker_module, '_admitted_request', observed_request)
    monkeypatch.setattr(DeviceImage, 'materialize', observed_materialize)
    try:
        chapter = ChapterDocument(width=1024, height=1024, document_kind='asset')
        page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 1024, 1024))
        page.shape_style.outline_thickness = 0
        obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 1024, 1024)))
        source = source_image(1024, 1024)
        tiles = TileStore()
        for y in range(4):
            for x in range(4):
                tiles.set_tile(obj.object_id, (x, y), source.copy(x*256, y*256, 256, 256))
        modifiers = effects(blend)
        for effect in modifiers:
            chapter.add_modifier(effect, [('object', obj.object_id)])
        owner.set_document(chapter, tiles)
        owner.set_selection('object', obj.object_id)
        # Use the ordinary queued QWidget graphics initialization and the real
        # asynchronous Apply action, with no injected worker or direct GPU call.
        wait_until(qapp, lambda: point_lut.graphics_worker() is not None
                   and point_lut.graphics_worker().ready.is_set())
        graphics = point_lut.graphics_worker()
        assert graphics.available and not graphics.closed
        expected = source
        for effect in modifiers:
            expected = apply_modifier_stack(expected, [effect], (0, 0), _point_lut=False)
        completed = []
        request_apply_raster_modifiers(owner, modifiers[-1].modifier_id, completed.append)
        assert completed == []
        wait_until(qapp, lambda: bool(completed), timeout=30)
        assert completed == [None]
        assert chapter.objects[obj.object_id].modifier_ids == []
        assert len(owner.command_stack._undo) == 1

        def assembled():
            result = QImage(1024, 1024, QImage.Format_ARGB32_Premultiplied)
            result.fill(0)
            painter = QPainter(result)
            try:
                for (x, y), image in owner.tiles.iter_tiles(obj.object_id):
                    painter.drawImage(x*256, y*256, image)
            finally:
                painter.end()
            return result

        np.testing.assert_array_equal(_qimage_premultiplied(assembled()), _qimage_premultiplied(expected))
        assert any(operation == 'segment' for operation, _thread in operations), operations
        assert all(thread != gui_thread for _operation, thread in operations)
        assert readbacks and all(thread != gui_thread for thread in readbacks)
        assert identity['renderer'].split('/')[0] == 'NVIDIA GeForce RTX 5060'
        owner.command_stack.undo()
        np.testing.assert_array_equal(_qimage_premultiplied(assembled()), _qimage_premultiplied(source))
        assert chapter.objects[obj.object_id].modifier_ids == [effect.modifier_id for effect in modifiers]
        owner.command_stack.redo()
        np.testing.assert_array_equal(_qimage_premultiplied(assembled()), _qimage_premultiplied(expected))
    finally:
        consumers = getattr(owner, '_scene_consumers', None)
        if consumers is not None:
            consumers.shutdown()
            consumers.executor.shutdown(wait=True, cancel_futures=True)
        owner._scene_controller.reset()
        owner._scene_controller.scheduler.close()
        owner._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
        owner._effect_jobs.cancel()
        owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        owner.close()
        owner.deleteLater()
