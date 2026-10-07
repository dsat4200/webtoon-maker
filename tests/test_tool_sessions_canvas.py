from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QTransform
import pytest
from comic_editor.core.document_patch import RecordSnapshot
from comic_editor.core.models import ChapterDocument, BoundGeometry, RasterObject, VectorDrawingObject, TextObject, CageTransformModifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.mark.parametrize('kind', ['raster', 'vector', 'image'])
def test_metadata_transform_retains_only_placement_and_owned_attachments(qapp, monkeypatch, kind):
    from comic_editor.core.models import ImageObject, VectorStroke, VectorStrokePoint, BlurModifier, ToneMask, ParameterMaskBinding
    canvas, chapter, page = scene()
    constructors = {
        'raster': lambda: RasterObject(interaction_rect=(0, 0, 120, 120)),
        'vector': lambda: VectorDrawingObject(strokes=[VectorStroke(points=[VectorStrokePoint(x=0, y=0), VectorStrokePoint(x=120, y=120)])]),
        'image': lambda: ImageObject(pixel_width=120, pixel_height=120),
    }
    obj = chapter.add_object(page.layer_id, constructors[kind]())
    other = chapter.add_object(page.layer_id, VectorDrawingObject())
    modifier = BlurModifier(focal_center=(50, 60))
    chapter.add_modifier(modifier, [('object', obj.object_id)])
    mask = ToneMask(paint_offset=(3, 4))
    chapter.masks[mask.mask_id] = mask
    obj.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    canvas.set_selection('object', obj.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    monkeypatch.setattr(chapter, 'to_dict', lambda: pytest.fail('whole chapter transform capture'))
    monkeypatch.setattr(other, 'to_dict', lambda: pytest.fail('unrelated object transform capture'))
    monkeypatch.setattr(obj, 'to_dict', lambda: pytest.fail('unchanged artwork transform capture'))
    monkeypatch.setattr(canvas, '_selected_object_transform_hit', lambda *_a: ('translate', None))
    try:
        assert canvas._begin_selected_raster_transform(QPointF(0, 0))
        assert isinstance(canvas._model_before, RecordSnapshot)
        canvas._transform_preview_quad = [(x+20, y+30) for x, y in canvas._transform_start_quad]
        canvas._commit_object_transform()
        assert (obj.x, obj.y) == (20, 30)
        assert modifier.focal_center == (70, 90)
        assert mask.paint_offset == (23, 34)
        canvas.command_stack.undo()
        assert (obj.x, obj.y) == (0, 0)
        assert modifier.focal_center == (50, 60)
        assert mask.paint_offset == (3, 4)
        canvas.command_stack.redo()
        assert chapter.objects[obj.object_id] is obj
        assert chapter.objects[other.object_id] is other
        assert (obj.x, obj.y) == (20, 30)
    finally:
        canvas.close()


def scene():
    chapter = ChapterDocument()
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 1080, 1080))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(640, 480)
    canvas.set_document(chapter, TileStore())
    return canvas, chapter, page


@pytest.mark.parametrize('kind', ['shape', 'gradient'])
def test_geometry_gesture_and_cancel_do_not_serialize_unrelated_artwork(qapp, monkeypatch, kind):
    canvas, chapter, page = scene()
    unrelated = chapter.add_object(page.layer_id, VectorDrawingObject())
    if kind == 'shape':
        edited = chapter.add_layer(page.layer_id, bound=BoundGeometry.rectangle(20, 20, 150, 100))
        canvas.set_selection('layer', edited.layer_id)
        canvas.set_tool(ToolKind.SHAPE_EDIT)
        monkeypatch.setattr(canvas, '_shape_hit_test', lambda *_a, **_k: {'kind': 'interior'})
        begin, update = canvas._begin_shape_edit, canvas._update_shape_edit
        position = lambda: (edited.translate_x, edited.translate_y)
    else:
        edited = canvas.create_gradient(page.layer_id, 'radial', radial=((50, 50), 30))
        canvas.set_tool(ToolKind.GRADIENT)
        monkeypatch.setattr(canvas, '_gradient_control_hit', lambda *_a: ('origin', ''))
        begin = lambda point: canvas._begin_gradient_edit(edited, point)
        update = lambda point: canvas._update_gradient_edit(edited, point)
        position = lambda: (edited.radial_field.origin_x, edited.radial_field.origin_y)
    canvas.command_stack.clear()
    monkeypatch.setattr(chapter, 'to_dict', lambda: pytest.fail('Whole chapter geometry capture'))
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated artwork geometry capture'))
    original = position()
    try:
        assert begin(QPointF(50, 50))
        assert isinstance(canvas._model_before, RecordSnapshot)
        update(QPointF(75, 80))
        changed = position()
        assert changed != original
        canvas._tool_release()
        assert len(canvas.command_stack._undo) == 1
        canvas.command_stack.undo()
        assert position() == original
        canvas.command_stack.redo()
        assert position() == changed
        assert canvas.chapter is chapter and chapter.objects[unrelated.object_id] is unrelated
        assert begin(QPointF(75, 80))
        before = canvas._model_before
        update(QPointF(90, 95))
        canvas.replace_chapter(before)
        assert position() == changed
        assert len(canvas.command_stack._undo) == 1
    finally:
        canvas.close()


