"""Real property edits preserve tree indices and unrelated UI resources."""
import pytest
from PySide6.QtCore import QPersistentModelIndex, Qt
from PySide6.QtTest import QSignalSpy

from comic_editor.core.models import BoundGeometry, BrightnessContrastModifier, ChapterDocument, RasterObject
from comic_editor.core.tiles import TileStore
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def owner(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    window = MainWindow()
    chapter = ChapterDocument(width=32, height=32, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 32, 32))
    shape = chapter.add_layer(page.layer_id, bound=BoundGeometry.rectangle(0, 0, 20, 20))
    first = chapter.add_object(shape.layer_id, RasterObject(name='First'))
    second = chapter.add_object(shape.layer_id, RasterObject(name='Unrelated'))
    modifier = BrightnessContrastModifier()
    chapter.add_modifier(modifier, [('object', first.object_id)])
    window.chapter = chapter
    window.canvas.set_document(chapter, TileStore())
    window.hierarchy_model.set_chapter(chapter)
    window.canvas.set_selection('object', first.object_id, activate_default_tool=False)
    window.modifier_controls.refresh()
    yield window, shape, first, second, modifier
    window._dirty = False
    window.deleteLater()


def no_full_serialization(monkeypatch, chapter):
    def forbidden():
        raise AssertionError('Property edit serialized the whole chapter')
    monkeypatch.setattr(chapter, 'to_dict', forbidden)


def test_mask_binding_and_contributors_keep_unrelated_records_and_rows(owner, monkeypatch):
    from comic_editor.core.models import ToneMask
    window, _shape, first, unrelated, _modifier = owner
    canvas, chapter = window.canvas, window.chapter
    saved = ToneMask(name='Independent', saved=True)
    chapter.masks[saved.mask_id] = saved
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated drawing serialized for mask binding'))
    monkeypatch.setattr(first, 'to_dict', lambda: pytest.fail('Drawing pixels/geometry serialized for opacity binding'))
    monkeypatch.setattr(saved, 'to_dict', lambda: pytest.fail('Independent saved mask serialized'))
    stable = QPersistentModelIndex(window.hierarchy_model.index_for_entity('object', unrelated.object_id))
    resets = QSignalSpy(window.hierarchy_model.modelReset)
    context = ('opacity', 'object', first.object_id, 0., 100., 0., 100.)
    window._request_parameter_mask(context)
    mask_id = first.opacity_mask.mask_id
    assert window._toggle_mask_contributor('object', unrelated.object_id)
    window._finish_mask_mode(True)
    assert chapter.masks[mask_id].contributors == [('object', unrelated.object_id)]
    canvas.command_stack.undo()
    assert chapter.masks[mask_id].contributors == []
    canvas.command_stack.redo()
    window._detach_parameter_mask(context)
    assert first.opacity_mask is None and mask_id not in chapter.masks
    canvas.command_stack.undo()
    assert first.opacity_mask.mask_id == mask_id
    assert chapter.masks[mask_id].contributors == [('object', unrelated.object_id)]
    canvas.command_stack.redo()
    assert mask_id not in chapter.masks and first.opacity_mask is None
    assert chapter.objects[unrelated.object_id] is unrelated and chapter.masks[saved.mask_id] is saved
    assert stable.isValid() and resets.count() == 0


def test_saved_mask_rename_is_metadata_only_and_has_focused_history(owner, monkeypatch):
    from comic_editor.core.models import ToneMask
    window, _shape, _first, unrelated, _modifier = owner
    canvas, chapter = window.canvas, window.chapter
    mask = ToneMask(name='Before', saved=True)
    chapter.masks[mask.mask_id] = mask
    canvas.active_tone_mask_id = mask.mask_id
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated drawing serialized for mask rename'))
    monkeypatch.setattr(mask, 'to_dict', lambda: pytest.fail('Mask artwork serialized for mask rename'))
    monkeypatch.setattr('comic_editor.ui.main_window.QInputDialog.getText', lambda *_a, **_k: ('After', True))
    revision = canvas._document_projection.revision
    window._rename_current_mask()
    assert mask.name == 'After' and canvas._document_projection.revision == revision
    canvas.command_stack.undo()
    assert mask.name == 'Before' and canvas._document_projection.revision == revision
    canvas.command_stack.redo()
    assert mask.name == 'After' and chapter.objects[unrelated.object_id] is unrelated


