"""Large native drawings acquire cold feedback for their exact edited view."""
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.models import BoundGeometry
from comic_editor.render.raster_feedback import prepare_raster_feedback
from comic_editor.render.raster_feedback_bootstrap import RasterFeedbackBootstrap, visible_source_keys
from comic_editor.ui.canvas import ToolKind
from test_cold_raster_contact_feedback import cold_canvas, send, wait_for
from test_fractional_raster_feedback_native import fixed_native_reference
from test_raster_contact_feedback import pixels, ready_paint, scene
from test_raster_contact_feedback_boundaries import begin, finish
from test_raster_feedback_bootstrap import bootstrap_for, complete_capture, drain_native


def large_native_source(canvas, selected, format=QImage.Format_ARGB32_Premultiplied):
    image = QImage(256, 256, format)
    image.fill(QColor.fromRgbF(.1837, .5139, .8271, .7213))
    keys = ((0, 0), (2, 0), *((64 + index, 0) for index in range(144)))
    for key in keys:
        canvas.tiles.set_tile(selected.object_id, key, image)
    return {key: QImage(image) for key in keys}


def configure(canvas, selected, mapping, zoom):
    canvas.chapter.width = 768
    canvas.setFixedSize(768, 128)
    canvas.center_x, canvas.center_y, canvas.scale = 384., 64., zoom
    page = canvas.chapter.layers[selected.parent_layer_id]
    page.bound = BoundGeometry.rectangle(8, 8, 752, 112)
    selected.interaction_rect = (0., 0., 768., 256.)
    if mapping == 'fractional':
        selected.x, selected.y = .37, .63
    elif mapping in {'affine', 'implicit-affine'}:
        transform = QTransform(1., .015, .08, .9, -12., 4.)
        selected.transform_quad = [transform.map(QPointF(x, y)).toTuple()
            for x, y in ((0, 0), (768, 0), (768, 256), (0, 256))]
        selected.transform_frame = None if mapping == 'implicit-affine' else selected.interaction_rect
    elif mapping == 'projective':
        selected.transform_frame = selected.interaction_rect
        selected.transform_quad = [(0.2, 2.3), (768.2, 2.3), (766.9, 238.7), (.2, 234.3)]
    back = canvas.chapter.objects[page.children[-1].entity_id]
    blue = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    blue.fill(QColor('blue'))
    for x in (1, 2):
        canvas.tiles.set_tile(back.object_id, (x, 0), blue)
    canvas._invalidate_scene_cache()


