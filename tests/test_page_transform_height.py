"""Saved page quads remain part of chapter growth and trimming safety."""
import math

import pytest

from comic_editor.core.assets import entity_visual_bounds
from comic_editor.core.models import BoundGeometry, ChapterDocument, GROWTH_MARGIN
from comic_editor.core.tiles import TileStore
from test_page_warp_transform import page_scene, pixels, start_move


@pytest.mark.parametrize("quad", [
    [(20, 330), (220, 330), (220, 430), (20, 430)],
    [(300, 200), (300, 600), (250, 600), (250, 200)],
    [(20, 330), (220, 350), (250, 450), (10, 410)],
    [(20, -230), (220, -230), (220, -130), (20, -130)],
])
def test_transformed_page_height_matches_world_corners_and_survives_roundtrip(quad):
    chapter = ChapterDocument(height=1000)
    page = chapter.add_page(bound=BoundGeometry.rectangle(20, 30, 200, 100))
    page.transform_frame = (20, 30, 200, 100)
    page.transform_quad = quad
    page.translate_y = 999  # A stored quad supersedes the translation fallback.
    expected = max(1, math.ceil(max(point[1] for point in quad)))
    assert chapter.minimum_safe_height() == expected
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.minimum_safe_height() == expected
    with pytest.raises(ValueError, match="shorter"):
        restored.trim_height(expected - 1)
    restored.trim_height(expected)
    assert restored.height == expected
    chapter.height = 100
    changed = chapter.ensure_height_for(page.layer_id)
    assert changed == (expected > 100)
    assert chapter.height == (expected + GROWTH_MARGIN if changed else 100)


def test_page_without_quad_keeps_translation_height_semantics():
    chapter = ChapterDocument(height=1000)
    page = chapter.add_page(bound=BoundGeometry.rectangle(20, 30, 200, 100))
    page.translate_y = 300
    assert chapter.minimum_safe_height() == 430
    chapter.height = 100
    assert chapter.ensure_height_for(page.layer_id)
    assert chapter.height == 430 + GROWTH_MARGIN


def test_growth_maps_nested_bounds_through_stored_ancestor_transform():
    chapter = ChapterDocument(height=1000)
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 200, 100))
    page.transform_frame = (0, 0, 200, 100)
    page.transform_quad = [(300, 200), (300, 600), (250, 600), (250, 200)]
    group = chapter.add_layer(page.layer_id, "Nested", BoundGeometry.rectangle(40, 20, 80, 50))
    group.translate_x, group.translate_y = 20, -10
    expected = math.ceil(entity_visual_bounds(chapter, TileStore(), "layer", group.layer_id).bottom())
    chapter.height = 100
    assert chapter.ensure_height_for(group.layer_id)
    assert chapter.height == expected + GROWTH_MARGIN


def test_image_document_does_not_auto_grow_for_transformed_page():
    chapter = ChapterDocument(height=100, document_kind="image")
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 100, 100))
    page.transform_frame = (0, 0, 100, 100)
    page.transform_quad = [(0, 200), (100, 200), (100, 300), (0, 300)]
    assert not chapter.ensure_height_for(page.layer_id)
    assert chapter.height == 100


def test_singular_saved_quad_uses_renderer_identity_fallback():
    chapter = ChapterDocument(height=1000)
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 100, 100))
    page.transform_frame = (0, 0, 100, 100)
    page.transform_quad = [(0, 0), (100, 0), (100, 0), (0, 100)]
    page.translate_y = 999
    chapter.validate()
    assert chapter.minimum_safe_height() == 100


@pytest.mark.parametrize("stored_quad", [False, True])
def test_adding_page_above_negative_page_preserves_warp_alignment(page_scene, stored_quad):
    import numpy as np
    from PySide6.QtGui import QTransform

    canvas, page, warp = page_scene
    original = pixels(canvas)
    if stored_quad:
        start_move(canvas, (0, -100))
        canvas._commit_geometry_transform()
    else:
        page.translate_y = -100
        canvas._transform_single_target_focal_modifiers(
            "layer", page.layer_id, QTransform.fromTranslate(0, -100))
    canvas.chapter.validate()
    assert canvas.page_world_bounds(page.layer_id).top() == -80
    np.testing.assert_array_equal(pixels(canvas, (0, -100)), original)
    other = canvas.chapter.add_page("Added page", BoundGeometry.rectangle(20, 300, 260, 80))
    other.fill_color, other.border_width = None, 0
    canvas._ensure_page_height_safety()
    canvas.chapter.validate()
    assert canvas.page_world_bounds(page.layer_id).top() == 120
    assert canvas.page_world_bounds(other.layer_id).top() == 500
    assert canvas.chapter.height >= 580
    assert canvas.chapter.modifiers[warp.modifier_id].frame[:2] == (70, 170)
    np.testing.assert_array_equal(pixels(canvas, (0, 100)), original)
    assert (page.transform_quad is not None) == stored_quad
