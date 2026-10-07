"""Exact device graph segments, leases, sharing and genuine CPU consumers."""
from concurrent.futures import ThreadPoolExecutor
import threading

import numpy as np
import pytest
from PySide6.QtCore import QRect, QRectF, QSizeF
from PySide6.QtGui import QOpenGLContext, QTransform

from comic_editor.core.models import BlurModifier, ParameterMaskBinding
from comic_editor.render.device import ByteQuantizeStage, PointTableStage, ScalarBlurStage, DeviceImage
from comic_editor.render.gpu.worker import GpuWorker
from comic_editor.ui.point_lut import device_modifier_stack, compile_point_table
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _qimage_premultiplied
from test_gpu_worker import wait_until
from test_point_chain import effects, signature, source_image


@pytest.fixture
def worker(qapp):
    result = GpuWorker(gpu_budget=32*1024*1024,queue_budget=24*1024*1024)
    wait_until(qapp,result.ready.is_set)
    if not result.available:
        result.close()
        pytest.skip(result.reason)
    yield result
    result.close()
    assert result.surface is None and not result.worker_thread.isRunning()
    assert result.worker_thread.service is None and result.share_context is None


def materialize(qapp,image):
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(image.materialize_pixels)
        wait_until(qapp,future.done)
        return future.result()


def test_cpu_consumer_borrows_exclusive_admission_when_oversized(worker,qapp,monkeypatch):
    from comic_editor.render.admission import WorkAdmission
    pixels = _qimage_premultiplied(source_image(17,13))
    future = worker.submit_segment(pixels,[ByteQuantizeStage()],source_key='consumer-admission',canonical_input=True)
    wait_until(qapp,lambda:future.done() and worker.queued_bytes == 0)
    image = future.result()
    admission = WorkAdmission(budget=1)
    monkeypatch.setattr('comic_editor.render.admission.RENDER_ADMISSION',admission)
    np.testing.assert_array_equal(materialize(qapp,image),pixels)
    assert admission.admitted == 1 and admission.copied_bytes == 0
    image.release()


def test_failed_detached_copy_releases_shared_admission(worker,monkeypatch):
    from comic_editor.render.admission import RENDER_ADMISSION
    pixels = _qimage_premultiplied(source_image(17,13))
    key = signature(effects())
    table = compile_point_table(key)
    before = RENDER_ADMISSION.copied_bytes
    def allocation_failed(*args,**kwargs):
        raise MemoryError('Simulated request allocation failure')
    with monkeypatch.context() as patch:
        patch.setattr('comic_editor.render.gpu.worker.np.array',allocation_failed)
        with pytest.raises(MemoryError):
            worker.submit_lut(pixels,table,source_key='failed-copy',palette_key=key)
    assert RENDER_ADMISSION.copied_bytes == before and worker.queued_bytes == 0


def test_failed_device_context_retires_owner_and_stale_handles(worker,qapp,monkeypatch):
    from contextlib import contextmanager
    from comic_editor.render.gpu.point_chain import GpuPointChain
    from comic_editor.render.gpu.residency import GRAPHICS_RESIDENCY
    pixels = _qimage_premultiplied(source_image(17,13))
    future = worker.submit_segment(pixels,[ByteQuantizeStage()],source_key='before-loss',canonical_input=True)
    wait_until(qapp,lambda:future.done() and worker.queued_bytes == 0)
    image = future.result()
    original = GpuPointChain._current
    @contextmanager
    def lost(renderer):
        if renderer.context is worker.render_context:
            renderer.available = False
            raise RuntimeError('Simulated graphics context loss')
        with original(renderer):
            yield
    monkeypatch.setattr(GpuPointChain,'_current',lost)
    pending = worker.submit_segment(image,[ByteQuantizeStage()],source_key='after-loss',canonical_input=True)
    wait_until(qapp,lambda:pending.done() and not worker.worker_thread.isRunning())
    with pytest.raises(RuntimeError,match='context loss'):
        pending.result()
    assert image.isNull() and not worker.available
    assert worker.render_context is None and worker.queued_bytes == 0
    assert GRAPHICS_RESIDENCY.bytes == 0
    image.release()


