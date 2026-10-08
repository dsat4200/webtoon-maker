"""Unrun actual Raster presentation/typing regressions; no native sampling waiver."""
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest

from comic_editor.core.models import BoundGeometry, ChapterDocument
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import RasterCanvasWidget
from test_detached_owned_rig_preview import _close
from test_text_outlines import add_text, outline, render, rgba


@pytest.fixture
def text_canvas(qapp, text_outline_font_family):
    chapter = ChapterDocument(height=480)
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 1080, 480))
    page.fill_color, page.border_width = None, 0
    widget = RasterCanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    widget.resize(700, 500)
    widget.set_document(chapter, TileStore())
    widget._test_outline_font_family = text_outline_font_family
    widget.center_x, widget.center_y, widget.scale = 350, 240, 1
    widget.set_selection('layer', page.layer_id)
    try:
        yield widget
    finally:
        _close(widget)


def setup(widget, target_kind, qapp):
    obj, _, target = add_text(widget, target_kind)
    outline(widget, target)
    widget.set_selection('object', obj.object_id)
    widget.show()
    widget.activateWindow()
    widget.setFocus()
    qapp.processEvents()
    return obj


def capture(widget, wait_scene):
    wait_scene(widget)
    widget._text_caret_timer.stop()
    widget._text_caret_visible = False
    return widget.grab().toImage()


