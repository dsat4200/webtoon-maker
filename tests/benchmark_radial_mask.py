"""Opt-in full-resolution radial intensity-gradient edit benchmark.

Run from the repository root: python tests/benchmark_radial_mask.py [--size 1080]
The first render represents the angular integration previously repeated for
every mask edit. Subsequent measurements include real gradient-field rendering,
mixing and final image conversion, with no reduced-resolution drafts.
"""
import argparse
import os
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QImage, QPainter, QColor, QTransform
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import BoundGeometry, ChapterDocument, ParameterMaskBinding, RadialBlurModifier, ToneMask
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.radial_blur import radial_blur


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=1080)
    size = parser.parse_args().size
    app = QApplication.instance() or QApplication([])
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    chapter = ChapterDocument(width=size, height=size, document_kind="asset")
    chapter.add_page("Page", BoundGeometry.rectangle(0, 0, size, size))
    modifier = RadialBlurModifier(angle=24, center=(size/2, size/2))
    chapter.modifiers[modifier.modifier_id] = modifier
    mask = ToneMask(saved=True)
    chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    canvas.set_document(chapter, TileStore())
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas._mask_gradient_press(QPointF(size*.2, size*.5))
    canvas._mask_gradient_move(QPointF(size*.8, size*.5))
    canvas._finish_mask_gradient()
    source = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("#ffaa5599"))
    painter = QPainter(source)
    painter.fillRect(QRectF(size*.4, size*.1, size*.25, size*.6), QColor("#8055bbff"))
    painter.end()
    bounds = QRectF(0, 0, size, size)
    arguments = dict(source_key=("benchmark-source",), request_scope=("object", "benchmark", "canvas"))
    try:
        with patch("comic_editor.ui.radial_blur.radial_blur", wraps=radial_blur) as integration:
            started = time.perf_counter()
            first, output_bounds = render_stages(canvas, source, bounds, [modifier], QTransform(), **arguments)
            cold = time.perf_counter()-started
            print(f"{size}px radial 24deg initial integration: {cold:.3f}s; output={first.width()}x{first.height()}", flush=True)
            timings = []
            for fraction in (.7, .6, .9, .5, .75):
                gradient = mask.gradient
                started = time.perf_counter()
                gradient.line_field.geometry.nodes[-1].position = (size*fraction, size*.5)
                gradient.touch_revision()
                mask.touch()
                canvas.documentChanged.emit(QRectF())
                result, actual_bounds = render_stages(canvas, source, bounds, [modifier], QTransform(), **arguments)
                timings.append(time.perf_counter()-started)
                assert result != first and actual_bounds == output_bounds
            assert integration.call_count == 1, "Intensity gradient edits recomputed angular integration"
            median = statistics.median(timings)
            print(f"Gradient edits: median={median*1000:.1f}ms; max={max(timings)*1000:.1f}ms; speedup={cold/median:.1f}x; integrations={integration.call_count}")
            print(f"Result cache: {canvas._modifier_render_cache_bytes/1048576:.1f}MiB")
    finally:
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    main()
