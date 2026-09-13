"""Show-on-top is persistent entity state, independent of normal visibility."""
import copy
import json

import pytest

from comic_editor.core.assets import AssetManifest, extract_asset, instantiate_asset
from comic_editor.core.commands import CallbackCommand, CommandStack, ObjectPatchCommand
from comic_editor.core.models import (
    SCHEMA_VERSION, BoundGeometry, ChapterDocument, ColorFillGradientObject,
    DocumentObject, GradientObject, ImageObject, LayerNode, RasterObject,
    SpeedLineCenterObject, SpeedLinesGradientObject, TextObject, ToneMask,
    VectorDrawingObject, object_from_dict,
)
from comic_editor.core.tiles import TileStore


OBJECT_FAMILIES = [DocumentObject, RasterObject, ImageObject, TextObject,
                   VectorDrawingObject, GradientObject, ColorFillGradientObject,
                   SpeedLinesGradientObject, SpeedLineCenterObject]


@pytest.mark.parametrize("factory", OBJECT_FAMILIES)
@pytest.mark.parametrize("enabled", [False, True])
def test_all_object_families_round_trip_flag_without_changing_visibility(factory, enabled):
    obj = factory(show_on_top=enabled, visible=False, opacity=.37)
    payload = obj.to_dict()
    assert payload["show_on_top"] is enabled
    restored = object_from_dict(json.loads(json.dumps(payload)))
    assert type(restored) is factory
    assert restored.show_on_top is enabled
    assert restored.visible is False
    assert restored.opacity == .37
    assert restored.to_dict() == payload
    payload.pop("show_on_top")
    assert object_from_dict(payload).show_on_top is False
    assert factory().show_on_top is False


@pytest.mark.parametrize("kind", ["page", "bounded", "open_shape", "text_container"])
@pytest.mark.parametrize("enabled", [False, True])
def test_all_layer_kinds_and_pages_round_trip_flag(kind, enabled):
    chapter = ChapterDocument()
    page = chapter.add_page()
    if kind == "page":
        layer = page
    else:
        bound = BoundGeometry.polygon([(10, 20), (50, 70), (90, 30)])
        if kind == "open_shape":
            bound.closed = False
        layer = chapter.add_layer(page.layer_id, bound=bound, layer_kind=kind)
    layer.show_on_top, layer.visible, layer.opacity = enabled, False, .45
    chapter.validate()
    payload = layer.to_dict()
    restored = LayerNode.from_dict(json.loads(json.dumps(payload)))
    assert restored.show_on_top is enabled
    assert restored.visible is False
    assert restored.opacity == .45
    assert restored.to_dict() == payload
    payload.pop("show_on_top")
    assert LayerNode.from_dict(payload).show_on_top is False


def document():
    chapter = ChapterDocument()
    page = chapter.add_page()
    root = chapter.add_layer(page.layer_id, "Root", BoundGeometry.rectangle(10, 20, 100, 120))
    child = chapter.add_layer(root.layer_id, "Child", BoundGeometry.circle(50, 70, 20))
    first = chapter.add_object(child.layer_id, RasterObject(name="Top"))
    second = chapter.add_object(child.layer_id, VectorDrawingObject(name="Ordinary"))
    root.show_on_top, first.show_on_top = True, True
    return chapter, page, root, child, first, second


def test_chapter_and_embedded_mask_preserve_independent_flags_and_legacy_defaults():
    chapter, page, root, child, first, second = document()
    child.visible = False
    first.visible = False
    mask = ToneMask(saved=True, name="Mask", gradient=ColorFillGradientObject(show_on_top=True))
    chapter.masks[mask.mask_id] = mask
    payload = chapter.to_dict()
    restored = ChapterDocument.from_dict(json.loads(json.dumps(payload)))
    assert restored.to_dict() == payload
    assert restored.layers[root.layer_id].show_on_top
    assert not restored.layers[child.layer_id].show_on_top
    assert not restored.layers[page.layer_id].show_on_top
    assert restored.objects[first.object_id].show_on_top
    assert not restored.objects[second.object_id].show_on_top
    assert not restored.layers[child.layer_id].visible
    assert not restored.objects[first.object_id].visible
    assert restored.masks[mask.mask_id].gradient.show_on_top
    payload["schema_version"] = SCHEMA_VERSION - 1
    for item in [*payload["layers"], *payload["objects"], payload["masks"][0]["gradient"]]:
        item.pop("show_on_top")
    legacy = ChapterDocument.from_dict(payload)
    assert all(not entity.show_on_top for entity in [*legacy.layers.values(), *legacy.objects.values()])
    assert not legacy.masks[mask.mask_id].gradient.show_on_top
    assert not legacy.layers[child.layer_id].visible
    assert not legacy.objects[first.object_id].visible


