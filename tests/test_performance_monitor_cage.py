"""Cage preparation diagnostics follow one privately owned ordinary queue."""
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
from threading import Event, get_ident
import json
import time

import pytest
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.tile_backing import TileResidency
from comic_editor.render import input_capture
from comic_editor.render.admission import RENDER_ADMISSION
from comic_editor.ui import scene_consumers
from test_performance_monitor_integration import editor, controller, _phases


def _pending(canvas, compute):
    canvas.setUpdatesEnabled(False)
    targets = tuple(canvas.selected_entities)
    if compute is input_capture.cage_initial:
        canvas._cage_prepare = {}
        arguments, lane = (targets,), ('cage-initial',)
    else:
        canvas._cage_session = {'targets': targets}
        canvas._cage_commit_pending = object()
        arguments, lane = (targets, object()), ('cage-commit',)
    jobs = scene_consumers.scene_consumers(canvas)
    key = jobs.request(lane, compute, arguments, lambda *_a: None, valid=lambda: True)
    jobs.timer.stop()
    return jobs, jobs.pending[key]


@pytest.mark.parametrize('compute', [input_capture.cage_initial, input_capture.cage_commit])
def test_cage_scope_rejects_spoofed_functions_foreign_queue_stale_selection_and_capture(
        editor, controller, monkeypatch, compute):
    canvas, identifier = editor[0].canvas, editor[2].object_id
    canvas.set_selection('object', identifier)
    controller.start()
    jobs, job = _pending(canvas, compute)
    try:
        details = controller._cage_consumer_scope((jobs, job), {})
        assert details['consumer'] == compute.__name__
        assert details['target_count'] == 1 and details['targets'] == (('object', identifier),)
        request = [row for row in controller.snapshot()['critical_events']
            if row['name'] == 'cage.request']
        assert len(request) == 1 and request[0]['details']['consumer'] == compute.__name__
        original_compute = job.compute
        @wraps(compute)
        def spoof(*args, **kwargs):
            return compute(*args, **kwargs)
        job.compute = spoof
        assert controller._cage_consumer_scope((jobs, job), {}) is None
        job.compute = original_compute
        document = job.document
        job.document = replace(document, revision=document.revision + 1)
        assert controller._cage_consumer_scope((jobs, job), {}) is None
        job.document = document
        valid = job.valid
        job.valid = lambda: False
        assert controller._cage_consumer_scope((jobs, job), {}) is None
        job.valid = valid
        lane = job.lane
        job.lane = ('unrelated',)
        assert controller._cage_consumer_scope((jobs, job), {}) is None
        job.lane = lane
        job.cancelled.set()
        assert controller._cage_consumer_scope((jobs, job), {}) is None
        job.cancelled.clear()
        with monkeypatch.context() as patch:
            patch.setattr(canvas, 'selected_entities', [])
            assert controller._cage_consumer_scope((jobs, job), {}) is None
        foreign = scene_consumers.SceneConsumers(canvas)
        try:
            assert controller._cage_consumer_scope((foreign, job), {}) is None
        finally:
            foreign.shutdown()
            foreign.executor.shutdown(wait=True)
        jobs.pending.pop(job.key)
        jobs.active = job
        capture = jobs._capture_job(job)
        job.capture = capture
        assert controller._capture_scope((capture,), {})['scope'] == 'gui_cage_capture_slice'
        other = canvas._scene_snapshot_compiler.capture(canvas, document)
        assert controller._capture_scope((other,), {})['scope'] == 'gui_capture_slice'
        # A registered job can be observed during adoption after active clears,
        # but the positive selection/document/validator guards still apply.
        jobs.active = None
        assert controller._cage_consumer_scope((jobs, job), {})['cage_job_id'] == id(job)
        job.valid = lambda: False
        assert controller._cage_consumer_scope((jobs, job), {}) is None
    finally:
        jobs.shutdown()
        jobs.executor.shutdown(wait=True)
        canvas._cage_prepare = None
        canvas._cage_commit_pending = None
        canvas._cage_session = None
        controller.stop()


def _submit_without_gui_events(jobs):
    deadline = time.monotonic() + 10
    while jobs.active is None or jobs.active.future is None:
        jobs.advance()
        jobs.timer.stop()
        assert time.monotonic() < deadline
        time.sleep(.001)
    return jobs.active


