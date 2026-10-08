"""Unrun native/cold/supersession guards for the admitted exact worker token.

The independent reference is the byte-pinned original R12 pipeline. The
kernel, global sampling coordinates, count and dtype remain the production
ones. Controlled events stop at actual SciPy samples, not fake rendered pixels.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import importlib.util
from pathlib import Path
from threading import Event, Lock
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.models import ParameterMaskBinding, RadialBlurModifier, RasterObject, ToneMask
from comic_editor.render.pixels import LEGACY_PIXELS, FLOAT_PIXELS, pixel_scope
from comic_editor.render.scheduler import SceneDemand, SceneScheduler, _scene_cancellation
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, TileBatchPolicy
from comic_editor.ui.radial_pipeline import render_radial_stage
from test_detached_scene import canvas, freeze
from test_effect_regions import scene, image


def _reference():
    path = Path(__file__).resolve().parent/'references/radial_pipeline_native_r12.py'
    expected = '37ef05e74181400b8e2fa524cc5761679b44ce593d133d52d189e8c072cdbadd'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    spec = importlib.util.spec_from_file_location('_original_radial_pipeline_r12', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render_radial_stage


def _bytes(value):
    return (value.format().value, value.width(), value.height(), value.bytesPerLine(),
            value.devicePixelRatio(), bytes(value.colorSpace().iccProfile()), bytes(value.constBits()))


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('raises', [False, True])
def test_borrowed_token_restores_absence_previous_value_and_nested_exception(existing, raises):
    owner = SimpleNamespace()
    prior, outer, inner = object(), lambda: False, lambda: True
    if existing:
        owner._scene_cancelled = prior
    try:
        with _scene_cancellation(owner, outer):
            assert owner._scene_cancelled is outer
            with _scene_cancellation(owner, inner):
                assert owner._scene_cancelled is inner and owner._scene_cancelled()
            assert owner._scene_cancelled is outer and not owner._scene_cancelled()
            if raises:
                raise RuntimeError('ordinary native capture failed')
    except RuntimeError as error:
        assert raises and str(error) == 'ordinary native capture failed'
    if existing:
        assert owner._scene_cancelled is prior
    else:
        assert '_scene_cancelled' not in vars(owner)


@pytest.mark.parametrize('contract', [LEGACY_PIXELS, replace(FLOAT_PIXELS, precision='float16'), FLOAT_PIXELS])
@pytest.mark.parametrize('intensity', [0., 43.75, 100.])
def test_uncancelled_original_native_bits_masks_and_precision(scene, monkeypatch, contract, intensity):
    from comic_editor.ui import radial_blur
    scene.chapter.pixel_contract = contract
    modifier = RadialBlurModifier(center=(27., 19.), angle=18., intensity=intensity)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0., intensity)
    scene.chapter.modifiers[modifier.modifier_id] = modifier
    fields = {(modifier.modifier_id, 'intensity'):
        np.broadcast_to(np.linspace(0., 1., 61, dtype=np.float32), (47, 61))}
    mapping, origin = QTransform(1., .03, -.04, 1., -7., 2.), (-3., -5.)
    callbacks = []
    original = radial_blur.radial_blur
    def observed(*args, **kwargs):
        callbacks.append(kwargs.get('cancelled'))
        return original(*args, **kwargs)
    monkeypatch.setattr(radial_blur, 'radial_blur', observed)
    with pixel_scope(contract):
        source = image(61, 47)
        raw = _bytes(source)
        common = dict(scope=('native-reference',), asynchronous=False, deferred=False,
                      provisional=False, navigator=False, exact=True)
        expected, old_provisional = _reference()(scene, source, source, modifier, fields,
            mapping, origin, source_key=('old-original',), **common)
        token = Event()
        with _scene_cancellation(scene, token.is_set):
            actual, provisional = render_radial_stage(scene, source, source, modifier, fields,
                mapping, origin, source_key=('current-uncancelled',), **common)
        assert not token.is_set() and not provisional and not old_provisional
        assert _bytes(actual) == _bytes(expected)
        assert actual.format() == contract.image_format and _bytes(source) == raw
    assert len(callbacks) == 2 and callbacks[0] is None and callable(callbacks[1])
    assert '_scene_cancelled' not in vars(scene)


def _ordinary_sampler(module):
    """Observe the actual whole RGBA sampler when available, original otherwise.

    The selected function delegates unchanged: no component disabling, pixel
    replacement, callback change or hidden fallback. Native C and its ordinary
    unsupported SciPy fallback both pass through this owned sample seam.
    """
    value = getattr(module, '_rgba_linear_sample', None)
    return ('_rgba_linear_sample', value) if callable(value) else ('map_coordinates', module.map_coordinates)


@pytest.mark.parametrize('when', ['before', 'during'])
def test_actual_kernel_cancellation_admits_no_partial_integration_or_result(scene, monkeypatch, when):
    from comic_editor.ui import radial_blur
    token, calls = Event(), []
    source, modifier = image(61, 47), RadialBlurModifier(center=(150., 110.), angle=40.)
    scene.chapter.modifiers[modifier.modifier_id] = modifier
    sampler_name, original = _ordinary_sampler(radial_blur)
    def sample(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 4:
            token.set()
        return result
    monkeypatch.setattr(radial_blur, sampler_name, sample)
    if when == 'before':
        token.set()
    before_retained = tuple(scene._effect_jobs.retained)
    with _scene_cancellation(scene, token.is_set), pytest.raises(radial_blur.RadialRenderCancelled):
        render_radial_stage(scene, source, source, modifier, {}, QTransform(), (0., 0.),
            source_key=('cancelled-source',), scope=('cancelled-integration',), asynchronous=False,
            deferred=False, provisional=False, navigator=False, exact=True)
    assert token.is_set() and '_scene_cancelled' not in vars(scene)
    assert not any(key[0] == 'radial-integration' for key in scene._modifier_render_cache)
    assert tuple(scene._effect_jobs.retained) == before_retained
    assert not scene._effect_jobs.running_jobs and not scene._effect_jobs.pending
    if when == 'before':
        assert not calls
    else:
        assert 4 <= len(calls) <= 64, 'Existing every16samples cancellation check did not retire the work'


def _small_radial(canvas):
    page = canvas.chapter.root_page_ids[0]
    obj = canvas.chapter.add_object(page, RasterObject(interaction_rect=(0., 0., 64., 64.)))
    modifier = RadialBlurModifier(center=(90., 80.), angle=40.)
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    canvas.tiles.set_tile(obj.object_id, (0, 0), image(256, 256))
    canvas._invalidate_scene_cache()
    return obj, modifier


@pytest.mark.parametrize('record', [False, True])
@pytest.mark.parametrize('stop_owner', [False, True])
def test_real_scheduler_retires_superseded_native_work_and_preserves_current_cold_disk(canvas,
        monkeypatch, tmp_path, record, stop_owner):
    from comic_editor.ui import radial_blur
    from comic_editor.render.cache import PersistentRenderCache
    from comic_editor.ui.cache_dependencies import RenderDependencies
    from comic_editor.render.pixels import color_environment
    obj, modifier = _small_radial(canvas)
    contract = canvas.chapter.pixel_contract
    backing = PersistentRenderCache(tmp_path/'ordinary-cache', contract=contract.signature,
        environment=(*RenderDependencies.environment(), color_environment(contract)))
    canvas._persistent_render_cache = backing
    entered, released = Event(), Event()
    old_kernel_cancelled = []
    closed, samples, sample_lock = [], [0], Lock()
    sampler_name, original_sample = _ordinary_sampler(radial_blur)
    original_close = DetachedSceneBackend.close
    def sample(*args, **kwargs):
        result = original_sample(*args, **kwargs)
        with sample_lock:
            samples[0] += 1
            first = samples[0] == 4
        if first:
            entered.set()
            assert released.wait(5), 'Ordinary scheduler never received the replacement/stop request'
        return result
    original_kernel = radial_blur.radial_blur
    def kernel(*args, **kwargs):
        assert callable(kwargs.get('cancelled')), 'Exact worker lost its admitted token'
        try:
            return original_kernel(*args, **kwargs)
        except radial_blur.RadialRenderCancelled:
            old_kernel_cancelled.append(1)
            raise
    def close(owner):
        closed.append((owner.snapshot.document.revision,
            '_scene_cancelled' in vars(owner.scene),
            [key for key in owner.scene._modifier_render_cache if key[0] == 'radial-integration']))
        return original_close(owner)
    monkeypatch.setattr(radial_blur, sampler_name, sample)
    monkeypatch.setattr(radial_blur, 'radial_blur', kernel)
    monkeypatch.setattr(DetachedSceneBackend, 'close', close)
    old_snapshot = freeze(canvas)
    old_model = canvas.chapter.to_dict()
    old_history = vars(canvas).get('_history_generation', 0)
    old_native_owner = old_snapshot.tiles._tiles[obj.object_id]
    old_source_version = old_native_owner.version((0, 0))
    old_source_pixels = _bytes(old_snapshot.tiles.tile(obj.object_id, (0, 0)))
    requests = tuple(canvas._document_projection.requests(QRectF(0., 0., 64., 64.), 1.))
    assert requests
    scheduler = SceneScheduler(handoff_budget=2*1024*1024)
    try:
        scheduler.submit(SceneDemand(1, old_snapshot, requests, (None,), (32., 32.), record=record))
        assert entered.wait(5), 'Actual exact radial kernel did not execute'
        old_future = scheduler.future
        if stop_owner:
            scheduler.close()
        else:
            tile = QImage(256, 256, contract.image_format)
            tile.fill(QColor('blue'))
            canvas.tiles.set_tile(obj.object_id, (0, 0), tile)
            canvas._invalidate_scene_cache()
            current = freeze(canvas)
            assert current.document.revision > old_snapshot.document.revision
            current_native_owner = current.tiles._tiles[obj.object_id]
            assert current_native_owner is not old_native_owner
            assert current_native_owner.version((0, 0)) != old_source_version
            assert _bytes(current.tiles.tile(obj.object_id, (0, 0))) != old_source_pixels
            assert old_native_owner.version((0, 0)) == old_source_version
            assert _bytes(old_snapshot.tiles.tile(obj.object_id, (0, 0))) == old_source_pixels
            assert canvas.chapter.to_dict() == old_model
            assert vars(canvas).get('_history_generation', 0) == old_history
            scheduler.submit(SceneDemand(2, current, requests, (None,), (32., 32.), record=record))
        released.set()
        old_future.result(timeout=5)
        assert old_kernel_cancelled == [1]
        assert closed and closed[0] == (old_snapshot.document.revision, False, [])
        assert not scheduler.ready, 'Canceled native pixels or errors were published'
        index = json_index = None
        backing.drain()
        index = backing.root/'index.json'
        if index.exists():
            import json
            json_index = json.loads(index.read_text())
            assert not any(entry['kind'] == 'projection' for entry in json_index['entries'].values())
            assert 'radial-integration' not in json.dumps(json_index['entries'])
        if stop_owner:
            assert scheduler.stopped.is_set() and scheduler.pending is None
            return
        completions = []
        deadline = time.monotonic()+10
        while scheduler.busy and time.monotonic() < deadline:
            completions.extend(scheduler.poll())
            time.sleep(.002)
        completions.extend(scheduler.poll())
        assert not scheduler.busy and scheduler.submitted == 2
        assert completions and all(value.demand.serial == 2 for value in completions)
        assert not any(value.error for value in completions) and any(value.done for value in completions)
        reference_calls = []
        def reference_kernel(*args, **kwargs):
            reference_calls.append(1)
            return original_kernel(*args, **kwargs)
        monkeypatch.setattr(radial_blur, 'radial_blur', reference_kernel)
        independent_snapshot = replace(current, cache_spec=None)
        assert independent_snapshot.document == current.document
        def independently_render():
            owner = DetachedSceneBackend(independent_snapshot)
            assert owner.scene._persistent_render_cache is None
            service = DocumentRenderService(owner)
            service.projection.revision = current.document.revision
            try:
                return service.render_tiles(current.document, list(requests), TileBatchPolicy((32.,32.)))
            finally:
                owner.close()
        with ThreadPoolExecutor(max_workers=1) as worker:
            reference = worker.submit(independently_render).result(timeout=10)
        assert not reference.pending and not reference.error and all(exact for _image, exact in reference.tiles.values())
        assert reference_calls, 'Cold reference read a completed effect instead of integrating actual native input'
        if record:
            assert all(value.recorded for value in completions)
            current_owner = scheduler._backend
            assert current_owner is not None and current_owner.snapshot.document == current.document
            # This is the ordinary recording completion method, on its worker
            # lane, before opening the independent disk reader. No manual put
            # or cache-key substitution manufactures a missing projection.
            scheduler.executor.submit(current_owner.finish_recording).result(timeout=5)
            current_backing = current_owner.scene._persistent_render_cache
            assert current_backing is not None and not current_backing.error
            assert not current_backing.writes and not current_backing.staged
            assert current_backing.publication is None
            manifest = scheduler.executor.submit(current_owner.record_manifest).result(timeout=5)
            assert manifest is not None and manifest[0]
            assert any(entry['kind'] == 'projection' for entry in manifest[0].values())
            reader = DetachedSceneBackend(current)
            try:
                for request in requests:
                    disk = reader.lookup_tile(request, None)
                    assert disk is not None and _bytes(disk) == _bytes(reference.tiles[request.address][0])
            finally:
                reader.close()
        else:
            actual = {address: value for completion in completions for address,value in (completion.tiles or {}).items()}
            assert actual.keys() == reference.tiles.keys()
            for address,(result,exact) in actual.items():
                assert exact and _bytes(result) == _bytes(reference.tiles[address][0])
    finally:
        released.set()
        scheduler.close()
        scheduler.executor.shutdown(wait=True, cancel_futures=True)
        canvas._persistent_render_cache = None
        backing.close()
