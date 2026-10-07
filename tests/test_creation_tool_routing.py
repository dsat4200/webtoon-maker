"""Creation gestures own the pointer even while another object is selected."""
import math

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QEvent, QIODevice, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (BoundGeometry, ChapterDocument, ImageObject,
    RasterObject, VectorDrawingObject, VectorStroke, VectorStrokePoint)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def pointer(canvas, kind, document_point):
    position = canvas.document_to_widget(document_point)
    button = Qt.NoButton if kind == QEvent.MouseMove else Qt.LeftButton
    buttons = Qt.NoButton if kind == QEvent.MouseButtonRelease else Qt.LeftButton
    event = QMouseEvent(kind, position, QPointF(canvas.mapToGlobal(position.toPoint())),
                        button, buttons, Qt.NoModifier)
    QCoreApplication.sendEvent(canvas, event)


def drag(canvas, start, end):
    pointer(canvas, QEvent.MouseButtonPress, start)
    pointer(canvas, QEvent.MouseMove, end)
    pointer(canvas, QEvent.MouseButtonRelease, end)


@pytest.fixture
def selected_scene(qapp):
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 1080, 1080))
    layer = chapter.add_layer(page.layer_id, 'Shape', BoundGeometry.rectangle(80, 80, 600, 600))
    tiles, images = TileStore(), ImageStore()
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, pencil_transform_handles_visible=True))
    canvas.resize(900, 700)
    canvas.set_document(chapter, tiles, images)
    canvas.scale, canvas.center_x, canvas.center_y = 1., 300., 300.
    yield canvas, page.layer_id, layer.layer_id
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def select_object(canvas, parent, kind):
    placement = dict(transform_frame=(0, 0, 240, 200),
                     transform_quad=[(120, 140), (360, 140), (360, 340), (120, 340)])
    if kind == 'image':
        obj = ImageObject(pixel_width=240, pixel_height=200,
            source_filename='creation.png', source_mime_type='image/png', **placement)
        pixels = QImage(240, 200, QImage.Format_ARGB32_Premultiplied)
        pixels.fill(QColor('orange'))
        payload = QByteArray()
        output = QBuffer(payload)
        assert output.open(QIODevice.WriteOnly) and pixels.save(output, 'PNG')
        output.close()
        canvas.images.put(obj.object_id, 'creation.png', bytes(payload), 'image/png')
    elif kind == 'raster':
        obj = RasterObject(interaction_rect=(0, 0, 240, 200), **placement)
        canvas.tiles.paint_dab(obj.object_id, QPointF(70, 80), 15, QColor('orange'))
    else:
        obj = VectorDrawingObject(strokes=[VectorStroke(points=[
            VectorStrokePoint(x=20, y=30), VectorStrokePoint(x=200, y=160)])], **placement)
    canvas.chapter.add_object(parent, obj)
    canvas.set_selection('object', obj.object_id, False)
    return obj


def check_single_creation(canvas, original, identifier, kind, parent, history_count):
    assert canvas.selected_kind == kind and canvas.selected_id == identifier
    table = canvas.chapter.layers if kind == 'layer' else canvas.chapter.objects
    created = table[identifier]
    assert (created.parent_id if kind == 'layer' else created.parent_layer_id) == parent
    assert len(canvas.command_stack._undo) == history_count + 1
    if original:
        assert canvas.chapter.objects[original['id']].to_dict() == original['model']
    canvas.command_stack.undo()
    assert identifier not in (canvas.chapter.layers if kind == 'layer' else canvas.chapter.objects)
    if original:
        assert canvas.chapter.objects[original['id']].to_dict() == original['model']
    canvas.command_stack.redo()
    table = canvas.chapter.layers if kind == 'layer' else canvas.chapter.objects
    assert identifier in table and table[identifier].to_dict() == created.to_dict()


@pytest.mark.parametrize('kind', ['image', 'raster', 'vector'])
@pytest.mark.parametrize('start_kind', ['interior', 'handle'])
@pytest.mark.parametrize('tool', [ToolKind.BOX_BOUND, ToolKind.CIRCLE_BOUND,
                                  ToolKind.SHAPE_CREATE, ToolKind.DRAW_SHAPE, ToolKind.RASTER_CREATE])
