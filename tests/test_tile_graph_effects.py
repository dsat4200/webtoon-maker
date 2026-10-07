"""Exact spatial footprints, prefix reuse, and lazy scene capture."""
import numpy as np
import pytest
from threading import Event, get_ident
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from test_effect_regions import scene, image, crop, register
from comic_editor.core.models import (
    BlurModifier, BrightnessContrastModifier, CurvesModifier, DitheringModifier,
    HueSaturationLightnessModifier, KuwaharaModifier, OutlineModifier, SharpnessModifier,
    ParameterMaskBinding, ToneMask, PixelateModifier, ArrayModifier, MirrorModifier,
)
from comic_editor.ui.effect_pipeline import render_stages, cached_stage_output
from comic_editor.ui.modifier_rendering import apply_modifier_stack
from comic_editor.ui.tile_effects import tile_output


def enable(scene):
    scene._interactive_render = True
    scene._effect_region_requests = True
    scene._projection_exact = True


@pytest.mark.parametrize('reverse', [False, True])
def test_regional_smudged_image_reuse_keeps_pixel_placement(scene, monkeypatch, reverse):
    from PySide6.QtGui import QPainter
    from comic_editor.core.models import ImageObject
    from comic_editor.ui.effect_pipeline import empty_image
    from test_smudge_rendering import modifier, stroke
    quad = [(25, 45), (590, 90), (555, 650), (40, 600)]
    obj = scene.chapter.add_object(scene.chapter.root_page_ids[0], ImageObject(
        pixel_width=530, pixel_height=403, placement_mode='free', transform_quad=quad))
    scene.images.put_decoded(obj.object_id, 'sample.png', b'', image())
    modifiers = [OutlineModifier(thickness=7), modifier(stroke(start=(100, 150), end=(300, 280)))]
    for effect in reversed(modifiers) if reverse else modifiers:
        scene.chapter.add_modifier(effect, [('object', obj.object_id)])
    enable(scene)
    def render(region):
        scene._effect_viewport_world = region
        result = empty_image(region)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(-region.x(), -region.y())
        try:
            scene._render_modified_object(painter, obj, 1., region)
        finally:
            painter.end()
        return result
    regions = [QRectF(0, 0, 300, 256), QRectF(0, 256, 300, 256),
               QRectF(256, 0, 300, 512), QRectF(0, 0, 700, 750)]
    with monkeypatch.context() as patch:
        patch.setattr('comic_editor.ui.translation_cache.get', lambda *_: None)
        patch.setattr('comic_editor.ui.translation_cache.put', lambda *_: None)
        expected = [render(region) for region in regions]
    monkeypatch.setattr(scene, '_render_object_content',
        lambda *_: pytest.fail('Completed regional pixels were recaptured'))
    for region, reference in zip(regions, expected):
        assert render(region) == reference
        assert render(region) == reference


def test_independent_pending_tiles_fill_workers_without_publishing_an_incomplete_region(scene, monkeypatch):
    from threading import Lock
    from comic_editor.render.blur_regions import RegionalBlur
    from comic_editor.ui.async_projection import ProjectionPending
    modifiers = [BrightnessContrastModifier(brightness=12), BlurModifier(strength=7),
                 HueSaturationLightnessModifier(hue=30)]
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    full, frame = render_stages(scene, source, bounds, modifiers, QTransform())
    enable(scene)
    scene._projection_defer_effects = True
    args = dict(required=QRectF(0, 0, 480, 280), source_key=('parallel-tile-pixels',),
                request_scope=('object', 'parallel-tile', 'canvas'))
    original = RegionalBlur.apply
    gui, count, lock = get_ident(), [], Lock()
    both_started, release = Event(), Event()
    def blocked(self, *a, **kw):
        assert get_ident() != gui
        with lock:
            count.append(get_ident())
            if len(count) == scene._effect_jobs.worker_limit:
                both_started.set()
        assert release.wait(5)
        return original(self, *a, **kw)
    monkeypatch.setattr(RegionalBlur, 'apply', blocked)
    try:
        with pytest.raises(ProjectionPending):
            render_stages(scene, source, bounds, modifiers, QTransform(), **args)
        assert both_started.wait(1)
        assert len(set(count)) == scene._effect_jobs.worker_limit
        assert scene._effect_jobs.worker_limit >= 2
        assert scene._effect_jobs.bytes_in_flight <= scene._effect_jobs.budget
        assert not scene._effect_jobs.pending
        assert not any(key[0] == 'tile-output' for key in scene._modifier_render_cache)
    finally:
        release.set()
    for _ in range(12):
        for job in scene._effect_jobs.running_jobs:
            job[3].result(timeout=5)
        scene._effect_jobs.poll()
        try:
            result = render_stages(scene, source, bounds, modifiers, QTransform(), **args)
            break
        except ProjectionPending:
            pass
    else:
        pytest.fail('Parallel tile dependencies did not finish')
    assert result == crop(full, frame, args['required'])


