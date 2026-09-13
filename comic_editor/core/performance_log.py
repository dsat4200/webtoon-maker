"""Opt-in, bounded-background performance checkpoints for later diagnosis.

The provider must return a detached, thread-safe snapshot. It must never read
QObjects, a canvas, or the mutable document: the controller supplies cached
editor context together with the recorder's locked snapshot.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(value: Any, limit: int = 260) -> str:
    if isinstance(value, (dict, list, tuple)):
        result = json.dumps(value, ensure_ascii=False, default=str)
    else:
        result = str(value)
    result = result.replace("\n", " ").replace("\r", " ")
    return result[:limit] + ("…" if len(result) > limit else "")


def _cell(value: Any) -> str:
    return _text(value).replace("|", "\\|").replace("`", "'")


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list:
    if isinstance(value, Mapping):
        return [dict(item, name=name) if isinstance(item, Mapping)
                else {"name": name, "value": item}
                for name, item in list(value.items())[:80]]
    return list(value[:80]) if isinstance(value, (list, tuple)) else []


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first(row: Mapping, *keys: str, default: Any = "—") -> Any:
    return next((row[key] for key in keys if key in row), default)


def _section_mapping(lines: list[str], heading: str, value: Any) -> None:
    lines.extend(["", f"## {heading}", ""])
    mapping = _mapping(value)
    if not mapping:
        lines.append("No samples available.")
        return
    for key, item in list(mapping.items())[:60]:
        lines.append(f"- **{_cell(key)}:** {_text(item, 900)}")


def render_performance_summary(snapshot: Mapping[str, Any]) -> str:
    """Render a compact reading guide; capture.json contains the full snapshot."""
    log = _mapping(snapshot.get("log"))
    lines = [
        "# Drawing performance capture", "",
        f"Status: **{_cell(log.get('status', snapshot.get('status', 'unknown')))}**",
        f"Checkpoint (UTC): {_text(log.get('checkpoint_utc', 'unknown'))}",
        f"Capture started (UTC): {_text(log.get('started_utc', 'unknown'))}", "",
        "Read this summary with `capture.json` for complete bounded events, "
        "contexts, and stacks. A recording checkpoint remains available if the "
        "editor freezes or closes unexpectedly; it may precede the final stall.", "",
        "Phase times are wall-clock durations. Nested spans are inclusive; "
        "do not add parent and child totals. Sampled Python stacks identify "
        "where the GUI thread was observed, not a complete profiler or proof "
        "of causation. GPU submission/readback timing is not GPU utilization, "
        "GPU execution time, presentation time, or physical pen latency.",
    ]
    _section_mapping(lines, "Capture metadata", snapshot.get("metadata"))
    _section_mapping(lines, "Current reproduction context", snapshot.get("context"))
    _section_mapping(lines, "Editor, cache, and worker metrics", snapshot.get("editor_metrics"))

    lines.extend(["", "## Slow phases", ""])
    summary = snapshot.get("summary", [])
    if isinstance(summary, Mapping) and any(k in summary for k in ("phases", "spans", "operations")):
        summary = _first(summary, "phases", "spans", "operations", default=[])
    rows = [row for row in _rows(summary) if isinstance(row, Mapping)]
    rows.sort(key=lambda row: _number(_first(row, "total_ms", "inclusive_ms", "max_ms", default=0)), reverse=True)
    if rows:
        lines.extend([
            "| Phase | Count | Total ms (inclusive) | P95 ms | Max ms |",
            "|---|---:|---:|---:|---:|",
        ])
        for row in rows[:35]:
            lines.append("| " + " | ".join(_cell(item) for item in (
                _first(row, "name", "phase", "operation"),
                _first(row, "count", "samples"),
                _first(row, "total_ms", "inclusive_ms"),
                _first(row, "p95_ms", "p95"),
                _first(row, "max_ms", "max"),
            )) + " |")
    else:
        lines.append("No completed duration spans yet. Inspect transitions and stall samples below; an unfinished call may still be the problem.")

    lines.extend(["", "### Calls still active at the checkpoint", ""])
    active = _rows(snapshot.get("active_spans"))
    if active:
        lines.extend(f"- {_text(item, 1800)}" for item in active[-30:])
    else:
        lines.append("No active spans in this snapshot.")

    lines.extend(["", "## Recent tool, selection, and solo transitions and user marks", ""])
    # Dedicated breadcrumbs survive high-volume cache/paint phases overwriting
    # the general event ring between background checkpoints.
    timeline = snapshot.get("recent_transitions") or snapshot.get("timeline", [])
    transitions = []
    if isinstance(timeline, (list, tuple)):
        for entry in timeline:
            if not isinstance(entry, Mapping):
                continue
            label = str(_first(entry, "name", "phase", "event", "kind", default=""))
            if entry.get("category") == "marker" or any(
                word in label.lower() for word in ("tool", "solo", "selection", "record", "start", "stop")
            ):
                transitions.append(entry)
    if transitions:
        for entry in transitions[-35:]:
            lines.append(f"- {_text(entry, 1600)}")
    else:
        lines.append("No matching transitions recorded yet.")

    lines.extend(["", "## Stalls and sampled source locations", ""])
    stalls = _rows(snapshot.get("stalls"))
    if stalls:
        for index, entry in enumerate(stalls[-15:], 1):
            lines.append(f"### Stall {index}")
            lines.append("")
            if isinstance(entry, Mapping):
                for key, value in list(entry.items())[:30]:
                    if key in {"stack", "latest_stack", "frames", "traceback"}:
                        lines.extend([f"{_cell(key)}:", "", "```text"])
                        if isinstance(value, (list, tuple)):
                            lines.extend(_text(frame, 700) for frame in value[-35:])
                        else:
                            lines.append(str(value)[:18000].replace("```", "'''"))
                        lines.extend(["```", ""])
                    else:
                        lines.append(f"- **{_cell(key)}:** {_text(value, 1400)}")
            else:
                lines.append(_text(entry, 1800))
            lines.append("")
    else:
        lines.append("No stall samples recorded.")
    lines.extend(["", "### Stack hot spots", ""])
    hotspots = _rows(snapshot.get("stack_hotspots"))
    lines.extend(f"- {_text(item, 1400)}" for item in hotspots[:30])
    if not hotspots:
        lines.append("No sampled stack hot spots.")

    _section_mapping(lines, "CPU, memory, and monitoring metrics", snapshot.get("metrics"))
    _section_mapping(lines, "Recording bounds", snapshot.get("bounds"))
    _section_mapping(lines, "Dropped or overwritten samples", snapshot.get("dropped"))
    lines.extend(["", "## Monitoring overhead and limits", "",
        "Recording adds phase clocks, bounded event storage, stack sampling, "
        "periodic editor-context collection, and background checkpoint encoding "
        "and disk writes. Compare equivalent reproductions with recording off "
        "when evaluating a performance change. The writer uses cached, detached "
        "snapshots and does not query the document or Qt widgets from its thread.", "",
        f"Checkpoint write duration, previous save: {_text(log.get('previous_write_ms', 'unavailable'))} ms.",
        f"Checkpoint interval: {_text(log.get('interval_seconds', 'unknown'))} seconds.",
        "All quantities are observed samples or estimates, not guaranteed CPU/GPU attribution. "
        "Missing metrics mean unavailable, not zero. Recording is opt-in and stops with the monitor.", "",
    ])
    return "\n".join(lines)


class PerformanceLogWriter:
    """Create one unique run directory only after an explicit ``start`` call."""

    def __init__(self, snapshot_provider: Callable[[], Mapping[str, Any]], directory: Path,
                 *, interval_seconds: float = 2.0) -> None:
        self.snapshot_provider = snapshot_provider
        self._base_directory = Path(directory)
        self.directory = self._base_directory
        self.json_path: Path | None = None
        self.summary_path: Path | None = None
        self.error = ""
        self.interval_seconds = max(0.05, float(interval_seconds))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._initial_saved = threading.Event()
        self._checkpoint_condition = threading.Condition()
        self._checkpoint = 0
        self._started_utc = ""
        self._previous_write_ms = 0.0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        identifier = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:10]
        self.directory = self._base_directory / identifier
        self.json_path = self.directory / "capture.json"
        self.summary_path = self.directory / "summary.md"
        self.error = ""
        self._stop_event.clear()
        self._wake_event.clear()
        self._initial_saved.clear()
        self._checkpoint = 0
        self._previous_write_ms = 0.0
        self._started_utc = _utc()
        self._thread = threading.Thread(target=self._run, name="drawing-performance-log", daemon=True)
        try:
            self._thread.start()
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            self._thread = None
            return
        # Usually a few milliseconds. A slow disk/provider must not freeze UI startup.
        self._initial_saved.wait(0.5)

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        self._wake_event.set()
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if thread.is_alive():
            self.error = "Performance log is still finishing; shutdown wait reached 2 seconds."

    def flush(self, timeout: float = 2.0) -> bool:
        """Request a checkpoint and wait boundedly; useful before reading a log."""
        if not self.running:
            return self._checkpoint > 0 and not self.error
        with self._checkpoint_condition:
            previous = self._checkpoint
            self._wake_event.set()
            self._checkpoint_condition.wait_for(
                lambda: self._checkpoint > previous or not self.running,
                timeout=max(0.0, min(2.0, timeout)),
            )
            return self._checkpoint > previous and not self.error

    @staticmethod
    def _atomic_write(path: Path, contents: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as output:
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            # Only our exact temporary file, never recursive cleanup.
            temporary.unlink(missing_ok=True)

    def _save(self, status: str) -> None:
        started = time.perf_counter()
        try:
            snapshot = dict(self.snapshot_provider())
            snapshot["status"] = status
            snapshot["log"] = {
                "status": status,
                "started_utc": self._started_utc,
                "checkpoint_utc": _utc(),
                "checkpoint_number": self._checkpoint + 1,
                "interval_seconds": self.interval_seconds,
                "previous_write_ms": round(self._previous_write_ms, 3),
                "capture_file": "capture.json", "summary_file": "summary.md",
            }
            serialized = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str) + "\n"
            summary = render_performance_summary(snapshot)
            self.directory.mkdir(parents=True, exist_ok=True)
            assert self.json_path is not None and self.summary_path is not None
            self._atomic_write(self.json_path, serialized)
            self._atomic_write(self.summary_path, summary)
            self.error = ""
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
        finally:
            self._previous_write_ms = (time.perf_counter() - started) * 1000.0
            with self._checkpoint_condition:
                self._checkpoint += 1
                self._checkpoint_condition.notify_all()

    def _run(self) -> None:
        try:
            self._save("recording")
            self._initial_saved.set()
            while not self._stop_event.is_set():
                self._wake_event.wait(self.interval_seconds)
                self._wake_event.clear()
                if not self._stop_event.is_set():
                    self._save("recording")
            self._save("stopped")
        finally:
            self._initial_saved.set()
            with self._checkpoint_condition:
                self._checkpoint_condition.notify_all()
