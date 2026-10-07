"""Cold external color resources never become GUI capture/paint work."""
from copy import deepcopy
from dataclasses import replace
import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import BoundGeometry, ChapterDocument
from comic_editor.core.pixel_contract import FLOAT_PIXELS
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render import pixels
from comic_editor.render.scene import SceneSnapshotCompiler
from comic_editor.render.service import RenderPending
from comic_editor.ui.cache_dependencies import RenderDependencies
from comic_editor.ui.canvas import CanvasWidget


def cube(path, scale):
    path.write_text(f'LUT_1D_SIZE 2\n0 0 0\n{scale} {scale} {scale}\n', encoding='ascii')


def custom_contract(tmp_path):
    import PyOpenColorIO as ocio
    lut, path = tmp_path / 'curve.cube', tmp_path / 'config.ocio'
    cube(lut, .5)
    config = deepcopy(pixels.color_config())
    space = config.getColorSpace('srgb')
    space.setTransform(ocio.FileTransform(src=lut.name), ocio.COLORSPACE_DIR_TO_REFERENCE)
    config.addColorSpace(space)
    path.write_text(config.serialize(), encoding='utf8')
    return replace(FLOAT_PIXELS, working_space='linear_srgb', ocio_config=str(path)), lut, path


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    owner = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    owner.resize(64, 64)
    yield owner
    resources = getattr(owner, '_color_resources', None)
    if resources is not None:
        resources.shutdown()
    owner._scene_controller.reset()
    owner._scene_controller.scheduler.close()
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.deleteLater()


def bind(canvas, contract):
    chapter = ChapterDocument(width=64, height=64, document_kind='asset', pixel_contract=contract)
    chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 64, 64))
    canvas.set_document(chapter, TileStore())
    return chapter


def wait_for(qapp, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.002)
    pytest.fail('Detached color ownership did not reach the expected state')


def freeze(canvas, qapp):
    capture = SceneSnapshotCompiler().capture(canvas, canvas._render_document_state())
    wait_for(qapp, lambda: capture.advance(.001))
    assert not capture.stale
    return capture.result