@pytest.mark.parametrize('region', [(200, 130, 71, 65), (0, 0, 31, 25), (739, 445, 61, 55)])
def test_stroke_color_regions_keep_exact_stack_pixels_in_a_small_capture(scene, region):
    modifiers = [BrightnessContrastModifier(brightness=13.25, contrast=23.5, intensity=57.75),
                 HueSaturationLightnessModifier(hue=33.25, intensity=71.5),
                 OutlineModifier(thickness=6, blur_radius=3, blur_strength=70)]
    register(scene, modifiers)
    source, bounds = image(801, 503), QRectF(0, 0, 801, 503)
    full = apply_modifier_stack(source, modifiers, (0, 0), _point_lut=False)
    enable(scene)
    scene._projection_exact = False
    scene._stroke_projection_active = True
    required, captures = QRectF(*region), []
    def capture(rect):
        captures.append(QRectF(rect))
        return crop(source, bounds, rect)[0]
    result = tile_output(scene, None, bounds, modifiers, QTransform(), required=required,
        source_identity=('stroke-colors',), request_scope=('object', 'stroke', 'canvas'),
        capture=capture, float_pipeline=True)
    assert result == crop(full, bounds, required)
    assert captures and max(max(r.width(), r.height()) for r in captures) <= 64
    scene._projection_exact = True
    scene._stroke_projection_active = False
    released = tile_output(scene, None, bounds, modifiers, QTransform(), required=required,
        source_identity=('stroke-colors',), request_scope=('object', 'stroke', 'canvas'),
        capture=capture, float_pipeline=True)
    assert released == result


@pytest.mark.parametrize('float_pipeline', [False, True])
@pytest.mark.parametrize('prefix', [None, 'blur', 'hsl', 'masked'])
def test_fused_point_nodes_keep_stage_rounding_and_noncanonical_inputs(scene, monkeypatch, float_pipeline, prefix):
    from comic_editor.ui import point_lut
    modifiers = [BrightnessContrastModifier(brightness=13.25, contrast=23.5, intensity=57.75),
                 CurvesModifier(intensity=83.25, curves={
                     'rgb:master': [[0,0],[.35,.65],[1,1]],
                     'rgb:alpha': [[0,0],[.5,.7],[1,1]]}),
                 BrightnessContrastModifier(brightness=-21.25, contrast=-30.5, intensity=66.75)]
    if prefix == 'blur':
        modifiers.insert(0, BlurModifier(strength=3.75, intensity=41.5))
    elif prefix == 'hsl':
        modifiers.insert(0, HueSaturationLightnessModifier(hue=33.25, intensity=71.5))
    elif prefix == 'masked':
        mask = ToneMask()
        scene.chapter.masks[mask.mask_id] = mask
        modifiers[1].parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 20, 90)
        monkeypatch.setattr(scene, '_modifier_mask_fields', lambda mods, width, height, *_:
            {(m.modifier_id, 'intensity'): np.full((height, width), .33, np.float32)
             for m in mods if m.parameter_masks})
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    if float_pipeline:
        fields = scene._modifier_mask_fields(modifiers, source.width(), source.height(), QTransform(), bounds)
        full, frame = apply_modifier_stack(source, modifiers, (bounds.x(), bounds.y()), fields, _point_lut=False), bounds
    else:
        full, frame = render_stages(scene, source, bounds, modifiers, QTransform(), tile_evaluation=False)
    enable(scene)
    calls = []
    original = point_lut.point_chain
    def counted(*args, **kwargs):
        if 'quantize_stages' in kwargs:
            calls.append(kwargs['quantize_stages'])
        return original(*args, **kwargs)
    monkeypatch.setattr(point_lut, 'point_chain', counted)
    required = QRectF(0, 0, 300, 280)
    result = tile_output(scene, source, bounds, modifiers, QTransform(), required=required,
        source_identity=('fused-source', prefix, float_pipeline), request_scope=('object', 'fused', 'canvas'),
        float_pipeline=float_pipeline)
    assert result == crop(full, frame, required)
    if prefix != 'masked' and (not float_pipeline or prefix is None):
        assert calls and all(value == (not float_pipeline) for value in calls)
    else:
        assert not calls


