"""Bounded native batch output preserves canonical exact tile semantics."""
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import BrightnessContrastModifier, ParameterMaskBinding, ToneMask
from comic_editor.core.pixel_contract import LEGACY_PIXELS, PixelContract
from comic_editor.render.pixels import pixel_scope, working_image
from comic_editor.render.service import RenderPending
from comic_editor.ui import tile_effects, distort_rendering
from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from test_effect_regions import scene, register
from test_tile_graph_assembly import bits
from test_tile_graph_pressure import clear, spatial


CONTRACTS = [LEGACY_PIXELS,
    PixelContract(version=2, precision='float16', working_space='linear_srgb'),
    PixelContract(version=2, precision='float32', working_space='linear_srgb')]


def exact(canvas):
    canvas._effect_region_requests = canvas._interactive_render = canvas._projection_exact = True
    canvas._exact_reference_render = True


def settle(canvas, render, tries=35):
    for _ in range(tries):
        try:
            return render()
        except RenderPending:
            assert canvas._effect_region_requests and canvas._exact_reference_render
            for job in canvas._effect_jobs.running_jobs:
                job[3].result(timeout=10)
            canvas._effect_jobs.poll()
    pytest.fail('native output batches did not converge')


@pytest.mark.parametrize('contract', CONTRACTS)
@pytest.mark.parametrize('kind', ['twirl', 'deform', 'mesh', 'lens', 'pinch'])
@pytest.mark.parametrize('projective', [False, True])
@pytest.mark.parametrize('masked', [False, True], ids=['uniform100', 'varying-mask'])
def test_batch_native_bytes_match_unbatched_tiles(scene, monkeypatch, contract, kind, projective, masked):
    frame, required = QRectF(-37, -61, 333, 277), QRectF(-11, -19, 281, 241)
    mapping = (QTransform(1.1, .13, .00015, -.07, .9, -.00021, 13.375, 22.625, 1.) if projective
               else QTransform().translate(13.375, 22.625).rotate(19.25).scale(1.25, .73))
    effect = spatial(kind)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    if masked:
        effect.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 12.5, 83.75)
    else:
        effect.intensity = 100.
    modifiers = [effect, BrightnessContrastModifier(brightness=12.25)]
    register(scene, modifiers)
    def fields(mods, width, height, transform, _bounds):
        inverse = transform.inverted()[0]
        yy, xx = np.mgrid[:height, :width]
        denominator = inverse.m13()*(xx+.5) + inverse.m23()*(yy+.5) + inverse.m33()
        world_x = (inverse.m11()*(xx+.5) + inverse.m21()*(yy+.5) + inverse.dx()) / denominator
        value = np.clip((world_x+100)/500, 0, 1).astype(np.float32)
        return {(mod.modifier_id, 'intensity'): value for mod in mods if mod.parameter_masks}
    monkeypatch.setattr(scene, '_modifier_mask_fields', fields)
    scene.chapter.pixel_contract = contract
    exact(scene)
    yy, xx = np.mgrid[:277, :333]
    alpha = ((xx+yy)%193)/192.
    pixels = np.stack((1.7*xx/333, -.125+yy/277, (xx%7)/7, np.ones_like(xx)), axis=-1)*alpha[..., None]
    arguments = dict(required=required, request_scope=('object', 'native-batch', 'canvas'),
                     source_identity=('native-batch-source', contract.signature))
    batches, original = [], tile_effects._batched_stage
    def observe(canvas, delegate, output, node_frame, *args):
        batches.append((QRectF(output), tile_effects._native_batch_frame(output, node_frame)))
        return original(canvas, delegate, output, node_frame, *args)
    monkeypatch.setattr(tile_effects, '_batched_stage', observe)
    with pixel_scope(contract):
        source = working_image(pixels, contract)
        expected = tile_effects.tile_output(scene, source, frame, modifiers, mapping, **arguments)
        assert not batches
        clear(scene)
        # Trigger only the preparation policy, not a different native sampler.
        monkeypatch.setattr(distort_rendering, 'PREPARED_DISTORT_CACHE_BUDGET', 4096)
        scene._projection_defer_effects = True
        actual = settle(scene, lambda: tile_effects.tile_output(scene, source, frame, modifiers, mapping, **arguments))
        assert batches
        assert all(batch.contains(output) and batch.width()*batch.height() <= 512*512
                   for output, batch in batches)
        assert actual[1] == expected[1] and actual[0].format() == expected[0].format()
        assert bits(actual[0]) == bits(expected[0])
        assert scene._effect_region_requests and scene._exact_reference_render
        assert scene._effect_jobs.retained_bytes <= scene._effect_jobs.retained_budget
        assert len(scene._effect_jobs.retained) + len(scene._effect_jobs._regions) <= scene._effect_jobs.retained_limit
        graph_keys = [entry[0] for scope, entry in scene._effect_jobs.retained.items()
                      if scope[0] == 'tile-graph' and entry[0][0] == 'effect-tile']
        assert graph_keys and all(key[-1][2] <= 256 and key[-1][3] <= 256 for key in graph_keys)


