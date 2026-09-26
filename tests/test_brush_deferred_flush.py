"""Offscreen batching changes publication frequency, never finalized paint."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushInput, BrushTexture, BrushTip
from comic_editor.core.brush_raster import RasterBrushStroke, image_pixels, pixels_image
from comic_editor.core.tiles import TileStore
from test_brush_raster import png


def definitions():
    grain = np.zeros((5, 7, 4), np.uint8)
    grain[..., :3] = ((np.indices((5, 7)).sum(axis=0)*47) % 256)[..., None]
    grain[..., 3] = 255
    texture = BrushTexture(png=png(grain), scale=1.3, mode="color_burn")
    material = np.zeros((6, 11, 4), np.uint8)
    material[1:5, 1:10] = (72, 162, 230, 220)
    material[2:4, 3:7] = (229, 69, 114, 160)
    tip = BrushTip("Ribbon", 11, 6, png(material), mode="color", shape="image")
    ordinary = BrushDefinition(size=13, opacity=.57, density=.42, spacing=.3)
    second = BrushDefinition(size=9, spacing=.42, density=.65, hardness=.4)
    edge = replace(ordinary, watercolor_edge=2, watercolor_opacity=.46, watercolor_after=False)
    return {
        "dry": ordinary,
        "color_ribbon": replace(ordinary, ribbon=True, tips=(tip,)),
        "erase": replace(ordinary, blending_mode="erase"),
        "blend": replace(ordinary, mixing_mode="blend", paint_amount=.24, paint_density=.3),
        "running": replace(ordinary, mixing_mode="running", paint_amount=.18, blur=.25),
        "smear": replace(ordinary, mixing_mode="smear", paint_amount=.2, color_stretch=.7),
        "dual": replace(ordinary, dual=second, dual_mode="height_linear"),
        "live_edge": edge,
        "after_edge": replace(edge, watercolor_after=True, watercolor_blur=.6),
        "stroke_texture": replace(ordinary, texture=texture,
            dynamics={"texture_density": BrushDynamics(pressure=True, minimum=.2)}),
        "dual_wet_texture_edge": replace(edge, dual=replace(second, texture=texture),
            dual_mode="multiply", mixing_mode="smear", paint_amount=.4, texture=texture),
        "corrected": replace(edge, post_correction=.4),
        "ending": replace(edge, taper_end=23, taper_parameters=("size", "opacity")),
        "dual_ending": replace(ordinary, dual=replace(second, taper_end=19), dual_mode="normal"),
        "percentage": replace(edge, taper_start=22, taper_end=41, taper_mode="percentage"),
        "spacing_end": replace(ordinary, taper_end=31, taper_parameters=("spacing",)),
    }


class RecordingTiles(TileStore):
    def __init__(self):
        super().__init__(32)
        self.writes = 0

    def set_tile(self, *args):
        self.writes += 1
        return super().set_tile(*args)


def seeded_tiles():
    store = RecordingTiles()
    for key, rgb in (((0, 0), (.13, .48, .8)), ((1, 0), (.8, .72, .14))):
        image = np.empty((32, 32, 4), np.float32)
        image[..., :3] = rgb
        image[..., 3] = .65
        store.set_tile("paint", key, pixels_image(image))
    store.writes = 0
    return store


def snapshot(store):
    return {key: QImage(image) for key, image in store.iter_tiles("paint")}


def draw(brush, deferred, *, selection=False):
    store = seeded_tiles()
    initial = snapshot(store)
    before = {}
    def mask(key):
        if key[1] != 0:
            image = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
            image.fill(QColor("transparent"))
            return image
        image = np.ones((32, 32, 4), np.float32)
        image[..., 3] = np.linspace(0, 1, 32)
        return pixels_image(image)
    stroke = RasterBrushStroke(store, "paint", brush, QColor("#993d62"), before,
        seed=412, defer_flush=deferred, selection_tile=mask if selection else None)
    samples = [BrushInput(14+i*3, 17+3*np.sin(i*.6), pressure=.25+i*.045, time=i*.017)
               for i in range(16)]
    live = [stroke.begin(samples[0])]
    live.extend(stroke.add(sample) for sample in samples[1:])
    if deferred:
        assert store.writes == 0
        assert snapshot(store) == initial
        assert all(rect.isEmpty() for rect in live)
    else:
        assert store.writes > 0
        assert any(not rect.isEmpty() for rect in live)
    final = stroke.finish()
    if deferred:
        assert not final.isEmpty()
    assert stroke.finish().isEmpty()
    result = snapshot(store)
    for key, image in before.items():
        store.set_tile("paint", key, image)
    assert snapshot(store) == initial, "Both modes preserve exact undo snapshots"
    return result


@pytest.mark.parametrize("name,brush", list(definitions().items()), ids=list(definitions()))
def test_deferred_flush_preserves_final_pixels_and_undo(name, brush):
    immediate, deferred = draw(brush, False), draw(brush, True)
    empty = np.zeros((32, 32, 4), np.float32)
    for key in immediate.keys() | deferred.keys():
        a = image_pixels(immediate[key]) if key in immediate else empty
        b = image_pixels(deferred[key]) if key in deferred else empty
        np.testing.assert_array_equal(a, b, err_msg=f"{name}: tile {key}")


def test_deferred_edges_preserve_antialiased_selection():
    brush = definitions()["dual_wet_texture_edge"]
    immediate, deferred = draw(brush, False, selection=True), draw(brush, True, selection=True)
    assert immediate.keys() == deferred.keys()
    for key in immediate:
        np.testing.assert_array_equal(image_pixels(immediate[key]), image_pixels(deferred[key]))


def test_static_edge_stroke_publishes_each_touched_tile_only_once():
    tiles = seeded_tiles()
    stroke = RasterBrushStroke(tiles, "paint", definitions()["live_edge"], QColor("red"), {},
        defer_flush=True)
    stroke.begin(BrushInput(12, 17))
    for x in range(13, 54):
        stroke.add(BrushInput(x, 17, time=x/100))
    assert tiles.writes == 0
    stroke.finish()
    assert tiles.writes == len(list(tiles.iter_tiles("paint")))
