"""Cold input work retains action order and original source pixels off GUI."""
from threading import Event, get_ident
import time

import pytest
from PySide6.QtCore import QCoreApplication, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainterPath, QTransform

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, ShapeStyle, ToneMask
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.core.settings import EditorSettings
from comic_editor.ui.main_window import MainWindow


def wait_for(predicate):
    deadline = time.monotonic()+10
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.002)
    assert predicate()


@pytest.fixture
def wand(qapp):
    chapter = ChapterDocument(width=128, height=128, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 128, 128))
    chapter.add_layer(page.layer_id, 'Shape', BoundGeometry.rectangle(20, 20, 50, 50),
        style=ShapeStyle(primary_color='#FF202020', outline_thickness=0))
    mask = ToneMask(saved=True)
    chapter.masks[mask.mask_id] = mask
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, canvas_renderer='raster'))
    canvas.set_document(chapter, TileStore())
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas.set_tool(ToolKind.MASK_WAND)
    yield canvas, mask
    canvas._scene_consumers.shutdown() if hasattr(canvas, '_scene_consumers') else None
    canvas.deleteLater()


def test_wand_cold_render_is_detached_and_consecutive_clicks_keep_history_order(wand, monkeypatch):
    canvas, mask = wand
    owner = get_ident()
    threads = []
    original = TileStore.advanced_fill
    def classify(*args, **kwargs):
        threads.append(get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(TileStore, 'advanced_fill', classify)
    monkeypatch.setattr(canvas, 'render_preview', lambda *_a, **_k: pytest.fail('GUI wand evaluation'))
    canvas._mask_wand_press(QPointF(40, 40), Qt.NoModifier)
    canvas._mask_wand_press(QPointF(40, 40), Qt.ControlModifier)
    assert not canvas.command_stack.can_undo
    wait_for(lambda: not canvas._scene_consumers.contains(('mask-wand',)))
    assert threads and all(thread != owner for thread in threads)
    assert len(canvas.command_stack._undo) == 2
    assert canvas.tiles.tile(mask.mask_id, (0, 0)).pixelColor(40, 40) == QColor('black')
    canvas.command_stack.undo()
    assert canvas.tiles.tile(mask.mask_id, (0, 0)).pixelColor(40, 40) == QColor('white')


def test_wand_cancel_and_changed_source_cannot_publish(wand, monkeypatch):
    canvas, mask = wand
    entered, release = Event(), Event()
    original = TileStore.advanced_fill
    def classify(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(TileStore, 'advanced_fill', classify)
    canvas._mask_wand_press(QPointF(40, 40), Qt.NoModifier)
    wait_for(entered.is_set)
    assert canvas._cancel_mask_selection()
    release.set()
    wait_for(lambda: canvas._scene_consumers.active is None)
    assert not canvas.command_stack.can_undo
    assert not canvas.tiles.object_tiles(mask.mask_id)


def test_copy_cut_keeps_native_source_format_and_immediate_paste_order(qapp, monkeypatch):
    window = MainWindow()
    chapter = ChapterDocument(width=128, height=128, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 128, 128))
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    source = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    source.fill(QColor.fromRgbF(.31, .53, .79, .41))
    tiles = TileStore()
    tiles.set_tile(obj.object_id, (0, 0), source)
    window._set_chapter(chapter, tiles)
    window.canvas.set_selection('object', obj.object_id)
    path = QPainterPath()
    path.addRect(QRectF(20, 20, 20, 20))
    window.canvas._drawing_selection_path = path
    owner = get_ident()
    copied = []
    from comic_editor.render import input_capture
    original = input_capture.drawing_selection
    def capture(*args, **kwargs):
        copied.append(get_ident())
        return original(*args, **kwargs)
    capture.accepts_cancelled = True
    monkeypatch.setattr(input_capture, 'drawing_selection', capture)
    monkeypatch.setattr(window.canvas, 'drawing_selection_clipboard', lambda: pytest.fail('GUI clipping'))
    monkeypatch.setattr('comic_editor.ui.main_window.cursor_world', lambda _: QPointF(80, 80))
    pasted = []
    monkeypatch.setattr(window, '_paste_drawing_payload', lambda payload, point: pasted.append((payload, point)) or True)
    try:
        assert window._cut_drawing_selection()
        assert window._paste()
        assert not pasted
        wait_for(lambda: bool(pasted))
        payload, position = pasted[0]
        assert len(copied) == 1 and copied[0] != owner
        assert position == QPointF(80, 80)
        assert payload.tiles[(0, 0)].format() == source.format()
        assert payload.tiles[(0, 0)].pixelColor(30, 30).getRgbF() == source.pixelColor(30, 30).getRgbF()
        assert tiles.tile(obj.object_id, (0, 0)).pixelColor(30, 30).alpha() == 0
        assert tiles.tile(obj.object_id, (0, 0)).pixelColor(50, 50).getRgbF() == source.pixelColor(50, 50).getRgbF()
        assert len(window.canvas.command_stack._undo) == 1
        window.canvas.command_stack.undo()
        assert tiles.tile(obj.object_id, (0, 0)).pixelColor(30, 30).getRgbF() == source.pixelColor(30, 30).getRgbF()
    finally:
        window._dirty = False
        window.deleteLater()


def test_history_thumbnail_does_not_render_or_serialize_on_gui(qapp, monkeypatch):
    from comic_editor.core.clipboard import RasterSelectionClipboard
    from comic_editor.ui.clipboard_history import ClipboardImageHistory
    from comic_editor.render import outputs
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    chapter = ChapterDocument(width=64, height=64)
    chapter.add_page()
    canvas.set_document(chapter, TileStore())
    source = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor('red'))
    payload = RasterSelectionClipboard({(0, 0): source}, QPainterPath(), QTransform(), 'Captured', 256)
    owner, calls = get_ident(), []
    original = outputs.entity_crop
    def crop(*args, **kwargs):
        calls.append(get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(outputs, 'entity_crop', crop)
    monkeypatch.setattr(canvas, '_render_entity_crop', lambda *_a, **_k: pytest.fail('GUI history render'))
    history = ClipboardImageHistory()
    try:
        history.add_drawing(canvas, payload)
        entry = history.entries[0]
        key = entry.thumbnail.cacheKey()
        wait_for(lambda: entry.thumbnail.cacheKey() != key)
        assert calls and all(thread != owner for thread in calls)
        assert entry.thumbnail.pixelColor(36, 36) == QColor('red')
    finally:
        canvas._scene_consumers.shutdown()
        canvas.deleteLater()


def test_cage_preparation_preserves_final_handle_and_commit_runs_off_gui(qapp, monkeypatch):
    from comic_editor.render import input_capture
    chapter = ChapterDocument(width=128, height=128, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 128, 128))
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    tiles = TileStore()
    tiles.paint_dab(obj.object_id, QPointF(40, 40), 8, QColor('red'))
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, tiles)
    canvas.set_selection('object', obj.object_id)
    owner, threads = get_ident(), []
    original = input_capture.cage_commit
    def commit(*args, **kwargs):
        threads.append(get_ident())
        return original(*args, **kwargs)
    commit.accepts_cancelled = True
    monkeypatch.setattr(input_capture, 'cage_commit', commit)
    monkeypatch.setattr(chapter, 'to_dict', lambda: pytest.fail('GUI cage chapter serialization'))
    handles = []
    apply_move = canvas._apply_cage_move
    def moved(point):
        handles.append(point.toTuple())
        return apply_move(point)
    canvas._cage_input.apply = moved
    try:
        assert canvas.set_tool(ToolKind.CAGE_TRANSFORM)
        assert canvas._cage_prepare is not None
        press = canvas.document_to_widget(QPointF(0, 0))
        final = press+QPointF(15, 10)
        assert canvas._begin_cage_handle(press, Qt.NoModifier)
        assert canvas._move_cage_handle(press+QPointF(5, 5))
        assert canvas._move_cage_handle(final)
        assert canvas._finish_cage_handle()
        wait_for(lambda: canvas._active_cage() is not None)
        assert handles == [final.toTuple()]
        assert canvas._cage_drag is None and canvas._cage_input.pending is None
        grid = canvas._active_cage()
        grid.points = [(x+20, y) for x, y in grid.rest_points()]
        assert canvas.finish_cage(True)
        assert canvas._cage_commit_pending is not None
        assert tiles.tile(obj.object_id, (0, 0)).pixelColor(40, 40) == QColor('red')
        wait_for(lambda: canvas._cage_session is None and canvas._cage_commit_pending is None)
        assert canvas._cage_commit_error is None
        assert threads and all(thread != owner for thread in threads)
        assert len(canvas.command_stack._undo) == 1
        assert tiles.tile(obj.object_id, (0, 0)).pixelColor(60, 40).red() > 200
        assert chapter.objects[obj.object_id] is obj
        canvas.command_stack.undo()
        assert tiles.tile(obj.object_id, (0, 0)).pixelColor(40, 40) == QColor('red')
        assert chapter.objects[obj.object_id] is obj
    finally:
        canvas._scene_consumers.shutdown()
        canvas.deleteLater()


