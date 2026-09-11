"""CPU fallback snapshots, provisional stages, and bounded effect scheduling."""
import copy
from threading import Event, get_ident

import numpy as np
import pytest
from PySide6.QtCore import QObject, QPointF, QRectF, Signal
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, CageTransformModifier, ChapterDocument, HalftoneModifier,
    HueSaturationLightnessModifier, OutlineModifier, ParameterMaskBinding,
    PixelateModifier, RadialBlurModifier, RasterObject, ShapeStyle, WobbleModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_jobs import EffectJobs
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.modifier_rendering import apply_pattern_modifier
from comic_editor.ui.pattern_rendering import apply_pattern_effect
from comic_editor.ui.radial_blur import RadialRenderCancelled
from comic_editor.ui.stroke_rendering import render_stroke_stack


@pytest.fixture
def scene(qapp):
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    chapter = ChapterDocument(width=320, height=240, document_kind="asset")
    chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 320, 240))
    canvas.set_document(chapter, TileStore())
    yield canvas
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def image():
    source = QImage(320, 240, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor(40, 100, 180, 210))
    return source


def test_pattern_worker_snapshots_pixels_parameters_masks_and_crop(scene, monkeypatch):
    source = image()
    modifier = HalftoneModifier(base_resolution=320, spacing=12, blur=0, contrast=.3)
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("mask", 0, 100)
    field = np.ones((240, 320), dtype=np.float32)
    field[:, :120] = .25
    masks = {(modifier.modifier_id, "intensity"): field}
    monkeypatch.setattr(scene, "_modifier_mask_fields", lambda *args: masks)
    queued = []
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *args, **kwargs: queued.append(args) or True)
    original, saved_modifier, saved_masks = QImage(source), copy.deepcopy(modifier), copy.deepcopy(masks)
    scene._interactive_render = True
    draft, bounds = render_stages(scene, source, QRectF(0, 0, 320, 240), [modifier],
        QTransform(), required=QRectF(20, 30, 150, 120), request_scope=("test",))
    assert draft.size().toTuple() == (150, 120)
    assert bounds == QRectF(20, 30, 150, 120)
    assert len(queued) == 1
    assert scene._effect_provisional_revision > 0
    assert scene._modifier_cache_get(queued[0][1]) is None
    source.fill(QColor("red"))
    modifier.contrast = 2
    field.fill(0)
    completed = queued[0][2](lambda: False)
    expected = apply_pattern_modifier(original, saved_modifier, saved_masks).copy(20, 30, 150, 120)
    assert completed == expected


def test_provisional_pattern_parent_only_builds_bounded_drafts(scene, monkeypatch):
    queued = []
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *args, **kwargs: queued.append(args) or True)
    scene._interactive_render = True
    result, _ = render_stages(scene, image(), QRectF(0, 0, 320, 240),
        [HalftoneModifier(blur=0), PixelateModifier(pixel_size=12)], QTransform(),
        request_scope=("test",), provisional=True)
    assert not queued
    assert not result.isNull()
    assert scene._effect_provisional_revision > 0
    assert not any(key[0] == "stage" for key in scene._modifier_render_cache)


def test_gpu_failure_falls_back_in_worker_but_export_stays_exact(scene, monkeypatch):
    class UnavailableRenderer:
        def render(self, *args, **kwargs):
            return None
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _: UnavailableRenderer())
    modifier = PixelateModifier(pixel_size=13, brightness=40)
    queued = []
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *args, **kwargs: queued.append(args) or True)
    source = image()
    scene._interactive_render = True
    render_stages(scene, source, QRectF(0, 0, 320, 240), [modifier], QTransform(), request_scope=("test",))
    assert len(queued) == 1
    scene._interactive_render = False
    actual, _ = render_stages(scene, source, QRectF(0, 0, 320, 240), [modifier], QTransform())
    assert actual == apply_pattern_modifier(source, modifier)
    assert len(queued) == 1


