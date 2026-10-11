"""Compare ordinary and batch keys against an untouched saved project copy."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--project-copy", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--repeats", type=int, default=3)
parser.add_argument("--row-stride", type=int, default=8)
args = parser.parse_args()
project, output = args.project_copy.resolve(), args.output.resolve()
assert project.is_relative_to(ROOT / ".artifacts") and (project / "series.json").is_file()
assert output.is_relative_to(ROOT / ".artifacts") and not output.exists()
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QRectF
from PySide6.QtWidgets import QApplication
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.render.cache import PersistentRenderCache, digest
from comic_editor.render.scene import DetachedSceneBackend, SceneSnapshotCompiler
from comic_editor.ui.cache_dependencies import RenderDependencies
from comic_editor.ui.canvas import CanvasWidget


def manifest():
    return {str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in project.rglob("*") if path.is_file()}


app = QApplication.instance() or QApplication([])
before = manifest()
repository = SeriesRepository(project)
chapter, tiles, images = repository.load_chapter("0a72f08009294aa0a3d14e6a38e22bbb", include_images=True)
canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
canvas.set_document(chapter, tiles, images)
capture = SceneSnapshotCompiler().capture(canvas, canvas._render_document_state())
while not capture.advance(.004):
    app.processEvents()
assert not capture.stale and capture.result is not None
snapshot = replace(capture.result, cache_spec=None)
requests = tuple(request for request in canvas._document_projection.requests(
    QRectF(0, 0, chapter.width, chapter.height), 1.)
    if request.address.y % max(1, args.row_stride) == 0)
configuration = (*snapshot.document.configuration, None)


def inspect():
    backend = DetachedSceneBackend(snapshot)
    backing = PersistentRenderCache(output.parent / "dependency-probe-cache",
        contract=chapter.pixel_contract.signature)
    scene = backend.scene
    dependencies = RenderDependencies(scene, backing)
    scene._render_dependencies = dependencies
    scene.tiles.render_fingerprint, scene.images.render_fingerprint = dependencies.tiles, dependencies.image
    try:
        # Prepare ordinary hashes and resolved font tables before comparing the
        # repeated dependency-inspection cost; no drawing output is rendered.
        dependencies.projection_keys(requests, configuration)
        rows = []
        reference = None
        for repeat in range(args.repeats):
            started = time.perf_counter()
            single = tuple(dependencies.projection_key(request, configuration) for request in requests)
            single_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            batch = dependencies.projection_keys(requests, configuration)
            batch_ms = (time.perf_counter() - started) * 1000
            assert batch == single
            if reference is not None:
                assert reference == batch
            reference = batch
            rows.append(dict(repeat=repeat, single_ms=single_ms, batch_ms=batch_ms))
        return dict(rows=rows, semantic_keys_equal=True, semantic_key_digest=digest(reference),
            single_median_ms=statistics.median(row["single_ms"] for row in rows),
            batch_median_ms=statistics.median(row["batch_ms"] for row in rows),
            native_request_count=len(requests), chapter_size=[chapter.width, chapter.height])
    finally:
        backing.close()
        backend.close()


try:
    output.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=1) as worker:
        result = worker.submit(inspect).result(timeout=180)
    assert manifest() == before
    result.update(project_files=len(before), input_files_unchanged=True,
        row_stride=max(1, args.row_stride),
        platform=os.environ["QT_QPA_PLATFORM"], scope="dependency key inspection only, not rendering or presentation")
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
finally:
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
