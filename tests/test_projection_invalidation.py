"""Retained document pixels follow real edits across capture block boundaries."""
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, HueSaturationLightnessModifier, ImageObject, OutlineModifier,
    ParameterMaskBinding, RasterObject, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=2048, height=512, document_kind="asset", background="#FFFFFFFF")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 2048, 512))
    page.fill_color, page.border_width = None, 0
    group = chapter.add_layer(page.layer_id, "Group", BoundGeometry.rectangle(0, 0, 2048, 512))
    group.fill_color, group.border_width = None, 0
    obj = chapter.add_object(group.layer_id, RasterObject(interaction_rect=(0, 0, 2048, 512)))
    tiles = TileStore()
    for x in (400, 1200):
        tiles.paint_dab(obj.object_id, QPointF(x, 120), 8, QColor("#FFFF238A"))
    settings = EditorSettings(canvas_renderer="raster", snap_to_grid=False,
                              predictive_ink=False, grid_overlay_visible=False)
    settings.pencil_size_px[settings.active_pencil_size] = 8
    canvas = CanvasWidget(settings)
    canvas.setMinimumSize(1, 1)
    canvas.resize(1024, 256)
    canvas.set_document(chapter, tiles)
    canvas.center_x, canvas.center_y, canvas.scale = 1024., 128., 1.
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.primary_color = "#FFFF238A"
    canvas._document_projection_enabled = True
    yield canvas, obj, group
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def frame(canvas, *, fresh=False):
    if fresh:
        canvas._document_projection.clear()
    # Recompose the entire screen from retained document tiles to expose stale
    # document pixels independently of the legacy widget dirty rectangle.
    canvas._invalidate_scene_cache(projection=False)
    canvas._ensure_scene_cache()
    assert not canvas._effect_jobs.running and not canvas._effect_jobs.pending
    return canvas._scene_cache.copy()


@pytest.mark.parametrize("ancestor", [False, True], ids=["same-stack", "ancestor-stack"])
def test_cumulative_effect_halo_invalidates_across_document_blocks(scene, ancestor):
    canvas, obj, page = scene
    canvas.chapter.add_modifier(OutlineModifier(thickness=20), [("object", obj.object_id)])
    canvas.chapter.add_modifier(OutlineModifier(thickness=20),
        [("layer", page.layer_id)] if ancestor else [("object", obj.object_id)])
    before = frame(canvas)
    canvas._begin_stroke(QPointF(990, 120), 1.)
    canvas._end_stroke()
    updated = frame(canvas)
    expected = frame(canvas, fresh=True)
    assert updated != before
    # The second outline extends into the next 1024px capture block. A maximum
    # halo (instead of cumulative support) leaves that neighboring tile stale.
    assert updated == expected


def test_unrelated_partial_redraws_keep_exact_pixels_across_blocks(scene):
    canvas, obj, _ = scene
    canvas.chapter.add_modifier(OutlineModifier(thickness=20), [("object", obj.object_id)])
    expected = frame(canvas)
    for region in (QRectF(1018, 110, 12, 8), QRectF(770, 110, 8, 8), QRectF(1280, 120, 8, 8)):
        canvas._mark_scene_dirty_world(region)
        assert frame(canvas) == expected


def test_shared_modifier_edit_refreshes_both_sides_of_block_boundary(scene):
    canvas, obj, page = scene
    other = canvas.chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 2048, 512)))
    canvas.tiles.paint_dab(other.object_id, QPointF(800, 120), 8, QColor("#FF12AACC"))
    modifier = OutlineModifier(thickness=10)
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id), ("object", other.object_id)])
    before = frame(canvas)
    modifier.color = "#FF24BB44"
    modifier.thickness = 18
    canvas.documentChanged.emit(QRectF())
    updated = frame(canvas)
    assert updated != before
    assert updated == frame(canvas, fresh=True)


def test_painted_parameter_mask_refreshes_cached_output(scene):
    canvas, obj, _ = scene
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    modifier = OutlineModifier(thickness=20)
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    before = frame(canvas)
    canvas.tiles.paint_dab(mask.mask_id, QPointF(1200, 120), 80, QColor("white"))
    mask.touch()
    canvas._mask_tiles_changed()
    updated = frame(canvas)
    assert updated != before
    assert updated == frame(canvas, fresh=True)


def test_parent_scale_expands_effect_dirty_footprint_in_world_space(scene):
    canvas, obj, group = scene
    group.transform_frame = (0, 0, 2048, 512)
    group.transform_quad = [(0, 0), (4096, 0), (4096, 1024), (0, 1024)]
    canvas.chapter.add_modifier(OutlineModifier(thickness=20), [("object", obj.object_id)])
    before = frame(canvas)
    canvas._begin_stroke(QPointF(990, 120), 1.)
    canvas._end_stroke()
    updated = frame(canvas)
    expected = frame(canvas, fresh=True)
    assert updated != before
    assert updated == expected


def test_mask_enabled_effect_with_zero_nominal_intensity_has_dirty_halo(scene):
    canvas, obj, _ = scene
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    modifier = OutlineModifier(thickness=25, intensity=0)
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 100, 100)
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    before = frame(canvas)
    canvas._begin_stroke(QPointF(1005, 120), 1.)
    canvas._end_stroke()
    updated = frame(canvas)
    expected = frame(canvas, fresh=True)
    assert updated != before
    assert expected.pixelColor(1030 - 512, 120) != QColor("white")
    assert updated == expected


