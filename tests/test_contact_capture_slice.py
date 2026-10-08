"""Real contact capture boundaries preserve ownership without per-record checks."""
from copy import deepcopy

import pytest
from PySide6.QtCore import QPointF

from comic_editor.core.models import ToneMask
from test_raster_contact_feedback import scene
from test_contact_native_preview_contracts import finish_contact, native_bytes


def observed_capture(canvas, document):
    capture = canvas._scene_snapshot_compiler.capture(canvas, document)
    iterator = capture.iterator
    yielded = []
    def actual_records():
        for record in iterator:
            yielded.append(None)
            yield record
    capture.iterator = actual_records()
    return capture, yielded


def test_actual_many_record_capture_validates_contact_once_per_gui_slice(
        scene, qapp, wait_scene, monkeypatch):
    canvas, selected, _front = scene
    wait_scene(canvas)
    canvas._begin_stroke(QPointF(32, 64), 1.)
    document = canvas._render_document_state()
    assert document.contact_only and canvas._raster_tile_input.current()
    before = {key: native_bytes(image) for key, image in canvas.tiles.object_tiles(selected.object_id).items()}
    assert before
    calls = []
    original = canvas._projection_contact_only
    def checked():
        calls.append(None)
        return original()
    monkeypatch.setattr(canvas, '_projection_contact_only', checked)
    try:
        capture, yielded = observed_capture(canvas, document)
        assert capture.advance(1.) and not capture.stale
        assert len(yielded) >= 32, 'Exercise actual incremental models/state/source capture'
        assert len(calls) == 1
        assert capture.result is not None and capture.result.document == document
        snapshot = capture.result.finish_sources()
        assert {key: native_bytes(image) for key, image in snapshot.tiles.object_tiles(selected.object_id).items()} == before
    finally:
        finish_contact(canvas, qapp)


@pytest.mark.parametrize('change', ['mask_mode', 'same_id_replacement'])
def test_next_capture_slice_revalidates_actual_mode_and_original_gate_owner(
        scene, qapp, wait_scene, monkeypatch, change):
    canvas, selected, _front = scene
    wait_scene(canvas)
    canvas._begin_stroke(QPointF(32, 64), 1.)
    document = canvas._render_document_state()
    assert document.contact_only and canvas._raster_tile_input.current()
    calls = []
    original = canvas._projection_contact_only
    def checked():
        calls.append(None)
        return original()
    monkeypatch.setattr(canvas, '_projection_contact_only', checked)
    original_object = selected
    changes = pytest.MonkeyPatch()
    try:
        capture, yielded = observed_capture(canvas, document)
        assert not capture.advance(0.) and capture.result is None
        assert len(calls) == 1 and len(yielded) == 1
        if change == 'mask_mode':
            mask = ToneMask(name='Actual next-slice mask context', saved=True)
            canvas.chapter.masks[mask.mask_id] = mask
            changes.setattr(canvas, 'active_tone_mask_id', mask.mask_id)
        else:
            replacement = deepcopy(selected)
            assert replacement.object_id == selected.object_id and replacement is not selected
            canvas.chapter.objects[selected.object_id] = replacement
            assert not canvas._raster_tile_input.current()
        # Neither identity nor revision changed: positive mode/gate ownership
        # must reject before another record, independent of cheap revision checks.
        assert document.identity == (id(canvas.chapter), id(canvas.tiles), id(canvas.images))
        assert canvas._document_projection.revision == document.revision
        assert capture.advance(1.) and capture.stale and capture.result is None
        assert len(calls) == 2 and len(yielded) == 1
    finally:
        canvas.chapter.objects[selected.object_id] = original_object
        changes.undo()
        finish_contact(canvas, qapp)
