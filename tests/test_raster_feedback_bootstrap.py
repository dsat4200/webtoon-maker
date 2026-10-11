"""A cold capture progresses across native input without losing dirty sources."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage

from comic_editor.core.changes import ChangeSet, EntityChange, OrderChange, ResourceChange
from comic_editor.core.brushes import BrushDefinition
from comic_editor.render.raster_feedback import prepare_raster_feedback
from comic_editor.render.raster_feedback_bootstrap import RasterFeedbackBootstrap
from comic_editor.ui.canvas import ToolKind
from test_raster_contact_feedback import scene, pixels, ready_paint, native_reference


def complete_capture(bootstrap, canvas):
    for _ in range(10000):
        if bootstrap.advance(canvas._render_document_state(), seconds=0.):
            assert bootstrap.snapshot is not None
            return bootstrap.snapshot
    pytest.fail('Cold metadata capture did not finish')


def bootstrap_for(canvas, selected):
    document = canvas._render_document_state()
    visible = canvas.visible_document_rect().intersected(document.bounds)
    return RasterFeedbackBootstrap(canvas, document, selected.object_id, visible.getRect())


def source_change(selected, *keys):
    return ChangeSet(resources=tuple(ResourceChange(('object', selected.object_id), 'raster', key)
        for key in keys), transient=True)


def drain_native(canvas, qapp):
    deadline = time.monotonic() + 5.
    while time.monotonic() < deadline:
        qapp.processEvents()
        gate = getattr(canvas, '_paint_brush_tile_input' if canvas.tool == ToolKind.BRUSH
            else '_raster_tile_input', None)
        if gate is None or not gate.busy:
            return
        time.sleep(.001)
    pytest.fail('Native source input did not finish')


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_cold_capture_survives_stream_and_presents_changed_held_pixels_against_native_oracle(
        scene, tool, wait_scene, monkeypatch, qapp):
    canvas, selected, _foreground = scene
    canvas.primary_color = '#ffff0000'
    if tool == ToolKind.RASTER_ERASER:
        tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        tile.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id, (0, 0), tile)
        canvas._invalidate_scene_cache()
    canvas.set_tool(tool)
    wait_scene(canvas)
    before = ready_paint(canvas)
    controller = canvas._scene_controller
    controller.retire_feedback()
    bootstrap = bootstrap_for(canvas, selected)
    assert not bootstrap.advance(canvas._render_document_state(), seconds=0.)
    original_changed = controller.changed
    def changed(change, **kwargs):
        bootstrap.changed(change)
        return original_changed(change, **kwargs)
    monkeypatch.setattr(controller, 'changed', changed)
    # The independent capture is the resource under qualification. Prevent an
    # ordinary revision completion or a second automatic owner from supplying it.
    monkeypatch.setattr(controller.scheduler, 'submit', lambda *_: None)
    history = canvas.command_stack.revision
    for index, x in enumerate((24., 32., 40., 72., 80., 88., 100.)):
        point = QPointF(x, 64.)
        if tool == ToolKind.BRUSH:
            if index == 0:
                canvas._begin_paint_brush(point, 1., _definition=BrushDefinition(size=12, antialiasing=0, spacing=.08))
            else:
                canvas._continue_paint_brush(point, 1.)
        else:
            (canvas._begin_stroke if index == 0 else canvas._continue_stroke)(point, 1.)
        drain_native(canvas, qapp)
        bootstrap.advance(canvas._render_document_state(), seconds=0.)
        assert not bootstrap.retired
        assert canvas._drawing and canvas.command_stack.revision == history
    snapshot = complete_capture(bootstrap, canvas)
    assert not snapshot.document.live_preview and not snapshot.document.contact_only
    assert snapshot.cache_spec is None and snapshot.graphics_worker is None
    # Preparation is the production native prefix/source/suffix renderer on a
    # detached worker, rather than presentation or GUI source decoding.
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared = worker.submit(prepare_raster_feedback, snapshot, selected.object_id,
            bootstrap.visible).result(timeout=5.)
    assert prepared is not None
    controller.feedback = prepared
    controller.feedback_dirty = set(bootstrap.dirty)
    controller.feedback_patches.clear()
    assert controller.feedback_dirty == {(0, 0)}
    actual, expected = ready_paint(canvas), native_reference(canvas)
    assert actual.pixelColor(80, 64) != before.pixelColor(80, 64)
    assert actual.pixelColor(80, 64) == QColor('blue' if tool == ToolKind.RASTER_ERASER else 'red')
    assert actual.pixelColor(56, 64) == QColor('green')
    np.testing.assert_array_equal(pixels(actual)[2:126, 8:504], pixels(expected)[2:126, 8:504])
    assert canvas._drawing and canvas.command_stack.revision == history
    with monkeypatch.context() as presentation:
        presentation.setattr(canvas, '_render_scene_layers', lambda *_a, **_k: pytest.fail('GUI scene render'))
        presentation.setattr(canvas.tiles.residency, 'get', lambda *_a, **_k: pytest.fail('GUI source decode'))
        assert ready_paint(canvas).pixelColor(80, 64) == actual.pixelColor(80, 64)
    (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
    drain_native(canvas, qapp)
    assert canvas.command_stack.revision == history + 1


def test_native_map_clone_owns_original_after_add_prune_and_undo(scene, monkeypatch):
    canvas, selected, _front = scene
    red = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    red.fill(QColor('red'))
    canvas.tiles.set_tile(selected.object_id, (0, 0), red)
    bootstrap = bootstrap_for(canvas, selected)
    assert not bootstrap.advance(canvas._render_document_state(), seconds=0.)
    canvas.tiles.set_tile(selected.object_id, (0, 0), None)
    canvas.tiles.set_tile(selected.object_id, (1, 0), red)
    bootstrap.changed(source_change(selected, (0, 0), (1, 0)))
    # A restore is still an eligible source packet; neither the original dirty
    # address nor the newly added address can be dropped while capture yields.
    canvas.tiles.set_tile(selected.object_id, (0, 0), red)
    bootstrap.changed(source_change(selected, (0, 0)))
    snapshot = complete_capture(bootstrap, canvas)
    assert bootstrap.dirty == {(0, 0), (1, 0)}
    assert snapshot.tiles.tile(selected.object_id, (0, 0)).pixelColor(80, 64) == QColor('red')
    assert snapshot.tiles.tile(selected.object_id, (1, 0)) is None
    assert canvas.tiles.tile(selected.object_id, (1, 0)).pixelColor(80, 64) == QColor('red')


@pytest.mark.parametrize('mutation', ['selection', 'configuration', 'visible', 'other-preview'])
def test_capture_ownership_changes_retire_before_publication(scene, mutation):
    canvas, selected, _front = scene
    bootstrap = bootstrap_for(canvas, selected)
    document = canvas._render_document_state()
    visible = bootstrap.visible
    if mutation == 'selection':
        canvas.selected_id = 'another'
    elif mutation == 'configuration':
        document = replace(document, configuration=(*document.configuration, 'changed'))
    elif mutation == 'visible':
        visible = (1., *visible[1:])
    else:
        canvas._gradient_preview_active = True
    assert bootstrap.advance(document, visible, seconds=0.)
    assert bootstrap.retired and bootstrap.snapshot is None


@pytest.mark.parametrize('change', [
    ChangeSet(conservative=True),
    ChangeSet(document_fields=frozenset({'background'})),
    ChangeSet(orders=(OrderChange('layers', (), ('new',)),)),
    ChangeSet(entities=(EntityChange(('object', 'other'), frozenset({'opacity'})),)),
    ChangeSet(resources=(ResourceChange(('object', 'selected'), 'raster', None),)),
])
def test_unowned_or_unaddressed_changes_retire_the_bootstrap(scene, change):
    canvas, selected, _front = scene
    bootstrap = bootstrap_for(canvas, selected)
    assert not bootstrap.changed(change)
    assert bootstrap.retired and bootstrap.snapshot is None


def test_large_visible_native_map_declines_before_any_capture_or_source_read(scene, monkeypatch):
    canvas, selected, _front = scene
    source = canvas.tiles._object_tiles(selected.object_id)
    source.entries = {(x, 0): Path(f'undecoded-native-source-{x}.png') for x in range(129)}
    entries = dict(source.entries)
    canvas.chapter.width = 256 * 130
    canvas.center_x = canvas.chapter.width / 2
    canvas.scale = canvas.width() / canvas.chapter.width
    with monkeypatch.context() as bounded:
        bounded.setattr('comic_editor.render.raster_feedback_bootstrap.SceneCapture._capture_tiles',
            lambda *_: pytest.fail('Unbounded selected native map freeze'))
        bounded.setattr(canvas.tiles.residency, 'get', lambda *_: pytest.fail('GUI source read'))
        bootstrap = bootstrap_for(canvas, selected)
    assert bootstrap.retired and bootstrap.capture is None and bootstrap.snapshot is None
    assert source.entries == entries and source.versions == {}


def test_seeded_and_fresh_capture_keep_precontact_dirty_artwork_and_match_held_native_pixels(
        scene, wait_scene, monkeypatch):
    canvas, selected, front = scene
    wait_scene(canvas)
    previous = canvas._scene_snapshot_compiler
    original_record = previous.records[('object', front.object_id)]
    assert original_record.opacity == 1.
    front.opacity = .5
    change = ChangeSet(entities=(EntityChange(('object', front.object_id), frozenset({'opacity'})),))
    canvas._publish_change_set(change)
    original_dirty = set(previous.dirty)
    assert ('object', front.object_id) in original_dirty
    seeded = bootstrap_for(canvas, selected)
    assert seeded.seeded_records
    with monkeypatch.context() as independent:
        independent.setattr(previous, 'conservative', True)
        fresh = bootstrap_for(canvas, selected)
    assert not fresh.seeded_records
    controller = canvas._scene_controller
    original_changed = controller.changed
    def changed(change, **kwargs):
        seeded.changed(change)
        fresh.changed(change)
        return original_changed(change, **kwargs)
    monkeypatch.setattr(controller, 'changed', changed)
    monkeypatch.setattr(controller.scheduler, 'submit', lambda *_: None)
    canvas._begin_stroke(QPointF(32, 64), 1.)
    canvas._continue_stroke(QPointF(100, 64), 1.)
    previous_dirty_at_capture = set(previous.dirty)
    snapshots = [complete_capture(owner, canvas) for owner in (seeded, fresh)]
    assert snapshots[0].chapter.objects[front.object_id].opacity == .5
    assert snapshots[1].chapter.objects[front.object_id].opacity == .5
    assert previous.records[('object', front.object_id)] is original_record
    assert original_record.opacity == 1. and previous.dirty == previous_dirty_at_capture
    assert seeded.compiler.copied_records < fresh.compiler.copied_records
    assert seeded.compiler.records is not previous.records
    assert seeded.compiler.stroke_records is not previous.stroke_records
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared = [worker.submit(prepare_raster_feedback, snapshot, selected.object_id,
            owner.visible).result(timeout=5.) for owner, snapshot in zip((seeded, fresh), snapshots)]
    assert all(item is not None for item in prepared)
    assert [tile.prefix for tile in prepared[0].tiles] == [tile.prefix for tile in prepared[1].tiles]
    assert [tile.suffix for tile in prepared[0].tiles] == [tile.suffix for tile in prepared[1].tiles]
    images = []
    for owner, feedback in zip((seeded, fresh), prepared):
        controller.feedback = feedback
        controller.feedback_dirty = set(owner.dirty)
        controller.feedback_patches.clear()
        images.append(ready_paint(canvas))
    np.testing.assert_array_equal(pixels(images[0]), pixels(images[1]))
    assert images[0].pixelColor(80, 64) == QColor('red')
    np.testing.assert_array_equal(pixels(images[0])[2:126, 8:504],
        pixels(native_reference(canvas))[2:126, 8:504])
    assert canvas._drawing
    canvas._end_stroke()


def test_seeded_pin_index_is_private_and_rejects_replaced_clean_native_source(
        scene, wait_scene, tmp_path):
    canvas, selected, _front = scene
    red = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    red.fill(QColor('red'))
    red_path = tmp_path / 'original-native-red.png'
    assert red.save(str(red_path))
    source = canvas.tiles._object_tiles(selected.object_id)
    source.register((0, 0), red_path)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    previous = canvas._scene_snapshot_compiler
    address = ('document', selected.object_id, (0, 0))
    original_pin = previous.resource_pins[address]
    unchanged = bootstrap_for(canvas, selected)
    assert unchanged.compiler.resource_pins is not previous.resource_pins
    assert unchanged.compiler.resource_pins[address] == original_pin
    # Rebinding a clean resource can have the same numeric generation. The
    # ordinary path witness must still replace its previous immutable pin job.
    blue = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    blue.fill(QColor('blue'))
    blue_path = tmp_path / 'replacement-native-blue.png'
    assert blue.save(str(blue_path))
    source.residency.remove(source, (0, 0))
    source.register((0, 0), blue_path)
    canvas._publish_change_set(source_change(selected, (0, 0)))
    replaced = bootstrap_for(canvas, selected)
    assert replaced.compiler.resource_pins[address][1] == blue_path
    assert replaced.compiler.resource_pins[address][2] is not original_pin[2]
    assert previous.resource_pins[address] == original_pin
    snapshot = complete_capture(replaced, canvas)
    with ThreadPoolExecutor(max_workers=1) as worker:
        actual = worker.submit(lambda: snapshot.finish_sources().tiles.tile(selected.object_id, (0, 0))).result(timeout=5.)
    assert actual.pixelColor(80, 64) == QColor('blue')