@pytest.mark.parametrize("modifier", [
    BlurModifier(strength=11.25), BlurModifier(strength=6, algorithm="legacy"),
    BlurModifier(strength=4, mode="focal", focal_center=(50, 80), focal_radius=180),
    SharpnessModifier(radius=3.7), KuwaharaModifier(variant="original", size=3),
    KuwaharaModifier(variant="generalized", size=3), KuwaharaModifier(size=3),
    OutlineModifier(thickness=7), OutlineModifier(thickness=6, blur_radius=3, blur_strength=70),
])
def test_neighborhood_stage_tiles_match_full_frame(scene, modifier):
    modifiers = [BrightnessContrastModifier(brightness=12), modifier, HueSaturationLightnessModifier(hue=30)]
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    full, frame = render_stages(scene, source, bounds, modifiers, QTransform())
    enable(scene)
    for requested in (QRectF(-50, -35, 280, 199), QRectF(128, 12, 300, 230), QRectF(-500, -500, 1200, 1200)):
        result = render_stages(scene, source, bounds, modifiers, QTransform(), required=requested,
            source_key=("pixels",), request_scope=("object", "test", "canvas"))
        assert result == crop(full, frame, requested)


@pytest.mark.parametrize("modifier", [BlurModifier(strength=11.25), SharpnessModifier(radius=4),
                                      BlurModifier(strength=0), BlurModifier(strength=1e-7),
                                      KuwaharaModifier(variant="generalized", size=3),
                                      DitheringModifier(pixel_size=3.5), OutlineModifier(thickness=4),
                                      BlurModifier(strength=7, mode="focal", focal_center=(10, 30), focal_radius=180)])
def test_generic_stack_keeps_float_rounding_and_lattice_origin(scene, modifier):
    modifiers = [BrightnessContrastModifier(brightness=12), modifier, HueSaturationLightnessModifier(hue=30)]
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    mapping = QTransform().translate(13, 22).rotate(19).scale(1.5, .5)
    full = apply_modifier_stack(source, modifiers, mapping.map(bounds.topLeft()).toTuple())
    enable(scene)
    for requested in (QRectF(-25, -45, 280, 310), QRectF(128, 12, 300, 230)):
        result = tile_output(scene, source, bounds, modifiers, mapping, required=requested,
            source_identity=("pixels",), request_scope=("object", "test", "canvas"), float_pipeline=True)
        assert result is not None
        assert result == crop(full, bounds, requested)


def test_blur_level_and_neighborhood_prefix_reuse_without_source(scene, monkeypatch):
    modifiers = [BrightnessContrastModifier(brightness=12), BlurModifier(strength=7), SharpnessModifier(radius=2)]
    register(scene, modifiers)
    bounds = QRectF(-37, -61, 4097, 1921)
    captures = []
    def capture(rect):
        captures.append(rect)
        return image(int(rect.width()), int(rect.height()))
    enable(scene)
    arguments = dict(required=QRectF(256, 256, 250, 200), request_scope=("object", "bounded", "canvas"), source_identity=("pixels",))
    result = tile_output(scene, None, bounds, modifiers, QTransform(), capture=capture, **arguments)
    assert result is not None
    assert sum(r.width()*r.height() for r in captures) < bounds.width()*bounds.height()/2
    assert max(r.width()*r.height() for r in captures) <= 256**2
    before = len(captures)
    assert tile_output(scene, None, bounds, modifiers, QTransform(), **arguments) == result
    modifiers[-1].strength = 75
    assert tile_output(scene, None, bounds, modifiers, QTransform(), capture=capture, **arguments) != result
    assert len(captures) == before