def test_cage_source_pending_tool_switch_cancels_without_source_changes(qapp):
    chapter = ChapterDocument(width=64, height=64)
    page = chapter.add_page()
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 64, 64)))
    tiles = TileStore()
    tiles.paint_dab(obj.object_id, QPointF(20, 20), 5, QColor('red'))
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, tiles)
    canvas.set_selection('object', obj.object_id)
    try:
        assert canvas.set_tool(ToolKind.CAGE_TRANSFORM)
        assert canvas._cage_prepare is not None
        assert canvas.set_tool(ToolKind.TRANSFORM)
        assert canvas._cage_prepare is None and canvas._cage_session is None
        assert not canvas._scene_consumers.contains(('cage-initial',))
        assert not canvas.command_stack.can_undo
        assert tiles.tile(obj.object_id, (0, 0)).pixelColor(20, 20) == QColor('red')
    finally:
        canvas._scene_consumers.shutdown()
        canvas.deleteLater()


def test_save_as_finishes_accepted_cage_before_session_capture(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.ui import main_window as window_module
    monkeypatch.setattr(window_module, 'save_settings', lambda *_a: None)
    repository = SeriesRepository(tmp_path/'Original')
    series = repository.create('Original')
    chapter, tiles = repository.create_chapter(series, 'Chapter')
    window = MainWindow()
    assert window.open_series(repository.root)
    canvas = window.canvas
    obj = next(record for record in canvas.chapter.objects.values() if isinstance(record, RasterObject))
    obj.interaction_rect = (0, 0, 128, 128)
    canvas.tiles.paint_dab(obj.object_id, QPointF(40, 40), 8, QColor('red'))
    canvas.set_selection('object', obj.object_id)
    try:
        assert canvas.set_tool(ToolKind.CAGE_TRANSFORM)
        wait_for(lambda: canvas._active_cage() is not None)
        grid = canvas._active_cage()
        grid.points = [(x+20, y) for x, y in grid.rest_points()]
        monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *_a, **_k: str(tmp_path))
        monkeypatch.setattr(QInputDialog, 'getText', lambda *_a, **_k: ('Clone', True))
        errors = []
        monkeypatch.setattr(QMessageBox, 'critical', lambda _p, _t, message, *_a: errors.append(message))
        assert window._save_as(), errors
        assert canvas._cage_commit_pending is None and canvas._cage_session is None
        cloned, cloned_tiles = SeriesRepository(tmp_path/'Clone').load_chapter(chapter.chapter_id)
        assert cloned_tiles.tile(obj.object_id, (0, 0)).pixelColor(60, 40).red() > 200
        assert cloned_tiles.tile(obj.object_id, (0, 0)).pixelColor(40, 40).alpha() == 0
        assert len(canvas.command_stack._undo) == 1
    finally:
        window._dirty = False
        for session in window.sessions.values():
            session.dirty = False
        window.close()


