"""Multiple raster selections share a world transform, not local tile frames."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainterPath

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, HueSaturationLightnessModifier,
    MirrorModifier, OutlineModifier, ParameterMaskBinding, RasterObject, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def preview_scene(qapp):
    chapter = ChapterDocument(height=700)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 700))
    first = chapter.add_layer(page.layer_id, "First", BoundGeometry.rectangle(0, 0, 700, 600))
    second = chapter.add_layer(page.layer_id, "Second", BoundGeometry.rectangle(0, 0, 700, 600))
    second.translate_x, second.translate_y = 100, 90
    for layer in (page, first, second):
        layer.fill_color, layer.border_width = None, 0
    a = chapter.add_object(first.layer_id, RasterObject())
    b = chapter.add_object(second.layer_id, RasterObject(x=20, y=30))
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.resize(480, 480)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 240, 240, 1
    for obj, color in ((a, "red"), (b, "blue")):
        canvas.tiles.paint_dab(obj.object_id, QPointF(50, 70), 16, QColor(color), antialias=False)
        canvas.tiles.paint_dab(obj.object_id, QPointF(350, 350), 16, QColor(color), antialias=False)
    canvas.set_selection("object", a.object_id)
    canvas._selection_raster_states = {}
    source = QPainterPath()
    source.addRect(QRectF(20, 40, 220, 180))
    for obj in (a, b):
        inverse, valid = canvas._drawing_local_to_world_transform(obj).inverted()
        assert valid
        canvas._selection_raster_states[obj.object_id] = {
            "before_tiles": canvas.tiles.object_tiles(obj.object_id),
            "source_path": inverse.map(source),
            "overlay_tiles": None,
        }
    canvas._drawing_selection_path = QPainterPath(source)
    canvas._selection_before_tiles = canvas.tiles.object_tiles(a.object_id)
    canvas._selection_transform_start_quad = canvas._rect_quad(source.boundingRect())
    canvas._selection_transform_quad = [(x + 130, y + 60)
        for x, y in canvas._selection_transform_start_quad]
    yield canvas, chapter, a, b
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    image = QImage(1080, 700, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def test_each_raster_moves_its_local_selection_and_keeps_unselected_pixels(preview_scene):
    canvas, _chapter, a, b = preview_scene
    image = render(canvas)
    assert image.pixelColor(180, 130) == QColor("red")
    assert image.pixelColor(300, 250) == QColor("blue")
    assert image.pixelColor(50, 70).alpha() == 0
    assert image.pixelColor(170, 190).alpha() == 0
    assert image.pixelColor(350, 350) == QColor("red")
    assert image.pixelColor(470, 470) == QColor("blue")
    assert canvas.tiles.tile(a.object_id, (0, 0)).pixelColor(50, 70) == QColor("red")
    assert canvas.tiles.tile(b.object_id, (0, 0)).pixelColor(50, 70) == QColor("blue")


def test_each_copy_overlay_preserves_its_background_and_empty_overlay_stays_empty(preview_scene):
    canvas, _chapter, a, b = preview_scene
    for state in canvas._selection_raster_states.values():
        state["overlay_tiles"] = state["before_tiles"]
    copied = render(canvas)
    assert copied.pixelColor(50, 70) == QColor("red")
    assert copied.pixelColor(170, 190) == QColor("blue")
    assert copied.pixelColor(180, 130) == QColor("red")
    assert copied.pixelColor(300, 250) == QColor("blue")
    canvas._selection_raster_states[b.object_id]["overlay_tiles"] = {}
    blank = render(canvas)
    assert blank.pixelColor(170, 190) == QColor("blue")
    assert blank.pixelColor(300, 250).alpha() == 0


def test_nonanchor_modified_raster_invalidates_effect_source_while_moving(preview_scene):
    canvas, chapter, _a, b = preview_scene
    chapter.add_modifier(OutlineModifier(thickness=5), [("object", b.object_id)])
    first_signature = canvas._modifier_object_signature(b)
    first_parent_signature = canvas._modifier_layer_signature(b.parent_layer_id)
    first = render(canvas)
    assert first.pixelColor(300, 250) == QColor("blue")
    assert first.pixelColor(310, 250).alpha() > 0
    canvas._selection_transform_quad = [(x + 40, y)
        for x, y in canvas._selection_transform_quad]
    assert first_signature != canvas._modifier_object_signature(b)
    assert first_parent_signature != canvas._modifier_layer_signature(b.parent_layer_id)
    moved = render(canvas)
    assert moved.pixelColor(340, 250) == QColor("blue")
    assert moved.pixelColor(300, 250).alpha() == 0


def test_viewport_retains_all_active_rasters_and_their_ancestors(preview_scene):
    canvas, _chapter, a, b = preview_scene
    canvas._render_bounds.prepare()
    for obj in (a, b):
        assert ("object", obj.object_id) in canvas._render_bounds.live_branches
        assert ("layer", obj.parent_layer_id) in canvas._render_bounds.live_branches
    canvas._interactive_render = True
    try:
        assert canvas._render_bounds.usable()
        assert canvas._render_bounds.object_visible(b, QRectF(900, 900, 20, 20))
    finally:
        canvas._interactive_render = False
    canvas._ensure_scene_cache()
    assert canvas._scene_cache.pixelColor(180, 130) == QColor("red")
    assert canvas._scene_cache.pixelColor(300, 250) == QColor("blue")


def test_multi_raster_selection_preview_culls_unrelated_drawings(preview_scene, monkeypatch):
    canvas, chapter, _a, _b = preview_scene
    distant = chapter.add_object(chapter.root_page_ids[0], RasterObject(x=850, y=600))
    canvas.tiles.paint_dab(distant.object_id, QPointF(10, 10), 20, QColor("green"))
    original = canvas._render_raster_content
    calls = []
    monkeypatch.setattr(canvas, "_render_raster_content", lambda painter, obj, *args, **kwargs:
                        (calls.append(obj.object_id), original(painter, obj, *args, **kwargs))[1])
    canvas._ensure_scene_cache()
    assert distant.object_id not in calls
    culled = QImage(canvas._scene_cache)
    prepare = canvas._render_bounds.prepare
    def disabled():
        prepare()
        canvas._render_bounds.enabled = False
    monkeypatch.setattr(canvas._render_bounds, "prepare", disabled)
    canvas._invalidate_scene_cache()
    canvas._ensure_scene_cache()
    assert distant.object_id in calls
    assert culled == canvas._scene_cache


@pytest.mark.parametrize("scope,modifier", [
    ("object", MirrorModifier(axis_start=(650, 0), axis_end=(650, 700))),
    ("layer", MirrorModifier(axis_start=(650, 0), axis_end=(650, 700))),
    ("layer", OutlineModifier(thickness=6)),
])
def test_effect_capture_includes_selection_moved_beyond_its_old_frame(preview_scene, scope, modifier):
    canvas, chapter, _a, b = preview_scene
    parent = chapter.layers[b.parent_layer_id]
    parent.bound = BoundGeometry.rectangle(0, 0, 220, 220)
    b.ignore_parent_mask = True
    canvas._selection_transform_quad = [(x + 400, y)
        for x, y in canvas._selection_transform_start_quad]
    identifier = b.object_id if scope == "object" else parent.layer_id
    chapter.add_modifier(modifier, [(scope, identifier)])
    image = render(canvas)
    assert image.pixelColor(570, 190) == QColor("blue")
    if isinstance(modifier, MirrorModifier):
        assert image.pixelColor(730, 190) == QColor("blue")
    else:
        assert image.pixelColor(580, 190).alpha() > 0


def test_nonanchor_raster_mask_updates_dependent_effect_before_commit(preview_scene):
    canvas, chapter, _a, contributor = preview_scene
    layer = chapter.add_layer(chapter.root_page_ids[0], "Masked color",
        BoundGeometry.rectangle(0, 0, 1080, 700), index=0)
    layer.fill_color, layer.border_width = None, 0
    target = chapter.add_object(layer.layer_id, RasterObject(interaction_rect=(0, 0, 1080, 700)))
    for x in (300, 340):
        canvas.tiles.paint_dab(target.object_id, QPointF(x, 250), 20,
                               QColor("red"), antialias=False)
    mask = ToneMask(contributors=[("object", contributor.object_id)])
    chapter.masks[mask.mask_id] = mask
    modifier = HueSaturationLightnessModifier(hue=120)
    modifier.parameter_masks["hue"] = ParameterMaskBinding(mask.mask_id, 0, 120)
    chapter.add_modifier(modifier, [("object", target.object_id)])
    before_tiles = canvas.tiles.object_tiles(contributor.object_id)

    initial = render(canvas)
    assert initial.pixelColor(300, 250).green() > 245
    assert initial.pixelColor(340, 250) == QColor("red")
    canvas._selection_transform_quad = [(x + 40, y)
        for x, y in canvas._selection_transform_quad]
    # This is the invalidation performed by a live selection drag. Dependent
    # modifier results must also recognize the uncommitted mask movement.
    canvas._tone_mask_contributor_cache.clear()
    moved = render(canvas)

    assert moved.pixelColor(300, 250) == QColor("red")
    assert moved.pixelColor(340, 250).green() > 245
    assert canvas.tiles.object_tiles(contributor.object_id) == before_tiles
