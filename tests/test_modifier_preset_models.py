"""Series presets retain effect data without moving instance-local bindings."""
import copy
import json

import pytest

from comic_editor.core.models import (
    ArrayModifier, BlurModifier, BoundGeometry, CageTransformModifier, ChapterDocument,
    DotDashModifier, HalftoneModifier, HueSaturationLightnessModifier, MirrorModifier,
    ModifierPreset, OutlineModifier, ParameterMaskBinding, PixelateModifier,
    PosterizeModifier, PosterizeRange, PosterizeValueModifier, RadialBlurModifier,
    RasterObject, ScreamModifier, SeriesDocument, TilingModifier, WobbleModifier,
)
from comic_editor.core.modifier_presets import (
    INSTANCE_FIELDS, apply_modifier_preset, modifier_preset_settings, preset_from_modifier,
)
from comic_editor.core.persistence import SeriesRepository


MODIFIERS = [
    HueSaturationLightnessModifier(hue=45, saturation=30, lightness=-20),
    BlurModifier(strength=24, mode="focal", focal_center=(15, 25), focal_radius=47, focal_ramp=.2, focal_angle=16),
    BlurModifier(strength=17, algorithm="legacy"),
    OutlineModifier(thickness=18, color="#80446688", opacity=32),
    MirrorModifier(axis_start=(13, 27), axis_end=(59, 93), compound_operation="subtract"),
    ArrayModifier(axis_start=(1, 2), axis_end=(35, 49), center=(23, 43), count=7,
                  angle_offset=30, scale_offset=10, repeat_type="center"),
    RadialBlurModifier(center=(73, 11), angle=91),
    CageTransformModifier(frame=(12, 32, 120, 70), columns=2, rows=2,
                          points=[(10, 31), (140, 36), (12, 102), (125, 118)],
                          smoothness=70, interpolation="bilinear", pivot=(52, 32), uniform=True),
    PosterizeModifier(ranges=[PosterizeRange(0, "#FF123456"), PosterizeRange(100, "#FF887766")],
                      simplify_enabled=True, simplify_radius=7, simplify_strength=65),
    PosterizeValueModifier(ranges=[PosterizeRange(0, "#FF111111"), PosterizeRange(160, "#FFEEDDEE")]),
    TilingModifier(shape="hexagon", center=(15, 19), side=89, rotation=67),
    ScreamModifier(height=50, width=65, roundness=30),
    WobbleModifier(position=40, strength=50, noise_scale=125, noise_offset=43, seed=341),
    DotDashModifier(mode="dash", pattern="-- - ", distance=19, length=31, roundness=45),
    HalftoneModifier(grid_type="stippling", dot_style="polygon", color_mode="target_layer",
                     target_layer_id="private-source", target_hue=36, target_lightness=17,
                     gradient_stops=[[0, "#FF3300FF"], [.7, "#80FF0011"], [1, "#FFFFFFFF"]],
                     gradient_interpolation="oklch", sides=9, star=True, star_inner=.35,
                     merge_strength=1.2, collide_min=.1, collide_max=1.4),
    PixelateModifier(pixel_size=17, brightness=125, contrast=-20, saturation=170, blur=4.5),
]


@pytest.mark.parametrize("source", MODIFIERS, ids=lambda value: value.name + (" legacy" if getattr(value, "algorithm", "") == "legacy" else ""))
def test_all_modifier_kinds_snapshot_apply_and_round_trip(source):
    source = copy.deepcopy(source)
    source.intensity = 63
    source.muted, source.expanded = True, False
    source.parameter_masks = {"intensity": ParameterMaskBinding("source-mask", 15, 80)}
    original = copy.deepcopy(source)
    preset = preset_from_modifier("  Saved effect  ", source)
    assert source == original
    assert preset.name == "Saved effect"
    assert not set(preset.settings).intersection(INSTANCE_FIELDS)
    assert preset.settings["intensity"] == 63
    assert "target_layer_id" not in preset.settings
    restored = ModifierPreset.from_dict(json.loads(json.dumps(preset.to_dict())))
    assert restored.to_dict() == preset.to_dict()
    target = type(source)()
    target.name = "Instance name"
    target.expanded, target.muted = False, True
    target.parameter_masks = {"intensity": ParameterMaskBinding("target-mask", 10, 90)}
    if isinstance(target, HalftoneModifier):
        target.target_layer_id = "keep-local-source"
    before = copy.deepcopy(target)
    applied = apply_modifier_preset(target, restored)
    assert target == before
    assert applied is not target and type(applied) is type(target)
    assert applied.modifier_id == target.modifier_id
    assert applied.name == target.name
    assert (applied.expanded, applied.muted) == (False, True)
    assert applied.parameter_masks == target.parameter_masks
    assert applied.parameter_masks is not target.parameter_masks
    assert modifier_preset_settings(applied) == preset.settings
    if isinstance(target, HalftoneModifier):
        assert applied.target_layer_id == "keep-local-source"


def test_settings_and_loaded_values_do_not_alias_preset_or_original():
    modifier = HalftoneModifier()
    preset = preset_from_modifier("Ink", modifier)
    loaded = apply_modifier_preset(HalftoneModifier(), preset)
    loaded.gradient_stops[0][1] = "#FFFF0000"
    assert preset.settings["gradient_stops"][0][1] == "#FF000000"
    assert modifier.gradient_stops[0][1] == "#FF000000"
    payload = preset.to_dict()
    payload["settings"]["gradient_stops"][0][1] = "#FF00FF00"
    assert preset.settings["gradient_stops"][0][1] == "#FF000000"


