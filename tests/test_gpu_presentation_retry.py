"""Coherent pressure retries cannot cross scene or native contact ownership."""
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QRect, QRectF
from PySide6.QtGui import QRegion

from comic_editor.core.changes import ChangeSet, DependencyIndex, EntityChange, ResourceChange
from comic_editor.core.models import RasterObject
from comic_editor.ui.document_projection_features import DocumentProjectionFeatures
from comic_editor.ui.scene_controller import SceneController
from test_gpu_contact_display_cadence import SingleShot, warm_contact


@pytest.fixture
def retry_contact(warm_contact):
    canvas = warm_contact
    canvas.chapter.objects[canvas.selected_id] = RasterObject()
    canvas.command_stack = SimpleNamespace(revision=4)
    canvas._projection_provisional_visible = False
    canvas._document_projection = SimpleNamespace(revision=3)
    controller = canvas._scene_controller
    controller._presentation_dependency_generation = 0
    controller._geometry_preview_dependency_generation = 0
    controller._geometry_preview_owner = None
    controller.visibility_presentation = SimpleNamespace(changed=lambda *_args: None)
    controller.geometry_continuity = SimpleNamespace(changed=lambda *_args: None)
    controller.feedback = SimpleNamespace(identifier=canvas.selected_id)
    controller._feedback_covers = lambda *_args: False
    controller._feedback_contact_covers = lambda *_args: True
    controller._covered_contact_gate = MethodType(SceneController._covered_contact_gate, controller)
    controller._owned_native_contact_gate = MethodType(SceneController._owned_native_contact_gate, controller)
    document = SimpleNamespace(live_preview=True, revision=3, bounds=QRectF(0, 0, 128, 128),
        identity=(id(canvas.chapter), id(canvas.tiles), id(canvas.images)), configuration=('native',))
    yield canvas, document


def context(canvas, document):
    return canvas._native_presentation_retry_context(document, document.bounds,
        complete=True, draw_live=False, phases=(None,))


def test_first_contact_can_replay_the_idle_exact_scene_key(retry_contact):
    canvas, document = retry_contact
    held = context(canvas, document)
    assert held is not None
    document.live_preview = False
    canvas._drawing = canvas._raster_contact_active = False
    assert context(canvas, document) == held


@pytest.mark.parametrize('transition', ['release', 'error', 'closed', 'owner', 'source',
    'history', 'dependency', 'camera-preview', 'coverage', 'busy', 'foreign-document'])
def test_replay_owner_and_dependency_transitions_reject_or_change_key(retry_contact, transition, monkeypatch):
    canvas, document = retry_contact
    original = context(canvas, document)
    assert original is not None
    gate = canvas._raster_tile_input
    if transition in {'release', 'error', 'closed', 'busy'}:
        setattr(gate, {'release': 'released'}.get(transition, transition),
            RuntimeError('input error') if transition == 'error' else True)
    elif transition == 'owner':
        gate.canvas = object()
    elif transition == 'source':
        canvas.tiles._tiles[canvas.selected_id] = object()
    elif transition == 'history':
        canvas.command_stack.revision += 1
    elif transition == 'dependency':
        canvas._scene_controller._presentation_dependency_generation += 1
    elif transition == 'camera-preview':
        monkeypatch.setattr(canvas, '_projection_has_live_preview', lambda **_kwargs: True)
    elif transition == 'coverage':
        canvas._scene_controller._feedback_contact_covers = lambda *_args: False
    else:
        document.identity = ('foreign',)
    assert context(canvas, document) != original


def controller_stub():
    return SimpleNamespace(canvas=SimpleNamespace(selected_id='selected', _render_document_state=lambda: None),
        _presentation_dependency_generation=0, _contact_preview_owner=None, _bootstrap=None, feedback=None,
        _geometry_preview_dependency_generation=0, _geometry_preview_owner=None,
        visibility_presentation=SimpleNamespace(changed=lambda *_args: None),
        geometry_continuity=SimpleNamespace(changed=lambda *_args: None),
        raster_continuity=SimpleNamespace(changed=lambda *_args: None),
        release_handoff=SimpleNamespace(changed=lambda *_args: None))


