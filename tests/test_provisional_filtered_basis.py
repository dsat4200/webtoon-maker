"""An old displayed background stays explicit while native ink catches up.

The swap/context adapter below exercises the GUI ownership guards on a normal
QImage-only test device. Native buffers are compared with independently
evaluated ordinary scenes; the actual Windows GL harness checks real swaps.
"""
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
import time

import pytest
from PySide6.QtCore import QPointF, QRect
from PySide6.QtGui import QColor, QColorSpace, QImage, QPainter

from comic_editor.core.changes import ChangeSet, EntityChange, OrderChange
from comic_editor.core.models import BlurModifier, ChapterDocument
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tools import ToolKind
from comic_editor.render.outputs import capture_document
from comic_editor.render.device import DeviceImage
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, TileBatchPolicy
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls
from test_filtered_native_recipe import fixture as native_fixture, native
from test_filtered_raster_contact_feedback import begin, finish, move, source_pixels, world_point
from test_raster_contact_feedback import ready_paint


@pytest.fixture
def provisional_scene(qapp, monkeypatch, tmp_path, request):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    format = getattr(request, 'param', QImage.Format_ARGB32_Premultiplied)
    chapter, tiles, images, obj, _base, _request, _recipe = native_fixture(format, True, True)
    sibling = list(chapter.objects.values())[-1]
    blur = BlurModifier(strength=3.)
    chapter.add_modifier(blur, [('object', sibling.object_id)])
    for key, image in tiles.object_tiles(obj.object_id).items():
        image.setColorSpace(QColorSpace(QColorSpace.SRgb))
        tiles.set_tile(obj.object_id, key, image)
    # A real unchanged original backing remains byte-identical through the
    # contact and both history operations. Selected sources retain their native
    # byte/16-bit/float buffers instead of being converted for this fixture.
    original_path = tmp_path / 'original-sibling.png'
    assert tiles.object_tiles(sibling.object_id)[(0, 0)].save(str(original_path), 'PNG')
    sibling_source = tiles._tiles[sibling.object_id]
    sibling_source.register((0, 0), original_path)
    tiles.residency.remove(sibling_source, (0, 0))
    original_bytes = original_path.read_bytes()
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', predictive_ink=False,
        grid_overlay_visible=False, snap_to_grid=False))
    canvas.setFixedSize(512, 512)
    canvas.set_document(chapter, tiles, images)
    canvas.center_x = canvas.center_y = 256.
    canvas.scale = 1.
    canvas.primary_color = '#ff0099cc'
    canvas.settings.pencil_size_px = dict.fromkeys(('small', 'medium', 'large'), 37)
    canvas.settings.eraser_size_px = dict.fromkeys(('small', 'medium', 'large'), 16)
    canvas.set_selection('object', obj.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    context = SimpleNamespace(isValid=lambda: True)
    monkeypatch.setattr(canvas, 'context', lambda: context, raising=False)
    monkeypatch.setattr(canvas, 'isValid', lambda: True, raising=False)
    controls = ModifierControls(canvas)
    workers = []
    yield canvas, obj, blur, controls, context, original_path, original_bytes, workers
    state = canvas._scene_controller._filtered_state
    if state is not None:
        workers.append(state.worker)
    canvas._clear_detached_input_state()
    canvas._scene_controller.reset()
    for worker in set(workers):
        worker.close()
        worker.executor.shutdown(wait=True)
    for scheduler in (canvas._scene_controller.scheduler, canvas._scene_controller.bootstrap_scheduler):
        scheduler.close()
        scheduler.executor.shutdown(wait=True, cancel_futures=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    controls.deleteLater()
    canvas.deleteLater()


def observe_native_swap(canvas):
    ready_paint(canvas)
    controller = canvas._scene_controller
    state = controller._filtered_state
    assert state is not None
    assert not canvas._projection_frame_pending and not canvas._projection_provisional_visible
    controller._remember_filtered_presented_basis()
    assert state.presented_basis is not None
    return state


def test_presented_native_basis_has_one_deduplicated_presentation_charge(
        provisional_scene, wait_scene):
    canvas, _obj, _blur, _controls, _context, _path, _bytes, workers = provisional_scene
    wait_scene(canvas)
    state = observe_native_swap(canvas)
    workers.append(state.worker)
    base = state.presented_basis[1]
    storage = {tile.image.cacheKey(): tile.image.sizeInBytes()
        for _phase, tiles in base[1] for tile in tiles}
    assert state.presented_basis_bytes == sum(storage.values()) > 0
    assert isinstance(base[1], tuple) and all(isinstance(tiles, tuple) for _phase, tiles in base[1])
    continuity = canvas._scene_controller.raster_continuity
    # Shared native handles in another presentation do not count twice.
    continuity.batch = base[1]
    assert state._presentation_storage_bytes(base[1]) == state.presented_basis_bytes
    continuity.budget = state.presented_basis_bytes - 1
    assert state._presentation_storage_bytes(base[1]) is None
    assert not state.remember_presented(canvas._render_document_state(),
        canvas._scene_controller._filtered_requests, canvas._projection_completed_view,
        canvas._projection_completed_view[1], canvas._scene_controller.presentation_view())
    state.close()
    assert state.presented_basis is None and state.presented_basis_bytes == 0


def test_borrowed_native_views_charge_the_full_shared_producer_allocation(
        provisional_scene, wait_scene):
    canvas, _obj, _blur, _controls, _context, _path, _bytes, workers = provisional_scene
    wait_scene(canvas)
    state = observe_native_swap(canvas)
    workers.append(state.worker)
    class Owner:
        closed = False
        available = True
        def release(self, _token):
            pass
    owner = Owner()
    image = DeviceImage(owner, 77, ('native-budget-test',), 1024, 1024, 1, 0)
    views = (image.copy(QRect(0, 0, 260, 260)), image.copy(QRect(256, 0, 260, 260)))
    assert views[0].cacheKey() != views[1].cacheKey()
    template = state.presented_basis[1][1][0][1][0]
    batch = ((None, tuple(replace(template, image=view) for view in views)),)
    continuity = canvas._scene_controller.raster_continuity
    continuity.clear()
    assert state._presentation_storage_bytes(batch) == 1024 * 1024 * 16
    continuity.budget = 1024 * 1024 * 16 - 1
    assert state._presentation_storage_bytes(batch) is None
    for view in views:
        view.release()
    image.release()


def ordinary_tiles(canvas, requests, *, old_blur=None):
    chapter = ChapterDocument.from_dict(canvas.chapter.to_dict())
    if old_blur is not None:
        chapter.modifiers[old_blur[0]].strength = old_blur[1]
    snapshot = capture_document(chapter, canvas.tiles, canvas.images)
    backend = DetachedSceneBackend(snapshot)
    service = DocumentRenderService(backend)
    service.projection.revision = snapshot.document.revision
    try:
        result = service.render_tiles(snapshot.document, requests, TileBatchPolicy((256., 256.)))
        assert not result.error and all(exact for _image, exact in result.tiles.values())
        return {address: native(image) for address, (image, _exact) in result.tiles.items()}
    finally:
        backend.close()


def wait_provisional(canvas, state, qapp):
    deadline = time.monotonic() + 15.
    while time.monotonic() < deadline:
        ready_paint(canvas)
        qapp.processEvents()
        assert canvas._scene_controller._filtered_state is state
        assert not state.error, state.error
        if (state.provisional_source_completed_revision == canvas._document_projection.revision
                and not state.generations and not state.worker.busy):
            return
        time.sleep(.001)
    pytest.fail('Owned provisional source packet did not finish')


@pytest.mark.parametrize('provisional_scene', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied], indirect=True)
@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('handoff', [False, True])
def test_old_background_native_contacts_then_current_exact_and_undo(
        provisional_scene, tool, handoff, wait_scene, qapp, monkeypatch):
    canvas, obj, blur, controls, _context, path, file_bytes, workers = provisional_scene
    canvas.set_tool(tool)
    wait_scene(canvas)
    state = observe_native_swap(canvas)
    workers.append(state.worker)
    old_document = state.presented_basis[0]
    original_source = source_pixels(canvas, obj)
    original_model = canvas.chapter.to_dict()
    original_history = canvas.command_stack.revision
    old_recipe = state.prepared
    requests = canvas._scene_controller._filtered_requests
    submitted = []
    # Ordinary work remains requested; this local functional adapter defers its
    # execution while the separately owned native worker completes real ink.
    with monkeypatch.context() as patch:
        patch.setattr(canvas._scene_controller.scheduler, 'submit',
            lambda demand, **_options: submitted.append(demand))
        if handoff:
            sibling = next(item for item in canvas.chapter.objects.values()
                if blur.modifier_id in item.modifier_ids)
            canvas.set_selection('object', sibling.object_id)
            assert state.selection_handoff is not None and not state.provisional
        controls.set_parameter(blur.modifier_id, 'strength', 5., True)
        if handoff:
            assert state.selection_handoff is not None and state.selection_handoff['committed']
            assert not state.provisional_valid(canvas._render_document_state(),
                canvas._scene_controller.presentation_view())
            canvas.set_selection('object', obj.object_id)
            canvas.set_tool(tool)
            assert state.selection_handoff is None
        assert canvas._scene_controller._filtered_state is state and state.provisional
        assert state.prepared is old_recipe and state.completed_revision == old_document.revision
        assert state.provisional_valid(canvas._render_document_state(),
            canvas._scene_controller.presentation_view())
        for index, local in enumerate((QPointF(137., 113.), QPointF(253., 128.))):
            point = world_point(canvas, obj, local)
            begin(canvas, point, tool) if index == 0 else move(canvas, point, tool)
            wait_provisional(canvas, state, qapp)
            document = canvas._render_document_state()
            actual, count = canvas._scene_controller.filtered_batch(document, requests,
                canvas._projection_completed_view[1])
            assert count and canvas._projection_frame_pending and canvas._projection_provisional_visible
            assert not canvas._raster_feedback_contact_covered
            assert not state.covers(document, requests, canvas._projection_completed_view)
            assert state.completed_revision == old_document.revision < document.revision
            buffers = {tile.key[1]: native(tile.image) for _phase, tiles in actual for tile in tiles}
            assert buffers == ordinary_tiles(canvas, requests, old_blur=(blur.modifier_id, 3.))
            assert buffers != ordinary_tiles(canvas, requests)
            assert state.worker.owner.cache_spec is None and state.worker.owner.document.live_preview
        assert submitted, 'Provisional coverage must not suppress normal current work'
        assert source_pixels(canvas, obj) != original_source
        finish(canvas, tool)
        assert canvas._scene_controller._filtered_state is None and state.worker.stopped.is_set()
    wait_scene(canvas)
    assert canvas.command_stack.revision == original_history + 2
    assert not canvas._projection_frame_pending and not canvas._projection_provisional_visible
    exact = {tile.key[1]: native(tile.image)
        for _phase, tiles in canvas._projection_completed_view[1] for tile in tiles}
    assert exact == ordinary_tiles(canvas, requests)
    canvas.command_stack.undo()  # Actual native contact first.
    wait_scene(canvas)
    assert source_pixels(canvas, obj) == original_source
    canvas.command_stack.undo()  # Then the independent Blur transaction.
    wait_scene(canvas)
    assert canvas.chapter.to_dict() == original_model
    assert source_pixels(canvas, obj) == original_source
    exact = {tile.key[1]: native(tile.image)
        for _phase, tiles in canvas._projection_completed_view[1] for tile in tiles}
    assert exact == ordinary_tiles(canvas, requests)
    assert path.read_bytes() == file_bytes
    assert sha256(path.read_bytes()).digest() == sha256(file_bytes).digest()


@pytest.mark.parametrize('reason', ['request_before_change', 'swap_before_change',
    'request_since_swap', 'third_selection', 'source_before_change', 'source_after_commit',
    'configuration', 'selected_geometry', 'sibling_geometry', 'history', 'uncommitted_return',
    'second_parameter', 'wrong_modifier', 'filtered_reuse_while_away', 'wrong_feedback_target'])
def test_sibling_scalar_handoff_declines_intermediate_work_or_changed_ownership(
        provisional_scene, reason, wait_scene):
    canvas, obj, blur, controls, _context, _path, _bytes, workers = provisional_scene
    wait_scene(canvas)
    controller = canvas._scene_controller
    state = observe_native_swap(canvas)
    workers.append(state.worker)
    sibling = next(item for item in canvas.chapter.objects.values()
        if blur.modifier_id in item.modifier_ids)
    if reason == 'request_since_swap':
        ready_paint(canvas)  # A real document request, without a new swap witness.
    elif reason == 'wrong_feedback_target':
        controller.feedback_target = ''
    canvas.set_selection('object', sibling.object_id)
    if reason in {'request_since_swap', 'wrong_feedback_target'}:
        assert controller._filtered_state is None and state.worker.stopped.is_set()
        return
    assert state.selection_handoff is not None
    if reason == 'request_before_change':
        ready_paint(canvas)
    elif reason == 'swap_before_change':
        controller._remember_filtered_presented_basis()
    elif reason == 'third_selection':
        canvas.set_selection('layer', obj.parent_layer_id, activate_default_tool=False)
    elif reason == 'wrong_modifier':
        controls.set_parameter(obj.modifier_ids[-1], 'thickness', 13., True)
    else:
        if reason == 'configuration':
            canvas.chapter.background = '#fffefefe'
        elif reason == 'selected_geometry':
            obj.interaction_rect = (-1., -1., 600., 600.)
        elif reason == 'sibling_geometry':
            sibling.interaction_rect = (-1., -1., 600., 600.)
        elif reason == 'history':
            canvas._history_generation = getattr(canvas, '_history_generation', 0) + 1
        if reason == 'source_before_change':
            image = canvas.tiles.tile(obj.object_id, (0, 0))
            painter = QPainter(image)
            painter.fillRect(100, 100, 2, 2, QColor('#ff000000'))
            painter.end()
        controls.set_parameter(blur.modifier_id, 'strength', 5., reason != 'uncommitted_return')
        if reason == 'source_after_commit':
            image = canvas.tiles.tile(obj.object_id, (0, 0))
            painter = QPainter(image)
            painter.fillRect(100, 100, 2, 2, QColor('#ff000000'))
            painter.end()
        if reason == 'second_parameter':
            controls.set_parameter(blur.modifier_id, 'strength', 6., False)
        elif reason == 'filtered_reuse_while_away':
            batch, count = controller.filtered_batch(canvas._render_document_state(),
                controller._filtered_requests, canvas._projection_completed_view[1])
            assert not count
        else:
            canvas.set_selection('object', obj.object_id)
    assert controller._filtered_state is None and state.worker.stopped.is_set()


@pytest.mark.parametrize('reason', ['no_swap', 'pending', 'context', 'camera', 'history',
    'selected_filter', 'selected_frame', 'source', 'order', 'selected_parameter', 'second_parameter',
    'editor_scalar'])
def test_invalid_or_changed_presented_owner_cannot_park(
        provisional_scene, reason, wait_scene, monkeypatch):
    canvas, obj, blur, controls, context, _path, _bytes, workers = provisional_scene
    wait_scene(canvas)
    state = canvas._scene_controller._filtered_state
    workers.append(state.worker)
    if reason != 'no_swap':
        observe_native_swap(canvas)
    if reason == 'pending':
        state.presented_basis = None
        canvas._projection_frame_pending = True
        canvas._scene_controller._remember_filtered_presented_basis()
        assert state.presented_basis is None
    elif reason == 'context':
        monkeypatch.setattr(context, 'isValid', lambda: False)
    elif reason == 'camera':
        canvas.rotation += .5
    elif reason == 'history':
        canvas._history_generation = getattr(canvas, '_history_generation', 0) + 1
    elif reason == 'selected_filter':
        canvas.chapter.modifiers[obj.modifier_ids[-1]].thickness += 1.
    elif reason == 'selected_frame':
        obj.interaction_rect = (0., 0., 500., 500.)
    elif reason == 'source':
        # Unpublished mutation of a borrowed native buffer cannot hide behind
        # an unchanged raw generation table: version() observes its Qt COW key.
        image = canvas.tiles.tile(obj.object_id, (0, 0))
        painter = QPainter(image)
        painter.fillRect(100, 100, 2, 2, QColor('#ff000000'))
        painter.end()
    if reason == 'editor_scalar':
        controls.set_parameter(blur.modifier_id, 'strength', 5., True)
        assert state.provisional
        canvas._scene_controller.changed(ChangeSet((EntityChange(('layer', obj.parent_layer_id),
            frozenset({'name'})),), transient=True), action='transient')
    elif reason == 'order':
        change = ChangeSet(orders=(OrderChange('objects', ('a', 'b'), ('b', 'a')),))
        canvas._scene_controller.changed(change)
    elif reason == 'selected_parameter':
        controls.set_parameter(obj.modifier_ids[-1], 'thickness', 13., True)
    else:
        controls.set_parameter(blur.modifier_id, 'strength', 5., True)
        if reason == 'second_parameter':
            assert state.provisional
            controls.set_parameter(blur.modifier_id, 'strength', 6., False)
    assert canvas._scene_controller._filtered_state is None
    assert state.worker.stopped.is_set()


@pytest.mark.parametrize('reason', ['release', 'cancel', 'camera', 'document', 'source_frame'])
def test_parked_owner_retires_without_polling_stale_source_packets(
        provisional_scene, reason, wait_scene, monkeypatch):
    canvas, obj, blur, controls, _context, _path, _bytes, workers = provisional_scene
    wait_scene(canvas)
    state = observe_native_swap(canvas)
    workers.append(state.worker)
    controls.set_parameter(blur.modifier_id, 'strength', 5., True)
    assert state.provisional
    begin(canvas, world_point(canvas, obj, QPointF(137., 113.)), ToolKind.RASTER_PENCIL)
    state.provisional_contact_started = True
    if reason in {'release', 'cancel'}:
        canvas._raster_contact_active = False
    elif reason == 'camera':
        canvas.scale *= 1.1
    elif reason == 'document':
        canvas._history_generation = getattr(canvas, '_history_generation', 0) + 1
    else:
        old_bounds = canvas.entity_world_rect('object', obj.object_id).getRect()
        obj.interaction_rect = (-1., -1., 600., 600.)
        new_bounds = canvas.entity_world_rect('object', obj.object_id).getRect()
        canvas._publish_change_set(ChangeSet((EntityChange(('object', obj.object_id),
            frozenset({'interaction_rect'}), old_bounds, new_bounds),), transient=True))
    monkeypatch.setattr(state, 'advance', lambda *_a, **_k:
        pytest.fail('Retired provisional source packets were advanced'))
    canvas._scene_controller._check_provisional_filtered()
    assert canvas._scene_controller._filtered_state is None and state.worker.stopped.is_set()
    canvas._cancel_native_raster_stroke()
