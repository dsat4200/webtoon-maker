"""Compare preserved conversion/color kernels including exact bits and peak allocation."""
from pathlib import Path
import argparse
import ast
import json
import os
from statistics import median
import sys
import time
import tracemalloc

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--baseline-source', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
output = args.output.resolve()
assert output.is_relative_to(root / '.artifacts') and not output.exists()
output.mkdir(parents=True)
sys.path.insert(0, str(root))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import numpy as np
from PIL import Image
from PySide6.QtGui import QImage
from comic_editor.core.pixel_arrays import normalized_bytes, truncated_bytes
from comic_editor.core.tiles import TileStore
from comic_editor.ui import modifier_rendering as modifiers, distort_rendering as distort

def reference(relative, names, globals=None):
    tree = ast.parse((args.baseline_source / relative).read_text(encoding='utf-8'))
    namespace = {'np': np, 'QImage': QImage, 'PILImage': Image, **(globals or {})}
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(functions, type_ignores=[]), str(relative), 'exec'), namespace)
    return namespace

old = reference('comic_editor/ui/modifier_rendering.py', {'_straight', '_hsl_effect_dense', '_qimage_premultiplied', '_premultiplied_qimage'})
old_warp = reference('comic_editor/ui/distort_rendering.py', {'_byte_pixels'})
old_bounds = reference('comic_editor/core/tiles.py', {'_alpha_bbox'})
# Check awkward finite/signed-zero/NaN colors as well as ordinary project pixels.
edges = np.array([0., -0., 1e-8, .1, .5, 1., 2., -1., np.inf, -np.inf, np.nan], np.float32)
rgb = np.stack(np.meshgrid(edges, edges, edges, indexing='ij'), axis=-1).reshape(-1, 1, 3)
edge_pixels = np.concatenate([rgb, np.full((*rgb.shape[:2], 1), .7, np.float32)], axis=-1)
with np.errstate(all='ignore'):
    for h, s, l in ((30., 0., 0.), (13.75, -21.25, 31.5)):
        before = old['_hsl_effect_dense'](edge_pixels, h, s, l)
        after = modifiers._hsl_effect_dense(edge_pixels, h, s, l)
        np.testing.assert_array_equal(before.view(np.uint32), after.view(np.uint32))

rows = []
def compare(name, before, after):
    expected, actual = before(), after()
    if isinstance(expected, QImage):
        assert actual == expected
    elif isinstance(expected, np.ndarray):
        assert actual.dtype == expected.dtype
        np.testing.assert_array_equal(actual.view(np.uint8), expected.view(np.uint8))
    else:
        assert expected == actual
    row = {'operation': name, 'exact_bits': True}
    for label, function in (('before', before), ('after', after)):
        samples = []
        for _ in range(4):
            start = time.perf_counter()
            result = function()
            samples.append((time.perf_counter()-start)*1000)
            del result
        tracemalloc.start()
        result = function()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        del result
        row[label] = {'median_ms': median(samples), 'peak_python_bytes': peak}
    rows.append(row)

for width, height in ((1920, 1080), (1533, 4123)):
    raw = np.random.default_rng(31).integers(0, 256, (height, width, 4), np.uint8)
    pixels = raw.astype(np.float32)/255.
    pixels[..., :3] *= pixels[..., 3:4]
    image = QImage(raw.data, width, height, raw.strides[0], QImage.Format_RGBA8888_Premultiplied).copy().convertToFormat(QImage.Format_ARGB32_Premultiplied)
    prefix = f'{width}x{height}:'
    compare(prefix+'normalize', lambda: raw.astype(np.float32)/255., lambda: normalized_bytes(raw))
    compare(prefix+'quantize', lambda: old['_premultiplied_qimage'](pixels), lambda: modifiers._premultiplied_qimage(pixels))
    compare(prefix+'warp_quantize', lambda: old_warp['_byte_pixels'](pixels), lambda: distort._byte_pixels(pixels))
    compare(prefix+'straight', lambda: old['_straight'](pixels), lambda: modifiers._straight(pixels))
    compare(prefix+'hsl', lambda: old['_hsl_effect_dense'](pixels, 31.25, -12.5, 22.75), lambda: modifiers._hsl_effect_dense(pixels, 31.25, -12.5, 22.75))
    compare(prefix+'alpha_bounds', lambda: old_bounds['_alpha_bbox'](image), lambda: TileStore._alpha_bbox(image))
report = {'measurements': rows, 'edge_hsl_exact_bits': True,
          'peak_note': 'tracemalloc tracks NumPy/Python allocations, not every native Qt/Pillow buffer.'}
(output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
