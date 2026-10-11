"""Serial, bounded ownership for provisional finite-filter contact updates."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Event, get_ident
import copy
import math

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage

from comic_editor.render.admission import RENDER_ADMISSION, snapshot_working_bytes
from comic_editor.render.filtered_raster_feedback import (FeedbackUnavailable, FILTERED_PREPARED_BUDGET,
    update_native_feedback_recipe)
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.source_resources import ReadyOriginals


@dataclass(frozen=True)
class FilteredContactPacket:
    sequence: int
    revision: int
    values: tuple
    dirty: tuple
    interaction_rect: tuple

    @property
    def byte_count(self):
        return sum(image.sizeInBytes() for image in {image.cacheKey(): image
            for _key, _generation, image in self.values if image is not None}.values())

    def merged(self, newer):
        if newer.sequence <= self.sequence or newer.revision < self.revision:
            raise FeedbackUnavailable('Native source packet order changed')
        values = {key: (generation, image) for key, generation, image in self.values}
        values.update({key: (generation, image) for key, generation, image in newer.values})
        return replace(newer, values=tuple((key, *values[key]) for key in sorted(values)),
            dirty=tuple(QRectF(*self.dirty).united(QRectF(*newer.dirty)).getRect()))


def capture_filtered_contact_packet(tiles, identifier, generations, dirty, interaction_rect,
        sequence, revision, *, budget=32 * 1024 * 1024):
    """GUI capture reads only typed changed-key metadata and resident COW handles."""
    owner = tiles._tiles.get(identifier)
    coordinates = (*dirty.getRect(), *interaction_rect)
    if (owner is None or sequence <= 0 or revision < 0 or len(interaction_rect) != 4
            or not all(math.isfinite(value) for value in coordinates) or dirty.isEmpty()):
        raise FeedbackUnavailable('Native source packet lost its owner or finite influence')
    values = []
    for key, generation in sorted(generations.items()):
        if (len(key) != 2 or not all(type(value) is int for value in key)
                or not isinstance(generation, tuple) or len(generation) != 2
                or not all(type(value) is int for value in generation)):
            raise FeedbackUnavailable('Unowned native source generation')
        if key not in owner.entries:
            image = None
        else:
            entry = owner.residency.entries.get((owner, key))
            if entry is None:
                raise FeedbackUnavailable('Native source packet may not decode an evicted tile')
            image = QImage(entry[0])
            if (image.isNull() or image.width() != tiles.tile_size or image.height() != tiles.tile_size
                    or image.devicePixelRatio() != 1.):
                raise FeedbackUnavailable('Native source packet contract changed')
        values.append((tuple(key), tuple(generation), image))
    packet = FilteredContactPacket(sequence, revision, tuple(values), tuple(dirty.getRect()), tuple(interaction_rect))
    if not values or packet.byte_count > budget:
        raise FeedbackUnavailable('Native source packet cannot fit its handoff')
    return packet


@dataclass(frozen=True)
class FilteredContactCompletion:
    sequence: int
    revision: int
    updates: tuple


class FilteredFeedbackWorker:
    """One active packet and one merged latest packet, never a GUI wait."""
    def __init__(self, snapshot, identifier, recipes, *, notify=lambda: None,
            prepared_budget=FILTERED_PREPARED_BUDGET, packet_budget=32 * 1024 * 1024):
        self.gui_thread = get_ident()
        self.snapshot, self.identifier, self.recipes = snapshot, identifier, tuple(recipes)
        self.prepared_bytes = sum(recipe.byte_count for recipe in self.recipes)
        if (not self.recipes or any(recipe.identifier != identifier or recipe.document != snapshot.document
                for recipe in self.recipes) or self.prepared_bytes > prepared_budget):
            raise FeedbackUnavailable('Filtered feedback lost its prepared bounded basis')
        self.notify, self.packet_budget = notify, packet_budget
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='native-filtered-contact')
        self.stopped = Event()
        self.future = self.pending = None
        self.owner = self.backend = None
        self.progress = {}
        self.submitted_sequence = 0
        self.completed_sequence = 0
        self.error = ''
        self.worker_threads = set()
        # Fork the immutable source owner before the first contact. Publication
        # and cancellation remain asynchronous; the GUI only polls readiness.
        self.preparation = self.executor.submit(self._prepare)
        self.preparation.add_done_callback(self._finished)

    @property
    def busy(self):
        return self.preparation is not None or self.future is not None or self.pending is not None

    def submit(self, packet):
        if self.stopped.is_set() or packet.sequence <= self.submitted_sequence:
            return False
        pending = packet if self.pending is None else self.pending.merged(packet)
        if pending.byte_count > self.packet_budget:
            self.error = 'Merged native source packet exceeded its handoff budget'
            self.close()
            return False
        self.submitted_sequence = packet.sequence
        self.pending = pending
        self._start()
        return True

    def _start(self):
        if self.preparation is not None or self.future is not None or self.pending is None or self.stopped.is_set():
            return
        packet, self.pending = self.pending, None
        self.future = self.executor.submit(self._evaluate, packet)
        self.future.add_done_callback(self._finished)

    def _finished(self, _future):
        # add_done_callback executes on its caller if a very small preparation
        # has already finished. Keep publication wakes on the serial owner.
        if get_ident() == self.gui_thread:
            if not self.stopped.is_set():
                self.executor.submit(self._finished, _future)
            return
        try:
            self.notify()
        except RuntimeError:
            pass  # The owning Qt consumer may already have been retired.

    def _prepare(self):
        thread = get_ident()
        if thread == self.gui_thread:
            raise FeedbackUnavailable('Filtered preparation may never use the document thread')
        self.worker_threads.add(thread)
        estimate = snapshot_working_bytes(self.snapshot) + self.prepared_bytes
        with RENDER_ADMISSION.reserve('native-filtered-prepare', estimate, priority=0,
                cancelled=self.stopped.is_set):
            if self.stopped.is_set():
                raise FeedbackUnavailable('Filtered source owner retired before preparation')
            self._fork_owner()

    def _fork_owner(self):
        original = self.snapshot.finish_sources()
        chapter = copy.copy(original.chapter)
        chapter.objects = dict(chapter.objects)
        chapter.objects[self.identifier] = copy.deepcopy(chapter.objects[self.identifier])
        # The scene capture already owns revision-reader jobs for cold paths.
        # Adopt their pins before the normal detached copy: otherwise that
        # copy hard-links every unvisited file again at the first contact.
        # This remains on the admitted worker, including any unfinished pin.
        for source in original.tiles._tiles.values():
            for key in source:
                original.tiles.residency.prepare(source, key)
        tiles = original.tiles.detached_snapshot(set(original.tiles._tiles))
        images = original.images.clone()
        ready = ReadyOriginals()
        ready.capture(original)
        self.owner = replace(original, chapter=chapter, tiles=tiles, images=images,
            document=replace(original.document, live_preview=True, contact_only=True),
            cache_spec=None, graphics_worker=None)
        # Consume unchanged, pinned native buffers into the fork's own LRUs.
        # No source decode or independent lifetime crosses the document thread.
        ready.adopt(self.owner)
        self.backend = DetachedSceneBackend(self.owner)

    def _evaluate(self, packet):
        thread = get_ident()
        if thread == self.gui_thread:
            raise FeedbackUnavailable('Filtered evaluation may never use the document thread')
        self.worker_threads.add(thread)
        estimate = snapshot_working_bytes(self.snapshot) + self.prepared_bytes
        with RENDER_ADMISSION.reserve('native-filtered-contact', estimate, priority=0,
                cancelled=self.stopped.is_set):
            if self.owner is None:
                self._fork_owner()
            if self.stopped.is_set():
                raise FeedbackUnavailable('Filtered contact owner retired')
            owner = self.owner.tiles._tiles[self.identifier]
            for key, generation, image in packet.values:
                if image is None:
                    if key in owner.entries:
                        del owner[key]
                else:
                    owner[key] = QImage(image)
                owner.versions[key], owner.content_keys[key] = generation
            self.owner.chapter.objects[self.identifier].interaction_rect = packet.interaction_rect
            self.owner = replace(self.owner, document=replace(self.owner.document, revision=packet.revision))
            updates = []
            for index, recipe in enumerate(self.recipes):
                if not recipe.request.bounds.intersects(QRectF(*packet.dirty)):
                    continue
                previous = self.progress.get(index)
                update = update_native_feedback_recipe(recipe, self.owner, QRectF(*packet.dirty),
                    previous_source=None if previous is None else previous.source,
                    backend=self.backend, cancelled=self.stopped.is_set)
                self.progress[index] = update
                updates.append((index, update))
            return FilteredContactCompletion(packet.sequence, packet.revision, tuple(updates))

    def poll(self, *, start_pending=True):
        completion = None
        if self.preparation is not None and self.preparation.done():
            future, self.preparation = self.preparation, None
            try:
                future.result()
            except Exception as error:
                self.error = f'{type(error).__name__}: {error}'
                self.close()
                return None
            if start_pending:
                self._start()
        if self.future is not None and self.future.done():
            future, self.future = self.future, None
            try:
                result = future.result()
                if not self.stopped.is_set() and result.sequence > self.completed_sequence:
                    self.completed_sequence = result.sequence
                    completion = result
            except Exception as error:
                self.error = f'{type(error).__name__}: {error}'
                self.close()
            if start_pending:
                self._start()
        return completion

    def close(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        self.pending = None
        # Cleanup stays on the context/source owner after any active packet.
        self.executor.submit(self._retire_owner)
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _retire_owner(self):
        if self.backend is not None:
            self.backend.close()
        self.backend = self.owner = None
        self.progress.clear()
        self.snapshot = None
        self.recipes = ()
        self.preparation = None
