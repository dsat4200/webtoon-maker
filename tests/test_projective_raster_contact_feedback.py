"""Held perspective contacts preserve native pixels without scene evaluation."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.tools import ToolKind
from comic_editor.render.raster_feedback import prepare_raster_feedback
from test_raster_contact_feedback import scene, pixels, ready_paint
from test_raster_contact_feedback_boundaries import (
    begin, finish, boundary_scene, native_decorated, block_existing_scene_demand,
)


def map_saved_quad(entity, transform):
    entity.transform_quad = [transform.map(QPointF(*point)).toTuple()
                             for point in entity.transform_quad]


@pytest.mark.parametrize('mapping', ['perspective', 'ancestor_perspective', 'nested_perspective', 'mirrored_perspective'])
@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('format,opacity', [(QImage.Format_ARGB32_Premultiplied, 1.),
                                         (QImage.Format_RGBA64_Premultiplied, .43),
                                         (QImage.Format_RGBA32FPx4_Premultiplied, .43)])
def test_saved_projective_contacts_publish_every_native_buffer_and_release_exactly(
        scene, mapping, tool, format, opacity, wait_scene, monkeypatch):
    canvas, selected, front = scene
    boundary_scene(canvas, selected, front, format, opacity)
    layer = canvas.chapter.layers[selected.parent_layer_id]
    if mapping in ('perspective', 'nested_perspective', 'mirrored_perspective'):
        selected.transform_quad[2] = (selected.transform_quad[2][0] - 13., selected.transform_quad[2][1] + 7.)
    if mapping == 'mirrored_perspective':
        map_saved_quad(selected, QTransform(-1., 0., 0., 1., 720., 0.))
    if mapping in ('ancestor_perspective', 'nested_perspective'):
        layer.transform_quad[1] = (layer.transform_quad[1][0] - 9., layer.transform_quad[1][1] + 3.)
    canvas._invalidate_scene_cache()
    canvas.set_tool(tool)
    wait_scene(canvas)
    controller = canvas._scene_controller
    prepared = controller.feedback
    assert prepared is not None and prepared.source_transform is not None
    assert prepared.byte_count <= 32 * 1024 * 1024
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_decorated(canvas)))
    sources_before = {key: QImage(image) for key, image in canvas.tiles.object_tiles(selected.object_id).items()}
    history = canvas.command_stack.revision
    blocker = block_existing_scene_demand(canvas, monkeypatch)
    try:
        with monkeypatch.context() as guarded:
            guarded.setattr(canvas._scene_snapshot_compiler, 'capture',
                            lambda *_a, **_k: pytest.fail('Held affine ink recaptured the chapter'))
            guarded.setattr(controller.scheduler, 'submit',
                            lambda *_a, **_k: pytest.fail('Held affine ink submitted a scene'))
            previous = ready_paint(canvas)
            for index, x in enumerate((240, 272, 328, 520)):
                point = QPointF(x, 64.)
                if index == 0:
                    begin(canvas, tool, point)
                else:
                    (canvas._continue_paint_brush if tool == ToolKind.BRUSH else canvas._continue_stroke)(point, 1.)
                actual = ready_paint(canvas)
                assert canvas._drawing and canvas._raster_contact_active
                assert controller.feedback is prepared
                assert controller.capture is None and not controller.timer.isActive()
                assert canvas._raster_feedback_contact_covered and not canvas._raster_feedback_pending_visible
                assert actual.pixelColor(x, 64) != previous.pixelColor(x, 64)
                np.testing.assert_array_equal(pixels(actual), pixels(native_decorated(canvas)))
                with monkeypatch.context() as presentation:
                    presentation.setattr(canvas, '_render_scene_layers', lambda *_a, **_k: pytest.fail('GUI scene evaluation'))
                    presentation.setattr(canvas.tiles.residency, 'get', lambda *_a, **_k: pytest.fail('GUI source decode'))
                    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(actual))
                previous = actual
        finish(canvas, tool)
        assert canvas.command_stack.revision == history + 1
    finally:
        if canvas._drawing:
            finish(canvas, tool)
        blocker.set()
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_decorated(canvas)))
    canvas.command_stack.undo()
    assert sources_before == canvas.tiles.object_tiles(selected.object_id)


    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_decorated(canvas)))
    canvas.command_stack.redo()
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_decorated(canvas)))
    stable = {key: QImage(image) for key, image in canvas.tiles.object_tiles(selected.object_id).items()}
    stable_model, stable_revision = canvas.chapter.to_dict(), canvas.command_stack.revision
    begin(canvas, tool, QPointF(600., 70.))
    (canvas._continue_paint_brush if tool == ToolKind.BRUSH else canvas._continue_stroke)(QPointF(620., 70.), 1.)
    assert canvas.tiles.object_tiles(selected.object_id) != stable
    (canvas._cancel_paint_brush if tool == ToolKind.BRUSH else canvas._cancel_native_raster_stroke)()
    assert canvas.tiles.object_tiles(selected.object_id) == stable
    assert canvas.chapter.to_dict() == stable_model
    assert canvas.command_stack.revision == stable_revision
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_decorated(canvas)))


@pytest.mark.parametrize('quad', [
    [(0., 0.), (128., 0.), (0., 128.), (128., 128.)],
    [(0., 0.), (128., 0.), (30., 30.), (0., 128.)],
    [(0., 0.), (128., 0.), (128., 0.), (0., 128.)],
    [(float('nan'), 0.), (128., 0.), (127., 128.), (0., 128.)],
    [(float('inf'), 0.), (128., 0.), (127., 128.), (0., 128.)],
])
def test_invalid_projective_quad_declines_live_feedback(quad):
    from comic_editor.core.models import RasterObject
    from comic_editor.render.raster_feedback import _feedback_mapping_supported
    assert not _feedback_mapping_supported(RasterObject(transform_frame=(0., 0., 128., 128.), transform_quad=quad))


def test_projective_solver_identity_fallback_is_not_mapping_evidence(monkeypatch):
    from comic_editor.core.models import RasterObject
    from comic_editor.render.raster_feedback import _feedback_mapping_supported
    from comic_editor.render.scene_kernels import SceneKernels
    obj = RasterObject(transform_frame=(0., 0., 128., 128.),
        transform_quad=[(0., 0.), (128., 0.), (127., 128.), (0., 128.)])
    monkeypatch.setattr(SceneKernels, '_quad_transform', lambda *_: QTransform())
    assert not _feedback_mapping_supported(obj)


def test_projective_horizon_and_inverse_key_growth_decline_before_source_work():
    from PySide6.QtCore import QRectF
    from comic_editor.render.raster_feedback import _feedback_projective_rect_supported, _feedback_source_keys
    mapping = QTransform(1., 0., -1. / 128., 0., 1., 0., 0., 0., 1.)
    assert not _feedback_projective_rect_supported(mapping, QRectF(0., 0., 256., 256.))
    assert _feedback_source_keys((0., 0., 256., 256.), mapping, 256) is None
    assert _feedback_projective_rect_supported(mapping, QRectF(0., 0., 64., 64.))
    assert _feedback_projective_rect_supported(mapping, QRectF(256., 0., 64., 64.))
    assert _feedback_source_keys((0., 0., 127.9999, 127.9999), mapping, 256) is None


@pytest.mark.parametrize('mapping', ['object', 'ancestor', 'nested'])
@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_projective_curved_clips_and_translucent_foreground_match_complete_native_buffer(
        scene, mapping, tool, wait_scene, monkeypatch):
    from comic_editor.core.models import BoundGeometry
    from test_transformed_raster_contact_feedback import configure_transforms, native_reference
    canvas, selected, front = scene
    page = canvas.chapter.layers[selected.parent_layer_id]
    layer = configure_transforms(canvas, selected)
    if mapping in ('object', 'nested'):
        selected.transform_quad[2] = (selected.transform_quad[2][0] - 4., selected.transform_quad[2][1] + 2.)
    if mapping in ('ancestor', 'nested'):
        layer.transform_quad[2] = (layer.transform_quad[2][0] - 3., layer.transform_quad[2][1] + 1.)
    page.bound = BoundGeometry.circle(64.25, 64.75, 54.5)
    page.border_width, page.border_color = 4., '#ffcc8800'
    layer.bound = BoundGeometry.circle(64.25, 64.75, 55.5)
    selected.opacity = .54
    front.opacity = .35
    canvas.settings.eraser_size_px = dict.fromkeys(('small', 'medium', 'large'), 8)
    canvas.chapter.objects[page.children[-1].entity_id].opacity = .4
    image = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor.fromRgbF(.0713, .2817, .7891, .7243))
    canvas.tiles.set_tile(selected.object_id, (0, 0), image)
    canvas._invalidate_scene_cache()
    canvas.set_tool(tool)
    wait_scene(canvas)
    prepared = canvas._scene_controller.feedback
    assert prepared is not None
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_reference(canvas)))
    blocker = block_existing_scene_demand(canvas, monkeypatch)
    try:
        with monkeypatch.context() as guard:
            guard.setattr(canvas._scene_snapshot_compiler, 'capture', lambda *_a, **_k: pytest.fail('GUI scene capture'))
            guard.setattr(canvas._scene_controller.scheduler, 'submit', lambda *_a, **_k: pytest.fail('Obsolete scene submission'))
            for index, x in enumerate((18., 32., 88., 100.)):
                before = ready_paint(canvas)
                if index == 0:
                    begin(canvas, tool, QPointF(x, 64.))
                else:
                    (canvas._continue_paint_brush if tool == ToolKind.BRUSH else canvas._continue_stroke)(QPointF(x, 64.), 1.)
                actual = ready_paint(canvas)
                assert actual.pixelColor(round(x), 64) != before.pixelColor(round(x), 64)
                assert canvas._raster_feedback_contact_covered
                np.testing.assert_array_equal(pixels(actual), pixels(native_reference(canvas)))
        finish(canvas, tool)
    finally:
        if canvas._drawing:
            finish(canvas, tool)
        blocker.set()
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_reference(canvas)))
