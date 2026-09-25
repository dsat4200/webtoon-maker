"""Layers-beneath maps use scene pixels without disturbing the live document."""
from __future__ import annotations

import base64
import copy

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, DistortModifier,
    ParameterMaskBinding, RasterObject, ShapeStyle, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.distort_sources import capture_distort_beneath


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=120, height=100, document_kind="image")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 120, 100),
                            style=ShapeStyle(primary_color=None, outline_thickness=0))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    yield canvas, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def ink(canvas, parent, color, center=(40, 40), width=30, index=None):
    obj = canvas.chapter.add_object(parent.layer_id, RasterObject(), index=index)
    canvas.tiles.paint_dab(obj.object_id, QPointF(*center), width, QColor(color))
    return obj


def group(canvas, parent, bounds=(0, 0, 120, 100), index=None):
    return canvas.chapter.add_layer(parent.layer_id, "Group", BoundGeometry.rectangle(*bounds),
                                    index=index, style=ShapeStyle(primary_color=None, outline_thickness=0))


def effect(canvas, owner, frame=(0, 0, 120, 100)):
    modifier = DistortModifier(modifier_type="distort_displace", frame=frame)
    canvas.chapter.add_modifier(modifier, [("object", owner.object_id)])
    return modifier


def capture(canvas, modifier):
    encoded = capture_distort_beneath(canvas, modifier.modifier_id)
    image = QImage.fromData(base64.b64decode(encoded), "PNG")
    assert not image.isNull()
    return image


def test_sibling_order_excludes_owner_and_all_above(scene):
    canvas, page = scene
    ink(canvas, page, "red")
    target = ink(canvas, page, "lime", index=0)
    ink(canvas, page, "blue", index=0)
    result = capture(canvas, effect(canvas, target))
    assert result.pixelColor(40, 40) == QColor("red")


def test_nested_snapshot_includes_lower_siblings_at_each_ancestor(scene):
    canvas, page = scene
    low_group = group(canvas, page)
    ink(canvas, low_group, "red", center=(20, 30), width=20)
    middle = group(canvas, page, index=0)
    ink(canvas, middle, "lime", center=(55, 30), width=20)
    inner = group(canvas, middle, index=0)
    ink(canvas, inner, "blue", center=(90, 30), width=20)
    target = ink(canvas, inner, "black", center=(55, 30), width=100, index=0)
    ink(canvas, inner, "magenta", center=(90, 30), width=20, index=0)
    higher = group(canvas, page, index=0)
    ink(canvas, higher, "yellow", center=(20, 30), width=20)
    result = capture(canvas, effect(canvas, target))
    assert result.pixelColor(20, 30) == QColor("red")
    assert result.pixelColor(55, 30) == QColor("lime")
    assert result.pixelColor(90, 30) == QColor("blue")


def test_world_frame_respects_parent_transform_clip_and_opacity(scene):
    canvas, page = scene
    parent = group(canvas, page, bounds=(0, 0, 20, 20))
    parent.translate_x, parent.translate_y, parent.opacity = 30, 10, .5
    ink(canvas, parent, "red", center=(10, 10), width=100)
    target = ink(canvas, parent, "blue", center=(10, 10), width=100, index=0)
    result = capture(canvas, effect(canvas, target, frame=(20, 0, 50, 50)))
    assert result.size().toTuple() == (50, 50)
    assert result.pixelColor(20, 20).getRgb() == pytest.approx((255, 0, 0, 128), abs=1)
    assert result.pixelColor(5, 20).alpha() == 0
    assert result.pixelColor(35, 20).alpha() == 0
    assert result.pixelColor(20, 35).alpha() == 0


def test_hidden_lower_artwork_stays_hidden(scene):
    canvas, page = scene
    ink(canvas, page, "red")
    hidden = ink(canvas, page, "blue", index=0)
    hidden.visible = False
    target = ink(canvas, page, "lime", index=0)
    assert capture(canvas, effect(canvas, target)).pixelColor(40, 40) == QColor("red")


