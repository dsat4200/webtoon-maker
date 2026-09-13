"""Current full-resolution compound-child gesture timings with real text.

Run `python tests/benchmark_compound_children.py`. This uses synthetic artwork,
large local coordinates, temporary settings and actual MainWindow callbacks.
Open tails retain tapered core widths and varying per-anchor outline widths.
"""
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
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from comic_editor.core import settings
from comic_editor.core.models import BoundGeometry, ChapterDocument, ShapeStyle, TextObject, OutlineModifier, BlurModifier
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.main_window import MainWindow


def measure(window, family, open_tail, effects, gesture):
    chapter = ChapterDocument(height=9800)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 9800))
    page.fill_color, page.border_width = None, 0
    style = ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF000000", outline_thickness=4)
    bubble = chapter.add_layer(page.layer_id, "Bubble", BoundGeometry.rectangle(150, 22707, 420, 213), style=style)
    bubble.compound_enabled, bubble.translate_y = True, -13396
    for node in bubble.bound.nodes:
        node.roundness, node.roundness_enabled = 106.5, True
    bound = BoundGeometry.polygon([(360, 22800), (438, 23086), (647, 23023)])
    if open_tail:
        bound.closed = False
        for node, width, outline in zip(bound.nodes, (10., 3.2, .1), (1., 1.3566, 1.5224)):
            node.width_multiplier, node.outline_multiplier = width, outline
    tail = chapter.add_layer(bubble.layer_id, "Tail", bound,
        layer_kind="open_shape" if open_tail else "bounded", style=copy.deepcopy(style))
    tail.shape_style.base_thickness = 12
    text = chapter.add_object(bubble.layer_id, TextObject(
        text="Why is this bubble so slow?", font_family=family, font_size=32))
    if effects:
        for modifier in (OutlineModifier(thickness=3), BlurModifier(strength=2)):
            chapter.add_modifier(modifier, [("layer", bubble.layer_id)])
    window._set_chapter(chapter, TileStore())
    canvas = window.canvas
    canvas.resize(900, 700)
    canvas.center_x, canvas.center_y, canvas.scale = 400, 9500, 1.2
    canvas.settings.snap_to_grid, canvas.settings.grid_overlay_visible = False, False
    canvas.settings.transform_mode = "uniform"
    canvas._effect_jobs.request = lambda *args, **kwargs: False
    canvas.set_selection("layer", tail.layer_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    with_text = canvas.grab().toImage()
    text.visible = False
    canvas.documentChanged.emit(QRectF())
    assert canvas.grab().toImage() != with_text, "Real text must be visible"
    text.visible = True
    canvas.documentChanged.emit(QRectF())
    canvas.grab()
    bounds = QRectF(*tail.bound.bbox())
    point = bounds.center() if gesture == "move" else bounds.bottomRight()
    start = canvas.layer_world_transform(tail.layer_id).map(point)
    if gesture != "node":
        assert canvas._begin_geometry_transform(start)
        canvas._transform_drag_mode = "translate" if gesture == "move" else "scale"
        canvas._transform_handle_index = 2
    frames = []
    for index in range(5):
        started = time.perf_counter()
        if gesture == "node":
            tail.bound.nodes[1].x += 3
            tail.bound.nodes[1].y += 2
        else:
            canvas._update_geometry_transform_preview(start+QPointF(3+index*2, 2+index))
        canvas.documentChanged.emit(QRectF())
        canvas.grab()
        frames.append(round((time.perf_counter()-started)*1000, 2))
    canvas._geometry_transform_target = None
    canvas._transform_start_quad = canvas._transform_preview_quad = None
    canvas._transform_drag_mode = canvas._model_before = None
    return {"tail": "open" if open_tail else "closed", "effects": effects,
            "gesture": gesture, "median_ms": round(statistics.median(frames), 2), "frame_ms": frames}


def main():
    app = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    families = QFontDatabase.applicationFontFamilies(font_id)
    assert families, "Windows Segoe UI must be available"
    original_settings_path = settings.settings_path
    with tempfile.TemporaryDirectory() as temporary:
        settings.settings_path = lambda: Path(temporary)/"settings.json"
        window = MainWindow()
        try:
            for open_tail, effects in ((True, False), (True, True), (False, False)):
                for gesture in ("move", "resize", "node"):
                    print(json.dumps(measure(window, families[0], open_tail, effects, gesture)), flush=True)
        finally:
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
