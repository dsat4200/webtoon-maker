"""The diagnostic window must stay opt-in and produce a stopped, bounded view."""
from __future__ import annotations

import copy
import json

import pytest
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtTest import QTest

from comic_editor.ui.performance_monitor_dialog import PerformanceMonitorDialog


class FakeController(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.enabled = False
        self.starts = 0
        self.stops = 0
        self.snapshots = 0
        self.recorder = self
        self.capture = {
            "metadata": {"purpose": "test"}, "context": {"solo": True},
            "summary": [], "timeline": [], "stalls": [], "stack_hotspots": [],
            "metrics": {"rss_bytes": 123}, "bounds": {}, "dropped": {},
        }

    def start(self):
        self.starts += 1
        self.enabled = True
        self.changed.emit()

    def stop(self):
        self.stops += 1
        self.enabled = False
        self.changed.emit()

    def clear(self):
        self.capture["timeline"] = []
        self.changed.emit()

    def record_event(self, name, category="event"):
        self.capture["timeline"].append({"t_ms": 7, "name": name, "category": category})

    def snapshot(self):
        self.snapshots += 1
        return {**copy.deepcopy(self.capture), "enabled": self.enabled}


@pytest.fixture
def monitor_dialog(qapp):
    controller = FakeController()
    dialog = PerformanceMonitorDialog(controller)
    dialog.show()
    qapp.processEvents()
    yield dialog, controller
    dialog.close()
    dialog.deleteLater()


def test_opening_monitor_never_enables_recording(monitor_dialog, qapp):
    dialog, controller = monitor_dialog
    assert not dialog.isModal()
    assert not controller.enabled
    assert controller.starts == 0
    assert not dialog.enable_checkbox.isChecked()
    assert not dialog.mark_button.isEnabled()
    assert not dialog.stop_button.isEnabled()
    assert "OFF" in dialog.status_label.text()
    # Showing it again preserves the disabled state; there is no saved preference.
    dialog.close()
    dialog.show()
    qapp.processEvents()
    assert controller.starts == 0
    assert not controller.enabled


@pytest.mark.parametrize("close_method", ["close", "escape"])
def test_enabling_is_explicit_and_close_stops_it(monitor_dialog, qapp, close_method):
    dialog, controller = monitor_dialog
    dialog.enable_checkbox.setChecked(True)
    assert controller.enabled
    assert controller.starts == 1
    assert dialog.mark_button.isEnabled()
    assert "ON" in dialog.status_label.text()
    if close_method == "close":
        dialog.close()
    else:
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert not controller.enabled
    assert not dialog.enable_checkbox.isChecked()
    dialog.show()
    qapp.processEvents()
    assert not controller.enabled
    assert controller.starts == 1


def test_hidden_dialog_does_no_signal_driven_refresh(monitor_dialog):
    dialog, controller = monitor_dialog
    dialog.hide()
    count = controller.snapshots
    controller.changed.emit()
    assert controller.snapshots == count


def test_mark_and_clear_capture(monitor_dialog):
    dialog, controller = monitor_dialog
    dialog.enable_checkbox.setChecked(True)
    dialog.mark_button.click()
    assert controller.capture["timeline"][-1]["name"] == "user.mark_slowdown"
    dialog.clear_button.click()
    assert not controller.capture["timeline"]
    assert controller.enabled
    dialog.stop_button.click()
    assert not controller.enabled
    assert not dialog.enable_checkbox.isChecked()


def test_export_stops_before_file_picker_and_writes_valid_json(monitor_dialog, monkeypatch, tmp_path):
    dialog, controller = monitor_dialog
    dialog.enable_checkbox.setChecked(True)
    dialog.mark_button.click()
    path = tmp_path / "capture.json"
    path.write_text("previous capture", encoding="utf-8")

    def select_filename(*_args):
        assert not controller.enabled
        assert not dialog.enable_checkbox.isChecked()
        return str(path), "JSON files (*.json)"

    monkeypatch.setattr(
        "comic_editor.ui.performance_monitor_dialog.QFileDialog.getSaveFileName", select_filename
    )
    dialog.export_button.click()
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["enabled"] is False
    assert result["context"]["solo"] is True
    assert result["timeline"][-1]["name"] == "user.mark_slowdown"
    assert "exported" in dialog.status_label.text()
    assert not controller.enabled


def test_cancel_export_keeps_capture_stopped(monitor_dialog, monkeypatch):
    dialog, controller = monitor_dialog
    dialog.enable_checkbox.setChecked(True)
    monkeypatch.setattr(
        "comic_editor.ui.performance_monitor_dialog.QFileDialog.getSaveFileName",
        lambda *_args: ("", ""),
    )
    dialog.export_button.click()
    assert not controller.enabled
    assert not dialog.enable_checkbox.isChecked()


def test_display_can_use_a_limited_snapshot_but_export_preserves_full_trace(
    monitor_dialog, monkeypatch, tmp_path,
):
    dialog, controller = monitor_dialog
    controller.capture["timeline"] = [
        {"t_ms": i, "name": f"event-{i}"} for i in range(150)
    ]
    calls = []

    def display_snapshot():
        calls.append(True)
        result = controller.snapshot()
        result["timeline"] = result["timeline"][-100:]
        return result

    monkeypatch.setattr(controller, "snapshot_for_display", display_snapshot, raising=False)
    controller.changed.emit()
    assert calls
    assert len(dialog._snapshot["timeline"]) == 100
    path = tmp_path / "full-capture.json"
    monkeypatch.setattr(
        "comic_editor.ui.performance_monitor_dialog.QFileDialog.getSaveFileName",
        lambda *_args: (str(path), ""),
    )
    dialog.export_button.click()
    assert len(json.loads(path.read_text(encoding="utf-8"))["timeline"]) == 150


def test_automatic_log_status_and_folder_are_accessible(monitor_dialog, monkeypatch, tmp_path):
    dialog, controller = monitor_dialog
    controller.log_directory = tmp_path
    controller.last_log_path = tmp_path / "run.json"
    controller.log_status = "Saved JSON and Markdown"
    controller.changed.emit()
    assert "Saved JSON and Markdown" in dialog.log_label.text()
    assert "run.json" in dialog.log_label.text()
    assert dialog.log_folder_button.isEnabled()
    opened = []
    monkeypatch.setattr(
        "comic_editor.ui.performance_monitor_dialog.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    dialog.log_folder_button.click()
    from pathlib import Path
    assert Path(opened[0]) == tmp_path
    assert controller.starts == 0


def test_export_failure_keeps_existing_file_and_reports_error(monitor_dialog, monkeypatch, tmp_path):
    dialog, controller = monitor_dialog
    path = tmp_path / "capture.json"
    path.write_text("keep this", encoding="utf-8")
    # Strict JSON failure occurs before opening the atomic output file.
    controller.capture["metrics"]["bad"] = float("nan")
    monkeypatch.setattr(
        "comic_editor.ui.performance_monitor_dialog.QFileDialog.getSaveFileName",
        lambda *_args: (str(path), ""),
    )
    errors = []
    monkeypatch.setattr(
        "comic_editor.ui.performance_monitor_dialog.QMessageBox.warning",
        lambda *_args: errors.append(_args[-1]),
    )
    dialog.export_button.click()
    assert path.read_text(encoding="utf-8") == "keep this"
    assert len(errors) == 1
    assert "Could not save" in errors[0]


def test_views_are_bounded_and_stacks_include_files_lines(monitor_dialog):
    dialog, controller = monitor_dialog
    controller.capture["summary"] = [
        {"name": f"phase-{i}", "count": 1, "total_ms": i, "p95_ms": i, "max_ms": i}
        for i in range(80)
    ]
    controller.capture["active_spans"] = [
        {"name": "editor.set_tool", "duration_ms": 4567.8}
    ]
    controller.capture["timeline"] = [
        {"t_ms": i, "name": f"event-{i}", "context": {"solo": True, "tool": "brush"}}
        for i in range(150)
    ]
    controller.capture["stalls"] = [
        {"t_ms": i, "gap_ms": 500, "completed": True,
         "stack": [{"file": "editor.py", "line": 42, "function": "set_tool"}]}
        for i in range(30)
    ]
    controller.capture["stack_hotspots"] = [
        {"file": "editor.py", "line": i, "function": "set_tool", "samples": i}
        for i in range(80)
    ]
    controller.changed.emit()
    assert dialog.phase_table.rowCount() == 50
    assert dialog.phase_table.item(0, 0).text() == "phase-79"
    assert "editor.set_tool (4,567.80 ms)" in dialog.active_phase_label.text()
    # Hidden views are not rebuilt by capture notifications.
    assert dialog.event_table.rowCount() == 0
    dialog.tabs.setCurrentIndex(1)
    assert dialog.event_table.rowCount() == 100
    assert dialog.event_table.item(0, 1).text() == "event-149"
    assert '"solo": true' in dialog.event_table.item(0, 4).text()
    dialog.tabs.setCurrentIndex(2)
    assert dialog.stall_tree.topLevelItemCount() == 20
    frame = dialog.stall_tree.topLevelItem(0).child(0).child(0)
    assert frame.text(0) == "set_tool"
    assert frame.text(1) == "42"
    assert frame.text(2) == "editor.py"
    assert dialog.stack_table.rowCount() == 50
    dialog.tabs.setCurrentIndex(3)
    assert '"rss_bytes": 123' in dialog.context_text.toPlainText()
