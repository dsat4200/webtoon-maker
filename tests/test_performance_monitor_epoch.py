"""External span ownership is pinned at the recorder's locked epoch boundary."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from comic_editor.core import performance_monitor as recorder_module
from comic_editor.core.performance_monitor import PerformanceRecorder


@pytest.mark.parametrize('boundary', ['restart', 'clear'])
def test_supplied_old_epoch_rejects_events_and_span_entry_after_boundary(boundary):
    recorder = PerformanceRecorder()
    recorder.start()
    old = recorder.capture_epoch
    delayed = recorder.measure('created before boundary', capture_epoch=old)
    if boundary == 'restart':
        recorder.stop()
        recorder.start()
    else:
        recorder.clear()
    current = recorder.capture_epoch
    assert current != old
    try:
        recorder.record_event('stale event', 2, capture_epoch=old)
        with recorder.measure('stale span', capture_epoch=old):
            pass
        with delayed:
            assert not recorder.snapshot()['active_spans']
        assert not recorder.snapshot()['timeline']
        recorder.record_event('current event', 1, capture_epoch=current)
        with recorder.measure('current span', capture_epoch=current):
            pass
        assert {event['name'] for event in recorder.snapshot()['timeline']} == {'current event', 'current span'}
    finally:
        recorder.stop()


@pytest.mark.parametrize('boundary', ['restart', 'clear'])
@pytest.mark.parametrize('explicit_epoch', [False, True])
def test_record_event_detachment_race_does_not_cross_epoch(monkeypatch, boundary, explicit_epoch):
    recorder = PerformanceRecorder()
    recorder.start()
    old = recorder.capture_epoch
    entered, release = threading.Event(), threading.Event()
    payload = {'sentinel': 'bounded old worker'}
    original = recorder_module._json_value
    def detach(value, *args, **kwargs):
        if value is payload:
            entered.set()
            assert release.wait(10)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(recorder_module, '_json_value', detach)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            kwargs = {'capture_epoch': old} if explicit_epoch else {}
            future = executor.submit(recorder.record_event, 'old detached event',
                1, details=payload, **kwargs)
            assert entered.wait(10)
            if boundary == 'restart':
                recorder.stop()
                recorder.start()
            else:
                recorder.clear()
            release.set()
            future.result(timeout=10)
        assert not recorder.snapshot()['timeline']
    finally:
        release.set()
        recorder.stop()


def test_legacy_recorder_call_signatures_remain_valid_and_exceptions_propagate():
    recorder = PerformanceRecorder()
    recorder.start()
    try:
        recorder.record_event('old signature', 4., 'phase', {'legacy': True})
        with pytest.raises(RuntimeError, match='unchanged application error'):
            with recorder.measure('old measure signature', 'phase', {'legacy': True}):
                raise RuntimeError('unchanged application error')
        events = recorder.snapshot()['timeline']
        assert len(events) == 2
        assert events[-1]['details'] == {'legacy': True, 'exception_type': 'RuntimeError'}
    finally:
        recorder.stop()
