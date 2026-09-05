"""Coverage regressions for the disappearing-edge reports, not just rectangles."""
import math
import random
from statistics import median
from time import perf_counter

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor, QImage, QMouseEvent, QPainter, QPainterPath, QPointingDevice,
    QTabletEvent, QTransform,
)

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, MirrorModifier, PathContour, PathNode,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.shape_contours import compile_bound, bound_path
from comic_editor.ui.shape_contours import make_custom
from comic_editor.ui.shape_outline import (
    OutlineCache, edge_stroke, outline_mesh, path_key, remove_nodes,
)


SCREENSHOT = [(234, 124), (328, 171), (470, 76), (565, 218),
              (470, 407), (281, 454), (187, 314)]


def polygon(points=SCREENSHOT, closed=True):
    return BoundGeometry.path([PathNode(x=x, y=y) for x, y in points], closed)


def edge_probe(bound, index, inset=2):
    a, b = bound.nodes[index], bound.nodes[(index+1) % len(bound.nodes)]
    mid = (QPointF(*a.position) + QPointF(*b.position))/2
    delta = QPointF(-(b.y-a.y), b.x-a.x)
    delta /= math.hypot(delta.x(), delta.y())
    fill = bound_path(bound)
    return mid+delta*inset if fill.contains(mid+delta*inset) else mid-delta*inset


def rgba(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*4].copy()


def paint_path(path, color="#80000000", size=650):
    image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.fillPath(path, QColor(color))
    painter.end()
    return image


@pytest.mark.parametrize("rotation", range(7))
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("hidden", range(-1, 7))
def test_screenshot_all_other_edges_survive(rotation, reverse, hidden):
    points = SCREENSHOT[rotation:] + SCREENSHOT[:rotation]
    if reverse:
        points = list(reversed(points))
    bound = polygon(points)
    if hidden >= 0:
        bound.nodes[hidden].outline_enabled = False
    mesh = outline_mesh(bound, 20, bound_path(bound))
    for i in range(7):
        assert mesh.contains(edge_probe(bound, i)) == (i != hidden)


@pytest.mark.parametrize("multiplier", [0., .01, .5, 1., 2., 5., 10.])
@pytest.mark.parametrize("index", range(7))
def test_width_edits_never_erase_other_edges(index, multiplier):
    bound = polygon()
    fill = bound_path(bound)
    bound.nodes[index].outline_multiplier = multiplier
    mesh = outline_mesh(bound, 12, fill)
    for i in range(7):
        assert mesh.contains(edge_probe(bound, i))
    assert path_key(bound_path(bound)) == path_key(fill)


def test_mixed_width_randomized_concave_paths_keep_every_enabled_edge():
    randomizer = random.Random(3492)
    for _ in range(300):
        count = randomizer.randrange(4, 16)
        nodes = []
        points = []
        for i in range(count):
            radius = randomizer.uniform(70, 220)
            points.append((300+radius*math.cos(i*2*math.pi/count),
                           300+radius*math.sin(i*2*math.pi/count)))
        for x, y in points:
            nodes.append(PathNode(x=x, y=y, outline_multiplier=randomizer.uniform(.5, 10),
                                  outline_enabled=randomizer.random() > .2))
        bound = BoundGeometry.path(nodes, True)
        mesh = outline_mesh(bound, 8, bound_path(bound))
        for i, node in enumerate(nodes):
            if node.outline_enabled:
                assert mesh.contains(edge_probe(bound, i, .1))


def test_hide_all_consecutive_breaks_and_baseline_zero():
    bound = polygon()
    fill = bound_path(bound)
    for node in bound.nodes:
        node.outline_enabled = False
        node.outline_multiplier = 2.5
    assert outline_mesh(bound, 20, fill).isEmpty()
    for node in bound.nodes[2:5]:
        node.outline_enabled = True
    mesh = outline_mesh(bound, 20, fill)
    assert [mesh.contains(edge_probe(bound, i)) for i in range(7)] == [
        False, False, True, True, True, False, False]
    assert outline_mesh(bound, 0, fill).isEmpty()
    assert all(n.outline_multiplier == 2.5 for n in bound.nodes)


