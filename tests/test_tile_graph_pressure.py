"""Bounded exact demand changes keep existing spatial kernel sampling."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import (BrightnessContrastModifier, CageTransformModifier,
    DistortModifier, ParameterMaskBinding, RadialBlurModifier, ToneMask)
from comic_editor.core.pixel_contract import LEGACY_PIXELS, PixelContract
from comic_editor.render.pixels import pixel_scope, working_image
from comic_editor.render.service import RenderPending
from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from comic_editor.ui import tile_effects
from test_effect_regions import scene, register
from test_tile_graph_assembly import bits


def clear(canvas):
    canvas._effect_jobs.cancel()
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0


def spatial(kind):
    if kind == 'cage':
        effect = CageTransformModifier(frame=(-25.3, -21.7, 355.8, 287.3), intensity=61.25)
        effect.validate()
        effect.points[5] = (effect.points[5][0]+19.375, effect.points[5][1]-11.25)
    elif kind == 'deform':
        effect = DistortModifier(modifier_type='distort_deform', frame=(-25.3, -21.7, 355.8, 287.3),
            source_points=[(0, 0), (1, 0), (1, 1), (0, 1)],
            points=[(.03, .01), (.96, .04), (.99, .97), (.02, .94)], intensity=61.25)
    elif kind == 'mesh':
        effect = DistortModifier(modifier_type='distort_mesh_warp', frame=(-25.3, -21.7, 355.8, 287.3), intensity=61.25)
    elif kind in ('lens', 'pinch'):
        effect = DistortModifier(modifier_type='distort_lens_distortion' if kind == 'lens' else 'distort_pinch_punch',
            frame=(-25.3, -21.7, 355.8, 287.3), center=(150.375, 120.625), radius=123.75,
            parameters={'amount': 23.75}, intensity=61.25)
    else:
        effect = DistortModifier(modifier_type='distort_twirl', frame=(-25.3, -21.7, 355.8, 287.3),
            center=(150.375, 120.625), radius=123.75, parameters={'angle': 45.25}, intensity=61.25)
    effect.validate()
    return effect


@pytest.mark.parametrize('contract', [LEGACY_PIXELS,
    PixelContract(version=2, precision='float16', working_space='linear_srgb'),
    PixelContract(version=2, precision='float32', working_space='linear_srgb')])
@pytest.mark.parametrize('kind', ['twirl', 'deform', 'mesh', 'lens', 'pinch'])
@pytest.mark.parametrize('projective', [False, True])
@pytest.mark.parametrize('pressure', ['retained-pair', 'native-preparation'])
def test_pressure_full_stage_matches_regional_native_masked_pixels(scene, monkeypatch, contract, kind, projective, pressure):
    from comic_editor.render.tile_graph import TileGraph
    frame, required = QRectF(-37, -61, 333, 277), QRectF(-11, -19, 281, 241)
    mapping = (QTransform(1.1, .13, .00015, -.07, .9, -.00021, 13.375, 22.625, 1.) if projective
               else QTransform().translate(13.375, 22.625).rotate(19.25).scale(1.25, .73))
    yy, xx = np.mgrid[:277, :333]
    alpha = ((xx + yy) % 193) / 192.
    pixels = np.stack((1.7 * xx / 333, -.125 + yy / 277, (xx % 7) / 7,
                       np.ones_like(xx)), axis=-1) * alpha[..., None]
    effect = spatial(kind)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    effect.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 12.5, 83.75)
    modifiers = [BrightnessContrastModifier(brightness=12.25), effect]
    if pressure == 'native-preparation':
        # The next spatial stage demands every pixel from the tested stage.
        # Reduce only its decision threshold to exercise the real whole native
        # kernel without allocating a 463MB preparation in the test process.
        modifiers.append(spatial('twirl'))
    register(scene, modifiers)
    def fields(mods, width, height, transform, _bounds):
        inverse = transform.inverted()[0]
        y, x = np.mgrid[:height, :width]
        denominator = inverse.m13()*(x+.5) + inverse.m23()*(y+.5) + inverse.m33()
        world_x = (inverse.m11()*(x+.5) + inverse.m21()*(y+.5) + inverse.dx()) / denominator
        value = np.clip((world_x+100)/500, 0, 1).astype(np.float32)
        return {(modifier.modifier_id, 'intensity'): value for modifier in mods if modifier.parameter_masks}
    monkeypatch.setattr(scene, '_modifier_mask_fields', fields)
    scene.chapter.pixel_contract = contract
    scene._effect_region_requests = True
    scene._interactive_render = True
    scene._projection_exact = True
    scene._exact_reference_render = True
    flags = []
    original = TileGraph.__init__
    def record(self, nodes, *args, **kwargs):
        flags.append(tuple(node.shared_frame for node in nodes))
        return original(self, nodes, *args, **kwargs)
    monkeypatch.setattr(TileGraph, '__init__', record)
    arguments = dict(required=required, request_scope=('object', 'pressure', 'canvas'), source_identity=('pressure-source', contract.signature))
    with pixel_scope(contract):
        source = working_image(pixels, contract)
        oracle = tile_effects.tile_output(scene, source, frame, modifiers, mapping, **arguments)
        assert flags[-1][-1] is False
        clear(scene)
        if pressure == 'retained-pair':
            scene._effect_jobs.retained_budget = 512 * 1024  # Pair ~740KiB, full input/output cannot coexist.
        else:
            from comic_editor.ui import distort_rendering
            monkeypatch.setattr(distort_rendering, 'PREPARED_DISTORT_CACHE_BUDGET', 4096)
        scene._projection_defer_effects = True
        for _ in range(25):
            try:
                actual = tile_effects.tile_output(scene, source, frame, modifiers, mapping, **arguments)
                break
            except RenderPending:
                assert scene._effect_region_requests and scene._exact_reference_render
                for job in scene._effect_jobs.running_jobs:
                    job[3].result(timeout=10)
                scene._effect_jobs.poll()
        else:
            pytest.fail('Exclusive full-frame pressure fallback did not converge')
        assert flags[-1][2] is True
        if pressure == 'native-preparation':
            assert flags[-1][-1] is False  # Final partial viewport keeps its original demand.
        assert scene._effect_region_requests and scene._exact_reference_render
        assert actual[1] == oracle[1]
        assert actual[0].format() == oracle[0].format()
        assert bits(actual[0]) == bits(oracle[0])
        assert len(scene._effect_jobs.retained) + len(scene._effect_jobs._regions) <= scene._effect_jobs.retained_limit


@pytest.mark.parametrize('next_kind', [None, 'point', 'lens'])
def test_real_29mp_native_preparation_pressure_requires_complete_downstream_demand(scene, monkeypatch, next_kind):
    from comic_editor.render.tile_graph import TileGraph
    from comic_editor.ui.distort_rendering import PREPARED_DISTORT_CACHE_BUDGET
    frame = QRectF(0, 0, 5383, 5383)
    modifiers = [BrightnessContrastModifier(brightness=11), spatial('twirl')]
    if next_kind:
        modifiers.append(spatial('lens') if next_kind == 'lens' else BrightnessContrastModifier(brightness=12))
    register(scene, modifiers)
    scene._effect_region_requests = scene._interactive_render = scene._projection_exact = True
    scene._projection_defer_effects = True
    assert 2 * 4 * frame.width() * frame.height() + 262144 < scene._effect_jobs.retained_budget
    assert 16 * frame.width() * frame.height() > PREPARED_DISTORT_CACHE_BUDGET
    monkeypatch.setattr(tile_effects, 'effect_bounds', lambda bounds, *_args: bounds)
    captured = []
    def inspect(self, nodes, *_a, **_kw):
        captured.extend(nodes)
        raise RuntimeError('inspected nodes before allocation')
    monkeypatch.setattr(TileGraph, '__init__', inspect)
    with pytest.raises(RuntimeError, match='inspected nodes'):
        tile_effects.tile_output(scene, None, frame, modifiers, QTransform(), capture=lambda _: None,
            required=QRectF(0, 0, 90, 80), source_identity=('native-preparation',), request_scope=('object', 'native-preparation', 'canvas'))
    assert captured[2].shared_frame is (next_kind == 'lens')
    assert captured[-1].shared_frame is False


@pytest.mark.parametrize('spatial_consumer', [False, True])
def test_actual_lens_expansion_promotes_complete_outgoing_preparation_not_small_incoming(scene, monkeypatch, spatial_consumer):
    from comic_editor.render.tile_graph import TileGraph
    incoming = QRectF(-15, 17555, 1105, 1466)
    expanded = QRectF(-2160, 15611, 5383, 5383)
    modifiers = [spatial('lens')]
    if spatial_consumer:
        modifiers.append(spatial('pinch'))
    modifiers.append(BrightnessContrastModifier(brightness=11))
    register(scene, modifiers)
    scene._effect_region_requests = scene._interactive_render = scene._projection_exact = True
    scene._projection_defer_effects = True
    assert 16 * incoming.width() * incoming.height() < 256 * 1024 * 1024
    assert 16 * expanded.width() * expanded.height() > 256 * 1024 * 1024
    assert 4 * (incoming.width()*incoming.height() + expanded.width()*expanded.height()) + 262144 < scene._effect_jobs.retained_budget
    monkeypatch.setattr(tile_effects, 'effect_bounds', lambda bounds, effects, _mapping:
        expanded if effects[0] is modifiers[0] else bounds)
    captured = []
    def inspect(self, nodes, *_a, **_kw):
        captured.extend(nodes)
        raise RuntimeError('inspected nodes before allocation')
    monkeypatch.setattr(TileGraph, '__init__', inspect)
    with pytest.raises(RuntimeError, match='inspected nodes'):
        tile_effects.tile_output(scene, None, incoming, modifiers, QTransform(), capture=lambda _: None,
            required=QRectF(0, 18157, 1080, 1686), source_identity=('actual-lens-expansion',),
            request_scope=('object', 'actual-lens-expansion', 'canvas'))
    assert captured[1].frame == expanded and captured[1].shared_frame is spatial_consumer
    if spatial_consumer:
        assert captured[2].shared_frame is False


@pytest.mark.parametrize('contract', [LEGACY_PIXELS,
    PixelContract(version=2, precision='float16', working_space='linear_srgb'),
    PixelContract(version=2, precision='float32', working_space='linear_srgb')])
@pytest.mark.parametrize('projective', [False, True])
def test_expanded_lens_outgoing_preparation_keeps_exact_native_masked_crop(scene, monkeypatch, contract, projective):
    from comic_editor.render.tile_graph import TileGraph
    from comic_editor.ui import distort_rendering
    # Scale the actual saved rig to keep the same small-input/expanded-output
    # geometry while testing native pixels within a practical memory bound.
    frame, required = QRectF(-4, 4388, 278, 367), QRectF(0, 4420, 230, 310)
    lens = DistortModifier(modifier_type='distort_lens_distortion', intensity=61.25,
        frame=(-8.894771405779228/4, 17539.255451723126/4, 1084.6531563923008/4, 2395.8097315440355/4),
        center=(531.3839817111582/4, 18302.36463314537/4), radius=364.51304668905397/4,
        parameters={'amount': -32.16, 'interpolation': 'bilinear', 'edges': 'transparent'})
    pinch = DistortModifier(modifier_type='distort_pinch_punch', frame=lens.frame,
        center=lens.center, radius=lens.radius, parameters={'amount': 23.75})
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    lens.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 12.5, 83.75)
    modifiers = [lens, pinch, BrightnessContrastModifier(brightness=11.25)]
    register(scene, modifiers)
    mapping = (QTransform(1.1, .13, .000015, -.07, .9, -.000021, 13.375, 22.625, 1.) if projective
               else QTransform().translate(13.375, 22.625).rotate(1.25).scale(1.025, .97))
    def fields(mods, width, height, transform, _bounds):
        inverse = transform.inverted()[0]
        y, x = np.mgrid[:height, :width]
        denominator = inverse.m13()*(x+.5) + inverse.m23()*(y+.5) + inverse.m33()
        world_x = (inverse.m11()*(x+.5) + inverse.m21()*(y+.5) + inverse.dx()) / denominator
        value = np.clip((world_x+400)/1000, 0, 1).astype(np.float32)
        return {(modifier.modifier_id, 'intensity'): value for modifier in mods if modifier.parameter_masks}
    monkeypatch.setattr(scene, '_modifier_mask_fields', fields)
    scene.chapter.pixel_contract = contract
    scene._effect_region_requests = scene._interactive_render = scene._projection_exact = True
    scene._exact_reference_render = True
    yy, xx = np.mgrid[:367, :278]
    alpha = ((xx+yy)%193)/192.
    pixels = np.stack((1.7*xx/278, -.125+yy/367, (xx%7)/7, np.ones_like(xx)), axis=-1)*alpha[..., None]
    captured = []
    original = TileGraph.__init__
    def inspect(self, nodes, *args, **kwargs):
        captured.append(nodes)
        return original(self, nodes, *args, **kwargs)
    monkeypatch.setattr(TileGraph, '__init__', inspect)
    arguments = dict(required=required, request_scope=('object', 'expanded-lens', 'canvas'),
        source_identity=('expanded-lens-native-source', contract.signature))
    with pixel_scope(contract):
        source = working_image(pixels, contract)
        oracle = tile_effects.tile_output(scene, source, frame, modifiers, mapping, **arguments)
        assert not captured[-1][1].shared_frame
        clear(scene)
        preparation_bound = 8 * 1024 * 1024
        assert 16*frame.width()*frame.height() < preparation_bound
        assert 16*captured[-1][1].frame.width()*captured[-1][1].frame.height() > preparation_bound
        monkeypatch.setattr(distort_rendering, 'PREPARED_DISTORT_CACHE_BUDGET', preparation_bound)
        scene._projection_defer_effects = True
        for _ in range(25):
            try:
                actual = tile_effects.tile_output(scene, source, frame, modifiers, mapping, **arguments)
                break
            except RenderPending:
                for job in scene._effect_jobs.running_jobs:
                    job[3].result(timeout=10)
                scene._effect_jobs.poll()
        else:
            pytest.fail('Expanded full Lens did not converge')
        assert captured[-1][1].shared_frame and not captured[-1][2].shared_frame
        assert actual[1] == oracle[1] and actual[0].format() == oracle[0].format()
        assert bits(actual[0]) == bits(oracle[0])


@pytest.mark.parametrize('kind', ['radial', 'cage'])
def test_unproven_spatial_pressure_preserves_native_demand_partition(scene, monkeypatch, kind):
    from comic_editor.render.tile_graph import TileGraph
    modifiers = [BrightnessContrastModifier(brightness=11),
                 RadialBlurModifier(angle=.25, center=(150.375, 120.625)) if kind == 'radial' else spatial('cage')]
    register(scene, modifiers)
    frame, required = QRectF(-37, -61, 333, 277), QRectF(0, 0, 90, 80)
    scene._effect_region_requests = scene._interactive_render = scene._projection_exact = True
    scene._projection_defer_effects = True
    scene._effect_jobs.retained_budget = 1
    captured = []
    def inspect(self, nodes, *_a, **_kw):
        captured.extend(nodes)
        raise RuntimeError('inspected nodes before allocation')
    monkeypatch.setattr(TileGraph, '__init__', inspect)
    with pytest.raises(RuntimeError, match='inspected nodes'):
        tile_effects.tile_output(scene, None, frame, modifiers, QTransform(), capture=lambda _: None,
            required=required, source_identity=('radial-partition',), request_scope=('object', 'radial', 'canvas'))
    assert captured[-1].shared_frame is False


def test_large_expanded_spatial_output_keeps_small_regional_demand(scene, monkeypatch):
    from comic_editor.render.tile_graph import TileGraph
    frame, expanded = QRectF(0, 0, 300, 280), QRectF(-5000, -5000, 10000, 10000)
    modifiers = [BrightnessContrastModifier(brightness=11), spatial('twirl')]
    register(scene, modifiers)
    scene._effect_region_requests = scene._interactive_render = scene._projection_exact = True
    scene._projection_defer_effects = True
    scene._effect_jobs.retained_budget = 1
    original = tile_effects.effect_bounds
    monkeypatch.setattr(tile_effects, 'effect_bounds', lambda bounds, effects, mapping:
        expanded if isinstance(effects[0], DistortModifier) else original(bounds, effects, mapping))
    captured = []
    def inspect(self, nodes, *_a, **_kw):
        captured.extend(nodes)
        raise RuntimeError('inspected nodes before allocation')
    monkeypatch.setattr(TileGraph, '__init__', inspect)
    with pytest.raises(RuntimeError, match='inspected nodes'):
        tile_effects.tile_output(scene, None, frame, modifiers, QTransform(), capture=lambda _: None,
            required=QRectF(0, 0, 90, 80), source_identity=('huge-spatial',), request_scope=('object', 'huge', 'canvas'))
    assert captured[-1].frame == expanded and captured[-1].shared_frame is False


@pytest.mark.parametrize('failure', [ProjectionPending, ProjectionFailed])
def test_promoted_stage_restores_region_policy_on_worker_control_flow(scene, monkeypatch, failure):
    from comic_editor.render.tile_graph import TileGraph
    from comic_editor.ui import effect_pipeline
    frame = QRectF(0, 0, 333, 277)
    modifiers = [BrightnessContrastModifier(brightness=11), spatial('mesh')]
    register(scene, modifiers)
    scene._effect_region_requests = scene._interactive_render = scene._projection_exact = True
    scene._projection_defer_effects = scene._exact_reference_render = True
    scene._effect_jobs.retained_budget = 512 * 1024
    def stage(*_args, **_kwargs):
        assert not scene._effect_region_requests and scene._exact_reference_render
        raise failure('existing-full-stage', 'current-native-key')
    monkeypatch.setattr(effect_pipeline, 'render_stages', stage)
    class InspectGraph:
        def __init__(self, nodes, *_args, **_kwargs):
            self.node = nodes[-1]
        def output(self, _required):
            assert self.node.shared_frame
            return self.node.evaluate(self.node.frame, working_image(np.zeros((277, 333, 4))), frame)
    monkeypatch.setattr(tile_effects, 'TileGraph', InspectGraph)
    with pytest.raises(failure):
        tile_effects.tile_output(scene, None, frame, modifiers, QTransform(), capture=lambda _: None,
            required=QRectF(0, 0, 90, 80), source_identity=('scope-restore',), request_scope=('object', 'restore', 'canvas'))
    assert scene._effect_region_requests and scene._exact_reference_render
