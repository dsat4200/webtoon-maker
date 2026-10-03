"""Compare recovery capture/write latency on an isolated real project."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project-copy', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--source-code', type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
project, output = args.project_copy.resolve(), args.output.resolve()
assert project.is_relative_to(root / '.artifacts') and (project / 'series.json').is_file()
assert output.is_relative_to(root / '.artifacts') and not output.exists()
output.mkdir(parents=True)
sys.path.insert(0, str(args.source_code.resolve() if args.source_code else root))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
from comic_editor.core.models import RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui.autosave import RecoverySnapshot

def manifest(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file()}

app = QApplication.instance() or QApplication([])
before = manifest(project)
repository = SeriesRepository(project)
loaded = [repository.load_chapter(ref.chapter_id, include_images=True)
          for ref in repository.load_series().chapters]
chapter, tiles, images = max(loaded, key=lambda item: len(item[0].objects))
pin_preparation_ms = None
if hasattr(tiles, '_snapshot_future') and tiles._snapshot_future is not None:
    # Let ordinary idle GUI events run while the prefetch started by open
    # completes. Recovery is normally requested long after this setup.
    started = time.perf_counter()
    while not tiles._snapshot_future.done():
        app.processEvents()
        time.sleep(.001)
    tiles.finish_snapshot_prefetch()
    pin_preparation_ms = (time.perf_counter() - started) * 1000
destination = SeriesRepository(output / 'recovery-sandbox')
rows, previous = [], None
with ThreadPoolExecutor(max_workers=1) as worker:
    for name in ('initial', 'metadata-only', 'one-dab', 'metadata-repeat'):
        chapter.name = 'Recovery benchmark ' + name
        if name == 'one-dab':
            obj = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
            tiles.paint_dab(obj.object_id, QPointF(128, 128), 12, QColor('#8734a8'))
        started = time.perf_counter()
        snapshot = RecoverySnapshot.capture(destination.root, chapter, tiles, images)
        capture_ms = (time.perf_counter() - started) * 1000
        if previous is not None and hasattr(snapshot, 'saved_tiles'):
            snapshot.saved_tiles, snapshot.saved_images = previous.saved_tiles, previous.saved_images
        ticks = [time.perf_counter()]
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
        timer.start()
        started = time.perf_counter()
        future = worker.submit(snapshot.write)
        while not future.done():
            app.processEvents()
            # Native idle event loops release the GIL. QTest.qWait can retain
            # it in this binding, artificially starving the writer thread.
            time.sleep(.001)
        future.result()
        if hasattr(tiles, 'adopt_snapshot_bounds'):
            tiles.adopt_snapshot_bounds(snapshot.derived_bounds)
        elapsed = (time.perf_counter() - started) * 1000
        timer.stop()
        gaps = [(right - left)*1000 for left, right in zip(ticks, ticks[1:])]
        recovered, saved_tiles = destination.load_chapter(chapter.chapter_id, recover=True)
        assert recovered.to_dict() == chapter.to_dict()
        if name == 'one-dab':
            assert saved_tiles.tile(obj.object_id, (0, 0)) == tiles.tile(obj.object_id, (0, 0))
        rows.append({'operation': name, 'capture_ms': capture_ms, 'write_ms': elapsed,
                     'heartbeat_max_ms': max(gaps, default=0),
                     'live_tile_decodes': getattr(getattr(tiles, 'residency', None), 'decodes', None)})
        previous = snapshot
assert manifest(project) == before
report = {'source_code': str(args.source_code or root), 'input_files_unchanged': True,
          'background_pin_preparation_ms': pin_preparation_ms,
          'chapter': {'objects': len(chapter.objects), 'layers': len(chapter.layers),
                      'modifiers': len(chapter.modifiers), 'masks': len(chapter.masks)},
          'measurements': rows}
(output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
