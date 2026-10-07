from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor

from comic_editor.core.changes import ChangeSet, DependencyIndex, EntityChange, ResourceChange
from comic_editor.core.commands import CallbackCommand, CommandStack, TilePatchCommand
from comic_editor.core.document_patch import DocumentPatch, RecordSnapshot
from comic_editor.core.models import (ChapterDocument, RasterObject, ToneMask,
    ParameterMaskBinding, BlurModifier, HalftoneModifier)
from comic_editor.core.tiles import TileStore


def scene():
    chapter = ChapterDocument()
    page = chapter.add_page()
    first = chapter.add_object(page.layer_id, RasterObject())
    second = chapter.add_object(page.layer_id, RasterObject())
    mask = ToneMask(contributors=[("object", first.object_id)])
    chapter.masks[mask.mask_id] = mask
    modifier = BlurModifier(parameter_masks={"strength": ParameterMaskBinding(mask.mask_id, 0, 8)})
    chapter.add_modifier(modifier, [("object", second.object_id)])
    return chapter, page, first, second, mask, modifier


def test_reverse_dependencies_include_masks_effects_and_composition_without_sibling_damage():
    chapter, page, first, second, mask, modifier = scene()
    unrelated = chapter.add_object(page.layer_id, RasterObject())
    index = DependencyIndex(chapter)
    refs = index.publish(ChangeSet(resources=(ResourceChange(("object", first.object_id), "raster", (0, 0)),)))
    assert {("object", first.object_id), ("object", second.object_id), ("mask", mask.mask_id),
            ("modifier", modifier.modifier_id), ("layer", page.layer_id)} <= refs
    assert ("object", unrelated.object_id) not in refs
    assert index.generation("object", unrelated.object_id) == 0


def test_parameter_changes_do_not_rebuild_or_serialize_graph(monkeypatch):
    chapter, page, first, second, mask, modifier = scene()
    index = DependencyIndex(chapter)
    before = RecordSnapshot.capture(chapter, modifiers=[modifier.modifier_id])
    monkeypatch.setattr(chapter, "to_dict", lambda: (_ for _ in ()).throw(AssertionError("full serialization")))
    modifier.strength = 20
    old, new = DocumentPatch.pair(before, before.after(chapter))
    count = index.refresh_count
    affected = index.publish(new.change_set(old))
    assert index.refresh_count == count
    assert ("object", second.object_id) in affected
    assert ("object", first.object_id) not in affected
    assert new.change_set(old).entities[0].fields == frozenset({"strength"})


def test_dependency_rebind_retains_old_and_new_mask_consumers():
    chapter, page, first, second, mask, modifier = scene()
    index = DependencyIndex(chapter)
    before = RecordSnapshot.capture(chapter, masks=[mask.mask_id])
    mask.contributors = [("object", second.object_id)]
    old, new = DocumentPatch.pair(before, before.after(chapter))
    index.publish(new.change_set(old))
    # Removed source is no longer a mask dependency; a newly connected source
    # remains safely traversable even if it introduces a cycle in this fixture.
    assert ("mask", mask.mask_id) not in index.affected(ChangeSet(entities=(EntityChange(("object", first.object_id), frozenset({"pixels"})),)))
    assert ("mask", mask.mask_id) in index.affected(ChangeSet(entities=(EntityChange(("object", second.object_id), frozenset({"pixels"})),)))


def test_ancestor_geometry_propagates_descendants_but_metadata_does_not():
    chapter, page, first, second, mask, modifier = scene()
    index = DependencyIndex(chapter)
    assert not index.publish(ChangeSet(entities=(EntityChange(("layer", page.layer_id), frozenset({"name"})),)))
    affected = index.publish(ChangeSet(entities=(EntityChange(("layer", page.layer_id), frozenset({"bound"})),)))
    assert {("object", first.object_id), ("object", second.object_id), ("mask", mask.mask_id)} <= affected


