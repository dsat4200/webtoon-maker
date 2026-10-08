"""Cold packet publication rejects changed source versions between GUI slices."""
from itertools import count
from threading import get_ident
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.tile_backing import TileResidency
from comic_editor.ui import tile_input
from comic_editor.ui.tile_input import TileInputGate
from test_native_input_admission import source, methods, wait_for


def native_bytes(image):
    return (image.size(), image.format(), image.bytesPerLine(),
            image.colorSpace(), image.devicePixelRatio(), bytes(image.constBits()))


@pytest.mark.parametrize('target', ['raster', 'mask'])
@pytest.mark.parametrize('changed_record', ['published', 'unpublished'])
def test_cold_source_version_change_between_publication_slices_rejects_stale_packet(
        source, target, changed_record, monkeypatch, qapp):
    canvas, _obj, mask, original = source
    begin, _move, end, attribute, identifier = methods(canvas, mask, target)
    owner = canvas.tiles._tiles[identifier]
    keys = {(0, 0), (1, 0)}
    assert all((owner, key) not in owner.residency.entries for key in keys)
    original_versions = {key: owner.version(key) for key in keys}
    canvas.tiles._alpha_bounds_dirty.update((identifier, *key) for key in keys)
    history = canvas.command_stack.revision
    undo = len(canvas.command_stack._undo)
    changes = []
    canvas.documentChanged.connect(changes.append)
    batches, adopted, decoded = [], [], []
    gui = get_ident()
    original_prepare = tile_input.prepare_input_tiles
    original_get = TileResidency.get
    original_publish = TileInputGate.publish

    def prepare(*args):
        assert get_ident() != gui, 'cold source preparation must remain worker-owned'
        result = original_prepare(*args)
        batches.append(result)
        return result

    for name in ('admission_priority', 'working_bytes', 'snapshot_working_bytes', 'source_capture'):
        setattr(prepare, name, getattr(original_prepare, name))

    def get(residency, mapping, key, path):
        if (mapping, key) not in residency.entries:
            assert get_ident() != gui, 'publication may not decode a cold GUI source'
            decoded.append((mapping.object_id, key))
        return original_get(residency, mapping, key, path)

    def publish(gate, result, adopt, **kwargs):
        def observe(record):
            adopted.append(record[0])
            return adopt(record)
        return original_publish(gate, result, observe, **kwargs)

    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    monkeypatch.setattr(TileResidency, 'get', get)
    monkeypatch.setattr(TileInputGate, 'publish', publish)

    # Each actual bounded publication slice adopts exactly one record. Hold
    # only its next GUI callback, so the source change is a deterministic
    # intervening editor operation rather than a wall-clock race.
    ticks = count()
    monkeypatch.setattr(tile_input, 'time', SimpleNamespace(monotonic=lambda: next(ticks) * .003))
    callbacks = []
    real_timer = tile_input.QTimer
    gate = None
    try:
        # The seam-spanning first packet requires both original cold tiles.
        begin(QPointF(256, 70), .5)
        gate = getattr(canvas, attribute)
        assert gate is not None and gate.pending

        class SliceTimers:
            @staticmethod
            def singleShot(delay, *arguments):
                if len(arguments) == 2 and arguments[0] is gate.jobs:
                    callbacks.append(arguments[1])
                else:
                    real_timer.singleShot(delay, *arguments)

        monkeypatch.setattr(tile_input, 'QTimer', SliceTimers)
        end()
        wait_for(lambda: bool(callbacks))
        assert gate.released and gate.pending and not gate.closed
        assert len(batches) == 1 and {record[0] for record in batches[0]} == keys
        assert decoded and len(adopted) == 1 and len(callbacks) == 1
        assert set(gate.buffers) == {adopted[0]}
        for _key, image, bounds in batches[0]:
            assert native_bytes(image) == native_bytes(original)
            assert bounds == (0, 0, 256, 256)
        assert canvas.command_stack.revision == history and not changes

        first = adopted[0]
        unpublished = next(iter(keys - {first}))
        changed = first if changed_record == 'published' else unpublished
        replacement = QImage(256, 256, original.format())
        replacement.fill(Qt.transparent)
        painter = QPainter(replacement)
        try:
            painter.fillRect(31, 47, 19, 23, QColor('yellow'))
        finally:
            painter.end()
        # The ordinary mapping mutation changes this same owner and marks
        # alpha metadata dirty. No fake generation or validity flag is used.
        owner[changed] = QImage(replacement)
        version = owner.version(changed)
        assert version != original_versions[changed]
        marker = identifier, *changed
        assert marker in canvas.tiles.dirty and marker in canvas.tiles._alpha_bounds_dirty
        bounds_after_edit = dict(canvas.tiles._alpha_bounds[identifier])
        dirty_after_edit = set(canvas.tiles.dirty)
        alpha_dirty_after_edit = set(canvas.tiles._alpha_bounds_dirty)
        assert gate.current(), 'owner and contact stay current; only source version changes'

        callbacks.pop(0)()
        qapp.processEvents()
        assert gate.closed and not gate.pending and not gate.buffers
        assert not gate.queue and not callbacks and len(adopted) == 1
        assert getattr(canvas, attribute) is None and not canvas._drawing
        assert canvas.tiles._tiles[identifier] is owner
        assert owner.version(changed) == version
        assert native_bytes(owner.residency.entries[(owner, changed)][0]) == native_bytes(replacement)
        assert canvas.tiles.dirty == dirty_after_edit
        assert canvas.tiles._alpha_bounds_dirty == alpha_dirty_after_edit
        assert canvas.tiles._alpha_bounds[identifier] == bounds_after_edit
        assert (identifier, *unpublished) in canvas.tiles._alpha_bounds_dirty
        assert canvas.command_stack.revision == history
        assert len(canvas.command_stack._undo) == undo and not changes
        assert gate.error is None
    finally:
        monkeypatch.setattr(tile_input, 'QTimer', real_timer)
        if gate is not None:
            gate.cancel()
            gate.jobs.shutdown()
            gate.jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
