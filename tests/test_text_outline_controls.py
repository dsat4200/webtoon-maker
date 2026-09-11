"""Outline eligibility and editing preserve editable text and its modifier links."""
from __future__ import annotations

import pytest

from comic_editor.core.assets import extract_asset, instantiate_asset
from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HueSaturationLightnessModifier,
    OutlineModifier, RasterObject, TextObject,
)
from comic_editor.core.modifier_presets import preset_from_modifier
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_presets import ModifierPresetController


def text_document(layout_mode="free"):
    chapter = ChapterDocument(width=400, height=300, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 400, 300))
    text = chapter.add_object(page.layer_id, TextObject(
        text="Editable outline", layout_mode=layout_mode,
        x=30, y=30, width=280, height=150,
    ))
    return chapter, page, text


@pytest.fixture
def editor(qapp):
    chapter, _, text = text_document()
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", text.object_id)
    controls = ModifierControls(canvas)
    controls.refresh()
    yield canvas, controls, text.object_id
    canvas._effect_jobs.cancel()
    controls.deleteLater()
    canvas.deleteLater()


@pytest.mark.parametrize("layout_mode", ["strict", "free"])
def test_outline_on_text_survives_validation_round_trip_and_sharing(layout_mode):
    chapter, page, text = text_document(layout_mode)
    container = chapter.add_layer(page.layer_id, "Text group", layer_kind="text_container")
    other = chapter.add_object(container.layer_id, TextObject(layout_mode="free"))
    outline = OutlineModifier(thickness=7, color="#FFFF0066")
    refs = [("object", text.object_id), ("layer", container.layer_id), ("object", other.object_id)]
    chapter.add_modifier(outline, refs)
    assert chapter.modifier_target("object", text.object_id) is text
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert set(restored.modifier_target_ids(outline.modifier_id)) == set(refs)
    assert restored.objects[text.object_id].text == "Editable outline"
    assert restored.objects[text.object_id].layout_mode == layout_mode
    assert restored.modifiers[outline.modifier_id].thickness == 7


@pytest.mark.parametrize("modifier", [BlurModifier(), HueSaturationLightnessModifier()])
def test_direct_text_rejects_other_modifiers_and_sanitizes_old_invalid_links(modifier):
    chapter, page, text = text_document()
    ref = ("object", text.object_id)
    with pytest.raises(ValueError, match="Text boxes support Outline"):
        chapter.add_modifier(modifier, [ref])
    raster = chapter.add_object(page.layer_id, RasterObject())
    chapter.add_modifier(modifier, [("object", raster.object_id)])
    outline = OutlineModifier()
    chapter.add_modifier(outline, [ref])
    text.modifier_ids.append(modifier.modifier_id)
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.objects[text.object_id].modifier_ids == [outline.modifier_id]
    assert restored.objects[raster.object_id].modifier_ids == [modifier.modifier_id]


def test_text_outline_controls_add_edit_link_remove_and_undo(editor):
    canvas, controls, text_id = editor
    assert [action.text() for action in controls.add_button.menu().actions() if action.isVisible()] == ["Outline"]
    controls.add_modifier("outline")
    modifier_id = controls.common_ids()[0]
    assert canvas.chapter.objects[text_id].modifier_ids == [modifier_id]
    assert not hasattr(controls._cards[modifier_id], "apply_button")
    canvas.command_stack.undo()
    assert canvas.chapter.objects[text_id].modifier_ids == []
    canvas.command_stack.redo()
    assert controls.common_ids() == [modifier_id]

    before_drag = canvas.command_stack.revision
    controls.begin_parameter_drag()
    for thickness in (3, 8, 12):
        controls.set_parameter(modifier_id, "thickness", thickness, False)
    assert canvas.command_stack.revision == before_drag
    controls.finish_parameter_drag()
    assert canvas.command_stack.revision == before_drag + 1
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier_id].thickness == OutlineModifier().thickness
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[modifier_id].thickness == 12

    page_id = canvas.chapter.root_page_ids[0]
    other = canvas.chapter.add_object(page_id, TextObject(text="Linked", layout_mode="free"))
    controls.toggle_link_mode(modifier_id)
    assert controls.toggle_link_target("object", other.object_id)
    controls.commit_link_mode()
    assert set(canvas.chapter.modifier_target_ids(modifier_id)) == {
        ("object", text_id), ("object", other.object_id),
    }
    canvas.command_stack.undo()
    assert canvas.chapter.modifier_target_ids(modifier_id) == [("object", text_id)]
    canvas.command_stack.redo()
    controls.remove_modifier(modifier_id)
    assert modifier_id not in canvas.chapter.modifiers
    canvas.command_stack.undo()
    assert canvas.chapter.objects[text_id].modifier_ids == [modifier_id]
    assert canvas.chapter.objects[other.object_id].modifier_ids == [modifier_id]


def test_loading_text_outline_preset_retains_text_and_is_undoable(editor, tmp_path):
    canvas, controls, text_id = editor
    repository = SeriesRepository(tmp_path / "Outline series")
    series = repository.create("Outline series")
    controller = ModifierPresetController(controls, lambda: (series, repository))
    controls.add_modifier("outline")
    modifier_id = controls.common_ids()[0]
    preset = preset_from_modifier("Blue caption", OutlineModifier(thickness=11, color="#FF0022FF", opacity=64))
    series.modifier_presets.append(preset)
    before = canvas.chapter.to_dict()
    assert controller.load(modifier_id, preset.preset_id)
    loaded = canvas.chapter.modifiers[modifier_id]
    assert (loaded.thickness, loaded.color, loaded.opacity) == (11, "#FF0022FF", 64)
    assert canvas.chapter.objects[text_id].modifier_ids == [modifier_id]
    assert canvas.chapter.objects[text_id].text == "Editable outline"
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    canvas.command_stack.redo()
    restored = ChapterDocument.from_dict(canvas.chapter.to_dict())
    assert restored.objects[text_id].modifier_ids == [modifier_id]
    assert restored.modifier_preset_ids[modifier_id] == preset.preset_id


def test_text_outline_asset_round_trip_preserves_editable_text_and_remaps_modifier():
    chapter, _, text = text_document()
    outline = OutlineModifier(thickness=9)
    chapter.add_modifier(outline, [("object", text.object_id)])
    manifest, tiles = extract_asset(chapter, TileStore(), "object", text.object_id, "Caption")
    asset_outline_id = manifest.document.objects[text.object_id].modifier_ids[0]
    target, page, _ = text_document()
    kind, clone_id, _ = instantiate_asset(manifest, tiles, target, TileStore(), page.layer_id, 200, 150)
    clone = target.objects[clone_id]
    assert kind == "object" and isinstance(clone, TextObject)
    assert clone.text == text.text
    assert len(clone.modifier_ids) == 1
    assert clone.modifier_ids[0] not in {outline.modifier_id, asset_outline_id}
    assert target.modifiers[clone.modifier_ids[0]].thickness == 9
