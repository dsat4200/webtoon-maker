"""Opt-in native OpenGL benchmark and 10% regression gate for 3D frames.

Run this on the same native GPU/driver with ``--write-baseline`` once, then use
``--baseline`` in release checks.  It records render submission, blocking
framebuffer readback, camera-navigation, and object-transform P50/P95
independently; it is not part of the offscreen Qt suite.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import statistics
import sys

import numpy as np

from comic_editor.three_d.renderer.mesh import SourceMaterial
from comic_editor.three_d.renderer.offscreen import OffscreenRenderer, RenderOptions
from comic_editor.three_d.renderer.primitives import cube_mesh
from comic_editor.three_d.renderer.scene import (
    LightType, SceneData, SceneLight, SceneNode,
)


def percentile(values: list[float], amount: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * amount
    low = int(position)
    high = min(len(ordered) - 1, low + 1)
    fraction = position - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def representative_scene() -> SceneData:
    nodes: dict[str, SceneNode] = {}
    roots: list[str] = []
    for row in range(5):
        for column in range(7):
            node_id = f"cube-{row}-{column}"
            matrix = np.identity(4)
            matrix[:3, 3] = [
                column * 1.25 - 3.75, 0.5, row * 1.25 - 2.5,
            ]
            nodes[node_id] = SceneNode(
                node_id, node_id, local_matrix=matrix, mesh_index=0
            )
            roots.append(node_id)
    lights = (
        SceneLight("sun", "Sun", LightType.SUN, energy=3.0),
        SceneLight(
            "fill", "Fill", LightType.POINT, energy=500.0, range=20.0
        ),
    )
    for index, (light, position) in enumerate(zip(
        lights, ((5.0, 8.0, 4.0), (-4.0, 5.0, -3.0))
    )):
        node_id = f"light-{index}"
        matrix = np.identity(4)
        matrix[:3, 3] = position
        nodes[node_id] = SceneNode(
            node_id, light.name, local_matrix=matrix, light_index=index
        )
        roots.append(node_id)
    scene = SceneData(
        scene_id="representative-3d-performance",
        nodes=nodes, root_node_ids=tuple(roots), meshes=(cube_mesh(),),
        source_materials=(SourceMaterial("default"),), lights=lights,
    )
    scene.active_camera.frame_bounds(*scene.bounds(), 16 / 9, 50.0, True)
    return scene


def run(samples: int, size: tuple[int, int]) -> dict[str, object]:
    renderer = OffscreenRenderer()
    scene = representative_scene()
    options = RenderOptions(
        draw_floor=True, draw_grid=True, draw_axes=True,
        selected_node_ids=frozenset({"cube-2-3"}),
    )
    totals: list[float] = []
    submits: list[float] = []
    readbacks: list[float] = []
    navigation: list[float] = []
    transforms: list[float] = []
    # Integrated GPUs can remain in a low-power clock state for several dozen
    # frames.  A short three-frame warm-up produced run-to-run swings larger
    # than the regression budget, so release measurements deliberately reach a
    # steady state before collecting any percentile samples.
    warmup_samples = 120
    context_info = dict(getattr(renderer.context, "info", {}))
    try:
        for _ in range(warmup_samples):
            renderer.render(scene, size, options)
        for _ in range(samples):
            renderer.render(scene, size, options)
            metrics = renderer.last_metrics
            totals.append(metrics.elapsed_ms)
            submits.append(metrics.draw_submit_ms)
            readbacks.append(metrics.readback_ms)
        for index in range(samples):
            scene.active_camera.orbit(0.35, -0.2)
            renderer.render(scene, size, options)
            navigation.append(renderer.last_metrics.elapsed_ms)
            node = scene.nodes["cube-2-3"]
            node.local_matrix[0, 3] += 0.002 if index % 2 else -0.002
            scene.recompute_world_matrices()
            renderer.render(scene, size, options)
            transforms.append(renderer.last_metrics.elapsed_ms)
    finally:
        renderer.release()
    return {
        "schema_version": 1,
        "scene": scene.scene_id,
        "target_size": list(size),
        "samples": samples,
        "warmup_samples": warmup_samples,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "gl_vendor": str(context_info.get("GL_VENDOR", "")),
        "gl_renderer": str(context_info.get("GL_RENDERER", "")),
        "gl_version": str(context_info.get("GL_VERSION", "")),
        "render_p50_ms": statistics.median(totals),
        "render_p95_ms": percentile(totals, 0.95),
        "draw_submit_p50_ms": statistics.median(submits),
        "draw_submit_p95_ms": percentile(submits, 0.95),
        "readback_p50_ms": statistics.median(readbacks),
        "readback_p95_ms": percentile(readbacks, 0.95),
        "navigation_p50_ms": statistics.median(navigation),
        "navigation_p95_ms": percentile(navigation, 0.95),
        "transform_p50_ms": statistics.median(transforms),
        "transform_p95_ms": percentile(transforms, 0.95),
    }


_TIMING_KEYS = (
    "render_p50_ms", "render_p95_ms", "draw_submit_p50_ms",
    "draw_submit_p95_ms", "readback_p50_ms", "readback_p95_ms",
    "navigation_p50_ms", "navigation_p95_ms",
    "transform_p50_ms", "transform_p95_ms",
)


def run_calibration(
    samples: int, size: tuple[int, int], rounds: int,
) -> dict[str, object]:
    """Return the observed upper envelope across steady-state runs.

    Laptop and integrated-GPU clocks can vary enough to swamp a 10% gate in a
    single process.  Comparing the worst P50/P95 from several independently
    warmed contexts keeps the threshold strict without mistaking power-state
    variance for a code regression.
    """
    results = [run(samples, size) for _ in range(max(1, rounds))]
    combined = dict(results[0])
    for key in _TIMING_KEYS:
        combined[key] = max(float(result[key]) for result in results)
    combined["calibration_rounds"] = len(results)
    return combined


def compare(result: dict[str, object], baseline: dict[str, object]) -> list[str]:
    failures: list[str] = []
    for key in ("scene", "target_size", "gl_vendor", "gl_renderer"):
        if result.get(key) != baseline.get(key):
            failures.append(
                f"{key}: current {result.get(key)!r} does not match "
                f"baseline {baseline.get(key)!r}"
            )
    for key in _TIMING_KEYS:
        current = float(result[key])
        expected = float(baseline[key])
        if current > expected * 1.10:
            failures.append(
                f"{key}: {current:.3f} ms exceeds the 10% budget "
                f"({expected:.3f} ms baseline)"
            )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=180)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--calibration-rounds", type=int, default=3)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--write-baseline", type=Path)
    arguments = parser.parse_args()
    result = run_calibration(
        max(5, arguments.samples),
        (arguments.width, arguments.height),
        max(1, arguments.calibration_rounds),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if arguments.write_baseline:
        arguments.write_baseline.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if arguments.baseline:
        baseline = json.loads(
            arguments.baseline.read_text(encoding="utf-8")
        )
        failures = compare(result, baseline)
        if failures:
            print("\n".join(failures), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
