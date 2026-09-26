"""Real engine event latency and tile-memory benchmark; run as a module/script."""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QColor
from comic_editor.core.brushes import BrushInput, default_brushes
from comic_editor.core.brush_raster import RasterBrushStroke, _material_cache
from comic_editor.core.tiles import TileStore


def main():
    results = []
    for brush in default_brushes():
        store = TileStore()
        samples = [BrushInput(100+i*8,128+40*math.sin(i/14),
                             pressure=.25+.75*math.sin(math.pi*i/80),time=i/120)
                   for i in range(81)]
        before = {}
        stroke = RasterBrushStroke(store,"paint",brush,QColor("#385b92"),before,seed=5)
        started = time.perf_counter()
        stroke.begin(samples[0])
        times = []
        peak_working = 0
        for sample in samples[1:]:
            at = time.perf_counter()
            stroke.add(sample)
            times.append((time.perf_counter()-at)*1000)
            peak_working = max(peak_working,stroke.main.bytes+stroke._base_bytes+
                               (stroke.secondary.bytes if stroke.secondary else 0))
        at = time.perf_counter()
        stroke.finish()
        finish = (time.perf_counter()-at)*1000
        results.append({"brush":brush.name,"median_event_ms":round(statistics.median(times),2),
                        "p95_event_ms":round(sorted(times)[int(len(times)*.95)-1],2),
                        "finish_ms":round(finish,2),
                        "total_ms":round((time.perf_counter()-started)*1000,2),
                        "peak_working_mib":round(peak_working/1024**2,2),
                        "tiles":len(store.object_tiles("paint"))})
    print(json.dumps({"material_cache_mib":round(_material_cache.bytes/1024**2,2),
                      "brushes":results},indent=2))


if __name__ == "__main__":
    main()
