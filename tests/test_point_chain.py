import json

import numpy as np
import pytest
from PySide6.QtGui import QImage

from comic_editor.core.curves import CURVE_BLEND_MODES
from comic_editor.core.models import BrightnessContrastModifier, CurvesModifier, ParameterMaskBinding
from comic_editor.render.pixels import FLOAT_PIXELS, pixel_scope
from comic_editor.ui.modifier_rendering import apply_modifier_stack, modifier_render_settings
from comic_editor.ui.point_lut import compile_point_table, point_chain


def source_image(width=273, height=257):
    data = np.random.default_rng(4).integers(0, 256, (height, width, 4), np.uint8)
    data[..., :3] = np.minimum(data[..., :3], data[..., 3:4])
    return QImage(data.data, width, height, data.strides[0], QImage.Format_RGBA8888_Premultiplied).copy()


def effects(blend='normal'):
    return [BrightnessContrastModifier(brightness=13.25, contrast=23.5, intensity=57.75),
        CurvesModifier(blend_mode=blend, intensity=83.25, curves={
            'rgb:master': [[0,0],[.35,.65],[1,1]],
            'rgb:red': [[0,0],[.4,.2],[1,1]],
            'rgb:blue': [[0,.1],[.7,.6],[1,1]],
            'rgb:alpha': [[0,0],[.5,.7],[1,1]]}),
        BrightnessContrastModifier(brightness=-21.25, contrast=-30.5, intensity=66.75)]


def signature(modifiers):
    result = []
    for modifier in modifiers:
        settings = modifier_render_settings(modifier)
        settings['id'] = 'compiled-point-operation'
        result.append(json.dumps(settings, sort_keys=True, separators=(',', ':')))
    return tuple(result)


@pytest.mark.parametrize('blend', CURVE_BLEND_MODES)
def test_point_tables_preserve_every_float_bit_and_final_pixel(blend, qapp):
    source, modifiers = source_image(), effects(blend)
    expected = apply_modifier_stack(source, modifiers, (0,0), _point_lut=False, return_pixels=True)
    actual = apply_modifier_stack(source, modifiers, (0,0), return_pixels=True)
    np.testing.assert_array_equal(actual, expected)
    reference = apply_modifier_stack(source, modifiers, (0,0), _point_lut=False)
    assert apply_modifier_stack(source, modifiers, (0,0)) == reference


def test_point_table_prefix_reuse_and_unsupported_inputs_keep_reference_path(qapp):
    source, modifiers = source_image(), effects()
    key = signature(modifiers)
    prefix = compile_point_table(key[:-1])
    before = compile_point_table.cache_info()
    modifiers[-1].brightness += 4
    result = compile_point_table(signature(modifiers))
    assert compile_point_table.cache_info().hits > before.hits
    assert compile_point_table(key[:-1]) is prefix
    assert not result.flags.writeable
    from comic_editor.ui.modifier_rendering import _qimage_premultiplied
    pixels = _qimage_premultiplied(source)
    modifiers[1].color_mode = 'lab'
    assert point_chain(source, pixels, modifiers) is None
    modifiers[1].color_mode = 'rgb'
    modifiers[0].parameter_masks['brightness'] = ParameterMaskBinding('mask', -10, 20)
    assert point_chain(source, pixels, modifiers) is None
    modifiers[0].parameter_masks.clear()
    with pixel_scope(FLOAT_PIXELS):
        assert point_chain(source, pixels, modifiers) is None
    assert apply_modifier_stack(source, modifiers, (0,0), cancelled=lambda: True) is None


def test_float_source_without_canonical_byte_contract_uses_reference(qapp):
    from comic_editor.render.pixels import working_image, premultiplied_pixels
    data = np.random.default_rng(8).random((257, 273, 4), dtype=np.float32)
    data[..., :3] *= data[..., 3:4]
    source = working_image(data, FLOAT_PIXELS)
    assert source.depth() > 32
    assert point_chain(source, premultiplied_pixels(source), effects()) is None
    # Legacy conversion remains the established reference. Float contracts
    # must retain their non-byte input instead of indexing a 256-value table.
    assert apply_modifier_stack(source, effects(), (0, 0)) == apply_modifier_stack(
        source, effects(), (0, 0), _point_lut=False)
    with pixel_scope(FLOAT_PIXELS):
        actual = apply_modifier_stack(source, effects(), (0, 0), return_pixels=True)
        reference = apply_modifier_stack(source, effects(), (0, 0), return_pixels=True, _point_lut=False)
    np.testing.assert_array_equal(actual, reference)


@pytest.fixture
def gpu(qapp):
    from comic_editor.render.gpu.point_chain import GpuPointChain
    renderer = GpuPointChain(budget=8 * 1024 * 1024)
    if not renderer.available:
        renderer.close()
        pytest.skip(renderer.reason)
    yield renderer
    renderer.close()


def test_native_float_texture_upload_and_readback_preserve_hdr_and_rows(gpu):
    pixels = np.array([[[1.2,.0001,-.05,.4],[.234567,.012345,.456789,.65]],
                       [[.2,.3,.4,.5],[0,0,0,0]]], np.float32)
    actual = gpu.apply(pixels, [(1,0,0)], source_key='identity-hdr')
    np.testing.assert_array_equal(actual, pixels)


def test_native_fused_lookup_matches_reference_float_and_reuses_gpu_resources(gpu):
    modifiers = effects('soft_light')
    key = signature(modifiers)
    palette = compile_point_table(key)
    source = source_image()
    from comic_editor.ui.modifier_rendering import _qimage_premultiplied
    pixels = _qimage_premultiplied(source)
    expected = apply_modifier_stack(source, modifiers, (0,0), return_pixels=True, _point_lut=False)
    actual = gpu.apply_lut(pixels, palette, source_key='reference', palette_key=key)
    np.testing.assert_array_equal(actual, expected)
    assert gpu.uploads == 2 and gpu.draws == 1 and gpu.readbacks == 1
    np.testing.assert_array_equal(gpu.apply_lut(pixels, palette, source_key='reference', palette_key=key), expected)
    assert gpu.uploads == 2 and gpu.draws == 1 and gpu.hits == 1
    modifiers[-1].brightness += 5
    changed = signature(modifiers)
    gpu.apply_lut(pixels, compile_point_table(changed), source_key='reference', palette_key=changed)
    assert gpu.uploads == 3 and gpu.draws == 2 and gpu.compiles == 2
    assert gpu.bytes <= gpu.budget


def test_native_resource_budget_evicts_and_refuses_oversized_work(gpu):
    gpu.budget = 4096
    pixels = np.full((8,8,4), .5, np.float32)
    for i in range(12):
        result = gpu.apply(pixels, [(1,.1,1)], source_key=i)
        assert result is not None and gpu.bytes <= gpu.budget
    assert gpu.apply(np.zeros((80,80,4),np.float32), [(1,0,0)], source_key='too-large') is None
