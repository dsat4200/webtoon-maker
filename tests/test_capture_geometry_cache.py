"""Pure capture-local planning reuse preserves unsignaled state and native math."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextvars import copy_context
from dataclasses import dataclass, field
import json
import math

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.effect_geometry import effect_bounds
from comic_editor.core.models import ParameterMaskBinding
from comic_editor.render import geometry_cache as cache
from comic_editor.ui import distort_rendering as distortion
from comic_editor.ui.modifier_rendering import modifier_render_settings
from comic_editor.ui.render_signatures import signature_scope
from test_distort_rendering import modifier, source_image, BOUNDS


@pytest.mark.parametrize('smoothness', [0., 50., 100.])
@pytest.mark.parametrize('rows,columns', [(2, 2), (5, 6)])
def test_bounds_destination_tessellation_matches_full_mesh_exactly(smoothness, rows, columns):
    effect = modifier('mesh_warp', rows=rows, columns=columns, smoothness=smoothness)
    effect.points[1] = (-.25, .3)
    effect.source_points[2] = (.35, 1.3)
    frame = np.array((-30.125, 100.0000002, 1041.25, 1778.5))
    expected = distortion._mesh(effect, frame, effect.parameters)[1]
    actual = distortion._mesh_bounds_destinations(effect, frame, effect.parameters)
    np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize('attribute,value', [('points', [(0., 0.)]),
    ('source_points', [(0., 0.)])])
def test_bounds_specialization_preserves_source_and_destination_validation(attribute, value):
    effect = modifier('mesh_warp')
    setattr(effect, attribute, value)
    frame = np.asarray(effect.frame)
    with pytest.raises(ValueError) as full:
        distortion._mesh(effect, frame, effect.parameters)
    with pytest.raises(ValueError) as bounds:
        distortion._mesh_bounds_destinations(effect, frame, effect.parameters)
    assert str(bounds.value) == str(full.value)


def test_raw_nonfinite_source_does_not_change_destination_bounds_behavior():
    effect = modifier('mesh_warp')
    effect.source_points = [(float('nan'), 0.)] * 16
    frame = np.asarray(effect.frame)
    # The original raw helper propagates nonfinite source samples without
    # validating them; bounds still use only its finite destination output.
    expected = distortion._mesh(effect, frame, effect.parameters)[1]
    np.testing.assert_array_equal(
        distortion._mesh_bounds_destinations(effect, frame, effect.parameters), expected)


def test_mesh_bounds_specialization_preserves_native_pixels(monkeypatch):
    effect = modifier('mesh_warp', smoothness=75)
    effect.points[5] = (.72, .39)
    mapping = QTransform().translate(.0000002, -.0000003).rotate(13).scale(.8, 1.2)
    bounds = distortion.distort_bounds(BOUNDS, effect, mapping)
    actual = distortion.render_distort(source_image(), BOUNDS, effect, mapping, bounds)
    with monkeypatch.context() as patch:
        patch.setattr(distortion, '_mesh_bounds_destinations',
                      lambda m, f, p: distortion._mesh(m, f, p)[1])
        original_bounds = distortion.distort_bounds(BOUNDS, effect, mapping)
        expected = distortion.render_distort(source_image(), BOUNDS, effect, mapping, original_bounds)
    assert bounds.getRect() == original_bounds.getRect()
    assert bytes(actual.constBits()) == bytes(expected.constBits())


def test_bounds_memo_reuses_math_but_checks_mutable_fields_every_lookup(monkeypatch):
    effect = modifier('mesh_warp', smoothness=50)
    calls = []
    original = distortion.distort_bounds
    def count(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(distortion, 'distort_bounds', count)
    mapping = QTransform().rotate(13).scale(.8, 1.2)
    with cache.geometry_scope(('canvas', 'exact')):
        first = effect_bounds(BOUNDS, [effect], mapping)
        assert effect_bounds(BOUNDS, [effect], mapping).getRect() == first.getRect()
        first.translate(200, 300)
        assert effect_bounds(BOUNDS, [effect], mapping) != first
        assert len(calls) == 1
        effect.points[5] = (.8, -.4)
        assert effect_bounds(BOUNDS, [effect], mapping) != first
        effect.source_points[1] = (.2, .8)
        effect_bounds(BOUNDS, [effect], mapping)
        effect.parameters['smoothness'] = 25
        effect_bounds(BOUNDS, [effect], mapping)
        effect.parameter_masks['intensity'] = ParameterMaskBinding(
            mask_id='mask', black_value=20, white_value=90)
        effect_bounds(BOUNDS, [effect], mapping)
        effect.parameter_masks['intensity'].white_value = 70
        effect_bounds(BOUNDS, [effect], mapping)
        assert len(calls) == 6


def test_unrounded_bounds_and_mapping_are_distinct(monkeypatch):
    effect = modifier('lens_distortion', amount=65)
    calls = []
    original = distortion.distort_bounds
    monkeypatch.setattr(distortion, 'distort_bounds',
                        lambda *args: calls.append(1) or original(*args))
    bounds = QRectF(.0000001, -.0000002, 64, 48)
    mapping = QTransform().rotate(13).scale(.8, 1.2)
    with cache.geometry_scope():
        effect_bounds(bounds, [effect], mapping)
        bounds.translate(.0000001, -.0000001)
        effect_bounds(bounds, [effect], mapping)
        mapping.translate(.0000001, 0)
        effect_bounds(bounds, [effect], mapping)
    assert len(calls) == 3


def test_capture_settings_strings_remain_exact_and_unsignaled_changes_are_visible():
    effect = modifier('twirl', angle=65)
    calls = []
    def serialize(value):
        calls.append(1)
        return modifier_render_settings(value)
    with cache.geometry_scope():
        expected = repr(modifier_render_settings(effect))
        assert cache.settings_signature(effect, serialize) == expected
        assert cache.settings_signature(effect, serialize) == expected
        assert len(calls) == 1
        effect.parameters['angle'] = -30
        assert cache.settings_signature(effect, serialize) != expected
        assert len(calls) == 2
        effect.parameter_masks['intensity'] = ParameterMaskBinding(mask_id='mask')
        masked = cache.settings_signature(effect, serialize)
        effect.parameter_masks['intensity'].black_value = 20
        assert cache.settings_signature(effect, serialize) != masked
        assert len(calls) == 4


def test_numeric_fingerprint_preserves_types_and_signed_zero():
    tokens = [cache._fingerprint(value)[0] for value in (True, 1, 1., False, 0, 0., -0.)]
    assert len(set(tokens)) == len(tokens)


def test_finite_float_tokens_preserve_adjacent_and_extreme_values():
    values = (1., math.nextafter(1., 2.), 0., -0., 1e300, -1e300, 1e-300, -1e-300)
    states = [cache._fingerprint(value) for value in values]
    assert len({state[0] for state in states}) == len(values)
    assert all(state[1] >= 160 for state in states)


def test_dataclass_type_schema_and_original_named_field_charge_are_preserved():
    @dataclass
    class Values:
        label: str = 'raw'
        scalar: float = -0.
        flag: bool = True
    @dataclass
    class OtherValues:
        label: str = 'raw'
        scalar: float = -0.
        flag: bool = True
    token, retained = cache._fingerprint(Values())
    other, other_retained = cache._fingerprint(OtherValues())
    assert token != other
    # Reference named-field encoding: 136 outer bytes, then 384 for label,
    # 424 for scalar and 352 for flag, including all name/pair allowances.
    assert retained == other_retained == 1296


def test_dataclass_settings_recheck_typed_and_nested_mutations_every_lookup():
    @dataclass
    class State:
        scalar: object = 0.
        label: str = 'initial'
        binding: object = field(default_factory=lambda: ParameterMaskBinding('mask', 0, 100))
        options: dict = field(default_factory=lambda: {'axis': [1., 2.]})
    state, calls = State(), []
    def payload(value):
        return {'type': type(value.scalar).__name__, 'scalar': repr(value.scalar),
                'label': value.label, 'binding': value.binding.to_dict(),
                'options': deepcopy(value.options)}
    def serialize(value):
        calls.append(1)
        return payload(value)
    with cache.geometry_scope():
        for index, value in enumerate((True, 1, 1., False, 0, 0., -0.), 1):
            state.scalar = value
            expected = repr(payload(state))
            assert cache.settings_signature(state, serialize) == expected
            assert cache.settings_signature(state, serialize) == expected
            assert len(calls) == index
        previous = cache.settings_signature(state, serialize)
        state.label = 'changed without a model signal'
        assert cache.settings_signature(state, serialize) != previous
        previous = cache.settings_signature(state, serialize)
        state.binding.black_value = 33
        assert cache.settings_signature(state, serialize) != previous
        previous = cache.settings_signature(state, serialize)
        state.options['axis'][0] = math.nextafter(1., 2.)
        assert cache.settings_signature(state, serialize) != previous
        assert len(calls) == 10
        state.options['cycle'] = state
        assert cache._fingerprint(state) is None
        state.options.pop('cycle')
        state.scalar = math.nan
        assert cache._fingerprint(state) is None


def test_dataclass_serializer_normalization_is_snapshotted_after_first_call():
    @dataclass
    class State:
        scalar: object = -1
        controls: dict = field(default_factory=lambda: {'endpoint': [1, 2.]})
    state, calls = State(), []
    def serialize(value):
        calls.append(1)
        value.scalar = max(0., float(value.scalar))
        return {'scalar': value.scalar, 'controls': deepcopy(value.controls)}
    with cache.geometry_scope():
        first = cache.settings_signature(state, serialize)
        assert state.scalar == 0. and type(state.scalar) is float
        assert cache.settings_signature(state, serialize) == first and len(calls) == 1
        state.controls['endpoint'][1] = math.nextafter(2., 3.)
        assert cache.settings_signature(state, serialize) != first
        assert len(calls) == 2


@pytest.mark.parametrize('value', [math.nan, math.inf, -math.inf])
def test_nonfinite_float_and_nested_cycles_bypass_capture_memo(value):
    assert cache._fingerprint({'nested': [value]}) is None
    cyclic = {'nested': []}
    cyclic['nested'].append(cyclic)
    assert cache._fingerprint(cyclic) is None


def test_nested_capture_clears_outer_and_direct_calls_remain_uncached(monkeypatch):
    effect, calls = modifier('twirl', angle=65), []
    original = distortion.distort_bounds
    monkeypatch.setattr(distortion, 'distort_bounds',
                        lambda *args: calls.append(1) or original(*args))
    class Canvas:
        pass
    canvas = Canvas()
    with signature_scope(canvas):
        effect_bounds(BOUNDS, [effect])
        effect_bounds(BOUNDS, [effect])
        assert len(calls) == 1
        with signature_scope(canvas):
            effect_bounds(BOUNDS, [effect])
        effect_bounds(BOUNDS, [effect])
        assert len(calls) == 3
    effect_bounds(BOUNDS, [effect])
    effect_bounds(BOUNDS, [effect])
    assert len(calls) == 5
    assert getattr(cache._local, 'memo', None) is None


def test_capture_scope_never_propagates_to_workers_or_changes_model():
    effect = modifier('mesh_warp')
    before = json.dumps(effect.to_dict(), sort_keys=True)
    with cache.geometry_scope(('canvas', 'interactive')):
        effect_bounds(BOUNDS, [effect])
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(lambda: context.run(
                lambda: getattr(cache._local, 'memo', None))).result() is None
    assert json.dumps(effect.to_dict(), sort_keys=True) == before


def test_geometry_memo_is_bounded_and_unsupported_mutable_values_bypass(monkeypatch):
    effect = modifier('twirl', angle=65)
    monkeypatch.setattr(cache, '_LIMIT', 2)
    with cache.geometry_scope() as memo:
        for offset in range(3):
            effect_bounds(QRectF(offset, 0, 64, 48), [effect])
        assert len(memo.entries) == 2 and 0 < memo.bytes <= cache._BUDGET
        state = deepcopy(effect)
        state.parameters['unsupported'] = object()
        assert cache._fingerprint(state) is None
        previous = len(memo.entries)
        effect_bounds(BOUNDS, [state])
        assert len(memo.entries) == previous
