"""Projection diagnostics observe the real retained path without pixel capture."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import DistortModifier, OutlineModifier, RasterObject
from comic_editor.ui import distort_pipeline, distort_rendering, modifier_rendering
from comic_editor.ui.document_presentation import PresentationStats
from test_performance_monitor_integration import editor, controller, _phases


@pytest.mark.parametrize("selected_promoted", [False, True])
def test_real_promoted_projection_records_capture_and_prediction_phases(editor, controller, selected_promoted):
    window, _, raster = editor
    canvas = window.canvas
    canvas._document_projection_enabled = True
    canvas.set_selection("object", raster.object_id)
    if selected_promoted:
        raster.show_on_top = True
    else:
        canvas.chapter.add_object(raster.parent_layer_id, RasterObject(show_on_top=True))
    expected_phases = {None} if selected_promoted else {"base", "top"}
    canvas.settings.predictive_ink = True
    canvas._predictive = (QPointF(30, 40), QPointF(60, 45), 4., QColor("red"))
    canvas._invalidate_scene_cache()
    names = ("_paint_document_projection", "_collect_document_projection",
             "_render_document_tiles", "_render_document_region",
             "_draw_predictive_ink", "_draw_live_vector_gesture", "_show_on_top_plan")
    originals = {name: getattr(canvas, name) for name in names}
    assert not controller._patches
    controller.start()
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("transparent"))
    painter = QPainter(image)
    try:
        canvas._paint_document_projection(painter, live_ink=True)
    finally:
        painter.end()
        canvas._predictive = None
    phases = _phases(controller)
    assert all(phases["canvas." + name.lstrip("_")]["count"] >= 1 for name in names)
    timeline = controller.snapshot()["timeline"]
    captures = [row["details"] for row in timeline if row["name"] == "canvas.render_document_region"]
    assert captures and all(row["exact"] for row in captures)
    assert {row["projection_phase"] for row in captures} == expected_phases
    assert all(row["output_size"][0] > 0 and row["pixel_scale"] > 0 for row in captures)
    assert any(row["details"]["live_ink"] for row in timeline
               if row["name"] == "canvas.paint_document_projection")
    predictions = [row["details"] for row in timeline if row["name"] == "canvas.draw_predictive_ink"]
    assert {row["projection_phase"] for row in predictions} == expected_phases
    controller.stop()
    assert all(getattr(canvas, name) == original for name, original in originals.items())
    assert not controller._patches


def test_projection_metrics_are_bounded_detached_and_read_only_on_gui(editor, controller, monkeypatch):
    window, _, _ = editor
    canvas = window.canvas
    canvas._document_projection_enabled = True
    canvas._collect_document_projection()
    canvas._document_presentation_stats = PresentationStats("cpu", 7, 2, 4096)
    canvas._projection_frame_pending = True
    canvas._projection_render_error = "private failure details"
    canvas._projection_presented_revision = 17
    projection = canvas._document_projection
    counters = projection.snapshot()
    gui_thread = threading.get_ident()
    reads = []

    class ArtworkMustNeverBeRead:
        def __repr__(self):
            raise AssertionError("Artwork was inspected")

    supplied = {**counters, "images": ArtworkMustNeverBeRead(),
                "configuration": ["private-document-content"] * 1000}
    preparation = distort_rendering.PreparedDistortCache(budget=1024, entry_limit=3)
    preparation._entries["private-document-content"] = ArtworkMustNeverBeRead()
    preparation.bytes, preparation.hits, preparation.misses = 128, 4, 2
    canvas._distort_preparation_cache = preparation

    def snapshot_on_gui():
        assert threading.get_ident() == gui_thread
        reads.append(threading.get_ident())
        return supplied

    monkeypatch.setattr(projection, "snapshot", snapshot_on_gui)
    controller.start()
    cached = controller.snapshot()["editor_metrics"]
    assert cached["projection"] == {
        **counters, "enabled": True, "budget_bytes": projection.budget,
        "tile_size": projection.tile_size, "revision": projection.revision,
        "frame_pending": True, "render_failed": True, "presented_revision": 17,
    }
    assert cached["presentation"] == dict(backend="cpu", tiles=7, uploads=2, texture_bytes=4096)
    assert cached["distort_preparation"] == dict(bytes=128, budget=1024, hits=4, misses=2,
                                                evictions=0, entry_limit=3, entries=1)
    preparation.hits = 100
    supplied["hits"] = 123456
    canvas._document_presentation_stats = PresentationStats("gpu", 999, 0, 8192)
    assert cached["projection"]["hits"] == counters["hits"]
    assert cached["presentation"]["tiles"] == 7
    assert cached["distort_preparation"]["hits"] == 4
    read_count = len(reads)
    assert controller._writer.flush()
    assert len(reads) == read_count
    with ThreadPoolExecutor(max_workers=1) as executor:
        report = executor.submit(controller.snapshot).result()
    encoded = json.dumps(report)
    assert "private-document-content" not in encoded and "images" not in report["editor_metrics"]["projection"]
    assert "private failure details" not in encoded
    assert len(reads) == read_count
    controller.stop()
    read_count = len(reads)
    controller.snapshot()
    controller._tick()
    assert len(reads) == read_count


def test_distort_pipeline_and_outline_kernels_are_observed_and_restored(editor, controller):
    window, _, _ = editor
    canvas = window.canvas
    image = QImage(48, 40, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#aa4488cc"))
    bounds = QRectF(0, 0, image.width(), image.height())
    modifier = DistortModifier(modifier_type="distort_twirl", frame=(0, 0, 48, 40),
                              center=(24, 20), radius=20, parameters={"angle": 25})
    original_distort = distort_rendering.render_distort
    outline_names = ("_outline_qimage", "_outline_stack_qimage", "_outline_effect")
    original_outlines = {name: getattr(modifier_rendering, name) for name in outline_names}
    controller.start()
    # The actual stage performs a local import of the source module's function.
    actual, provisional = distort_pipeline.render_distort_stage(
        canvas, image, image, bounds, bounds, modifier, QTransform(), {},
        ("diagnostic",), None, False, False)
    assert not actual.isNull() and not provisional
    modifier_rendering.apply_modifier_stack(image, [OutlineModifier(thickness=3)], (0, 0))
    modifier_rendering.apply_modifier_stack(image, [OutlineModifier(thickness=3), OutlineModifier(thickness=2)], (0, 0))
    # A worker that retained an enabled probe also reports its real thread.
    retained_probe = distort_rendering.render_distort
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert not executor.submit(retained_probe, image, bounds, modifier, QTransform(), bounds).result().isNull()
    phases = _phases(controller)
    assert phases["effects.render_distort"]["count"] == 2
    assert phases["effects.outline_qimage"]["count"] == 1
    assert phases["effects.outline_stack_qimage"]["count"] == 1
    calls = [row["details"] for row in controller.snapshot()["timeline"] if row["name"] == "effects.render_distort"]
    assert {row["thread"] for row in calls} == {"gui", "worker"}
    assert all(row["modifier"] == {"id": modifier.modifier_id, "type": "distort_twirl"} for row in calls)
    controller.stop()
    assert distort_rendering.render_distort is original_distort
    assert all(getattr(modifier_rendering, name) is original for name, original in original_outlines.items())
    stopped = controller.snapshot()["timeline"]
    retained_probe(image, bounds, modifier, QTransform(), bounds)
    assert controller.snapshot()["timeline"] == stopped
