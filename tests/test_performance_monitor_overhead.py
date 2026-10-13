"""Self-observation is owner filtered and preserves ordinary report encoding."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest
from PySide6.QtWidgets import QApplication

from comic_editor.core import performance_log
from comic_editor.core.performance_log import PerformanceLogWriter
from comic_editor.core.performance_resources import ProcessResourceSampler
from comic_editor.ui.performance_monitor import _LogJsonFacade
from comic_editor.ui.performance_monitor_dialog import PerformanceMonitorDialog
from test_performance_monitor_integration import editor, controller, _phases


def _payload():
    return {'schema_version': 1, 'metadata': {'fixture': 'detached'},
        'timeline': [], 'summary': [], 'metrics': [],
        'unicode': '漫画 🖊', 'nested': {'a': [1, None, True], 'tuple': (2, 3)},
        'number': float('nan')}


def _manual_writer(provider, directory):
    writer = PerformanceLogWriter(provider, directory)
    writer.directory = directory
    writer.json_path = directory / 'capture.json'
    writer.summary_path = directory / 'summary.md'
    writer._started_utc = 'fixed-start'
    return writer


def _use_manual_writer(controller, writer, *, clear=True):
    controller._writer.stop()
    controller._writer = writer
    controller._writer_finalized = False
    if clear:
        controller.recorder.clear()
    controller._register_monitor_object(writer, 'detached_monitor_log')


def _rows(controller, prefix):
    return [row for row in controller.snapshot()['timeline']
        if row['name'].startswith(prefix)]


def _assert_clock_pairs(rows, names, *, scope, thread):
    assert {row['name'] for row in rows} == names | {name + '.thread_cpu' for name in names}
    assert all(row['duration_ms'] >= 0 and row['details']['scope'] == scope
        and row['details']['thread'] == thread for row in rows)
    assert {row['category'] for row in rows if row['name'].endswith('.thread_cpu')} == {
        'gui_cpu' if thread == 'gui' else 'worker_cpu'}


def test_resource_sample_and_metrics_only_observe_current_gui_sampler(
        editor, controller, monkeypatch):
    gui = threading.get_ident()
    calls = []
    original = ProcessResourceSampler.sample
    def sample(sampler):
        token = controller._current_scope()
        calls.append((threading.get_ident(), token.details() if token else None))
        return original(sampler)
    monkeypatch.setattr(ProcessResourceSampler, 'sample', sample)
    original_focus = QApplication.focusWidget
    def gui_only_focus():
        assert threading.get_ident() == gui, 'Diagnostic worker read Qt focus'
        return original_focus()
    monkeypatch.setattr(QApplication, 'focusWidget', gui_only_focus)
    controller.start()
    controller._writer.stop()
    calls.clear()
    try:
        resources = controller._resources
        foreign = ProcessResourceSampler()
        before = _phases(controller)['monitor.resource_sample']['count']
        resources.sample()
        controller._metrics()
        assert _phases(controller)['monitor.resource_sample']['count'] == before + 2
        foreign.sample()
        with ThreadPoolExecutor(max_workers=1) as pool:
            worker_sample = pool.submit(resources.sample).result(timeout=10)
            worker_snapshot = pool.submit(controller.snapshot).result(timeout=10)
        assert 'memory_source' in worker_sample and worker_snapshot['schema_version'] == 1
        assert _phases(controller)['monitor.resource_sample']['count'] == before + 2
        assert calls[0][1]['scope'] == calls[1][1]['scope'] == 'gui_monitor_resource'
        assert calls[2][1] is None and calls[3][1] is None and calls[3][0] != gui
        rows = _rows(controller, 'monitor.resource_sample')
        _assert_clock_pairs(rows, {'monitor.resource_sample'},
            scope='gui_monitor_resource', thread='gui')
        _assert_clock_pairs(_rows(controller, 'monitor.metrics_collect'),
            {'monitor.metrics_collect'}, scope='gui_monitor_metrics', thread='gui')
        assert all(row['details']['thread_id'] == gui for row in rows)
        # A direct epoch clear never re-adopts previously registered resources.
        controller.recorder.clear()
        resources.sample()
        controller._metrics()
        assert not _rows(controller, 'monitor.resource_sample')
        assert not _rows(controller, 'monitor.metrics_collect')
    finally:
        controller.stop()


def test_log_facade_preserves_exact_bytes_and_rejects_nested_foreign_writer(
        editor, controller, monkeypatch, tmp_path):
    monkeypatch.setattr(performance_log, '_utc', lambda: 'fixed-checkpoint')
    original_json, original_dumps = performance_log.json, json.dumps
    payload = _payload()
    pristine = json.dumps(payload, ensure_ascii=False, default=str)
    baseline = _manual_writer(lambda: payload, tmp_path / 'baseline')
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(baseline._save, 'recording').result(timeout=10)
    assert not baseline.error
    controller.start()
    assert isinstance(performance_log.json, _LogJsonFacade)
    assert json.dumps is original_dumps
    foreign = _manual_writer(lambda: payload, tmp_path / 'foreign')
    def provider():
        # Same header and worker thread deliberately exercise scope masking.
        foreign._save('recording')
        return payload
    candidate = _manual_writer(provider, tmp_path / 'candidate')
    _use_manual_writer(controller, candidate)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(candidate._save, 'recording').result(timeout=10)
        assert not candidate.error and not foreign.error
        assert candidate.json_path.read_bytes() == baseline.json_path.read_bytes() == foreign.json_path.read_bytes()
        assert candidate.summary_path.read_bytes() == baseline.summary_path.read_bytes()
        assert json.dumps(payload, ensure_ascii=False, default=str) == pristine
        rows = _rows(controller, 'monitor.log_')
        _assert_clock_pairs(rows, {'monitor.log_save', 'monitor.log_serialize',
            'monitor.log_summary', 'monitor.log_write'},
            scope='detached_monitor_log', thread='worker')
        assert all(row['details']['writer_id'] == id(candidate) for row in rows)
        assert all(row['details']['thread_id'] != threading.get_ident() for row in rows)
        phases = _phases(controller)
        assert phases['monitor.log_save']['count'] == phases['monitor.log_serialize']['count'] == 1
        assert phases['monitor.log_write']['count'] == 2
        assert {row['details']['file_role'] for row in rows if row['name'] == 'monitor.log_write'} == {
            'capture_json', 'summary'}
        count = phases['monitor.log_save']['count']
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(foreign._save, 'recording').result(timeout=10)
        assert _phases(controller)['monitor.log_save']['count'] == count
    finally:
        controller.stop()
    assert performance_log.json is original_json and json.dumps is original_dumps


def test_old_sampler_and_retained_wrapper_never_enter_restarted_capture(editor, controller):
    controller.start()
    controller._writer.stop()
    old_sampler, old_wrapper = controller._resources, ProcessResourceSampler.sample
    controller.stop()
    controller.start()
    controller._writer.stop()
    try:
        current = controller._resources
        count = _phases(controller)['monitor.resource_sample']['count']
        old_sampler.sample()
        old_wrapper(old_sampler)
        old_wrapper(current)
        assert _phases(controller)['monitor.resource_sample']['count'] == count
        current.sample()
        assert _phases(controller)['monitor.resource_sample']['count'] == count + 1
        assert all(row['details']['run_generation'] == controller._run_serial
            for row in _rows(controller, 'monitor.resource_sample'))
    finally:
        controller.stop()


def test_owned_writer_snapshot_is_detached_and_has_wall_and_thread_cpu(
        editor, controller, monkeypatch, tmp_path):
    gui = threading.get_ident()
    controller.start()
    candidate = _manual_writer(controller.snapshot, tmp_path / 'candidate')
    _use_manual_writer(controller, candidate)
    def no_qt_read():
        raise AssertionError('Log instrumentation read live Qt state')
    monkeypatch.setattr(QApplication, 'focusWidget', no_qt_read)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(candidate._save, 'recording').result(timeout=10)
        assert not candidate.error
        rows = _rows(controller, 'monitor.log_snapshot')
        _assert_clock_pairs(rows, {'monitor.log_snapshot'},
            scope='detached_monitor_log', thread='worker')
        assert all(row['details']['writer_id'] == id(candidate)
            and row['details']['thread_id'] != gui for row in rows)
        # Restore the GUI-only focus function before ordinary stop metrics.
    finally:
        monkeypatch.setattr(QApplication, 'focusWidget', lambda: None)
        controller.stop()


def test_encoding_error_is_unchanged_and_reported_in_the_owned_leaf(
        editor, controller, tmp_path):
    class BadText:
        def __str__(self):
            raise ValueError('intentional encoding failure')
    payload = _payload()
    payload['bad'] = BadText()
    baseline = _manual_writer(lambda: payload, tmp_path / 'baseline')
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(baseline._save, 'recording').result(timeout=10)
    controller.start()
    candidate = _manual_writer(lambda: payload, tmp_path / 'candidate')
    _use_manual_writer(controller, candidate)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(candidate._save, 'recording').result(timeout=10)
        assert candidate.error == baseline.error == 'ValueError: intentional encoding failure'
        assert not candidate.json_path.exists() and not baseline.json_path.exists()
        rows = _rows(controller, 'monitor.log_serialize')
        assert len(rows) == 2
        assert all(row['details']['exception_type'] == 'ValueError' for row in rows)
    finally:
        controller.stop()


@pytest.mark.parametrize('boundary', ['clear', 'restart'])
def test_old_running_writer_cannot_complete_or_reenter_in_a_new_capture(
        editor, controller, tmp_path, boundary):
    entered, release = threading.Event(), threading.Event()
    def provider():
        entered.set()
        assert release.wait(10)
        return _payload()
    controller.start()
    old_writer = _manual_writer(provider, tmp_path / 'old')
    _use_manual_writer(controller, old_writer)
    old_epoch = controller.recorder.capture_epoch
    old_wrapper = PerformanceLogWriter._save
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            future = pool.submit(old_writer._save, 'recording')
            assert entered.wait(10)
            assert any(row['name'] == 'monitor.log_save'
                for row in controller.snapshot()['active_spans'])
            if boundary == 'restart':
                controller.stop()
                controller.start()
                controller._writer.stop()
            else:
                controller.recorder.clear()
            assert controller.recorder.capture_epoch != old_epoch
            release.set()
            future.result(timeout=10)
            pool.submit(old_wrapper, old_writer, 'recording').result(timeout=10)
            assert not old_writer.error
            assert not any(row['details'].get('writer_id') == id(old_writer)
                for row in _rows(controller, 'monitor.log_'))
        finally:
            release.set()
            controller.stop()


@pytest.mark.parametrize('stage', ['serialize', 'write'])
def test_stop_between_child_scope_validation_and_use_cannot_break_original_save(
        editor, controller, monkeypatch, tmp_path, stage):
    controller.start()
    candidate = _manual_writer(_payload, tmp_path / stage)
    _use_manual_writer(controller, candidate)
    old_epoch = controller.recorder.capture_epoch
    current_scope = controller._current_log_scope
    target_name = '_log_serialize_scope' if stage == 'serialize' else '_log_write_scope'
    original_target = getattr(controller, target_name)
    state = {'target': False, 'stopped': False}
    def targeted_scope(*args):
        state['target'] = True
        try:
            return original_target(*args)
        finally:
            state['target'] = False
    def stopped_after_validation():
        current = current_scope()
        if state['target'] and not state['stopped']:
            assert current[1] is candidate
            state['stopped'] = True
            # Model the GUI's detach/restart boundary exactly after a worker
            # validates its child scope. The original old writer still owns
            # its Python paths/provider and must finish normally.
            controller._writer = None
            controller._capture_generation = object()
            controller.recorder.clear()
        return current
    monkeypatch.setattr(controller, '_current_log_scope', stopped_after_validation)
    monkeypatch.setattr(controller, target_name, targeted_scope)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(candidate._save, 'recording').result(timeout=10)
        assert state['stopped'] and controller.recorder.capture_epoch != old_epoch
        assert not candidate.error
        assert json.loads(candidate.json_path.read_text(encoding='utf-8'))['unicode'] == '漫画 🖊'
        assert candidate.summary_path.exists()
        assert not _rows(controller, 'monitor.log_')
    finally:
        controller._writer = candidate
        controller.stop()


def test_facade_delegates_stdlib_options_and_exceptions():
    facade = _LogJsonFacade(json)
    value = {'unicode': '漫画', 'number': float('nan'), 'tuple': (1, 2)}
    for options in ({}, {'ensure_ascii': False, 'indent': 2}, {'sort_keys': True}, {'separators': (',', ':')}):
        assert facade.dumps(value, **options) == json.dumps(value, **options)
    with pytest.raises(ValueError, match='Out of range float'):
        facade.dumps(value, allow_nan=False)
    assert facade.loads(facade.dumps({'a': 1})) == {'a': 1}


def _owned_dialog(editor, controller, qapp, monkeypatch):
    dialog = PerformanceMonitorDialog(controller, editor[0])
    monkeypatch.setattr(editor[0], '_performance_monitor_dialog', dialog, raising=False)
    dialog.show()
    qapp.processEvents()
    controller.start()
    controller._writer.stop()
    return dialog


def test_display_refresh_and_snapshot_are_owned_gui_scopes_without_recursive_sampling(
        editor, controller, qapp, monkeypatch):
    dialog = _owned_dialog(editor, controller, qapp, monkeypatch)
    gui = threading.get_ident()
    try:
        phases = _phases(controller)
        before = {name: phases[name]['count'] for name in
            ('monitor.display_refresh', 'monitor.display_snapshot')}
        dialog._refresh()
        phases = _phases(controller)
        assert all(phases[name]['count'] == count+1 for name, count in before.items())
        rows = _rows(controller, 'monitor.display_')
        assert {row['name'] for row in rows} == {'monitor.display_refresh',
            'monitor.display_refresh.thread_cpu', 'monitor.display_snapshot',
            'monitor.display_snapshot.thread_cpu'}
        assert all(row['details']['thread_id'] == gui and row['details']['thread'] == 'gui'
            and row['details']['dialog_id'] == id(dialog) and row['details']['tab_index'] == 0
            and row['details']['capture_epoch'] == controller.recorder.capture_epoch
            and row['duration_ms'] >= 0 for row in rows)
        assert {row['details']['scope'] for row in rows} == {
            'gui_monitor_display', 'gui_monitor_display_snapshot'}
        assert dialog._snapshot['schema_version'] == 1
        assert controller.recorder._bounds['max_events'] == 3000
        assert not any(row['name'].startswith('monitor.display_')
            for row in controller.snapshot()['critical_events'])
    finally:
        dialog.close()
        dialog.deleteLater()


def test_foreign_or_hidden_display_and_background_snapshot_do_not_borrow_owner_token(
        editor, controller, qapp, monkeypatch):
    dialog = _owned_dialog(editor, controller, qapp, monkeypatch)
    foreign = PerformanceMonitorDialog(controller, editor[0])
    foreign.show()
    original = controller.snapshot_for_display
    nested = []
    def snapshot(*args, **kwargs):
        if not nested:
            nested.append(True)
            foreign._refresh()
        return original(*args, **kwargs)
    # The dynamic method keeps the installed owned wrapper; no replacement
    # capture/encoder API is introduced by this test's nested foreign display.
    monkeypatch.setattr(controller, 'snapshot_for_display', snapshot)
    try:
        count = _phases(controller)['monitor.display_refresh']['count']
        dialog._refresh()
        assert nested and _phases(controller)['monitor.display_refresh']['count'] == count+1
        before = {name: _phases(controller)[name]['count'] for name in
            ('monitor.display_refresh', 'monitor.display_snapshot')}
        foreign._refresh()
        dialog.hide()
        dialog._refresh()
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(controller._monitor_display_scope, (dialog,), {}).result(timeout=10) is None
            assert pool.submit(controller.snapshot_for_display).result(timeout=10)['schema_version'] == 1
        assert all(_phases(controller)[name]['count'] == count for name, count in before.items())
    finally:
        controller.stop()
        foreign.close()
        dialog.close()
        foreign.deleteLater()
        dialog.deleteLater()


@pytest.mark.parametrize('boundary', ['clear', 'restart', 'closed'])
def test_display_registration_and_retained_wrapper_cannot_cross_epoch_or_close(
        editor, controller, qapp, monkeypatch, boundary):
    dialog = _owned_dialog(editor, controller, qapp, monkeypatch)
    old_wrapper = PerformanceMonitorDialog._refresh
    old_epoch = controller.recorder.capture_epoch
    try:
        if boundary == 'clear':
            controller.recorder.clear()
            dialog._refresh()
            assert not _rows(controller, 'monitor.display_')
        elif boundary == 'closed':
            dialog.close()
            count = _phases(controller)['monitor.display_refresh']['count']
            old_wrapper(dialog)
            assert not controller.enabled
            assert _phases(controller)['monitor.display_refresh']['count'] == count
        else:
            controller.stop()
            controller.start()
            controller._writer.stop()
            count = _phases(controller)['monitor.display_refresh']['count']
            old_wrapper(dialog)
            assert _phases(controller)['monitor.display_refresh']['count'] == count
            dialog._refresh()
            assert _phases(controller)['monitor.display_refresh']['count'] == count+1
            assert all(row['details']['run_generation'] == controller._run_serial
                for row in _rows(controller, 'monitor.display_'))
        assert controller.recorder.capture_epoch != old_epoch
    finally:
        controller.stop()
        dialog.close()
        dialog.deleteLater()