@pytest.mark.parametrize('frame,output,expected', [
    ((-2160, 15611, 5383, 5383), (-2048, 18432, 256, 256), (-2048, 18432, 512, 512)),
    ((-2160, 15611, 5383, 5383), (-2160, 15611, 112, 5), (-2160, 15611, 112, 261)),
    ((3, 7, 97, 93), (3, 7, 97, 93), (3, 7, 97, 93)),
])
def test_batch_uses_fixed_document_grid_and_clips_semantic_edges(frame, output, expected):
    actual = tile_effects._native_batch_frame(QRectF(*output), QRectF(*frame))
    assert actual == QRectF(*expected)


@pytest.mark.parametrize('edge', [False, True])
def test_completed_crop_never_consumes_full_batch_handle_even_at_clipped_edge(scene, edge):
    frame = QRectF(3, 7, 97, 93) if edge else QRectF(0, 0, 600, 600)
    output = QRectF(frame) if edge else QRectF(0, 0, 256, 256)
    batch = tile_effects._native_batch_frame(output, frame)
    full = QImage(int(batch.width()), int(batch.height()), QImage.Format_ARGB32_Premultiplied)
    full.fill(0xff804020)
    scope, key = ('batch-worker',), ('completed-batch', tuple(batch.getRect()))
    scene._effect_jobs.retained_put(('result', scope), key, full)
    def ready(required, *, batch_frame):
        assert required == batch and batch_frame == batch
        return QImage(full)
    scene._effect_region_requests = True
    cropped = tile_effects._batched_stage(scene, ready, output, frame)
    assert cropped.cacheKey() != full.cacheKey()
    tile_effects._put_graph_result(scene, ('object', 'batch', 'canvas'), (1, (0, 0)),
        ('effect-tile', ('canonical',), tuple(output.getRect())), cropped)
    assert scene._effect_jobs.result(scope, key) == full
    assert scene._effect_region_requests


@pytest.mark.parametrize('failure', [ProjectionPending, ProjectionFailed])
def test_batched_preflight_and_evaluation_restore_region_policy_on_yield_or_failure(scene, failure):
    scene._effect_region_requests = scene._exact_reference_render = True
    def unavailable(*args, **kwargs):
        assert not scene._effect_region_requests and scene._exact_reference_render
        raise failure('batch', 'key')
    with pytest.raises(failure):
        tile_effects._batched_stage(scene, unavailable, QRectF(0, 0, 256, 256), QRectF(0, 0, 600, 600))
    assert scene._effect_region_requests and scene._exact_reference_render