def test_forward_undo_redo_publish_same_changes_with_oriented_bounds():
    chapter, page, first, second, mask, modifier = scene()
    index = DependencyIndex(chapter)
    before = RecordSnapshot.capture(chapter, modifiers=[modifier.modifier_id])
    modifier.strength = 16
    old, new = DocumentPatch.pair(before, before.after(chapter))
    bounds = {("modifier", modifier.modifier_id): ((0, 0, 10, 10), (0, 0, 30, 30))}
    forward = new.change_set(old, bounds=bounds)
    backward = forward.reversed()
    events = []
    stack = CommandStack()
    stack.change_callback = lambda change, action, revision: (events.append((change, action, revision)), index.publish(change))
    stack.push(CallbackCommand("Blur", lambda: new.apply(chapter), lambda: old.apply(chapter), forward, backward), already_done=True)
    stack.undo()
    assert modifier.strength == 8
    stack.redo()
    assert modifier.strength == 16
    assert [item[1] for item in events] == ["push", "undo", "redo"]
    assert events[1][0].entities[0].old_bounds == (0, 0, 30, 30)
    assert index.generation("object", second.object_id) == 3
    assert chapter.objects[first.object_id] is first


def test_raster_history_publication_carries_addresses_and_mask_dependencies():
    chapter, page, first, second, mask, modifier = scene()
    tiles = TileStore()
    before = {(0, 0): None}
    tiles.paint_dab(mask.mask_id, QPointF(5, 5), 4, QColor("white"))
    after = tiles.snapshot(mask.mask_id, {(0, 0)})
    index = DependencyIndex(chapter)
    events = []
    stack = CommandStack()
    stack.change_callback = lambda change, action, revision: events.append((change, index.publish(change)))
    stack.push(TilePatchCommand("Mask", tiles, mask.mask_id, before, after), already_done=True)
    stack.undo()
    assert events[0][0].resources[0].address == (0, 0)
    assert ("object", second.object_id) in events[0][1]
    assert ("object", second.object_id) in events[1][1]


def test_unknown_change_preserves_monotonic_generations_and_epoch():
    chapter, page, first, second, mask, modifier = scene()
    index = DependencyIndex(chapter)
    epoch = index.epoch
    index.publish(ChangeSet(conservative=True))
    index.publish(ChangeSet(conservative=True))
    assert index.epoch == epoch
    assert index.generation("object", first.object_id) == 2


def test_target_layer_sources_and_compound_children_retag_downstream_outputs():
    chapter, page, first, second, mask, modifier = scene()
    source = chapter.add_layer(page.layer_id, "Source")
    source_child = chapter.add_object(source.layer_id, RasterObject())
    halftone = HalftoneModifier(color_mode="target_layer", target_layer_id=source.layer_id)
    chapter.add_modifier(halftone, [("object", first.object_id)])
    page.compound_enabled = True
    index = DependencyIndex(chapter)
    affected = index.publish(ChangeSet(entities=(EntityChange(("object", source_child.object_id), frozenset({"pixels"})),)))
    assert {("layer", source.layer_id), ("modifier", halftone.modifier_id),
            ("object", first.object_id), ("mask", mask.mask_id), ("object", second.object_id)} <= affected
    # A compound operand/child edit changes the parent's clip, hence sibling
    # output. Propagate that influence without serializing their records.
    before = RecordSnapshot.capture(chapter, layers=[source.layer_id])
    source.translate_x = 12
    old, new = DocumentPatch.pair(before, before.after(chapter))
    change = new.change_set(old)
    assert "translation" in change.entities[0].fields
    assert ("object", source_child.object_id) in index.publish(change)


def test_collection_order_changes_are_explicit_and_reversible():
    chapter, page, first, second, mask, modifier = scene()
    before = RecordSnapshot.capture(chapter, objects=None)
    chapter.objects = dict(reversed(tuple(chapter.objects.items())))
    old, new = DocumentPatch.pair(before, before.after(chapter))
    change = new.change_set(old)
    assert not change.empty and change.structural
    assert change.orders[0].before == tuple(reversed(tuple(chapter.objects)))
    assert change.reversed().orders[0].after == change.orders[0].before


def test_command_apply_exposes_typed_change_and_failed_undo_retains_cursor():
    import pytest
    change = ChangeSet((EntityChange(("object", "drawing"), frozenset({"position"})),))
    stack, observed = CommandStack(), []
    def fail():
        observed.append(stack.applying_change)
        raise ValueError("failed history mutation")
    stack.push(CallbackCommand("Move", lambda: None, fail, change, change.reversed()), already_done=True)
    revision = stack.revision
    with pytest.raises(ValueError, match="failed history"):
        stack.undo()
    assert stack.can_undo and not stack.can_redo and stack.revision == revision
    assert stack.applying_change is None
    assert observed == [change.reversed()]