def test_async_outline_stage_crops_only_after_exact_work_is_cached(scene, monkeypatch):
    source = QImage(2050, 1100, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor(40, 100, 180, 210))
    modifier = OutlineModifier(thickness=5)
    queued = []
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *args, **kwargs: queued.append(args) or True)
    scene._interactive_render = True
    frame, required = QRectF(0, 0, 2050, 1100), QRectF(20, 30, 300, 200)
    render_stages(scene, source, frame, [modifier], QTransform(), required=required, request_scope=("test",))
    assert len(queued) == 1
    assert queued[0][1][0] == "stage-work"
    exact_work = queued[0][2](lambda: False)
    assert exact_work.size().toTuple() == (2100, 1150)
    scene._modifier_cache_put(queued[0][1], exact_work)
    revision = scene._effect_provisional_revision
    exact, bounds = render_stages(scene, source, frame, [modifier], QTransform(),
        required=required, request_scope=("test",))
    assert len(queued) == 1
    assert scene._effect_provisional_revision == revision
    assert exact == exact_work.copy(45, 55, 300, 200)
    assert bounds == required


def test_outline_draft_source_never_enters_exact_source_cache(scene):
    scene._interactive_render = True
    render_stages(scene, image(), QRectF(0, 0, 320, 240), [OutlineModifier()], QTransform(),
        request_scope=("test",), provisional=True)
    assert not any(key[0] == "outline-stage-source" for key in scene._modifier_source_cache)


def test_stroke_then_async_color_blur_converges_without_rebuilding_sources(scene, monkeypatch):
    chapter = scene.chapter
    target = chapter.add_layer(chapter.root_page_ids[0], "Shape", BoundGeometry.circle(160, 120, 90),
        style=ShapeStyle(primary_color="#FF806040", outline_color="#FF205090", outline_thickness=6))
    modifiers = [WobbleModifier(position=3), HueSaturationLightnessModifier(hue=30), BlurModifier(strength=3)]
    for modifier in modifiers:
        chapter.add_modifier(modifier, [("layer", target.layer_id)])
    source, bounds = image(), QRectF(0, 0, 320, 240)
    scope, source_key = ("stroke-test",), ("fixed-stroke-source",)
    expected, expected_bounds = render_stroke_stack(scene, target, source, bounds, modifiers,
        QTransform(), source_key, None)
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    queued, keys = [], []
    def request(*args, **kwargs):
        queued.append(args)
        keys.append(args[1])
        return True
    monkeypatch.setattr(scene._effect_jobs, "request", request)
    scene._interactive_render = True
    for _ in range(6):
        revision = getattr(scene, "_effect_provisional_revision", 0)
        actual, actual_bounds = render_stroke_stack(scene, target, source, bounds, modifiers,
            QTransform(), source_key, scope)
        if revision == getattr(scene, "_effect_provisional_revision", 0):
            break
        assert queued
        for job in queued:
            scene._modifier_cache_put(job[1], job[2](lambda: False))
        queued.clear()
    else:
        pytest.fail("Stable stroke output did not converge to the exact downstream effect cache")
    assert actual_bounds == expected_bounds
    assert actual == expected
    assert len(keys) == 4
    assert len(set(keys)) == 4


@pytest.mark.parametrize("child_kind", ["layer", "object"])
def test_selected_mask_only_children_stay_hidden_in_interactive_target_colors(scene, child_kind):
    from comic_editor.ui.halftone_source import render_color_source
    chapter = scene.chapter
    parent = chapter.add_layer(chapter.root_page_ids[0], "Colors", BoundGeometry.rectangle(0, 0, 320, 240))
    parent.fill_color, parent.border_width = "#2468ac", 0
    if child_kind == "layer":
        child = chapter.add_layer(parent.layer_id, "Mask", BoundGeometry.rectangle(80, 60, 160, 120))
        child.fill_color, child.border_width = "#ff0000", 0
        identifier = child.layer_id
    else:
        child = chapter.add_object(parent.layer_id, RasterObject())
        scene.tiles.paint_dab(child.object_id, QPointF(160, 120), 70, QColor("red"))
        identifier = child.object_id
    child.mask_only = True
    scene.set_selection(child_kind, identifier)
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=parent.layer_id)
    expected = render_color_source(scene, modifier, image(), QRectF(0, 0, 320, 240), QTransform())
    scene._modifier_source_cache.clear()
    scene._modifier_source_cache_bytes = 0
    scene._interactive_render = True
    actual = render_color_source(scene, modifier, image(), QRectF(0, 0, 320, 240), QTransform())
    assert actual == expected
    assert actual.pixelColor(160, 120) == QColor("#2468ac")
    assert scene.selected_kind == child_kind and scene.selected_id == identifier


def test_halftone_observes_cancellation_between_cell_neighborhoods():
    calls = []
    def cancelled():
        calls.append(True)
        return len(calls) == 3
    with pytest.raises(RadialRenderCancelled):
        apply_pattern_effect(image(), HalftoneModifier(blur=0), cancelled=cancelled)
    assert len(calls) == 3


