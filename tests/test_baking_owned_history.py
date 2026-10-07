"""Cold bake publication uses prepared native resources and focused history."""
import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (BoundGeometry, ChapterDocument, CurvesModifier,
                                      ImageObject, RasterObject, VectorDrawingObject)
from comic_editor.core.pixel_arrays import native_rgba_pixels
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.baking import request_apply_raster_modifiers, request_rasterize
from comic_editor.ui.canvas import CanvasWidget


def test_focused_bake_history_validates_private_copies_of_frozen_records(qapp, monkeypatch):
    from comic_editor.render.bake_sources import raster_prefix_history
    from comic_editor.render.scene import EvaluatedScene, SceneSnapshotCompiler
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    chapter = ChapterDocument(width=64, height=64, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 64, 64))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 64, 64)))
    modifier = CurvesModifier()
    chapter.add_modifier(modifier, [('object', raster.object_id)])
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, TileStore())
    try:
        capture = SceneSnapshotCompiler().capture(canvas, canvas._render_document_state())
        while not capture.advance(.004):
            pass
        snapshot = capture.result
        frozen = snapshot.chapter.modifiers[modifier.modifier_id]
        original = CurvesModifier.to_dict
        serialized = []
        def private_only(record):
            assert record is not frozen, 'History serializer validated a shared immutable record'
            serialized.append(record)
            return original(record)
        monkeypatch.setattr(CurvesModifier, 'to_dict', private_only)
        state = raster_prefix_history(EvaluatedScene(snapshot), modifier.modifier_id,
                                      [('object', raster.object_id)])
        assert serialized and state.model.records['modifiers'][modifier.modifier_id]
        assert snapshot.chapter.modifiers[modifier.modifier_id] is frozen
    finally:
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()


def wait_for(predicate):
    deadline = time.monotonic()+20
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.002)
    assert predicate()


@pytest.mark.parametrize('action', ['rasterize', 'apply'])
def test_cold_bake_never_serializes_unrelated_records_or_reads_pixels_on_gui(
        action, qapp, tmp_path, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    chapter = ChapterDocument(width=128, height=128, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 128, 128))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    unrelated = chapter.add_object(page.layer_id, VectorDrawingObject(name='Unrelated'))
    modifier = CurvesModifier()
    chapter.add_modifier(modifier, [('object', raster.object_id)])
    native = QImage(256, 256, QImage.Format_RGBA64)
    native.fill(QColor.fromRgbF(.31, .53, .79, .41))
    tiles = TileStore()
    tiles.set_tile(raster.object_id, (0, 0), native)
    root = tmp_path/'native'
    tiles.save_directory(root, {raster.object_id}, complete=True)
    cold = TileStore()
    cold.load_directory(root, {raster.object_id})
    assert cold.residency.decodes == 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, cold)
    canvas.set_selection('object', raster.object_id, activate_default_tool=False)
    gui = threading.get_ident()
    bbox = TileStore._alpha_bbox
    def owned_bbox(image):
        assert threading.get_ident() != gui, 'GUI bake/history alpha scan'
        return bbox(image)
    monkeypatch.setattr(TileStore, '_alpha_bbox', staticmethod(owned_bbox))
    monkeypatch.setattr(ChapterDocument, 'to_dict', lambda *_a, **_k:
        pytest.fail('Whole chapter bake history'))
    monkeypatch.setattr(VectorDrawingObject, 'to_dict', lambda *_a, **_k:
        pytest.fail('Unrelated drawing bake history'))
    completed = []
    try:
        if action == 'rasterize':
            request_rasterize(canvas, 'object', raster.object_id, completed.append)
        else:
            request_apply_raster_modifiers(canvas, modifier.modifier_id, completed.append)
        assert completed == []
        wait_for(lambda: bool(completed))
        assert completed == [None]
        assert cold.residency.decodes == 0
        assert chapter.objects[unrelated.object_id] is unrelated
        assert len(canvas.command_stack._undo) == 1
        if action == 'rasterize':
            assert isinstance(chapter.objects[raster.object_id], ImageObject)
        else:
            assert chapter.objects[raster.object_id].modifier_ids == []
        canvas.command_stack.undo()
        assert chapter.objects[unrelated.object_id] is unrelated
        assert isinstance(chapter.objects[raster.object_id], RasterObject)
        assert chapter.objects[raster.object_id].modifier_ids == [modifier.modifier_id]
        restored = cold.tile(raster.object_id, (0, 0))
        np.testing.assert_array_equal(native_rgba_pixels(restored)[0], native_rgba_pixels(native)[0])
        assert restored.format() == native.format()
        canvas.command_stack.redo()
        assert chapter.objects[unrelated.object_id] is unrelated
    finally:
        consumers = getattr(canvas, '_scene_consumers', None)
        if consumers is not None:
            consumers.shutdown()
            consumers.executor.shutdown(wait=True, cancel_futures=True)
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas.deleteLater()
