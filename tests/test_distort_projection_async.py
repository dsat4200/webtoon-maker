"""Exact projection work hands off immutable pixels without GUI-side drafts."""
from threading import Event, get_ident
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.ui import distort_pipeline, distort_rendering
from comic_editor.ui.async_projection import (
    ProjectionFailed, ProjectionPending, projection_deferred,
    projection_result_or_pending,
)
from test_distort_rendering import BOUNDS, modifier, source_image
from test_effect_job_retention import Owner


@pytest.fixture
def canvas(qapp):
    result = Owner()
    result._projection_exact = result._projection_defer_effects = True
    yield result
    result._effect_jobs.cancel()
    result._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    result.deleteLater()


def inputs():
    return (source_image().scaled(192, 144), source_image().scaled(48, 36),
            QRectF(BOUNDS), QRectF(4, 5, 48, 36),
            modifier("twirl", angle=85), QTransform(), {})


def stage(canvas, values, key="revision"):
    return distort_pipeline.render_distort_stage(
        canvas, *values, key, "object", False, False)


def test_pending_distort_is_detached_exact_and_never_draws_a_draft(canvas, monkeypatch):
    values = inputs()
    field = np.linspace(15, 90, 48 * 36, dtype=np.float32).reshape(36, 48)
    monkeypatch.setattr(distort_pipeline, "_parameter_field", lambda *_: field)
    canvas._projection_defer_effects = False
    expected, provisional = stage(canvas, values)
    assert not provisional
    canvas._projection_defer_effects = True
    started, release = Event(), Event()
    calls, gui_thread = [], get_ident()
    original = distort_rendering.render_distort

    def compute(*args, **kwargs):
        calls.append((get_ident(), kwargs.get("preparation_cache"), kwargs.get("pixel_scale")))
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(distort_rendering, "render_distort", compute)
    try:
        with pytest.raises(ProjectionPending):
            stage(canvas, values)
        assert started.wait(2)
        assert len(calls) == 1 and calls[0][0] != gui_thread and calls[0][2] == 1.0
        assert isinstance(calls[0][1], distort_rendering.PreparedDistortCache)
        assert calls[0][1] is not canvas._distort_preparation_cache
        # All caller-owned inputs can change while the worker is blocked.
        values[0].fill(QColor("red"))
        values[1].fill(QColor("blue"))
        values[2].translate(20, 30)
        values[3].translate(10, 20)
        values[4].parameters["angle"] = 0
        values[5].translate(8, 9)
        field[:] = 0
        # Retrying the same key must not copy snapshots or evaluate fields.
        monkeypatch.setattr(distort_pipeline, "_parameter_field",
                            lambda *_: pytest.fail("recopied pending inputs"))
        with pytest.raises(ProjectionPending):
            stage(canvas, values)
        assert canvas._effect_jobs.submitted == 1
    finally:
        release.set()
    canvas._effect_jobs.running[3].result(timeout=5)
    # The result is adopted even before the GUI timer gets its next tick.
    actual, provisional = stage(canvas, values)
    assert actual == expected and not provisional
    assert canvas._effect_jobs.completed == 1 and len(calls) == 1
    assert "distort-draft" not in repr(canvas.results)


def test_changed_scope_key_discards_cancelled_snapshot(canvas, monkeypatch):
    values, release, started = inputs(), Event(), Event()
    original = distort_rendering.render_distort
    calls = []

    def compute(*args, **kwargs):
        calls.append(get_ident())
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        # Returning an image despite cancellation still must not publish it.
        return original(*args[:5], None, **kwargs)

    monkeypatch.setattr(distort_rendering, "render_distort", compute)
    try:
        with pytest.raises(ProjectionPending):
            stage(canvas, values, "old")
        assert started.wait(2)
        with pytest.raises(ProjectionPending):
            stage(canvas, values, "new")
        old = canvas._effect_jobs.running
        assert old[2].is_set()
    finally:
        release.set()
    old[3].result(timeout=5)
    canvas._effect_jobs.poll()
    canvas._effect_jobs.running[3].result(timeout=5)
    result, provisional = stage(canvas, values, "new")
    assert not result.isNull() and not provisional
    assert canvas._effect_jobs.discarded == 1
    assert canvas._effect_jobs.result("object", "old") is None