def test_mask_panel_thumbnail_uses_detached_common_kernel(owner, monkeypatch, qapp):
    import time
    from threading import get_ident
    import numpy as np
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QColor, QImage, QTransform
    from comic_editor.core.models import ToneMask
    from comic_editor.render.scene_kernels import SceneKernels
    window, _shape, first, _unrelated, _modifier = owner
    canvas, chapter = window.canvas, window.chapter
    mask = ToneMask(name='Source', saved=True, contributors=[('object', first.object_id)])
    chapter.masks[mask.mask_id] = mask
    canvas.tiles.paint_segment(first.object_id, QPointF(10, 10), QPointF(10, 10), 8, 8, QColor('white'), 1., 1.)
    transform = QTransform.fromScale(80 / chapter.width, 80 / chapter.height)
    reference = canvas.render_tone_mask_field(mask.mask_id, 80, 80, transform, QRectF(0, 0, 32, 32))
    expected = np.ascontiguousarray(np.clip(reference * 255., 0, 255).astype(np.uint8))
    gui = get_ident()
    calls = []
    original = SceneKernels.render_tone_mask_field
    def sample(scene, *_a, **_k):
        calls.append(get_ident())
        assert get_ident() != gui
        return original(scene, *_a, **_k)
    monkeypatch.setattr(SceneKernels, 'render_tone_mask_field', sample)
    monkeypatch.setattr(canvas, 'render_tone_mask_field', lambda *_a, **_k: pytest.fail('GUI evaluated a mask thumbnail'))
    window._refresh_masks_panel()
    assert getattr(window, '_mask_thumbnail_state', None) is None
    deadline = time.monotonic() + 15
    while getattr(window, '_mask_thumbnail_request', None) is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert calls and all(thread != gui for thread in calls)
    result = window._mask_thumbnail_state[2][mask.mask_id]
    assert result.format() == QImage.Format_Grayscale8
    actual = np.frombuffer(result.constBits(), np.uint8).reshape(80, result.bytesPerLine())[:, :80]
    np.testing.assert_array_equal(actual, expected)


def test_deleting_cold_painted_mask_captures_off_gui_and_restores_focused_sources(owner, monkeypatch, qapp, tmp_path):
    import time
    from threading import Event, get_ident
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor
    from comic_editor.core.models import ParameterMaskBinding, ToneMask
    from comic_editor.render import source_sampling
    window, _shape, first, unrelated, modifier = owner
    canvas, chapter = window.canvas, window.chapter
    mask = ToneMask(name='Paint', saved=True)
    independent = ToneMask(name='Keep', saved=True)
    chapter.masks.update({mask.mask_id: mask, independent.mask_id: independent})
    first.opacity_mask = ParameterMaskBinding(mask.mask_id, 0., .6)
    modifier.parameter_masks['brightness'] = ParameterMaskBinding(mask.mask_id, 0., 30.)
    canvas.tiles.paint_segment(mask.mask_id, QPointF(10, 10), QPointF(10, 10), 7, 7, QColor('white'), 1., 1.)
    expected = canvas.tiles.object_tiles(mask.mask_id)
    canvas.tiles.save_directory(tmp_path, {mask.mask_id})
    cold = TileStore()
    cold.load_directory(tmp_path, {mask.mask_id})
    canvas.set_document(chapter, cold)
    canvas.active_tone_mask_id = mask.mask_id
    window.hierarchy_model.set_chapter(chapter)
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Deleting mask serialized unrelated artwork'))
    gui = get_ident()
    serialize_mask = ToneMask.to_dict
    def mask_record(value):
        if value.mask_id == independent.mask_id:
            assert get_ident() != gui, 'Deleting mask serialized unrelated mask on GUI'
        return serialize_mask(value)
    monkeypatch.setattr(ToneMask, 'to_dict', mask_record)
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr('comic_editor.ui.main_window.QMessageBox.question', lambda *_a, **_k: QMessageBox.Yes)
    entered, release = Event(), Event()
    original = source_sampling.owned_object_tiles
    def prepare(snapshot, identifier):
        assert get_ident() != gui
        entered.set()
        assert release.wait(10)
        return original(snapshot, identifier)
    monkeypatch.setattr(source_sampling, 'owned_object_tiles', prepare)
    revision = canvas.command_stack.revision
    window._delete_current_mask()
    deadline = time.monotonic() + 10
    while not entered.is_set() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    try:
        assert entered.is_set() and mask.mask_id in chapter.masks
        assert canvas.command_stack.revision == revision
    finally:
        release.set()
    jobs = canvas._scene_consumers
    while jobs.contains(('delete-mask',)) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert not jobs.contains(('delete-mask',))
    assert mask.mask_id not in chapter.masks and not cold._tiles.get(mask.mask_id)
    assert first.opacity == .6 and first.opacity_mask is None
    assert modifier.brightness == 30. and not modifier.parameter_masks
    assert len(canvas.command_stack._undo) == 1
    canvas.command_stack.undo()
    assert first.opacity_mask.mask_id == mask.mask_id and modifier.parameter_masks['brightness'].mask_id == mask.mask_id
    for key, image in expected.items():
        restored = cold.tile(mask.mask_id, key)
        assert restored.format() == image.format() and bytes(restored.constBits()) == bytes(image.constBits())
    canvas.command_stack.redo()
    assert mask.mask_id not in chapter.masks
    assert chapter.objects[unrelated.object_id] is unrelated and chapter.masks[independent.mask_id] is independent