@pytest.mark.parametrize('change, action, retained', [
    (ChangeSet(resources=(ResourceChange(('object', 'selected'), 'raster', (0, 0)),), transient=True), 'transient', True),
    (ChangeSet(entities=(EntityChange(('object', 'selected'), frozenset({'pixels'})),), transient=True), 'transient', True),
    (ChangeSet(transient=True), 'transient', False),
    (ChangeSet(entities=(EntityChange(('object', 'selected'), frozenset({'name'})),), transient=True), 'transient', False),
    (ChangeSet(resources=(ResourceChange(('object', 'foreign'), 'raster', (0, 0)),), transient=True), 'transient', False),
    (ChangeSet(resources=(ResourceChange(('object', 'selected'), 'raster', (0, 0)),), transient=True), 'undo', False),
    (ChangeSet(resources=(ResourceChange(('object', 'selected'), 'raster', (0, 0)),)), 'transient', False),
])
def test_epoch_requires_positive_selected_transient_source_change_without_preview_owner(change, action, retained):
    controller = controller_stub()
    SceneController.changed(controller, change, action=action)
    assert controller._presentation_dependency_generation == (0 if retained else 1)


def test_legacy_feedback_retirement_invalidates_the_scene_epoch():
    controller = controller_stub()
    controller._retire_contact_preview = controller._retire_bootstrap = controller._retire_filtered = lambda: None
    controller.feedback_dirty = set()
    controller.feedback_patches = SimpleNamespace(clear=lambda: None)
    SceneController.retire_feedback(controller)
    assert controller._presentation_dependency_generation == 1


def test_stationary_pending_retry_is_bounded_and_ready_or_foreign_scene_cancels_it():
    class Timer(SingleShot):
        def stop(self):
            self.active = False
    timer = Timer()
    updates = []
    canvas = SimpleNamespace(_gpu_presentation_retry_timer=timer, update=lambda: updates.append(True))
    schedule = lambda pending: DocumentProjectionFeatures._schedule_native_presentation_retry(canvas, pending)
    schedule(True)
    schedule(True)
    assert timer.starts == [16]
    for _ in range(5):
        timer.active = False
        DocumentProjectionFeatures._retry_pending_native_presentation(canvas)
        schedule(True)
    assert timer.starts == [16, 32, 64, 128, 128, 128] and len(updates) == 5
    schedule(False)
    DocumentProjectionFeatures._retry_pending_native_presentation(canvas)
    assert not timer.active and len(updates) == 5
    schedule(True)
    assert timer.starts[-1] == 16


class StoppableTimer(SingleShot):
    def stop(self):
        self.active = False


def adopt_pressure_retry(canvas, document):
    canvas._visual_frame_timer = StoppableTimer()
    canvas._gpu_presentation_retry_timer = StoppableTimer()
    canvas._schedule_native_presentation_retry(True)
    canvas._raster_feedback_contact_covered = False
    canvas._raster_feedback_pending_visible = True
    canvas._adopt_native_presentation_retry(document, context(canvas, document))
    assert canvas._gpu_contact_pacing_current(pending=True)
    assert not canvas._gpu_contact_pacing_current()


@pytest.mark.parametrize('rotation', [0., 17.])
def test_continuing_busy_source_packets_share_one_retry_and_flush_latest_damage_once(
        retry_contact, monkeypatch, rotation):
    canvas, document = retry_contact
    canvas.rotation = rotation
    adopt_pressure_retry(canvas, document)
    canvas._raster_tile_input.busy = True
    timer = canvas._gpu_presentation_retry_timer
    canvas._visual_frame_timer.start(0)  # Earlier cursor damage must not re-fence the old scene.
    for index in range(200):
        world = QRectF(index, index + 1, 2, 3)
        canvas._visual_pending_world = canvas._visual_pending_world.united(world)
        canvas._queue_widget_dirty(QRect(index % 100, index % 100, 3, 4))
    assert timer.starts == [16] and timer.isActive()
    assert canvas._visual_frame_timer.starts == [0] and not canvas._visual_frame_timer.isActive()
    assert canvas._visual_pending_widget == QRegion(canvas.rect())
    assert not canvas._raster_feedback_contact_covered and canvas._raster_feedback_pending_visible
    assert canvas._raster_tile_input.busy and canvas._gpu_contact_pacing_current(pending=True)
    updates, published = [], []
    monkeypatch.setattr(canvas, 'update', lambda region=None: updates.append(region))
    canvas.visualChanged.connect(lambda world: published.append(QRectF(world)))
    expected = QRectF(canvas._visual_pending_world)
    timer.active = False
    canvas._retry_pending_native_presentation()
    assert updates == [QRegion(canvas.rect())] and published == [expected]
    assert canvas._visual_pending_widget.isEmpty() and canvas._visual_pending_world.isEmpty()
    canvas._schedule_native_presentation_retry(True)
    assert timer.starts == [16, 32]
    # Stationary pressure has one future paint even without any input damage.
    timer.active = False
    canvas._retry_pending_native_presentation()
    assert len(updates) == 2 and updates[-1] is None


