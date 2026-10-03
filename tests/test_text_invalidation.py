"""Text edits preserve unrelated document tiles and exact affected pixels."""
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QKeyEvent, QTransform

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HalftoneModifier,
    MirrorModifier, OutlineModifier, TextObject, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def scene(qapp, text_outline_font_family):
    chapter = ChapterDocument(width=2048, height=512, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 2048, 512))
    page.fill_color, page.border_width = None, 0
    parent = chapter.add_layer(page.layer_id, "Bubble", BoundGeometry.rectangle(80, 70, 320, 210))
    parent.fill_color, parent.border_width = None, 0
    obj = chapter.add_object(parent.layer_id, TextObject(
        text="Before", font_family=text_outline_font_family, font_size=38,
        layout_mode="free", width=280, height=150, x=90, y=80,
        transform_quad=[(90, 80), (370, 80), (370, 230), (90, 230)],
    ))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False, snap_to_grid=False))
    canvas.resize(2048, 512)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 1024, 256, 1
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.TEXT_EDIT)
    # Tests use the synchronous exact compositor, independent of workers.
    canvas._projection_async_enabled = False
    yield canvas, obj, parent
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


def frame(canvas, fresh=False):
    if fresh:
        canvas._document_projection.clear()
    canvas._invalidate_scene_cache(projection=False)
    canvas._ensure_scene_cache()
    return canvas._scene_cache.copy()


@pytest.mark.parametrize("layout", ["free", "strict"])
@pytest.mark.parametrize("effect", ["plain", "outline", "ancestor_blur"])
def test_typing_local_undo_and_commit_keep_distant_tiles_and_match_fresh(scene, layout, effect):
    canvas, obj, parent = scene
    obj.layout_mode = layout
    parent.transform_frame = (80, 70, 320, 210)
    transform = QTransform().translate(240, 175).rotate(12).translate(-240, -175)
    parent.transform_quad = [transform.map(QPointF(*point)).toTuple()
                             for point in canvas._rect_quad(QRectF(*parent.transform_frame))]
    if effect == "outline":
        canvas.chapter.add_modifier(OutlineModifier(thickness=9), [("object", obj.object_id)])
    elif effect == "ancestor_blur":
        canvas.chapter.add_modifier(BlurModifier(strength=5), [("layer", parent.layer_id)])
    before = frame(canvas)
    distant = {key: tile for key, tile in canvas._document_projection.tiles.items()
               if tile.request.world_rect.left() >= 1280}
    assert distant and all(tile.valid for tile in distant.values())
    hierarchy = []
    canvas.hierarchyChanged.connect(lambda: hierarchy.append(True))
    canvas.start_text_edit(select_all=True)
    assert all(tile.valid for tile in distant.values()), "Caret/selection entry invalidated artwork"
    assert frame(canvas) == before
    canvas._replace_text_selection("After\nMore text")
    assert all(tile.valid for tile in distant.values())
    after = frame(canvas)
    assert after != before
    assert after == frame(canvas, fresh=True)
    canvas._handle_text_key(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Z, Qt.ControlModifier))
    assert frame(canvas) == before
    canvas._replace_text_selection("Committed")
    canvas.commit_active_text_edit()
    committed = frame(canvas)
    assert committed == frame(canvas, fresh=True)
    assert not hierarchy, "Typing changes no hierarchy structure"
    canvas.command_stack.undo()
    assert frame(canvas) == before
    canvas.command_stack.redo()
    assert frame(canvas) == committed


@pytest.mark.parametrize("dependency", ["object_mask", "ancestor_mask", "halftone", "mirror"])
def test_nonlocal_text_dependencies_use_conservative_document_dirty(scene, dependency):
    canvas, obj, parent = scene
    if dependency.endswith("mask"):
        mask = ToneMask(contributors=[("object", obj.object_id)] if dependency == "object_mask"
                        else [("layer", parent.layer_id)])
        canvas.chapter.masks[mask.mask_id] = mask
    else:
        modifier = (HalftoneModifier() if dependency == "halftone"
                    else MirrorModifier(axis_start=(800, 0), axis_end=(800, 512)))
        canvas.chapter.add_modifier(modifier, [("layer", parent.layer_id)])
    dirty = canvas._text_visual_dirty(obj)
    assert dirty.isEmpty(), "Nonlocal dependencies need full invalidation, including overflow"
    # The empty signal must actually retire remote retained tiles, rather
    # than merely describing the edited box's source rectangle.
    frame(canvas)
    distant = [tile for tile in canvas._document_projection.tiles.values()
               if tile.request.world_rect.left() >= 1280]
    assert distant and all(tile.valid for tile in distant)
    canvas._replace_text_selection("Changed")
    assert all(not tile.valid for tile in distant)


def test_layout_property_change_covers_both_frames_and_exact_pixels(scene):
    canvas, obj, parent = scene
    before = canvas.chapter.to_dict()
    old_rect = canvas.object_world_rect(obj.object_id)
    original = frame(canvas)
    obj.layout_mode = "strict"
    obj.margin = 48
    changed = []
    canvas.documentChanged.connect(lambda rect: changed.append(QRectF(rect)))
    canvas._finish_text_property_change(before, "Change layout")
    assert changed[-1].contains(old_rect)
    assert changed[-1].contains(canvas.object_world_rect(obj.object_id))
    assert frame(canvas) != original
    assert frame(canvas) == frame(canvas, fresh=True)


def test_ribbon_size_change_preserves_distant_tiles_and_exact_output(scene):
    from comic_editor.ui.tool_ribbon_pages import TextObjectControls
    canvas, obj, _parent = scene
    controls = TextObjectControls(canvas, canvas.settings)
    try:
        original = frame(canvas)
        distant = [tile for tile in canvas._document_projection.tiles.values()
                   if tile.request.world_rect.left() >= 1280]
        controls.font_size.setValue(48)
        controls.font_size.editingFinished.emit()
        assert obj.font_size == 48
        assert distant and all(tile.valid for tile in distant)
        updated = frame(canvas)
        assert updated != original
        assert updated == frame(canvas, fresh=True)
    finally:
        for widget in (controls.object_widget, controls.typography_widget, controls.layout_widget):
            widget.deleteLater()
        controls.deleteLater()


def test_bounds_resize_keeps_distant_tiles_and_repaints_both_frames(scene):
    canvas, obj, _parent = scene
    before = frame(canvas)
    distant = [tile for tile in canvas._document_projection.tiles.values()
               if tile.request.world_rect.left() >= 1280]
    old_rect = canvas.object_world_rect(obj.object_id)
    corner = QPointF(*canvas.object_world_quad(obj.object_id)[2])
    assert canvas._begin_free_text_transform(corner)
    changes = []
    canvas.documentChanged.connect(lambda rect: changes.append(QRectF(rect)))
    canvas._queue_free_text_drag(corner + QPointF(90, 40))
    canvas._flush_free_text_drag()
    assert (obj.width, obj.height) == (370, 190)
    assert changes[-1].contains(old_rect)
    assert changes[-1].contains(canvas.object_world_rect(obj.object_id))
    assert all(tile.valid for tile in distant)
    after = frame(canvas)
    assert after != before and after == frame(canvas, fresh=True)
    canvas._finish_free_text_drag()
    canvas.command_stack.undo()
    assert frame(canvas) == before


def test_full_invalidation_survives_resize_union(scene):
    canvas, _obj, _parent = scene
    region = QRectF(0, 0, 100, 100)
    assert canvas._text_dirty_union(QRectF(), region).isEmpty()
    assert canvas._text_dirty_union(region, QRectF()).isEmpty()