def test_outliner_rename_and_undo_preserve_indices_and_only_change_named_row(owner, monkeypatch):
    window, _shape, first, second, _modifier = owner
    model = window.hierarchy_model
    index = model.index_for_entity('object', first.object_id)
    unrelated = QPersistentModelIndex(model.index_for_entity('object', second.object_id))
    resets, changed = QSignalSpy(model.modelReset), QSignalSpy(model.dataChanged)
    revision = window.canvas._document_projection.revision
    no_full_serialization(monkeypatch, window.chapter)
    assert model.setData(index, 'Renamed', Qt.EditRole)
    assert first.name == 'Renamed'
    assert resets.count() == 0 and unrelated.isValid()
    assert all(model.item_for_index(changed.at(i)[0]).entity_id == first.object_id
               for i in range(changed.count()))
    assert window.canvas._document_projection.revision == revision
    window.canvas.command_stack.undo()
    assert first.name == 'First' and unrelated.isValid() and resets.count() == 0
    window.canvas.command_stack.redo()
    assert first.name == 'Renamed' and resets.count() == 0


def test_common_opacity_drag_is_one_history_edit_and_keeps_unrelated_card(owner, monkeypatch):
    window, _shape, first, second, modifier = owner
    card = window.modifier_controls._cards[modifier.modifier_id]
    resets = QSignalSpy(window.hierarchy_model.modelReset)
    no_full_serialization(monkeypatch, window.chapter)
    revision = window.canvas.command_stack.revision
    common = window.selection_common
    common._begin_opacity_drag()
    common._opacity_changed(70)
    common._opacity_changed(30)
    common._finish_opacity_drag()
    assert first.opacity == .3 and second.opacity == 1.
    assert window.canvas.command_stack.revision == revision+1
    assert resets.count() == 0
    assert window.modifier_controls._cards[modifier.modifier_id] is card
    window.canvas.command_stack.undo()
    assert first.opacity == 1. and resets.count() == 0


def test_raster_property_edit_avoids_full_chapter_and_tree_reset(owner, monkeypatch):
    window, _shape, first, _second, _modifier = owner
    resets = QSignalSpy(window.hierarchy_model.modelReset)
    no_full_serialization(monkeypatch, window.chapter)
    controls = window.selection_settings.raster_controls
    controls.name.setText('Drawing')
    controls._apply_discrete()
    assert first.name == 'Drawing' and resets.count() == 0
    window.canvas.command_stack.undo()
    assert first.name == 'First' and resets.count() == 0


def test_layer_opacity_history_restores_attached_locked_objects(owner, monkeypatch):
    window, shape, first, second, _modifier = owner
    window.canvas.set_selection('layer', shape.layer_id, activate_default_tool=False)
    no_full_serialization(monkeypatch, window.chapter)
    panel = window.selection_settings.layer_page
    panel.opacity.setValue(35)
    assert shape.opacity == .35 and first.opacity == .35 and second.opacity == .35
    window.canvas.command_stack.undo()
    assert shape.opacity == 1. and first.opacity == 1. and second.opacity == 1.


