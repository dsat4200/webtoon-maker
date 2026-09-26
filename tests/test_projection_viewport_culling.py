"""Cull invisible output tiles without changing effect input or screen pixels."""
import pytest
from PySide6.QtCore import QRectF
from test_projection_invalidation import scene
from test_projection_async_publication import frame


@pytest.mark.parametrize("rotation", [0, 22, 45, -61])
@pytest.mark.parametrize("scale", [.5, 1., 2.])
def test_polygon_culling_preserves_every_screen_pixel(scene, rotation, scale):
    canvas, _, _ = scene
    canvas.rotation, canvas.scale = rotation, scale
    canvas._projection_cull_outside_view = False
    expected = frame(canvas)
    canvas._document_projection.clear()
    canvas._projection_cull_outside_view = True
    assert frame(canvas) == expected


def test_recorded_extreme_camera_does_not_render_invisible_chapter_rows(scene):
    canvas, _, _ = scene
    canvas.resize(955, 927)
    canvas.center_x, canvas.center_y = -4571.983550547249, 18899.65961248689
    canvas.scale, canvas.rotation = .053351510286541996, -44.16637539018104
    projection = canvas._document_projection
    bounds = canvas.visible_document_rect().adjusted(-256, -256, 256, 256)
    requests = projection.requests(bounds.intersected(QRectF(0, 0, 1080, 54896)), canvas.scale)
    culled = canvas._projection_visible_requests(requests, 256)
    assert len(culled) < len(requests) * .8
    # Every output tile touching the actual viewport survives filtering.
    visible = canvas._projection_visible_requests(requests, 0)
    assert {request.address for request in visible} <= {request.address for request in culled}
