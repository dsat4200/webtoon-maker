"""Small live integrations identify current pixels, while every blend stays fresh."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import ParameterMaskBinding, RadialBlurModifier, ToneMask
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope, working_image
from comic_editor.ui import radial_blur
from comic_editor.ui.cache_dependencies import cache_put, exact_cache_allowed
from comic_editor.ui.modifier_rendering import _premultiplied_qimage, _qimage_premultiplied
from comic_editor.ui.radial_pipeline import render_radial_stage
from test_effect_regions import scene


CONTRACTS = [LEGACY_PIXELS, replace(FLOAT_PIXELS, precision='float16'), FLOAT_PIXELS]


def native(image):
    return image.size(), image.format(), bytes(image.constBits())


def source(contract, width=43, height=31):
    yy, xx = np.mgrid[:height, :width]
    alpha = ((xx * 3 + yy * 5) % 13 + 1).astype(np.float32) / 15
    color = np.stack((xx / max(1, width), yy / max(1, height),
                      ((xx + yy) % 7) / 8, np.ones_like(xx)), axis=-1).astype(np.float32)
    return working_image(color * alpha[..., None], contract)


def setup(scene, contract):
    scene.chapter.pixel_contract = contract
    scene._interactive_render = True
    scene._projection_exact = False
    scene._bounded_effect_preview = True
    scene._effect_preview_channel = 'canvas'
    return RadialBlurModifier(center=(18.625, 15.375), angle=14, intensity=87)


def render(scene, image, effect, fields=None, *, base=None, mapping=None, origin=(0., 0.),
           source_key=('current-source',), **policies):
    flags = dict(asynchronous=False, deferred=False, provisional=True, navigator=False,
                 exact=False, bounded_preview=True)
    flags.update(policies)
    return render_radial_stage(scene, image, image if base is None else base, effect,
        {} if fields is None else fields, QTransform() if mapping is None else mapping, origin,
        source_key=source_key, scope=('object', 'native-radial-proof', 'canvas'), **flags)


def field(effect, name, fields, shape):
    binding = effect.parameter_masks.get(name)
    values = fields.get((effect.modifier_id, name))
    if binding is None or values is None or values.shape != shape:
        return float(getattr(effect, name))
    return np.clip(np.asarray(values, np.float32), 0, 1) * (binding.white_value - binding.black_value) + binding.black_value


def reference(image, base, effect, fields, mapping, origin):
    shape = (base.height(), base.width())
    integrated = radial_blur.radial_blur(_qimage_premultiplied(image), effect.center,
        field(effect, 'angle', fields, shape), mapping, output_shape=shape, output_origin=origin)
    amount = np.asarray(field(effect, 'intensity', fields, shape)) / 100
    if amount.ndim == 2:
        amount = amount[..., None]
    return _premultiplied_qimage(_qimage_premultiplied(base) * (1 - amount) + integrated * amount)


def count_kernel(monkeypatch):
    calls = []
    original = radial_blur.radial_blur
    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(radial_blur, 'radial_blur', observed)
    return calls


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
def test_new_capture_same_native_pixels_reuses_existing_lru_without_changing_result(scene, monkeypatch, contract):
    effect = setup(scene, contract)
    image = source(contract)
    original = native(image)
    budget = scene._modifier_render_cache_budget
    with pixel_scope(contract):
        expected = reference(image, image, effect, {}, QTransform(), (0., 0.))
        calls = count_kernel(monkeypatch)
        first, provisional = render(scene, image, effect)
        detached = image.copy()
        assert detached.cacheKey() != image.cacheKey()
        second, still_provisional = render(scene, detached, effect)
        assert native(first) == native(second) == native(expected)
        assert provisional and still_provisional and len(calls) == 1
        keys = list(scene._modifier_render_cache)
        assert len(keys) == 1 and keys[0][0] == 'radial-integration-preview'
        assert scene._modifier_render_cache[keys[0]].format() == QImage.Format_RGBA32FPx4_Premultiplied
        first.fill(Qt.black)
        again, _ = render(scene, detached, effect)
        assert native(again) == native(expected) and len(calls) == 1
    assert native(image) == original and scene._modifier_render_cache_budget == budget
    assert not scene._effect_jobs.retained and scene._effect_jobs.submitted == 0


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
def test_borrowed_native_input_mutation_without_cachekey_or_model_change_recomputes(scene, monkeypatch, contract):
    effect = setup(scene, contract)
    owned = source(contract)
    data = np.frombuffer(owned.constBits(), np.uint8).copy()
    borrowed = QImage(data.data, owned.width(), owned.height(), owned.bytesPerLine(), owned.format())
    key = borrowed.cacheKey()
    with pixel_scope(contract):
        calls = count_kernel(monkeypatch)
        first, _ = render(scene, borrowed, effect)
        render(scene, borrowed, effect)
        assert len(calls) == 1
        if contract.floating:
            dtype = np.float16 if contract.precision == 'float16' else np.float32
            pixels = data.view(dtype).reshape(owned.height(), -1)[:, :owned.width()*4].reshape(owned.height(), owned.width(), 4)
            pixels[8:23, 10:32] = (.02, .35, .03, .7)
        else:
            pixels = data.reshape(owned.height(), -1)[:, :owned.width()*4].reshape(owned.height(), owned.width(), 4)
            pixels[8:23, 10:32] = (17, 121, 42, 201)
        assert borrowed.cacheKey() == key
        second, _ = render(scene, borrowed, effect)
        assert len(calls) == 2 and native(second) != native(first)
        expected = reference(borrowed, borrowed, effect, {}, QTransform(), (0., 0.))
        assert native(second) == native(expected)


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
@pytest.mark.parametrize('change', ['angle_field', 'mask_endpoints', 'source', 'mapping',
                                   'subrounded_mapping', 'center', 'origin', 'shape', 'angle'])
def test_sampling_dependency_mutation_matches_current_uncached_native_reference(scene, monkeypatch, contract, change):
    effect = setup(scene, contract)
    image = source(contract)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    effect.parameter_masks['angle'] = ParameterMaskBinding(mask.mask_id, 2, 16)
    fields = {(effect.modifier_id, 'angle'): np.broadcast_to(
        np.linspace(.1, .9, image.width(), dtype=np.float32), (image.height(), image.width())).copy()}
    base, mapping, origin, source_key = image, QTransform(), (0., 0.), ('current-source',)
    with pixel_scope(contract):
        calls = count_kernel(monkeypatch)
        render(scene, image, effect, fields)
        render(scene, image, effect, fields)
        assert len(calls) == 1
        old_mask_signature = scene._tone_mask_signature(mask.mask_id)
        if change == 'angle_field':
            fields[(effect.modifier_id, 'angle')][:, :23] *= .35
            # A derived contributor can improve without its serialized signature changing.
            assert scene._tone_mask_signature(mask.mask_id) == old_mask_signature
        elif change == 'mask_endpoints':
            effect.parameter_masks['angle'].white_value = 23
        elif change == 'source':
            source_key = ('different-source',)
        elif change == 'mapping':
            mapping = QTransform(1.04, .03, .0001, -.025, 1.08, -.00008, 1.25, -.75, 1.)
        elif change == 'subrounded_mapping':
            mapping.translate(.00000013, .00000017)
            assert scene._modifier_mapping_signature(mapping) != scene._modifier_mapping_signature(QTransform())
        elif change == 'center':
            effect.center = (9.875, 12.25)
        elif change == 'origin':
            origin = (-1.75, 2.625)
        elif change == 'shape':
            base = image.copy(0, 0, 39, 29)
        else:
            effect.angle = 31
        actual, provisional = render(scene, image, effect, fields, base=base, mapping=mapping,
                                     origin=origin, source_key=source_key)
        assert len(calls) == 2 and provisional
        expected = reference(image, base, effect, fields, mapping, origin)
        assert native(actual) == native(expected)


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
def test_same_integration_blends_new_base_intensity_and_field_without_reusing_final_pixels(scene, monkeypatch, contract):
    effect = setup(scene, contract)
    image = source(contract)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    effect.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0, 92)
    fields = {(effect.modifier_id, 'intensity'): np.full((image.height(), image.width()), .8, np.float32)}
    with pixel_scope(contract):
        calls = count_kernel(monkeypatch)
        first, _ = render(scene, image, effect, fields)
        effect.intensity = 24
        effect.parameter_masks['intensity'].black_value = 9
        effect.parameter_masks['intensity'].white_value = 77
        fields[(effect.modifier_id, 'intensity')][:, :22] = .17
        base = source(contract)
        base.fill(Qt.black)
        actual, _ = render(scene, image, effect, fields, base=base)
        assert len(calls) == 1 and native(actual) != native(first)
        expected = reference(image, base, effect, fields, QTransform(), (0., 0.))
        assert native(actual) == native(expected)


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
def test_pressure_evicts_from_same_lru_and_recomputed_pixels_remain_correct(scene, monkeypatch, contract):
    effect = setup(scene, contract)
    image = source(contract)
    scene._modifier_render_cache_budget = 24 * 1024
    with pixel_scope(contract):
        expected = reference(image, image, effect, {}, QTransform(), (0., 0.))
        calls = count_kernel(monkeypatch)
        render(scene, image, effect)
        first_key = next(iter(scene._modifier_render_cache))
        for x in range(9):
            render(scene, image, effect, origin=(float(x + 1), .25))
            assert scene._modifier_render_cache_bytes <= scene._modifier_render_cache_budget
        assert first_key not in scene._modifier_render_cache
        actual, _ = render(scene, image, effect)
        assert len(calls) == 11 and native(actual) == native(expected)
        assert not scene._effect_jobs.retained


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
def test_poisoned_preview_never_satisfies_exact_or_becomes_durable(scene, monkeypatch, tmp_path, contract):
    effect = setup(scene, contract)
    image = source(contract)
    disk = scene._persistent_render_cache = PersistentRenderCache(tmp_path)
    try:
        with pixel_scope(contract), disk.record():
            draft, _ = render(scene, image, effect)
            keys = list(scene._modifier_render_cache)
            assert keys and not scene._effect_jobs.retained
            scene._projection_exact = True
            for key in keys:
                assert not exact_cache_allowed(scene, key)
                cache_put(scene, 'effect', key, scene._modifier_render_cache[key])
        disk.drain()
        assert not disk.entries and not disk.writes
        scene._persistent_render_cache = None
        for key in keys:
            scene._modifier_render_cache[key].fill(Qt.black)
        with pixel_scope(contract):
            expected = reference(image, image, effect, {}, QTransform(), (0., 0.))
            calls = count_kernel(monkeypatch)
            exact, provisional = render(scene, image, effect, provisional=False, exact=True,
                                        bounded_preview=False)
            assert not provisional and len(calls) == 1
            assert native(exact) == native(expected) == native(draft)
    finally:
        scene._persistent_render_cache = None
        disk.close()


@pytest.mark.parametrize('changed', ['scope_contract', 'actual_contract'])
def test_same_native_storage_under_different_contract_context_recomputes(scene, monkeypatch, changed):
    image = source(FLOAT_PIXELS)
    effect = setup(scene, FLOAT_PIXELS)
    calls = count_kernel(monkeypatch)
    with pixel_scope(FLOAT_PIXELS):
        render(scene, image, effect)
        render(scene, image, effect)
        assert len(calls) == 1
    current = replace(FLOAT_PIXELS, precision='float16')
    if changed == 'actual_contract':
        scene.chapter.pixel_contract = current
        current = FLOAT_PIXELS
    with pixel_scope(current):
        actual, _ = render(scene, image, effect)
        assert len(calls) == 2
        assert native(actual) == native(reference(image, image, effect, {}, QTransform(), (0., 0.)))


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
def test_native_padding_is_not_pixel_content_and_cannot_replace_current_visible_rows(scene, monkeypatch, contract):
    effect = setup(scene, contract)
    owned = source(contract)
    row_bytes = owned.width() * owned.depth() // 8
    stride = row_bytes + 16
    data = np.zeros((owned.height(), stride), np.uint8)
    original = np.frombuffer(owned.constBits(), np.uint8).reshape(owned.height(), owned.bytesPerLine())
    data[:, :row_bytes] = original[:, :row_bytes]
    padded = QImage(data.data, owned.width(), owned.height(), stride, owned.format())
    with pixel_scope(contract):
        expected = reference(owned, owned, effect, {}, QTransform(), (0., 0.))
        calls = count_kernel(monkeypatch)
        first, _ = render(scene, padded, effect)
        data[:, row_bytes:] = 197
        second, _ = render(scene, padded, effect)
        assert len(calls) == 1 and native(first) == native(second) == native(expected)


def test_loaded_color_configuration_identity_remains_a_preview_dependency(scene, monkeypatch):
    import PyOpenColorIO as ocio
    from comic_editor.render.pixels import color_config
    effect = setup(scene, FLOAT_PIXELS)
    image = source(FLOAT_PIXELS)
    # Mutate the same explicitly selected config instance the render context
    # reads; functools' no-argument cache entry can hold a different instance.
    config = color_config(FLOAT_PIXELS.ocio_config)
    name = 'radial-preview-proof-unused-space'
    identity = config.getCacheID()
    with pixel_scope(FLOAT_PIXELS):
        calls = count_kernel(monkeypatch)
        render(scene, image, effect)
        render(scene, image, effect)
        assert len(calls) == 1
        try:
            config.addColorSpace(ocio.ColorSpace(name=name))
            assert config.getCacheID() != identity
            actual, _ = render(scene, image, effect)
            assert len(calls) == 2
            assert native(actual) == native(reference(image, image, effect, {}, QTransform(), (0., 0.)))
        finally:
            config.removeColorSpace(name)


def test_exact_output_boundary_is_allowed_but_larger_evaluated_angle_storage_is_not(scene, monkeypatch):
    from comic_editor.ui.radial_pipeline import _preview_integration_key
    effect = setup(scene, LEGACY_PIXELS)
    effect.angle = 0
    image = source(LEGACY_PIXELS)
    base = source(LEGACY_PIXELS, 256, 256)
    with pixel_scope(LEGACY_PIXELS):
        calls = count_kernel(monkeypatch)
        first, _ = render(scene, image, effect, base=base)
        second, _ = render(scene, image, effect, base=base)
        assert len(calls) == 1 and native(first) == native(second)
        assert next(iter(scene._modifier_render_cache.values())).sizeInBytes() == 1024 * 1024
        # Normal masked fields are float32. Reject any larger evaluated numeric
        # representation too, even when the independently bounded output fits.
        field64 = np.zeros((256, 256), np.float64)
        assert _preview_integration_key(scene, ('radial-integration',), image,
            field64, QTransform(), (256, 256)) is None


@pytest.mark.parametrize('limit', ['input', 'output', 'angle', 'format', 'unbounded', 'navigator'])
def test_unsupported_or_out_of_bound_request_never_admits_preview_integration(scene, monkeypatch, limit):
    effect = setup(scene, LEGACY_PIXELS)
    effect.angle = 0
    image = source(LEGACY_PIXELS)
    base, fields, policy = image, {}, {}
    if limit == 'input':
        image = source(LEGACY_PIXELS, 1025, 256)
    elif limit == 'output':
        base = source(LEGACY_PIXELS, 257, 256)
    elif limit == 'angle':
        base = source(LEGACY_PIXELS, 257, 256)
        mask = ToneMask()
        scene.chapter.masks[mask.mask_id] = mask
        effect.parameter_masks['angle'] = ParameterMaskBinding(mask.mask_id, 0, 0)
        fields[(effect.modifier_id, 'angle')] = np.zeros((256, 257), np.float32)
    elif limit == 'format':
        image = image.convertToFormat(QImage.Format_RGBA8888)
    elif limit == 'unbounded':
        policy['bounded_preview'] = False
    else:
        policy['navigator'] = True
    with pixel_scope(LEGACY_PIXELS):
        calls = count_kernel(monkeypatch)
        render(scene, image, effect, fields, base=base, **policy)
        render(scene, image, effect, fields, base=base, **policy)
        assert not scene._modifier_render_cache and not scene._effect_jobs.retained
        assert len(calls) == (0 if limit == 'unbounded' else 2)