def test_round_ends_at_hidden_edge_are_not_flat_or_square():
    bound = polygon([(50, 50), (150, 50), (250, 50), (250, 200), (50, 200)])
    bound.nodes[1].outline_enabled = False
    mesh = outline_mesh(bound, 20, bound_path(bound))
    assert mesh.contains(QPointF(160, 55))  # Beyond exposed endpoint, round cap.
    assert not mesh.contains(QPointF(168, 68))  # Outside the quarter circle.
    assert not mesh.contains(QPointF(200, 55))


def test_arc_distance_width_interpolation_on_unequal_bezier_speed():
    bound = polygon([(30, 40), (230, 40)], closed=False)
    bound.nodes[0].outgoing = (30, 40)
    bound.nodes[1].incoming = (50, 40)
    path = compile_bound(bound)[0].edges[0].path
    mesh = edge_stroke(path, 10, 30)
    assert mesh.contains(QPointF(130, 59))
    assert not mesh.contains(QPointF(130, 62))


def test_shared_rounded_corner_halves_follow_fill():
    bound = polygon([(50, 50), (250, 50), (250, 250), (50, 250)])
    for node in bound.nodes:
        node.roundness_enabled, node.roundness = True, 40
    contour = compile_bound(bound)[0]
    for edge, following in zip(contour.edges, contour.edges[1:]+contour.edges[:1]):
        assert edge.path.currentPosition() == following.path.pointAtPercent(0)
    mesh = outline_mesh(bound, 15, contour.path)
    assert not mesh.contains(QPointF(51, 51))
    assert mesh.contains(QPointF(150, 55))
    bound.nodes[0].outline_enabled = False
    changed = outline_mesh(bound, 15, bound_path(bound))
    assert not changed.contains(QPointF(150, 55))
    for i in range(1, 4):
        assert changed.contains(edge_probe(bound, i))
    assert path_key(contour.path) == path_key(bound_path(bound))


def test_additional_hole_contour_and_degenerate_edge():
    bound = polygon([(20, 20), (300, 20), (300, 300), (20, 300)])
    hole = polygon([(100, 100), (200, 100), (200, 200), (100, 200)])
    bound.additional_contours = [PathContour(nodes=hole.nodes, closed=True)]
    hole.nodes[0].outline_enabled = False
    mesh = outline_mesh(bound, 15, bound_path(bound))
    assert not mesh.contains(QPointF(150, 95))
    assert mesh.contains(QPointF(205, 150))
    assert not mesh.contains(QPointF(195, 150))
    bound.nodes.insert(1, PathNode(x=20, y=20, outline_multiplier=3))
    mesh = outline_mesh(bound, 15, bound_path(bound))
    assert mesh.contains(QPointF(150, 25))


@pytest.mark.parametrize("start", ["point", "square", "round"])
@pytest.mark.parametrize("end", ["point", "square", "round"])
def test_open_outline_preserves_endpoint_caps_and_round_breaks(start, end):
    bound = polygon([(60, 100), (160, 100), (260, 100), (360, 100)], closed=False)
    core = CanvasWidget.open_shape_mesh(bound, 20, start_cap=start, end_cap=end)
    bound.nodes[1].outline_enabled = False
    ring = outline_mesh(bound, 10, QPainterPath(), core=core, base_width=20,
                        start_cap=start, end_cap=end)
    assert ring.contains(QPointF(100, 115))
    assert ring.contains(QPointF(166, 113))
    assert not ring.contains(QPointF(210, 115))
    assert ring.contains(QPointF(320, 115))
    assert ring.contains(QPointF(48, 100))
    assert ring.contains(QPointF(372, 100))
    assert ring.contains(QPointF(42, 118)) == (start == "square")
    assert ring.contains(QPointF(378, 118)) == (end == "square")
    for node in bound.nodes:
        node.outline_multiplier = 0
    assert outline_mesh(bound, 10, QPainterPath(), core=core, base_width=20).isEmpty()


def test_overlap_coverage_is_painted_once():
    bound = polygon()
    bound.nodes[1].outline_multiplier = 3
    image = paint_path(outline_mesh(bound, 20, bound_path(bound)))
    assert rgba(image)[:, 3::4].max() == 128