def test_exact_worker_failure_is_terminal_until_key_changes(canvas, monkeypatch):
    values = inputs()

    def fail(*_args, **_kwargs):
        raise ValueError("synthetic exact render failure")

    monkeypatch.setattr(distort_rendering, "render_distort", fail)
    with pytest.raises(ProjectionPending):
        stage(canvas, values)
    with pytest.raises(ValueError):
        canvas._effect_jobs.running[3].result(timeout=5)
    for _ in range(2):
        with pytest.raises(ProjectionFailed, match="synthetic exact render failure"):
            stage(canvas, values)
    assert canvas._effect_jobs.submitted == 1


@pytest.mark.parametrize("field,value", [
    ("_projection_exact", False), ("_projection_defer_effects", False),
    ("_interactive_render", False), ("_render_base_alpha", True),
    ("_rendering_mask_contributor", 1), ("_rendering_halftone_source", True),
    ("_render_modifier_sources", {("layer", "source")}),
    ("_render_cage_source", True), ("_tiling_capture_geometry", object()),
    ("_rendering_compound_references", True), ("_rendering_outward_gradient", True),
    ("_effect_preview_channel", "navigator"), ("_effect_preview_channel", "export"),
])
def test_recursive_or_noninteractive_contexts_never_defer(canvas, field, value):
    assert projection_deferred(canvas)
    setattr(canvas, field, value)
    assert not projection_deferred(canvas)


def test_export_and_recursive_source_capture_remain_synchronous(canvas, monkeypatch):
    values = inputs()
    calls = []

    def compute(*_args, **kwargs):
        calls.append((get_ident(), kwargs["pixel_scale"]))
        return QImage(values[1])

    monkeypatch.setattr(distort_rendering, "render_distort", compute)
    canvas._interactive_render = False
    assert not stage(canvas, values)[1]
    canvas._interactive_render = True
    canvas._render_modifier_sources = {("layer", "source")}
    assert not stage(canvas, values)[1]
    assert calls == [(get_ident(), 1.0), (get_ident(), 1.0)]
    assert canvas._effect_jobs.submitted == 0


def test_memory_waiting_exact_request_does_not_recopy_snapshot():
    jobs = SimpleNamespace(result=lambda *_: None, has_running=lambda *_: False, pending={},
                           waiting={"object": "revision"})
    canvas = SimpleNamespace(_effect_jobs=jobs)
    with pytest.raises(ProjectionPending):
        projection_result_or_pending(canvas, "object", "revision")
    assert projection_result_or_pending(canvas, "object", "changed") is None


def test_worker_source_and_mesh_setup_reused_without_stale_pixels_or_masks(canvas, monkeypatch):
    values = list(inputs())
    values[4] = modifier("mesh_warp")
    values[4].points[5] = (.58, .39)
    setup, caches = [], []
    original_rgba, original_mesh = distort_rendering._rgba, distort_rendering._mesh
    original_render = distort_rendering.render_distort
    monkeypatch.setattr(distort_rendering, "_rgba",
                        lambda image: (setup.append("pixels"), original_rgba(image))[1])
    monkeypatch.setattr(distort_rendering, "_mesh",
                        lambda *args: (setup.append("mesh"), original_mesh(*args))[1])
    def observe(*args, **kwargs):
        caches.append(kwargs.get("preparation_cache"))
        return original_render(*args, **kwargs)
    monkeypatch.setattr(distort_rendering, "render_distort", observe)

    def finished(key):
        with pytest.raises(ProjectionPending):
            stage(canvas, values, key)
        canvas._effect_jobs.running[3].result(timeout=5)
        return stage(canvas, values, key)[0]

    first = finished("first")
    values[3] = QRectF(9, 5, 48, 36)
    shifted = finished("next-region")
    assert shifted != first
    assert setup == ["pixels", "mesh"]
    assert caches[0] is caches[1] and caches[0].budget == 256 * 1024 * 1024
    assert not hasattr(canvas, "_distort_preparation_cache")
    values[0].fill(QColor("#204080"))
    changed = finished("new-source")
    assert changed != shifted and setup == ["pixels", "mesh", "pixels"]
    values[4].intensity = 0
    assert finished("new-mask") == values[1]
    assert setup == ["pixels", "mesh", "pixels"]
    assert caches[0].bytes <= caches[0].budget


def test_worker_preparation_lifetime_ends_with_executor():
    from concurrent.futures import ThreadPoolExecutor
    import weakref
    with ThreadPoolExecutor(max_workers=1) as executor:
        reference = executor.submit(lambda: weakref.ref(
            distort_pipeline._worker_preparation_cache())).result(timeout=5)
        assert reference() is not None
    assert reference() is None
