"""Ordinary contact native output, durable exclusion and capture ownership.

Original drawing-reuse and transform/draft fixtures remain unchanged. These
tests use the real Raster entry/gate and independent exact output/cache owners.
"""
from copy import deepcopy
from dataclasses import replace
import time

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor

from comic_editor.core.models import BoundGeometry, HueSaturationLightnessModifier, ParameterMaskBinding, ToneMask
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.live_canvas_preview import LiveCanvasPreviewPolicy, live_canvas_policy
from comic_editor.render.outputs import _render_snapshot
from comic_editor.render.pixels import color_environment
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, RenderQuality, RenderRequest, RenderStatus
from comic_editor.ui import interactive_effects
from comic_editor.ui.cache_dependencies import RenderDependencies, cache_get, cache_put
from test_dirty_modifier_reuse import scene


def native_bytes(image):
    return (image.width(), image.height(), image.format().value, image.bytesPerLine(),
            image.devicePixelRatio(), bytes(image.colorSpace().iccProfile()), bytes(image.constBits()))


def freeze(canvas, document):
    capture = canvas._scene_snapshot_compiler.capture(canvas, document)
    while not capture.advance(.004):
        pass
    assert not capture.stale and capture.result.document == document
    snapshot = capture.result.finish_sources()
    assert snapshot.document.contact_only and snapshot.document.live_preview
    return snapshot


def finish_contact(canvas, qapp):
    gate = getattr(canvas, '_raster_tile_input', None)
    if gate is not None and not gate.closed:
        canvas._end_stroke()
    deadline = time.monotonic() + 5.
    while getattr(canvas, '_raster_tile_input', None) is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert getattr(canvas, '_raster_tile_input', None) is None
    assert not canvas._drawing and not canvas._render_document_state().contact_only


@pytest.mark.parametrize('target_kind', ['object', 'layer'])
def test_real_contact_native_masked_own_or_parent_effect_matches_exact_and_cannot_write_disk(
        scene, qapp, monkeypatch, tmp_path, target_kind):
    canvas, _, trace = scene
    if target_kind == 'layer':
        # Pages deliberately reject HSL; use the supported ordinary parent.
        parent = canvas.chapter.add_layer(trace.parent_layer_id, 'Contact native parent',
            bound=BoundGeometry.rectangle(0., 0., 384., 256.), index=0)
        parent.fill_color, parent.border_width = None, 0
        canvas.chapter.move_entity('object', trace.object_id, parent.layer_id, 0)
        assert trace.parent_layer_id == parent.layer_id and not parent.is_page
    target_id = trace.object_id if target_kind == 'object' else trace.parent_layer_id
    mask = ToneMask(name='Contact native intensity', saved=True)
    canvas.chapter.masks[mask.mask_id] = mask
    canvas.tiles.paint_dab(mask.mask_id, QPointF(192, 128), 500, QColor('#737373'))
    modifier = HueSaturationLightnessModifier(hue=35., saturation=-23., lightness=19., intensity=62.,
        parameter_masks={'intensity': ParameterMaskBinding(mask_id=mask.mask_id,
                                                           black_value=17., white_value=83.)})
    canvas.chapter.add_modifier(modifier, [(target_kind, target_id)])
    canvas._invalidate_scene_cache()
    assert not canvas._render_document_state().contact_only
    canvas._begin_stroke(QPointF(170, 100), 1.)
    canvas._continue_stroke(QPointF(210, 102), 1.)
    document = canvas._render_document_state()
    assert document.contact_only and document.live_preview
    assert canvas._raster_tile_input.current()
    snapshot = freeze(canvas, document)
    backend = DetachedSceneBackend(snapshot)
    backend.native_preview, backend.artwork_scale = True, 1.
    policy = LiveCanvasPreviewPolicy(document.identity, document.revision, 73, lambda: False)
    # Even an accidentally attached draft policy cannot change contact sampling.
    backend.live_canvas_preview_policy = policy
    request = RenderRequest((0., 0., 384., 256.), 1., (384, 256), ('detached-preview', 73),
                            document.revision, quality=RenderQuality.INTERACTIVE)
    assert not policy.matches(snapshot, document, request)
    backend.scene._live_canvas_preview_policy = policy
    assert live_canvas_policy(backend.scene) is None
    backend.scene._live_canvas_preview_policy = None
    independent = None
    try:
        def no_draft(*args, **kwargs):
            pytest.fail('An ordinary contact must keep native effect sampling')
        with monkeypatch.context() as control:
            control.setattr(interactive_effects, '_draft', no_draft)
            service = DocumentRenderService(backend)
            # Match the original native helpers' actual document admission.
            service.projection.revision = document.revision
            assert service.current(document, request)
            result = service.render_region(document, request)
        assert result.status is RenderStatus.PROVISIONAL and not result.exact
        assert not result.image.isNull() and result.image.devicePixelRatio() == 1.
        # This independent owner uses the ordinary exact/export route, resets
        # contact/live metadata and starts with no derived backend caches.
        expected = _render_snapshot(replace(snapshot, cache_spec=None), request.region)
        assert native_bytes(result.image) == native_bytes(expected)
        assert snapshot.document == document and document.contact_only

        exact_document = replace(document, live_preview=False, contact_only=False)
        independent = DetachedSceneBackend(replace(snapshot, document=exact_document, cache_spec=None))
        environment = (*RenderDependencies.environment(), color_environment(document.pixel_contract))
        directory = tmp_path / 'ordinary-exact-cache'
        backing = PersistentRenderCache(directory, contract=document.pixel_contract.signature,
                                        environment=environment)
        independent.scene._persistent_render_cache = backing
        key = ('contact-regression-native-stage', target_kind, target_id, modifier.modifier_id)
        with backing.record():
            cache_put(independent.scene, 'retained', key, expected)
            backing.drain()
        assert backing.has('retained', key), 'Positive ordinary exact recording control must exist'
        environment = tuple(backing.environment)
        independent.close()
        independent = None

        # Reopen the completed exact cache on the contact owner. Live contacts
        # may adopt valid memory results, but neither read nor write durable ones.
        live_backing = PersistentRenderCache(directory, contract=document.pixel_contract.signature,
                                             environment=environment)
        backend.scene._persistent_render_cache = live_backing
        # A reopened reader has no verified blobs yet. Complete the ordinary
        # read before the unchanged positive has() control; drain() already
        # published the writer's index, but has() is intentionally nonblocking.
        recorded = live_backing.lookup('retained', key, wait=True)
        assert recorded is not None
        assert native_bytes(recorded) == native_bytes(expected)
        assert live_backing.has('retained', key)
        assert cache_get(backend.scene, 'retained', key) is None
        new_key = (*key, 'current-contact')
        with live_backing.record():
            cache_put(backend.scene, 'retained', new_key, result.image)
            live_backing.drain()
        assert not live_backing.has('retained', new_key)
        assert live_backing.has('retained', key)
        assert not backend._record_allowed()
    finally:
        if independent is not None:
            independent.close()
        backend.close()
        finish_contact(canvas, qapp)


