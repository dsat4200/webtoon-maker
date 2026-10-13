"""Actual Pocket Boyfriend native GPU drawing under retained texture pressure.

Run with the application's Python in a fresh Windows process. The project must
be a private copy below this checkout's .artifacts; no saves or user settings are
written. Native Qt tablet packets run at 120 Hz, with seven consecutive held
contacts for each tool, a rotated camera, and an ordinary GpuTilePresenter near
its 256 MiB limit. The pressure fixture aliases already completed native 260px
scene pixels under distinct *presentation-only* keys: it models the captured
991-texture residency, without fabricating artwork/exact-cache entries or
changing original source/effect sampling.

Frame timestamps precede optional GL readback. Dispatch-to-swap is application
latency, excluding driver/scanout; it does not by itself prove changed ink.
Separate bounded pixel witnesses compare the actual first admitted native dab
with the unchanged ordinary renderer after timed contacts. Full-size witnesses
are reported separately as convergence checks, not first-pressure ink latency.
The same wrappers/readback must be used for baseline and candidate comparisons.
Final native source buffers, complete undo, file hashes, and native framebuffer
images are retained. --compare-to compares all selected source bytes and native
framebuffer bytes with a prior run. A fresh process is required for each run.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
import ctypes
from dataclasses import replace
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import traceback


ROOT = Path(__file__).resolve().parents[1]
CHAPTER = '0a72f08009294aa0a3d14e6a38e22bbb'
SELECTED = '06be89900eb540ee83a26514822d32ec'
ANCESTORS = ('842203117b69437fad52bf77b25cde5b', 'ceeb7336f3ea40509b016cb71fac5f0c')
CAMERA = (849.6412686948373, 51393.78584631346, .6308544791184257, -.663008407605119)
COLORS = ('#ffff0000', '#ff00bb00', '#ff0066ff', '#ffff6600', '#ff8800ff', '#ff00bbbb', '#ffff00aa')


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, default=ROOT)
    parser.add_argument('--chapter', default=CHAPTER, help='Saved document ID, including an asset inner-document ID')
    parser.add_argument('--asset', help='Asset ID for a capture opened from the asset library')
    parser.add_argument('--selected', default=SELECTED)
    parser.add_argument('--camera', type=float, nargs=4, default=CAMERA,
                        metavar=('CENTER_X', 'CENTER_Y', 'SCALE', 'ROTATION_DEGREES'))
    parser.add_argument('--expected-counts', type=int, nargs=4, default=(106, 173, 103, 30),
                        metavar=('LAYERS', 'OBJECTS', 'EFFECTS', 'MASKS'))
    parser.add_argument('--expected-size', type=int, nargs=2, default=(1080, 82000), metavar=('WIDTH', 'HEIGHT'))
    parser.add_argument('--expected-source-tiles', type=int, help='Assert initial selected native source tile cardinality')
    parser.add_argument('--project-copy', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True, help='New result directory below .artifacts')
    parser.add_argument('--copy-from', type=Path, help='Create the private fixture before the run')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--compare-to', type=Path, help='Prior result directory')
    parser.add_argument('--contacts', type=int, default=7)
    parser.add_argument('--seconds', type=float, default=3.)
    parser.add_argument('--hz', type=int, default=120)
    parser.add_argument('--gap', type=float, default=.015)
    parser.add_argument('--pressure-tiles', type=int, default=991)
    parser.add_argument('--pencil-size', type=int, default=12)
    parser.add_argument('--eraser-size', type=int, default=28, help='Use44for captured active-large stress variant')
    parser.add_argument('--settle-seconds', type=float, default=60.)
    parser.add_argument('--tools', choices=('both', 'pencil', 'eraser'), default='both')
    parser.add_argument('--no-pixel-observer', action='store_true', help='Swap observer only; use identically on both runs')
    parser.add_argument('--cold-source-first', action='store_true',
        help='Evict selected clean native source buffers immediately before first press, leaving the ordinary scene ready')
    parser.add_argument('--busy-pressure-first', action='store_true',
        help='Diagnostic: submit fresh presentation pressure immediately before first press without a completion barrier')
    parser.add_argument('--busy-known-scene-first', action='store_true',
        help='Append offscreen pressure to one ordinary complete idle scene paint, then press without pumping or readback')
    parser.add_argument('--busy-scene-first', action='store_true',
        help='Optional real Blur27-to28 operation; cold original native press must arrive while its ordinary scene block and ticket are still running')
    parser.add_argument('--pixel-witness-no-cursor', action='store_true',
        help='Correctness observer only: suppress hover for initial, held, release, stage, final and undo frames; restore only at cleanup')
    args = parser.parse_args()
    if args.busy_known_scene_first:
        args.busy_pressure_first = True
    if args.pixel_witness_no_cursor and args.no_pixel_observer:
        parser.error('Cursor-free pixel witness requires the separate pixel observer')
    if args.busy_scene_first:
        if (args.selected != SELECTED or args.tools == 'eraser' or args.pressure_tiles
                or args.busy_pressure_first or args.busy_known_scene_first):
            parser.error('Real busy-scene trial requires the original raster, first tool pencil and pressure-tiles0 without synthetic busy pressure')
        args.cold_source_first = True
    if not (1 <= args.contacts <= 7 and .1 <= args.seconds <= 10 and 1 <= args.hz <= 240
            and 0 <= args.gap <= 2 and 0 <= args.pressure_tiles <= 991
            and 1 <= args.pencil_size <= 64 and 1 <= args.eraser_size <= 64):
        parser.error('Invalid workload bounds')
    args.project_copy = args.project_copy.resolve()
    args.out = args.out.resolve()
    args.runtime_root = args.runtime_root.resolve()
    for path in (args.project_copy, args.out):
        if not path.is_relative_to((ROOT / '.artifacts').resolve()) or path == ROOT / '.artifacts':
            parser.error('Copies and results must be private .artifacts descendants')
    return args


def file_manifest(project):
    return {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(project.rglob('*')) if p.is_file()}


def prepare(args):
    storage_kind, storage_id = ('assets', args.asset) if args.asset else ('chapters', args.chapter)
    manifest_name = 'asset.json' if args.asset else 'chapter.json'
    if args.copy_from:
        assert not args.project_copy.exists(), 'Use a new private copy destination'
        args.project_copy.mkdir(parents=True)
        shutil.copy2(args.copy_from / 'series.json', args.project_copy / 'series.json')
        # Recovery sources are independent. Keep the shared repository's normal
        # recover=True selection; do not merge or rewrite a manifest ourselves.
        shutil.copytree(args.copy_from / storage_kind / storage_id,
                        args.project_copy / storage_kind / storage_id,
                        ignore=shutil.ignore_patterns('.render-cache'))
    assert (args.project_copy / 'series.json').is_file()
    assert not any(p.name == '.render-cache' for p in args.project_copy.rglob('*'))
    manual_root = args.project_copy / storage_kind / storage_id
    manual = manual_root / ('last_good' if (manual_root / '.save_pending').is_file() else '') / manifest_name
    recovery_root = manual_root / 'autosave'
    recovery = recovery_root / ('last_good' if (recovery_root / '.save_pending').is_file() else '') / manifest_name
    selected = recovery if recovery.is_file() and recovery.stat().st_mtime > manual.stat().st_mtime else manual
    model = json.loads(selected.read_text(encoding='utf-8'))
    if args.asset:
        model = model['document']
    counts = {key: len(model[key]) for key in ('layers', 'objects', 'modifiers', 'masks')}
    assert tuple(counts.values()) == tuple(args.expected_counts), counts
    assert model['size'] == list(args.expected_size), model['size']
    assert model['id'] == args.chapter, model['id']
    selected_record = next(value for value in model['objects'] if value['id'] == args.selected)
    ancestors, layers = [], {value['id']: value for value in model['layers']}
    parent = selected_record['parent_layer_id']
    while parent is not None:
        assert parent not in ancestors
        ancestors.append(parent)
        parent = layers[parent]['parent_id']
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / 'source-files-before.json').write_text(json.dumps(file_manifest(args.project_copy), indent=2), encoding='utf-8')
    metadata = {'project': str(args.project_copy), 'manifest': str(selected), 'counts': counts,
                'size': model['size'], 'selected': args.selected, 'ancestors': ancestors,
                'document_id': args.chapter, 'asset_id': args.asset,
                'source_files': len(file_manifest(args.project_copy)),
                'source_bytes': sum(p.stat().st_size for p in args.project_copy.rglob('*') if p.is_file()),
                'runtime': str(args.runtime_root), 'camera': args.camera, 'canvas': [1042, 937]}
    (args.out / 'fixture.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    print(json.dumps(metadata), flush=True)
    return metadata


def main():
    args = parse_args()
    fixture = prepare(args)
    if args.prepare_only:
        return
    assert sys.platform == 'win32', 'Use actual Windows OpenGL, not offscreen/minimal Qt'
    sys.path.insert(0, str(args.runtime_root))
    os.environ['QT_QPA_PLATFORM'] = 'windows'
    os.environ['QT_TLS_BACKEND'] = 'schannel'
    import numpy as np
    from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, QRectF, QSizeF, QTimer, Qt
    from PySide6.QtGui import QColor, QColorSpace, QImage, QInputDevice, QPainter, QPointingDevice, QSurfaceFormat, QTabletEvent, QTransform
    from PySide6.QtWidgets import QApplication
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.core.pressure import BrushPreset, PressureCurve
    from comic_editor.core.settings import EditorSettings
    import comic_editor.ui.canvas as canvas_module
    from comic_editor.ui.canvas import create_canvas, ToolKind
    from comic_editor.ui.document_presentation import GpuTilePresenter, PresentedTile
    from comic_editor.ui.scene_controller import SceneController
    from drawing_benchmark_support import canvas_pending, pixel_difference, read_native_frame

    def write_json(name, value):
        (args.out / name).write_text(json.dumps(value, indent=2), encoding='utf-8')

    def distribution(values):
        if not values:
            return {'count': 0, 'p50_ms': None, 'p95_ms': None, 'max_ms': None}
        return {'count': len(values), 'total_ms': float(sum(values)),
                'p50_ms': float(np.percentile(values, 50)), 'p95_ms': float(np.percentile(values, 95)),
                'p99_ms': float(np.percentile(values, 99)), 'max_ms': float(max(values)),
                'over_16_7ms': sum(v > 16.7 for v in values), 'over_50ms': sum(v > 50 for v in values),
                'over_250ms': sum(v > 250 for v in values)}

    QCoreApplication.setAttribute(Qt.AA_CompressTabletEvents, False)
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setSamples(0)
    fmt.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication([])
    app.setApplicationName('Private Pocket GPU latency benchmark')
    app.setOrganizationName('Isolated benchmark')
    canvas_module.create_network_manager = lambda *_: None
    pencil_preset = BrushPreset(name='Linear', pressure_size=True,
                               pressure_opacity=False, antialiasing=False).to_dict()
    eraser_preset = BrushPreset(name='Soft', pressure_size=True, pressure_opacity=False,
        antialiasing=False, size_curve=PressureCurve(minimum=.1, maximum=1.,
                                                   control_x=.4186991869918699,
                                                   control_y=.6266094420600858)).to_dict()
    settings = EditorSettings(canvas_renderer='gpu', predictive_ink=True,
        grid_overlay_visible=False, snap_to_grid=False,
        pencil_size_px={k: args.pencil_size for k in ('small', 'medium', 'large')},
        eraser_size_px={k: args.eraser_size for k in ('small', 'medium', 'large')},
        pencil_presets=[pencil_preset], eraser_presets=[eraser_preset],
        active_pencil_preset='Linear', active_eraser_preset='Soft')
    phase = 'setup'
    active_contact = None
    started = time.perf_counter()
    frame_rows, batch_rows, timing_rows, heartbeat_rows, input_rows, contacts = [], [], [], [], [], []
    pending_inputs = []
    gl_counts = Counter()
    original_methods = []
    observed_swaps = 0
    readback_times = []
    dispatching_release = False
    interruption_rows = []
    library = None
    canvas = selected = None
    filtered_rows = []
    filtered_rows_dropped = 0
    witness_scene_base = None
    witness_native_block = witness_display_frame = None
    native_witness_contexts = {}
    first_dab_packets = {}
    busy_scene = None

    def retain_first_dab(values):
        """Borrow only resident Qt COW handles at the actual admission edge."""
        if args.no_pixel_observer or active_contact is None or active_contact in first_dab_packets:
            return
        contact = contacts[active_contact]
        actual_point = QPointF(values[1])
        if (not canvas._drawing or canvas.tool.name != contact['tool'] or canvas.selected_id != args.selected
                or math.dist(actual_point.toTuple(), contact['witness']) > 1e-7):
            contact['first_dab_witness_declined'] = 'Admission belongs to a different queued contact'
            return
        owner = canvas.tiles._tiles.get(args.selected)
        if owner is None:
            contact['first_dab_witness_declined'] = 'The actual admitted source owner is absent'
            return
        packet = {}
        for key in canvas._stroke_before:
            entry = owner.residency.entries.get((owner, key))
            if entry is None or not isinstance(entry[0], QImage):
                contact['first_dab_witness_declined'] = 'A touched source is not resident at admission; diagnostic will not decode it'
                return
            packet[tuple(key)] = QImage(entry[0])
        if not packet or sum(image.sizeInBytes() for image in packet.values()) > 4*1024*1024:
            contact['first_dab_witness_declined'] = 'Actual first-dab source packet is empty or exceeds4MiB'
            return
        preset = canvas._stroke_preset
        size, opacity = canvas._brush_values(canvas._last_pressure)
        # This is the actual Qt source already painted by the production brush,
        # not a reconstructed brush, rounded point, or assumed alpha-lock mode.
        document = canvas._render_document_state()
        first_dab_packets[active_contact] = packet
        contact['first_dab_source'] = {'admitted_at_ms': (time.perf_counter()-started)*1000,
            'world': actual_point.toTuple(),
            'native_local': canvas._raster_local_point(canvas.chapter.objects[args.selected], actual_point).toTuple(),
            'effective_pressure': canvas._last_pressure,
            'diameter_px': size*preset.stroke_start_ratio, 'opacity': opacity,
            'preset': copy.deepcopy(preset).to_dict(), 'document_revision': document.revision,
            'feedback_basis_at_admission': filtered_metadata(),
            'document_configuration_matches': document.configuration == native_witness_contexts[active_contact]['document'].configuration,
            'selected_record': canvas.chapter.objects[args.selected].to_dict(),
            'tiling': canvas._tiling_stroke_context is not None,
            'keys': [list(key) for key in packet],
            'byte_count': sum(image.sizeInBytes() for image in packet.values()),
            'contract': 'Actual admitted native QtCOW source handles; no decode/render/readback in handler'}

    def filtered_metadata():
        """GUI-owned scalar evidence only; never read a worker-owned QImage."""
        controller = canvas._scene_controller
        state = getattr(controller, '_filtered_state', None)
        chapter_now = getattr(canvas, 'chapter', None)
        selected_now = (chapter_now.objects.get(args.selected)
            if chapter_now is not None else None)
        document = None
        metadata_error = None
        # set_document retires the old scene before it initializes all drawing
        # fields. Diagnostic wrappers must not turn that normal lifecycle into
        # an application error or invent a completed render state.
        if (chapter_now is not None and hasattr(canvas, '_document_projection')
                and hasattr(canvas, '_raster_feedback_contact_covered')):
            try:
                document = canvas._render_document_state()
            except AttributeError as error:
                metadata_error = str(error)
        result = {'document_revision': getattr(document, 'revision', None),
            'document_live_preview': getattr(document, 'live_preview', None),
            'selected_interaction_rect': (list(selected_now.interaction_rect)
                if selected_now is not None and hasattr(selected_now, 'interaction_rect') else None),
            'owner_present': state is not None,
            'feedback_present': getattr(controller, 'feedback', None) is not None,
            'drawing': bool(getattr(canvas, '_drawing', False)),
            'tool': getattr(getattr(canvas, 'tool', None), 'name', None),
            'covered': bool(getattr(canvas, '_raster_feedback_contact_covered', False)),
            'request_generation': getattr(controller, '_feedback_request_generation', None),
            'selection_generation': getattr(controller, '_feedback_selection_generation', None),
            'initializing': document is None,
            'metadata_unavailable': metadata_error}
        if state is not None:
            worker = state.worker
            result.update(owner_id=id(state), revision=state.revision,
                completed_revision=state.completed_revision,
                base_revision=state.base_revision, sequence=state.sequence,
                pending_generations=len(state.generations),
                updates=len(state.updates), dirty=list(state.dirty.getRect()),
                error=state.error, worker_error=worker.error,
                worker_busy=worker.busy, worker_stopped=worker.stopped.is_set(),
                worker_submitted_sequence=worker.submitted_sequence,
                worker_completed_sequence=worker.completed_sequence,
                recipes=len(state.prepared.recipes))
            basis = getattr(state, 'presented_basis', None)
            parked_change = getattr(state, 'provisional_change', None)
            handoff = getattr(state, 'selection_handoff', None)
            result.update(provisional=bool(getattr(state, 'provisional', False)),
                provisional_source_completed_revision=getattr(state, 'provisional_source_completed_revision', None),
                presented_basis_revision=basis[0].revision if basis is not None else None,
                presented_basis_bytes=getattr(state, 'presented_basis_bytes', 0),
                presented_request_generation=getattr(state, 'presented_request_generation', None),
                presented_selection_generation=getattr(state, 'presented_selection_generation', None),
                selection_handoff=({key: handoff[key] for key in
                    ('identifier', 'request_generation', 'selection_generation', 'history_revision', 'committed')}
                    if handoff is not None else None),
                provisional_change=({'modifier_id': parked_change[0],
                    'fields': sorted(parked_change[1]), 'values': dict(parked_change[2])}
                    if parked_change is not None else None))
        return result

    def record_filtered(name, **values):
        nonlocal filtered_rows_dropped
        if len(filtered_rows) >= 4096:
            filtered_rows_dropped += 1
            return
        filtered_rows.append({'phase': phase, 'contact': active_contact,
            'name': name, 'at_ms': (time.perf_counter()-started)*1000, **values})

    # These GUI methods already inspect/consume the serial worker's published
    # metadata. The diagnostic copies scalar state only at the same boundary.
    for name in ('filtered_batch', 'retire_feedback', '_retire_filtered', '_filtered_selection_changed'):
        if not hasattr(SceneController, name):
            continue
        original = getattr(SceneController, name)
        def filtered_call(self, *values, _original=original, _name=name, **kwargs):
            owned = (canvas is not None and self is getattr(canvas, '_scene_controller', None)
                and getattr(canvas, 'chapter', None) is not None
                and args.selected in canvas.chapter.objects)
            before = filtered_metadata() if owned else None
            result = None
            try:
                result = _original(self, *values, **kwargs)
                return result
            finally:
                if owned:
                    after = filtered_metadata()
                    count = result[1] if _name == 'filtered_batch' and result is not None else None
                    if active_contact is not None or before['owner_present']:
                        record_filtered(_name, before=before, after=after,
                            replaced_count=count,
                            complete=kwargs.get('complete') if _name == 'filtered_batch' else None,
                            stack=traceback.format_stack(limit=8) if _name == 'retire_feedback' else None)
        setattr(SceneController, name, filtered_call)
        original_methods.append((SceneController, name, original))

    class GlRecorder:
        """Count the presenter's actual Python-visible native GL entry calls."""
        def __init__(self, original):
            self.original, self.wrapped = original, {}

        def __getattr__(self, name):
            value = getattr(self.original, name)
            if not name.startswith('gl') or not callable(value):
                return value
            if name not in self.wrapped:
                @wraps(value)
                def counted(*values, **kw):
                    gl_counts[name] += 1
                    return value(*values, **kw)
                self.wrapped[name] = counted
            return self.wrapped[name]

    def patch(owner, name, label):
        original = getattr(owner, name)
        @wraps(original)
        def measured(*values, **kwargs):
            nonlocal phase
            before = time.perf_counter()
            cpu_before = time.thread_time()
            terminal = None
            if (label == 'canvas.end' and active_contact is not None
                    and 'press_at' in contacts[active_contact]):
                terminal = {'expected_release': dispatching_release,
                    'drawing_before': bool(canvas._drawing),
                    'stack': traceback.format_stack(limit=12)}
            counts_before = dict(gl_counts)
            presenter = values[0] if values and isinstance(values[0], GpuTilePresenter) else None
            before_upload = getattr(presenter, 'uploads', 0)
            before_draw = getattr(presenter, 'draws', 0)
            before_geometry = getattr(presenter, 'geometry_uploads', 0)
            before_allocations = getattr(presenter, 'texture_allocations', 0)
            before_reuses = getattr(presenter, 'texture_reuses', 0)
            result = None
            returned_normally = False
            try:
                result = original(*values, **kwargs)
                returned_normally = True
                if label == 'presenter.initialize' and presenter is not None and presenter.functions is not None:
                    if not isinstance(presenter.functions, GlRecorder):
                        presenter.functions = GlRecorder(presenter.functions)
                return result
            finally:
                end = time.perf_counter()
                ms = (end - before) * 1000
                row = {'phase': phase, 'contact': active_contact, 'name': label,
                       'start_ms': (before-started)*1000, 'ms': ms,
                       'thread_id': threading.get_ident(), 'thread_cpu_ms': (time.thread_time()-cpu_before)*1000}
                if label == 'canvas.begin_admitted' and active_contact is not None:
                    contact = contacts[active_contact]
                    contact.setdefault('begin_admitted_ms', (before-contact['press_at'])*1000)
                    row['returned_normally'] = returned_normally
                    # A successful cold-source callback can run inside an
                    # enclosing caught exception. sys.exc_info() describes
                    # that caller context, not this wrapped call's success.
                    row['enclosing_exception_context'] = (getattr(sys.exc_info()[0], '__name__', None)
                        if returned_normally else None)
                    if returned_normally and result is None:
                        retention_started = time.perf_counter()
                        retain_first_dab(values)
                        contact['first_dab_capture_wall_ms'] = (time.perf_counter()-retention_started)*1000
                        if 'first_dab_source' in contact:
                            contact['first_dab_source']['production_body_start_ms'] = (before-contact['press_at'])*1000
                            contact['first_dab_source']['production_body_wall_ms'] = ms
                            contact['first_dab_source']['production_body_end_ms'] = (end-contact['press_at'])*1000
                if terminal is not None:
                    row.update(terminal)
                if label == 'presenter.upload' and len(values) > 1:
                    tile = values[1]
                    row.update(image_size=[tile.image.width(), tile.image.height()],
                        native_bytes=tile.image.width()*tile.image.height()*4,
                        logical_key=repr(tile.key), world_rect=list(tile.world_rect.getRect()))
                timing_rows.append(row)
                if label == 'presenter.integer_vector':
                    # This correctly sized raw-ctypes query bypasses the proxy.
                    gl_counts['raw_glGetIntegerv'] += 1
                if label == 'presenter.batch':
                    counts = {key: value-counts_before.get(key, 0) for key, value in gl_counts.items()
                              if value != counts_before.get(key, 0)}
                    row.update(result=bool(result), texture_bytes=presenter.texture_bytes,
                        texture_entries=len(presenter._textures), uploads=presenter.uploads-before_upload,
                        batches=presenter.draws-before_draw,
                        geometry_uploads=presenter.geometry_uploads-before_geometry, gl=counts,
                        texture_allocations=getattr(presenter, 'texture_allocations', 0)-before_allocations,
                        texture_reuses=getattr(presenter, 'texture_reuses', 0)-before_reuses,
                        allocated_texture_bytes=getattr(presenter, 'allocated_texture_bytes', presenter.texture_bytes),
                        reason=presenter.reason)
                    batch_rows.append(row)
                if label == 'canvas.paintGL':
                    # The canvas's owned-framebuffer guard and native clear use
                    # context functions directly, outside presenter instrumentation.
                    gl_counts['canvas_clear_state_queries'] += 4
                    continuity = canvas._scene_controller.raster_continuity
                    stats = getattr(canvas, '_document_presentation_stats', None)
                    retry_timer = getattr(canvas, '_gpu_presentation_retry_timer', None)
                    frame_rows.append({**row, 'feedback_covered': bool(canvas._raster_feedback_contact_covered),
                        'filtered': filtered_metadata(),
                        'continuity_bytes': continuity.byte_count, 'continuity_overlays': len(continuity.overlays),
                        'continuity_phases': len(continuity.batch), 'drawing': canvas._drawing,
                        'gpu_contact_pacing_token_present': getattr(canvas, '_gpu_contact_pacing_token', None) is not None,
                        'gpu_presentation_pending': bool(getattr(canvas, '_gpu_presentation_pending', False)),
                        'gpu_retry_token_present': getattr(canvas, '_gpu_presentation_retry_token', None) is not None,
                        'gpu_retry_delay_ms': getattr(canvas, '_gpu_presentation_retry_delay', None),
                        'gpu_retry_remaining_ms': retry_timer.remainingTime() if retry_timer is not None and retry_timer.isActive() else None,
                        'presentation_tiles': getattr(stats, 'tiles', None),
                        'presentation_backend': getattr(stats, 'backend', None)})
        setattr(owner, name, measured)
        original_methods.append((owner, name, original))

    for name, label in (('paintGL', 'canvas.paintGL'), ('paintEvent', 'canvas.paintEvent'),
                        ('tabletEvent', 'canvas.tablet')):
        patch(canvas_module.GpuCanvasWidget, name, label)
    for name, label in (('_begin_stroke', 'canvas.begin'), ('_begin_stroke_admitted', 'canvas.begin_admitted'),
                        ('_continue_stroke', 'canvas.move'),
                        ('_end_stroke', 'canvas.end'), ('_paint_document_projection', 'canvas.projection')):
        patch(canvas_module._CanvasLogic, name, label)
    batch_method = 'draw_groups' if hasattr(GpuTilePresenter, 'draw_groups') else 'draw'
    for name in ('draw', 'draw_groups', '_initialize', '_upload', '_remove', '_save_state', '_restore_state',
                 '_integer_vector', '_prepare_geometry'):
        if hasattr(GpuTilePresenter, name):
            patch(GpuTilePresenter, name, 'presenter.batch' if name == batch_method else 'presenter.' + name.lstrip('_'))

    canvas = create_canvas(settings)
    assert type(canvas).__name__ == 'GpuCanvasWidget'
    canvas.setFixedSize(1042, 937)
    canvas.setAttribute(Qt.WA_ShowWithoutActivating)
    original_hover_painter = canvas._draw_tablet_hover
    if args.pixel_witness_no_cursor:
        # Keep one identical observer-decoration state across every comparison,
        # including release and undo. Restoring before release introduced a
        # 57-pixel crosshair into the old final/undo frames.
        canvas._draw_tablet_hover = lambda _painter: None

    class ContactEvents(QObject):
        def eventFilter(self, watched, event):
            if watched is canvas and event.type() in {QEvent.UngrabMouse, QEvent.WindowDeactivate}:
                interruption_rows.append({'phase': phase, 'contact': active_contact,
                    'at_ms': (time.perf_counter()-started)*1000, 'event': event.type().name,
                    'drawing': bool(canvas._drawing), 'stack': traceback.format_stack(limit=8)})
            return False

    contact_events = ContactEvents(canvas)
    canvas.installEventFilter(contact_events)
    repository = SeriesRepository(args.project_copy)
    if args.asset:
        from comic_editor.core.assets import AssetRepository
        asset_repository = AssetRepository(args.project_copy)
        asset_manifest, tiles, images = asset_repository.load(args.asset,
            recover=asset_repository.has_recovery(args.asset), include_images=True)
        chapter = asset_manifest.document
        canvas.asset_repository = asset_repository
    else:
        chapter, tiles, images = repository.load_chapter(args.chapter,
            recover=repository.has_recovery(args.chapter), include_images=True)
    assert (len(chapter.layers), len(chapter.objects), len(chapter.modifiers), len(chapter.masks)) == tuple(args.expected_counts)
    assert (chapter.width, chapter.height) == tuple(args.expected_size)
    selected = chapter.objects[args.selected]
    assert selected.parent_layer_id == fixture['ancestors'][0]
    canvas.set_document(chapter, tiles, images)
    canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = args.camera
    canvas.set_selection('object', args.selected)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.show()

    def pump(seconds):
        deadline = time.perf_counter() + max(0., seconds)
        while time.perf_counter() < deadline:
            app.processEvents()
            time.sleep(.001)

    def settle():
        canvas.update()
        deadline = time.perf_counter() + args.settle_seconds
        while time.perf_counter() < deadline:
            app.processEvents()
            controller = canvas._scene_controller
            if controller.error:
                raise AssertionError(controller.error)
            document = canvas._render_document_state()
            state = canvas_pending(canvas)
            if (controller.capture is None and not controller.scheduler.busy
                    and controller.snapshot is not None and controller.snapshot.document == document
                    and not state['frame_pending'] and not canvas._native_input_predecessor()
                    and not canvas._native_deferred_activations and not canvas._drawing):
                pump(.05)
                return
            time.sleep(.001)
        raise AssertionError(f'Exact scene did not settle: {canvas_pending(canvas)}')

    def source_buffers():
        result, metadata = {}, {}
        for key, image in canvas.tiles.object_tiles(args.selected).items():
            name = f'{key[0]}_{key[1]}'
            result[name] = np.frombuffer(image.constBits(), np.uint8).copy()
            metadata[name] = {'size': [image.width(), image.height()], 'stride': image.bytesPerLine(),
                              'format': image.format().name, 'color_space': bytes(image.colorSpace().iccProfile()).hex(),
                              'dpr': image.devicePixelRatio(),
                              'sha256': hashlib.sha256(result[name]).hexdigest()}
        return result, metadata

    def save_buffers(label):
        data, metadata = source_buffers()
        np.savez_compressed(args.out / f'{label}-source.npz', **data)
        write_json(f'{label}-source.json', metadata)
        return data, metadata

    def pressure_state():
        presenter = canvas._document_tile_presenter
        return {'texture_bytes': presenter.texture_bytes, 'texture_entries': len(presenter._textures),
                'limit': presenter.byte_limit, 'uploads': presenter.uploads, 'draw_batches': presenter.draws,
                'geometry_uploads': presenter.geometry_uploads, 'reason': presenter.reason,
                'texture_allocations': getattr(presenter, 'texture_allocations', None),
                'texture_reuses': getattr(presenter, 'texture_reuses', None),
                'allocated_texture_bytes': getattr(presenter, 'allocated_texture_bytes', presenter.texture_bytes)}

    pressure_tiles = []
    pressure_known_scene = {}
    def apply_pressure():
        nonlocal phase
        if not args.pressure_tiles:
            return pressure_state()
        presenter = canvas._document_tile_presenter
        batch = canvas._scene_controller._presentation_batch[1]
        native = [tile for _phase, values in batch for tile in values
                  if isinstance(tile.image, QImage) and tile.image.width() == 260 and tile.image.height() == 260]
        assert native, 'Need ordinary completed native scene images for presentation pressure'
        additions = max(0, args.pressure_tiles-len(presenter._textures))
        for index in range(additions):
            original = native[index % len(native)]
            pressure_tiles.append(PresentedTile(('benchmark-presentation-pressure', index), QImage(original.image),
                QRectF(100000. + (index % 32) * 256., 100000. + (index // 32) * 256., 256., 256.),
                QRectF(2., 2., 256., 256.), original.pixel_contract, original.pixel_environment))
        previous = phase
        phase = 'pressure-warmup'
        submitted_at = time.perf_counter()
        canvas.makeCurrent()
        try:
            if args.busy_known_scene_first:
                # Authoritative normal paint owns the clear, complete ordered
                # composition, scene context, and presentation overlays. Inject
                # only offscreen aliases into that one real presenter call;
                # do not manufacture or restore a remembered frame afterwards.
                document = canvas._render_document_state()
                controller = canvas._scene_controller
                assert not document.live_preview and controller.snapshot.document == document
                assert controller.capture is None and not controller.scheduler.busy
                assert tuple(item[0] for item in batch) == (None,)
                assert not controller.raster_continuity.overlays and not controller.raster_continuity.batch
                actual = tuple(tile for _phase, values in batch for tile in values)
                actual_keys = tuple(presenter._key(tile) for tile in actual)
                calls = []
                original = getattr(presenter, batch_method)
                def submit_known(values, *values_args, **values_kwargs):
                    if batch_method == 'draw_groups':
                        from comic_editor.ui.document_presentation import PresentedTileGroup
                        values = tuple(values)
                        tiles_now = tuple(tile for group in values for tile in group.tiles)
                        assert all(group.replace_background is None for group in values)
                        if hasattr(presenter, '_last_native_frame'):
                            assert values_kwargs.get('pending_context') is not None
                        submitted = values + (PresentedTileGroup(tuple(pressure_tiles)),)
                    else:
                        tiles_now = tuple(values)
                        submitted = tiles_now + tuple(pressure_tiles)
                    assert tuple(presenter._key(tile) for tile in tiles_now) == actual_keys
                    calls.append(tuple(presenter._key(tile) for tile in tiles_now))
                    return original(submitted, *values_args, **values_kwargs)
                setattr(presenter, batch_method, submit_known)
                try:
                    canvas.context().functions().glBindFramebuffer(0x8D40, canvas.defaultFramebufferObject())
                    canvas.paintGL()
                finally:
                    setattr(presenter, batch_method, original)
                assert len(calls) == 1, len(calls)
                assert all(key in presenter._textures for key in actual_keys)
                remembered = getattr(presenter, '_last_native_frame', None)
                if hasattr(presenter, '_last_native_frame'):
                    assert remembered is not None
                    assert all(key in remembered[2] for key in actual_keys)
                    assert all(key in presenter._textures for key in remembered[2])
                pressure_known_scene.update(actual_scene_keys=[repr(key) for key in actual_keys],
                    actual_scene_key_count=len(actual_keys), actual_scene_keys_resident=True,
                    remembered_key_count=len(remembered[2]) if remembered is not None else None,
                    remembered_scene_guarded=remembered is not None,
                    method='One ordinary owned canvas.paintGL with offscreen aliases appended; no manual snapshot restoration')
            else:
                assert presenter.draw(pressure_tiles, canvas.camera_transform(), QSizeF(canvas.size()),
                                      device_pixel_ratio=canvas.devicePixelRatioF())
        finally:
            canvas.doneCurrent()
            phase = previous
        if args.busy_pressure_first:
            state = pressure_state()
            state.update(warmup_barrier_ms=None, warmup_barrier_pixels=None,
                pressure_submitted_at=(submitted_at-started)*1000,
                pressure_submit_cpu_return_at=(time.perf_counter()-started)*1000,
                known_scene=pressure_known_scene,
                busy_pressure_assumption='All991 fresh texture allocations queued together; diagnostic pressure, not captured historical idle residency')
            return state
        canvas.update()
        pump(.05)
        # A captured historical texture cache was populated long before the
        # contact. Our artificial951-texture warmup instead queues a large new
        # batch. Complete that test-owned batch *outside input measurement* in
        # both runtimes; otherwise a fence-safe pool can legitimately reject
        # busy capacity and Qt's raster fallback creates an unrelated cold GL
        # texture workload. This single native readback is a warmup barrier.
        barrier_started = time.perf_counter()
        barrier = read_native_frame(canvas)
        state = pressure_state()
        state['warmup_barrier_ms'] = (time.perf_counter()-barrier_started)*1000
        state['warmup_barrier_pixels'] = [barrier.width(), barrier.height()]
        assert state['texture_entries'] >= args.pressure_tiles-1, state
        if args.pressure_tiles == 991:
            assert 254 * 1024 * 1024 <= state['texture_bytes'] <= state['limit'], state
        return state

    def contact_paths():
        # Source alpha alone does not prove that the selected object is visible:
        # an ancestor can clip it, a sibling can cover it, and the chapter ends
        # at x=0. Validate those cases through the unchanged ordinary renderer
        # before timing, using private source copies and native sampling only.
        nonlocal witness_scene_base, witness_native_block, witness_display_frame
        from comic_editor.render.device import cpu_image
        from comic_editor.render.pixels import display_image, pixel_scope
        from comic_editor.render.scene import DetachedSceneBackend
        from comic_editor.render.service import DocumentRenderService, RenderRequest
        local_to_world = canvas._drawing_local_to_world_transform(selected)
        transform = local_to_world * canvas.camera_transform()
        world_to_local, invertible = local_to_world.inverted()
        assert invertible, 'Selected source must have an invertible native placement'
        world_clips = []
        for layer in chapter.ancestor_layers(selected.parent_layer_id):
            assert layer.visible and layer.opacity > 0, 'Selected source has an invisible ancestor'
            world_clips.append(canvas.layer_world_transform(layer.layer_id).map(
                canvas.layer_effective_path(layer.layer_id)))
        chapter_bounds = QRectF(0., 0., chapter.width, chapter.height)
        candidates = []
        rejected = Counter()
        for key, image in canvas.tiles.object_tiles(args.selected).items():
            rgba = image.convertToFormat(QImage.Format_RGBA8888)
            array = np.frombuffer(rgba.constBits(), np.uint8).reshape(rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4].reshape(rgba.height(), rgba.width(), 4)
            ys, xs = np.nonzero(array[:, :, 3] >= 250)
            for x, y in zip(xs[::4], ys[::4]):
                local = QPointF(key[0]*256.+float(x)+.5, key[1]*256.+float(y)+.5)
                world = local_to_world.map(local)
                screen = transform.map(local)
                if not (120. < screen.x() < canvas.width()-120. and 120. < screen.y() < canvas.height()-120.):
                    rejected['outside_viewport'] += 1
                    continue
                # Keep the entire short contact and eraser footprint away from
                # clipping edges; the ordinary witness remains authoritative.
                margin = max(args.pencil_size, args.eraser_size)/2. + 8.
                neighborhood = QRectF(world.x()-margin, world.y()-margin, 2*margin, 2*margin)
                if not chapter_bounds.contains(neighborhood):
                    rejected['outside_chapter'] += 1
                    continue
                if any(not clip.contains(neighborhood) for clip in world_clips):
                    rejected['outside_ancestor_clip'] += 1
                    continue
                candidates.append(local.toTuple())
        assert candidates, 'Selected saved raster has no opaque native pixels under the recorded camera'
        candidates = np.asarray(candidates)
        local_center = world_to_local.map(QPointF(*args.camera[:2]))
        # The current ordinary snapshot includes the real clip, selected state,
        # sibling order, source/effect grids and color environment. It has no
        # disk-cache writer and each counterfactual has its own source owner.
        base = canvas._scene_controller.snapshot.finish_sources()
        assert base.document == canvas._render_document_state() and not base.document.live_preview
        base = replace(base, cache_spec=None, graphics_worker=None)
        witness_scene_base = base
        base_blocks = {}
        attempts, centers, witnesses = [], [], []

        def native_block(snapshot, region, key):
            backend = DetachedSceneBackend(snapshot)
            backend.artwork_scale = 1.
            service = DocumentRenderService(backend)
            service.projection.revision = snapshot.document.revision
            try:
                request = RenderRequest(tuple(region.getRect()), 1.,
                    (int(region.width()), int(region.height())), key, snapshot.document.revision)
                result = service.render_region(snapshot.document, request)
                assert result.exact, result.error or result.status
                image = cpu_image(result.image)
                if image is not result.image:
                    result.image.release()
                return image
            finally:
                backend.close()

        def display_frame(image, region):
            # Match the native image's ordinary presentation edge and camera.
            # Qt and GPU interpolation can differ by a few byte values; the
            # observer uses an explicit tolerance and never accepts any change.
            with pixel_scope(base.document.pixel_contract, environment=base.pixel_environment):
                display = display_image(image, base.document.pixel_contract)
            target = QImage(canvas.size(), QImage.Format_RGBA8888)
            target.fill(QColor('#242428'))
            painter = QPainter(target)
            try:
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                painter.setTransform(canvas.camera_transform())
                painter.drawImage(region, display, QRectF(display.rect()))
            finally:
                painter.end()
            return target

        def display_sample(image, region, screen):
            color = display_frame(image, region).pixelColor(round(screen.x()), round(screen.y()))
            return [color.red(), color.green(), color.blue(), color.alpha()]

        witness_native_block, witness_display_frame = native_block, display_frame

        def first_pressure_parameters(value, erasing=False):
            # Mirror _brush_values and _begin_stroke_impl, including the
            # pressure curve lookup and start ratio. This is eligibility only:
            # the timed witness still retains the actual production source.
            preset = BrushPreset.from_dict(eraser_preset if erasing else pencil_preset)
            size = float(args.eraser_size if erasing else args.pencil_size)
            pressure = .2
            if preset.pressure_size:
                size *= preset.size_curve.evaluate_fast(pressure)
            opacity = (preset.opacity_curve.evaluate_fast(pressure)
                if preset.pressure_opacity else 1.)
            if not erasing:
                opacity *= QColor(value).alphaF()
            return {'effective_pressure': pressure,
                'diameter_px': max(.5, size)*preset.stroke_start_ratio,
                'opacity': opacity, 'preset': copy.deepcopy(preset).to_dict(),
                'erase': erasing, 'square': preset.shape == 'square',
                'antialias': preset.antialiasing, 'replace_alpha': False,
                'tiling': False}

        def pressure_dab(store, candidate, value, parameters):
            return store.paint_dab(args.selected, QPointF(*candidate),
                parameters['diameter_px'], QColor(value), parameters['opacity'],
                erase=parameters['erase'], square=parameters['square'],
                antialias=parameters['antialias'])

        def native_image_contract(image):
            return (image.size(), image.bytesPerLine(), image.format(),
                bytes(image.colorSpace().iccProfile()), image.devicePixelRatio())

        def changed_patch(before, after, patch, validate_baseline=False):
            # The same strict byte/color threshold is used by the independent
            # post-timing actual-packet proof. Keep all49 samples, not merely a
            # center whose full-sized brush happens to reach visible artwork.
            x0, y0 = patch['origin']
            pixels = []
            for py in range(7):
                for px in range(7):
                    old_color = before.pixelColor(x0+px, y0+py)
                    new_color = after.pixelColor(x0+px, y0+py)
                    old = [old_color.red(), old_color.green(), old_color.blue(), old_color.alpha()]
                    new = [new_color.red(), new_color.green(), new_color.blue(), new_color.alpha()]
                    if (validate_baseline and max(abs(a-b) for a,b in
                            zip(old, patch['rgba'][py][px])) > 8):
                        continue
                    if (max(abs(a-b) for a,b in zip(old[:3], new[:3])) >= 40
                            and all(max(abs(a-b) for a,b in zip(new[:3], decoration)) > 8
                                for decoration in ([32,32,36], [36,36,40], [232,245,255], [243,197,106]))):
                        pixels.append({'offset': [px, py], 'screen': [x0+px, y0+py],
                            'before': old, 'first_dab': new})
            return pixels

        def prove_candidate(candidate):
            world = local_to_world.map(QPointF(*candidate))
            screen = canvas.camera_transform().map(world)
            # Ordinary final tiles are evaluated in shared 4x4 native blocks.
            # Keep that full region instead of inventing a cropped effect path.
            bx, by = math.floor(world.x()/1024), math.floor(world.y()/1024)
            region = QRectF(bx*1024.-2., by*1024.-2., 1028., 1028.)
            key = ('diagnostic-visible-source', bx, by)
            if (bx, by) not in base_blocks:
                base_blocks[bx, by] = native_block(base, region, key)
            before = base_blocks[bx, by]
            before_pixel = display_sample(before, region, screen)
            framebuffer_patch = sample_pixel_patch(world.toTuple())
            framebuffer_pixel = framebuffer_patch['rgba'][3][3]
            row = dict(local=candidate.tolist(), world=world.toTuple(), screen=screen.toTuple(),
                native_region=region.getRect(), before_display=before_pixel,
                before_framebuffer=framebuffer_pixel, native_format=before.format().name,
                native_stride=before.bytesPerLine(),
                native_color_space=bytes(before.colorSpace().iccProfile()).hex(),
                native_pixel_contract=repr(base.document.pixel_contract),
                first_pressure_trials=[])
            attempts.append(row)
            if max(abs(a-b) for a,b in zip(before_pixel, framebuffer_pixel)) > 24:
                row['rejected'] = 'Ordinary native baseline does not match current visible framebuffer sample'
                return None
            ranked_colors = sorted(COLORS, key=lambda value: sum((a-b)**2 for a,b in zip(
                (QColor(value).red(), QColor(value).green(), QColor(value).blue()), before_pixel[:3])), reverse=True)
            for value in ranked_colors[:3]:
                pencil_parameters = first_pressure_parameters(value)
                eraser_parameters = first_pressure_parameters(value, erasing=True)
                first_tiles = base.tiles.detached_snapshot(set(base.tiles._tiles))
                first_snapshot = replace(base, tiles=first_tiles)
                dirty = pressure_dab(first_tiles, candidate, value, pencil_parameters)
                # Production expands the selected frame around a new pencil
                # mark. Reject a candidate that would change this witness's
                # model/clip assumptions before any input is timed.
                margin = canvas_module.RASTER_FRAME_MARGIN
                if not QRectF(*selected.interaction_rect).contains(
                        dirty.adjusted(-margin, -margin, margin, margin)):
                    row['first_pressure_trials'].append({'color': value,
                        'rejected': 'First pencil dab would grow the selected native frame'})
                    continue
                first_painted = native_block(first_snapshot, region, key)
                if native_image_contract(first_painted) != native_image_contract(before):
                    row['first_pressure_trials'].append({'color': value,
                        'rejected': 'Ordinary first pencil changed native size, precision, alpha, stride, color or density contract'})
                    continue
                before_display = display_frame(before, region)
                first_display = display_frame(first_painted, region)
                pencil_pixels = changed_patch(before_display, first_display,
                    framebuffer_patch, validate_baseline=True)
                trial = {'color': value, 'pencil_parameters': pencil_parameters,
                    'eraser_parameters': eraser_parameters,
                    'pencil_changed_pixels': pencil_pixels,
                    'before_native_sha256': hashlib.sha256(bytes(before.constBits())).hexdigest(),
                    'first_pencil_native_sha256': hashlib.sha256(bytes(first_painted.constBits())).hexdigest()}
                row['first_pressure_trials'].append(trial)
                if not pencil_pixels:
                    trial['rejected'] = 'Pressure-sized first pencil dab has no unambiguous visible pixel in the observed7x7 patch'
                    continue
                private_tiles = base.tiles.detached_snapshot(set(base.tiles._tiles))
                private = replace(base, tiles=private_tiles)
                full_dirty = private_tiles.paint_dab(args.selected, QPointF(*candidate), args.pencil_size,
                    QColor(value), antialias=False)
                if not QRectF(*selected.interaction_rect).contains(
                        full_dirty.adjusted(-margin, -margin, margin, margin)):
                    trial['rejected'] = 'Full pencil convergence basis would grow the selected native frame'
                    continue
                painted = native_block(private, region, key)
                painted_pixel = display_sample(painted, region, screen)
                delta = max(abs(a-b) for a,b in zip(before_pixel[:3], painted_pixel[:3]))
                if delta < 64:
                    continue
                # The eraser follows held pencil contacts, so validate its
                # small first dab against the private full pencil mark. That
                # constructed basis is eligibility only; actual eraser source
                # and model are captured independently before its timed press.
                first_eraser_tiles = private_tiles.detached_snapshot(set(private_tiles._tiles))
                first_eraser_snapshot = replace(base, tiles=first_eraser_tiles)
                pressure_dab(first_eraser_tiles, candidate, value, eraser_parameters)
                first_erased = native_block(first_eraser_snapshot, region, key)
                if native_image_contract(first_erased) != native_image_contract(before):
                    trial['rejected'] = 'Ordinary first eraser changed native size, precision, alpha, stride, color or density contract'
                    continue
                eraser_pixels = changed_patch(display_frame(painted, region),
                    display_frame(first_erased, region), framebuffer_patch)
                trial.update(eraser_changed_pixels=eraser_pixels,
                    eraser_basis_native_sha256=hashlib.sha256(bytes(painted.constBits())).hexdigest(),
                    first_eraser_native_sha256=hashlib.sha256(bytes(first_erased.constBits())).hexdigest())
                if not eraser_pixels:
                    trial['rejected'] = 'Pressure-sized first eraser dab has no unambiguous visible pixel in the observed7x7 patch'
                    continue
                private_tiles.paint_dab(args.selected, QPointF(*candidate), args.eraser_size,
                    QColor(value), erase=True, antialias=False)
                erased = native_block(private, region, key)
                erased_pixel = display_sample(erased, region, screen)
                if max(abs(a-b) for a,b in zip(painted_pixel[:3], erased_pixel[:3])) < 64:
                    continue
                # These colors belong to the benchmark's cursor/pending UI.
                # A counterfactual near them cannot disambiguate actual ink
                # from a decoration, even when the native source did change.
                decorations = ([32,32,36], [36,36,40], [232,245,255])
                if any(max(abs(a-b) for a,b in zip(pixel[:3], decoration)) <= 24
                       for pixel in (painted_pixel, erased_pixel) for decoration in decorations):
                    continue
                row.update(accepted=True, color=value, pencil_display=painted_pixel,
                    eraser_display=erased_pixel, visible_native_delta=delta,
                    before_native_sha256=hashlib.sha256(bytes(before.constBits())).hexdigest(),
                    pencil_native_sha256=hashlib.sha256(bytes(painted.constBits())).hexdigest(),
                    eraser_native_sha256=hashlib.sha256(bytes(erased.constBits())).hexdigest())
                row['witness_kind'] = 'Full-size convergence/visibility only; configured diameter, not first pressure-sized dab'
                row['first_pressure_eligibility'] = dict(trial,
                    selected_record=selected.to_dict(),
                    camera=list(args.camera), dpr=canvas.devicePixelRatioF(),
                    document_configuration=repr(base.document.configuration),
                    native_region=list(region.getRect()),
                    native_format=first_painted.format().name,
                    native_stride=first_painted.bytesPerLine(),
                    native_color_space=bytes(first_painted.colorSpace().iccProfile()).hex(),
                    contract='Eligibility before timing only; exact ordinary pressure-sized paint_dab on private native sources, full ordinary native block, fixed model/grid/clips; actual packet proof remains independent after timing')
                return row
            row['rejected'] = 'Selected pencil/eraser counterfactual does not change visible ordinary native pixels'
            return None

        exhausted = np.zeros(len(candidates), dtype=bool)
        while len(centers) < args.contacts and len(attempts) < 32:
            if not centers:
                score = -np.sum((candidates-np.asarray(local_center.toTuple()))**2, axis=1)
            else:
                # Farthest-point placement also works for the selected saved
                # raster's sparse circle, which has only five source tiles.
                score = np.min(np.stack([np.sum((candidates-center)**2, axis=1)
                                         for center in centers]), axis=0)
            score[exhausted] = -np.inf
            candidate_index = int(np.argmax(score))
            if not math.isfinite(float(score[candidate_index])):
                break
            candidate = candidates[candidate_index]
            if centers and math.sqrt(float(score[candidate_index])) < 36.:
                break
            # A rejected hidden neighborhood must not consume all32 attempts
            # by proposing adjacent native pixels of the same occluded region.
            exhausted |= np.sum((candidates-candidate)**2, axis=1) < 12.**2
            witness = prove_candidate(candidate)
            if witness is None:
                continue
            centers.append(candidate)
            witnesses.append(witness)
        write_json('visible-source-validation.json', {'attempts': attempts,
            'candidate_rejections': dict(rejected), 'accepted': len(centers),
            'contract': 'Unchanged ordinary full native block; private source copies; no durable exact cache',
            'display_tolerance_bytes': 24, 'first_pressure_patch_tolerance_bytes': 8,
            'first_pressure_patch_minimum_rgb_delta': 40,
            'first_pressure_patch_size': [7, 7]})
        assert len(centers) == args.contacts, 'Need disjoint, clipped, visible native pencil AND eraser witnesses; inspect visible-source-validation.json'
        count = max(2, round(args.seconds*args.hz))
        paths = []
        for center in centers:
            nearby = candidates[np.sum((candidates-center)**2, axis=1) <= 6.**2]
            local_points = []
            for index in range(count+1):
                t = index/count
                wanted = center if index == 0 else center + (math.sin(t*math.pi*6)*4, math.cos(t*math.pi*4)*2)
                local_points.append(nearby[np.argmin(np.sum((nearby-wanted)**2, axis=1))])
            world_points = [local_to_world.map(QPointF(*point)) for point in local_points]
            assert all(chapter_bounds.contains(point) and all(clip.contains(point) for clip in world_clips)
                for point in world_points), 'Every held path sample must remain inside visible chapter/ancestor clips'
            paths.append([point.toTuple() for point in world_points])
        write_json('paths.json', {'candidates': len(candidates), 'local_centers': [v.tolist() for v in centers],
                                 'world_paths': paths, 'witnesses': witnesses})
        return paths, witnesses

    pen = QPointingDevice('Private benchmark native pen', 1, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen,
        QInputDevice.Capability.Position | QInputDevice.Capability.Pressure, 1, 3)
    last_heartbeat = time.perf_counter()
    def heartbeat():
        nonlocal last_heartbeat
        now = time.perf_counter()
        heartbeat_rows.append({'phase': phase, 'contact': active_contact, 'at_ms': (now-started)*1000,
                               'gap_ms': (now-last_heartbeat)*1000})
        last_heartbeat = now
    timer = QTimer()
    timer.setTimerType(Qt.PreciseTimer)
    timer.setInterval(8)
    timer.timeout.connect(heartbeat)
    timer.start()

    def sample_pixel_patch(world):
        point = canvas.camera_transform().map(QPointF(*world))
        ratio = canvas.devicePixelRatioF()
        x, y = round(point.x()*ratio), round(point.y()*ratio)
        radius = 3
        side = 2*radius+1
        sample = np.empty((side, side, 4), np.uint8)
        assert radius <= x < round(canvas.width()*ratio)-radius
        assert radius <= y < round(canvas.height()*ratio)-radius
        canvas.makeCurrent()
        try:
            canvas.context().functions().glBindFramebuffer(0x8D40, canvas.defaultFramebufferObject())
            library.glReadPixels(x-radius, round(canvas.height()*ratio)-y-1-radius,
                side, side, 0x1908, 0x1401, sample.ctypes.data)
        finally:
            canvas.doneCurrent()
        # GL rows start at the bottom; retain Qt's top-left framebuffer order.
        return {'origin': [x-radius, y-radius], 'rgba': sample[::-1].tolist()}

    def sample_pixel(world):
        return sample_pixel_patch(world)['rgba'][3][3]

    def prove_actual_first_dabs():
        """Ordinary native rendering happens only after all timed contacts."""
        if args.no_pixel_observer:
            return
        rows = []
        final_model = chapter.to_dict()

        def without_selected_frame(value):
            value = copy.deepcopy(value)
            for record in value['objects']:
                if record['id'] == args.selected:
                    record['interaction_rect'] = None
            return value

        def rgba_at(image, x, y):
            color = image.pixelColor(x, y)
            return [color.red(), color.green(), color.blue(), color.alpha()]

        def image_metadata(image):
            return {'size': [image.width(), image.height()], 'stride': image.bytesPerLine(),
                'format': image.format().name, 'color_space': bytes(image.colorSpace().iccProfile()).hex(),
                'dpr': image.devicePixelRatio(),
                'sha256': hashlib.sha256(bytes(image.constBits())).hexdigest()}

        for contact in contacts:
            index = contact['index']
            context = native_witness_contexts[index]
            packet = first_dab_packets.get(index)
            admission = contact.get('first_dab_source')
            row = {'contact': index, 'tool': contact['tool'],
                'witness_kind': 'Actual admitted first-pressure native source, evaluated after timed contacts'}
            rows.append(row)
            contact['first_dab_native_witness'] = row
            if packet is None or admission is None:
                row['declined'] = contact.get('first_dab_witness_declined', 'No actual first-dab packet was retained')
                continue
            eligibility = contact['witness_validation']['first_pressure_eligibility']
            expected_parameters = eligibility['pencil_parameters'
                if contact['tool'] == 'RASTER_PENCIL' else 'eraser_parameters']
            parity = {'configuration': admission['document_configuration_matches']
                    and context['document'].configuration == witness_scene_base.document.configuration,
                'selected_record': admission['selected_record'] == context['chapter'].objects[args.selected].to_dict(),
                'unrelated_model': without_selected_frame(final_model)
                    == without_selected_frame(context['chapter'].to_dict()),
                'camera': tuple(args.camera) == (canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation),
                'dpr': canvas.devicePixelRatioF() == 1., 'ordinary_untiled_brush': not admission['tiling'],
                'first_pressure': abs(admission['effective_pressure']-expected_parameters['effective_pressure']) <= 1e-8,
                'first_diameter': abs(admission['diameter_px']-expected_parameters['diameter_px']) <= 1e-6,
                'first_opacity': abs(admission['opacity']-expected_parameters['opacity']) <= 1e-8,
                'first_preset': admission['preset'] == expected_parameters['preset'],
                'first_native_point': math.dist(admission['native_local'], contact['witness_validation']['local']) <= 1e-7}
            row['parity'] = parity
            if not all(parity.values()):
                row['declined'] = 'Actual first-dab model/clip/configuration/native placement differs from its captured ordinary basis'
                continue
            proof_basis = witness_scene_base
            proof_chapter = context['chapter']
            basis_at_admission = admission['feedback_basis_at_admission']
            provisional_proof = bool(basis_at_admission.get('provisional'))
            row['whole_scene_basis'] = 'Current ordinary model'
            if provisional_proof:
                old_basis = context.get('previously_presented_basis')
                change = basis_at_admission.get('provisional_change')
                old_model = old_basis.chapter.to_dict() if old_basis is not None else None
                expected_old = context['chapter'].to_dict()
                current_modifier = next((record for record in expected_old['modifiers']
                    if change is not None and record['id'] == change['modifier_id']), None)
                old_modifier = next((record for record in (old_model or {}).get('modifiers', ())
                    if change is not None and record['id'] == change['modifier_id']), None)
                provisional_parity = (old_basis is not None and change is not None
                    and current_modifier is not None and old_modifier is not None
                    and set(change['fields']) <= {'strength', 'intensity'} and bool(change['fields'])
                    and all(current_modifier[field] == change['values'][field] for field in change['fields'])
                    and basis_at_admission.get('presented_basis_revision') == old_basis.document.revision
                    and old_basis.document.configuration == context['document'].configuration)
                if provisional_parity:
                    for field in change['fields']:
                        current_modifier[field] = old_modifier[field]
                    provisional_parity = expected_old == old_model
                row['provisional_model_parity'] = provisional_parity
                if not provisional_parity:
                    row['declined'] = 'The parked old-background model is not the exact recorded unrelated scalar basis'
                    continue
                proof_basis, proof_chapter = old_basis, old_basis.chapter
                row['whole_scene_basis'] = 'Previously presented old background plus actual current native first-dab source; provisional only'
                row['old_background_revision'] = old_basis.document.revision
                row['old_background_modifier'] = old_modifier
            source_contracts = {str(key): image_metadata(image) for key, image in packet.items()}
            row['actual_source_packet'] = source_contracts
            if any(image.isNull() or image.width() != canvas.tiles.tile_size
                    or image.height() != canvas.tiles.tile_size
                    or image.devicePixelRatio() != 1. for image in packet.values()):
                row['declined'] = 'Actual native source dimensions or display density changed'
                continue
            changed_contracts = []
            for source_key, image in packet.items():
                name = f'{source_key[0]}_{source_key[1]}'
                if name in context['metadata']:
                    before_contract = context['metadata'][name]
                    actual_contract = image_metadata(image)
                    if any(before_contract[field] != actual_contract[field]
                            for field in ('size', 'stride', 'format', 'color_space', 'dpr')):
                        changed_contracts.append(name)
            if changed_contracts:
                row['declined'] = 'Actual native source precision, alpha, stride, color or density contract changed'
                row['source_contract_changed_keys'] = changed_contracts
                continue
            # These lossless artifacts are written after every timed contact.
            # They preserve native16/float buffers as raw bytes, without a PNG
            # conversion or a second brush implementation.
            stem = f'first-dab-{index}'
            np.savez_compressed(args.out / f'{stem}-before-source.npz', **context['before'])
            write_json(f'{stem}-before-source.json', context['metadata'])
            packet_bytes = {f'{key[0]}_{key[1]}': np.frombuffer(image.constBits(), np.uint8).copy()
                for key, image in packet.items()}
            np.savez_compressed(args.out / f'{stem}-actual-source.npz', **packet_bytes)
            write_json(f'{stem}-actual-source.json', {
                f'{key[0]}_{key[1]}': image_metadata(image) for key, image in packet.items()})
            row['source_artifact_prefix'] = stem
            # Decode no PNGs from the edited owner. The existing precontact byte
            # guard is a lossless representation of each original source buffer,
            # with its exact stride, precision, alpha format and ICC metadata.
            private_tiles = proof_basis.tiles.detached_snapshot(set(proof_basis.tiles._tiles))
            owner = private_tiles._object_tiles(args.selected)
            for key in tuple(owner):
                del owner[key]
            for name, data in context['before'].items():
                metadata = context['metadata'][name]
                image = QImage(data.data, *metadata['size'], metadata['stride'],
                    getattr(QImage.Format, metadata['format'])).copy()
                assert not image.isNull()
                if metadata['color_space']:
                    image.setColorSpace(QColorSpace.fromIccProfile(bytes.fromhex(metadata['color_space'])))
                image.setDevicePixelRatio(metadata['dpr'])
                restored = image_metadata(image)
                assert all(restored[field] == metadata[field]
                    for field in ('size', 'stride', 'format', 'color_space', 'dpr', 'sha256')), (
                    'Precontact native source bytes or contract changed during reconstruction')
                owner[tuple(map(int, name.split('_')))] = image
            document = replace(proof_basis.document, revision=admission['document_revision'],
                live_preview=False, contact_only=False)
            snapshot = replace(proof_basis, document=document,
                chapter=proof_chapter, tiles=private_tiles, cache_spec=None, graphics_worker=None)
            region = QRectF(*contact['witness_validation']['native_region'])
            key = ('diagnostic-actual-first-dab', index)
            before_image = witness_native_block(snapshot, region, key)
            for source_key, image in packet.items():
                owner[source_key] = QImage(image)
            after_image = witness_native_block(snapshot, region, key)
            row.update(native_before=image_metadata(before_image), native_first_dab=image_metadata(after_image),
                effective_pressure=admission['effective_pressure'], diameter_px=admission['diameter_px'],
                opacity=admission['opacity'], native_region=list(region.getRect()))
            if row['native_before']['sha256'] == row['native_first_dab']['sha256']:
                row['declined'] = 'The actual first-pressure source has no visible ordinary native scene change in this block'
                continue
            displayed_before = witness_display_frame(before_image, region)
            displayed_after = witness_display_frame(after_image, region)
            prior = contact['witness_before_patch']
            x0, y0 = prior['origin']
            trusted = []
            for py in range(7):
                for px in range(7):
                    old = rgba_at(displayed_before, x0+px, y0+py)
                    new = rgba_at(displayed_after, x0+px, y0+py)
                    if (max(abs(a-b) for a,b in zip(old, prior['rgba'][py][px])) <= 8
                            and max(abs(a-b) for a,b in zip(old[:3], new[:3])) >= 40
                            and all(max(abs(a-b) for a,b in zip(new[:3], decoration)) > 8
                                for decoration in ([32,32,36], [36,36,40], [232,245,255], [243,197,106]))):
                        trusted.append({'offset': [px, py], 'screen': [x0+px, y0+py],
                            'before': old, 'first_dab': new})
            row['trusted_changed_pixels'] = trusted
            row['presentation_tolerance_bytes'] = 8
            if not trusted:
                row['declined'] = 'The actual first-pressure mark has no unambiguous changed display pixel in the observed7x7 patch'
                continue
            for sample in contact['swap_pixel_trace']:
                state = sample['filtered']
                if provisional_proof:
                    if (not state.get('provisional') or not state['owner_present']
                            or state.get('owner_id') != basis_at_admission.get('owner_id')
                            or state.get('presented_basis_revision') != row['old_background_revision']):
                        continue
                    source_revision = state.get('provisional_source_completed_revision', -1)
                else:
                    if state.get('provisional'):
                        continue
                    source_revision = state.get('completed_revision', -1)
                # A filtered native completion must include this actual source
                # admission; old basis colors or a pending UI mark cannot prove
                # first ink even if a single displayed byte happens to match.
                if state['owner_present'] and (state.get('updates', 0) == 0
                        or source_revision is None or source_revision < admission['document_revision']):
                    continue
                for pixel in trusted:
                    px, py = pixel['offset']
                    observed = sample['pixel_patch']['rgba'][py][px]
                    if (max(abs(a-b) for a,b in zip(observed, pixel['first_dab'])) <= 8
                            and max(abs(a-b) for a,b in zip(observed[:3], pixel['before'][:3])) >= 32):
                        row['first_fresh_ink'] = {'ms': sample['ms'], 'screen': pixel['screen'],
                            'color': observed, 'expected': pixel['first_dab'],
                            'drawing': sample['drawing'], 'covered': sample['covered'],
                            'filtered': state, 'whole_scene_basis': row['whole_scene_basis'],
                            'old_background_revision': row.get('old_background_revision')}
                        row['temporal_contract'] = ('Earliest observed swap matching an unambiguous actual first-dab pixel '
                            'in the bounded patch; an upper bound on first fresh ink anywhere in the canvas')
                        contact['first_fresh_ink_ms'] = sample['ms']
                        contact['first_fresh_ink_while_held'] = sample['drawing']
                        contact['first_fresh_ink_pixel'] = row['first_fresh_ink']
                        break
                if 'first_fresh_ink' in row:
                    break
            if 'first_fresh_ink' not in row:
                row['declined'] = 'No observed swap matches an unambiguous actual first-pressure native mark'
        write_json('actual-first-dab-native-witness.json', rows)

    def observe_swap():
        nonlocal observed_swaps
        now = time.perf_counter()
        observed_swaps += 1
        covered = bool(canvas._raster_feedback_contact_covered)
        for row in pending_inputs:
            row['dispatch_to_swap_ms'] = (now-row['received_at'])*1000
            row['schedule_to_swap_ms'] = (now-row['scheduled_at'])*1000
            row['feedback_covered_at_swap'] = covered
            row['drawing_at_swap'] = bool(canvas._drawing)
        pending_inputs.clear()
        if active_contact is not None:
            contact = contacts[active_contact]
            if 'press_at' not in contact:
                # The optional real modifier operation pumps ordinary events
                # before a contact clock or native event exists.
                return
            contact.setdefault('first_swap_ms', (now-contact['press_at'])*1000)
            if covered:
                contact.setdefault('first_covered_swap_ms', (now-contact['press_at'])*1000)
        if active_contact is None or args.no_pixel_observer:
            return
        contact = contacts[active_contact]
        if not input_rows:
            return
        before = time.perf_counter()
        patch_sample = sample_pixel_patch(contact['witness'])
        color = patch_sample['rgba'][3][3]
        readback_times.append((time.perf_counter()-before)*1000)
        trace = contact['swap_pixel_trace']
        if len(trace) < 512:
            trace.append({'ms': (now-contact['press_at'])*1000, 'color': color,
                'pixel_patch': patch_sample,
                'drawing': bool(canvas._drawing), 'covered': covered,
                'filtered': filtered_metadata()})
        else:
            contact['swap_pixel_trace_dropped'] += 1
        if color != contact['witness_before'] and 'first_pixel_change_ms' not in contact:
            contact['first_pixel_change_ms'] = (now-contact['press_at'])*1000
            contact['first_pixel_color'] = color
            contact['first_pixel_change_while_held'] = bool(canvas._drawing)
            contact['first_pixel_feedback_covered'] = covered
        cursor_color = color[:3] == [232, 245, 255]
        expected = contact['witness_expected_display']
        matches_native_witness = (expected is not None and not cursor_color
            and max(abs(a-b) for a,b in zip(color, expected)) <= 24
            and max(abs(a-b) for a,b in zip(color[:3], contact['witness_before'][:3])) >= 40)
        if matches_native_witness and 'validated_ink_pixel_change_ms' not in contact:
            contact['validated_ink_pixel_change_ms'] = (now-contact['press_at'])*1000
            contact['validated_ink_pixel_color'] = color
            contact['validated_ink_pixel_change_while_held'] = bool(canvas._drawing)
            contact['validated_ink_feedback_covered'] = covered
            contact['full_size_convergence_ms'] = contact['validated_ink_pixel_change_ms']
            contact['validated_ink_witness_kind'] = 'Full-size convergence only'
        if matches_native_witness and covered and 'native_feedback_pixel_change_ms' not in contact:
            contact['native_feedback_pixel_change_ms'] = (now-contact['press_at'])*1000
            contact['native_feedback_pixel_color'] = color
            contact['native_feedback_pixel_change_while_held'] = bool(canvas._drawing)

    canvas.frameSwapped.connect(observe_swap)
    failure = None
    result = {}
    try:
        settle()
        assert canvas.devicePixelRatioF() == 1., canvas.devicePixelRatioF()
        canvas.makeCurrent()
        try:
            library = ctypes.WinDLL('opengl32')
            library.glGetString.restype = ctypes.c_char_p
            library.glReadPixels.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
            graphics = library.glGetString(0x1F01).decode()
            version = library.glGetString(0x1F02).decode()
            actual_format = canvas.context().format()
        finally:
            canvas.doneCurrent()
        print(f'Native GPU ready: {graphics}; {version}', flush=True)
        initial_source, initial_metadata = save_buffers('initial')
        initial_source_tiles = len(initial_source)
        if args.expected_source_tiles is not None:
            assert initial_source_tiles == args.expected_source_tiles, (initial_source_tiles, args.expected_source_tiles)
        initial_frame = read_native_frame(canvas)
        initial_frame.save(str(args.out / 'initial-native.png'))
        initial_model = chapter.to_dict()
        paths, witnesses = contact_paths()
        # Private ordinary witness work precedes pressure and all timed input;
        # give any ordinary scene worker publication its normal idle boundary.
        settle()
        if args.busy_scene_first:
            from pocket_busy_scene_support import PocketBusySceneSupport
            busy_scene = PocketBusySceneSupport(canvas)
        # Capture the witness before any queued pressure. A readback between
        # pressure and first press would silently turn busy storage into idle.
        first_witness_patch = sample_pixel_patch(paths[0][0]) if not args.no_pixel_observer else None
        pressure = pressure_state() if args.busy_known_scene_first else apply_pressure()
        write_json('pressure-before.json', pressure)
        print(f'Pressure ready: {pressure}', flush=True)
        history = canvas.command_stack.revision
        tools = ((ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER) if args.tools == 'both' else
                 (ToolKind.RASTER_PENCIL,) if args.tools == 'pencil' else (ToolKind.RASTER_ERASER,))
        sequence = 0
        for tool in tools:
            canvas.set_tool(tool)
            phase = tool.name
            for index, path in enumerate(paths):
                active_contact = len(contacts)
                witness_contract = witnesses[index]
                canvas.primary_color = witness_contract['color']
                witness = path[0]
                before_patch = (first_witness_patch if active_contact == 0 else sample_pixel_patch(witness)) if not args.no_pixel_observer else None
                before_pixel = before_patch['rgba'][3][3] if before_patch is not None else None
                contact = {'index': active_contact, 'tool': tool.name, 'ordinal': index,
                    'witness': witness, 'witness_before': before_pixel,
                    'witness_before_patch': before_patch,
                    # The ordinary Blur edit has a different model basis.
                    # Pre-edit full-size colors are visibility eligibility only;
                    # the actual first-dab proof evaluates the post-edit model.
                    'witness_expected_display': (None if args.busy_scene_first else
                        witness_contract['pencil_display' if tool == ToolKind.RASTER_PENCIL else 'eraser_display']),
                    'witness_validation': witness_contract,
                    'color': witness_contract['color'],
                    'pressure_before': pressure_state(), 'history_before': canvas.command_stack.revision,
                    'continuity_bytes_before': canvas._scene_controller.raster_continuity.byte_count,
                    'scene_submitted_before': canvas._scene_controller.scheduler.submitted}
                contact['held_drawing_lost'] = []
                contact['swap_pixel_trace'] = []
                contact['swap_pixel_trace_dropped'] = 0
                source_before, source_before_meta = source_buffers()
                if not args.no_pixel_observer:
                    # Immutable model/source metadata capture is outside this
                    # contact's timing. Its source bytes were already required
                    # for the native change/undo guard; no extra decode is added.
                    native_witness_contexts[active_contact] = {
                        'chapter': copy.deepcopy(chapter),
                        'document': canvas._render_document_state(),
                        'before': source_before, 'metadata': source_before_meta}
                contact['source_digest_before'] = {key: value['sha256'] for key, value in source_before_meta.items()}
                contact['filtered_before_press'] = filtered_metadata()
                contacts.append(contact)
                if active_contact == 0 and busy_scene is not None:
                    from comic_editor.core.models import ChapterDocument
                    from comic_editor.core.tiles import TileStore
                    from comic_editor.render.scene import SCENE_FIELDS, SceneSnapshotCompiler, detached_value
                    phase = 'busy-scene-setup'
                    assert set(source_before) == set(initial_source) and all(
                        np.array_equal(source_before[key], initial_source[key]) for key in initial_source), (
                        'The optional busy trial must begin with exact original native source bytes')
                    busy_scene.queue_modifier_change()
                    basis = busy_scene.post_mutation_basis()
                    basis_model = json.loads(basis.model_json)
                    basis_chapter = ChapterDocument.from_dict(basis_model)
                    assert basis_chapter.to_dict() == basis_model == chapter.to_dict(), (
                        'The ordinary post-modifier model must roundtrip without migration or mutation')
                    assert basis.camera == tuple(args.camera) and basis.dpr == 1.
                    assert basis.document == canvas._render_document_state()
                    assert not basis.document.live_preview and not basis.document.contact_only
                    # Reuse the ordinary capture's metadata detacher, without
                    # visiting artwork sources or reading any worker snapshot.
                    # Live transient source/cage ownership is outside this
                    # narrowly defined real modifier trial.
                    basis_state = {}
                    empty_store_fields = []
                    for name in SCENE_FIELDS:
                        if hasattr(canvas, name):
                            value = getattr(canvas, name)
                            if isinstance(value, TileStore):
                                assert (name == '_vector_preview_tiles'
                                    and not canvas._vector_gesture_mode
                                    and not canvas._vector_samples
                                    and not canvas._vector_sweep
                                    and canvas._promoted_vector_preview is None
                                    and all(not owner.entries for owner in value._tiles.values())), (
                                        f'Busy trial rejects active transient source store: {name}')
                                # Ordinary canvases always own an empty vector
                                # preview TileStore. Use the same capture
                                # helper as SceneCapture instead of deepcopy
                                # or retaining its mutable editor owner.
                                capture = SceneSnapshotCompiler().capture(canvas, basis.document)
                                iterator = capture._capture_tiles(value, name)
                                try:
                                    next(iterator)
                                except StopIteration as completed:
                                    saved, pins, addresses = completed.value
                                else:
                                    iterator.close()
                                    raise AssertionError('Empty preview capture unexpectedly visited native sources')
                                assert not pins and not addresses
                                assert saved is not value and all(not owner.entries for owner in saved._tiles.values())
                                basis_state[name] = saved
                                empty_store_fields.append(name)
                                continue
                            if name == '_cage_session':
                                assert value is None, 'Busy trial rejects an active cage draft'
                            basis_state[name] = detached_value(value)
                    mesh = canvas._mesh_warp_preview_modifier()
                    basis_state['_mesh_warp_preview_id'] = mesh.modifier_id if mesh is not None else None
                    basis_state['_smudge_preview_id'] = (mesh.modifier_id if mesh is not None
                        and mesh.modifier_type == 'distort_smudge' else None)
                    if not args.no_pixel_observer:
                        native_witness_contexts[active_contact]['previously_presented_basis'] = witness_scene_base
                    witness_scene_base = replace(witness_scene_base,
                        chapter=basis_chapter, document=basis.document, state=basis_state)
                    if not args.no_pixel_observer:
                        native_witness_contexts[active_contact].update(
                            chapter=basis_chapter, document=basis.document)
                    (args.out / 'busy-scene-post-mutation-model.json').write_text(basis.model_json, encoding='utf-8')
                    write_json('busy-scene-post-mutation-basis.json', {
                        'document_identity': basis.document.identity,
                        'document_configuration': repr(basis.document.configuration),
                        'document_revision': basis.document.revision,
                        'pixel_contract': repr(basis.document.pixel_contract),
                        'camera': basis.camera, 'dpr': basis.dpr,
                        'history_revision': basis.history_revision,
                        'original_owner_id': basis.original_owner_id,
                        'original_versions': basis.original_versions,
                        'empty_store_fields': empty_store_fields,
                        'scene_state_contract': 'Ordinary SCENE_FIELDS/detached_value metadata and _capture_tiles for proven empty vector preview only; no artwork traversal, active transient source stores or cage drafts',
                        'contract': 'Exact ordinary post-Blur28 model/source basis before worker pumping; pre-edit full-size colors are not convergence proof'})
                    history = basis.history_revision
                    contact['history_before'] = history
                if active_contact == 0 and args.cold_source_first:
                    if busy_scene is not None:
                        busy_scene.make_original_sources_cold()
                    else:
                        owner = canvas.tiles._tiles.get(args.selected)
                        assert owner is not None and all(isinstance(owner.entries[key], Path) for key in owner), 'Cold diagnostic requires unchanged disk-backed source tiles'
                        for key in tuple(owner):
                            canvas.tiles.residency.evict(owner, key)
                if active_contact == 0 and busy_scene is not None:
                    deadline = time.perf_counter()+args.settle_seconds
                    while not busy_scene.ready and time.perf_counter() < deadline:
                        app.processEvents()
                        if not busy_scene.ready:
                            time.sleep(.001)
                    assert busy_scene.ready, 'No positive real Raster63 ordinary-render block notification arrived'
                    contact['filtered_before_press'] = filtered_metadata()
                    contact['scene_submitted_before'] = canvas._scene_controller.scheduler.submitted
                    contact['continuity_bytes_before'] = canvas._scene_controller.raster_continuity.byte_count
                    phase = tool.name
                owner = canvas.tiles._tiles.get(args.selected)
                contact['source_readiness_before'] = {
                    'native_source_keys': [list(key) for key in owner or ()],
                    'resident_keys': [list(key) for key in owner or () if (owner, key) in canvas.tiles.residency.entries],
                    'known_alpha_keys': [list(key) for key in canvas.tiles._alpha_bounds.get(args.selected, {})],
                    'scene_snapshot_ready': canvas._scene_controller.snapshot is not None,
                    'scene_capture_pending': canvas._scene_controller.capture is not None,
                    'scene_worker_busy': bool(canvas._scene_controller.scheduler.busy),
                    'received_contact_pending': bool(canvas._has_received_painting_contact()),
                    'raster_feedback_covered': bool(canvas._raster_feedback_contact_covered),
                    'cold_source_diagnostic': bool(args.cold_source_first and active_contact == 0),
                }
                if active_contact == 0 and args.busy_known_scene_first:
                    # All source hashes, path selection, and witness reads are
                    # already complete. Queue pressure immediately before the
                    # guard/press, without disk output or an event pump.
                    pressure = apply_pressure()
                    contact['pressure_before'] = pressure_state()
                    presenter = canvas._document_tile_presenter
                    actual_keys = tuple(presenter._key(tile) for _phase, values in canvas._scene_controller._presentation_batch[1] for tile in values)
                    assert all(key in presenter._textures for key in actual_keys)
                    remembered = getattr(presenter, '_last_native_frame', None)
                    if hasattr(presenter, '_last_native_frame'):
                        assert remembered is not None and all(key in remembered[2] for key in actual_keys)
                        assert all(key in presenter._textures for key in remembered[2])
                    contact['known_scene_guard_before'] = dict(all_actual_scene_keys_resident=True,
                        actual_scene_key_count=len(actual_keys), guarded=remembered is not None,
                        all_remembered_keys_resident=remembered is not None and all(key in presenter._textures for key in remembered[2]))
                contact['press_at'] = time.perf_counter()
                if active_contact == 0 and args.busy_known_scene_first:
                    contact['pressure_return_to_press_ms'] = ((contact['press_at']-started)*1000
                        - pressure['pressure_submit_cpu_return_at'])
                for point_index, world in enumerate(path):
                    due = contact['press_at'] + point_index/args.hz
                    pump(max(0., due-time.perf_counter()))
                    screen = canvas.camera_transform().map(QPointF(*world))
                    event_type = QEvent.TabletPress if point_index == 0 else QEvent.TabletMove
                    pressure_sample = .2 + .75 * (.5-.5*math.cos(point_index/len(path)*math.pi*4))
                    event = QTabletEvent(event_type, pen, screen, QPointF(canvas.mapToGlobal(screen.toPoint())),
                        pressure_sample, 0., 0., 0., 0., 0., Qt.NoModifier,
                        Qt.LeftButton if point_index == 0 else Qt.NoButton, Qt.LeftButton)
                    event.setTimestamp(sequence+1)
                    received = time.perf_counter()
                    row = {'sequence': sequence, 'contact': active_contact, 'tool': tool.name,
                        'point_index': point_index, 'kind': 'press' if point_index == 0 else 'move',
                        'world': world, 'screen': screen.toTuple(), 'pressure': pressure_sample, 'scheduled_at': due,
                        'received_at': received, 'schedule_lateness_ms': max(0., received-due)*1000}
                    input_rows.append(row)
                    pending_inputs.append(row)
                    if active_contact == 0 and point_index == 0 and busy_scene is not None:
                        busy_scene.dispatch_if_current(lambda: QCoreApplication.sendEvent(canvas, event))
                    else:
                        QCoreApplication.sendEvent(canvas, event)
                    row['handler_ms'] = (time.perf_counter()-received)*1000
                    if active_contact == 0 and point_index == 0 and busy_scene is not None:
                        assert busy_scene.report()['actual_press_busy_guard_valid'], (
                            'The real ordinary ticket/block expired before native press receipt; busy trial declined')
                    if not canvas._drawing:
                        contact['held_drawing_lost'].append(point_index)
                    sequence += 1
                world = path[-1]
                screen = canvas.camera_transform().map(QPointF(*world))
                event = QTabletEvent(QEvent.TabletRelease, pen, screen,
                    QPointF(canvas.mapToGlobal(screen.toPoint())), 0., 0., 0., 0., 0., 0.,
                    Qt.NoModifier, Qt.LeftButton, Qt.NoButton)
                event.setTimestamp(sequence+1)
                contact['release_at'] = time.perf_counter()
                dispatching_release = True
                try:
                    QCoreApplication.sendEvent(canvas, event)
                finally:
                    dispatching_release = False
                contact['release_handler_ms'] = (time.perf_counter()-contact['release_at'])*1000
                contact['filtered_after_release'] = filtered_metadata()
                contact['history_at_release'] = canvas.command_stack.revision
                contact['continuity_bytes_at_release'] = canvas._scene_controller.raster_continuity.byte_count
                contact['pressure_after'] = pressure_state()
                contact['held_scene_submissions'] = canvas._scene_controller.scheduler.submitted-contact['scene_submitted_before']
                sequence += 1
                # No exact settle between contacts: ordinary retained live
                # presentation and pending successor transactions remain active.
                pump(args.gap)
                source_after, source_after_meta = source_buffers()
                contact['source_digest_after'] = {key: value['sha256'] for key, value in source_after_meta.items()}
                changed_keys = []
                changed_bytes = 0
                for key in set(source_before) | set(source_after):
                    if key not in source_before or key not in source_after:
                        changed_keys.append(key)
                        changed_bytes += (source_before.get(key, source_after.get(key))).size
                    elif not np.array_equal(source_before[key], source_after[key]):
                        changed_keys.append(key)
                        changed_bytes += int(np.count_nonzero(source_before[key] != source_after[key]))
                contact['source_changed_keys'] = sorted(changed_keys)
                contact['source_changed_bytes'] = changed_bytes
                first_second = [row for row in timing_rows if row['contact'] == active_contact
                    and row['name'] in {'canvas.paintEvent', 'canvas.paintGL', 'canvas.begin', 'canvas.begin_admitted', 'presenter.batch'}
                    and (contact['press_at']-started)*1000 <= row['start_ms'] < (contact['press_at']-started)*1000+1000]
                contact['first_second_spans'] = first_second
                assert changed_bytes, f'Contact{active_contact}changed no native source bytes'
                assert not contact['held_drawing_lost'], (
                    f'Contact {active_contact} stopped drawing before the benchmark sent release; '
                    'inspect canvas.end stacks and interruptions.json')
            active_contact = None
            phase = f'{tool.name}-drain'
            settle()
            save_buffers(tool.name.lower())
            read_native_frame(canvas).save(str(args.out / f'{tool.name.lower()}-native.png'))
            if not args.no_pixel_observer:
                for contact in contacts:
                    if contact['tool'] == tool.name:
                        pixel = sample_pixel(contact['witness'])
                        contact['witness_after_exact_drain'] = pixel
                        expected = contact['witness_expected_display']
                        contact['witness_exact_drain_matches'] = (None if expected is None else
                            max(abs(a-b) for a,b in zip(pixel, expected)) <= 24)
            write_json('contacts-progress.json', contacts)
        phase = 'final'
        final_source, final_metadata = save_buffers('final')
        final_frame = read_native_frame(canvas)
        final_frame.save(str(args.out / 'final-native.png'))
        command_count = canvas.command_stack.revision-history
        assert command_count == len(contacts), (command_count, len(contacts))
        assert final_metadata != initial_metadata, 'Workload must actually alter native sources'
        compare = None
        if args.compare_to:
            reference_final = 'final' if args.tools == 'both' else f'raster_{args.tools}'
            with np.load(args.compare_to / f'{reference_final}-source.npz') as baseline:
                identical = set(baseline.files) == set(final_source)
                differences = {}
                for key in set(baseline.files) & set(final_source):
                    a, b = baseline[key], final_source[key]
                    equal = a.shape == b.shape and np.array_equal(a, b)
                    if not equal:
                        differences[key] = {'baseline_bytes': a.size, 'candidate_bytes': b.size,
                                            'changed_bytes': int(np.count_nonzero(a != b)) if a.shape == b.shape else None}
                compare = {'native_source_identical': identical and not differences,
                           'native_source_differences': differences,
                           'native_frame': pixel_difference(QImage(str(args.compare_to / f'{reference_final}-native.png')).convertToFormat(final_frame.format()), final_frame)}
            stages = {}
            for stage in ['initial', *(tool.name.lower() for tool in tools)]:
                with np.load(args.compare_to / f'{stage}-source.npz') as baseline, np.load(args.out / f'{stage}-source.npz') as candidate:
                    different_keys = sorted(set(baseline.files) ^ set(candidate.files))
                    differences = {key: int(np.count_nonzero(baseline[key] != candidate[key]))
                        if baseline[key].shape == candidate[key].shape else None
                        for key in set(baseline.files) & set(candidate.files)
                        if not np.array_equal(baseline[key], candidate[key])}
                frame = QImage(str(args.out / f'{stage}-native.png'))
                stages[stage] = {'native_source_identical': not different_keys and not differences,
                    'native_source_key_difference': different_keys, 'native_source_differences': differences,
                    'native_frame': pixel_difference(QImage(str(args.compare_to / f'{stage}-native.png')).convertToFormat(frame.format()), frame)}
            compare['stages'] = stages
        phase = 'first-dab-witness-proof'
        prove_actual_first_dabs()
        phase = 'undo'
        for _index in range(command_count):
            canvas.command_stack.undo()
        settle()
        if busy_scene is not None:
            busy_scene.undo_modifier_operation()
            settle()
        undo_source, undo_metadata = save_buffers('undo')
        undo_frame = read_native_frame(canvas)
        undo_frame.save(str(args.out / 'undo-native.png'))
        undo_identical = set(undo_source) == set(initial_source) and all(np.array_equal(undo_source[k], initial_source[k]) for k in initial_source)
        undo_model_identical = chapter.to_dict() == initial_model
        assert undo_identical and undo_model_identical
        summary = {}
        for tool in tools:
            group = [row for row in input_rows if row['tool'] == tool.name]
            frames = [row for row in frame_rows if row['phase'] == tool.name]
            batches = [row for row in batch_rows if row['phase'] == tool.name]
            beats = [row['gap_ms'] for row in heartbeat_rows if row['phase'] == tool.name]
            inside_beats = [b['gap_ms'] for a, b in zip(heartbeat_rows, heartbeat_rows[1:])
                            if a['phase'] == b['phase'] == tool.name]
            calls = Counter()
            for batch in batches:
                calls.update(batch['gl'])
            summary[tool.name] = {'inputs': len(group), 'swap_observed_inputs': sum('dispatch_to_swap_ms' in row for row in group),
                'covered_input_swaps': sum(row.get('feedback_covered_at_swap', False) for row in group),
                'all_paint_wall': distribution([row['ms'] for row in frames]),
                'all_paint_outer_wall': distribution([row['ms'] for row in timing_rows
                    if row['phase'] == tool.name and row['name'] == 'canvas.paintEvent']),
                'all_input_to_swap': distribution([row['dispatch_to_swap_ms'] for row in group if 'dispatch_to_swap_ms' in row]),
                'all_schedule_to_swap': distribution([row['schedule_to_swap_ms'] for row in group if 'schedule_to_swap_ms' in row]),
                'input_schedule_lateness': distribution([row['schedule_lateness_ms'] for row in group]),
                'input_handler': distribution([row['handler_ms'] for row in group]),
                'heartbeat_gap': distribution(beats), 'heartbeat_within_phase': distribution(inside_beats),
                'presenter_batches': len(batches),
                'native_draw_calls': calls['glDrawArrays'],
                'presenter_state_queries': sum(value for key, value in calls.items() if key.startswith(('glGet', 'glIs', 'raw_glGet'))),
                'texture_uploads': sum(row['uploads'] for row in batches),
                'geometry_uploads': sum(row['geometry_uploads'] for row in batches),
                'texture_allocations': sum(row['texture_allocations'] for row in batches),
                'texture_reuses': sum(row['texture_reuses'] for row in batches),
                'max_allocated_texture_bytes': max((row['allocated_texture_bytes'] for row in batches), default=0),
                'fallback_batches': sum(not row['result'] for row in batches),
                'fallback_reasons': dict(Counter(row['reason'] for row in batches if row['reason'])),
                'presenter_draw_wall': distribution([row['ms'] for row in batches]),
                'retained_contact_frames': sum(row['continuity_bytes'] > 0 for row in frames),
                'gpu_pacing_token_frames': sum(row.get('gpu_contact_pacing_token_present', False) for row in frames),
                'gpu_pending_frames': sum(row.get('gpu_presentation_pending', False) for row in frames),
                'gpu_pending_retry_token_frames': sum(row.get('gpu_retry_token_present', False) for row in frames),
                'gpu_pending_frame_times_ms': [row['start_ms'] for row in frames if row.get('gpu_presentation_pending', False)],
                'max_continuity_bytes': max((row['continuity_bytes'] for row in frames), default=0),
                'gl': dict(calls)}
        after_files = file_manifest(args.project_copy)
        before_files = json.loads((args.out / 'source-files-before.json').read_text(encoding='utf-8'))
        assert before_files == after_files, 'Private authored files changed despite no save'
        result = {'fixture': fixture, 'python': sys.version, 'canvas': type(canvas).__name__,
            'graphics': graphics, 'gl_version': version, 'gl_format': [actual_format.majorVersion(), actual_format.minorVersion(), actual_format.profile().name],
            'dpr': canvas.devicePixelRatioF(), 'sampling': 'Original source/effect grids; native260px scene tile presentation aliases only',
            'workload': {'contacts_per_tool': args.contacts, 'hold_seconds': args.seconds, 'hz': args.hz,
                         'gap_seconds': args.gap, 'pencil_size_px': args.pencil_size,
                         'eraser_size_px': args.eraser_size, 'opacity_locked': selected.opacity_locked,
                         'predictive_ink': True, 'pencil_preset': pencil_preset, 'eraser_preset': eraser_preset,
                         'authored_foreground': '#ff000000',
                         'observer_colors': [witness['color'] for witness in witnesses],
                         'visible_source_validation': True,
                         'full_size_convergence_tolerance_bytes': 24,
                         'first_dab_witness_tolerance_bytes': 8,
                         'first_dab_witness_contract': 'Actual first-pressure QtCOW source; post-timing ordinary full native block;7x7 swap patch',
                         'first_dab_capture_byte_limit': 4*1024*1024,
                         'swap_pixel_trace_limit_per_contact': 512,
                         'pressure_assumption': 'Synthetic smooth0.2to0.95pressure; original packets unavailable',
                         'no_pixel_observer': args.no_pixel_observer, 'pressure_tiles': args.pressure_tiles,
                         'cold_source_first': args.cold_source_first, 'busy_pressure_first': args.busy_pressure_first,
                         'busy_known_scene_first': args.busy_known_scene_first,
                         'busy_scene_first': args.busy_scene_first,
                         'pixel_witness_no_cursor': args.pixel_witness_no_cursor},
            'pressure_before': pressure, 'pressure_final': pressure_state(), 'summary': summary,
            'initial_source_tiles': initial_source_tiles,
            'held_contacts_valid': not any(contact['held_drawing_lost'] for contact in contacts),
            'validated_ink_contacts': sum('validated_ink_pixel_change_ms' in contact for contact in contacts),
            'validated_held_ink_contacts': sum(contact.get('validated_ink_pixel_change_while_held', False) for contact in contacts),
            'legacy_validated_ink_fields_meaning': 'Full-size convergence only; these are not first-pressure fresh-ink timings',
            'full_size_convergence_contacts': sum('full_size_convergence_ms' in contact for contact in contacts),
            'first_fresh_ink_contacts': sum('first_fresh_ink_ms' in contact for contact in contacts),
            'first_fresh_held_ink_contacts': sum(contact.get('first_fresh_ink_while_held', False) for contact in contacts),
            'first_fresh_ink_ms': [contact.get('first_fresh_ink_ms') for contact in contacts],
            'first_dab_capture_wall': distribution([contact['first_dab_capture_wall_ms']
                for contact in contacts if 'first_dab_capture_wall_ms' in contact]),
            'first_dab_observer_effect': 'Resident QtCOW retention may cause a later paint detach; capture overhead is included in outer input-handler spans',
            'swap_pixel_trace_dropped': sum(contact['swap_pixel_trace_dropped'] for contact in contacts),
            'filtered_diagnostic_events_dropped': filtered_rows_dropped,
            'unexpected_contact_ends': sum(row['name'] == 'canvas.end' and not row.get('expected_release', True) for row in timing_rows),
            'all_paint_wall_including_release_drain': distribution([row['ms'] for row in frame_rows if row['phase'] not in {'setup', 'pressure-warmup', 'busy-scene-setup', 'first-dab-witness-proof', 'undo'}]),
            'all_paint_outer_wall_including_release_drain': distribution([row['ms'] for row in timing_rows
                if row['name'] == 'canvas.paintEvent' and row['phase'] not in {'setup', 'pressure-warmup', 'busy-scene-setup', 'first-dab-witness-proof', 'undo'}]),
            'pixel_observer_wall': distribution(readback_times), 'observed_swaps': observed_swaps,
            'commands': command_count, 'undo_source_identical': undo_identical, 'undo_model_identical': undo_model_identical,
            'busy_scene': busy_scene.report() if busy_scene is not None else None,
            'undo_native_frame': pixel_difference(initial_frame, undo_frame),
            'private_files_unchanged': before_files == after_files, 'comparison': compare,
            'wall_seconds': time.perf_counter()-started}
        write_json('pressure-before.json', pressure)
        write_json('summary.json', result)
        print(json.dumps(result, indent=2), flush=True)
        if compare is not None:
            assert compare['native_source_identical'] and compare['native_frame']['identical']
            assert all(stage['native_source_identical'] and stage['native_frame']['identical']
                       for stage in compare['stages'].values()), 'Intermediate native source/frame differs from baseline'
    except BaseException as error:
        failure = {'type': type(error).__name__, 'message': str(error), 'phase': phase,
                   'contact': active_contact, 'pending': canvas_pending(canvas)}
        write_json('failure.json', failure)
        raise
    finally:
        timer.stop()
        canvas._draw_tablet_hover = original_hover_painter
        for name, value in (('frames.json', frame_rows), ('batches.json', batch_rows), ('timings.json', timing_rows),
                            ('inputs.json', input_rows), ('heartbeat.json', heartbeat_rows), ('contacts.json', contacts)):
            write_json(name, value)
        write_json('interruptions.json', interruption_rows)
        write_json('filtered-lifecycle.json', filtered_rows)
        if busy_scene is not None:
            write_json('busy-scene.json', busy_scene.report())
            busy_scene.close()
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        worker = getattr(canvas, '_graphics_worker', None)
        if worker is not None:
            worker.close()
        presenter = getattr(canvas, '_document_tile_presenter', None)
        if presenter is not None:
            presenter.close()
        canvas.close()
        canvas.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        for owner, name, original in reversed(original_methods):
            setattr(owner, name, original)
        print('Private native canvas shutdown completed.', flush=True)


if __name__ == '__main__':
    main()