def test_sibling_tiles_reuse_one_completed_batch_after_ordinary_cache_eviction(scene, monkeypatch):
    effect = spatial('twirl')
    modifiers = [effect, BrightnessContrastModifier(brightness=12.25)]
    register(scene, modifiers)
    exact(scene)
    scene._projection_defer_effects = True
    monkeypatch.setattr(distort_rendering, 'PREPARED_DISTORT_CACHE_BUDGET', 4096)
    source = QImage(530, 530, QImage.Format_ARGB32_Premultiplied)
    source.fill(0xff804020)
    frame = QRectF(0, 0, 530, 530)
    calls, original = [], distort_rendering.render_distort
    def observed(*args, **kwargs):
        calls.append(QRectF(args[4]))
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_rendering, 'render_distort', observed)
    arguments = dict(request_scope=('object', 'batch-siblings', 'canvas'), source_identity=('batch-sibling-source',))
    first = settle(scene, lambda: tile_effects.tile_output(scene, source, frame, modifiers, QTransform(),
                                                        required=QRectF(0, 0, 256, 256), **arguments))
    assert calls == [QRectF(0, 0, 512, 512)]
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    for scope in tuple(scene._effect_jobs.retained):
        if scope[0] == 'tile-graph':
            scene._effect_jobs.retained_remove(scope)
    def forbidden(_):
        pytest.fail('a completed sibling batch recaptured its full upstream source')
    second = settle(scene, lambda: tile_effects.tile_output(scene, None, frame, modifiers, QTransform(), capture=forbidden,
                                                          required=QRectF(256, 0, 256, 256), **arguments))
    assert calls == [QRectF(0, 0, 512, 512)]
    assert first[0].size() == second[0].size()
    before = (scene._effect_jobs.submitted, len(scene._effect_jobs._regions), len(scene._effect_jobs.retained))
    assert tile_effects.tile_output(scene, None, frame, modifiers, QTransform(),
        required=QRectF(0, 256, 256, 256), **arguments) is None
    assert (scene._effect_jobs.submitted, len(scene._effect_jobs._regions), len(scene._effect_jobs.retained)) == before


@pytest.mark.parametrize('change', ['source', 'parameter', 'mask-paint', 'mask-endpoint'])
def test_batch_cannot_reuse_changed_native_source_or_modifier_mask(scene, monkeypatch, change):
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor
    effect = spatial('twirl')
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    effect.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 12.5, 83.75)
    modifiers = [effect, BrightnessContrastModifier(brightness=12.25)]
    register(scene, modifiers)
    exact(scene)
    scene._projection_defer_effects = True
    monkeypatch.setattr(distort_rendering, 'PREPARED_DISTORT_CACHE_BUDGET', 4096)
    source = QImage(333, 277, QImage.Format_ARGB32_Premultiplied)
    source.fill(0xff804020)
    frame = QRectF(-37, -61, 333, 277)
    identity = ['native-source', 1]
    def render():
        return tile_effects.tile_output(scene, source, frame, modifiers, QTransform(),
            required=QRectF(0, 0, 100, 100), request_scope=('object', 'batch-mutation', 'canvas'),
            source_identity=tuple(identity))
    calls, original = [], distort_rendering.render_distort
    def observed(*args, **kwargs):
        calls.append(QRectF(args[4]))
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_rendering, 'render_distort', observed)
    settle(scene, render)
    before = len(calls)
    if change == 'source':
        source.fill(0xff208040)
        identity[1] += 1
    elif change == 'parameter':
        effect.parameters['angle'] += 23.5
    elif change == 'mask-paint':
        scene.tiles.paint_dab(mask.mask_id, QPointF(150, 120), 140, QColor('white'))
    else:
        effect.parameter_masks['intensity'].black_value = 25.75
    actual = settle(scene, render)
    assert len(calls) > before
    clear(scene)
    scene._projection_defer_effects = False
    # The synchronous 256px path is the unchanged native oracle after mutation.
    expected = render()
    assert actual[1] == expected[1] and bits(actual[0]) == bits(expected[0])


