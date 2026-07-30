from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from comic_editor.core import settings as settings_module
from comic_editor.core.models import BoundGeometry, ChapterDocument
from comic_editor.core.settings import (
    EditorSettings, default_hotkey_hold, load_settings,
)
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.hotkeys import ChordCaptureEdit, chord_keys, normalize_chord
from comic_editor.ui.hotkeys_dialog import HotkeysDialog
from comic_editor.ui.main_window import MainWindow


def _window_with_shape():
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(
        page.layer_id, "Shape", BoundGeometry.rectangle(0, 0, 200, 200)
    )
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection(
        "layer", layer.layer_id, activate_default_tool=False
    )
    window.canvas.set_tool(ToolKind.OBJECT_SELECT)
    return window


def test_single_chord_normalization_supports_standalone_modifiers():
    assert normalize_chord("Ctrl") == "Ctrl"
    assert normalize_chord("Ctrl+Shift") == "Ctrl+Shift"
    assert normalize_chord("Ctrl+P") == "Ctrl+P"
    assert chord_keys("Ctrl+Shift") == frozenset({
        int(Qt.Key_Control), int(Qt.Key_Shift),
    })


def test_chord_capture_replaces_clears_and_cancels(qapp):
    editor = ChordCaptureEdit("P")
    editor.show()
    editor.setFocus()
    QTest.keyPress(editor, Qt.Key_Control)
    QTest.keyPress(editor, Qt.Key_Shift)
    QTest.keyRelease(editor, Qt.Key_Shift)
    QTest.keyRelease(editor, Qt.Key_Control)
    assert editor.chord() == "Ctrl+Shift"

    QTest.keyPress(editor, Qt.Key_Control)
    QTest.keyPress(editor, Qt.Key_K)
    QTest.keyRelease(editor, Qt.Key_K)
    QTest.keyRelease(editor, Qt.Key_Control)
    assert editor.chord() == "Ctrl+K"
    QTest.keyClick(editor, Qt.Key_Escape)
    assert editor.chord() == "Ctrl+K"
    QTest.keyClick(editor, Qt.Key_Backspace)
    assert editor.chord() == ""
    editor.hide()


def test_hotkey_dialog_has_hold_only_for_tools_and_rejects_duplicates(qapp):
    settings = EditorSettings()
    dialog = HotkeysDialog(settings.hotkeys, settings.hotkey_hold)
    assert set(dialog.hold_checks) == set(default_hotkey_hold())
    assert "save" not in dialog.hold_checks
    dialog.editors["save"].setChord("P")
    dialog.editors["raster_pencil"].setChord("P")
    try:
        dialog.bindings()
    except ValueError as error:
        assert "unique" in str(error)
    else:
        raise AssertionError("duplicate chords must be rejected")


def test_modal_hotkey_accept_saves_reloads_and_reopens(
    qapp, monkeypatch, tmp_path,
):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "settings_path", lambda: path)
    window = MainWindow()
    try:
        def edit_dialog():
            dialog = next(
                widget for widget in QApplication.topLevelWidgets()
                if isinstance(widget, HotkeysDialog)
            )
            editor = dialog.editors["shape_edit"]
            editor.setFocus()
            # Accept while the key is still considered pressed to cover
            # finalization of an in-progress capture.
            QTest.keyPress(editor, Qt.Key_F8)
            dialog.hold_checks["shape_edit"].setChecked(True)
            dialog.accept()

        QTimer.singleShot(20, edit_dialog)
        window._edit_hotkeys()
        assert window.settings.hotkeys["shape_edit"] == "F8"
        assert window.settings.hotkey_hold["shape_edit"] is True
        assert load_settings().hotkeys["shape_edit"] == "F8"
        assert load_settings().hotkey_hold["shape_edit"] is True
        reopened = HotkeysDialog(
            window.settings.hotkeys, window.settings.hotkey_hold
        )
        assert reopened.editors["shape_edit"].chord() == "F8"
        assert reopened.hold_checks["shape_edit"].isChecked()
    finally:
        window.deleteLater()


