"""Manual GPU parity/performance check; opens no windows and writes sample PNGs.

Run: python tests/benchmark_cage.py
"""
import os
from pathlib import Path
import sys
import time
import statistics
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "windows")
import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from comic_editor.core.cage import CageGrid
from comic_editor.ui.cage_rendering import warp_image
from comic_editor.ui.gpu_textures import GpuTextureRenderer
from comic_editor.ui.modifier_rendering import _qimage_premultiplied


def main():
    app = QApplication.instance() or QApplication([])
    gpu = GpuTextureRenderer(allow_offscreen=True)
    if not gpu.available:
        print("GPU unavailable:", gpu.reason)
        return
    output = Path(__file__).resolve().parents[1]/".artifacts"/"cage-transform"
    output.mkdir(parents=True, exist_ok=True)
    image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#cc3685e8"))
    painter.drawEllipse(QRectF(8, 12, 105, 77))
    painter.setBrush(QColor("#ffd95d82"))
    painter.drawRect(QRectF(17, 61, 32, 51))
    painter.end()
    cage = CageGrid(frame=(0, 0, 128, 128), columns=4, rows=4)
    cage.validate_grid()
    cage.points[5] = (62, 21)
    cage.points[10] = (77, 99)
    bounds = QRectF(-30, -30, 188, 188)
    for interpolation in ("nearest", "bilinear", "bicubic"):
        cage.interpolation = interpolation
        cpu, _ = warp_image(image, QRectF(0, 0, 128, 128), cage, output_bounds=bounds)
        rendered = gpu.cage(image, QRectF(0, 0, 128, 128), cage, QTransform(), bounds)
        assert rendered is not None, gpu.reason
        difference = np.abs(_qimage_premultiplied(cpu)-_qimage_premultiplied(rendered))
        print(interpolation, "GPU/CPU maximum error:", round(float(difference.max()*255), 3), "byte levels")
        if interpolation == "nearest":
            # Hardware snaps triangle vertices to its subpixel raster grid.
            # At texel boundaries this can choose the adjacent nearest texel.
            assert np.count_nonzero(difference.max(axis=2) > 2.01/255) <= 4
            assert float(difference.mean()*255) < .01
        else:
            assert difference.max() <= 2.01/255
        rendered.save(str(output/f"cage-{interpolation}.png"))
    for size in (512, 1080):
        large = image.scaled(size, size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        cage = CageGrid(frame=(0, 0, size, size))
        cage.validate_grid()
        timings = []
        for n in range(6):
            cage.points[5] = (size*.48+n, size*.21)
            start = time.perf_counter()
            rendered = gpu.cage(large, QRectF(0, 0, size, size), cage, QTransform(), QRectF(0, 0, size, size))
            assert rendered is not None
            timings.append((time.perf_counter()-start)*1000)
        print(size, "cage GPU warm median:", round(statistics.median(timings[1:]), 2), "ms")
    print("GPU draws:", gpu.draws, "uploads:", gpu.uploads)
    print("Saved visual checks to", output)
    gpu.close()


if __name__ == "__main__":
    main()
