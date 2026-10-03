"""Profile storage on an isolated multi-chapter project copy.

The source must be under .artifacts. Saves use a second sandbox and never alter
the input copy. --source-code allows an archived checkout for before/after runs.
"""
from pathlib import Path
import argparse
import cProfile
import hashlib
import json
import os
import shutil
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--project-copy', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--source-code', type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
project = args.project_copy.resolve()
output = args.output.resolve()
assert project.is_relative_to(root / '.artifacts') and (project / 'series.json').is_file()
assert output.is_relative_to(root / '.artifacts') and not output.exists()
output.mkdir(parents=True)
sys.path.insert(0, str(args.source_code.resolve() if args.source_code else root))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor
from comic_editor.core.models import RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.performance_resources import ProcessResourceSampler

def manifest(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file()}

before = manifest(project)
sampler = ProcessResourceSampler()
rows = []
def measure(name, work):
    start = time.perf_counter()
    value = work()
    rows.append({'name': name, 'milliseconds': (time.perf_counter()-start)*1000,
                 'resources': sampler.sample()})
    return value

profile = cProfile.Profile()
profile.enable()
repository = SeriesRepository(project)
series = repository.load_series()
loaded = []
for reference in series.chapters:
    chapter, tiles, images = measure('load:'+reference.chapter_id,
        lambda ref=reference: repository.load_chapter(ref.chapter_id, include_images=True))
    measure('content-bounds:'+reference.chapter_id,
        lambda: [tiles.content_bounds(oid) for oid in chapter.objects])
    loaded.append((chapter, tiles, images))
sandbox = output / 'save-sandbox'
shutil.copytree(project, sandbox)
saver = SeriesRepository(sandbox)
chapter, tiles, images = max(loaded, key=lambda item: len(item[0].objects))
measure('unchanged-save', lambda: saver.save_chapter(chapter, tiles, images))
chapter.name += ' storage benchmark'
measure('metadata-save', lambda: saver.save_chapter(chapter, tiles, images))
obj = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
tiles.paint_dab(obj.object_id, QPointF(128,128), 12, QColor('#8734a8'))
measure('one-dab-save', lambda: saver.save_chapter(chapter, tiles, images))
measure('autosave', lambda: saver.save_chapter(chapter, tiles, images, autosave=True))
measure('reopen', lambda: saver.load_chapter(chapter.chapter_id, include_images=True))
profile.disable()
profile.dump_stats(str(output / 'storage.prof'))
assert manifest(project) == before
report = {'source_code': str(args.source_code or root), 'measurements': rows,
          'input_files_unchanged': True,
          'chapters': [{'id': c.chapter_id, 'layers': len(c.layers), 'objects': len(c.objects),
                        'modifiers': len(c.modifiers), 'masks': len(c.masks)} for c,t,i in loaded]}
(output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