@pytest.mark.parametrize("kind,settings", [
    ("unknown", {}), ("hsl", {"type": "blur"}), ("blur", {"unrecognized": 1}),
    ("blur", {"parameter_masks": {}}), ("halftone", {"target_layer_id": "private"}),
    ("blur", {"id": "overwrite"}), ("blur", {"expanded": False}),
    ("hsl", {"hue": float("nan")}), ("blur", {"strength": "not a number"}),
    ("posterize", {"ranges": None}), ("cage_transform", {"frame": [0, 0]}),
    ("halftone", {"gradient_stops": "invalid"}), ("blur", []),
    ("blur", {1: "invalid"}), ("blur", {"strength": object()}),
])
def test_invalid_or_binding_injecting_preset_payloads_raise_value_error(kind, settings):
    with pytest.raises(ValueError):
        ModifierPreset(modifier_type=kind, settings=settings).validate()


def test_loading_another_type_is_rejected_without_mutation():
    target = HalftoneModifier()
    before = target.to_dict()
    preset = preset_from_modifier("Pixels", PixelateModifier())
    with pytest.raises(ValueError, match="same modifier type"):
        apply_modifier_preset(target, preset)
    assert target.to_dict() == before
    with pytest.raises(ValueError, match="Expected a modifier preset"):
        apply_modifier_preset(target, {})


def test_series_scope_legacy_defaults_unique_ids_and_disk_persistence(tmp_path):
    first_repository = SeriesRepository(tmp_path / "first")
    second_repository = SeriesRepository(tmp_path / "second")
    first, second = first_repository.create("First"), second_repository.create("Second")
    preset = preset_from_modifier("Blue print", HalftoneModifier(foreground="#FF123456"))
    first.modifier_presets.append(preset)
    first_repository.save_series(first)
    assert first_repository.load_series().modifier_presets[0].to_dict() == preset.to_dict()
    assert second_repository.load_series().modifier_presets == []
    legacy = first.to_dict()
    legacy.pop("modifier_presets")
    assert SeriesDocument.from_dict(legacy).modifier_presets == []
    first.modifier_presets.append(copy.deepcopy(preset))
    with pytest.raises(ValueError, match="unique IDs"):
        first.validate()
    for payload in ({"id": "series", "modifier_presets": {}},
                    {"id": "series", "modifier_presets": [None]}):
        with pytest.raises(ValueError):
            SeriesDocument.from_dict(payload)


def _chapter_with_modifier():
    chapter = ChapterDocument()
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 100, 100))
    raster = chapter.add_object(page.layer_id, RasterObject())
    modifier = PixelateModifier(pixel_size=19)
    chapter.add_modifier(modifier, [("object", raster.object_id)])
    chapter.modifier_preset_ids[modifier.modifier_id] = "selected-preset"
    return chapter, raster, modifier


def test_chapter_association_round_trip_legacy_defaults_and_validation():
    chapter, _, modifier = _chapter_with_modifier()
    chapter.modifier_preset_ids["removed-modifier"] = "orphan-preset"
    chapter.validate()
    expected = {modifier.modifier_id: "selected-preset"}
    assert chapter.modifier_preset_ids == expected
    restored = ChapterDocument.from_dict(json.loads(json.dumps(chapter.to_dict())))
    assert restored.modifier_preset_ids == expected
    legacy = chapter.to_dict()
    legacy.pop("modifier_preset_ids")
    assert ChapterDocument.from_dict(legacy).modifier_preset_ids == {}
    for invalid in ([], {modifier.modifier_id: 2}):
        payload = chapter.to_dict()
        payload["modifier_preset_ids"] = invalid
        with pytest.raises(ValueError, match="associations"):
            ChapterDocument.from_dict(payload)


@pytest.mark.parametrize("operation", ["remove", "unlink", "garbage", "validate"])
def test_modifier_removal_cleans_preset_association(operation):
    chapter, raster, modifier = _chapter_with_modifier()
    if operation == "remove":
        chapter.remove_modifier(modifier.modifier_id)
    elif operation == "unlink":
        chapter.set_modifier_targets(modifier.modifier_id, [])
    else:
        raster.modifier_ids.clear()
        if operation == "garbage":
            chapter._garbage_collect_modifiers()
        else:
            chapter.validate()
    assert chapter.modifier_preset_ids == {}
    assert modifier.modifier_id not in chapter.modifiers


def test_series_and_chapter_preset_association_survive_repository_reload(tmp_path):
    repository = SeriesRepository(tmp_path / "series")
    series = repository.create("Series presets")
    chapter, tiles = repository.create_chapter(series, "One")
    raster = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    modifier = BlurModifier(strength=30)
    chapter.add_modifier(modifier, [("object", raster.object_id)])
    preset = preset_from_modifier("Soft edges", modifier)
    series.modifier_presets.append(preset)
    chapter.modifier_preset_ids[modifier.modifier_id] = preset.preset_id
    repository.save_series(series)
    repository.save_chapter(chapter, tiles)
    loaded, _ = repository.load_chapter(chapter.chapter_id)
    assert loaded.modifier_preset_ids == {modifier.modifier_id: preset.preset_id}
    assert repository.load_series().modifier_presets[0].settings["strength"] == 30
