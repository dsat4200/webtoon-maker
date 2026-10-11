"""Scoped native cage inputs and bounded private live preview ownership."""
from dataclasses import replace
from threading import Event
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import (BoundGeometry, ChapterDocument, OutlineModifier, RasterObject,
    TilingModifier, VectorDrawingObject, VectorStroke, VectorStrokePoint)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.input_capture import cage_initial, cage_commit, _cage_source_capture
from comic_editor.render.live_canvas_preview import LiveCanvasPreviewPolicy, live_cage_source_policy
from comic_editor.render.pixels import FLOAT_PIXELS, pixel_scope
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, RenderQuality, RenderRequest, RenderStatus
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.cage_rendering import warp_image


def _canvas(qapp, *, image_format=QImage.Format_ARGB32_Premultiplied, width=512, height=384):
    document = ChapterDocument(document_kind='asset', width=width, height=height)
    page = document.add_page(bound=BoundGeometry.rectangle(0, 0, width, height))
    raster = document.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, width, height)))
    unrelated = document.add_object(page.layer_id, RasterObject())
    vector = document.add_object(page.layer_id, VectorDrawingObject(strokes=[VectorStroke(
        points=[VectorStrokePoint(x=12, y=24), VectorStrokePoint(x=28, y=48)])]))
    tiles = TileStore()
    for identifier, key in ((raster.object_id, (0, 0)), (unrelated.object_id, (6, 7))):
        image = QImage(256, 256, image_format)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.fillRect(0, 0, min(width, 256), min(height, 256),
                         QColor.fromRgbF(.371234, .618743, .791256, .712345))
        painter.end()
        tiles.set_tile(identifier, key, image)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.resize(512, 384)
    canvas.set_document(document, tiles)
    canvas.set_selection('object', raster.object_id)
    return canvas, raster, unrelated, vector


def _capture(canvas, targets, *, focused=True):
    state = canvas._render_document_state()
    capture = (_cage_source_capture(canvas, state, targets) if focused else
               canvas._scene_snapshot_compiler.capture(canvas, state))
    while not capture.advance(.004):
        pass
    assert not capture.stale
    return capture.result


def _close(canvas):
    canvas.deleteLater()


@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
                                        QImage.Format_RGBA64_Premultiplied])
def test_focused_cage_owns_only_selected_originals_and_preserves_full_snapshot_cache(qapp, image_format):
    canvas, raster, unrelated, vector = _canvas(qapp, image_format=image_format)
    targets = [('object', raster.object_id), ('object', vector.object_id)]
    try:
        full = _capture(canvas, targets, focused=False)
        compiler = canvas._scene_snapshot_compiler
        before_records, before_strokes = dict(compiler.records), dict(compiler.stroke_records)
        compiler.dirty.add(('object', unrelated.object_id))
        compiler.conservative = True
        focused = _capture(canvas, targets)
        assert set(focused.chapter.objects) == {raster.object_id, vector.object_id}
        assert set(focused.tiles._tiles) == {raster.object_id}
        assert not focused.images._sources and not focused.images._decoded
        assert focused.cache_spec is None
        assert compiler.records == before_records and compiler.stroke_records == before_strokes
        assert compiler.conservative and ('object', unrelated.object_id) in compiler.dirty
        isolated, reference = cage_initial(focused, targets), cage_initial(full, targets)
        assert isolated['grid'].frame == reference['grid'].frame
        for identifier in isolated['before_models']:
            assert isolated['before_models'][identifier].to_dict() == reference['before_models'][identifier].to_dict()
        for key, image in isolated['before_tiles'][raster.object_id].items():
            original = canvas.tiles.tile(raster.object_id, key)
            assert image.format() == original.format() == image_format
            assert bytes(image.constBits()) == bytes(original.constBits())
        assert set(_capture(canvas, targets, focused=False).chapter.objects) == set(canvas.chapter.objects)
    finally:
        _close(canvas)


