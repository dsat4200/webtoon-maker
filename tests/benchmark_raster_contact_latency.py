"""Native synthetic contact-to-frame probe, without desktop automation.

Run in a fresh process with the application's Python runtime. This uses the
ordinary canvas and detached renderer on a 70,000-pixel chapter. Timings run
from the native packet handler to Qt's frame swap, excluding tablet-driver and display scanout
latency. Framebuffer inspection follows the swap timestamp and adds observation
overhead; use the same observer for a baseline/candidate comparison.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runtime-root', type=Path, default=Path(__file__).resolve().parents[1])
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--renderer', choices=('gpu', 'raster'), default='gpu')
parser.add_argument('--points', type=int, default=40)
parser.add_argument('--zoom', type=float, default=1.)
args = parser.parse_args()
if not 2 <= args.points <= 40 or not .1 <= args.zoom <= 1.:
    parser.error('Use 2–40 points and a zoom from 0.1 to 1.0')
sys.path.insert(0, str(args.runtime_root.resolve()))
os.environ['QT_QPA_PLATFORM'] = 'windows' if sys.platform == 'win32' else 'xcb'

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QSurfaceFormat, QTransform
from PySide6.QtWidgets import QApplication
import numpy as np

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
import comic_editor.ui.canvas as canvas_module
from comic_editor.ui.canvas import create_canvas, ToolKind


fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.CoreProfile)
fmt.setSamples(0)
fmt.setSwapInterval(0)
QSurfaceFormat.setDefaultFormat(fmt)
app = QApplication([])
canvas_module.create_network_manager = lambda *_: None
canvas = create_canvas(EditorSettings(canvas_renderer=args.renderer,
    predictive_ink=False, grid_overlay_visible=False, snap_to_grid=False,
    pencil_size_px={'small': 8, 'medium': 8, 'large': 8},
    eraser_size_px={'small': 8, 'medium': 8, 'large': 8}))
canvas.setFixedSize(900, 700)
canvas.setAttribute(Qt.WA_ShowWithoutActivating)
chapter = ChapterDocument(width=1080, height=70000, background='#ffffffff')
page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 1080, 70000))
page.fill_color, page.border_width = None, 0
selected = chapter.add_object(page.layer_id, RasterObject())
selected.transform_frame = (0., 0., 1024., 1024.)
mapping = QTransform().translate(30.25, 5.5).rotate(-3.)
selected.transform_quad = [mapping.map(QPointF(x, y)).toTuple()
    for x, y in ((0, 0), (1024, 0), (1024, 1024), (0, 1024))]
canvas.set_document(chapter, TileStore())
canvas.center_x, canvas.center_y, canvas.scale = 540., 400., args.zoom
canvas.primary_color = '#ffff0000'
canvas.set_selection('object', selected.object_id)
canvas.show()


def pump(seconds):
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(.001)


def settle():
    canvas.update()
    deadline = time.perf_counter() + 15.
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(.001)
        controller = canvas._scene_controller
        if controller.error:
            raise AssertionError(controller.error)
        document = canvas._render_document_state()
        if (controller.capture is None and not controller.scheduler.busy
                and controller.snapshot is not None and controller.snapshot.document == document
                and not canvas._projection_frame_pending):
            pump(.025)
            return
    raise AssertionError('Exact scene did not settle')


def source_digest():
    return {str(key): hashlib.sha256(bytes(image.constBits())).hexdigest()
        for key, image in canvas.tiles.object_tiles(selected.object_id).items()}


rows, pending, active = [], [], None
observed_frames = 0
gpu = hasattr(canvas, 'frameSwapped')
graphics = None
library = None


def observe():
    global observed_frames
    if active is None or not pending:
        return
    observed_frames += 1
    swapped = time.perf_counter()
    if gpu:
        density = canvas.devicePixelRatioF()
        positions = [canvas.camera_transform().map(QPointF(*row['world'])) for row in pending]
        points = [(round(point.x()*density), round(point.y()*density)) for point in positions]
        left, top = min(x for x, _y in points), min(y for _x, y in points)
        width = max(x for x, _y in points)-left+1
        height = max(y for _x, y in points)-top+1
        native_height = round(canvas.height()*density)
        sample = np.empty((height, width, 4), np.uint8)
        canvas.makeCurrent()
        try:
            canvas.context().functions().glBindFramebuffer(0x8D40, canvas.defaultFramebufferObject())
            library.glReadPixels(left, native_height-top-height, width, height,
                0x1908, 0x1401, sample.ctypes.data)
        finally:
            canvas.doneCurrent()
    else:
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        canvas.render(image)
        swapped = time.perf_counter()
        density = image.devicePixelRatio()
    for index, row in enumerate(tuple(pending)):
        if gpu:
            x, y = points[index]
            color = QColor(*(int(channel) for channel in sample[height-1-(y-top), x-left]))
        else:
            point = canvas.camera_transform().map(QPointF(*row['world']))
            color = image.pixelColor(round(point.x()*density), round(point.y()*density))
        expected = QColor('white' if active == ToolKind.RASTER_ERASER else 'red')
        if color == expected:
            row['current_frame_ms'] = (swapped-row['received'])*1000
            row['prepared_feedback'] = canvas._raster_feedback_contact_covered
            pending.remove(row)


if gpu:
    canvas.frameSwapped.connect(observe)
    settle()
    canvas.makeCurrent()
    try:
        library = ctypes.WinDLL('opengl32')
        library.glGetString.restype = ctypes.c_char_p
        library.glReadPixels.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        graphics = library.glGetString(0x1F01).decode()
    finally:
        canvas.doneCurrent()
else:
    settle()

try:
    for tool in (ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH):
        canvas.set_tool(tool)
        settle()
        original = source_digest()
        history = canvas.command_stack.revision
        submitted = canvas._scene_controller.scheduler.submitted
        active = tool
        count = args.points
        started = time.perf_counter()
        for index in range(count):
            due = started + index/120.
            pump(max(0., due-time.perf_counter()))
            point = QPointF(200.+index*16., 450. if tool == ToolKind.BRUSH else 350.)
            row = {'tool': tool.name, 'index': index, 'world': point.toTuple(),
                'received': time.perf_counter(), 'schedule_lateness_ms': max(0., time.perf_counter()-due)*1000}
            rows.append(row)
            pending.append(row)
            if tool == ToolKind.BRUSH:
                if index == 0:
                    canvas._begin_paint_brush(point, 1.,
                        _definition=BrushDefinition(size=8, antialiasing=0, spacing=.08))
                else:
                    canvas._continue_paint_brush(point, 1.)
            elif index == 0:
                canvas._begin_stroke(point, 1.)
            else:
                canvas._continue_stroke(point, 1.)
            row['handler_ms'] = (time.perf_counter()-row['received'])*1000
            if not gpu:
                observe()
        pump(.25)
        held_submissions = canvas._scene_controller.scheduler.submitted-submitted
        for row in rows[-count:]:
            row['held_scene_submissions'] = held_submissions
        active = None
        if tool == ToolKind.BRUSH:
            canvas._finish_paint_brush()
        else:
            canvas._end_stroke()
        settle()
        assert canvas.command_stack.revision == history+1
        # Keep pencil pixels for the subsequent eraser; verify its undo restores
        # the complete source rather than only the observed center samples.
        if tool != ToolKind.RASTER_PENCIL:
            canvas.command_stack.undo()
            settle()
            assert source_digest() == original
        pending.clear()
    summary = {}
    for tool in (ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH):
        group = [row for row in rows if row['tool'] == tool.name]
        latencies = [row['current_frame_ms'] for row in group if 'current_frame_ms' in row]
        summary[tool.name] = {'inputs': len(group), 'current_while_held': len(latencies),
            'prepared_frames': sum(row.get('prepared_feedback', False) for row in group),
            'within_16_7_ms': sum(value <= 16.7 for value in latencies),
            'median_ms': float(np.median(latencies)) if latencies else None,
            'p95_ms': float(np.percentile(latencies, 95)) if latencies else None,
            'max_ms': max(latencies, default=None),
            'schedule_lateness_p95_ms': float(np.percentile([row['schedule_lateness_ms'] for row in group], 95)),
            'schedule_lateness_max_ms': max(row['schedule_lateness_ms'] for row in group),
            'handler_p95_ms': float(np.percentile([row['handler_ms'] for row in group], 95)),
            'handler_max_ms': max(row['handler_ms'] for row in group),
            'held_scene_submissions': group[0]['held_scene_submissions']}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    result = {'runtime': str(args.runtime_root.resolve()), 'python': sys.version,
        'canvas': type(canvas).__name__, 'graphics': graphics, 'dpr': canvas.devicePixelRatioF(),
        'zoom': args.zoom,
        'sampling': 'native document tiles; saved -3 degree rotation; synthetic 120 Hz contacts',
        'observed_frames': observed_frames, 'summary': summary, 'inputs': rows}
    args.out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('canvas', 'graphics', 'dpr', 'observed_frames', 'summary')}, indent=2), flush=True)
finally:
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
    import gc
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    print('Native canvas shutdown completed.', flush=True)
