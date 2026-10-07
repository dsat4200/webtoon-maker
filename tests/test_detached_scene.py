"""Detached production scene kernels retain native pixels and source ownership."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, ChildRef
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.scene import DetachedSceneBackend, SceneSnapshotCompiler
from comic_editor.render.scheduler import SceneDemand, SceneScheduler
from comic_editor.render.service import DocumentRenderService, RenderRequest, RenderQuality
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda _: None)
    chapter = ChapterDocument(width=64, height=64, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 64, 64))
    owner = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    owner.set_document(chapter, TileStore())
    yield owner
    owner._scene_controller.reset()
    owner._scene_controller.scheduler.close()
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.deleteLater()


def freeze(canvas, compiler=None):
    compiler = compiler or SceneSnapshotCompiler()
    capture = compiler.capture(canvas, canvas._render_document_state())
    while not capture.advance(.001):
        pass
    assert not capture.stale
    return capture.result


def render(snapshot, quality=RenderQuality.EXACT):
    backend = DetachedSceneBackend(snapshot)
    service = DocumentRenderService(backend)
    service.projection.revision = snapshot.document.revision
    request = RenderRequest((0., 0., 64., 64.), 1., (64, 64), ("test",), snapshot.document.revision,
                            quality=quality)
    return service.render_region(snapshot.document, request)


def pixels(image):
    return bytes(image.constBits())


def test_detached_worker_matches_live_native_scene(canvas):
    snapshot = freeze(canvas)
    request = RenderRequest((0., 0., 64., 64.), 1., (64, 64), ("test",), snapshot.document.revision)
    reference = canvas._render_service.render_region(snapshot.document, request)
    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(render, snapshot).result(timeout=20)
    assert result.exact
    assert pixels(result.image) == pixels(reference.image)


def test_source_and_model_mutation_cannot_change_older_snapshot(canvas):
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    obj = RasterObject(parent_layer_id=page.layer_id, interaction_rect=(0, 0, 64, 64))
    canvas.chapter.objects[obj.object_id] = obj
    page.children.append(ChildRef("object", obj.object_id))
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor("red"))
    canvas.tiles.set_tile(obj.object_id, (0, 0), tile)
    snapshot = freeze(canvas)
    canvas.tiles.tile(obj.object_id, (0, 0)).fill(QColor("blue"))
    obj.opacity = 0.
    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(render, snapshot).result(timeout=20)
    assert result.exact
    assert result.image.pixelColor(32, 32) == QColor("red")


def test_live_vector_tile_capture_owns_native_pixels_and_survives_retirement(canvas):
    from comic_editor.core.models import VectorDrawingObject
    from PySide6.QtCore import QPointF
    drawing = canvas.chapter.add_object(canvas.chapter.root_page_ids[0], VectorDrawingObject())
    canvas.set_selection('object', drawing.object_id)
    canvas.primary_color = '#ff0000ff'
    canvas._begin_vector_pencil(drawing, QPointF(10, 32), 1.)
    canvas._append_vector_sample(QPointF(55, 32), 1.)
    snapshot = freeze(canvas)
    source = snapshot.state['_vector_preview_tiles']
    assert source is not canvas._vector_preview_tiles
    assert source.object_tiles(canvas._vector_preview_id)
    canvas._cancel_vector_gesture()
    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(render, snapshot, RenderQuality.INTERACTIVE).result(timeout=20)
    assert not result.exact  # Live ink remains a draft even after its source retires.
    assert result.image.pixelColor(30, 32) == QColor('blue')


def test_changed_records_only_and_stale_capture_retired(canvas):
    compiler = SceneSnapshotCompiler()
    first = freeze(canvas, compiler)
    compiler.invalidate(ChangeSet((EntityChange(("layer", canvas.chapter.root_page_ids[0]), frozenset({"opacity"})),)))
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    page.opacity = .5
    second = freeze(canvas, compiler)
    assert first.chapter.layers[page.layer_id] is not second.chapter.layers[page.layer_id]
    assert first.chapter.layers[page.layer_id].opacity == 1.
    capture = compiler.capture(canvas, canvas._render_document_state())
    canvas._render_service.invalidate()
    assert capture.advance() and capture.stale and capture.result is None


def test_scheduler_publishes_completed_blocks_without_canvas_calls(canvas):
    snapshot = freeze(canvas)
    requests = tuple(canvas._document_projection.requests(QRectF(0, 0, 64, 64), 1.))
    scheduler = SceneScheduler(handoff_budget=2 * 1024 * 1024)
    scheduler.submit(SceneDemand(1, snapshot, requests, (None,), (32., 32.)))
    completions = []
    deadline = time.monotonic() + 20
    try:
        while scheduler.busy and time.monotonic() < deadline:
            completions.extend(scheduler.poll())
            time.sleep(.005)
        completions.extend(scheduler.poll())
        assert any(completion.tiles for completion in completions), completions
        assert not any(completion.error for completion in completions), completions
        assert completions[-1].done
    finally:
        scheduler.close()


def test_widget_paint_cannot_call_scene_evaluator(canvas, monkeypatch):
    from PySide6.QtGui import QPainter
    image = QImage(64, 64, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    monkeypatch.setattr(canvas, "_render_scene_layers", lambda *_a, **_k: pytest.fail("Scene evaluated during paint"))
    monkeypatch.setattr(canvas, "_render_document_region", lambda *_a, **_k: pytest.fail("Region evaluated during paint"))
    try:
        canvas._paint_ready_document_projection(painter)
    finally:
        painter.end()
    assert canvas._scene_controller.capture is not None


def test_detached_view_reuses_same_durable_tile_key_and_validation(canvas, tmp_path, monkeypatch):
    from comic_editor.render.cache import PersistentRenderCache
    from comic_editor.ui.cache_dependencies import RenderDependencies
    from comic_editor.render.pixels import color_environment
    request = canvas._document_projection.requests(QRectF(0, 0, 64, 64), 1.)[0]
    contract = canvas.chapter.pixel_contract
    backing = PersistentRenderCache(tmp_path / "cache", contract=contract.signature,
        environment=(*RenderDependencies.environment(), color_environment(contract)))
    canvas._persistent_render_cache = backing
    dependencies = RenderDependencies(canvas, backing)
    canvas._render_bounds.prepare()
    key = dependencies.projection_key(request, (*canvas._projection_configuration(), None))
    image = QImage(request.pixel_size, request.pixel_size, contract.image_format)
    image.fill(QColor("magenta"))
    try:
        with backing.record():
            backing.retain("projection", key, image)
        backing.drain()
        snapshot = freeze(canvas)
        def read():
            backend = DetachedSceneBackend(snapshot)
            try:
                result = backend.lookup_tile(request, None)
                assert result is not None
                assert pixels(result) == pixels(image)
                assert backend.scene._persistent_render_cache.recording == 0
            finally:
                backend.close()
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(read).result(timeout=20)
    finally:
        canvas._persistent_render_cache = None
        backing.close()


def test_real_navigator_uses_detached_scene_and_retains_completed_view(canvas, qapp, monkeypatch):
    from comic_editor.ui.preview import ChapterPreview
    from PySide6.QtTest import QTest
    canvas.chapter.background = "#FF1122CC"
    preview = ChapterPreview(canvas)
    preview.resize(92, 300)
    preview.show()
    monkeypatch.setattr(canvas, "render_preview", lambda *_a, **_k: pytest.fail("Live canvas evaluated navigator"))
    deadline = time.monotonic() + 10
    try:
        while preview._cache.isNull() and time.monotonic() < deadline:
            QTest.qWait(10)
        assert not preview._cache.isNull(), preview._navigator_jobs.error
        assert preview._cache.pixelColor(preview._cache.rect().center()) == QColor("#1122CC")
        assert preview._cache_chapter is canvas.chapter
    finally:
        preview._navigator_jobs.cancel()
        preview._navigator_jobs.scheduler.close()
        preview.close()
        preview.deleteLater()


def test_vector_snapshot_reuses_unchanged_strokes_and_rejects_partial_edits(canvas):
    from comic_editor.core.models import VectorDrawingObject, VectorStroke, VectorStrokePoint
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    drawing = VectorDrawingObject(parent_layer_id=page.layer_id, strokes=[
        VectorStroke(points=[VectorStrokePoint(x=i / 50, y=20) for i in range(2000)]),
        VectorStroke(points=[VectorStrokePoint(x=10, y=40), VectorStrokePoint(x=40, y=40)]),
    ])
    canvas.chapter.objects[drawing.object_id] = drawing
    page.children.append(ChildRef("object", drawing.object_id))
    compiler = SceneSnapshotCompiler()
    first = freeze(canvas, compiler)
    change = ChangeSet((EntityChange(("object", drawing.object_id), frozenset({"strokes"})),))
    drawing.strokes[1].points[0].y = 30
    drawing.strokes[1].touch_render_revision()
    drawing.touch_revision()
    compiler.invalidate(change)
    second = freeze(canvas, compiler)
    old = first.chapter.objects[drawing.object_id].strokes
    new = second.chapter.objects[drawing.object_id].strokes
    assert new[0] is old[0]
    assert new[1] is not old[1]
    assert old[1].points[0].y == 40 and new[1].points[0].y == 30
    compiler.invalidate()
    pending = compiler.capture(canvas, canvas._render_document_state())
    assert not pending.advance(0) and pending.result is None
    drawing.strokes[0].points[-1].y = 50
    canvas._render_service.invalidate()
    assert pending.advance() and pending.stale and pending.result is None
    # A conservative edit without a stroke revision still gets a new immutable
    # identity; undo/branch history cannot collide with an old rasterized stroke.
    third = freeze(canvas, compiler)
    assert third.chapter.objects[drawing.object_id].strokes[0]._scene_generation != new[0]._scene_generation
    assert third.chapter.objects[drawing.object_id].strokes[0].points[-1].y == 50


def test_scene_worker_retains_valid_vector_resources_across_another_edit(canvas, monkeypatch):
    from comic_editor.core.models import VectorDrawingObject, VectorStroke, VectorStrokePoint
    from comic_editor.render.scene_kernels import SceneKernels
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    drawing = VectorDrawingObject(parent_layer_id=page.layer_id, strokes=[
        VectorStroke(points=[VectorStrokePoint(x=10, y=20), VectorStrokePoint(x=40, y=20)])])
    canvas.chapter.objects[drawing.object_id] = drawing
    page.children.append(ChildRef("object", drawing.object_id))
    compiler = SceneSnapshotCompiler()
    stored = []
    original = SceneKernels._store_vector_render_cache
    def store(owner, key, value):
        stored.append(key)
        return original(owner, key, value)
    monkeypatch.setattr(SceneKernels, "_store_vector_render_cache", store)
    scheduler = SceneScheduler()
    requests = tuple(canvas._document_projection.requests(QRectF(0, 0, 64, 64), 1.))
    def complete(serial):
        snapshot = freeze(canvas, compiler)
        scheduler.submit(SceneDemand(serial, snapshot, requests, (None,), (32., 32.)))
        completions = []
        deadline = time.monotonic() + 20
        while scheduler.busy and time.monotonic() < deadline:
            completions.extend(scheduler.poll())
            time.sleep(.005)
        completions.extend(scheduler.poll())
        assert completions and completions[-1].done
        assert not any(item.error for item in completions), completions
    try:
        complete(1)
        count = len(stored)
        assert count
        page.opacity = .5
        compiler.invalidate(ChangeSet((EntityChange(("layer", page.layer_id), frozenset({"opacity"})),)))
        canvas._render_service.invalidate()
        complete(2)
        assert len(stored) == count
    finally:
        scheduler.close()


def test_small_tile_budget_still_finishes_a_native_overview(canvas, qapp, monkeypatch):
    from PySide6.QtTest import QTest
    canvas.resize(64, 64)
    canvas.center_x = canvas.center_y = 32.
    canvas.scale = 1.
    canvas._document_projection.budget = 4096
    canvas.chapter.background = "#FF1357CC"
    canvas._scene_snapshot_compiler.invalidate()
    canvas._render_service.invalidate()
    monkeypatch.setattr(canvas, "_render_scene_layers",
        lambda *_a, **_k: pytest.fail("Live widget evaluated an overview"))
    canvas.show()
    deadline = time.monotonic() + 10
    while (canvas._scene_controller.overview is None or canvas._projection_frame_pending) and time.monotonic() < deadline:
        QTest.qWait(10)
    controller = canvas._scene_controller
    assert controller.overview is not None,(controller.error,controller.serial,
        controller.scheduler.submitted,controller.scheduler.completed,
        controller.scheduler.future.done() if controller.scheduler.future is not None else None,
        controller.capture is not None,controller.desired == controller.dispatched)
    assert not canvas._projection_frame_pending
    assert not canvas._projection_provisional_visible
    assert canvas._projection_presented_revision == canvas._document_projection.revision
    image = canvas._scene_controller.overview[1].image
    assert image.pixelColor(image.rect().center()) == QColor("#1357CC")
    assert image.sizeInBytes() <= canvas._document_projection.budget
    assert not canvas._document_projection.tiles  # No reduced-density artwork entered the exact LRU.


def test_navigator_keeps_finished_pixels_for_metadata_only_notices(canvas):
    from comic_editor.ui.preview import ChapterPreview
    preview = ChapterPreview(canvas)
    preview._dirty_full = False
    change = ChangeSet((EntityChange(("layer", canvas.chapter.root_page_ids[0]), frozenset({"name"})),))
    old = canvas.command_stack.applying_change
    canvas.command_stack.applying_change = change
    try:
        preview.invalidate(None)
        preview.invalidate_all()
        assert not preview._dirty_full
    finally:
        canvas.command_stack.applying_change = old
        preview._navigator_jobs.scheduler.close()
        preview.deleteLater()


def test_unchanged_file_sources_share_pins_and_keep_their_captured_revision(canvas, tmp_path):
    from comic_editor.core.tile_backing import finish_revision_readers
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    obj = RasterObject(parent_layer_id=page.layer_id, interaction_rect=(0, 0, 64, 64))
    canvas.chapter.objects[obj.object_id] = obj
    page.children.append(ChildRef("object", obj.object_id))
    source = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("red"))
    path = tmp_path / "0_0.png"
    assert source.save(str(path))
    canvas.tiles._object_tiles(obj.object_id).entries[(0, 0)] = path
    compiler = SceneSnapshotCompiler()
    first = freeze(canvas, compiler)
    page.opacity = .5
    compiler.invalidate(ChangeSet((EntityChange(("layer", page.layer_id), frozenset({"opacity"})),)))
    second = freeze(canvas, compiler)
    assert first.pin_jobs and second.pin_jobs == first.pin_jobs
    finish_revision_readers(tmp_path)
    source.fill(QColor("blue"))
    replacement = tmp_path / "next.png"
    assert source.save(str(replacement))
    replacement.replace(path)
    with ThreadPoolExecutor(max_workers=1) as worker:
        original = worker.submit(render, first).result(timeout=20)
    assert original.image.pixelColor(32, 32) == QColor("red")
    canvas.tiles.set_tile(obj.object_id, (0, 0), source)
    compiler.invalidate(ChangeSet((EntityChange(("object", obj.object_id)),)))
    third = freeze(canvas, compiler)
    assert not third.pin_jobs
    with ThreadPoolExecutor(max_workers=1) as worker:
        latest = worker.submit(render, third).result(timeout=20)
    assert latest.image.pixelColor(32, 32).blue() > latest.image.pixelColor(32, 32).red()


def test_real_worker_keeps_an_edited_view_coherent_until_all_blocks_finish(canvas, qapp, wait_scene, monkeypatch):
    from threading import Event
    from PySide6.QtCore import QPointF
    canvas.chapter.width = 2048
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    page.bound = BoundGeometry.rectangle(0, 0, 2048, 64)
    page.fill_color = "#FF1565C0"
    canvas.resize(1024, 64)
    canvas.center_x, canvas.center_y, canvas.scale = 1024., 32., .5
    canvas._scene_snapshot_compiler.invalidate()
    canvas._render_service.invalidate()
    before = wait_scene(canvas)
    old_view = canvas._projection_completed_view
    old_revision = canvas._document_projection.revision
    waiting, release = Event(), Event()
    original = DetachedSceneBackend.paint
    counts = {}
    def delayed(backend, *args, **kwargs):
        revision = backend.snapshot.document.revision
        if revision > old_revision:
            counts[revision] = counts.get(revision, 0) + 1
            if counts[revision] == 2:
                waiting.set()
                assert release.wait(15)
        return original(backend, *args, **kwargs)
    monkeypatch.setattr(DetachedSceneBackend, "paint", delayed)
    page.fill_color = "#FF2E7D32"
    canvas._scene_snapshot_compiler.invalidate(ChangeSet((EntityChange(("layer", page.layer_id), frozenset({"shape_style"})),)))
    canvas._render_service.invalidate()
    deadline = time.monotonic() + 10
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    try:
        while not waiting.is_set() and time.monotonic() < deadline:
            canvas.render(image)
            qapp.processEvents()
            time.sleep(.002)
        assert waiting.is_set(), canvas._scene_controller.error
        # Let the first ready block publish while the second remains blocked.
        for _ in range(8):
            qapp.processEvents()
            time.sleep(.005)
        canvas.render(image)
        assert canvas._projection_frame_pending
        assert canvas._projection_completed_view is old_view
        assert image == before
    finally:
        release.set()
    after = wait_scene(canvas)
    assert after != before
    assert not canvas._projection_frame_pending
    assert canvas._projection_presented_revision == canvas._document_projection.revision
    for world in (QPointF(512, 32), QPointF(1536, 32)):
        probe = canvas.document_to_widget(world).toPoint()
        assert after.pixelColor(probe).green() > after.pixelColor(probe).blue()


def test_reused_pin_batches_never_override_a_newer_captured_source(canvas,tmp_path):
    from types import SimpleNamespace
    snapshot = freeze(canvas)
    target = snapshot.tiles._object_tiles('source')
    original = tmp_path/'source.png'
    old,new,other = (tmp_path/name for name in ('old.png','new.png','other.png'))
    for path,color in ((old,'red'),(new,'blue'),(other,'green')):
        image = QImage(256,256,QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(color))
        assert image.save(str(path))
    target.entries[(0,0)],target.versions[(0,0)] = original,2
    target.entries[(1,0)],target.versions[(1,0)] = other,1
    class Job:
        def __init__(self,records,pins):
            self.records,self.pins = records,pins
        def pin(self,identifier,key):
            return SimpleNamespace(path=self.pins[(identifier,key)])
    newer = Job({('source',(0,0)):((2,0),original)},{('source',(0,0)):new})
    older = Job({('source',(0,0)):((1,0),original),('source',(1,0)):((1,0),other)},
        {('source',(0,0)):old,('source',(1,0)):other})
    # The newer batch may flush before a reused batch later in source order.
    snapshot = replace(snapshot,pin_jobs=(newer,older)).finish_sources()
    assert target[(0,0)].pixelColor(10,10) == QColor('blue')
    assert target[(1,0)].pixelColor(10,10) == QColor('green')


def test_exact_overview_tile_gutters_preserve_fractional_display_sampling(canvas):
    import numpy as np
    from PySide6.QtCore import QRect,Qt
    from PySide6.QtGui import QPainter
    from PIL import Image
    from comic_editor.render.overview import exact_overview
    width,height = 1280,512
    chapter = ChapterDocument(width=width,height=height,document_kind='asset',background='#00000000')
    page = chapter.add_page('Page',BoundGeometry.rectangle(0,0,width,height))
    page.shape_style.outline_thickness = 0
    obj = RasterObject(parent_layer_id=page.layer_id,interaction_rect=(0,0,width,height))
    chapter.objects[obj.object_id] = obj
    page.children.append(ChildRef('object',obj.object_id))
    store = TileStore()
    x,y = np.meshgrid(np.arange(width),np.arange(height))
    rgba = np.stack(((x//7+y//11)%2*100+60,(x*3+y*5)%256,
        (x*11+y*2)%256,(x//13+y//17)%2*110+130),axis=-1).astype(np.uint8)
    source = QImage(rgba.data,width,height,width*4,QImage.Format_RGBA8888).convertToFormat(QImage.Format_ARGB32_Premultiplied)
    for tx in range(width//256):
        for ty in range(height//256):
            store.set_tile(obj.object_id,(tx,ty),source.copy(QRect(tx*256,ty*256,256,256)))
    canvas.set_document(chapter,store)
    snapshot = freeze(canvas)
    visible = (13.25,7.75,1245.5,496.25)
    requests = tuple(canvas._document_projection.requests(QRectF(*visible),1.))
    demand = SceneDemand(1,snapshot,requests,(None,),(640.,256.),visible,presentation_size=(176,59))
    def evaluate():
        backend = DetachedSceneBackend(snapshot)
        service = DocumentRenderService(backend)
        service.projection.revision = snapshot.document.revision
        try:
            native = service.render_region(snapshot.document,RenderRequest((0.,0.,width,height),
                1.,(width,height),('whole',),snapshot.document.revision))
            assert native.exact
            overview = exact_overview(demand,backend,service,lambda:False)
            from comic_editor.render.service import TileBatchPolicy
            blocks = service.render_tiles(snapshot.document,requests,TileBatchPolicy((640.,256.)))
            joined = QImage(width,height,QImage.Format_ARGB32_Premultiplied)
            joined.fill(Qt.transparent)
            joiner = QPainter(joined)
            for request in requests:
                joiner.drawImage(request.world_rect,blocks.tiles[request.address][0],QRectF(request.source_rect))
            joiner.end()
            return native.image,overview.image,joined
        finally:
            backend.close()
    with ThreadPoolExecutor(1) as worker:
        native,actual,joined = worker.submit(evaluate).result(20)
    assert native == joined
    # An independent whole-image premultiplied bilinear reference checks the
    # global sample phase. Pillow truncates while presentation rounds bytes.
    expected = Image.frombytes('RGBa',(width,height),bytes(native.constBits())).transform(
        (176,59),Image.Transform.AFFINE,(visible[2]/176,0.,visible[0],0.,visible[3]/59,visible[1]),
        resample=Image.Resampling.BILINEAR)
    a = np.frombuffer(actual.constBits(),np.uint8).reshape(59,176,4)
    b = np.asarray(expected)
    delta = np.abs(a.astype(int)-b.astype(int))
    assert delta.max() <= 1


def test_overview_context_loss_restores_same_native_request_off_gui(qapp):
    from types import SimpleNamespace
    from threading import get_ident
    from comic_editor.render.device import DeviceImage
    from comic_editor.render.overview import exact_overview
    from comic_editor.render.projection import ProjectionAddress,ProjectionRequest
    from comic_editor.render.service import RenderDocument
    request = ProjectionRequest(ProjectionAddress(0,0,0),tile_size=2,gutter=1)
    document = RenderDocument(('lost-context',),(),0,2,2,'#00000000')
    demand = SimpleNamespace(visible=(0.,0.,2.,2.),presentation_size=(2,2),center=(1.,1.),
        requests=(request,),snapshot=SimpleNamespace(document=document,pixel_environment=None))
    gui_thread = get_ident()
    calls = []
    class Owner:
        closed,available = False,True
        def materialize(self,_image):
            assert get_ident() != gui_thread
            self.closed,self.available = True,False
            raise RuntimeError('Context lost during actual CPU consumer')
        def release(self,_token): pass
    owner = Owner()
    device = DeviceImage(owner,123,('same-native-pixels',),4,4,1,0)
    native = QImage(4,4,QImage.Format_ARGB32_Premultiplied)
    native.fill(QColor('#FF1357CC'))
    def render(metadata,requests,*_a,**_k):
        assert get_ident() != gui_thread and metadata is document
        assert requests == [request]
        calls.append(tuple(requests))
        return SimpleNamespace(tiles={request.address:(device if len(calls)==1 else native,True)},
            error=None,pending=False)
    with ThreadPoolExecutor(1) as worker:
        result = worker.submit(exact_overview,demand,SimpleNamespace(lookup_tile=lambda *_a:None),
            SimpleNamespace(render_tiles=render),lambda:False).result(5)
    assert len(calls) == 2
    assert result.image.pixelColor(0,0) == native.pixelColor(0,0)
    device.release()


def test_overview_cancel_between_native_tiles_stops_cpu_consumers(monkeypatch):
    from types import SimpleNamespace
    from comic_editor.render.admission import WorkCancelled
    from comic_editor.render.overview import exact_overview
    from comic_editor.render.projection import ProjectionAddress,ProjectionRequest
    from comic_editor.render.service import RenderDocument
    requests = tuple(ProjectionRequest(ProjectionAddress(0,x,0),tile_size=2,gutter=1) for x in range(2))
    document = RenderDocument(('cancel-overview',),(),0,4,2,'#00000000')
    demand = SimpleNamespace(visible=(0.,0.,4.,2.),presentation_size=(4,2),center=(2.,1.),
        requests=requests,snapshot=SimpleNamespace(document=document,pixel_environment=None))
    native = QImage(4,4,QImage.Format_ARGB32_Premultiplied)
    native.fill(QColor('blue'))
    consumed = []
    def consume(image):
        consumed.append(image)
        return image
    monkeypatch.setattr('comic_editor.render.overview.cpu_image',consume)
    service = SimpleNamespace(render_tiles=lambda *_a,**_k:SimpleNamespace(
        tiles={request.address:(native,True) for request in requests},error=None,pending=False))
    with pytest.raises(WorkCancelled):
        exact_overview(demand,SimpleNamespace(lookup_tile=lambda *_a:None),service,lambda:bool(consumed))
    assert consumed == [native]


def test_same_demand_recovers_lost_device_storage_with_detached_cpu_tiles(canvas,qapp):
    from PySide6.QtTest import QTest
    from comic_editor.render.device import DeviceImage
    canvas.chapter.background = '#FF1357CC'
    snapshot = freeze(canvas)
    document = snapshot.document
    projection = canvas._document_projection
    visible = QRectF(0,0,64,64)
    requests = tuple(projection.requests(visible,1.))
    projection.configure((*document.configuration,None),document=document.identity)
    projection.revision = document.revision
    class Graphics:
        closed,available = False,True
        release = staticmethod(lambda *_a:None)
        materialize = staticmethod(lambda *_a:pytest.fail('Lost storage must not read back on GUI'))
    graphics = Graphics()
    request = requests[0]
    device = DeviceImage(graphics,123,('lost-tile',),request.pixel_size,request.pixel_size,1,0)
    assert projection.adopt(request,device,configuration=(*document.configuration,None),
        document=document.identity,revision=document.revision)
    controller = canvas._scene_controller
    signature = document,tuple(request.address for request in requests),(None,),tuple(visible.getRect())
    controller.snapshot,controller.desired,controller.dispatched = snapshot,signature,signature
    graphics.closed,graphics.available = True,False
    controller.request(document,requests,(None,),visible)
    assert controller.dispatched is None and controller.capture is None
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        controller.advance()
        ready = projection.ready(requests,configuration=(*document.configuration,None))
        if len(ready)==len(requests) and not controller.scheduler.busy:
            break
        QTest.qWait(5)
        time.sleep(.002)
    assert not controller.error
    assert len(ready)==len(requests) and not any(isinstance(tile.image,DeviceImage) for tile in ready)
    assert ready[0].image.pixelColor(projection.gutter+32,projection.gutter+32) == QColor('#FF1357CC')
    # A terminal renderer failure must not repeatedly resubmit unchanged work.
    controller.error = 'Reference evaluator failed'
    controller.dispatched = signature
    ready[0].valid = False
    controller.request(document,requests,(None,),visible)
    assert controller.dispatched == signature
    device.release()


@pytest.mark.parametrize('lost_at_copy',[False,True])
def test_display_materialization_reports_lost_storage_as_miss_without_gui_readback(canvas,lost_at_copy):
    from types import SimpleNamespace
    from threading import get_ident
    from comic_editor.render.device import DeviceImage
    gui = get_ident()
    class Graphics:
        closed,available = False,True
        def materialize(self,_image):
            assert get_ident() != gui
            self.closed,self.available = True,False
            raise RuntimeError('Device context lost')
        def release(self,_token): pass
    graphics = Graphics()
    image = DeviceImage(graphics,123,('lost-materialization',),4,4,1,0)
    if lost_at_copy:
        graphics.closed = True
    scheduler = SceneScheduler()
    try:
        demand = SceneDemand(1,SimpleNamespace(),(),(),(0.,0.))
        scheduler.materialize(demand,{image.cacheKey():image})
        for future in tuple(scheduler._transfers):
            future.result(5)
        completed = scheduler.poll()
        assert len(completed)==1 and completed[0].materialized == {}
        assert completed[0].transfer_keys == (image.cacheKey(),) and not completed[0].error
    finally:
        scheduler.close()
        image.release()


def test_capture_failure_is_terminal_for_revision_and_stops_repeated_gui_slices(canvas):
    document = canvas._render_document_state()
    controller = canvas._scene_controller
    visible = QRectF(0,0,64,64)
    controller.desired = document,(),(None,),tuple(visible.getRect())
    calls = []
    class FailedCapture:
        def advance(self,_budget):
            calls.append('capture')
            raise TypeError('Transient source is not detachable')
    controller.capture = FailedCapture()
    controller.timer.start(0)
    controller.advance()
    assert calls == ['capture'] and controller.capture is None and not controller.timer.isActive()
    assert controller.error == 'TypeError: Transient source is not detachable'
    assert canvas._projection_error_revision == document.revision
    controller.request(document,(),(None,),visible)
    assert not controller.timer.isActive() and controller.dispatched == controller.desired
    canvas._document_projection.revision += 1
    newer = canvas._render_document_state()
    controller.request(newer,(),(None,),visible)
    assert not controller.error and not canvas._projection_render_error
    assert canvas._projection_error_revision == -1
    deadline = time.monotonic()+15
    while controller.capture is not None and time.monotonic() < deadline:
        controller.advance()
    assert controller.snapshot.document == newer
    assert controller.dispatched == controller.desired and not controller.error
    controller.reset()
    assert not canvas._projection_render_error and canvas._projection_error_revision == -1
