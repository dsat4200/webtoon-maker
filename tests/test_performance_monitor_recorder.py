"""Recorder lifecycle and bounded diagnostics, independent of editor documents."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from comic_editor.core.performance_monitor import PerformanceRecorder


def test_disabled_recorder_has_no_thread_or_sampling(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Disabled recorder must not start monitoring")

    monkeypatch.setattr(threading.Thread, "start", unexpected)
    monkeypatch.setattr("sys._current_frames", unexpected)
    monkeypatch.setattr("sys.setprofile", unexpected)
    recorder = PerformanceRecorder()
    recorder.record_event("ignored", 12)
    recorder.record_event("ignored critical", critical=True)
    recorder.record_metrics({"cpu_percent": 17})
    recorder.update_context({"tool": "draw"})
    recorder.heartbeat()
    with recorder.measure("ignored span"):
        pass
    with recorder.measure("ignored critical span", critical=True):
        pass
    recorder.stop()
    report = recorder.snapshot()
    assert not report["enabled"]
    assert report["timeline"] == []
    assert report["critical_events"] == []
    assert report["summary"] == []
    assert report["stack_hotspots"] == []
    assert report["metadata"]["sample_count"] == 0
    assert recorder._thread is None
    json.dumps(report, allow_nan=False)


def test_phase_summary_percentiles_and_bounded_storage():
    recorder = PerformanceRecorder(
        max_events=4, max_phases=2, max_durations_per_phase=3,
        max_stack_locations=2, max_stack_depth=2,
    )
    recorder.start({"tool": "draw"})
    try:
        for duration in (1, 2, 3, 4, 100):
            recorder.record_event("draw", duration, category="input")
        recorder.record_event("paint", 5)
        recorder.record_event("excess phase", 6)
        # Force deterministic samples on this thread instead of timing a sleep.
        for _ in range(3):
            recorder._sample_once(recorder._generation)
    finally:
        recorder.stop()
    report = recorder.snapshot()
    assert len(report["timeline"]) == 4
    assert len(report["summary"]) == 2
    draw = next(row for row in report["summary"] if row["name"] == "draw")
    assert draw == {
        "name": "draw", "category": "input", "count": 5,
        "total_ms": 110, "mean_ms": 22, "max_ms": 100,
        "p50_ms": 4, "p95_ms": 100, "recent_sample_count": 3,
    }
    assert report["dropped"]["timeline_events"] == 3
    assert report["dropped"]["duration_samples"] == 2
    assert report["dropped"]["phase_events"] == 1
    assert len(report["stack_hotspots"]) <= 2
    assert report["dropped"]["truncated_stacks"] >= 3


def test_measure_records_exceptions_and_context_is_detached():
    context = {"tool": "draw", "selection": {"count": 5}}
    recorder = PerformanceRecorder()
    recorder.start(context)
    try:
        context["selection"]["count"] = 999
        with pytest.raises(ValueError, match="preserved"):
            with recorder.measure("tool activation", details={"solo": True}):
                raise ValueError("preserved")
        recorder.record_metrics({"process_cpu_percent": 10})
        report = recorder.snapshot()
        event = report["timeline"][0]
        assert event["name"] == "tool activation"
        assert event["duration_ms"] >= 0
        assert event["details"] == {"solo": True, "exception_type": "ValueError"}
        assert event["context"]["selection"]["count"] == 5
        event["context"]["selection"]["count"] = 888
        assert recorder.snapshot()["context"]["selection"]["count"] == 5
        assert report["metrics"]["process_cpu_percent"] == 10
        assert "preserved" not in json.dumps(report)
    finally:
        recorder.stop()


def test_summary_only_events_preserve_input_rows_and_every_duration():
    recorder = PerformanceRecorder(max_events=4)
    recorder.start()
    try:
        recorder.record_event('mouse.press')
        for _ in range(1000):
            recorder.record_event('new leaf', .1, category='phase', retain_timeline=False)
            recorder.record_event('new leaf.thread_cpu', 0, category='worker_cpu', retain_timeline=False)
        recorder.record_event('new leaf', 2, category='phase')
        recorder.record_event('existing unfiltered', .01, category='phase')
        report = recorder.snapshot()
        assert [row['name'] for row in report['timeline']] == [
            'mouse.press', 'new leaf', 'existing unfiltered']
        phases = {row['name']: row for row in report['summary']}
        assert phases['new leaf']['count'] == 1001
        assert phases['new leaf']['total_ms'] == pytest.approx(102)
        assert phases['new leaf']['max_ms'] == 2
        assert phases['new leaf.thread_cpu']['count'] == 1000
        assert phases['new leaf.thread_cpu']['total_ms'] == 0
        assert report['dropped']['timeline_events'] == 0
    finally:
        recorder.stop()


def test_explicit_critical_events_survive_ordinary_flood_without_changing_timeline():
    recorder = PerformanceRecorder(max_events=4, max_critical_events=3)
    recorder.start({'selection': {'count': 2}})
    try:
        # Semantic names alone never opt into this separate channel.
        recorder.record_event('mouse.press', category='input')
        with recorder.measure('canvas.begin_stroke'):
            pass
        assert recorder.snapshot()['critical_events'] == []

        for index in range(5):
            recorder.record_event(f'explicit.{index}', 1, category='input', critical=True)
        recorder.record_event('summary only critical', 2, retain_timeline=False, critical=True)
        for index in range(100):
            recorder.record_event('ordinary phase', .1)
        report = recorder.snapshot(timeline_limit=1)
        assert [event['name'] for event in report['critical_events']] == [
            'explicit.3', 'explicit.4', 'summary only critical']
        assert report['bounds']['max_critical_events'] == 3
        assert report['dropped']['critical_events'] == 3
        assert report['timeline'][0]['name'] == 'ordinary phase'
        assert len(recorder.snapshot()['timeline']) == 4
        assert recorder.snapshot(include_timeline=False)['critical_events'] == report['critical_events']
        assert next(row for row in report['summary'] if row['name'] == 'ordinary phase')['count'] == 100
        report['critical_events'][0]['context']['selection']['count'] = 999
        assert recorder.snapshot()['critical_events'][0]['context']['selection']['count'] == 2
    finally:
        recorder.stop()


def test_critical_event_capacity_cannot_exceed_512():
    recorder = PerformanceRecorder(max_critical_events=10000, max_events=1)
    recorder.start()
    try:
        for index in range(515):
            recorder.record_event(str(index), critical=True, retain_timeline=False)
        report = recorder.snapshot()
        assert report['bounds']['max_critical_events'] == 512
        assert len(report['critical_events']) == 512
        assert report['critical_events'][0]['name'] == '3'
        assert report['dropped']['critical_events'] == 3
        assert report['timeline'] == []
    finally:
        recorder.stop()


def test_critical_measure_detaches_bounded_metadata_and_preserves_exception():
    recorder = PerformanceRecorder()
    recorder.start()
    details = {'points': [1, 2], 'label': 'x' * 1000}
    try:
        with pytest.raises(ValueError, match='application error'):
            with recorder.measure('explicit span', details=details, critical=True):
                details['points'].append(3)
                raise ValueError('application error')
        report = recorder.snapshot()
        event = report['critical_events'][0]
        assert event == report['timeline'][0]
        assert event['details'] == {'points': [1, 2], 'label': 'x' * 256,
                                    'exception_type': 'ValueError'}
        event['details']['points'].append(99)
        assert recorder.snapshot()['critical_events'][0]['details']['points'] == [1, 2]
        json.dumps(report, allow_nan=False)
    finally:
        recorder.stop()


@pytest.mark.parametrize('boundary', ['clear', 'restart'])
def test_critical_events_and_delayed_spans_cannot_cross_capture_epoch(boundary):
    recorder = PerformanceRecorder()
    recorder.start()
    old_epoch = recorder.capture_epoch
    delayed = recorder.measure('old delayed', capture_epoch=old_epoch, critical=True)
    try:
        with recorder.measure('old active', critical=True):
            if boundary == 'clear':
                recorder.clear()
            else:
                recorder.stop()
                recorder.start()
        recorder.record_event('old event', capture_epoch=old_epoch, critical=True)
        with delayed:
            pass
        assert recorder.snapshot()['critical_events'] == []
        recorder.record_event('current', critical=True)
        assert [event['name'] for event in recorder.snapshot()['critical_events']] == ['current']
        recorder.clear()
        report = recorder.snapshot()
        assert report['critical_events'] == []
        assert report['dropped']['critical_events'] == 0
    finally:
        recorder.stop()


def test_sampler_observes_gui_sleep_and_heartbeat_stall():
    recorder = PerformanceRecorder(sample_interval_ms=5, stall_threshold_ms=25)
    sample_seen = threading.Event()
    original_sample = recorder._sample_once

    def sample_and_notify(generation):
        original_sample(generation)
        if recorder._active_stall is not None:
            sample_seen.set()

    recorder._sample_once = sample_and_notify
    recorder.start({"tool": "transform", "solo": True})
    thread = recorder._thread
    try:
        # Event.wait releases the GIL like many slow Qt/native calls. A generous
        # deadline checks the sampler's progress, not a fragile exact duration.
        assert sample_seen.wait(2), "Sampler failed to capture the blocked GUI thread"
        recorder.heartbeat()
    finally:
        recorder.stop()
    report = recorder.snapshot()
    assert not thread.is_alive()
    assert report["metadata"]["sample_count"] > 0
    assert len(report["stalls"]) == 1
    stall = report["stalls"][0]
    assert stall["completed"]
    assert stall["gap_ms"] >= 25
    assert stall["context"]["solo"] is True
    assert any(frame["function"] == "test_sampler_observes_gui_sleep_and_heartbeat_stall" for frame in stall["stack"])
    assert all(set(frame) == {"file", "function", "line"} for frame in stall["stack"])
    assert any(row["name"] == "GUI heartbeat gap" for row in report["summary"])


def test_stop_start_and_clear_lifecycle():
    recorder = PerformanceRecorder()
    recorder.start({"tool": "draw"})
    first_thread = recorder._thread
    recorder.start()
    assert recorder._thread is first_thread
    recorder.record_event("before clear", 2)
    recorder.clear()
    assert recorder.enabled
    assert recorder._thread is first_thread
    assert recorder.snapshot()["timeline"] == []
    recorder.record_event("after clear", 4)
    recorder.stop()
    recorder.stop()
    assert not first_thread.is_alive()
    assert recorder._thread is None
    recorder.start()
    second_thread = recorder._thread
    try:
        assert second_thread is not first_thread
        assert recorder.snapshot()["timeline"][0]["name"] == "after clear"
        assert recorder.snapshot()["metadata"]["sessions"] == 2
    finally:
        recorder.stop()
    assert not second_thread.is_alive()
    recorder.clear()
    assert not recorder.enabled
    assert recorder.snapshot()["timeline"] == []


def test_span_started_before_stop_is_not_attributed_to_new_capture():
    recorder = PerformanceRecorder()
    recorder.start()
    with recorder.measure("old span"):
        recorder.stop()
        recorder.start()
    recorder.stop()
    assert recorder.snapshot()["timeline"] == []


def test_stall_and_metadata_bounds():
    recorder = PerformanceRecorder(max_stalls=2)
    recorder.start({"items": list(range(100)), "label": "x" * 1000})
    try:
        # Synthesize elapsed heartbeats without relying on scheduler timings.
        for _ in range(5):
            with recorder._lock:
                recorder._last_heartbeat = time.perf_counter() - 1
            recorder.heartbeat()
        recorder.record_event("bounded", details={"value": float("nan"), "unsupported": object()})
    finally:
        recorder.stop()
    report = recorder.snapshot()
    assert len(report["stalls"]) == 2
    assert report["dropped"]["stalls"] == 3
    assert len(report["context"]["items"]) == 32
    assert len(report["context"]["label"]) == 256
    json.dumps(report, allow_nan=False)


def test_active_spans_bounded_visible_from_worker_and_clear_invalidates_span():
    recorder = PerformanceRecorder(max_active_spans=1)
    recorder.start({"solo": True})
    try:
        with recorder.measure("outer", details={"tool": "draw"}):
            with recorder.measure("inner"):
                reports = []
                worker = threading.Thread(target=lambda: reports.append(recorder.snapshot()))
                worker.start()
                worker.join(timeout=2)
                assert not worker.is_alive()
                active = reports[0]["active_spans"]
                assert len(active) == 1
                assert active[0]["name"] == "outer"
                assert active[0]["context"]["solo"] is True
                assert active[0]["duration_ms"] >= 0
                assert reports[0]["dropped"]["active_spans"] == 1
            assert len(recorder.snapshot()["active_spans"]) == 1
        assert recorder.snapshot()["active_spans"] == []
        with recorder.measure("before clear"):
            recorder.clear()
        assert recorder.snapshot()["timeline"] == []
        assert recorder.snapshot()["active_spans"] == []
    finally:
        recorder.stop()


def test_hotspot_locations_cannot_grow_beyond_cap(monkeypatch):
    recorder = PerformanceRecorder(sample_interval_ms=60000, max_stack_locations=2)
    recorder.start()
    try:
        for line in range(10):
            frame = SimpleNamespace(
                f_code=SimpleNamespace(co_filename="example.py", co_name="paint"),
                f_lineno=line,
                f_back=None,
            )
            monkeypatch.setattr("sys._current_frames", lambda: {recorder._target_thread_id: frame})
            recorder._sample_once(recorder._generation)
    finally:
        recorder.stop()
    report = recorder.snapshot()
    assert len(report["stack_hotspots"]) == 2
    assert report["dropped"]["stack_location_observations"] == 8
    assert report["metadata"]["sample_count"] == 10


def test_display_snapshot_limits_recent_rows_without_changing_full_capture():
    recorder = PerformanceRecorder(max_events=20, max_transitions=3)
    recorder.start({"nested": {"selected": "original"}})
    try:
        for index in range(5):
            recorder.record_event(f"tool.{index}.begin", category="transition")
        for index in range(30):
            recorder.record_event(f"cache.{index}", 0.1, category="cache")
        display = recorder.snapshot(timeline_limit=2)
        assert [row["name"] for row in display["timeline"]] == ["cache.28", "cache.29"]
        assert [row["name"] for row in display["recent_transitions"]] == [
            "tool.2.begin", "tool.3.begin", "tool.4.begin",
        ]
        assert display["dropped"]["transition_events"] == 2
        display["recent_transitions"][0]["context"]["nested"]["selected"] = "changed"
        assert recorder.snapshot()["context"]["nested"]["selected"] == "original"
        assert len(recorder.snapshot()["timeline"]) == 20
        assert recorder.snapshot(timeline_limit=0)["timeline"] == []
        assert recorder.snapshot(include_timeline=False)["timeline"] == []
    finally:
        recorder.stop()


def test_snapshot_deepcopy_does_not_hold_recording_lock(monkeypatch):
    import comic_editor.core.performance_monitor as module

    recorder = PerformanceRecorder(sample_interval_ms=60000)
    recorder.start({"selection": {"id": "original"}})
    copying = threading.Event()
    release_copy = threading.Event()
    reports = []
    original_copy = module.copy.deepcopy

    def pause_copy(value, *args, **kwargs):
        # Only pause the outer detached-report copy, not deepcopy's recursion.
        if isinstance(value, dict) and "schema_version" in value:
            copying.set()
            assert release_copy.wait(2)
        return original_copy(value, *args, **kwargs)

    monkeypatch.setattr(module.copy, "deepcopy", pause_copy)
    recorder.record_event("before", details={"value": [1]})
    worker = threading.Thread(target=lambda: reports.append(recorder.snapshot()))
    worker.start()
    try:
        assert copying.wait(2)
        # Acquiring non-blockingly proves input can record while export copies.
        assert recorder._lock.acquire(blocking=False)
        try:
            recorder.update_context({"selection": {"id": "after"}})
            recorder.record_event("during copy")
        finally:
            recorder._lock.release()
        release_copy.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert reports[0]["context"]["selection"]["id"] == "original"
        assert [row["name"] for row in reports[0]["timeline"]] == ["before"]
    finally:
        release_copy.set()
        worker.join(timeout=2)
        recorder.stop()


def test_snapshot_during_active_span_exception_keeps_detached_start_details():
    recorder = PerformanceRecorder(sample_interval_ms=60000)
    recorder.start()
    try:
        with pytest.raises(RuntimeError):
            with recorder.measure("fails", details={"requested_tool": "pencil"}):
                before = recorder.snapshot()
                raise RuntimeError("expected")
        assert "exception_type" not in before["active_spans"][0]["details"]
        after = recorder.snapshot()
        assert after["timeline"][0]["details"]["exception_type"] == "RuntimeError"
        assert after["active_spans"] == []
    finally:
        recorder.stop()


def test_internal_log_view_pins_immutable_records_and_copies_mutable_shells(monkeypatch):
    import comic_editor.core.performance_monitor as module
    recorder = PerformanceRecorder(sample_interval_ms=60000)
    details = {'points': [1, 2]}
    recorder.start({'selection': {'id': 'original'}})
    try:
        with recorder.measure('active', details=details):
            recorder.record_event('before', details=details, critical=True)
            expected = recorder.snapshot()
            with monkeypatch.context() as patch:
                patch.setattr(module.copy, 'deepcopy', lambda *a, **k:
                    pytest.fail('Internal read-only log view traversed published records'))
                report = recorder._snapshot_for_log()
            assert 'duration_ms' not in next(iter(recorder._active_spans.values()))
            assert report['active_spans'][0] is not next(iter(recorder._active_spans.values()))
            assert report['timeline'] is not recorder._timeline
            assert report['timeline'][0] is recorder._timeline[0]
            # Both reports contain identical data apart from capture clocks.
            for value in (report, expected):
                value['metadata'].pop('snapshot_at')
                value['metadata'].pop('capture_seconds')
                value['active_spans'][0].pop('duration_ms')
            assert report == expected
            details['points'].append(3)
            recorder.update_context({'selection': {'id': 'later'}})
            recorder.record_event('after')
        recorder.clear()
        recorder.record_event('replacement')
        assert report['context']['selection']['id'] == 'original'
        assert [event['name'] for event in report['timeline']] == ['before']
        assert report['timeline'][0]['details']['points'] == [1, 2]
        assert report['active_spans'][0]['name'] == 'active'
        assert report['critical_events'][0]['details']['points'] == [1, 2]
        assert report['dropped'] == expected['dropped']
    finally:
        recorder.stop()