def test_subtree_dirty_accumulates_support_in_each_owners_parent_frame(scene):
    canvas, obj, group = scene
    group.transform_frame = (0, 0, 2048, 512)
    group.transform_quad = [(0, 0), (4096, 0), (4096, 1024), (0, 1024)]
    canvas.chapter.add_modifier(OutlineModifier(thickness=20), [("object", obj.object_id)])
    canvas.chapter.add_modifier(OutlineModifier(thickness=5), [("layer", group.layer_id)])
    dirty = QRectF(990, 120, 8, 8)
    # The object's effect is scaled by its parent; the parent's own effect is
    # applied after that transform and remains 5 document pixels wide.
    expected = dirty.adjusted(-45, -45, 45, 45)
    assert canvas.modifier_expanded_dirty(obj.object_id, dirty) == expected
    assert canvas._entity_expanded_dirty("object", obj.object_id, dirty) == expected
    assert canvas._entity_expanded_dirty("layer", group.layer_id, dirty) == expected


@pytest.mark.parametrize("staged", [False, True], ids=["parent-frame", "native-raster-frame"])
def test_object_placement_scales_only_native_raster_effect_support(scene, staged):
    canvas, obj, _ = scene
    obj.transform_frame = (0, 0, 2048, 512)
    obj.transform_quad = [(0, 0), (4096, 0), (4096, 1024), (0, 1024)]
    if staged:
        obj.modifier_source_frame = (0, 0, 2048, 512)
    canvas.chapter.add_modifier(OutlineModifier(thickness=20), [("object", obj.object_id)])
    dirty = QRectF(990, 120, 8, 8)
    halo = 40 if staged else 20
    expected = dirty.adjusted(-halo, -halo, halo, halo)
    assert canvas.modifier_expanded_dirty(obj.object_id, dirty) == expected
    assert canvas._entity_expanded_dirty("object", obj.object_id, dirty) == expected


def test_warm_block_image_move_completes_effects_in_first_frame(scene, monkeypatch):
    canvas, _, group = scene
    obj = canvas.chapter.add_object(group.layer_id, ImageObject(
        x=600, y=60, pixel_width=200, pixel_height=150))
    source = QImage(200, 150, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("#FF20A0D0"))
    canvas.images.put_decoded(obj.object_id, "moving.png", b"", source)
    canvas.chapter.add_modifier(HueSaturationLightnessModifier(hue=90), [("object", obj.object_id)])
    before = frame(canvas)
    requests = []
    # Record worker requests without letting completion timing conceal a
    # missing first-frame object between two already-warm capture blocks.
    monkeypatch.setattr(canvas._effect_jobs, "request", lambda *args, **kwargs: requests.append(args) or True)
    old_bounds = canvas.object_world_rect(obj.object_id)
    obj.x = 1100
    new_bounds = canvas.object_world_rect(obj.object_id)
    canvas.documentChanged.emit(old_bounds.united(new_bounds))
    updated = frame(canvas)
    assert updated != before
    assert updated.pixelColor(1150 - 512, 80) != QColor("white")
    assert updated == frame(canvas, fresh=True)
    assert requests == []


def test_shrinking_transform_invalidates_previous_scaled_effect_halo(scene):
    canvas, _, group = scene
    obj = canvas.chapter.add_object(group.layer_id, RasterObject(
        interaction_rect=(0, 0, 20, 20), transform_frame=(0, 0, 20, 20),
        modifier_source_frame=(0, 0, 20, 20)))
    canvas.tiles.paint_dab(obj.object_id, QPointF(10, 10), 20, QColor("red"), square=True)
    canvas.chapter.add_modifier(OutlineModifier(thickness=20), [("object", obj.object_id)])
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.settings.transform_mode = "uniform"
    canvas._transform_start_quad = [(880, 80), (980, 80), (980, 180), (880, 180)]
    canvas._transform_preview_quad = list(canvas._transform_start_quad)
    canvas._transform_drag_mode = "scale"
    canvas._transform_handle_index = 2
    canvas._drag_start_doc = QPointF(980, 180)
    before = frame(canvas)
    assert before.pixelColor(1040 - 512, 120) != QColor("white")
    canvas._update_transform_preview(QPointF(890, 90))
    updated = frame(canvas)
    assert updated != before
    assert updated.pixelColor(1040 - 512, 120) == QColor("white")
    assert updated == frame(canvas, fresh=True)


@pytest.mark.parametrize("kind", ["object", "layer"])
def test_edit_below_mask_contributor_invalidates_dependants(scene, kind):
    canvas, obj, group = scene
    mask = ToneMask(contributors=[("layer", group.layer_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    if kind == "layer":
        child = canvas.chapter.add_layer(group.layer_id, "Moving child",
                                        BoundGeometry.rectangle(990, 120, 8, 8))
        identifier = child.layer_id
    else:
        identifier = obj.object_id
    expanded = canvas._entity_expanded_dirty(kind, identifier, QRectF(990, 120, 8, 8))
    assert expanded.contains(QRectF(0, 0, canvas.chapter.width, canvas.chapter.height))