def test_modifier_slider_keeps_card_and_updates_same_controls_through_undo(owner, monkeypatch):
    window, _shape, first, second, modifier = owner
    card = window.modifier_controls._cards[modifier.modifier_id]
    resets = QSignalSpy(window.hierarchy_model.modelReset)
    no_full_serialization(monkeypatch, window.chapter)
    revision = window.canvas.command_stack.revision
    window.modifier_controls.begin_parameter_drag(modifier.modifier_id)
    window.modifier_controls.set_parameter(modifier.modifier_id, 'brightness', 20., False)
    window.modifier_controls.set_parameter(modifier.modifier_id, 'brightness', 40., False)
    assert window.modifier_controls._cards[modifier.modifier_id] is card
    window.modifier_controls.finish_parameter_drag()
    assert window.canvas.command_stack.revision == revision+1
    assert resets.count() == 0 and second.opacity == 1.
    window.canvas.command_stack.undo()
    assert modifier.brightness == 0.
    assert window.modifier_controls._cards[modifier.modifier_id] is card
    assert card._parameter_controls['brightness'][0].value() == 0


@pytest.mark.parametrize('layout', ['free', 'strict'])
def test_live_typing_history_reads_only_edited_text_and_retains_modifier_card(owner, monkeypatch, layout):
    from copy import deepcopy
    from PySide6.QtGui import QKeyEvent
    from comic_editor.core.models import OutlineModifier, TextObject
    from comic_editor.core.text_styles import apply_text_color
    from comic_editor.core.document_patch import RecordSnapshot
    window, shape, _first, unrelated, _modifier = owner
    chapter, canvas = window.chapter, window.canvas
    text = chapter.add_object(shape.layer_id, TextObject(text='One🙂 two', layout_mode=layout))
    apply_text_color(text, 1, 5, '#FFCC2255')
    outline = OutlineModifier()
    chapter.add_modifier(outline, [('object', text.object_id)])
    window.hierarchy_model.set_chapter(chapter)
    canvas.set_selection('object', text.object_id)
    window.modifier_controls.refresh()
    card = window.modifier_controls._cards[outline.modifier_id]
    stable = QPersistentModelIndex(window.hierarchy_model.index_for_entity('object', unrelated.object_id))
    resets = QSignalSpy(window.hierarchy_model.modelReset)
    before = text.text, deepcopy(text.color_runs), text.margin
    revision = canvas.command_stack.revision
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Typing serialized an unrelated drawing'))
    assert canvas.start_text_edit(select_all=True)
    assert isinstance(canvas._text_before_state, RecordSnapshot)
    assert set(canvas._text_before_state.records['objects']) == {text.object_id}
    assert set(canvas._text_before_state.records['objects'][text.object_id]) == {'text', 'color_runs', 'margin'}
    canvas._replace_text_selection('Live🙂\nwrapping')
    assert window.modifier_controls._cards[outline.modifier_id] is card
    canvas._handle_text_key(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Z, Qt.ControlModifier))
    assert (text.text, text.color_runs, text.margin) == before
    canvas._replace_text_selection('Committed🙂')
    canvas.commit_active_text_edit()
    committed = text.text, deepcopy(text.color_runs), text.margin
    assert canvas.command_stack.revision == revision + 1
    unrelated.name = 'Outside the typing transaction'
    canvas.command_stack.undo()
    assert chapter.objects[text.object_id] is text
    assert (text.text, text.color_runs, text.margin) == before
    assert unrelated.name == 'Outside the typing transaction'
    canvas.command_stack.redo()
    assert (text.text, text.color_runs, text.margin) == committed
    assert window.modifier_controls._cards[outline.modifier_id] is card
    assert resets.count() == 0 and stable.isValid()


