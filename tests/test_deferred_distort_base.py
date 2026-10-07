"""Full-strength deferred CPU warps need no native padded blend image."""
from threading import Event, get_ident

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.models import ParameterMaskBinding, ToneMask
from comic_editor.core.pixel_contract import LEGACY_PIXELS, PixelContract
from comic_editor.render.pixels import pixel_scope, working_image
from comic_editor.ui.async_projection import ProjectionPending
from comic_editor.ui import effect_pipeline, distort_pipeline
from test_effect_regions import scene, register
from test_tile_graph_assembly import bits
from test_tile_graph_pressure import spatial, clear


CONTRACTS = [LEGACY_PIXELS,
    PixelContract(version=2, precision='float16', working_space='linear_srgb'),
    PixelContract(version=2, precision='float32', working_space='linear_srgb')]


def source_pixels():
    yy, xx = np.mgrid[:173, :181]
    alpha = ((xx + yy) % 139) / 138.
    return np.stack((1.75 * xx / 181, -.125 + yy / 173, (xx % 7) / 7,
                     np.ones_like(xx)), axis=-1) * alpha[..., None]


@pytest.mark.parametrize('contract', CONTRACTS)
@pytest.mark.parametrize('kind', ['twirl', 'deform', 'mesh', 'lens', 'pinch'])
@pytest.mark.parametrize('projective', [False, True])
@pytest.mark.parametrize('partial', [False, True])
def test_deferred_base_free_output_matches_original_native_padded_path(
        scene, monkeypatch, contract, kind, projective, partial):
    effect = spatial(kind)
    effect.intensity = 100
    register(scene, [effect])
    scene.chapter.pixel_contract = contract
    scene._interactive_render = scene._projection_exact = True
    scene._effect_region_requests = False
    mapping = (QTransform(1.1, .13, .00015, -.07, .9, -.00021, 13.375, 22.625, 1.) if projective
               else QTransform().translate(13.375, 22.625).rotate(19.25).scale(1.25, .73))
    bounds = QRectF(-37, -61, 181, 173)
    arguments = dict(source_key=('base-free-native-source', contract.signature),
        request_scope=('object', 'base-free', 'canvas'), tile_evaluation=False,
        required=QRectF(-11, -19, 129, 113) if partial else None)
    with pixel_scope(contract):
        source = working_image(source_pixels(), contract)
        oracle = effect_pipeline.render_stages(scene, source, bounds, [effect], mapping, **arguments)
        clear(scene)
        scene._projection_defer_effects = True
        owner_thread = get_ident()
        calls = []
        original = distort_pipeline.render_distort_stage
        def stage(canvas, incoming, base, *args, **kwargs):
            calls.append((base, kwargs['base_size']))
            assert base is None
            return original(canvas, incoming, base, *args, **kwargs)
        monkeypatch.setattr(distort_pipeline, 'render_distort_stage', stage)
        def forbidden(_):
            pytest.fail('deferred full-strength warp allocated a padded blend image')
        monkeypatch.setattr(effect_pipeline, 'empty_image', forbidden)
        with pytest.raises(ProjectionPending):
            effect_pipeline.render_stages(scene, source, bounds, [effect], mapping, **arguments)
        assert calls == [(None, (oracle[0].width(), oracle[0].height()))]
        job = scene._effect_jobs.running
        old_base = QImage(oracle[0].width(), oracle[0].height(), contract.image_format)
        assert job[4] == 12 * source.sizeInBytes() + 8 * old_base.sizeInBytes() + 4
        job[3].result(timeout=10)
        scene._effect_jobs.poll()
        actual = effect_pipeline.render_stages(scene, source, bounds, [effect], mapping, **arguments)
        assert get_ident() == owner_thread
        assert actual[1] == oracle[1]
        assert actual[0].format() == oracle[0].format()
        assert bits(actual[0]) == bits(oracle[0])
        assert scene._effect_jobs.submitted == 1


@pytest.mark.parametrize('contract', CONTRACTS)
def test_native_output_reservation_matches_qimage_stride(scene, contract):
    with pixel_scope(contract):
        image = QImage(7, 11, contract.image_format)
        assert distort_pipeline._native_frame_bytes(7, 11) == image.sizeInBytes()


