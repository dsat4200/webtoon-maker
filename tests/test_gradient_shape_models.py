"""Linear/circular fill shape survives documents, masks, presets and undo."""
import copy
import json

import pytest

from comic_editor.core.commands import CommandStack, ObjectPatchCommand
from comic_editor.core.models import (
    SCHEMA_VERSION, SERIES_SCHEMA_VERSION, BoundGeometry, ChapterDocument,
    ColorFillGradientObject, ColorGradientRamp, ColorGradientRampPreset,
    ColorGradientStop, LineGradientField, PathNode, RadialGradientField,
    SeriesDocument, ShapeGradientField, SpeedLinesGradientObject, ToneMask,
    default_gradient_ramp_preset, object_from_dict,
)


def gradient(shape="circular", field_type="line"):
    return ColorFillGradientObject(
        gradient_shape=shape, field_type=field_type, loaded_preset_id="named-ramp",
        line_field=LineGradientField(BoundGeometry.path([
            PathNode(x=12.5, y=80), PathNode(x=237, y=91.25),
        ]), reverse_direction=True, direction_mode="perpendicular",
            perpendicular_distance=-31),
        radial_field=RadialGradientField(
            origin_x=83, origin_y=97, radius_x=47, radius_y=33,
            ellipse_enabled=True, rotation=29, center_auto=False,
            manual_center=(77, 85), reverse_direction=True, uniform=True,
            distance=19),
        shape_field=ShapeGradientField(
            center_auto=False, manual_center=(42, 65), reverse_direction=True,
            uniform=True, distance=21),
        ramp=ColorGradientRamp(stops=[
            ColorGradientStop(position=0, color="#00112233"),
            ColorGradientStop(position=.4, color="#FF345678"),
            ColorGradientStop(position=.4, color="#FF987654"),
            ColorGradientStop(position=1, color="#80443322"),
        ]),
    )


@pytest.mark.parametrize("field_type", ["line", "radial", "parent_shape"])
@pytest.mark.parametrize("shape", ["linear", "circular"])
def test_shape_round_trip_preserves_family_legacy_geometry_and_ramp(field_type, shape):
    original = gradient(shape, field_type)
    payload = original.to_dict()
    restored = object_from_dict(json.loads(json.dumps(payload)))
    assert isinstance(restored, ColorFillGradientObject)
    assert restored.gradient_shape == shape
    assert restored.field_type == field_type
    assert restored.gradient_type == "color_fill"
    assert restored.to_dict() == payload
    assert restored.line_field.direction_mode == "perpendicular"
    assert restored.radial_field.manual_center == (77, 85)
    assert restored.shape_field.manual_center == (42, 65)


@pytest.mark.parametrize("shape", ["linear", "circular"])
def test_chapter_mask_and_object_json_round_trip_are_independent(shape):
    chapter = ChapterDocument()
    page = chapter.add_page()
    obj = chapter.add_object(page.layer_id, gradient(shape))
    mask = ToneMask(name="Gradient mask", saved=True, gradient=gradient(shape))
    chapter.masks[mask.mask_id] = mask
    payload = chapter.to_dict()
    restored = ChapterDocument.from_dict(json.loads(json.dumps(payload)))
    assert restored.to_dict() == payload
    ordinary = restored.objects[obj.object_id]
    embedded = restored.masks[mask.mask_id].gradient
    assert ordinary.gradient_shape == embedded.gradient_shape == shape
    assert embedded.field_type == "line"
    assert embedded.line_field.direction_mode == "parallel"
    assert embedded.mask_only and embedded.parent_layer_id == ""
    assert len(embedded.line_field.geometry.nodes) == 2
    embedded.ramp.stops[0].color = "#FFFFFFFF"
    assert ordinary.ramp.stops[0].color == "#00112233"
    assert mask.gradient.ramp.stops[0].color == "#00112233"


