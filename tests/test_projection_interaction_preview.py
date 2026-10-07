"""Committed feedback stays current while exact publication is blocked."""
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import BrightnessContrastModifier, OutlineModifier, ParameterMaskBinding, RasterObject, ToneMask
from comic_editor.core.commands import CallbackCommand
from comic_editor.render.service import RenderRequest, RenderQuality, RenderStatus
from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from comic_editor.ui.cache_dependencies import exact_cache_allowed
from comic_editor.ui.modifier_controls import ModifierControls
from test_projection_invalidation import scene


def paint(canvas, *, interactive=True, live_ink=False):
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#242428'))
    painter = QPainter(image)
    try:
        canvas._paint_document_projection(painter, interactive=interactive, live_ink=live_ink)
    finally:
        painter.end()
    return image


def warm(canvas):
    canvas._projection_async_enabled = False
    paint(canvas, interactive=False)
    canvas._projection_async_enabled = True
    return canvas._projection_completed_view


def block_exact(canvas, monkeypatch, *, failure=False):
    original = canvas._render_scene_layers
    def capture(*args, **kwargs):
        if canvas._projection_exact:
            assert canvas._projection_defer_effects, 'Committed widget must not run exact effects inline'
            if failure:
                raise ProjectionFailed('blocked exact failure')
            raise ProjectionPending('blocked-exact', 'key')
        return original(*args, **kwargs)
    monkeypatch.setattr(canvas, '_render_scene_layers', capture)
    return original


def oracle(canvas, preview):
    document = canvas._render_document_state()
    request = RenderRequest(tuple(preview.coverage.getRect()), preview.density,
                            (preview.tile.image.width(), preview.tile.image.height()),
                            ('oracle',), document.revision, quality=RenderQuality.INTERACTIVE)
    return canvas._render_service.render_region(document, request).image


def test_release_publishes_fresh_committed_model_and_local_patch_without_exact_cache(scene, monkeypatch):
    canvas, obj, _ = scene
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    completed = warm(canvas)
    controls = ModifierControls(canvas)
    controls.begin_parameter_drag(modifier.modifier_id)
    controls.set_parameter(modifier.modifier_id, 'brightness', 40, False)
    # A gesture frame cannot be retained as if it were the committed model.
    paint(canvas)
    assert canvas._projection_interaction_preview is None
    controls.finish_parameter_drag()
    # Simulate final model normalization after the last drag frame.
    modifier.brightness = 55
    before_tiles = {key: tile.image.cacheKey() for key, tile in canvas._document_projection.tiles.items()}
    original = block_exact(canvas, monkeypatch)
    seen, allowed = [], []
    render = canvas._render_service.render_region
    def traced(document, request):
        if request.key == ('interaction-preview',):
            seen.append(request)
        result = render(document, request)
        return result
    monkeypatch.setattr(canvas._render_service, 'render_region', traced)
    backend = canvas._render_service.backend
    original_paint = backend.paint
    def cache_guard(*args, **kwargs):
        if not canvas._projection_exact:
            allowed.append(exact_cache_allowed(canvas, ('test',)))
        return original_paint(*args, **kwargs)
    monkeypatch.setattr(backend, 'paint', cache_guard)
    paint(canvas)
    preview = canvas._projection_interaction_preview
    assert preview is not None and preview.revision == canvas._document_projection.revision
    assert preview.tile.image == oracle(canvas, preview)
    assert seen and all(request.quality is RenderQuality.INTERACTIVE for request in seen)
    assert seen[0].bounds.height() < preview.coverage.height(), 'Proven local edit should patch the exact seed'
    assert allowed and not any(allowed)
    assert canvas._projection_frame_pending
    assert canvas._projection_completed_view is completed
    assert canvas._projection_presented_revision == completed[2]
    assert all(tile.image.cacheKey() == before_tiles[key] for key, tile in canvas._document_projection.tiles.items())
    monkeypatch.setattr(canvas, '_render_scene_layers', original)
    # Detached exact/export captures cannot consume the provisional frame.
    paint(canvas, interactive=False)
    assert not canvas._projection_frame_pending
    assert canvas._projection_interaction_preview is None
    assert canvas._projection_presented_revision == canvas._document_projection.revision
    controls.deleteLater()


