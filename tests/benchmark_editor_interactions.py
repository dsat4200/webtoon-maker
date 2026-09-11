"""Profile editor-side work around rendering, drawing, and large fills.

Run from the repository root; --synchronous measures the old blocking display
path with the same kernels. Uses the CPU canvas deliberately; modifier/GPU
kernel measurements live in benchmark_modifier_interactions.py and fill event
loop measurements in benchmark_fill_interactions.py.
"""
from __future__ import annotations

import cProfile
import io
import json
import os
from pathlib import Path
import pstats
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HueSaturationLightnessModifier,
    OutlineModifier, RasterObject, VectorDrawingObject, VectorStroke, VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.preview import ChapterPreview


def measure(name, operation, frames=8):
    samples = []
    profiler = cProfile.Profile()
    profiler.enable()
    for frame in range(frames):
        start = time.perf_counter()
        operation(frame)
        samples.append((time.perf_counter() - start) * 1000)
    profiler.disable()
    report = io.StringIO()
    pstats.Stats(profiler, stream=report).strip_dirs().sort_stats("cumtime").print_stats(15)
    result = {"name": name, "first_ms": samples[0],
              "median_ms": statistics.median(samples[1:] or samples), "max_ms": max(samples)}
    print(json.dumps(result), flush=True)
    return result, report.getvalue()


def scene(pages=5):
    chapter = ChapterDocument(height=1000 * pages)
    tiles = TileStore()
    modifiers = []
    objects = []
    for index in range(pages):
        page = chapter.add_page(str(index), BoundGeometry.rectangle(0, index * 1000, 1080, 1000))
        page.fill_color, page.border_width = None, 0
        layer = chapter.add_layer(page.layer_id, "Ink", BoundGeometry.rectangle(0, index * 1000, 1080, 1000))
        layer.fill_color, layer.border_width = None, 0
        obj = chapter.add_object(layer.layer_id, RasterObject(interaction_rect=(0, index * 1000, 1080, 1000)))
        for x in range(70, 1050, 160):
            tiles.paint_dab(obj.object_id, QPointF(x, index*1000+500), 180, QColor("#cf407c"))
        hsl, blur, outline = HueSaturationLightnessModifier(hue=10), BlurModifier(strength=8), OutlineModifier(thickness=3)
        for modifier in (hsl, blur, outline):
            chapter.add_modifier(modifier, [("object", obj.object_id)])
        modifiers.append(hsl)
        objects.append(obj)
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, tiles)
    canvas.center_x, canvas.center_y, canvas.scale = 450, 350, 1
    canvas.set_selection("object", objects[0].object_id)
    return canvas, objects, modifiers


def main():
    app = QApplication.instance() or QApplication([])
    synchronous = "--synchronous" in sys.argv
    if synchronous:
        from comic_editor.ui import interactive_effects
        original = interactive_effects.render_interactive_stack
        def synchronous_stack(canvas, *args, **kwargs):
            previous = canvas._interactive_render
            canvas._interactive_render = False
            try:
                return original(canvas, *args, **kwargs)
            finally:
                canvas._interactive_render = previous
        interactive_effects.render_interactive_stack = synchronous_stack
    output = Path(__file__).resolve().parents[1] / ".artifacts" / "editor-performance"
    output.mkdir(parents=True, exist_ok=True)
    results, profiles = [], []
    canvas, objects, modifiers = scene()
    canvas.show()
    preview = ChapterPreview(canvas)
    preview.resize(92, 700)
    preview.show()
    app.processEvents()
    def run(name, operation, frames=8):
        result, profile = measure(name, operation, frames)
        results.append(result)
        profiles.append(name + "\n" + profile)
    run("unchanged widget", lambda _: canvas.grab())
    def slider(index):
        modifiers[0].hue = 12 + index
        canvas.documentChanged.emit(None)
        canvas.grab()
        preview.grab()
    run("modifier slider + navigator", slider)
    def move(index):
        objects[0].x = 3 * index
        canvas.documentChanged.emit(None)
        canvas.grab()
        preview.grab()
    run("move object with HSL blur outline + navigator", move)
    objects[0].x = 0
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_stroke(QPointF(150, 300), .8)
    def stroke(index):
        canvas._continue_stroke(QPointF(170+index*12, 300+index*4), .8)
        canvas._flush_visual_dirty()
        canvas.grab()
        preview.grab()
    run("drawing with HSL blur outline + navigator", stroke)
    canvas._end_stroke()
    preview.close()
    canvas.close()
    canvas._effect_jobs.cancel()
    canvas.deleteLater()
    app.processEvents()
    prefix = "synchronous-" if synchronous else ""
    (output / (prefix + "timings.json")).write_text(json.dumps(results, indent=2), encoding="utf-8")
    (output / (prefix + "profiles.txt")).write_text("\n\n".join(profiles), encoding="utf-8")


if __name__ == "__main__":
    main()
