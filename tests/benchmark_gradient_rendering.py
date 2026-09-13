"""Run `python tests/benchmark_gradient_rendering.py` for gradient kernel timings.

Measures changed endpoints and cache hits for the existing color-preview grids,
plus full-resolution mask sampling. No preferences, projects or drawings are
loaded or saved; all geometry and ramps are synthetic.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, ColorGradientRamp,
    ColorGradientStop, LineGradientField, PathNode,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


def measure(canvas, obj, render, repetitions=7):
    times = []
    for index in range(repetitions):
        obj.line_field.geometry.nodes[-1].x += 3.25
        start = time.perf_counter()
        result = render()
        times.append((time.perf_counter() - start) * 1000)
    return {
        "first_ms": round(times[0], 3),
        "median_changed_endpoint_ms": round(statistics.median(times[1:]), 3),
    }


def main():
    app = QApplication.instance() or QApplication([])
    chapter = ChapterDocument(height=2000)
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 1080, 810))
    obj = ColorFillGradientObject(
        line_field=LineGradientField(BoundGeometry.path([
            PathNode(x=360, y=405), PathNode(x=760, y=580),
        ])),
        ramp=ColorGradientRamp(stops=[
            ColorGradientStop(position=0, color="#001030FF"),
            ColorGradientStop(position=.4, color="#A060FF40"),
            ColorGradientStop(position=.6, color="#FFC08020"),
            ColorGradientStop(position=1, color="#FF001000"),
        ]),
    )
    chapter.add_object(page.layer_id, obj)
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    rows = []
    for shape in ("linear", "circular"):
        obj.gradient_shape = shape
        for active in (True, False):
            canvas._gradient_preview_active = active
            canvas._gradient_geometry_cache.clear()
            canvas._gradient_scalar_cache.clear()
            canvas._gradient_render_cache.clear()
            bounds = QRectF(0, 0, 1080, 810)
            def render_color():
                return canvas._line_gradient_image(
                    obj, canvas.bound_path(obj.line_field.geometry), bounds,
                )
            row = {"kind": "color", "shape": shape,
                   "grid": list(canvas._gradient_grid_for_preview(bounds)),
                   "active_edit": active}
            row.update(measure(canvas, obj, render_color))
            hits = []
            for _ in range(20):
                start = time.perf_counter()
                render_color()
                hits.append((time.perf_counter() - start) * 1000)
            row["median_cached_ms"] = round(statistics.median(hits), 3)
            rows.append(row)
        for width, height in ((1024, 768), (2048, 1536)):
            mapping = QTransform.fromScale(width / 1080, height / 810)
            row = {"kind": "mask", "shape": shape, "grid": [width, height]}
            row.update(measure(canvas, obj, lambda: canvas._render_mask_gradient_field(
                obj, width, height, mapping,
            ), repetitions=5))
            rows.append(row)
    print(json.dumps({"timings": rows,
                      "cached_ramps": len(canvas._gradient_ramp_cache)}, indent=2))
    canvas.close()
    canvas.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    main()
