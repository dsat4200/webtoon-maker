"""Cold native tiles cannot turn a pointer callback into source decoding."""
from threading import Event, get_ident
import time

import pytest
from PySide6.QtCore import QCoreApplication, QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, ToneMask
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tile_backing import TileResidency
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.tile_input import TileInputGate


def wait_for(predicate):
    deadline = time.monotonic()+10
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.002)
    assert predicate()


@pytest.fixture
def source(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    chapter = ChapterDocument(width=512, height=256, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 512, 256))
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 512, 256)))
    mask = ToneMask(saved=True)
    chapter.masks[mask.mask_id] = mask
    image = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor.fromRgbF(.3123, .5345, .7967, .4132))
    tiles = TileStore(cache_budget=256*256*8)
    for identifier in (obj.object_id, mask.mask_id):
        for x in (0, 1):
            path = tmp_path / f'{identifier}-{x}.png'
            assert image.save(str(path))
            tiles._object_tiles(identifier).register((x, 0), path)
        tiles._alpha_bounds[identifier] = {(0, 0): (0, 0, 256, 256), (1, 0): (0, 0, 256, 256)}
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', snap_to_grid=False,
        brush_size=14, predictive_ink=False, grid_overlay_visible=False))
    canvas.set_document(chapter, tiles)
    canvas.set_selection('object', obj.object_id)
    canvas.primary_color = '#AAB431D5'
    yield canvas, obj, mask, QImage(str(path))
    canvas._cancel_mask_stroke()
    canvas._cancel_native_raster_stroke()
    canvas._scene_controller.reset()
    canvas.deleteLater()


def prepare_gated(monkeypatch):
    from comic_editor.ui import tile_input
    entered, release = Event(), Event()
    original = tile_input.prepare_input_tiles
    def prepare(*args):
        from comic_editor.render.admission import current_ticket
        assert current_ticket().estimated_bytes == original.working_bytes(*args)
        entered.set()
        assert release.wait(10)
        return original(*args)
    prepare.admission_priority = 0
    prepare.working_bytes = original.working_bytes
    prepare.snapshot_working_bytes = original.snapshot_working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    owner, cold_threads = get_ident(), []
    original_get = TileResidency.get
    def get(residency, mapping, key, path):
        if (mapping, key) not in residency.entries:
            assert get_ident() != owner, 'pointer callback decoded a native source tile'
            cold_threads.append(get_ident())
        return original_get(residency, mapping, key, path)
    get.__wrapped__ = original_get
    monkeypatch.setattr(TileResidency, 'get', get)
    return entered, release, cold_threads


def methods(canvas, mask, target, erasing=False):
    canvas.set_tool(ToolKind.RASTER_ERASER if erasing else ToolKind.RASTER_PENCIL)
    if target == 'mask':
        canvas.set_tone_mask_mode(mask.mask_id)
        return canvas._begin_mask_stroke, canvas._continue_mask_stroke, canvas._end_mask_stroke, '_mask_tile_input', mask.mask_id
    return canvas._begin_stroke, canvas._continue_stroke, canvas._end_stroke, '_raster_tile_input', canvas.selected_id


@pytest.mark.parametrize('target', ['mask', 'raster'])
@pytest.mark.parametrize('erasing', [False, True])
def test_cold_packets_keep_native_precision_pressure_release_and_undo(source, target, erasing, monkeypatch):
    canvas, _obj, mask, original = source
    begin, move, end, attribute, identifier = methods(canvas, mask, target, erasing)
    # Run the same authoritative paint kernels on already resident originals.
    reference_tiles = TileStore()
    for key in ((0, 0), (1, 0)):
        reference_tiles.set_tile(identifier, key, QImage(original))
    reference = CanvasWidget(canvas.settings)
    reference.set_document(ChapterDocument.from_dict(canvas.chapter.to_dict()), reference_tiles)
    reference.set_selection('object', canvas.selected_id)
    reference.primary_color = canvas.primary_color
    expected_begin, expected_move, expected_end, _, _ = methods(reference, reference.chapter.masks[mask.mask_id], target, erasing)
    expected_begin(QPointF(30, 70), .2)
    expected_move(QPointF(240, 80), .8)
    expected_move(QPointF(290, 86), .4)
    expected_end()
    expected = {key: QImage(reference_tiles.tile(identifier, key)) for key in ((0, 0), (1, 0))}
    reference.deleteLater()
    entered, release, threads = prepare_gated(monkeypatch)
    baseline = canvas.command_stack.revision
    try:
        begin(QPointF(30, 70), .2)
        move(QPointF(240, 80), .8)
        move(QPointF(290, 86), .4)
        end()
        wait_for(entered.is_set)
        gate = getattr(canvas, attribute)
        assert gate.released and gate.busy
        assert canvas.command_stack.revision == baseline
        release.set()
        wait_for(lambda: getattr(canvas, attribute) is None)
        assert threads
        assert canvas.command_stack.revision == baseline+1
        # Completed-buffer inspection is an explicit test oracle, after every
        # pointer/release callback has passed the strict cold-source guard.
        monkeypatch.setattr(TileResidency, 'get', TileResidency.get.__wrapped__)
        for key in ((0, 0), (1, 0)):
            actual = canvas.tiles.tile(identifier, key)
            assert actual.format() == original.format()
            assert actual == expected[key]
        assert canvas.tiles.tile(identifier, (1, 0)).pixelColor(200, 190).getRgbF() == original.pixelColor(200, 190).getRgbF()
        canvas.command_stack.undo()
        for key in ((0, 0), (1, 0)):
            assert canvas.tiles.tile(identifier, key) == original
        canvas.command_stack.redo()
        for key in ((0, 0), (1, 0)):
            assert canvas.tiles.tile(identifier, key) == expected[key]
    finally:
        release.set()