@pytest.mark.parametrize('new_container', [False, True])
def test_text_placement_history_captures_parent_and_new_records_only(owner, monkeypatch, new_container):
    from copy import deepcopy
    from PySide6.QtCore import QPointF
    window, parent, first, unrelated, _modifier = owner
    chapter, canvas = window.chapter, window.canvas
    previous_children = deepcopy(parent.children)
    previous_orders = tuple(chapter.layers), tuple(chapter.objects)
    previous_size = chapter.width, chapter.height
    revision = canvas.command_stack.revision
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Placement serialized an unrelated drawing'))
    monkeypatch.setattr(first, 'to_dict', lambda: pytest.fail('Placement serialized an existing drawing'))
    canvas.settings.snap_to_grid = False
    canvas.begin_text_placement(parent.layer_id, new_container=new_container)
    canvas._text_placement_press(QPointF(2, 3))
    canvas._text_placement_move(QPointF(17, 16))
    assert canvas._finish_text_placement()
    text_id = canvas.selected_id
    text = chapter.objects[text_id]
    container_id = text.parent_layer_id
    assert text_id not in previous_orders[1]
    assert container_id == parent.layer_id if not new_container else container_id not in previous_orders[0]
    assert canvas.command_stack.revision == revision + 1
    canvas.commit_active_text_edit()
    assert canvas.command_stack.revision == revision + 1
    unrelated.name = 'Unrelated change after insertion'
    canvas.command_stack.undo()
    assert text_id not in chapter.objects
    assert tuple(chapter.layers) == previous_orders[0]
    assert tuple(chapter.objects) == previous_orders[1]
    assert parent.children == previous_children
    assert (chapter.width, chapter.height) == previous_size
    assert unrelated.name == 'Unrelated change after insertion'
    canvas.command_stack.redo()
    assert text_id in chapter.objects
    assert chapter.objects[text_id].parent_layer_id == container_id
    assert tuple(chapter.objects) == (*previous_orders[1], text_id)
    if new_container:
        assert tuple(chapter.layers) == (*previous_orders[0], container_id)
    assert unrelated.name == 'Unrelated change after insertion'
    revision = canvas.command_stack.revision
    canvas.begin_text_placement(parent.layer_id, new_container=new_container)
    canvas._text_placement_press(QPointF(1, 1))
    assert canvas._cancel_text_features(restore=True)
    assert canvas.command_stack.revision == revision
    assert len(chapter.objects) == len(previous_orders[1]) + 1


def test_add_text_action_preserves_insertion_order_without_serializing_unrelated_records(owner, monkeypatch):
    from copy import deepcopy
    window, parent, first, unrelated, _modifier = owner
    chapter, canvas = window.chapter, window.canvas
    previous = deepcopy(parent.children)
    revision = canvas.command_stack.revision
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Text action serialized unrelated drawing'))
    monkeypatch.setattr(first, 'to_dict', lambda: pytest.fail('Text action serialized selected drawing'))
    expected_index = window._new_object_insertion_index(parent.layer_id)
    window._add_text()
    text_id = canvas.selected_id
    text = chapter.objects[text_id]
    assert parent.children[expected_index].entity_id == text_id
    assert canvas.has_active_text_edit()
    assert text.text == 'Text'
    assert canvas.command_stack.revision == revision + 1
    canvas.commit_active_text_edit()
    unrelated.name = 'Changed after add text'
    canvas.command_stack.undo()
    assert text_id not in chapter.objects and parent.children == previous
    assert unrelated.name == 'Changed after add text'
    canvas.command_stack.redo()
    assert text_id in chapter.objects
    assert parent.children[expected_index].entity_id == text_id
    assert unrelated.name == 'Changed after add text'


def test_gradient_controls_and_drag_keep_original_target_without_chapter_capture(owner, monkeypatch):
    from comic_editor.core.models import ColorFillGradientObject, ColorGradientRamp, ColorGradientStop
    window, shape, _first, unrelated, _modifier = owner
    canvas, chapter = window.canvas, window.chapter
    gradient = chapter.add_object(shape.layer_id, ColorFillGradientObject())
    canvas.set_selection('object', gradient.object_id, activate_default_tool=False)
    controls = window.gradient_tools_controls
    controls.refresh()
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated object serialized by gradient control'))
    old = gradient.ramp.to_dict()
    revision = canvas.command_stack.revision
    controls._add_stop()
    assert len(gradient.ramp.stops) == 3
    canvas.command_stack.undo()
    assert gradient.ramp.to_dict() == old
    canvas.command_stack.redo()
    assert len(gradient.ramp.stops) == 3
    controls._begin_ramp_edit()
    replacement = ColorGradientRamp(stops=[
        ColorGradientStop(position=0., color='#FFFF0000'),
        ColorGradientStop(position=1., color='#FF00FF00'),
    ])
    canvas.set_selection('object', unrelated.object_id, activate_default_tool=False)
    controls._preview_ramp(replacement)
    controls._finish_ramp_edit()
    assert gradient.ramp.stops[0].color == '#FFFF0000'
    assert chapter.objects[unrelated.object_id] is unrelated
    assert canvas.command_stack.revision == revision + 4
    canvas.command_stack.undo()
    assert len(gradient.ramp.stops) == 3
    canvas.command_stack.redo()
    assert gradient.ramp.stops[0].color == '#FFFF0000'


