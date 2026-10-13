"""Current display sections stay detached; full reports keep every channel."""
import copy
import json
import time

import pytest

from comic_editor.core import performance_monitor as recorder_module
from comic_editor.core.performance_monitor import PerformanceRecorder
from comic_editor.ui.performance_monitor_dialog import PerformanceMonitorDialog
from test_performance_monitor_integration import editor, controller


VIEW_SECTIONS = (
    frozenset(('summary', 'active_spans')),
    frozenset(('timeline',)),
    frozenset(('stalls', 'stack_hotspots')),
    frozenset(('context', 'active_spans', 'recent_transitions', 'critical_events', 'metrics')),
)
CONTAINERS = frozenset(('summary', 'active_spans', 'timeline', 'stalls', 'stack_hotspots',
    'context', 'recent_transitions', 'critical_events', 'metrics'))


def populated_recorder():
    recorder = PerformanceRecorder(sample_interval_ms=60000, max_events=150,
        max_critical_events=4, max_transitions=3)
    recorder.start({'selection': {'ids': ['original']}})
    for index in range(170):
        recorder.record_event('native.packet', index / 10, details={'addresses': [index]},
            category='transition' if index < 5 else 'phase', critical=index < 6)
    recorder.record_metrics({'resource': {'rss_bytes': 123}})
    recorder._last_heartbeat = time.perf_counter() - .6
    recorder._sample_once(recorder._generation)
    recorder.heartbeat()
    return recorder


def alter(value):
    """Mutate public nested containers without changing recorder ownership."""
    if isinstance(value, list):
        if value:
            alter(value[0])
        value.append({'changed': True})
    elif isinstance(value, dict):
        for child in list(value.values()):
            if isinstance(child, (list, dict)):
                alter(child)
                break
        value['changed'] = True


@pytest.mark.parametrize('sections', VIEW_SECTIONS)
def test_requested_display_fields_equal_full_report_and_are_nested_detached(monkeypatch, sections):
    recorder = populated_recorder()
    try:
        # Stop only the sampler to make ordinary snapshots reproducible. Keep
        # the real recording epoch and active-span path throughout this check.
        recorder._stop_event.set()
        recorder._thread.join(timeout=2)
        recorder.snapshot()  # Initialize environment before freezing the clock.
        with recorder.measure('active native owner', details={'addresses': [3, 9]}):
            with monkeypatch.context() as patch:
                patch.setattr(recorder_module.time, 'perf_counter', lambda: recorder._origin + 10)
                patch.setattr(recorder_module, '_utc_now', lambda: 'fixed snapshot time')
                full = recorder.snapshot(timeline_limit=100)
                view = recorder.snapshot(timeline_limit=100, sections=sections)
                assert view.keys() == full.keys()
                assert all(view[key] == full[key] for key in sections)
                assert all(view[key] == full[key] for key in
                    ('schema_version', 'enabled', 'metadata', 'bounds', 'dropped'))
                for key in CONTAINERS - sections:
                    assert view[key] == ({} if key in ('context', 'metrics') else [])
                original = copy.deepcopy(view)
                for key in sections:
                    alter(view[key])
                assert recorder.snapshot(timeline_limit=100, sections=sections) == original
                # Taking or mutating a partial view cannot rewrite, trim or
                # otherwise reduce any ordinary full-report channel.
                assert recorder.snapshot(timeline_limit=100) == full
                assert json.dumps(recorder.snapshot(timeline_limit=100, sections=None),
                    ensure_ascii=False, indent=2) == json.dumps(full, ensure_ascii=False, indent=2)
        full = recorder.snapshot()
        assert len(full['timeline']) == 150
        assert len(full['critical_events']) == 4
        assert len(full['recent_transitions']) == 3
        assert full['stalls'] and full['stack_hotspots']
    finally:
        recorder.stop()