@pytest.mark.parametrize('kind', ['shape', 'raster', 'vector', 'gradient'])
def test_creation_captures_parent_and_new_record_only(qapp, monkeypatch, kind):
    canvas, chapter, page = scene()
    unrelated = chapter.add_object(page.layer_id, VectorDrawingObject())
    original_layers, original_objects = tuple(chapter.layers), tuple(chapter.objects)
    original_children = list(page.children)
    monkeypatch.setattr(chapter, 'to_dict', lambda: pytest.fail('Whole chapter creation capture'))
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated artwork creation capture'))
    try:
        if kind == 'shape':
            canvas._create_layer_from_world_bound(BoundGeometry.rectangle(20, 20, 80, 70),
                placement=(page.layer_id, 0))
        elif kind == 'raster':
            assert canvas.begin_raster_creation(page.layer_id)
            canvas._create_raster_from_world_rect((20, 20), (100, 90))
        elif kind == 'vector':
            assert canvas.create_vector_drawing(page.layer_id) is not None
        else:
            assert canvas.begin_gradient_creation(page.layer_id, 'radial')
            assert canvas.create_gradient(page.layer_id, 'radial', radial=((50, 50), 30),
                before=canvas._gradient_creation_before) is not None
        assert len(canvas.command_stack._undo) == 1
        current_layers, current_objects = tuple(chapter.layers), tuple(chapter.objects)
        canvas.command_stack.undo()
        assert tuple(chapter.layers) == original_layers
        assert tuple(chapter.objects) == original_objects
        assert page.children == original_children
        canvas.command_stack.redo()
        assert tuple(chapter.layers) == current_layers
        assert tuple(chapter.objects) == current_objects
        assert canvas.chapter is chapter and chapter.objects[unrelated.object_id] is unrelated
    finally:
        canvas.close()


def test_mask_gradient_gesture_and_registry_edit_capture_owned_mask_only(qapp, monkeypatch):
    from comic_editor.core.models import ToneMask
    canvas, chapter, page = scene()
    unrelated = chapter.add_object(page.layer_id, VectorDrawingObject())
    mask = ToneMask(saved=True)
    chapter.masks[mask.mask_id] = mask
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas.set_tool(ToolKind.GRADIENT)
    monkeypatch.setattr(chapter, 'to_dict', lambda: pytest.fail('Whole chapter mask gradient capture'))
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated artwork mask gradient capture'))
    try:
        canvas._mask_gradient_press(QPointF(30, 40))
        canvas._mask_gradient_move(QPointF(150, 40))
        assert canvas._finish_mask_gradient()
        assert len(canvas.command_stack._undo) == 1
        gradient_id = mask.gradient.object_id
        canvas.command_stack.undo()
        assert mask.gradient is None
        canvas.command_stack.redo()
        assert mask.gradient.object_id == gradient_id
        canvas._mask_gradient_press(QPointF(150, 40))
        canvas._mask_gradient_move(QPointF(190, 70))
        assert canvas._finish_mask_gradient(False)
        assert mask.gradient.line_field.geometry.nodes[-1].position == (150, 40)
        assert len(canvas.command_stack._undo) == 1
        limited = canvas.add_limited_mask_gradient('circular')
        assert limited is not None and len(mask.limited_gradients) == 1
        canvas.remove_active_mask_gradient()
        assert not mask.limited_gradients
        canvas.command_stack.undo()
        assert len(mask.limited_gradients) == 1
        assert chapter.masks[mask.mask_id] is mask
        assert chapter.objects[unrelated.object_id] is unrelated
    finally:
        canvas.close()


