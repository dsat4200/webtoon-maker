"""UNRUN R4-policy handoff tests; original R2 native cases stay preserved.

Ordinary primary actions and scheduler/paint are unchanged. The untimed oracle
explicitly declares a transient render of independently committed stored values.
That declaration never adds a committed transient evaluator to production.
"""
from copy import deepcopy
from dataclasses import replace
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import ParameterMaskBinding, ToneMask
from comic_editor.render.live_canvas_preview import LiveCanvasPreviewPolicy, live_canvas_policy
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, RenderQuality, RenderRequest, RenderStatus
from test_detached_owned_rig_preview import _start, _close, owned_rig_app, owned_rig_settings
from test_live_canvas_preview_policy import _make_scene, _replica, _native, _original_pose_policy_reference


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine()).copy()


def wait(canvas, app, *, live):
    surface = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    deadline = time.monotonic() + 25.
    while time.monotonic() < deadline:
        canvas.render(surface)
        app.processEvents()
        time.sleep(.002)
        controller = canvas._scene_controller
        document = canvas._render_document_state()
        assert document.live_preview is live
        assert not controller.error, controller.error
        if live:
            preview = controller.preview
            if (preview is not None and preview[0] == document
                    and preview[1].key == ('preview', controller.serial)
                    and controller.capture is None and not controller.scheduler.busy):
                canvas.render(surface)
                last = controller.release_handoff.last
                assert last and last.swapped and last.document == document and last.tile is preview[1]
                assert last.snapshot.state['_transform_preview_quad'] == canvas._transform_preview_quad
                assert preview[1].world_rect == QRectF(0., 0., 800., 800.)
                assert preview[1].image.size() == canvas.size()
                assert canvas._projection_provisional_visible and canvas._projection_frame_pending
                return QImage(preview[1].image)
        elif (controller.capture is None and not controller.scheduler.busy
                and controller.snapshot is not None and controller.snapshot.document == document
                and not canvas._projection_frame_pending):
            assert canvas._projection_presented_revision == document.revision
            return surface
    pytest.fail('Original current scene/presentation did not finish within existing25s')


def stored_model_draft(reference):
    """Cold originals + actual committed geometry under declared same policy.

    The actual stored model has no live geometry or effective attachment state.
    A RenderDocument declaration selects the same temporary operator sampling.
    This is an independent test oracle only, never an admission/production path.
    """
    assert not reference._projection_has_live_preview()
    assert not reference.images._decoded
    document = reference._render_document_state()
    capture = reference._scene_snapshot_compiler.capture(reference, document)
    while not capture.advance(.004):
        pass
    assert not capture.stale and capture.result.document == document
    assert not capture.result.state['_transform_preview_quad']
    assert not capture.result.state['_transform_start_quad']
    declared = replace(document, live_preview=True)
    snapshot = replace(capture.result, document=declared)
    backend = DetachedSceneBackend(snapshot)
    backend.native_preview, backend.artwork_scale = True, 1.
    backend.scene._effect_preview_channel = 'canvas'
    backend.live_canvas_preview_policy = LiveCanvasPreviewPolicy(declared.identity, declared.revision, 117, lambda: False)
    service = DocumentRenderService(backend)
    service.projection.revision = declared.revision
    request = RenderRequest((0., 0., 800., 800.), 1., (800, 800), ('detached-preview', 117),
        declared.revision, quality=RenderQuality.INTERACTIVE)
    try:
        assert not backend.scene._modifier_render_cache and not backend.scene._modifier_source_cache
        assert not backend.scene.images._decoded
        result = service.render_region(declared, request)
        assert result.status is RenderStatus.PROVISIONAL and not result.exact and not result.image.isNull()
        assert result.image.size().toTuple() == (800, 800)
        assert result.image.format() == document.pixel_contract.image_format
        assert backend.scene.images._decoded
        assert backend.scene._live_canvas_preview_policy is None  # capture finally restores it
        return QImage(result.image)
    finally:
        backend.close()


def cold_commit(primary, before, object_id, mode):
    reference = _replica(primary, before, object_id)
    try:
        assert not reference.images._decoded
        _start(reference, object_id, mode)
        reference._tool_release()
        assert len(reference.command_stack._undo) == 1
        assert reference.command_stack.top_undo_command.label == 'Transform image'
        assert not reference._projection_has_live_preview()
        model = deepcopy(reference.chapter.to_dict())
        assert model != before
        draft = stored_model_draft(reference)
        assert not reference.images._decoded  # detached oracle did not warm its owner
        native = _native(reference)
        return draft, native, model
    finally:
        _close(reference)


