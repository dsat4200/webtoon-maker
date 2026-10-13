"""A current reveal frame appears while native work is held; contexts retire it."""
from threading import Event

import pytest

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.tools import ToolKind
from comic_editor.render.scheduler import SceneScheduler
from test_geometry_preview_publication import scene, exact, native_reference, pump, wait, pixels


def assert_native_tiles_match_cold_scene(canvas):
    from PySide6.QtCore import QSizeF
    from PySide6.QtGui import QColor, QImage, QPainter, QTransform
    from comic_editor.render.projection import ProjectionRequest
    from comic_editor.ui.document_presentation import draw_document_tiles
    document, addresses, phases, _visible = canvas._scene_controller.desired
    requests = tuple(ProjectionRequest(address) for address in addresses)
    batch, complete = canvas._scene_controller.ready_batch(document, requests, phases)
    assert complete and phases == (None,)
    image = QImage(256, 256, document.pixel_contract.image_format)
    image.fill(QColor('transparent'))
    painter = QPainter(image)
    try:
        draw_document_tiles(painter, batch[0][1], QTransform(), QSizeF(256,256), smooth=False)
    finally:
        painter.end()
    assert pixels(image) == pixels(native_reference(canvas))


def visible(canvas, shape, value):
    shape.visible = value
    change = ChangeSet((EntityChange(('layer', shape.layer_id), frozenset({'visible'})),),
                       label='Toggle visibility')
    canvas._publish_change_set(change, action='commit')


def change_hidden_artwork(canvas, shape):
    # This reveal has no recently rendered visible native state. Its optional
    # provisional presentation must be tested independently of warm toggles.
    shape.opacity = .95
    canvas._publish_change_set(ChangeSet((EntityChange(('layer', shape.layer_id),
        frozenset({'opacity'})),), label='Changed hidden artwork'), action='commit')


@pytest.mark.parametrize('retire', [None, 'view', 'selection', 'tool', 'history', 'source', 'foreign', 'hide'])
def test_current_reveal_is_visible_before_native_and_cannot_survive_other_contexts(scene, qapp, monkeypatch, retire):
    canvas, shape = scene
    visible(canvas, shape, False)
    change_hidden_artwork(canvas, shape)
    exact(canvas, qapp)
    hidden = pump(canvas, qapp)
    held, release = Event(), Event()
    controller = canvas._scene_controller
    ordinary = SceneScheduler._evaluate
    demands = []
    target_revision = canvas._document_projection.revision + 1
    def evaluate(scheduler, demand, token):
        if scheduler is controller.scheduler and demand.snapshot.document.revision >= target_revision:
            demands.append(demand)
            if demand.visibility_preview_owner is None:
                held.set()
                assert release.wait(10)
        return ordinary(scheduler, demand, token)
    monkeypatch.setattr(SceneScheduler, '_evaluate', evaluate)
    try:
        visible(canvas, shape, True)
        wait(canvas, qapp, lambda: held.is_set() and controller.visibility_presentation.preview is not None)
        surface = pump(canvas, qapp)  # Observe the paint after GUI acceptance.
        state = controller.visibility_presentation
        shown = state.preview
        assert surface != hidden
        assert shown[0] == canvas._render_document_state() and not shown[0].live_preview
        assert canvas._projection_provisional_visible and canvas._projection_frame_pending
        assert len(demands) == 2
        preview_demand, native_demand = demands
        assert preview_demand.visibility_preview_owner == state.owner
        assert preview_demand.snapshot.cache_spec is None and not preview_demand.record
        assert native_demand.visibility_preview_owner is None and native_demand.preview_size is None
        assert state.for_request(controller.desired) is shown
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
            assert state.for_request(controller.desired) is None
            # Real drawing publishes source changes after editing its borrower;
            # native replacement must use that same invalidation stream.
            canvas._publish_change_set(ChangeSet((EntityChange(('object', identifier),
                frozenset({'pixels'})),), label='Changed source'), action='commit')
        elif retire == 'foreign':
            shape.opacity = .6
            canvas._publish_change_set(ChangeSet((EntityChange(('layer', shape.layer_id), frozenset({'opacity'})),),
                label='Other edit'), action='commit')
        elif retire == 'hide':
            visible(canvas, shape, False)
        if retire:
            pump(canvas, qapp)
            assert state.for_request(controller.desired) is None
            assert controller.preview is not shown
        release.set()
        exact(canvas, qapp)
        assert state.preview is None
        # An independent cold native evaluator agrees with the committed
        # geometry/source, irrespective of whether a preview was presented.
        assert_native_tiles_match_cold_scene(canvas)
        if retire is None:
            canvas._draw_selection = lambda _: None
            assert pump(canvas, qapp) != hidden
    finally:
        release.set()