def test_ordered_raster_and_vector_sessions_deliver_every_pressure_sample_and_one_history(qapp):
    for kind in (RasterObject, VectorDrawingObject):
        canvas, chapter, page = scene()
        obj = chapter.add_object(page.layer_id, kind())
        canvas.set_selection("object", obj.object_id)
        canvas.set_tool(ToolKind.RASTER_PENCIL)
        canvas._tool_press(canvas.document_to_widget(QPointF(40, 40)), .2)
        for number in range(1, 41):
            canvas._tool_move(canvas.document_to_widget(QPointF(40+number*2, 40+number)), .2+number/100)
        outer = canvas._pointer_tool_session
        kernel = canvas._raster_tool_session if kind is RasterObject else canvas._vector_tool_session
        assert outer.samples == kernel.samples == 41
        canvas._tool_release()
        assert outer.closed and kernel.closed
        assert canvas.command_stack.revision == 2  # initial clear + one gesture
        assert canvas._pointer_tool_session is None
        canvas.command_stack.undo()
        canvas.command_stack.redo()
        canvas.close()


def test_cage_parameter_gesture_does_not_serialize_unrelated_objects(qapp, monkeypatch):
    canvas, chapter, page = scene()
    unrelated = chapter.add_object(page.layer_id, VectorDrawingObject())
    layer = chapter.add_layer(page.layer_id, bound=BoundGeometry.rectangle(0, 0, 200, 200))
    cage = CageTransformModifier(frame=(0, 0, 200, 200))
    chapter.add_modifier(cage, [("layer", layer.layer_id)])
    canvas.set_selection("layer", layer.layer_id)
    canvas.modifier_mode = True
    canvas._remember_modifier(cage.modifier_id)
    monkeypatch.setattr(unrelated, "to_dict", lambda: (_ for _ in ()).throw(AssertionError("unrelated serialization")))
    canvas.set_cage_parameter("interpolation", "bilinear")
    canvas.finish_cage(True)
    # Focused record snapshots and both restore directions retain the object.
    canvas.command_stack.undo()
    canvas.command_stack.redo()
    assert chapter.objects[unrelated.object_id] is unrelated
    canvas.close()


def test_focused_history_publishes_bounds_and_preserves_unrelated_observer_state(qapp, monkeypatch):
    canvas, chapter, page = scene()
    moved = chapter.add_object(page.layer_id, RasterObject(x=20, y=30))
    unrelated_layer = chapter.add_layer(page.layer_id, bound=BoundGeometry.rectangle(400, 0, 100, 100))
    unrelated = chapter.add_object(unrelated_layer.layer_id, RasterObject())
    canvas._change_index.bind(chapter)
    marker = ("object", unrelated.object_id)
    canvas._render_bounds.bounds[marker] = QRectF(400, 0, 100, 100)
    canvas._render_bounds.transforms[unrelated_layer.layer_id] = QTransform.fromTranslate(400, 0)
    warm = object()
    canvas._gradient_render_cache["unrelated"] = warm
    events, replacements, hierarchies = [], [], []
    canvas.changesPublished.connect(events.append)
    canvas.chapterReplaced.connect(replacements.append)
    canvas.hierarchyChanged.connect(lambda: hierarchies.append(True))
    monkeypatch.setattr(chapter, "to_dict", lambda: (_ for _ in ()).throw(AssertionError("full serialization")))
    before = RecordSnapshot.capture(chapter, objects=[moved.object_id])
    moved.x = 120
    canvas.push_model_change(before, before.after(chapter), "Translate raster")
    forward = events[-1]
    assert forward.entities[0].old_bounds is not None and forward.entities[0].new_bounds is not None
    assert forward.entities[0].fields == frozenset({"position"})
    canvas.command_stack.undo()
    canvas.command_stack.redo()
    assert canvas.chapter is chapter and chapter.objects[moved.object_id] is moved
    assert events[-2].entities[0].old_bounds == forward.entities[0].new_bounds
    assert marker in canvas._render_bounds.bounds
    assert unrelated_layer.layer_id in canvas._render_bounds.transforms
    assert canvas._gradient_render_cache["unrelated"] is warm
    assert not replacements and not hierarchies
    assert canvas._change_index.generation(*marker) == 0
    canvas.close()
