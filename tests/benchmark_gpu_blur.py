"""Compare exact scalar blur with detached GPU work, including changed pixels."""
from concurrent.futures import ThreadPoolExecutor
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
os.environ['QT_QPA_PLATFORM'] = 'windows' if sys.platform == 'win32' else 'offscreen'
import numpy as np
from PySide6.QtWidgets import QApplication
from comic_editor.render.gpu.worker import GpuWorker
from comic_editor.ui import point_lut, gpu_effects
from comic_editor.ui.modifier_rendering import _variable_blur, BlurPyramidCache

app = QApplication.instance() or QApplication([])
worker = GpuWorker()
if not worker.ready.wait(10) or not worker.available:
    worker.close()
    raise RuntimeError(worker.reason)
rows = []
try:
    point_lut._worker = worker
    with ThreadPoolExecutor(1) as jobs:
        for width, height in ((1025, 1025), (1920, 1080)):
            for algorithm in ('normal', 'legacy'):
                pixels = np.random.default_rng(31).random((height, width, 4), dtype=np.float32)
                pixels[..., :3] *= pixels[..., 3:4]
                cache = BlurPyramidCache()
                with patch.object(gpu_effects, 'scalar_blur', return_value=None):
                    expected = _variable_blur(pixels, 11.25, cache, algorithm)
                    times = []
                    for _ in range(4):
                        started = time.perf_counter()
                        actual = _variable_blur(pixels, 11.25, cache, algorithm)
                        times.append((time.perf_counter()-started)*1000)
                        np.testing.assert_array_equal(actual, expected)
                row = {'size': [width, height], 'algorithm': algorithm,
                       'cpu_warm_median_ms': median(times), 'cpu_samples_ms': times}
                for scenario in ('same-input', 'radius-edit', 'pixel-edit'):
                    times = []
                    for index in range(4):
                        strength = 11.25 + (index*.5 if scenario == 'radius-edit' else 0)
                        if scenario == 'pixel-edit':
                            pixels[17+index, 21, :3] *= .93
                        started = time.perf_counter()
                        actual = jobs.submit(_variable_blur, pixels, strength, None, algorithm).result(timeout=15)
                        times.append((time.perf_counter()-started)*1000)
                        with patch.object(gpu_effects, 'scalar_blur', return_value=None):
                            reference = _variable_blur(pixels, strength, cache, algorithm)
                        np.testing.assert_array_equal(actual, reference)
                    row[scenario] = {'first_ms': times[0], 'warm_median_ms': median(times[1:]),
                                     'samples_ms': times}
                row['gpu'] = dict(worker.stats)
                rows.append(row)
finally:
    point_lut._worker = None
    worker.close()
report = {'measurements': rows, 'every_float_bit_matches': True}
output.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
