"""Global shape damage stays correct without reading all child source bounds."""
import pytest
from PySide6.QtCore import QPointF, QRectF

from comic_editor.core.changes import ChangeSet
from comic_editor.core.models import HalftoneModifier, ToneMask
from test_geometry_preview_publication import scene, exact, native_reference, pixels
from test_visibility_preview_publication import assert_native_tiles_match_cold_scene


@pytest.mark.parametrize('scope', ['mask', 'spatial'])
def test_global_boundary_damage_keeps_current_pixels_and_history_without_source_walk(
        scene, qapp, monkeypatch, scope):
    canvas, shape = scene
    if scope == 'mask':
        mask = ToneMask(saved=True, contributors=[('layer', shape.layer_id)])
        canvas.chapter.masks[mask.mask_id] = mask
    else:
        canvas.chapter.add_modifier(HalftoneModifier(blur=0, spacing=6),
            [('layer', shape.layer_id)])
    canvas._publish_change_set(ChangeSet(conservative=True), action='commit')
    exact(canvas, qapp)
    initial = pixels(native_reference(canvas))
    initial_model = canvas.chapter.to_dict()
    assert canvas._entity_dirty_scope('layer', shape.layer_id)[2] == scope
    changes, notices = [], []
    canvas.changesPublished.connect(changes.append)
    canvas.documentChanged.connect(notices.append)
    canvas._tool_press(canvas.document_to_widget(QPointF(182., 182.)), 1.)
    assert canvas._active_shape_control

    def source_walk(*_args, **_kwargs):
        pytest.fail('Global pointer damage must not traverse child source bounds')
    with monkeypatch.context() as patch:
        patch.setattr('comic_editor.ui.canvas.entity_visual_bounds', source_walk)
        canvas._tool_move(canvas.document_to_widget(QPointF(207., 199.)), 1.)
    assert canvas._scene_dirty_full
    assert notices[-1] == QRectF(0, 0, 256, 256)
    change = changes[-1]
    assert change.transient and not change.conservative and change.entities
    assert all(item.old_bounds is None and item.new_bounds is None for item in change.entities)
    canvas._tool_release()
    exact(canvas, qapp)
    assert_native_tiles_match_cold_scene(canvas)
    final, final_model = pixels(native_reference(canvas)), canvas.chapter.to_dict()
    assert final != initial
    canvas.command_stack.undo()
    exact(canvas, qapp)
    assert_native_tiles_match_cold_scene(canvas)
    assert pixels(native_reference(canvas)) == initial
    assert canvas.chapter.to_dict() == initial_model
    canvas.command_stack.redo()
    exact(canvas, qapp)
    assert_native_tiles_match_cold_scene(canvas)
    assert pixels(native_reference(canvas)) == final
    assert canvas.chapter.to_dict() == final_model
