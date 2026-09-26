"""Live watercolor updates match the original complete-tile equation."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.core.brushes import BrushDefinition, BrushInput
from comic_editor.core.brush_raster import RasterBrushStroke, image_pixels, pixels_image
from comic_editor.core.tiles import TileStore
from test_brush_deferred_flush import definitions, seeded_tiles, snapshot


class FullTileEdges(RasterBrushStroke):
    """Use the unchanged completion path after every live sample as a reference."""
    def _flush(self):
        finished = self._edge_finished
        if not self.definition.watercolor_after:
            self._edge_finished = True
        try:
            return super()._flush()
        finally:
            self._edge_finished = finished


def assert_same_pixels(first, second):
    empty = np.zeros((32, 32, 4), np.float32)
    for key in first.keys() | second.keys():
        a = image_pixels(first[key]) if key in first else empty
        b = image_pixels(second[key]) if key in second else empty
        np.testing.assert_array_equal(a, b, err_msg=f"tile {key}")


@pytest.mark.parametrize("name", ["dry", "smear", "dual_wet_texture_edge", "stroke_texture"])
@pytest.mark.parametrize("radius", [.2, 2.6, 17])
def test_each_live_edge_frame_matches_full_tiles_including_negative_seams(name, radius):
    brush = replace(definitions()[name], size=11, watercolor_edge=radius,
                    watercolor_opacity=.48, watercolor_blur=15, watercolor_after=False)
    stores = [seeded_tiles(), seeded_tiles()]
    initial = snapshot(stores[0])
    before = [{}, {}]
    strokes = [kind(store, "paint", brush, QColor("#aa4d68"), originals, seed=901)
               for kind, store, originals in zip((RasterBrushStroke, FullTileEdges), stores, before)]
    # Reverse across previous paint, then cross the x/y tile corner. The inner
    # edge changes as overlapping dabs replace the outline of earlier paint.
    path = [(25, 13), (31, 17), (35, 22), (31, 17), (23, 12), (2, 2), (-3, -4)]
    for index, point in enumerate(path):
        sample = BrushInput(*point, pressure=.3+index*.1, time=index*.021)
        for stroke in strokes:
            (stroke.begin if index == 0 else stroke.add)(sample)
        assert_same_pixels(*[snapshot(store) for store in stores])
    for stroke in strokes:
        stroke.finish()
    assert_same_pixels(*[snapshot(store) for store in stores])
    for store, originals in zip(stores, before):
        for key, image in originals.items():
            store.set_tile("paint", key, image)
        assert snapshot(store) == initial


def test_live_edges_keep_selection_feathering_and_erase_exact():
    brush = replace(definitions()["dual_wet_texture_edge"], blending_mode="erase")
    def selection(key):
        pixels = np.ones((32, 32, 4), np.float32)
        pixels[..., 3] = np.clip((np.arange(32)+key[0]*32-20)/20, 0, 1)
        return pixels_image(pixels)
    stores = [seeded_tiles(), seeded_tiles()]
    strokes = [kind(store, "paint", brush, QColor("black"), {}, seed=13, selection_tile=selection)
               for kind, store in zip((RasterBrushStroke, FullTileEdges), stores)]
    for index, x in enumerate(range(18, 46, 2)):
        for stroke in strokes:
            (stroke.begin if index == 0 else stroke.add)(BrushInput(x, 22, time=index*.02))
        assert_same_pixels(*[snapshot(store) for store in stores])
    for stroke in strokes:
        stroke.finish()
    assert_same_pixels(*[snapshot(store) for store in stores])


def test_live_edge_morphology_is_limited_to_changed_patch_and_halo(monkeypatch):
    from comic_editor.core import brush_raster
    shapes = []
    original = brush_raster.maximum_filter
    def record(array, *args, **kwargs):
        shapes.append(array.shape)
        return original(array, *args, **kwargs)
    monkeypatch.setattr(brush_raster, "maximum_filter", record)
    store = TileStore(256)
    stroke = RasterBrushStroke(store, "paint", BrushDefinition(size=10, watercolor_edge=2,
        watercolor_after=False), QColor("red"), {})
    bounds = stroke.begin(BrushInput(64, 64))
    assert shapes and max(h*w for h, w in shapes) < 32*32
    assert bounds.width() < 24 and bounds.height() < 24
    stroke.finish()


def test_after_stroke_blur_uses_existing_full_tile_path(monkeypatch):
    regions = []
    original = RasterBrushStroke._edge_source
    def record(self, key, source, region=None):
        regions.append(region)
        return original(self, key, source, region)
    monkeypatch.setattr(RasterBrushStroke, "_edge_source", record)
    stroke = RasterBrushStroke(TileStore(32), "paint",
        BrushDefinition(size=9, watercolor_edge=2, watercolor_blur=1.4, watercolor_after=True),
        QColor("red"), {})
    stroke.begin(BrushInput(29, 18))
    stroke.add(BrushInput(37, 18, time=.02))
    assert not regions
    stroke.finish()
    assert regions and all(region is None for region in regions)
