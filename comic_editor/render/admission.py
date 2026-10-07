"""Shared admission for memory-intensive detached rendering consumers.

Tickets are acquired off the GUI. Nested GPU/kernel work borrows its caller's
ticket, preventing a scene waiting for its own reservation. Retained pixel and
texture caches retain their own byte LRUs; a ticket bounds concurrent work.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from itertools import count
from threading import Condition
import time


class WorkCancelled(Exception):
    pass


@dataclass(frozen=True)
class WorkTicket:
    owner: str
    estimated_bytes: int
    priority: int
    oversized: bool


_active_ticket = ContextVar("native_render_ticket", default=None)


def current_ticket():
    return _active_ticket.get()


class CopyLease:
    def __init__(self, admission, marker, size):
        self.admission,self.marker,self.size = admission,marker,size
        self.released = False

    def release(self):
        with self.admission.condition:
            if not self.released:
                self.released = True
                self.admission.copies.pop(self.marker,None)
                self.admission.condition.notify_all()


class WorkAdmission:
    def __init__(self, budget=256 * 1024 * 1024):
        self.budget = max(1, int(budget))
        self.condition = Condition()
        self.sequence = count()
        self.waiters = {}
        self.running = {}
        self.copies = {}
        self.copy_credits = {}
        self.peak_bytes = 0
        self.admitted = 0

    @contextmanager
    def reserve(self, owner, size, *, priority=0, cancelled=lambda: False, copies=None):
        existing = current_ticket()
        if existing is not None:
            yield existing
            return
        size = max(0, int(size))
        marker = next(self.sequence)
        started = time.monotonic()
        with self.condition:
            self.waiters[marker] = priority, started
            try:
                while True:
                    if cancelled():
                        raise WorkCancelled()
                    # Aging prevents thumbnail/export starvation during long
                    # drawing sessions while visible work normally wins.
                    now = time.monotonic()
                    first = min(self.waiters, key=lambda key:
                        (self.waiters[key][0] - int((now - self.waiters[key][1]) / 2), key))
                    occupied = self._occupied()
                    credit = (min(size,copies.size) if copies is not None and not copies.released else 0)
                    heavy = size >= 16 * 1024 * 1024
                    eligible = (not self.running if size > self.budget else
                        len(self.running) < 4 and occupied + size-credit <= self.budget and
                        (not heavy or not any(ticket.estimated_bytes >= 16 * 1024 * 1024
                                             for ticket in self.running.values())))
                    if eligible and marker == first:
                        ticket = WorkTicket(owner, size, priority, size > self.budget)
                        self.running[marker] = ticket
                        self.copy_credits[marker] = credit
                        self.admitted += 1
                        self.peak_bytes = max(self.peak_bytes, occupied + size-credit)
                        break
                    self.condition.wait(.05)
            finally:
                self.waiters.pop(marker, None)
        token = _active_ticket.set(ticket)
        try:
            yield ticket
        finally:
            _active_ticket.reset(token)
            with self.condition:
                self.running.pop(marker, None)
                self.copy_credits.pop(marker,None)
                self.condition.notify_all()

    def _occupied(self):
        return (sum(ticket.estimated_bytes for ticket in self.running.values())
            +sum(size for _,size in self.copies.values())-sum(self.copy_credits.values()))

    @property
    def copied_bytes(self):
        with self.condition:
            return sum(size for _,size in self.copies.values())

    def reserve_copies(self, size):
        """Nonblocking ownership of queued arrays and output buffers.

        A sole admitted parent may borrow one oversized workspace. Unadmitted
        GUI submissions decline immediately; no allocation or wait happens here.
        """
        size = max(0,int(size))
        parent = current_ticket()
        with self.condition:
            occupied = self._occupied()
            exclusive = (parent is not None and len(self.running) == 1
                and next(iter(self.running.values())) is parent
                and all(ticket is parent for ticket,_ in self.copies.values()))
            if occupied+size > self.budget and not exclusive:
                return None
            marker = next(self.sequence)
            self.copies[marker] = parent,size
            self.peak_bytes = max(self.peak_bytes,occupied+size)
            return CopyLease(self,marker,size)


RENDER_ADMISSION = WorkAdmission()


def snapshot_working_bytes(snapshot, *, block_pixels=1028 * 1028):
    """Conservative known source/working-buffer reservation, without decoding."""
    pixels = 0
    for source in snapshot.tiles._tiles.values():
        pixels += len(source) * snapshot.tiles.tile_size ** 2 * 4
    # Source caches are bounded; clean chapter tiles do not decode all at once.
    source_bytes = min(pixels, snapshot.tiles.residency.budget)
    image_sizes = [int(getattr(obj, 'pixel_width', 0) or 0) * int(getattr(obj, 'pixel_height', 0) or 0)
        for obj in snapshot.chapter.objects.values()]
    # The source owner may retain one oversized original exclusively. Native
    # decode and working-space conversion cannot be bounded by its usual LRU
    # capacity; include the largest source's simultaneously owned buffers.
    native_sizes = [int(image.sizeInBytes()) for image in snapshot.images._decoded.values()]
    largest_workspace = max([pixels * 16 * 3 for pixels in image_sizes]
        + [size * 3 for size in native_sizes], default=0)
    image_bytes = max(min(sum(image_sizes) * 4, snapshot.images.decoded_budget), largest_workspace)
    return source_bytes + image_bytes + int(block_pixels) * 16 * 8
