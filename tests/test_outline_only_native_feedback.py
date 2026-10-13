"""Outline-only feedback observes the existing ordinary native byte route."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import OutlineModifier, ParameterMaskBinding
from comic_editor.core.tools import ToolKind
from comic_editor.render.filtered_raster_feedback import (
    FeedbackUnavailable, capture_native_feedback_recipe, update_native_feedback_recipe,
)
from comic_editor.render.outputs import capture_document
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.scene_kernels import SceneKernels
from comic_editor.render.service import DocumentRenderService, RenderRequest, TileBatchPolicy
from comic_editor.render.projection import ProjectionAddress, ProjectionRequest
from test_filtered_native_recipe import (
    fixture as mixed_fixture, native, ordinary, changed_snapshot,
)
from test_filtered_raster_contact_feedback import (
    filtered_scene, begin, move, finish, wait_feedback, world_point,
    native_presentation, source_pixels,
)
from test_raster_contact_feedback import pixels, ready_paint


def outline_fixture(format=QImage.Format_ARGB32_Premultiplied, rotate=False, fractional=True):
    chapter, tiles, images, obj, _baseline, _request, _recipe = mixed_fixture(format, True, rotate)
    for identifier in tuple(obj.modifier_ids):
        if not isinstance(chapter.modifiers[identifier], OutlineModifier):
            obj.modifier_ids.remove(identifier)
            del chapter.modifiers[identifier]
    outline = chapter.modifiers[obj.modifier_ids[0]]
    outline.thickness, outline.intensity, outline.color = 8., 91., '#ccefd17b'
    obj.x, obj.y = (-31.25, -17.5) if fractional else (-31., -17.)
    obj.interaction_rect = (-256., -256., 768., 768.)
    tiles.set_tile(obj.object_id, (-1, -1), QImage(tiles.tile(obj.object_id, (0, 0))))
    snapshot = capture_document(chapter, tiles, images)
    request = RenderRequest((0., 0., 512., 512.), 1., (512, 512), ('outline-native-proof',), 0)
    return chapter, tiles, images, obj, outline, snapshot, request


def influence(snapshot, obj, point, size=16.):
    backend = DetachedSceneBackend(snapshot)
    try:
        mapping = (backend.scene._drawing_object_transform(obj)
            * backend.scene.layer_world_transform(obj.parent_layer_id))
        # Raster source position is applied before the ordinary object mapping.
        point = QPointF(point.x() + obj.x, point.y() + obj.y)
        return mapping.mapRect(QRectF(point.x()-size, point.y()-size, 2*size, 2*size)).adjusted(-12, -12, 12, 12)
    finally:
        backend.close()


@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
@pytest.mark.parametrize('rotate,fractional', [(False, False), (False, True), (True, True)])
def test_cumulative_outline_contacts_match_full_ordinary_native_roi(qapp, format, rotate, fractional):
    chapter, tiles, images, obj, _outline, baseline, request = outline_fixture(format, rotate, fractional)
    original_pixels = {key: native(image) for key, image in tiles.snapshot(
        obj.object_id, set(tiles._tiles[obj.object_id])).items()}
    recipe = capture_native_feedback_recipe(baseline, obj.object_id, request)
    assert recipe.source_sampling is None and recipe.outline_sampling is not None
    assert native(recipe.initial) == native(ordinary(baseline, request))
    prepared_source, prepared_scene = native(recipe.source_image), native(recipe.initial)
    previous = None
    # Existing alpha, transparent pixels in an existing tile, a new native
    # tile inside the fixed source frame, then erasure of existing/new alpha.
    for revision, (point, erase) in enumerate(((QPointF(137., 113.), False),
            (QPointF(35., 40.), False), (QPointF(300., 80.), False),
            (QPointF(137., 113.), True), (QPointF(300., 80.), True)), 1):
        tiles.paint_dab(obj.object_id, point, 12., QColor('#c0997722'), opacity=.73, erase=erase)
        if erase:
            tiles.prune_empty(obj.object_id, {(1, 0)})
        updated = changed_snapshot(chapter, tiles, images, baseline, revision)
        actual = update_native_feedback_recipe(recipe, updated, influence(updated, obj, point),
            previous_source=previous)
        expected = ordinary(updated, replace(request, revision=revision))
        assert native(actual.image) == native(expected)
        assert native(recipe.source_image) == prepared_source
        assert native(recipe.initial) == prepared_scene
        previous = actual.source
    assert {key: native(image) for key, image in baseline.tiles.snapshot(
        obj.object_id, set(baseline.tiles._tiles[obj.object_id])).items()} == original_pixels


def test_cached_outline_output_still_supplies_ordinary_native_witness(qapp, monkeypatch):
    _chapter, _tiles, _images, obj, _outline, baseline, request = outline_fixture()
    backend = DetachedSceneBackend(baseline)
    backend.artwork_scale = 1.
    try:
        result = DocumentRenderService(backend).render_region(baseline.document, request)
        assert result.exact, result.error
        caches = backend.scene.cache_state()
    finally:
        backend.close()
    def recompute(*_args, **_kwargs):
        pytest.fail('A warm ordinary outline output was recomputed')
    monkeypatch.setattr('comic_editor.ui.interactive_effects.apply_modifier_stack', recompute)
    recipe = capture_native_feedback_recipe(baseline, obj.object_id, request, caches=caches)
    assert recipe.outline_sampling is not None
    assert native(recipe.initial) == native(result.image)


def test_partial_projection_demand_keeps_complete_prepared_outline_window(qapp):
    chapter, tiles, images, obj, _outline, _baseline, _request = outline_fixture()
    chapter.width = chapter.height = 1536
    page = chapter.layers[chapter.root_page_ids[0]]
    page.bound = type(page.bound).rectangle(0., 0., 1536., 1536.)
    layer = chapter.layers[obj.parent_layer_id]
    layer.bound = type(layer.bound).rectangle(0., 0., 1536., 1536.)
    obj.interaction_rect = (-256., -256., 1536., 1536.)
    baseline = capture_document(chapter, tiles, images)
    requests = [ProjectionRequest(ProjectionAddress(0, x, y)) for y in range(2) for x in range(2)]
    requested = QRectF()
    for projection in requests:
        requested = requested.united(projection.capture_rect)
    request = RenderRequest((-2., -2., 1028., 1028.), 1., (1028, 1028), (0, 0, 0), 0,
        tuple(requested.getRect()))
    recipe = capture_native_feedback_recipe(baseline, obj.object_id, request)
    assert recipe.outline_sampling.effect_region == (0., 0., 1026., 1026.)
    assert QRectF(*recipe.outline_sampling.effect_region) != request.requested
    tiles.paint_dab(obj.object_id, QPointF(300., 180.), 12., QColor('#c0997722'))
    updated = changed_snapshot(chapter, tiles, images, baseline, 1)
    actual = update_native_feedback_recipe(recipe, updated, influence(updated, obj, QPointF(300., 180.)))
    expected = ordinary(updated, replace(request, revision=1))
    assert native(actual.image) == native(expected)
    backend = DetachedSceneBackend(updated)
    try:
        service = DocumentRenderService(backend)
        service.projection.revision = updated.document.revision
        rendered = service.render_tiles(updated.document, requests, TileBatchPolicy((256., 256.)))
        assert not rendered.error and all(exact for _image, exact in rendered.tiles.values())
        for projection in requests:
            rectangle = projection.capture_rect.translated(-request.bounds.topLeft()).toAlignedRect()
            assert native(actual.image.copy(rectangle)) == native(rendered.tiles[projection.address][0])
    finally:
        backend.close()


@pytest.mark.parametrize('change', ['frame_growth', 'native_mapping', 'parent_mapping',
    'thickness', 'color', 'antialias', 'mask', 'previous_format'])
def test_changed_outline_sampling_contract_falls_back(qapp, change):
    chapter, tiles, images, obj, outline, baseline, request = outline_fixture()
    recipe = capture_native_feedback_recipe(baseline, obj.object_id, request)
    if change == 'frame_growth':
        obj.interaction_rect = (-256., -256., 1024., 768.)
    elif change == 'native_mapping':
        obj.x += .25
    elif change == 'parent_mapping':
        parent = chapter.layers[obj.parent_layer_id]
        parent.transform_quad = [(x + .25, y) for x, y in parent.transform_quad]
    elif change == 'thickness':
        outline.thickness += 1.
    elif change == 'color':
        outline.color = '#ff123456'
    elif change == 'antialias':
        outline.antialiasing = not outline.antialiasing
    elif change == 'mask':
        mask = next(iter(chapter.masks.values()))
        outline.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0., 90.)
    previous = recipe.source_image.convertToFormat(QImage.Format_RGBA64_Premultiplied) if change == 'previous_format' else None
    updated = changed_snapshot(chapter, tiles, images, baseline, 1)
    before = native(recipe.initial), native(recipe.source_image)
    with pytest.raises(FeedbackUnavailable):
        update_native_feedback_recipe(recipe, updated, QRectF(50., 50., 200., 200.), previous_source=previous)
    assert (native(recipe.initial), native(recipe.source_image)) == before


@pytest.mark.parametrize('unsupported', ['mask', 'blur', 'multiple', 'show_on_top', 'projective'])
def test_unproven_outline_routes_keep_ordinary_fallback(qapp, unsupported):
    chapter, tiles, images, obj, outline, _baseline, request = outline_fixture()
    if unsupported == 'mask':
        mask = next(iter(chapter.masks.values()))
        outline.parameter_masks['thickness'] = ParameterMaskBinding(mask.mask_id, 2., 8.)
    elif unsupported == 'blur':
        outline.blur_radius, outline.blur_strength = 4., 100.
    elif unsupported == 'multiple':
        chapter.add_modifier(OutlineModifier(thickness=3.), [('object', obj.object_id)])
    elif unsupported == 'show_on_top':
        obj.show_on_top = True
    else:
        obj.transform_frame = obj.interaction_rect
        obj.transform_quad = [(-250., -250.), (480., -230.), (510., 510.), (-256., 490.)]
    baseline = capture_document(chapter, tiles, images)
    with pytest.raises(FeedbackUnavailable):
        capture_native_feedback_recipe(baseline, obj.object_id, request)
    assert not ordinary(baseline, request).isNull()


def test_unsupported_selected_image_placement_is_not_reinterpreted(qapp, monkeypatch):
    _chapter, _tiles, _images, obj, _outline, baseline, request = outline_fixture()
    original = SceneKernels._render_modified_object
    class RectPlacement:
        def __init__(self, painter):
            self.painter = painter
        def __getattr__(self, name):
            return getattr(self.painter, name)
        def drawImage(self, point, image):
            return self.painter.drawImage(QRectF(point.x(), point.y(), image.width(), image.height()), image)
    def changed_route(scene, painter, target, *args):
        return original(scene, RectPlacement(painter) if target.object_id == obj.object_id else painter, target, *args)
    monkeypatch.setattr(SceneKernels, '_render_modified_object', changed_route)
    with pytest.raises(FeedbackUnavailable, match='placement'):
        capture_native_feedback_recipe(baseline, obj.object_id, request)


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
def test_actual_outline_contact_uses_feedback_and_release_undo_redo_exactly(
        filtered_scene, tool, wait_scene, qapp):
    canvas, obj = filtered_scene
    for identifier in tuple(obj.modifier_ids):
        if not isinstance(canvas.chapter.modifiers[identifier], OutlineModifier):
            obj.modifier_ids.remove(identifier)
            del canvas.chapter.modifiers[identifier]
    canvas.set_tool(tool)
    wait_scene(canvas)
    state = canvas._scene_controller._filtered_state
    assert state is not None and all(recipe.outline_sampling is not None for recipe in state.prepared.recipes)
    before_source = source_pixels(canvas, obj)
    before_frame = pixels(ready_paint(canvas))
    submitted = canvas._scene_controller.scheduler.submitted
    for index, local in enumerate((QPointF(137., 113.), QPointF(173., 144.))):
        point = world_point(canvas, obj, local)
        begin(canvas, point, tool) if index == 0 else move(canvas, point, tool)
        actual = wait_feedback(canvas, qapp)
        np.testing.assert_array_equal(pixels(actual), pixels(native_presentation(canvas)))
        assert not np.array_equal(before_frame, pixels(actual))
        assert canvas._scene_controller.scheduler.submitted == submitted
    finish(canvas, tool)
    wait_scene(canvas)
    changed_source = source_pixels(canvas, obj)
    assert changed_source != before_source
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_presentation(canvas)))
    canvas.command_stack.undo()
    wait_scene(canvas)
    assert source_pixels(canvas, obj) == before_source
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_presentation(canvas)))
    canvas.command_stack.redo()
    wait_scene(canvas)
    assert source_pixels(canvas, obj) == changed_source
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_presentation(canvas)))

