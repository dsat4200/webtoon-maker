"""Native input preparation is proportional to the requested source tiles."""
import os
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tile_backing import finish_revision_readers
from comic_editor.core.tiles import TileStore
from comic_editor.render.input_sources import InputSourceCapture
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def source(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    chapter = ChapterDocument(width=512, height=70000, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 512, 70000))
    page.fill_color, page.border_width = None, 0
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 512, 256)))
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', predictive_ink=False,
        grid_overlay_visible=False, snap_to_grid=False))
    canvas.setUpdatesEnabled(False)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection('object', raster.object_id)
    canvas.set_solo_entities({('layer', page.layer_id)})
    yield canvas, raster
    canvas._cancel_native_raster_stroke()
    canvas._cancel_paint_brush()
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def freeze(canvas, identifier, keys):
    capture = InputSourceCapture(canvas, canvas._render_document_state(), identifier, keys)
    while not capture.advance(.001):
        pass
    assert not capture.stale
    return capture.result.finish_sources()


@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied, QImage.Format_RGBA16FPx4_Premultiplied,
    QImage.Format_RGBA32FPx4_Premultiplied])
def test_requested_native_buffers_keep_format_bits_and_cow_ownership(source, format):
    canvas, obj = source
    image = QImage(256, 256, format)
    image.fill(QColor.fromRgbF(.3123, .5345, .7967, .4132))
    before = bytes(image.constBits())
    canvas.tiles.set_tile(obj.object_id, (-1, 3), image)
    snapshot = freeze(canvas, obj.object_id, [(-1, 3)])
    canvas.tiles.tile(obj.object_id, (-1, 3)).fill(QColor('blue'))
    result = snapshot.tiles.tile(obj.object_id, (-1, 3))
    assert result.format() == format and bytes(result.constBits()) == before


def test_only_requested_keys_are_visited_and_clean_files_survive_atomic_publication(source, tmp_path, monkeypatch):
    canvas, obj = source
    image = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor.fromRgbF(.3123, .5345, .7967, .4132))
    path = tmp_path / 'original.png'
    assert image.save(str(path))
    original = QImage(str(path))
    owner = canvas.tiles._object_tiles(obj.object_id)
    owner.register((0, 0), path)
    for y in range(1000):
        owner.register((1, y), tmp_path / 'must-not-be-read.png')
    canvas.tiles._object_tiles('unrelated').register((0, 0), tmp_path / 'also-not-read.png')
    visited, version = [], owner.version
    def counted(key):
        visited.append(key)
        assert key == (0, 0)
        return version(key)
    monkeypatch.setattr(owner, 'version', counted)
    snapshot = freeze(canvas, obj.object_id, [(0, 0)])
    assert visited and set(visited) == {(0, 0)}
    assert set(snapshot.tiles._tiles) == {obj.object_id}
    assert set(snapshot.tiles._tiles[obj.object_id]) == {(0, 0)}
    finish_revision_readers(tmp_path)
    replacement = tmp_path / 'replacement.png'
    image.fill(QColor('blue'))
    assert image.save(str(replacement))
    os.replace(replacement, path)
    assert snapshot.tiles.tile(obj.object_id, (0, 0)) == original


def test_unrelated_projection_revision_does_not_restart_owned_source_capture(source):
    canvas, obj = source
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    for x in range(3):
        canvas.tiles.set_tile(obj.object_id, (x, 0), QImage(image))
    capture = InputSourceCapture(canvas, canvas._render_document_state(), obj.object_id, [(0, 0), (1, 0)])
    assert not capture.advance(0.)
    canvas._render_service.invalidate()
    while not capture.advance(.001):
        pass
    assert not capture.stale and capture.result is not None
    assert set(capture.result.tiles._tiles[obj.object_id]) == {(0, 0), (1, 0)}


def test_replaced_source_owner_retires_capture(source):
    canvas, obj = source
    capture = InputSourceCapture(canvas, canvas._render_document_state(), obj.object_id, [(0, 0)])
    canvas.tiles = TileStore()
    assert capture.advance() and capture.stale and capture.result is None


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.BRUSH])
def test_cold_solo_contact_uses_no_chapter_capture_and_applies_while_held(source, tmp_path, monkeypatch, qapp, tool):
    canvas, obj = source
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('white'))
    path = tmp_path / 'cold.png'
    assert image.save(str(path))
    canvas.tiles._object_tiles(obj.object_id).register((0, 0), path)
    canvas.tiles._alpha_bounds[obj.object_id] = {(0, 0): (0, 0, 256, 256)}
    canvas.set_tool(tool)
    canvas.primary_color = '#ffff0000'
    history_before = canvas.command_stack.revision
    monkeypatch.setattr(canvas._scene_snapshot_compiler, 'capture',
        lambda *_args: pytest.fail('Native input visited the entire chapter'))
    if tool == ToolKind.RASTER_PENCIL:
        canvas._begin_stroke(QPointF(30, 70), 1.)
        attribute = '_raster_tile_input'
    else:
        canvas._begin_paint_brush(QPointF(30, 70), 1.)
        attribute = '_paint_brush_tile_input'
    deadline = time.monotonic() + 10
    gate = getattr(canvas, attribute)
    while gate.busy and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert not gate.busy and gate.error is None and canvas._drawing
    assert canvas.tiles.tile(obj.object_id, (0, 0)) != image
    assert canvas.command_stack.revision == history_before


def test_source_capture_opt_in_requires_owned_source_job(source, monkeypatch):
    canvas, _obj = source
    from comic_editor.ui.scene_consumers import SceneConsumers
    job = SimpleNamespace(owned_source=False, document=canvas._render_document_state(), arguments=(),
        compute=SimpleNamespace(source_capture=lambda *_args: pytest.fail('Unowned source shortcut')))
    expected = object()
    monkeypatch.setattr(canvas._scene_snapshot_compiler, 'capture', lambda *_args: expected)
    consumers = SceneConsumers(canvas)
    try:
        assert consumers._capture_job(job) is expected
    finally:
        consumers.shutdown()
