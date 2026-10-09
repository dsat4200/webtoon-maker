"""Saved affine rasters show current native ink without scene evaluation."""
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


@pytest.mark.parametrize('mapping', ['rotation', 'shear', 'mirror', 'ancestor_rotation', 'nested_rotation'])
@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('format,opacity', [(QImage.Format_ARGB32_Premultiplied, 1.),
                                         (QImage.Format_RGBA64_Premultiplied, .43)])
def test_saved_affine_contacts_publish_every_native_buffer_and_release_exactly(
        scene, mapping, tool, format, opacity, wait_scene, monkeypatch):
    canvas, selected, front = scene
    boundary_scene(canvas, selected, front, format, opacity)
    layer = canvas.chapter.layers[selected.parent_layer_id]
    rotation = QTransform().translate(350., 70.).rotate(-3.).translate(-350., -70.)
    if mapping == 'mirror':
        map_saved_quad(selected, QTransform(-1., 0., 0., 1., 720., 0.))
    elif mapping == 'shear':
        map_saved_quad(selected, QTransform(1., .025, .04, 1., -4., -8.))
    elif mapping == 'ancestor_rotation':
        map_saved_quad(layer, rotation)
    else:
        map_saved_quad(selected, rotation)
        if mapping == 'nested_rotation':
            map_saved_quad(layer, QTransform().translate(350., 70.).rotate(1.25).translate(-350., -70.))
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


def test_zoomed_out_affine_feedback_owns_four_native_blocks_with_bounded_resources(
        scene, wait_scene, monkeypatch):
    canvas, selected, front = scene
    boundary_scene(canvas, selected, front, QImage.Format_RGBA64_Premultiplied, .43)
    canvas.chapter.width = 1080
    canvas.setFixedSize(378, 672)
    canvas.center_x, canvas.center_y, canvas.scale = 540., 960., .35
    map_saved_quad(selected, QTransform().rotate(-3.))
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    controller = canvas._scene_controller
    complete = controller.feedback
    document = canvas._render_document_state()
    visible = tuple(canvas.visible_document_rect().getRect())
    assert complete is not None and len(complete.tiles) == 4
    assert 32 * 1024 * 1024 < complete.byte_count <= 64 * 1024 * 1024
    assert all(tile.source.isNull() and tile.bounds[2:] == (1028, 1028) for tile in complete.tiles)
    assert controller._feedback_covers(document, visible)
    partial = prepare_raster_feedback(controller.snapshot, selected.object_id, visible,
                                      budget=32 * 1024 * 1024)
    assert partial is not None and partial.byte_count <= 32 * 1024 * 1024
    assert len(partial.tiles) < len(complete.tiles)
    controller.feedback = partial
    assert not controller._feedback_covers(document, visible), 'Budget truncation is not complete coverage'
    missing = next(tile for tile in complete.tiles if tile.key not in {tile.key for tile in partial.tiles})
    point = QPointF(missing.bounds[0] + 80., missing.bounds[1] + 80.)
    try:
        begin(canvas, ToolKind.RASTER_PENCIL, point)
        ready_paint(canvas)
        assert controller._contact_reuse_gate is None
        assert controller.capture is not None and controller.timer.isActive()
    finally:
        finish(canvas, ToolKind.RASTER_PENCIL)


def test_rotated_pruned_tile_reveals_prefix_and_preserves_original_feedback_handles(
        scene, wait_scene, monkeypatch):
    from test_transformed_raster_contact_feedback import configure_transforms
    canvas, selected, _front = scene
    configure_transforms(canvas, selected)
    map_saved_quad(selected, QTransform().translate(64., 64.).rotate(-3.).translate(-64., -64.))
    native = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    native.fill(Qt.transparent)
    local = canvas._raster_local_point(selected, QPointF(32., 64.))
    painter = QPainter(native)
    painter.fillRect(round(local.x()) - 2, round(local.y()) - 2, 4, 4, QColor('red'))
    painter.end()
    canvas.tiles.set_tile(selected.object_id, (0, 0), native)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    prepared = canvas._scene_controller.feedback
    assert prepared is not None and prepared.source_images is not None
    originals = {key: bytes(image.constBits()) for key, image in prepared.source_images.items()}
    blocker = block_existing_scene_demand(canvas, monkeypatch)
    try:
        begin(canvas, ToolKind.RASTER_ERASER, QPointF(32., 64.))
        np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_decorated(canvas)))
        assert originals == {key: bytes(image.constBits()) for key, image in prepared.source_images.items()}
        finish(canvas, ToolKind.RASTER_ERASER)
        assert (0, 0) not in canvas.tiles._tiles[selected.object_id].entries
        np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_decorated(canvas)))
    finally:
        if canvas._drawing:
            finish(canvas, ToolKind.RASTER_ERASER)
        blocker.set()
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_decorated(canvas)))
    canvas.command_stack.undo()
    assert canvas.tiles.tile(selected.object_id, (0, 0)) == native
