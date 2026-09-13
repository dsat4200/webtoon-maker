"""A short spiky boundary can still produce a complex attributed outline."""
import pytest
from PySide6.QtCore import QPointF, QRectF

from comic_editor.core.models import BoundGeometry, ChapterDocument, PathNode, ScreamModifier, ShapeStyle
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.shape_outline_compound import compound_outline


@pytest.fixture
def spiky_bubble(qapp):
    # Six source anchors reproduce a failed Qt boolean normalization: at the
    # normal viewport tolerance the outline used to cover the entire bubble.
    chapter = ChapterDocument(height=23200, background="#FFFF004B")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 23200), y=120)
    page.fill_color, page.border_width = None, 0
    x, y = 173.80161300629106, 22648.10418109867
    bound = BoundGeometry.path([
        PathNode(x=x+dx, y=y+dy, roundness=210, roundness_enabled=True)
        for dx, dy in [(0, 4), (396, 0), (400, 250), (10, 272)]
    ], closed=True)
    root = chapter.add_layer(page.layer_id, "Bubble", bound,
        style=ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF000000", outline_thickness=5))
    root.compound_enabled = True
    root.transform_frame = (x, y, 400., 272.)
    root.transform_quad = [
        (669.8441196143405, 8759.548002675503),
        (1038.1558803856597, 8759.548002675503),
        (1038.1558803856597, 9009.999999999998),
        (669.8441196143405, 9009.999999999998),
    ]
    chapter.add_modifier(ScreamModifier(height=47, width=69, roundness=0), [("layer", root.layer_id)])
    tail = chapter.add_layer(root.layer_id, "Tail", BoundGeometry.path([
        PathNode(x=x+120, y=y+151, width_multiplier=10),
        PathNode(x=x-169, y=y+186, point_type="bezier", incoming=(x-69, y+343),
                 handles_locked=False, width_multiplier=.1, outline_multiplier=2.58013763281118),
    ]), layer_kind="open_shape",
        style=ShapeStyle(primary_color="#FFFFFFFF", base_thickness=12,
                         outline_color="#FF000000", outline_thickness=4))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore())
    center = canvas.layer_world_transform(root.layer_id).map(QPointF(374, 22784))
    canvas.center_x, canvas.center_y = center.x(), center.y()
    yield canvas, root, tail
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


@pytest.mark.parametrize("scale", [.5, .75, 1., 1.5, 2.])
def test_visible_tail_preserves_white_scream_interior_at_every_viewport_scale(spiky_bubble, scale):
    canvas, root, tail = spiky_bubble
    canvas.scale = scale
    before = root.to_dict(), tail.to_dict()
    probe = canvas.document_to_widget(
        canvas.layer_world_transform(root.layer_id).map(QPointF(400, 22780))).toPoint()
    for visible in (True, False, True):
        tail.visible = visible
        canvas.documentChanged.emit(QRectF())
        image = canvas.grab().toImage()
        assert image.pixelColor(probe).name() == "#ffffff"
    assert (root.to_dict(), tail.to_dict()) == before


@pytest.mark.parametrize("tolerance", [.25, .125, .0625])
def test_geometric_outline_fallback_preserves_interior_and_border(spiky_bubble, tolerance):
    canvas, root, _ = spiky_bubble
    fill = canvas.layer_effective_path(root.layer_id)
    sources = canvas._compound_outline_mesh(root, fill, tolerance, sources_only=True)
    coverage = compound_outline(fill, root.border_width, sources, canvas._outline_cache, tolerance)
    for position in [(320, 22750), (360, 22750), (400, 22780), (450, 22800)]:
        point = QPointF(*position)
        assert fill.contains(point)
        assert not coverage.contains(point)
    # A point two local units inside an upper spike still receives its black
    # outline, so avoiding the filled interior cannot simply discard coverage.
    assert coverage.contains(QPointF(325.77080471135787, 22628.871997727492))
