"""Cold Brush contacts keep native pixels and accepted packet order off GUI."""
import base64
import time
from dataclasses import replace
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QIODevice, QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushTip
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tile_backing import TileResidency
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.002)
    assert predicate()


def make_canvas(tmp_path, *, cold, tool, native=False):
    chapter = ChapterDocument(width=512, height=512, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 512, 512))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 512, 512)))
    tiles = TileStore()
    image = QImage(256, 256, QImage.Format_RGBA64_Premultiplied if native
                   else QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#80336299'))
    for key in ((0, 0), (1, 0)):
        if cold or native:
            path = tmp_path / f'{key[0]}.png'
            assert image.save(str(path), 'PNG')
            if cold:
                tiles._object_tiles(raster.object_id).register(key, path)
            else:
                # Compare against the same native saved source representation.
                tiles.set_tile(raster.object_id, key, QImage(str(path)))
        else:
            tiles.set_tile(raster.object_id, key, QImage(image))
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', snap_to_grid=False,
                                        grid_overlay_visible=False))
    canvas.set_document(chapter, tiles)
    canvas.set_selection('object', raster.object_id)
    canvas.set_tool(tool)
    canvas.primary_color = '#a0dd3311'
    definition = BrushDefinition(size=24, spacing=.23, spray=True, particle_size=3,
        particle_density=2, repeat_mode='random', post_correction=.2,
        dynamics={'size': BrushDynamics(pressure=True), 'opacity': BrushDynamics(pressure=True)})
    canvas.settings.active_paint_brush = lambda: definition
    if cold:
        owner = tiles._tiles[raster.object_id]
        for key in tuple(owner.residency.entries):
            if key[0] is owner:
                owner.residency.evict(owner, key[1])
        assert not owner.residency.entries
    return canvas, raster


def pixels(canvas, raster):
    return {key: (image.format(), bytes(image.constBits()))
            for key, image in canvas.tiles.object_tiles(raster.object_id).items()}


def close(canvas):
    canvas._cancel_paint_brush()
    canvas._cancel_lasso_brush()
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas.deleteLater()


def contact(canvas, tool, *, release=True):
    points = [(30, 30, .3, 10.), (270, 30, .8, 10.016), (270, 180, .5, 10.033)]
    if tool == ToolKind.BRUSH:
        for index, (x, y, pressure, timestamp) in enumerate(points):
            canvas._paint_brush_input_axes = (12., -7., 33.)
            canvas._paint_brush_packet_time = timestamp
            (canvas._begin_paint_brush if index == 0 else canvas._continue_paint_brush)(QPointF(x, y), pressure)
        if release:
            canvas._finish_paint_brush()
    else:
        canvas._begin_lasso_brush(QPointF(*points[0][:2]))
        for x, y, *_ in points[1:]:
            canvas._continue_lasso_brush(QPointF(x, y))
        if release:
            canvas._finish_lasso_brush()


@pytest.mark.parametrize('tool', [ToolKind.BRUSH, ToolKind.LASSO_BRUSH])
@pytest.mark.parametrize('native', [False, True])
def test_cold_packets_release_preserves_native_output_and_one_history(qapp, tmp_path, monkeypatch, tool, native):
    from comic_editor.ui import tile_input
    warm, warm_raster = make_canvas(tmp_path, cold=False, tool=tool, native=native)
    before = pixels(warm, warm_raster)
    contact(warm, tool)
    wait_for(lambda: (warm._paint_brush_tile_input is None if tool == ToolKind.BRUSH
                     else warm._lasso_brush is None))
    expected = pixels(warm, warm_raster)
    cold, raster = make_canvas(tmp_path, cold=True, tool=tool, native=native)
    entered, release = Event(), Event()
    original_prepare, original_get = tile_input.prepare_input_tiles, TileResidency.get
    gui, reads = get_ident(), []
    def prepare(*args):
        entered.set()
        assert release.wait(10)
        return original_prepare(*args)
    prepare.admission_priority = original_prepare.admission_priority
    prepare.working_bytes = original_prepare.working_bytes
    prepare.snapshot_working_bytes = original_prepare.snapshot_working_bytes
    def source_get(residency, owner, key, path):
        if (owner, key) not in residency.entries:
            assert get_ident() != gui, 'Cold native source read on GUI'
            reads.append(get_ident())
        return original_get(residency, owner, key, path)
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    monkeypatch.setattr(TileResidency, 'get', source_get)
    commits = []
    errors = []
    cold.documentChanged.connect(commits.append)
    cold.operationError.connect(lambda *_args: errors.append(_args))
    try:
        contact(cold, tool)
        admitted = (cold._paint_brush_tile_input if tool == ToolKind.BRUSH
                    else cold._lasso_brush['input_gate'])
        wait_for(lambda: entered.is_set() or bool(errors) or admitted.closed)
        assert not errors, errors
        assert not admitted.closed, ('Early discard', admitted.current(), admitted.jobs.active,
                                     cold._render_document_state())
        gate = (cold._paint_brush_tile_input if tool == ToolKind.BRUSH
                else cold._lasso_brush['input_gate'])
        assert gate.released and gate.busy and not commits
        release.set()
        wait_for(lambda: gate.closed)
        assert gate.error is None
        assert reads and all(thread != gui for thread in reads)
        # Native pixels are resident here; equality never drives a cold GUI read.
        assert pixels(cold, raster) == expected
        assert len(commits) == 1 and len(cold.command_stack._undo) == 1
        cold.command_stack.undo()
        assert pixels(cold, raster) == before
        cold.command_stack.redo()
        assert pixels(cold, raster) == expected
    finally:
        release.set()
        close(cold)
        close(warm)


