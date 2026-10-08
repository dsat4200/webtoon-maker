"""Unrun additive HSL-HSL-Outline Raster regressions for private R4 drafts.

Complete native captures and dense mask preparation remain uncapped. These
checks bound only the existing temporary generic operator input. Original R4
60 cases and original incoming tests remain separate and unchanged.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, QBuffer, QByteArray, QIODevice, QTimer, Qt
from PySide6.QtGui import QImage, QColor, QPainter

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (BoundGeometry, ChapterDocument, RasterObject,
    HueSaturationLightnessModifier, OutlineModifier, ParameterMaskBinding)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.live_canvas_preview import LiveCanvasPreviewPolicy, live_canvas_policy
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, RenderQuality, RenderRequest, RenderStatus
from comic_editor.ui.canvas import RasterCanvasWidget, ToolKind


def _close(canvas):
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
    consumers = getattr(canvas, "_scene_consumers", None)
    if consumers is not None:
        consumers.shutdown()
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    for timer in canvas.findChildren(QTimer):
        timer.stop()
    canvas.close()
    canvas.deleteLater()

def _encoded_pattern():
    image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#c0d0e0"))
    painter = QPainter(image)
    for y in range(0, 128, 16):
        for x in range(0, 128, 16):
            if (x // 16 + y // 16) % 2:
                painter.fillRect(x, y, 16, 16, QColor("#ff6c20"))
    painter.fillRect(13, 19, 25, 31, QColor("#143bb5"))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.WriteOnly) and image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)

def _replica(canvas, model, object_id):
    images = canvas.images.clone()
    assert not images._decoded
    tiles = TileStore()
    # Independent native scalar/source pixels, never derived preview caches.
    for owner in canvas.tiles._tiles:
        for address, image in canvas.tiles.object_tiles(owner).items():
            tiles.set_tile(owner, address, image.copy())
    replica = RasterCanvasWidget(EditorSettings(**asdict(canvas.settings)))
    replica.setUpdatesEnabled(False)
    replica.resize(800, 800)
    replica.set_document(ChapterDocument.from_dict(deepcopy(model)), tiles, images)
    replica.center_x = replica.center_y = 400.
    replica.scale, replica.rotation = 1., 0.
    replica.set_selection("object", object_id, activate_default_tool=False)
    replica.set_tool(ToolKind.TRANSFORM)
    assert replica.selected_object_id == object_id
    assert not replica._modifier_render_cache and not replica._modifier_source_cache
    return replica

def _wait(canvas, qapp, *, live):
    surface = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        canvas.render(surface)
        qapp.processEvents()
        time.sleep(.002)
        controller = canvas._scene_controller
        document = canvas._render_document_state()
        assert document.live_preview is live
        assert not controller.error, controller.error
        if live:
            preview = controller.preview
            if (preview is not None and preview[0] == document
                    and preview[1].key == ("preview", controller.serial)):
                canvas.render(surface)
                assert canvas._projection_provisional_visible and canvas._projection_frame_pending
                assert preview[1].image.format() == document.pixel_contract.image_format
                return QImage(preview[1].image)
        elif (controller.capture is None and not controller.scheduler.busy
                and controller.snapshot is not None and controller.snapshot.document == document
                and not canvas._projection_frame_pending):
            assert canvas._projection_presented_revision == document.revision
            return surface
    pytest.fail("Actual SceneController supplied no matching current artwork")

def _native(canvas):
    document = canvas._render_document_state()
    assert not document.live_preview
    capture = canvas._scene_snapshot_compiler.capture(canvas, document)
    while not capture.advance(.004):
        pass
    assert not capture.stale and capture.result.document == document
    backend = DetachedSceneBackend(capture.result)
    service = DocumentRenderService(backend)
    service.projection.revision = document.revision
    request = RenderRequest((0., 0., 800., 800.), 1., (800, 800), ("independent-native",),
                            document.revision, quality=RenderQuality.EXACT)
    try:
        assert not backend.scene._modifier_render_cache
        result = service.render_region(document, request)
        assert result.status is RenderStatus.EXACT and result.exact
        assert backend.scene._vector_render_scale_override == 1.
        assert live_canvas_policy(backend.scene) is None
        assert result.image.format() == document.pixel_contract.image_format
        return QImage(result.image)
    finally:
        backend.close()

def _original_pose_policy_reference(canvas):
    """Independent private draft of the unchanged pose, explicitly declared.

    This is an untimed oracle fixture, not an editor gesture or hidden warmup.
    Its snapshot retains native originals and has no derived effect caches.
    """
    document = canvas._render_document_state()
    assert not document.live_preview
    capture = canvas._scene_snapshot_compiler.capture(canvas, document)
    while not capture.advance(.004):
        pass
    assert not capture.stale and capture.result.document == document
    declared = replace(document, live_preview=True)
    snapshot = replace(capture.result, document=declared)
    backend = DetachedSceneBackend(snapshot)
    backend.native_preview, backend.artwork_scale = True, 1.
    backend.live_canvas_preview_policy = LiveCanvasPreviewPolicy(declared.identity, declared.revision, 97, lambda: False)
    service = DocumentRenderService(backend)
    service.projection.revision = declared.revision
    request = RenderRequest((0., 0., 800., 800.), 1., (800, 800), ("detached-preview", 97),
        declared.revision, quality=RenderQuality.INTERACTIVE)
    try:
        assert not backend.scene._modifier_render_cache and not backend.scene._modifier_source_cache
        result = service.render_region(declared, request)
        assert result.status is RenderStatus.PROVISIONAL and not result.image.isNull()
        assert not result.exact and result.image.format() == declared.pixel_contract.image_format
        return QImage(result.image)
    finally:
        backend.close()


_MASK_RECORD = {'id': '44b5092dd9c448cca26161d2537a7675', 'name': '', 'saved': False, 'contributors': [], 'revision': 12, 'gradient': {'id': 'e1e060557aab48f9b19832504642ee7a', 'type': 'gradient', 'name': 'Mask Gradient', 'custom_name': False, 'parent_layer_id': '', 'position': [0.0, 0.0], 'visible': True, 'opacity': 1.0, 'show_on_top': False, 'blend_mode': 'normal', 'mask_only': True, 'fill_reference': False, 'opacity_locked': True, 'geometry_reference': 'direct', 'ignore_parent_mask': False, 'underlay_opacity': 0.0, 'modifier_ids': [], 'opacity_mask': None, 'gradient_type': 'color_fill', 'field_type': 'line', 'line_field': {'geometry': {'type': 'path', 'closed': False, 'primitive': 'custom', 'nodes': [{'id': 'b0bb68b0cddf4a1faecb18f773bcf15a', 'position': [466.68111425060033, 24398.508141663413], 'point_type': 'vector', 'incoming': None, 'outgoing': None, 'handles_locked': True, 'roundness': 0.0, 'roundness_enabled': False, 'width_multiplier': 1.0, 'outline_multiplier': 1.0, 'outline_enabled': True}, {'id': 'd719531c533e4832b413ffb273f97d21', 'position': [498.1427624023263, 26467.98544231027], 'point_type': 'vector', 'incoming': None, 'outgoing': None, 'handles_locked': True, 'roundness': 0.0, 'roundness_enabled': False, 'width_multiplier': 1.0, 'outline_multiplier': 1.0, 'outline_enabled': True}], 'additional_contours': []}, 'direction_mode': 'parallel', 'reverse_direction': False, 'perpendicular_distance': 120.0}, 'radial_field': {'origin': [540.0, 540.0], 'radii': [270.0, 270.0], 'rotation': 0.0, 'ellipse_enabled': False, 'center_auto': True, 'manual_center': None, 'reverse_direction': False, 'uniform': False, 'distance': 120.0}, 'shape_field': {'center_auto': True, 'manual_center': None, 'reverse_direction': False, 'uniform': False, 'distance': 120.0}, 'gradient_revision': 22, 'ramp': {'value_type': 'color', 'interpolation': 'linear', 'stops': [{'id': '62944128996c4f7682301e25e12c9f09', 'position': 0.39952718676122934, 'color': '#00FFFFFF'}, {'id': 'c318cbf5dba04540bf285c77bce2e57c', 'position': 1.0, 'color': '#FF000000'}]}, 'loaded_preset_id': '', 'gradient_shape': 'circular'}, 'limited_gradients': [], 'paint_has_subtractions': False, 'paint_offset': [0.0, 0.0]}



def _mask_fixture_context(chapter, object_id):
    obj = chapter.objects[object_id]
    assert len(obj.modifier_ids) == 3
    effect = chapter.modifiers[obj.modifier_ids[1]]
    assert effect.modifier_type == 'hsl' and 'intensity' in effect.parameter_masks
    binding = effect.parameter_masks['intensity']
    assert binding.mask_id == _MASK_RECORD['id'] and binding.mask_id in chapter.masks
    mask = chapter.masks[binding.mask_id]
    assert mask.saved is False and mask.gradient is not None and mask.contributors == []
    assert binding.black_value == 0. and binding.white_value == 88.7878787878788
    assert binding.mask_id in chapter.referenced_mask_ids()
    return {'dimensions': (chapter.width, chapter.height),
        'object_modifier_ids': tuple(obj.modifier_ids),
        'binding': binding.to_dict(), 'mask': mask.to_dict()}


def _native_sources(canvas):
    return {owner: {address: (image.format().value, image.width(), image.height(),
        image.bytesPerLine(), bytes(image.colorSpace().iccProfile()), bytes(image.constBits()))
        for address, image in canvas.tiles.object_tiles(owner).items()}
        for owner in canvas.tiles._tiles}


def _raster_scene(mode, masked, parent_kind):
    chapter = ChapterDocument(width=1080, height=800)
    page = chapter.add_page('Private Raster', BoundGeometry.rectangle(0, 0, 800, 800))
    page.fill_color, page.border_width = None, 0
    parent = chapter.add_layer(page.layer_id, 'Ordinary mapped parent')
    if parent_kind == 'affine':
        parent.transform_frame = (0., 0., 800., 800.)
        parent.transform_quad = [(18., 24.), (770., 39.), (755., 774.), (3., 759.)]
    obj = chapter.add_object(parent.layer_id, RasterObject(x=80., y=90.,
        opacity=.54, opacity_locked=False, interaction_rect=(0., 0., 640., 512.)))
    effects = [HueSaturationLightnessModifier(hue=40.),
        HueSaturationLightnessModifier(hue=51., saturation=98., lightness=-66.),
        OutlineModifier(thickness=12., color='#FFFFFFFF')]
    if masked:
        raw = deepcopy(_MASK_RECORD)
        raw['gradient']['line_field']['geometry']['nodes'][0]['position'] = [410., 130.]
        raw['gradient']['line_field']['geometry']['nodes'][1]['position'] = [410., 590.]
        # Install the actual target effects and binding before the first
        # deserialization. An unsaved mask is retained only while referenced.
        effects[1].parameter_masks['intensity'] = ParameterMaskBinding(
            mask_id=raw['id'], black_value=0., white_value=88.7878787878788)
    for effect in effects:
        effect.validate()
        chapter.add_modifier(effect, [('object', obj.object_id)])
    if masked:
        model = chapter.to_dict()
        model['masks'].append(raw)
        chapter = ChapterDocument.from_dict(model)
        obj = chapter.objects[obj.object_id]
        _mask_fixture_context(chapter, obj.object_id)
    tiles = TileStore()
    pattern = QImage.fromData(_encoded_pattern(), 'PNG').scaled(640, 512)
    assert not pattern.isNull() and pattern.depth() == 32
    for y in range(2):
        for x in range(3):
            tile = pattern.copy(x*256, y*256, 256, 256)
            assert tile.width() == tile.height() == 256
            tiles.set_tile(obj.object_id, (x, y), tile)
    canvas = RasterCanvasWidget(EditorSettings(canvas_renderer='raster',
        snap_to_grid=False, grid_overlay_visible=False,
        transform_mode='free' if mode == 'warp' else 'uniform'))
    canvas.setUpdatesEnabled(False)
    canvas.resize(800, 800)
    canvas.set_document(chapter, tiles, ImageStore())
    canvas.center_x = canvas.center_y = 400.
    canvas.scale, canvas.rotation = 1., 0.
    canvas.set_selection('object', obj.object_id, activate_default_tool=False)
    assert canvas.selected_object_id == obj.object_id
    canvas.set_tool(ToolKind.TRANSFORM)
    assert len(tiles.object_tiles(obj.object_id)) == 6 and tiles.tile_size == 256
    assert [chapter.modifiers[mid].modifier_type for mid in obj.modifier_ids] == ['hsl', 'hsl', 'outline']
    if parent_kind == 'affine':
        mapping = canvas.layer_world_transform(obj.parent_layer_id)
        assert mapping.isAffine() and not mapping.isIdentity()
    return canvas, obj.object_id


def _start_raster(canvas, object_id, mode):
    quad = canvas.object_world_quad(object_id)
    assert len(quad) == 4
    if mode == 'translate':
        u, v = .31, .37
        weights = ((1-u)*(1-v), u*(1-v), u*v, (1-u)*v)
        press = QPointF(sum(quad[i][0]*w for i, w in enumerate(weights)),
                        sum(quad[i][1]*w for i, w in enumerate(weights)))
        expected = ('translate', None)
    else:
        press, expected = QPointF(*quad[2]), ('handle', 2)
    assert canvas._selected_object_transform_hit(canvas.chapter.objects[object_id], quad, press) == expected
    canvas._tool_press(canvas.document_to_widget(press), 1.)
    canvas._tool_move(canvas.document_to_widget(press + QPointF(31., 23.)), 1.)
    assert canvas._transform_drag_mode == expected[0]
    assert canvas._pending_raster_transform_press is None and canvas._geometry_transform_target is None
    assert canvas._transform_preview_quad and canvas._transform_preview_quad != canvas._transform_start_quad
    return press


@pytest.mark.parametrize('mode', ['translate', 'scale', 'warp'])
@pytest.mark.parametrize('masked', [False, True])
@pytest.mark.parametrize('parent_kind', ['identity', 'affine'])
def test_actual_hsl_hsl_outline_raster_current_draft_cold_native_release_undo(qapp, monkeypatch, mode, masked, parent_kind):
    from comic_editor.ui import interactive_effects
    canvas, object_id = _raster_scene(mode, masked, parent_kind)
    cold = native = original = unmasked = None
    try:
        before = deepcopy(canvas.chapter.to_dict())
        # A cold ordinary load must preserve the complete normalized fixture.
        normalized = ChapterDocument.from_dict(deepcopy(before))
        assert normalized.to_dict() == before
        if masked:
            primary_mask_context = _mask_fixture_context(canvas.chapter, object_id)
            assert _mask_fixture_context(normalized, object_id) == primary_mask_context
        prior, revision = tuple(canvas.command_stack._undo), canvas.command_stack.revision
        source = _native_sources(canvas)
        contract = canvas.chapter.pixel_contract.signature
        original_draft = _original_pose_policy_reference(canvas)
        calls, shortcut_calls, draft_calls = [], [], []
        original_stack = interactive_effects.render_interactive_stack
        original_draft_function = interactive_effects._draft
        def stack(owner, image, modifiers, *args, **kwargs):
            policy = live_canvas_policy(owner)
            result = original_stack(owner, image, modifiers, *args, **kwargs)
            shortcut_calls.append((policy is not None, tuple(m.modifier_type for m in modifiers), result[1],
                image.width(), image.height(), image.devicePixelRatio()))
            return result
        def draft(image, modifiers, origin, fields, mapping, nearest, cancelled=None):
            result = original_draft_function(image, modifiers, origin, fields, mapping, nearest, cancelled)
            draft_calls.append((image.width(), image.height(), result.width(), result.height(),
                tuple(m.modifier_type for m in modifiers), callable(cancelled),
                [(tuple(value.shape), float(np.min(value)), float(np.max(value))) for value in fields.values()]))
            return result
        monkeypatch.setattr(interactive_effects, 'render_interactive_stack', stack)
        monkeypatch.setattr(interactive_effects, '_draft', draft)
        original_apply = interactive_effects.apply_modifier_stack
        def apply(image, modifiers, origin, fields=None, **kwargs):
            record = (image.width(), image.height(), tuple(m.modifier_type for m in modifiers),
                {key: value.shape for key, value in (fields or {}).items()}, callable(kwargs.get('cancelled')))
            calls.append(record)
            return original_apply(image, modifiers, origin, fields, **kwargs)
        monkeypatch.setattr(interactive_effects, 'apply_modifier_stack', apply)
        press = _start_raster(canvas, object_id, mode)
        first = _wait(canvas, qapp, live=True)
        assert bytes(first.constBits()) != bytes(original_draft.constBits())
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        cold = _replica(canvas, before, object_id)
        assert cold.chapter.to_dict() == before
        if masked:
            assert _mask_fixture_context(cold.chapter, object_id) == primary_mask_context
        cold_press = _start_raster(cold, object_id, mode)
        assert press == cold_press
        expected = _wait(cold, qapp, live=True)
        np.testing.assert_array_equal(np.frombuffer(first.constBits(), np.uint8), np.frombuffer(expected.constBits(), np.uint8))
        for owner in (canvas, cold):
            owner._tool_move(owner.document_to_widget(press + QPointF(47., 34.)), 1.)
        second = _wait(canvas, qapp, live=True)
        assert bytes(second.constBits()) != bytes(first.constBits())
        expected_second = _wait(cold, qapp, live=True)
        np.testing.assert_array_equal(np.frombuffer(second.constBits(), np.uint8), np.frombuffer(expected_second.constBits(), np.uint8))
        assert calls and all(types == ('hsl', 'hsl', 'outline') for _w, _h, types, _fields, _cancel in calls)
        assert all(max(w, h) <= 256 and w*h <= 33217 and cancelled for w, h, _types, _fields, cancelled in calls)
        assert all(all(tuple(shape) == (h, w) for shape in fields.values()) for w, h, _types, fields, _cancel in calls)
        assert any(bool(fields) for _w, _h, _types, fields, _cancel in calls) is masked
        assert shortcut_calls and all(policy and types == ('hsl', 'hsl', 'outline') and provisional
            and max(w, h) > 256 and dpr == 1. for policy, types, provisional, w, h, dpr in shortcut_calls)
        assert draft_calls and len(draft_calls) == len(shortcut_calls)
        assert all(max(iw, ih) > 256 and max(ow, oh) <= 256 and ow*oh <= 33217
            and types == ('hsl', 'hsl', 'outline') and cancelled
            for iw, ih, ow, oh, types, cancelled, _fields in draft_calls)
        assert all(all(shape == (ih, iw) for shape, _minimum, _maximum in fields)
            for iw, ih, _ow, _oh, _types, _cancel, fields in draft_calls)
        if masked:
            assert all(fields and any(maximum-minimum > .1 for _shape, minimum, maximum in fields)
                for _iw, _ih, _ow, _oh, _types, _cancel, fields in draft_calls)
            no_mask = deepcopy(before)
            for modifier in no_mask['modifiers']:
                if modifier['type'] == 'hsl':
                    modifier['parameter_masks'] = {}
            unmasked = _replica(canvas, no_mask, object_id)
            _start_raster(unmasked, object_id, mode)
            unmasked._tool_move(unmasked.document_to_widget(press + QPointF(47., 34.)), 1.)
            unmasked_live = _wait(unmasked, qapp, live=True)
            assert bytes(unmasked_live.constBits()) != bytes(second.constBits())
        assert _native_sources(canvas) == source and canvas.chapter.pixel_contract.signature == contract
        monkeypatch.setattr(interactive_effects, 'apply_modifier_stack', original_apply)
        monkeypatch.setattr(interactive_effects, 'render_interactive_stack', original_stack)
        monkeypatch.setattr(interactive_effects, '_draft', original_draft_function)
        canvas._tool_release()
        cold._tool_release()
        committed = deepcopy(canvas.chapter.to_dict())
        assert committed != before and committed == cold.chapter.to_dict()
        assert len(canvas.command_stack._undo) == len(prior)+1 and tuple(canvas.command_stack._undo[:-1]) == prior
        assert canvas.command_stack.revision == revision+1
        own = canvas.command_stack.top_undo_command
        assert own.label == 'Transform raster'
        assert canvas._transform_preview_quad is None and canvas._transform_drag_mode is None
        _wait(canvas, qapp, live=False)
        native, original = _replica(canvas, committed, object_id), _replica(canvas, before, object_id)
        committed_native = _native(canvas)
        expected_committed_native = _native(native)
        np.testing.assert_array_equal(np.frombuffer(committed_native.constBits(), np.uint8),
            np.frombuffer(expected_committed_native.constBits(), np.uint8))
        assert _native_sources(canvas) == source and canvas.chapter.pixel_contract.signature == contract
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        undo_native = _native(canvas)
        expected_undo_native = _native(original)
        np.testing.assert_array_equal(np.frombuffer(undo_native.constBits(), np.uint8), np.frombuffer(expected_undo_native.constBits(), np.uint8))
        canvas.command_stack.redo()
        assert canvas.chapter.to_dict() == committed and canvas.command_stack.top_undo_command is own
        redo_native = _native(canvas)
        expected_redo_native = _native(native)
        np.testing.assert_array_equal(np.frombuffer(redo_native.constBits(), np.uint8), np.frombuffer(expected_redo_native.constBits(), np.uint8))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        assert _native_sources(canvas) == source and canvas.chapter.pixel_contract.signature == contract
    finally:
        for owner in (unmasked, original, native, cold, canvas):
            if owner is not None:
                _close(owner)