@pytest.mark.parametrize('change', ['second_edit', 'undo', 'camera', 'zoom', 'configuration', 'pixels'])
def test_guard_rejects_stale_commit_feedback(scene, monkeypatch, change):
    canvas, obj, _ = scene
    warm(canvas)
    obj.x += 70
    canvas.documentChanged.emit(None)
    assert canvas._capture_interaction_projection_preview()
    previous = canvas._projection_interaction_preview
    if change == 'second_edit':
        obj.x += 40
        canvas.documentChanged.emit(None)
    elif change == 'undo':
        canvas._history_generation = getattr(canvas, '_history_generation', 0) + 1
    elif change == 'camera':
        canvas.center_x += 300
    elif change == 'zoom':
        canvas.scale = .5
    elif change == 'configuration':
        canvas._solo_entities.add(('object', obj.object_id))
    else:
        from comic_editor.render.pixels import FLOAT_PIXELS
        canvas.chapter.pixel_contract = FLOAT_PIXELS
    assert canvas._current_interaction_projection_preview() is None
    assert canvas._projection_interaction_preview is None
    if change in ('second_edit', 'camera', 'zoom'):
        assert canvas._capture_interaction_projection_preview()
        assert canvas._projection_interaction_preview is not previous


@pytest.mark.parametrize('configuration', ['solo', 'mask_only', 'underlay'])
def test_visibility_configuration_switch_has_fresh_feedback_and_exact_roundtrip(scene, monkeypatch, configuration):
    canvas, obj, group = scene
    canvas.chapter.add_modifier(OutlineModifier(thickness=3, blur_radius=4, blur_strength=50),
                                [('object', obj.object_id)])
    other = canvas.chapter.add_object(group.layer_id, RasterObject())
    canvas.tiles.paint_dab(other.object_id, QPointF(900, 180), 25, QColor('blue'))
    old = warm(canvas)
    before = paint(canvas, interactive=False)
    old_config = canvas._projection_configuration()
    if configuration == 'solo':
        canvas.set_solo_entities([('object', obj.object_id)])
    elif configuration == 'mask_only':
        obj.mask_only = True
        canvas.documentChanged.emit(None)
    else:
        obj.underlay_opacity = .5
        canvas.documentChanged.emit(None)
    assert canvas._projection_configuration() != old_config
    original = block_exact(canvas, monkeypatch)
    paint(canvas)
    preview = canvas._projection_interaction_preview
    assert preview is not None and preview.configuration == canvas._projection_configuration()
    assert canvas._projection_frame_pending
    assert canvas._projection_completed_view is None
    assert canvas._projection_presented_revision == old[2]
    # The changed configuration retired the exact seed. Another edit must
    # capture fresh current-model feedback rather than forcing exact inline.
    obj.x += 5
    canvas.documentChanged.emit(None)
    paint(canvas)
    assert canvas._projection_interaction_preview is not preview
    assert canvas._projection_interaction_preview.revision == canvas._document_projection.revision
    monkeypatch.setattr(canvas, '_render_scene_layers', original)
    after = paint(canvas, interactive=False)
    canvas._render_service.invalidate()
    assert paint(canvas, interactive=False) == after
    obj.x -= 5
    if configuration == 'solo':
        canvas.set_solo_entities([])
    elif configuration == 'mask_only':
        obj.mask_only = False
    else:
        obj.underlay_opacity = 0
    canvas.documentChanged.emit(None)
    assert paint(canvas, interactive=False) == before


def test_reentrant_edit_rejects_capture_and_keeps_exact_work_deferred(scene, monkeypatch):
    canvas, obj, _ = scene
    completed = warm(canvas)
    obj.x += 70
    canvas.documentChanged.emit(None)
    original = canvas._render_service.render_region
    def capture(document, request):
        result = original(document, request)
        obj.x += 10
        canvas.documentChanged.emit(None)
        return result
    monkeypatch.setattr(canvas._render_service, 'render_region', capture)
    assert not canvas._capture_interaction_projection_preview()
    assert canvas._projection_interaction_preview is None
    assert canvas._projection_can_defer_effects()
    assert canvas._projection_presented_revision == completed[2]
    assert completed[2] != canvas._document_projection.revision
    monkeypatch.setattr(canvas._render_service, 'render_region', original)
    block_exact(canvas, monkeypatch)
    paint(canvas)
    preview = canvas._projection_interaction_preview
    assert preview is not None and preview.tile.image == oracle(canvas, preview)
    assert canvas._projection_frame_pending
    assert canvas._projection_completed_view is completed
    assert canvas._projection_presented_revision == completed[2]


