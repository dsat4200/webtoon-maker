import pytest
from comic_editor.core.document_patch import DocumentPatch, RecordSnapshot
from comic_editor.core.models import ChapterDocument, RasterObject, BlurModifier, HueSaturationLightnessModifier, ToneMask
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls


def document():
    chapter = ChapterDocument()
    page = chapter.add_page()
    obj = chapter.add_object(page.layer_id, RasterObject())
    modifier = BlurModifier()
    chapter.add_modifier(modifier, [('object', obj.object_id)])
    return chapter, page, obj, modifier


def test_serialized_patch_round_trips_structure_masks_and_scalars():
    chapter, page, obj, modifier = document()
    before = chapter.to_dict()
    chapter.name = 'Changed'
    chapter.export_rect = (1,2,300,400)
    chapter.export_rect_enabled = True
    layer = chapter.add_layer(page.layer_id, 'Child')
    mask = ToneMask(name='Saved', saved=True)
    chapter.masks[mask.mask_id] = mask
    modifier.strength = 18
    after = chapter.to_dict()
    old, new = DocumentPatch.pair(before, after)
    assert set(old.records) == {'layers', 'masks', 'modifiers'}
    old.apply(chapter)
    assert chapter.to_dict() == before
    assert chapter.objects[obj.object_id] is obj
    assert chapter.layers[page.layer_id] is page
    new.apply(chapter)
    assert chapter.to_dict() == after
    assert chapter.objects[obj.object_id] is obj


def test_parameter_snapshot_never_serializes_unrelated_records(monkeypatch):
    chapter, page, obj, modifier = document()
    def forbidden():
        pytest.fail('Unrelated drawing must not be serialized for an effect edit')
    monkeypatch.setattr(obj, 'to_dict', forbidden)
    before = RecordSnapshot.capture(chapter, modifiers=[modifier.modifier_id])
    modifier.strength = 12
    old, new = DocumentPatch.pair(before, before.after(chapter))
    old.apply(chapter)
    assert modifier.strength == 8
    new.apply(chapter)
    assert modifier.strength == 12
    assert chapter.objects[obj.object_id] is obj


def test_partial_stack_patch_retains_drawing_payloads_and_collection_order(monkeypatch):
    chapter, page, obj, modifier = document()
    second = HueSaturationLightnessModifier()
    chapter.add_modifier(second, [('object',obj.object_id)])
    before = RecordSnapshot.capture(chapter, objects=[obj.object_id],
        attributes={'objects': ('modifier_ids',)}, modifiers=[modifier.modifier_id])
    obj.modifier_ids.remove(modifier.modifier_id)
    chapter.modifiers.pop(modifier.modifier_id)
    after = before.after(chapter)
    old, new = DocumentPatch.pair(before, after)
    old.apply(chapter)
    assert obj.modifier_ids == [modifier.modifier_id,second.modifier_id]
    assert tuple(chapter.modifiers) == (modifier.modifier_id,second.modifier_id)
    new.apply(chapter)
    assert obj.modifier_ids == [second.modifier_id]
    assert chapter.objects[obj.object_id] is obj


def test_control_parameter_history_uses_focused_snapshot_and_abandons_undo_gesture(qapp, monkeypatch):
    chapter, page, obj, modifier = document()
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection('object',obj.object_id)
    controls = ModifierControls(canvas)
    controls.refresh()
    def forbidden():
        pytest.fail('Slider edits must not snapshot a complete document')
    monkeypatch.setattr(chapter, 'to_dict', forbidden)
    controls.set_parameter(modifier.modifier_id, 'strength', 14, True)
    assert canvas.command_stack.can_undo
    controls.begin_parameter_drag(modifier.modifier_id)
    controls.set_parameter(modifier.modifier_id, 'strength', 20, False)
    canvas.command_stack.undo()
    revision = canvas.command_stack.revision
    controls.finish_parameter_drag()
    assert canvas.command_stack.revision == revision
    assert controls._parameter_before is None
    assert modifier.strength == 8
    canvas.command_stack.redo()
    assert modifier.strength == 14
    controls.deleteLater()
    canvas.deleteLater()
