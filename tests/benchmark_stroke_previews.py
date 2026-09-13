"""Compare blocking stroke rendering with interactive previews during gestures.

Run from the repository root: python tests/benchmark_stroke_previews.py.
The scene is a non-compound 500x220 speech bubble with text. The blocking
comparison disables worker admission while preserving the same exact kernels.
"""
import json
import os
from pathlib import Path
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QPointF
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ScreamModifier, ShapeStyle, TextObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def measure(font_family, blocking, gesture):
    chapter = ChapterDocument(height=700)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 700))
    bound = BoundGeometry.rectangle(220, 240, 500, 220)
    bound.primitive = "ellipse"
    bubble = chapter.add_layer(page.layer_id, "Bubble", bound, style=ShapeStyle(
        primary_color="#FFFFFFFF", outline_color="#FF151515", outline_thickness=4))
    chapter.add_object(bubble.layer_id, TextObject(
        text="What? I haven't even started yet!", font_family=font_family, font_size=32))
    chapter.add_modifier(ScreamModifier(height=47, width=69, roundness=0),
                         [("layer", bubble.layer_id)])
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    try:
        canvas.resize(900, 700)
        canvas.set_document(chapter, TileStore())
        canvas.center_x, canvas.center_y, canvas.scale = 450, 350, 1
        canvas.set_selection("layer", bubble.layer_id)
        canvas.set_tool(ToolKind.TRANSFORM)
        if blocking:
            canvas._effect_jobs.request = lambda *args, **kwargs: False
        canvas.grab()
        start = QPointF(470, 350) if gesture == "move" else QPointF(720, 460)
        assert canvas._begin_geometry_transform(start)
        canvas._transform_drag_mode = "translate" if gesture == "move" else "scale"
        canvas._transform_handle_index = 2
        times = []
        for index in range(8):
            point = (QPointF(480 + index*4, 355 + index*2) if gesture == "move"
                     else QPointF(730 + index*5, 464.4 + index*2.2))
            started = time.perf_counter()
            canvas._update_geometry_transform_preview(point)
            canvas.grab()
            times.append(round((time.perf_counter() - started) * 1000, 2))
        return {"mode": "blocking" if blocking else "interactive", "gesture": gesture,
                "median_ms": round(statistics.median(times), 2), "frames_ms": times}
    finally:
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.close()
        canvas.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        QApplication.processEvents()


def main():
    app = QApplication.instance() or QApplication([])
    font_family = "Segoe UI"
    path = Path("C:/Windows/Fonts/segoeui.ttf")
    if path.exists():
        identifier = QFontDatabase.addApplicationFont(str(path))
        families = QFontDatabase.applicationFontFamilies(identifier)
        if families:
            font_family = families[0]
    for gesture in ("move", "resize"):
        for blocking in (True, False):
            print(json.dumps(measure(font_family, blocking, gesture)), flush=True)


if __name__ == "__main__":
    main()