def test_tall_pattern_source_never_runs_full_fallback_on_ui_thread(scene, monkeypatch):
    import comic_editor.ui.pattern_rendering as rendering
    source = QImage(1080, 3000, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor("#806040"))
    ui_thread, full_calls = get_ident(), []
    original = rendering.apply_pattern_effect
    def record(image, modifier, *args, **kwargs):
        if image.size() == source.size():
            full_calls.append(get_ident())
            return QImage(image)
        return original(image, modifier, *args, **kwargs)
    monkeypatch.setattr(rendering, "apply_pattern_effect", record)
    scene._interactive_render = True
    modifier = HalftoneModifier()
    bounds = QRectF(0, 0, 1080, 3000)
    render_stages(scene, source, bounds, [modifier], QTransform(), request_scope=("tall",))
    assert scene._effect_jobs.running is not None
    assert scene._effect_jobs.bytes_in_flight > 512 * 1024 * 1024
    scene._effect_jobs.running[3].result(timeout=3)
    scene._effect_jobs.poll()
    actual, _ = render_stages(scene, source, bounds, [modifier], QTransform(), request_scope=("tall",))
    assert actual == source
    assert len(full_calls) == 1 and full_calls[0] != ui_thread


class JobOwner(QObject):
    visualChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.results = {}
        self.repaints = 0

    def _modifier_cache_put(self, key, value):
        self.results[key] = value

    def _invalidate_scene_cache(self):
        pass

    def update(self):
        self.repaints += 1


def test_capacity_pressure_defers_without_running_expensive_work_inline(qapp):
    owner = JobOwner()
    jobs = EffectJobs(owner, budget=100)
    gate = Event()
    try:
        jobs.request(("scope",), ("old",), lambda cancel: (gate.wait(2), image())[1], 80)
        future = jobs.running[3]
        assert jobs.request(("scope",), ("new",), lambda cancel: image(), 80)
        assert jobs.bytes_in_flight == 80
        assert not jobs.pending
        assert jobs.retry_on_release
        gate.set()
        future.result(timeout=2)
        jobs.poll()
        assert owner.repaints == 1
        assert not owner.results
        assert jobs.request(("scope",), ("new",), lambda cancel: image(), 80)
        jobs.running[3].result(timeout=2)
        jobs.poll()
        assert ("new",) in owner.results
    finally:
        gate.set()
        jobs.cancel()
        owner.deleteLater()


def test_oversized_jobs_require_opt_in_and_run_exclusively(qapp):
    owner = JobOwner()
    jobs = EffectJobs(owner, budget=100)
    gate = Event()
    try:
        large_size = 513 * 1024 * 1024
        assert not jobs.request(("a",), ("too-large",), lambda cancel: image(), large_size)
        assert jobs.request(("a",), ("large",), lambda cancel: (gate.wait(2), image())[1],
                            large_size, allow_oversized=True)
        assert jobs.bytes_in_flight == large_size
        assert jobs.request(("b",), ("small",), lambda cancel: image(), 50)
        assert not jobs.pending
        assert jobs.bytes_in_flight == large_size
        gate.set()
        jobs.running[3].result(timeout=2)
        jobs.poll()
        assert ("large",) in owner.results
        assert jobs.bytes_in_flight == 0
    finally:
        gate.set()
        jobs.cancel()
        owner.deleteLater()


def _tiny_scene_caches(scene):
    for name in ("render", "source"):
        getattr(scene, f"_modifier_{name}_cache").clear()
        setattr(scene, f"_modifier_{name}_cache_bytes", 0)
        setattr(scene, f"_modifier_{name}_cache_budget", 1024)


def _finish_one_job(scene):
    job = scene._effect_jobs.running
    if job is not None:
        job[3].result(timeout=5)
        scene._effect_jobs.poll()