def test_hold_hotkey_tap_sticks_and_long_hold_restores(qapp):
    window = _window_with_shape()
    try:
        window.settings.hotkeys["shape_edit"] = "F8"
        window.settings.hotkey_hold["shape_edit"] = True
        window._install_shortcuts()
        now = [0.0]
        window._hotkey_clock = lambda: now[0]

        assert window._hotkey_press(int(Qt.Key_F8))
        assert window.canvas.tool == ToolKind.SHAPE_EDIT
        now[0] = 0.1
        assert window._hotkey_release(int(Qt.Key_F8))
        assert window.canvas.tool == ToolKind.SHAPE_EDIT

        window.canvas.set_tool(ToolKind.OBJECT_SELECT)
        now[0] = 1.0
        window._hotkey_press(int(Qt.Key_F8))
        now[0] = 1.25
        window._hotkey_release(int(Qt.Key_F8))
        assert window.canvas.tool == ToolKind.OBJECT_SELECT
    finally:
        window.deleteLater()


def test_real_key_events_apply_checked_and_unchecked_hold_behavior(qapp):
    window = _window_with_shape()
    try:
        window.settings.hotkeys["shape_edit"] = "F8"
        window.settings.hotkey_hold["shape_edit"] = True
        window._install_shortcuts()
        window.show()
        window.canvas.setFocus()
        qapp.processEvents()

        QTest.keyPress(window.canvas, Qt.Key_F8)
        assert window.canvas.tool == ToolKind.SHAPE_EDIT
        QTest.qWait(225)
        QTest.keyRelease(window.canvas, Qt.Key_F8)
        assert window.canvas.tool == ToolKind.OBJECT_SELECT

        window.settings.hotkey_hold["shape_edit"] = False
        window._install_shortcuts()
        QTest.keyPress(window.canvas, Qt.Key_F8)
        QTest.qWait(225)
        QTest.keyRelease(window.canvas, Qt.Key_F8)
        assert window.canvas.tool == ToolKind.SHAPE_EDIT
        window.hide()
    finally:
        window.deleteLater()


def test_prefix_chord_waits_and_longer_chord_wins(qapp):
    window = _window_with_shape()
    try:
        window.settings.hotkeys["object_select"] = "Ctrl"
        window.settings.hotkeys["shape_edit"] = "Ctrl+P"
        window.settings.hotkey_hold["object_select"] = False
        window._install_shortcuts()

        window.canvas.set_tool(ToolKind.TRANSFORM)
        assert window._hotkey_press(int(Qt.Key_Control))
        assert window.canvas.tool == ToolKind.TRANSFORM
        assert window._hotkey_press(int(Qt.Key_P))
        assert window.canvas.tool == ToolKind.SHAPE_EDIT
        window._hotkey_release(int(Qt.Key_P))
        window._hotkey_release(int(Qt.Key_Control))

        window.canvas.set_tool(ToolKind.SHAPE_EDIT)
        window._hotkey_press(int(Qt.Key_Control))
        window._hotkey_release(int(Qt.Key_Control))
        assert window.canvas.tool == ToolKind.OBJECT_SELECT
    finally:
        window.deleteLater()


def test_hold_restoration_cancels_on_manual_tool_change_and_focus_loss(qapp):
    window = _window_with_shape()
    try:
        window.settings.hotkeys["shape_edit"] = "F8"
        window.settings.hotkey_hold["shape_edit"] = True
        window._install_shortcuts()
        now = [0.0]
        window._hotkey_clock = lambda: now[0]
        window._hotkey_press(int(Qt.Key_F8))
        window.canvas.set_tool(ToolKind.TRANSFORM)
        assert window._hotkey_active_hold is None
        now[0] = 1.0
        window._hotkey_release(int(Qt.Key_F8))
        assert window.canvas.tool == ToolKind.TRANSFORM

        window.canvas.set_tool(ToolKind.OBJECT_SELECT)
        window._hotkey_press(int(Qt.Key_F8))
        window.eventFilter(window, QEvent(QEvent.ApplicationDeactivate))
        assert window.canvas.tool == ToolKind.OBJECT_SELECT
        assert not window._hotkey_pressed
    finally:
        window.deleteLater()
