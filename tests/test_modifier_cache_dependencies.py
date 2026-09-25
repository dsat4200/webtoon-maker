"""Cache reuse must still follow linked effects, source pixels and masks."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (BoundGeometry, ChapterDocument,
    HueSaturationLightnessModifier, ImageObject, ParameterMaskBinding,
    RasterObject, ToneMask)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui import interactive_effects


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=360, height=120, document_kind="asset", background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 360, 120))
    page.fill_color, page.border_width = None, 0
    images = ImageStore()
    objects = []
    for x in (0, 120, 240):
        obj = chapter.add_object(page.layer_id, ImageObject(x=x, pixel_width=100, pixel_height=100))
        image = QImage(100, 100, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(150, 70, 25))
        images.put_decoded(obj.object_id, "source.png", b"", image)
        objects.append(obj)
    linked = HueSaturationLightnessModifier(hue=20)
    chapter.add_modifier(linked, [("object", obj.object_id) for obj in objects[:2]])
    chapter.add_modifier(HueSaturationLightnessModifier(hue=40), [("object", objects[2].object_id)])
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), images)
    yield canvas, objects, linked, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    image = QImage(360, 120, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return np.frombuffer(image.constBits(), np.uint8).reshape(120, 360, 4).copy()


def count_work(canvas, monkeypatch):
    captures, effects = [], []
    original_capture = canvas._render_object_content
    original_effect = interactive_effects.apply_modifier_stack

    def capture(painter, obj, *args):
        captures.append(obj.object_id)
        return original_capture(painter, obj, *args)

    def effect(image, modifiers, *args, **kwargs):
        effects.extend(mod.modifier_id for mod in modifiers)
        return original_effect(image, modifiers, *args, **kwargs)

    monkeypatch.setattr(canvas, "_render_object_content", capture)
    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", effect)
    return captures, effects


def test_selecting_linked_target_keeps_pixels_but_edit_updates_all_linked_targets(scene, monkeypatch):
    canvas, objects, linked, _ = scene
    before = render(canvas)
    captures, effects = count_work(canvas, monkeypatch)
    canvas.set_selection("object", objects[1].object_id)
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    np.testing.assert_array_equal(render(canvas), before)
    assert not captures and not effects
    linked.hue = 110
    canvas.documentChanged.emit(None)
    after = render(canvas)
    for left in (0, 120):
        assert not np.array_equal(after[:, left:left+100], before[:, left:left+100])
    np.testing.assert_array_equal(after[:, 240:], before[:, 240:])
    assert effects == [linked.modifier_id, linked.modifier_id]
    assert objects[2].object_id not in captures


@pytest.mark.parametrize("dependency", ["source_pixels", "mask_paint", "mask_contributor", "opacity_mask"])
def test_actual_dependency_change_refreshes_only_dependent_artwork(scene, monkeypatch, dependency):
    canvas, objects, linked, page = scene
    first = objects[0]
    mask = None
    contributor = None
    if dependency != "source_pixels":
        mask = ToneMask()
        canvas.chapter.masks[mask.mask_id] = mask
        binding = ParameterMaskBinding(mask_id=mask.mask_id, black_value=0, white_value=100)
        if dependency == "opacity_mask":
            first.opacity_mask = ParameterMaskBinding(mask_id=mask.mask_id, black_value=.2, white_value=1)
        else:
            linked.parameter_masks["intensity"] = binding
        if dependency == "mask_contributor":
            contributor = canvas.chapter.add_object(page.layer_id, RasterObject(mask_only=True,
                interaction_rect=(0, 0, 100, 100)))
            mask.contributors = [("object", contributor.object_id)]
    before = render(canvas)
    captures, effects = count_work(canvas, monkeypatch)
    if dependency == "source_pixels":
        replacement = QImage(100, 100, QImage.Format_ARGB32_Premultiplied)
        replacement.fill(QColor(45, 110, 170))
        canvas.images.put_decoded(first.object_id, "replacement.png", b"", replacement)
    else:
        owner = contributor.object_id if contributor else mask.mask_id
        canvas.tiles.paint_dab(owner, QPointF(50, 50), 100, QColor("white"))
    canvas.documentChanged.emit(None)
    after = render(canvas)
    assert not np.array_equal(after[:, :100], before[:, :100])
    np.testing.assert_array_equal(after[:, 240:], before[:, 240:])
    assert objects[2].object_id not in captures
    assert effects and set(effects) == {linked.modifier_id}


def test_selecting_mask_contributor_and_renaming_records_does_not_recompute_effects(scene, monkeypatch):
    canvas, objects, linked, page = scene
    contributor = canvas.chapter.add_object(page.layer_id, RasterObject(mask_only=True))
    canvas.tiles.paint_dab(contributor.object_id, QPointF(50, 50), 100, QColor("white"))
    mask = ToneMask(contributors=[("object", contributor.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    linked.parameter_masks["intensity"] = ParameterMaskBinding(mask_id=mask.mask_id, black_value=0, white_value=100)
    before = render(canvas)
    captures, effects = count_work(canvas, monkeypatch)
    # This selection updates page.last_raster_id and mask-only visibility on the
    # canvas, neither of which changes an independent contributor capture.
    canvas.set_selection("object", contributor.object_id)
    contributor.name, page.name, objects[0].name = "Mask source", "Renamed page", "Reference"
    linked.name, linked.expanded = "Renamed adjustment", False
    canvas._vector_eraser_preview_revision += 1
    np.testing.assert_array_equal(render(canvas), before)
    assert not effects
    assert all(obj.object_id not in captures for obj in objects)


def test_excluding_stroke_target_only_invalidates_its_own_modified_ancestors(scene, monkeypatch):
    canvas, objects, _, page = scene
    objects[0].visible = False
    panel = canvas.chapter.add_layer(page.layer_id, "Independent effect", BoundGeometry.rectangle(0, 0, 80, 80))
    panel.fill_color, panel.border_width = "#bb5533", 0
    child = canvas.chapter.add_object(panel.layer_id, RasterObject(interaction_rect=(0, 0, 80, 80)))
    canvas.tiles.paint_dab(child.object_id, QPointF(40, 40), 60, QColor("#2277cc"))
    modifier = HueSaturationLightnessModifier(hue=90)
    canvas.chapter.add_modifier(modifier, [("layer", panel.layer_id)])
    before = render(canvas)
    _, effects = count_work(canvas, monkeypatch)
    canvas._render_excluded_object_id = objects[2].object_id
    outside = render(canvas)
    np.testing.assert_array_equal(outside[:, :80], before[:, :80])
    assert modifier.modifier_id not in effects
    canvas._render_excluded_object_id = child.object_id
    inside = render(canvas)
    assert not np.array_equal(inside[:, :80], before[:, :80])
    assert modifier.modifier_id in effects
