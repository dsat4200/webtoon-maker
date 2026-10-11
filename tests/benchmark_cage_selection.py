"""Measure cage startup from a read-only saved project copy.

Run with --project-copy under this checkout's .artifacts and --output there.
The benchmark uses a hidden native Canvas and writes only its evidence file.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project-copy', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--repeats', type=int, default=3)
args = parser.parse_args()
project, output = args.project_copy.resolve(), args.output.resolve()
assert project.is_relative_to(ROOT/'.artifacts') and (project/'series.json').is_file()
assert output.is_relative_to(ROOT/'.artifacts') and not output.exists()
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'windows' if os.name == 'nt' else 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from comic_editor.core.models import RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.render import input_capture
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def manifest():
    return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in project.rglob('*') if path.is_file()}


app = QApplication.instance() or QApplication([])
before = manifest()
repository = SeriesRepository(project)
chapter_id = '0a72f08009294aa0a3d14e6a38e22bbb'
rows = []
for repeat in range(args.repeats):
    chapter, tiles, images = repository.load_chapter(chapter_id, include_images=True)
    obj = max((record for record in chapter.objects.values() if isinstance(record, RasterObject)),
              key=lambda record: len(tiles._tiles.get(record.object_id, {})))
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', snap_to_grid=False))
    canvas.resize(955, 927)
    canvas.set_document(chapter, tiles, images)
    canvas.set_selection('object', obj.object_id)
    canvas.center_x, canvas.center_y, canvas.scale = 540, 32642.3076923, .13
    phases = {}
    original = input_capture.cage_initial
    def initial(snapshot, *arguments, **keywords):
        started = time.perf_counter()
        phases['captured_objects'] = len(snapshot.chapter.objects)
        phases['captured_tiles'] = sum(len(owner) for owner in snapshot.tiles._tiles.values())
        phases['source_pin_records'] = sum(len(job.records) for job in snapshot.pin_jobs)
        result = original(snapshot, *arguments, **keywords)
        phases['worker_ms'] = (time.perf_counter()-started)*1000
        return result
    initial.accepts_cancelled = True
    for attribute in ('source_capture', 'scene_capture_scope', 'working_bytes', 'snapshot_working_bytes'):
        if hasattr(original, attribute):
            setattr(initial, attribute, getattr(original, attribute))
    input_capture.cage_initial = initial
    started = time.perf_counter()
    try:
        assert canvas.set_tool(ToolKind.CAGE_TRANSFORM)
        assert canvas._cage_prepare is not None
        slices = []
        deadline = time.monotonic()+60
        while canvas._cage_session is None and time.monotonic() < deadline:
            tick = time.perf_counter()
            QCoreApplication.processEvents()
            slices.append((time.perf_counter()-tick)*1000)
            time.sleep(.001)
        assert canvas._cage_commit_error is None, canvas._cage_commit_error
        assert canvas._cage_session is not None
        session = canvas._cage_session
        phases.update(repeat=repeat, startup_ms=(time.perf_counter()-started)*1000,
            max_event_pump_ms=max(slices), selected_name=obj.name, selected_id=obj.object_id,
            selected_tiles=len(tiles._tiles[obj.object_id]), grid_frame=list(session['grid'].frame),
            original_formats=sorted({str(image.format()) for image in session['before_tiles'][obj.object_id].values()}))
        assert session['before_models'][obj.object_id].to_dict() == obj.to_dict()
        for key, image in session['before_tiles'][obj.object_id].items():
            assert image == tiles.tile(obj.object_id, key)
        assert canvas.finish_cage(False)
        assert not canvas.command_stack.can_undo
        rows.append(phases)
    finally:
        input_capture.cage_initial = original
        canvas._scene_consumers.shutdown()
        canvas.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
assert manifest() == before
report = dict(rows=rows, project_files=len(before), input_files_unchanged=True,
              chapter_size=[chapter.width, chapter.height], platform=os.environ['QT_QPA_PLATFORM'])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2), encoding='utf8')
print(json.dumps(report, indent=2))
