"""Smudge data preserves editable gestures and per-gesture pressure snapshots."""
import copy
import json
import math
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QTransform

from comic_editor.core.distort import default_parameters, gizmo_kind, validate_parameters
from comic_editor.core.models import (
    SERIES_SCHEMA_VERSION, BoundGeometry, ChapterDocument, DistortModifier,
    RasterObject, SeriesDocument, modifier_from_dict,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.smudge import (
    default_tool_settings, fit_smudge_stroke, pressure_at, pressure_response,
    stroke_cubic, stroke_parameters, transform_strokes, validate_strokes,
    validate_tool_settings,
)


def gesture(settings=None):
    return fit_smudge_stroke([(20., 30., .2), (40., 60., .6), (80., 30., 1.)],
                            settings or default_tool_settings())


def cubic_point(cubic, t):
    u = 1 - t
    weights = (u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3)
    return tuple(sum(point[axis] * weight for point, weight in zip(cubic, weights))
                 for axis in (0, 1))


def test_modifier_registration_round_trip_and_detached_defaults():
    modifier = DistortModifier(modifier_type="distort_smudge")
    modifier.validate()
    assert modifier.name == "Smudge Modifier"
    assert gizmo_kind(modifier.modifier_type) == "smudge"
    assert modifier.parameters["opacity"] == 100
    modifier.parameters["strokes"] = [gesture()]
    before = copy.deepcopy(modifier.to_dict())
    loaded = modifier_from_dict(json.loads(json.dumps(before)))
    assert loaded.to_dict() == before
    loaded.parameters["strokes"][0]["points"][0]["radius"] = 80
    loaded.parameters["tool_settings"]["strength_curve"]["min_ratio"] = .8
    assert modifier.to_dict() == before
    defaults = default_parameters("distort_smudge")
    defaults["strokes"].append(gesture())
    defaults["tool_settings"]["strength_curve"]["min_ratio"] = .9
    fresh = default_parameters("distort_smudge")
    assert fresh["strokes"] == []
    assert fresh["tool_settings"]["strength_curve"]["min_ratio"] == .1


def test_two_point_fit_uses_whole_gesture_and_keeps_pressure_profile():
    source = ((10., 90.), (10., 10.), (110., 10.), (110., 90.))
    samples = [(*cubic_point(source, index / 40), .2 + index / 50) for index in range(41)]
    stroke = fit_smudge_stroke(samples, default_tool_settings())
    assert len(stroke["points"]) == 2
    cubic = stroke_cubic(stroke)
    assert cubic[0] == source[0] and cubic[-1] == source[-1]
    assert cubic_point(cubic, .5)[1] < 38
    # The endpoints alone form a horizontal line, so this demonstrates the
    # interior gesture actually participates in fitting.
    assert len(stroke["pressure"]) == len(samples)
    assert pressure_at(stroke, 0) == .2
    assert pressure_at(stroke, 1) == 1
    assert .55 < pressure_at(stroke, .5) < .65
    assert validate_strokes([stroke]) == [stroke]


def test_line_and_stationary_gestures_are_finite_with_exact_endpoints():
    stroke = fit_smudge_stroke([(3., 4., 1.), (43., 24., 1.), (103., 54., 1.)], {})
    for x, y in stroke_cubic(stroke):
        assert y == pytest.approx(.5 * (x - 3) + 4)
    stationary = fit_smudge_stroke([(7., 9., .3), (7., 9., .6)], {})
    assert stroke_cubic(stationary) == ((7., 9.),) * 4
    assert stationary["pressure"] == [[0., .6], [1., .6]]


def test_stroke_snapshots_settings_and_endpoint_parameters_remain_editable():
    settings = default_tool_settings()
    stroke = gesture(settings)
    before = copy.deepcopy(stroke)
    settings["radius"] = 600
    settings["strength_curve"]["control_y"] = 1
    assert stroke == before
    assert stroke_parameters(stroke, .5)["radius"] == 32
    stroke["points"][0]["radius"], stroke["points"][1]["radius"] = 10, 90
    assert stroke_parameters(stroke, .5)["radius"] == 50
    stroke["points"][0]["point_type"] = "vector"
    assert stroke_cubic(stroke)[1] == tuple(stroke["points"][0]["position"])
    assert stroke["points"][0]["handle"] != stroke["points"][0]["position"]


def test_pressure_switches_are_independent_and_curve_drives_selected_channel():
    settings = default_tool_settings()
    assert settings["pressure_enabled"] and settings["pressure_strength"]
    assert not settings["pressure_flow"] and not settings["pressure_radius"]
    for name in ("radius", "flow", "strength"):
        settings[name + "_curve"] = {"min_ratio": 0, "max_ratio": 1, "control_x": .5, "control_y": .5}
    assert pressure_response(settings, "radius", .2) == 1
    assert pressure_response(settings, "flow", .2) == 1
    assert pressure_response(settings, "strength", .2) == pytest.approx(.2, abs=1e-6)
    settings["pressure_radius"] = True
    assert pressure_response(settings, "radius", .8) == pytest.approx(.8, abs=1e-6)
    settings["pressure_enabled"] = False
    assert all(pressure_response(settings, name, 0) == 1 for name in ("radius", "flow", "strength"))


@pytest.mark.parametrize("settings", [None, [], {"unknown": 4}, {"radius": math.inf},
                                     {"flow": math.nan}, {"pressure_strength": "false"},
                                     {"strength_curve": []}, {"radius_curve": {"control_x": math.nan}},
                                     {"radius_curve": {"surprise": 1}}])
def test_malformed_tool_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        validate_tool_settings(settings)


def test_numeric_settings_clamp_without_mutating_input():
    source = {"radius": -1, "flow": 700, "strength": -15,
              "strength_curve": {"min_ratio": -1, "max_ratio": 2}}
    before = copy.deepcopy(source)
    value = validate_tool_settings(source)
    assert (value["radius"], value["flow"], value["strength"]) == (.1, 100, 0)
    assert value["strength_curve"]["min_ratio"] == 0
    assert value["strength_curve"]["max_ratio"] == 1
    assert source == before
    assert validate_parameters("distort_smudge", {"opacity": 300})["opacity"] == 100


@pytest.mark.parametrize("mutation", ["one_point", "three_points", "position", "handle", "type",
                                      "duplicate_t", "missing_end", "pressure_nan", "settings", "id"])
def test_malformed_strokes_are_rejected(mutation):
    stroke = gesture()
    if mutation == "one_point":
        stroke["points"].pop()
    elif mutation == "three_points":
        stroke["points"].append(copy.deepcopy(stroke["points"][0]))
    elif mutation in {"position", "handle"}:
        stroke["points"][0][mutation] = [0, math.nan]
    elif mutation == "type":
        stroke["points"][0]["point_type"] = "unknown"
    elif mutation == "duplicate_t":
        stroke["pressure"] = [[0, 1], [0, .2], [1, .4]]
    elif mutation == "missing_end":
        stroke["pressure"] = [[.1, 1], [1, .4]]
    elif mutation == "pressure_nan":
        stroke["pressure"][0][1] = math.nan
    elif mutation == "settings":
        stroke["pressure_settings"] = {"radius": math.inf}
    else:
        stroke["id"] = ""
    with pytest.raises(ValueError):
        validate_parameters("distort_smudge", {"strokes": [stroke]})


def test_strokes_and_presets_cannot_share_duplicate_identifiers():
    stroke = gesture()
    with pytest.raises(ValueError, match="unique"):
        validate_strokes([stroke, copy.deepcopy(stroke)])
    series = SeriesDocument(smudge_tool_presets=[{"id": "a", "name": "Soft", "settings": {}},
                                               {"id": "a", "name": "Hard", "settings": {}}])
    with pytest.raises(ValueError, match="unique"):
        series.validate()


def test_tool_presets_are_series_local_detached_and_legacy_defaults_empty(tmp_path):
    first_repository, second_repository = SeriesRepository(tmp_path / "one"), SeriesRepository(tmp_path / "two")
    first, second = first_repository.create("One"), second_repository.create("Two")
    first.smudge_tool_presets = [{"id": "soft", "name": "  Soft  ", "settings": {"radius": 81}}]
    first_repository.save_series(first)
    loaded = first_repository.load_series()
    assert loaded.smudge_tool_presets[0]["name"] == "Soft"
    assert loaded.smudge_tool_presets[0]["settings"]["radius"] == 81
    assert second_repository.load_series().smudge_tool_presets == []
    encoded = first.to_dict()
    restored = SeriesDocument.from_dict(encoded)
    restored.smudge_tool_presets[0]["settings"]["radius_curve"]["min_ratio"] = .8
    assert encoded["smudge_tool_presets"][0]["settings"]["radius_curve"]["min_ratio"] == .1
    encoded.pop("smudge_tool_presets")
    encoded["schema_version"] = 17
    assert SeriesDocument.from_dict(encoded).smudge_tool_presets == []
    assert SeriesDocument.from_dict(encoded).schema_version == SERIES_SCHEMA_VERSION == 18
    assert second.smudge_tool_presets == []
    with pytest.raises(ValueError):
        SeriesDocument.from_dict({"id": "bad", "smudge_tool_presets": {}})


def test_world_stroke_transform_preserves_snapshot_and_maps_controls_and_radius():
    stroke = gesture()
    original = copy.deepcopy(stroke)
    mapped = transform_strokes([stroke], lambda point: (2 * point[0] + 30, 2 * point[1] - 10))[0]
    assert stroke == original
    assert mapped["pressure"] == stroke["pressure"]
    assert mapped["pressure_settings"] == stroke["pressure_settings"]
    for before, after in zip(stroke["points"], mapped["points"]):
        assert after["radius"] == before["radius"] * 2
        for key in ("position", "handle"):
            assert after[key] == [2 * before[key][0] + 30, 2 * before[key][1] - 10]


def test_modifier_rig_hook_transforms_smudge_controls_before_validation():
    from comic_editor.ui.distort_features import DistortFeatures
    from comic_editor.ui.transform_modifier_preview import transform_modifier_rig
    modifier = DistortModifier(modifier_type="distort_smudge", parameters={"strokes": [gesture()]})
    modifier.validate()
    original = copy.deepcopy(modifier.parameters["strokes"])
    canvas = SimpleNamespace(_transform_distort_modifier=DistortFeatures._transform_distort_modifier)
    transform_modifier_rig(canvas, modifier, QTransform.fromTranslate(17, -11))
    expected = transform_strokes(original, lambda point: (point[0] + 17, point[1] - 11))
    assert modifier.parameters["strokes"] == expected
    assert modifier.parameters["strokes"][0]["pressure_settings"] == original[0]["pressure_settings"]


def test_assets_translate_strokes_with_source_then_destination_without_mutating_original():
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor
    from comic_editor.core.assets import extract_asset, instantiate_asset
    from comic_editor.core.tiles import TileStore
    source = ChapterDocument()
    page = source.add_page("Page", BoundGeometry.rectangle(0, 0, 600, 600))
    obj = source.add_object(page.layer_id, RasterObject(x=35, y=40))
    tiles = TileStore()
    tiles.paint_dab(obj.object_id, QPointF(40, 40), 30, QColor("#ff0000"))
    modifier = DistortModifier(modifier_type="distort_smudge", parameters={"strokes": [gesture()]},
                              frame=(35., 40., 100., 100.))
    source.add_modifier(modifier, [("object", obj.object_id)])
    before = copy.deepcopy(modifier.to_dict())
    manifest, asset_tiles = extract_asset(source, tiles, "object", obj.object_id, "Smudged")
    asset_modifier = next(iter(manifest.document.modifiers.values()))
    dx, dy = asset_modifier.frame[0] - before["frame"][0], asset_modifier.frame[1] - before["frame"][1]
    assert asset_modifier.parameters["strokes"] == transform_strokes(
        before["parameters"]["strokes"], lambda point: (point[0] + dx, point[1] + dy))
    assert modifier.to_dict() == before
    target = ChapterDocument()
    target_page = target.add_page("Destination")
    instantiate_asset(manifest, asset_tiles, target, TileStore(), target_page.layer_id, 500, 650)
    placed = next(iter(target.modifiers.values()))
    bx, by, bw, bh = manifest.visual_bounds
    dx, dy = 500 - (bx + bw / 2), 650 - (by + bh / 2)
    assert placed.parameters["strokes"] == transform_strokes(
        asset_modifier.parameters["strokes"], lambda point: (point[0] + dx, point[1] + dy))
