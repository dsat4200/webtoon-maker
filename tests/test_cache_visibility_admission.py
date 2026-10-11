"""Visible native artwork must progress while a whole-chapter cache runs."""
from dataclasses import replace
from threading import Event
import time
from types import SimpleNamespace
from pathlib import Path
import hashlib
import json
import os

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.admission import WorkAdmission, current_ticket
from comic_editor.render.pixels import color_environment
from comic_editor.render.scheduler import SceneCompletion, SceneDemand, SceneScheduler
from comic_editor.render.service import DocumentRenderService, TileBatchPolicy
from comic_editor.ui.cache_dependencies import RenderDependencies
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def native_scene(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    chapter = ChapterDocument(width=256, height=768, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 256, 768))
    raster = chapter.add_object(page.layer_id, RasterObject())
    tiles = TileStore()
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    tiles.set_tile(raster.object_id, (0, 0), image)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, tiles)
    capture = canvas._scene_snapshot_compiler.capture(canvas, canvas._render_document_state())
    while not capture.advance(.001):
        pass
    assert capture.result is not None
    try:
        yield canvas, capture.result
    finally:
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()


@pytest.mark.parametrize('record', [False, True], ids=['memory', 'disk'])
def test_visible_tiles_finish_between_background_cache_rows(native_scene, tmp_path, monkeypatch, record):
    canvas, snapshot = native_scene
    # A single native workspace must exclude another native workspace, even
    # on a machine where both might otherwise fit a larger working budget.
    admission = WorkAdmission(64 * 1024 * 1024)
    monkeypatch.setattr('comic_editor.render.scheduler.RENDER_ADMISSION', admission)
    if record:
        snapshot = replace(snapshot, cache_spec=(tmp_path / 'cache',
            snapshot.document.pixel_contract.signature,
            (*RenderDependencies.environment(), color_environment(snapshot.document.pixel_contract))))
    requests = tuple(canvas._document_projection.requests(QRectF(0, 0, 256, 768), 1.))
    background, visible = SceneScheduler(), SceneScheduler()
    started, release, visible_rendered = Event(), Event(), Event()
    calls = []
    original = DocumentRenderService.render_tiles

    def controlled(service, *args, **kwargs):
        owner = current_ticket().owner
        calls.append(owner)
        if owner == 'cache' and calls.count('cache') == 1:
            started.set()
            assert release.wait(5)
        elif owner == 'cache' and calls.count('cache') == 2:
            # Holding one ticket across the chapter prevents this signal and
            # was the blank-canvas-until-Cancel failure.
            assert visible_rendered.wait(1), 'Visible rendering was starved by the cache job'
        result = original(service, *args, **kwargs)
        if owner == 'canvas':
            visible_rendered.set()
        return result

    monkeypatch.setattr(DocumentRenderService, 'render_tiles', controlled)
    completions = []
    try:
        background.submit(SceneDemand(1, snapshot, requests, (None,), (128., 0.),
            record=record, consumer='cache-memory'))
        assert started.wait(5)
        visible.submit(SceneDemand(2, snapshot, requests[:1], (None,), (128., 128.)))
        end = time.monotonic() + 5
        while not admission.waiters and time.monotonic() < end:
            time.sleep(.001)
        assert admission.waiters, 'Visible demand did not reach shared native admission'
        release.set()
        end = time.monotonic() + 10
        while (background.busy or visible.busy) and time.monotonic() < end:
            completions.extend(background.poll())
            completions.extend(visible.poll())
            time.sleep(.002)
        completions.extend(background.poll())
        completions.extend(visible.poll())
        assert not background.busy and not visible.busy
        assert not [item.error for item in completions if item.error]
        assert calls[:2] == ['cache', 'canvas']
        rendered = next(item for item in completions if item.demand.serial == 2 and item.tiles)
        image, exact = rendered.tiles[requests[0].address]
        assert exact and image.pixelColor(128, 128) == QColor('red')
        assert image.size().width() == requests[0].pixel_size
        assert not admission.running and not admission.waiters
    finally:
        release.set()
        background.close()
        visible.close()


def test_background_cache_rejects_live_preview_without_publishing_native_tiles(native_scene):
    _canvas, snapshot = native_scene
    snapshot = replace(snapshot, document=replace(snapshot.document, live_preview=True))
    scheduler = SceneScheduler()
    completions = []
    try:
        scheduler.submit(SceneDemand(1, snapshot, (), (None,), (0., 0.), consumer='cache-memory'))
        end = time.monotonic() + 5
        while scheduler.busy and time.monotonic() < end:
            completions.extend(scheduler.poll())
            time.sleep(.001)
        completions.extend(scheduler.poll())
        assert any('ordinary exact native tiles' in item.error for item in completions)
        assert all(not item.tiles and not item.recorded for item in completions)
    finally:
        scheduler.close()


