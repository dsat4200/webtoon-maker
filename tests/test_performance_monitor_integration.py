"""Exercise real Qt tool, solo, painting, capture, and teardown paths."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPointingDevice, QTabletEvent
from PySide6.QtTest import QTest
from shiboken6 import delete as delete_qobject, isValid

from comic_editor.core import settings as settings_module
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, SeriesDocument
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.performance_monitor import PerformanceMonitorController


@pytest.fixture
def editor(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(settings_module, "settings_path", lambda: tmp_path / "settings.json")
    window = MainWindow()
    chapter = ChapterDocument(width=500, height=1200)
    page = chapter.add_page("Synthetic page", BoundGeometry.rectangle(0, 0, 500, 1200))
    layer = chapter.add_layer(page.layer_id, "Synthetic drawing layer")
    raster = chapter.add_object(layer.layer_id, RasterObject())
    window.series = SeriesDocument()
    monkeypatch.setattr(window, "_schedule_series_preferences_save", lambda **_kwargs: None)
    window._set_chapter(chapter, TileStore())
    window.resize(1200, 850)
    window.show()
    qapp.processEvents()
    yield window, layer, raster
    monitor = getattr(window, "_performance_monitor_controller", None)
    if monitor:
        monitor.stop()
    if not isValid(window):
        return
    window._dirty = False
    window.canvas._effect_jobs.cancel()
    window.close()
    window.deleteLater()


@pytest.fixture
def controller(editor, tmp_path):
    window, _layer, _raster = editor
    monitor = PerformanceMonitorController(window, log_directory=tmp_path / "capture-runs")
    window._performance_monitor_controller = monitor
    yield monitor
    monitor.stop()


def _phases(controller):
    return {row["name"]: row for row in controller.snapshot()["summary"]}


def test_top_left_action_opens_a_lazy_disabled_monitor(editor, qapp):
    window, _layer, _raster = editor
    assert getattr(window, "_performance_monitor_controller", None) is None
    assert window.file_toolbar.actions()[1] is window.performance_monitor_action
    button = window.file_toolbar.widgetForAction(window.performance_monitor_action)
    assert button.isVisible()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    monitor = window._performance_monitor_controller
    dialog = window._performance_monitor_dialog
    assert dialog.isVisible() and not dialog.isModal()
    assert not monitor.enabled and not dialog.enable_checkbox.isChecked()
    assert not monitor.timer.isActive()
    assert monitor.recorder._thread is None
    assert monitor._writer is None
    assert not monitor._filter_installed and not monitor._patches
    assert not monitor.log_directory.exists()
    assert not window.canvas._performance.enabled
    frame_samples = list(window.canvas._performance.frame_ms)
    window.canvas.grab()
    qapp.processEvents()
    assert list(window.canvas._performance.frame_ms) == frame_samples
    dialog.close()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    assert window._performance_monitor_dialog is dialog
    assert not monitor.enabled


def test_real_tool_actions_record_solo_layer_to_raster_transition(editor, controller, qapp):
    window, layer, raster = editor
    canvas = window.canvas
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    canvas.set_selection("layer", layer.layer_id)
    previous_tool = canvas.tool.value
    assert layer.last_raster_id == raster.object_id
    controller.start()
    canvas.set_solo_entities({("layer", layer.layer_id)})
    qapp.processEvents()
    # Pencil's button is intentionally hidden for layer selection. Its real
    # keyboard action selects last_raster_id before activating the drawing tool.
    canvas.setFocus()
    QTest.keyClick(canvas, Qt.Key.Key_P)
    qapp.processEvents()
    assert canvas.tool == ToolKind.RASTER_PENCIL
    assert canvas.selected_kind == "object" and canvas.selected_id == raster.object_id
    events = controller.snapshot()["timeline"]
    begin = next(row for row in events if row["name"] == "window.activate_tool.begin")
    assert begin["details"]["requested_tool"] == "raster_pencil"
    assert begin["details"]["previous_tool"] == previous_tool
    assert begin["context"]["selection"]["kind"] == "layer"
    assert begin["context"]["selection"]["id"] == layer.layer_id
    assert begin["context"]["solo"]["enabled"] is True
    end = next(row for row in events if row["name"] == "window.activate_tool.end")
    assert end["context"]["selection"]["id"] == raster.object_id
    assert end["context"]["tool"] == "raster_pencil"
    assert any(row["name"] == "canvas.set_solo_entities.begin" for row in events)
    assert "window.activate_tool" in _phases(controller)
    assert "canvas.set_selection" in _phases(controller)
    eraser = window.tool_buttons[ToolKind.RASTER_ERASER]
    assert eraser.isVisible() and eraser.isEnabled()
    QTest.mouseClick(eraser, Qt.MouseButton.LeftButton)
    assert canvas.tool == ToolKind.RASTER_ERASER
    begin = next(row for row in reversed(controller.snapshot()["timeline"])
                 if row["name"] == "window.activate_tool.begin")
    assert begin["details"]["requested_tool"] == "raster_eraser"
    assert begin["details"]["previous_tool"] == "raster_pencil"
    assert begin["context"]["solo"]["enabled"] is True


def test_native_qt_paints_are_timed_and_stop_restores_every_probe(editor, controller, qapp):
    window, _layer, _raster = editor
    canvas = window.canvas
    original_paint = canvas.paintEvent
    original_tool = canvas.set_tool
    original_navigator = window.preview._render_live_preview
    controller.start()
    sampler = controller.recorder._thread
    writer = controller._writer
    assert controller.timer.isActive()
    assert sampler is not None and sampler.is_alive()
    assert writer.running and controller._filter_installed and controller._patches
    installed_probes = list(controller._patches)
    assert canvas._performance.enabled
    canvas._invalidate_scene_cache()
    # grab() asks Qt to deliver a real paintEvent; do not invoke the patched
    # Python method directly, which would miss virtual-dispatch regressions.
    assert not canvas.grab().isNull()
    window.navigator_panel.setExpanded(True, emit=False)
    window.preview.invalidate_all()
    assert not window.preview.grab().isNull()
    QTest.qWait(window.preview.REFRESH_DELAY_MS + 30)
    phases = _phases(controller)
    assert phases["canvas.paintEvent"]["count"] >= 1
    assert phases["canvas.paint_document_projection"]["count"] >= 1
    assert phases["navigator.request"]["count"] >= 1
    assert phases["scene.request"]["count"] >= 1
    assert "canvas.render_preview" not in phases
    assert controller._counts["paint.request"] >= 1
    controller.stop()
    assert not controller.enabled and not controller.timer.isActive()
    assert not sampler.is_alive() and not writer.running
    assert not controller._filter_installed and not controller._patches
    assert not controller._connections and not canvas._performance.enabled
    assert all(getattr(owner, name, None) is not wrapper
               for owner, name, _previous, wrapper in installed_probes)
    assert canvas.paintEvent == original_paint
    assert canvas.set_tool == original_tool
    assert window.preview._render_live_preview == original_navigator
    frame_samples = list(canvas._performance.frame_ms)
    stopped_events = controller.snapshot()["timeline"]
    stopped_counts = dict(controller._counts)
    canvas.grab()
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    qapp.processEvents()
    assert controller.snapshot()["timeline"] == stopped_events
    assert dict(controller._counts) == stopped_counts
    assert list(canvas._performance.frame_ms) == frame_samples
    saved = json.loads(Path(controller.last_log_path).read_text(encoding="utf-8"))
    assert saved["enabled"] is False
    assert saved["status"] == "stopped"
    assert writer.summary_path.is_file()


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "tablet"])
def test_real_drawing_events_record_stroke_phases_and_input_timings(editor, controller, stylus):
    window, layer, raster = editor
    canvas = window.canvas
    canvas.set_selection("object", raster.object_id)
    canvas.set_solo_entities({("layer", layer.layer_id)})
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    revision = canvas.command_stack.revision
    controller.start()
    prefix = "tablet" if stylus else "mouse"
    types = (
        (QEvent.Type.TabletPress, QEvent.Type.TabletMove, QEvent.Type.TabletRelease)
        if stylus else
        (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease)
    )
    for index, event_type in enumerate(types):
        position = canvas.document_to_widget(QPointF(35 + index * 20, 40 + index * 10))
        button = Qt.MouseButton.NoButton if index == 1 else Qt.MouseButton.LeftButton
        buttons = Qt.MouseButton.NoButton if index == 2 else Qt.MouseButton.LeftButton
        if stylus:
            event = QTabletEvent(
                event_type, QPointingDevice.primaryPointingDevice(), position, position,
                0.0 if index == 2 else 0.7, 0.0, 0.0, 0.0, 0.0, 0.0,
                Qt.KeyboardModifier.NoModifier, button, buttons,
            )
        else:
            event = QMouseEvent(
                event_type, position, position, button, buttons, Qt.KeyboardModifier.NoModifier
            )
        QCoreApplication.sendEvent(canvas, event)
    assert not canvas._drawing
    assert canvas.command_stack.revision > revision
    phases = _phases(controller)
    for name in ("canvas.begin_stroke", "canvas.continue_stroke", "canvas.end_stroke"):
        assert phases[name]["count"] >= 1
    assert canvas._performance.input_ms
    assert controller._counts[prefix + ".press"] == 1
    assert controller._counts[prefix + ".release"] == 1
    events = controller.snapshot()["timeline"]
    press = next(row for row in events if row["name"] == prefix + ".press")
    assert press["context"]["solo"]["enabled"] is True
    assert press["context"]["selection"]["id"] == raster.object_id


def test_slow_instrumented_call_captures_heartbeat_stall_and_code_location(
    editor, controller, monkeypatch, qapp,
):
    window, layer, _raster = editor
    canvas = window.canvas
    canvas.set_solo_entities({("layer", layer.layer_id)})
    original = canvas._invalidate_scene_cache

    def intentionally_slow_invalidation(*args):
        time.sleep(0.40)
        return original(*args)

    monkeypatch.setattr(canvas, "_invalidate_scene_cache", intentionally_slow_invalidation)
    controller.start()
    canvas._invalidate_scene_cache()
    # Deliver the timer that was unable to run during the simulated freeze.
    QTest.qWait(130)
    snapshot = controller.snapshot()
    assert _phases(controller)["canvas.invalidate_scene_cache"]["max_ms"] >= 350
    assert snapshot["metadata"]["max_heartbeat_gap_ms"] >= 350
    assert snapshot["stalls"]
    assert snapshot["stalls"][-1]["context"]["solo"]["enabled"] is True
    frames = [frame for stall in snapshot["stalls"] for frame in stall["stack"]]
    assert any(frame["function"] == "intentionally_slow_invalidation"
               and frame["file"].endswith("test_performance_monitor_integration.py")
               and frame["line"] > 0 for frame in frames)
    assert any(row["function"] == "intentionally_slow_invalidation"
               for row in snapshot["stack_hotspots"])


def test_every_enabled_run_gets_its_own_logs_and_disabled_activity_writes_nothing(
    editor, controller, qapp,
):
    window, _layer, _raster = editor
    paths = []
    for name in ("first-run", "second-run"):
        controller.start()
        controller.recorder.record_event(name, category="marker")
        controller.stop()
        paths.append(Path(controller.last_log_path))
    assert paths[0] != paths[1]
    first = json.loads(paths[0].read_text(encoding="utf-8"))
    second = json.loads(paths[1].read_text(encoding="utf-8"))
    assert any(row["name"] == "first-run" for row in first["timeline"])
    assert not any(row["name"] == "first-run" for row in second["timeline"])
    assert any(row["name"] == "second-run" for row in second["timeline"])
    before = {path: (path.stat().st_mtime_ns, path.read_bytes())
              for path in controller.log_directory.rglob("*") if path.is_file()}
    window.canvas.grab()
    window.canvas.set_tool(ToolKind.OBJECT_SELECT)
    QTest.qWait(130)
    after = {path: (path.stat().st_mtime_ns, path.read_bytes())
             for path in controller.log_directory.rglob("*") if path.is_file()}
    assert before == after


def test_closing_real_dialog_or_editor_finalizes_run(editor, qapp):
    window, _layer, _raster = editor
    window.performance_monitor_action.trigger()
    controller = window._performance_monitor_controller
    dialog = window._performance_monitor_dialog
    dialog.enable_checkbox.setChecked(True)
    assert controller.enabled
    dialog.close()
    assert not controller.enabled and not controller.timer.isActive()
    assert Path(controller.last_log_path).is_file()
    first_path = controller.last_log_path
    window.performance_monitor_action.trigger()
    assert not dialog.enable_checkbox.isChecked()
    dialog.enable_checkbox.setChecked(True)
    assert controller.enabled
    window._dirty = False
    window.close()
    assert not controller.enabled and not controller.timer.isActive()
    assert controller.last_log_path != first_path
    assert json.loads(Path(controller.last_log_path).read_text(encoding="utf-8"))["enabled"] is False


def test_failed_enable_and_failed_final_metrics_leave_no_active_probes(editor, controller, monkeypatch):
    window, _layer, _raster = editor
    original_tool = window.canvas.set_tool

    def unavailable_metrics():
        raise RuntimeError("Synthetic missing metrics")

    monkeypatch.setattr(controller, "_metrics", unavailable_metrics)
    with pytest.raises(RuntimeError, match="Synthetic missing metrics"):
        controller.start()
    assert not controller.enabled
    assert controller.recorder._thread is None
    assert not controller.timer.isActive()
    assert not controller._filter_installed and not controller._patches
    assert not controller._connections and not window.canvas._performance.enabled
    assert window.canvas.set_tool == original_tool


def test_clear_while_enabled_preserves_previous_run_then_starts_fresh(editor, controller):
    controller.start()
    first_path = Path(controller.last_log_path)
    controller.recorder.record_event("before-clear", category="marker")
    controller.clear()
    assert controller.enabled and controller.timer.isActive()
    assert Path(controller.last_log_path) != first_path
    first = json.loads(first_path.read_text(encoding="utf-8"))
    assert first["status"] == "stopped"
    assert any(row["name"] == "before-clear" for row in first["timeline"])
    assert not any(row["name"] == "before-clear" for row in controller.snapshot()["timeline"])


@pytest.mark.parametrize("target_name", ["controller", "window"])
@pytest.mark.parametrize("deferred", [True, False], ids=["deleteLater", "direct-delete"])
def test_qobject_deletion_restores_global_probes_and_finalizes_logs(
    editor, controller, target_name, deferred,
):
    from comic_editor.ui import modifier_rendering
    from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer

    window, _layer, _raster = editor
    original_gpu = GpuPatternRenderer.render
    original_effects = modifier_rendering.apply_modifier_stack
    controller.start()
    sampler = controller.recorder._thread
    writer = controller._writer
    path = Path(controller.last_log_path)
    target = controller if target_name == "controller" else window
    if deferred:
        target.deleteLater()
        QCoreApplication.sendPostedEvents(target, QEvent.Type.DeferredDelete)
    else:
        delete_qobject(target)
    assert not isValid(target)
    assert not controller.enabled
    assert not sampler.is_alive() and not writer.running
    assert GpuPatternRenderer.render is original_gpu
    assert modifier_rendering.apply_modifier_stack is original_effects
    assert not controller._patches and not controller._connections
    assert not window.canvas._performance.enabled
    capture = json.loads(path.read_text(encoding="utf-8"))
    assert capture["enabled"] is False and capture["status"] == "stopped"


@pytest.mark.parametrize("stage", ["context", "install"])
def test_early_or_partial_install_failure_cleans_up_active_owner(editor, controller, monkeypatch, stage):
    from comic_editor.ui import performance_monitor
    from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer

    original_gpu = GpuPatternRenderer.render
    original_context = controller.context
    original_install = controller._install

    def fail_context():
        raise RuntimeError("Synthetic context failure")

    def fail_install():
        original_install()
        raise RuntimeError("Synthetic install failure")

    monkeypatch.setattr(controller, "context" if stage == "context" else "_install",
                        fail_context if stage == "context" else fail_install)
    with pytest.raises(RuntimeError, match="Synthetic"):
        controller.start()
    assert not controller.enabled and controller.recorder._thread is None
    assert not controller.timer.isActive()
    assert not controller._filter_installed and not controller._patches
    assert not controller._connections and controller._resources is None
    assert performance_monitor._active_controller is None
    assert GpuPatternRenderer.render is original_gpu
    monkeypatch.setattr(controller, "context", original_context)
    monkeypatch.setattr(controller, "_install", original_install)
    controller.start()
    assert controller.enabled and controller._writer.running


def test_log_checkpoint_reads_detached_data_without_querying_qt_from_worker(editor, controller, monkeypatch):
    window, _layer, _raster = editor
    main_thread = threading.get_ident()
    original_context = controller.context
    original_metrics = controller._metrics
    original_width = window.canvas.width

    def context_on_gui():
        assert threading.get_ident() == main_thread
        return original_context()

    def metrics_on_gui():
        assert threading.get_ident() == main_thread
        return original_metrics()

    def width_on_gui():
        assert threading.get_ident() == main_thread
        return original_width()

    monkeypatch.setattr(controller, "context", context_on_gui)
    monkeypatch.setattr(controller, "_metrics", metrics_on_gui)
    monkeypatch.setattr(window.canvas, "width", width_on_gui)
    controller.start()
    assert controller._writer.flush()
    assert not controller._writer.error
    controller.stop()
    assert not controller._writer.error


def test_destroy_after_clear_does_not_replace_an_already_frozen_final_snapshot(editor, controller):
    controller.start()
    controller.recorder.record_event("preserve-final-run", category="marker")
    controller.stop()
    writer = controller._writer
    frozen_provider = writer.snapshot_provider
    controller.clear()
    assert not controller.snapshot()["timeline"]
    controller.deleteLater()
    QCoreApplication.sendPostedEvents(controller, QEvent.Type.DeferredDelete)
    assert writer.snapshot_provider is frozen_provider
    assert any(row["name"] == "preserve-final-run" for row in writer.snapshot_provider()["timeline"])