def test_actual_undo_discards_committed_preview_before_restoring_pixels(scene, monkeypatch):
    canvas, obj, _ = scene
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    completed = warm(canvas)
    controls = ModifierControls(canvas)
    controls.begin_parameter_drag(modifier.modifier_id)
    controls.set_parameter(modifier.modifier_id, 'brightness', 60, False)
    controls.finish_parameter_drag()
    original = block_exact(canvas, monkeypatch)
    paint(canvas)
    preview = canvas._projection_interaction_preview
    assert preview is not None
    canvas.command_stack.undo()
    assert canvas._projection_interaction_preview is None
    assert canvas.chapter.modifiers[modifier.modifier_id].brightness == 0
    assert preview.history != canvas._history_generation
    monkeypatch.setattr(canvas, '_render_scene_layers', original)
    paint(canvas, interactive=False)
    assert canvas._projection_completed_view[2] == canvas._document_projection.revision
    assert canvas._projection_completed_view is not completed
    controls.deleteLater()


def full_history_edit(canvas, modifier):
    """Use the same full-state callbacks as push_model_change's fallback."""
    before = canvas.chapter.to_dict()
    modifier.thickness += 3
    after = canvas.chapter.to_dict()
    canvas.documentChanged.emit(None)
    warm(canvas)
    canvas.command_stack.push(CallbackCommand('Full modifier history',
        lambda: canvas.replace_chapter(after), lambda: canvas.replace_chapter(before)), already_done=True)
    return canvas.chapter


def test_full_dict_modifier_undo_captures_fresh_history_model_with_blocked_worker(scene, monkeypatch):
    from comic_editor.ui import interactive_effects
    canvas, obj, _ = scene
    modifier = OutlineModifier(thickness=3, blur_radius=4, blur_strength=50)
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    previous_chapter = full_history_edit(canvas, modifier)
    previous_metadata = canvas._projection_last_exact_metadata
    canvas.command_stack.undo()
    assert canvas.chapter is not previous_chapter
    assert canvas.chapter.chapter_id == previous_chapter.chapter_id
    assert canvas._history_generation > previous_metadata[3]
    started, release, gui = Event(), Event(), get_ident()
    original = interactive_effects.apply_modifier_stack
    def gated(*args, **kwargs):
        if get_ident() != gui:
            started.set()
            assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(interactive_effects, 'apply_modifier_stack', gated)
    try:
        paint(canvas)
        assert started.wait(2)
        preview = canvas._projection_interaction_preview
        assert preview is not None
        assert preview.configuration == canvas._projection_configuration()
        assert preview.history == canvas._history_generation
        assert preview.revision == canvas._document_projection.revision
        assert canvas.chapter.modifiers[modifier.modifier_id].thickness == 3
        assert canvas._projection_frame_pending
        assert canvas._projection_completed_view is None
        assert canvas._projection_last_exact_metadata is previous_metadata
    finally:
        release.set()


def test_full_history_preview_failure_still_defers_exact_gui_work(scene, monkeypatch):
    from comic_editor.render.service import RenderResult
    canvas, obj, _ = scene
    modifier = OutlineModifier(thickness=3, blur_radius=4, blur_strength=50)
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    full_history_edit(canvas, modifier)
    canvas.command_stack.undo()
    original = canvas._render_service.render_region
    def failed_preview(document, request):
        if request.key == ('interaction-preview',):
            return RenderResult(request, document, QImage(), RenderStatus.FAILED, 'Preview unavailable')
        return original(document, request)
    monkeypatch.setattr(canvas._render_service, 'render_region', failed_preview)
    block_exact(canvas, monkeypatch)
    paint(canvas)
    assert canvas._projection_interaction_preview is None
    assert canvas._projection_completed_view is None
    assert canvas._projection_frame_pending


