"""Run with `python tests/benchmark_scene_culling.py` for a viewport comparison."""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import BoundGeometry, ChapterDocument, TextObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


def main():
    app = QApplication.instance() or QApplication([])
    document = ChapterDocument(height=180000)
    page = document.add_page("Long chapter", BoundGeometry.rectangle(0, 0, 1080, 180000))
    page.fill_color, page.border_width = None, 0
    for index in range(1000):
        layer = document.add_layer(page.layer_id, f"Panel {index}",
            BoundGeometry.rectangle(20, index * 180 + 10, 500, 140))
        layer.fill_color, layer.border_width = "#fffafa", 3
        document.add_object(layer.layer_id, TextObject(
            text=f"Panel {index}: unchanged dialogue", layout_mode="strict"))
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.resize(800, 600)
    canvas.set_document(document, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 400, 300, 1
    canvas._ensure_scene_cache()
    prepare = canvas._render_bounds.prepare

    def measure(enabled, dirty):
        def selected_mode():
            prepare()
            canvas._render_bounds.enabled = enabled
        canvas._render_bounds.prepare = selected_mode
        samples = []
        for iteration in range(14):
            started = time.perf_counter()
            canvas._render_scene_cache_rect(dirty)
            elapsed = (time.perf_counter() - started) * 1000
            if iteration >= 4:
                samples.append(elapsed)
        return round(statistics.median(samples), 3)

    result = {"layers": len(document.layers), "objects": len(document.objects)}
    for label, dirty in (("viewport_ms", canvas.rect()), ("stroke_dirty_ms", QRect(50, 60, 35, 35))):
        result[label] = {"unculled": measure(False, dirty), "culled": measure(True, dirty)}
    print(json.dumps(result, indent=2))
    canvas._render_bounds.prepare = prepare
    canvas.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    main()