@pytest.mark.parametrize('tool', [ToolKind.BRUSH, ToolKind.LASSO_BRUSH])
def test_cancel_pending_cold_contact_never_publishes(qapp, tmp_path, monkeypatch, tool):
    from comic_editor.ui import tile_input
    canvas, raster = make_canvas(tmp_path, cold=True, tool=tool)
    entered, release = Event(), Event()
    original = tile_input.prepare_input_tiles
    def prepare(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    prepare.admission_priority, prepare.working_bytes = original.admission_priority, original.working_bytes
    prepare.snapshot_working_bytes = original.snapshot_working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    try:
        contact(canvas, tool, release=False)
        wait_for(entered.is_set)
        gate = (canvas._paint_brush_tile_input if tool == ToolKind.BRUSH
                else canvas._lasso_brush['input_gate'])
        (canvas._cancel_paint_brush if tool == ToolKind.BRUSH else canvas._cancel_lasso_brush)()
        release.set()
        wait_for(lambda: gate.jobs.active is None)
        assert gate.closed and not gate.buffers and not canvas._drawing
        assert not canvas.command_stack.can_undo
        assert set(canvas.tiles._tiles[raster.object_id]) == {(0, 0), (1, 0)}
        assert not canvas.tiles.residency.entries
    finally:
        release.set()
        close(canvas)


def image_tip():
    image = QImage(32, 16, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#a0ff2222'))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.WriteOnly) and image.save(buffer, 'PNG')
    return BrushTip('Authored', 32, 16, base64.b64encode(bytes(data)).decode(), 'color', 'image')


def test_authored_material_first_contact_is_worker_owned_and_ordered(qapp, tmp_path, monkeypatch):
    from comic_editor.core import brush_raster
    from comic_editor.ui import brush_features
    canvas, raster = make_canvas(tmp_path, cold=False, tool=ToolKind.BRUSH)
    definition = replace(canvas.settings.active_paint_brush(), tips=(image_tip(),), spray=False)
    canvas.settings.active_paint_brush = lambda: definition
    entered, release = Event(), Event()
    gui, threads = get_ident(), []
    original = brush_features.prepare_material_resources
    def prepare(*args):
        threads.append(get_ident())
        entered.set()
        assert release.wait(10)
        return original(*args)
    prepare.admission_priority, prepare.working_bytes = original.admission_priority, original.working_bytes
    prepare.snapshot_working_bytes = getattr(original, 'snapshot_working_bytes', lambda *_args: 0)
    monkeypatch.setattr(brush_features, 'prepare_material_resources', prepare)
    monkeypatch.setattr(brush_raster._material_cache, 'get',
                        lambda *_a, **_k: pytest.fail('Live global material cache was sampled'))
    samples = []
    ordinary_add = brush_raster.RasterBrushStroke.add
    def add(stroke, sample):
        samples.append(sample)
        return ordinary_add(stroke, sample)
    monkeypatch.setattr(brush_raster.RasterBrushStroke, 'add', add)
    try:
        before = pixels(canvas, raster)
        contact(canvas, ToolKind.BRUSH)
        wait_for(entered.is_set)
        gate = canvas._paint_brush_tile_input
        assert gate.busy and gate.released and pixels(canvas, raster) == before
        release.set()
        wait_for(lambda: gate.closed)
        assert gate.error is None and threads == [threads[0]] and threads[0] != gui
        assert [(sample.pressure, sample.time, sample.tilt_x, sample.tilt_y, sample.rotation)
                for sample in samples] == [(.8, 10.016, 12., -7., 33.), (.5, 10.033, 12., -7., 33.)]
        assert pixels(canvas, raster) != before and len(canvas.command_stack._undo) == 1
    finally:
        release.set()
        close(canvas)


def test_failed_authored_material_reports_terminal_error_and_restores(qapp, tmp_path):
    canvas, raster = make_canvas(tmp_path, cold=False, tool=ToolKind.BRUSH)
    before, frame = pixels(canvas, raster), raster.interaction_rect
    definition = replace(canvas.settings.active_paint_brush(), tips=(BrushTip(png='broken', shape='image'),))
    canvas.settings.active_paint_brush = lambda: definition
    errors, commits = [], []
    canvas.operationError.connect(lambda *_args: errors.append(_args))
    canvas.documentChanged.connect(commits.append)
    try:
        contact(canvas, ToolKind.BRUSH)
        wait_for(lambda: bool(errors))
        assert len(errors) == 1 and not canvas._drawing and canvas._paint_brush_stroke is None
        assert pixels(canvas, raster) == before and raster.interaction_rect == frame
        assert not commits and not canvas.command_stack.can_undo
        assert canvas._native_input_error is not None
        canvas.settings.active_paint_brush = lambda: BrushDefinition(size=12)
        contact(canvas, ToolKind.BRUSH)
        wait_for(lambda: canvas._paint_brush_tile_input is None)
        assert canvas._native_input_error is None and len(errors) == 1
        assert len(commits) == 1 and len(canvas.command_stack._undo) == 1
    finally:
        close(canvas)


def test_cancel_pending_authored_material_never_installs_or_publishes(qapp, tmp_path, monkeypatch):
    from comic_editor.ui import brush_features
    canvas, raster = make_canvas(tmp_path, cold=False, tool=ToolKind.BRUSH)
    definition = replace(canvas.settings.active_paint_brush(), tips=(image_tip(),), spray=False)
    canvas.settings.active_paint_brush = lambda: definition
    entered, release = Event(), Event()
    original = brush_features.prepare_material_resources
    def prepare(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    prepare.admission_priority, prepare.working_bytes = original.admission_priority, original.working_bytes
    prepare.snapshot_working_bytes = getattr(original, 'snapshot_working_bytes', lambda *_args: 0)
    monkeypatch.setattr(brush_features, 'prepare_material_resources', prepare)
    try:
        before = pixels(canvas, raster)
        contact(canvas, ToolKind.BRUSH, release=False)
        gate = canvas._paint_brush_tile_input
        wait_for(entered.is_set)
        canvas._cancel_paint_brush()
        release.set()
        wait_for(lambda: gate.jobs.active is None)
        assert gate.closed and not gate.buffers and not canvas._drawing
        assert pixels(canvas, raster) == before and not canvas.command_stack.can_undo
    finally:
        release.set()
        close(canvas)


@pytest.mark.parametrize(('first_tool', 'second_tool'), [
    (ToolKind.BRUSH, ToolKind.RASTER_PENCIL),
    (ToolKind.RASTER_PENCIL, ToolKind.BRUSH),
    (ToolKind.BRUSH, ToolKind.LASSO_BRUSH),
    (ToolKind.LASSO_BRUSH, ToolKind.BRUSH)])
def test_cold_cross_tool_contacts_keep_two_ordered_histories(qapp, tmp_path, monkeypatch, first_tool, second_tool):
    from comic_editor.ui import tile_input
    def pencil(canvas):
        assert canvas.set_tool(ToolKind.RASTER_PENCIL)
        canvas._begin_stroke(QPointF(40, 60), .8)
        canvas._continue_stroke(QPointF(300, 60), .5)
        canvas._end_stroke()
    def activate(canvas, tool):
        if tool != ToolKind.RASTER_PENCIL:
            assert canvas.set_tool(tool)
            contact(canvas, tool)
        else:
            pencil(canvas)
    warm, warm_raster = make_canvas(tmp_path, cold=False, tool=ToolKind.BRUSH)
    activate(warm, first_tool)
    wait_for(lambda: warm._native_input_predecessor() is None)
    first = pixels(warm, warm_raster)
    activate(warm, second_tool)
    wait_for(lambda: warm._native_input_predecessor() is None)
    expected = pixels(warm, warm_raster)
    canvas, raster = make_canvas(tmp_path, cold=True, tool=ToolKind.BRUSH)
    entered, release = Event(), Event()
    original = tile_input.prepare_input_tiles
    def prepare(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)
    prepare.admission_priority, prepare.working_bytes = original.admission_priority, original.working_bytes
    prepare.snapshot_working_bytes = original.snapshot_working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    try:
        activate(canvas, first_tool)
        first_gate = canvas._native_input_predecessor()
        wait_for(entered.is_set)
        activate(canvas, second_tool)
        assert not canvas.command_stack.can_undo
        release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None
                 and not getattr(canvas, '_native_deferred_activations', ())
                 and len(canvas.command_stack._undo) == 2)
        assert first_gate.error is None
        assert pixels(canvas, raster) == expected
        labels = {ToolKind.BRUSH: 'Brush stroke', ToolKind.RASTER_PENCIL: 'Raster stroke',
                  ToolKind.LASSO_BRUSH: 'Lasso brush'}
        assert [command.label for command in canvas.command_stack._undo] == [labels[first_tool], labels[second_tool]]
        canvas.command_stack.undo()
        assert pixels(canvas, raster) == first
        canvas.command_stack.redo()
        assert pixels(canvas, raster) == expected
    finally:
        release.set()
        close(canvas)
        close(warm)