@pytest.mark.parametrize('target', ['mask', 'raster'])
@pytest.mark.parametrize('retire', ['cancel', 'document'])
def test_pending_native_contact_retirement_leaves_originals_and_history(source, target, retire, monkeypatch):
    canvas, _obj, mask, _original = source
    begin, move, _end, attribute, identifier = methods(canvas, mask, target)
    entered, release, _threads = prepare_gated(monkeypatch)
    original_store, chapter = canvas.tiles, canvas.chapter
    owner = original_store._tiles[identifier]
    versions = dict(owner.versions)
    baseline = canvas.command_stack.revision
    try:
        begin(QPointF(30, 70), .2)
        move(QPointF(90, 86), .8)
        wait_for(entered.is_set)
        gate = getattr(canvas, attribute)
        if retire == 'cancel':
            gate.cancel()
        else:
            other = ChapterDocument(width=64, height=64)
            other.add_page()
            canvas.set_document(other, TileStore())
        release.set()
        wait_for(lambda: gate.jobs.active is None)
        assert not original_store.dirty
        assert owner.versions == versions
        assert not original_store.residency.decodes
        if retire == 'cancel':
            assert canvas.chapter is chapter
            assert canvas.command_stack.revision == baseline
        else:
            assert canvas.chapter is other
        assert not canvas._drawing
        assert getattr(canvas, attribute) is None
    finally:
        release.set()


@pytest.mark.parametrize('target', ['mask', 'raster'])
def test_consecutive_released_cold_strokes_keep_two_history_boundaries(source, target, monkeypatch):
    canvas, _obj, mask, _original = source
    begin, move, end, attribute, identifier = methods(canvas, mask, target)
    entered, release, _threads = prepare_gated(monkeypatch)
    baseline = canvas.command_stack.revision
    try:
        begin(QPointF(30, 70), 1.)
        move(QPointF(90, 86), 1.)
        end()
        wait_for(entered.is_set)
        begin(QPointF(280, 30), 1.)
        move(QPointF(330, 45), 1.)
        end()
        release.set()
        wait_for(lambda: getattr(canvas, attribute) is None
                 and not getattr(canvas, '_native_deferred_activations', None))
        assert canvas.command_stack.revision == baseline+2
        final = QImage(canvas.tiles.tile(identifier, (1, 0)))
        canvas.command_stack.undo()
        assert canvas.tiles.tile(identifier, (1, 0)) != final
        canvas.command_stack.redo()
        assert canvas.tiles.tile(identifier, (1, 0)) == final
    finally:
        release.set()


def test_long_admitted_queue_yields_to_gui_without_dropping_packets_and_bounds_buffers(source, monkeypatch):
    canvas, obj, _mask, _original = source
    entered, release, _threads = prepare_gated(monkeypatch)
    gate = TileInputGate(canvas, obj.object_id)
    delivered, heartbeat = [], []
    gate.submit({(0, 0)}, lambda: delivered.append(0))
    wait_for(entered.is_set)
    for index in range(1, 41):
        def packet(index=index):
            # Deterministic native work stand-in proves the replay owner yields.
            deadline = time.monotonic()+.001
            while time.monotonic() < deadline:
                pass
            delivered.append(index)
        gate.submit({(index % 2, 0)}, packet)
    gate.finish(lambda: delivered.append('release'))
    QTimer.singleShot(0, lambda: heartbeat.append(len(delivered)))
    release.set()
    wait_for(lambda: not gate.busy)
    assert delivered == list(range(41))+['release']
    assert heartbeat and heartbeat[0] < 41
    assert sum(image.sizeInBytes() for image in gate.buffers.values()) <= gate.buffer_budget
    gate.retire()