@pytest.mark.parametrize('algorithm',['normal','legacy'])
@pytest.mark.parametrize('spatial',[False,True])
def test_device_point_blur_point_segment_matches_native_reference_without_intermediate_readback(worker,qapp,algorithm,spatial):
    source = source_image(53,37)
    modifiers = [*effects('soft_light'),BlurModifier(strength=11.25,algorithm=algorithm),*effects()]
    if spatial:
        expected = source
        for modifier in modifiers:
            expected = apply_modifier_stack(expected,[modifier],(0,0),_point_lut=False)
    else:
        expected = apply_modifier_stack(source,modifiers,(0,0),_point_lut=False)
    future = device_modifier_stack(source,modifiers,worker=worker,
                                   source_key=('native-source',1),quantize_stages=spatial)
    wait_until(qapp,lambda: future.done() and worker.queued_bytes == 0)
    image = future.result()
    assert isinstance(image,DeviceImage) and image.canonical
    assert worker.stats['readbacks'] == 0
    np.testing.assert_array_equal(materialize(qapp,image),_qimage_premultiplied(expected))
    assert worker.stats['readbacks'] == 1
    image.release()


def test_device_reuse_keeps_exact_pixels_pinned_and_release_returns_admission(worker,qapp):
    pixels = _qimage_premultiplied(source_image(257,273))
    key = signature(effects())
    stage = PointTableStage(compile_point_table(key),key)
    future = worker.submit_segment(pixels,[stage],source_key='pinned',canonical_input=True)
    wait_until(qapp,lambda: future.done() and worker.queued_bytes == 0)
    first = future.result()
    before = worker.stats['uploads'],worker.stats['draws']
    same = worker.submit_segment(pixels,[stage],source_key='pinned',canonical_input=True)
    wait_until(qapp,lambda: same.done() and worker.queued_bytes == 0)
    second = same.result()
    assert (worker.stats['uploads'],worker.stats['draws']) == before
    assert worker.stats['readbacks'] == 0
    expected = apply_modifier_stack(source_image(257,273),effects(),(0,0),_point_lut=False,return_pixels=True)
    np.testing.assert_array_equal(materialize(qapp,first),expected)
    np.testing.assert_array_equal(materialize(qapp,second),expected)
    with pytest.raises(RuntimeError,match='application thread'):
        first.materialize_pixels()
    first.release()
    assert first.isNull() and not second.isNull()
    second.release()


def test_device_inputs_continue_on_owner_and_reject_noncanonical_point_boundaries(worker,qapp):
    pixels = _qimage_premultiplied(source_image(53,37))
    key = signature(effects())
    table = PointTableStage(compile_point_table(key),key)
    first = worker.submit_segment(pixels,[table],source_key='float-point',canonical_input=True)
    wait_until(qapp,first.done)
    image = first.result()
    assert not image.canonical
    declined = worker.submit_segment(image,[table],source_key='bad-rounded-prefix')
    wait_until(qapp,declined.done)
    assert declined.result() is None
    next_image = worker.submit_segment(image,[ScalarBlurStage(7),table,ByteQuantizeStage()],source_key='downstream')
    wait_until(qapp,lambda: next_image.done() and worker.queued_bytes == 0)
    result = next_image.result()
    assert isinstance(result,DeviceImage) and result.canonical
    assert worker.stats['readbacks'] == 0
    image.release()
    result.release()


def test_device_crop_is_exact_and_keeps_base_lease(worker,qapp):
    source = source_image(53,37)
    future = worker.submit_segment(_qimage_premultiplied(source),[ByteQuantizeStage()],
                                   source_key='crop',canonical_input=True)
    wait_until(qapp,future.done)
    image = future.result()
    view = image.copy(QRect(7,9,13,11)).copy(QRect(2,3,8,5))
    assert view.source_origin == (9,12) and view.texture_width == 53 and view.texture_height == 37
    assert view.sizeInBytes() == 8*5*16 and worker.stats['readbacks'] == 0
    np.testing.assert_array_equal(materialize(qapp,view),_qimage_premultiplied(source)[12:17,9:17])
    view.release()
    assert not image.isNull()
    image.release()