def test_resident_native_tiles_are_ready_before_worker_capture(native_scene, monkeypatch):
    canvas, _snapshot = native_scene
    projection = canvas._document_projection
    configuration = (*canvas._projection_configuration(), None)
    projection.configure(configuration, document=configuration[:3])
    document = canvas._render_document_state()
    visible = QRectF(0, 0, 256, 256)
    requests = tuple(projection.requests(visible, 1.))
    image = QImage(requests[0].pixel_size, requests[0].pixel_size, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('magenta'))
    calls = []

    def lookup(request, incoming):
        calls.append((request.address, incoming))
        return image

    canvas._disk_cache_controller = SimpleNamespace(lookup_memory_tile=lookup)
    controller = canvas._scene_controller
    monkeypatch.setattr(controller, '_new_scene_capture',
        lambda _document: pytest.fail('A fully resident view captured the document'))
    controller.request(document, requests, (None,), visible)
    batch, complete = controller.ready_batch(document, requests, (None,))
    assert complete and batch[0][1][0].image.pixelColor(128, 128) == QColor('magenta')
    assert calls == [(requests[0].address, configuration)]
    assert controller.capture is None and not controller.timer.isActive()
    assert not document.live_preview


def test_resident_adoption_rechecks_document_revision_and_excludes_live(native_scene):
    canvas, _snapshot = native_scene
    projection = canvas._document_projection
    configuration = (*canvas._projection_configuration(), None)
    projection.configure(configuration, document=configuration[:3])
    document = canvas._render_document_state()
    requests = tuple(projection.requests(QRectF(0, 0, 256, 256), 1.))
    image = QImage(requests[0].pixel_size, requests[0].pixel_size, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    calls = []

    def obsolete(request, incoming):
        calls.append(request.address)
        projection.invalidate()
        return image

    canvas._disk_cache_controller = SimpleNamespace(lookup_memory_tile=obsolete)
    controller = canvas._scene_controller
    assert controller._adopt_resident_tiles(replace(document, live_preview=True), requests, (None,)) == 0
    assert not calls
    assert controller._adopt_resident_tiles(document, requests, (None,)) == 0
    assert calls and not projection.ready(requests, configuration=configuration)


def test_current_completion_publishes_memory_keys_only_after_revision_guard(native_scene):
    canvas, snapshot = native_scene
    controller = canvas._scene_controller
    projection = canvas._document_projection
    configuration = (*canvas._projection_configuration(), None)
    projection.configure(configuration, document=configuration[:3])
    document = canvas._render_document_state()
    snapshot = replace(snapshot, document=document)
    requests = tuple(projection.requests(QRectF(0, 0, 256, 256), 1.))
    keys = ((requests[0].address, None, ('ordinary-native-key',)),)
    adopted = []
    canvas._disk_cache_controller = SimpleNamespace(adopt_memory_keys=lambda *values: adopted.append(values))
    controller.serial = 4
    controller.desired = document, tuple(request.address for request in requests), (None,), (0., 0., 256., 256.)
    controller.dispatched = controller.desired
    controller.snapshot = snapshot
    stale = SceneCompletion(SceneDemand(3, snapshot, requests, (None,), (128., 128.)), cache_keys=keys)
    current = SceneCompletion(SceneDemand(4, snapshot, requests, (None,), (128., 128.)), cache_keys=keys)
    controller.scheduler.ready.extend((stale, current))
    controller.advance()
    assert adopted == [(current, document)]


@pytest.mark.skipif(not os.environ.get('POCKET_CACHE_PROJECT_COPY'),
    reason='Set POCKET_CACHE_PROJECT_COPY to the private saved Pocket Boyfriend copy')
def test_saved_pocket_native_view_finishes_while_memory_cache_is_running(qapp, tmp_path, monkeypatch):
    """Use the original saved graph and sources; write only private evidence."""
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.render.cache import PersistentRenderCache
    from comic_editor.render.resident_cache import ResidentRenderCache
    from comic_editor.render.scene import DetachedSceneBackend
    from comic_editor.ui.color_resources import semantic_color_identity
    import numpy as np

    project = Path(os.environ['POCKET_CACHE_PROJECT_COPY']).resolve()
    artifacts = Path(__file__).resolve().parents[1] / '.artifacts'
    assert project.is_relative_to(artifacts.resolve()) and (project / 'series.json').is_file()

    def manifest():
        return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in project.rglob('*') if path.is_file()}

    before = manifest()
    repository = SeriesRepository(project)
    chapter, tiles, images = repository.load_chapter('0a72f08009294aa0a3d14e6a38e22bbb', include_images=True)
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.resize(993, 937)
    canvas.set_document(chapter, tiles, images)
    resident = ResidentRenderCache(1024 * 1024 * 1024).view(chapter.chapter_id)
    backing = PersistentRenderCache(tmp_path / 'private-cache', contract=chapter.pixel_contract.signature,
        environment=(*RenderDependencies.environment(), semantic_color_identity(canvas, chapter.pixel_contract)),
        resident=resident)
    canvas._persistent_render_cache = backing
    drawing = chapter.objects['06be89900eb540ee83a26514822d32ec']
    point = canvas._drawing_local_to_world_transform(drawing).map(QPointF(1020., 1000.))
    top = int(point.y() // 256) * 256
    canvas.center_x, canvas.center_y, canvas.scale = point.x(), point.y(), 1.
    configuration = (*canvas._projection_configuration(), None)
    canvas._document_projection.configure(configuration, document=configuration[:3])
    document = canvas._render_document_state()
    capture = canvas._scene_snapshot_compiler.capture(canvas, document)
    while not capture.advance(.004):
        qapp.processEvents()
    snapshot = capture.result
    assert snapshot is not None and len(snapshot.chapter.objects) == 173
    background, visible = SceneScheduler(), SceneScheduler()
    first_block, release = Event(), Event()
    original = DocumentRenderService.render_tiles
    cache_blocks = []
    visible_finished = []
    visible_received = Event()
    visible_request = canvas._document_projection.requests(QRectF(point.x(), point.y(), 1., 1.), 1.)[0]
    background_requests = tuple(canvas._document_projection.requests(QRectF(0, top, 1080, 2048), 1.))

    def controlled(service, *args, **kwargs):
        owner = current_ticket().owner
        if owner == 'cache':
            cache_blocks.append(time.perf_counter())
            if len(cache_blocks) == 1:
                first_block.set()
                assert release.wait(10)
            elif len(cache_blocks) == 2:
                assert visible_received.wait(30)
        result = original(service, *args, **kwargs)
        if owner == 'canvas':
            visible_finished.append(time.perf_counter())
        return result

    monkeypatch.setattr(DocumentRenderService, 'render_tiles', controlled)
    completions, result, elapsed = [], None, None
    try:
        background.submit(SceneDemand(1, snapshot, background_requests, (None,), (540., float(top)),
            consumer='cache-memory'))
        assert first_block.wait(20)
        started = time.perf_counter()
        visible.submit(SceneDemand(2, snapshot, (visible_request,), (None,), (point.x(), point.y())))
        deadline = time.monotonic() + 5
        from comic_editor.render.admission import RENDER_ADMISSION
        while not RENDER_ADMISSION.waiters and time.monotonic() < deadline:
            time.sleep(.001)
        assert RENDER_ADMISSION.waiters
        release.set()
        deadline = time.monotonic() + 120
        while result is None and time.monotonic() < deadline:
            completions.extend(background.poll())
            received = visible.poll()
            completions.extend(received)
            result = next((item for item in received if item.tiles), None)
            assert not [item.error for item in completions if item.error]
            qapp.processEvents()
            time.sleep(.002)
        elapsed = (time.perf_counter() - started) * 1000
        assert result is not None
        assert background.busy and visible_finished, 'Saved drawing waited for the cache range to finish'
        assert len(cache_blocks) <= 2 and (len(cache_blocks) == 1 or visible_finished[0] < cache_blocks[1])
        image, exact = result.tiles[visible_request.address]
        assert exact and image.size().width() == visible_request.pixel_size
        assert np.any(np.frombuffer(image.constBits(), np.uint8)[3::4] > 0), 'Saved visible page stayed transparent'
        assert result.cache_keys and resident.snapshot()
        visible_received.set()
        # Finish the visible handoff, then stop background evaluation. Cancel
        # was never needed to obtain the saved project's visible native tile.
        deadline = time.monotonic() + 10
        while visible.busy and time.monotonic() < deadline:
            completions.extend(visible.poll())
            time.sleep(.001)
        background.cancel()
        deadline = time.monotonic() + 30
        while background.busy and time.monotonic() < deadline:
            background.poll()
            time.sleep(.002)
        assert not background.busy
        reference = DetachedSceneBackend(replace(snapshot, cache_spec=None))
        try:
            service = DocumentRenderService(reference)
            service.projection.revision = document.revision
            expected = original(service, document, [visible_request],
                TileBatchPolicy((point.x(), point.y())),
                phase=None, defer_effects=False).tiles[visible_request.address]
            assert expected[1] and bytes(image.constBits()) == bytes(expected[0].constBits())
        finally:
            reference.close()
        assert not list(backing.root.glob('**/*')), 'Memory warming wrote a disk artifact'
        report_path = os.environ.get('POCKET_CACHE_REPORT')
        if report_path:
            report = Path(report_path).resolve()
            assert report.is_relative_to(artifacts.resolve())
            report.parent.mkdir(parents=True, exist_ok=True)
            image.save(str(report.with_suffix('.png')))
            report.write_text(json.dumps(dict(project_files=len(before), saved_objects=len(chapter.objects),
                saved_layers=len(chapter.layers), saved_modifiers=len(chapter.modifiers),
                visible_native_tile=list(visible_request.capture_rect.getRect()),
                visible_ms=elapsed, completed_background_blocks_when_visible=1,
                visible_before_cache_cancel=True, exact_bytes_match_independent_cold_pipeline=True,
                memory_bytes=resident.bytes, disk_artifacts=0, sources_unchanged=True), indent=2), encoding='utf8')
    finally:
        release.set()
        visible_received.set()
        background.close()
        visible.close()
        canvas._persistent_render_cache = None
        backing.close()
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()
        assert manifest() == before
