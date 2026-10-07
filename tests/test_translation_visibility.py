"""Descendant visibility cannot leak through exact translation aliases."""
from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import BlurModifier, BoundGeometry
from comic_editor.ui import translation_cache
from test_effect_capture_integrity import capture_scene
from test_attached_translation import scene, render, mask_for, FACTORIES


def alias(canvas, layer, effects):
    return translation_cache.output_key(canvas, layer, QRectF(0, 0, 700, 400),
                                        QTransform(), effects)


@pytest.mark.parametrize('interactive', [False, True])
def test_descendant_mask_only_visibility_rejects_translation_alias(capture_scene, interactive):
    canvas, _, layer, child, _ = capture_scene
    effects = [BlurModifier(strength=3)]
    ordinary = alias(canvas, layer, effects)
    assert ordinary is not None
    child.mask_only = True
    canvas.set_selection('layer', child.layer_id)
    canvas._interactive_render = interactive
    assert not canvas._modifier_layer_signature(layer.layer_id)[4]
    assert canvas._modifier_layer_signature(child.layer_id)[4]
    assert alias(canvas, layer, effects) is None
    child.mask_only = False
    assert alias(canvas, layer, effects) == ordinary


def test_nested_layer_transform_preview_rejects_parent_alias(capture_scene):
    canvas, _, layer, child, _ = capture_scene
    effects = [BlurModifier(strength=3)]
    ordinary = alias(canvas, layer, effects)
    assert ordinary is not None
    canvas.set_selection('layer', child.layer_id)
    canvas._geometry_transform_target = ('layer', child.layer_id)
    canvas._transform_start_quad = canvas._rect_quad(QRectF(*child.bound.bbox()))
    canvas._transform_preview_quad = [(x+35, y+23) for x, y in canvas._transform_start_quad]
    assert not canvas._modifier_layer_signature(layer.layer_id)[4]
    assert canvas._modifier_layer_signature(child.layer_id)[4]
    assert alias(canvas, layer, effects) is None
    canvas._geometry_transform_target = canvas._transform_start_quad = canvas._transform_preview_quad = None
    assert alias(canvas, layer, effects) == ordinary


@pytest.mark.parametrize('kind', ['solo', 'show-on-top', 'mask-only-visible', 'unknown-future-preview'])
def test_any_descendant_scope_preview_cannot_bypass_root_eligibility(capture_scene, monkeypatch, kind):
    canvas, _, layer, child, _ = capture_scene
    effects = [BlurModifier(strength=3)]
    ordinary = alias(canvas, layer, effects)
    assert ordinary is not None
    original = canvas._modifier_layer_signature
    def scoped(identifier):
        record = original(identifier)
        # Simulate a scope-local descendant preview while the root's own live
        # tuple remains empty. The actual visibility/transform cases above use
        # real model flags; this checks conservative future-token handling.
        return (*record[:4], (*record[4], (kind, ('layer', child.layer_id)))) if identifier == child.layer_id else record
    monkeypatch.setattr(canvas, '_modifier_layer_signature', scoped)
    assert not canvas._modifier_layer_signature(layer.layer_id)[4]
    assert alias(canvas, layer, effects) is None
    monkeypatch.setattr(canvas, '_modifier_layer_signature', original)
    assert alias(canvas, layer, effects) == ordinary


def test_ordinary_nested_group_translation_reuses_alias_and_native_pixels(scene, monkeypatch):
    canvas, obj = scene
    group = canvas.chapter.add_layer(obj.parent_layer_id, 'Group', BoundGeometry.rectangle(60, 60, 160, 160))
    nested = canvas.chapter.add_layer(group.layer_id, 'Nested', BoundGeometry.rectangle(60, 60, 160, 160))
    for layer in (group, nested):
        layer.fill_color, layer.border_width = None, 0
    canvas.chapter.move_entity('object', obj.object_id, nested.layer_id, 0)
    canvas.chapter.add_modifier(FACTORIES[0](), [('layer', group.layer_id)])
    child_effect = FACTORIES[5]()
    canvas.chapter.add_modifier(child_effect, [('object', obj.object_id)])
    mask_for(canvas, obj, child_effect)
    before = render(canvas)
    canvas.set_selection('layer', group.layer_id)
    canvas._model_before = deepcopy(canvas.chapter.to_dict())
    canvas._geometry_transform_target = ('layer_group', group.layer_id)
    canvas._transform_start_quad = canvas._rect_quad(QRectF(*group.bound.bbox()))
    canvas._transform_preview_quad = [(x+35, y+23) for x, y in canvas._transform_start_quad]
    canvas._transform_drag_mode = 'translate'
    fields = []
    original = canvas._modifier_mask_fields
    def counted(*args, **kwargs):
        fields.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(canvas, '_modifier_mask_fields', counted)
    during = render(canvas)
    assert not fields
    canvas._commit_geometry_transform()
    after = render(canvas)
    np.testing.assert_array_equal(during, after)
    assert not fields
    np.testing.assert_array_equal(before[40:225, 40:225], after[63:248, 75:260])
    monkeypatch.setattr(translation_cache, 'get', lambda *_: None)
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    for scope in list(canvas._effect_jobs.retained):
        canvas._effect_jobs.retained_remove(scope)
    np.testing.assert_array_equal(after, render(canvas))