def test_mask_gradient_control_captures_only_owned_gradient_fields(owner, monkeypatch):
    from comic_editor.core.models import ColorFillGradientObject, ToneMask
    from comic_editor.ui.canvas import ToolKind
    window, _shape, _first, unrelated, _modifier = owner
    canvas, chapter = window.canvas, window.chapter
    mask = ToneMask(saved=True, gradient=ColorFillGradientObject())
    chapter.masks[mask.mask_id] = mask
    canvas.active_tone_mask_id = mask.mask_id
    canvas.set_tool(ToolKind.GRADIENT)
    controls = window.gradient_tools_controls
    controls.refresh()
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated drawing serialized by mask gradient control'))
    monkeypatch.setattr(mask, 'to_dict', lambda: pytest.fail('Mask contributors serialized for gradient field edit'))
    old = mask.gradient.ramp.to_dict()
    revision = mask.revision
    controls._add_stop()
    assert len(mask.gradient.ramp.stops) == 3 and mask.revision > revision
    canvas.command_stack.undo()
    assert mask.gradient.ramp.to_dict() == old
    canvas.command_stack.redo()
    assert len(mask.gradient.ramp.stops) == 3
    assert chapter.objects[unrelated.object_id] is unrelated


def test_vector_creation_captures_new_record_and_parent_only(owner, monkeypatch):
    from comic_editor.core.models import VectorDrawingObject
    window, shape, _first, unrelated, _modifier = owner
    chapter, canvas = window.chapter, window.canvas
    canvas.set_selection('layer', shape.layer_id, activate_default_tool=False)
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated artwork serialized by vector creation'))
    window._add_vector_drawing()
    drawing = next(obj for obj in chapter.objects.values() if isinstance(obj, VectorDrawingObject))
    canvas.command_stack.undo()
    assert drawing.object_id not in chapter.objects
    canvas.command_stack.redo()
    assert drawing.object_id in chapter.objects and chapter.objects[unrelated.object_id] is unrelated


def test_trim_and_grid_changes_capture_only_document_fields(owner, monkeypatch):
    from types import SimpleNamespace
    from comic_editor.core.models import GridSettings
    from PySide6.QtWidgets import QDialog
    window, _shape, _first, unrelated, _modifier = owner
    chapter, canvas = window.chapter, window.canvas
    chapter.height = 128
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated artwork serialized for chapter field'))
    monkeypatch.setattr(canvas, 'object_world_rect', lambda *_: None)
    monkeypatch.setattr('comic_editor.ui.main_window.QInputDialog.getInt', lambda *_a, **_k: (64, True))
    resize_dialog = SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted,
        height=SimpleNamespace(value=lambda: 64),
        add_position=SimpleNamespace(currentData=lambda: 'bottom'),
    )
    monkeypatch.setattr('comic_editor.ui.main_window.ResizeCanvasDialog', lambda *_a: resize_dialog)
    stable = QPersistentModelIndex(window.hierarchy_model.index_for_entity('object', unrelated.object_id))
    resets = QSignalSpy(window.hierarchy_model.modelReset)
    window._trim_height()
    assert chapter.height == 64
    canvas.command_stack.undo()
    assert chapter.height == 128
    canvas.command_stack.redo()
    assert chapter.height == 64
    old_grid, old_enabled = chapter.grid.to_dict(), chapter.grid_override_enabled
    dialog = SimpleNamespace(
        exec=lambda: QDialog.DialogCode.Accepted,
        apply_user_settings=lambda *_: None,
        document_override=SimpleNamespace(isChecked=lambda: True),
        document_grid=lambda: GridSettings(size=64, divisions=8),
    )
    monkeypatch.setattr('comic_editor.ui.main_window.SettingsDialog', lambda *_: dialog)
    window._edit_settings()
    assert chapter.grid.size == 64 and chapter.grid_override_enabled
    canvas.command_stack.undo()
    assert chapter.grid.to_dict() == old_grid and chapter.grid_override_enabled == old_enabled
    canvas.command_stack.redo()
    assert chapter.grid.size == 64 and chapter.objects[unrelated.object_id] is unrelated
    assert stable.isValid() and resets.count() == 0


