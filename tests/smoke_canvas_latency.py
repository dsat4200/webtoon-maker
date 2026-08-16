"""Opt-in offscreen latency gate for drawing and live text transforms.

Run directly from the repository root:
    python tests/smoke_canvas_latency.py
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from comic_editor.core.models import (  # noqa: E402
    BoundGeometry, ChapterDocument, RasterObject, VectorDrawingObject,
    TextObject, VectorStroke, VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings  # noqa: E402
from comic_editor.core.tiles import TileStore  # noqa: E402
from comic_editor.ui.canvas import CanvasWidget, ToolKind  # noqa: E402


MOVES = 800
INPUT_P95_LIMIT_MS = 8.0
FRAME_P95_LIMIT_MS = 16.7
COMMIT_LIMIT_MS = 100.0
VECTOR_GROWTH_LIMIT = 1.25


def percentile(values: list[float], amount: float) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * amount)]


def make_canvas(kind: str, *, eraser: bool = False):
    chapter = ChapterDocument()
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1800, 1800)
    )
    layer = chapter.add_layer(
        page.layer_id, "Ink", BoundGeometry.rectangle(0, 0, 1800, 1800)
    )
    tiles = TileStore()
    if kind == "raster":
        obj = chapter.add_object(
            layer.layer_id,
            RasterObject(interaction_rect=(0, 0, 1800, 1800)),
        )
        if eraser:
            tiles.paint_segment(
                obj.object_id, QPointF(450, 80), QPointF(450, 720),
                60, 60, QColor("#111111"), 1.0, 1.0,
            )
    else:
        strokes = []
        if eraser:
            strokes = [VectorStroke(points=[
                VectorStrokePoint(x=100, y=400, width=16),
                VectorStrokePoint(x=800, y=400, width=16),
            ])]
        obj = chapter.add_object(
            layer.layer_id, VectorDrawingObject(strokes=strokes)
        )
    settings = EditorSettings(
        snap_to_grid=False,
        predictive_ink=False,
        canvas_renderer="raster",
        vector_eraser_mode="point",
    )
    canvas = CanvasWidget(settings)
    canvas.resize(1000, 800)
    canvas.set_document(chapter, tiles)
    canvas.center_x = 500
    canvas.center_y = 400
    canvas.scale = 1.0
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(
        ToolKind.RASTER_ERASER if eraser else ToolKind.RASTER_PENCIL
    )
    canvas.show()
    QApplication.processEvents()
    return canvas, obj


def run_pencil(kind: str) -> dict[str, float]:
    canvas, obj = make_canvas(kind)

    def point(index: int) -> QPointF:
        return QPointF(
            450 + 300 * math.cos(index * 0.035),
            400 + 220 * math.sin(index * 0.035),
        )

    if kind == "raster":
        canvas._begin_stroke(point(0), 0.6)
    else:
        canvas._begin_vector_pencil(obj, point(0), 0.6)
    QApplication.processEvents()
    moves: list[float] = []
    for index in range(1, MOVES + 1):
        started = time.perf_counter_ns()
        pressure = 0.4 + 0.4 * ((index % 50) / 49)
        if kind == "raster":
            canvas._continue_stroke(point(index), pressure)
        else:
            canvas._continue_vector_gesture(point(index), pressure)
        QApplication.processEvents()
        moves.append((time.perf_counter_ns() - started) / 1_000_000)
    started = time.perf_counter_ns()
    if kind == "raster":
        canvas._end_stroke()
    else:
        canvas._finish_vector_pencil(obj)
    QApplication.processEvents()
    commit = (time.perf_counter_ns() - started) / 1_000_000
    frame = canvas.performance_snapshot()["frame_p95_ms"]
    result = {
        "input_p95_ms": percentile(moves, 0.95),
        "frame_p95_ms": frame,
        "commit_ms": commit,
        "growth": (
            statistics.mean(moves[-100:])
            / max(0.001, statistics.mean(moves[:100]))
        ),
    }
    canvas.close()
    QApplication.processEvents()
    return result


def run_eraser(kind: str) -> dict[str, float]:
    canvas, obj = make_canvas(kind, eraser=True)
    start = QPointF(450, 100)
    if kind == "raster":
        canvas._begin_stroke(start, 1.0)
    else:
        canvas._begin_vector_gesture(obj, start, 1.0)
    QApplication.processEvents()
    moves: list[float] = []
    for index in range(1, MOVES + 1):
        point = QPointF(450, 100 + index * 0.75)
        started = time.perf_counter_ns()
        if kind == "raster":
            canvas._continue_stroke(point, 1.0)
        else:
            canvas._continue_vector_gesture(point, 1.0)
        QApplication.processEvents()
        moves.append((time.perf_counter_ns() - started) / 1_000_000)
    started = time.perf_counter_ns()
    if kind == "raster":
        canvas._end_stroke()
    else:
        canvas._finish_vector_eraser(obj)
    QApplication.processEvents()
    commit = (time.perf_counter_ns() - started) / 1_000_000
    frame = canvas.performance_snapshot()["frame_p95_ms"]
    result = {
        "input_p95_ms": percentile(moves, 0.95),
        "frame_p95_ms": frame,
        "commit_ms": commit,
        "growth": 1.0,
    }
    canvas.close()
    QApplication.processEvents()
    return result


def run_text_transform() -> dict[str, float]:
    chapter = ChapterDocument()
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1800, 1800)
    )
    layer = chapter.add_layer(
        page.layer_id, "Text", BoundGeometry.rectangle(0, 0, 1800, 1800)
    )
    obj = chapter.add_object(
        layer.layer_id,
        TextObject(
            text="Cached live transform " * 80,
            layout_mode="free",
            width=720,
            height=420,
            transform_quad=[
                (240, 180), (960, 180), (960, 600), (240, 600),
            ],
        ),
    )
    settings = EditorSettings(
        snap_to_grid=False, canvas_renderer="raster"
    )
    canvas = CanvasWidget(settings)
    canvas.resize(1000, 800)
    canvas.set_document(chapter, TileStore())
    canvas.center_x = 600
    canvas.center_y = 400
    canvas.scale = 1.0
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.show()
    QApplication.processEvents()

    start = canvas.document_to_widget(QPointF(600, 390))
    canvas._tool_press(start, 1.0)
    QApplication.processEvents()
    moves: list[float] = []
    for index in range(1, MOVES + 1):
        point = start + QPointF(
            30 * math.sin(index * 0.05),
            20 * math.cos(index * 0.05),
        )
        started = time.perf_counter_ns()
        canvas._tool_move(point, 1.0)
        QApplication.processEvents()
        moves.append((time.perf_counter_ns() - started) / 1_000_000)
    started = time.perf_counter_ns()
    canvas._tool_release()
    QApplication.processEvents()
    commit = (time.perf_counter_ns() - started) / 1_000_000
    result = {
        "input_p95_ms": percentile(moves, 0.95),
        "frame_p95_ms": canvas.performance_snapshot()["frame_p95_ms"],
        "commit_ms": commit,
        "growth": (
            statistics.mean(moves[-100:])
            / max(0.001, statistics.mean(moves[:100]))
        ),
    }
    canvas.close()
    QApplication.processEvents()
    return result


def run_dense_vector_navigation() -> dict[str, float]:
    """Exercise warmed pan and balanced-live zoom above the old 384 cap."""
    chapter = ChapterDocument()
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1800, 1800)
    )
    layer = chapter.add_layer(
        page.layer_id, "Dense vectors",
        BoundGeometry.rectangle(0, 0, 1800, 1800),
    )
    strokes = [
        VectorStroke(points=[
            VectorStrokePoint(
                x=100 + (index % 20) * 70,
                y=100 + (index // 20) * 32,
                width=4,
            ),
            VectorStrokePoint(
                x=145 + (index % 20) * 70,
                y=110 + (index // 20) * 32,
                width=4,
            ),
        ])
        for index in range(550)
    ]
    drawing = chapter.add_object(
        layer.layer_id, VectorDrawingObject(strokes=strokes)
    )
    canvas = CanvasWidget(EditorSettings(
        snap_to_grid=False, predictive_ink=False,
        canvas_renderer="raster",
    ))
    canvas.resize(1000, 800)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 800, 700, 0.75
    canvas.set_selection("object", drawing.object_id)
    canvas.show()
    QApplication.processEvents()
    QApplication.processEvents()

    frames: list[float] = []
    start = QPointF(500, 400)
    canvas._begin_navigation("pan", start)
    for index in range(60):
        started = time.perf_counter_ns()
        canvas._update_navigation(start + QPointF(index % 24, index % 11))
        QApplication.processEvents()
        frames.append((time.perf_counter_ns() - started) / 1_000_000)
    canvas._end_navigation()

    canvas._begin_navigation("zoom", start)
    for index in range(60):
        started = time.perf_counter_ns()
        canvas._update_navigation(start + QPointF((index % 20) - 10, 0))
        QApplication.processEvents()
        frames.append((time.perf_counter_ns() - started) / 1_000_000)
    started = time.perf_counter_ns()
    canvas._end_navigation()
    QApplication.processEvents()
    commit = (time.perf_counter_ns() - started) / 1_000_000
    result = {
        "input_p95_ms": percentile(frames, 0.95),
        "frame_p95_ms": canvas.performance_snapshot()["frame_p95_ms"],
        "commit_ms": commit,
        "growth": 1.0,
    }
    canvas.close()
    QApplication.processEvents()
    return result


def median_runs(function) -> dict[str, float]:
    function()  # warm Qt, painters, caches, and imports
    runs = [function() for _ in range(3)]
    return {
        key: statistics.median(run[key] for run in runs)
        for key in runs[0]
    }


def main() -> int:
    _app = QApplication.instance() or QApplication([])
    results = {
        "raster_pencil": median_runs(lambda: run_pencil("raster")),
        "raster_eraser": median_runs(lambda: run_eraser("raster")),
        "vector_pencil": median_runs(lambda: run_pencil("vector")),
        "vector_eraser": median_runs(lambda: run_eraser("vector")),
        "text_transform": median_runs(run_text_transform),
        "dense_vector_navigation": median_runs(
            run_dense_vector_navigation
        ),
    }
    print(json.dumps(results, indent=2, sort_keys=True))
    failures: list[str] = []
    for name, metrics in results.items():
        if metrics["input_p95_ms"] > INPUT_P95_LIMIT_MS:
            failures.append(f"{name} input p95")
        if metrics["frame_p95_ms"] > FRAME_P95_LIMIT_MS:
            failures.append(f"{name} frame p95")
        if metrics["commit_ms"] > COMMIT_LIMIT_MS:
            failures.append(f"{name} commit")
    if results["vector_pencil"]["growth"] > VECTOR_GROWTH_LIMIT:
        failures.append("vector_pencil long-stroke growth")
    if failures:
        print("FAILED: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
