"""Opt-in canvas text-outline slider and typing latency measurements.

Run ``python tests/benchmark_canvas_text_outlines.py`` from the repository root.
"""
from __future__ import annotations

import os
from pathlib import Path
from statistics import median
import sys
from time import perf_counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont, QFontDatabase, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSlider

from comic_editor.core.models import BoundGeometry, ChapterDocument, OutlineModifier, TextObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls


def report(label, samples):
    ordered = sorted(samples[5:])
    print(f"  {label}: median={median(ordered):.2f}ms; "
          f"p95={ordered[int(len(ordered)*.95)]:.2f}ms")


def measure(target_kind, size, font_size, font_family):
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    chapter = ChapterDocument(height=720)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 720))
    page.fill_color, page.border_width = None, 0
    canvas.set_document(chapter, TileStore())
    parent = (chapter.add_layer(page.layer_id, "Text", layer_kind="text_container")
              if target_kind == "container" else page)
    rect = QRectF(100, 100, *size)
    obj = chapter.add_object(parent.layer_id, TextObject(
        text="REALTIME\nText outlines", font_family=font_family,
        font_size=font_size, layout_mode="free",
        x=rect.x(), y=rect.y(), width=rect.width(), height=rect.height(), margin=0,
        transform_quad=canvas._rect_quad(rect),
    ))
    modifier = OutlineModifier(thickness=4, color="#FFFF0000")
    target = ("layer", parent.layer_id) if target_kind == "container" else ("object", obj.object_id)
    chapter.add_modifier(modifier, [target])
    image = QImage(chapter.width, chapter.height, QImage.Format_ARGB32_Premultiplied)
    started = perf_counter()
    canvas.render_preview(image)
    cold = (perf_counter()-started)*1000
    samples = []
    for index in range(40):
        modifier.thickness = 1 + index*.6
        modifier.opacity = 70 + index % 20
        started = perf_counter()
        canvas.render_preview(image)
        samples.append((perf_counter()-started)*1000)
    print(f"{target_kind} {size[0]}x{size[1]}: cold={cold:.2f}ms; "
          f"distance builds={canvas._outline_distance_cache.computations}; "
          f"source entries={len(canvas._modifier_source_cache)}; "
          f"source={canvas._modifier_source_cache_bytes/1048576:.2f}MiB; "
          f"distance={canvas._outline_distance_cache.bytes/1048576:.2f}MiB; "
          f"results={canvas._modifier_render_cache_bytes/1048576:.2f}MiB")
    report("canvas render_preview slider edits", samples)

    canvas.resize(1080, 720)
    canvas.center_x, canvas.center_y, canvas.scale = 540, 360, 1
    canvas.set_selection(*target)
    controls = ModifierControls(canvas)
    controls.refresh()
    slider = next(item for item in controls._cards[modifier.modifier_id].findChildren(QSlider)
                  if item.maximum() == 25)
    canvas.show()
    canvas.activateWindow()
    canvas.setFocus()
    QApplication.processEvents()
    canvas.grab()
    slider.sliderPressed.emit()
    ui_samples = []
    for index in range(30):
        started = perf_counter()
        slider.setValue(1+index % 25)
        canvas.grab()
        ui_samples.append((perf_counter()-started)*1000)
    slider.sliderReleased.emit()
    report("1080x720 widget slider signal + full paint", ui_samples)

    canvas.set_selection("object", obj.object_id)
    canvas.start_text_edit()
    canvas._text_caret_timer.stop()
    canvas._text_caret_visible = False
    canvas.grab()
    typing_samples = []
    preview_samples = []
    for index in range(25):
        started = perf_counter()
        QTest.keyClicks(canvas, chr(ord("a")+index))
        canvas.grab()
        typing_samples.append((perf_counter()-started)*1000)
        started = perf_counter()
        canvas.render_preview(image)
        preview_samples.append((perf_counter()-started)*1000)
    report("typing one character + widget full paint", typing_samples)
    report("export after changed text", preview_samples)
    canvas.commit_active_text_edit()

    if size == (850, 340) and target_kind == "container":
        obj.text = "REALTIME\nText outlines"
        modifier.thickness, modifier.opacity = 5, 100
        canvas.render_preview(image)
        output = Path(__file__).resolve().parents[1]/".artifacts"/"text-outlines"
        output.mkdir(parents=True, exist_ok=True)
        image.save(str(output/"outlined-text.png"))
        canvas.documentChanged.emit(None)
        canvas.grab().save(str(output/"outlined-text-widget.png"))
        print(f"  Visual samples: {output}")
    controls.deleteLater()
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    for candidate in (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/segoeui.ttf")):
        if candidate.is_file():
            font_id = QFontDatabase.addApplicationFont(str(candidate))
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                break
    else:
        families = QFontDatabase.families()
    if not families:
        raise RuntimeError("No real font available for text outline benchmark")
    family = families[0]
    app.setFont(QFont(family, 10))
    print(f"Glyph font: {family}")
    for size, font in [((320, 120), 36), ((850, 340), 96)]:
        for kind in ("object", "container"):
            measure(kind, size, font, family)
    app.processEvents()
