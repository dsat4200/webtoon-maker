"""Loaded color/source policy isolates ordinary native cache paths."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.core.models import HueSaturationLightnessModifier
from comic_editor.render import pixels
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope, premultiplied_pixels
from comic_editor.render.projection import ProjectionAddress, ProjectionRequest
from comic_editor.render.source_context import source_color_context, contextual_source_key
from comic_editor.ui.cache_dependencies import RenderDependencies, exact_cache_allowed
from comic_editor.ui.effect_pipeline import _draft_image_prefix, _stage_key, _stage_plan, render_stages
from comic_editor.ui.scene_render_backend import source_capture_key
from comic_editor.ui.source_images import image_for_render, _working_representation
from comic_editor.ui.translation_cache import output_key
from test_float_source_scene import canvas, source


def configuration(gamma):
    import PyOpenColorIO as ocio
    value = ocio.Config()
    value.setMajorVersion(2)
    value.setMinorVersion(0)
    value.addColorSpace(ocio.ColorSpace(name='linear_srgb', bitDepth=ocio.BIT_DEPTH_F32))
    value.addColorSpace(ocio.ColorSpace(name='srgb', bitDepth=ocio.BIT_DEPTH_F32,
        toReference=ocio.ExponentWithLinearTransform(gamma=[gamma, gamma, gamma, 1.],
                                                    offset=[.055, .055, .055, 0.])))
    value.setRole(ocio.ROLE_DEFAULT, 'srgb')
    value.setRole(ocio.ROLE_SCENE_LINEAR, 'linear_srgb')
    value.addDisplayView('sRGB', 'Standard', 'srgb', '')
    value.validate()
    return value


@pytest.fixture
def color_path(tmp_path):
    path = tmp_path / 'test.ocio'
    path.write_text(configuration(2.4).serialize(), 'utf-8')
    pixels.color_config.cache_clear()
    pixels.color_processor.cache_clear()
    pixels.gpu_color_processor.cache_clear()
    yield str(path)
    pixels.color_config.cache_clear()
    pixels.color_processor.cache_clear()
    pixels.gpu_color_processor.cache_clear()


def change_color(path, mode):
    from pathlib import Path
    if mode == 'reload':
        Path(path).write_text(configuration(3.1).serialize(), 'utf-8')
        # This explicitly models an existing caller reloading a configuration;
        # no automatic reload/file watcher is part of the production change.
        pixels.color_config.cache_clear()
        return pixels.color_config(path)
    old = pixels.color_config(path)
    old.clearColorSpaces()
    for space in configuration(3.1).getColorSpaces():
        old.addColorSpace(space)
    old.validate()
    return old


def native_expected(config, channels, precision):
    expected = np.array(channels, np.float32) / np.float32(65535)
    alpha = expected[3].copy()
    config.getProcessor('srgb', 'linear_srgb').getDefaultCPUProcessor().applyRGBA(expected)
    expected[:3] *= alpha
    expected[3] = alpha
    return expected.astype(np.float16).astype(np.float32) if precision == 'float16' else expected


def raw_key(canvas, obj):
    record = canvas._modifier_object_signature(obj)
    return ('mirror-source', 'object', obj.object_id, record[0], record[3], record[4],
            (2., 3., 1., 1.), False)


def test_default_legacy_keys_and_configuration_do_not_load_ocio(canvas, monkeypatch):
    obj, _, _ = source(canvas)
    def forbidden(*_):
        raise AssertionError('Legacy source identities must not resolve a color configuration')
    monkeypatch.setattr(pixels, 'color_config', forbidden)
    key = raw_key(canvas, obj)
    assert source_color_context(LEGACY_PIXELS, LEGACY_PIXELS) == ()
    assert source_capture_key(canvas, key) == key
    assert len(canvas._projection_configuration()) == 11
    with pixel_scope(FLOAT_PIXELS):
        assert source_color_context(LEGACY_PIXELS)[-1] is None
        assert source_capture_key(canvas, key) != key
    # Actual floating + temporary legacy still selects the display source edge.
    assert source_color_context(FLOAT_PIXELS, LEGACY_PIXELS)[-1] is None


@pytest.mark.parametrize('key', [117, 'identity-only'])
def test_identity_only_inputs_cannot_become_durable_semantic_sources(canvas, key):
    canvas.chapter.pixel_contract = FLOAT_PIXELS
    canvas._projection_exact = True
    with pixel_scope(FLOAT_PIXELS):
        actual = source_capture_key(canvas, key)
    assert not exact_cache_allowed(canvas, actual)
    assert not exact_cache_allowed(canvas, ('retained', ('nested',), actual))


@pytest.mark.parametrize('precision', ['float16', 'float32'])
def test_actual_policy_isolates_source_stage_and_live_prefixes(canvas, precision):
    obj, raw, _ = source(canvas)
    effect = HueSaturationLightnessModifier(saturation=-10)
    canvas.chapter.add_modifier(effect, [('object', obj.object_id)])
    scoped = replace(FLOAT_PIXELS, precision=precision)
    original = raw_key(canvas, obj)
    draft = ('live-effect-draft-source', .25, original)
    modifier_bounds = QRectF(0, 0, 1, 1)
    keys, images, results = [], [], []
    canvas._interactive_render = canvas._bounded_effect_preview = True
    canvas._projection_exact = False
    with pixel_scope(scoped):
        for actual in (LEGACY_PIXELS, scoped):
            canvas.chapter.pixel_contract = actual
            key = source_capture_key(canvas, draft)
            assert len(key) == 3 and key[:2] == draft[:2]
            assert key[2][:2] == ('mirror-source', 'object')
            assert _draft_image_prefix(canvas, key, [effect])
            assert contextual_source_key(key, source_color_context(actual)) == key
            plan = _stage_plan(canvas, modifier_bounds, [effect], QTransform(), key, False, None)
            stage = _stage_key(canvas, plan, key, 0, QTransform(), False)
            keys.append((key, stage))
            canvas._interactive_render = False
            image = image_for_render(canvas, obj.object_id)
            canvas._interactive_render = True
            images.append(QImage(image))
            canvas._modifier_source_cache_put(key, image)
            results.append(render_stages(canvas, image, modifier_bounds, [effect], QTransform(),
                source_key=key, provisional=True, request_scope=('object', obj.object_id, 'canvas'))[0])
        assert keys[0][0] != keys[1][0] and keys[0][1] != keys[1][1]
        assert bytes(images[0].constBits()) != bytes(images[1].constBits())
        live = [key for key in canvas._modifier_render_cache if key[:1] == ('live-effect-draft-stage',)]
        assert len(live) == 2 and live[0] != live[1]
        assert not exact_cache_allowed(canvas, live[0])
        assert not exact_cache_allowed(canvas, live[1])
        # A fresh native source/stage execution is the publication oracle.
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        cold = render_stages(canvas, images[1], modifier_bounds, [effect], QTransform(),
            source_key=keys[1][0], provisional=True,
            request_scope=('object', obj.object_id, 'canvas'))[0]
        assert bytes(cold.constBits()) == bytes(results[1].constBits())
    assert canvas.images.source(obj.object_id).data == raw


@pytest.mark.parametrize('precision', ['float16', 'float32'])
@pytest.mark.parametrize('alpha', [1, 4096])
def test_actual_scene_source_capture_cannot_reuse_temporary_legacy_float_pixels(canvas, precision, alpha):
    obj, raw, _ = source(canvas, channels=(32768, 16384, 8192, alpha))
    policy = replace(FLOAT_PIXELS, precision=precision)
    def render():
        result = QImage(8, 8, policy.image_format)
        result.fill(0)
        painter = QPainter(result)
        try:
            canvas._render_mirror_target(painter, obj, 1., QRectF(0, 0, 8, 8))
        finally:
            painter.end()
        return result
    with pixel_scope(policy):
        legacy = render()
        legacy_source = bytes(next(reversed(canvas._modifier_source_cache.values())).constBits())
        legacy_sources = set(canvas._modifier_source_cache)
        assert legacy_sources
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        canvas.chapter.pixel_contract = policy
        current = render()
        warm_source = bytes(next(reversed(canvas._modifier_source_cache.values())).constBits())
        assert set(canvas._modifier_source_cache) > legacy_sources
        canvas._modifier_source_cache.clear()
        canvas._modifier_source_cache_bytes = 0
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        canvas._effect_jobs.cancel()
        fresh = render()
        fresh_source = bytes(next(reversed(canvas._modifier_source_cache.values())).constBits())
    assert bytes(current.constBits()) == bytes(fresh.constBits())
    assert warm_source == fresh_source and warm_source != legacy_source
    if alpha > 1:
        assert premultiplied_pixels(current)[..., 3].max() > 0
    assert canvas.images.source(obj.object_id).data == raw


@pytest.mark.parametrize('mode', ['reload', 'mutation'])
def test_cpu_and_gpu_processors_use_current_loaded_config_identity(color_path, mode):
    old_config = pixels.color_config(color_path)
    old_identity = old_config.getCacheID()
    cpu = pixels.color_processor(color_path, 'srgb', 'linear_srgb')
    gpu = pixels.gpu_color_processor(color_path, 'srgb', 'linear_srgb')
    assert pixels.color_processor(color_path, 'srgb', 'linear_srgb') is cpu
    assert pixels.gpu_color_processor(color_path, 'srgb', 'linear_srgb') is gpu
    new_config = change_color(color_path, mode)
    assert new_config.getCacheID() != old_identity
    new_cpu = pixels.color_processor(color_path, 'srgb', 'linear_srgb')
    new_gpu = pixels.gpu_color_processor(color_path, 'srgb', 'linear_srgb')
    assert new_cpu is not cpu and new_gpu is not gpu
    assert new_gpu.getShaderText() != gpu.getShaderText()
    original = np.array([.5, .25, .125, .25], np.float32)
    expected, actual = original.copy(), original.copy()
    new_config.getProcessor('srgb', 'linear_srgb').getDefaultCPUProcessor().applyRGBA(expected)
    new_cpu.getDefaultCPUProcessor().applyRGBA(actual)
    np.testing.assert_array_equal(actual, expected)
    assert pixels.color_processor.cache_parameters()['maxsize'] == 32
    assert pixels.gpu_color_processor.cache_parameters()['maxsize'] == 32


@pytest.mark.parametrize('precision', ['float16', 'float32'])
@pytest.mark.parametrize('mode', ['reload', 'mutation'])
def test_native_working_source_and_semantic_cache_aliases_follow_loaded_config(
        canvas, color_path, precision, mode, tmp_path):
    channels = (32768, 16384, 8192, 4096)
    obj, raw, original = source(canvas, channels=channels)
    policy = replace(FLOAT_PIXELS, precision=precision, working_space='linear_srgb', ocio_config=color_path)
    canvas.chapter.pixel_contract = policy
    source_bytes = bytes(canvas.images.native_image(obj.object_id).constBits())
    profile = bytes(original.colorSpace().iccProfile())
    base = raw_key(canvas, obj)
    cache_root = tmp_path / 'derived'
    cache = PersistentRenderCache(cache_root, contract=policy.signature)
    canvas._persistent_render_cache = cache
    canvas._projection_exact = True
    try:
        with pixel_scope(policy):
            old_key = source_capture_key(canvas, base)
            old_image = image_for_render(canvas, obj.object_id)
            old_representation = _working_representation(policy)
            np.testing.assert_array_equal(premultiplied_pixels(old_image)[0, 0],
                native_expected(pixels.color_config(color_path), channels, precision))
            with cache.record():
                canvas._modifier_source_cache_put(old_key, old_image)
                canvas._modifier_cache_put(('stage-proof', old_key), old_image)
                canvas._effect_jobs.retained_put(('proof', obj.object_id), old_key, old_image)
            cache.drain()
            assert len(cache.entries) == 3
            old_config = canvas._projection_configuration()
            old_alias = output_key(canvas, obj, QRectF(2, 3, 1, 1), QTransform(), [])
            loaded = change_color(color_path, mode)
            new_key = source_capture_key(canvas, base)
            assert new_key != old_key and _working_representation(policy) != old_representation
            assert canvas._projection_configuration() != old_config
            assert output_key(canvas, obj, QRectF(2, 3, 1, 1), QTransform(), []) != old_alias
            new_image = image_for_render(canvas, obj.object_id)
            np.testing.assert_array_equal(premultiplied_pixels(new_image)[0, 0],
                native_expected(loaded, channels, precision))
            assert bytes(new_image.constBits()) != bytes(old_image.constBits())
            # Retired data remains lossless; current context cannot read it
            # through ordinary RAM/retained or reopened disk-backed aliases.
            canvas._modifier_source_cache.clear()
            canvas._modifier_source_cache_bytes = 0
            canvas._modifier_render_cache.clear()
            canvas._modifier_render_cache_bytes = 0
            canvas._effect_jobs.cancel()
            cache.close()
            cache = PersistentRenderCache(cache_root, contract=policy.signature)
            canvas._persistent_render_cache = cache
            assert canvas._modifier_source_cache_get(new_key) is None
            assert canvas._modifier_cache_get(('stage-proof', new_key)) is None
            assert canvas._effect_jobs.retained_get(('proof', obj.object_id), new_key) is None
            restored = canvas._modifier_source_cache_get(old_key)
            assert restored.format() == old_image.format()
            assert bytes(restored.constBits()) == bytes(old_image.constBits())
            assert canvas.images.decoded_bytes <= canvas.images.decoded_budget
            assert canvas.images.cached_working_image(obj.object_id, old_representation) is not None
            assert canvas.images.cached_working_image(obj.object_id, _working_representation(policy)) is not None
    finally:
        cache.close()
        canvas._persistent_render_cache = None
    assert canvas.images.source(obj.object_id).data == raw
    assert bytes(canvas.images.native_image(obj.object_id).constBits()) == source_bytes
    assert bytes(canvas.images.native_image(obj.object_id).colorSpace().iccProfile()) == profile


def test_projection_context_uses_document_scope_and_shared_disk_key(canvas, color_path, tmp_path):
    obj, _, _ = source(canvas)
    policy = replace(FLOAT_PIXELS, working_space='linear_srgb', ocio_config=color_path)
    canvas.chapter.pixel_contract = policy
    # Normal document configuration is requested outside a floating pixel_scope.
    first = canvas._projection_configuration()
    assert first[-1] == source_color_context(policy, policy)
    backing = PersistentRenderCache(tmp_path / 'projection', contract=policy.signature)
    try:
        dependencies = RenderDependencies(canvas, backing)
        request = ProjectionRequest(ProjectionAddress(0, 0, 0))
        disk_key = dependencies.projection_key(request, first)
        change_color(color_path, 'mutation')
        second = canvas._projection_configuration()
        assert second[:11] == first[:11] and second[-1] != first[-1]
        assert dependencies.projection_key(request, second) != disk_key
        canvas._disk_cache_capture = True
        assert canvas._projection_configuration()[-1] == second[-1]
    finally:
        backing.close()
        canvas._disk_cache_capture = False