@pytest.mark.parametrize('transition', ['release', 'camera', 'history', 'selection', 'context',
    'dependency', 'source', 'feedback', 'error'])
def test_stale_pressure_owner_promotes_damage_immediately(retry_contact, monkeypatch, transition):
    canvas, document = retry_contact
    adopt_pressure_retry(canvas, document)
    canvas._queue_widget_dirty(QRect(3, 4, 5, 6))
    assert not canvas._visual_frame_timer.isActive()
    if transition == 'release':
        canvas._raster_tile_input.released = True
        canvas._drawing = False
    elif transition == 'camera':
        canvas.center_x += 1.
    elif transition == 'history':
        canvas.command_stack.revision += 1
    elif transition == 'selection':
        canvas.selected_id = 'foreign'
    elif transition == 'context':
        canvas._gpu_contact_pacing_context = object()
    elif transition == 'dependency':
        canvas._scene_controller._presentation_dependency_generation += 1
    elif transition == 'source':
        canvas.tiles._tiles[canvas.selected_id] = object()
    elif transition == 'feedback':
        canvas._scene_controller.feedback = object()
    else:
        canvas._raster_tile_input.error = RuntimeError('input stopped')
    assert not canvas._gpu_contact_pacing_current(pending=True)
    monkeypatch.setattr('comic_editor.ui.canvas.time.perf_counter', lambda: 100.)
    canvas._queue_widget_dirty(QRect(110, 112, 8, 9))
    assert canvas._visual_frame_timer.starts == [0]
    assert canvas._visual_pending_widget == QRegion(canvas.rect())


def test_already_validated_scene_survives_busy_chunk_without_claiming_fresh_coverage(retry_contact):
    canvas, document = retry_contact
    original = context(canvas, document)
    canvas._raster_tile_input.busy = True
    assert context(canvas, document) is None  # Cold busy admission cannot opt itself in.
    canvas._raster_tile_input.busy = False
    adopt_pressure_retry(canvas, document)
    canvas._raster_tile_input.busy = True
    canvas._scene_controller._feedback_contact_covers = lambda *_args: False
    assert context(canvas, document) == original
    assert not canvas._raster_feedback_contact_covered and canvas._raster_feedback_pending_visible
    document.configuration = ('new-native-configuration',)
    assert context(canvas, document) is None


