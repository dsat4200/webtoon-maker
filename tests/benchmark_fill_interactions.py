"""Run directly to measure submission latency and GUI heartbeat during large fills."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPointF, QRectF, QTimer
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def measure(app, height, mode):
    chapter = ChapterDocument(height=height)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, height))
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 1080, height)))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.FILL)
    profile = canvas.settings.active_fill_profile()
    if mode in {"painted", "painted_narrow"}:
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor("#FFEEEEEE"))
        for key in canvas.tiles.keys_for_rect(QRectF(*obj.interaction_rect)):
            canvas.tiles.set_tile(obj.object_id, key, image)
        if mode == "painted_narrow":
            profile.update({"close_gap": False, "antialiasing": False})
            spot = QImage(image)
            painter = QPainter(spot)
            painter.fillRect(QRectF(90, 90, 40, 40), QColor("white"))
            painter.end()
            canvas.tiles.set_tile(obj.object_id, (0, 0), spot)
    if mode == "references":
        reference = chapter.add_object(page.layer_id, RasterObject())
        reference.fill_reference = True
        profile["reference_mode"] = "reference"
        for key in canvas.tiles.keys_for_rect(QRectF(535, 0, 10, height)):
            image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            painter.fillRect(QRectF(535-key[0]*256, 0, 10, 256), QColor("black"))
            painter.end()
            canvas.tiles.set_tile(reference.object_id, key, image)
    if mode == "morphology":
        profile.update({"close_gap": True, "fill_narrow_areas": False, "area_scaling": True, "area_amount": 6})
    heartbeat = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: heartbeat.append(time.perf_counter()))
    poll = QTimer()
    poll.setInterval(5)
    poll.timeout.connect(lambda: app.quit() if not canvas._fill_workers and canvas._fill_job_cancel is None else None)
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(app.quit)
    started = time.perf_counter()
    canvas._begin_fill_gesture(obj, QPointF(100, 100))
    canvas._finish_fill_gesture(obj)
    submission_ms = (time.perf_counter() - started)*1000
    timer.start()
    poll.start()
    timeout.start(30000)
    app.exec()
    completed = time.perf_counter()
    timer.stop()
    poll.stop()
    timeout.stop()
    samples = [started, *heartbeat, completed]
    gap = max((second-first)*1000 for first, second in zip(samples, samples[1:]))
    success = canvas.command_stack.can_undo and canvas._fill_job_error is None and canvas._fill_job_cancel is None
    print(f"1080x{height} {mode}: submit={submission_ms:.2f}ms total={(completed-started)*1000:.2f}ms "
          f"max GUI heartbeat gap={gap:.2f}ms ticks={len(heartbeat)} committed={success}", flush=True)
    canvas._cancel_fill_job()
    canvas._clear_fill_replay()
    canvas.deleteLater()
    app.processEvents()
    assert success


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    for height in (2000, 6000):
        for mode in ("editing", "painted", "morphology", "references"):
            measure(app, height, mode)
    measure(app, 20000, "painted_narrow")