def test_focused_source_keeps_ordinary_revision_invalidation_and_tiling_fallback(qapp):
    canvas, raster, unrelated, _vector = _canvas(qapp)
    targets = [('object', raster.object_id)]
    try:
        capture = _cage_source_capture(canvas, canvas._render_document_state(), targets)
        canvas._invalidate_scene_cache()
        assert capture.advance(.004) and capture.stale
        tiling = TilingModifier()
        canvas.chapter.modifiers[tiling.modifier_id] = tiling
        raster.modifier_ids.append(tiling.modifier_id)
        canvas._scene_snapshot_compiler.invalidate()
        snapshot = _capture(canvas, targets)
        assert unrelated.object_id in snapshot.chapter.objects
        assert unrelated.object_id in snapshot.tiles._tiles
    finally:
        _close(canvas)


@pytest.mark.parametrize('compute', [cage_initial, cage_commit])
def test_only_explicitly_guarded_cage_jobs_use_full_revision_focused_capture(qapp, monkeypatch, compute):
    from comic_editor.ui.scene_consumers import SceneConsumers
    canvas, raster, _unrelated, _vector = _canvas(qapp)
    consumers = SceneConsumers(canvas)
    expected = object()
    monkeypatch.setattr(canvas._scene_snapshot_compiler, 'capture', lambda *_a: expected)
    try:
        job = SimpleNamespace(owned_source=False, detached=False, valid=lambda: True,
            document=canvas._render_document_state(), arguments=([('object', raster.object_id)],),
            compute=compute, cancelled=Event())
        captured = consumers._capture_job(job)
        assert captured.source_objects == frozenset([raster.object_id])
        assert consumers._valid(job, job.document)
        assert not consumers._valid(job, replace(job.document, revision=job.document.revision+1))
        job.valid = None
        assert consumers._capture_job(job) is expected
    finally:
        consumers.shutdown()
        _close(canvas)


def _preview_backend(canvas, raster, *, cancelled=None):
    snapshot = _capture(canvas, [('object', raster.object_id)], focused=False)
    session = cage_initial(snapshot, [('object', raster.object_id)])
    grid = session['grid']
    grid.points[5] = (grid.points[5][0]+17, grid.points[5][1]-8)
    document = replace(snapshot.document, live_preview=True)
    # The actual renderer snapshot retains only render inputs, excluding history.
    snapshot = replace(snapshot, document=document, state=dict(snapshot.state,
        _cage_session={key: session[key] for key in ('grid', 'targets', 'sources')}))
    backend = DetachedSceneBackend(snapshot)
    backend.scene._live_canvas_preview_policy = LiveCanvasPreviewPolicy(document.identity,
        document.revision, 51, cancelled or (lambda: False))
    return backend, snapshot, grid


