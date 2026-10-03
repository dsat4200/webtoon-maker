"""Measure scalar blur allocation against a preserved source function.

Both implementations share the same warmed pyramid; each output is compared
bit for bit. This isolates radius selection from pyramid construction.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import statistics
import sys
import time
import tracemalloc

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from comic_editor.ui import modifier_rendering as rendering


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    tree = ast.parse(args.reference_source.read_text(encoding='utf-8'))
    definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == '_variable_blur')
    namespace = vars(rendering).copy()
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(args.reference_source), 'exec'), namespace)
    reference = namespace['_variable_blur']
    rows = []
    for size in ((273, 257), (1025, 1023), (1920, 1080)):
        width, height = size
        values = np.random.default_rng(27).integers(0, 256, (height, width, 4), np.uint8)
        values[..., :3] = np.minimum(values[..., :3], values[..., 3:4])
        pixels = values.astype(np.float32) / 255.
        for algorithm in ('normal', 'legacy'):
            cache = rendering.BlurPyramidCache()
            cache.pyramid(pixels, algorithm)
            timings = {'before': [], 'after': []}
            peaks = {}
            for strength in (-1., 0., .0000005, 1., 3., 7., 11.25, 15., 63., 100., 101.):
                outputs = []
                for name, operation in (('before', reference), ('after', rendering._variable_blur)):
                    start = time.perf_counter()
                    outputs.append(operation(pixels, strength, cache, algorithm))
                    timings[name].append((time.perf_counter()-start)*1000)
                np.testing.assert_array_equal(*outputs)
            for name, operation in (('before', reference), ('after', rendering._variable_blur)):
                tracemalloc.start()
                operation(pixels, 11.25, cache, algorithm)
                peaks[name] = tracemalloc.get_traced_memory()[1]
                tracemalloc.stop()
            row = {'size': size, 'algorithm': algorithm, 'pixels_identical': True,
                   'median_ms': {name: statistics.median(samples) for name, samples in timings.items()},
                   'peak_bytes': peaks}
            rows.append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, indent=2), encoding='utf-8')
    print(json.dumps(rows, indent=2))


if __name__ == '__main__':
    main()
