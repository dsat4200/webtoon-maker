"""Actual detached pixels, native dispatch and monitor generation ownership."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import threading
import time
import weakref

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QTransform
from shiboken6 import delete as delete_qobject, isValid

from comic_editor.core import settings as settings_module
from comic_editor.core.performance_monitor import PerformanceRecorder
from comic_editor.core.models import (BoundGeometry, ChapterDocument, RasterObject, SeriesDocument,
    HueSaturationLightnessModifier, ParameterMaskBinding, ToneMask)
from comic_editor.core.tools import ToolKind
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.scene import SceneCapture, SceneSnapshotCompiler, DetachedSceneBackend
from comic_editor.render.scheduler import SceneDemand, SceneScheduler
from comic_editor.render.service import DocumentRenderService, RenderRequest
from comic_editor.render import effect_pipeline, scene_kernels, tile_effects
from comic_editor.render import modifier_rendering as render_modifiers
from comic_editor.ui import interactive_effects
from comic_editor.ui.canvas import GpuCanvasWidget
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.performance_monitor import PerformanceMonitorController, _monitor_scope


@pytest.fixture
def make_owner(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(settings_module, 'settings_path', lambda: tmp_path / 'settings.json')
    owners = []
    def make(renderer='raster'):
        monkeypatch.setattr('comic_editor.ui.main_window.load_settings',
            lambda: EditorSettings(canvas_renderer=renderer, grid_overlay_visible=False))
        window = MainWindow()
        monkeypatch.setattr(window, '_schedule_series_preferences_save', lambda **_kw: None)
        chapter = ChapterDocument(width=64, height=64, document_kind='asset', background='#00000000')
        page = chapter.add_page('Bounded monitor fixture', BoundGeometry.rectangle(0, 0, 64, 64))
        page.fill_color, page.border_width = None, 0
        obj = chapter.add_object(page.layer_id, RasterObject(tile_size=64,
            interaction_rect=(0, 0, 64, 64)))
        tiles = TileStore(tile_size=64)
        source = QImage(64, 64, QImage.Format_ARGB32_Premultiplied)
        source.fill(QColor('#ffcc2040'))
        painter = QPainter(source)
        painter.fillRect(12, 17, 29, 23, QColor('#ff2080d0'))
        painter.end()
        tiles.set_tile(obj.object_id, (0, 0), source)
        effect = HueSaturationLightnessModifier(hue=53, saturation=17)
        chapter.add_modifier(effect, [('object', obj.object_id)])
        mask = ToneMask(saved=True)
        chapter.masks[mask.mask_id] = mask
        tiles.paint_dab(mask.mask_id, QPointF(32, 32), 180, QColor('white'), opacity=.5)
        obj.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
        window.series = SeriesDocument()
        window._set_chapter(chapter, tiles)
        canvas = window.canvas
        binding = canvas.chapter.objects[obj.object_id].opacity_mask
        assert binding is not None and binding.mask_id == mask.mask_id
        assert mask.mask_id in canvas.chapter.masks and canvas.chapter.masks[mask.mask_id].saved
        alpha = canvas.tiles.tile(mask.mask_id, (0, 0)).pixelColor(32, 32).alpha()
        assert .49 < alpha / 255. < .51, 'Mask brush source lost its explicit half coverage'
        field = canvas.render_tone_mask_field(mask.mask_id, 64, 64, QTransform(), QRectF(0, 0, 64, 64))
        assert field.shape == (64, 64) and .49 < float(field[32, 32]) < .51
        assert float(field[32, 32]) == pytest.approx(alpha / 255., abs=1e-6)
        window.canvas._scene_controller.reset()
        window.setUpdatesEnabled(False)
        window.resize(800, 620)
        monitor = PerformanceMonitorController(window, log_directory=tmp_path / str(len(owners)))
        window._performance_monitor_controller = monitor
        owners.append((window, monitor))
        return window, monitor, obj
    yield make
    for window, monitor in reversed(owners):
        monitor.stop()
        assert not monitor.enabled and not monitor._patches and not monitor._connections
        assert monitor.recorder._thread is None
        assert monitor._capture_generation is None and not monitor._owned_schedulers
        assert monitor._writer is None or not monitor._writer.running
        if not isValid(window):
            continue
        canvas = window.canvas
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=False)
        navigator = window.preview._navigator_jobs
        navigator.cancel()
        navigator.scheduler.close()
        navigator.scheduler.executor.shutdown(wait=True, cancel_futures=False)
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        window._dirty = False
        window.close()
        window.deleteLater()
        # Retire each complete native widget tree before another unrelated
        # test begins, including exception paths after monitor.start().
        QCoreApplication.sendPostedEvents(window, QEvent.DeferredDelete)
        qapp.processEvents()
    from comic_editor.ui import performance_monitor as monitor_module
    assert monitor_module._active_controller is None


def freeze(canvas, compiler=None):
    pending = (compiler or SceneSnapshotCompiler()).capture(canvas, canvas._render_document_state())
    deadline = time.monotonic() + 10
    while not pending.advance(.001):
        assert time.monotonic() < deadline
    assert not pending.stale and pending.result is not None
    return pending.result


def evaluate(scheduler, snapshot, serial, consumer='canvas'):
    requests = tuple(__import__('comic_editor.render.projection', fromlist=['DocumentProjection'])
        .DocumentProjection().requests(QRectF(0, 0, 64, 64), 1.))
    if consumer == 'navigator':
        # NavigatorJobs uses a bounded visible/preview demand, never exact
        # document tile requests with the navigator preview channel.
        scheduler.submit(SceneDemand(serial, snapshot, (), (None,), (32., 32.),
            (0., 0., 64., 64.), (64, 64), consumer))
    else:
        scheduler.submit(SceneDemand(serial, snapshot, requests, (None,), (32., 32.), consumer=consumer))
    completions = []
    deadline = time.monotonic() + 20
    while scheduler.busy:
        assert time.monotonic() < deadline, 'Real bounded detached render did not finish'
        completions.extend(scheduler.poll())
        time.sleep(.002)
    completions.extend(scheduler.poll())
    assert not any(item.error for item in completions), [item.error for item in completions if item.error]
    tiles = {address: QImage(image) for item in completions
        for address, (image, exact) in (item.tiles or {}).items() if exact}
    if consumer == 'navigator':
        previews = [item.preview for item in completions if item.preview is not None]
        assert previews and completions[-1].done
        assert previews[-1].status.value in {'exact', 'provisional'}
        tiles = {('navigator-preview',): QImage(previews[-1].image)}
    assert tiles and completions[-1].done
    return {address: (image.size(), image.format(), image.bytesPerLine(), bytes(image.constBits()))
            for address, image in tiles.items()}


def owned_worker_events(monitor):
    return [event for event in monitor.snapshot()['timeline']
        if event['name'].startswith('scene.') and event['details'].get('scope') == 'detached_worker']


def test_actual_detached_masked_effect_pixels_match_disabled_and_owned_spans(make_owner, qapp, monkeypatch):
    window, monitor, obj = make_owner()
    canvas = window.canvas
    alias_original = interactive_effects.apply_modifier_stack
    alias_calls = []
    def observe_actual_scoped_alias(image, *args, **kwargs):
        if monitor.enabled:
            alias_calls.append(alias_scope_observation(monitor, image))
        return alias_original(image, *args, **kwargs)
    monkeypatch.setattr(interactive_effects, 'apply_modifier_stack', observe_actual_scoped_alias)
    stages_original = interactive_effects.render_interactive_stack
    stage_calls = []
    def observe_actual_stage_alias(scene, image, *args, **kwargs):
        if monitor.enabled:
            stage_calls.append(alias_scope_observation(monitor, image))
        return stages_original(scene, image, *args, **kwargs)
    monkeypatch.setattr(interactive_effects, 'render_interactive_stack', observe_actual_stage_alias)
    snapshot = freeze(canvas)
    binding = snapshot.chapter.objects[obj.object_id].opacity_mask
    assert binding is not None and binding.mask_id in snapshot.chapter.masks
    assert snapshot.chapter.masks[binding.mask_id].saved
    captured_alpha = snapshot.tiles.tile(binding.mask_id, (0, 0)).pixelColor(32, 32).alpha()
    assert .49 < captured_alpha / 255. < .51
    scheduler = canvas._scene_controller.scheduler
    model = copy.deepcopy(canvas.chapter.to_dict())
    source = bytes(canvas.tiles.tile(obj.object_id, (0, 0)).constBits())
    # An independent unmonitored scheduler is a real detached native control.
    control = SceneScheduler()
    try:
        reference = evaluate(control, snapshot, 41)
    finally:
        control.close()
        control.executor.shutdown(wait=True, cancel_futures=False)
    monitor.start()
    gui = threading.get_ident()
    original_context = monitor.context
    def gui_context():
        assert threading.get_ident() == gui, 'Worker queried live QWidget context'
        return original_context()
    monkeypatch.setattr(monitor, 'context', gui_context)
    actual = evaluate(scheduler, snapshot, 42)
    assert actual == reference
    # Real output must contain source and mask effects, not an empty equality.
    request = RenderRequest((0., 0., 64., 64.), 1., (64, 64), ('mask-reference',), snapshot.document.revision)
    backend = DetachedSceneBackend(snapshot)
    try:
        from comic_editor.render.pixels import pixel_scope
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            field = backend.scene.render_tone_mask_field(binding.mask_id, 64, 64,
                QTransform(), QRectF(0, 0, 64, 64))
        assert field.shape == (64, 64) and .49 < float(field[32, 32]) < .51
        assert float(field[32, 32]) == pytest.approx(captured_alpha / 255., abs=1e-6)
        service = DocumentRenderService(backend)
        service.projection.revision = snapshot.document.revision
        result = service.render_region(snapshot.document, request)
        assert result.exact and 0 < result.image.pixelColor(32, 32).alpha() < 255
    finally:
        backend.close()
    owned_alias_calls = [row for row in alias_calls if row['scope'] is not None]
    assert owned_alias_calls and all(row['scope']['ownership'] == 'monitored_editor' and
        row['scope']['demand_serial'] == 42 and row['scope']['document_revision'] == snapshot.document.revision and
        row['scope']['scheduler_role'] == 'canvas' and row['thread_id'] != gui for row in owned_alias_calls)
    assert any(row['scope'] is None for row in alias_calls), 'Independent cold backend missed actual scoped alias'
    owned_stage_calls = [row for row in stage_calls if row['scope'] is not None]
    assert owned_stage_calls and all(row['scope']['ownership'] == 'monitored_editor' and
        row['scope']['scope'] == 'detached_worker' and row['scope']['demand_serial'] == 42 and
        row['scope']['document_revision'] == snapshot.document.revision and
        row['scope']['scheduler_role'] == 'canvas' and row['thread_id'] != gui for row in owned_stage_calls)
    assert any(row['scope'] is None for row in stage_calls), 'Independent cold backend missed actual stage alias'
    events = owned_worker_events(monitor)
    names = {event['name'] for event in events}
    assert {'scene.worker_envelope', 'scene.worker_admitted', 'scene.render_tiles',
        'scene.render_region', 'scene.backend_paint', 'scene.render_modified_object',
        'scene.effects.stages', 'scene.effects.stack', 'scene.effects.opacity_mask'} <= names
    cpu = [event for event in events if event['category'] == 'worker_cpu']
    assert {event['name'] for event in cpu} == {
        'scene.worker_envelope.thread_cpu', 'scene.worker_admitted.thread_cpu'}
    assert all(event['details']['clock'] == 'thread_cpu' and event['duration_ms'] >= 0 for event in cpu)
    assert all(event['details']['thread_id'] != gui and event['details']['thread'] == 'worker'
               for event in events)
    assert all(event['details']['demand_serial'] == 42 and
               event['details']['document_revision'] == snapshot.document.revision for event in events)
    assert all(event['details']['scheduler_role'] == 'canvas' and
               event['details']['consumer'] == 'canvas' and
               event['details']['run_generation'] == 1 and
               event['details']['capture_epoch'] == monitor.recorder.capture_epoch for event in events)
    assert canvas.chapter.to_dict() == model
    assert bytes(canvas.tiles.tile(obj.object_id, (0, 0)).constBits()) == source
    assert 'Worker stacks are not sampled' in monitor.snapshot()['instrumentation']['detached_workers']
    monitor.stop()
    assert interactive_effects.apply_modifier_stack is observe_actual_scoped_alias
    assert interactive_effects.render_interactive_stack is observe_actual_stage_alias


def test_existing_lazy_capture_slice_and_unrelated_owner_filter(make_owner):
    window, monitor, _obj = make_owner()
    canvas = window.canvas
    capture = canvas._scene_snapshot_compiler.capture(canvas, canvas._render_document_state())
    original = SceneCapture.advance
    monitor.start()
    while not capture.advance(.001):
        pass
    assert capture.result is not None
    slices = [row for row in monitor.snapshot()['timeline'] if row['name'] == 'scene.capture_slice']
    assert slices and all(row['details']['scope'] == 'gui_capture_slice' for row in slices)
    unrelated = SceneScheduler()
    before = len(owned_worker_events(monitor))
    try:
        evaluate(unrelated, capture.result, 73)
        assert len(owned_worker_events(monitor)) == before
    finally:
        unrelated.close()
        unrelated.executor.shutdown(wait=True, cancel_futures=False)
    monitor.stop()
    assert SceneCapture.advance is original


@pytest.mark.parametrize('renderer', ['raster', 'gpu'])
def test_real_native_frame_dynamic_dispatch_records_gui_frame(make_owner, qapp, renderer):
    if renderer == 'gpu' and qapp.platformName() == 'offscreen':
        pytest.skip('Native QOpenGLWidget presentation is unavailable on offscreen platform')
    window, monitor, _obj = make_owner(renderer)
    canvas = window.canvas
    if renderer == 'gpu':
        assert isinstance(canvas, GpuCanvasWidget), 'GPU fixture fell back to software presentation'
    timer_callback = canvas._scene_controller.advance
    scene = canvas._scene_controller
    from comic_editor.render.projection import DocumentProjection
    document = canvas._render_document_state()
    visible = QRectF(0, 0, 64, 64)
    scene.request(document, tuple(DocumentProjection().requests(visible, 1.)), (None,), visible)
    old_capture = scene.capture
    assert old_capture is not None and not old_capture.advance(0.)
    window.setUpdatesEnabled(True)
    window.show()
    monitor.start()
    swapped = []
    if renderer == 'gpu':
        canvas.frameSwapped.connect(lambda: swapped.append(True))
    canvas.update()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
        names = {row['name'] for row in monitor.snapshot()['timeline']}
        if ('canvas.paint_canvas_frame' in names and 'canvas.paint_ready_document_projection' in names
                and 'scene.capture_slice' in names and 'scene.publish' in names and 'scene.submit' in names
                and 'scene.worker_envelope' in names
                and (renderer != 'gpu' or swapped)):
            break
    else:
        pytest.fail('Actual native frame/capture/worker dispatch was not observed')
    assert canvas._scene_controller.advance == timer_callback
    slices = [event for event in monitor.snapshot()['timeline'] if event['name'] == 'scene.capture_slice']
    assert any(event['details'].get('consumer') == 'canvas' for event in slices)
    assert not any(event['name'] in {'scene.advance', 'navigator.advance'}
                   for event in monitor.snapshot()['timeline'])
    monitor.stop()


@pytest.mark.parametrize('operation', ['restart', 'clear', 'recorder_clear'])
def test_old_inflight_generation_cannot_enter_new_capture(make_owner, monkeypatch, operation):
    window, monitor, _obj = make_owner()
    snapshot = freeze(window.canvas)
    scheduler = window.canvas._scene_controller.scheduler
    entered, release = threading.Event(), threading.Event()
    original = SceneScheduler._evaluate_admitted
    def blocked(owner, demand, token):
        if demand.serial == 91:
            entered.set()
            assert release.wait(10)
        return original(owner, demand, token)
    monkeypatch.setattr(SceneScheduler, '_evaluate_admitted', blocked)
    monitor.start()
    with ThreadPoolExecutor(max_workers=1) as driver:
        future = driver.submit(evaluate, scheduler, snapshot, 91)
        assert entered.wait(10)
        if operation == 'restart':
            monitor.stop()
            monitor.start()
        elif operation == 'clear':
            monitor.clear()
        else:
            monitor.recorder.clear()
        release.set()
        future.result(timeout=20)
    assert not owned_worker_events(monitor), 'An old demand leaked through new-generation nested hooks'
    evaluate(scheduler, snapshot, 92)
    events = owned_worker_events(monitor)
    assert events and all(event['details']['demand_serial'] == 92 for event in events)
    monitor.stop()


def test_owned_scope_restores_TLS_and_application_exception(make_owner):
    _window, monitor, _obj = make_owner()
    monitor.start()
    previous = getattr(_monitor_scope, 'value', None)
    with pytest.raises(ValueError, match='Real scope failure'):
        with monitor._owned_scope({'scope': 'test', 'ownership': 'monitored_editor'}):
            raise ValueError('Real scope failure')
    assert getattr(_monitor_scope, 'value', None) is previous
    monitor.stop()


@pytest.mark.parametrize('failure', ['partial_install', 'cpp_delete'])
def test_all_new_class_alias_probes_restore_on_failure_or_deletion(make_owner, monkeypatch, failure):
    window, monitor, _obj = make_owner()
    original = {(SceneCapture, 'advance'): SceneCapture.advance,
        (SceneScheduler, '_evaluate'): SceneScheduler._evaluate,
        (SceneScheduler, '_evaluate_admitted'): SceneScheduler._evaluate_admitted,
        (effect_pipeline, 'apply_modifier_stack'): effect_pipeline.apply_modifier_stack,
        (render_modifiers, 'apply_modifier_stack'): render_modifiers.apply_modifier_stack,
        (tile_effects, 'apply_modifier_stack'): tile_effects.apply_modifier_stack,
        (interactive_effects, 'apply_modifier_stack'): interactive_effects.apply_modifier_stack,
        (interactive_effects, 'render_interactive_stack'): interactive_effects.render_interactive_stack,
        (render_modifiers, 'apply_opacity_mask'): render_modifiers.apply_opacity_mask,
        (scene_kernels, 'apply_opacity_mask'): scene_kernels.apply_opacity_mask,
        (scene_kernels, 'render_stages'): scene_kernels.render_stages}
    if failure == 'partial_install':
        patch = monitor._patch_owned
        def partial(*args, **kwargs):
            patch(*args, **kwargs)
            if args[0] is interactive_effects and args[1] == 'apply_modifier_stack':
                raise RuntimeError('Partial detached monitor install')
        monkeypatch.setattr(monitor, '_patch_owned', partial)
        with pytest.raises(RuntimeError, match='Partial detached'):
            monitor.start()
    else:
        monitor.start()
        delete_qobject(monitor)
        assert not isValid(monitor)
    assert not monitor.enabled and not monitor._patches
    assert monitor._capture_generation is None and not monitor._owned_schedulers
    assert all(getattr(owner, name) is value for (owner, name), value in original.items())
    # Window survives monitor deletion; fixture closes its ordinary resources.
    if failure == 'cpp_delete':
        window._performance_monitor_controller = None


def test_canvas_and_navigator_identity_tags_and_second_editor_exclusion(make_owner):
    window, monitor, _obj = make_owner()
    other, _other_monitor, _other_obj = make_owner()
    snapshot, other_snapshot = freeze(window.canvas), freeze(other.canvas)
    # Independent native Navigator demand follows the same actual production
    # preview path. Its output has its own dimensions and phase contract.
    control = SceneScheduler()
    try:
        expected_navigator = evaluate(control, snapshot, 110, 'navigator')
    finally:
        control.close()
        control.executor.shutdown(wait=True, cancel_futures=False)
    monitor.start()
    canvas_result = evaluate(window.canvas._scene_controller.scheduler, snapshot, 111)
    assert canvas_result
    navigator_result = evaluate(window.preview._navigator_jobs.scheduler, snapshot, 112, 'navigator')
    assert navigator_result == expected_navigator
    events = owned_worker_events(monitor)
    assert {(event['details']['scheduler_role'], event['details']['consumer'],
             event['details']['demand_serial']) for event in events} == {
        ('canvas', 'canvas', 111), ('navigator', 'navigator', 112)}
    before = len(events)
    evaluate(other.canvas._scene_controller.scheduler, other_snapshot, 113)
    assert len(owned_worker_events(monitor)) == before
    # Unattributed legacy compatibility probes can still report direct global
    # calls. They must never be relabelled as this editor's worker demand.
    assert not any(event['details'].get('demand_serial') == 113
        for event in monitor.snapshot()['timeline'])
    monitor.stop()


def test_legacy_source_alias_direct_worker_calls_still_record_as_unattributed(make_owner, monkeypatch):
    window, monitor, obj = make_owner()
    from comic_editor.ui import modifier_rendering as ui_modifiers
    assert ui_modifiers is render_modifiers
    source = QImage(window.canvas.tiles.tile(obj.object_id, (0, 0)))
    original_context = monitor.context
    gui = threading.get_ident()
    def context_only_on_gui():
        assert threading.get_ident() == gui, 'Legacy worker probe queried QWidget context'
        return original_context()
    monkeypatch.setattr(monitor, 'context', context_only_on_gui)
    monitor.start()
    def direct_source_calls():
        adjusted = render_modifiers.apply_modifier_stack(source,
            [HueSaturationLightnessModifier(hue=17)], (0., 0.))
        return render_modifiers.apply_opacity_mask(adjusted,
            np.full((64, 64), .5, dtype=np.float32), 0., 1.)
    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(direct_source_calls).result(timeout=10)
    assert 0 < result.pixelColor(32, 32).alpha() < 255
    events = [event for event in monitor.snapshot()['timeline'] if event['name'] in
        {'effects.apply_modifier_stack', 'effects.apply_opacity_mask'}]
    assert {event['name'] for event in events} == {'effects.apply_modifier_stack', 'effects.apply_opacity_mask'}
    assert all(event['details']['ownership'] == 'legacy_global_unattributed' and
        event['details']['thread'] == 'worker' and 'demand_serial' not in event['details'] for event in events)
    assert not owned_worker_events(monitor)
    monitor.stop()


def test_existing_bound_timer_consumes_pre_enable_lazy_capture_without_rebinding(make_owner, qapp, monkeypatch):
    window, monitor, _obj = make_owner()
    canvas, scene = window.canvas, window.canvas._scene_controller
    from comic_editor.render.projection import DocumentProjection
    original_advance = SceneCapture.advance
    seen = []
    def observe_actual_slice(capture, seconds=.004):
        seen.append(capture)
        return original_advance(capture, seconds)
    monkeypatch.setattr(SceneCapture, 'advance', observe_actual_slice)
    callback = scene.advance
    document, visible = canvas._render_document_state(), QRectF(0, 0, 64, 64)
    scene.request(document, tuple(DocumentProjection().requests(visible, 1.)), (None,), visible)
    capture = scene.capture
    assert capture is not None and not capture.advance(0.)
    seen.clear()
    serial = scene.serial
    monitor.start()
    # The original timer slot was connected at construction. No call to its
    # advance() method or capture.advance() occurs here after enable.
    pump_until(qapp, lambda: scene.snapshot is not None and not scene.scheduler.busy and
        any(event['name'] == 'scene.worker_envelope' for event in monitor.snapshot()['timeline']),
        'Existing bound Qt timer did not drive lazy capture and ordinary publication')
    assert capture in seen and scene.advance == callback
    slices = [event for event in monitor.snapshot()['timeline'] if event['name'] == 'scene.capture_slice']
    attributed = [event for event in slices if event['details'].get('consumer') == 'canvas']
    assert attributed and all(event['details']['demand_serial'] == serial for event in attributed)
    # Standalone lazy captures share the compiler owner but have no adapter
    # identity; their additional slices are intentionally not given a consumer.
    assert all(event['details']['ownership'] == 'monitored_editor' for event in slices)
    names = {event['name'] for event in monitor.snapshot()['timeline']}
    assert {'scene.publish', 'scene.submit', 'scene.worker_admitted'} <= names
    monitor.stop()


def test_pre_enable_submitted_worker_is_unobserved_until_new_demand(make_owner, monkeypatch):
    window, monitor, _obj = make_owner()
    snapshot, scheduler = freeze(window.canvas), window.canvas._scene_controller.scheduler
    entered, release = threading.Event(), threading.Event()
    original = SceneScheduler._evaluate
    calls = []
    def submitted_before_enable(owner, demand, token):
        calls.append(demand.serial)
        if demand.serial == 121:
            entered.set()
            assert release.wait(10)
        return original(owner, demand, token)
    monkeypatch.setattr(SceneScheduler, '_evaluate', submitted_before_enable)
    with ThreadPoolExecutor(max_workers=1) as driver:
        future = driver.submit(evaluate, scheduler, snapshot, 121)
        assert entered.wait(10)
        monitor.start()
        release.set()
        future.result(timeout=20)
    assert not owned_worker_events(monitor)
    evaluate(scheduler, snapshot, 122)
    assert calls == [121, 122]
    assert owned_worker_events(monitor)
    assert all(event['details']['demand_serial'] == 122 for event in owned_worker_events(monitor))
    assert 'submitted before enable' in monitor.snapshot()['instrumentation']['measurement_boundary']
    monitor.stop()


def test_restart_during_worker_wrapper_epoch_read_rejects_old_generation(make_owner, monkeypatch):
    window, monitor, _obj = make_owner()
    snapshot, scheduler = freeze(window.canvas), window.canvas._scene_controller.scheduler
    monitor.start()
    entered, release = threading.Event(), threading.Event()
    original = PerformanceRecorder.capture_epoch.fget
    blocked = []
    def capture_epoch(recorder):
        if (recorder is monitor.recorder and threading.current_thread().name.startswith('scene-evaluate')
                and not blocked):
            blocked.append(True)
            entered.set()
            assert release.wait(10)
        return original(recorder)
    monkeypatch.setattr(PerformanceRecorder, 'capture_epoch', property(capture_epoch))
    with ThreadPoolExecutor(max_workers=1) as driver:
        future = driver.submit(evaluate, scheduler, snapshot, 125)
        assert entered.wait(10)
        monitor.stop()
        monitor.start()
        release.set()
        future.result(timeout=20)
    assert not owned_worker_events(monitor), 'Old wrapper paired its generation with the restarted epoch'
    evaluate(scheduler, snapshot, 126)
    assert owned_worker_events(monitor)
    assert all(event['details']['demand_serial'] == 126 for event in owned_worker_events(monitor))
    monitor.stop()


def test_original_exception_runs_once_TLS_restores_and_retained_wrapper_is_weak(make_owner, monkeypatch):
    window, monitor, _obj = make_owner()
    capture = window.canvas._scene_snapshot_compiler.capture(window.canvas, window.canvas._render_document_state())
    calls = []
    def failure(_capture, _seconds=.004):
        calls.append(True)
        raise ValueError('Application capture failure')
    monkeypatch.setattr(SceneCapture, 'advance', failure)
    monitor.start()
    retained = SceneCapture.advance
    previous = getattr(_monitor_scope, 'value', None)
    with pytest.raises(ValueError, match='Application capture failure'):
        capture.advance(0.)
    assert calls == [True] and getattr(_monitor_scope, 'value', None) is previous
    recorded = [event for event in monitor.snapshot()['timeline'] if event['name'] == 'scene.capture_slice']
    assert len(recorded) == 1 and recorded[0]['details']['exception_type'] == 'ValueError'
    monitor.stop()
    before = len(monitor.snapshot()['timeline'])
    with pytest.raises(ValueError, match='Application capture failure'):
        retained(capture, 0.)
    assert calls == [True, True] and len(monitor.snapshot()['timeline']) == before
    # A class wrapper held by an executor cannot retain its stopped monitor.
    window._performance_monitor_controller = None
    # Qt owns the controller until deletion; after native deletion the Python
    # wrapper's closure must contain only a weak reference to it.
    delete_qobject(monitor)
    cells = [cell.cell_contents for cell in (retained.__closure__ or ())]
    assert all(value is not monitor for value in cells)
    assert any(isinstance(value, weakref.ReferenceType) and value() is monitor for value in cells)
    assert not monitor._patches and not monitor.enabled


def source_payload(canvas):
    return {(owner, key): (image.size(), image.format(), image.bytesPerLine(), bytes(image.constBits()))
        for owner, tiles in canvas.tiles._tiles.items() for key, image in tiles.items()}


def pointer_contact(canvas, qapp):
    revision = canvas.command_stack.revision
    for index, event_type in enumerate((QEvent.MouseButtonPress, QEvent.MouseMove, QEvent.MouseButtonRelease)):
        position = canvas.document_to_widget(QPointF(22 + index * 7, 26 + index * 4))
        button = Qt.NoButton if index == 1 else Qt.LeftButton
        buttons = Qt.NoButton if index == 2 else Qt.LeftButton
        QCoreApplication.sendEvent(canvas, QMouseEvent(event_type, position, position,
            button, buttons, Qt.NoModifier))
    pump_until(qapp, lambda: not canvas._drawing and canvas.command_stack.revision > revision,
        'Released real pointer contact did not finish its accepted source/history transaction')


def pump_until(qapp, predicate, message, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.002)
    pytest.fail(message)


def test_native_gpu_pointer_edit_whole_output_model_and_sources_equal_disabled_control(make_owner, qapp, monkeypatch):
    if qapp.platformName() == 'offscreen':
        pytest.skip('Native QOpenGLWidget presentation is unavailable on offscreen platform')
    window, monitor, obj = make_owner('gpu')
    control, _control_monitor, _unused = make_owner('gpu')
    alias_original = interactive_effects.apply_modifier_stack
    owned_alias_calls = []
    def observe_actual_gpu_scene_alias(image, *args, **kwargs):
        token = monitor._current_scope()
        if monitor.enabled and token is not None:
            owned_alias_calls.append(alias_scope_observation(monitor, image))
        return alias_original(image, *args, **kwargs)
    monkeypatch.setattr(interactive_effects, 'apply_modifier_stack', observe_actual_gpu_scene_alias)
    assert isinstance(window.canvas, GpuCanvasWidget) and isinstance(control.canvas, GpuCanvasWidget)
    original_sources = source_payload(window.canvas)
    cloned_tiles = TileStore(tile_size=64)
    for owner, tiles in window.canvas.tiles._tiles.items():
        for key, image in tiles.items():
            cloned_tiles.set_tile(owner, key, image)
    cloned_chapter = ChapterDocument.from_dict(copy.deepcopy(window.canvas.chapter.to_dict()))
    control._set_chapter(cloned_chapter, cloned_tiles)
    for owner in (control, window):
        owner.canvas.set_selection('object', obj.object_id)
        assert owner.canvas.set_tool(ToolKind.RASTER_PENCIL)
        owner.canvas.settings.predictive_ink = False
        owner.setUpdatesEnabled(True)
        owner.show()
    # Let both native surfaces initialize with normal event-loop work.
    pump_until(qapp, lambda: getattr(window.canvas, '_projection_presented_revision', -1) >= 0 and
        getattr(control.canvas, '_projection_presented_revision', -1) >= 0,
        'Initial GPU projection did not become ready')
    pointer_contact(control.canvas, qapp)
    pump_until(qapp, lambda: not control.canvas._scene_controller.scheduler.busy and
        control.canvas._projection_presented_revision == control.canvas._document_projection.revision,
        'Disabled control did not present its edited revision')
    expected_model, expected_sources = copy.deepcopy(control.canvas.chapter.to_dict()), source_payload(control.canvas)
    assert expected_sources != original_sources, 'Pointer control failed to change native source pixels'
    monitor.start()
    swapped = []
    window.canvas.frameSwapped.connect(lambda: swapped.append(True))
    pointer_contact(window.canvas, qapp)
    revision = window.canvas._document_projection.revision
    pump_until(qapp, lambda: swapped and not window.canvas._scene_controller.scheduler.busy and
        window.canvas._projection_presented_revision == revision and
        any(event['name'] == 'scene.worker_envelope' and event['details'].get('document_revision') == revision
            for event in monitor.snapshot()['timeline']), 'Monitored pointer GPU revision was not observed')
    events = monitor.snapshot()['timeline']
    names = {event['name'] for event in events}
    assert {'canvas.paint_canvas_frame', 'canvas.paint_ready_document_projection',
        'scene.capture_slice', 'scene.worker_envelope', 'scene.worker_admitted',
        'scene.effects.stack', 'scene.effects.opacity_mask', 'mouse.press', 'mouse.release'} <= names
    assert owned_alias_calls and any(row['scope']['ownership'] == 'monitored_editor' and
        row['scope']['scope'] == 'detached_worker' and row['scope']['scheduler_role'] == 'canvas' and
        row['scope']['document_revision'] == revision and row['thread_id'] != threading.get_ident()
        for row in owned_alias_calls), 'GPU pointer revision missed actual scoped CPU effect alias'
    assert window.canvas.chapter.to_dict() == expected_model
    assert source_payload(window.canvas) == expected_sources
    monitor.stop()
    assert interactive_effects.apply_modifier_stack is observe_actual_gpu_scene_alias
    actual_snapshot, control_snapshot = freeze(window.canvas), freeze(control.canvas)
    # Compare complete native tile buffers with independent cold scene owners.
    first, second = SceneScheduler(), SceneScheduler()
    try:
        assert evaluate(first, actual_snapshot, 131) == evaluate(second, control_snapshot, 132)
    finally:
        for scheduler in (first, second):
            scheduler.close()
            scheduler.executor.shutdown(wait=True, cancel_futures=False)



def large_alias_scene(window, *, local_halo=False):
    """A real native raster spans the TileGraph's >256*256 eligibility edge."""
    from comic_editor.core.models import DitheringModifier, SharpnessModifier
    # Exact document capture groups contain 4 x 4 native 256px tiles.
    # A local-halo source must span more than one group for a real partial ROI.
    width, height = (1281, 1153) if local_halo else (321, 289)
    chapter = ChapterDocument(width=width, height=height, document_kind='asset',
        background='#00000000')
    page = chapter.add_page('Actual imported alias fixture',
        BoundGeometry.rectangle(0, 0, width, height))
    page.fill_color, page.border_width = None, 0
    obj = chapter.add_object(page.layer_id, RasterObject(tile_size=64,
        interaction_rect=(0, 0, width, height)))
    source = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor('#ff5e718f'))
    painter = QPainter(source)
    try:
        for y in range(0, height, 17):
            painter.fillRect(0, y, width, 8,
                QColor((31 + y) % 256, (113 + y // 2) % 256, (211 - y) % 256, 255))
        painter.fillRect(13, 21, 53, 79, QColor('#ffcc2070'))
        painter.fillRect(273, 245, 41, 37, QColor('#ff309050'))
    finally:
        painter.end()
    tiles = TileStore(tile_size=64)
    for y in range((height + 63) // 64):
        for x in range((width + 63) // 64):
            tile = QImage(64, 64, QImage.Format_ARGB32_Premultiplied)
            tile.fill(QColor('transparent'))
            painter = QPainter(tile)
            try:
                painter.drawImage(-x * 64, -y * 64, source)
            finally:
                painter.end()
            tiles.set_tile(obj.object_id, (x, y), tile)
    effect = (SharpnessModifier(radius=1.25, strength=83, threshold=0)
        if local_halo else DitheringModifier(method='ordered', levels=5, strength=73,
            pixel_size=1., matrix_size=4, seed=37))
    chapter.add_modifier(effect, [('object', obj.object_id)])
    window._set_chapter(chapter, tiles)
    window.canvas._scene_controller.reset()
    return obj, effect


def evaluate_whole_alias_scene(scheduler, snapshot, serial, *, requested_world=None):
    """Only submit/poll real demands; never invoke an effect alias directly."""
    from comic_editor.render.projection import DocumentProjection
    document = snapshot.document
    requested = (QRectF(0, 0, document.width, document.height)
        if requested_world is None else QRectF(requested_world))
    assert not requested.isEmpty() and QRectF(0, 0, document.width, document.height).contains(requested)
    requests = tuple(DocumentProjection().requests(requested, 1.))
    assert requests and all(request.scale == 1. and request.tile_size == 256 for request in requests)
    scheduler.submit(SceneDemand(serial, snapshot, requests, (None,),
        (requested.center().x(), requested.center().y())))
    completed = []
    deadline = time.monotonic() + 30
    while scheduler.busy:
        assert time.monotonic() < deadline, 'Whole native imported-alias render did not finish'
        completed.extend(scheduler.poll())
        time.sleep(.002)
    completed.extend(scheduler.poll())
    assert completed and completed[-1].done
    assert not any(item.error for item in completed), [item.error for item in completed if item.error]
    actual = {address: image for item in completed
        for address, (image, exact) in (item.tiles or {}).items() if exact}
    assert set(actual) == {request.address for request in requests}
    if requested_world is None:
        assert len(actual) >= 4
    assert actual and any(image.pixelColor(100, 100).alpha() > 0 for image in actual.values())
    return {address: (image.size(), image.format(), image.bytesPerLine(), bytes(image.constBits()))
        for address, image in actual.items()}


def alias_scope_observation(monitor, image):
    token = monitor._current_scope()
    return {'scope': token.details() if token is not None else None,
        'thread_id': threading.get_ident(), 'image_size': (image.width(), image.height())}


def test_actual_scope_free_bake_full_stage_executes_pipeline_bound_alias_owned(make_owner, monkeypatch):
    """The production bake consumer supplies scope=None to the real stage renderer.

    Ordinary scene demands always use an entity scope and take the interactive
    stack adapter. A test-only admitted-job adapter runs actual immutable bake
    source preparation before ordinary scene rendering, under the existing
    monitored scheduler envelope. It never calls either wrapped alias directly.
    """
    from comic_editor.render.bake_sources import applied_raster_sources
    window, monitor, _unused = make_owner()
    obj, effect = large_alias_scene(window)
    snapshot = freeze(window.canvas)
    before_model, before_source = copy.deepcopy(window.canvas.chapter.to_dict()), source_payload(window.canvas)
    alias_original = effect_pipeline.apply_modifier_stack
    admitted_original = SceneScheduler._evaluate_admitted
    observed, bakes, gate = [], {}, threading.Lock()
    def observe_native_alias(image, *args, **kwargs):
        if monitor.enabled:
            with gate:
                observed.append(alias_scope_observation(monitor, image))
        return alias_original(image, *args, **kwargs)
    def prepare_real_bake_then_render(owner, demand, cancelled):
        prepared = applied_raster_sources(demand.snapshot, effect.modifier_id,
            (('object', obj.object_id),))
        assert len(prepared.sources) == 1
        output = prepared.sources[0]
        assert output.identifier == obj.object_id and output.baked == (effect.modifier_id,)
        payload = (tuple(output.bounds.getRect()),
            {address: (image.size(), image.format(), image.bytesPerLine(), bytes(image.constBits()))
             for address, image in output.tiles.items()})
        with gate:
            bakes[demand.serial] = payload
        return admitted_original(owner, demand, cancelled)
    monkeypatch.setattr(effect_pipeline, 'apply_modifier_stack', observe_native_alias)
    monkeypatch.setattr(SceneScheduler, '_evaluate_admitted', prepare_real_bake_then_render)
    control = SceneScheduler()
    try:
        expected = evaluate_whole_alias_scene(control, snapshot, 201)
    finally:
        control.close()
        control.executor.shutdown(wait=True, cancel_futures=False)
    assert bakes[201][1] and not observed
    monitor.start()
    actual = evaluate_whole_alias_scene(window.canvas._scene_controller.scheduler, snapshot, 202)
    assert actual == expected and bakes[202] == bakes[201]
    assert observed and all(row['scope'] is not None for row in observed)
    assert all(row['scope']['ownership'] == 'monitored_editor' and
        row['scope']['scope'] == 'detached_worker' and row['scope']['scheduler_role'] == 'canvas' and
        row['scope']['demand_serial'] == 202 and row['scope']['document_revision'] == snapshot.document.revision
        and row['thread_id'] != threading.get_ident() and row['image_size'][0] > 256 and
        row['image_size'][1] > 256 for row in observed)
    events = owned_worker_events(monitor)
    assert any(event['name'] == 'scene.effects.stack' and event['details']['demand_serial'] == 202 for event in events)
    assert window.canvas.chapter.to_dict() == before_model and source_payload(window.canvas) == before_source
    monitor.stop()
    assert effect_pipeline.apply_modifier_stack is observe_native_alias
    assert SceneScheduler._evaluate_admitted is prepare_real_bake_then_render


def test_actual_large_native_tile_graph_executes_tile_bound_alias_owned(make_owner, monkeypatch):
    """An ordinary cold detached scene exercises the >256px local TileGraph."""
    window, monitor, _unused = make_owner()
    _obj, _effect = large_alias_scene(window, local_halo=True)
    from comic_editor.ui.point_lut import _supported
    halo = tile_effects.footprint(_effect)
    assert isinstance(halo, int) and halo > 0 and not _supported(_effect)
    assert not _effect.parameter_masks, 'This CPU Sharpness route requires a finite local halo'
    snapshot = freeze(window.canvas)
    before_model, before_source = copy.deepcopy(window.canvas.chapter.to_dict()), source_payload(window.canvas)
    alias_original, tile_original = tile_effects.apply_modifier_stack, tile_effects.tile_output
    observed, eligible_outputs, gate = [], [], threading.Lock()
    def observe_native_alias(image, *args, **kwargs):
        if monitor.enabled:
            with gate:
                observed.append(alias_scope_observation(monitor, image))
        return alias_original(image, *args, **kwargs)
    def observe_actual_tile_output(scene, image, bounds, modifiers, mapping, **kwargs):
        eligible = tile_effects.eligible(scene, kwargs.get('required'), kwargs.get('request_scope'), bounds, modifiers)
        result = tile_original(scene, image, bounds, modifiers, mapping, **kwargs)
        if monitor.enabled and eligible and result is not None:
            token = monitor._current_scope()
            with gate:
                eligible_outputs.append({'bounds': tuple(bounds.getRect()),
                    'scope': token.details() if token is not None else None,
                    'required': tuple(kwargs['required'].getRect()),
                    'request_scope': repr(kwargs['request_scope']),
                    'footprints': tuple(tile_effects.footprint(effect) for effect in modifiers)})
        return result
    monkeypatch.setattr(tile_effects, 'apply_modifier_stack', observe_native_alias)
    monkeypatch.setattr(tile_effects, 'tile_output', observe_actual_tile_output)
    control = SceneScheduler()
    try:
        expected = evaluate_whole_alias_scene(control, snapshot, 211)
    finally:
        control.close()
        control.executor.shutdown(wait=True, cancel_futures=False)
    requested_world = QRectF(32, 40, 160, 170)
    # This independent cold control renders the actual smaller ordinary demand.
    roi_control = SceneScheduler()
    try:
        expected_roi = evaluate_whole_alias_scene(roi_control, snapshot, 215,
            requested_world=requested_world)
    finally:
        roi_control.close()
        roi_control.executor.shutdown(wait=True, cancel_futures=False)
    assert len(expected_roi) == 1 and set(expected_roi) < set(expected)
    assert expected_roi == {address: expected[address] for address in expected_roi}
    assert not observed and not eligible_outputs
    monitor.start()
    scheduler = window.canvas._scene_controller.scheduler
    actual_roi = evaluate_whole_alias_scene(scheduler, snapshot, 212, requested_world=requested_world)
    assert actual_roi == expected_roi
    assert eligible_outputs and all(row['scope'] is not None and row['scope']['demand_serial'] == 212
        for row in eligible_outputs), 'The ordinary smaller request did not enter the real local TileGraph'
    assert observed and all(row['scope'] is not None and row['scope']['demand_serial'] == 212
        for row in observed), 'The ordinary smaller request did not call the actual CPU tile alias'
    # Retain complete native-document equality after the real partial demand.
    actual = evaluate_whole_alias_scene(scheduler, snapshot, 214)
    assert actual == expected
    assert actual_roi == {address: actual[address] for address in actual_roi}
    assert eligible_outputs and all(row['bounds'][2] > 256 and row['bounds'][3] > 256 and
        row['request_scope'] and row['footprints'] == (halo,) and
        row['required'][2] * row['required'][3] < row['bounds'][2] * row['bounds'][3]
        for row in eligible_outputs)
    assert observed and all(row['scope'] is not None and
        row['scope']['ownership'] == 'monitored_editor' and row['scope']['scope'] == 'detached_worker' and
        row['scope']['scheduler_role'] == 'canvas' and row['scope']['demand_serial'] in (212, 214) and
        row['scope']['document_revision'] == snapshot.document.revision and
        row['thread_id'] != threading.get_ident() and
        0 < row['image_size'][0] <= 256 + 2 * halo and 0 < row['image_size'][1] <= 256 + 2 * halo and
        row['image_size'][0] < snapshot.document.width and row['image_size'][1] < snapshot.document.height
        for row in observed), 'Actual tile effect did not use bounded native CPU halo inputs'
    assert any(event['name'] == 'scene.effects.stack' and event['details']['demand_serial'] == 212
        for event in owned_worker_events(monitor))
    count = len(owned_worker_events(monitor))
    unrelated = SceneScheduler()
    try:
        assert evaluate_whole_alias_scene(unrelated, snapshot, 213) == expected
    finally:
        unrelated.close()
        unrelated.executor.shutdown(wait=True, cancel_futures=False)
    assert len(owned_worker_events(monitor)) == count
    assert any(row['scope'] is None for row in observed), 'Unrelated cold renderer failed to exercise the actual alias'
    assert window.canvas.chapter.to_dict() == before_model and source_payload(window.canvas) == before_source
    monitor.stop()
    assert tile_effects.apply_modifier_stack is observe_native_alias
    assert tile_effects.tile_output is observe_actual_tile_output