@pytest.mark.parametrize("value,expected", [(None, False), (0, False), (1, True), ([], False), ([1], True)])
def test_chapter_validation_and_standalone_serialization_use_boolean_flags(value, expected):
    chapter, _, root, _, first, _ = document()
    root.show_on_top = copy.deepcopy(value)
    first.show_on_top = copy.deepcopy(value)
    assert root.to_dict()["show_on_top"] is expected
    assert first.to_dict()["show_on_top"] is expected
    chapter.validate()
    assert root.show_on_top is expected
    assert first.show_on_top is expected


def test_asset_extract_save_and_instantiate_preserve_each_flag_with_independent_copies():
    chapter, _, root, child, first, second = document()
    manifest, source_tiles = extract_asset(chapter, TileStore(), "layer", root.layer_id, "Top asset")
    manifest = AssetManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    target = ChapterDocument()
    page = target.add_page()
    kind, copied_root_id, copied_ids = instantiate_asset(
        manifest, source_tiles, target, TileStore(), page.layer_id, 200, 300)
    assert kind == "layer"
    assert copied_root_id != root.layer_id
    assert target.layers[copied_root_id].show_on_top
    copied_child = next(layer for layer in target.layers.values() if layer.parent_id == copied_root_id)
    assert not copied_child.show_on_top
    copied = {target.objects[key].name: target.objects[key] for key in copied_ids}
    assert copied["Top"].show_on_top
    assert not copied["Ordinary"].show_on_top
    target.layers[copied_root_id].show_on_top = False
    copied["Top"].show_on_top = False
    assert chapter.layers[root.layer_id].show_on_top
    assert chapter.objects[first.object_id].show_on_top
    assert manifest.document.layers[root.layer_id].show_on_top
    assert manifest.document.objects[first.object_id].show_on_top


def test_focused_object_undo_redo_preserves_flag_and_same_object_reference():
    chapter, _, _, _, first, _ = document()
    before = first.to_dict()
    after = copy.deepcopy(before)
    after["show_on_top"] = False
    after["visible"] = False
    stack = CommandStack()
    stack.push(ObjectPatchCommand("Show on top", chapter,
        {first.object_id: before}, {first.object_id: after}))
    assert chapter.objects[first.object_id] is first
    assert first.to_dict() == after
    stack.undo()
    assert first.to_dict() == before
    assert not stack.can_undo
    stack.redo()
    assert first.to_dict() == after


def test_layer_snapshot_undo_redo_preserves_child_flags_and_order():
    chapter, _, root, _, _, _ = document()
    before = chapter.to_dict()
    root.show_on_top = False
    after = chapter.to_dict()
    active = [chapter]

    def restore(payload):
        active[0] = ChapterDocument.from_dict(copy.deepcopy(payload))

    stack = CommandStack()
    stack.push(CallbackCommand("Show on top", lambda: restore(after), lambda: restore(before)))
    assert active[0].to_dict() == after
    stack.undo()
    assert active[0].to_dict() == before
    stack.redo()
    assert active[0].to_dict() == after


@pytest.mark.parametrize("flag", [None, False, True])
def test_legacy_fill_conversion_preserves_flag_when_present(flag):
    chapter, _, root, _, _, _ = document()
    drawing = chapter.add_object(root.layer_id, VectorDrawingObject())
    payload = chapter.to_dict()
    old_layer = LayerNode(layer_id="fill-layer", parent_id=root.layer_id).to_dict()
    old_layer.update(layer_kind="fill", bound=None)
    old_vector = DocumentObject(object_id="vector-fill", object_type="vector_fill",
                                parent_layer_id=root.layer_id).to_dict()
    old_vector.update(owner_drawing_id=drawing.object_id,
                      geometry=BoundGeometry.circle(50, 60, 20).to_dict())
    for item in (old_layer, old_vector):
        if flag is None:
            item.pop("show_on_top")
        else:
            item["show_on_top"] = flag
    payload["layers"].append(old_layer)
    payload["objects"].append(old_vector)
    next(item for item in payload["layers"] if item["id"] == root.layer_id)["children"].append(
        {"kind": "layer", "id": "fill-layer"})
    next(item for item in payload["objects"] if item["id"] == drawing.object_id)["fill_child_ids"] = ["vector-fill"]
    restored = ChapterDocument.from_dict(payload)
    for key in ("fill-layer", "vector-fill"):
        assert isinstance(restored.objects[key], RasterObject)
        assert restored.objects[key].show_on_top is bool(flag)
