"""Run `python tests/benchmark_bubble_interactions.py` for exact frame timings.

Measures MainWindow callbacks plus a synchronous canvas repaint for a compound
500x220 ellipse, tail and text. Temporary settings keep user preferences intact.
Each row includes the first cold frame and later frames; no async draft images
are timed. Geometry resizing changes the original ellipse each frame.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF
from PySide6.QtGui import QFont, QFontDatabase, QRawFont
from PySide6.QtWidgets import QApplication

from comic_editor.core import settings
from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, OutlineModifier,
    ScreamModifier, ShapeStyle, TextObject,
)
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.main_window import MainWindow


def measure(window, effect_stack, gesture, font_family):
    chapter = ChapterDocument(height=700)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 700))
    page.fill_color, page.border_width = None, 0
    style = ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF111111", outline_thickness=4)
    bound = BoundGeometry.rectangle(220, 240, 500, 220)
    bound.primitive = "ellipse"
    bubble = chapter.add_layer(page.layer_id, "Bubble", bound, style=style)
    bubble.compound_enabled = True
    tail = chapter.add_layer(bubble.layer_id, "Tail", BoundGeometry.polygon(
        [(440, 420), (550, 530), (500, 420)]), style=copy.deepcopy(style))
    tail.compound_operation = "add"
    text = chapter.add_object(bubble.layer_id, TextObject(
        text="What? I haven't even started yet!", font_size=32, font_family=font_family))
    modifiers = [ScreamModifier(height=47, width=69, roundness=0)]
    if effect_stack == "scream_outline_blur":
        modifiers += [OutlineModifier(thickness=3), BlurModifier(strength=2)]
    for modifier in modifiers:
        chapter.add_modifier(modifier, [("layer", bubble.layer_id)])
    window._set_chapter(chapter, TileStore())
    canvas = window.canvas
    canvas.resize(900, 700)
    canvas.center_x, canvas.center_y, canvas.scale = 450, 350, 1
    canvas.settings.snap_to_grid = False
    canvas.settings.grid_overlay_visible = False
    canvas.settings.transform_mode = "uniform"
    canvas.set_selection("layer", bubble.layer_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas._effect_jobs.request = lambda *args, **kwargs: False
    with_text = canvas.grab().toImage()
    # Verify the actual scene contains glyphs, not only a valid font/layout.
    text.visible = False
    canvas.documentChanged.emit(QRectF())
    without_text = canvas.grab().toImage()
    assert with_text != without_text, "The benchmark text did not render"
    text.visible = True
    canvas.documentChanged.emit(QRectF())
    canvas.grab()
    if gesture != "geometry_resize":
        start = QPointF(470, 350) if gesture == "translate" else QPointF(720, 460)
        assert canvas._begin_geometry_transform(start)
        canvas._transform_drag_mode = "translate" if gesture == "translate" else "scale"
        canvas._transform_handle_index = 2
    samples = []
    for index in range(5):
        started = time.perf_counter()
        if gesture == "translate":
            canvas._update_geometry_transform_preview(QPointF(480+index*4, 355+index*2))
        elif gesture == "uniform_resize":
            canvas._update_geometry_transform_preview(QPointF(730+index*5, 464.4+index*2.2))
        else:
            bubble.bound = BoundGeometry.rectangle(220, 240, 510+index*8, 224+index*4)
            bubble.bound.primitive = "ellipse"
        # Include connected application updates, not just the geometry helper.
        canvas.documentChanged.emit(QRectF())
        canvas.grab()
        samples.append(round((time.perf_counter()-started)*1000, 2))
    canvas._geometry_transform_target = None
    canvas._transform_preview_quad = None
    canvas._transform_start_quad = None
    canvas._transform_drag_mode = None
    canvas._model_before = None
    return {"stack": effect_stack, "gesture": gesture, "frame_ms": samples,
            "median_ms": round(statistics.median(samples), 2)}


def main():
    app = QApplication.instance() or QApplication([])
    # Offscreen Qt on Windows may not discover installed system fonts.
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    families = QFontDatabase.applicationFontFamilies(font_id)
    if not families:
        raise RuntimeError("Segoe UI could not be loaded from C:/Windows/Fonts/segoeui.ttf")
    font_family = families[0]
    font = QFont(font_family)
    font.setPixelSize(32)
    raw_font = QRawFont.fromFont(font)
    glyphs = raw_font.glyphIndexesForString("Text")
    assert raw_font.isValid() and glyphs and all(glyphs)
    assert all(not raw_font.pathForGlyph(glyph).isEmpty() for glyph in glyphs)
    original_settings_path = settings.settings_path
    with tempfile.TemporaryDirectory() as temporary:
        settings.settings_path = lambda: Path(temporary) / "settings.json"
        window = None
        try:
            window = MainWindow()
            for stack in ("scream", "scream_outline_blur"):
                for gesture in ("translate", "uniform_resize", "geometry_resize"):
                    print(json.dumps(measure(window, stack, gesture, font_family)), flush=True)
        finally:
            if window is not None:
                window.autosave_timer.stop()
                window.layout_settings_timer.stop()
                window.series_preferences_timer.stop()
                window.canvas._effect_jobs.cancel()
                window.blender_sources.shutdown()
                window._autosave_jobs.shutdown()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                app.processEvents()
            settings.settings_path = original_settings_path
            QFontDatabase.removeApplicationFont(font_id)


if __name__ == "__main__":
    main()