def test_cache_geometry_reuse_incident_edges_and_budget():
    cache = OutlineCache()
    bound = polygon()
    fill = bound_path(bound)
    outline_mesh(bound, 20, fill, cache=cache)
    assert cache.builds["contours"] == 1
    assert cache.builds["edge"] == 1  # One native closed run.
    bound.nodes[3].outline_multiplier = 2
    outline_mesh(bound, 20, fill, cache=cache)
    assert cache.builds["contours"] == 1
    assert cache.builds["edge"] == 4  # Two incident edges and the remaining run.
    bound.nodes[3].outline_multiplier = 2.5
    outline_mesh(bound, 20, fill, cache=cache)
    assert cache.builds["edge"] == 6  # The constant run is reused.
    bound.nodes[3].outline_enabled = False
    outline_mesh(bound, 20, fill, cache=cache)
    assert cache.builds["edge"] == 8  # Only the two new exposed caps change.


def test_connected_variable_widths_do_not_grow_a_cap_blob():
    bound = polygon([(50, 50), (150, 50), (250, 50), (250, 300), (50, 300)])
    bound.nodes[1].outline_multiplier = 10
    mesh = outline_mesh(bound, 8, bound_path(bound))
    assert mesh.contains(QPointF(80, 78))
    assert not mesh.contains(QPointF(80, 85))


def test_small_cache_never_exceeds_budget():
    bound = polygon()
    fill = bound_path(bound)
    tiny = OutlineCache(budget=8192)
    for width in range(1, 30):
        outline_mesh(bound, width, fill, cache=tiny)
        assert tiny.bytes <= tiny.budget
    tiny.clear()
    assert tiny.bytes == 0 and not tiny.entries


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=600)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 650, 600))
    page.fill_color, page.border_width = None, 0
    layer = chapter.add_layer(page.layer_id, "Outline", polygon())
    layer.fill_color, layer.border_color, layer.border_width = None, "#80000000", 20
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 800)
    canvas.set_document(chapter, TileStore(), reset_view=False)
    canvas.scale = 1
    canvas.set_selection("layer", layer.layer_id)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    yield canvas, layer
    canvas.deleteLater()


def render(canvas, image=None, rect=None):
    if image is None:
        image = QImage(canvas.chapter.width, canvas.chapter.height,
                       QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image, rect) if rect is not None else canvas.render_preview(image)
    return image


def test_full_partial_render_and_color_only_cache_reuse(scene):
    canvas, layer = scene
    before = render(canvas)
    builds = dict(canvas._outline_cache.builds)
    layer.border_color = "#8000FF00"
    render(canvas)
    assert canvas._outline_cache.builds == builds
    layer.bound.nodes[1].outline_multiplier = 3
    layer.bound.nodes[4].outline_enabled = False
    partial = render(canvas, before, QRect(160, 60, 425, 415))
    assert np.array_equal(rgba(partial), rgba(render(canvas)))
    canvas.set_document(ChapterDocument(), TileStore())
    assert canvas._outline_cache.bytes == 0


