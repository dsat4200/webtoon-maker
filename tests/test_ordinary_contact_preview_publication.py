"""Unrun artifact source: real native-contact revision/publication boundary.

Historical control expects current cancellation. Candidate assertion expects a
same-gesture older immutable preview to be usable while latest work is pending.
Do not run both expectations on one implementation. Worker barriers establish
event order only; this is neither a measured latency nor a full MainWindow test.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event, get_ident
import time

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BlurModifier, BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.tools import ToolKind
from comic_editor.render.outputs import capture_document
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.scheduler import SceneCompletion, SceneScheduler
from comic_editor.render.service import DocumentRenderService, RenderQuality, RenderRequest, RenderStatus
from comic_editor.ui.canvas import CanvasWidget
from test_raster_contact_feedback import ready_paint


def native(image):
    return (image.size().toTuple(), image.format().value, image.bytesPerLine(),
            image.devicePixelRatio(), bytes(image.colorSpace().iccProfile()), bytes(image.constBits()))


def source(canvas, identifier):
    return {key: native(image) for key, image in canvas.tiles.object_tiles(identifier).items()}


def wait(canvas, qapp, predicate, timeout=20.):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Real contact/worker boundary did not arrive'
        ready_paint(canvas)
        qapp.processEvents()
        assert not canvas._scene_controller.error, canvas._scene_controller.error
        time.sleep(.001)


def oracle(snapshot, request):
    snapshot = replace(snapshot, document=replace(snapshot.document, live_preview=False, contact_only=False),
                       cache_spec=None, graphics_worker=None)
    request = replace(request, quality=RenderQuality.EXACT)
    backend = DetachedSceneBackend(snapshot)
    service = DocumentRenderService(backend)
    service.projection.revision = snapshot.document.revision
    try:
        assert backend.scene._persistent_render_cache is None
        result = service.render_region(snapshot.document, request)
        assert result.status is RenderStatus.EXACT and result.exact
        assert backend.scene._vector_render_scale_override == 1.
        return native(result.image)
    finally:
        backend.close()


def close(canvas, releases):
    for event in releases:
        event.set()
    canvas._clear_detached_input_state()
    for scheduler in (canvas._scene_controller.scheduler, canvas._scene_controller.bootstrap_scheduler):
        scheduler.close()
        scheduler.executor.shutdown(wait=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas._visual_frame_timer.stop()
    canvas.deleteLater()


def fixture(monkeypatch, modifier_target='object', tool=ToolKind.RASTER_PENCIL):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', predictive_ink=False,
                         grid_overlay_visible=False, snap_to_grid=False))
    try:
        canvas.setUpdatesEnabled(False)
        canvas.setFixedSize(256, 256)
        chapter = ChapterDocument(width=256, height=256, document_kind='asset', background='#00000000')
        page = chapter.add_page('Page', BoundGeometry.rectangle(0., 0., 256., 256.))
        page.fill_color, page.border_width = None, 0
        obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0., 0., 256., 256.)))
        if modifier_target == 'object':
            # Twelve intentionally stays outside the proposed bounded Blur<=8 lane.
            chapter.add_modifier(BlurModifier(strength=12., algorithm='normal', mode='full'),
                                 [('object', obj.object_id)])
        else:
            parent = chapter.add_layer(page.layer_id, 'Parent Blur',
                bound=BoundGeometry.rectangle(0., 0., 256., 256.))
            parent.fill_color, parent.border_width = None, 0
            chapter.move_entity('object', obj.object_id, parent.layer_id, 0)
            chapter.add_modifier(BlurModifier(strength=8., algorithm='normal', mode='full'),
                                 [('layer', parent.layer_id)])
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('#44553322'))
        painter = QPainter(image)
        painter.setBrush(QColor('#80779922'))
        painter.drawEllipse(24, 24, 190, 190)
        painter.end()
        tiles = TileStore()
        tiles.set_tile(obj.object_id, (0, 0), image)
        canvas.set_document(chapter, tiles, ImageStore())
        canvas.center_x = canvas.center_y = 128.
        canvas.scale = 1.
        canvas.primary_color = '#ffff2299'
        canvas.set_selection('object', obj.object_id)
        canvas.set_tool(tool)
        return canvas, obj
    except BaseException:
        close(canvas, ())
        raise


def exercise(qapp, monkeypatch, *, expect_cancel, modifier_target='object', tool=ToolKind.RASTER_PENCIL):
    canvas, obj = fixture(monkeypatch, modifier_target, tool)
    first_done, second_done = Event(), Event()
    first_release, second_release = Event(), Event()
    observed = []
    gui_thread = get_ident()
    original_region = DocumentRenderService.render_region
    original_source = source(canvas, obj.object_id)
    history = canvas.command_stack.revision
    armed = True
    tokens = {}
    owned_scheduler = canvas._scene_controller.scheduler
    original_evaluate = SceneScheduler._evaluate

    def evaluate(scheduler, demand, token):
        # Observe only the actual immutable demand and its event token. The
        # worker receives no QWidget or mutable live document input from us.
        if scheduler is owned_scheduler:
            tokens[demand.serial] = token
        return original_evaluate(scheduler, demand, token)

    monkeypatch.setattr(SceneScheduler, '_evaluate', evaluate)

    def region(service, document, request, *args, **kwargs):
        result = original_region(service, document, request, *args, **kwargs)
        if (armed and document.live_preview and document.contact_only
                and request.key[:1] == ('detached-preview',) and len(observed) < 2):
            assert get_ident() != gui_thread
            assert service.backend.native_preview and service.backend.artwork_scale == 1.
            assert service.backend.scene._live_canvas_preview_policy is None
            assert result.status is RenderStatus.PROVISIONAL and not result.exact
            assert not result.image.isNull() and result.image.format() == document.pixel_contract.image_format
            assert request.revision == document.revision and service.backend.snapshot.document == document
            index = len(observed)
            observed.append((document, request, service.backend.snapshot,
                             native(result.image), tokens[request.key[1]]))
            (first_done if index == 0 else second_done).set()
            assert (first_release if index == 0 else second_release).wait(20.)
        return result

    monkeypatch.setattr(DocumentRenderService, 'render_region', region)
    try:
        canvas._begin_stroke(QPointF(60., 96.), 1.)
        wait(canvas, qapp, lambda: canvas._raster_tile_input is not None and not canvas._raster_tile_input.busy)
        document = canvas._render_document_state()
        assert document.live_preview and document.contact_only
        armed = True
        wait(canvas, qapp, first_done.is_set)
        first_document, first_request, first_snapshot, first_pixels, first_token = observed[0]
        assert not first_token.is_set()

        canvas._continue_stroke(QPointF(170., 112.), 1.)
        # This targeted race uses the same already-resident native source tile.
        # Cold-input priority cancellation is a separate required regression.
        assert not canvas._raster_tile_input.busy
        wait(canvas, qapp, lambda: not canvas._raster_tile_input.busy
             and canvas._render_document_state().revision > first_document.revision)
        latest = canvas._render_document_state()
        ready_paint(canvas)
        controller = canvas._scene_controller
        assert controller.desired[0] == latest and canvas._drawing
        assert latest.identity == first_document.identity
        assert latest.configuration == first_document.configuration
        assert first_token.is_set() is expect_cancel
        first_release.set()
        wait(canvas, qapp, second_done.is_set)
        preview = controller.preview
        if expect_cancel:
            assert preview is None or preview[0] != first_document
        else:
            wait(canvas, qapp, lambda: controller.preview is not None
                 and controller.preview[0] == first_document)
            ready_paint(canvas)
            assert canvas._projection_provisional_visible and canvas._projection_frame_pending
            assert native(controller.preview[1].image) == first_pixels
            assert controller.preview[0].revision < latest.revision
        # Re-render the older immutable source independently after the barrier;
        # a later live native packet must not alter its model or source pixels.
        second_release.set()
        wait(canvas, qapp, lambda: controller.preview is not None and controller.preview[0] == latest)
        final_preview = native(controller.preview[1].image)
        with ThreadPoolExecutor(1) as worker:
            assert worker.submit(oracle, first_snapshot, first_request).result(20.) == first_pixels
            fresh = capture_document(canvas.chapter, canvas.tiles, canvas.images)
            final_request = RenderRequest((0., 0., 256., 256.), 1., (256, 256),
                                          ('fresh-native-contact-oracle',), fresh.document.revision)
            assert worker.submit(oracle, fresh, final_request).result(20.) == final_preview
        assert canvas._drawing and canvas._raster_tile_input is not None
        assert not canvas._raster_feedback_contact_covered
        assert canvas.command_stack.revision == history
        changed = source(canvas, obj.object_id)
        assert changed != original_source
        canvas._end_stroke()
        wait(canvas, qapp, lambda: canvas._raster_tile_input is None and not canvas._drawing)
        assert canvas.command_stack.revision == history + 1
        canvas.command_stack.undo()
        assert source(canvas, obj.object_id) == original_source
        canvas.command_stack.redo()
        assert source(canvas, obj.object_id) == changed
    finally:
        close(canvas, (first_release, second_release))


@pytest.mark.parametrize('modifier_target', ['object', 'layer'])
@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
def test_candidate_same_contact_immutable_preview_progresses_before_latest_finishes(qapp, monkeypatch, modifier_target, tool):
    exercise(qapp, monkeypatch, expect_cancel=False, modifier_target=modifier_target, tool=tool)


@pytest.mark.parametrize('transition', ['view', 'selection', 'tool', 'history', 'unrelated_artwork', 'modifier'])
def test_candidate_owner_retires_for_other_dependencies_and_rejects_old_epoch(qapp, monkeypatch, transition):
    from comic_editor.core.changes import ChangeSet, EntityChange
    canvas, obj = fixture(monkeypatch)
    produced, release = Event(), Event()
    actual_demands, completions = {}, []
    scheduler = canvas._scene_controller.scheduler
    original_evaluate = SceneScheduler._evaluate
    original_region = DocumentRenderService.render_region

    def evaluate(owner, demand, token):
        if owner is scheduler:
            actual_demands[demand.serial] = demand, token
        return original_evaluate(owner, demand, token)

    def region(service, document, request, *args, **kwargs):
        result = original_region(service, document, request, *args, **kwargs)
        if (document.live_preview and document.contact_only
                and request.key[:1] == ('detached-preview',) and not completions):
            demand, token = actual_demands[request.key[1]]
            completions.append((SceneCompletion(demand, done=True, preview=result), token))
            produced.set()
            assert release.wait(20.)
        return result

    monkeypatch.setattr(SceneScheduler, '_evaluate', evaluate)
    monkeypatch.setattr(DocumentRenderService, 'render_region', region)
    try:
        canvas._begin_stroke(QPointF(60., 96.), 1.)
        wait(canvas, qapp, lambda: canvas._raster_tile_input is not None and not canvas._raster_tile_input.busy)
        ready_paint(canvas)
        wait(canvas, qapp, produced.is_set)
        controller = canvas._scene_controller
        old_owner = controller._contact_preview_owner
        assert old_owner is not None
        generation = controller._contact_preview_dependency_generation
        canvas._continue_stroke(QPointF(100., 104.), 1.)
        assert not canvas._raster_tile_input.busy
        ready_paint(canvas)
        assert controller._contact_preview_owner == old_owner
        assert controller._contact_preview_dependency_generation == generation
        old_completion, old_token = completions[0]
        assert old_completion.demand.contact_preview_owner == old_owner and not old_token.is_set()
        document, addresses, phases, visible = controller.desired
        if transition == 'view':
            canvas.rotation = 13.
        elif transition == 'selection':
            canvas.set_selection('layer', canvas.chapter.root_page_ids[0])
        elif transition == 'tool':
            canvas.set_tool(ToolKind.TRANSFORM)
        elif transition == 'history':
            controller.changed(ChangeSet(), action='undo')
        elif transition == 'unrelated_artwork':
            page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
            page.opacity = .54
            canvas._publish_change_set(ChangeSet((EntityChange(('layer', page.layer_id),
                frozenset({'opacity'})),)), action='transient')
        else:
            modifier = canvas.chapter.modifiers[obj.modifier_ids[0]]
            modifier.strength = 13.
            canvas._publish_change_set(ChangeSet((EntityChange(('modifier', modifier.modifier_id),
                frozenset({'strength'})),)), action='transient')
        # Positive live/current owner or typed epoch is lost. A late completion
        # from old_owner cannot meet _accept_contact_preview's equality guard.
        assert (controller._contact_preview_owner != old_owner or
                controller._contact_preview_key_for(document, addresses, phases, visible)
                    != controller._contact_preview_key)
        current = canvas._render_document_state()
        assert not controller._accept_contact_preview(old_completion, current, addresses, phases, visible)
        release.set()
        ready_paint(canvas)
        assert controller._contact_preview_owner != old_owner
    finally:
        close(canvas, (release,))
