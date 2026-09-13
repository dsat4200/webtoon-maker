"""Capture logs are explicit, durable, detached, and safe to fail."""
from __future__ import annotations

import json
from pathlib import Path
import threading

from comic_editor.core.performance_log import (
    PerformanceLogWriter, render_performance_summary,
)


def _snapshot():
    return {
        "schema_version": 1, "enabled": True,
        "metadata": {"python": "3.11", "sample_count": 8},
        "context": {"tool": "raster_pencil", "solo_entities": [["layer", "ink"]]},
        "summary": [{"name": "canvas.paint", "category": "render", "count": 2,
                     "total_ms": 340, "p95_ms": 220, "max_ms": 220}],
        "active_spans": [{"name": "canvas.set_tool", "duration_ms": 500}],
        "timeline": [{"t_ms": 15, "name": "tool.requested", "details": {
            "previous": "transform", "requested": "raster_pencil"},
            "context": {"solo_entities": [["layer", "ink"]]}}],
        "stalls": [{"gap_ms": 500, "completed": False,
                    "stack": [{"file": "comic_editor/ui/canvas.py", "line": 1404,
                               "function": "_ensure_scene_cache"}]}],
        "stack_hotspots": [{"file": "canvas.py", "line": 1404,
                            "function": "_ensure_scene_cache", "samples": 3}],
        "editor_metrics": {"source_cache_bytes": 1048576,
                           "effect_jobs": {"pending": 3}},
        "metrics": {"cpu_percent": 42, "rss_bytes": 2200000},
        "bounds": {"timeline_capacity": 2000}, "dropped": {"timeline": 7},
    }


def test_construction_and_unused_stop_do_not_start_or_touch_disk(tmp_path):
    destination = tmp_path / "not-created"
    invoked = []
    writer = PerformanceLogWriter(lambda: invoked.append(True), destination)
    writer.stop()
    assert not writer.running
    assert not writer.flush()
    assert not destination.exists()
    assert writer.json_path is None
    assert writer.summary_path is None
    assert invoked == []


def test_start_creates_readable_checkpoint_and_stop_saves_final_on_worker(tmp_path):
    provider_threads = []
    snapshot = _snapshot()

    def provider():
        provider_threads.append(threading.get_ident())
        return snapshot

    writer = PerformanceLogWriter(provider, tmp_path)
    try:
        writer.start()
        assert writer.flush()
        checkpoint = json.loads(writer.json_path.read_text(encoding="utf-8"))
        assert checkpoint["status"] == "recording"
        assert checkpoint["log"]["checkpoint_number"] >= 1
        assert checkpoint["log"]["started_utc"]
        assert checkpoint["log"]["checkpoint_utc"]
        assert checkpoint["context"]["tool"] == "raster_pencil"
        assert "status" not in snapshot  # The supplied snapshot was not changed.
        assert writer.running
        text = writer.summary_path.read_text(encoding="utf-8")
        assert "canvas.paint" in text
        assert "raster_pencil" in text and "solo_entities" in text
        assert "canvas.set_tool" in text  # Unfinished call remains useful.
        assert "_ensure_scene_cache" in text and "1404" in text
        assert "source_cache_bytes" in text and "cpu_percent" in text
        assert "inclusive" in text and "not GPU utilization" in text
        assert "Dropped or overwritten samples" in text
        assert all(identifier != threading.get_ident() for identifier in provider_threads)
    finally:
        writer.stop()
    final = json.loads(writer.json_path.read_text(encoding="utf-8"))
    assert final["status"] == "stopped"
    assert final["log"]["status"] == "stopped"
    assert "**stopped**" in writer.summary_path.read_text(encoding="utf-8")
    assert not writer.running and not writer.error
    assert set(path.name for path in writer.directory.iterdir()) == {"capture.json", "summary.md"}


def test_each_run_is_unique_and_previous_capture_is_unchanged(tmp_path):
    first = PerformanceLogWriter(_snapshot, tmp_path)
    second = PerformanceLogWriter(_snapshot, tmp_path)
    first.start()
    first.stop()
    original_path, original_text = first.json_path, first.json_path.read_bytes()
    second.start()
    second.stop()
    assert first.directory != second.directory
    first.start()
    first.stop()
    assert original_path != first.json_path
    assert original_path.read_bytes() == original_text
    assert len(list(tmp_path.iterdir())) == 3


