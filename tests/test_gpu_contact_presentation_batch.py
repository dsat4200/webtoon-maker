"""Actual GPU contact frames retain the former ordered presentation pixels.

Run with QT_QPA_PLATFORM=windows. Sources are synthetic; the native widget is
hidden and framebuffer readback belongs only to this verification code.
"""
from collections import Counter
from threading import Event
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, RasterObject
from comic_editor.core.tools import ToolKind
from comic_editor.ui.document_presentation import PresentationStats, draw_document_tiles
from test_document_projection_gpu import native_scene
from test_raster_contact_feedback import _queue_blocked_native_scene
from test_raster_feedback_bootstrap import drain_native


def _capture_frame(canvas, qapp):
    before = canvas.frames
    canvas.update()
    deadline = time.monotonic() + 5.
    while canvas.frames == before and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert canvas.frames > before, 'No actual owned GPU frame was captured'
    return canvas.frame_pixels.copy()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('rotation', [-.663008407605119, -38.],
                         ids=['captured-degrees', 'large-rotation-degrees'])
def test_rotated_contacts_batch_base_continuity_and_live_feedback_without_pixel_changes(
        native_scene, qapp, monkeypatch, wait_scene, tool, rotation):
    import comic_editor.ui.document_projection_features as projection_ui

    canvas = native_scene
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    layer = canvas.chapter.add_layer(page.layer_id, 'Nested native ink',
        BoundGeometry.rectangle(0., 0., 1024., 1024.), index=0)
    layer.fill_color, layer.border_width = None, 0
    layer.transform_frame = (0., 0., 1024., 1024.)
    layer.transform_quad = [(15.25, 10.5), (1003.5, 27.25),
                            (985.5, 999.25), (-2.75, 982.5)]
    drawing = canvas.chapter.add_object(layer.layer_id,
        RasterObject(x=-3.5, y=2.25, opacity=.43))
    for y in range(4):
        for x in range(4):
            image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
            image.fill(QColor('#7f800080'))
            canvas.tiles.set_tile(drawing.object_id, (x, y), image)
    canvas.primary_color = '#80ff3300'
    canvas.settings.pencil_size_px = dict.fromkeys(('small', 'medium', 'large'), 24)
    canvas.settings.eraser_size_px = dict.fromkeys(('small', 'medium', 'large'), 24)
    canvas.scale = .6308544791184257 / canvas.devicePixelRatioF()
    canvas.rotation = rotation  # Canvas/Qt rotations are stored in degrees.
    canvas.set_selection('object', drawing.object_id)
    canvas.set_tool(tool)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    _capture_frame(canvas, qapp)
    controller = canvas._scene_controller
    assert controller.feedback is not None
    assert controller.feedback.source_transform is not None
    original_sources = {key: QImage(image)
        for key, image in canvas.tiles.object_tiles(drawing.object_id).items()}
    history = canvas.command_stack.revision

    # Hold exact work before shared render admission, allowing legitimate
    # native source preparation and subsequent contacts to keep progressing.
    release, entered = Event(), Event()
    evaluate = controller.scheduler._evaluate
    def blocked(demand, token):
        entered.set()
        assert release.wait(20.), 'Exact-scene test barrier was not released'
        return evaluate(demand, token)
    monkeypatch.setattr(controller.scheduler, '_evaluate', blocked)
    _queue_blocked_native_scene(canvas)
    assert entered.wait(3.)

    presenter = canvas._document_tile_presenter
    save_state = presenter._save_state
    draw_groups = projection_ui.draw_document_tile_groups
    mode = {'batched': True}
    saved_frames, presented = [], []
    def observed_save_state():
        if mode['batched']:
            saved_frames.append(canvas.frames)
        return save_state()
    monkeypatch.setattr(presenter, '_save_state', observed_save_state)

    def observed_groups(painter, groups, camera, size, *, owner, smooth, pending_context=None):
        groups = tuple(groups)
        uploads, geometry = presenter.uploads, presenter.geometry_uploads
        stats = draw_groups(painter, groups, camera, size, owner=owner, smooth=smooth,
            pending_context=pending_context)
        assert stats.backend == 'gpu'
        assert stats.tiles == sum(len(group.tiles) for group in groups)
        assert stats.uploads == presenter.uploads - uploads
        assert presenter.geometry_uploads - geometry <= 1
        presented.append((canvas.frames, groups, stats))
        return stats
    monkeypatch.setattr(projection_ui, 'draw_document_tile_groups', observed_groups)

    def sequential_groups(painter, groups, camera, size, *, owner, smooth, pending_context=None):
        # The former native path entered/exited GL separately for the base,
        # each retained patch, and current feedback, in this exact order.
        stats = [draw_document_tiles(painter, group.tiles, camera, size,
            owner=owner, smooth=smooth, clip_world=group.clip_world,
            native_context=group.native_context, native_bounds=group.native_bounds,
            replace_background=group.replace_background) for group in groups]
        return PresentationStats('gpu', sum(item.tiles for item in stats),
            sum(item.uploads for item in stats), stats[-1].texture_bytes)

    def compare_current_frame():
        actual = _capture_frame(canvas, qapp)
        uploads = presenter.uploads
        unchanged = _capture_frame(canvas, qapp)
        np.testing.assert_array_equal(unchanged, actual)
        assert presenter.uploads == uploads
        assert canvas._document_presentation_stats.uploads == 0
        mode['batched'] = False
        try:
            with monkeypatch.context() as reference:
                reference.setattr(projection_ui, 'draw_document_tile_groups', sequential_groups)
                np.testing.assert_array_equal(_capture_frame(canvas, qapp), actual)
        finally:
            mode['batched'] = True

    try:
        for source_x in (240., 490., 735.):
            point = canvas._raster_world_point(drawing, QPointF(source_x, 512.))
            canvas._begin_stroke(point, 1.)
            canvas._continue_stroke(point + QPointF(18., 3.), .7)
            drain_native(canvas, qapp)
            assert controller.scheduler.busy and not release.is_set()
            assert canvas._drawing and canvas._raster_contact_active
            compare_current_frame()
            assert canvas._raster_feedback_contact_covered
            canvas._end_stroke()
            drain_native(canvas, qapp)
            compare_current_frame()
            assert controller.raster_continuity.overlays
        assert canvas.command_stack.revision == history + 3
        assert presented and any(stats.uploads > 0 for _, _, stats in presented)
        assert any(len(groups) >= 3 for _, groups, _ in presented), (
            'The regression must exercise retained continuity and current feedback')
        for _frame, groups, stats in presented:
            assert stats.tiles > len(groups[0].tiles), 'Live/retained tiles must enter frame statistics'
        assert Counter(saved_frames) == Counter(frame for frame, _, _ in presented)
        assert all(count == 1 for count in Counter(saved_frames).values()), (
            'Each ordinary contact paint must preserve GL state exactly once')
    finally:
        if canvas._drawing:
            canvas._end_stroke()
        release.set()
    wait_scene(canvas)
    assert not canvas._projection_frame_pending
    assert not canvas._projection_provisional_visible
    for _ in range(3):
        canvas.command_stack.undo()
    assert original_sources == canvas.tiles.object_tiles(drawing.object_id)