@pytest.mark.parametrize('change', [None, 'view', 'selection', 'tool', 'history', 'source', 'foreign', 'reset', 'cache-miss'])
def test_recent_native_visibility_avoids_extra_preview_and_other_contexts_do_not(scene, qapp, monkeypatch, change):
    canvas, shape = scene
    controller = canvas._scene_controller
    state = controller.visibility_presentation
    assert state.native_views
    visible(canvas, shape, False)
    exact(canvas, qapp)
    assert len(state.native_views) == 2
    if change == 'view':
        canvas.center_x += 8
    elif change == 'selection':
        canvas.set_selection('layer', canvas.chapter.root_page_ids[0])
    elif change == 'tool':
        canvas.set_tool(ToolKind.OBJECT_SELECT)
    elif change == 'history':
        canvas.command_stack.clear()
    elif change == 'source':
        from PySide6.QtGui import QColor
        identifier = next(iter(canvas.chapter.objects))
        canvas.tiles.tile(identifier, (0,0)).fill(QColor('red'))
        canvas._publish_change_set(ChangeSet((EntityChange(('object', identifier),
            frozenset({'pixels'})),), label='Changed source'), action='commit')
    elif change == 'foreign':
        change_hidden_artwork(canvas, shape)
    elif change == 'reset':
        controller.reset()
        assert not state.native_views
    elif change == 'cache-miss':
        previous = controller.scheduler
        previous.close()
        previous.executor.shutdown(wait=True)
        controller.scheduler = SceneScheduler(ready_callback=previous.ready_callback)
        assert controller.scheduler._backend is None and controller.scheduler._cache_state is None
    original = SceneScheduler._evaluate
    demands = []
    def evaluate(scheduler, demand, token):
        if scheduler is controller.scheduler:
            demands.append(demand)
        return original(scheduler, demand, token)
    monkeypatch.setattr(SceneScheduler, '_evaluate', evaluate)
    visible(canvas, shape, True)
    exact(canvas, qapp)
    previews = [demand for demand in demands if demand.visibility_preview_owner is not None]
    assert bool(previews) == (change not in (None, 'cache-miss'))
    assert_native_tiles_match_cold_scene(canvas)
    assert not state.preview
    assert len(state.native_views) <= 2


def test_only_two_recent_native_states_are_retained_with_recent_use_order(scene, qapp):
    canvas, shape = scene
    state = canvas._scene_controller.visibility_presentation
    shown_key = state.native_views[0]
    visible(canvas, shape, False)
    exact(canvas, qapp)
    hidden_key = state.native_views[-1]
    visible(canvas, shape, True)
    exact(canvas, qapp)
    identifier, record = next(iter(canvas.chapter.objects.items()))
    record.visible = False
    canvas._publish_change_set(ChangeSet((EntityChange(('object',identifier),
        frozenset({'visible'})),), label='Hide child'), action='commit')
    exact(canvas, qapp)
    assert len(state.native_views) == 2
    assert shown_key in state.native_views and hidden_key not in state.native_views
    assert_native_tiles_match_cold_scene(canvas)


@pytest.mark.parametrize('retire', [None, 'view', 'show', 'source'])
def test_large_first_hide_shows_current_artwork_before_native_and_retires(scene, qapp, monkeypatch, retire):
    from comic_editor.core.models import BoundGeometry
    canvas, shape = scene
    for index in range(16):
        canvas.chapter.add_layer(shape.layer_id, f'Child {index}',
            bound=BoundGeometry.rectangle(40.,40.,1.,1.))
    canvas._publish_change_set(ChangeSet(conservative=True, label='Added children'), action='commit')
    exact(canvas, qapp)
    initial = pump(canvas, qapp)
    controller = canvas._scene_controller
    held, release = Event(), Event()
    target_revision = canvas._document_projection.revision + 1
    original = SceneScheduler._evaluate
    demands = []
    def evaluate(scheduler, demand, token):
        if scheduler is controller.scheduler and demand.snapshot.document.revision >= target_revision:
            demands.append(demand)
            if demand.visibility_preview_owner is None:
                held.set()
                assert release.wait(10)
        return original(scheduler, demand, token)
    monkeypatch.setattr(SceneScheduler, '_evaluate', evaluate)
    try:
        visible(canvas, shape, False)
        wait(canvas, qapp, lambda: held.is_set() and controller.visibility_presentation.preview is not None)
        state = controller.visibility_presentation
        shown = state.preview
        assert pump(canvas, qapp) != initial
        assert shown[0] == canvas._render_document_state() and not shown[0].live_preview
        assert canvas._projection_frame_pending and canvas._projection_provisional_visible
        assert demands[0].snapshot.cache_spec is None and not demands[0].record
        assert demands[0].visibility_preview_owner == state.owner
        if retire == 'view':
            canvas.center_x += 8
        elif retire == 'show':
            visible(canvas, shape, True)
        elif retire == 'source':
            from PySide6.QtGui import QColor
            identifier = next(iter(canvas.chapter.objects))
            canvas.tiles.tile(identifier,(0,0)).fill(QColor('red'))
            canvas._publish_change_set(ChangeSet((EntityChange(('object',identifier),
                frozenset({'pixels'})),), label='Changed source'), action='commit')
        if retire:
            pump(canvas, qapp)
            assert state.for_request(controller.desired) is None and controller.preview is not shown
        release.set()
        exact(canvas, qapp)
        assert_native_tiles_match_cold_scene(canvas)
        if retire == 'source':
            visible(canvas, shape, True)
            exact(canvas, qapp)
            assert_native_tiles_match_cold_scene(canvas)
    finally:
        release.set()


