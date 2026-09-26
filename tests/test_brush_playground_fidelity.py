"""Saved exercise samples use the drawing preset, not a thumbnail's visual zoom."""
from dataclasses import replace

import pytest

import brush_playground
from comic_editor.core.brushes import BrushDefinition, BrushTexture, default_brushes
from comic_editor.core.brush_preview import preview_samples
from comic_editor.core.brush_raster import RasterBrushStroke
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore


@pytest.mark.parametrize("linked", [False, True])
def test_drawing_size_preserves_fixed_lengths_and_only_resizes_linked_secondary(linked):
    texture = BrushTexture(scale=.35)
    child = BrushDefinition(size=105, texture=texture, particle_size=12,
                            watercolor_edge=3, blur_mode="fixed", blur_width=11)
    brush = BrushDefinition(size=150, texture=texture, particle_size=17,
                            watercolor_edge=4, blur_mode="fixed", blur_width=19,
                            dual=child, dual_link_size=linked)
    before = brush.to_dict()
    resized = brush.with_size(40)
    assert resized.size == 40
    assert resized.dual.size == pytest.approx(28 if linked else 105)
    assert resized.texture is texture and resized.dual.texture is texture
    assert (resized.particle_size, resized.watercolor_edge, resized.blur_width) == (17, 4, 19)
    assert (resized.dual.particle_size, resized.dual.watercolor_edge,
            resized.dual.blur_width) == (12, 3, 11)
    assert brush.to_dict() == before


def test_sample_definition_matches_live_size_control_and_retains_world_texture_scale():
    texture = BrushTexture(scale=.35)
    brush = BrushDefinition(size=150, texture=texture,
                            dual=BrushDefinition(size=105), dual_link_size=True)
    sample = brush_playground.exercise_brush(brush)
    settings = EditorSettings(brush_presets=[brush.to_dict()], active_brush_id=brush.id,
                              brush_size_px=brush_playground.exercise_size(brush),
                              brush_opacity=brush.opacity)
    assert sample == settings.active_paint_brush()
    assert sample.size == 40 and sample.dual.size == 28
    assert sample.texture.scale == .35
    assert brush.size == 150 and brush.dual.size == 105


def _pixels(tiles, object_id):
    return {key: bytes(image.constBits())
            for key, image in tiles.object_tiles(object_id).items()}


def test_saved_sample_pixels_equal_the_same_gesture_in_the_drawing_pad(qapp):
    source = next(brush for brush in default_brushes() if brush.id == "pixel-checker")
    brush = replace(source, size=150, texture=replace(source.texture, scale=.35))
    before = brush.to_dict()
    chapter, sample_tiles, cells = brush_playground.build_sheet([brush])
    sample = next(obj for obj in chapter.objects.values() if obj.name == "Sample stroke")
    settings = EditorSettings(brush_presets=[brush.to_dict()], active_brush_id=brush.id,
                              brush_size_px=cells[0]["test_size"], brush_opacity=brush.opacity)
    color, sub_color = brush_playground.exercise_colors(brush)
    drawing = replace(settings.active_paint_brush(), sub_color=sub_color.getRgb())
    tiles = TileStore()
    stroke = RasterBrushStroke(tiles, "pad", drawing, color, {}, seed=42)
    samples = preview_samples(462, 100)
    stroke.begin(samples[0])
    for point in samples[1:]:
        stroke.add(point)
    stroke.finish()
    assert _pixels(sample_tiles, sample.object_id) == _pixels(tiles, "pad")
    assert brush.to_dict() == before