@pytest.mark.parametrize('status', [RenderStatus.PENDING, RenderStatus.FAILED])
def test_failed_preview_after_same_document_edit_never_forces_gui_exact_work(scene, qapp, monkeypatch, status):
    from PySide6.QtCore import QTimer
    from comic_editor.render.service import RenderResult
    canvas, obj, _ = scene
    canvas.chapter.add_modifier(OutlineModifier(thickness=3, blur_radius=4, blur_strength=50),
                                [('object', obj.object_id)])
    completed = warm(canvas)
    obj.x += 80
    canvas.documentChanged.emit(None)
    assert completed[0] == canvas._projection_configuration()
    assert completed[2] != canvas._document_projection.revision
    original = canvas._render_service.render_region
    def unavailable_preview(document, request):
        if request.key == ('interaction-preview',):
            return RenderResult(request, document, QImage(), status, 'Preview unavailable')
        return original(document, request)
    monkeypatch.setattr(canvas._render_service, 'render_region', unavailable_preview)
    source = block_exact(canvas, monkeypatch)
    paint(canvas)
    assert canvas._projection_interaction_preview is None
    assert canvas._projection_completed_view is completed
    assert canvas._projection_frame_pending
    assert canvas._projection_presented_revision == completed[2]
    heartbeat = []
    QTimer.singleShot(0, lambda: heartbeat.append(True))
    qapp.processEvents()
    assert heartbeat
    # A later successful exact render still converges from the edited model;
    # neither the old view nor the failed draft can acquire its revision.
    monkeypatch.setattr(canvas, '_render_scene_layers', source)
    monkeypatch.setattr(canvas._render_service, 'render_region', original)
    settled = paint(canvas, interactive=False)
    assert not canvas._projection_frame_pending
    assert canvas._projection_presented_revision == canvas._document_projection.revision
    canvas._render_service.invalidate()
    assert paint(canvas, interactive=False) == settled


@pytest.mark.parametrize('interactive', [True, False], ids=['widget', 'detached'])
def test_live_gesture_retires_exact_jobs_before_native_capture_and_release_converges(scene, monkeypatch, interactive):
    from types import SimpleNamespace
    from comic_editor.ui import native_artwork
    canvas, obj, _ = scene
    modifier = OutlineModifier(thickness=3, blur_radius=4, blur_strength=50)
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    before = warm(canvas)
    controls = ModifierControls(canvas)
    controls.begin_parameter_drag(modifier.modifier_id)
    controls.set_parameter(modifier.modifier_id, 'thickness', 7, False)
    jobs = canvas._effect_jobs
    jobs.worker_limit = 1
    checkpoint = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    checkpoint.fill(QColor('green'))
    jobs.retained_put(('pipeline', 'stable'), 'prefix', checkpoint, shared=True)
    started, release = Event(), Event()
    def gated(cancelled):
        started.set()
        assert release.wait(5)
        return checkpoint
    jobs.request('obsolete-exact', ('obsolete-exact',), gated, 100, require_exact=True)
    assert started.wait(1)
    obsolete = jobs.running
    jobs.request('live-preview', ('live-preview',), lambda _: checkpoint, 100)
    jobs.request('obsolete-queued', ('obsolete-queued',), lambda _: checkpoint, 100, require_exact=True)
    tiles = {key: (tile.image.cacheKey(), tile.valid) for key, tile in canvas._document_projection.tiles.items()}
    durable = []
    canvas._persistent_render_cache = SimpleNamespace(
        lookup=lambda *args, **kwargs: None,
        retain=lambda kind, key, *args, **kwargs: durable.append((kind, key)))
    original = native_artwork.paint_scene
    def capture(*args, **kwargs):
        assert obsolete[2].is_set() is interactive
        assert 'live-preview' in jobs.pending
        assert ('obsolete-queued' not in jobs.pending) is interactive
        assert not exact_cache_allowed(canvas, ('live-scene',))
        return original(*args, **kwargs)
    monkeypatch.setattr(native_artwork, 'paint_scene', capture)
    try:
        paint(canvas, interactive=interactive)
        assert not durable
        assert canvas._projection_completed_view is before
        assert canvas._projection_interaction_preview is None
        assert {key: (tile.image.cacheKey(), tile.valid) for key, tile in canvas._document_projection.tiles.items()} == tiles
        assert jobs.retained_get(('pipeline', 'stable'), 'prefix')[0] == checkpoint
    finally:
        release.set()
        controls.finish_parameter_drag()
        if not interactive:
            jobs.cancel_exact()
        obsolete[3].result(timeout=5)
        jobs.poll()
        canvas._persistent_render_cache = None
    assert jobs.retained_get(('result', 'obsolete-exact'), ('obsolete-exact',)) is None
    assert not any('obsolete-exact' in repr(key) for _kind, key in durable)
    # Release can publish fresh committed feedback and finish the ordinary
    # exact phase set, without waiting for an obsolete snapshot to publish.
    for _ in range(20):
        settled = paint(canvas)
        if not canvas._projection_frame_pending:
            break
        for job in jobs.running_jobs:
            job[3].result(timeout=5)
        jobs.poll()
    assert not canvas._projection_frame_pending
    assert canvas._projection_completed_view[2] == canvas._document_projection.revision
    canvas._render_service.invalidate()
    assert settled == paint(canvas, interactive=False)
    controls.deleteLater()