@pytest.mark.parametrize("field_type", ["line", "radial", "parent_shape"])
def test_legacy_chapter_and_mask_default_to_linear_without_converting_fields(field_type):
    chapter = ChapterDocument()
    page = chapter.add_page()
    obj = chapter.add_object(page.layer_id, gradient("circular", field_type))
    mask = ToneMask(name="Saved gradient", saved=True, gradient=gradient())
    chapter.masks[mask.mask_id] = mask
    payload = chapter.to_dict()
    payload["schema_version"] = SCHEMA_VERSION - 1
    payload["objects"][0].pop("gradient_shape")
    payload["masks"][0]["gradient"].pop("gradient_shape")
    restored = ChapterDocument.from_dict(payload)
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.objects[obj.object_id].gradient_shape == "linear"
    assert restored.objects[obj.object_id].field_type == field_type
    assert restored.masks[mask.mask_id].gradient.gradient_shape == "linear"
    assert restored.objects[obj.object_id].line_field.to_dict() == obj.line_field.to_dict()
    assert restored.objects[obj.object_id].radial_field.to_dict() == obj.radial_field.to_dict()
    assert restored.objects[obj.object_id].shape_field.to_dict() == obj.shape_field.to_dict()


@pytest.mark.parametrize("invalid", [None, "", "radial", "CIRCULAR", 7, False, [], {}])
def test_invalid_shapes_normalize_on_object_and_preset_validation_and_load(invalid):
    obj = gradient()
    obj.gradient_shape = copy.deepcopy(invalid)
    obj.validate_gradient()
    assert obj.gradient_shape == "linear"
    payload = gradient().to_dict()
    payload["gradient_shape"] = copy.deepcopy(invalid)
    assert object_from_dict(payload).gradient_shape == "linear"
    preset = ColorGradientRampPreset(gradient_shape=copy.deepcopy(invalid))
    preset.validate()
    assert preset.gradient_shape == "linear"
    payload = preset.to_dict()
    payload["gradient_shape"] = copy.deepcopy(invalid)
    assert ColorGradientRampPreset.from_dict(payload).gradient_shape == "linear"


def test_series_presets_save_shape_migrate_missing_shape_and_keep_copied_ramps():
    circular = ColorGradientRampPreset(name="Circular fade", gradient_shape="circular",
                                       ramp=gradient().ramp)
    linear = default_gradient_ramp_preset("#FF998877", "#FF001122")
    series = SeriesDocument(gradient_ramp_presets=[circular, linear])
    payload = json.loads(json.dumps(series.to_dict()))
    restored = SeriesDocument.from_dict(payload)
    assert restored.to_dict() == payload
    assert [preset.gradient_shape for preset in restored.gradient_ramp_presets] == ["circular", "linear"]
    restored.gradient_ramp_presets[0].ramp.stops[0].color = "#FFFFFFFF"
    assert circular.ramp.stops[0].color == "#00112233"
    payload["schema_version"] = SERIES_SCHEMA_VERSION - 1
    for item in payload["gradient_ramp_presets"]:
        item.pop("gradient_shape")
    legacy = SeriesDocument.from_dict(payload)
    assert all(preset.gradient_shape == "linear" for preset in legacy.gradient_ramp_presets)
    assert legacy.gradient_ramp_presets[0].ramp.to_dict() == circular.ramp.to_dict()
    payload.pop("gradient_ramp_presets")
    assert SeriesDocument.from_dict(payload).gradient_ramp_presets[0].gradient_shape == "linear"


def test_object_patch_undo_redo_and_clone_preserve_shape_and_endpoint_values():
    chapter = ChapterDocument()
    page = chapter.add_page()
    obj = chapter.add_object(page.layer_id, gradient("linear"))
    before = obj.to_dict()
    clone = object_from_dict(copy.deepcopy(before))
    clone.gradient_shape = "circular"
    clone.line_field.geometry.nodes[-1].x += 40
    after = clone.to_dict()
    stack = CommandStack()
    stack.push(ObjectPatchCommand("Gradient shape", chapter,
        {obj.object_id: before}, {obj.object_id: after}))
    assert chapter.objects[obj.object_id] is obj
    assert obj.to_dict() == after
    clone.ramp.stops[0].color = "#FFFFFFFF"
    assert obj.ramp.stops[0].color == "#00112233"
    stack.undo()
    assert obj.to_dict() == before
    assert not stack.can_undo
    stack.redo()
    assert obj.to_dict() == after


def test_speed_lines_family_does_not_adopt_color_fill_shape():
    payload = SpeedLinesGradientObject().to_dict()
    assert "gradient_shape" not in payload
    payload["gradient_shape"] = "circular"
    restored = object_from_dict(payload)
    assert isinstance(restored, SpeedLinesGradientObject)
    assert "gradient_shape" not in restored.to_dict()
