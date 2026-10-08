"""Proposed actual-owner/current-pixel regressions; unrun artifact payload.

Install only in a root-reviewed isolated candidate. Original tests are unchanged.
The 32768 distortion budget is nominal: ceil gives a conservative33217 pixels.
Full native source, base and mask preparation is deliberately not capped here.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from hashlib import sha256
from threading import Event
from types import SimpleNamespace
import time

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.models import (ChapterDocument, BoundGeometry, ParameterMaskBinding, ToneMask,
    BlurModifier, KuwaharaModifier, OutlineModifier, RadialBlurModifier)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.live_canvas_preview import LiveCanvasPreviewPolicy, live_canvas_policy
from comic_editor.render.pixels import FLOAT_PIXELS
from comic_editor.render.scene import DetachedSceneBackend, EvaluatedScene
from comic_editor.render.service import DocumentRenderService, RenderDocument, RenderQuality, RenderRequest, RenderStatus
from comic_editor.ui.canvas import RasterCanvasWidget, ToolKind
from test_detached_owned_rig_preview import _scene, _start, _close, _encoded_pattern


def _pixels(image):
    return bytes(image.constBits())


def _encode(image):
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.WriteOnly) and image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def _make_scene(kind, mode, masked):
    canvas, object_id, rig_id = _scene("deform" if kind == "mesh_warp" else kind, mode)
    obj = canvas.chapter.objects[object_id]
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    page.bound = BoundGeometry.rectangle(0, 0, 800, 800)
    canvas.chapter.height = 800
    image = QImage.fromData(_encoded_pattern(), "PNG").scaled(640, 512)
    assert not image.isNull()
    canvas.images.put(object_id, "pattern.png", _encode(image), "image/png")
    obj.pixel_width, obj.pixel_height = 640, 512
    obj.transform_frame = (0., 0., 640., 512.)
    obj.transform_quad = [(80., 90.), (720., 90.), (720., 602.), (80., 602.)]
    rig = canvas.chapter.modifiers[rig_id]
    rig.frame, rig.center, rig.radius = (80., 90., 640., 512.), (400., 346.), 250.
    if kind == "mesh_warp":
        rig.modifier_type = "distort_mesh_warp"
        rig.parameters = {"rows": 3, "columns": 3, "smoothness": 30.,
                          "interpolation": "bilinear", "edges": "transparent"}
        rig.points = [(x / 2, y / 2) for y in range(3) for x in range(3)]
        rig.source_points = deepcopy(rig.points)
        rig.points[4] = (.63, .41)
    rig.validate()
    if masked:
        mask = ToneMask(name="Native scalar mask")
        canvas.chapter.masks[mask.mask_id] = mask
        canvas.tiles.paint_dab(mask.mask_id, QPointF(400, 346), 420, QColor("#b3b3b3"))
        rig.parameter_masks["intensity"] = ParameterMaskBinding(mask_id=mask.mask_id,
            black_value=27., white_value=83.)
    canvas.resize(800, 800)
    canvas.center_x = canvas.center_y = 400.
    canvas.scale, canvas.rotation = 1., 0.
    canvas._scene_snapshot_compiler.invalidate()
    canvas._render_service.invalidate()
    return canvas, object_id, rig_id


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


@pytest.mark.parametrize("kind", ["deform", "twirl", "mesh_warp"])
@pytest.mark.parametrize("mode", ["translate", "scale", "warp"])
@pytest.mark.parametrize("masked", [False, True])
def test_actual_live_scheduler_matches_independent_cold_draft_then_native_commit_undo(qapp, monkeypatch, kind, mode, masked):
    from comic_editor.ui import distort_rendering
    canvas, object_id, rig_id = _make_scene(kind, mode, masked)
    cold = native = None
    calls, preprocessing = [], []
    original_warp, original_rgba = distort_rendering.render_distort, distort_rendering._rgba
    def warp(*args, **kwargs):
        result = original_warp(*args, **kwargs)
        calls.append((kwargs.get("pixel_scale", 1.), result.size().toTuple() if result is not None else None))
        return result
    def rgba(image):
        preprocessing.append(image.size().toTuple())
        return original_rgba(image)
    try:
        before = deepcopy(canvas.chapter.to_dict())
        source = sha256(canvas.images.source(object_id).data).hexdigest()
        source_mask = tuple(canvas.tiles.object_signature(mask_id) for mask_id in canvas.chapter.masks)
        prior, revision = tuple(canvas.command_stack._undo), canvas.command_stack.revision
        original_native = _native(canvas)
        original_draft = _original_pose_policy_reference(canvas)
        _wait(canvas, qapp, live=False)  # Deliberately warm valid native effects.
        native_tiles = {key: tile.image.cacheKey() for key, tile in canvas._document_projection.tiles.items()}
        _start(canvas, object_id, mode)  # Real supported ordinary transform handlers.
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        monkeypatch.setattr(distort_rendering, "render_distort", warp)
        monkeypatch.setattr(distort_rendering, "_rgba", rgba)
        first = _wait(canvas, qapp, live=True)
        assert _pixels(first) != _pixels(original_draft)
        assert calls and any(scale < 1. for scale, _size in calls)
        reduced = [size for scale, size in calls if scale < 1. and size is not None]
        assert reduced and all(width * height <= 33217 and max(width, height) <= 225 for width, height in reduced)
        assert preprocessing and max(max(size) for size in preprocessing) <= 512
        assert {key: tile.image.cacheKey() for key, tile in canvas._document_projection.tiles.items()} == native_tiles
        cold = _replica(canvas, before, object_id)
        _start(cold, object_id, mode)
        assert cold._transform_preview_quad == canvas._transform_preview_quad
        np.testing.assert_array_equal(np.frombuffer(_pixels(first), np.uint8),
                                      np.frombuffer(_pixels(_wait(cold, qapp, live=True)), np.uint8))
        # A second actual current geometry must replace the first draft.
        if mode == "translate":
            move = canvas.layer_world_transform(canvas.chapter.objects[object_id].parent_layer_id).map(QPointF(111., 126.)) + QPointF(47., 34.)
        else:
            move = QPointF(*canvas.object_world_quad(object_id)[2]) + QPointF(45., 31.)
        canvas._tool_move(canvas.document_to_widget(move), 1.)
        cold._tool_move(cold.document_to_widget(move), 1.)
        second = _wait(canvas, qapp, live=True)
        assert second != first
        np.testing.assert_array_equal(np.frombuffer(_pixels(second), np.uint8),
                                      np.frombuffer(_pixels(_wait(cold, qapp, live=True)), np.uint8))
        monkeypatch.setattr(distort_rendering, "render_distort", original_warp)
        monkeypatch.setattr(distort_rendering, "_rgba", original_rgba)
        intended = deepcopy(canvas._transform_preview_quad)
        canvas._tool_release()
        cold._tool_release()
        committed = deepcopy(canvas.chapter.to_dict())
        assert committed == cold.chapter.to_dict() and committed != before
        assert canvas.chapter.objects[object_id].transform_quad == intended
        assert canvas._transform_preview_quad is None and canvas._transform_drag_mode is None
        assert len(canvas.command_stack._undo) == len(prior) + 1
        assert canvas.command_stack.revision == revision + 1
        own = canvas.command_stack.top_undo_command
        assert own.label == "Transform image"
        assert all(a is b for a, b in zip(prior, canvas.command_stack._undo[:-1]))
        _wait(canvas, qapp, live=False)
        native = _replica(canvas, committed, object_id)
        np.testing.assert_array_equal(np.frombuffer(_pixels(_native(canvas)), np.uint8),
                                      np.frombuffer(_pixels(_native(native)), np.uint8))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        np.testing.assert_array_equal(np.frombuffer(_pixels(_native(canvas)), np.uint8),
                                      np.frombuffer(_pixels(original_native), np.uint8))
        assert sha256(canvas.images.source(object_id).data).hexdigest() == source
        assert tuple(canvas.tiles.object_signature(mask_id) for mask_id in canvas.chapter.masks) == source_mask
        canvas.command_stack.redo()
        assert canvas.chapter.to_dict() == committed and canvas.command_stack.top_undo_command is own
        np.testing.assert_array_equal(np.frombuffer(_pixels(_native(canvas)), np.uint8),
                                      np.frombuffer(_pixels(_native(native)), np.uint8))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
    finally:
        for owner in (native, cold, canvas):
            if owner is not None:
                _close(owner)


@pytest.mark.parametrize("field,value", [
    ("_effect_preview_channel", "navigator"), ("_effect_preview_channel", "overflow"),
    ("_render_base_alpha", True), ("_rendering_mask_contributor", 1),
    ("_render_modifier_sources", {("object", "source")}), ("_render_cage_source", True),
    ("_rendering_halftone_source", True), ("_rendering_compound_references", True),
    ("_rendering_outward_gradient", True), ("_tiling_capture_geometry", object()),
])
def test_special_captures_cannot_reduce_native_sampling(field, value):
    document = RenderDocument((1, 2, 3), (), 4, 800, 800, "#00000000", live_preview=True)
    policy = LiveCanvasPreviewPolicy(document.identity, 4, 7, lambda: False)
    scene = SimpleNamespace(snapshot=SimpleNamespace(document=document), _live_canvas_preview_policy=policy,
                            _effect_preview_channel="canvas")
    assert live_canvas_policy(scene) is policy
    setattr(scene, field, value)
    assert live_canvas_policy(scene) is None


@pytest.mark.parametrize("change", ["float", "committed", "revision", "identity", "overflow", "underlay"])
def test_other_document_contracts_retain_native_policy(change):
    document = RenderDocument((1, 2, 3), (), 4, 800, 800, "#00000000", live_preview=True)
    policy = LiveCanvasPreviewPolicy(document.identity, 4, 7, lambda: False)
    altered = {"float": dict(pixel_contract=FLOAT_PIXELS), "committed": dict(live_preview=False),
               "revision": dict(revision=5), "identity": dict(identity=(3, 2, 1)),
               "overflow": dict(overflow=.2), "underlay": dict(underlay=("object", .2))}[change]
    scene = SimpleNamespace(snapshot=SimpleNamespace(document=replace(document, **altered)),
        _live_canvas_preview_policy=policy, _effect_preview_channel="canvas")
    assert live_canvas_policy(scene) is None


def test_detached_capture_request_scope_restores_policy_on_exception(qapp):
    canvas, object_id, _rig_id = _make_scene("twirl", "translate", False)
    backend = None
    try:
        _start(canvas, object_id, "translate")
        document = canvas._render_document_state()
        capture = canvas._scene_snapshot_compiler.capture(canvas, document)
        while not capture.advance(.004):
            pass
        backend = DetachedSceneBackend(capture.result)
        policy = LiveCanvasPreviewPolicy(document.identity, document.revision, 9, lambda: False)
        backend.live_canvas_preview_policy = policy
        request = RenderRequest((0., 0., 800., 800.), 1., (800, 800), ("detached-preview", 9),
                                document.revision, quality=RenderQuality.INTERACTIVE)
        sentinel = object()
        backend.scene._live_canvas_preview_policy = sentinel
        with pytest.raises(RuntimeError, match="scope failure"):
            with backend.capture(document, request, QRectF(0, 0, 800, 800)):
                assert live_canvas_policy(backend.scene) is policy
                raise RuntimeError("scope failure")
        assert backend.scene._live_canvas_preview_policy is sentinel
        for changed in (replace(request, quality=RenderQuality.EXACT), replace(request, key=("export",)),
                        replace(request, target=("object", object_id)), replace(request, revision=document.revision + 1)):
            with backend.capture(document, changed, QRectF(0, 0, 800, 800)):
                assert live_canvas_policy(backend.scene) is None
        assert backend.scene._live_canvas_preview_policy is sentinel
    finally:
        if backend is not None:
            backend.close()
        _close(canvas)


def test_ordinary_caller_has_no_live_canvas_policy():
    assert live_canvas_policy(SimpleNamespace(_interactive_render=True, _projection_exact=False)) is None


def _second_move(canvas, object_id):
    move = canvas.layer_world_transform(canvas.chapter.objects[object_id].parent_layer_id).map(QPointF(111., 126.)) + QPointF(47., 34.)
    canvas._tool_move(canvas.document_to_widget(move), 1.)
    return move


def _check_pair_release(canvas, cold, before, object_id, qapp):
    prior = tuple(canvas.command_stack._undo)
    canvas._tool_release()
    cold._tool_release()
    committed = deepcopy(canvas.chapter.to_dict())
    assert committed != before and committed == cold.chapter.to_dict()
    assert len(canvas.command_stack._undo) == len(prior) + 1
    own = canvas.command_stack.top_undo_command
    assert own.label == "Transform image"
    assert canvas._transform_preview_quad is None and canvas._transform_drag_mode is None
    _wait(canvas, qapp, live=False)
    native = _replica(canvas, committed, object_id)
    original = _replica(canvas, before, object_id)
    try:
        np.testing.assert_array_equal(np.frombuffer(_pixels(_native(canvas)), np.uint8),
                                      np.frombuffer(_pixels(_native(native)), np.uint8))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        np.testing.assert_array_equal(np.frombuffer(_pixels(_native(canvas)), np.uint8),
                                      np.frombuffer(_pixels(_native(original)), np.uint8))
        canvas.command_stack.redo()
        assert canvas.chapter.to_dict() == committed and canvas.command_stack.top_undo_command is own
        np.testing.assert_array_equal(np.frombuffer(_pixels(_native(canvas)), np.uint8),
                                      np.frombuffer(_pixels(_native(native)), np.uint8))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
    finally:
        for reference in (original, native):
            _close(reference)


@pytest.mark.parametrize("upstream_draft", [False, True])
def test_actual_radial_first_and_after_draft_compute_current_integration(qapp, monkeypatch, upstream_draft):
    from comic_editor.ui import radial_blur
    canvas, object_id, rig_id = _make_scene("twirl", "translate", True)
    cold = None
    try:
        radial = RadialBlurModifier(modifier_id=rig_id, center=(400., 346.), angle=22.,
            parameter_masks=deepcopy(canvas.chapter.modifiers[rig_id].parameter_masks))
        radial.parameter_masks["angle"] = ParameterMaskBinding(
            mask_id=next(iter(canvas.chapter.masks)), black_value=0., white_value=22.)
        radial.validate()
        canvas.chapter.modifiers[rig_id] = radial
        if upstream_draft:
            blur = BlurModifier(strength=14.)
            canvas.chapter.add_modifier(blur, [("object", object_id)])
            canvas.chapter.objects[object_id].modifier_ids[:] = [blur.modifier_id, rig_id]
        canvas._scene_snapshot_compiler.invalidate()
        before = deepcopy(canvas.chapter.to_dict())
        original_draft = _original_pose_policy_reference(canvas)
        original = radial_blur.radial_blur
        calls, sampling = [], []
        def integrate(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append((tuple(args[1]), np.array(args[2], copy=True), result.shape))
            sampling.append((args[0].shape, result.shape, kwargs.get("output_to_world"), kwargs.get("cancelled")))
            return result
        monkeypatch.setattr(radial_blur, "radial_blur", integrate)
        _start(canvas, object_id, "translate")
        first = _wait(canvas, qapp, live=True)
        assert _pixels(first) != _pixels(original_draft)
        assert calls and any(np.max(angle) > 0 for _center, angle, _shape in calls)
        assert all(height > 0 and width > 0 and channels == 4 for _center, _angle, (height, width, channels) in calls)
        assert canvas.chapter.to_dict() == before
        cold = _replica(canvas, before, object_id)
        _start(cold, object_id, "translate")
        np.testing.assert_array_equal(np.frombuffer(_pixels(first), np.uint8),
                                      np.frombuffer(_pixels(_wait(cold, qapp, live=True)), np.uint8))
        _second_move(canvas, object_id)
        _second_move(cold, object_id)
        second = _wait(canvas, qapp, live=True)
        assert _pixels(second) != _pixels(first)
        np.testing.assert_array_equal(np.frombuffer(_pixels(second), np.uint8),
                                      np.frombuffer(_pixels(_wait(cold, qapp, live=True)), np.uint8))
        assert len(calls) >= 4  # Each independent current geometry integrates.
        assert sampling and all(max(input_shape[:2]) <= 512 for input_shape, _output, _map, _cancel in sampling)
        assert all(output[0]*output[1] <= 33217 and max(output[:2]) <= 225 for _input, output, _map, _cancel in sampling)
        assert all(isinstance(output_map, QTransform) and callable(cancel) for _input, _output, output_map, cancel in sampling)
        monkeypatch.setattr(radial_blur, "radial_blur", original)
        _check_pair_release(canvas, cold, before, object_id, qapp)
    finally:
        for owner in (cold, canvas):
            if owner is not None:
                _close(owner)


@pytest.mark.parametrize("kind", ["blur", "kuwahara", "outline"])
def test_actual_generic_stack_policy_is_current_and_native_after_release(qapp, monkeypatch, kind):
    from comic_editor.ui import interactive_effects
    canvas, object_id, rig_id = _make_scene("twirl", "translate", True)
    cold = None
    try:
        bindings = deepcopy(canvas.chapter.modifiers[rig_id].parameter_masks)
        effect = {"blur": BlurModifier(modifier_id=rig_id, strength=14., parameter_masks=bindings),
            "kuwahara": KuwaharaModifier(modifier_id=rig_id, size=6., parameter_masks=bindings),
            "outline": OutlineModifier(modifier_id=rig_id, thickness=8., blur_radius=2., blur_strength=100., parameter_masks=bindings)}[kind]
        effect.validate()
        canvas.chapter.modifiers[rig_id] = effect
        canvas._scene_snapshot_compiler.invalidate()
        before = deepcopy(canvas.chapter.to_dict())
        original_draft = _original_pose_policy_reference(canvas)
        original = interactive_effects.apply_modifier_stack
        calls = []
        def apply(image, modifiers, *args, **kwargs):
            calls.append((image.size().toTuple(), tuple(m.modifier_type for m in modifiers), kwargs.get("cancelled")))
            return original(image, modifiers, *args, **kwargs)
        monkeypatch.setattr(interactive_effects, "apply_modifier_stack", apply)
        _start(canvas, object_id, "translate")
        first = _wait(canvas, qapp, live=True)
        assert _pixels(first) != _pixels(original_draft)
        assert calls and all(max(size) <= 256 for size, _types, _cancelled in calls)
        assert any(callable(cancelled) for _size, _types, cancelled in calls)
        cold = _replica(canvas, before, object_id)
        _start(cold, object_id, "translate")
        np.testing.assert_array_equal(np.frombuffer(_pixels(first), np.uint8),
                                      np.frombuffer(_pixels(_wait(cold, qapp, live=True)), np.uint8))
        _second_move(canvas, object_id)
        _second_move(cold, object_id)
        second = _wait(canvas, qapp, live=True)
        assert _pixels(second) != _pixels(first)
        np.testing.assert_array_equal(np.frombuffer(_pixels(second), np.uint8),
                                      np.frombuffer(_pixels(_wait(cold, qapp, live=True)), np.uint8))
        monkeypatch.setattr(interactive_effects, "apply_modifier_stack", original)
        _check_pair_release(canvas, cold, before, object_id, qapp)
    finally:
        for owner in (cold, canvas):
            if owner is not None:
                _close(owner)


def test_latest_actual_live_demand_cancels_running_kernel_and_cannot_publish_old_pixels(qapp, monkeypatch):
    from comic_editor.ui import distort_rendering
    canvas, object_id, _rig_id = _make_scene("twirl", "translate", True)
    cold = None
    entered, cancelled, release = Event(), Event(), Event()
    original = distort_rendering.render_distort
    scheduler = canvas._scene_controller.scheduler
    publish, publications = scheduler._publish, []
    first_serial = None
    def observed_publish(completion, token):
        result = publish(completion, token)
        publications.append((completion.demand.serial, completion.preview is not None, result, completion.error))
        return result
    def kernel(*args, **kwargs):
        if kwargs.get("pixel_scale", 1.) < 1. and not entered.is_set():
            entered.set()
            token = args[5]
            assert callable(token)
            deadline = time.monotonic() + 10
            while not release.is_set() and time.monotonic() < deadline:
                if token():
                    cancelled.set()
                    break
                time.sleep(.002)
            assert cancelled.is_set(), "Latest actual demand did not cancel the running kernel"
        return original(*args, **kwargs)
    try:
        before = deepcopy(canvas.chapter.to_dict())
        prior = tuple(canvas.command_stack._undo)
        original_draft = _original_pose_policy_reference(canvas)
        monkeypatch.setattr(distort_rendering, "render_distort", kernel)
        monkeypatch.setattr(scheduler, "_publish", observed_publish)
        _start(canvas, object_id, "translate")
        surface = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        deadline = time.monotonic() + 10
        while not entered.is_set() and time.monotonic() < deadline:
            canvas.render(surface)
            qapp.processEvents()
            time.sleep(.002)
        assert entered.is_set() and scheduler.future is not None
        first_serial = canvas._scene_controller.serial
        _second_move(canvas, object_id)
        current = _wait(canvas, qapp, live=True)
        assert _pixels(current) != _pixels(original_draft)
        assert cancelled.is_set() and canvas._scene_controller.serial > first_serial
        assert not any(serial == first_serial and is_preview and accepted for serial, is_preview, accepted, _error in publications)
        assert any(serial == canvas._scene_controller.serial and is_preview and accepted for serial, is_preview, accepted, _error in publications)
        assert canvas.chapter.to_dict() == before and tuple(canvas.command_stack._undo) == prior
        cold = _replica(canvas, before, object_id)
        _start(cold, object_id, "translate")
        _second_move(cold, object_id)
        np.testing.assert_array_equal(np.frombuffer(_pixels(current), np.uint8),
                                      np.frombuffer(_pixels(_wait(cold, qapp, live=True)), np.uint8))
        monkeypatch.setattr(distort_rendering, "render_distort", original)
        _check_pair_release(canvas, cold, before, object_id, qapp)
    finally:
        release.set()
        for owner in (cold, canvas):
            if owner is not None:
                _close(owner)


def test_actual_float_live_scheduler_keeps_native_sampling_and_cold_commit_bytes(qapp, monkeypatch):
    from comic_editor.ui import distort_rendering
    canvas, object_id, _rig_id = _make_scene("twirl", "translate", True)
    cold = None
    try:
        canvas.chapter.pixel_contract = FLOAT_PIXELS
        canvas._scene_snapshot_compiler.invalidate()
        before = deepcopy(canvas.chapter.to_dict())
        original_native = _native(canvas)
        original = distort_rendering.render_distort
        calls = []
        def warp(*args, **kwargs):
            calls.append((kwargs.get("pixel_scale", 1.), args[0].format()))
            return original(*args, **kwargs)
        monkeypatch.setattr(distort_rendering, "render_distort", warp)
        _start(canvas, object_id, "translate")
        current = _wait(canvas, qapp, live=True)
        assert _pixels(current) != _pixels(original_native)
        assert current.format() == FLOAT_PIXELS.image_format
        assert calls and all(scale == 1. for scale, _format in calls)
        cold = _replica(canvas, before, object_id)
        _start(cold, object_id, "translate")
        np.testing.assert_array_equal(np.frombuffer(_pixels(current), np.uint8),
                                      np.frombuffer(_pixels(_wait(cold, qapp, live=True)), np.uint8))
        monkeypatch.setattr(distort_rendering, "render_distort", original)
        _check_pair_release(canvas, cold, before, object_id, qapp)
    finally:
        for owner in (cold, canvas):
            if owner is not None:
                _close(owner)


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
@pytest.mark.parametrize("mapping_kind", ["identity", "affine", "projective"])
@pytest.mark.parametrize("cropped", [False, True])
def test_default_native_radial_math_matches_held_original_bits(dtype, mapping_kind, cropped):
    from pathlib import Path
    from comic_editor.ui.radial_blur import radial_blur
    import test_live_radial_native_reference as reference
    assert Path(reference.__file__).resolve().parent == Path(__file__).resolve().parent
    assert sha256(Path(reference.__file__).read_bytes()).hexdigest() == "3c6a4ea010b0630c092bba3ca0be9e39ceae4fd8fe6221a6d3f92d40af57f9e3"
    rng = np.random.default_rng(20261008)
    original = rng.uniform(0., 1., (48, 64, 4)).astype(dtype)
    original[..., :3] *= original[..., 3:4]
    mapping = {"identity": QTransform(),
        "affine": QTransform(1.1, .12, -.07, .91, 3., -2.),
        "projective": QTransform(1.1, .12, .0008, -.07, .91, -.0004, 3., -2., 1.)}[mapping_kind]
    shape, origin = ((35, 42), (4.25, -7.5)) if cropped else (None, (0., 0.))
    angle_shape = shape or original.shape[:2]
    angle = np.zeros(angle_shape, dtype=np.float32)
    angle[:, angle_shape[1]//2:] = 17.
    old = reference.radial_blur(original, (32., 24.), angle, mapping,
        output_shape=shape, output_origin=origin)
    current = radial_blur(original, (32., 24.), angle, mapping,
        output_shape=shape, output_origin=origin)
    assert current.dtype == old.dtype and current.shape == old.shape
    np.testing.assert_array_equal(current.view(np.uint8), old.view(np.uint8))


@pytest.mark.parametrize("projective", [False, True])
def test_radial_draft_zero_angle_uses_complete_independent_source_and_output_world_frames(projective):
    from scipy.ndimage import map_coordinates
    from comic_editor.ui.radial_blur import radial_blur, _map
    yy, xx = np.mgrid[0:9, 0:13]
    source = np.stack((xx/12., yy/8., (xx+yy)/20., np.ones_like(xx)), axis=-1).astype(np.float32)
    original_map = (QTransform(1., .07, .0004, -.04, .93, -.0002, 13., 29., 1.)
                    if projective else QTransform.fromTranslate(13., 29.))
    inverse, valid = original_map.inverted()
    assert valid
    source_map = original_map * QTransform.fromScale(13/640., 9/512.)
    output_to_world = (QTransform.fromScale(188/19., 147/23.)
        * QTransform.fromTranslate(-20., -14.) * inverse)
    angle = np.zeros((23, 19), dtype=np.float32)
    current = radial_blur(source, (400., 346.), angle, source_map,
        output_shape=(23, 19), output_to_world=output_to_world)
    oy, ox = np.mgrid[0:23, 0:19].astype(np.float64)
    world_x, world_y = _map(output_to_world, ox+.5, oy+.5)
    sx, sy = _map(source_map, world_x, world_y)
    coords = np.stack((sy-.5, sx-.5))
    expected = np.stack([map_coordinates(source[..., channel], coords,
        order=1, mode="grid-constant", cval=0., prefilter=False) for channel in range(4)], axis=-1)
    expected = np.clip(expected, 0., 1.)
    expected[..., :3] = np.minimum(expected[..., :3], expected[..., 3:4])
    assert current.shape == (23, 19, 4)
    np.testing.assert_array_equal(current.view(np.uint8), expected.view(np.uint8))


@pytest.mark.parametrize("remix", ["intensity", "mask", "base"])
def test_same_owner_radial_draft_reuses_only_integration_then_remixes_current_inputs(qapp, monkeypatch, remix):
    from comic_editor.ui import radial_blur
    from comic_editor.ui.radial_pipeline import render_radial_stage
    canvas, object_id, rig_id = _make_scene("twirl", "translate", True)
    backend = reference = None
    try:
        before = deepcopy(canvas.chapter.to_dict())
        _start(canvas, object_id, "translate")
        document = canvas._render_document_state()
        capture = canvas._scene_snapshot_compiler.capture(canvas, document)
        while not capture.advance(.004):
            pass
        assert not capture.stale
        snapshot = capture.result
        backend, reference = DetachedSceneBackend(snapshot), DetachedSceneBackend(snapshot)
        for owner in (backend, reference):
            owner.native_preview, owner.artwork_scale = True, 1.
            owner.live_canvas_preview_policy = LiveCanvasPreviewPolicy(document.identity, document.revision, 81, lambda: False)
        request = RenderRequest((0., 0., 800., 800.), 1., (800, 800), ("detached-preview", 81),
            document.revision, quality=RenderQuality.INTERACTIVE)
        incoming = QImage.fromData(_encoded_pattern(), "PNG").scaled(320, 256)
        first_base, second_base = QImage(incoming), QImage(incoming)
        first_effect = RadialBlurModifier(modifier_id=rig_id, center=(160., 128.), angle=22., intensity=25.)
        second_effect = deepcopy(first_effect)
        first_fields, second_fields = {}, {}
        if remix == "intensity":
            second_effect.intensity = 75.
        elif remix == "mask":
            binding = ParameterMaskBinding(mask_id=next(iter(canvas.chapter.masks)), black_value=10., white_value=90.)
            first_effect.parameter_masks["intensity"] = deepcopy(binding)
            second_effect.parameter_masks["intensity"] = deepcopy(binding)
            first_fields[(rig_id, "intensity")] = np.zeros((256, 320), dtype=np.float32)
            second_fields[(rig_id, "intensity")] = np.ones((256, 320), dtype=np.float32)
        else:
            first_effect.intensity = second_effect.intensity = 50.
            second_base.fill(QColor("#2436b0"))
        calls = []
        original = radial_blur.radial_blur
        def integrate(*args, **kwargs):
            calls.append((args[0].shape, kwargs.get("output_shape")))
            return original(*args, **kwargs)
        monkeypatch.setattr(radial_blur, "radial_blur", integrate)
        def render(owner, effect, base, fields):
            with owner.capture(document, request, QRectF(0, 0, 800, 800)):
                assert live_canvas_policy(owner.scene) is owner.live_canvas_preview_policy
                image, provisional = render_radial_stage(owner.scene, incoming, base, effect, fields,
                    QTransform(), (0., 0.), source_key=("declared-current-cache-remix",), scope=("owned-remix",),
                    asynchronous=True, deferred=False, provisional=False, navigator=False, exact=False)
                assert provisional and image.size() == base.size()
                return image
        first = render(backend, first_effect, first_base, first_fields)
        second = render(backend, second_effect, second_base, second_fields)
        assert len(calls) == 1, "Intensity/base edits should reuse the unblended integration"
        assert _pixels(first) != _pixels(second)
        stored = [image for key, image in backend.scene._modifier_render_cache.items()
            if isinstance(key, tuple) and key[:1] == ("live-canvas-radial-integration-draft",)]
        assert stored and all(image.format() == QImage.Format_RGBA32FPx4_Premultiplied for image in stored)
        expected = render(reference, second_effect, second_base, second_fields)
        assert len(calls) == 2
        np.testing.assert_array_equal(np.frombuffer(_pixels(second), np.uint8), np.frombuffer(_pixels(expected), np.uint8))
        assert canvas.chapter.to_dict() == before
    finally:
        for owner in (reference, backend):
            if owner is not None:
                owner.close()
        _close(canvas)
