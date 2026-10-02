"""Isolated synthetic viewport timing and baseline pixel comparison.

Run with --baseline to import the preserved pre-change source. Writes timings,
allocation sizes, and rendered images under .artifacts/tile-evaluator-20261001.
No live project, editor settings, or Blender process is touched.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline", action="store_true")
parser.add_argument("--width", type=int, default=8192)
parser.add_argument("--height", type=int, default=1024)
parser.add_argument("--native", action="store_true")
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts/tile-evaluator-20261001"
label = "baseline" if args.baseline else "current"
OUT = ARTIFACTS / f"{label}-{args.width}x{args.height}{'-native' if args.native else ''}"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ARTIFACTS / "baseline-source" if args.baseline else ROOT))
os.environ["QT_QPA_PLATFORM"] = "windows" if args.native else "offscreen"

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication
from comic_editor.core.models import (BoundGeometry, ChapterDocument, RasterObject,
    BlurModifier, BrightnessContrastModifier, HueSaturationLightnessModifier, SharpnessModifier)
from comic_editor.core.settings import EditorSettings
from comic_editor.core import settings
from comic_editor.core.tiles import TileStore
from comic_editor.ui import canvas as canvas_module, effect_pipeline
from comic_editor.ui.canvas import CanvasWidget

canvas_module.create_network_manager = lambda _: None
settings.settings_path = lambda: OUT / "isolated-settings.json"
app = QApplication.instance() or QApplication([])
measurements = []
for case in ("generic-blur", "stage-blur-sharpen"):
    chapter = ChapterDocument(width=args.width, height=args.height, document_kind="asset")
    page = chapter.add_page("Synthetic", BoundGeometry.rectangle(0, 0, args.width, args.height))
    obj = chapter.add_object(page.layer_id, RasterObject(object_id="synthetic", interaction_rect=(0, 0, args.width, args.height)))
    modifiers = [BrightnessContrastModifier(brightness=12, modifier_id="brightness"),
                 BlurModifier(strength=7, modifier_id="blur"),
                 HueSaturationLightnessModifier(hue=30, modifier_id="hue")]
    if case == "stage-blur-sharpen":
        modifiers.insert(2, SharpnessModifier(radius=2, modifier_id="sharpen"))
    obj.modifier_ids = [m.modifier_id for m in modifiers]
    chapter.modifiers.update({m.modifier_id: m for m in modifiers})
    tiles = TileStore()
    origin = args.width//2//256*256
    for x, y in {(0, 0), ((args.width-1)//256, (args.height-1)//256),
                 *((x, y) for x in range(origin//256-2, origin//256+5) for y in range(4))}:
        tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        tile.fill(Qt.transparent)
        p = QPainter(tile)
        p.fillRect(13, 17, 200, 170, QColor(210, (x*47+y*51)%255, 80, 193))
        p.fillRect(77, 55, 100, 115, QColor(50, 170, 230, 225))
        p.end()
        tiles.set_tile(obj.object_id, (x, y), tile)
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(chapter, tiles)
    canvas._interactive_render = True
    canvas._effect_region_requests = True
    canvas._projection_exact = True
    allocations = []
    original_empty = effect_pipeline.empty_image
    def empty(bounds):
        allocations.append(int(bounds.width()*bounds.height()))
        return original_empty(bounds)
    effect_pipeline.empty_image = empty
    canvas_module.empty_image = empty
    rows = []
    for name, offset, edit in [("cold", 0, None), ("warm", 0, None), ("pan", 256, None),
                               ("edit-final", 256, 65), ("edit-blur", 256, 13), ("edit-source", 256, None)]:
        if name == "edit-final":
            modifiers[-1].hue = edit
        elif name == "edit-blur":
            modifiers[1].strength = edit
        elif name == "edit-source":
            tile = tiles.tile(obj.object_id, (origin//256+1, 1))
            p = QPainter(tile)
            p.fillRect(90, 90, 35, 35, QColor("lime"))
            p.end()
            canvas._invalidate_render_bounds("object", obj.object_id)
        view = QRectF(origin+offset, 192, 768, 512)
        canvas._effect_viewport_world = view
        output = QImage(768, 512, QImage.Format_ARGB32_Premultiplied)
        output.fill(Qt.transparent)
        p = QPainter(output)
        p.translate(-view.x(), -view.y())
        allocations.clear()
        start = time.perf_counter()
        canvas._render_modified_object(p, obj, 1., view)
        elapsed = (time.perf_counter()-start)*1000
        p.end()
        filename = f"{case}-{name}.png"
        output.save(str(OUT / filename))
        rows.append({"request": name, "ms": elapsed, "largest_allocation_pixels": max(allocations, default=0),
                     "allocated_pixels": sum(allocations), "sha256": hashlib.sha256(bytes(output.constBits())).hexdigest(),
                     "file": filename, "cache_bytes": canvas._modifier_render_cache_bytes,
                     "retained_bytes": canvas._effect_jobs.retained_bytes})
        print(json.dumps({"case": case, **rows[-1]}), flush=True)
    measurements.append({"case": case, "requests": rows})
    effect_pipeline.empty_image = original_empty
    canvas_module.empty_image = original_empty
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()
    app.processEvents()
(OUT / "results.json").write_text(json.dumps({"width": args.width, "height": args.height,
    "baseline": args.baseline, "source": str(Path(sys.path[0])), "measurements": measurements}, indent=2))