def test_real_cold_cage_initial_and_commit_preserve_native_source_undo_and_frozen_worker_scope(
        editor, controller, tmp_path, monkeypatch):
    canvas, raster = editor[0].canvas, editor[2]
    canvas.setUpdatesEnabled(False)
    canvas.set_selection('object', raster.object_id)
    raster.interaction_rect = (0, 0, 64, 64)
    image = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.fillRect(8, 8, 40, 40, QColor.fromRgbF(.371234, .618743, .791256, .712345))
    painter.end()
    path = tmp_path / 'private native original.png'
    assert image.save(str(path))
    # PNG's ordinary decoder owns the cold source format, including its alpha
    # representation; compare against those exact decoded native bytes.
    image = QImage(str(path))
    original = bytes(image.constBits())
    canvas.tiles._object_tiles(raster.object_id).register((0, 0), path)
    gui = get_ident()
    cold_threads, scopes = [], []
    get = TileResidency.get
    def checked_get(residency, owner, key, source_path):
        if (owner, key) not in residency.entries:
            assert get_ident() != gui, 'monitor/cage source capture decoded a GUI tile'
            cold_threads.append(get_ident())
        return get(residency, owner, key, source_path)
    monkeypatch.setattr(TileResidency, 'get', checked_get)
    entered, release = Event(), Event()
    reserve = RENDER_ADMISSION.reserve
    @contextmanager
    def held_reserve(*args, **kwargs):
        token = controller._current_scope()
        if token is not None and token.details().get('scope') == 'detached_cage_worker':
            assert get_ident() != gui
            scopes.append(token.details())
            entered.set()
            assert release.wait(10)
        with reserve(*args, **kwargs) as admission:
            yield admission
    monkeypatch.setattr(RENDER_ADMISSION, 'reserve', held_reserve)
    controller.start()
    jobs = None
    baseline_revision = canvas.command_stack.revision
    try:
        assert canvas.begin_cage_tool()
        jobs = canvas._scene_consumers
        first = _submit_without_gui_events(jobs)
        assert entered.wait(10) and canvas._cage_prepare is not None
        assert canvas.command_stack.revision == baseline_revision
        # Worker scope is frozen before submit, independent of Qt selection reads.
        assert scopes[0]['consumer'] == 'cage_initial'
        assert scopes[0]['cage_job_id'] == id(first)
        release.set()
        first.future.result(timeout=10)
        jobs.advance()
        jobs.timer.stop()
        session = canvas._cage_session
        assert session is not None and canvas._cage_prepare is None
        before = session['before_tiles'][raster.object_id][(0, 0)]
        assert before.format() == image.format() and bytes(before.constBits()) == original
        assert cold_threads and all(thread != gui for thread in cold_threads)
        entered.clear()
        release.clear()
        point = session['grid'].points[5]
        session['grid'].points[5] = (point[0] + 11, point[1])
        canvas._commit_cage_drawings(session)
        commit = _submit_without_gui_events(jobs)
        assert entered.wait(10) and canvas._cage_commit_pending is not None
        assert canvas.command_stack.revision == baseline_revision
        release.set()
        prepared, _alpha, _installed = commit.future.result(timeout=10)
        jobs.advance()
        jobs.timer.stop()
        assert canvas._cage_commit_pending is None and canvas._cage_session is None
        assert canvas.command_stack.revision == baseline_revision + 1
        actual = {key: (value.format(), bytes(value.constBits()))
            for key, value in canvas.tiles.object_tiles(raster.object_id).items()}
        expected = {key: (value.format(), bytes(value.constBits()))
            for key, value in prepared[raster.object_id][1].items()}
        assert actual == expected and actual[(0, 0)][1] != original
        monkeypatch.setattr(TileResidency, 'get', get)
        canvas.command_stack.undo()
        assert bytes(canvas.tiles.tile(raster.object_id, (0, 0)).constBits()) == original
        canvas.command_stack.redo()
        assert {key: (value.format(), bytes(value.constBits()))
            for key, value in canvas.tiles.object_tiles(raster.object_id).items()} == expected
        phases = _phases(controller)
        assert {'cage.request', 'cage.capture_job', 'cage.capture_slice',
            'cage.capture_slice.thread_cpu', 'cage.submit', 'cage.worker_envelope',
            'cage.worker_envelope.thread_cpu', 'cage.accept'} <= phases.keys()
        critical = controller.snapshot()['critical_events']
        for consumer in ('cage_initial', 'cage_commit'):
            rows = [row for row in critical if row['details'].get('consumer') == consumer]
            assert {'cage.request.begin', 'cage.request', 'cage.capture_job.begin',
                'cage.capture_job', 'cage.submit.begin', 'cage.submit',
                'cage.worker_envelope.begin', 'cage.worker_envelope',
                'cage.accept.begin', 'cage.accept'} <= {row['name'] for row in rows}
            workers = [row for row in rows if row['name'].startswith('cage.worker_envelope')]
            assert workers and all(row['details']['scope'] == 'detached_cage_worker'
                and row['details']['thread'] == 'worker'
                and row['details']['thread_id'] != gui for row in workers)
        assert str(path) not in json.dumps(critical)
    finally:
        release.set()
        if jobs is not None:
            jobs.shutdown()
            jobs.executor.shutdown(wait=True)
        controller.stop()


@pytest.mark.parametrize('boundary', ['restart', 'clear'])
def test_old_cage_worker_token_rejects_replacement_epoch(editor, controller, monkeypatch, boundary):
    canvas = editor[0].canvas
    canvas.set_selection('object', editor[2].object_id)
    controller.start()
    jobs, job = _pending(canvas, input_capture.cage_initial)
    entered, release = Event(), Event()
    reserve = RENDER_ADMISSION.reserve
    @contextmanager
    def held_reserve(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        with reserve(*args, **kwargs) as admission:
            yield admission
    monkeypatch.setattr(RENDER_ADMISSION, 'reserve', held_reserve)
    # A genuine function/queue/validator registers the immutable worker token.
    jobs.pending.pop(job.key)
    jobs.active = job
    capture = jobs._capture_job(job)
    try:
        while not capture.advance(.004):
            pass
        jobs._submit(job, capture.result)
        assert entered.wait(10)
        if boundary == 'restart':
            controller.stop()
            controller.start()
        else:
            controller.recorder.clear()
        release.set()
        result = job.future.result(timeout=10)
        assert result['targets'] == list(job.arguments[0])
        assert not any(row['name'].startswith('cage.worker_envelope')
            for row in controller.snapshot()['critical_events'])
        assert controller._input_worker_scope((input_capture.cage_initial, None,
            job.arguments, job.cancelled, jobs.stopped), {}) is None
    finally:
        release.set()
        jobs.shutdown()
        jobs.executor.shutdown(wait=True)
        canvas._cage_prepare = None
        controller.stop()