def test_selected_object_creation_gesture_does_not_transform_existing_artwork(selected_scene, kind, start_kind, tool):
    canvas, _page, parent = selected_scene
    obj = select_object(canvas, parent, kind)
    original = {'id': obj.object_id, 'model': obj.to_dict()}
    history_count = len(canvas.command_stack._undo)
    start = QPointF(260, 240) if start_kind == 'interior' else QPointF(120, 140)
    end = start + QPointF(42, 37)
    if tool == ToolKind.RASTER_CREATE:
        assert canvas.begin_raster_creation(parent)
    else:
        assert canvas.set_tool(tool)
    pointer(canvas, QEvent.MouseButtonPress, start)
    assert canvas._pending_raster_transform_press is None and canvas._transform_drag_mode is None
    assert canvas._active_transform_cage() is None
    pointer(canvas, QEvent.MouseMove, end)
    if tool == ToolKind.DRAW_SHAPE:
        pointer(canvas, QEvent.MouseMove, start + QPointF(100, 80))
        end = start + QPointF(0, 110)
        pointer(canvas, QEvent.MouseMove, end)
    pointer(canvas, QEvent.MouseButtonRelease, end)
    if tool == ToolKind.SHAPE_CREATE:
        assert len(canvas._creation_nodes) == 1 and len(canvas.command_stack._undo) == history_count
        drag(canvas, start + QPointF(100, 0), start + QPointF(100, 0))
        drag(canvas, start + QPointF(60, 110), start + QPointF(60, 110))
        QCoreApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    created_kind = 'object' if tool == ToolKind.RASTER_CREATE else 'layer'
    identifier = canvas.selected_id
    assert identifier != obj.object_id
    check_single_creation(canvas, original, identifier, created_kind, parent, history_count)


@pytest.mark.parametrize('compound', [False, True])
@pytest.mark.parametrize('tool', [ToolKind.BOX_BOUND, ToolKind.CIRCLE_BOUND, ToolKind.SHAPE_CREATE])
def test_selected_shape_creation_places_sibling_or_compound_child_immediately(selected_scene, compound, tool):
    canvas, page, owner = selected_scene
    layer = canvas.chapter.layers[owner]
    layer.compound_enabled = compound
    original_bound = layer.bound.to_dict()
    canvas.set_selection('layer', owner, False)
    assert canvas.set_tool(tool)
    history_count = len(canvas.command_stack._undo)
    start, end = QPointF(260, 240), QPointF(302, 277)
    drag(canvas, start, end)
    if tool == ToolKind.SHAPE_CREATE:
        drag(canvas, QPointF(360, 240), QPointF(360, 240))
        drag(canvas, QPointF(320, 350), QPointF(320, 350))
        QCoreApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    identifier = canvas.selected_id
    assert identifier != owner and canvas.chapter.layers[owner].bound.to_dict() == original_bound
    created = canvas.chapter.layers[identifier]
    assert created.compound_operation == 'add'
    if tool == ToolKind.BOX_BOUND:
        assert created.bound.bbox() == (260., 240., 42., 37.)
    elif tool == ToolKind.CIRCLE_BOUND:
        radius = math.dist(start.toTuple(), end.toTuple())
        assert created.bound.bbox() == pytest.approx((260-radius, 240-radius, 2*radius, 2*radius))
    check_single_creation(canvas, None, identifier, 'layer', owner if compound else page, history_count)


@pytest.mark.parametrize('kind', ['image', 'raster', 'vector'])
@pytest.mark.parametrize('tool', [ToolKind.OBJECT_SELECT, ToolKind.TRANSFORM])
def test_selection_and_transform_keep_explicit_object_handles(selected_scene, kind, tool):
    canvas, _page, parent = selected_scene
    obj = select_object(canvas, parent, kind)
    before = obj.to_dict()
    assert canvas.set_tool(tool)
    drag(canvas, QPointF(120, 140), QPointF(108, 128))
    assert obj.to_dict() != before and len(canvas.command_stack._undo) == 1
    assert canvas._transform_drag_mode is None


@pytest.mark.parametrize('kind', ['image', 'raster', 'vector'])
def test_transform_keeps_interior_translation(selected_scene, kind):
    canvas, _page, parent = selected_scene
    obj = select_object(canvas, parent, kind)
    before = canvas.object_world_quad(obj.object_id)
    assert canvas.set_tool(ToolKind.TRANSFORM)
    drag(canvas, QPointF(260, 240), QPointF(282, 259))
    assert canvas.object_world_quad(obj.object_id) != before and len(canvas.command_stack._undo) == 1


def test_select_keeps_free_image_interior_translation(selected_scene):
    canvas, _page, parent = selected_scene
    obj = select_object(canvas, parent, 'image')
    before = canvas.object_world_quad(obj.object_id)
    assert canvas.set_tool(ToolKind.OBJECT_SELECT)
    drag(canvas, QPointF(260, 240), QPointF(282, 259))
    assert canvas.object_world_quad(obj.object_id) != before and len(canvas.command_stack._undo) == 1


@pytest.mark.parametrize('kind', ['raster', 'vector'])
def test_pencil_keeps_enabled_explicit_transform_handles(selected_scene, kind):
    canvas, _page, parent = selected_scene
    obj = select_object(canvas, parent, kind)
    before = canvas.object_world_quad(obj.object_id)
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    drag(canvas, QPointF(120, 140), QPointF(108, 128))
    assert canvas.object_world_quad(obj.object_id) != before and len(canvas.command_stack._undo) == 1
    assert not canvas._drawing and canvas._vector_gesture_mode is None
