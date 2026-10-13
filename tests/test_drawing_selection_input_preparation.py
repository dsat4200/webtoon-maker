"""Cold area transforms prepare originals without decoding in pointer callbacks."""
from threading import Event, Thread, get_ident
import time

import pytest
from PySide6.QtCore import QCoreApplication, QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tile_backing import TileResidency
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.002)
    assert predicate()


def select_region(canvas):
    canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    path = QPainterPath()
    path.addRect(QRectF(16, 16, 128, 128))
    canvas._drawing_selection_path = path
    canvas._refresh_drawing_selection_transform()


@pytest.fixture(params=[QImage.Format_RGBA8888_Premultiplied,
                        QImage.Format_RGBA64_Premultiplied])
def source(qapp, tmp_path, monkeypatch, request):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    chapter = ChapterDocument(width=512, height=256, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 512, 256))
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 512, 256)))
    tiles = TileStore(cache_budget=256 * 256 * 8)
    originals = {}
    for x in range(2):
        image = QImage(256, 256, request.param)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        painter.fillRect(QRectF(20, 20, 100, 100),
                         QColor.fromRgbF(.3123, .5345, .7967, .4132))
        painter.fillRect(QRectF(190, 170, 42, 37),
                         QColor.fromRgbF(.7231, .1645, .2867, .8152))
        painter.end()
        path = tmp_path / f'{x}.png'
        assert image.save(str(path))
        originals[(x, 0)] = QImage(str(path))
        if originals[(x, 0)].depth() <= 32:
            originals[(x, 0)] = originals[(x, 0)].convertToFormat(QImage.Format_ARGB32_Premultiplied)
        tiles._object_tiles(obj.object_id).register((x, 0), path)
    tiles._alpha_bounds[obj.object_id] = {
        key: TileStore._alpha_bbox(image) for key, image in originals.items()}
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', snap_to_grid=False,
                                        grid_overlay_visible=False, transform_mode='free'))
    canvas.set_document(chapter, tiles)
    canvas.set_selection('object', obj.object_id)
    canvas.scale = 1.
    select_region(canvas)
    yield canvas, obj, originals
    canvas._resolve_drawing_selection_input()
    canvas._scene_controller.reset()
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def blocked_preparation(monkeypatch):
    from comic_editor.ui import tile_input
    entered, release = Event(), Event()
    original = tile_input.prepare_input_tiles

    def prepare(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    for name in ('admission_priority', 'working_bytes', 'snapshot_working_bytes', 'source_capture'):
        setattr(prepare, name, getattr(original, name))
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    gui_thread, cold_threads = get_ident(), []
    original_get = TileResidency.get

    def get(residency, owner, key, path):
        if (owner, key) not in residency.entries:
            assert get_ident() != gui_thread, 'transform pointer callback decoded an original'
            cold_threads.append(get_ident())
        return original_get(residency, owner, key, path)

    monkeypatch.setattr(TileResidency, 'get', get)
    return entered, release, cold_threads, original_get


def synchronous_reference(canvas, obj, originals, press, positions):
    tiles = TileStore()
    for key, image in originals.items():
        tiles.set_tile(obj.object_id, key, QImage(image), _preserve_empty=True)
    reference = CanvasWidget(canvas.settings)
    reference.set_document(ChapterDocument.from_dict(canvas.chapter.to_dict()), tiles)
    reference.set_selection('object', obj.object_id)
    reference.scale = canvas.scale
    select_region(reference)
    # Execute the former sequential source read with the same native transform.
    def prepare(target):
        reference._selection_before_tiles = reference.tiles.object_tiles(target.object_id)
        reference._selection_overlay_tiles = None
    reference._prepare_drawing_selection_input = prepare
    target = reference.chapter.objects[obj.object_id]
    assert reference._begin_drawing_selection_transform(target, press)
    for position in positions:
        reference._update_drawing_selection_transform(target, position)
    assert reference._finish_drawing_selection_transform(target)
    expected = reference.tiles.object_tiles(obj.object_id)
    model = reference.chapter.to_dict()
    reference._scene_controller.reset()
    reference.deleteLater()
    return expected, model


@pytest.mark.parametrize('handle', [False, True])
def test_cold_drag_release_matches_sequential_native_pixels_and_undo(source, monkeypatch, handle):
    canvas, obj, originals = source
    press = QPointF(16, 16) if handle else QPointF(100, 80)
    positions = [press + QPointF(22, 16), press + QPointF(36, 28)]
    expected, expected_model = synchronous_reference(canvas, obj, originals, press, positions)
    before_model = canvas.chapter.to_dict()
    initial_quad = list(canvas._selection_transform_quad)
    entered, release, threads, original_get = blocked_preparation(monkeypatch)
    baseline = canvas.command_stack.revision
    finished = []
    canvas.interactionFinished.connect(lambda: finished.append(True))
    try:
        assert canvas._begin_drawing_selection_transform(obj, press)
        for position in positions:
            assert canvas._continue_drawing_selection(position, canvas.document_to_widget(position))
        canvas._tool_release()
        wait_for(entered.is_set)
        pending = canvas._drawing_selection_input
        assert pending['released'] and pending['gate'].busy
        assert canvas._selection_before_tiles is None
        assert canvas._selection_transform_quad == initial_quad
        assert not finished and canvas.command_stack.revision == baseline
        assert not canvas.tiles.dirty
        release.set()
        wait_for(lambda: canvas._drawing_selection_input is None)
        assert threads and len(finished) == 1
        assert canvas.command_stack.revision == baseline + 1
        monkeypatch.setattr(TileResidency, 'get', original_get)
        actual = canvas.tiles.object_tiles(obj.object_id)
        assert actual == expected
        assert canvas.chapter.to_dict() == expected_model
        assert actual[(1, 0)].format() == originals[(1, 0)].format()
        canvas.command_stack.undo()
        assert canvas.tiles.object_tiles(obj.object_id) == originals
        assert canvas.chapter.to_dict() == before_model
        canvas.command_stack.redo()
        assert canvas.tiles.object_tiles(obj.object_id) == expected
        assert canvas.chapter.to_dict() == expected_model
    finally:
        release.set()


def test_commit_notification_can_reenter_capture_and_tool_retirement(source, monkeypatch):
    canvas, obj, originals = source
    press, target = QPointF(100, 80), QPointF(136, 108)
    expected, _model = synchronous_reference(canvas, obj, originals, press, [target])
    entered, release, _threads, original_get = blocked_preparation(monkeypatch)
    baseline = canvas.command_stack.revision
    observations, finished = [], []
    def notified(_rect):
        pending = canvas._drawing_selection_input
        if pending is not None and pending['finishing']:
            observations.append(canvas._capture_drawing_selection_state())
            canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.documentChanged.connect(notified)
    canvas.interactionFinished.connect(lambda: finished.append(True))
    try:
        assert canvas._begin_drawing_selection_transform(obj, press)
        canvas._update_drawing_selection_transform(obj, target)
        canvas._tool_release()
        wait_for(entered.is_set)
        release.set()
        wait_for(lambda: canvas._drawing_selection_input is None)
        assert observations and len(finished) == 1
        assert canvas.command_stack.revision == baseline + 1
        monkeypatch.setattr(TileResidency, 'get', original_get)
        assert canvas.tiles.object_tiles(obj.object_id) == expected
        assert canvas._selection_transform_mode is None
        assert canvas.tool == ToolKind.RASTER_PENCIL
    finally:
        release.set()


def test_cold_transform_retains_unaffected_nonnull_native_empty_source(source, monkeypatch):
    canvas, obj, originals = source
    empty = QImage(originals[(1, 0)])
    empty.fill(QColor.fromRgbF(.7231, .1645, .2867, 0.))
    canvas.tiles.set_tile(obj.object_id, (2, 0), empty, _preserve_empty=True)
    originals[(2, 0)] = QImage(empty)
    press, target = QPointF(100, 80), QPointF(136, 108)
    expected, _model = synchronous_reference(canvas, obj, originals, press, [target])
    entered, release, _threads, original_get = blocked_preparation(monkeypatch)
    try:
        assert canvas._begin_drawing_selection_transform(obj, press)
        canvas._update_drawing_selection_transform(obj, target)
        canvas._tool_release()
        wait_for(entered.is_set)
        release.set()
        wait_for(lambda: canvas._drawing_selection_input is None)
        monkeypatch.setattr(TileResidency, 'get', original_get)
        assert canvas.tiles.object_tiles(obj.object_id) == expected
        assert canvas.tiles.tile(obj.object_id, (2, 0)) == empty
        canvas.command_stack.undo()
        assert canvas.tiles.object_tiles(obj.object_id) == originals
    finally:
        release.set()


def test_warm_transform_is_immediate_and_release_survives_lru_eviction(source, monkeypatch):
    canvas, obj, _originals = source
    baseline = canvas.command_stack.revision
    canvas.tiles.residency.budget = 256 * 256 * 8 * 3
    canvas.tiles.object_tiles(obj.object_id)
    owner = canvas.tiles._tiles[obj.object_id]
    gui_thread = get_ident()
    original_get = TileResidency.get
    def get(residency, mapping, key, path):
        if (mapping, key) not in residency.entries:
            assert get_ident() != gui_thread, 'release decoded an evicted original'
        return original_get(residency, mapping, key, path)
    monkeypatch.setattr(TileResidency, 'get', get)
    assert canvas._begin_drawing_selection_transform(obj, QPointF(100, 80))
    pending = canvas._drawing_selection_input
    assert pending['ready'] and not pending['gate'].busy
    assert pending['gate'].jobs.active is None and not pending['gate'].jobs.pending
    canvas._update_drawing_selection_transform(obj, QPointF(136, 108))
    for key in tuple(owner):
        owner.residency.remove(owner, key)
    assert canvas._finish_drawing_selection_transform(obj)
    assert canvas._drawing_selection_input is None
    assert canvas.command_stack.revision == baseline + 1


@pytest.mark.parametrize('retire', ['tool', 'selection', 'capture', 'document'])
def test_held_pending_retirement_restores_press_and_keeps_originals(source, monkeypatch, retire):
    canvas, obj, _originals = source
    entered, release, _threads, _get = blocked_preparation(monkeypatch)
    original_tiles = canvas.tiles
    owner = original_tiles._tiles[obj.object_id]
    versions = dict(owner.versions)
    initial_quad = list(canvas._selection_transform_quad)
    baseline = canvas.command_stack.revision
    try:
        assert canvas._begin_drawing_selection_transform(obj, QPointF(100, 80))
        canvas._update_drawing_selection_transform(obj, QPointF(136, 108))
        wait_for(entered.is_set)
        gate = canvas._drawing_selection_tile_input
        if retire == 'tool':
            canvas.set_tool(ToolKind.RASTER_PENCIL)
        elif retire == 'selection':
            canvas.set_selection('layer', obj.parent_layer_id)
        elif retire == 'capture':
            state = canvas._capture_drawing_selection_state()
            assert state.quad == initial_quad
        else:
            replacement = ChapterDocument(width=64, height=64)
            replacement.add_page()
            canvas.set_document(replacement, TileStore())
        assert canvas._drawing_selection_input is None and gate.closed
        release.set()
        wait_for(lambda: gate.jobs.active is None)
        assert not original_tiles.dirty and owner.versions == versions
        assert canvas.command_stack.revision == (baseline + 1 if retire == 'document' else baseline)
        if retire in {'tool', 'capture'}:
            assert canvas._selection_transform_quad == initial_quad
    finally:
        release.set()


@pytest.mark.parametrize('stale', ['history', 'path', 'source', 'revision', 'foreign_gate'])
def test_late_source_preparation_cannot_adopt_changed_owner(source, monkeypatch, stale):
    canvas, obj, _originals = source
    entered, release, _threads, _get = blocked_preparation(monkeypatch)
    baseline = canvas.command_stack.revision
    try:
        assert canvas._begin_drawing_selection_transform(obj, QPointF(100, 80))
        canvas._update_drawing_selection_transform(obj, QPointF(136, 108))
        wait_for(entered.is_set)
        gate = canvas._drawing_selection_tile_input
        if stale == 'history':
            canvas.command_stack.clear()
            # History revision is monotonic, including an otherwise empty clear.
            assert canvas.command_stack.revision != baseline
        elif stale == 'path':
            path = QPainterPath()
            path.addRect(QRectF(30, 30, 40, 40))
            canvas._drawing_selection_path = path
        elif stale == 'source':
            canvas.tiles._tiles[obj.object_id].versions[(0, 0)] += 1
        elif stale == 'revision':
            canvas._invalidate_scene_cache()
        else:
            # A different native owner cannot replace this gesture's gate.
            canvas._drawing_selection_tile_input = object()
        release.set()
        wait_for(lambda: gate.jobs.active is None)
        assert canvas._selection_before_tiles is None
        assert not canvas.tiles.dirty
        if stale == 'foreign_gate':
            assert canvas._drawing_selection_tile_input is not gate
            canvas._drawing_selection_tile_input = None
            gate.retire()
        else:
            assert canvas._drawing_selection_input is None and gate.closed
    finally:
        release.set()


@pytest.mark.parametrize('boundary', ['capture', 'tool', 'document'])
def test_accepted_release_drains_before_capture_or_context_retirement(source, monkeypatch, boundary):
    canvas, obj, originals = source
    press, target = QPointF(100, 80), QPointF(136, 108)
    expected, _model = synchronous_reference(canvas, obj, originals, press, [target])
    entered, release, _threads, original_get = blocked_preparation(monkeypatch)
    original_tiles = canvas.tiles
    baseline = canvas.command_stack.revision
    try:
        assert canvas._begin_drawing_selection_transform(obj, press)
        canvas._update_drawing_selection_transform(obj, target)
        canvas._tool_release()
        wait_for(entered.is_set)
        worker = Thread(target=lambda: (time.sleep(.02), release.set()))
        worker.start()
        if boundary == 'capture':
            state = canvas._capture_drawing_selection_state()
            assert state.quad == canvas._selection_transform_quad
            assert canvas.command_stack.revision == baseline + 1
        elif boundary == 'tool':
            canvas.set_tool(ToolKind.RASTER_PENCIL)
            assert canvas.command_stack.revision == baseline + 1
        else:
            replacement = ChapterDocument(width=64, height=64)
            replacement.add_page()
            canvas.set_document(replacement, TileStore())
        worker.join()
        assert canvas._drawing_selection_input is None
        monkeypatch.setattr(TileResidency, 'get', original_get)
        assert original_tiles.object_tiles(obj.object_id) == expected
    finally:
        release.set()
