"""Optional real-work diagnostic for the private Pocket contact harness.

This helper never starts fake work, holds a ticket open, sleeps in a renderer,
or changes an admission policy. Install after fixture/witness setup, queue the
ordinary modifier transaction, and let the harness's existing Qt pump run.
Construct the press event before ``dispatch_if_current``; that method performs
the last positive busy-work guard and immediately calls the supplied dispatch.
Save ``report()`` only after contacts finish. Undo native contact commands before
``undo_modifier_operation``. No artwork files are written by this helper.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
from threading import Event, RLock, get_ident, local
import time
from types import MethodType

from PySide6.QtCore import QObject, Qt, Signal

from comic_editor.core.models import BlurModifier, RasterObject
from comic_editor.core.tools import ToolKind
from comic_editor.render.admission import RENDER_ADMISSION, current_ticket
from comic_editor.render.input_sources import InputSourceCapture, InputSourceSnapshot
from comic_editor.render.scene_kernels import SceneKernels
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui import scene_consumers
from comic_editor.ui.tile_input import prepare_input_tiles


RASTER63 = '0d80589c01fb430786da5a51cb0eb6b4'
BLUR63 = '563887446d3a4af795def9021f967368'
ORIGINAL = '06be89900eb540ee83a26514822d32ec'
_ABSENT = object()


@dataclass(frozen=True)
class _NativeJob:
    snapshot: object
    arguments: tuple
    cancelled: Event
    stopped: Event
    gate_id: int
    source_id: str


@dataclass(frozen=True)
class _BusyBlock:
    demand: object
    token: Event
    ticket: object
    finished: Event
    entered_at: float


@dataclass(frozen=True)
class PostMutationBasis:
    """Private immutable ordinary basis, captured before worker pumping.

    Decode ``model_json`` with the ordinary chapter loader for a detached
    witness. No live model container is shared with the caller or a worker.
    """
    model_json: str
    document: object
    camera: tuple
    dpr: float
    history_revision: int
    original_owner_id: int
    original_versions: tuple


class BusySceneUnavailable(RuntimeError):
    """The positive real-work conditions did not hold for this trial."""


class PocketBusySceneSupport(QObject):
    blockReady = Signal()
    MAX_ROWS = 1024

    def __init__(self, canvas, *, modifier_controls=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.scheduler = canvas._scene_controller.scheduler
        self.chapter, self.tiles, self.images = canvas.chapter, canvas.tiles, canvas.images
        self.original = self.chapter.objects[ORIGINAL]
        self.gui_thread = get_ident()
        self.origin = time.perf_counter()
        self.controls = modifier_controls or ModifierControls(canvas)
        if self.controls.canvas is not canvas:
            raise ValueError('Modifier controls must own this exact canvas')
        self._owns_controls = modifier_controls is None
        self._lock, self._local = RLock(), local()
        self._rows, self._patches, self._native_jobs, self._blocks = [], [], {}, {}
        self._dropped = 0
        self._closed = self._queued = self._press_started = False
        self._notification_seen = False
        self._block = None
        self._first_admitted = False
        self._expected_revision = None
        self._modifier_command = None
        self._baseline_model = None
        self._history_before = None
        self._basis = None
        self._original_owner = None
        self._original_versions = ()
        self.blockReady.connect(self._ready, Qt.ConnectionType.QueuedConnection)
        self._install()

    def _record(self, name, **details):
        row = {'name': name, 'at_ms': (time.perf_counter() - self.origin) * 1000,
               'thread_id': get_ident(), **details}
        with self._lock:
            if len(self._rows) < self.MAX_ROWS:
                self._rows.append(row)
            else:
                self._dropped += 1

    def _gui_current(self):
        return (get_ident() == self.gui_thread and not self._closed
                and self.canvas.chapter is self.chapter and self.canvas.tiles is self.tiles
                and self.canvas.images is self.images
                and self.chapter.objects.get(ORIGINAL) is self.original)

    def _require_gui(self):
        if not self._gui_current():
            raise BusySceneUnavailable('The diagnostic canvas/source owner retired')

    @staticmethod
    def _ticket_fields(ticket):
        return {'ticket_id': id(ticket), 'ticket_owner': ticket.owner,
                'estimated_bytes': ticket.estimated_bytes, 'priority': ticket.priority,
                'oversized': ticket.oversized}

    def _patch(self, owner, attribute, replacement, *, bind=False):
        previous_slot = vars(owner).get(attribute, _ABSENT)
        previous = getattr(owner, attribute)
        installed = MethodType(replacement, owner) if bind else replacement
        setattr(owner, attribute, installed)
        self._patches.append((owner, attribute, previous_slot, installed))
        return previous

    def _owned_demand(self, demand):
        # Worker checks inspect only this immutable demand/snapshot and frozen
        # Python owner identities; no canvas/widget reads occur here.
        document = demand.snapshot.document
        modifier = demand.snapshot.chapter.modifiers.get(BLUR63)
        return (self._queued and not self._closed and demand.consumer == 'canvas'
                and not demand.record and not document.live_preview
                and demand.contact_preview_owner is None and not demand.feedback_only
                and document.identity == (id(self.chapter), id(self.tiles), id(self.images))
                and document.revision == self._expected_revision
                and self._basis is not None and document.configuration == self._basis.document.configuration
                and isinstance(modifier, BlurModifier) and modifier.strength == 28.)

    def _install(self):
        original_evaluate = self.scheduler._evaluate_admitted
        def evaluate(scheduler, demand, token):
            if not self._owned_demand(demand):
                return original_evaluate(demand, token)
            previous = getattr(self._local, 'scene', None)
            self._local.scene = (demand, token)
            self._record('ordinary_compute_enter', serial=demand.serial,
                         revision=demand.snapshot.document.revision)
            try:
                return original_evaluate(demand, token)
            finally:
                self._record('ordinary_compute_exit', serial=demand.serial,
                             cancelled=token.is_set())
                self._local.scene = previous
        self._patch(self.scheduler, '_evaluate_admitted', evaluate, bind=True)

        original_modified = SceneKernels._render_modified_object
        def modified(scene, painter, obj, parent_opacity, local_visible):
            scope = getattr(self._local, 'scene', None)
            ticket = current_ticket()
            if (scope is None or self._closed or obj.object_id != RASTER63
                    or scene.chapter is not scope[0].snapshot.chapter or ticket is None
                    or ticket.owner != 'canvas' or not self._owned_demand(scope[0])):
                return original_modified(scene, painter, obj, parent_opacity, local_visible)
            block = _BusyBlock(scope[0], scope[1], ticket, Event(), time.perf_counter())
            with self._lock:
                # One first block owns the trial. Later native blocks remain
                # ordinary work and cannot silently replace this notification.
                first = self._block is None
                if first:
                    self._block = block
                    self._blocks[id(ticket)] = block
            if not first:
                return original_modified(scene, painter, obj, parent_opacity, local_visible)
            self._record('real_modified_object_enter', source_id=RASTER63,
                         serial=block.demand.serial, **self._ticket_fields(ticket))
            try:
                self.blockReady.emit()
            except RuntimeError:
                # A closed private window must not turn diagnostics into a
                # renderer failure while its genuine block is unwinding.
                pass
            try:
                return original_modified(scene, painter, obj, parent_opacity, local_visible)
            finally:
                block.finished.set()
                self._record('real_modified_object_exit', source_id=RASTER63,
                             serial=block.demand.serial, cancelled=block.token.is_set())
        self._patch(SceneKernels, '_render_modified_object', modified)

        original_submit = scene_consumers.SceneConsumers._submit
        def submit(jobs, job, snapshot):
            if (get_ident() == self.gui_thread and self._press_started and not self._closed
                    and jobs.canvas is self.canvas and jobs.active is job
                    and job.compute is prepare_input_tiles and job.owned_source and job.ordered
                    and type(snapshot) is InputSourceSnapshot and len(job.arguments) == 2
                    and job.arguments[0] == ORIGINAL and job.valid is not None
                    and self._gui_current()):
                gate = next((gate for gate in self.canvas._native_input_gates()
                             if gate.jobs is jobs and gate.identifier == ORIGINAL
                             and job.lane == ('native-input', id(gate))
                             and gate.pending and gate.current()), None)
                if gate is not None:
                    native = _NativeJob(snapshot, job.captured_arguments or job.arguments,
                                        job.cancelled, jobs.stopped, id(gate), ORIGINAL)
                    with self._lock:
                        self._native_jobs[id(job.cancelled)] = native
                    self._record('native_source_worker_submit', gate_id=id(gate),
                                 source_id=ORIGINAL, source_keys=len(job.arguments[1]))
            return original_submit(jobs, job, snapshot)
        self._patch(scene_consumers.SceneConsumers, '_submit', submit)

        original_compute = scene_consumers.compute_snapshot
        def compute(function, snapshot, arguments, cancelled, stopped, **options):
            with self._lock:
                native = self._native_jobs.get(id(cancelled))
            if (native is None or native.snapshot is not snapshot
                    or native.arguments != arguments or native.cancelled is not cancelled
                    or native.stopped is not stopped or function is not prepare_input_tiles
                    or getattr(function, 'source_capture', None) is not InputSourceCapture
                    or self._closed):
                return original_compute(function, snapshot, arguments, cancelled, stopped, **options)
            previous = getattr(self._local, 'native', None)
            self._local.native = native
            self._record('native_worker_enter', gate_id=native.gate_id, source_id=native.source_id)
            try:
                return original_compute(function, snapshot, arguments, cancelled, stopped, **options)
            finally:
                self._record('native_worker_exit', gate_id=native.gate_id,
                             source_id=native.source_id, cancelled=cancelled.is_set())
                self._local.native = previous
                with self._lock:
                    if self._native_jobs.get(id(cancelled)) is native:
                        self._native_jobs.pop(id(cancelled), None)
        self._patch(scene_consumers, 'compute_snapshot', compute)

        original_reserve = RENDER_ADMISSION.reserve
        @contextmanager
        def reserve(admission, owner, size, **options):
            inherited = current_ticket()
            native = getattr(self._local, 'native', None)
            observed = (not self._closed and native is not None and inherited is None
                        and owner == 'source-sampling' and options.get('priority') == 0)
            if observed:
                self._record('native_reservation_requested', gate_id=native.gate_id,
                             source_id=native.source_id, estimated_bytes=int(size))
            ticket = None
            try:
                with original_reserve(owner, size, **options) as ticket:
                    if observed:
                        self._record('native_source_prepare_admitted', gate_id=native.gate_id,
                                     source_id=native.source_id, **self._ticket_fields(ticket))
                    try:
                        yield ticket
                    finally:
                        if inherited is None:
                            with self._lock:
                                block = self._blocks.get(id(ticket))
                            if block is not None:
                                self._record('old_scene_ticket_unwind_started', serial=block.demand.serial,
                                             **self._ticket_fields(ticket))
            finally:
                # The original context has already released a newly acquired
                # ticket. A nested graphics borrower is never called a release.
                if inherited is None and ticket is not None:
                    with self._lock:
                        block = self._blocks.get(id(ticket))
                    if block is not None:
                        self._record('old_scene_ticket_released', serial=block.demand.serial,
                                     **self._ticket_fields(ticket))
                    if observed:
                        self._record('native_source_ticket_released', gate_id=native.gate_id,
                                     source_id=native.source_id, **self._ticket_fields(ticket))
        self._patch(RENDER_ADMISSION, 'reserve', reserve, bind=True)

        original_cancel = self.scheduler.cancel
        def cancel(scheduler):
            block = self._block
            if self._press_started and block is not None and scheduler.active_demand is block.demand:
                self._record('pending_contact_cancel_requested', serial=block.demand.serial,
                             ticket_id=id(block.ticket))
            return original_cancel()
        self._patch(self.scheduler, 'cancel', cancel, bind=True)

        original_begin = self.canvas._begin_stroke
        def begin(canvas, *args, **kwargs):
            if self._press_started and not self._closed:
                block = self._block
                with RENDER_ADMISSION.condition:
                    running = block is not None and any(ticket is block.ticket
                              for ticket in RENDER_ADMISSION.running.values())
                self._record('native_press_received', old_ticket_still_running=running,
                             real_block_unfinished=block is not None and not block.finished.is_set(),
                             old_job_cancelled=block is not None and block.token.is_set(),
                             same_demand=block is not None and self.scheduler.active_demand is block.demand,
                             same_source_owner=self._gui_current()
                                 and self.tiles._tiles.get(ORIGINAL) is self._original_owner,
                             same_selection=canvas.selected_kind == 'object'
                                 and canvas.selected_id == ORIGINAL)
            return original_begin(*args, **kwargs)
        self._patch(self.canvas, '_begin_stroke', begin, bind=True)

        original_admitted = self.canvas._begin_stroke_impl
        def admitted(canvas, *args, **kwargs):
            gate = getattr(canvas, '_raster_tile_input', None)
            if (self._press_started and not self._first_admitted and self._gui_current()
                    and gate is not None and gate.canvas is canvas and gate.chapter is self.chapter
                    and gate.tiles is self.tiles and gate.identifier == ORIGINAL and gate.current()
                    and canvas.selected_kind == 'object' and canvas.selected_id == ORIGINAL):
                self._first_admitted = True
                self._record('first_native_begin_impl', gate_id=id(gate), source_id=ORIGINAL)
            return original_admitted(*args, **kwargs)
        self._patch(self.canvas, '_begin_stroke_impl', admitted, bind=True)

    def queue_modifier_change(self):
        self._require_gui()
        if self._queued or self.canvas._drawing or self.canvas._native_input_predecessor() is not None:
            raise BusySceneUnavailable('The modifier operation requires an idle native input owner')
        raster = self.chapter.objects.get(RASTER63)
        modifier = self.chapter.modifiers.get(BLUR63)
        if (not isinstance(raster, RasterObject) or not raster.visible or BLUR63 not in raster.modifier_ids
                or not isinstance(modifier, BlurModifier) or modifier.muted or modifier.strength != 27.):
            raise BusySceneUnavailable('The expected authored Raster63/Blur27 fixture differs')
        self._baseline_model = json.dumps(self.chapter.to_dict(), ensure_ascii=False)
        self._history_before = self.canvas.command_stack.revision
        self._record('modifier_operation_begin', modifier_id=BLUR63, before_strength=27.)
        self.canvas.set_selection('object', RASTER63)
        self.controls.set_parameter(BLUR63, 'strength', 28., True)
        self.canvas.set_selection('object', ORIGINAL)
        self.canvas.set_tool(ToolKind.RASTER_PENCIL)
        self._modifier_command = self.canvas.command_stack.top_undo_command
        if self.canvas.command_stack.revision != self._history_before + 1:
            raise BusySceneUnavailable('The ordinary modifier operation did not create exactly one command')
        self._expected_revision = self.canvas._document_projection.revision
        self._original_owner = self.tiles._tiles.get(ORIGINAL)
        if self._original_owner is None:
            raise BusySceneUnavailable('The original native source owner is absent')
        self._original_versions = tuple((key, self._original_owner.version(key))
                                        for key in self._original_owner)
        self._basis = PostMutationBasis(
            json.dumps(self.chapter.to_dict(), ensure_ascii=False),
            self.canvas._render_document_state(),
            (self.canvas.center_x, self.canvas.center_y, self.canvas.scale, self.canvas.rotation),
            self.canvas.devicePixelRatioF(),
            self.canvas.command_stack.revision, id(self._original_owner), self._original_versions)
        self._queued = True
        self._record('modifier_operation_committed', modifier_id=BLUR63, after_strength=28.,
                     history_revision=self.canvas.command_stack.revision,
                     document_revision=self._expected_revision)
        self.canvas.update()

    def post_mutation_basis(self):
        self._require_gui()
        if self._basis is None or not self._queued or self._press_started:
            raise BusySceneUnavailable('Capture the ordinary mutation basis before dispatching contact')
        return self._basis

    def _ready(self):
        if self._gui_current():
            self._notification_seen = True
            self._record('real_block_notification_received')

    @property
    def ready(self):
        self._require_gui()
        return self._notification_seen

    def make_original_sources_cold(self):
        """Use the same clean native LRU eviction as the existing cold control."""
        self._require_gui()
        owner = self.tiles._tiles.get(ORIGINAL)
        if (owner is not self._original_owner
                or tuple((key, owner.version(key)) for key in owner) != self._original_versions
                or any(not isinstance(owner.entries[key], Path) for key in owner)):
            raise BusySceneUnavailable('Cold control requires unchanged disk-backed originals')
        for key in tuple(owner):
            self.tiles.residency.evict(owner, key)

    def dispatch_if_current(self, dispatch):
        self._require_gui()
        block = self._block
        canvas, scheduler = self.canvas, self.scheduler
        owner = self.tiles._tiles.get(ORIGINAL)
        if (not self._notification_seen or block is None or block.finished.is_set()
                or block.token.is_set() or scheduler.active_demand is not block.demand
                or scheduler.future is None or scheduler.future.done()
                or not self._owned_demand(block.demand)
                or canvas._document_projection.revision != self._expected_revision
                or canvas._render_document_state() != self._basis.document
                or (canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation) != self._basis.camera
                or canvas.devicePixelRatioF() != self._basis.dpr
                or canvas.selected_kind != 'object' or canvas.selected_id != ORIGINAL
                or canvas.selected_entities != [('object', ORIGINAL)]
                or canvas.tool != ToolKind.RASTER_PENCIL or canvas._drawing
                or canvas._native_input_predecessor() is not None
                or canvas.command_stack.top_undo_command is not self._modifier_command
                or owner is not self._original_owner
                or tuple((key, owner.version(key)) for key in owner) != self._original_versions
                or any(not isinstance(owner.entries[key], Path) for key in owner)
                or any((owner, key) in self.tiles.residency.entries for key in owner)):
            raise BusySceneUnavailable('The real ordinary block/source/selection guard expired before press')
        with RENDER_ADMISSION.condition:
            running = any(ticket is block.ticket for ticket in RENDER_ADMISSION.running.values())
        if not running:
            raise BusySceneUnavailable('The original admitted ticket already released before press')
        self._press_started = True
        self._record('busy_guard_before_press', serial=block.demand.serial,
                     revision=block.demand.snapshot.document.revision, **self._ticket_fields(block.ticket))
        # Nothing is pumped, hashed, decoded, allocated as artwork, or written
        # between the positive guard and the existing real event dispatch.
        return dispatch()

    def undo_modifier_operation(self):
        self._require_gui()
        if self.canvas._native_input_predecessor() is not None or self.canvas._drawing:
            raise BusySceneUnavailable('Native release must finish before operation undo')
        if self.canvas.command_stack.top_undo_command is not self._modifier_command:
            raise BusySceneUnavailable('Undo native contacts first; the modifier command is still below them')
        self.canvas.command_stack.undo()
        if self.chapter.to_dict() != json.loads(self._baseline_model):
            raise AssertionError('Ordinary modifier undo did not restore the exact pre-operation model')
        self._record('modifier_operation_undone', model_exact=True,
                     history_revision=self.canvas.command_stack.revision)

    def report(self):
        with self._lock:
            rows = [dict(row) for row in self._rows]
            dropped = self._dropped
        receipt = next((row for row in rows if row['name'] == 'native_press_received'), None)
        valid = receipt is not None and all(receipt[field] for field in (
            'old_ticket_still_running', 'real_block_unfinished', 'same_demand',
            'same_source_owner', 'same_selection'))
        return {'rows': rows, 'dropped_rows': dropped, 'max_rows': self.MAX_ROWS,
                'actual_press_busy_guard_valid': valid,
                'operation': {'source_id': RASTER63, 'modifier_id': BLUR63,
                    'attribute': 'strength', 'before': 27., 'after': 28.,
                    'native_control': ORIGINAL, 'modifier_history_commands': 1},
                'limitations': ['Diagnostic hooks are inclusive and add bounded Python metadata work.',
                    'No renderer/admission wait, fake computation or held ticket is introduced.',
                    'Actual ticket release is bracketed by unwind-start and released rows; the latter is an upper bound.',
                    'A block that finishes between guard and actual press receipt invalidates the busy trial.',
                    'The real source/control contract and fresh-pixel witness remain the caller\'s responsibility.',
                    'The helper does not hash/write source files or claim first-visible-pixel latency.']}

    def close(self):
        if get_ident() != self.gui_thread:
            raise RuntimeError('Close diagnostic hooks on their installing thread')
        self._closed = True
        for owner, attribute, previous_slot, installed in reversed(self._patches):
            if getattr(owner, attribute) is not installed:
                continue  # Never remove a later independent diagnostic wrapper.
            if previous_slot is _ABSENT:
                delattr(owner, attribute)
            else:
                setattr(owner, attribute, previous_slot)
        self._patches.clear()
        if self._owns_controls:
            self.controls.deleteLater()
