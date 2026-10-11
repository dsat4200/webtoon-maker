"""Native neighboring regions remain owned across a held drawing boundary."""
from contextlib import contextmanager
from threading import get_ident

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (BoundGeometry, ChapterDocument, RasterObject, ImageObject,
    HueSaturationLightnessModifier, OutlineModifier)
from comic_editor.core.tiles import TileStore
from comic_editor.core.images import ImageStore
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tools import ToolKind
from comic_editor.render.admission import RENDER_ADMISSION, snapshot_working_bytes
from comic_editor.render.filtered_raster_feedback import FeedbackUnavailable
from comic_editor.render.filtered_feedback_worker import FilteredFeedbackWorker
from comic_editor.ui.canvas import CanvasWidget
from test_filtered_raster_contact_feedback import (
    begin, move, finish, wait_feedback, source_pixels, native_presentation,
    ready_paint, pixels,
)


@pytest.fixture
def neighboring_scene(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    admissions = []
    original_reserve = RENDER_ADMISSION.reserve
    @contextmanager
    def reserve(owner, size, **kwargs):
        with original_reserve(owner, size, **kwargs) as ticket:
            if owner.startswith('native-filtered-'):
                admissions.append((owner, ticket.estimated_bytes, get_ident()))
            yield ticket
    monkeypatch.setattr(RENDER_ADMISSION, 'reserve', reserve)
    chapter = ChapterDocument(width=1080, height=2048, document_kind='asset', background='#00000000')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0., 0., 1080., 2048.))
    page.fill_color, page.border_width = '#a0775566', 1.
    layer = chapter.add_layer(page.layer_id, 'Branch',
        bound=BoundGeometry.rectangle(10.25, .5, 1050.5, 2046.5))
    layer.fill_color = '#40112233'
    selected = chapter.add_object(layer.layer_id,
        RasterObject(opacity=.54, interaction_rect=(0., 0., 1024., 2048.)))
    chapter.add_modifier(HueSaturationLightnessModifier(hue=51., saturation=38., lightness=-26.),
        [('object', selected.object_id)])
    chapter.add_modifier(OutlineModifier(thickness=12., color='#ffffffff', opacity=100., antialiasing=True),
        [('object', selected.object_id)])
    tiles, images = TileStore(), ImageStore()
    for index in range(8):
        obj = chapter.add_object(layer.layer_id,
            ImageObject(x=0., y=512., opacity=.045, pixel_width=1024, pixel_height=1024))
        image = QImage(1024, 1024, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(60 + index * 13, 90, 140, 145 + index * 5))
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        assert buffer.open(QIODevice.WriteOnly)
        assert image.save(buffer, 'PNG')
        buffer.close()
        images.put_decoded(obj.object_id, f'plane-{index}.png', bytes(encoded), image)
    layer.children.reverse()
    for x in range(4):
        for y in range(8):
            image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
            image.fill(QColor(60, 70 + x * 11, 80 + y * 9, 145))
            tiles.set_tile(selected.object_id, (x, y), image)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', predictive_ink=False,
        grid_overlay_visible=False, snap_to_grid=False))
    canvas.setFixedSize(512, 512)
    canvas.set_document(chapter, tiles, images)
    canvas.center_x, canvas.center_y, canvas.scale = 512., 1024., 1.
    canvas.primary_color = '#ffffff00'
    canvas.settings.pencil_size_px = dict.fromkeys(('small', 'medium', 'large'), 16)
    canvas.settings.eraser_size_px = dict.fromkeys(('small', 'medium', 'large'), 16)
    canvas.set_selection('object', selected.object_id)
    canvas._test_neighbor_admissions = admissions
    yield canvas, selected
    state = canvas._scene_controller._filtered_state
    canvas._scene_controller.reset()
    if state is not None:
        state.worker.executor.shutdown(wait=True)
    canvas._scene_controller.scheduler.close()
    canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_held_contacts_cross_two_native_regions_without_fallback_or_history_loss(
        neighboring_scene, tool, wait_scene, qapp):
    canvas, obj = neighboring_scene
    canvas.set_tool(tool)
    wait_scene(canvas, timeout=60)
    controller = canvas._scene_controller
    state = controller._filtered_state
    assert state is not None
    assert len(state.prepared.recipes) == 2
    assert 64 * 1024 * 1024 < state.prepared.byte_count <= 128 * 1024 * 1024
    basis = [(bytes(recipe.initial.constBits()), bytes(recipe.source_image.constBits()))
             for recipe in state.prepared.recipes]
    original = source_pixels(canvas, obj)
    history, submitted = canvas.command_stack.revision, controller.scheduler.submitted
    for index, point in enumerate((QPointF(520., 1052.), QPointF(523., 1024.), QPointF(516., 996.))):
        begin(canvas, point, tool) if index == 0 else move(canvas, point, tool)
        actual = wait_feedback(canvas, qapp, timeout=30)
        assert controller._contact_reuse_gate is not None
        assert controller.scheduler.submitted == submitted
        np.testing.assert_array_equal(pixels(actual), pixels(native_presentation(canvas)))
    assert len(state.updates) == 2
    expected_reservation = snapshot_working_bytes(state.worker.snapshot) + state.prepared.byte_count
    reservations = canvas._test_neighbor_admissions
    assert {'native-filtered-prepare', 'native-filtered-contact'} <= {row[0] for row in reservations}
    assert all(size == expected_reservation and thread != get_ident()
               for _owner, size, thread in reservations)
    assert basis == [(bytes(recipe.initial.constBits()), bytes(recipe.source_image.constBits()))
                     for recipe in state.prepared.recipes]
    finish(canvas, tool)
    wait_scene(canvas, timeout=60)
    changed = source_pixels(canvas, obj)
    assert changed != original and canvas.command_stack.revision == history + 1
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_presentation(canvas)))
    canvas.command_stack.undo()
    wait_scene(canvas, timeout=60)
    assert source_pixels(canvas, obj) == original
    canvas.command_stack.redo()
    wait_scene(canvas, timeout=60)
    assert source_pixels(canvas, obj) == changed
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_presentation(canvas)))


def test_explicit_lower_capacity_rejects_complete_neighbor_ownership_before_starting_work(
        neighboring_scene, wait_scene):
    canvas, obj = neighboring_scene
    wait_scene(canvas, timeout=60)
    state = canvas._scene_controller._filtered_state
    assert state is not None and len(state.prepared.recipes) == 2
    assert state.prepared.byte_count > 64 * 1024 * 1024
    with pytest.raises(FeedbackUnavailable, match='prepared bounded basis'):
        FilteredFeedbackWorker(state.prepared.snapshot, obj.object_id, state.prepared.recipes,
            prepared_budget=64 * 1024 * 1024)