def test_releasing_base_cannot_expire_a_retained_view_or_queued_device_input(worker,qapp):
    source = source_image(53,37)
    submitted = worker.submit_segment(_qimage_premultiplied(source),[ByteQuantizeStage()],
        source_key='view-lease',canonical_input=True)
    wait_until(qapp,submitted.done)
    base = submitted.result()
    view = base.copy(QRect(7,9,13,11))
    base.release()
    assert not view.isNull()
    continued = worker.submit_segment(view,[ByteQuantizeStage()],source_key='view-consumer')
    view.release()
    wait_until(qapp,lambda: continued.done() and worker.queued_bytes == 0)
    result = continued.result()
    assert result.size() == QRect(0,0,13,11).size() and worker.stats['readbacks'] == 0
    np.testing.assert_array_equal(materialize(qapp,result),_qimage_premultiplied(source)[9:20,7:20])
    result.release()


def test_resident_stack_keeps_unsupported_masks_on_reference_path(worker,qapp):
    source,modifiers = source_image(),effects()
    modifiers[0].parameter_masks['brightness'] = ParameterMaskBinding('mask',0,100)
    assert device_modifier_stack(source,modifiers,worker=worker) is None
    assert not worker.queue


def test_native_shared_texture_presentation_uses_fence_and_no_readback(qapp):
    from test_document_presentation import gl_context, framebuffer, pixels
    from comic_editor.ui.document_presentation import GpuTilePresenter, PresentedTile
    from PySide6.QtGui import QOffscreenSurface, QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setVersion(4,3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    context = QOpenGLContext()
    context.setFormat(fmt)
    if not context.create():
        pytest.skip('No native shared OpenGL context')
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    assert context.makeCurrent(surface)
    worker = GpuWorker(share_context=context)
    presenter = GpuTilePresenter()
    try:
        wait_until(qapp,worker.ready.is_set)
        if not worker.available:
            pytest.skip(worker.reason)
        source = source_image(16,16)
        submitted = worker.submit_segment(_qimage_premultiplied(source),[ByteQuantizeStage()],
                                          source_key='shared',canonical_input=True)
        wait_until(qapp,lambda: submitted.done() and worker.queued_bytes == 0)
        image = submitted.result()
        context.makeCurrent(surface)
        presenter._initialize()
        assert QOpenGLContext.areSharing(context,worker.render_context)
        from comic_editor.render.gpu.sync import GlSync
        wait_until(qapp,lambda: GlSync(context).ready(image.fence))
        assert presenter.functions.glIsTexture(image.texture)
        target = framebuffer(presenter,32,32)
        tile = PresentedTile('native-device',image.copy(QRect(3,4,8,7)),QRectF(5,6,8,7))
        wait_until(qapp,lambda: presenter.draw([tile],QTransform(),QSizeF(32,32),smooth=False))
        actual = pixels(target.toImage())
        np.testing.assert_array_equal(actual[6:13,5:13],np.rint(_qimage_premultiplied(source)[4:11,3:11]*255).astype(np.uint8))
        assert presenter.uploads == 0 and worker.stats['readbacks'] == 0
        borrowed = presenter._textures[presenter._key(tile)].texture.image
        assert borrowed is not tile.image and borrowed.source_origin == tile.image.source_origin
        tile.image.release()
        assert not borrowed.isNull()
        target.release()
        del target
        presenter.close()
        assert borrowed.isNull()
        image.release()
    finally:
        worker.close()
        assert worker.worker_thread.service is None and worker.share_context is None
        context.doneCurrent()
        surface.destroy()


@pytest.mark.parametrize('kind',['pattern','cage','blend'])
def test_legacy_helper_dispatch_uses_exact_shader_on_one_graphics_owner(worker,qapp,kind,monkeypatch):
    from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer
    from comic_editor.ui.gpu_textures import GpuTextureRenderer
    from comic_editor.ui.gpu_object_blending import GpuObjectBlendRenderer
    from comic_editor.core.models import PixelateModifier,CageTransformModifier
    from PySide6.QtGui import QImage
    from PySide6.QtCore import QThread
    source = source_image(53,37)
    if kind == 'pattern':
        constructor,method = GpuPatternRenderer,'render'
        args = (source,PixelateModifier(pixel_size=4))
    elif kind == 'cage':
        constructor,method = GpuTextureRenderer,'cage'
        grid = CageTransformModifier()
        grid.frame = (0.,0.,53.,37.)
        args = (source,QRectF(0,0,53,37),grid,QTransform(),QRectF(0,0,53,37))
    else:
        constructor,method = GpuObjectBlendRenderer,'composite'
        args = (source,source,'linear_burn',QRect(0,0,53,37),1.)
    reference = constructor()
    if not reference.available:
        reference.close()
        pytest.skip(reference.reason)
    try:
        expected = getattr(reference,method)(*args)
    finally:
        reference.close()
    owners = []
    original = getattr(constructor,method)
    def observed(renderer,*args,**kwargs):
        owners.append(threading.get_ident())
        assert QThread.currentThread() == renderer.context.thread()
        assert renderer.surface is worker.surface
        return original(renderer,*args,**kwargs)
    monkeypatch.setattr(constructor,method,observed)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.helper,kind,method,args,{})
        wait_until(qapp,lambda: future.done() and worker.queued_bytes == 0)
        actual = future.result()
    assert isinstance(actual,QImage) and actual == expected
    assert owners and owners[0] != threading.get_ident()