def test_masked_blur_and_sharpen_use_global_mapping_and_mask_generation(scene, monkeypatch):
    blur = BlurModifier(strength=7)
    sharpen = SharpnessModifier(radius=3)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    for modifier, name, maximum in ((blur, "strength", 35), (sharpen, "radius", 13)):
        modifier.parameter_masks[name] = ParameterMaskBinding(mask.mask_id, 0, maximum)
    modifiers = [blur, sharpen]
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    def fields(modifiers, width, height, mapping, _bounds):
        inverse = mapping.inverted()[0]
        yy, xx = np.mgrid[:height, :width]
        world_x = inverse.m11()*(xx+.5)+inverse.m21()*(yy+.5)+inverse.dx()
        value = np.clip((world_x+100)/700 + mask.revision*.1, 0, 1).astype(np.float32)
        return {(m.modifier_id, name): value for m in modifiers for name in m.parameter_masks}
    monkeypatch.setattr(scene, "_modifier_mask_fields", fields)
    full, frame = render_stages(scene, source, bounds, modifiers, QTransform())
    enable(scene)
    args = dict(required=QRectF(0, 0, 280, 210), source_key=("pixels",), request_scope=("object", "masked", "canvas"))
    result = render_stages(scene, source, bounds, modifiers, QTransform(), **args)
    assert result == crop(full, frame, args["required"])
    mask.revision += 1
    assert cached_stage_output(scene, bounds, modifiers, QTransform(), **args) is None
    assert render_stages(scene, source, bounds, modifiers, QTransform(), **args) != result


def test_global_pattern_is_shared_while_downstream_neighbors_use_tiles(scene, monkeypatch):
    modifiers = [PixelateModifier(pixel_size=8), SharpnessModifier(radius=2)]
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    full, frame = render_stages(scene, source, bounds, modifiers, QTransform())
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    enable(scene)
    for required in (QRectF(0, 0, 230, 210), QRectF(200, 120, 230, 210)):
        result = render_stages(scene, source, bounds, modifiers, QTransform(), required=required,
            source_key=("pixels",), request_scope=("object", "pattern", "canvas"))
        assert result == crop(full, frame, required)


@pytest.mark.parametrize("modifier", [ArrayModifier(count=3, axis_end=(650, 90)),
                                       ArrayModifier(count=2, axis_end=(320, 90), angle_offset=13),
                                       MirrorModifier(axis_start=(600, -100), axis_end=(600, 600))])
def test_spatial_copies_fetch_inverse_regions_and_keep_native_pixels(scene, modifier):
    modifiers = [BrightnessContrastModifier(brightness=12), modifier, HueSaturationLightnessModifier(hue=30)]
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    mapping = QTransform().translate(13, 22).rotate(19).scale(1.5, .5)
    full, frame = render_stages(scene, source, bounds, modifiers, mapping)
    enable(scene)
    for required in (QRectF(0, 0, 230, 210), QRectF(420, 120, 230, 210), QRectF(800, -350, 250, 180)):
        result = render_stages(scene, source, bounds, modifiers, mapping, required=required,
            source_key=("pixels",), request_scope=("object", "copies", "canvas"))
        if frame.intersected(required).isEmpty():
            assert result[1].isEmpty()
        else:
            assert result == crop(full, frame, required)


def test_deferred_regional_blur_uses_detached_worker_and_retries_without_recapture(scene, monkeypatch):
    from comic_editor.render.blur_regions import RegionalBlur
    from comic_editor.ui.async_projection import ProjectionPending
    modifiers = [BrightnessContrastModifier(brightness=12), BlurModifier(strength=7),
                 HueSaturationLightnessModifier(hue=30)]
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    full, frame = render_stages(scene, source, bounds, modifiers, QTransform())
    enable(scene)
    scene._projection_defer_effects = True
    args = dict(required=QRectF(0, 0, 256, 256), source_key=("worker-pixels",), request_scope=("object", "async", "canvas"))
    gui = get_ident()
    entered, release = Event(), Event()
    original = RegionalBlur.apply
    calls = []
    def blocked(self, *a, **k):
        calls.append(get_ident())
        assert get_ident() != gui
        entered.set()
        assert release.wait(5)
        return original(self, *a, **k)
    monkeypatch.setattr(RegionalBlur, "apply", blocked)
    try:
        with pytest.raises(ProjectionPending):
            render_stages(scene, source, bounds, modifiers, QTransform(), **args)
        assert entered.wait(2)
        job = scene._effect_jobs.running
        before = scene._effect_jobs.submitted
        monkeypatch.setattr(scene, "_modifier_mask_fields", lambda *_a, **_k: pytest.fail("Pending job recaptured masks"))
        with pytest.raises(ProjectionPending):
            render_stages(scene, source, bounds, modifiers, QTransform(), **args)
        assert scene._effect_jobs.running == job and scene._effect_jobs.submitted == before
        assert len(calls) == 1
    finally:
        release.set()
    job[3].result(timeout=10)
    scene._effect_jobs.poll()
    # The downstream color node legitimately samples its own fields.
    monkeypatch.undo()
    assert render_stages(scene, source, bounds, modifiers, QTransform(), **args) == crop(full, frame, args["required"])


