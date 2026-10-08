"""Additive original user affine-cage editor cases; existing tests stay unchanged."""
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QTransform
from PySide6.QtTest import QTest
from comic_editor.core.models import BoundGeometry, CageTransformModifier
from test_text_updates import _canvas_with_text
from test_main_window_text_visibility import text_window, create_text, capture

@pytest.fixture
def await_completed_projection(wait_scene):
    """Reuse the incoming matching detached-scene publication gate unchanged."""
    return wait_scene

def test_translated_cage_text_edit_keeps_native_artwork_and_live_typing_visible(
    text_window, qapp, await_completed_projection,
):
    canvas = text_window.canvas
    obj = create_text(text_window, "object", ignore_parent_mask=True)
    cage = CageTransformModifier(frame=(0, 0, 600, 400))
    cage.validate_grid()
    cage.points = [(x, y + 170) for x, y in cage.points]
    canvas.chapter.add_modifier(cage, [("layer", obj.parent_layer_id)])
    canvas._invalidate_scene_cache()
    await_completed_projection(canvas)
    resting = capture(canvas, qapp)
    assert canvas.start_text_edit()
    assert capture(canvas, qapp) == resting
    QTest.keyClick(canvas, Qt.Key_A, Qt.ControlModifier)
    QTest.keyClicks(canvas, "Cage edit")
    live = capture(canvas, qapp)
    assert obj.text == "Cage edit"
    assert live != resting
    canvas.commit_active_text_edit()
    await_completed_projection(canvas)
    assert capture(canvas, qapp) == live

def test_clicking_displaced_text_from_parent_shape_reserves_first_tool_hotkey(
    text_window, qapp,
):
    canvas = text_window.canvas
    obj = create_text(text_window, "object", ignore_parent_mask=True)
    cage = CageTransformModifier(frame=(0, 0, 600, 400))
    cage.validate_grid()
    cage.points = [(x, y + 170) for x, y in cage.points]
    canvas.chapter.add_modifier(cage, [("layer", obj.parent_layer_id)])
    document, origin, mapping = canvas._text_edit_layout(obj)
    position = canvas.document_to_widget(
        origin + mapping.map(canvas._text_caret_rect(document, 7).center())
    ).toPoint()
    canvas.set_selection("layer", obj.parent_layer_id)
    QTest.mouseClick(canvas, Qt.LeftButton, pos=position)
    assert canvas.selected_object_id == obj.object_id
    assert canvas.has_active_text_edit()
    cursor = canvas._text_cursor_position
    before = obj.text
    # T is also a tool shortcut; the initiating click must reserve typing.
    QTest.keyClick(canvas, Qt.Key_T)
    assert obj.text == before[:cursor] + "t" + before[cursor:]