@pytest.mark.parametrize('kind', ['deform', 'twirl', 'mirror', 'radial'])
@pytest.mark.parametrize('mode', ['translate', 'scale', 'warp'])
def test_actual_r4_live_final_pose_handoff_equals_cold_stored_policy_then_exact(owned_rig_app, kind, mode):
    canvas, object_id, _rig_id = _make_scene(kind, mode, False)
    try:
        before = deepcopy(canvas.chapter.to_dict())
        prior, revision = tuple(canvas.command_stack._undo), canvas.command_stack.revision
        initial_native = _native(canvas)
        initial_draft = _original_pose_policy_reference(canvas)
        wait(canvas, owned_rig_app, live=False)
        _start(canvas, object_id, mode)
        final_pose = deepcopy(canvas._transform_preview_quad)
        live = wait(canvas, owned_rig_app, live=True)
        assert np.count_nonzero(pixels(live) != pixels(initial_draft)) > 0
        draft, native, committed = cold_commit(canvas, before, object_id, mode)
        np.testing.assert_array_equal(pixels(live), pixels(draft))
        assert np.count_nonzero(pixels(native) != pixels(initial_native)) > 0
        canvas._tool_release()
        controller = canvas._scene_controller
        ticket = controller.release_handoff.ticket
        assert ticket is not None, controller.release_handoff.reason
        assert canvas.chapter.to_dict() == committed
        assert canvas.chapter.objects[object_id].transform_quad == final_pose
        assert canvas.command_stack.revision == revision + 1
        assert len(canvas.command_stack._undo) == len(prior) + 1
        assert all(a is b for a,b in zip(prior, canvas.command_stack._undo[:-1]))
        own = canvas.command_stack.top_undo_command
        assert own.label == 'Transform image'
        previous_presented = canvas._projection_presented_revision
        first = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        canvas.render(first)  # first original committed paint; no event pumping
        document = canvas._render_document_state()
        assert not document.live_preview and controller.desired[0] == document
        assert controller.preview is ticket.presentation
        assert ticket.presentation[0] == document and ticket.presentation[1].key == ('preview', controller.serial)
        assert ticket.source_document.live_preview and ticket.source_serial != controller.serial
        assert ticket.source_request.quality is RenderQuality.INTERACTIVE
        assert canvas._projection_frame_pending and canvas._projection_provisional_visible
        assert canvas._document_presentation_stats.tiles > 0
        assert canvas._projection_presented_revision == previous_presented != document.revision
        np.testing.assert_array_equal(pixels(ticket.presentation[1].image), pixels(draft))
        requests = canvas._document_projection.requests(canvas.visible_document_rect(), 1.)
        assert len(canvas._document_projection.ready(requests, configuration=(*document.configuration, None))) < len(requests)
        assert not hasattr(ticket, 'cache_state')
        assert controller._preview_snapshot is None and controller._preview_request is None
        wait(canvas, owned_rig_app, live=False)
        assert controller.release_handoff.ticket is None
        np.testing.assert_array_equal(pixels(_native(canvas)), pixels(native))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        assert controller.release_handoff.ticket is None
        np.testing.assert_array_equal(pixels(_native(canvas)), pixels(initial_native))
        canvas.command_stack.redo()
        assert canvas.command_stack.top_undo_command is own and canvas.chapter.to_dict() == committed
        np.testing.assert_array_equal(pixels(_native(canvas)), pixels(native))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before
        assert controller.release_handoff.validation_summary
    finally:
        _close(canvas)


def test_r4_rapid_unpainted_final_pose_is_not_relabelled_current(owned_rig_app):
    canvas, object_id, _ = _make_scene('twirl', 'warp', False)
    reference = None
    try:
        before = deepcopy(canvas.chapter.to_dict())
        wait(canvas, owned_rig_app, live=False)
        _, move = _start(canvas, object_id, 'warp')
        painted = wait(canvas, owned_rig_app, live=True)
        old_pose = deepcopy(canvas._transform_preview_quad)
        canvas._tool_move(canvas.document_to_widget(move + QPointF(45, 31)), 1.)
        new_pose = deepcopy(canvas._transform_preview_quad)
        assert new_pose != old_pose
        canvas._tool_release()
        assert canvas._scene_controller.release_handoff.ticket is None
        assert canvas.chapter.objects[object_id].transform_quad == new_pose
        committed = deepcopy(canvas.chapter.to_dict())
        assert committed != before
        reference = _replica(canvas, committed, object_id)
        fresh_draft = stored_model_draft(reference)
        assert np.count_nonzero(pixels(fresh_draft) != pixels(painted)) > 0
        np.testing.assert_array_equal(pixels(_native(canvas)), pixels(_native(reference)))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before
    finally:
        if reference is not None:
            _close(reference)
        _close(canvas)


@pytest.mark.parametrize('mutation', ['native_mask', 'native_decoded_qt_mutator'])
def test_r4_painted_draft_does_not_hide_unannounced_native_source_change(owned_rig_app, mutation):
    canvas, object_id, rig_id = _make_scene('twirl', 'scale', False)
    try:
        if mutation == 'native_mask':
            mask = ToneMask(name='Unannounced native ownership control')
            canvas.chapter.masks[mask.mask_id] = mask
            canvas.chapter.modifiers[rig_id].parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 15., 90.)
            canvas.tiles.paint_dab(mask.mask_id, QPointF(400, 346), 300, QColor('#dddddd'))
            canvas._scene_snapshot_compiler.invalidate()
        wait(canvas, owned_rig_app, live=False)
        _start(canvas, object_id, 'scale')
        wait(canvas, owned_rig_app, live=True)
        if mutation == 'native_mask':
            canvas.tiles.paint_dab(mask.mask_id, QPointF(400, 346), 80, QColor('#111111'))
        else:
            canvas.images._decoded[object_id].fill(QColor('#ee00aa'))
        canvas._tool_release()
        assert canvas._scene_controller.release_handoff.ticket is None
    finally:
        _close(canvas)


@pytest.mark.parametrize('action', ['undo', 'camera'])
def test_r4_pending_presentation_retires_before_changed_history_or_view(owned_rig_app, action):
    canvas, object_id, _ = _make_scene('deform', 'scale', False)
    try:
        before = deepcopy(canvas.chapter.to_dict())
        wait(canvas, owned_rig_app, live=False)
        _start(canvas, object_id, 'scale')
        wait(canvas, owned_rig_app, live=True)
        canvas._tool_release()
        controller = canvas._scene_controller
        assert controller.release_handoff.ticket is not None, controller.release_handoff.reason
        if action == 'undo':
            canvas.command_stack.undo()
            assert canvas.chapter.to_dict() == before
        else:
            canvas.center_y += 17
        surface = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        canvas.render(surface)
        assert controller.release_handoff.ticket is None
    finally:
        _close(canvas)
