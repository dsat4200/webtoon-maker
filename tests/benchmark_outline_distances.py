"""Compare silhouette scans and exact distance fields with the preserved source."""
import argparse
import ast
import json
import os
from pathlib import Path
from statistics import median
import sys
import time
import tracemalloc

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import numpy as np
from comic_editor.ui import modifier_rendering as current

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
output = args.output.resolve()
assert output.is_relative_to(root/'.artifacts') and not output.exists()
source = root/'.artifacts/optimization-20261002/baseline-source/comic_editor/ui/modifier_rendering.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
         and node.name in ('OutlineDistanceCache', '_outside_distance')]
namespace = dict(vars(current))
exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
original = namespace['OutlineDistanceCache']
rows = []
for width, height in ((273, 257), (1920, 1080)):
    alpha = np.random.default_rng(91).choice(np.array((0., .4, 1.), np.float32),
                                             (height, width), p=(.9,.05,.05))
    for operation in ('cold', 'warm'):
        row = {'size': [width, height], 'operation': operation}
        reference = original().field(alpha)
        for name, factory in (('preserved', original), ('current', current.OutlineDistanceCache)):
            cache = factory()
            if operation == 'warm':
                cache.field(alpha)
            times = []
            for _ in range(4):
                if operation == 'cold':
                    cache.clear()
                started = time.perf_counter()
                actual = cache.field(alpha)
                times.append((time.perf_counter()-started)*1000)
                np.testing.assert_array_equal(actual[0], reference[0])
                assert actual[1:] == reference[1:]
            if operation == 'cold':
                cache.clear()
            tracemalloc.start()
            cache.field(alpha)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            row[name] = {'median_ms': median(times), 'peak_bytes': peak, 'samples_ms': times}
        rows.append(row)
output.parent.mkdir(parents=True, exist_ok=True)
report = {'measurements': rows, 'every_float_bit_matches': True}
output.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps(report, indent=2))
