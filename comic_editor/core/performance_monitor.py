"""Opt-in, bounded performance capture without Qt or document dependencies.

Constructing a recorder does not install profiling hooks or start any activity.
Only ``start`` creates a sampler. The caller of ``start`` is the sampled thread,
normally the GUI thread. The sampler reads Python stack *locations*, never frame
locals, source lines, Qt objects, or document contents. Python stack sampling can
locate the caller of a slow native operation, but cannot measure GPU execution.
"""
from __future__ import annotations

from collections import deque
import copy
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from itertools import islice
import math
import platform
import sys
import threading
import time
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    """Detach and bound supplied metadata without calling arbitrary repr methods."""
    if budget is None:
        budget = [512]
    budget[0] -= 1
    if budget[0] < 0:
        return "<metadata limit>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:256]
    if depth >= 4:
        return "<depth limit>"
    if isinstance(value, dict):
        return {
            str(key)[:128]: _json_value(item, depth + 1, budget)
            for key, item in islice(value.items(), 32)
            if isinstance(key, (str, int, bool))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth + 1, budget) for item in value[:32]]
    return "<unsupported metadata type>"


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


class PerformanceRecorder:
    """A bounded recorder with locked epoch validation for events and spans.

    ``start`` resumes existing data; ``clear`` resets it without changing whether
    capture is enabled. All storage is bounded, including distinct phase names
    and sampled locations. Percentiles use the most recent bounded durations per
    phase, while counts, totals, and maxima cover the whole capture since clear.
    Timing spans are inclusive, so parent and child totals must not be added.
    """

    def __init__(
        self,
        *,
        sample_interval_ms: float = 20.0,
        stall_threshold_ms: float = 250.0,
        max_events: int = 3000,
        max_phases: int = 256,
        max_durations_per_phase: int = 512,
        max_stack_locations: int = 4096,
        max_stalls: int = 100,
        max_stack_depth: int = 48,
        max_active_spans: int = 128,
        max_transitions: int = 256,
    ) -> None:
        self.enabled = False
        self._sample_interval = max(0.005, float(sample_interval_ms) / 1000.0)
        self._stall_threshold_ms = max(1.0, float(stall_threshold_ms))
        self._bounds = {
            "max_events": max(1, int(max_events)),
            "max_phases": max(1, int(max_phases)),
            "max_durations_per_phase": max(1, int(max_durations_per_phase)),
            "max_stack_locations": max(1, int(max_stack_locations)),
            "max_stalls": max(1, int(max_stalls)),
            "max_stack_depth": max(1, int(max_stack_depth)),
            "max_active_spans": max(1, int(max_active_spans)),
            "max_transitions": max(1, int(max_transitions)),
            "sample_interval_ms": self._sample_interval * 1000.0,
            "stall_threshold_ms": self._stall_threshold_ms,
            "metadata_max_keys": 32,
            "metadata_max_string_length": 256,
            "metadata_max_depth": 4,
            "metadata_max_nodes": 512,
        }
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._target_thread_id: int | None = None
        self._generation = 0
        self._epoch = 0
        self._context: dict[str, Any] = {}
        self._environment: dict[str, str] | None = None
        self._reset_locked()

    def _reset_locked(self) -> None:
        self._epoch += 1
        self._created_at = _utc_now()
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._origin = time.perf_counter()
        self._last_heartbeat = self._origin
        self._active_stall: dict[str, Any] | None = None
        self._timeline: deque[dict[str, Any]] = deque(maxlen=self._bounds["max_events"])
        self._transitions: deque[dict[str, Any]] = deque(maxlen=self._bounds["max_transitions"])
        self._stalls: deque[dict[str, Any]] = deque(maxlen=self._bounds["max_stalls"])
        self._phases: dict[tuple[str, str], dict[str, Any]] = {}
        self._active_spans: dict[int, dict[str, Any]] = {}
        self._next_span_id = 0
        self._locations: dict[tuple[str, str, int], list[int]] = {}
        self._metrics: dict[str, Any] = {}
        self._samples = 0
        self._heartbeat_count = 0
        self._max_heartbeat_gap_ms = 0.0
        self._sessions = 0
        self._capture_seconds = 0.0
        self._active_since: float | None = None
        self._dropped = {
            "timeline_events": 0,
            "phase_events": 0,
            "duration_samples": 0,
            "stack_location_observations": 0,
            "truncated_stacks": 0,
            "stalls": 0,
            "sampling_errors": 0,
            "active_spans": 0,
            "transition_events": 0,
        }

    def start(self, context: dict[str, Any] | None = None) -> None:
        """Begin explicit capture, sampling the calling thread; safe to repeat."""
        detached = _json_value(context) if context is not None else None
        with self._lifecycle_lock:
            with self._lock:
                if isinstance(detached, dict):
                    self._context = detached
                if self.enabled:
                    return
                now = time.perf_counter()
                self._generation += 1
                self._epoch += 1
                self._target_thread_id = threading.get_ident()
                self._last_heartbeat = now
                self._active_stall = None
                self._active_since = now
                self._sessions += 1
                self._started_at = self._started_at or _utc_now()
                self._stopped_at = None
                self._stop_event = threading.Event()
                self.enabled = True
                thread = threading.Thread(
                    target=self._sample_loop,
                    args=(self._stop_event, self._generation),
                    name="drawing-performance-sampler",
                    daemon=True,
                )
                self._thread = thread
            try:
                thread.start()
            except BaseException:
                with self._lock:
                    self.enabled = False
                    self._active_since = None
                    self._thread = None
                    self._stop_event.set()
                raise

    def stop(self) -> None:
        """Disable capture and release its sampler promptly, preserving results."""
        with self._lifecycle_lock:
            with self._lock:
                if self.enabled:
                    now = time.perf_counter()
                    if self._active_since is not None:
                        self._capture_seconds += now - self._active_since
                    self._active_since = None
                    self._stopped_at = _utc_now()
                self.enabled = False
                self._generation += 1
                self._epoch += 1
                self._active_spans.clear()
                self._stop_event.set()
                thread = self._thread
                self._thread = None
                if self._active_stall is not None:
                    self._active_stall["completed"] = False
                    self._active_stall["capture_stopped"] = True
                    self._active_stall = None
            if thread is not None and thread is not threading.current_thread():
                # The sampler only performs bounded in-memory work; setting the
                # event wakes its wait immediately. Joining ensures no old sampler
                # survives a restart or a closed monitor window.
                thread.join()

    def clear(self) -> None:
        """Discard recorded data while preserving capture state and context."""
        with self._lock:
            self._reset_locked()
            if self.enabled:
                self._started_at = _utc_now()
                self._active_since = time.perf_counter()
                self._sessions = 1

    def update_context(self, context: dict[str, Any]) -> None:
        """Replace cached GUI metadata; the sampler never reads a live model."""
        if not self.enabled:
            return
        detached = _json_value(context)
        with self._lock:
            if self.enabled and isinstance(detached, dict):
                if detached != self._context:
                    self._context = detached

    @property
    def capture_epoch(self) -> int:
        """Locked identity of the current capture, including clear boundaries."""
        with self._lock:
            return self._epoch

    def record_event(
        self,
        name: str,
        duration_ms: float | None = None,
        category: str = "event",
        details: dict[str, Any] | None = None,
        *, capture_epoch: int | None = None,
    ) -> None:
        # Pin before detaching payloads. Stop/restart/clear during detachment
        # cannot charge this call to a later capture.
        with self._lock:
            epoch = self._epoch if capture_epoch is None else capture_epoch
            if not self.enabled or epoch != self._epoch:
                return
        name, category = str(name)[:256], str(category)[:128]
        if duration_ms is not None:
            duration_ms = float(duration_ms)
            if not math.isfinite(duration_ms):
                duration_ms = None
            else:
                duration_ms = max(0.0, duration_ms)
        detached = _json_value(details) if details is not None else {}
        with self._lock:
            if self.enabled and epoch == self._epoch:
                self._record_locked(name, duration_ms, category, detached)

    def _record_locked(
        self, name: str, duration_ms: float | None, category: str, details: Any
    ) -> None:
        event = {
            "t_ms": (time.perf_counter() - self._origin) * 1000.0,
            "name": name,
            "category": category,
            "details": details,
            # Context dictionaries are replaced, not mutated, and exported as
            # copies. Sharing them here avoids copying one for every phase.
            "context": self._context,
        }
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        if len(self._timeline) == self._timeline.maxlen:
            self._dropped["timeline_events"] += 1
        self._timeline.append(event)
        # Keep requested-tool/solo breadcrumbs after busy paint/cache phases
        # have displaced them from the general timeline between checkpoints.
        if category in {"transition", "marker", "stall"} or (
            category == "signal" and any(word in name.lower() for word in ("tool", "solo", "selection"))
        ):
            if len(self._transitions) == self._transitions.maxlen:
                self._dropped["transition_events"] += 1
            self._transitions.append(event)
        if duration_ms is None:
            return
        key = (category, name)
        phase = self._phases.get(key)
        if phase is None:
            if len(self._phases) >= self._bounds["max_phases"]:
                self._dropped["phase_events"] += 1
                return
            phase = {
                "count": 0,
                "total_ms": 0.0,
                "max_ms": 0.0,
                "durations": deque(maxlen=self._bounds["max_durations_per_phase"]),
            }
            self._phases[key] = phase
        phase["count"] += 1
        phase["total_ms"] += duration_ms
        phase["max_ms"] = max(phase["max_ms"], duration_ms)
        if len(phase["durations"]) == phase["durations"].maxlen:
            self._dropped["duration_samples"] += 1
        phase["durations"].append(duration_ms)

    def measure(
        self, name: str, category: str = "phase", details: dict[str, Any] | None = None,
        *, capture_epoch: int | None = None,
    ):
        """Time inclusive wall time; an optional epoch pins external ownership."""
        with self._lock:
            epoch = self._epoch if capture_epoch is None else capture_epoch
            if not self.enabled or epoch != self._epoch:
                return nullcontext()
        return self._measure_active(name, category, details, epoch)

    @contextmanager
    def _measure_active(
        self, name: str, category: str, details: dict[str, Any] | None, epoch: int
    ) -> Iterator[None]:
        started = time.perf_counter()
        # Detach details at entry so mutations inside the operation cannot alter
        # its recorded input context.
        detached = _json_value(details) if details is not None else {}
        name, category = str(name)[:256], str(category)[:128]
        span_id = None
        with self._lock:
            accepted = self.enabled and epoch == self._epoch
            if accepted:
                if len(self._active_spans) < self._bounds["max_active_spans"]:
                    self._next_span_id += 1
                    span_id = self._next_span_id
                    self._active_spans[span_id] = {
                        "id": span_id,
                        "name": name,
                        "category": category,
                        "thread_id": threading.get_ident(),
                        "t_ms": (started - self._origin) * 1000.0,
                        "details": detached,
                        "context": self._context,
                    }
                else:
                    self._dropped["active_spans"] += 1
        raised = None
        try:
            yield
        except BaseException as exc:
            raised = type(exc).__name__
            raise
        finally:
            duration = (time.perf_counter() - started) * 1000.0
            with self._lock:
                if accepted and self.enabled and epoch == self._epoch:
                    if span_id is not None:
                        self._active_spans.pop(span_id, None)
                    # Published active-span details remain immutable, allowing
                    # snapshots to detach them after releasing the hot-path lock.
                    final_details = {**detached, "exception_type": raised} if raised is not None else detached
                    self._record_locked(name, duration, category, final_details)

    def record_metrics(self, metrics: dict[str, Any]) -> None:
        """Record supplied process/rendering metrics without collecting them here."""
        if not self.enabled:
            return
        detached = _json_value(metrics)
        with self._lock:
            if self.enabled and isinstance(detached, dict):
                self._metrics = detached
                self._record_locked("Process / rendering metrics", None, "metrics", detached)

    def heartbeat(self) -> None:
        """Call from a GUI timer; gaps include its normal timer interval."""
        if not self.enabled:
            return
        now = time.perf_counter()
        with self._lock:
            if not self.enabled:
                return
            gap_ms = (now - self._last_heartbeat) * 1000.0
            self._last_heartbeat = now
            self._heartbeat_count += 1
            self._max_heartbeat_gap_ms = max(self._max_heartbeat_gap_ms, gap_ms)
            if gap_ms >= self._stall_threshold_ms:
                if self._active_stall is None:
                    self._new_stall_locked(now, gap_ms, [])
                    self._active_stall["sampler_missed_gap"] = True
                self._active_stall["gap_ms"] = gap_ms
                self._active_stall["completed"] = True
                self._record_locked("GUI heartbeat gap", gap_ms, "stall", {})
            self._active_stall = None

    def _new_stall_locked(
        self, now: float, gap_ms: float, stack: list[dict[str, Any]]
    ) -> None:
        stall = {
            "t_ms": (now - self._origin) * 1000.0,
            "gap_ms": gap_ms,
            "stack": stack,
            "latest_stack": stack,
            "context": self._context,
            "completed": False,
        }
        if len(self._stalls) == self._stalls.maxlen:
            self._dropped["stalls"] += 1
        self._stalls.append(stall)
        self._active_stall = stall

    def _sample_loop(self, stop_event: threading.Event, generation: int) -> None:
        while not stop_event.wait(self._sample_interval):
            try:
                self._sample_once(generation)
            except Exception:
                # Diagnostic failures must not take down or interrupt the editor.
                with self._lock:
                    self._dropped["sampling_errors"] += 1

    def _sample_once(self, generation: int) -> None:
        frames = sys._current_frames()
        frame = frames.get(self._target_thread_id)
        stack: list[dict[str, Any]] = []
        truncated = False
        try:
            while frame is not None and len(stack) < self._bounds["max_stack_depth"]:
                code = frame.f_code
                stack.append({
                    "file": code.co_filename,
                    "function": code.co_name,
                    "line": frame.f_lineno,
                })
                frame = frame.f_back
            truncated = frame is not None
        finally:
            # In particular, do not retain frames and therefore artwork/model
            # references indirectly through a sample's frame locals.
            frame = None
            frames.clear()
        if not stack:
            return
        stack.reverse()
        now = time.perf_counter()
        with self._lock:
            if not self.enabled or generation != self._generation:
                return
            self._samples += 1
            if truncated:
                self._dropped["truncated_stacks"] += 1
            # A recursive location receives at most one inclusive count/sample.
            seen: set[tuple[str, str, int]] = set()
            for index, location in enumerate(stack):
                key = (location["file"], location["function"], location["line"])
                leaf = index == len(stack) - 1
                counts = self._locations.get(key)
                if counts is None:
                    if len(self._locations) >= self._bounds["max_stack_locations"]:
                        self._dropped["stack_location_observations"] += 1
                        continue
                    counts = [0, 0]
                    self._locations[key] = counts
                if key not in seen:
                    counts[0] += 1
                if leaf:
                    counts[1] += 1
                seen.add(key)
            gap_ms = (now - self._last_heartbeat) * 1000.0
            self._max_heartbeat_gap_ms = max(self._max_heartbeat_gap_ms, gap_ms)
            if gap_ms >= self._stall_threshold_ms:
                if self._active_stall is None:
                    self._new_stall_locked(now, gap_ms, stack)
                else:
                    self._active_stall["gap_ms"] = gap_ms
                    self._active_stall["latest_stack"] = stack

    def snapshot(self, *, timeline_limit: int | None = None,
                 include_timeline: bool = True) -> dict[str, Any]:
        """Return detached results without blocking input on sorting/deep copies.

        Display refreshes can request a short recent timeline. Logs use the
        default full bounded timeline. Internal published metadata/event/stack
        containers are immutable; mutable counters and stall shells are copied
        under the lock, then all expensive work happens after releasing it.
        """
        if self._environment is None:
            # Lazy once per recorder, never at construction or on input paths.
            self._environment = {
                "python": sys.version,
                "platform": platform.platform(),
                "implementation": platform.python_implementation(),
            }
        with self._lock:
            captured_seconds = self._capture_seconds
            if self._active_since is not None:
                captured_seconds += time.perf_counter() - self._active_since
            phases = [
                (category, name, phase["count"], phase["total_ms"], phase["max_ms"], list(phase["durations"]))
                for (category, name), phase in self._phases.items()
            ]
            locations = [(key, counts[0], counts[1]) for key, counts in self._locations.items()]
            now_ms = (time.perf_counter() - self._origin) * 1000.0
            limit = len(self._timeline) if timeline_limit is None else max(0, int(timeline_limit))
            timeline = list(islice(self._timeline, max(0, len(self._timeline) - limit), None)) if include_timeline else []
            captured = {
                "schema_version": 1,
                "enabled": self.enabled,
                "metadata": {
                    **self._environment,
                    "created_at": self._created_at,
                    "started_at": self._started_at,
                    "stopped_at": self._stopped_at,
                    "snapshot_at": _utc_now(),
                    "capture_seconds": captured_seconds,
                    "sessions": self._sessions,
                    "sample_count": self._samples,
                    "heartbeat_count": self._heartbeat_count,
                    "max_heartbeat_gap_ms": self._max_heartbeat_gap_ms,
                    "target_thread_id": self._target_thread_id,
                    "timing_semantics": "Inclusive spans; totals can overlap. Percentiles use recent bounded samples.",
                    "sampling_semantics": "Python stack locations only; native/GPU work appears at its Python caller. Sampling can be delayed by code holding the GIL.",
                },
                "context": self._context,
                "active_spans": list(self._active_spans.values()),
                "timeline": timeline,
                "recent_transitions": list(self._transitions),
                # Only the shell changes (gap, completed, latest stack pointer).
                # The stack arrays and contexts themselves are immutable.
                "stalls": [dict(stall) for stall in self._stalls],
                "metrics": self._metrics,
                "bounds": dict(self._bounds),
                "dropped": dict(self._dropped),
            }
        report = copy.deepcopy(captured)
        summary = []
        for category, name, count, total, maximum, values in phases:
            values.sort()
            summary.append({
                "name": name, "category": category, "count": count,
                "total_ms": total, "mean_ms": total / count, "max_ms": maximum,
                "p50_ms": _percentile(values, 0.50), "p95_ms": _percentile(values, 0.95),
                "recent_sample_count": len(values),
            })
        summary.sort(key=lambda row: row["total_ms"], reverse=True)
        report["summary"] = summary
        hotspots = [
            {"file": key[0], "function": key[1], "line": key[2], "samples": samples, "leaf_samples": leaf}
            for key, samples, leaf in locations
        ]
        hotspots.sort(key=lambda row: (row["leaf_samples"], row["samples"]), reverse=True)
        report["stack_hotspots"] = hotspots
        for span in report["active_spans"]:
            span["duration_ms"] = max(0.0, now_ms - span["t_ms"])
        return report
