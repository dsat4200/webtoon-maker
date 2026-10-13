"""Projection diagnostics observe the real retained path without pixel capture."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import DistortModifier, OutlineModifier, RasterObject
from comic_editor.ui import distort_pipeline, distort_rendering, modifier_rendering
from comic_editor.ui.document_presentation import PresentationStats
from test_performance_monitor_integration import editor, controller, _phases


def test_dependency_pin_and_index_probes_require_owned_scope_and_restore(editor, controller, monkeypatch):
    from comic_editor.core.tile_backing import PrefetchedPins, SnapshotBacking
    from comic_editor.render.cache import PersistentRenderCache
    from comic_editor.render.scene import DetachedSceneBackend
    from comic_editor.render.scene_kernels import SceneKernels
    from comic_editor.ui.cache_dependencies import RenderDependencies
    probes = (
        (SceneKernels, '_modifier_object_signature', 'scene.modifier_object_signature'),
        (SceneKernels, '_modifier_layer_signature', 'scene.modifier_layer_signature'),
        (RenderDependencies, 'tiles', 'scene.dependencies.tiles'),
        (RenderDependencies, 'file_digest', 'scene.dependencies.file_digest'),
        (PrefetchedPins, 'pin', 'scene.source_pin'),
        (SnapshotBacking, 'pin', 'scene.backing_pin'),
        (PersistentRenderCache, '__init__', 'scene.disk_cache_init'),
        (PersistentRenderCache, '_committed_index', 'scene.disk_cache_committed_index'),
        (PersistentRenderCache, '_reload_index', 'scene.disk_cache_reload_index'),
        (DetachedSceneBackend, '__init__', 'scene.backend_init'),
    )

    class SemanticKey:
        def __repr__(self):
            raise AssertionError('Diagnostic inspected a semantic key')
        __str__ = __repr__

    sentinel, private_argument, calls = object(), SemanticKey(), []
    originals = {}
    for owner, name, label in probes:
        def original(*args, _label=label, **kwargs):
            calls.append((_label, args, kwargs))
            return sentinel
        monkeypatch.setattr(owner, name, original)
        originals[owner, name] = original
    controller.start()
    from comic_editor.ui import performance_monitor
    clocks = iter(i * .002 for i in range(100))
    monkeypatch.setattr(performance_monitor, 'time', SimpleNamespace(
        perf_counter=lambda: next(clocks), thread_time=performance_monitor.time.thread_time))
    retained = {(owner, name): getattr(owner, name) for owner, name, _ in probes}
    metadata = dict(ownership='monitored_editor', scope='detached_worker',
        consumer='canvas', scheduler_role='canvas', demand_serial=17, document_revision=23)

    def invoke(metadata=None):
        if metadata is not None:
            with controller._owned_scope(metadata):
                invoke()
            return
        for owner, name, _ in probes:
            assert getattr(owner, name)(private_argument, private_argument) is sentinel

    # Unscoped and unrelated legacy work remains invisible even on a worker.
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(invoke).result()
        executor.submit(invoke, dict(ownership='legacy_global_unattributed', scope='legacy_global')).result()
        assert not any(label in _phases(controller) for _, _, label in probes)
        executor.submit(invoke, metadata).result()
    phases = _phases(controller)
    for _, _, label in probes:
        assert phases[label]['count'] == 1
        assert phases[label + '.thread_cpu']['count'] == 1
    rows = [row for row in controller.snapshot()['timeline']
        if row['name'] in {label for _, _, label in probes}]
    assert len(rows) == len(probes)
    assert all(row['details']['thread'] == 'worker'
        and row['details']['demand_serial'] == 17
        and row['details']['document_revision'] == 23 for row in rows)
    json.dumps(rows)  # No source argument or raw semantic representation escaped.
    controller.stop()
    for owner, name, label in probes:
        assert getattr(owner, name) is originals[owner, name]
        assert retained[owner, name](private_argument, private_argument) is sentinel
        assert _phases(controller)[label]['count'] == 1
    assert len(calls) == len(probes) * 4


def test_filtered_owned_leaf_keeps_all_summaries_and_slow_error_pairs(editor, controller, monkeypatch):
    from comic_editor.ui import performance_monitor

    class Leaf:
        def invoke(self, fail=False):
            if fail:
                raise ValueError('private exception message')
            return 17

    controller.start()
    controller._patch_owned(Leaf, 'invoke', 'scene.new_leaf', cpu=True, timeline_min_ms=1.)
    original_time = performance_monitor.time
    walls = iter((0, .0002, .010, .012, .020, .0201))
    cpus = iter((0, .0001, .020, .021, .040, .040))
    monkeypatch.setattr(performance_monitor, 'time', SimpleNamespace(
        perf_counter=lambda: next(walls), thread_time=lambda: next(cpus)))
    metadata = dict(ownership='monitored_editor', scope='detached_worker', demand_serial=29)
    def invoke():
        with controller._owned_scope(metadata):
            assert Leaf().invoke() == 17
            assert Leaf().invoke() == 17
            with pytest.raises(ValueError, match='private exception message'):
                Leaf().invoke(True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(invoke).result()
    monkeypatch.setattr(performance_monitor, 'time', original_time)
    rows = [row for row in controller.snapshot()['timeline'] if row['name'].startswith('scene.new_leaf')]
    assert [row['name'] for row in rows] == ['scene.new_leaf', 'scene.new_leaf.thread_cpu'] * 2
    assert all(row['details']['thread'] == 'worker' and row['details']['demand_serial'] == 29 for row in rows)
    assert rows[2]['details']['exception_type'] == rows[3]['details']['exception_type'] == 'ValueError'
    assert 'private exception message' not in json.dumps(rows)
    phases = _phases(controller)
    assert phases['scene.new_leaf']['count'] == phases['scene.new_leaf.thread_cpu']['count'] == 3
    assert phases['scene.new_leaf']['total_ms'] == pytest.approx(2.3)
    assert phases['scene.new_leaf.thread_cpu']['total_ms'] == pytest.approx(1.1)


def test_gpu_presentation_spans_require_owned_paint_and_restore(editor, controller, monkeypatch):
    from comic_editor.ui.document_presentation import GpuTilePresenter
    canvas = editor[0].canvas
    presenter = GpuTilePresenter()
    original = lambda _self: {}
    monkeypatch.setattr(GpuTilePresenter, '_save_state', original)
    monkeypatch.setattr(canvas, '_draw_selection', lambda _painter: presenter._save_state())
    controller.start()
    presenter._save_state()
    assert 'presentation.save_state' not in _phases(controller)
    canvas._draw_selection(None)
    assert _phases(controller)['presentation.save_state']['count'] == 1
    rows = [row for row in controller.snapshot()['timeline']
        if row['name'] == 'presentation.save_state']
    assert rows and all(row['details']['ownership'] == 'monitored_editor'
        and row['details']['thread'] == 'gui' for row in rows)
    retained = presenter._save_state
    controller.stop()
    assert GpuTilePresenter._save_state is original
    retained()
    assert _phases(controller)['presentation.save_state']['count'] == 1


def test_native_painter_boundaries_require_owned_gui_canvas_and_restore(editor, controller, monkeypatch):
    canvas = editor[0].canvas
    calls = []
    begin = lambda painter: calls.append(('begin', painter))
    end = lambda painter: calls.append(('end', painter))
    monkeypatch.setattr(QPainter, 'beginNativePainting', begin)
    monkeypatch.setattr(QPainter, 'endNativePainting', end)
    gui = threading.get_ident()

    class Painter:
        def __init__(self, device, active=True):
            self.owner, self.active = device, active

        def isActive(self):
            assert threading.get_ident() == gui, 'Worker probe inspected a Qt painter'
            return self.active

        def device(self):
            assert threading.get_ident() == gui, 'Worker probe inspected a Qt device'
            return self.owner

    owned = Painter(canvas)
    foreign = Painter(QImage(2, 2, QImage.Format_ARGB32_Premultiplied))
    inactive = Painter(canvas, active=False)
    controller.start()
    QPainter.beginNativePainting(owned)
    assert 'presentation.begin_native_painting' not in _phases(controller)
    metadata = dict(ownership='monitored_editor', scope='gui_adapter')
    with controller._owned_scope(metadata):
        QPainter.beginNativePainting(foreign)
        QPainter.endNativePainting(inactive)
        QPainter.beginNativePainting(owned)
        QPainter.endNativePainting(owned)
    assert _phases(controller)['presentation.begin_native_painting']['count'] == 1
    assert _phases(controller)['presentation.end_native_painting']['count'] == 1

    def worker_call():
        with controller._owned_scope(metadata):
            QPainter.endNativePainting(owned)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(worker_call).result()
    assert _phases(controller)['presentation.end_native_painting']['count'] == 1
    retained = QPainter.endNativePainting
    controller.stop()
    assert QPainter.beginNativePainting is begin and QPainter.endNativePainting is end
    retained(owned)
    assert _phases(controller)['presentation.end_native_painting']['count'] == 1
    assert len(calls) == 7


def test_border_split_probes_require_guarded_owner_and_restore(editor, controller, monkeypatch):
    from comic_editor.ui import document_presentation
    canvas = editor[0].canvas
    builds, draws = [], []
    build = lambda *args: builds.append(args) or []
    draw = lambda *args: draws.append(args)
    def border(painter):
        patches = document_presentation._border_patches(object(), object(), 1.)
        document_presentation._draw_document_border_patches(painter, patches)
    monkeypatch.setattr(document_presentation, '_border_patches', build)
    monkeypatch.setattr(document_presentation, '_draw_document_border_patches', draw)
    monkeypatch.setattr(document_presentation, 'draw_document_border', border)
    gui = threading.get_ident()
    class Painter:
        def __init__(self, device):
            self.owner = device
        def isActive(self):
            assert threading.get_ident() == gui
            return True
        def device(self):
            assert threading.get_ident() == gui
            return self.owner
    owned, foreign = Painter(canvas), Painter(None)
    controller.start()
    metadata = dict(ownership='monitored_editor', scope='gui_adapter')
    document_presentation.draw_document_border(owned)
    with controller._owned_scope(metadata):
        document_presentation._border_patches(object(), object(), 1.)
        document_presentation.draw_document_border(foreign)
        document_presentation.draw_document_border(owned)
    def worker():
        with controller._owned_scope(metadata):
            document_presentation.draw_document_border(owned)
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(worker).result()
    for name in ('document_border', 'border_construction', 'border_draw_patches'):
        assert _phases(controller)['presentation.' + name]['count'] == 1
    rows = [row for row in controller.snapshot()['timeline']
        if row['name'] == 'presentation.border_construction']
    assert rows[0]['details']['presentation_owner'] == 'document_border'
    retained = document_presentation.draw_document_border
    controller.stop()
    assert document_presentation._border_patches is build
    assert document_presentation._draw_document_border_patches is draw
    retained(owned)
    assert _phases(controller)['presentation.border_construction']['count'] == 1
    assert len(builds) == 6 and len(draws) == 5


def test_postpresent_helpers_and_trim_require_current_canvas_owner(editor, controller, monkeypatch):
    from comic_editor.render.projection import DocumentProjection
    from comic_editor.ui import document_projection_features, raster_feedback
    canvas = editor[0].canvas
    calls = []
    border = lambda *args, **kwargs: calls.append('border')
    groups = lambda *args, **kwargs: calls.append('groups')
    pending = lambda *args, **kwargs: calls.append('pending')
    monkeypatch.setattr(document_projection_features, 'draw_document_border', border)
    monkeypatch.setattr(document_projection_features, 'draw_document_tile_groups', groups)
    monkeypatch.setattr(raster_feedback, 'present_pending_raster_gesture', pending)
    owner = type('Painter', (), {'isActive': lambda self: True, 'device': lambda self: canvas})()
    foreign = type('Painter', (), {'isActive': lambda self: True, 'device': lambda self: None})()
    other_projection = DocumentProjection()
    controller.start()
    metadata = dict(ownership='monitored_editor', scope='gui_adapter')
    with controller._owned_scope(metadata):
        document_projection_features.draw_document_border(owner)
        document_projection_features.draw_document_tile_groups(owner)
        raster_feedback.present_pending_raster_gesture(canvas, owner, None)
        canvas._document_projection._trim(set())
        document_projection_features.draw_document_border(foreign)
        raster_feedback.present_pending_raster_gesture(object(), owner, None)
        other_projection._trim(set())
    phases = _phases(controller)
    for name in ('presentation.document_border', 'presentation.tile_groups',
                 'presentation.pending_gesture', 'presentation.projection_trim'):
        assert phases[name]['count'] == 1
    controller.stop()
    assert document_projection_features.draw_document_border is border
    assert document_projection_features.draw_document_tile_groups is groups
    assert raster_feedback.present_pending_raster_gesture is pending
    assert len(calls) == 5


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