def _draw_preview(backend, raster):
    image = QImage(512, 384, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        with pixel_scope(backend.snapshot.document.pixel_contract, environment=backend.snapshot.pixel_environment):
            assert backend.scene._cage_object_preview(painter, raster, 1., QRectF(0, 0, 512, 384))
    finally:
        painter.end()
    return image


def test_live_cage_caps_source_before_allocating_and_has_separate_draft_cache(qapp):
    canvas, raster, _unrelated, _vector = _canvas(qapp, width=1024, height=80000)
    backend = None
    try:
        before = bytes(canvas.tiles.tile(raster.object_id, (0, 0)).constBits())
        backend, _snapshot, _grid = _preview_backend(canvas, raster)
        _draw_preview(backend, raster)
        source_keys = list(backend.scene._cage_session['sources'])
        assert all(key[0] == 'cage-tool-draft-source' for key in source_keys)
        for image, bounds in backend.scene._cage_session['sources'].values():
            assert max(image.width(), image.height()) <= 224
            assert image.width()*image.height() <= 33217
            assert bounds.width()*bounds.height() > 64*1024*1024
        assert all(key[0] == 'cage-tool-draft' for key in backend.scene._modifier_render_cache)
        assert bytes(canvas.tiles.tile(raster.object_id, (0, 0)).constBits()) == before
        # An exact/reference consumer retains the original native allocation
        # limits rather than adopting an interactive reduced source.
        backend.scene._live_canvas_preview_policy = None
        with pytest.raises(ValueError, match='too large'):
            _draw_preview(backend, raster)
    finally:
        backend.close() if backend is not None else None
        _close(canvas)


@pytest.mark.parametrize('dimensions', [(128, 96), (512, 384)])
def test_draft_then_native_cage_matches_independent_original_grid_and_commit(qapp, monkeypatch, dimensions):
    monkeypatch.setattr('comic_editor.ui.gpu_textures.renderer_for', lambda *_a: None)
    canvas, raster, _unrelated, _vector = _canvas(qapp, width=dimensions[0], height=dimensions[1])
    backend = None
    try:
        backend, snapshot, grid = _preview_backend(canvas, raster)
        baseline, baseline_alpha, _models = cage_commit(snapshot, [('object', raster.object_id)], grid)
        _draw_preview(backend, raster)
        backend.scene._live_canvas_preview_policy = None
        actual = _draw_preview(backend, raster)
        source, bounds = backend.scene._cage_session['sources'][raster.object_id]
        assert source.size().toTuple() == dimensions
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            expected, expected_bounds = warp_image(source, bounds, grid)
        reference = QImage(actual.size(), actual.format())
        reference.fill(Qt.transparent)
        painter = QPainter(reference)
        painter.drawImage(expected_bounds, expected)
        painter.end()
        assert bytes(actual.constBits()) == bytes(reference.constBits())
        candidate, candidate_alpha, _models = cage_commit(snapshot, [('object', raster.object_id)], grid)
        assert candidate_alpha == baseline_alpha
        assert candidate[raster.object_id][0].to_dict() == baseline[raster.object_id][0].to_dict()
        assert candidate[raster.object_id][1].keys() == baseline[raster.object_id][1].keys()
        for key, image in candidate[raster.object_id][1].items():
            original = baseline[raster.object_id][1][key]
            assert image.format() == original.format()
            assert bytes(image.constBits()) == bytes(original.constBits())
    finally:
        backend.close() if backend is not None else None
        _close(canvas)


@pytest.mark.parametrize('modified', ['none', 'object', 'parent', 'both'])
def test_actual_detached_native_scene_preview_renders_giant_cage_provisionally(qapp, modified):
    canvas, raster, _unrelated, _vector = _canvas(qapp, width=1024, height=80000)
    backend = None
    try:
        for target in ([raster] if modified == 'object' else
                       [canvas.chapter.layers[raster.parent_layer_id]] if modified == 'parent' else
                       [raster, canvas.chapter.layers[raster.parent_layer_id]] if modified == 'both' else []):
            modifier = OutlineModifier(thickness=8)
            canvas.chapter.modifiers[modifier.modifier_id] = modifier
            target.modifier_ids.append(modifier.modifier_id)
        canvas._scene_snapshot_compiler.invalidate()
        backend, snapshot, _grid = _preview_backend(canvas, raster)
        policy = backend.scene._live_canvas_preview_policy
        backend.native_preview, backend.artwork_scale = True, 1.
        backend.live_canvas_preview_policy = policy
        service = DocumentRenderService(backend)
        service.projection.revision = snapshot.document.revision
        request = RenderRequest((0., 0., 512., 384.), 1., (512, 384), ('detached-preview', 51),
            snapshot.document.revision, quality=RenderQuality.INTERACTIVE)
        result = service.render_region(snapshot.document, request)
        assert result.status is RenderStatus.PROVISIONAL and not result.exact
        assert not result.image.isNull()
        assert result.image.format() == snapshot.document.pixel_contract.image_format
        sources = backend.scene._cage_session['sources']
        assert sources and all(key[0] == 'cage-tool-draft-source' for key in sources)
        assert all(max(image.width(), image.height()) <= 224 for image, _bounds in sources.values())
        assert getattr(backend.scene, '_cage_preview_source_policy', None) is None
    finally:
        backend.close() if backend is not None else None
        _close(canvas)


@pytest.mark.parametrize('flag', ['_render_base_alpha', '_rendering_mask_contributor', '_render_cage_source',
    '_rendering_halftone_source', '_rendering_compound_references', '_rendering_outward_gradient', '_tiling_capture_geometry'])
def test_scoped_cage_source_policy_cannot_reduce_native_masks_or_coupled_sources(qapp, flag):
    canvas, raster, _unrelated, _vector = _canvas(qapp)
    backend = None
    try:
        backend, _snapshot, _grid = _preview_backend(canvas, raster)
        backend.scene._cage_preview_source_policy = backend.scene._live_canvas_preview_policy
        backend.scene._live_canvas_preview_policy = None
        backend.scene._render_modifier_sources = {('object', raster.object_id)}
        assert live_cage_source_policy(backend.scene) is not None
        setattr(backend.scene, flag, True)
        assert live_cage_source_policy(backend.scene) is None
    finally:
        backend.close() if backend is not None else None
        _close(canvas)


@pytest.mark.parametrize('dimensions', [(128, 96), (512, 384)])
@pytest.mark.parametrize('interpolation', ['nearest', 'bilinear', 'bicubic'])
@pytest.mark.parametrize('floating', [False, True])
def test_native_after_draft_matches_frozen_original_preview_pixels(qapp, monkeypatch,
        dimensions, interpolation, floating):
    from references.cage_preview_native_20261011 import _cage_object_preview as original_preview
    monkeypatch.setattr('comic_editor.ui.gpu_textures.renderer_for', lambda *_a: None)
    canvas, raster, _unrelated, _vector = _canvas(qapp, width=dimensions[0], height=dimensions[1])
    candidate = reference = None
    try:
        if floating:
            canvas.chapter.pixel_contract = FLOAT_PIXELS
            canvas._scene_snapshot_compiler.invalidate()
        candidate, _snapshot, candidate_grid = _preview_backend(canvas, raster)
        candidate_grid.interpolation = interpolation
        _draw_preview(candidate, raster)
        candidate.scene._live_canvas_preview_policy = None
        actual = _draw_preview(candidate, raster)
        reference, _snapshot, reference_grid = _preview_backend(canvas, raster)
        reference_grid.interpolation = interpolation
        reference.scene._live_canvas_preview_policy = None
        reference.scene._cage_object_preview = MethodType(original_preview, reference.scene)
        expected = _draw_preview(reference, raster)
        assert actual.format() == expected.format() and actual.size() == expected.size()
        assert bytes(actual.constBits()) == bytes(expected.constBits())
        source, bounds = candidate.scene._cage_session['sources'][raster.object_id]
        original_source, original_bounds = reference.scene._cage_session['sources'][raster.object_id]
        assert bounds == original_bounds and source.size() == original_source.size()
        assert source.format() == original_source.format()
        assert source.colorSpace() == original_source.colorSpace()
        assert bytes(source.constBits()) == bytes(original_source.constBits())
        native = next(image for key, image in candidate.scene._modifier_render_cache.items()
                      if key[0] == 'cage-tool')
        original_native = next(iter(reference.scene._modifier_render_cache.values()))
        assert native.size() == original_native.size() and native.format() == original_native.format()
        assert native.colorSpace() == original_native.colorSpace()
        assert bytes(native.constBits()) == bytes(original_native.constBits())
    finally:
        candidate.close() if candidate is not None else None
        reference.close() if reference is not None else None
        _close(canvas)


def test_cancelled_live_cage_prepares_no_source_or_cache(qapp):
    from comic_editor.render.admission import WorkCancelled
    canvas, raster, _unrelated, _vector = _canvas(qapp)
    backend = None
    try:
        cancelled = Event()
        backend, _snapshot, _grid = _preview_backend(canvas, raster, cancelled=cancelled.is_set)
        cancelled.set()
        with pytest.raises(WorkCancelled):
            _draw_preview(backend, raster)
        assert not backend.scene._cage_session['sources']
        assert not backend.scene._modifier_render_cache
    finally:
        backend.close() if backend is not None else None
        _close(canvas)
