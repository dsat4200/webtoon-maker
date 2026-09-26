"""Manual isolated mesh benchmark using only the copied saved chapter.

Compare complete full-resolution RGBA output with the preserved exhaustive
triangle mapper. No repository, MainWindow, Blender, or settings are opened.
Coordinate CPU timing with other performance runs before starting this file.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts" / "drawing-stutters-20260926"
CHAPTER_ID = "0a72f08009294aa0a3d14e6a38e22bbb"
OBJECT_ID = "d05523e0b3084570abefb49981eafe2a"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--label", default="saved-mesh")
parser.add_argument("--repeats", type=int, default=3)
args = parser.parse_args()
assert 1 <= args.repeats <= 10
out = (ARTIFACTS / args.label).resolve()
assert out.is_relative_to(ARTIFACTS.resolve()) and out != ARTIFACTS.resolve()
assert not out.exists(), "Use a new label to preserve previous measurements"
out.mkdir(parents=True)
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import numpy as np
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QImage, QPolygonF, QTransform

from comic_editor.core.models import modifier_from_dict
from comic_editor.ui import distort_rendering as rendering


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def image_bytes(image):
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    return bytes(rgba.constBits())


def quad_transform(frame, points):
    rectangle = QRectF(*frame)
    source = QPolygonF([rectangle.topLeft(), rectangle.topRight(),
                        rectangle.bottomRight(), rectangle.bottomLeft()])
    result = QTransform.quadToQuad(source, QPolygonF([QPointF(*p) for p in points]))
    assert isinstance(result, QTransform) and result.isInvertible()
    return result


chapter_path = ARTIFACTS / "project-copy" / "chapters" / CHAPTER_ID / "chapter.json"
chapter = json.loads(chapter_path.read_text(encoding="utf-8"))
obj = next(item for item in chapter["objects"] if item["id"] == OBJECT_ID)
raw_modifier = next(item for item in chapter["modifiers"] if item["id"] == obj["modifier_ids"][0])
assert obj["name"] == "slam.png" and raw_modifier["type"] == "distort_mesh_warp"
assert len(obj["modifier_ids"]) == 1 and raw_modifier["intensity"] == 100
assert raw_modifier["parameter_masks"] == {} and not raw_modifier["muted"]
modifier = modifier_from_dict(raw_modifier)
image_path = chapter_path.parent / "images" / OBJECT_ID / obj["source_filename"]
source = QImage(str(image_path))
assert not source.isNull() and [source.width(), source.height()] == obj["pixel_size"] == [1280, 1789]
source_bounds = QRectF(0, 0, source.width(), source.height())
placement = quad_transform(obj["transform_frame"], obj["transform_quad"])
layers = {item["id"]: item for item in chapter["layers"]}
ancestors = []
parent_id = obj["parent_layer_id"]
while parent_id:
    layer = layers[parent_id]
    ancestors.append(layer)
    parent_id = layer["parent_id"]
for layer in ancestors:
    if layer["transform_frame"] is not None:
        parent = quad_transform(layer["transform_frame"], layer["transform_quad"])
    else:
        parent = QTransform.fromTranslate(*layer["translation"])
    placement = placement * parent
output_bounds = rendering.distort_bounds(source_bounds, modifier, placement)

baseline_path = ARTIFACTS / "baseline-source" / "comic_editor" / "ui" / "distort_rendering.py"
baseline_source = baseline_path.read_text(encoding="utf-8")
tree = ast.parse(baseline_source)
function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_triangle_map")
baseline_function_source = ast.get_source_segment(baseline_source, function)
namespace = {"np": np}
exec(compile(ast.Module(body=[function], type_ignores=[]), str(baseline_path), "exec"), namespace)
exhaustive = namespace["_triangle_map"]
assert "blocks" not in baseline_function_source
accelerated = rendering._triangle_map
input_hashes = {str(path.relative_to(ROOT)): sha(path) for path in (chapter_path, image_path, baseline_path)}
mesh = rendering._mesh(modifier, np.asarray(modifier.frame, np.float64), modifier.parameters)
metadata = {
    "purpose": "Isolate spatial triangle rejection with finished full-resolution RGBA output; supplementary to sustained input replay",
    "source": str(image_path.relative_to(ROOT)), "source_size": obj["pixel_size"],
    "object": obj, "ancestor_placements": [{key: layer[key] for key in ("id", "translation", "transform_frame", "transform_quad")} for layer in ancestors],
    "modifier": raw_modifier,
    "local_to_world": [getattr(placement, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)],
    "source_bounds": [source_bounds.x(), source_bounds.y(), source_bounds.width(), source_bounds.height()],
    "output_bounds": [output_bounds.x(), output_bounds.y(), output_bounds.width(), output_bounds.height()],
    "mesh_vertices": len(mesh[0]), "mesh_faces": len(mesh[2]),
    "baseline_function_sha256": hashlib.sha256(baseline_function_source.encode()).hexdigest(),
    "input_sha256": input_hashes, "numpy": np.__version__,
    "method": "Alternating order, three repeats by default; identical current rendering function with only triangle mapper substituted; decoding/mesh metadata/equality outside timed interval; complete render includes its normal mesh setup, sampling and RGBA conversion. No draft or asynchronous job.",
}
(out / "inputs.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
(out / "exhaustive_triangle_map.py").write_text(baseline_function_source + "\n", encoding="utf-8")

samples = {"exhaustive": [], "accelerated": []}
reference_bytes = None
output_hash = None
for repeat in range(args.repeats):
    order = [("exhaustive", exhaustive), ("accelerated", accelerated)]
    if repeat % 2:
        order.reverse()
    for name, mapper in order:
        triangle_ns = [0]
        triangle_calls = [0]

        def measured_mapper(*values):
            start = time.perf_counter_ns()
            result = mapper(*values)
            triangle_ns[0] += time.perf_counter_ns() - start
            triangle_calls[0] += 1
            return result

        rendering._triangle_map = measured_mapper
        started = time.perf_counter_ns()
        result = rendering.render_distort(source, source_bounds, modifier, placement, output_bounds)
        elapsed_ms = (time.perf_counter_ns() - started) / 1e6
        pixels = image_bytes(result)
        if reference_bytes is None:
            reference_bytes = pixels
            output_hash = hashlib.sha256(pixels).hexdigest()
            result.save(str(out / "exhaustive.png"))
        assert pixels == reference_bytes, f"Pixel difference in {name} repetition {repeat}"
        if name == "accelerated" and repeat == 0:
            result.save(str(out / "accelerated.png"))
        sample = {"repeat": repeat, "complete_rgba_ms": elapsed_ms,
                  "triangle_ms": triangle_ns[0] / 1e6, "triangle_calls": triangle_calls[0],
                  "pixel_equal": True, "size": [result.width(), result.height()]}
        samples[name].append(sample)
        print(json.dumps({"algorithm": name, **sample}), flush=True)
rendering._triangle_map = accelerated
assert input_hashes == {str(path.relative_to(ROOT)): sha(path) for path in (chapter_path, image_path, baseline_path)}
summary = {}
for name, values in samples.items():
    summary[name] = {measure: {"median_ms": statistics.median(item[measure] for item in values),
                               "max_ms": max(item[measure] for item in values)}
                     for measure in ("complete_rgba_ms", "triangle_ms")}
summary["complete_speedup"] = summary["exhaustive"]["complete_rgba_ms"]["median_ms"] / summary["accelerated"]["complete_rgba_ms"]["median_ms"]
summary["triangle_speedup"] = summary["exhaustive"]["triangle_ms"]["median_ms"] / summary["accelerated"]["triangle_ms"]["median_ms"]
summary["all_rgba_pixels_equal"] = True
summary["rgba_sha256"] = output_hash
summary["inputs_unchanged"] = True
(out / "results.json").write_text(json.dumps({"samples": samples, "summary": summary}, indent=2), encoding="utf-8")
print(json.dumps(summary, indent=2), flush=True)
