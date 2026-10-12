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
Separate center-pixel witnesses record actual first visible contact changes.
The same wrappers/readback must be used for baseline and candidate comparisons.
Final native source buffers, complete undo, file hashes, and native framebuffer
images are retained. --compare-to compares all selected source bytes and native
framebuffer bytes with a prior run. A fresh process is required for each run.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import ctypes
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
    args = parser.parse_args()
    if args.busy_known_scene_first:
        args.busy_pressure_first = True
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
    from PySide6.QtGui import QColor, QImage, QInputDevice, QPointingDevice, QSurfaceFormat, QTabletEvent, QTransform
    from PySide6.QtWidgets import QApplication
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.core.pressure import BrushPreset, PressureCurve
    from comic_editor.core.settings import EditorSettings
    import comic_editor.ui.canvas as canvas_module
    from comic_editor.ui.canvas import create_canvas, ToolKind
    from comic_editor.ui.document_presentation import GpuTilePresenter, PresentedTile
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
            if label == 'canvas.end' and active_contact is not None:
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
            try:
                result = original(*values, **kwargs)
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
        # Saved opacity lock remains enabled. Pick seven disjoint visible opaque
        # source neighborhoods so every contact really changes authored pixels.
        local_to_world = canvas._drawing_local_to_world_transform(selected)
        transform = local_to_world * canvas.camera_transform()
        candidates = []
        for key, image in canvas.tiles.object_tiles(args.selected).items():
            rgba = image.convertToFormat(QImage.Format_RGBA8888)
            array = np.frombuffer(rgba.constBits(), np.uint8).reshape(rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4].reshape(rgba.height(), rgba.width(), 4)
            ys, xs = np.nonzero(array[:, :, 3] >= 250)
            for x, y in zip(xs[::4], ys[::4]):
                local = QPointF(key[0]*256.+float(x)+.5, key[1]*256.+float(y)+.5)
                screen = transform.map(local)
                if (120. < screen.x() < canvas.width()-120. and 120. < screen.y() < canvas.height()-120.):
                    candidates.append(local.toTuple())
        assert candidates, 'Selected saved raster has no opaque native pixels under the recorded camera'
        candidates = np.asarray(candidates)
        local_center = local_to_world.inverted()[0].map(QPointF(*args.camera[:2]))
        centers = []
        for index in range(args.contacts):
            if not centers:
                score = -np.sum((candidates-np.asarray(local_center.toTuple()))**2, axis=1)
            else:
                # Farthest-point placement also works for the selected saved
                # raster's sparse circle, which has only five source tiles.
                score = np.min(np.stack([np.sum((candidates-center)**2, axis=1)
                                         for center in centers]), axis=0)
            candidate = candidates[np.argmax(score)]
            assert not centers or math.sqrt(float(np.max(score))) >= 36., 'Need seven disjoint visible native ink neighborhoods'
            centers.append(candidate)
        count = max(2, round(args.seconds*args.hz))
        paths = []
        for center in centers:
            nearby = candidates[np.sum((candidates-center)**2, axis=1) <= 6.**2]
            local_points = []
            for index in range(count+1):
                t = index/count
                wanted = center + (math.sin(t*math.pi*6)*4, math.cos(t*math.pi*4)*2)
                local_points.append(nearby[np.argmin(np.sum((nearby-wanted)**2, axis=1))])
            paths.append([local_to_world.map(QPointF(*point)).toTuple() for point in local_points])
        write_json('paths.json', {'candidates': len(candidates), 'local_centers': [v.tolist() for v in centers],
                                 'world_paths': paths})
        return paths

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

    def sample_pixel(world):
        point = canvas.camera_transform().map(QPointF(*world))
        ratio = canvas.devicePixelRatioF()
        x, y = round(point.x()*ratio), round(point.y()*ratio)
        sample = np.empty((1, 1, 4), np.uint8)
        canvas.makeCurrent()
        try:
            canvas.context().functions().glBindFramebuffer(0x8D40, canvas.defaultFramebufferObject())
            library.glReadPixels(x, round(canvas.height()*ratio)-y-1, 1, 1, 0x1908, 0x1401, sample.ctypes.data)
        finally:
            canvas.doneCurrent()
        return sample[0, 0].tolist()

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
            contact.setdefault('first_swap_ms', (now-contact['press_at'])*1000)
            if covered:
                contact.setdefault('first_covered_swap_ms', (now-contact['press_at'])*1000)
        if active_contact is None or args.no_pixel_observer:
            return
        contact = contacts[active_contact]
        if ('first_pixel_change_ms' in contact and 'native_feedback_pixel_change_ms' in contact) or not input_rows:
            return
        before = time.perf_counter()
        color = sample_pixel(contact['witness'])
        readback_times.append((time.perf_counter()-before)*1000)
        if color != contact['witness_before'] and 'first_pixel_change_ms' not in contact:
            contact['first_pixel_change_ms'] = (now-contact['press_at'])*1000
            contact['first_pixel_color'] = color
            contact['first_pixel_change_while_held'] = bool(canvas._drawing)
            contact['first_pixel_feedback_covered'] = covered
        if color != contact['witness_before'] and covered and 'native_feedback_pixel_change_ms' not in contact:
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
        paths = contact_paths()
        # Capture the witness before any queued pressure. A readback between
        # pressure and first press would silently turn busy storage into idle.
        first_witness_pixel = sample_pixel(paths[0][0]) if not args.no_pixel_observer else None
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
                canvas.primary_color = COLORS[index]
                witness = path[0]
                before_pixel = (first_witness_pixel if active_contact == 0 else sample_pixel(witness)) if not args.no_pixel_observer else None
                contact = {'index': active_contact, 'tool': tool.name, 'ordinal': index,
                    'witness': witness, 'witness_before': before_pixel, 'color': COLORS[index],
                    'pressure_before': pressure_state(), 'history_before': canvas.command_stack.revision,
                    'continuity_bytes_before': canvas._scene_controller.raster_continuity.byte_count,
                    'scene_submitted_before': canvas._scene_controller.scheduler.submitted}
                contact['held_drawing_lost'] = []
                source_before, source_before_meta = source_buffers()
                contact['source_digest_before'] = {key: value['sha256'] for key, value in source_before_meta.items()}
                contacts.append(contact)
                if active_contact == 0 and args.cold_source_first:
                    owner = canvas.tiles._tiles.get(args.selected)
                    assert owner is not None and all(isinstance(owner.entries[key], Path) for key in owner), 'Cold diagnostic requires unchanged disk-backed source tiles'
                    for key in tuple(owner):
                        canvas.tiles.residency.evict(owner, key)
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
                    QCoreApplication.sendEvent(canvas, event)
                    row['handler_ms'] = (time.perf_counter()-received)*1000
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
        phase = 'undo'
        for _index in range(command_count):
            canvas.command_stack.undo()
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
                         'authored_foreground': '#ff000000', 'observer_colors': COLORS,
                         'pressure_assumption': 'Synthetic smooth0.2to0.95pressure; original packets unavailable',
                         'no_pixel_observer': args.no_pixel_observer, 'pressure_tiles': args.pressure_tiles,
                         'cold_source_first': args.cold_source_first, 'busy_pressure_first': args.busy_pressure_first,
                         'busy_known_scene_first': args.busy_known_scene_first},
            'pressure_before': pressure, 'pressure_final': pressure_state(), 'summary': summary,
            'initial_source_tiles': initial_source_tiles,
            'held_contacts_valid': not any(contact['held_drawing_lost'] for contact in contacts),
            'unexpected_contact_ends': sum(row['name'] == 'canvas.end' and not row.get('expected_release', True) for row in timing_rows),
            'all_paint_wall_including_release_drain': distribution([row['ms'] for row in frame_rows if row['phase'] not in {'setup', 'pressure-warmup', 'undo'}]),
            'all_paint_outer_wall_including_release_drain': distribution([row['ms'] for row in timing_rows
                if row['name'] == 'canvas.paintEvent' and row['phase'] not in {'setup', 'pressure-warmup', 'undo'}]),
            'pixel_observer_wall': distribution(readback_times), 'observed_swaps': observed_swaps,
            'commands': command_count, 'undo_source_identical': undo_identical, 'undo_model_identical': undo_model_identical,
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
        for name, value in (('frames.json', frame_rows), ('batches.json', batch_rows), ('timings.json', timing_rows),
                            ('inputs.json', input_rows), ('heartbeat.json', heartbeat_rows), ('contacts.json', contacts)):
            write_json(name, value)
        write_json('interruptions.json', interruption_rows)
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
