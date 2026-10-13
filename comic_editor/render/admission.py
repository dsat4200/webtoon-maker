"""Shared admission for memory-intensive detached rendering consumers.

Tickets are acquired off the GUI. Nested GPU/kernel work borrows its caller's
ticket, preventing a scene waiting for its own reservation. Retained pixel and
texture caches retain their own byte LRUs; a ticket bounds concurrent work.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
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
    native_source: bool = False
    cancelled: object = field(default=lambda: False, compare=False, repr=False)


# One positively owned input source quantum may coexist with an ordinary
# exclusive workspace. This is additional working storage, never a source
# sampling exception, and is included in the aggregate accounting below.
NATIVE_SOURCE_WORKSPACE = 2 * 1024 * 1024


_active_ticket = ContextVar("native_render_ticket", default=None)


def current_ticket():
    return _active_ticket.get()


def check_scene_cancelled(scene):
    """Stop a detached capture before more source/scene work is started."""
    cancelled = vars(scene).get('_scene_cancelled')
    if cancelled is not None and cancelled():
        raise WorkCancelled()


class CopyLease:
    def __init__(self, admission, marker, size, scope=None):
        self.admission,self.marker,self.size = admission,marker,size
        self.scope = scope
        self.released = False

    def release(self):
        with self.admission.condition:
            if not self.released:
                self.released = True
                self.admission.copies.pop(self.marker,None)
                self.admission.copy_scopes.pop(self.marker,None)
                self.admission.condition.notify_all()


class WorkAdmission:
    def __init__(self, budget=256 * 1024 * 1024):
        self.budget = max(1, int(budget))
        self.condition = Condition()
        self.sequence = count()
        self.waiters = {}
        self.running = {}
        self.copies = {}
        self.copy_scopes = {}
        self.copy_credits = {}
        self.owned_copy_markers = {}
        self.peak_bytes = 0
        self.admitted = 0

    @contextmanager
    def reserve(self, owner, size, *, priority=0, cancelled=lambda: False, copies=None,
                native_source=False, own_copies=False):
        size = max(0, int(size))
        if own_copies and (native_source or not isinstance(copies, CopyLease)
                or copies.admission is not self or copies.released):
            raise ValueError('An owned capture requires its live admission copy lease')
        if native_source and (priority != 0 or not 0 < size <= NATIVE_SOURCE_WORKSPACE
                              or copies is not None):
            raise ValueError('Owned native source work requires a bounded independent workspace')
        existing = current_ticket()
        if existing is not None:
            if existing.native_source and size > existing.estimated_bytes:
                raise ValueError('Nested work exceeds its owned native source workspace')
            if native_source:
                with self.condition:
                    owned = existing.native_source and any(
                        ticket is existing for ticket in self.running.values())
                if not owned:
                    raise ValueError('Owned native source work cannot borrow a foreign ticket')
            yield existing
            return
        marker = next(self.sequence)
        started = time.monotonic()
        with self.condition:
            self.waiters[marker] = priority, started, bool(native_source)
            try:
                while True:
                    if cancelled():
                        raise WorkCancelled()
                    # Aging prevents thumbnail/export starvation during long
                    # drawing sessions while visible work normally wins.
                    now = time.monotonic()
                    # The one bounded source lane does not wait behind an
                    # ineligible ordinary reservation, nor does a waiting
                    # second source quantum block eligible ordinary work.
                    lane = [key for key,value in self.waiters.items()
                            if value[2] == bool(native_source)]
                    first = min(lane, key=lambda key:
                        (self.waiters[key][0] - int((now - self.waiters[key][1]) / 2), key))
                    occupied = self._occupied()
                    credit = (min(size,copies.size) if copies is not None and not copies.released else 0)
                    heavy = size >= 16 * 1024 * 1024
                    ordinary = [ticket for ticket in self.running.values() if not ticket.native_source]
                    source_bytes = sum(ticket.estimated_bytes for ticket in self.running.values()
                                       if ticket.native_source)
                    if native_source:
                        eligible = source_bytes == 0
                    elif size > self.budget:
                        eligible = not ordinary
                    else:
                        eligible = (len(ordinary) < 4 and
                            occupied-source_bytes + size-credit <= self.budget and
                            (not heavy or not any(ticket.estimated_bytes >= 16 * 1024 * 1024
                                                 for ticket in ordinary)))
                    if eligible and marker == first:
                        ticket = WorkTicket(owner, size, priority, size > self.budget,
                                            bool(native_source), cancelled)
                        self.running[marker] = ticket
                        self.copy_credits[marker] = credit
                        if own_copies:
                            self.owned_copy_markers[marker] = copies.marker
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
                self.owned_copy_markers.pop(marker,None)
                self.condition.notify_all()

    def _occupied(self):
        return (sum(ticket.estimated_bytes for ticket in self.running.values())
            +sum(size for _,size in self.copies.values())-sum(self.copy_credits.values()))

    @property
    def copied_bytes(self):
        with self.condition:
            return sum(size for _,size in self.copies.values())

    def higher_priority_waiting(self, priority):
        """A provisional consumer may abandon its capture for visible work.

        This does not release a live workspace or alter byte reservations.
        Its owner must unwind normally before another ticket is admitted.
        """
        with self.condition:
            return any(value[0] < priority for value in self.waiters.values())

    def reserve_copies(self, size, *, scope=None):
        """Nonblocking ownership of queued arrays and output buffers.

        A sole admitted parent may borrow one oversized workspace. Unadmitted
        GUI submissions decline immediately; no allocation or wait happens here.
        """
        size = max(0,int(size))
        parent = current_ticket()
        with self.condition:
            occupied = self._occupied()
            ordinary = [ticket for ticket in self.running.values() if not ticket.native_source]
            parent_marker = next((marker for marker, ticket in self.running.items()
                                  if ticket is parent), None)
            owned_copy = self.owned_copy_markers.get(parent_marker)
            owned_scope = self.copy_scopes.get(owned_copy)
            # A bounded scene capture was queued on the GUI, before this
            # worker's ticket existed. Its positively owned credited lease is
            # part of this sole parent's workspace. Treating it as foreign
            # would make a nested graphics copy wait for its own caller.
            exclusive = (parent is not None and not parent.native_source and len(ordinary) == 1
                and ordinary[0] is parent
                and all(size == 0 or ticket is parent or marker == owned_copy
                        or owned_scope is not None and self.copy_scopes.get(marker) is owned_scope
                        for marker, (ticket, size) in self.copies.items()))
            if size and occupied+size > self.budget and not exclusive:
                return None
            marker = next(self.sequence)
            self.copies[marker] = parent,size
            if scope is not None:
                self.copy_scopes[marker] = scope
            self.peak_bytes = max(self.peak_bytes,occupied+size)
            return CopyLease(self,marker,size,scope)


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
