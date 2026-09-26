"""A paint-free blender must have something to blend in its live preview."""
from dataclasses import replace

import numpy as np
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.brush_preview import (prepare_blender_preview, preview_samples,
                                            render_brush_preview)
from comic_editor.core.brush_raster import RasterBrushStroke, image_pixels
from comic_editor.core.tiles import TileStore


def test_blender_preview_carries_seed_paint_beyond_original_patch():
    brush = BrushDefinition(size=24,mixing_mode="running",paint_amount=0,
                            paint_density=0,color_stretch=1,spacing=.12)
    store = TileStore(256)
    prepare_blender_preview(store,"preview",brush,240,100)
    before = image_pixels(store.tile("preview",(0,0)))
    assert not np.any(before[:,120:220,3])
    samples = preview_samples(240,100)
    stroke = RasterBrushStroke(store,"preview",brush,QColor("red"),{},seed=42)
    stroke.begin(samples[0])
    for sample in samples[1:]:
        stroke.add(sample)
    stroke.finish()
    after = image_pixels(store.tile("preview",(0,0)))
    assert np.max(after[:,120:180,3]) > .05
    assert brush.paint_amount == 0 and brush.paint_density == 0


def test_painting_previews_do_not_receive_blender_underpaint():
    store = TileStore(256)
    for brush in (BrushDefinition(),BrushDefinition(mixing_mode="blend",paint_amount=.1)):
        prepare_blender_preview(store,"preview",brush,240,100)
    assert not store._tiles.get("preview")


def test_blender_live_preview_is_deterministic_and_responds_to_stretch():
    brush = BrushDefinition(size=24,mixing_mode="running",paint_amount=0,
                            paint_density=0,color_stretch=1,spacing=.12)
    before = brush.to_dict()
    image = render_brush_preview(brush,240,100)
    repeated = render_brush_preview(brush,240,100)
    changed = render_brush_preview(replace(brush,color_stretch=0),240,100)
    np.testing.assert_array_equal(image_pixels(image),image_pixels(repeated))
    assert not np.array_equal(image_pixels(image),image_pixels(changed))
    assert brush.to_dict() == before
