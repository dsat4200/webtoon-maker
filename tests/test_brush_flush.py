"""Small-area compositing must match publishing every full tile."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDefinition,BrushDynamics,BrushInput,default_brushes
from comic_editor.core.brush_raster import RasterBrushStroke,pixels_image
from comic_editor.core.tiles import TileStore
from test_brush_raster import atlas


class FullTileStroke(RasterBrushStroke):
    def _flush(self):
        self._dirty_regions.clear()
        return super()._flush()


@pytest.mark.parametrize("identifier",["round-pen","pencil","dual-pencil","pixel-dots","erase"])
def test_partial_flush_matches_full_tile_with_selection_retracing_and_pressure(identifier):
    brush=next(b for b in default_brushes() if b.id==("round-pen" if identifier=="erase" else identifier))
    brush=replace(brush,size=18,opacity=.6,density=.4,
                  blending_mode="erase" if identifier=="erase" else "normal",
                  dynamics={**brush.dynamics,"opacity":BrushDynamics(pressure=True,minimum=.3)})
    def draw(stroke_type):
        store=TileStore(64)
        pixels=np.empty((64,64,4),np.float32)
        pixels[:]=(.1,.5,.8,.7)
        for key in ((0,0),(1,0),(2,0)):
            store.set_tile("paint",key,pixels_image(pixels))
        mask=np.ones((64,64,4),np.float32)
        mask[:,28:38,3]=.4
        before={}
        stroke=stroke_type(store,"paint",brush,QColor("red"),before,
                           selection_tile=lambda key:pixels_image(mask))
        points=[BrushInput(12,32,.2)]
        points += [BrushInput(x,32+(x%7),.2+.8*(x%29)/28,time=x/120) for x in range(16,168,4)]
        points += [BrushInput(x,33,.8,time=2+(168-x)/120) for x in range(168,24,-4)]
        stroke.begin(points[0])
        for point in points[1:]:
            stroke.add(point)
        stroke.finish()
        return atlas(store,(0,0,192,64)),before
    partial,partial_before=draw(RasterBrushStroke)
    full,full_before=draw(FullTileStroke)
    np.testing.assert_array_equal(partial,full)
    assert partial_before==full_before
