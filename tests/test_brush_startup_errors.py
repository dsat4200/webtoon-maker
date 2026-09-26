"""Missing brush images must be explained before an editor can overwrite settings."""
import importlib
import json
import sys
from types import SimpleNamespace

import pytest

from comic_editor.core import settings as settings_module


class Signal:
    def connect(self, callback):
        self.callback = callback


class App:
    def __init__(self):
        self.aboutToQuit = Signal()

    def setApplicationName(self, value):
        pass

    def setOrganizationName(self, value):
        pass

    def setFont(self, value):
        pass

    def exec(self):
        pytest.fail('An editor event loop started after brush assets failed to load')


def damaged_preferences(tmp_path, monkeypatch, damage):
    path = tmp_path / 'preferences.json'
    digest = 'a' * 64
    original = json.dumps({'brush_presets': [{'tips': [{'png': {'$brush_png': digest}}]}]}).encode()
    path.write_bytes(original)
    if damage == 'corrupt':
        assets = tmp_path / 'brush-assets'
        assets.mkdir()
        (assets / (digest + '.png')).write_bytes(b'broken image')
    monkeypatch.setattr(settings_module, 'settings_path', lambda: path)
    monkeypatch.setattr(settings_module, 'save_settings',
                        lambda *args: pytest.fail('Startup saved over unavailable brush assets'))
    return path, original


def capture_dialog(module, monkeypatch):
    messages = []
    monkeypatch.setattr(module, 'QApplication', lambda args: App())
    monkeypatch.setattr(module.QMessageBox, 'critical',
                        lambda parent, title, message: messages.append((title, message)))
    return messages


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_editor_reports_asset_error_and_releases_broker(tmp_path, monkeypatch, damage):
    module = importlib.import_module('main')
    path, original = damaged_preferences(tmp_path, monkeypatch, damage)
    messages = capture_dialog(module, monkeypatch)
    closed = []
    broker = SimpleNamespace(start=lambda paths: True, files_requested=Signal(),
                             close=lambda: closed.append(True))
    monkeypatch.setattr(module, 'FileLaunchBroker', lambda app: broker)
    monkeypatch.setattr(sys, 'argv', ['main.py'])

    def window():
        settings_module.load_settings()
        pytest.fail('MainWindow continued after unavailable brush assets')

    monkeypatch.setitem(sys.modules, 'comic_editor.ui.main_window', SimpleNamespace(MainWindow=window))
    assert module.main() == 1
    assert closed == [True]
    assert len(messages) == 1
    assert messages[0][0] == 'Brush images unavailable'
    assert str(tmp_path / 'brush-assets') in messages[0][1]
    assert 'Preferences were not changed.' in messages[0][1]
    assert path.read_bytes() == original


@pytest.mark.parametrize('headless', [False, True])
def test_playground_reports_asset_error_without_editor_or_save(tmp_path, monkeypatch, capsys, headless):
    module = importlib.import_module('brush_playground')
    path, original = damaged_preferences(tmp_path, monkeypatch, 'missing')
    messages = capture_dialog(module, monkeypatch)
    monkeypatch.setattr(module, 'QFontDatabase', SimpleNamespace(addApplicationFont=lambda path: -1))
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'series.json').write_text('{}')
    (tmp_path / 'playground.json').write_text(json.dumps({'project': str(project)}))
    args = ['brush_playground.py', '--session', str(tmp_path)]
    if headless:
        args += ['--snapshot', str(tmp_path / 'snapshot.png')]
    monkeypatch.setattr(sys, 'argv', args)

    def window(folder, data):
        settings_module.load_settings()
        pytest.fail('Playground continued after unavailable brush assets')

    monkeypatch.setattr(module, 'create_window', window)
    assert module.main() == 1
    if headless:
        assert messages == []
        assert 'Restore the brush-assets folder' in capsys.readouterr().err
        assert not (tmp_path / 'snapshot.png').exists()
    else:
        assert len(messages) == 1
        assert messages[0][0] == 'Brush images unavailable'
        assert 'Restore the brush-assets folder' in messages[0][1]
    assert path.read_bytes() == original
