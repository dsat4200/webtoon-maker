"""Ancestor warp previews keep warmed dependency captures and shared rigs correct."""
import copy

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.models import (
    BoundGeometry, HalftoneModifier, HueSaturationLightnessModifier, ImageObject,
    ParameterMaskBinding, ToneMask,
)
from comic_editor.ui.halftone_source import render_color_source, source_signature
from test_page_warp_transform import page_scene, pixels, start_move


def clear_pixels(canvas):
    canvas._effect_jobs.cancel()
    for name in ("_modifier_render_cache", "_modifier_source_cache", "_tone_mask_contributor_cache"):
        getattr(canvas, name).clear()
        setattr(canvas, name + "_bytes", 0)
    canvas._distort_preparation_cache = None


def child(canvas, warp):
    kind, identifier = canvas.chapter.modifier_target_ids(warp.modifier_id)[0]
    assert kind == "object"
    return canvas.chapter.objects[identifier]


@pytest.mark.parametrize("warm", [False, True])
def test_nested_transformed_descendant_preview_matches_translated_cached_or_cold_pixels(page_scene, warm):
    canvas, page, warp = page_scene
    obj = child(canvas, warp)
    group = canvas.chapter.add_layer(page.layer_id, "Rotated group",
        BoundGeometry.rectangle(20, 20, 260, 260))
    group.fill_color, group.border_width = None, 0
    group.transform_frame = (20, 20, 260, 260)
    group.transform_quad = [(280, 20), (280, 280), (20, 280), (20, 20)]
    canvas.chapter.move_entity("object", obj.object_id, group.layer_id, 0)
    before = pixels(canvas)
    stored = copy.deepcopy(canvas.chapter.to_dict())
    before_signature = canvas._modifier_parameter_signature([warp.modifier_id])
    if not warm:
        clear_pixels(canvas)
    start_move(canvas, (35, 23))
    assert canvas._modifier_parameter_signature([warp.modifier_id]) != before_signature
    np.testing.assert_array_equal(pixels(canvas, (35, 23)), before)
    assert canvas.chapter.to_dict() == stored
    clear_pixels(canvas)
    np.testing.assert_array_equal(pixels(canvas, (35, 23)), before)
    canvas._clear_transform_preview()
    assert canvas._modifier_parameter_signature([warp.modifier_id]) == before_signature
    np.testing.assert_array_equal(pixels(canvas), before)


@pytest.mark.parametrize("target_kind", ["object", "layer"])
def test_warmed_warp_mask_contributor_tracks_ancestor_preview_and_cancel(page_scene, target_kind):
    canvas, page, warp = page_scene
    obj = child(canvas, warp)
    target = obj.object_id
    if target_kind == "layer":
        group = canvas.chapter.add_layer(page.layer_id, "Mask contributor",
            BoundGeometry.rectangle(20, 20, 260, 260))
        group.fill_color, group.border_width = None, 0
        canvas.chapter.move_entity("object", obj.object_id, group.layer_id, 0)
        target = group.layer_id
    mask = ToneMask(contributors=[(target_kind, target)])
    canvas.chapter.masks[mask.mask_id] = mask
    before_signature = canvas._tone_mask_signature(mask.mask_id)

    def field():
        return canvas.render_tone_mask_field(mask.mask_id, 320, 320,
            QTransform(), QRectF(0, 0, 320, 320))

    before = field().copy()
    start_move(canvas, (35, 23))
    assert canvas._tone_mask_signature(mask.mask_id) != before_signature
    expected = np.zeros_like(before)
    expected[23:, 35:] = before[:-23, :-35]
    np.testing.assert_array_equal(field(), expected)
    clear_pixels(canvas)
    np.testing.assert_array_equal(field(), expected)
    canvas._clear_transform_preview()
    assert canvas._tone_mask_signature(mask.mask_id) == before_signature
    np.testing.assert_array_equal(field(), before)


@pytest.mark.parametrize("target_kind", ["object", "layer"])
def test_warmed_halftone_color_capture_tracks_ancestor_preview(page_scene, target_kind):
    canvas, page, warp = page_scene
    obj = child(canvas, warp)
    target = obj.object_id if target_kind == "object" else page.layer_id
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=target)
    source = QImage(320, 320, QImage.Format_ARGB32_Premultiplied)
    source.fill(Qt.transparent)

    def colors(delta=(0, 0)):
        return render_color_source(canvas, modifier, source,
            QRectF(delta[0], delta[1], 320, 320), QTransform())

    before = colors()
    signature = source_signature(canvas, target)
    start_move(canvas, (35, 23))
    assert source_signature(canvas, target) != signature
    assert colors((35, 23)) == before
    clear_pixels(canvas)
    assert colors((35, 23)) == before
    canvas._clear_transform_preview()
    assert source_signature(canvas, target) == signature
    assert colors() == before


