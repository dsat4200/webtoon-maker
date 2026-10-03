"""Native GPU and exact CPU lookup comparisons, including parameter edits."""
from pathlib import Path
from statistics import median
from unittest.mock import patch
import argparse
import json
import os
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
output = args.output.resolve()
assert output.is_relative_to(root / '.artifacts') and not output.exists()
output.parent.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(root))
os.environ['QT_QPA_PLATFORM'] = 'windows' if sys.platform == 'win32' else os.environ.get('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from comic_editor.core.models import BrightnessContrastModifier, CurvesModifier
from comic_editor.ui.modifier_rendering import apply_modifier_stack
from comic_editor.ui import point_lut

app = QApplication.instance() or QApplication([])
rows = []
for width, height in ((273, 257), (1025, 1025), (1920, 1080)):
    data = np.random.default_rng(19).integers(0, 256, (height, width, 4), np.uint8)
    data[..., :3] = np.minimum(data[..., :3], data[..., 3:4])
    source = QImage(data.data, width, height, data.strides[0], QImage.Format_RGBA8888_Premultiplied).copy()
    modifiers = [BrightnessContrastModifier(brightness=13.25, contrast=23.5, intensity=57.75),
                 CurvesModifier(intensity=83.25, curves={
                     'rgb:master': [[0, 0], [.35, .65], [1, 1]],
                     'rgb:red': [[0, 0], [.4, .2], [1, 1]],
                     'rgb:alpha': [[0, 0], [.5, .7], [1, 1]]}),
                 BrightnessContrastModifier(brightness=-21.25, contrast=-30.5, intensity=66.75)]
    def render(fast=True):
        return apply_modifier_stack(source, modifiers, (0, 0), return_pixels=True, _point_lut=fast)
    expected = render(False)
    row = {'size': [width, height]}
    for kind in ('reference', 'cpu-table', 'native-gpu'):
        if kind == 'native-gpu' and width * height < 1024 * 1024:
            continue
        times = []
        for index in range(5):
            started = time.perf_counter()
            if kind == 'cpu-table':
                with patch.object(point_lut, '_gpu_renderer', return_value=None):
                    actual = render()
            else:
                actual = render(kind != 'reference')
            times.append((time.perf_counter() - started) * 1000)
            np.testing.assert_array_equal(actual, expected)
        row[kind] = {'first_ms': times[0], 'warm_median_ms': median(times[1:]), 'samples_ms': times}
    parameter_times = []
    for index in range(4):
        modifiers[-1].brightness += 1.5
        started = time.perf_counter()
        actual = render()
        parameter_times.append((time.perf_counter() - started) * 1000)
        np.testing.assert_array_equal(actual, render(False))
    row['parameter_edit_ms'] = parameter_times
    if point_lut._gpu is not None:
        gpu = point_lut._gpu
        row['gpu'] = {'available': gpu.available, 'reason': gpu.reason, 'uploads': gpu.uploads,
                      'draws': gpu.draws, 'readbacks': gpu.readbacks, 'bytes': gpu.bytes,
                      'budget': gpu.budget, 'compiles': gpu.compiles}
    rows.append(row)
report = {'measurements': rows, 'every_float_bit_matches': True}
output.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
if point_lut._gpu is not None:
    point_lut._gpu.close()
