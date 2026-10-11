"""A transient editing overlay follows current input, view and guide settings."""
import pytest
from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import QFont

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.tools import ToolKind
from test_native_contact_partial_repaint import contact_canvas, packet, pump, snapshot


def begin(canvas, qapp):
    canvas.scale = .57
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.settings.pencil_transform_handles_visible = True
    canvas.repaint()
    pump(qapp)
    packet(canvas, QEvent.TabletPress, QPointF(180., 150.), 0)
    pump(qapp)
    assert canvas._drawing
    canvas.repaint()
    assert canvas._painting_selection_cache is not None
    return canvas._painting_selection_cache


def assert_cached_matches_uncached_widget(canvas, monkeypatch):
    canvas.repaint()
    actual = snapshot(canvas)
    with monkeypatch.context() as patch:
        patch.setattr(canvas, '_draw_painting_selection', canvas._draw_selection)
        canvas.repaint()
        expected = snapshot(canvas)
    assert actual.size() == expected.size()
    assert bytes(actual.constBits()) == bytes(expected.constBits())


def test_stable_gizmo_overlay_reuses_commands_and_matches_uncached_widget(
        contact_canvas, qapp, monkeypatch):
    canvas, _selected = contact_canvas
    cached = begin(canvas, qapp)
    retained_overlay = cached[1]
    from comic_editor.ui.painting_gizmos import PaintingGizmoCommands, MAX_GIZMO_BYTES, MAX_GIZMO_COMMANDS
    assert isinstance(retained_overlay, PaintingGizmoCommands)
    assert 0 < retained_overlay.byte_count <= MAX_GIZMO_BYTES
    assert 0 < retained_overlay.command_count <= MAX_GIZMO_COMMANDS
    called = []
    original = canvas._draw_selection
    monkeypatch.setattr(canvas, '_draw_selection', lambda painter: (called.append(True), original(painter)))
    canvas.repaint()
    assert canvas._painting_selection_cache[1] is retained_overlay
    assert not called
    assert_cached_matches_uncached_widget(canvas, monkeypatch)
    assert called


@pytest.mark.parametrize('change', ['view', 'handles', 'mode', 'font', 'frame', 'solo'])
def test_current_view_guide_and_frame_cannot_reuse_stale_overlay(
        contact_canvas, change, qapp, monkeypatch):
    canvas, selected = contact_canvas
    cached = begin(canvas, qapp)
    if change == 'view':
        canvas.center_x += 7.25
    elif change == 'handles':
        canvas.settings.pencil_transform_handles_visible = False
    elif change == 'mode':
        canvas.settings.transform_mode = 'uniform' if canvas.settings.transform_mode != 'uniform' else 'free'
    elif change == 'font':
        font = QFont(canvas.font())
        font.setFamily('Courier New')
        canvas.setFont(font)
    elif change == 'solo':
        other = next(obj for obj in canvas.chapter.objects.values() if obj.object_id != selected.object_id)
        canvas.set_solo_entities({('object', other.object_id)})
    else:
        selected.interaction_rect = (30., 30., 600., 420.)
    canvas.repaint()
    assert canvas._painting_selection_cache is not None
    assert canvas._painting_selection_cache[1] is not cached[1]
    assert_cached_matches_uncached_widget(canvas, monkeypatch)


def test_modifier_ribbon_state_cannot_replay_previous_selection(
        contact_canvas, qapp, wait_scene, monkeypatch):
    from comic_editor.core.models import TextureModifier
    canvas, selected = contact_canvas
    modifier = TextureModifier(texture_quad=[(48., 48.), (560., 48.), (560., 432.), (48., 432.)])
    canvas.chapter.add_modifier(modifier, [('object', selected.object_id)])
    canvas.active_modifier_id = modifier.modifier_id
    canvas.modifier_mode = False
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    begin(canvas, qapp)
    # Ribbon changes are presentation state: the document/effect is unchanged.
    canvas.modifier_mode = True
    assert canvas._active_texture_modifier() is modifier
    canvas.repaint()
    assert canvas._painting_selection_cache is None
    assert_cached_matches_uncached_widget(canvas, monkeypatch)


def test_unrelated_document_change_retires_overlay_before_current_repaint(contact_canvas, qapp):
    canvas, selected = contact_canvas
    begin(canvas, qapp)
    other = next(obj for obj in canvas.chapter.objects.values() if obj.object_id != selected.object_id)
    other.opacity = .5
    canvas._publish_change_set(ChangeSet((EntityChange(('object', other.object_id), frozenset({'opacity'})),),
                                         transient=True))
    assert canvas._painting_selection_cache is None


def test_release_and_selection_change_retire_transient_overlay(contact_canvas, qapp):
    canvas, selected = contact_canvas
    begin(canvas, qapp)
    packet(canvas, QEvent.TabletRelease, QPointF(180., 150.), 1)
    pump(qapp)
    canvas.repaint()
    assert canvas._painting_selection_cache is None
    canvas.set_selection('layer', selected.parent_layer_id, activate_default_tool=False)
    canvas.repaint()
    assert canvas._painting_selection_cache is None


def test_command_budget_fallback_keeps_direct_gizmos_without_retention(
        contact_canvas, qapp, monkeypatch):
    import comic_editor.ui.painting_gizmos as gizmos
    canvas, _selected = contact_canvas
    begin(canvas, qapp)
    monkeypatch.setattr(gizmos, 'MAX_GIZMO_COMMANDS', 1)
    canvas.center_x += 7.25
    canvas.repaint()
    assert canvas._painting_selection_cache is None
    assert_cached_matches_uncached_widget(canvas, monkeypatch)