def test_shared_warp_stays_document_fixed_and_matches_commit(page_scene):
    canvas, page, warp = page_scene
    other_page = canvas.chapter.add_page("Unaffected", BoundGeometry.rectangle(330, 20, 160, 260))
    other_page.fill_color, other_page.border_width = None, 0
    other = canvas.chapter.add_object(other_page.layer_id,
        ImageObject(x=350, y=70, pixel_width=128, pixel_height=128))
    original = child(canvas, warp)
    canvas.images.put_decoded(other.object_id, "other.png", b"", canvas.images.image(original.object_id))
    canvas.chapter.set_modifier_targets(warp.modifier_id,
        [("object", original.object_id), ("object", other.object_id)])
    stored = copy.deepcopy(warp.to_dict())
    signature = canvas._modifier_parameter_signature([warp.modifier_id])
    pixels(canvas)
    start_move(canvas, (35, 23))
    assert canvas._active_modifier_instances([warp.modifier_id])[0].to_dict() == stored
    assert canvas._modifier_parameter_signature([warp.modifier_id]) == signature
    preview = pixels(canvas, (35, 23))
    canvas._commit_geometry_transform()
    assert canvas.chapter.modifiers[warp.modifier_id].to_dict() == stored
    np.testing.assert_array_equal(pixels(canvas, (35, 23)), preview)


def test_world_painted_hsl_mask_keeps_geometry_and_matches_preview_commit(page_scene):
    canvas, _page, warp = page_scene
    obj = child(canvas, warp)
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    canvas.tiles.paint_dab(mask.mask_id, QPointF(110, 134), 80, QColor("white"))
    effect = HueSaturationLightnessModifier(hue=80, parameter_masks={
        "intensity": ParameterMaskBinding(mask.mask_id, 0., 100.)})
    # A nonmoving consumer keeps this mask fixed in chapter coordinates.
    # Place it outside both capture rectangles so this test compares only
    # the moving object's painted parameter field and native warped pixels.
    fixed_page = canvas.chapter.add_page("Fixed mask consumer",
        BoundGeometry.rectangle(700, 20, 260, 260))
    fixed_page.fill_color, fixed_page.border_width = None, 0
    fixed = canvas.chapter.add_object(fixed_page.layer_id,
        ImageObject(x=750, y=70, pixel_width=128, pixel_height=128))
    canvas.images.put_decoded(fixed.object_id, "fixed-consumer.png", b"",
                             canvas.images.image(obj.object_id))
    canvas.chapter.add_modifier(effect,
        [("object", obj.object_id), ("object", fixed.object_id)])
    before = pixels(canvas).reshape(320, 320, 4)
    mask_state = copy.deepcopy(mask.to_dict())
    paint = {key: bytes(tile.constBits()) for key, tile in canvas.tiles.object_tiles(mask.mask_id).items()}
    start_move(canvas, (35, 23))
    preview = pixels(canvas, (35, 23)).reshape(320, 320, 4)
    np.testing.assert_array_equal(preview[..., 3], before[..., 3])
    assert np.any(preview[..., :3] != before[..., :3])  # Paint is chapter-local.
    canvas._commit_geometry_transform()
    np.testing.assert_array_equal(pixels(canvas, (35, 23)).reshape(320, 320, 4), preview)
    assert canvas.chapter.masks[mask.mask_id].to_dict() == mask_state
    assert {key: bytes(tile.constBits()) for key, tile in canvas.tiles.object_tiles(mask.mask_id).items()} == paint


def test_exclusively_owned_painted_hsl_mask_moves_native_grid_with_page_and_history(page_scene):
    canvas, _page, warp = page_scene
    obj = child(canvas, warp)
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    canvas.tiles.paint_dab(mask.mask_id, QPointF(110, 134), 80, QColor("white"))
    effect = HueSaturationLightnessModifier(hue=80, parameter_masks={
        "intensity": ParameterMaskBinding(mask.mask_id, 0., 100.)})
    canvas.chapter.add_modifier(effect, [("object", obj.object_id)])
    before = pixels(canvas).reshape(320, 320, 4)
    mask_state = copy.deepcopy(mask.to_dict())
    paint = {key: bytes(tile.constBits()) for key, tile in canvas.tiles.object_tiles(mask.mask_id).items()}
    start_move(canvas, (35, 23))
    preview = pixels(canvas, (35, 23)).reshape(320, 320, 4)
    np.testing.assert_array_equal(preview, before)
    assert canvas.chapter.masks[mask.mask_id].to_dict() == mask_state
    canvas._commit_geometry_transform()
    np.testing.assert_array_equal(pixels(canvas, (35, 23)).reshape(320, 320, 4), preview)
    expected_mask = copy.deepcopy(mask_state)
    expected_mask["paint_offset"] = [35., 23.]
    assert canvas.chapter.masks[mask.mask_id].to_dict() == expected_mask
    assert {key: bytes(tile.constBits()) for key, tile in canvas.tiles.object_tiles(mask.mask_id).items()} == paint
    canvas.command_stack.undo()
    assert canvas.chapter.masks[mask.mask_id].to_dict() == mask_state
    np.testing.assert_array_equal(pixels(canvas).reshape(320, 320, 4), before)
    canvas.command_stack.redo()
    assert canvas.chapter.masks[mask.mask_id].to_dict() == expected_mask
    np.testing.assert_array_equal(pixels(canvas, (35, 23)).reshape(320, 320, 4), preview)
    assert {key: bytes(tile.constBits()) for key, tile in canvas.tiles.object_tiles(mask.mask_id).items()} == paint
