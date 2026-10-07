"""Known earlier renderer namespaces remain read-only misses."""
import hashlib

import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.render import cache as storage


def files(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('kind,key', [
    ('effect', ('translated-exact-output', 'parent', 'ordinary-semantic-source')),
    ('projection', ('chapter', 0, 0, 0, 256, 2, 'correct-hidden-scene-dependency')),
    ('source', ('layer-source', 'parent', 'ordinary-semantic-source')),
])
@pytest.mark.parametrize('old_renderer', [
    'native-artwork-3', 'native-artwork-4', 'native-artwork-20261007-visibility-source-1',
])
def test_prior_visibility_renderer_entries_are_read_only_misses(tmp_path, monkeypatch, kind, key, old_renderer):
    assert storage.RENDERER_VERSION == 'native-artwork-20261007-source-context-2'
    image = QImage(12, 11, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#cf407c'))
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'RENDERER_VERSION', old_renderer)
        old = storage.PersistentRenderCache(tmp_path)
        with old.record():
            assert old.retain(kind, key, image)
        old.drain()
        identity = old.descriptor(kind, key).identity
        assert old.lookup(kind, key, wait=True) is not None
        old.close()
    preserved = files(tmp_path)
    current = storage.PersistentRenderCache(tmp_path)
    try:
        assert current.descriptor(kind, key).identity != identity
        assert not current.entries and not current.can_lookup
        assert current.lookup(kind, key, wait=True) is None
        assert not current.has(kind, key)
        current.poll()
        current.drain()
    finally:
        current.close()
    assert files(tmp_path) == preserved


@pytest.mark.parametrize('old_renderer', [
    'native-artwork-3', 'native-artwork-4', 'native-artwork-20261007-visibility-source-1',
])
def test_correct_current_renderer_recording_reuses_ordinary_pipeline(tmp_path, monkeypatch, old_renderer):
    assert storage.RENDERER_VERSION == 'native-artwork-20261007-source-context-2'
    key = ('chapter', 0, 0, 0, 256, 2, 'correct-hidden-scene-dependency')
    old = QImage(12, 11, QImage.Format_ARGB32_Premultiplied)
    old.fill(QColor('#cf407c'))
    correct = QImage(old.size(), old.format())
    correct.fill(QColor('white'))
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'RENDERER_VERSION', old_renderer)
        backing = storage.PersistentRenderCache(tmp_path)
        with backing.record():
            assert backing.retain('projection', key, old)
        backing.close()
    current = storage.PersistentRenderCache(tmp_path)
    try:
        assert current.lookup('projection', key, wait=True) is None
        with current.record():
            assert current.retain('projection', key, correct)
        current.drain()
        assert bytes(current.lookup('projection', key, wait=True).constBits()) == bytes(correct.constBits())
    finally:
        current.close()
    reopened = storage.PersistentRenderCache(tmp_path)
    try:
        assert bytes(reopened.lookup('projection', key, wait=True).constBits()) == bytes(correct.constBits())
    finally:
        reopened.close()