@pytest.mark.parametrize('known_bounds', [True, False])
def test_typed_source_publication_keeps_one_pressure_retry_without_losing_invalidation(
        retry_contact, monkeypatch, known_bounds):
    canvas, document = retry_contact
    chapter, controller = canvas.chapter, canvas._scene_controller
    chapter.layers, chapter.modifiers, chapter.masks = {}, {}, {}
    canvas._change_index = DependencyIndex(chapter)
    invalidated, snapshots, continuity, published, updates = [], [], [], [], []
    canvas._scene_snapshot_compiler = SimpleNamespace(invalidate=snapshots.append)
    canvas._render_bounds = SimpleNamespace(invalidate_refs=lambda refs: invalidated.append(set(refs)))
    canvas._render_service = SimpleNamespace(invalidate=lambda world=None: continuity.append(world))
    canvas._scene_dirty_widget = QRect()
    canvas._painting_overlay_damage = QRegion()
    controller._contact_preview_owner = controller._bootstrap = None
    controller.feedback_dirty = set()
    controller.raster_continuity = SimpleNamespace(changed=lambda *_args: None)
    controller.release_handoff = SimpleNamespace(changed=lambda *_args: None)
    controller.changed = MethodType(SceneController.changed, controller)
    monkeypatch.setattr(canvas, '_render_document_state', lambda: document)
    monkeypatch.setattr(canvas, '_world_dirty_to_widget', lambda _world: canvas.rect())
    overlay_reads = []
    monkeypatch.setattr(canvas, '_painting_overlay_region',
        lambda: overlay_reads.append(True) or QRegion(QRect(5, 6, 2, 3)))
    monkeypatch.setattr(canvas, 'update', lambda region=None: updates.append(region))
    canvas.changesPublished.connect(published.append)
    adopt_pressure_retry(canvas, document)
    canvas._raster_tile_input.busy = True
    selected = ('object', canvas.selected_id)
    changes = []
    for index in range(200):
        world = QRectF(index, index + 1, 2, 3)
        bounds = tuple(world.getRect()) if known_bounds else None
        change = ChangeSet(
            entities=(EntityChange(selected, frozenset({'pixels'}), bounds, bounds),),
            resources=(ResourceChange(selected, 'raster', (index, 0)),), transient=True)
        assert not canvas._live_raster_repaint(change, 'transient')
        canvas._publish_change_set(change)
        # Native drawing publishes preview damage after its typed source packet.
        canvas._queue_visual_dirty(world, scene=False)
        canvas._queue_painting_overlay_damage()
        changes.append(change)
    assert published == snapshots == changes
    assert canvas._change_index.revision == canvas._change_index.generation(*selected) == 200
    assert invalidated == [{selected}] * 200 and len(continuity) == 200
    assert controller.feedback_dirty == {(index, 0) for index in range(200)}
    assert controller._presentation_dependency_generation == 0
    assert updates == [] and canvas._visual_frame_timer.starts == []
    assert overlay_reads == []
    assert canvas._gpu_presentation_retry_timer.starts == [16]
    assert canvas._visual_pending_widget == QRegion(canvas.rect())
    assert canvas._visual_pending_world == QRectF(0, 1, 201, 202)
    assert not canvas._raster_feedback_contact_covered and canvas._raster_feedback_pending_visible
    # Committed history must wake immediately through this same publication path.
    committed = ChangeSet(entities=changes[-1].entities, resources=changes[-1].resources)
    canvas._publish_change_set(committed, action='commit')
    assert updates == [None] and published[-1] is committed
    assert canvas._change_index.revision == 201
    assert not canvas._gpu_contact_pacing_current(pending=True)


@pytest.mark.parametrize('transition', ['release', 'camera', 'dependency', 'context'])
def test_pending_cursor_packets_coalesce_until_annotation_owner_changes(
        retry_contact, monkeypatch, transition):
    canvas, document = retry_contact
    canvas.rotation = 17.
    canvas._painting_overlay_damage = QRegion()
    adopt_pressure_retry(canvas, document)
    canvas._raster_tile_input.busy = True
    updates = []
    monkeypatch.setattr(canvas, 'update', lambda region=None: updates.append(region))
    monkeypatch.setattr(canvas, '_painting_overlay_region', lambda: QRegion(QRect(5, 6, 2, 3)))
    for _ in range(200):
        canvas._queue_painting_overlay_damage()
    assert updates == [] and canvas._visual_frame_timer.starts == []
    assert canvas._gpu_presentation_retry_timer.starts == [16]
    assert canvas._visual_pending_widget == QRegion(canvas.rect())
    assert not canvas._raster_feedback_contact_covered and canvas._raster_feedback_pending_visible
    if transition == 'release':
        canvas._raster_tile_input.released = True
        canvas._drawing = False
    elif transition == 'camera':
        canvas.center_x += 1.
    elif transition == 'dependency':
        canvas._scene_controller._presentation_dependency_generation += 1
    else:
        canvas._gpu_contact_pacing_context = object()
    assert not canvas._gpu_contact_pacing_current(pending=True)
    canvas._queue_painting_overlay_damage()
    assert updates == [None]  # The changed pending annotation remains immediate.
    assert canvas._visual_frame_timer.starts == [0]


def test_all_borrowed_replay_leases_are_checked_before_the_first_draw():
    from comic_editor.ui.document_presentation import GpuTilePresenter, _BorrowedTexture
    closed = [False]
    borrowed = object.__new__(_BorrowedTexture)
    borrowed.image = SimpleNamespace(isNull=lambda: closed[0])
    presenter = SimpleNamespace(_textures={
        'native-prefix': SimpleNamespace(texture=object()),
        'borrowed-tail': SimpleNamespace(texture=borrowed)})
    keys = ('native-prefix', 'borrowed-tail')
    assert GpuTilePresenter._frame_available(presenter, keys)
    closed[0] = True
    assert not GpuTilePresenter._frame_available(presenter, keys)
    borrowed.image = None
    assert not GpuTilePresenter._frame_available(presenter, keys)
    assert not GpuTilePresenter._frame_available(presenter, ('native-prefix', 'missing'))