def test_two_stage_pipelines_converge_when_scene_caches_cannot_hold_one_frame(scene, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _: None)
    bounds = QRectF(0, 0, 320, 240)
    stacks = [[PixelateModifier(pixel_size=11), HueSaturationLightnessModifier(hue=hue),
               BlurModifier(strength=2)] for hue in (30, -80)]
    expected = [render_stages(scene, image(), bounds, stack, QTransform())[0] for stack in stacks]
    _tiny_scene_caches(scene)
    scene._effect_jobs.retained_budget = 6 * int(image().sizeInBytes())
    scene._interactive_render = True

    def paint():
        exact = []
        for index, stack in enumerate(stacks):
            revision = getattr(scene, "_effect_provisional_revision", 0)
            # Source captures are rebuilt after the source LRU evicts them.
            actual, _ = render_stages(scene, image(), bounds, stack, QTransform(),
                request_scope=("small-cache", index), source_key=("capture", index))
            is_exact = revision == getattr(scene, "_effect_provisional_revision", 0)
            if is_exact:
                assert actual == expected[index]
            exact.append(is_exact)
        return all(exact)

    for _ in range(12):
        if paint():
            break
        _finish_one_job(scene)
    else:
        pytest.fail("Two pipelines repeatedly restarted after exact prefix eviction")
    assert scene._effect_jobs.submitted == 6
    assert scene._effect_jobs.retained_bytes <= scene._effect_jobs.retained_budget
    for _ in range(3):
        _tiny_scene_caches(scene)
        assert paint()
    assert scene._effect_jobs.submitted == 6


def test_two_direct_stacks_keep_exact_completions_after_scene_lru_eviction(scene):
    from comic_editor.ui.interactive_effects import render_interactive_stack
    from comic_editor.ui.modifier_rendering import apply_modifier_stack
    stacks = [[HueSaturationLightnessModifier(hue=hue), BlurModifier(strength=2)] for hue in (30, -80)]
    expected = [apply_modifier_stack(image(), stack, (0, 0)) for stack in stacks]
    _tiny_scene_caches(scene)
    scene._effect_jobs.retained_budget = 2 * int(image().sizeInBytes())
    scene._interactive_render = True

    def paint():
        exact = []
        for index, stack in enumerate(stacks):
            actual, provisional = render_interactive_stack(scene, image(), stack, (0, 0),
                cache_key=("output", index), scope=("direct", index))
            if not provisional:
                assert actual == expected[index]
            exact.append(not provisional)
        return all(exact)

    for _ in range(6):
        if paint():
            break
        _finish_one_job(scene)
    else:
        pytest.fail("Exact direct-stack completions were lost between target repaints")
    for _ in range(3):
        _tiny_scene_caches(scene)
        assert paint()
    assert scene._effect_jobs.submitted == 2
    assert scene._effect_jobs.retained_bytes <= scene._effect_jobs.retained_budget


@pytest.mark.parametrize("modifier", [HalftoneModifier(blur=0),
    CageTransformModifier(frame=(0, 0, 320, 240)), RadialBlurModifier(angle=25, center=(160, 120))])
def test_navigator_fallback_uses_compact_drafts_without_exact_jobs(scene, monkeypatch, modifier):
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _: None)
    monkeypatch.setattr("comic_editor.ui.gpu_textures.renderer_for", lambda _: None)
    scene._interactive_render = True
    scene._effect_preview_channel = "navigator"
    actual, _ = render_stages(scene, image(), QRectF(0, 0, 320, 240), [modifier],
        QTransform(), request_scope=("navigator",), source_key=("capture",))
    assert not actual.isNull()
    assert not scene._effect_jobs.submitted
    assert not scene._effect_jobs.retained
    assert scene._effect_provisional_revision > 0
    for key, value in scene._modifier_render_cache.items():
        if key[0] in {"pattern-draft", "cage-draft"}:
            assert max(value.width(), value.height()) <= 192


def test_exact_retention_replaces_scopes_is_byte_bounded_and_detaches_reads(qapp):
    owner = JobOwner()
    source = image()
    size = int(source.sizeInBytes())
    jobs = EffectJobs(owner, retained_budget=2 * size)
    try:
        for index in range(3):
            jobs.retained_put((index,), ("pixels",), source)
        assert len(jobs.retained) == 2 and jobs.retained_bytes == 2 * size
        assert jobs.retained_get((0,), ("pixels",)) is None
        detached, _ = jobs.retained_get((2,), ("pixels",))
        detached.fill(QColor("red"))
        assert jobs.retained_get((2,), ("pixels",))[0] == source
        jobs.retained_put((2,), ("new",), source)
        assert jobs.retained_get((2,), ("pixels",)) is None
        large = source.scaled(960, 720)
        jobs.retained_put((3,), ("oversized",), large)
        assert len(jobs.retained) == 1
        assert jobs.retained_bytes == large.sizeInBytes()
        jobs.cancel()
        assert not jobs.retained and not jobs.retained_bytes
    finally:
        owner.deleteLater()
