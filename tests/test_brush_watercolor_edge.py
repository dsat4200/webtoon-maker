"""A retained watercolor blur width is active only for post-stroke edges."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDefinition, BrushInput
from comic_editor.core.brush_raster import RasterBrushStroke
from comic_editor.core.brush_units import has_physical_lengths
from comic_editor.core.settings import EditorSettings
from comic_editor.core.sut_import import import_sut
from comic_editor.core.tiles import TileStore
from comic_editor.ui.brush_controls import BrushControls
from test_brush_raster import atlas
from test_brush_units import unit_sut


def edge_stroke(brush):
    tiles = TileStore(64)
    stroke = RasterBrushStroke(tiles,"paint",brush,QColor("#799ac8"),{})
    stroke.begin(BrushInput(44,32))
    bounds = stroke.add(BrushInput(78,32,time=.1))
    live = atlas(tiles).copy()
    stroke.finish()
    return live, atlas(tiles), bounds, stroke


def test_live_edges_ignore_retained_blur_in_pixels_and_dirty_bounds():
    brush = BrushDefinition(size=18,opacity=.7,watercolor_edge=2,
        watercolor_opacity=.65,watercolor_blur=15,watercolor_after=False)
    live, final, bounds, _ = edge_stroke(brush)
    clean_live, clean_final, clean_bounds, _ = edge_stroke(replace(brush,watercolor_blur=0))
    np.testing.assert_array_equal(live,clean_live)
    np.testing.assert_array_equal(final,clean_final)
    assert bounds == clean_bounds
    assert brush.watercolor_blur == 15


def test_after_stroke_blur_is_dormant_live_and_changes_final_edge():
    brush = BrushDefinition(size=18,opacity=.7,watercolor_edge=2,
        watercolor_opacity=.65,watercolor_blur=3,watercolor_after=True)
    live, final, _, _ = edge_stroke(brush)
    sharp_live, sharp_final, _, _ = edge_stroke(replace(brush,watercolor_blur=0))
    plain_live, _, _, _ = edge_stroke(replace(brush,watercolor_edge=0))
    np.testing.assert_array_equal(live,plain_live)
    np.testing.assert_array_equal(live,sharp_live)
    assert np.any(final != sharp_final)
    assert final[18,60,3] > sharp_final[18,60,3]


def test_dormant_millimeter_edge_blur_does_not_prompt_but_retains_value(qapp,tmp_path,monkeypatch):
    from comic_editor.ui import brush_controls
    path = unit_sut(tmp_path, BrushSize=20, BrushSizeUnit=0, BrushUseWaterEdge=1,
        BrushWaterEdgeRadius=2, BrushWaterEdgeRadiusUnit=0,
        BrushWaterEdgeBlur=.1, BrushWaterEdgeBlurUnit=2, BrushWaterEdgeAfterDrag=0)
    brush = import_sut(path)
    assert not has_physical_lengths(brush)
    assert brush.watercolor_blur == pytest.approx(.1*300/25.4)
    record = brush.source["length_units"]["fields"]["watercolor_blur"]
    assert not record["active"] and record["source_value"] == .1 and record["source_unit"] == 2
    assert brush.source["variant"]["BrushWaterEdgeBlur"] == .1
    controls = BrushControls(EditorSettings())
    monkeypatch.setattr(brush_controls.QFileDialog,"getOpenFileName",lambda *a,**k:(str(path),""))
    monkeypatch.setattr(brush_controls.QInputDialog,"getDouble",
                        lambda *a,**k:pytest.fail("Dormant blur must not prompt for DPI"))
    monkeypatch.setattr(brush_controls.QMessageBox,"exec",lambda self:0)
    controls._import()
    assert controls.settings.brush_presets[-1]["watercolor_blur"] == brush.watercolor_blur
    controls.deleteLater()
