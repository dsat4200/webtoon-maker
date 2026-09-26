"""Manual isolated 1MP smudge latency/appearance check, with no live document."""
import argparse
import copy
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from comic_editor.ui.distort_rendering import PreparedDistortCache, render_distort
from test_smudge_rendering import modifier, stroke


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
image = QImage(1024, 1024, QImage.Format_ARGB32_Premultiplied)
image.fill(QColor("#FFE8C15C"))
painter = QPainter(image)
for x in range(0, 1024, 96):
    painter.fillRect(x, 0, 48, 1024, QColor("#FF2676B5"))
for y in range(32, 1024, 128):
    painter.fillRect(0, y, 1024, 8, QColor("#FFDF4E48"))
painter.end()
strokes = [stroke((150, 180), (850, 280), identifier="first", radius=32, strength=85),
           stroke((160, 500), (870, 550), identifier="second", radius=40, flow=80, strength=90),
           stroke((190, 760), (820, 760), identifier="third", radius=28, flow=90, strength=85)]
for item, handles in zip(strokes, [((330, 30), (470, 450)),
                                  ((370, 700), (600, 320)), ((300, 550), (620, 940))]):
    for point, handle in zip(item["points"], handles):
        point.update(point_type="bezier", handle=list(handle))
effect = modifier(*strokes)
bounds = QRectF(0, 0, 1024, 1024)
cache = PreparedDistortCache()
records = {}
def capture(label, scale=1.):
    started = time.perf_counter()
    output = render_distort(image, bounds, effect, output_bounds=bounds,
                            pixel_scale=scale, preparation_cache=cache)
    records.setdefault(label, []).append((time.perf_counter()-started)*1000)
    return output

image.save(str(args.output/"source.png"))
native = capture("native_cold")
native.save(str(args.output/"smudged.png"))
assert capture("native_cached") == native
for x in (640, 660, 680, 700):
    effect.parameters["strokes"][-1]["points"][-1]["handle"][0] = x
    capture("native_edit_last_handle")
capture("draft_cold", 224/1024)
for x in (710, 720, 730, 740):
    effect.parameters["strokes"][-1]["points"][-1]["handle"][0] = x
    capture("draft_edit_last_handle", 224/1024)
records["cache"] = {name: getattr(cache, name) for name in ("bytes", "budget", "hits", "misses", "evictions")}
(args.output/"timings.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
print(json.dumps(records, indent=2))
