"""Moving selected objects keeps unrelated offscreen artwork out of drag frames."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, DistortModifier, ImageObject,
    OutlineModifier, PosterizeModifier, RasterObject, TextObject,
    VectorDrawingObject, VectorStroke, VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.document_projection import (
    ProjectionAddress, ProjectionRequest, ProjectionTile,
)


@pytest.fixture
def scene(qapp, text_outline_font_family):
    chapter = ChapterDocument(height=4000)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 4000))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False, snap_to_grid=False))
    canvas.resize(480, 480)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 240, 240, 1
    distant = chapter.add_layer(page.layer_id, "Distant", BoundGeometry.rectangle(0, 1500, 600, 600))
    distant.fill_color, distant.border_width = "#00ff00", 2
    unrelated = chapter.add_object(distant.layer_id, ImageObject(
        x=100, y=1600, pixel_width=40, pixel_height=40, show_on_top=True))
    image = QImage(40, 40, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("blue"))
    canvas.images.put_decoded(unrelated.object_id, "sample.png", b"", image)
    yield canvas, chapter, page, unrelated, text_outline_font_family
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def add_object(scene, kind, x=700):
    canvas, chapter, page, _unrelated, font = scene
    parent = chapter.add_layer(page.layer_id, "Offscreen parent",
                               BoundGeometry.rectangle(x-20, 650, 180, 180))
    parent.fill_color, parent.border_width = None, 0
    if kind == "raster":
        obj = RasterObject(x=x, y=700, interaction_rect=(0, 0, 40, 40))
    elif kind == "image":
        obj = ImageObject(x=x, y=700, pixel_width=40, pixel_height=40)
    elif kind == "vector":
        obj = VectorDrawingObject(x=x, y=700, strokes=[VectorStroke(
            color="#ff0000", points=[VectorStrokePoint(x=8, y=20, width=16),
                                      VectorStrokePoint(x=32, y=20, width=16)])])
    else:
        obj = TextObject(x=x, y=700, width=80, height=40, text="Move",
                         font_family=font, layout_mode="free", margin=0)
    obj.ignore_parent_mask = True
    chapter.add_object(parent.layer_id, obj)
    if kind == "raster":
        canvas.tiles.paint_dab(obj.object_id, QPointF(20, 20), 30, QColor("red"))
    elif kind == "image":
        image = QImage(40, 40, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor("red"))
        canvas.images.put_decoded(obj.object_id, "moving.png", b"", image)
    return obj


def render(canvas, culling=True):
    prepare = canvas._render_bounds.prepare
    if not culling:
        def disabled():
            prepare()
            canvas._render_bounds.enabled = False
        canvas._render_bounds.prepare = disabled
    try:
        canvas._invalidate_scene_cache()
        canvas._ensure_scene_cache()
        return np.frombuffer(canvas._scene_cache.constBits(), np.uint8).copy()
    finally:
        canvas._render_bounds.prepare = prepare


@pytest.mark.parametrize("kind", ["raster", "image", "vector", "text"])
@pytest.mark.parametrize("projective", [False, True])
def test_object_preview_moves_cached_offscreen_artwork_into_view(scene, monkeypatch, kind, projective):
    canvas, _chapter, _page, unrelated, _font = scene
    obj = add_object(scene, kind)
    initial = render(canvas)
    cached = canvas._render_bounds.entity_bounds("object", obj.object_id)
    assert cached.left() > 480
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas._transform_start_quad = canvas.object_world_quad(obj.object_id)
    canvas._transform_preview_quad = [(80, 80), (180, 80),
                                      (165 if projective else 180, 140), (80, 140)]
    expected = render(canvas, False)
    assert not np.array_equal(initial, expected)
    seen = []
    original = canvas._render_object_content
    monkeypatch.setattr(canvas, "_render_object_content", lambda painter, value, visible:
                        (seen.append(value.object_id), original(painter, value, visible))[1])
    assert np.array_equal(render(canvas), expected)
    assert canvas._render_bounds.enabled
    assert obj.object_id in seen and unrelated.object_id not in seen
    assert ("layer", obj.parent_layer_id) in canvas._render_bounds.live_branches
    canvas._clear_transform_preview()
    assert np.array_equal(render(canvas), render(canvas, False))


def test_multi_object_preview_retains_each_parent_but_culls_unrelated_scene(scene, monkeypatch):
    canvas, _chapter, _page, unrelated, _font = scene
    first, second = add_object(scene, "raster", 700), add_object(scene, "image", 900)
    initial = render(canvas)
    for obj in (first, second):
        assert canvas._render_bounds.entity_bounds("object", obj.object_id).left() > 480
    canvas.set_selection_set([("object", first.object_id), ("object", second.object_id)])
    canvas._geometry_transform_target = ("multi", "")
    canvas._transform_start_quad = [(700, 700), (940, 700), (940, 740), (700, 740)]
    canvas._transform_preview_quad = [(50, 70), (290, 70), (290, 110), (50, 110)]
    canvas._multi_transform_start_world_quads = {
        obj.object_id: canvas.object_world_quad(obj.object_id) for obj in (first, second)}
    canvas._multi_transform_preview_quads = {
        first.object_id: [(50, 70), (90, 70), (90, 110), (50, 110)],
        second.object_id: [(250, 70), (290, 70), (290, 110), (250, 110)],
    }
    expected = render(canvas, False)
    assert not np.array_equal(initial, expected)
    original = canvas._render_object_content
    def checked(painter, obj, visible):
        assert obj.object_id != unrelated.object_id
        return original(painter, obj, visible)
    monkeypatch.setattr(canvas, "_render_object_content", checked)
    assert np.array_equal(render(canvas), expected)
    for obj in (first, second):
        assert ("object", obj.object_id) in canvas._render_bounds.live_branches
        assert ("layer", obj.parent_layer_id) in canvas._render_bounds.live_branches


def test_multi_image_translation_keeps_distant_tiles_and_commits_image(scene, monkeypatch):
    canvas, chapter, page, _unrelated, _font = scene
    parent = chapter.add_layer(page.layer_id, "Moving",
                               BoundGeometry.rectangle(0, 0, 500, 500))
    parent.fill_color, parent.border_width = None, 0
    first = chapter.add_object(parent.layer_id, ImageObject(
        x=60, y=60, pixel_width=50, pixel_height=50))
    second = chapter.add_object(parent.layer_id, RasterObject(
        x=160, y=80, interaction_rect=(0, 0, 50, 50)))
    image = QImage(50, 50, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    canvas.images.put_decoded(first.object_id, "moving.png", b"", image)
    chapter.add_modifier(OutlineModifier(thickness=4), [("object", first.object_id)])
    chapter.add_modifier(DistortModifier(frame=(0, 0, 50, 50),
                                        center=(85, 85), radius=20),
                         [("object", first.object_id)])
    chapter.add_modifier(PosterizeModifier(), [("object", second.object_id)])
    canvas.set_selection_set([("object", second.object_id),
                              ("object", first.object_id)],
                             ("object", first.object_id))
    start_first = list(canvas.object_world_quad(first.object_id))
    start_second = list(canvas.object_world_quad(second.object_id))
    press = QPointF(105, 80)
    assert canvas._begin_multi_transform(press)
    assert canvas._transform_drag_mode == "translate"
    assert canvas._multi_transform_start_render_bounds is not None

    projection = canvas._document_projection
    for y in (0, 1, 6):
        request = ProjectionRequest(ProjectionAddress(0, 0, y))
        image = QImage(request.pixel_size, request.pixel_size,
                       QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor("blue"))
        projection.tiles[request.address] = ProjectionTile(
            request, image, projection.revision)
    distant = projection.tiles[ProjectionAddress(0, 0, 6)]

    canvas._update_multi_transform_preview(press + QPointF(30, 20))
    assert distant.valid
    preview = list(canvas._multi_transform_preview_quads[first.object_id])
    assert preview == pytest.approx([(x + 30, y + 20) for x, y in start_first])
    observed = []
    original = canvas._quad_transform
    monkeypatch.setattr(canvas, "_quad_transform", lambda source, quad:
                        (observed.append(list(quad)), original(source, quad))[1])
    target = QImage(300, 300, QImage.Format_ARGB32_Premultiplied)
    target.fill(QColor("transparent"))
    painter = QPainter(target)
    try:
        canvas._render_image_object(painter, first)
    finally:
        painter.end()
    assert observed[-1] == preview

    canvas._commit_geometry_transform()
    assert distant.valid
    assert first.transform_quad == pytest.approx(preview)
    assert second.transform_quad == pytest.approx(
        [(x + 30, y + 20) for x, y in start_second])
    assert canvas.command_stack.can_undo
    canvas.command_stack.undo()
    assert distant.valid
    assert canvas.chapter.objects[first.object_id].transform_quad is None
    assert canvas.chapter.objects[second.object_id].transform_quad is None
    canvas.command_stack.redo()
    assert distant.valid
    assert canvas.chapter.objects[first.object_id].transform_quad == pytest.approx(preview)


def test_object_preview_started_before_prepare_bypasses_stale_bounds(scene):
    canvas, _chapter, _page, _unrelated, _font = scene
    obj = add_object(scene, "raster")
    render(canvas)
    canvas.set_selection("object", obj.object_id)
    canvas._transform_start_quad = canvas.object_world_quad(obj.object_id)
    canvas._transform_preview_quad = [(80, 80), (180, 80), (180, 140), (80, 140)]
    canvas._interactive_render = True
    try:
        assert not canvas._render_bounds.usable()
        assert canvas._render_bounds.object_visible(obj, QRectF(0, 0, 480, 480))
        canvas._render_bounds.prepare()
        assert canvas._render_bounds.usable()
        canvas._render_modifier_sources.add(("object", obj.object_id))
        assert not canvas._render_bounds.usable()
    finally:
        canvas._render_modifier_sources.clear()
        canvas._interactive_render = False


@pytest.mark.parametrize("outlined", [False, True])
def test_text_drag_with_promoted_artwork_updates_before_one_undoable_commit(scene, monkeypatch, outlined):
    canvas, chapter, _page, _unrelated, _font = scene
    obj = add_object(scene, "text")
    obj.width, obj.height = 200, 100
    if outlined:
        chapter.add_modifier(OutlineModifier(thickness=4), [("object", obj.object_id)])
    monkeypatch.setattr(canvas._effect_jobs, "request", lambda *_a, **_kw: False)
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    initial = render(canvas)
    original_quad = list(obj.transform_quad)
    start = QPointF(750, 700)
    assert canvas._begin_selected_text_transform(start)
    assert canvas._transform_drag_mode == "translate"
    assert canvas._transform_static_cache.isNull()
    assert canvas._show_on_top_plan().entries
    before_commands = len(canvas.command_stack._undo)
    for offset in (550, 580, 600):
        canvas._update_transform_preview(start-QPointF(offset, offset))
        preview = render(canvas)
        assert not np.array_equal(initial, preview)
        assert obj.transform_quad == original_quad
        assert len(canvas.command_stack._undo) == before_commands
    final_quad = list(canvas._transform_preview_quad)
    canvas._commit_object_transform()
    assert chapter.objects[obj.object_id].transform_quad == final_quad
    assert len(canvas.command_stack._undo) == before_commands + 1
    assert np.array_equal(render(canvas), preview)
    canvas.command_stack.undo()
    assert chapter.objects[obj.object_id].transform_quad == original_quad
    assert np.array_equal(render(canvas), initial)
    canvas.command_stack.redo()
    assert chapter.objects[obj.object_id].transform_quad == final_quad
    assert np.array_equal(render(canvas), preview)


def test_strict_text_keeps_parent_layout_during_unrelated_preview(scene):
    canvas, _chapter, _page, _unrelated, _font = scene
    obj = add_object(scene, "text")
    obj.layout_mode = "strict"
    before = list(canvas.object_world_quad(obj.object_id))
    canvas.set_selection("object", obj.object_id)
    canvas._transform_start_quad = before
    canvas._transform_preview_quad = [(20, 20), (100, 20), (100, 80), (20, 80)]
    assert canvas.object_world_quad(obj.object_id) == before
    assert np.array_equal(render(canvas), render(canvas, False))
