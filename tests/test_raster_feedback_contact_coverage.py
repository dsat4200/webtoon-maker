"""Budgeted native feedback proves all changed visible source footprints."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.render.raster_feedback import (
    RasterFeedback, RasterFeedbackTile, _feedback_source_keys, feedback_keys,
)
from comic_editor.render.raster_feedback_coverage import feedback_contact_covers
from comic_editor.render.service import RenderDocument


def prepared_fixture(*, transform=None, origin=(0, 0), dirty=(3, 3), patch_size=256):
    document = RenderDocument((1, 2, 3), ('native',), 7, 4096, 4096, '#ffffffff')
    visible = (0., 0., 2048., 2048.)
    gutter = 2
    image = QImage(patch_size + 2 * gutter, patch_size + 2 * gutter,
        QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    keys = feedback_keys(document, visible, origin, patch_size, gutter,
        budget=(patch_size + 2 * gutter) ** 2 * 4 * 3 * 9,
        focus=(origin[0] + (dirty[0] + .5) * patch_size,
               origin[1] + (dirty[1] + .5) * patch_size))
    mapping = QTransform(*transform) if transform is not None else QTransform.fromTranslate(*origin)
    inverse, valid = mapping.inverted()
    assert valid
    tiles = []
    for key in keys:
        bounds = (origin[0] + key[0] * patch_size - gutter,
            origin[1] + key[1] * patch_size - gutter, patch_size + 2 * gutter, patch_size + 2 * gutter)
        source_keys = _feedback_source_keys(bounds, inverse, 256) if transform is not None else ()
        tiles.append(RasterFeedbackTile(key, bounds, image, image, image, source_keys))
    prepared = RasterFeedback(document, 'selected', origin, 256, gutter, 1., (),
        tuple(tiles), transform, patch_size=patch_size)
    return prepared, document, visible


@pytest.mark.parametrize('origin,transform', [
    ((0, 0), None),
    ((-17, 23), None),
    ((0, 0), (1., 0., 0., 1., 4.25, 7.75)),
    ((0, 0), (.9, .05, -.05, .9, 85., 85.)),
    ((0, 0), (1., 0., .00001, 0., 1., .00001, 10., 10., 1.)),
])
def test_partial_view_can_cover_complete_native_dirty_footprint(origin, transform):
    prepared, document, visible = prepared_fixture(origin=origin, transform=transform)
    assert len(feedback_keys(document, visible, prepared.origin, prepared.world_size, budget=None)) > len(prepared.tiles)
    assert feedback_contact_covers(prepared, document, visible, {(3, 3)}, presentation_margin=4.)
    # An additional offscreen changed source does not make an unchanged visible
    # region wait for scene rendering. Its source footprint is still validated.
    assert feedback_contact_covers(prepared, document, visible, {(3, 3), (30, 30)}, presentation_margin=4.)


def test_contact_coverage_requires_every_visible_dirty_source_not_only_current_point():
    prepared, document, visible = prepared_fixture()
    assert feedback_contact_covers(prepared, document, visible, {(3, 3)})
    assert not feedback_contact_covers(prepared, document, visible, {(3, 3), (6, 6)})
    incomplete = replace(prepared, tiles=tuple(tile for tile in prepared.tiles if tile.key != (2, 2)))
    assert not feedback_contact_covers(incomplete, document, visible, {(3, 3)})


def test_native_gutter_neighbor_and_physical_filter_margin_are_part_of_coverage():
    prepared, document, _visible = prepared_fixture()
    single = replace(prepared, tiles=tuple(tile for tile in prepared.tiles if tile.key == (3, 3)))
    assert not feedback_contact_covers(single, document, (0., 0., 2048., 2048.), {(3, 3)})
    assert feedback_contact_covers(single, document, (768., 768., 256., 256.), {(3, 3)})
    assert not feedback_contact_covers(single, document, (768., 768., 256., 256.),
        {(3, 3)}, presentation_margin=4.)
    # At the document border no off-document patch is required.
    edge, document, _ = prepared_fixture(dirty=(0, 0))
    assert feedback_contact_covers(edge, document, (0., 0., 256., 256.), {(0, 0)}, presentation_margin=4.)


def test_mapped_patch_must_include_current_source_dependency():
    prepared, document, visible = prepared_fixture(transform=(1., 0., 0., 1., 4.25, 7.75))
    tile = next(tile for tile in prepared.tiles if tile.key == (4, 4))
    invalid = replace(prepared, tiles=tuple(replace(item, source_keys=()) if item is tile else item
        for item in prepared.tiles))
    assert not feedback_contact_covers(invalid, document, visible, {(3, 3)})


def test_evicted_edited_pixels_decline_without_source_decode_and_pruned_tiles_cover():
    prepared, document, visible = prepared_fixture()
    class SourceOwner:
        def __init__(self):
            self.entries = {(3, 3): 'encoded-source-path'}
            self.residency = SimpleNamespace(entries={})
        def get(self, *_args):
            pytest.fail('Coverage must not open source artwork')
    owner = SourceOwner()
    assert not feedback_contact_covers(prepared, document, visible, {(3, 3)}, source_owner=owner)
    owner.residency.entries[(owner, (3, 3))] = (prepared.tiles[0].source, 1)
    assert feedback_contact_covers(prepared, document, visible, {(3, 3)}, source_owner=owner)
    owner.entries.clear()
    owner.residency.entries.clear()
    assert feedback_contact_covers(prepared, document, visible, {(3, 3)}, source_owner=owner)
    assert not feedback_contact_covers(prepared, document, visible, {(3, 3)}, source_owner=None)


@pytest.mark.parametrize('change', ['identity', 'configuration', 'bounds', 'empty_dirty', 'bad_key',
                                  'bad_bounds', 'missing_plane', 'singular', 'nonfinite', 'horizon'])
def test_stale_or_invalid_native_coverage_never_claims_current_artwork(change):
    prepared, document, visible = prepared_fixture()
    dirty = {(3, 3)}
    if change == 'identity':
        document = replace(document, identity=(4, 5, 6))
    elif change == 'configuration':
        document = replace(document, configuration=('other',))
    elif change == 'bounds':
        document = replace(document, width=8192)
    elif change == 'empty_dirty':
        dirty = set()
    elif change == 'bad_key':
        dirty = {(3.5, 3)}
    elif change == 'bad_bounds':
        prepared = replace(prepared, tiles=tuple(replace(tile, bounds=(1., 2., 3., 4.))
            for tile in prepared.tiles))
    elif change == 'missing_plane':
        prepared = replace(prepared, tiles=tuple(replace(tile, source=QImage()) for tile in prepared.tiles))
    elif change == 'singular':
        prepared = replace(prepared, source_transform=(0., 0., 0., 0., 0., 0.))
    elif change == 'nonfinite':
        prepared = replace(prepared, source_transform=(1., 0., 0., 1., float('nan'), 0.))
    else:
        # Source tile x=768..1024 crosses this exact projective horizon.
        prepared = replace(prepared, source_transform=(1., 0., -1. / 900., 0., 1., 0., 0., 0., 1.))
    assert not feedback_contact_covers(prepared, document, visible, dirty)


def test_extreme_finite_mapping_rejects_address_growth_before_enumeration():
    prepared, document, visible = prepared_fixture()
    prepared = replace(prepared, source_transform=(100000., 0., 0., 100000., -76800000., -76800000.))
    assert not feedback_contact_covers(prepared, document, visible, {(3, 3)})


# Real rendering/input qualification complements the geometry guards above.
from test_raster_contact_feedback import scene, pixels, ready_paint


@pytest.mark.parametrize('tool_name', ['RASTER_PENCIL', 'RASTER_ERASER', 'BRUSH'])
def test_low_zoom_held_input_reuses_only_owned_dirty_coverage_and_matches_full_native_view(
        scene, tool_name, wait_scene, monkeypatch, qapp):
    from concurrent.futures import ThreadPoolExecutor
    import time
    import numpy as np
    from comic_editor.core.brushes import BrushDefinition
    from comic_editor.core.models import BoundGeometry
    from comic_editor.core.tools import ToolKind
    from comic_editor.render.outputs import capture_document
    from comic_editor.render.raster_feedback import prepare_raster_feedback
    from test_fractional_raster_feedback_native import fixed_native_reference

    canvas, selected, foreground = scene
    canvas.chapter.width = canvas.chapter.height = 4096
    canvas.chapter.background = '#ffffffff'
    canvas.setFixedSize(512, 512)
    canvas.center_x = canvas.center_y = 1152.
    canvas.scale = .3
    page = canvas.chapter.layers[selected.parent_layer_id]
    page.bound = BoundGeometry.rectangle(0., 0., 4096., 4096.)
    page.fill_color, page.border_width = '#ff0000ff', 0.
    selected.x, selected.y = 1100.25, 1100.75
    foreground.x, foreground.y = selected.x, selected.y
    canvas.settings.pencil_size_px = dict.fromkeys(('small', 'medium', 'large'), 24)
    canvas.settings.eraser_size_px = dict.fromkeys(('small', 'medium', 'large'), 24)
    tool = getattr(ToolKind, tool_name)
    if tool == ToolKind.RASTER_ERASER:
        source = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        source.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id, (0, 0), source)
    canvas._invalidate_scene_cache()
    canvas.set_tool(tool)
    wait_scene(canvas)
    controller = canvas._scene_controller
    snapshot = capture_document(canvas.chapter, canvas.tiles, canvas.images)
    snapshot = replace(snapshot, document=canvas._render_document_state())
    visible = canvas.visible_document_rect().intersected(snapshot.document.bounds).getRect()
    # Nine exact native plane triplets around the contact, independent of the
    # 64-key viewport. This uses the production renderer and unchanged grids.
    budget = 9 * 260 * 260 * 4 * 3
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared = worker.submit(prepare_raster_feedback, snapshot, selected.object_id,
            visible, budget=budget, focus=(1152., 1152.)).result(timeout=10.)
    assert prepared is not None and len(prepared.tiles) == 9
    assert prepared.byte_count <= budget
    controller.feedback = prepared
    controller.feedback_dirty.clear()
    controller.feedback_patches.clear()
    assert not controller._feedback_covers(canvas._render_document_state(), visible)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(fixed_native_reference(canvas)))
    original = {key: QImage(image) for key, image in canvas.tiles.object_tiles(selected.object_id).items()}
    history = canvas.command_stack.revision
    submissions = []
    original_submit = controller.scheduler.submit
    monkeypatch.setattr(controller.scheduler, 'submit', lambda demand: submissions.append(demand))
    try:
        previous = ready_paint(canvas)
        for index, point in enumerate((QPointF(1200.25, 1200.75), QPointF(1240.25, 1220.75),
                                       QPointF(1320.25, 1220.75))):
            if tool == ToolKind.BRUSH:
                if index == 0:
                    canvas._begin_paint_brush(point, 1.,
                        _definition=BrushDefinition(size=24, antialiasing=0, spacing=.08))
                else:
                    canvas._continue_paint_brush(point, 1.)
            else:
                (canvas._begin_stroke if index == 0 else canvas._continue_stroke)(point, 1.)
            deadline = time.monotonic() + 5.
            while time.monotonic() < deadline:
                qapp.processEvents()
                gate = getattr(canvas, '_paint_brush_tile_input' if tool == ToolKind.BRUSH
                    else '_raster_tile_input', None)
                if gate is None or not gate.busy:
                    break
                time.sleep(.001)
            assert gate is not None and not gate.busy
            actual = ready_paint(canvas)
            assert canvas._drawing and canvas.command_stack.revision == history
            assert feedback_contact_covers(prepared, canvas._render_document_state(), visible,
                controller.feedback_dirty, presentation_margin=1. / .3,
                source_owner=canvas.tiles._tiles[selected.object_id])
            widget = canvas.camera_transform().map(point)
            assert actual.pixelColor(round(widget.x()), round(widget.y())) != previous.pixelColor(
                round(widget.x()), round(widget.y()))
            np.testing.assert_array_equal(pixels(actual), pixels(fixed_native_reference(canvas)))
            assert not submissions, 'Covered native input enqueued heavy ordinary scene work'
            assert controller.capture is None and not controller.timer.isActive()
            assert controller.feedback is prepared
            assert canvas._raster_feedback_contact_covered and not canvas._raster_feedback_pending_visible
            previous = actual
    finally:
        monkeypatch.setattr(controller.scheduler, 'submit', original_submit)
        (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
    assert canvas.command_stack.revision == history + 1
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(fixed_native_reference(canvas)))
    canvas.command_stack.undo()
    assert canvas.tiles.object_tiles(selected.object_id) == original
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(fixed_native_reference(canvas)))


@pytest.mark.parametrize('tool_name', ['RASTER_PENCIL', 'RASTER_ERASER', 'BRUSH'])
def test_cold_wide_long_contact_can_use_64mib_private_planes_without_restarting_scene(
        scene, tool_name, wait_scene, monkeypatch, qapp):
    """The production private-lane cap retains native wide-view contact pixels."""
    import time
    import numpy as np
    from comic_editor.core.brushes import BrushDefinition
    from comic_editor.core.models import BoundGeometry
    from comic_editor.core.tools import ToolKind
    import comic_editor.render.raster_feedback as resources
    from test_fractional_raster_feedback_native import fixed_native_reference

    canvas, selected, foreground = scene
    canvas.chapter.width, canvas.chapter.height = 1080, 70000
    canvas.chapter.background = '#ffffffff'
    canvas.setFixedSize(512, 960)
    canvas.center_x, canvas.center_y, canvas.scale = 540., 51123., .3
    page = canvas.chapter.layers[selected.parent_layer_id]
    page.bound = BoundGeometry.rectangle(0., 0., 1080., 70000.)
    page.fill_color, page.border_width = '#ff0000ff', 0.
    selected.x, selected.y = -.75, 49996.3125
    foreground.x, foreground.y = selected.x, selected.y
    canvas.settings.pencil_size_px = dict.fromkeys(('small', 'medium', 'large'), 24)
    canvas.settings.eraser_size_px = dict.fromkeys(('small', 'medium', 'large'), 24)
    tool = getattr(ToolKind, tool_name)
    if tool == ToolKind.RASTER_ERASER:
        source = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        source.fill(QColor('red'))
        for y in range(-2, 12):
            canvas.tiles.set_tile(selected.object_id, (2, y), source)
    canvas._invalidate_scene_cache()
    canvas.set_tool(tool)
    wait_scene(canvas)
    controller = canvas._scene_controller
    old = controller.feedback
    assert old is not None and len(old.tiles) == 41 and old.byte_count <= 32 * 1024 * 1024
    controller.retire_feedback()
    document = canvas._render_document_state()
    visible = canvas.visible_document_rect().intersected(document.bounds).getRect()
    needed = feedback_keys(document, visible, (0, 0), 256, budget=None)
    assert len(needed) == 65
    original = {key: QImage(image) for key, image in canvas.tiles.object_tiles(selected.object_id).items()}
    history = canvas.command_stack.revision
    before = ready_paint(canvas)
    # Only the stable private feedback-only scheduler passes progressive
    # publication. Observe its production cap; ordinary preparation stays 32MiB.
    prepare = resources.prepare_raster_feedback
    private_calls = []
    def private_cap(*args, **kwargs):
        if kwargs.get('progress') is not None:
            assert kwargs.get('budget') == 64 * 1024 * 1024
            private_calls.append(kwargs['budget'])
        return prepare(*args, **kwargs)
    monkeypatch.setattr(resources, 'prepare_raster_feedback', private_cap)
    submissions = []
    submit = controller.scheduler.submit
    monkeypatch.setattr(controller.scheduler, 'submit', lambda demand: submissions.append(demand))
    def drain_and_paint(condition, timeout=10.):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            qapp.processEvents()
            image = ready_paint(canvas)
            if condition():
                return image
            time.sleep(.001)
        pytest.fail('Cold private native contact did not progress while held')
    try:
        first = QPointF(600., 49600.)
        if tool == ToolKind.BRUSH:
            canvas._begin_paint_brush(first, 1.,
                _definition=BrushDefinition(size=24, antialiasing=0, spacing=.2))
        else:
            canvas._begin_stroke(first, 1.)
        actual = drain_and_paint(lambda: controller.feedback is not None
            and len(controller.feedback.tiles) == 65 and controller._bootstrap is None)
        prepared = controller.feedback
        assert len(private_calls) == 1 and prepared.byte_count == 65 * 260 * 260 * 4 * 3
        assert prepared.byte_count <= 64 * 1024 * 1024
        assert prepared.tile_size == prepared.world_size == 256
        assert {tile.key for tile in prepared.tiles} == set(needed)
        assert all(tile.prefix.size().toTuple() == (260, 260) for tile in prepared.tiles)
        assert not feedback_contact_covers(old, canvas._render_document_state(), visible,
            controller.feedback_dirty, presentation_margin=1. / .3)
        first_widget = canvas.camera_transform().map(first)
        assert actual.pixelColor(round(first_widget.x()), round(first_widget.y())) != before.pixelColor(
            round(first_widget.x()), round(first_widget.y()))
        np.testing.assert_array_equal(pixels(actual), pixels(fixed_native_reference(canvas)))
        earlier_color = actual.pixelColor(round(first_widget.x()), round(first_widget.y()))
        for y in (50000., 50400., 50800., 51200., 51600., 52000., 52600.):
            point = QPointF(600., y)
            previous = ready_paint(canvas)
            (canvas._continue_paint_brush if tool == ToolKind.BRUSH else canvas._continue_stroke)(point, 1.)
            def native_ready():
                gate = getattr(canvas, '_paint_brush_tile_input' if tool == ToolKind.BRUSH
                    else '_raster_tile_input', None)
                return gate is not None and not gate.busy
            actual = drain_and_paint(native_ready)
            widget = canvas.camera_transform().map(point)
            assert actual.pixelColor(round(widget.x()), round(widget.y())) != previous.pixelColor(
                round(widget.x()), round(widget.y()))
            assert actual.pixelColor(round(first_widget.x()), round(first_widget.y())) == earlier_color
            assert canvas._drawing and canvas.command_stack.revision == history
            assert controller.feedback is prepared and controller._bootstrap is None
            assert controller.capture is None and not controller.timer.isActive()
            assert not submissions and len(private_calls) == 1
            assert feedback_contact_covers(prepared, canvas._render_document_state(), visible,
                controller.feedback_dirty, presentation_margin=1. / .3,
                source_owner=canvas.tiles._tiles[selected.object_id])
            assert not canvas._raster_feedback_pending_visible
            np.testing.assert_array_equal(pixels(actual), pixels(fixed_native_reference(canvas)))
    finally:
        monkeypatch.setattr(controller.scheduler, 'submit', submit)
        monkeypatch.setattr(resources, 'prepare_raster_feedback', prepare)
        (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
    assert canvas.command_stack.revision == history + 1
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(fixed_native_reference(canvas)))
    canvas.command_stack.undo()
    assert canvas.tiles.object_tiles(selected.object_id) == original
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(fixed_native_reference(canvas)))
