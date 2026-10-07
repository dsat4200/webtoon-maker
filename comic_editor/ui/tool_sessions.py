"""Input normalization and gesture lifetimes, independent of canvas rendering."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time


@dataclass(frozen=True)
class DeviceSample:
    timestamp: float | None
    tilt_x: float = 0.
    tilt_y: float = 0.
    rotation: float = 0.
    tablet: bool = False


@dataclass(frozen=True)
class PointerInput:
    position: tuple[float, float]
    pressure: float
    timestamp: float | None


class InputAdapter:
    """Map device milliseconds to the monotonic clock once per device epoch.

    Every capture returns a sample. It never coalesces drawing pressure/axes or
    replaces event timestamps with dispatch time. Clock resets start an epoch.
    """
    def __init__(self, clock=None):
        self.clock = clock if clock is not None else lambda: time.monotonic()
        self.offset = None
        self.last_event_time = None

    def capture(self, event, *, tablet=False):
        timestamp = float(getattr(event, "timestamp", lambda: 0)()) / 1000
        if timestamp > 0:
            if self.offset is None or (self.last_event_time is not None and timestamp < self.last_event_time):
                self.offset = self.clock() - timestamp
            self.last_event_time = timestamp
            timestamp += self.offset
        else:
            timestamp = None
        axes = tuple(float(getattr(event, name, lambda: 0.)()) for name in ("xTilt", "yTilt", "rotation")) if tablet else (0., 0., 0.)
        return DeviceSample(timestamp, *axes, tablet)


class Interruption(Enum):
    COMMIT = "commit"
    CANCEL = "cancel"


class ToolSession:
    """One ordered sample stream and exactly one terminal transition.

    Holds no growing event history. Tool kernels own the source/draft and undo
    baseline. Absolute navigation targets may instead use LatestValueInput.
    """
    def __init__(self, begin, update, commit, cancel=None, *, interruption=Interruption.CANCEL):
        self._begin, self._update, self._commit, self._cancel = begin, update, commit, cancel
        self.interruption = interruption
        self.active = False
        self.closed = False
        self.samples = 0

    def begin(self, sample):
        if self.active or self.closed:
            raise RuntimeError("Tool session has already begun")
        self.active = True
        self.samples += 1
        return self._begin(sample)

    def update(self, sample):
        if not self.active or self.closed:
            raise RuntimeError("Tool session is not active")
        self.samples += 1
        return self._update(sample)

    def commit(self, final_sample=None):
        if not self.active or self.closed:
            return None
        if final_sample is not None:
            self.update(final_sample)
        result = self._commit()
        self.active, self.closed = False, True
        return result

    def cancel(self):
        if not self.active or self.closed:
            return None
        self.active, self.closed = False, True
        return self._cancel() if self._cancel is not None else None

    def interrupt(self):
        return self.commit() if self.interruption is Interruption.COMMIT else self.cancel()


class LatestValueInput:
    """Coalesce absolute gizmo/navigation targets, never drawing samples/deltas."""
    def __init__(self, apply):
        self.apply = apply
        self.pending = None

    def queue(self, value):
        self.pending = value

    def flush(self):
        value, self.pending = self.pending, None
        if value is not None:
            return self.apply(value)

    def finish(self, final_value=None):
        if final_value is not None:
            self.queue(final_value)
        return self.flush()

    def cancel(self):
        self.pending = None


def retire_input_sessions(owner):
    """Retire dispatch state after the owner resolves source commit/rollback.

    The source-owning brush must be finished/canceled first. Session retirement
    deliberately does not decide whether focus loss commits a particular tool.
    """
    for name in ("_pointer_tool_session", "_raster_tool_session", "_vector_tool_session",
                 "_free_text_tool_session", "_cage_tool_session"):
        session = getattr(owner, name, None)
        if session is not None:
            session.cancel()
        setattr(owner, name, None)
    for name in ("_navigation_input", "_free_text_input", "_cage_input"):
        stream = getattr(owner, name, None)
        if stream is not None:
            stream.cancel()
    for name in ('_cage_prepare', '_cage_commit_pending', '_cage_commit_error'):
        setattr(owner, name, None)
    jobs = getattr(owner, '_scene_consumers', None)
    if jobs is not None:
        for lane in (('cage-initial',), ('cage-commit',), ('mask-wand',), ('drawing-clipboard',)):
            jobs.cancel(lane)
