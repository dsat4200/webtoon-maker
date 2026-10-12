"""Workspace saves own small packets and preserve ordinary brush preferences."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from comic_editor.core import brush_storage, settings as settings_module
import comic_editor.core.workspace_preferences as preferences_io
from comic_editor.core.settings import EditorSettings, load_settings, save_settings
from comic_editor.core.viewport_state import MAX_SAVED_VIEWPORTS
from comic_editor.core.workspace_preferences import WorkspacePreferencesWriter, workspace_snapshot
from comic_editor.ui.main_window import MainWindow
from test_brush_storage import library


def _view(center):
    return dict(center_x=float(center), center_y=20., scale=.63, rotation=-.663)


def test_workspace_handoff_does_not_clamp_or_visit_brushes_and_preserves_encoded_fields(
        tmp_path, monkeypatch):
    path = tmp_path / 'preferences.json'
    monkeypatch.setattr(settings_module, 'settings_path', lambda: path)
    settings = EditorSettings(brush_presets=[library().to_dict()])
    save_settings(settings)
    before = json.loads(path.read_text())
    before['future_preserved_field'] = {'native': [1, 2, 3]}
    path.write_text(json.dumps(before), encoding='utf-8')
    assets = {item: item.read_bytes() for item in (tmp_path / 'brush-assets').glob('*.png')}
    settings.document_viewports = {'document': _view(55)}
    monkeypatch.setattr(settings, 'clamp', lambda: pytest.fail('GUI traversed the brush library'))
    monkeypatch.setattr(brush_storage, 'encode_brush_assets',
                        lambda *_a, **_k: pytest.fail('Workspace traversed brush assets'))
    writer = WorkspacePreferencesWriter()
    entered, release = Event(), Event()
    owner_thread = get_ident()
    write = preferences_io._write_workspace
    def detached(*args):
        assert get_ident() != owner_thread
        entered.set()
        assert release.wait(5.)
        return write(*args)
    monkeypatch.setattr(preferences_io, '_write_workspace', detached)
    splitter = SimpleNamespace(sizes=lambda: [123, 456])
    window = SimpleNamespace(settings=settings, _workspace_preferences=writer,
        workspace_splitter=splitter, sidebar_splitter=splitter,
        tool_canvas_splitter=splitter, outliner_splitter=splitter,
        navigator_panel=SimpleNamespace(isExpanded=lambda: True))
    window._capture_workspace_layout = lambda: MainWindow._capture_workspace_layout(window)
    try:
        MainWindow._save_workspace_layout(window)
        assert entered.wait(3.), 'GUI handoff failed to start detached IO'
        # The saved packet must not borrow the live mutable camera dictionary.
        settings.document_viewports['document']['center_x'] = 99.
    finally:
        release.set()
        writer.shutdown()
    after = json.loads(path.read_text())
    assert after['document_viewports'] == {'document': _view(55)}
    assert after['navigator_expanded'] is True
    assert all(values == [123, 456] for values in after['ui_splitter_sizes'].values())
    for key, value in before.items():
        if key not in {'document_viewports', 'navigator_expanded', 'ui_splitter_sizes'}:
            assert after[key] == value
    assert all(item.read_bytes() == data for item, data in assets.items())


def test_workspace_coalesces_rapid_updates_and_shutdown_drains_latest(tmp_path, monkeypatch):
    path = tmp_path / 'preferences.json'
    path.write_text('{}', encoding='utf-8')
    settings = EditorSettings()
    entered, release = Event(), Event()
    writes = []
    write = preferences_io._write_workspace
    def held(path, packet, *args):
        writes.append(packet['document_viewports']['document']['center_x'])
        if len(writes) == 1:
            entered.set()
            assert release.wait(5.)
        return write(path, packet, *args)
    monkeypatch.setattr(preferences_io, '_write_workspace', held)
    writer = WorkspacePreferencesWriter()
    try:
        settings.document_viewports = {'document': _view(1)}
        writer.submit(path, settings)
        assert entered.wait(3.)
        for value in range(2, 10):
            settings.document_viewports = {'document': _view(value)}
            writer.submit(path, settings)
    finally:
        release.set()
        writer.shutdown()
    assert writes == [1., 9.]
    assert json.loads(path.read_text())['document_viewports'] == {'document': _view(9)}


def test_new_full_save_supersedes_old_queued_workspace_packet(tmp_path, monkeypatch):
    path = tmp_path / 'preferences.json'
    monkeypatch.setattr(settings_module, 'settings_path', lambda: path)
    settings = EditorSettings(brush_presets=[library().to_dict()])
    save_settings(settings)
    entered, release = Event(), Event()
    write = preferences_io._write_workspace
    def held(*args):
        entered.set()
        assert release.wait(5.)
        return write(*args)
    monkeypatch.setattr(preferences_io, '_write_workspace', held)
    writer = WorkspacePreferencesWriter()
    try:
        settings.document_viewports = {'document': _view(1)}
        writer.submit(path, settings)
        assert entered.wait(3.)
        settings.document_viewports = {'document': _view(2)}
        settings.brush_presets[0]['name'] = 'Latest brush edit'
        save_settings(settings)
    finally:
        release.set()
        writer.shutdown()
    loaded = load_settings()
    assert loaded.document_viewports == {'document': _view(2)}
    assert loaded.brush_presets == settings.brush_presets


def test_full_settings_save_cannot_interleave_a_workspace_atomic_merge(tmp_path, monkeypatch):
    path = tmp_path / 'preferences.json'
    monkeypatch.setattr(settings_module, 'settings_path', lambda: path)
    settings = EditorSettings(brush_presets=[library().to_dict()])
    save_settings(settings)
    entered, release, full_started, encoding = Event(), Event(), Event(), Event()
    atomic = brush_storage.atomic_write
    gui = get_ident()
    def held(target, write):
        if target == path and get_ident() != gui and not full_started.is_set():
            entered.set()
            assert release.wait(5.)
        return atomic(target, write)
    monkeypatch.setattr(brush_storage, 'atomic_write', held)
    encode = brush_storage.encode_brush_assets
    def encoded(*args):
        encoding.set()
        return encode(*args)
    monkeypatch.setattr(brush_storage, 'encode_brush_assets', encoded)
    writer = WorkspacePreferencesWriter()
    try:
        settings.document_viewports = {'document': _view(1)}
        writer.submit(path, settings)
        assert entered.wait(3.)
        settings.document_viewports = {'document': _view(2)}
        settings.active_pencil_size = 'large'
        def full_save():
            full_started.set()
            save_settings(settings)
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(full_save)
            assert full_started.wait(3.)
            assert not encoding.is_set(), 'Full save entered the live merge publication scope'
            release.set()
            future.result(timeout=5.)
    finally:
        release.set()
        writer.shutdown()
    loaded = load_settings()
    assert loaded.document_viewports == {'document': _view(2)}
    assert loaded.active_pencil_size == 'large'
    assert loaded.brush_presets == settings.brush_presets


def test_workspace_normalization_is_bounded_and_failed_write_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / 'preferences.json'
    original = b'{"brush_presets": [{"png": "original bytes"}], "unchanged": 7}'
    path.write_bytes(original)
    settings = EditorSettings()
    settings.ui_splitter_sizes = {'tool_canvas': [-10, 200_000], 'bad': [1, 2]}
    settings.document_viewports = {str(index): _view(index) for index in range(MAX_SAVED_VIEWPORTS + 3)}
    snapshot = workspace_snapshot(settings)
    assert snapshot['ui_splitter_sizes'] == {'tool_canvas': [0, 100_000]}
    assert len(snapshot['document_viewports']) == MAX_SAVED_VIEWPORTS
    assert '0' not in snapshot['document_viewports']
    monkeypatch.setattr(brush_storage, 'atomic_write',
                        lambda *_a: (_ for _ in ()).throw(OSError('injected preferences failure')))
    writer = WorkspacePreferencesWriter()
    writer.submit(path, settings)
    with pytest.raises(OSError, match='injected preferences failure'):
        writer.shutdown()
    assert path.read_bytes() == original


@pytest.mark.parametrize('baseline', ['save', 'load'])
@pytest.mark.parametrize('damage', [None, b'{broken', b'[]', b'\xff'])
def test_workspace_repairs_missing_or_invalid_file_from_complete_encoding(
        tmp_path, monkeypatch, baseline, damage):
    path = tmp_path / 'preferences.json'
    monkeypatch.setattr(settings_module, 'settings_path', lambda: path)
    settings = EditorSettings(brush_presets=[library().to_dict()])
    settings.brush_color = '#123456'
    settings.active_pencil_size = 'large'
    save_settings(settings)
    if baseline == 'load':
        # Simulate an existing preferences file first loaded in this process.
        # Unknown future fields must survive the validated encoded backup too.
        encoded = json.loads(path.read_text())
        encoded['future_preserved_field'] = {'values': [2, 3, 5]}
        path.write_text(json.dumps(encoded), encoding='utf-8')
        preferences_io._owner(path).encoded = None
        settings = load_settings()
    before = json.loads(path.read_text())
    assets = {asset: asset.read_bytes() for asset in (tmp_path / 'brush-assets').glob('*.png')}
    if damage is None:
        path.unlink()
    else:
        path.write_bytes(damage)
    settings.document_viewports = {'document': _view(345)}
    monkeypatch.setattr(settings, 'clamp', lambda: pytest.fail('Repair visited live settings'))
    monkeypatch.setattr(brush_storage, 'encode_brush_assets',
                        lambda *_a, **_k: pytest.fail('Repair traversed live brush assets'))
    monkeypatch.setattr(brush_storage, 'decode_brush_assets',
                        lambda *_a, **_k: pytest.fail('Repair decoded brush assets'))
    writer = WorkspacePreferencesWriter()
    writer.submit(path, settings)
    writer.shutdown()
    after = json.loads(path.read_text())
    assert after['document_viewports'] == {'document': _view(345)}
    for key, value in before.items():
        if key not in {'document_viewports', 'navigator_expanded', 'ui_splitter_sizes'}:
            assert after[key] == value
    assert all(asset.read_bytes() == data for asset, data in assets.items())


@pytest.mark.parametrize('damage', [None, b'{broken'])
def test_workspace_cannot_publish_partial_defaults_without_known_encoding(
        tmp_path, damage):
    path = tmp_path / 'preferences.json'
    if damage is not None:
        path.write_bytes(damage)
    writer = WorkspacePreferencesWriter()
    writer.submit(path, EditorSettings())
    with pytest.raises((OSError, ValueError)):
        writer.shutdown()
    if damage is None:
        assert not path.exists()
    else:
        assert path.read_bytes() == damage


def test_settings_read_waits_for_atomic_workspace_publication(tmp_path, monkeypatch):
    path = tmp_path / 'preferences.json'
    monkeypatch.setattr(settings_module, 'settings_path', lambda: path)
    settings = EditorSettings()
    save_settings(settings)
    entered, release, reading = Event(), Event(), Event()
    atomic = brush_storage.atomic_write
    def held(target, write):
        entered.set()
        assert release.wait(5.)
        return atomic(target, write)
    monkeypatch.setattr(brush_storage, 'atomic_write', held)
    writer = WorkspacePreferencesWriter()
    settings.document_viewports = {'document': _view(77)}
    try:
        writer.submit(path, settings)
        assert entered.wait(3.)
        def read():
            reading.set()
            return load_settings()
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(read)
            assert reading.wait(3.)
            assert not future.done(), 'Settings read escaped the publication lock'
            release.set()
            loaded = future.result(timeout=5.)
    finally:
        release.set()
        writer.shutdown()
    assert loaded.document_viewports == {'document': _view(77)}