@pytest.mark.parametrize('source_kind,blur',[('image',False),('raster',False),('image',True)])
def test_detached_document_device_result_matches_scene_and_presents_without_transfer(qapp,monkeypatch,source_kind,blur):
    from comic_editor.core.images import ImageStore
    from comic_editor.core.models import BoundGeometry, ChapterDocument, ChildRef, ImageObject, RasterObject
    from comic_editor.core.settings import EditorSettings
    from comic_editor.core.tiles import TileStore
    from comic_editor.render.scene import SceneSnapshotCompiler, DetachedSceneBackend
    from comic_editor.render.service import DocumentRenderService, RenderRequest
    from comic_editor.ui.canvas import CanvasWidget
    from comic_editor.ui.document_presentation import GpuTilePresenter, PresentedTile
    from test_document_presentation import framebuffer, pixels
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QOffscreenSurface,QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setVersion(4,3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    context = QOpenGLContext()
    context.setFormat(fmt)
    if not context.create():
        pytest.skip('No native shared OpenGL context')
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    assert context.makeCurrent(surface)
    graphics = GpuWorker(share_context=context)
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager',lambda _: None)
    owner = CanvasWidget(EditorSettings(canvas_renderer='auto'))
    presenter = GpuTilePresenter()
    try:
        wait_until(qapp,graphics.ready.is_set)
        if not graphics.available:
            pytest.skip(graphics.reason)
        chapter = ChapterDocument(width=128,height=128,document_kind='asset')
        page = chapter.add_page('Page',BoundGeometry.rectangle(0,0,128,128))
        page.shape_style.outline_thickness = 0
        source = source_image(128,128)
        image_store,tile_store = ImageStore(),TileStore(tile_size=128)
        if source_kind == 'image':
            obj = ImageObject(parent_layer_id=page.layer_id,pixel_width=128,pixel_height=128)
            raw = QByteArray()
            buffer = QBuffer(raw)
            buffer.open(QIODevice.WriteOnly)
            assert source.save(buffer,'PNG')
            image_store.put_decoded(obj.object_id,'source.png',bytes(raw),source)
        else:
            obj = RasterObject(parent_layer_id=page.layer_id,tile_size=128,interaction_rect=(0,0,128,128))
            tile_store.set_tile(obj.object_id,(0,0),source)
        chapter.objects[obj.object_id] = obj
        page.children.append(ChildRef('object',obj.object_id))
        modifiers = [*effects(), *([BlurModifier(strength=7.25),*effects()] if blur else [])]
        for effect in modifiers:
            chapter.modifiers[effect.modifier_id] = effect
            obj.modifier_ids.append(effect.modifier_id)
        owner.set_document(chapter,tile_store,image_store)
        owner._graphics_worker = graphics
        capture = SceneSnapshotCompiler().capture(owner,owner._render_document_state())
        while not capture.advance(.001):
            pass
        snapshot = capture.result
        assert snapshot is not None
        request = RenderRequest((8.,9.,44.,44.),1.,(44,44),('device-scene',),snapshot.document.revision)
        reference = owner._render_service.render_region(snapshot.document,request)
        def evaluate():
            backend = DetachedSceneBackend(snapshot)
            service = DocumentRenderService(backend)
            service.projection.revision = snapshot.document.revision
            return service,service.render_region(snapshot.document,request)
        with ThreadPoolExecutor(1) as pool:
            job = pool.submit(evaluate)
            wait_until(qapp,job.done)
            service,result = job.result()
        assert result.exact and isinstance(result.image,DeviceImage)
        assert graphics.stats['readbacks'] == 0
        context.makeCurrent(surface)
        presenter._initialize()
        target = framebuffer(presenter,44,44)
        tile = PresentedTile(request.key,result.image,QRectF(0,0,44,44))
        wait_until(qapp,lambda: presenter.draw([tile],QTransform(),QSizeF(44,44),smooth=False))
        np.testing.assert_array_equal(pixels(target.toImage()),pixels(reference.image))
        assert presenter.uploads == 0 and graphics.stats['readbacks'] == 0
        target.release()
        del target
        presenter.close()
        # Revision/configuration guard a final result; they do not change this
        # immutable source frame or require its graph to upload again.
        from dataclasses import replace
        uploads = graphics.stats['uploads']
        next_document = replace(snapshot.document,revision=snapshot.document.revision+1,
            configuration=(*snapshot.document.configuration,'unrelated-label-change'))
        next_snapshot = replace(snapshot,document=next_document)
        next_request = replace(request,revision=next_document.revision)
        def evaluate_next():
            backend = DetachedSceneBackend(next_snapshot)
            service = DocumentRenderService(backend)
            service.projection.revision = next_document.revision
            try:
                return service.render_region(next_document,next_request)
            finally:
                backend.close()
        with ThreadPoolExecutor(1) as pool:
            job = pool.submit(evaluate_next)
            wait_until(qapp,job.done)
            unchanged = job.result()
        assert unchanged.exact and isinstance(unchanged.image,DeviceImage)
        assert unchanged.document == next_document and unchanged.request == next_request
        assert graphics.stats['uploads'] == uploads and graphics.stats['readbacks'] == 0
        unchanged.image.release()
        # A genuine raster consumer queues the CPU edge on the same detached
        # lane. The GUI still performs no device readback or conversion wait.
        from comic_editor.render.projection import ProjectionRequest,ProjectionAddress
        from comic_editor.ui.document_presentation import draw_document_tiles
        from PySide6.QtGui import QImage,QPainter
        projection = owner._document_projection
        projection.tile_size = 40
        projection.configure((*snapshot.document.configuration,None),document=snapshot.document.identity)
        projection.revision = snapshot.document.revision
        address = ProjectionAddress(0,0,0)
        projected = ProjectionRequest(address,40,2)
        assert projection.adopt(projected,result.image,configuration=(*snapshot.document.configuration,None),
            document=snapshot.document.identity,revision=snapshot.document.revision)
        controller = owner._scene_controller
        controller.snapshot = snapshot
        controller.serial = 11
        controller.desired = snapshot.document,(address,),(None,),(0.,0.,40.,40.)
        controller.dispatched = controller.desired
        owner._projection_completed_view = snapshot.document.configuration,[(None,[tile])],snapshot.document.revision
        owner._projection_progress_view = owner._projection_completed_view
        target_cpu = QImage(44,44,QImage.Format_ARGB32_Premultiplied)
        materialization_threads = []
        original_materialize = DeviceImage.materialize
        gui_thread = threading.get_ident()
        def observed_materialize(image):
            materialization_threads.append(threading.get_ident())
            assert materialization_threads[-1] != gui_thread
            return original_materialize(image)
        monkeypatch.setattr(DeviceImage,'materialize',observed_materialize)
        painter = QPainter(target_cpu)
        try:
            pending = draw_document_tiles(painter,[tile],QTransform(),QSizeF(44,44),owner=owner)
            assert pending.backend == 'pending'
        finally:
            painter.end()
        wait_until(qapp,lambda:not isinstance(projection.tiles[address].image,DeviceImage))
        assert projection.tiles[address].valid and projection.tiles[address].revision == snapshot.document.revision
        assert projection.tiles[address].image == reference.image
        assert not isinstance(owner._projection_completed_view[1][0][1][0].image,DeviceImage)
        assert not isinstance(owner._projection_progress_view[1][0][1][0].image,DeviceImage)
        assert graphics.stats['readbacks'] == 1
        assert materialization_threads
        graphics.close()
        assert result.image.isNull()
        with ThreadPoolExecutor(1) as pool:
            fallback = pool.submit(service.render_region,snapshot.document,request).result(20)
        assert fallback.exact and not isinstance(fallback.image,DeviceImage)
        np.testing.assert_array_equal(pixels(fallback.image),pixels(reference.image))
    finally:
        presenter.close()
        graphics.close()
        owner._scene_controller.reset()
        owner._scene_controller.scheduler.close()
        owner._effect_jobs.cancel()
        owner._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
        owner.deleteLater()
        context.doneCurrent()
        surface.destroy()


@pytest.mark.parametrize('spatial',[False,True])
def test_production_point_blur_point_cpu_consumer_materializes_once(qapp,monkeypatch,spatial):
    from comic_editor.core.models import BoundGeometry,ChapterDocument
    from comic_editor.core.settings import EditorSettings
    from comic_editor.core.tiles import TileStore
    from comic_editor.render.scene import SceneSnapshotCompiler,DetachedSceneBackend
    from comic_editor.render.effect_pipeline import render_stages
    from comic_editor.ui.canvas import CanvasWidget
    from comic_editor.ui.point_lut import graphics_scope
    source = source_image(1024,1024)
    modifiers = [*effects(),BlurModifier(strength=7.25),*effects('soft_light')]
    graphics = GpuWorker(gpu_budget=96*1024*1024,queue_budget=64*1024*1024)
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager',lambda _:None)
    owner = CanvasWidget(EditorSettings(canvas_renderer='auto'))
    try:
        wait_until(qapp,graphics.ready.is_set)
        if not graphics.available:
            pytest.skip(graphics.reason)
        chapter = ChapterDocument(width=1024,height=1024,document_kind='asset')
        page = chapter.add_page('Page',BoundGeometry.rectangle(0,0,1024,1024))
        page.shape_style.outline_thickness = 0
        for modifier in modifiers:
            chapter.modifiers[modifier.modifier_id] = modifier
        owner.set_document(chapter,TileStore())
        owner._graphics_worker = graphics
        owner._projection_exact = True
        frame = QRectF(-13,-9,1024,1024)
        if spatial:
            expected,expected_frame = render_stages(owner,source,frame,modifiers,QTransform())
        else:
            expected = apply_modifier_stack(source,modifiers,(frame.x(),frame.y()),_point_lut=False)
        capture = SceneSnapshotCompiler().capture(owner,owner._render_document_state())
        while not capture.advance(.001):
            pass
        def evaluate():
            scene = DetachedSceneBackend(capture.result).scene
            with graphics_scope(graphics):
                return (render_stages(scene,source,frame,modifiers,QTransform()) if spatial else
                    apply_modifier_stack(source,modifiers,(frame.x(),frame.y())))
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(evaluate)
            wait_until(qapp,lambda: result.done() and graphics.queued_bytes == 0,timeout=30)
            actual = result.result()
        if spatial:
            actual,actual_frame = actual
            assert actual_frame == expected_frame
        assert actual == expected
        assert graphics.stats['readbacks'] == 1
    finally:
        graphics.close()
        owner._scene_controller.reset()
        owner._scene_controller.scheduler.close()
        owner._effect_jobs.cancel()
        owner._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
        owner.deleteLater()