def test_reveal_presentation_cannot_record_or_adopt_derived_native_cache(scene, qapp, monkeypatch):
    from dataclasses import replace
    from comic_editor.render.scene import DetachedSceneBackend
    from comic_editor.render.live_canvas_preview import VisibilityCanvasPreviewPolicy, live_canvas_policy
    from comic_editor.render.admission import WorkCancelled
    canvas, shape = scene
    visible(canvas, shape, False)
    change_hidden_artwork(canvas, shape)
    exact(canvas, qapp)
    held, release = Event(), Event()
    controller = canvas._scene_controller
    ordinary = SceneScheduler._evaluate
    evaluated = []
    def evaluate(scheduler, demand, token):
        if scheduler is controller.scheduler:
            if demand.visibility_preview_owner is not None:
                evaluated.append(demand)
            elif evaluated:
                held.set()
                assert release.wait(10)
        return ordinary(scheduler, demand, token)
    monkeypatch.setattr(SceneScheduler, '_evaluate', evaluate)
    try:
        visible(canvas, shape, True)
        wait(canvas, qapp, held.is_set)
        demand = evaluated[0]
        assert controller.scheduler._visibility_preview_bounded(demand)
        assert not controller.scheduler._visibility_preview_bounded(replace(demand, record=True))
        assert not controller.scheduler._visibility_preview_bounded(replace(demand, consumer='cache'))
        assert not controller.scheduler._visibility_preview_bounded(replace(demand, presentation_size=(256,256)))
        from comic_editor.core.pixel_contract import FLOAT_PIXELS
        for fields in ({'live_preview': True}, {'contact_only': True}, {'overflow': 10.},
                       {'underlay': ('#ffffff', .5)}, {'pixel_contract': FLOAT_PIXELS}):
            unsupported = replace(demand.snapshot.document, **fields)
            assert not controller.scheduler._visibility_preview_bounded(replace(demand,
                snapshot=replace(demand.snapshot, document=unsupported)))
            assert controller.visibility_presentation.key(unsupported,
                demand.requests, demand.phases, demand.visible) is None
        for fields in ({'consumer':'export'}, {'phases':('base',)}, {'preview_size':(0,256)},
                       {'preview_size':(100000,100000)}, {'feedback_only':True},
                       {'feedback_target':'foreign-source'}):
            assert not controller.scheduler._visibility_preview_bounded(replace(demand, **fields))
        backend = DetachedSceneBackend(demand.snapshot)
        try:
            assert not backend._record_allowed()
            assert backend.scene._persistent_render_cache is None
            policy = VisibilityCanvasPreviewPolicy(demand.snapshot.document.identity,
                demand.snapshot.document.revision, demand.serial, lambda: True)
            backend.scene._live_canvas_preview_policy = policy
            backend.scene._effect_preview_channel = 'canvas'
            assert live_canvas_policy(backend.scene) is policy
            with pytest.raises(WorkCancelled):
                policy.check_cancelled()
            for name in ('_render_base_alpha','_rendering_mask_contributor','_rendering_halftone_source',
                         '_rendering_compound_references','_tiling_capture_geometry'):
                previous = getattr(backend.scene, name, None)
                setattr(backend.scene, name, True)
                assert live_canvas_policy(backend.scene) is None
                setattr(backend.scene, name, previous)
        finally:
            backend.close()
    finally:
        release.set()
