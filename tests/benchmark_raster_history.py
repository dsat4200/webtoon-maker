"""Edit, undo, redo and publish actual tiles from an isolated Pocket Boyfriend copy."""
from pathlib import Path
import argparse
import hashlib
import json
import os
from statistics import median
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project-copy', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
project, output = args.project_copy.resolve(), args.output.resolve()
assert project.is_relative_to(root / '.artifacts') and (project/'series.json').is_file()
assert output.is_relative_to(root / '.artifacts') and not output.exists()
output.mkdir(parents=True)
sys.path.insert(0, str(root))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPointF
from PySide6.QtGui import QColor, QImage
from comic_editor.core.commands import CommandStack, TilePatchCommand
from comic_editor.core.models import RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tile_history import TileHistoryCache

def manifest():
    return {str(p.relative_to(project)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in project.rglob('*') if p.is_file()}

def serialized_reference(image):
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, 'PNG')
    buffer.close()
    return QImage.fromData(payload).convertToFormat(QImage.Format_ARGB32_Premultiplied)

before_files = manifest()
repository = SeriesRepository(project)
loaded = [repository.load_chapter(ref.chapter_id, include_images=True)
          for ref in repository.load_series().chapters]
chapter, tiles, images = max(loaded, key=lambda item: len(item[0].objects))
obj = max((obj for obj in chapter.objects.values() if isinstance(obj, RasterObject)),
          key=lambda obj: len(tiles._tiles.get(obj.object_id, {})))
keys = sorted(tiles._tiles[obj.object_id])[:40]
assert len(keys) >= 20
tiles.finish_snapshot_prefetch()
original = tiles.detached_snapshot({obj.object_id})
tiles.residency.budget = 4*1024*1024
tiles.residency.clear()
tiles._history_cache = TileHistoryCache(4*1024*1024)
stack, rows = CommandStack(), []
for index, key in enumerate(keys):
    start = time.perf_counter()
    before = tiles.snapshot(obj.object_id, {key})
    tiles.paint_dab(obj.object_id, QPointF(key[0]*256+128, key[1]*256+128),
                    19, QColor('#8734a8'), before={})
    after = tiles.snapshot(obj.object_id, {key})
    stack.push(TilePatchCommand('Test stroke', tiles, obj.object_id, before, after), already_done=True)
    rows.append({'edit_ms': (time.perf_counter()-start)*1000,
                 'resident_bytes': tiles.residency.bytes, 'history_bytes': tiles._history_cache.bytes})
    assert tiles.residency.bytes <= tiles.residency.budget
    assert tiles._history_cache.bytes <= tiles._history_cache.budget
painted = tiles.detached_snapshot({obj.object_id})
for index in range(len(keys)):
    start = time.perf_counter()
    stack.undo()
    rows[-index-1]['undo_ms'] = (time.perf_counter()-start)*1000
for key in keys:
    assert tiles.tile(obj.object_id, key) == original.tile(obj.object_id, key)
for index in range(len(keys)):
    start = time.perf_counter()
    stack.redo()
    rows[index]['redo_ms'] = (time.perf_counter()-start)*1000
for key in keys:
    assert tiles.tile(obj.object_id, key) == painted.tile(obj.object_id, key)
chapter.name += ' isolated history verification'
saver = SeriesRepository(output/'saved')
saver.create('Isolated history verification')
saver.save_chapter(chapter, tiles, images)
reopened, saved = saver.load_chapter(chapter.chapter_id)
assert reopened.to_dict() == chapter.to_dict()
for key in keys:
    assert saved.tile(obj.object_id, key) == serialized_reference(painted.tile(obj.object_id, key))
assert manifest() == before_files
report = {'chapter': {'layers':len(chapter.layers), 'objects':len(chapter.objects),
                      'modifiers':len(chapter.modifiers), 'masks':len(chapter.masks)},
          'edited_existing_tiles':len(keys), 'object_id':obj.object_id,
          'timings': {name: {'median_ms':median(r[name] for r in rows), 'max_ms':max(r[name] for r in rows)}
                      for name in ('edit_ms','undo_ms','redo_ms')},
          'max_resident_bytes':max(r['resident_bytes'] for r in rows),
          'max_history_bytes':max(r['history_bytes'] for r in rows),
          'private_tile_spills':tiles.residency._spill_cache.spills,
          'history_spills':tiles._history_cache.spills,
          'undo_redo_exact': True, 'reopened_matches_existing_png_semantics':True,
          'input_files_unchanged':True}
(output/'report.json').write_text(json.dumps(report,indent=2), encoding='utf-8')
print(json.dumps(report,indent=2))