@pytest.mark.parametrize('change', ['history', 'cancel', 'pixel-contract'])
def test_pending_batch_cannot_publish_through_old_compiled_context(scene, monkeypatch, change):
    from comic_editor.render.tile_graph import TileGraph
    effect = spatial('twirl')
    modifiers = [effect, BrightnessContrastModifier(brightness=12.25)]
    register(scene, modifiers)
    exact(scene)
    scene._projection_defer_effects = True
    monkeypatch.setattr(distort_rendering, 'PREPARED_DISTORT_CACHE_BUDGET', 4096)
    source = QImage(333, 277, QImage.Format_ARGB32_Premultiplied)
    source.fill(0xff804020)
    frame, required = QRectF(-37, -61, 333, 277), QRectF(0, 0, 100, 100)
    entered, release, graphs = Event(), Event(), []
    original_render, original_graph = distort_rendering.render_distort, TileGraph.__init__
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_render(*args, **kwargs)
    def remember(self, *args, **kwargs):
        original_graph(self, *args, **kwargs)
        graphs.append(self)
    monkeypatch.setattr(distort_rendering, 'render_distort', blocked)
    monkeypatch.setattr(TileGraph, '__init__', remember)
    try:
        with pytest.raises(RenderPending):
            tile_effects.tile_output(scene, source, frame, modifiers, QTransform(), required=required,
                request_scope=('object', 'batch-history', 'canvas'), source_identity=('native-current-source',))
        assert entered.wait(2)
        old = graphs[-1]
        before = set(scene._modifier_render_cache)
        if change == 'history':
            scene._history_generation = getattr(scene, '_history_generation', 0) + 1
        elif change == 'cancel':
            scene._effect_jobs.cancel_exact()
        else:
            scene.chapter.pixel_contract = CONTRACTS[-1]
        with pytest.raises(RenderPending):
            old.output(required)
        assert set(scene._modifier_render_cache) == before
        assert scene._effect_region_requests and scene._exact_reference_render
    finally:
        release.set()
        for job in scene._effect_jobs.running_jobs:
            job[3].result(timeout=10)
        scene._effect_jobs.poll()


@pytest.mark.parametrize('policy', ['fit', 'sync', 'live', 'navigator', 'provisional', 'over64mp', 'oversized'])
def test_native_batch_policy_is_explicit_bounded_and_read_only_at_preflight(scene, monkeypatch, policy):
    from comic_editor.render.tile_graph import TileGraph
    from comic_editor.ui import effect_pipeline
    effect = spatial('twirl')
    modifiers = [effect, BrightnessContrastModifier(brightness=12.25)]
    register(scene, modifiers)
    exact(scene)
    scene._projection_defer_effects = policy != 'sync'
    frame = QRectF(0, 0, 9000, 9000) if policy == 'over64mp' else QRectF(0, 0, 5383, 5383)
    if policy == 'fit':
        frame = QRectF(0, 0, 530, 403)
    if policy == 'live':
        scene._projection_exact = False
    elif policy == 'navigator':
        scene._effect_preview_channel = 'navigator'
    elif policy == 'provisional':
        scene._last_modifier_provisional = True
    monkeypatch.setattr(tile_effects, 'effect_bounds', lambda bounds, *_: bounds)
    nodes, requests = [], []
    def inspect(self, compiled, *_args, **_kwargs):
        nodes.extend(compiled)
        raise RuntimeError('compiled without allocating artwork')
    def cached(*_args, **kwargs):
        requests.append(kwargs)
        return None
    monkeypatch.setattr(TileGraph, '__init__', inspect)
    monkeypatch.setattr(effect_pipeline, 'cached_stage_output', cached)
    args = dict(required=QRectF(256, 256, 256, 256), request_scope=('object', 'batch-policy', 'canvas'),
                source_identity=('native-batch-policy',))
    if policy in {'live', 'navigator'}:
        assert tile_effects.tile_output(scene, None, frame, modifiers, QTransform(), **args) is None
        assert not nodes
        return
    with pytest.raises(RuntimeError, match='compiled'):
        tile_effects.tile_output(scene, None, frame, modifiers, QTransform(), **args)
    assert not nodes[1].shared_frame
    assert nodes[1].cached_output(args['required']) is None
    expected = QRectF(0, 0, 512, 512) if policy == 'oversized' else args['required']
    assert requests[0]['required'] == expected
    assert any(item[0] == 'native-output-batch' for item in requests[0]['request_scope'][3:]) is (policy == 'oversized')
    assert not scene._effect_jobs.submitted and not scene._effect_jobs.retained and not scene._effect_jobs._regions
    assert scene._effect_region_requests