def test_new_gesture_rejects_preview_and_exact_top_base_publish_together(scene, monkeypatch):
    canvas, obj, group = scene
    promoted = canvas.chapter.add_object(group.layer_id, RasterObject(show_on_top=True))
    canvas.tiles.paint_dab(promoted.object_id, QPointF(900, 120), 30, QColor('blue'))
    canvas._projection_async_enabled = False
    paint(canvas, interactive=False, live_ink=True)
    canvas._projection_async_enabled = True
    completed = canvas._projection_completed_view
    assert [phase for phase, _tiles in completed[1]] == ['base', 'top']
    obj.x += 30
    canvas.documentChanged.emit(None)
    original = canvas._render_scene_layers
    def capture(*args, **kwargs):
        if canvas._projection_exact and kwargs.get('only_phase') == 'top':
            raise ProjectionPending('top-exact', 'key')
        return original(*args, **kwargs)
    monkeypatch.setattr(canvas, '_render_scene_layers', capture)
    paint(canvas, live_ink=True)
    preview = canvas._projection_interaction_preview
    assert preview is not None and canvas._projection_frame_pending
    assert canvas._projection_completed_view is completed
    canvas._modifier_parameter_drag_id = 'new-gesture'
    paint(canvas)
    assert canvas._projection_interaction_preview is None
    canvas._modifier_parameter_drag_id = None
    monkeypatch.setattr(canvas, '_render_scene_layers', original)
    paint(canvas, interactive=False, live_ink=True)
    assert not canvas._projection_frame_pending
    assert [phase for phase, _tiles in canvas._projection_completed_view[1]] == ['base', 'top']


def test_failed_exact_worker_keeps_current_committed_feedback(scene, monkeypatch):
    canvas, obj, _ = scene
    before = warm(canvas)
    obj.x += 80
    canvas.documentChanged.emit(None)
    block_exact(canvas, monkeypatch, failure=True)
    paint(canvas)
    preview = canvas._projection_interaction_preview
    assert preview is not None and preview.tile.image == oracle(canvas, preview)
    assert canvas._projection_frame_pending and canvas._projection_render_error
    assert canvas._projection_completed_view is before
    paint(canvas)
    assert canvas._projection_interaction_preview is preview


@pytest.mark.parametrize('precision', ['float16', 'float32'])
def test_local_committed_preview_preserves_float_precision_contract(scene, precision):
    from comic_editor.core.pixel_contract import PixelContract
    canvas, obj, _ = scene
    canvas.chapter.pixel_contract = PixelContract(version=2, precision=precision)
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    warm(canvas)
    controls = ModifierControls(canvas)
    controls.begin_parameter_drag(modifier.modifier_id)
    controls.set_parameter(modifier.modifier_id, 'brightness', 40, False)
    controls.finish_parameter_drag()
    assert canvas._capture_interaction_projection_preview()
    preview = canvas._projection_interaction_preview
    assert preview.tile.image.format() == canvas.chapter.pixel_contract.image_format
    assert preview.tile.image == oracle(canvas, preview)
    controls.deleteLater()