def cold_native(widget):
    # Decode a detached stored model/settings, with no scene/source/effect caches.
    model = deepcopy(widget.chapter.to_dict())
    fresh = RasterCanvasWidget(deepcopy(widget.settings))
    try:
        fresh.set_document(ChapterDocument.from_dict(model), TileStore())
        assert not fresh._projection_has_live_preview()
        image = QImage(fresh.chapter.width, fresh.chapter.height,
                       QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(1.)
        return rgba(render(fresh, image))
    finally:
        _close(fresh)


def observe_tiles(monkeypatch):
    import comic_editor.ui.document_projection_features as module
    original = module.draw_document_tiles
    calls = []
    def measured(painter, tiles, *args, **kwargs):
        keys = tuple(tile.key for tile in tiles)
        result = original(painter, tiles, *args, **kwargs)
        calls.append(keys)
        return result
    monkeypatch.setattr(module, 'draw_document_tiles', measured)
    return calls


def last_keys(calls):
    assert calls and calls[-1], 'Real current artwork was not painted'
    return calls[-1]


@pytest.mark.parametrize('target_kind', ['object', 'container'])
def test_decoration_only_keeps_current_native_glyph_tiles(text_canvas, qapp, wait_scene,
                                                        monkeypatch, target_kind):
    canvas = text_canvas
    setup(canvas, target_kind, qapp)
    calls = observe_tiles(monkeypatch)
    resting = capture(canvas, wait_scene)
    baseline = deepcopy(canvas.chapter.to_dict())
    native = rgba(render(canvas))
    np.testing.assert_array_equal(native, cold_native(canvas))
    keys = last_keys(calls)
    assert all(key[0] is None for key in keys)
    exact_revision = canvas._projection_presented_revision
    history = tuple(canvas.command_stack._undo)
    assert canvas.start_text_edit()
    editing = capture(canvas, wait_scene)
    assert editing == resting  # Original full-widget equality is retained.
    assert canvas._text_decoration_only_preview()
    assert canvas._render_document_state().live_preview
    assert canvas._projection_provisional_visible and canvas._projection_frame_pending
    assert canvas._projection_presented_revision == exact_revision
    assert last_keys(calls) == keys
    assert canvas.chapter.to_dict() == baseline
    assert tuple(canvas.command_stack._undo) == history
    np.testing.assert_array_equal(rgba(render(canvas)), native)
    np.testing.assert_array_equal(cold_native(canvas), native)
    canvas._text_caret_visible = True
    assert canvas.grab().toImage() != editing
    QTest.keyClick(canvas, Qt.Key_A, Qt.ControlModifier)
    assert capture(canvas, wait_scene) != editing
    np.testing.assert_array_equal(rgba(render(canvas)), native)
    canvas.commit_active_text_edit()
    assert capture(canvas, wait_scene) == resting
    assert canvas.chapter.to_dict() == baseline
    assert tuple(canvas.command_stack._undo) == history


@pytest.mark.parametrize('target_kind', ['object', 'container'])
def test_actual_typing_falls_back_to_current_preview_and_native_commit(text_canvas, qapp,
        wait_scene, monkeypatch, target_kind):
    canvas = text_canvas
    obj = setup(canvas, target_kind, qapp)
    calls = observe_tiles(monkeypatch)
    resting = capture(canvas, wait_scene)
    before = deepcopy(canvas.chapter.to_dict())
    initial_native = rgba(render(canvas))
    prior = tuple(canvas.command_stack._undo)
    assert canvas.start_text_edit(select_all=True)
    QTest.keyClicks(canvas, 'Live!')
    live = capture(canvas, wait_scene)
    assert obj.text == 'Live!'
    assert live != resting
    assert not canvas._text_decoration_only_preview()
    assert canvas._scene_controller.preview[0] == canvas._render_document_state()
    assert all(key[0] == 'preview' for key in last_keys(calls))
    typed_native = rgba(render(canvas))
    assert not np.array_equal(typed_native, initial_native)
    np.testing.assert_array_equal(typed_native, cold_native(canvas))
    canvas.commit_active_text_edit()
    assert capture(canvas, wait_scene) == live  # Strict original live/commit pixels.
    np.testing.assert_array_equal(rgba(render(canvas)), typed_native)
    np.testing.assert_array_equal(cold_native(canvas), typed_native)
    assert tuple(canvas.command_stack._undo[:-1]) == prior
    assert len(canvas.command_stack._undo) == len(prior)+1
    assert canvas.command_stack.top_undo_command.label == 'Edit text'
    committed = deepcopy(canvas.chapter.to_dict())
    canvas.command_stack.undo()
    capture(canvas, wait_scene)
    assert canvas.chapter.to_dict() == before
    np.testing.assert_array_equal(rgba(render(canvas)), initial_native)
    np.testing.assert_array_equal(cold_native(canvas), initial_native)
    canvas.command_stack.redo()
    capture(canvas, wait_scene)
    assert canvas.chapter.to_dict() == committed
    np.testing.assert_array_equal(rgba(render(canvas)), typed_native)
    np.testing.assert_array_equal(cold_native(canvas), typed_native)


@pytest.mark.parametrize('reason', ['no-snapshot', 'foreign-snapshot', 'incomplete', 'other-preview'])
def test_decoration_native_selection_rejects_unproved_state(text_canvas, qapp, wait_scene,
                                                          monkeypatch, reason):
    canvas = text_canvas
    setup(canvas, 'object', qapp)
    capture(canvas, wait_scene)
    before = deepcopy(canvas.chapter.to_dict())
    native = rgba(render(canvas))
    assert canvas.start_text_edit()
    saved = canvas._text_before_state
    if reason == 'no-snapshot':
        canvas._text_before_state = None
    elif reason == 'foreign-snapshot':
        canvas._text_before_state = replace(saved, document_identity=id(canvas.chapter)+1)
    elif reason == 'incomplete':
        canvas._document_projection.invalidate(QRectF(0, 0, 400, 300))
    else:
        # Eligibility-only control: another live owner cannot be ignored. Do not
        # invent a gesture payload or pass this dummy through scene evaluation.
        original = canvas._text_property_drag
        canvas._text_property_drag = {'foreign-owner': True}
        try:
            assert not canvas._text_decoration_only_preview()
        finally:
            canvas._text_property_drag = original
        assert canvas.chapter.to_dict() == before
        np.testing.assert_array_equal(rgba(render(canvas)), native)
        np.testing.assert_array_equal(cold_native(canvas), native)
        return
    calls = observe_tiles(monkeypatch)
    try:
        capture(canvas, wait_scene)
        assert all(key[0] == 'preview' for key in last_keys(calls))
        assert canvas._scene_controller.preview[0] == canvas._render_document_state()
        if reason != 'incomplete':
            assert not canvas._text_decoration_only_preview()
        assert canvas.chapter.to_dict() == before
        np.testing.assert_array_equal(rgba(render(canvas)), native)
        np.testing.assert_array_equal(cold_native(canvas), native)
    finally:
        canvas._text_before_state = saved