def test_blocked_config_loader_does_not_block_gui_paint_capture_or_cache_metadata(canvas, qapp, tmp_path, monkeypatch):
    contract, _, _ = custom_contract(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = pixels._file_color_config
    original_identity = pixels._file_identity
    calls = []
    identities = []
    def blocked(*args):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(pixels, '_file_color_config', blocked)
    gui = threading.get_ident()
    def identity(path):
        identities.append(threading.get_ident())
        assert threading.get_ident() != gui, 'Color resources were hashed on the GUI'
        return original_identity(path)
    monkeypatch.setattr(pixels, '_file_identity', identity)
    try:
        bind(canvas, contract)
        document = canvas._render_document_state()
        assert document.configuration[-1][2][0] == 'async-color'
        assert entered.wait(2)
        image = QImage(64, 64, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('white'))
        painter = QPainter(image)
        try:
            canvas._paint_ready_document_projection(painter)
        finally:
            painter.end()
        document = canvas._render_document_state()
        capture = SceneSnapshotCompiler().capture(canvas, document)
        assert not capture.advance(.002) and capture.result is None
        request = canvas._document_projection.requests(QRectF(0, 0, 64, 64), 1.)[0]
        dependencies = RenderDependencies(canvas, None)
        with pytest.raises(RenderPending):
            dependencies.projection_key(request, (*document.configuration, None))
        assert calls == [calls[0]] and calls[0] != gui
        assert identities and gui not in identities
        assert canvas._scene_controller.snapshot is None
        release.set()
        wait_for(qapp, lambda: capture.advance(.001))
        assert not capture.stale and capture.result.pixel_environment.config is not None
        assert capture.result.document.configuration == document.configuration
        assert canvas._projection_configuration() == document.configuration
    finally:
        release.set()


def test_lut_change_during_preparation_retries_and_old_captured_native_environment_survives(canvas, qapp, tmp_path, monkeypatch):
    contract, lut, path = custom_contract(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = pixels._capture_external_config
    calls = []
    def blocked(signature, *args):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            assert release.wait(10)
        return original(signature, *args)
    monkeypatch.setattr(pixels, '_capture_external_config', blocked)
    samples = np.array([[[.500049, .250049, .125049, .400049]]], np.float32)
    try:
        bind(canvas, contract)
        document = canvas._render_document_state()
        assert entered.wait(2)
        cube(lut, .75)
        release.set()
        captured = freeze(canvas, qapp)
        assert len(calls) == 2 and all(owner != threading.get_ident() for owner in calls)
        assert captured.document.configuration == document.configuration
        with pixels.pixel_scope(contract, environment=captured.pixel_environment):
            result = pixels.transform_pixels(samples, 'srgb', 'linear_srgb', contract=contract,
                                             premultiplied=False)
        np.testing.assert_allclose(result[..., :3], samples[..., :3] * .75, rtol=1e-6)
        np.testing.assert_array_equal(result[..., 3], samples[..., 3])
        before = captured.pixel_environment
        cube(lut, 1.)
        resources = canvas._color_resources
        resources.checked = 0.
        resources.poll()
        wait_for(qapp, lambda: resources.environment.signature != before.signature)
        assert canvas._projection_configuration() != document.configuration
        lut.unlink()
        path.unlink()
        with pixels.pixel_scope(contract, environment=before):
            np.testing.assert_array_equal(pixels.transform_pixels(samples, 'srgb', 'linear_srgb',
                contract=contract, premultiplied=False), result)
    finally:
        release.set()


def test_document_switch_does_not_adopt_old_color_preparation(canvas, qapp, tmp_path, monkeypatch):
    contract, _, _ = custom_contract(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = pixels.capture_color_environment
    def blocked(policy):
        if policy.ocio_config:
            entered.set()
            assert release.wait(10)
        return original(policy)
    monkeypatch.setattr(pixels, 'capture_color_environment', blocked)
    try:
        old = bind(canvas, contract)
        old_document = canvas._render_document_state()
        capture = SceneSnapshotCompiler().capture(canvas, old_document)
        assert entered.wait(2)
        new = bind(canvas, FLOAT_PIXELS)
        release.set()
        assert capture.advance(.001) and capture.stale and capture.result is None
        snapshot = freeze(canvas, qapp)
        assert snapshot.chapter.chapter_id == new.chapter_id != old.chapter_id
        assert snapshot.pixel_environment.signature == ('builtin-srgb-v1',)
        wait_for(qapp, lambda: not canvas._color_resources.timer.isActive())
        assert canvas._projection_configuration()[-1][2] == ('builtin-srgb-v1',)
    finally:
        release.set()


def test_invalid_config_has_class_diagnostic_then_recovers_with_semantic_disk_keys(canvas, qapp, tmp_path):
    contract, _, path = custom_contract(tmp_path)
    valid = path.read_text(encoding='utf8')
    path.write_text('not a config', encoding='utf8')
    bind(canvas, contract)
    document = canvas._render_document_state()
    requests = canvas._document_projection.requests(QRectF(0, 0, 64, 64), 1.)
    controller = canvas._scene_controller
    controller.request(document, requests, (None,), QRectF(0, 0, 64, 64))
    wait_for(qapp, lambda: bool(controller.error))
    assert 'Exception:' in controller.error or 'ValueError:' in controller.error
    assert controller.snapshot is None
    dependencies = RenderDependencies(canvas, None)
    with pytest.raises(RenderPending):
        dependencies.projection_key(requests[0], (*document.configuration, None))
    path.write_text(valid, encoding='utf8')
    canvas._color_resources.checked = 0.
    canvas._color_resources.poll()
    wait_for(qapp, lambda: canvas._color_resources.error is None)
    snapshot = freeze(canvas, qapp)
    with pixels.pixel_scope(contract, environment=snapshot.pixel_environment):
        first = dependencies.projection_key(requests[0], (*snapshot.document.configuration, None))
        configuration = list(snapshot.document.configuration)
        configuration[-1] = ('pixel-environment', contract.signature, ('async-color', 999))
        second = dependencies.projection_key(requests[0], (*configuration, None))
    assert first == second  # Runtime ownership tickets never enter durable keys.


def test_navigator_color_capture_error_is_terminal_and_corrected_resources_retry(canvas, qapp, tmp_path):
    from comic_editor.ui.preview import ChapterPreview
    contract, _, path = custom_contract(tmp_path)
    valid = path.read_text(encoding='utf8')
    path.write_text('not a config', encoding='utf8')
    bind(canvas, contract)
    preview = ChapterPreview(canvas)
    preview.resize(92, 200)
    preview.show()
    jobs = preview._navigator_jobs
    try:
        jobs.request()
        wait_for(qapp, lambda: bool(jobs.error))
        assert 'Exception:' in jobs.error or 'ValueError:' in jobs.error
        assert jobs.capture is None and not jobs.timer.isActive()
        error, document = jobs.error, jobs.document
        jobs.request()
        assert jobs.error == error and jobs.document == document and not jobs.timer.isActive()
        resources = canvas._color_resources
        resources.checked = 0.
        resources.poll()
        wait_for(qapp, lambda: resources.future is None)
        # Repeated validation of the same failure must not restart a Qt timer.
        assert jobs.error == error and not jobs.timer.isActive()
        path.write_text(valid, encoding='utf8')
        resources.checked = 0.
        resources.poll()
        wait_for(qapp, lambda: resources.error is None)
        jobs.request()
        assert not jobs.error and jobs.document != document
        wait_for(qapp, lambda: jobs.sent and not jobs.scheduler.busy)
        assert not preview._cache.isNull()
    finally:
        jobs.cancel()
        jobs.scheduler.close()
        preview.hide()
        preview.deleteLater()


@pytest.mark.parametrize('invalid', [False, True])
def test_cold_disk_binding_waits_for_environment_and_records_only_semantic_native_entries(canvas, qapp, tmp_path, monkeypatch, invalid):
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.ui.main_window import MainWindow
    contract, _, path = custom_contract(tmp_path)
    repository = SeriesRepository(tmp_path / 'project')
    series = repository.create('Color cache')
    chapter, tiles = repository.create_chapter(series, 'Chapter')
    chapter.height = 64
    chapter.pixel_contract = contract
    repository.save_chapter(chapter, tiles)
    if invalid:
        path.write_text('not a config', encoding='utf8')
    entered, release = threading.Event(), threading.Event()
    original = pixels._file_color_config
    gui = threading.get_ident()
    def blocked(*args):
        assert threading.get_ident() != gui, 'Cold disk binding loaded OCIO on the GUI'
        entered.set()
        assert release.wait(10)
        return original(*args)
    monkeypatch.setattr(pixels, '_file_color_config', blocked)
    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        cache = window.disk_cache
        assert entered.wait(2)
        assert cache.backing is not None and not cache.row_ready(0)
        assert cache.start()
        cache.tick()
        assert cache.building and cache._capture is not None
        release.set()
        def complete():
            cache.tick()
            return not cache.building and cache._maintenance is None
        wait_for(qapp, complete)
        if invalid:
            assert not cache.canvas.document_read_only and not cache.canvas.command_stack.read_only
            assert 'Exception:' in cache.backing.error or 'ValueError:' in cache.backing.error
            assert cache.message.startswith('Cache failed: ')
            assert not cache.row_ready(0) and not cache.backing.entries
            return
        assert not cache.backing.error, cache.message
        assert cache.row_ready(0)
        actual = window.canvas._color_resources.environment.signature
        assert cache.backing.environment[-1] == actual
        assert cache.backing.entries
        assert 'async-color' not in repr(cache.backing.entries)
        assert 'color-resources-pending' not in repr(cache.backing.entries)
        with cache.capture():
            configuration = (*window.canvas._projection_configuration(), None)
            request = cache.requests(0)[0]
            key = cache.tile_key(request, configuration)
            native = cache.backing.lookup('projection', key, wait=True)
        assert native.format() == contract.image_format
    finally:
        release.set()
        resources = getattr(window.canvas, '_color_resources', None)
        if resources is not None:
            resources.shutdown()
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.close()
        window.deleteLater()