def test_entity_deletion_history_keeps_unrelated_artwork_and_saved_masks(owner, monkeypatch):
    from comic_editor.core.models import ParameterMaskBinding, ToneMask
    from PySide6.QtWidgets import QMessageBox
    window, _shape, first, unrelated, modifier = owner
    chapter, canvas = window.chapter, window.canvas
    mask = ToneMask(contributors=[('object', unrelated.object_id)])
    dependent = ToneMask(saved=True, contributors=[('object', first.object_id)])
    saved = ToneMask(saved=True, name='Independent')
    chapter.masks.update({mask.mask_id: mask, saved.mask_id: saved, dependent.mask_id: dependent})
    first.opacity_mask = ParameterMaskBinding(mask_id=mask.mask_id)
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated artwork serialized by deletion'))
    monkeypatch.setattr(saved, 'to_dict', lambda: pytest.fail('Unrelated saved mask serialized by deletion'))
    monkeypatch.setattr('comic_editor.ui.main_window.QMessageBox.question', lambda *_: QMessageBox.Yes)
    window._delete_selected()
    assert first.object_id not in chapter.objects and modifier.modifier_id not in chapter.modifiers
    assert mask.mask_id not in chapter.masks
    assert not dependent.contributors
    canvas.command_stack.undo()
    assert first.object_id in chapter.objects and modifier.modifier_id in chapter.modifiers
    assert chapter.objects[first.object_id].opacity_mask.mask_id == mask.mask_id
    assert chapter.masks[mask.mask_id].contributors == [('object', unrelated.object_id)]
    assert dependent.contributors == [('object', first.object_id)]
    canvas.command_stack.redo()
    assert first.object_id not in chapter.objects
    assert chapter.objects[unrelated.object_id] is unrelated and chapter.masks[saved.mask_id] is saved


def test_gradient_path_point_deletion_keeps_unrelated_records_and_rows(owner, monkeypatch):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    from comic_editor.core.models import ColorFillGradientObject, LineGradientField, PathNode
    from comic_editor.ui.canvas import ToolKind
    window, shape, first, unrelated, _modifier = owner
    canvas, chapter = window.canvas, window.chapter
    nodes = [PathNode(x=2, y=2), PathNode(x=10, y=9), PathNode(x=18, y=3)]
    gradient = chapter.add_object(shape.layer_id, ColorFillGradientObject(
        field_type='line', line_field=LineGradientField(BoundGeometry.path(nodes))))
    window.hierarchy_model.set_chapter(chapter)
    canvas.set_selection('object', gradient.object_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._selected_shape_node_id = nodes[1].node_id
    no_full_serialization(monkeypatch, chapter)
    monkeypatch.setattr(first, 'to_dict', lambda: pytest.fail('Unrelated drawing serialized by point deletion'))
    monkeypatch.setattr(unrelated, 'to_dict', lambda: pytest.fail('Unrelated drawing serialized by point deletion'))
    stable = QPersistentModelIndex(window.hierarchy_model.index_for_entity('object', unrelated.object_id))
    resets = QSignalSpy(window.hierarchy_model.modelReset)
    revision = canvas.command_stack.revision
    canvas.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
    assert [node.node_id for node in gradient.line_field.geometry.nodes] == [nodes[0].node_id, nodes[2].node_id]
    assert canvas.command_stack.revision == revision + 1
    canvas.command_stack.undo()
    assert [node.node_id for node in gradient.line_field.geometry.nodes] == [node.node_id for node in nodes]
    canvas.command_stack.redo()
    assert len(gradient.line_field.geometry.nodes) == 2
    assert chapter.objects[unrelated.object_id] is unrelated
    assert stable.isValid() and resets.count() == 0
