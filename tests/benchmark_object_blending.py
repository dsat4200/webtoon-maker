"""No-window blend benchmark: 1080×720 viewport in an 8640px-tall chapter.

Run with ``python tests/benchmark_object_blending.py [--gpu]``. Measures exact
cold composition and warm retained redraw, not input-to-display latency.
"""
import json
import os
from pathlib import Path
import statistics
import sys
import time

USE_GPU = "--gpu" in sys.argv
os.environ.setdefault("QT_QPA_PLATFORM", "windows" if USE_GPU else "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from comic_editor.core.blend_modes import OBJECT_BLEND_MODES
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui import canvas as canvas_module


def main():
    app = QApplication.instance() or QApplication([])
    canvas_module.create_network_manager = lambda *_: None
    canvas = canvas_module.CanvasWidget(EditorSettings(grid_overlay_visible=False, canvas_renderer="gpu" if USE_GPU else "raster"))
    canvas.setFixedSize(1080, 720)
    chapter = ChapterDocument(height=8640, background="#3c78b4")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, chapter.height))
    page.fill_color, page.border_width = None, 0
    obj = chapter.add_object(page.layer_id, RasterObject())
    tiles = TileStore()
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor(150, 90, 30, 210))
    for y in range(34):
        for x in range(5):
            tiles.set_tile(obj.object_id, (x, y), tile)
    canvas.set_document(chapter, tiles)
    canvas.center_x, canvas.center_y, canvas.scale = 540, 360, 1
    canvas._document_projection_enabled = True
    report = []
    for mode, label in OBJECT_BLEND_MODES:
        obj.blend_mode = mode
        cold = []
        for _ in range(3):
            canvas.documentChanged.emit(None)
            start = time.perf_counter()
            canvas._ensure_scene_cache()
            cold.append((time.perf_counter() - start) * 1000)
        renders = canvas._document_projection.renders
        warm = []
        for _ in range(5):
            canvas._invalidate_scene_cache(projection=False)
            start = time.perf_counter()
            canvas._ensure_scene_cache()
            warm.append((time.perf_counter() - start) * 1000)
        # Moving within the already-captured guard band needs no new source.
        canvas.center_y += 12
        canvas._invalidate_scene_cache(projection=False)
        canvas._ensure_scene_cache()
        assert canvas._document_projection.renders == renders, "Warm redraw/pan repeated composition"
        canvas.center_y -= 12
        report.append({"mode": label, "cold_ms": round(statistics.median(cold), 2),
                       "warm_ms": round(statistics.median(warm), 2), "warm_recaptures": 0})
    blend_renderer = getattr(canvas, "_gpu_object_blend_renderer", None)
    print(json.dumps({"viewport": [1080, 720], "chapter_height": 8640,
                      "gpu": bool(blend_renderer and blend_renderer.available), "results": report}, indent=2))
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    main()