def test_outliner_copy_and_duplicate_extract_owned_sources_off_gui(qapp, monkeypatch):
    from comic_editor.render import input_capture
    window = MainWindow()
    chapter = ChapterDocument(width=128, height=128, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 128, 128))
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    tiles = TileStore()
    tiles.paint_dab(obj.object_id, QPointF(40, 40), 8, QColor('red'))
    window._set_chapter(chapter, tiles)
    window.canvas.set_selection('layer', page.layer_id)
    owner, threads = get_ident(), []
    original = input_capture.object_clipboard
    def capture(*args, **kwargs):
        threads.append(get_ident())
        return original(*args, **kwargs)
    capture.accepts_cancelled = True
    monkeypatch.setattr(input_capture, 'object_clipboard', capture)
    monkeypatch.setattr('comic_editor.ui.main_window.capture_object', lambda *_a: pytest.fail('GUI outliner extraction'))
    monkeypatch.setattr('comic_editor.ui.main_window.cursor_world', lambda *_a: QPointF(80, 80))
    try:
        assert window._copy_outliner_object('object', obj.object_id)
        assert window._paste()
        assert len(chapter.objects) == 1
        wait_for(lambda: not window.canvas._scene_consumers.contains(('outliner-copy',)))
        assert len(chapter.objects) == 2 and len(window.canvas.command_stack._undo) == 1, window.statusBar().currentMessage()
        assert window._duplicate_outliner_object('object', obj.object_id)
        assert window._duplicate_outliner_object('object', obj.object_id)
        wait_for(lambda: not window.canvas._scene_consumers.contains(('outliner-duplicate',)))
        assert len(chapter.objects) == 4 and len(window.canvas.command_stack._undo) == 3
        assert threads and all(thread != owner for thread in threads)
        window.canvas.command_stack.undo()
        assert len(chapter.objects) == 3
    finally:
        window._dirty = False
        window.close()
