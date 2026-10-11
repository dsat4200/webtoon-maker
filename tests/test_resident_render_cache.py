"""Shared memory retains the existing native semantic values and checkpoints."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QRectF
from PySide6.QtGui import QColor, QColorSpace, QImage

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.cache import CacheDescriptor, PersistentRenderCache, restore_state
from comic_editor.render.resident_cache import ResidentRenderCache
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.ui.cache_dependencies import RenderDependencies, cache_put, exact_cache_allowed
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.color_resources import semantic_color_identity


@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied, QImage.Format_RGBA16FPx4_Premultiplied,
    QImage.Format_RGBA32FPx4_Premultiplied])
def test_ram_preserves_native_pixel_bits_format_and_color_metadata(image_format):
    owner = ResidentRenderCache(1024 * 1024)
    view = owner.view('chapter')
    descriptor = CacheDescriptor('source', ('native-original',), ('original-format',), ('color-environment',))
    image = QImage(17, 11, image_format)
    image.fill(QColor(80, 130, 220, 110))
    image.setColorSpace(QColorSpace(QColorSpace.SRgb))
    image.setOffset(QPoint(-13, 7))
    image.setDevicePixelRatio(1.5)
    image.bits()[4:12] = b'\x01\x02\x03\x04\x05\x06\x07\x08'
    before = bytes(image.constBits())
    assert view.retain(descriptor, image)
    image.fill(QColor('blue'))
    restored = view.lookup(descriptor)
    assert restored.format() == image_format and restored.colorSpace() == image.colorSpace()
    assert restored.offset() == QPoint(-13, 7) and restored.devicePixelRatio() == 1.5
    assert bytes(restored.constBits()) == before
    restored.fill(QColor('red'))
    assert bytes(view.lookup(descriptor).constBits()) == before  # Consumer edits detach COW storage.


@pytest.mark.parametrize('dtype', [np.float32, np.float64])
def test_ram_preserves_float_working_bits_and_owned_checkpoint_placement(dtype):
    owner = ResidentRenderCache(1024)
    view = owner.view('chapter')
    descriptor = CacheDescriptor('effect', ('native-stage',))
    pixels = np.array([-0., -.01, 1.7, np.inf, np.nan, .33333333333], dtype=dtype).reshape(1, 2, 3)
    before = pixels.tobytes()
    bounds = QRectF(-11.25, 7.5, 120.75, 88.125)
    state = [3, bounds]
    assert view.retain(descriptor, pixels, state)
    pixels.fill(0.)
    bounds.translate(1000, 1000)
    state[0] = 8
    assert view.lookup(descriptor).tobytes() == before
    assert not view.lookup(descriptor).flags.writeable
    restored = restore_state(view.state(descriptor))
    assert restored == (3, QRectF(-11.25, 7.5, 120.75, 88.125))
    metadata = view.state(descriptor)
    metadata[0] = 15
    assert restore_state(view.state(descriptor))[0] == 3


def test_all_chapters_share_one_bounded_lru_and_reject_oversized_values():
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    owner = ResidentRenderCache(2 * image.sizeInBytes())
    first, second = owner.view('one'), owner.view('two')
    descriptors = [CacheDescriptor('projection', ('tile', number)) for number in range(3)]
    assert first.retain(descriptors[0], image)
    assert second.retain(descriptors[1], image)
    assert first.lookup(descriptors[0]) is not None
    assert first.retain(descriptors[2], image)
    assert first.has(descriptors[0]) and first.has(descriptors[2])
    assert not second.has(descriptors[1]) and owner.evictions == 1
    oversized = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
    oversized.fill(QColor('blue'))
    assert not first.retain(CacheDescriptor('projection', ('huge',)), oversized)
    assert not first.retain(CacheDescriptor('source', ('empty',)), QImage())
    assert not first.retain(CacheDescriptor('effect', ('object-array',)), np.array([object()], dtype=object))
    assert owner.bytes <= owner.budget
    first.clear(lambda entry: entry.descriptor == descriptors[0])
    assert not first.has(descriptors[0]) and first.has(descriptors[2])
    assert owner.bytes == image.sizeInBytes()


def test_ram_uses_the_existing_typed_descriptor_identity_and_source_dependencies():
    view = ResidentRenderCache(4096).view('chapter')
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    source = CacheDescriptor('source', ('source', 1), ('uint8',), ('profile-v1',))
    assert view.retain(source, image)
    for mismatch in (replace(source, key=('source', True)), replace(source, key=('source', 1.)),
                     replace(source, key=('source', 2)), replace(source, contract=('uint16',)),
                     replace(source, environment=('profile-v2',))):
        assert view.lookup(mismatch) is None
    assert view.lookup(source) is not None


def test_memory_hits_write_nothing_until_explicit_recording_and_reopen_losslessly(tmp_path, monkeypatch):
    view = ResidentRenderCache(4096).view('chapter')
    cache = PersistentRenderCache(tmp_path, contract=('native',), environment=('profile',), resident=view)
    image = QImage(8, 8, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor(90, 170, 220, 100))
    image.bits()[:8] = b'\x01\x00\x02\x00\x03\x00\x04\x00'
    key, state = ('native-effect',), (2, QRectF(-3.5, 7.25, 8, 8))
    expected = bytes(image.constBits())
    try:
        cache.retain('retained', key, image, state=state)
        assert not cache.entries and not cache.writes and not list(tmp_path.iterdir())
        # A resident hit must not stat, hash, decode or touch an IO worker.
        monkeypatch.setattr(cache, '_entry', lambda *_: pytest.fail('Resident hit read a disk entry'))
        assert bytes(cache.lookup('retained', key).constBits()) == expected
        assert restore_state(cache.retained_state('retained', key)) == state
        with cache.record():
            cache.retain('retained', key, cache.lookup('retained', key), state=state)
        cache.drain()
        assert cache.entries and (tmp_path / 'index.json').is_file()
    finally:
        cache.close()
    reloaded = PersistentRenderCache(tmp_path, contract=('native',), environment=('profile',))
    try:
        restored = reloaded.lookup('retained', key, wait=True)
        assert restored.format() == QImage.Format_RGBA64_Premultiplied
        assert bytes(restored.constBits()) == expected
        assert restore_state(reloaded.retained_state('retained', key)) == state
    finally:
        reloaded.close()


@pytest.fixture
def memory_canvas(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    chapter = ChapterDocument(width=64, height=64, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 64, 64))
    chapter.add_object(page.layer_id, RasterObject())
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, TileStore())
    view = ResidentRenderCache(1024 * 1024).view('chapter')
    backing = PersistentRenderCache(tmp_path, contract=chapter.pixel_contract.signature,
        environment=(*RenderDependencies.environment(), semantic_color_identity(canvas, chapter.pixel_contract)),
        resident=view)
    canvas._persistent_render_cache = backing
    canvas._projection_exact = True
    try:
        yield canvas, view, backing
    finally:
        canvas._persistent_render_cache = None
        backing.close()
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()


@pytest.mark.parametrize('detached', [False, True], ids=['gui-checkpoint', 'worker-checkpoint'])
def test_memory_checkpoint_restores_native_image_and_effect_placement(memory_canvas, detached):
    canvas, view, backing = memory_canvas
    backend = None
    if detached:
        capture = canvas._scene_snapshot_compiler.capture(canvas, canvas._render_document_state())
        while not capture.advance(.001):
            pass
        snapshot = replace(capture.result, cache_spec=(backing.root, backing.contract, backing.environment, view))
        backend = DetachedSceneBackend(snapshot)
        canvas = backend.scene
        canvas._projection_exact = True
    scope, key = ('outline-stack', 'drawing'), ('stage-stack', 'drawing', 'source', 3)
    image = QImage(13, 11, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor(20, 30, 80, 120))
    state = (3, QRectF(-3.25, 12.75, 13, 11))
    try:
        cache_put(canvas, 'retained', ('retained', scope, key), image, state=state)
        assert not canvas._persistent_render_cache.entries
        restored = canvas._effect_jobs.retained_get(scope, key)
        assert restored is not None
        assert bytes(restored[0].constBits()) == bytes(image.constBits())
        assert restored[0].format() == image.format() and restored[1] == state
    finally:
        if backend is not None:
            backend.close()


@pytest.mark.parametrize('unsafe', ['live', 'navigator', 'draft-key', 'nonexact'])
def test_existing_exact_gates_exclude_all_private_preview_values(memory_canvas, unsafe):
    canvas, view, backing = memory_canvas
    key = ('effect', 'original')
    if unsafe == 'live':
        canvas._drawing = True
    elif unsafe == 'navigator':
        canvas._effect_preview_channel = 'navigator'
    elif unsafe == 'draft-key':
        key = ('cage-tool-draft', 'drawing')
    else:
        canvas._projection_exact = False
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    assert not exact_cache_allowed(canvas, key)
    cache_put(canvas, 'effect', key, image)
    assert not view.snapshot() and not backing.entries and not backing.writes
    canvas._drawing = False


def test_scene_capture_keeps_legacy_cache_spec_and_shares_only_explicit_resident(memory_canvas):
    canvas, view, backing = memory_canvas
    for binding in (SimpleNamespace(root=backing.root, contract=(), environment=(), closed=False),
                    SimpleNamespace(root=backing.root, contract=(), environment=(), closed=False, resident=None),
                    backing):
        canvas._persistent_render_cache = binding
        capture = canvas._scene_snapshot_compiler.capture(canvas, canvas._render_document_state())
        while not capture.advance(.001):
            pass
        spec = capture.result.cache_spec
        assert len(spec) == (4 if binding is backing else 3)
        assert spec[:3] == (str(binding.root), binding.contract, binding.environment)
        if binding is backing:
            assert spec[3] is view
    canvas._persistent_render_cache = backing