def test_promoted_artwork_above_ordinary_owner_is_not_included(scene):
    canvas, page = scene
    ink(canvas, page, "red")
    promoted = ink(canvas, page, "blue", index=0)
    promoted.show_on_top = True
    target = ink(canvas, page, "lime", index=0)
    assert capture(canvas, effect(canvas, target)).pixelColor(40, 40) == QColor("red")


def test_promoted_owner_includes_ordinary_artwork_above_it_in_tree(scene):
    canvas, page = scene
    target = ink(canvas, page, "lime")
    target.show_on_top = True
    ink(canvas, page, "blue", index=0)
    assert capture(canvas, effect(canvas, target)).pixelColor(40, 40) == QColor("blue")


def test_children_outside_parent_clip_use_their_actual_late_render_pass(scene):
    canvas, page = scene
    late = ink(canvas, page, "blue")
    late.ignore_parent_mask = True
    ink(canvas, page, "red", index=0)
    target = ink(canvas, page, "lime", index=0)
    # Despite appearing lower in the tree, clip-independent blue paints later.
    assert capture(canvas, effect(canvas, target)).pixelColor(40, 40) == QColor("red")


def test_capture_is_full_quality_and_preserves_document_solo_and_caches(scene, monkeypatch):
    canvas, page = scene
    lower = ink(canvas, page, "red", center=(40, 40), width=10)
    canvas.chapter.add_modifier(BlurModifier(strength=4), [("object", lower.object_id)])
    target = ink(canvas, page, "blue", index=0)
    modifier = effect(canvas, target)
    canvas.set_solo_entities({("object", target.object_id)})
    canvas.scale = .01
    canvas._interactive_render = True
    saved = copy.deepcopy(canvas.chapter)
    caches = (canvas._modifier_render_cache, canvas._modifier_source_cache,
              canvas._compound_path_cache, canvas._vector_render_cache)
    from comic_editor.ui.effect_jobs import EffectJobs
    monkeypatch.setattr(EffectJobs, "request", lambda *a, **k: pytest.fail("Capture scheduled an asynchronous draft"))
    result = capture(canvas, modifier)
    assert result.pixelColor(40, 40).red() == 255
    assert 0 < result.pixelColor(49, 40).alpha() < 255  # Full blurred fringe.
    assert canvas.chapter == saved
    assert canvas.solo_entities == {("object", target.object_id)}
    assert canvas.scale == .01 and canvas._interactive_render
    assert all(a is b for a, b in zip(caches, (canvas._modifier_render_cache,
        canvas._modifier_source_cache, canvas._compound_path_cache, canvas._vector_render_cache)))


def test_above_mask_contributor_remains_available_to_lower_artwork(scene):
    canvas, page = scene
    lower = ink(canvas, page, "red", width=80)
    target = ink(canvas, page, "blue", index=0)
    mask_obj = ink(canvas, page, "black", width=20, index=0)
    mask_obj.mask_only = True
    mask = ToneMask(contributors=[("object", mask_obj.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    lower.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    result = capture(canvas, effect(canvas, target))
    assert result.pixelColor(40, 40) == QColor("red")
    assert result.pixelColor(70, 40).alpha() == 0


def test_missing_modifier_or_owner_returns_empty(scene):
    canvas, _ = scene
    assert capture_distort_beneath(canvas, "missing") == ""
    modifier = DistortModifier(modifier_type="distort_displace")
    canvas.chapter.modifiers[modifier.modifier_id] = modifier
    assert capture_distort_beneath(canvas, modifier.modifier_id) == ""


def test_shared_modifier_uses_selected_owner_position(scene):
    canvas, page = scene
    ink(canvas, page, "red")
    lower_owner = ink(canvas, page, "lime", index=0)
    upper_owner = ink(canvas, page, "blue", index=0)
    modifier = effect(canvas, lower_owner)
    canvas.chapter.set_modifier_targets(modifier.modifier_id,
        [("object", lower_owner.object_id), ("object", upper_owner.object_id)])
    canvas.selected_kind, canvas.selected_id = "object", upper_owner.object_id
    result = capture(canvas, modifier)
    assert result.pixelColor(40, 40) == QColor("lime")