@pytest.mark.parametrize('mapping', ['plain', 'fractional', 'affine', 'projective'])
@pytest.mark.parametrize('zoom', [.3, .57, 1.])
@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied, QImage.Format_RGBA64_Premultiplied])
@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_large_global_source_freezes_only_native_view_and_matches_every_held_buffer(
        scene, mapping, zoom, format, tool, monkeypatch, qapp, wait_scene):
    canvas, selected, _front = scene
    before_sources = large_native_source(canvas, selected, format)
    configure(canvas, selected, mapping, zoom)
    canvas.set_tool(tool)
    wait_scene(canvas)
    controller = canvas._scene_controller
    controller.retire_feedback()
    # This test owns the bootstrap under qualification; ordinary completion
    # cannot produce its held pixels or overwrite the resource being checked.
    monkeypatch.setattr(controller.scheduler, 'submit', lambda *_: None)
    monkeypatch.setattr(controller, '_advance_bootstrap', lambda *_a, **_k: False)
    model_before = selected.to_dict()
    with monkeypatch.context() as no_gui_reads:
        no_gui_reads.setattr(canvas.tiles.residency, 'get', lambda *_a, **_k: pytest.fail('GUI source decode'))
        bootstrap = bootstrap_for(canvas, selected)
    assert not bootstrap.retired
    assert bootstrap.selected_source_keys is not None
    assert len(bootstrap.selected_source_keys) < 128
    assert (0, 0) in bootstrap.selected_source_keys and (2, 0) in bootstrap.selected_source_keys
    assert all(key[0] < 64 for key in bootstrap.selected_source_keys)
    assert selected.to_dict() == model_before
    original_changed = controller.changed
    def changed(change, **kwargs):
        bootstrap.changed(change)
        return original_changed(change, **kwargs)
    monkeypatch.setattr(controller, 'changed', changed)
    snapshot = complete_capture(bootstrap, canvas)
    assert set(snapshot.tiles._tiles[selected.object_id]) == set(bootstrap.selected_source_keys)
    assert snapshot.chapter.objects[selected.object_id].interaction_rect == selected.interaction_rect
    assert snapshot.chapter.objects[selected.object_id].transform_frame == selected.transform_frame
    assert snapshot.tiles._alpha_bounds[selected.object_id] == canvas.tiles._alpha_bounds[selected.object_id]
    # An uncropped provisional capture is a second control. Culling must produce
    # exactly the same native feedback as retaining the complete original map.
    with monkeypatch.context() as complete_source:
        complete_source.setattr('comic_editor.render.raster_feedback_bootstrap.visible_source_keys',
            lambda *_a, **_k: (tuple(canvas.tiles._tiles[selected.object_id]), bootstrap.source_mapping))
        full_owner = bootstrap_for(canvas, selected)
    full_snapshot = complete_capture(full_owner, canvas)
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared = worker.submit(prepare_raster_feedback, snapshot, selected.object_id,
            bootstrap.visible).result(timeout=10.)
        full_prepared = worker.submit(prepare_raster_feedback, full_snapshot, selected.object_id,
            bootstrap.visible).result(timeout=10.)
    assert prepared is not None
    assert full_prepared is not None
    controller.feedback = prepared
    controller.feedback_dirty = set()
    controller.feedback_patches.clear()
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(fixed_native_reference(canvas)))
    revision = canvas.command_stack.revision
    for index, x in enumerate((224., 250., 280., 320.)):
        point = canvas._raster_world_point(selected, QPointF(x, 64.))
        if index == 0:
            begin(canvas, tool, point)
        else:
            (canvas._continue_paint_brush if tool == ToolKind.BRUSH else canvas._continue_stroke)(point, .7)
        drain_native(canvas, qapp)
        controller.feedback_dirty = set(bootstrap.dirty)
        controller.feedback_patches.clear()
        assert canvas._drawing and canvas.command_stack.revision == revision
        actual = ready_paint(canvas)
        controller.feedback = full_prepared
        controller.feedback_patches.clear()
        full_actual = ready_paint(canvas)
        np.testing.assert_array_equal(pixels(actual), pixels(full_actual))
        controller.feedback = prepared
        controller.feedback_patches.clear()
        np.testing.assert_array_equal(pixels(actual), pixels(fixed_native_reference(canvas)))
        with monkeypatch.context() as no_gui_work:
            no_gui_work.setattr(canvas.tiles.residency, 'get', lambda *_a, **_k: pytest.fail('GUI source decode'))
            no_gui_work.setattr(canvas, '_render_scene_layers', lambda *_a, **_k: pytest.fail('GUI scene evaluation'))
            np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(actual))
    if tool != ToolKind.RASTER_ERASER:
        assert (1, 0) in bootstrap.dirty
        assert canvas.tiles.tile(selected.object_id, (1, 0)) is not None
        assert snapshot.tiles.tile(selected.object_id, (1, 0)) is None
    for key, before in before_sources.items():
        if key[0] >= 64:
            assert canvas.tiles.tile(selected.object_id, key) == before
    finish(canvas, tool)
    drain_native(canvas, qapp)
    assert canvas.command_stack.revision == revision + 1
    canvas.command_stack.undo()
    assert canvas.tiles.object_tiles(selected.object_id) == before_sources


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_actual_cold_mouse_contact_on_large_global_source_updates_while_held(
        cold_canvas, tool, wait_scene, monkeypatch):
    canvas, selected, _front = cold_canvas
    large_native_source(canvas, selected)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    before = ready_paint(canvas)
    controller = canvas._scene_controller
    monkeypatch.setattr(controller.scheduler, 'submit', lambda *_: None)
    canvas.set_selection('object', selected.object_id, activate_default_tool=False)
    canvas.set_tool(tool)
    revision = canvas.command_stack.revision
    send(canvas, 'press', 24.)
    for x in (32., 40., 72., 88., 100.):
        send(canvas, 'move', x)
    wait_for(lambda: controller.feedback is not None, canvas)
    held = ready_paint(canvas)
    assert held.pixelColor(80, 64) != before.pixelColor(80, 64)
    assert held.pixelColor(56, 64) == QColor('green')
    assert canvas._drawing and canvas.command_stack.revision == revision
    np.testing.assert_array_equal(pixels(held), pixels(fixed_native_reference(canvas)))
    assert controller.bootstrap_scheduler.submitted == 1
    send(canvas, 'release', 100.)
    wait_for(lambda: canvas.command_stack.revision == revision + 1)


def test_culled_implicit_frame_mapping_change_retires_owner_before_adoption(scene):
    canvas, selected, _front = scene
    large_native_source(canvas, selected)
    configure(canvas, selected, 'implicit-affine', 1.)
    bootstrap = bootstrap_for(canvas, selected)
    assert not bootstrap.retired
    selected.interaction_rect = (0., 0., 800., 256.)
    assert bootstrap.changed(ChangeSet(entities=(EntityChange(('object', selected.object_id),
        frozenset({'interaction_rect'})),), transient=True))
    assert bootstrap.advance(canvas._render_document_state(), seconds=0.)
    assert bootstrap.retired and bootstrap.snapshot is None


def test_missing_authored_frame_retains_canonical_feedback_rejection(scene):
    # A quad with no authored frame is an incomplete document transform, which
    # the regular feedback pipeline rejects before source/view preparation.
    canvas, selected, _front = scene
    large_native_source(canvas, selected)
    configure(canvas, selected, 'implicit-affine', .3)
    bootstrap = bootstrap_for(canvas, selected)
    snapshot = complete_capture(bootstrap, canvas)
    assert prepare_raster_feedback(snapshot, selected.object_id, bootstrap.visible) is None