@pytest.mark.parametrize("layout_mode", ["strict", "free"])
@pytest.mark.parametrize("select_parent", [False, True])
@pytest.mark.parametrize("affine", [False, True])
def test_displaced_cage_text_click_edits_without_starting_shape_transform(
    qapp, monkeypatch, layout_mode, select_parent, affine,
):
    canvas, first, _second = _canvas_with_text()
    try:
        parent = canvas.chapter.add_layer(
            first.parent_layer_id, "Bubble", BoundGeometry.rectangle(80, 80, 240, 100)
        )
        canvas.chapter.move_entities([("object", first.object_id)], parent.layer_id, 0)
        first.layout_mode = layout_mode
        cage = CageTransformModifier(frame=(80, 80, 240, 100))
        cage.validate_grid()
        mapping = QTransform.fromTranslate(-7, 225)
        if affine:
            mapping.translate(200, 130).rotate(12).scale(1.1, .9).translate(-200, -130)
        cage.points = [mapping.map(QPointF(x, y)).toTuple() for x, y in cage.points]
        canvas.chapter.add_modifier(cage, [("layer", parent.layer_id)])
        canvas.set_selection("object", first.object_id)
        if select_parent:
            canvas.set_selection("layer", parent.layer_id)
        source = canvas.layer_world_transform(parent.layer_id).map(
            QPointF(200, 130)
        )
        displayed = mapping.map(source)
        native_quad = canvas.object_world_quad(first.object_id)
        for actual, source_point in zip(canvas._text_interaction_quad(first), native_quad):
            assert actual == pytest.approx(mapping.map(QPointF(*source_point)).toTuple())
        doc, origin, placement = canvas._text_edit_layout(first)
        caret = canvas._text_caret_rect(doc, 2).center()
        assert canvas._text_position_at(
            first, origin + placement.map(caret), require_inside=True
        )[1] == 2
        assert canvas.hit_test_objects(displayed, text_only=True) == [first.object_id]
        assert canvas.object_world_quad(first.object_id) == native_quad

        def unexpected_render(*args, **kwargs):
            pytest.fail("A text click must not capture artwork for a transform")

        monkeypatch.setattr(canvas, "_build_text_transform_cache", unexpected_render)
        monkeypatch.setattr(canvas, "_build_raster_transform_cache", unexpected_render)
        widget = canvas.document_to_widget(displayed).toPoint()
        QTest.mouseClick(canvas, Qt.LeftButton, pos=widget)
        assert canvas.selected_object_id == first.object_id
        assert canvas.has_active_text_edit()
        assert canvas._model_before is None
        QTest.keyClick(canvas, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClicks(canvas, "Edited")
        assert first.text == "Edited"
        assert canvas._text_interaction_quad(first) != canvas.object_world_quad(first.object_id)
        canvas.commit_active_text_edit()
        canvas.command_stack.undo()
        assert canvas.chapter.objects[first.object_id].text == "First"
    finally:
        canvas.hide()
        canvas.deleteLater()

def test_text_cage_editor_mapping_rechecks_live_grid_and_rejects_curved_blends(qapp):
    canvas, first, _second = _canvas_with_text()
    try:
        parent = canvas.chapter.add_layer(
            first.parent_layer_id, "Bubble", BoundGeometry.rectangle(80, 80, 240, 100)
        )
        canvas.chapter.move_entities([("object", first.object_id)], parent.layer_id, 0)
        cage = CageTransformModifier(frame=(80, 80, 240, 100))
        cage.validate_grid()
        cage.points = [(x, y + 225) for x, y in cage.points]
        canvas.chapter.add_modifier(cage, [("layer", first.parent_layer_id)])
        assert canvas._text_presentation_transform(first).dy() == pytest.approx(225)
        cage.points = [(x + 20, y) for x, y in cage.points]
        assert canvas._text_presentation_transform(first).dx() == pytest.approx(20)
        cage.intensity = 50
        assert canvas._text_presentation_transform(first).isIdentity()
        cage.intensity = 100
        cage.points[5] = (cage.points[5][0] + 10, cage.points[5][1])
        assert canvas._text_presentation_transform(first).isIdentity()
        cage.muted = True
        assert canvas._text_presentation_transform(first).isIdentity()
    finally:
        canvas.hide()
        canvas.deleteLater()

def test_cage_text_hit_respects_outer_clip_after_inner_displacement(qapp):
    canvas, first, _second = _canvas_with_text()
    try:
        page = canvas.chapter.layers[first.parent_layer_id]
        page.bound = BoundGeometry.rectangle(0, 0, 1080, 250)
        parent = canvas.chapter.add_layer(
            page.layer_id, "Bubble", BoundGeometry.rectangle(80, 80, 240, 100)
        )
        canvas.chapter.move_entities([("object", first.object_id)], parent.layer_id, 0)
        cage = CageTransformModifier(frame=(80, 80, 240, 100))
        cage.validate_grid()
        cage.points = [(x, y + 225) for x, y in cage.points]
        canvas.chapter.add_modifier(cage, [("layer", parent.layer_id)])
        displayed = QPointF(200, 355)
        assert canvas._object_hit_contains(first, displayed)
        assert not canvas.hit_test_objects(displayed, text_only=True)
        assert not canvas._text_presentation_clip(first).contains(displayed)
        parent.ignore_parent_mask = True
        assert canvas.hit_test_objects(displayed, text_only=True) == [first.object_id]
        assert canvas._text_presentation_clip(first).contains(displayed)
    finally:
        canvas.hide()
        canvas.deleteLater()

