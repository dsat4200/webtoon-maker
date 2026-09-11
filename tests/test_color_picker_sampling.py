"""Modal color dialogs must release canvas input while their eyedropper runs."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import BoundGeometry, ChapterDocument
from comic_editor.core.tiles import TileStore
from comic_editor.ui import main_window as main_window_module
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.color_picker import ColorPickerPopup
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def layer_color_window(qapp, monkeypatch):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _settings: None)
    window = MainWindow()
    chapter = ChapterDocument(height=400)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 400))
    layer = chapter.add_layer(page.layer_id, "Fill", BoundGeometry.rectangle(30, 40, 160, 160))
    layer.fill_color, layer.border_width = "#FFAA1122", 0
    sample = chapter.add_layer(page.layer_id, "Sample", BoundGeometry.rectangle(220, 40, 160, 160))
    sample.fill_color, sample.border_width = "#FF2266CC", 0
    window._set_chapter(chapter, TileStore())
    window.resize(1200, 800)
    window.show()
    window.canvas.set_selection("layer", layer.layer_id)
    window.canvas.center_x, window.canvas.center_y, window.canvas.scale = 300, 150, 1
    qapp.processEvents()
    yield window, layer.layer_id
    for popup in window.findChildren(ColorPickerPopup):
        if popup._sampling:
            popup.finish_sample()
        popup.reject()
    for session in window.sessions.values():
        session.dirty = False
    window._dirty = False
    window.canvas._effect_jobs.cancel()
    window.deleteLater()


def open_fill_picker(window, qapp):
    window.layer_settings.fill_color.click()
    qapp.processEvents()
    popup = window.layer_settings._color_popup
    assert popup.isVisible()
    assert QApplication.activeModalWidget() is popup
    return popup


def test_layer_fill_eyedropper_releases_modal_input_samples_and_applies_once(layer_color_window, qapp):
    window, layer_id = layer_color_window
    popup = open_fill_picker(window, qapp)
    original_color = window.canvas.chapter.layers[layer_id].fill_color
    original_tool = window.canvas.tool
    original_modality = popup.windowModality()
    original_revision = window.canvas.command_stack.revision

    QTest.mouseClick(popup.workspace.panel.eyedropper, Qt.LeftButton)
    qapp.processEvents()
    assert not popup.isVisible()
    # A hidden modal dialog still registered here blocks all native canvas
    # events even though direct calls to the sampling functions appear to work.
    assert QApplication.activeModalWidget() is None
    assert window.canvas.tool == ToolKind.EYEDROPPER
    assert window._color_dialog_sample is popup
    assert window.canvas.chapter.layers[layer_id].fill_color == original_color

    point = window.canvas.document_to_widget(QPointF(280, 100)).toPoint()
    assert window.canvas.rect().contains(point)
    QTest.mouseClick(window.canvas, Qt.LeftButton, pos=point)
    qapp.processEvents()
    assert popup.isVisible()
    assert QApplication.activeModalWidget() is popup
    assert popup.windowModality() == original_modality
    assert window.canvas.tool == original_tool
    assert window._color_dialog_sample is None
    assert popup.color_argb() == "#FF2266CC"
    assert window.canvas.chapter.layers[layer_id].fill_color == original_color

    popup.accept()
    qapp.processEvents()
    assert QApplication.activeModalWidget() is None
    assert window.canvas.chapter.layers[layer_id].fill_color == "#FF2266CC"
    assert window.canvas.command_stack.revision == original_revision + 1
    window.canvas.command_stack.undo()
    assert window.canvas.chapter.layers[layer_id].fill_color == original_color


def test_layer_fill_sampling_escape_restores_dialog_and_can_sample_again(layer_color_window, qapp):
    window, layer_id = layer_color_window
    popup = open_fill_picker(window, qapp)
    original_tool = window.canvas.tool
    original_color = popup.color_argb()
    revision = window.canvas.command_stack.revision
    for _ in range(2):
        QTest.mouseClick(popup.workspace.panel.eyedropper, Qt.LeftButton)
        assert QApplication.activeModalWidget() is None
        window._eyedropper_preview("#FF00FF00")
        QTest.keyClick(window.canvas, Qt.Key_Escape)
        qapp.processEvents()
        assert popup.isVisible()
        assert QApplication.activeModalWidget() is popup
        assert popup.color_argb() == original_color
        assert window.canvas.tool == original_tool
        assert window._color_dialog_sample is None
    popup.reject()
    assert QApplication.activeModalWidget() is None
    assert window.canvas.chapter.layers[layer_id].fill_color == original_color
    assert window.canvas.command_stack.revision == revision


def test_nested_color_picker_sampling_releases_and_restores_all_modal_dialogs(layer_color_window, qapp):
    window, _ = layer_color_window
    outer = open_fill_picker(window, qapp)
    outer.workspace.panel.apply_color("#FF335577")
    inner = ColorPickerPopup("#FF112233", outer.workspace)
    inner.open()
    qapp.processEvents()
    modality = (outer.windowModality(), inner.windowModality())
    original_tool = window.canvas.tool
    assert QApplication.activeModalWidget() is inner

    QTest.mouseClick(inner.workspace.panel.eyedropper, Qt.LeftButton)
    qapp.processEvents()
    assert not outer.isVisible() and not inner.isVisible()
    assert QApplication.activeModalWidget() is None
    assert window.canvas.tool == ToolKind.EYEDROPPER

    window._eyedropper_commit("#FF88AA44")
    qapp.processEvents()
    assert outer.isVisible() and inner.isVisible()
    assert (outer.windowModality(), inner.windowModality()) == modality
    assert QApplication.activeModalWidget() is inner
    assert window.canvas.tool == original_tool
    assert outer.color_argb() == "#FF335577"
    assert inner.color_argb() == "#FF88AA44"
    inner.reject()
    assert QApplication.activeModalWidget() is outer
    outer.reject()
    assert QApplication.activeModalWidget() is None
