"""A rounded tapered tail must not expose its internal join-sector seams."""
import math

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import BoundGeometry, ChapterDocument, PathNode, ShapeStyle
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.shape_outline import core_mesh, round_join
from comic_editor.ui.shape_outline_compound import compound_outline, ribbon_source


def tail_bound(local=False, reverse=False):
    # Regression geometry from a bent speech-bubble tail. Its non-cardinal
    # corner and large document coordinates expose nearly coincident seams.
    values = [
        (360., 22800., 10., 1.),
        (437.95839514474926, 23085.813649243533, 3.2, 1.3566307516629306),
        (646.9583951447493, 23022.813649243533, .1, 1.5223502053141933),
    ]
    if reverse:
        values.reverse()
    return BoundGeometry.path([
        PathNode(x=x-(200 if local else 0), y=y-(22720 if local else 0),
                 width_multiplier=width, outline_multiplier=outline)
        for x, y, width, outline in values
    ])


def directions(bound):
    a, b, c = [QPointF(*node.position) for node in bound.nodes]
    incoming, outgoing = b-a, c-b
    incoming /= math.hypot(incoming.x(), incoming.y())
    outgoing /= math.hypot(outgoing.x(), outgoing.y())
    cross = incoming.x()*outgoing.y()-incoming.y()*outgoing.x()
    outward = QPointF(-incoming.y(), incoming.x()) * (-1 if cross > 0 else 1)
    return b, incoming, outgoing, outward


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_round_sector_uses_exact_shared_strip_endpoints(local, reverse):
    bound = tail_bound(local, reverse)
    point, incoming, outgoing, first_normal = directions(bound)
    cross = incoming.x()*outgoing.y()-incoming.y()*outgoing.x()
    last_normal = QPointF(-outgoing.y(), outgoing.x()) * (-1 if cross > 0 else 1)
    radius = 19.2
    sector = round_join(point, radius, incoming, outgoing)
    elements = [QPointF(sector.elementAt(i).x, sector.elementAt(i).y)
                for i in range(sector.elementCount())]
    for endpoint in (point+first_normal*radius, point+last_normal*radius):
        assert min(math.dist(endpoint.toTuple(), value.toTuple()) for value in elements) < 1e-10


@pytest.mark.parametrize("cap", ["round", "point", "square"])
@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_tapered_compound_tail_has_no_interior_join_outline(cap, local, reverse):
    bound = tail_bound(local, reverse)
    core = core_mesh(bound, 12, start_cap=cap, end_cap=cap)
    point, _, _, outward = directions(bound)
    probe = point+outward*8
    assert core.contains(probe)
    # No part of the normalized surface should run radially to the center.
    boundary = [core.elementAt(index) for index in range(core.elementCount())]
    assert min(math.dist(point.toTuple(), (value.x, value.y)) for value in boundary) > 17
    source = ribbon_source(bound, 4, QTransform(), 12, cap, cap)
    mesh = compound_outline(core, 4, [source])
    assert not mesh.contains(probe)
    # The real outer edge still carries its varying source outline width.
    assert mesh.contains(point+outward*17)


def test_actual_compound_bubble_render_has_white_interior_at_tail_bend(qapp):
    chapter = ChapterDocument(height=23200, background="#FFFF004B")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 23200))
    page.fill_color, page.border_width = None, 0
    root = chapter.add_layer(page.layer_id, "Bubble", BoundGeometry.circle(360, 22800, 100),
        style=ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF000000", outline_thickness=4))
    root.compound_enabled = True
    child = chapter.add_layer(root.layer_id, "Tail", tail_bound(), layer_kind="open_shape",
        style=ShapeStyle(primary_color="#FFFFFFFF", base_thickness=12,
                         outline_color="#FF000000", outline_thickness=4))
    before = child.to_dict()
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(500, 440)
    canvas.set_document(chapter, TileStore())
    try:
        image = QImage(500, 440, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(image, source_rect=QRectF(200, 22700, 500, 440))
        point, _, _, outward = directions(child.bound)
        probe = point+outward*8
        assert image.pixelColor(round(probe.x()-200), round(probe.y()-22700)).name() == "#ffffff"
        assert child.to_dict() == before
    finally:
        canvas._effect_jobs.cancel()
        canvas.deleteLater()
