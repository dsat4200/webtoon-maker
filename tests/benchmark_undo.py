"""Run `python tests/benchmark_undo.py` for current undo-handler timings.

Synthetic documents and temporary preferences leave open projects/settings
untouched. Timings exclude document construction, repaint, and redo work.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from comic_editor.core import settings
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, VectorDrawingObject,
    VectorStroke, VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.main_window import MainWindow


def measure(undo, redo):
    samples = []
    for iteration in range(8):
        started = time.perf_counter()
        undo()
        elapsed = (time.perf_counter() - started) * 1000
        redo()
        if iteration:
            samples.append(elapsed)
    return round(statistics.median(samples), 3)


def object_move():
    document = ChapterDocument(height=50000)
    page = document.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 50000))
    for index in range(1000):
        layer = document.add_layer(page.layer_id, str(index),
            BoundGeometry.rectangle(0, index * 50, 600, 45))
        document.add_object(layer.layer_id, RasterObject())
    window = MainWindow()
    try:
        window._set_chapter(document, TileStore())
        target = next(iter(document.objects.values()))
        window.canvas.set_selection("object", target.object_id)
        before = document.to_dict()
        target.x += 10
        after = document.to_dict()
        window.canvas.push_model_change(before, after, "Move object")
        elapsed = measure(window._undo, window._redo)
        assert window.canvas.chapter.to_dict() == after
        return {"workload": "object_move_main_window", "layers": len(document.layers),
                "objects": len(document.objects), "median_undo_ms": elapsed}
    finally:
        window.autosave_timer.stop()
        window.layout_settings_timer.stop()
        window.series_preferences_timer.stop()
        window.canvas._effect_jobs.cancel()
        window.blender_sources.shutdown()
        window._autosave_jobs.shutdown()
        window.deleteLater()


def vector_stroke():
    document = ChapterDocument()
    page = document.add_page()
    drawing = document.add_object(page.layer_id, VectorDrawingObject(strokes=[
        VectorStroke(points=[VectorStrokePoint(x=x, y=y, width=2)
                             for x in range(10)]) for y in range(5000)
    ]))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    try:
        canvas.set_document(document, TileStore())
        canvas.set_selection("object", drawing.object_id)
        before = drawing.to_dict()
        drawing.strokes.append(VectorStroke(points=[VectorStrokePoint(x=100, y=100)]))
        drawing.touch_revision()
        after = drawing.to_dict()
        canvas._push_vector_change({drawing.object_id: before}, "Add stroke")
        elapsed = measure(canvas.command_stack.undo, canvas.command_stack.redo)
        assert drawing.to_dict() == after
        return {"workload": "vector_stroke_canvas", "existing_strokes": 5000,
                "points_per_existing_stroke": 10, "median_undo_ms": elapsed}
    finally:
        canvas._effect_jobs.cancel()
        canvas.deleteLater()


def main():
    app = QApplication.instance() or QApplication([])
    original_settings_path = settings.settings_path
    with tempfile.TemporaryDirectory() as temporary:
        settings.settings_path = lambda: Path(temporary) / "settings.json"
        try:
            for operation in (object_move, vector_stroke):
                print(json.dumps(operation()), flush=True)
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                app.processEvents()
        finally:
            settings.settings_path = original_settings_path


if __name__ == "__main__":
    main()