def test_edited_outline_preview_png_and_rendered_alpha_mask_agree(scene, tmp_path):
    from comic_editor.core.models import ToneMask
    canvas, layer = scene
    layer.bound.nodes[0].outline_multiplier = 4
    layer.bound.nodes[2].outline_enabled = False
    preview = render(canvas)
    path = tmp_path / "outline.png"
    assert preview.save(str(path), "PNG")
    assert np.array_equal(rgba(preview), rgba(QImage(str(path))))
    mask = ToneMask(name="Outline alpha", contributors=[("layer", layer.layer_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    field = canvas.render_tone_mask_field(mask.mask_id, preview.width(), preview.height(),
        QTransform(), QRectF(0, 0, preview.width(), preview.height()))
    alpha = rgba(preview)[:, 3::4]/255.
    assert np.max(np.abs(field-alpha)) <= 1/255.


def test_compound_hidden_source_boundary_and_style_cache(scene):
    canvas, root = scene
    root.bound = polygon([(20, 20), (220, 20), (220, 220), (20, 220)])
    root.compound_enabled = True
    root.border_width = 10
    child = canvas.chapter.add_layer(root.layer_id, "Child", polygon(
        [(180, 20), (380, 20), (380, 220), (180, 220)]))
    child.border_width = 10
    child.bound.nodes[1].outline_enabled = False
    canvas._clear_compound_path_cache()
    image = render(canvas)
    assert image.pixelColor(375, 120).alpha() == 0
    assert image.pixelColor(300, 25).alpha() == 128
    assert image.pixelColor(210, 120).alpha() == 0  # Removed interior seam.
    assert image.pixelColor(385, 120).alpha() == 0  # Inside placement.
    builds = canvas._outline_cache.builds["attribution"]
    fill = canvas._compound_path_cache[root.layer_id]
    child.bound.nodes[0].outline_multiplier = 2
    child.bound.nodes[1].outline_enabled = True
    canvas.documentChanged.emit(canvas.entity_world_rect("layer", root.layer_id))
    assert canvas._compound_path_cache[root.layer_id] is fill
    image = render(canvas)
    assert image.pixelColor(375, 120).alpha() == 128
    assert canvas._outline_cache.builds["attribution"] == builds


def test_compound_variable_width_join_matches_standalone(scene):
    canvas, layer = scene
    layer.bound = polygon([(50, 50), (150, 50), (250, 50), (250, 300), (50, 300)])
    layer.bound.nodes[1].outline_multiplier = 10
    layer.border_width = 8
    before = rgba(render(canvas))
    layer.compound_enabled = True
    assert np.array_equal(before, rgba(render(canvas)))


@pytest.mark.parametrize("width", [0, 3, 12])
def test_primitive_ellipse_compound_retains_source_baseline(scene, width):
    canvas, root = scene
    root.bound = polygon([(20, 20), (80, 20), (80, 80), (20, 80)])
    root.compound_enabled = True
    root.border_width = 20
    child = canvas.chapter.add_layer(root.layer_id, "Ellipse",
                                     BoundGeometry.circle(300, 200, 100))
    child.border_width = width
    image = render(canvas)
    assert (image.pixelColor(397, 200).alpha() > 0) == (width >= 3)
    assert (image.pixelColor(390, 200).alpha() > 0) == (width >= 12)
    assert image.pixelColor(381, 200).alpha() == 0


@pytest.mark.parametrize("cap", ["round", "square", "point"])
def test_open_compound_surface_styles_and_cache(scene, cap):
    canvas, root = scene
    root.bound = polygon([(20, 20), (80, 20), (80, 80), (20, 80)])
    root.compound_enabled = True
    root.border_width = 6
    child = canvas.chapter.add_layer(root.layer_id, "Ribbon", polygon(
        [(120, 180), (220, 180), (320, 180), (420, 180)], closed=False),
        layer_kind="open_shape")
    child.shape_style.base_thickness = 60
    child.shape_style.start_cap = child.shape_style.end_cap = cap
    child.border_width = 6
    child.bound.nodes[1].outline_enabled = False
    image = render(canvas)
    for y in [152, 207]:
        assert image.pixelColor(170, y).alpha() == 128
        assert image.pixelColor(270, y).alpha() == 0
        assert image.pixelColor(370, y).alpha() == 128
    assert image.pixelColor(370, 164).alpha() == 0
    fill = path_key(canvas.layer_effective_path(root.layer_id))
    builds = dict(canvas._outline_cache.builds)
    child.bound.nodes[2].outline_multiplier = child.bound.nodes[3].outline_multiplier = 3
    image = render(canvas)
    assert image.pixelColor(370, 164).alpha() == 128
    assert image.pixelColor(270, 152).alpha() == 0
    assert path_key(canvas.layer_effective_path(root.layer_id)) == fill
    for key in ["ribbon_surface", "core", "attribution", "contours"]:
        assert canvas._outline_cache.builds[key] == builds[key]


@pytest.mark.parametrize("reflect", [False, True])
def test_curved_open_compound_source_attribution_under_transform(reflect):
    from comic_editor.ui.shape_outline import core_mesh
    from comic_editor.ui.shape_outline_compound import compound_outline, ribbon_source
    bound = polygon([(100, 180), (220, 100), (350, 180)], closed=False)
    bound.nodes[0].point_type = bound.nodes[1].point_type = "bezier"
    bound.nodes[0].outgoing = (100, 100)
    bound.nodes[1].incoming = (160, 100)
    bound.nodes[0].width_multiplier = .5
    bound.nodes[1].width_multiplier = 1.5
    bound.nodes[0].outline_enabled = False
    mapping = QTransform().translate(500, 40).rotate(17).scale(-1 if reflect else 1, 1.3)
    cache = OutlineCache()
    core = core_mesh(bound, 40, cache=cache)
    source = ribbon_source(bound, 6, mapping, 40, "round", "round", cache)
    mesh = compound_outline(mapping.map(core), 6, [source], cache)
    # The midpoint of each actual surface span knows its source edge, even
    # though both sides of the ribbon are far from the centerline.
    checked = {True: 0, False: 0}
    for contour, styles in zip(source.bound.iter_contours(), source.edge_styles):
        for index, (a, b, enabled) in enumerate(styles):
            first, last = contour.nodes[index], contour.nodes[(index+1) % len(contour.nodes)]
            if math.dist(first.position, last.position) < 2:
                continue
            point = (QPointF(*first.position)+QPointF(*last.position))/2
            normal = QPointF(-(last.y-first.y), last.x-first.x)
            normal /= math.hypot(normal.x(), normal.y())
            probe = point+normal*1.5 if core.contains(point+normal*1.5) else point-normal*1.5
            # Avoid endpoint/ownership switches where a neighbor's cap is valid.
            if min(math.dist(probe.toTuple(), n.position) for n in bound.nodes) < 45:
                continue
            assert mesh.contains(mapping.map(probe)) == enabled
            checked[enabled] += 1
    assert min(checked.values()) > 0


def test_mirror_subtraction_attribution_retains_hidden_edge(scene):
    canvas, root = scene
    root.bound = polygon([(10, 10), (300, 10), (300, 300), (10, 300)])
    root.compound_enabled = True
    root.border_width = 10
    child = canvas.chapter.add_layer(root.layer_id, "Cut", polygon(
        [(40, 50), (80, 50), (80, 150), (40, 150)]))
    child.compound_operation = "ignore"
    child.border_width = 10
    child.bound.nodes[1].outline_enabled = False
    mirror = MirrorModifier(axis_start=(150, 0), axis_end=(150, 300),
                            compound_operation="subtract")
    canvas.chapter.add_modifier(mirror, [("layer", child.layer_id)])
    image = render(canvas)
    assert image.pixelColor(215, 100).alpha() == 0  # Reflected hidden right edge.
    assert image.pixelColor(265, 100).alpha() == 128
    assert image.pixelColor(250, 100).alpha() == 0  # Inside the hole.
    mirror.muted = True
    canvas.documentChanged.emit(canvas.entity_world_rect("layer", root.layer_id))
    assert render(canvas).pixelColor(265, 100).alpha() == 0


@pytest.mark.parametrize("projective", [False, True])
@pytest.mark.parametrize("stylus", [False, True])
def test_transformed_handle_release_uses_final_position_one_undo(scene, projective, stylus):
    canvas, layer = scene
    if projective:
        layer.transform_frame = (180, 70, 390, 390)
        layer.transform_quad = [(160, 80), (540, 120), (590, 530), (190, 470)]
    else:
        layer.transform_frame = (180, 70, 390, 390)
        layer.transform_quad = [(520, 60), (520, 450), (130, 450), (130, 60)]
    node = layer.bound.nodes[0]
    canvas._selected_shape_node_id = node.node_id
    display = canvas._shape_handle_display_transform(node)
    origin = display.map(QPointF(*node.position))
    handle = display.map(canvas._shape_gizmo_positions(layer.bound, node)["outline_width"])
    direction = (handle-origin)/70
    assert math.hypot((handle-origin).x(), (handle-origin).y()) == pytest.approx(70)
    def send(kind, pos):
        if stylus:
            types = {"press": QEvent.TabletPress, "move": QEvent.TabletMove, "release": QEvent.TabletRelease}
            event = QTabletEvent(types[kind], QPointingDevice.primaryPointingDevice(),
                pos, pos, 0 if kind == "release" else .7, 0., 0., 0., 0., 0.,
                Qt.NoModifier, Qt.LeftButton if kind != "move" else Qt.NoButton,
                Qt.NoButton if kind == "release" else Qt.LeftButton)
        else:
            types = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove, "release": QEvent.MouseButtonRelease}
            event = QMouseEvent(types[kind], pos, pos, Qt.NoButton if kind == "move" else Qt.LeftButton,
                                Qt.NoButton if kind == "release" else Qt.LeftButton, Qt.NoModifier)
        QCoreApplication.sendEvent(canvas, event)
    send("press", handle)
    assert canvas._active_shape_control == "outline_width"
    for offset in range(1, 20):
        send("move", handle+direction*offset)
    assert node.outline_multiplier == 1  # Pending packets coalesce.
    send("release", handle+direction*30)  # No move packet at release position.
    assert canvas.chapter.layers[layer.layer_id].bound.nodes[0].outline_multiplier == pytest.approx(4)
    assert canvas._outline_pending_point is None
    canvas.command_stack.undo()
    assert canvas.chapter.layers[layer.layer_id].bound.nodes[0].outline_multiplier == 1
    canvas.command_stack.redo()
    assert canvas.chapter.layers[layer.layer_id].bound.nodes[0].outline_multiplier == pytest.approx(4)


def test_shift_toggle_hidden_edge_still_hittable_and_undo(scene):
    canvas, layer = scene
    point = (QPointF(*SCREENSHOT[0]) + QPointF(*SCREENSHOT[1]))/2
    before = layer.bound.to_dict()
    assert canvas._begin_shape_edit(point, modifiers=Qt.ShiftModifier)
    assert not layer.bound.nodes[0].outline_enabled
    assert len(layer.bound.nodes) == 7
    assert canvas._begin_shape_edit(point, modifiers=Qt.ShiftModifier)
    assert layer.bound.nodes[0].outline_enabled
    assert layer.bound.to_dict() == before
    canvas.command_stack.undo()
    assert not canvas.chapter.layers[layer.layer_id].bound.nodes[0].outline_enabled


def test_outline_benchmark():
    cache = OutlineCache()
    bound = polygon()
    fill = bound_path(bound)
    times = []
    for index in range(80):
        bound.nodes[2].outline_multiplier = 1 + index/50
        start = perf_counter()
        outline_mesh(bound, 20, fill, cache=cache)
        times.append((perf_counter()-start)*1000)
    print(f"Screenshot warmed median={median(times[5:]):.3f}ms; "
          f"max={max(times[5:]):.3f}ms; cache={cache.bytes}; builds={cache.builds}")
    # Timing is reported, not a flaky correctness assertion on shared CI.
    assert cache.builds["contours"] == 1


@pytest.mark.parametrize("count", [100, 1000])
def test_large_outline_edit_benchmark(scene, count):
    canvas, layer = scene
    layer.bound = polygon([(350+250*math.cos(i*2*math.pi/count),
                            300+250*math.sin(i*2*math.pi/count)) for i in range(count)])
    layer.border_width = 10
    image = render(canvas)
    times = []
    for index in range(18):
        layer.bound.nodes[2].outline_multiplier = 1+index/30
        start = perf_counter()
        render(canvas, image)
        times.append((perf_counter()-start)*1000)
    print(f"{count} anchors full preview median={median(times[3:]):.3f}ms "
          f"max={max(times[3:]):.3f}ms cache={canvas._outline_cache.bytes} bytes")
    assert canvas._outline_cache.bytes <= canvas._outline_cache.budget


def test_large_compound_style_edit_benchmark(scene):
    canvas, root = scene
    root.bound = polygon([(10, 10), (450, 10), (450, 500), (10, 500)])
    root.compound_enabled = True
    sources = []
    for i in range(20):
        child = canvas.chapter.add_layer(root.layer_id, f"Cut {i}", polygon(
            [(40+(i%4)*100, 40+(i//4)*85), (95+(i%4)*100, 40+(i//4)*85),
             (95+(i%4)*100, 85+(i//4)*85), (40+(i%4)*100, 85+(i//4)*85)]))
        child.compound_operation = "subtract"
        child.border_width = 5
        sources.append(child)
    start = perf_counter()
    image = render(canvas)
    cold = (perf_counter()-start)*1000
    times = []
    for i in range(15):
        sources[5].bound.nodes[0].outline_multiplier = 1+i/20
        canvas.documentChanged.emit(canvas.entity_world_rect("layer", root.layer_id))
        start = perf_counter()
        render(canvas, image)
        times.append((perf_counter()-start)*1000)
    print(f"20-cutout compound cold={cold:.3f}ms warmed={median(times[3:]):.3f}ms "
          f"attribution builds={canvas._outline_cache.builds['attribution']} "
          f"cache={canvas._outline_cache.bytes} bytes")
    assert canvas._outline_cache.builds["attribution"] == 1


def test_ellipse_conversion_toggle_preserves_curve_and_undo(scene):
    canvas, layer = scene
    layer.bound = BoundGeometry.circle(250, 250, 100)
    original = bound_path(layer.bound)
    point = original.pointAtPercent(.125)
    requested = []
    canvas.primitiveConversionRequested.connect(requested.append)
    assert canvas._begin_shape_edit(point, modifiers=Qt.ShiftModifier)
    assert requested == ["ellipse"]
    canvas.resolve_primitive_conversion(False)
    assert layer.bound.primitive == "ellipse"
    assert canvas._begin_shape_edit(point, modifiers=Qt.ShiftModifier)
    canvas.resolve_primitive_conversion(True)
    assert layer.bound.primitive == "custom"
    assert sum(not n.outline_enabled for n in layer.bound.nodes) == 1
    converted = bound_path(layer.bound)
    assert original == converted
    canvas.command_stack.undo()
    assert canvas.chapter.layers[layer.layer_id].bound.primitive == "ellipse"


def test_split_interpolates_arc_width_and_visibility_then_join():
    bound = polygon([(30, 40), (230, 40)], closed=False)
    first, last = bound.nodes
    first.point_type = last.point_type = "bezier"
    first.outgoing, last.incoming = (30, 40), (50, 40)
    first.outline_multiplier, last.outline_multiplier = 1, 3
    first.outline_enabled = False
    inserted = CanvasWidget._split_shape_segment(bound, 0, .5)
    assert inserted.outline_multiplier == pytest.approx(1+2*(inserted.x-30)/200)
    assert not inserted.outline_enabled
    bound.nodes.insert(1, inserted)
    last.outline_enabled = True
    remove_nodes(next(bound.iter_contours()), {inserted.node_id})
    assert not first.outline_enabled


def test_hidden_curved_compound_boundary_at_high_zoom(scene):
    canvas, root = scene
    root.bound = polygon([(50, 50), (200, 50), (200, 350), (50, 350)])
    root.compound_enabled = True
    child = canvas.chapter.add_layer(root.layer_id, "Curve", BoundGeometry.circle(250, 200, 100))
    make_custom(child.bound)
    child.bound.nodes[0].outline_enabled = False
    child.border_width = 8
    effective = canvas.layer_effective_path(root.layer_id)
    for tolerance in [.125, .03, .0078]:
        outline = canvas._compound_outline_mesh(root, effective, tolerance)
        edge = compile_bound(child.bound)[0].edges[0].path
        for fraction in [.2, .4, .6, .8]:
            point = edge.pointAtPercent(fraction)
            toward = QPointF(250, 200)-point
            point += toward/math.hypot(toward.x(), toward.y())*2
            assert not outline.contains(point)


def test_open_additional_contours_and_visual_bounds_cover_wide_square_caps(scene):
    from comic_editor.core.assets import entity_visual_bounds
    canvas, layer = scene
    layer.layer_kind = "open_shape"
    layer.bound = polygon([(100, 100), (200, 200)], closed=False)
    layer.bound.additional_contours = [PathContour(
        nodes=[PathNode(x=300, y=100), PathNode(x=400, y=200)], closed=False)]
    layer.shape_style.base_thickness = 20
    layer.shape_style.start_cap = layer.shape_style.end_cap = "square"
    layer.border_width = 10
    layer.bound.nodes[0].outline_multiplier = 4
    image = render(canvas)
    bounds = entity_visual_bounds(canvas.chapter, canvas.tiles, "layer", layer.layer_id)
    painted = np.nonzero(rgba(image)[:, 3::4])
    assert bounds.adjusted(-1, -1, 1, 1).contains(QPointF(painted[1].min(), painted[0].min()))
    assert bounds.adjusted(-1, -1, 1, 1).contains(QPointF(painted[1].max(), painted[0].max()))
    assert image.pixelColor(350, 150).alpha() > 0
    assert image.pixelColor(250, 150).alpha() == 0


def test_outline_rasterization_is_pixel_equivalent_and_undo_restores(scene):
    from comic_editor.ui.baking import rasterize
    canvas, layer = scene
    layer.bound.nodes[1].outline_multiplier = 3
    layer.bound.nodes[3].outline_enabled = False
    before = rgba(render(canvas))
    rasterize(canvas, "layer", layer.layer_id)
    after = rgba(render(canvas))
    assert np.array_equal(before, after)
    canvas.command_stack.undo()
    assert np.array_equal(before, rgba(render(canvas)))


def test_screen_space_hidden_segment_hits_and_double_tap_reset(scene):
    canvas, layer = scene
    node = layer.bound.nodes[0]
    node.outline_multiplier = 3
    canvas._selected_shape_node_id = node.node_id
    handle = canvas.document_to_widget(canvas._shape_gizmo_positions(layer.bound, node)["outline_width"])
    for _ in range(2):
        for kind, pressure, buttons in [(QEvent.TabletPress, .7, Qt.LeftButton),
                                       (QEvent.TabletRelease, 0., Qt.NoButton)]:
            event = QTabletEvent(kind, QPointingDevice.primaryPointingDevice(),
                handle, handle, pressure, 0., 0., 0., 0., 0., Qt.NoModifier, Qt.LeftButton, buttons)
            QCoreApplication.sendEvent(canvas, event)
    assert canvas.chapter.layers[layer.layer_id].bound.nodes[0].outline_multiplier == 1
    canvas.command_stack.undo()
    assert canvas.chapter.layers[layer.layer_id].bound.nodes[0].outline_multiplier == 3


def test_outline_edit_with_modifier_overlay_and_widget_paint(scene, qapp):
    canvas, layer = scene
    node = layer.bound.nodes[0]
    canvas._selected_shape_node_id = node.node_id
    handle = canvas._shape_gizmo_positions(layer.bound, node)["outline_width"]
    assert canvas._begin_shape_edit(handle)
    image = QImage(900, 800, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        # Qt can swallow Python paint exceptions, leaving an active painter
        # that crashes a later widget. Exercise these overlays directly too.
        canvas._draw_focal_modifier_handles(painter)
        canvas._draw_selected_shape_gizmos(painter, layer.bound, node, layer.shape_style)
    finally:
        painter.end()
    canvas.show()
    qapp.processEvents()
    assert not canvas.grab().isNull()
    canvas._tool_release()
    canvas.close()


@pytest.mark.parametrize("scale", [1, 4, 16])
def test_adaptive_variable_curve_stays_within_quarter_output_pixel(scale):
    from scipy.spatial import cKDTree
    path = QPainterPath(QPointF(40, 100))
    path.cubicTo(QPointF(60, -50), QPointF(230, 300), QPointF(270, 100))
    coarse = edge_stroke(path, 8, 30, tolerance=.125/scale)
    fine = edge_stroke(path, 8, 30, tolerance=.001/scale)
    def boundary(path):
        # Normalize winding overlaps before measuring the coverage boundary.
        path = path.simplified()
        points = []
        for poly in path.toSubpathPolygons():
            for a, b in zip(poly, list(poly)[1:]):
                count = max(1, math.ceil(math.dist(a.toTuple(), b.toTuple())*scale/.05))
                points.extend((a+(b-a)*(i/count)).toTuple() for i in range(count))
        return np.array(points)*scale
    a, b = boundary(coarse), boundary(fine)
    assert cKDTree(b).query(a)[0].max() <= .25
    assert cKDTree(a).query(b)[0].max() <= .25
