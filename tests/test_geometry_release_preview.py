"""Quick real releases show current artwork without promoting unfinished pixels."""
from copy import deepcopy
from threading import Event

import pytest

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.tools import ToolKind
from comic_editor.render.geometry_capture import GeometrySceneSnapshot
from comic_editor.render.scheduler import SceneScheduler
from test_geometry_preview_publication import scene, begin, exact, pump, wait
from test_visibility_preview_publication import assert_native_tiles_match_cold_scene


@pytest.mark.parametrize('kind', ['boundary', 'translate'])
@pytest.mark.parametrize('started', [False, True])
def test_unpainted_real_release_renders_current_committed_preview_before_native(scene, qapp, monkeypatch, kind, started):
    canvas, shape = scene
    controller = canvas._scene_controller
    before = deepcopy(canvas.chapter.to_dict())
    initial = pump(canvas, qapp)
    held, release = Event(), Event()
    live_started, live_release = Event(), Event()
    demands = []
    ordinary = SceneScheduler._evaluate
    def evaluate(scheduler, demand, token):
        if scheduler is controller.scheduler:
            if demand.snapshot.document.live_preview:
                live_started.set()
                assert live_release.wait(10)
                return ordinary(scheduler, demand, token)
            demands.append(demand)
            if demand.visibility_preview_owner is None:
                held.set()
                assert release.wait(10)
        return ordinary(scheduler, demand, token)
    monkeypatch.setattr(SceneScheduler, '_evaluate', evaluate)
    try:
        begin(canvas, kind)  # Deliberately no paint/event pumping before release.
        if started:
            wait(canvas, qapp, live_started.is_set)
            assert controller._geometry_preview_owner is not None
        assert controller.geometry_continuity.last is None
        canvas._tool_release()
        state = controller.visibility_presentation
        assert state.purpose == 'geometry-release'
        assert controller.geometry_continuity.ticket is None
        assert controller.release_handoff.ticket is None
        if started:
            assert controller.desired is None
            controller.advance()  # Queued timer arrives before committed paint.
            assert state.waiting_for_first_request()
        live_release.set()
        committed = deepcopy(canvas.chapter.to_dict())
        assert committed != before
        wait(canvas, qapp, lambda: held.is_set() and state.preview is not None)
        assert pump(canvas, qapp) != initial
        shown = state.preview
        assert shown[0] == canvas._render_document_state() and not shown[0].live_preview
        assert shown[1].key == ('geometry-release-preview', controller.serial)
        assert state.for_request(controller.desired) is shown
        assert canvas._projection_provisional_visible and canvas._projection_frame_pending
        assert len(demands) == 2
        draft, native = demands
        assert draft.visibility_preview_owner == state.owner
        assert not isinstance(draft.snapshot, GeometrySceneSnapshot)
        assert draft.snapshot.cache_spec is None and not draft.record
        assert draft.snapshot.document == native.snapshot.document
        assert native.visibility_preview_owner is None and native.preview_size is None
        release.set()
        exact(canvas, qapp)
        assert state.preview is None
        assert canvas.chapter.to_dict() == committed
        assert_native_tiles_match_cold_scene(canvas)
        canvas.command_stack.undo()
        exact(canvas, qapp)
        assert canvas.chapter.to_dict() == before
        assert_native_tiles_match_cold_scene(canvas)
        canvas.command_stack.redo()
        exact(canvas, qapp)
        assert canvas.chapter.to_dict() == committed
        assert_native_tiles_match_cold_scene(canvas)
    finally:
        live_release.set()
        release.set()


@pytest.mark.parametrize('kind', ['boundary', 'translate'])
@pytest.mark.parametrize('retire', ['view', 'selection', 'tool', 'history', 'source', 'foreign', 'undo', 'command_owner'])
def test_quick_release_cannot_first_admit_after_context_changes(scene, qapp, monkeypatch, kind, retire):
    canvas, shape = scene
    controller = canvas._scene_controller
    begin(canvas, kind)
    canvas._tool_release()
    state = controller.visibility_presentation
    assert state.purpose == 'geometry-release' and state.owner is None
    if retire == 'view':
        canvas.center_x += 8
    elif retire == 'selection':
        canvas.set_selection('layer', canvas.chapter.root_page_ids[0])
    elif retire == 'tool':
        canvas.set_tool(ToolKind.OBJECT_SELECT)
    elif retire == 'history':
        canvas.command_stack.clear()
    elif retire == 'source':
        from PySide6.QtGui import QColor
        identifier = next(iter(canvas.chapter.objects))
        canvas.tiles.tile(identifier, (0, 0)).fill(QColor('red'))
    elif retire == 'foreign':
        shape.opacity = .6
        canvas._publish_change_set(ChangeSet((EntityChange(('layer', shape.layer_id),
            frozenset({'opacity'})),), label='Other edit'), action='commit')
    elif retire == 'undo':
        canvas.command_stack.undo()
    elif retire == 'command_owner':
        from copy import copy
        current = canvas.command_stack.top_undo_command
        replacement = copy(current)
        assert replacement == current and replacement is not current
        canvas.command_stack._undo[-1] = replacement
    # Guard the first admission, before any request has frozen an owner/key.
    document = canvas._render_document_state()
    assert state.key(document, controller.desired[1], controller.desired[2],
        tuple(canvas.visible_document_rect().intersected(document.bounds).getRect())) is None
    presented = []
    ordinary = state.accept
    monkeypatch.setattr(state, 'accept', lambda *args: (presented.append(args), ordinary(*args))[1])
    exact(canvas, qapp)
    assert not presented and state.preview is None