def test_changed_working_space_cannot_seed_from_old_exact_composite(scene, monkeypatch):
    from comic_editor.core.pixel_contract import PixelContract
    canvas, obj, _ = scene
    canvas.chapter.pixel_contract = PixelContract(version=2, precision='float32')
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    warm(canvas)
    old_contract = canvas._projection_completed_pixel_contract
    canvas.chapter.pixel_contract = PixelContract(version=2, precision='float32', working_space='linear_srgb')
    assert old_contract.image_format == canvas.chapter.pixel_contract.image_format
    controls = ModifierControls(canvas)
    controls.set_parameter(modifier.modifier_id, 'brightness', 40, True)
    assert canvas._projection_interaction_dirty is None
    assert all(not tile.valid for tile in canvas._document_projection.tiles.values())
    captures = []
    original = canvas._render_service.render_region
    def capture(document, request):
        captures.append(request)
        return original(document, request)
    monkeypatch.setattr(canvas._render_service, 'render_region', capture)
    assert canvas._capture_interaction_projection_preview()
    preview = canvas._projection_interaction_preview
    assert captures[0].bounds == preview.coverage
    assert preview.pixel_contract != old_contract
    controls.deleteLater()


def test_stale_exact_metadata_cannot_seed_new_chapter_preview_at_same_revision(scene, monkeypatch):
    from comic_editor.core.models import ChapterDocument
    canvas, _obj, _ = scene
    warm(canvas)
    previous = canvas._projection_last_exact_metadata
    replacement = ChapterDocument.from_dict(canvas.chapter.to_dict())
    replacement.chapter_id = 'another-chapter'
    replacement.objects[_obj.object_id].x += 250
    canvas.set_document(replacement, canvas.tiles, canvas.images, reset_view=False)
    assert canvas._projection_last_exact_metadata is None
    # Even a stale external metadata value with the same numeric revision is
    # never an exact seed for this different live document. A complete fresh
    # capture of the new model is still useful while its exact tiles settle.
    canvas._projection_last_exact_metadata = previous
    canvas._document_projection.revision = previous[1]
    captures = []
    original = canvas._render_service.render_region
    def capture(document, request):
        captures.append(request)
        return original(document, request)
    monkeypatch.setattr(canvas._render_service, 'render_region', capture)
    assert canvas._capture_interaction_projection_preview()
    preview = canvas._projection_interaction_preview
    assert preview.configuration[:3] == (id(replacement), id(canvas.tiles), id(canvas.images))
    assert captures[0].bounds == preview.coverage
    assert preview.tile.image == oracle(canvas, preview)


@pytest.mark.parametrize('initial', [True, False], ids=['initial-load', 'cold-navigation'])
def test_uncovered_viewport_gets_current_model_feedback_while_exact_is_pending(scene, monkeypatch, initial):
    canvas, _obj, _group = scene
    canvas.center_x = 512.
    if not initial:
        completed = warm(canvas)
        revision = canvas._document_projection.revision
        canvas.center_x = 1536.
        assert not canvas._completed_projection_covers(completed, canvas.visible_document_rect())
    else:
        assert canvas._projection_completed_view is None
        revision = canvas._document_projection.revision
    original = block_exact(canvas, monkeypatch)
    requests = []
    render = canvas._render_service.render_region
    def capture(document, request):
        if request.key == ('interaction-preview',):
            requests.append(request)
        return render(document, request)
    monkeypatch.setattr(canvas._render_service, 'render_region', capture)
    paint(canvas)
    preview = canvas._projection_interaction_preview
    assert preview is not None and canvas._projection_frame_pending
    assert preview.revision == canvas._document_projection.revision
    if not initial:
        assert preview.revision == revision, 'Navigation must not change the artwork revision'
    assert preview.coverage == canvas.visible_document_rect()
    assert requests[0].bounds == preview.coverage, 'Uncovered regions cannot patch an old exact seed'
    assert preview.tile.image == oracle(canvas, preview)
    assert preview.tile.image.width() * preview.tile.image.height() <= 1024 * 1024
    assert not any(tile.valid for tile in canvas._document_projection.tiles.values()) or not initial
    monkeypatch.setattr(canvas, '_render_scene_layers', original)
    exact = paint(canvas, interactive=False)
    assert not canvas._projection_frame_pending and canvas._projection_interaction_preview is None
    canvas._render_service.invalidate()
    assert paint(canvas, interactive=False) == exact