def test_periodic_worker_checkpoint_needs_no_gui_events(tmp_path):
    saved = threading.Event()
    invocations = 0

    def provider():
        nonlocal invocations
        invocations += 1
        if invocations >= 2:
            saved.set()
        return {"enabled": True}

    writer = PerformanceLogWriter(provider, tmp_path, interval_seconds=0.05)
    try:
        writer.start()
        assert saved.wait(1.0)
        assert writer.flush()
        capture = json.loads(writer.json_path.read_text(encoding="utf-8"))
        assert capture["log"]["checkpoint_number"] >= 2
    finally:
        writer.stop()


def test_minimal_snapshot_without_spans_is_still_a_useful_log(tmp_path):
    writer = PerformanceLogWriter(lambda: {}, tmp_path)
    writer.start()
    writer.stop()
    capture = json.loads(writer.json_path.read_text(encoding="utf-8"))
    assert capture["status"] == "stopped"
    text = writer.summary_path.read_text(encoding="utf-8")
    assert "No completed duration spans yet" in text
    assert "No stall samples recorded" in text
    assert "Missing metrics mean unavailable, not zero" in text


def test_filesystem_error_is_reported_without_throwing(tmp_path):
    destination = tmp_path / "file-instead-of-directory"
    destination.write_text("keep this", encoding="utf-8")
    writer = PerformanceLogWriter(_snapshot, destination)
    writer.start()
    writer.stop()
    assert writer.error
    assert not writer.running
    assert destination.read_text(encoding="utf-8") == "keep this"


def test_provider_error_is_reported_without_creating_partial_capture(tmp_path):
    destination = tmp_path / "unused"

    def broken():
        raise RuntimeError("synthetic snapshot error")

    writer = PerformanceLogWriter(broken, destination)
    writer.start()
    writer.stop()
    assert "synthetic snapshot error" in writer.error
    assert not destination.exists()
    assert not writer.running


def test_worker_start_failure_is_reported_and_stop_remains_safe(tmp_path, monkeypatch):
    def fail_start(_self):
        raise RuntimeError("cannot start worker")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    destination = tmp_path / "unused"
    writer = PerformanceLogWriter(_snapshot, destination)
    writer.start()
    writer.stop()
    assert "cannot start worker" in writer.error
    assert not writer.running
    assert not destination.exists()


def test_failed_atomic_replacement_preserves_previous_file_and_cleans_own_temp(tmp_path, monkeypatch):
    path = tmp_path / "capture.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def denied(self, target):
        raise PermissionError("replacement denied")

    monkeypatch.setattr(Path, "replace", denied)
    try:
        PerformanceLogWriter._atomic_write(path, '{"new": true}')
    except PermissionError:
        pass
    else:
        raise AssertionError("Expected a replacement error")
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.iterdir()) == [path]


def test_summary_is_bounded_and_handles_named_phase_dictionary():
    text = render_performance_summary({
        "summary": {"slow|phase": {"count": 2, "total_ms": 45, "p95_ms": 24, "max_ms": 24}},
        "timeline": [{"name": "tool.changed", "details": "x" * 5000}] * 100,
        "stack_hotspots": [{"file": "x" * 5000}] * 100,
        "editor_metrics": {"large": "x" * 20000},
    })
    assert "slow\\|phase" in text
    assert len(text) < 120000


def test_summary_uses_preserved_transitions_after_timeline_overflow():
    text = render_performance_summary({
        "timeline": [{"name": "cache.get", "duration_ms": 0.01}],
        "recent_transitions": [{"name": "tool.requested", "details": {"requested": "pencil"},
                                "context": {"solo": True}}],
    })
    assert "tool.requested" in text and "pencil" in text and "solo" in text


def test_summary_includes_user_slowdown_mark_with_reproduction_context():
    text = render_performance_summary({
        "recent_transitions": [{"t_ms": 2500, "name": "User marked slowdown", "category": "marker",
                                "context": {"tool": "pencil", "solo": True}}],
    })
    assert "User marked slowdown" in text
    assert "2500" in text and "pencil" in text and "solo" in text
