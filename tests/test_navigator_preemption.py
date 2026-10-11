"""A stale/provisional navigator cannot occupy the visible rendering lane."""
from threading import Event
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.admission import WorkAdmission, current_ticket
from comic_editor.render.scheduler import SceneDemand, SceneScheduler
from comic_editor.render.scene_kernels import SceneKernels
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.navigator_jobs import NavigatorJobs


@pytest.fixture
def native_scene(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    chapter = ChapterDocument(width=256, height=768, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 256, 768))
    page.fill_color, page.border_width = None, 0
    tiles = TileStore()
    for y in range(3):
        obj = chapter.add_object(page.layer_id, RasterObject(y=y * 256))
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('red'))
        tiles.set_tile(obj.object_id, (0, 0), image)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, tiles)
    capture = canvas._scene_snapshot_compiler.capture(canvas, canvas._render_document_state())
    while not capture.advance(.001):
        pass
    try:
        yield canvas, capture.result
    finally:
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()


def drain(schedulers, seconds=5):
    received = []
    deadline = time.monotonic() + seconds
    while any(scheduler.busy for scheduler in schedulers) and time.monotonic() < deadline:
        for scheduler in schedulers:
            received.extend(scheduler.poll())
        time.sleep(.001)
    for scheduler in schedulers:
        received.extend(scheduler.poll())
    assert all(not scheduler.busy for scheduler in schedulers)
    assert not [item.error for item in received if item.error]
    return received


@pytest.mark.parametrize('reason', ['visible', 'stale'])
def test_navigator_unwinds_before_another_source_and_drops_partial_image(native_scene, monkeypatch, reason):
    canvas, snapshot = native_scene
    admission = WorkAdmission(64 * 1024 * 1024)
    monkeypatch.setattr('comic_editor.render.scheduler.RENDER_ADMISSION', admission)
    navigator, visible = SceneScheduler(), SceneScheduler()
    entered, release = Event(), Event()
    calls = []
    original = SceneKernels._render_object_content

    def controlled(scene, *args, **kwargs):
        if current_ticket().owner == 'navigator':
            calls.append(args[1].object_id)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
        return original(scene, *args, **kwargs)

    monkeypatch.setattr(SceneKernels, '_render_object_content', controlled)
    demand = SceneDemand(1, snapshot, (), (None,), (128., 384.),
        (0., 0., 256., 768.), (64, 192), consumer='navigator')
    try:
        navigator.submit(demand)
        assert entered.wait(5)
        if reason == 'visible':
            request = canvas._document_projection.requests(QRectF(0, 0, 256, 256), 1.)[0]
            visible.submit(SceneDemand(2, snapshot, (request,), (None,), (128., 128.)))
            deadline = time.monotonic() + 5
            while not admission.waiters and time.monotonic() < deadline:
                time.sleep(.001)
            assert admission.waiters
        else:
            navigator.cancel()
        release.set()
        received = drain((navigator, visible))
        assert len(calls) == 1, 'Canceled thumbnail kept rendering unrelated artwork'
        assert not any(item.preview is not None for item in received)
        if reason == 'visible':
            assert any(item.demand.serial == 1 and item.done for item in received)
            result = next(item for item in received if item.tiles)
            image, exact = result.tiles[request.address]
            assert exact and image.pixelColor(128, 128) == QColor('red')
        assert not admission.running and not admission.waiters
        # An idle retry still produces the entire ordinary thumbnail.
        navigator.submit(demand)
        retried = drain((navigator,))
        result = next(item.preview for item in retried if item.preview is not None)
        assert result.image.size().width() == 64
        assert all(result.image.pixelColor(32, y) == QColor('red') for y in (32, 96, 160))
    finally:
        release.set()
        navigator.close()
        visible.close()
        navigator.executor.shutdown(wait=True, cancel_futures=True)
        visible.executor.shutdown(wait=True, cancel_futures=True)


def test_preempted_navigator_keeps_dirty_state_and_schedules_idle_retry(native_scene):
    canvas, snapshot = native_scene
    scheduled = []
    preview = SimpleNamespace(canvas=canvas, _dirty_full=False, _dirty_bands=[QRectF(0, 200, 10, 20)],
        isVisible=lambda: True, _interaction_active=lambda: False,
        _schedule_refresh=lambda: scheduled.append(True))
    # QObject ownership is independent of the widget-shaped fixture.
    jobs = NavigatorJobs(canvas)
    jobs.preview = preview
    jobs.document = snapshot.document
    jobs.capture = None
    jobs.sent = True
    try:
        jobs.advance()
        assert scheduled == [True]
        assert preview._dirty_bands and not preview._dirty_full
        assert jobs.document is None and not jobs.timer.isActive()
    finally:
        jobs.scheduler.close()
        jobs.scheduler.executor.shutdown(wait=True, cancel_futures=True)
        jobs.deleteLater()