def test_complete_exact_current_viewport_skips_redundant_preview(scene, monkeypatch):
    canvas, _obj, _group = scene
    warm(canvas)
    def unexpected(*args, **kwargs):
        raise AssertionError('Covered unchanged exact view must not render an extra preview')
    monkeypatch.setattr(canvas._render_service, 'render_region', unexpected)
    assert not canvas._capture_interaction_projection_preview()
    assert canvas._projection_interaction_preview is None


def test_live_stroke_takes_precedence_and_capture_is_bounded(scene):
    canvas, obj, _ = scene
    warm(canvas)
    obj.x += 20
    canvas.documentChanged.emit(None)
    canvas.scale = .125
    assert canvas._capture_interaction_projection_preview()
    preview = canvas._projection_interaction_preview
    assert preview.density <= 1
    assert preview.tile.image.width() * preview.tile.image.height() <= 1024 * 1024
    canvas._drawing = True
    assert not canvas._capture_interaction_projection_preview()
    assert canvas._projection_interaction_preview is None
    canvas._drawing = False


@pytest.mark.parametrize('configuration', ['edit', 'solo'])
def test_actual_blocked_heavy_worker_returns_fresh_commit_preview(scene, monkeypatch, configuration):
    from comic_editor.ui import interactive_effects
    canvas, obj, group = scene
    canvas.chapter.add_modifier(OutlineModifier(thickness=3, blur_radius=4, blur_strength=50),
                                [('object', obj.object_id)])
    completed = warm(canvas)
    obj.x += 35
    canvas.documentChanged.emit(None)
    if configuration == 'solo':
        canvas.set_solo_entities([('object', obj.object_id)])
    started, release, gui = Event(), Event(), get_ident()
    original = interactive_effects.apply_modifier_stack
    def gated(*args, **kwargs):
        if get_ident() != gui:
            started.set()
            assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(interactive_effects, 'apply_modifier_stack', gated)
    try:
        paint(canvas)
        assert started.wait(2)
        assert canvas._projection_interaction_preview is not None
        assert canvas._projection_interaction_preview.revision == canvas._document_projection.revision
        assert canvas._projection_frame_pending
        assert canvas._projection_completed_view is (completed if configuration == 'edit' else None)
    finally:
        release.set()


def test_unknown_heavy_hierarchy_and_show_on_top_capture_whole_scene(scene, monkeypatch):
    canvas, obj, group = scene
    page = canvas.chapter.layers[group.parent_id]
    group.compound_enabled = True
    canvas.chapter.add_modifier(OutlineModifier(thickness=4), [('layer', group.layer_id)])
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    mask = ToneMask(contributors=[('object', obj.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    group.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    page.show_on_top = True
    completed = warm(canvas)
    controls = ModifierControls(canvas)
    controls.set_parameter(modifier.modifier_id, 'brightness', 35, True)
    assert canvas._projection_interaction_dirty is None
    block_exact(canvas, monkeypatch)
    captures = []
    render = canvas._render_service.render_region
    def traced(document, request):
        result = render(document, request)
        if request.key == ('interaction-preview',):
            captures.append(result)
        return result
    monkeypatch.setattr(canvas._render_service, 'render_region', traced)
    paint(canvas)
    preview = canvas._projection_interaction_preview
    assert preview is not None and preview.tile.image == captures[0].image
    assert captures[0].request.bounds == preview.coverage
    assert captures[0].document.revision == canvas._document_projection.revision
    assert canvas._projection_completed_view is completed
    assert canvas._projection_presented_revision == completed[2]
    controls.deleteLater()
