"""Opt-in text outline cold/warmed latency and distance-cache measurements."""
import argparse
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QRawFont
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import OutlineModifier
from comic_editor.ui.modifier_rendering import OutlineDistanceCache, apply_modifier_stack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="Compare against the renderer in git HEAD")
    parser.add_argument("--stacks", action="store_true", help="Also measure two- and three-outline stacks")
    options = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    if font_path.exists():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
    else:
        families = QFontDatabase.families()
    if not families:
        raise RuntimeError("Text benchmarks require an installed font with actual glyphs")
    font_family = families[0]
    assert all(QRawFont.fromFont(QFont(font_family, 40)).glyphIndexesForString("Realtime text"))
    renderers = [("current", apply_modifier_stack, OutlineDistanceCache)]
    if options.baseline:
        namespace = {"__name__": "baseline_modifier_rendering"}
        code = subprocess.check_output(
            ["git", "show", "HEAD:comic_editor/ui/modifier_rendering.py"], text=True)
        exec(compile(code, "baseline_modifier_rendering", "exec"), namespace)
        renderers.insert(0, ("baseline", namespace["apply_modifier_stack"], namespace["OutlineDistanceCache"]))
    print(f"Verified glyph font: {font_family}")
    for width, height in ((512, 256), (1024, 512), (2048, 1024)):
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setFont(QFont(font_family, height // 6))
        painter.setPen(QColor("white"))
        painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "Realtime text")
        painter.end()
        for count in ((1, 2, 3) if options.stacks else (1,)):
            modifiers = [OutlineModifier(thickness=4, color=color)
                         for color in ("#FF204080", "#FF903000", "#FF000000")[:count]]
            for label, render, cache_type in renderers:
                modifiers[-1].thickness = 4
                cache = cache_type()
                start = time.perf_counter()
                render(image, modifiers, (0, 0), outline_distance_cache=cache)
                cold = (time.perf_counter() - start) * 1000
                times = []
                for index in range(20):
                    modifiers[-1].thickness = 3 + index * .2
                    start = time.perf_counter()
                    render(image, modifiers, (0, 0), outline_distance_cache=cache)
                    times.append((time.perf_counter() - start) * 1000)
                print(f"{label} {width}x{height}, {count} outline(s): cold={cold:.2f}ms, "
                      f"warmed median={statistics.median(times):.2f}ms, "
                      f"distance builds={cache.computations}, cache={cache.bytes / 1048576:.2f}MiB")
                assert cache.computations == count
    app.processEvents()


if __name__ == "__main__":
    main()
