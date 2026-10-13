"""Recording compaction preserves whole detached reports and final readability."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest

from comic_editor.core.performance_log import PerformanceLogWriter
from test_performance_log import _snapshot


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def test_real_recording_flush_is_compact_and_final_stop_pretty_with_exact_content(
        tmp_path, monkeypatch):
    gui = threading.get_ident()
    opaque_threads = []
    artwork = tmp_path / 'source-that-the-log-must-not-read.png'
    artwork.write_bytes(b'original private source bytes')
    original_open = Path.open

    def log_only_open(path, *args, **kwargs):
        assert path != artwork, 'Checkpoint writer read artwork'
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', log_only_open)

    class CachedLabel:
        def __str__(self):
            opaque_threads.append(threading.get_ident())
            return 'detached metadata'
        @property
        def pixels(self):
            raise AssertionError('Checkpoint encoder inspected source pixels')

    label = CachedLabel()
    snapshot = _snapshot()
    snapshot.update(unicode='漫画 🖊\n雪', cached_label=label,
                    cached_reference=artwork, nonfinite=[float('nan'), float('inf'), -float('inf')])
    expected = {**snapshot, 'cached_label': 'detached metadata', 'cached_reference': str(artwork)}
    writer = PerformanceLogWriter(lambda: snapshot, tmp_path / 'logs', interval_seconds=60.)
    try:
        writer.start()
        assert writer.flush()
        recording_text = writer.json_path.read_text(encoding='utf-8')
        recording = json.loads(recording_text)
        assert recording_text == json.dumps(recording, ensure_ascii=False, default=str) + '\n'
        assert '\n  "' not in recording_text and '漫画 🖊' in recording_text
        assert recording['status'] == recording['log']['status'] == 'recording'
        assert _canonical({key: recording[key] for key in snapshot}) == _canonical(expected)
        assert writer.running and not writer.error
    finally:
        writer.stop()
    final_text = writer.json_path.read_text(encoding='utf-8')
    final = json.loads(final_text)
    assert final_text == json.dumps(final, ensure_ascii=False, indent=2, default=str) + '\n'
    assert final_text.startswith('{\n  ') and final['status'] == final['log']['status'] == 'stopped'
    assert _canonical({key: final[key] for key in snapshot}) == _canonical(expected)
    assert final['log']['checkpoint_number'] > recording['log']['checkpoint_number']
    assert opaque_threads and set(opaque_threads) == {writer._thread.ident} and gui not in opaque_threads
    assert snapshot['cached_label'] is label and 'status' not in snapshot and 'log' not in snapshot
    assert not writer.running and not writer.error


@pytest.mark.parametrize('status', ['stopped', 'error'])
def test_nonrecording_checkpoint_keeps_standard_pretty_encoding(tmp_path, status):
    writer = PerformanceLogWriter(lambda: {'unicode': '雪'}, tmp_path)
    writer.directory, writer.json_path, writer.summary_path = tmp_path, tmp_path/'capture.json', tmp_path/'summary.md'
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(writer._save, status).result(timeout=10)
    value = writer.json_path.read_text(encoding='utf-8')
    assert not writer.error
    assert value == json.dumps(json.loads(value), ensure_ascii=False, indent=2, default=str) + '\n'
    assert json.loads(value)['status'] == status


@pytest.mark.parametrize('status', ['recording', 'stopped'])
def test_encoding_failure_preserves_complete_previous_checkpoint_and_recovers(tmp_path, status):
    class BadText:
        def __str__(self):
            raise ValueError('intentional detached encoding error')
    payload = _snapshot()
    writer = PerformanceLogWriter(lambda: payload, tmp_path)
    writer.directory, writer.json_path, writer.summary_path = tmp_path, tmp_path/'capture.json', tmp_path/'summary.md'
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(writer._save, 'recording').result(timeout=10)
        original = writer.json_path.read_bytes(), writer.summary_path.read_bytes()
        payload['bad'] = BadText()
        pool.submit(writer._save, status).result(timeout=10)
        assert writer.error == 'ValueError: intentional detached encoding error'
        assert (writer.json_path.read_bytes(), writer.summary_path.read_bytes()) == original
        assert {path.name for path in tmp_path.iterdir()} == {'capture.json', 'summary.md'}
        payload.pop('bad')
        pool.submit(writer._save, 'stopped').result(timeout=10)
    assert not writer.error
    assert writer.json_path.read_text(encoding='utf-8').startswith('{\n  ')
