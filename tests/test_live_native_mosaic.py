import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, QSizeF
from PySide6.QtGui import QImage
from test_native_raster_mosaic import pattern, tiles_for, owner, camera, assemble, output, pixels
from test_filtered_raster_contact_feedback import (
    filtered_scene, begin, move, finish, world_point, wait_feedback, native_presentation)
from comic_editor.core.tools import ToolKind


@pytest.mark.parametrize('dpr',[1.,1.5,2.])
@pytest.mark.parametrize('transform',[camera(1.,.5,.5),camera(1.25,3.25,-2.75),camera(.45,2.5,7.25)])
def test_fractional_view_keeps_whole_native_samples_and_display(qapp,dpr,transform):
    native = pattern(1024,768)
    tiles = tiles_for(native)
    before = [pixels(tile.image) for tile in tiles]
    viewport = QSizeF(954,687)
    mosaic = assemble(owner(),tiles,native,transform=transform,viewport=viewport,dpr=dpr)
    assert mosaic is not None
    joined,region = mosaic
    assert pixels(joined)==pixels(native.copy(region.toAlignedRect()))
    assert joined.devicePixelRatio()==1. and joined.sizeInBytes()<=4*1024*1024
    assert pixels(output(joined,region,transform,viewport,dpr))==pixels(
        output(native,QRectF(native.rect()),transform,viewport,dpr))
    assert [pixels(tile.image) for tile in tiles]==before


@pytest.mark.parametrize('tool',[ToolKind.RASTER_PENCIL,ToolKind.RASTER_ERASER,ToolKind.BRUSH])
def test_live_native_mosaic_is_used_and_typed_dirtiness_publishes_once(filtered_scene,wait_scene,qapp,tool):
    canvas,obj = filtered_scene
    canvas.set_tool(tool)
    wait_scene(canvas)
    before = canvas._document_projection.revision
    begin(canvas,world_point(canvas,obj,QPointF(137.,113.)),tool)
    assert canvas._document_projection.revision==before+1
    actual = wait_feedback(canvas,qapp)
    assert canvas._projection_provisional_visible
    # The production optimization belongs to the widget painter. Detached
    # artwork/export QImage painters deliberately keep the ordinary tile path.
    widget_frame = QImage(canvas.size(),QImage.Format_ARGB32_Premultiplied)
    canvas.render(widget_frame)
    assert canvas._native_raster_mosaic is not None
    assert canvas._native_raster_mosaic[1].sizeInBytes()<=4*1024*1024
    assert pixels(actual)==pixels(native_presentation(canvas))
    finish(canvas,tool)
    wait_scene(canvas)
    assert not canvas._projection_provisional_visible