def test_translated_array_keeps_distant_source_requests_disjoint(scene):
    modifiers = [ArrayModifier(count=2, axis_end=(4000, 0))]
    register(scene, modifiers)
    bounds = QRectF(0, 0, 2048, 1024)
    captures = []
    def capture(rect):
        captures.append(rect)
        return image(int(rect.width()), int(rect.height()))
    enable(scene)
    result = tile_output(scene, None, bounds, modifiers, QTransform(), capture=capture,
        required=QRectF(4300, 256, 250, 250), source_identity=("copies",), request_scope=("object", "copies", "canvas"))
    assert result is not None
    assert max(r.width()*r.height() for r in captures) <= 256**2
    assert sum(r.width()*r.height() for r in captures) < 2048*1024/2


def test_flat_patch_of_varying_sharpen_mask_keeps_whole_field_mode(scene, monkeypatch):
    modifier = SharpnessModifier(radius=3.7)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["radius"] = ParameterMaskBinding(mask.mask_id, 0, 10)
    modifiers = [BrightnessContrastModifier(brightness=12), modifier]
    register(scene, modifiers)
    source, bounds = image(801, 503), QRectF(0, 0, 801, 503)
    def fields(modifiers, width, height, mapping, _bounds):
        xx = np.arange(width) + mapping.inverted()[0].dx()
        value = np.where(xx < 600, .37, .9).astype(np.float32)
        return {(modifier.modifier_id, "radius"): np.broadcast_to(value, (height, width)).copy()}
    monkeypatch.setattr(scene, "_modifier_mask_fields", fields)
    full, frame = render_stages(scene, source, bounds, modifiers, QTransform())
    enable(scene)
    required = QRectF(0, 0, 256, 256)
    result = render_stages(scene, source, bounds, modifiers, QTransform(), required=required,
        source_key=("flat-mask",), request_scope=("object", "masked", "canvas"))
    assert result == crop(full, frame, required)


def test_generic_zero_strength_mask_retains_reference_identity_mode(scene):
    modifier = BlurModifier(strength=7)
    modifier.parameter_masks["strength"] = ParameterMaskBinding("synthetic-mask", 0, 20)
    enable(scene)
    # The whole-field all-zero test has a different float rounding contract
    # from a zero tile within a nonzero field. Keep this explicit fallback.
    assert tile_output(scene, image(), QRectF(0, 0, 530, 403), [modifier], QTransform(),
        required=QRectF(0, 0, 256, 256), request_scope=("object", "zero", "canvas"),
        source_identity=("pixels",), float_pipeline=True) is None