def test_unknown_untouched_alpha_bounds_are_scanned_off_gui_without_returning_native_buffers(source, monkeypatch):
    canvas, obj, _mask, original = source
    from comic_editor.ui import tile_input
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.tiles._alpha_bounds[obj.object_id].pop((1, 0))
    owner, calls = get_ident(), []
    compute = tile_input.prepare_input_bounds
    def bounds(*args):
        from comic_editor.render.admission import current_ticket
        assert get_ident() != owner
        assert current_ticket().estimated_bytes == compute.working_bytes(*args)
        result = compute(*args)
        assert all(len(record) == 3 and not isinstance(record[2], QImage) for record in result)
        calls.append(result)
        return result
    bounds.admission_priority, bounds.working_bytes = 0, compute.working_bytes
    bounds.snapshot_working_bytes = compute.snapshot_working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_bounds', bounds)
    original_get = TileResidency.get
    def get(residency, mapping, key, path):
        if (mapping, key) not in residency.entries:
            assert get_ident() != owner, 'final frame bounds decoded an untouched source on GUI'
        return original_get(residency, mapping, key, path)
    monkeypatch.setattr(TileResidency, 'get', get)
    canvas._begin_stroke(QPointF(30, 70), .2)
    canvas._continue_stroke(QPointF(90, 86), .8)
    canvas._end_stroke()
    wait_for(lambda: canvas._raster_tile_input is None)
    assert calls and any(record[0] == (1, 0) for result in calls for record in result)
    assert canvas.tiles._alpha_bounds[obj.object_id][(1, 0)] == (0, 0, 256, 256)
    assert canvas.tiles._tiles[obj.object_id].version((1, 0)) == (0, 0)
    assert canvas.chapter.objects[obj.object_id].interaction_rect[2] >= 512
    assert original.pixelColor(200, 190).alphaF() > 0


def test_large_metadata_publication_yields_and_retirement_releases_buffers(source):
    canvas, obj, _mask, _original = source
    gate = TileInputGate(canvas, obj.object_id)
    delivered, heartbeat = [], []
    gate.pending = True
    QTimer.singleShot(0, lambda: heartbeat.append(len(delivered)))
    def adopt(value):
        deadline = time.monotonic()+.001
        while time.monotonic() < deadline:
            pass
        delivered.append(value)
    gate.publish(tuple(range(30)), adopt)
    wait_for(lambda: not gate.busy)
    assert delivered == list(range(30))
    assert heartbeat and heartbeat[0] < 30
    gate.buffers[(0, 0)] = QImage(256, 256, QImage.Format_RGBA32FPx4_Premultiplied)
    gate.finish(gate.retire)
    assert gate.closed and not gate.buffers and not gate.busy


@pytest.mark.parametrize('target', ['mask', 'raster'])
@pytest.mark.parametrize('change', ['selection-tool', 'unrelated-property'])
def test_released_cold_contact_keeps_original_source_across_unrelated_editor_changes(source, target, change, monkeypatch):
    canvas, obj, mask, _original = source
    unrelated = canvas.chapter.add_object(obj.parent_layer_id, RasterObject())
    begin, move, end, attribute, identifier = methods(canvas, mask, target)
    entered, release, _threads = prepare_gated(monkeypatch)
    baseline = canvas.command_stack.revision
    try:
        begin(QPointF(30, 70), .2)
        move(QPointF(90, 86), .8)
        end()
        wait_for(entered.is_set)
        if change == 'selection-tool':
            canvas.set_selection('object', unrelated.object_id, activate_default_tool=False)
            canvas.set_tool(ToolKind.OBJECT_SELECT)
        else:
            unrelated.name = 'Unrelated edit during source preparation'
            canvas._invalidate_scene_cache()
        release.set()
        wait_for(lambda: getattr(canvas, attribute) is None)
        assert canvas.command_stack.revision == baseline+1
        assert not canvas.tiles._tiles.get(unrelated.object_id)
        monkeypatch.setattr(TileResidency, 'get', TileResidency.get.__wrapped__)
        final = QImage(canvas.tiles.tile(identifier, (0, 0)))
        assert final != _original
        if change == 'selection-tool':
            assert canvas.selected_id == unrelated.object_id and canvas.tool == ToolKind.OBJECT_SELECT
        else:
            assert unrelated.name == 'Unrelated edit during source preparation'
        canvas.command_stack.undo()
        assert canvas.tiles.tile(identifier, (0, 0)) == _original
        canvas.command_stack.redo()
        assert canvas.tiles.tile(identifier, (0, 0)) == final
    finally:
        release.set()