def test_phase_projection_does_not_detach_unused_event_and_stack_graphs(monkeypatch):
    recorder = populated_recorder()
    original_copy = recorder_module.copy.deepcopy
    captured = []
    def observed_copy(value, *args, **kwargs):
        if isinstance(value, dict) and 'schema_version' in value:
            captured.append(value)
        return original_copy(value, *args, **kwargs)
    try:
        monkeypatch.setattr(recorder_module.copy, 'deepcopy', observed_copy)
        full = recorder.snapshot()
        assert full['timeline'] and full['stalls'] and full['critical_events']
        phase = recorder.snapshot(sections=VIEW_SECTIONS[0])
        detached = captured[-1]
        assert all(not detached[key] for key in CONTAINERS - VIEW_SECTIONS[0]
            if key not in ('summary', 'stack_hotspots'))
        assert phase['summary'] == full['summary']
        assert recorder.snapshot()['critical_events'] == full['critical_events']
    finally:
        recorder.stop()


def test_actual_tab_changes_fetch_fresh_sections_and_export_full_capture(
        editor, controller, qapp, monkeypatch, tmp_path):
    dialog = PerformanceMonitorDialog(controller, editor[0])
    monkeypatch.setattr(editor[0], '_performance_monitor_dialog', dialog, raising=False)
    dialog.show()
    qapp.processEvents()
    controller.start()
    controller._writer.stop()
    recorder = controller.recorder
    try:
        for index in range(150):
            recorder.record_event(f'view-source-{index}', 1, details={'native': [index]},
                category='transition' if index < 2 else 'phase', critical=index < 3)
        dialog._refresh()
        assert dialog.tabs.currentIndex() == 0
        assert dialog._snapshot['summary']
        assert dialog._snapshot['timeline'] == dialog._snapshot['critical_events'] == []
        assert dialog._snapshot['stalls'] == dialog._snapshot['stack_hotspots'] == []
        recorder.record_event('fresh after phase tab', details={'native': [151]})
        dialog.tabs.setCurrentIndex(1)
        assert len(dialog._snapshot['timeline']) == 100
        assert any(row['name'] == 'fresh after phase tab' for row in dialog._snapshot['timeline'])
        assert dialog._snapshot['summary'] == dialog._snapshot['stalls'] == []
        assert dialog.event_table.rowCount() == 100
        recorder._last_heartbeat = time.perf_counter() - .6
        recorder._sample_once(recorder._generation)
        recorder.heartbeat()
        dialog.tabs.setCurrentIndex(2)
        assert dialog._snapshot['stalls'] and dialog._snapshot['stack_hotspots']
        assert dialog.stall_tree.topLevelItemCount() > 0
        assert dialog._snapshot['timeline'] == dialog._snapshot['critical_events'] == []
        recorder.update_context({'selection': {'id': 'fresh context tab'}})
        dialog.tabs.setCurrentIndex(3)
        assert dialog._snapshot['context']['selection']['id'] == 'fresh context tab'
        assert len(dialog._snapshot['critical_events']) >= 3
        assert 'fresh context tab' in dialog.context_text.toPlainText()
        assert not dialog._snapshot['summary'] and not dialog._snapshot['stalls']
        # Existing no-argument API still returns the ordinary limited full
        # display report, including every detail channel.
        compatible = controller.snapshot_for_display()
        assert len(compatible['timeline']) == 100
        assert compatible['summary'] and compatible['stalls'] and compatible['critical_events']
        path = tmp_path / 'full native capture.json'
        monkeypatch.setattr('comic_editor.ui.performance_monitor_dialog.QFileDialog.getSaveFileName',
            lambda *_args: (str(path), 'JSON files (*.json)'))
        dialog._export_capture()
        exported = json.loads(path.read_text(encoding='utf-8'))
        assert not exported['enabled']
        assert len([row for row in exported['timeline'] if row['name'].startswith('view-source-')]) == 150
        assert exported['summary'] and exported['stalls'] and exported['stack_hotspots']
        assert exported['critical_events'] and exported['recent_transitions']
        assert exported['context']['selection']['id'] == 'fresh context tab'
    finally:
        controller.stop()
        dialog.close()
        dialog.deleteLater()
