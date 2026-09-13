"""Large document positions must not change the winding of tiny join pieces."""
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPainterPath, QPolygonF

from comic_editor.ui.shape_outline import _area, positive_path


@pytest.mark.parametrize("offset", [(0., 0.), (200., 23000.), (-9000., 73000.)])
@pytest.mark.parametrize("reverse", [False, True])
def test_tiny_sector_orientation_is_translation_invariant(offset, reverse):
    x, y = offset
    step = 2**-18
    points = [QPointF(x, y), QPointF(x+step, y), QPointF(x, y+step)]
    if reverse:
        points.reverse()
    assert _area(points) == (-1 if reverse else 1)*step**2
    path = QPainterPath()
    path.addPolygon(QPolygonF(points))
    path.closeSubpath()
    assert _area(list(positive_path(path).toSubpathPolygons()[0])) == step**2
