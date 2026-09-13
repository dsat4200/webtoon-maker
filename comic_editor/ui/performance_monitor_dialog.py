"""Opt-in diagnostic window; constructing or opening it never starts a capture."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QIODevice, QSaveFile, QSignalBlocker, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from .performance_monitor import PerformanceMonitorController


def _number(value: object) -> str:
    return f"{value:,.2f}" if isinstance(value, (int, float)) else "—"


def _compact(value: object, limit: int = 700) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


class PerformanceMonitorDialog(QDialog):
    """Display bounded captures without adding a second polling timer."""

    def __init__(self, controller: PerformanceMonitorController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self._snapshot: dict = {}
        self.setWindowTitle("Drawing Performance Monitor")
        self.setModal(False)
        self.resize(1080, 740)
        self.setMinimumSize(700, 480)

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.enable_checkbox = QCheckBox("Enable monitoring", self)
        self.enable_checkbox.setToolTip(
            "Starts diagnostic timing and sampling for this session only. "
            "Opening this window does not enable monitoring."
        )
        controls.addWidget(self.enable_checkbox)
        controls.addStretch()
        self.stop_button = QPushButton("Stop", self)
        self.mark_button = QPushButton("Mark slowdown", self)
        self.clear_button = QPushButton("Clear capture", self)
        self.export_button = QPushButton("Export JSON…", self)
        for button in (
            self.stop_button, self.mark_button, self.clear_button, self.export_button
        ):
            controls.addWidget(button)
        layout.addLayout(controls)

        self.status_label = QLabel(self)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status_label)
        instructions = QLabel(
            "Enable monitoring, reproduce the slow drawing or tool switch on a solo "
            "layer, then Stop. Each enabled run automatically saves diagnostic logs. "
            "Mark slowdown adds a point in the timeline; Export JSON saves an extra copy. "
            "Closing this window stops monitoring and finalizes the run.", self
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        caution = QLabel(
            "Monitoring adds overhead. Phase timings include nested calls; sampled "
            "Python stacks help locate freezes. GPU utilization and native-call "
            "internals are not measured.", self
        )
        caution.setWordWrap(True)
        layout.addWidget(caution)
        log_row = QHBoxLayout()
        self.log_label = QLabel(self)
        self.log_label.setTextFormat(Qt.TextFormat.PlainText)
        self.log_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.log_label.setWordWrap(True)
        log_row.addWidget(self.log_label, 1)
        self.log_folder_button = QPushButton("Open log folder", self)
        log_row.addWidget(self.log_folder_button)
        layout.addLayout(log_row)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        summary_tab = QWidget(self.tabs)
        summary_layout = QVBoxLayout(summary_tab)
        self.summary_label = QLabel(summary_tab)
        self.summary_label.setWordWrap(True)
        summary_layout.addWidget(self.summary_label)
        self.active_phase_label = QLabel(summary_tab)
        self.active_phase_label.setTextFormat(Qt.TextFormat.PlainText)
        self.active_phase_label.setWordWrap(True)
        summary_layout.addWidget(self.active_phase_label)
        self.phase_table = self._table(
            ["Phase", "Category", "Count", "Total ms", "p50 ms", "p95 ms", "Max ms"]
        )
        summary_layout.addWidget(self.phase_table)
        self.tabs.addTab(summary_tab, "Phase timings")

        events_tab = QWidget(self.tabs)
        events_layout = QVBoxLayout(events_tab)
        events_description = QLabel(
            "Latest 100 retained events, newest first. Tool, solo mode and selection "
            "context are captured with each event. Export includes the full retained trace.",
            events_tab,
        )
        events_description.setWordWrap(True)
        events_layout.addWidget(events_description)
        self.event_table = self._table(
            ["Time ms", "Event", "Category", "Duration ms", "Context", "Details"]
        )
        events_layout.addWidget(self.event_table)
        self.tabs.addTab(events_tab, "Event timeline")

        stacks_tab = QWidget(self.tabs)
        stacks_layout = QVBoxLayout(stacks_tab)
        stacks_layout.addWidget(QLabel(
            "GUI heartbeat stalls and sampled Python frames. Expand a stall for the "
            "stack captured while the editor was unresponsive. Paths and line numbers "
            "identify code; source text and local variables are not captured.", stacks_tab
        ))
        stacks_layout.itemAt(0).widget().setWordWrap(True)
        splitter = QSplitter(Qt.Orientation.Vertical, stacks_tab)
        self.stall_tree = QTreeWidget(splitter)
        self.stall_tree.setHeaderLabels(["Stall / frame", "Gap ms / line", "Context / file"])
        self.stall_tree.setUniformRowHeights(True)
        self.stall_tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.stall_tree.setColumnWidth(0, 290)
        self.stall_tree.setColumnWidth(1, 120)
        self.stack_table = self._table(
            ["Function", "File", "Line", "Samples", "Leaf samples"]
        )
        splitter.addWidget(self.stack_table)
        stacks_layout.addWidget(splitter)
        self.tabs.addTab(stacks_tab, "Freezes & stacks")

        context_tab = QWidget(self.tabs)
        context_layout = QVBoxLayout(context_tab)
        context_description = QLabel(
            "Current editor context, resource metrics, capture limits and instrumented "
            "methods. Counters describe this capture; the export contains all retained data.",
            context_tab,
        )
        context_description.setWordWrap(True)
        context_layout.addWidget(context_description)
        self.context_text = QPlainTextEdit(context_tab)
        self.context_text.setReadOnly(True)
        self.context_text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        context_layout.addWidget(self.context_text)
        self.tabs.addTab(context_tab, "Context & resources")

        footer = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh", self)
        footer.addWidget(self.refresh_button)
        footer.addStretch()
        self.close_button = QPushButton("Close", self)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        # Prevent Return while editing from accidentally starting/stopping/exporting.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

        self.enable_checkbox.toggled.connect(self._toggle_monitoring)
        self.stop_button.clicked.connect(self.controller.stop)
        self.mark_button.clicked.connect(self._mark_slowdown)
        self.clear_button.clicked.connect(self.controller.clear)
        self.export_button.clicked.connect(self._export_capture)
        self.refresh_button.clicked.connect(self._refresh)
        self.log_folder_button.clicked.connect(self._open_log_folder)
        self.close_button.clicked.connect(self.close)
        self.tabs.currentChanged.connect(self._render_visible_tab)
        self.controller.changed.connect(self._capture_changed)
        self._refresh()

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # The application's dark stylesheet does not define AlternateBase for tables.
        table.setAlternatingRowColors(False)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 250)
        table.setColumnWidth(1, 170)
        return table

    @staticmethod
    def _fill_table(table: QTableWidget, rows: list[list[str]]) -> None:
        table.setUpdatesEnabled(False)
        try:
            table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                for column, text in enumerate(row):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    table.setItem(row_index, column, item)
        finally:
            table.setUpdatesEnabled(True)

    def _toggle_monitoring(self, enabled: bool) -> None:
        if enabled:
            self.controller.start()
        else:
            self.controller.stop()
        self._refresh()

    def _mark_slowdown(self) -> None:
        if self.controller.enabled:
            self.controller.recorder.record_event("user.mark_slowdown", category="marker")
            self._refresh()

    def _capture_changed(self) -> None:
        # A hidden dialog does no snapshots, JSON formatting or table updates.
        if self.isVisible():
            self._refresh()

    def _refresh(self) -> None:
        snapshot_for_display = getattr(self.controller, "snapshot_for_display", self.controller.snapshot)
        self._snapshot = snapshot_for_display()
        enabled = self.controller.enabled
        blocker = QSignalBlocker(self.enable_checkbox)
        self.enable_checkbox.setChecked(enabled)
        del blocker
        self.stop_button.setEnabled(enabled)
        self.mark_button.setEnabled(enabled)
        self.status_label.setText(
            "Monitoring ON — recording timings, responsiveness and sampled stacks."
            if enabled else "Monitoring OFF — capture is stopped. Enable monitoring to begin."
        )
        log_status = str(getattr(self.controller, "log_status", ""))
        last_path = getattr(self.controller, "last_log_path", None)
        self.log_label.setText(
            "Automatic logs: " + (log_status or "Logs are created when monitoring is enabled.")
            + (f"\nLatest capture: {last_path}" if last_path else "")
        )
        self.log_folder_button.setEnabled(self._log_folder() is not None)
        self._render_visible_tab()

    def _log_folder(self) -> Path | None:
        directory = getattr(self.controller, "log_directory", None)
        if directory:
            return Path(directory)
        path = getattr(self.controller, "last_log_path", None)
        return Path(path).parent if path else None

    def _open_log_folder(self) -> None:
        directory = self._log_folder()
        if directory is not None and not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
            QMessageBox.warning(self, "Could not open log folder", f"Open this folder manually:\n{directory}")

    def _render_visible_tab(self, _index: int | None = None) -> None:
        snapshot = self._snapshot
        index = self.tabs.currentIndex()
        if index == 0:
            phases = sorted(snapshot.get("summary", []), key=lambda row: row.get("total_ms", 0), reverse=True)
            self.summary_label.setText(
                f"{len(phases)} recorded phases. Showing up to 50 by total time. "
                "Times include nested calls and must not be added together. "
                "p50/p95 describe retained timing samples."
            )
            active = sorted(
                snapshot.get("active_spans", []),
                key=lambda row: row.get("duration_ms", 0), reverse=True,
            )
            self.active_phase_label.setText(
                "In progress: " + "; ".join(
                    f"{row.get('name', '')} ({_number(row.get('duration_ms'))} ms)"
                    for row in active[:5]
                )
            )
            self.active_phase_label.setVisible(bool(active))
            self._fill_table(self.phase_table, [
                [str(row.get("name", "")), str(row.get("category", "")),
                 str(row.get("count", 0)), _number(row.get("total_ms")),
                 _number(row.get("p50_ms")), _number(row.get("p95_ms")),
                 _number(row.get("max_ms"))]
                for row in phases[:50]
            ])
        elif index == 1:
            self._fill_table(self.event_table, [
                [_number(row.get("t_ms")), str(row.get("name", "")),
                 str(row.get("category", "")), _number(row.get("duration_ms")),
                 _compact(row.get("context", {})), _compact(row.get("details", {}))]
                for row in reversed(snapshot.get("timeline", [])[-100:])
            ])
        elif index == 2:
            self._render_stacks(snapshot)
        else:
            details = {
                key: value for key, value in snapshot.items()
                if key not in {"summary", "timeline", "stack_hotspots", "stalls"}
            }
            text = json.dumps(details, indent=2, ensure_ascii=False, sort_keys=True, default=str)
            if len(text) > 100_000:
                text = text[:100_000] + "\n… Display truncated. Export JSON for all retained data."
            scroll = self.context_text.verticalScrollBar().value()
            self.context_text.setPlainText(text)
            self.context_text.verticalScrollBar().setValue(scroll)

    def _render_stacks(self, snapshot: dict) -> None:
        # Remember expanded rows while recording; otherwise each refresh collapses them.
        expanded = {
            self.stall_tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
            for i in range(self.stall_tree.topLevelItemCount())
            if self.stall_tree.topLevelItem(i).isExpanded()
        }
        self.stall_tree.setUpdatesEnabled(False)
        try:
            self.stall_tree.clear()
            for stall in reversed(snapshot.get("stalls", [])[-20:]):
                time = stall.get("t_ms", 0)
                state = "completed" if stall.get("completed", False) else "in progress"
                item = QTreeWidgetItem([
                    f"{_number(time)} ms — {state}", _number(stall.get("gap_ms")),
                    _compact(stall.get("context", {})),
                ])
                item.setData(0, Qt.ItemDataRole.UserRole, time)
                self.stall_tree.addTopLevelItem(item)
                for label, frames in (("Initial stack", stall.get("stack", [])),
                                      ("Latest stack", stall.get("latest_stack", []))):
                    if not frames or (label == "Latest stack" and frames == stall.get("stack")):
                        continue
                    group = QTreeWidgetItem([label])
                    item.addChild(group)
                    for frame in frames[-80:]:
                        child = QTreeWidgetItem([
                            str(frame.get("function", "")), str(frame.get("line", "")),
                            str(frame.get("file", "")),
                        ])
                        child.setToolTip(2, str(frame.get("file", "")))
                        group.addChild(child)
                    group.setExpanded(True)
                item.setExpanded(time in expanded)
        finally:
            self.stall_tree.setUpdatesEnabled(True)
        hotspots = sorted(
            snapshot.get("stack_hotspots", []), key=lambda row: row.get("samples", 0), reverse=True
        )
        self._fill_table(self.stack_table, [
            [str(row.get("function", "")), str(row.get("file", "")),
             str(row.get("line", "")), str(row.get("samples", 0)),
             str(row.get("leaf_samples", 0))]
            for row in hotspots[:50]
        ])

    def _export_capture(self) -> None:
        # Stop before the native file dialog enters a nested event loop. Waiting for
        # a filename should not become an apparent editor freeze in the export.
        self.controller.stop()
        self._refresh()
        snapshot = self.controller.snapshot()
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export drawing performance capture",
            f"drawing-performance-{datetime.now():%Y%m%d-%H%M%S}.json", "JSON files (*.json)",
        )
        if not filename:
            return
        output = QSaveFile(filename)
        try:
            payload = json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if not output.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(output.errorString())
            if output.write(payload) != len(payload):
                raise OSError(output.errorString())
            if not output.commit():
                raise OSError(output.errorString())
        except (OSError, TypeError, ValueError) as exc:
            output.cancelWriting()
            QMessageBox.warning(self, "Performance export failed", f"Could not save the capture:\n{exc}")
            return
        self.status_label.setText(f"Monitoring OFF — capture exported to {filename}")

    def showEvent(self, event) -> None:
        self._refresh()
        super().showEvent(event)

    def done(self, result: int) -> None:
        # QDialog's Escape/reject path also goes through done().
        self.controller.stop()
        self._refresh()
        super().done(result)

    def closeEvent(self, event) -> None:
        self.controller.stop()
        self._refresh()
        super().closeEvent(event)
