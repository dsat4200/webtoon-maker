"""Matched original/candidate native Brush packet CPU timings.

This measures accepted live engine publication, excluding physical input, Qt
dispatch and display. The independent original grid/color/envelope primitives
retain the same dabs, materials, RNG and float precision. Runs alternate owners
to reduce order bias, and final complete native buffers must match bit for bit.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QColor, QImage
import numpy as np

from comic_editor.core import brush_raster
from comic_editor.core.brushes import BrushDefinition, BrushInput, default_brushes
from comic_editor.core.brush_raster import RasterBrushStroke, prepare_brush_materials
from comic_editor.core.brush_stroke import BrushStroke
from comic_editor.core.tiles import TileStore
from references.brush_native_primitives_20261008 import NativeBrushStroke, NativeRasterBrushStroke


@contextmanager
def original_primitives(enabled):
    previous = brush_raster.RasterBrushStroke, brush_raster.BrushStroke
    if enabled:
        brush_raster.RasterBrushStroke, brush_raster.BrushStroke = NativeRasterBrushStroke, NativeBrushStroke
    try:
        yield brush_raster.RasterBrushStroke
    finally:
        brush_raster.RasterBrushStroke, brush_raster.BrushStroke = previous


def benchmark(iterations):
    points = [BrushInput(80+index*16, 128+10*math.sin(index*.23), time=index/120)
              for index in range(40)]
    builtins = {brush.id: brush for brush in default_brushes()}
    brushes = {'dense_8px': BrushDefinition(size=8, antialiasing=0, spacing=.08),
               'soft_24px': BrushDefinition(size=24, hardness=.4, spacing=.08, opacity=.63),
               'dual_16px': BrushDefinition(size=16, spacing=.08, opacity=.57,
                   dual=BrushDefinition(size=9, hardness=.4, spacing=.13)),
               'authored_color': replace(builtins['color-stamp'], size=16, spacing=.08)}
    results = []
    for name, brush in brushes.items():
        materials = prepare_brush_materials(brush)
        for native in (False, True):
            timings = {'original': [], 'candidate': []}
            digests = {}
            for run in range(iterations):
                for original in ((True, False) if run % 2 == 0 else (False, True)):
                    with original_primitives(original) as kind:
                        store = TileStore()
                        image = QImage(256, 256, QImage.Format_RGBA64_Premultiplied if native
                                       else QImage.Format_ARGB32_Premultiplied)
                        image.fill(QColor('#713A5C81'))
                        for key in ((0, 0), (1, 0), (2, 0)):
                            store.set_tile('paint', key, QImage(image))
                        stroke = kind(store, 'paint', brush, QColor('#C05C92'), {}, seed=721, materials=materials)
                        stroke.begin(points[0])
                        label = 'original' if original else 'candidate'
                        for point in points[1:]:
                            started = time.perf_counter()
                            stroke.add(point)
                            timings[label].append((time.perf_counter()-started)*1000)
                        stroke.finish()
                        digest = hashlib.sha256()
                        for key, tile in sorted(store.iter_tiles('paint')):
                            digest.update(str((key, tile.format().name)).encode())
                            digest.update(bytes(tile.colorSpace().iccProfile()))
                            digest.update(tile.constBits())
                        current = digest.hexdigest()
                        if label in digests:
                            assert digests[label] == current, 'Repeated brush output changed'
                        digests[label] = current
            assert digests['original'] == digests['candidate'], 'Original native output differs'
            summary = {label: {'median_ms': statistics.median(values),
                'p95_ms': float(np.percentile(values, 95)), 'max_ms': max(values), 'packets': len(values)}
                for label, values in timings.items()}
            results.append({'brush': name, 'source': 'RGBA64' if native else 'ARGB32',
                'summary': summary, 'exact_sha256': digests['candidate'], 'timings_ms': timings})
    return {'scope': 'CPU native Brush add/publication; 40-point 120Hz authored path; no GPU/display timing',
            'iterations': iterations, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=12)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('--iterations must be positive')
    result = benchmark(args.iterations)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'scope': result['scope'], 'iterations': result['iterations'],
        'results': [{key: row[key] for key in ('brush', 'source', 'summary', 'exact_sha256')}
                    for row in result['results']]}, indent=2))