@pytest.mark.parametrize('mode', ['blend', 'mask', 'live', 'sync', 'navigator', 'provisional', 'unsupported', 'scope'])
def test_other_routes_preserve_original_padded_base(scene, monkeypatch, mode):
    effect = spatial('twirl')
    effect.intensity = 100
    scene._interactive_render = scene._projection_exact = scene._projection_defer_effects = True
    scene._effect_region_requests = False
    provisional = False
    scope = ('object', 'base-policy', 'canvas')
    if mode == 'blend':
        effect.intensity = 99.75
    elif mode == 'mask':
        mask = ToneMask()
        scene.chapter.masks[mask.mask_id] = mask
        effect.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 100, 100)
    elif mode == 'live':
        scene._projection_exact = False
    elif mode == 'sync':
        scene._projection_defer_effects = False
    elif mode == 'navigator':
        scene._effect_preview_channel = 'navigator'
    elif mode == 'provisional':
        provisional = True
        scene._projection_exact = False
    elif mode == 'unsupported':
        effect.modifier_type = 'distort_spherical'
        effect.parameters = {}
    else:
        scope = None
    register(scene, [effect])
    allocations, bases = [], []
    allocate = effect_pipeline.empty_image
    def empty(bounds):
        allocations.append(QRectF(bounds))
        return allocate(bounds)
    def stage(canvas, incoming, base, *args, **kwargs):
        assert base is not None and kwargs['base_size'] is None
        bases.append(QImage(base))
        return QImage(base), provisional
    monkeypatch.setattr(effect_pipeline, 'empty_image', empty)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', stage)
    source = working_image(source_pixels())
    effect_pipeline.render_stages(scene, source, QRectF(-37, -61, 181, 173), [effect], QTransform(),
        request_scope=scope, source_key=('other-base-route', mode), provisional=provisional,
        tile_evaluation=False)
    assert len(allocations) == len(bases) == 1


def test_omitted_base_requires_native_dimensions_and_uniform_parameter(scene, monkeypatch):
    effect = spatial('twirl')
    effect.intensity = 100
    register(scene, [effect])
    source = working_image(source_pixels())
    bounds = QRectF(0, 0, 181, 173)
    scene._interactive_render = scene._projection_exact = scene._projection_defer_effects = True
    arguments = (scene, source, None, bounds, bounds, effect, QTransform(), {},
                 ('base-free-preview',), ('object', 'base-validation', 'canvas'), False, False)
    with pytest.raises(ValueError, match='dimensions'):
        distort_pipeline.render_distort_stage(*arguments, base_size=(181, 172))
    monkeypatch.setattr(distort_pipeline, '_parameter_field',
        lambda *_: np.full((173, 181), 100, np.float32))
    with pytest.raises(ValueError, match='parameter field'):
        distort_pipeline.render_distort_stage(*arguments, base_size=(181, 173))
    assert not scene._effect_jobs.submitted


def test_invalid_mapping_preserves_existing_native_rejection(scene, monkeypatch):
    effect = spatial('twirl')
    effect.intensity = 100
    register(scene, [effect])
    scene._interactive_render = scene._projection_exact = scene._projection_defer_effects = True
    scene._effect_region_requests = False
    source = working_image(source_pixels())
    def forbidden(*args):
        pytest.fail('singular placement reached padded-base preparation')
    monkeypatch.setattr(effect_pipeline, 'empty_image', forbidden)
    with pytest.raises(ValueError, match='singular placement'):
        effect_pipeline.render_stages(scene, source, QRectF(-37, -61, 181, 173), [effect],
            QTransform().scale(0, 0), request_scope=('object', 'singular-base', 'canvas'),
            source_key=('singular-base-source',), tile_evaluation=False)
    assert not scene._effect_jobs.submitted


@pytest.mark.parametrize('cancel', [False, True])
def test_base_free_worker_detaches_source_and_rig_and_rejects_cancellation(scene, monkeypatch, cancel):
    from comic_editor.ui import distort_rendering
    effect = spatial('twirl')
    effect.intensity = 100
    register(scene, [effect])
    source, bounds = working_image(source_pixels()), QRectF(-37, -61, 181, 173)
    arguments = dict(request_scope=('object', 'detached-base-free', 'canvas'),
        source_key=('detached-base-free-source',), tile_evaluation=False)
    oracle = effect_pipeline.render_stages(scene, source, bounds, [effect], QTransform(), **arguments)
    clear(scene)
    scene._interactive_render = scene._projection_exact = scene._projection_defer_effects = True
    scene._effect_region_requests = False
    entered, release, threads = Event(), Event(), []
    owner_thread, kernel = get_ident(), distort_rendering.render_distort
    def blocked(*args, **kwargs):
        threads.append(get_ident())
        assert get_ident() != owner_thread
        entered.set()
        assert release.wait(5)
        return kernel(*args, **kwargs)
    monkeypatch.setattr(distort_rendering, 'render_distort', blocked)
    try:
        with pytest.raises(ProjectionPending) as pending:
            effect_pipeline.render_stages(scene, source, bounds, [effect], QTransform(), **arguments)
        assert entered.wait(2)
        job = scene._effect_jobs.running
        source.fill(QColor('black'))
        effect.parameters['angle'] = 151.25
        if cancel:
            scene._history_generation = getattr(scene, '_history_generation', 0) + 1
            scene._effect_jobs.cancel(clear_retained=False)
        release.set()
        job[3].result(timeout=10)
        scene._effect_jobs.poll()
        result = scene._effect_jobs.result(pending.value.scope, pending.value.key)
        if cancel:
            assert result is None and not scene._modifier_render_cache
        else:
            assert result is not None and bits(result) == bits(oracle[0])
        assert len(threads) == 1 and threads[0] != owner_thread
    finally:
        release.set()


def test_base_free_target_keeps_existing_allocation_limit(scene):
    with pytest.raises(ValueError, match='Effect bounds are too large'):
        effect_pipeline._empty_image_size(QRectF(0, 0, 8193, 8193))

