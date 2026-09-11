"""Bounded modifier slider/move benchmarks, with exact-final verification.

Run --backend cpu to exercise the cancellable fallback, or --backend auto for
the installed GPU driver. No editor windows are opened. Fill and navigator
interaction profiles live in benchmark_editor_interactions.py.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
from pathlib import Path
import pstats
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HalftoneModifier,
    HueSaturationLightnessModifier, OutlineModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.gpu_pattern_effects import renderer_for
from comic_editor.ui.interactive_effects import render_interactive_stack
from comic_editor.ui.modifier_rendering import apply_modifier_stack, apply_pattern_modifier


def pixels(image):
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    return np.frombuffer(converted.constBits(), dtype=np.uint8).reshape(
        converted.height(), converted.width(), 4).copy()


def source_image(width, height):
    y, x = np.mgrid[:height, :width].astype(np.float32)
    x /= max(1, width - 1)
    y /= max(1, height - 1)
    alpha = np.clip((1 - ((x - .5) ** 2 + (y - .5) ** 2) * 3) * 1.5, 0, 1)
    colors = np.stack((.15 + x * .7, .1 + y * .8, .8 - x * y * .6, alpha), axis=-1)
    data = np.ascontiguousarray(np.rint(colors * 255).astype(np.uint8))
    return QImage(data.data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("cpu", "auto"), default="auto")
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--frames", type=int, default=8)
    options = parser.parse_args()
    if options.backend == "cpu":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    width, height = options.width, options.height
    chapter = ChapterDocument(width=width, height=height, document_kind="asset")
    page = chapter.add_page("Benchmark", BoundGeometry.rectangle(0, 0, width, height))
    target = chapter.add_layer(page.layer_id, "Artwork", BoundGeometry.rectangle(0, 0, width, height))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster" if options.backend == "cpu" else "auto"))
    canvas.set_document(chapter, TileStore())
    source = source_image(width, height)
    bounds = QRectF(0, 0, width, height)
    results, profiles = [], []
    scenarios = [
        ("square halftone", [HalftoneModifier()]),
        ("stippling", [HalftoneModifier(grid_type="stippling")]),
        ("HSL blur outline", [HueSaturationLightnessModifier(hue=20),
                              BlurModifier(strength=8), OutlineModifier(thickness=4)]),
    ]
    try:
        for name, modifiers in scenarios:
            for modifier in modifiers:
                chapter.add_modifier(modifier, [("layer", target.layer_id)])
            pattern = isinstance(modifiers[0], HalftoneModifier)
            for interaction in ("slider", "move"):
                canvas._modifier_render_cache.clear()
                canvas._modifier_render_cache_bytes = 0
                mapping = QTransform()
                scope = ("benchmark", name, interaction)
                frame = 0

                def render():
                    if pattern:
                        revision = getattr(canvas, "_effect_provisional_revision", 0)
                        result, _ = render_stages(canvas, source, bounds, modifiers, mapping, request_scope=scope)
                        return result, getattr(canvas, "_effect_provisional_revision", 0) != revision
                    key = (scope, frame, repr([modifier.to_dict() for modifier in modifiers]))
                    return render_interactive_stack(canvas, source, modifiers, (mapping.dx(), mapping.dy()),
                        cache_key=key, scope=scope, world_to_image=mapping)

                canvas._interactive_render = False
                started = time.perf_counter()
                render()
                exact_ms = (time.perf_counter() - started) * 1000
                canvas._interactive_render = True
                times = []
                profile = cProfile.Profile()
                profile.enable()
                for frame in range(1, options.frames + 1):
                    if interaction == "slider":
                        if pattern:
                            modifiers[0].contrast = frame * .06
                        else:
                            modifiers[0].hue = 20 + frame * 4
                    else:
                        mapping = QTransform.fromTranslate(frame * 3, frame * 2)
                    started = time.perf_counter()
                    result, provisional = render()
                    times.append((time.perf_counter() - started) * 1000)
                profile.disable()
                last_edit = time.perf_counter()
                deadline = last_edit + 20
                while provisional and time.perf_counter() < deadline:
                    app.processEvents()
                    canvas._effect_jobs.poll()
                    result, provisional = render()
                    if provisional:
                        time.sleep(.005)
                assert not provisional, f"{name} did not converge to an exact image"
                settle_ms = (time.perf_counter() - last_edit) * 1000
                expected = (apply_pattern_modifier(source, modifiers[0], renderer=renderer_for(canvas))
                            if pattern else apply_modifier_stack(source, modifiers,
                                (mapping.dx(), mapping.dy()), world_to_image=mapping))
                difference = int(np.abs(pixels(result).astype(np.int16) - pixels(expected).astype(np.int16)).max())
                assert difference <= 1, f"{name} exact output changed: {difference} bytes"
                report = {"scenario": name, "interaction": interaction,
                          "synchronous_ms": round(exact_ms, 3),
                          "interactive_median_ms": round(statistics.median(times), 3),
                          "interactive_max_ms": round(max(times), 3),
                          "exact_after_last_edit_ms": round(settle_ms, 3),
                          "max_final_byte_difference": difference}
                results.append(report)
                print(json.dumps(report), flush=True)
                stream = io.StringIO()
                pstats.Stats(profile, stream=stream).strip_dirs().sort_stats("cumtime").print_stats(16)
                profiles.append(f"{name} / {interaction}\n{stream.getvalue()}")
        output = Path(__file__).resolve().parents[1] / ".artifacts" / "editor-performance"
        output.mkdir(parents=True, exist_ok=True)
        renderer = getattr(canvas, "_gpu_pattern_renderer", None)
        report = {"backend": options.backend, "size": [width, height], "frames": options.frames,
                  "gpu_available": bool(renderer and renderer.available),
                  "gpu_reason": renderer.reason if renderer is not None else "CPU requested",
                  "source_uploads": renderer.uploads if renderer is not None else 0,
                  "jobs_submitted": canvas._effect_jobs.submitted,
                  "jobs_discarded": canvas._effect_jobs.discarded, "timings": results}
        (output / f"modifier-interactions-{options.backend}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (output / f"modifier-profiles-{options.backend}.txt").write_text("\n".join(profiles), encoding="utf-8")
    finally:
        canvas._effect_jobs.cancel()
        renderer = getattr(canvas, "_gpu_pattern_renderer", None)
        if renderer is not None:
            renderer.close()
        canvas.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    main()
