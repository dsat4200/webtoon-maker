"""Opt-in blur latency/cache/memory measurements. Run from the repository root."""
import os
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import BlurModifier, BoundGeometry, ChapterDocument, RadialBlurModifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_rendering import BlurPyramidCache, _variable_blur
from comic_editor.ui.radial_blur import radial_blur


def main():
    app = QApplication.instance() or QApplication([])
    source = np.zeros((128, 128, 4), np.float32)
    source[40:90, 70:100] = (.4, .1, .3, .5)
    cache = BlurPyramidCache()
    _variable_blur(source, 8, cache)
    timings = []
    for strength in (9, 12, 15, 18, 20):
        start = time.perf_counter()
        _variable_blur(source, strength, cache)
        timings.append((time.perf_counter()-start)*1000)
    print(f"128px normal Blur warmed edits: median={statistics.median(timings):.2f}ms; pyramid builds={cache.builds}; cache={cache.bytes/1048576:.2f}MiB")
    for angle in (15, 180, 360):
        radial_blur(source, (64, 64), angle, QTransform())
        times = []
        for _ in range(3):
            start = time.perf_counter()
            radial_blur(source, (64, 64), angle, QTransform())
            times.append((time.perf_counter()-start)*1000)
        tracemalloc.start()
        radial_blur(source, (64, 64), angle, QTransform())
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"128px radial {angle}deg: median={statistics.median(times):.2f}ms; traced peak={peak/1048576:.2f}MiB")
    canvas = CanvasWidget(EditorSettings())
    chapter = ChapterDocument(height=360)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 360))
    shape = chapter.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(200, 100, 50, 40))
    modifier = RadialBlurModifier(center=(190, 120), angle=15)
    chapter.add_modifier(modifier, [("layer", shape.layer_id)])
    canvas.set_document(chapter, TileStore())
    image = QImage(1080, 360, QImage.Format_ARGB32_Premultiplied)
    times = []
    for angle in (15, 20, 25, 30, 35):
        modifier.angle = angle
        start = time.perf_counter()
        canvas.render_preview(image)
        times.append((time.perf_counter()-start)*1000)
    print(f"50x40 target radial edits: median={statistics.median(times[1:]):.2f}ms; source stages={len(canvas._modifier_source_cache)}; source cache={canvas._modifier_source_cache_bytes/1048576:.2f}MiB; result cache={canvas._modifier_render_cache_bytes/1048576:.2f}MiB")
    assert len(canvas._modifier_source_cache) == 1
    canvas.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    main()