def test_spatial_stage_preflight_survives_source_eviction_and_pending_retries(scene, monkeypatch):
    from comic_editor.core.models import DistortModifier
    from comic_editor.ui import distort_rendering
    from comic_editor.ui.async_projection import ProjectionPending
    warp = DistortModifier(modifier_type='distort_twirl', frame=(0, 0, 300, 280),
                           center=(150, 140), radius=90, parameters={'angle': 85})
    warp.validate()
    modifiers = [warp, OutlineModifier(thickness=3)]
    register(scene, modifiers)
    source, bounds = image(300, 280), QRectF(0, 0, 300, 280)
    full, frame = render_stages(scene, source, bounds, modifiers, QTransform(), tile_evaluation=False)
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    enable(scene)
    scene._projection_defer_effects = True
    args = dict(required=QRectF(10, 10, 100, 100), source_identity=('preflight-source',),
                request_scope=('object', 'preflight', 'canvas'))
    started, release = Event(), Event()
    original = distort_rendering.render_distort
    def blocked(*a, **kw):
        started.set()
        assert release.wait(5)
        return original(*a, **kw)
    monkeypatch.setattr(distort_rendering, 'render_distort', blocked)
    captures = []
    def capture(rect):
        captures.append(QRectF(rect))
        return crop(source, bounds, rect)[0]
    try:
        with pytest.raises(ProjectionPending):
            tile_output(scene, None, bounds, modifiers, QTransform(), capture=capture, **args)
        assert started.wait(2)
        submitted = scene._effect_jobs.submitted
        assert submitted > 0
        scene._modifier_render_cache.clear()
        scene._modifier_render_cache_bytes = 0
        for scope in tuple(scene._effect_jobs.retained):
            if scope[0] in {'tile-graph', 'pipeline'}:
                scene._effect_jobs.retained_remove(scope)
        before = len(captures)
        with pytest.raises(ProjectionPending):
            tile_output(scene, None, bounds, modifiers, QTransform(), capture=capture, **args)
        assert len(captures) == before and scene._effect_jobs.submitted == submitted
    finally:
        release.set()
    # Completed worker scopes can be adopted before their graph tile/pipeline
    # checkpoint exists. Neither polling nor resumption needs the raw source.
    for job in scene._effect_jobs.running_jobs:
        job[3].result(timeout=5)
    scene._effect_jobs.poll()
    def forbidden(_rect):
        pytest.fail('completed warp output recaptured evicted source')
    for _ in range(12):
        try:
            result = tile_output(scene, None, bounds, modifiers, QTransform(), capture=forbidden, **args)
            break
        except ProjectionPending:
            for job in scene._effect_jobs.running_jobs:
                job[3].result(timeout=5)
            scene._effect_jobs.poll()
    else:
        pytest.fail('spatial stages did not converge')
    assert result == crop(full, frame, args['required'])


def test_single_stage_preflight_keeps_mask_dependency_validation(scene):
    from comic_editor.core.models import DistortModifier
    warp = DistortModifier(modifier_type='distort_twirl', frame=(0, 0, 300, 280),
                           center=(150, 140), radius=90, parameters={'angle': 85})
    warp.validate()
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    warp.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 20, 90)
    register(scene, [warp])
    enable(scene)
    source, bounds = image(300, 280), QRectF(0, 0, 300, 280)
    args = dict(required=QRectF(10, 10, 100, 100), source_key=('masked-preflight-source',),
                request_scope=('object', 'masked-preflight', 'canvas'), tile_evaluation=False)
    expected = render_stages(scene, source, bounds, [warp], QTransform(), **args)
    for scope in tuple(scene._effect_jobs.retained):
        if scope[0] == 'pipeline':
            scene._effect_jobs.retained_remove(scope)
    assert cached_stage_output(scene, bounds, [warp], QTransform(), **args) == expected
    mask.revision += 1
    assert cached_stage_output(scene, bounds, [warp], QTransform(), **args) is None


def test_shared_stage_key_preserves_unrounded_mapped_sampling_origin(scene):
    from comic_editor.ui.effect_pipeline import _stage_plan, _stage_key
    modifier = BrightnessContrastModifier(brightness=12)
    register(scene, [modifier])
    mapping = QTransform().translate(.00321, .00987).rotate(19).scale(100000, 300000)
    source_key, keys, plans = ('fractional-source',), [], []
    for x in (1e-6, 2e-6):
        bounds = QRectF(x, 3e-6, 300, 280)
        plan = _stage_plan(scene, bounds, [modifier], mapping, source_key, False, None)
        expected_upstream = ('stage-input', source_key, (), plan.placement, ())
        expected = ('stage', expected_upstream, scene._rect_signature(bounds),
            scene._rect_signature(plan.targets[0]), plan.signatures[0][0], plan.signatures[0][1],
            tuple(mapping.map(bounds.topLeft()).toTuple()), False, plan.signatures[0][2], plan.transform_signature)
        key = _stage_key(scene, plan, source_key, 0, mapping, False)
        assert key == expected
        keys.append(key)
        plans.append(plan)
    assert plans[0].geometry == plans[1].geometry
    assert keys[0] != keys[1]
