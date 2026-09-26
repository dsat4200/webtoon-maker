"""Manual finished-artwork comparison at the three recorded problem cameras.

Runs frozen and current source in separate processes, on a fresh chapter-only
copy of the already isolated project. Uses disconnected, never-shown canvases
and software presentation so GPU-driver rounding cannot conceal CPU changes.
No MainWindow, Blender controller, broker, network, autosave, or project save is
constructed. Run only outside concurrent performance measurement windows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".artifacts" / "drawing-stutters-20260926"
CHAPTER = "0a72f08009294aa0a3d14e6a38e22bbb"
RASTER = "9db9d8094e744ee4aaffb84e9db10f7c"
CAMERAS = {
    "rotated": (723.417434792031, 19631.426410948996, 2.864183112473176, -45.42587201378674),
    "lowzoom": (614.9302438479234, 19880.8243941934, .6943877037630306, 0.),
    "extreme": (-4571.983550547249, 18899.65961248689, .053351510286541996, -44.16637539018104),
}


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def manifest(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(folder.rglob("*")) if path.is_file()}


def compare(a, b):
    import numpy as np
    delta = np.abs(a.astype(np.int16) - b.astype(np.int16))
    changed = np.any(delta != 0, axis=2)
    yy, xx = np.nonzero(changed)
    return {"identical": not bool(changed.any()), "changed_pixels": int(changed.sum()),
            "changed_bytes": int(np.count_nonzero(delta)),
            "maximum_byte_error": int(delta.max(initial=0)),
            "mean_byte_error": float(delta.mean()),
            "difference_bounds": ([int(xx.min()), int(yy.min()), int(xx.max()), int(yy.max())]
                                  if xx.size else None)}


def child(args, out):
    source = ARTIFACTS / "baseline-source" if args.worker == "baseline" else ROOT
    sys.path.insert(0, str(source))
    os.environ["QT_QPA_PLATFORM"] = "windows"
    os.environ["QT_TLS_BACKEND"] = "schannel"
    import numpy as np
    from PySide6.QtCore import QCoreApplication, QEvent, QRectF
    from PySide6.QtGui import QFontDatabase, QImage
    from PySide6.QtWidgets import QApplication
    from comic_editor.core import settings as settings_module
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.ui import canvas as canvas_module, gpu_pattern_effects
    from comic_editor.ui.canvas import CanvasWidget, ToolKind

    assert Path(canvas_module.__file__).resolve().is_relative_to(source.resolve())
    settings_module.settings_path = lambda: out / "settings.json"
    # The renderer has no need for networking or an OpenGL effect context here.
    canvas_module.create_network_manager = lambda *_args, **_kwargs: None
    gpu_pattern_effects.renderer_for = lambda *_args, **_kwargs: None
    app = QApplication([])
    app.setApplicationName("Isolated artwork comparison")
    for font in ("segoeui.ttf", "arial.ttf", "times.ttf", "comic.ttf"):
        path = Path("C:/Windows/Fonts") / font
        if path.is_file():
            QFontDatabase.addApplicationFont(str(path))
    settings = settings_module.load_settings()
    settings.canvas_renderer = "raster"
    chapter, tiles, images = SeriesRepository(out / "project-copy").load_chapter(
        CHAPTER, include_images=True)
    assert RASTER in chapter.objects
    canvas = CanvasWidget(settings)
    canvas.setFixedSize(955, 927)
    canvas.set_document(chapter, tiles, images)
    canvas.set_selection("object", RASTER)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._document_projection_enabled = True
    results = []

    def render():
        canvas._visual_frame_timer.stop()
        canvas._flush_visual_dirty()
        canvas._ensure_scene_cache()
        assert not canvas._effect_jobs.running and not canvas._effect_jobs.pending
        # Offscreen tiles at a previous resolution may correctly remain dirty.
        # Only a requested incomplete capture would make this frame unfinished.
        assert canvas._document_projection.incomplete == 0
        image = canvas._scene_cache.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
        pixels = np.frombuffer(image.constBits(), np.uint8).reshape(
            image.height(), image.bytesPerLine())[:, :image.width() * 4]
        return image, pixels.reshape(image.height(), image.width(), 4).copy()

    try:
        for name in args.cameras.split(","):
            camera = CAMERAS[name]
            canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation = camera
            canvas._invalidate_scene_cache(projection=False)
            started = time.perf_counter()
            image, first = render()
            elapsed = time.perf_counter() - started
            assert image.save(str(out / f"{args.worker}-{name}.png"))
            np.save(out / f"{args.worker}-{name}.npy", first)
            _, settled = render()
            row = {"camera": name, "values": camera, "seconds": elapsed,
                   "first_vs_settled": compare(first, settled),
                   "projection": canvas._document_projection.snapshot()}
            # Exercise the changed subset/source-region keys after a finished
            # frame, without modifying any saved artwork in memory or on disk.
            if args.worker == "current":
                focus = canvas.visible_document_rect().intersected(
                    QRectF(0, 0, chapter.width, chapter.height)).center()
                canvas._mark_scene_dirty_world(QRectF(focus.x()-5, focus.y()-5, 10, 10))
                # Recompose the whole widget from only the dirty document
                # tiles, matching native presentation. A clipped raster-only
                # screen repaint has separate Qt subpixel sampling behavior.
                canvas._invalidate_scene_cache(projection=False)
                _, partial = render()
                row["first_vs_partial"] = compare(first, partial)
            results.append(row)
            write_json(out / f"{args.worker}-results.json", results)
            print(json.dumps({"worker": args.worker, **row}), flush=True)
    finally:
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--cameras", default="rotated,lowzoom,extreme")
    parser.add_argument("--worker", choices=("baseline", "current"))
    parser.add_argument("--reuse-baseline", action="store_true",
                        help="Repeat current captures against this label's verified unchanged copy/baseline")
    args = parser.parse_args()
    assert all(name in CAMERAS for name in args.cameras.split(","))
    out = (ARTIFACTS / args.label).resolve()
    assert out.is_relative_to(ARTIFACTS.resolve()) and out != ARTIFACTS.resolve()
    if args.worker:
        assert (out / "project-copy" / "series.json").is_file()
        return child(args, out)
    if args.reuse_baseline:
        assert manifest(out / "project-copy") == json.loads((out / "copy-before.json").read_text())
        assert all((out / f"baseline-{name}.npy").is_file() for name in args.cameras.split(","))
    else:
        assert not out.exists(), "Use a new label to preserve previous quality checks"
    frozen = ARTIFACTS / "baseline-source" / "comic_editor" / "ui" / "canvas.py"
    assert frozen.is_file(), "The pre-fix source snapshot is required"
    source = ARTIFACTS / "project-copy"
    assert (source / "series.json").is_file()
    out.mkdir(parents=True, exist_ok=args.reuse_baseline)
    copy = out / "project-copy"
    if not args.reuse_baseline:
        copy.mkdir()
        shutil.copy2(source / "series.json", copy / "series.json")
        # Only this chapter is required; avoid scanning unrelated chapters/assets
        # and exports before every render. Its complete directory preserves load
        # recovery semantics, and both code versions read the same fresh copy.
        shutil.copytree(source / "chapters" / CHAPTER, copy / "chapters" / CHAPTER)
        shutil.copy2(ARTIFACTS / "user-settings-snapshot.json", out / "settings.json")
    before = manifest(copy)
    write_json(out / "copy-before.json", before)
    for worker in (("current",) if args.reuse_baseline else ("baseline", "current")):
        command = [sys.executable, str(Path(__file__).resolve()), "--label", args.label,
                   "--cameras", args.cameras, "--worker", worker]
        with (out / f"{worker}.log").open("w", encoding="utf-8") as log:
            subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=300)
    import numpy as np
    comparisons = {name: compare(np.load(out / f"baseline-{name}.npy"),
                                 np.load(out / f"current-{name}.npy"))
                   for name in args.cameras.split(",")}
    after = manifest(copy)
    write_json(out / "copy-after.json", after)
    stability = [row for worker in ("baseline", "current")
                 for row in json.loads((out / f"{worker}-results.json").read_text())]
    stable = all(row["first_vs_settled"]["identical"]
                 and row.get("first_vs_partial", {"identical": True})["identical"]
                 for row in stability)
    result = {"baseline_vs_current": comparisons, "all_frames_stable": stable,
              "isolated_copy_unchanged": before == after,
              "rendering": "Finished projection, grid and border with software presentation; no selection handles"}
    write_json(out / "comparison.json", result)
    print(json.dumps(result, indent=2))
    return 0 if before == after and stable and all(
        row["identical"] for row in comparisons.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
