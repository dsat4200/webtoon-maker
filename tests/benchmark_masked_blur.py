"""Opt-in 1080p warmed masked-blur benchmark.

Run directly from the repository root:
    python tests/benchmark_masked_blur.py

This is intentionally not a pytest test. Machine-specific timings are printed
for profiling, while the invariant that parameter edits reuse one pyramid is
checked on every run.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import BlurModifier, ParameterMaskBinding
from comic_editor.ui.modifier_rendering import (
    BlurPyramidCache, apply_modifier_stack,
)


def main() -> int:
    width, height = 1920, 1080
    image = QImage(
        width, height, QImage.Format.Format_ARGB32_Premultiplied
    )
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillRect(160, 120, 1600, 840, QColor("#ff8050"))
    painter.end()
    mask = np.tile(
        np.linspace(0.0, 1.0, width, dtype=np.float32), (height, 1)
    )
    modifier = BlurModifier(strength=40)
    modifier.parameter_masks["strength"] = ParameterMaskBinding(
        "benchmark-mask", 0, 40
    )
    fields = {(modifier.modifier_id, "strength"): mask}
    cache = BlurPyramidCache()
    samples: list[float] = []
    for strength in (12, 20, 28, 36, 44, 52):
        modifier.parameter_masks["strength"].white_value = strength
        started = time.perf_counter()
        apply_modifier_stack(
            image, [modifier], (0, 0), fields,
            blur_pyramid_cache=cache,
        )
        samples.append((time.perf_counter() - started) * 1000.0)
    if cache.builds != 1:
        raise SystemExit(
            f"expected one warmed pyramid build, got {cache.builds}"
        )
    print(
        f"1080p masked blur: median={statistics.median(samples[1:]):.1f}ms "
        f"pyramid_builds={cache.builds} cache={cache.bytes / 1048576:.1f}MiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
