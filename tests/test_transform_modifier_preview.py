"""Value-only rig previews match commit without changing the stored document."""
import copy

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QTransform

from comic_editor.core.models import (
    ArrayModifier, BlurModifier, CageTransformModifier, DistortModifier,
    MirrorModifier, RadialBlurModifier, TilingModifier, TextureModifier,
)
from comic_editor.ui.transform_modifier_preview import effective_preview_modifier
from test_page_warp_transform import page_scene, start_move


def add_rig(canvas, kind):
    modifier = kind()
    if isinstance(modifier, DistortModifier):
        modifier.modifier_type = "distort_mesh_warp"
    if isinstance(modifier, TextureModifier):
        modifier.texture_quad = [(20., 20.), (160., 20.), (160., 160.), (20., 160.)]
    modifier.validate()
    target = next(iter(canvas.chapter.objects.values()))
    canvas.chapter.add_modifier(modifier, [("object", target.object_id)])
    return modifier, target


@pytest.mark.parametrize("kind", [ArrayModifier, BlurModifier, CageTransformModifier,
    DistortModifier, MirrorModifier, RadialBlurModifier, TilingModifier, TextureModifier])
@pytest.mark.parametrize("rotate_scale", [False, True])
def test_all_rig_preview_values_match_commit_without_model_mutation(page_scene, kind, rotate_scale):
    canvas, _page, _warp = page_scene
    modifier, _target = add_rig(canvas, kind)
    saved = copy.deepcopy(canvas.chapter.to_dict())
    start_move(canvas, (35, 23))
    if rotate_scale:
        mapping = QTransform().translate(35, 23).rotate(30).scale(1.5, 1.5)
        canvas._transform_preview_quad = [mapping.map(QPointF(*p)).toTuple()
                                          for p in canvas._transform_start_quad]
    effective = effective_preview_modifier(canvas, modifier)
    assert effective is not modifier
    preview = copy.deepcopy(effective.to_dict())
    assert canvas.chapter.to_dict() == saved
    canvas._commit_geometry_transform()
    assert canvas.chapter.modifiers[modifier.modifier_id].to_dict() == preview
    assert effective_preview_modifier(canvas, modifier) is modifier


def test_nested_parent_coordinates_are_converted_to_world_delta(page_scene):
    canvas, page, warp = page_scene
    # Move a child group in the local coordinates of an already rotated page.
    group = canvas.chapter.add_layer(page.layer_id, "Child")
    target = next(iter(canvas.chapter.objects.values()))
    canvas.chapter.move_entity("object", target.object_id, group.layer_id, 0)
    page.transform_frame = (20, 20, 260, 260)
    page.transform_quad = [(280, 20), (280, 280), (20, 280), (20, 20)]
    canvas._geometry_transform_target = ("layer_group", group.layer_id)
    canvas._transform_start_quad = [(0., 0.), (100., 0.), (100., 100.), (0., 100.)]
    canvas._transform_preview_quad = [(x + 10, y + 20) for x, y in canvas._transform_start_quad]
    preview = effective_preview_modifier(canvas, warp)
    np.testing.assert_allclose(preview.center, (warp.center[0] - 20, warp.center[1] + 10))
    np.testing.assert_allclose(preview.frame, (warp.frame[0] - 20, warp.frame[1] + 10, *warp.frame[2:]))
    assert warp.center == (134., 134.)


def test_tiling_accessor_tracks_preview_and_returns_original_after_cancel(page_scene):
    canvas, _page, _warp = page_scene
    modifier, target = add_rig(canvas, TilingModifier)
    modifier.center = (90., 100.)
    assert canvas._own_tiling(target) is modifier
    start_move(canvas, (35, 23))
    assert canvas._own_tiling(target).center == (125., 123.)
    assert modifier.center == (90., 100.)
    canvas._clear_transform_preview()
    assert canvas._own_tiling(target) is modifier


def test_preview_cache_is_bounded_and_detects_changed_rig_values(page_scene):
    canvas, _page, _warp = page_scene
    modifiers = [add_rig(canvas, MirrorModifier)[0] for _ in range(130)]
    start_move(canvas, (35, 23))
    for modifier in modifiers:
        effective_preview_modifier(canvas, modifier)
    cache = canvas._transform_modifier_preview_cache
    assert len(cache[3]) == 128
    modifier = modifiers[-1]
    first = effective_preview_modifier(canvas, modifier)
    assert effective_preview_modifier(canvas, modifier) is first
    assert canvas._transform_modifier_preview_cache is cache
    modifier.axis_start = (12., 18.)
    updated = effective_preview_modifier(canvas, modifier)
    assert updated is not first
    assert updated.axis_start == (47., 41.)
    canvas._clear_transform_preview()
    assert effective_preview_modifier(canvas, modifier) is modifier
    assert canvas._transform_modifier_preview_cache is None
