"""Sparse slow capture outcomes survive leaf traffic without changing capture."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from comic_editor.render.scene import SceneCapture
from comic_editor.ui import performance_monitor
from test_performance_monitor_integration import editor, controller, _phases


def _install(monkeypatch, controller, canvas):
    clock = SimpleNamespace(wall=performance_monitor.time.perf_counter(), cpu=1.)
    calls = []
    sentinel = object()

    def advance(capture, seconds=.004):
        calls.append((capture, seconds))
        clock.wall += capture.wall_ms / 1000.
        clock.cpu += capture.cpu_ms / 1000.
        if getattr(capture, 'retire', False):
            controller.recorder.clear()
        if getattr(capture, 'fail', False):
            raise ValueError('private source text must not escape')
        return sentinel

    monkeypatch.setattr(SceneCapture, 'advance', advance)
    controller.start()
    controller._writer.stop()
    monkeypatch.setattr(performance_monitor, 'time', SimpleNamespace(
        perf_counter=lambda: clock.wall, thread_time=lambda: clock.cpu))
    document = canvas._render_document_state()

    def capture(wall=23., cpu=3., **extra):
        return SimpleNamespace(owner=canvas, document=document,
                               wall_ms=wall, cpu_ms=cpu, **extra)
    return capture, calls, sentinel, advance


def _slow(controller):
    return [row for row in controller.snapshot()['critical_events']
            if row['name'].startswith('scene.capture_slice.slow')]


def test_only_slow_owned_capture_promotes_wall_cpu_and_survives_flood(
        editor, controller, monkeypatch):
    canvas = editor[0].canvas
    capture, calls, sentinel, _ = _install(monkeypatch, controller, canvas)
    assert SceneCapture.advance(capture(19.), .004) is sentinel
    assert not _slow(controller)
    assert SceneCapture.advance(capture(23., 7.), .002) is sentinel
    with pytest.raises(ValueError, match='private source text'):
        SceneCapture.advance(capture(27., 4., fail=True))
    ordinary = _phases(controller)['scene.capture_slice']
    assert ordinary['count'] == 3
    assert len(calls) == 3 and calls[1][1] == .002
    for index in range(3500):
        controller.recorder.record_event('diagnostic leaf flood', details={'index': index})
    rows = _slow(controller)
    assert [row['name'] for row in rows] == [
        'scene.capture_slice.slow', 'scene.capture_slice.slow.thread_cpu'] * 2
    assert [row['duration_ms'] for row in rows] == pytest.approx([23., 7., 27., 4.])
    assert all(row['details']['scope'] == 'gui_capture_slice'
               and row['details']['thread'] == 'gui'
               and row['details']['source_phase'] == 'scene.capture_slice' for row in rows)
    for row in rows[::2]:
        assert row['details']['source_end_ms'] - row['details']['source_start_ms'] == pytest.approx(
            row['duration_ms'])
    assert rows[2]['details']['exception_type'] == 'ValueError'
    assert 'private source text' not in json.dumps(rows)
    assert not any(row['name'].startswith('scene.capture_slice')
                   for row in controller.snapshot()['timeline'])
    assert _phases(controller)['scene.capture_slice']['count'] == ordinary['count']
    assert controller.snapshot()['bounds']['max_critical_events'] == 512


def test_foreign_worker_closed_and_retired_epoch_do_not_promote(
        editor, controller, monkeypatch):
    canvas = editor[0].canvas
    capture, calls, sentinel, original = _install(monkeypatch, controller, canvas)
    owned = capture()
    foreign = capture()
    foreign.owner = object()
    assert SceneCapture.advance(foreign) is sentinel
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(SceneCapture.advance, owned).result(timeout=10) is sentinel
    assert not _slow(controller)
    assert 'scene.capture_slice' not in _phases(controller)
    assert SceneCapture.advance(capture(retire=True)) is sentinel
    assert not _slow(controller)
    assert 'scene.capture_slice' not in _phases(controller)
    retained = SceneCapture.advance
    controller.stop()
    assert SceneCapture.advance is original
    assert retained(owned) is sentinel
    assert not _slow(controller)
    assert len(calls) == 4


def test_previous_run_wrapper_cannot_promote_into_restart(
        editor, controller, monkeypatch):
    capture, calls, sentinel, _ = _install(monkeypatch, controller, editor[0].canvas)
    retained = SceneCapture.advance
    assert retained(capture()) is sentinel
    assert len(_slow(controller)) == 2
    controller.stop()
    # Stop's detached writer/resource lifecycle uses the ordinary real clock.
    with monkeypatch.context() as restart_patch:
        import time
        restart_patch.setattr(performance_monitor, 'time', time)
        controller.recorder.clear()
        controller.start()
        controller._writer.stop()
        assert retained(capture()) is sentinel
        assert not _slow(controller)
    assert len(calls) == 2


def test_non_opted_owned_probe_keeps_ordinary_summary_without_promotion(
        editor, controller, monkeypatch):
    capture, _calls, sentinel, _ = _install(monkeypatch, controller, editor[0].canvas)

    class Ordinary:
        def invoke(self):
            return sentinel
    controller._patch_owned(Ordinary, 'invoke', 'scene.ordinary_probe', cpu=True)
    with controller._owned_scope({'ownership': 'monitored_editor', 'scope': 'gui_capture_slice'}):
        assert Ordinary().invoke() is sentinel
    assert _phases(controller)['scene.ordinary_probe']['count'] == 1
    assert not _slow(controller)
    assert not any(row['name'].startswith('scene.ordinary_probe')
                   for row in controller.snapshot()['critical_events'])