@pytest.mark.parametrize('transition', ['mask', 'transform', 'source', 'same_id_replacement'])
def test_positive_contact_capture_rejects_changed_mode_source_or_original_object(
        scene, qapp, monkeypatch, transition):
    canvas, _, trace = scene
    canvas._begin_stroke(QPointF(170, 100), 1.)
    document = canvas._render_document_state()
    assert document.contact_only and document.live_preview
    assert canvas._raster_tile_input.current()
    # A separately completed snapshot supplies a native COW ownership witness.
    frozen = freeze(canvas, document)
    old_source = {key: native_bytes(image) for key, image in frozen.tiles.object_tiles(trace.object_id).items()}
    assert old_source, 'The original current contact must really paint native pixels'
    capture = canvas._scene_snapshot_compiler.capture(canvas, document)
    assert not capture.advance(0.) and capture.result is None
    original_object = trace
    changes = pytest.MonkeyPatch()
    try:
        if transition == 'mask':
            mask = ToneMask(name='Actual mask-mode transition', saved=True)
            canvas.chapter.masks[mask.mask_id] = mask
            changes.setattr(canvas, 'active_tone_mask_id', mask.mask_id)
            assert canvas._document_projection.revision == document.revision
        elif transition == 'transform':
            changes.setattr(canvas, '_transform_preview_quad',
                                [(3., 4.), (387., 4.), (387., 260.), (3., 260.)])
            assert canvas._document_projection.revision == document.revision
        elif transition == 'same_id_replacement':
            replacement = deepcopy(trace)
            assert replacement.object_id == trace.object_id and replacement is not trace
            canvas.chapter.objects[trace.object_id] = replacement
            assert canvas._document_projection.revision == document.revision
            assert not canvas._raster_tile_input.current()
        else:
            canvas._continue_stroke(QPointF(210, 102), 1.)
            qapp.processEvents()
            assert canvas._document_projection.revision != document.revision
            assert {key: native_bytes(image) for key, image in canvas.tiles.object_tiles(trace.object_id).items()} != old_source
        if transition != 'source':
            assert not canvas._projection_contact_only()
        assert capture.advance(.004) and capture.stale and capture.result is None
        assert frozen.document.contact_only and frozen.document == document
        assert {key: native_bytes(image) for key, image in frozen.tiles.object_tiles(trace.object_id).items()} == old_source
    finally:
        canvas.chapter.objects[trace.object_id] = original_object
        changes.undo()
        finish_contact(canvas, qapp)
