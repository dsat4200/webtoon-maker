"""Exact completion aliases must not evict unchanged compact image prefixes."""
import pytest
from threading import Event
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.render.tile_graph import TileGraph, TileNode
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.ui import distort_pipeline
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.tile_effects import _put_graph_result
from test_live_image_prefix_reuse import scene
from test_navigator_patterns import canvas, preview
from test_tile_graph_assembly import bits, native_image


@pytest.mark.parametrize('kind', ['twirl', 'mesh'])
@pytest.mark.parametrize('pressure_kind', ['oversized', 'regional'])
def test_native_exact_completion_preserves_prefix_through_late_mask_undo(
        canvas, monkeypatch, kind, pressure_kind):
    _obj, warp, _color, mask = scene(canvas)
    if kind == 'mesh':
        warp.modifier_type = 'distort_mesh_warp'
        warp.parameters = {'rows': 2, 'columns': 2}
        warp.points = []
        warp.source_points = []
        warp.validate()
        warp.points[0] = (.04, .02)
    calls, original = [], distort_pipeline.render_distort_stage
    def observed(*args, **kwargs):
        calls.append(args[5].modifier_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', observed)
    before = preview(canvas, False, live=True)
    assert calls == [warp.modifier_id]
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_mask_stroke(QPointF(380, 310), 1.)
    canvas._flush_mask_samples()
    canvas._end_mask_stroke()
    painted = preview(canvas, False, live=True)
    assert painted != before and calls == [warp.modifier_id]
    prefixes = {key: image.cacheKey() for key, image in canvas._modifier_render_cache.items()
                if key[:1] == ('live-effect-draft-stage',)}
    assert prefixes
    if pressure_kind == 'oversized':
        canvas._modifier_render_cache_budget = canvas._modifier_render_cache_bytes
        pressure = QImage(1024, 512, QImage.Format_ARGB32_Premultiplied)
        pressure.fill(QColor('green'))
        assert pressure.sizeInBytes() > canvas._modifier_render_cache_budget
        jobs, scope, key = canvas._effect_jobs, ('pressure-frame',), ('native-pressure-frame',)
        jobs.request(scope, key, lambda _: QImage(pressure), pressure.sizeInBytes(), require_exact=True)
        jobs.running[3].result(timeout=5)
        jobs.poll()
        # The unchanged native completion must remain available for actual consumers.
        assert jobs.result(scope, key) == pressure
    else:
        # Real v12 settlement evicted all 21 compact prefixes with a stream of
        # native 256px exact tiles, each below the ordinary 64 MiB budget.
        # Scale the existing budget while retaining its measured quarter limit.
        canvas._modifier_render_cache_budget = 4 * sum(
            canvas._modifier_render_cache[key].sizeInBytes() for key in prefixes)
        for index in range(32):
            pressure = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
            pressure.fill(QColor(index, 200, 40))
            assert pressure.sizeInBytes() < canvas._modifier_render_cache_budget
            key = ('effect-tile', ('native-regional-pressure', index), (0., 0., 256., 256.))
            canvas._modifier_cache_put(key, pressure)
            assert canvas._modifier_cache_get(key).cacheKey() == pressure.cacheKey()
            assert canvas._modifier_render_cache_bytes <= canvas._modifier_render_cache_budget
    assert all(key in canvas._modifier_render_cache and
               canvas._modifier_render_cache[key].cacheKey() == storage
               for key, storage in prefixes.items())
    canvas.command_stack.undo()
    assert canvas.tiles.content_bounds(mask.mask_id) is None
    restored = preview(canvas, False, live=True)
    assert restored == before
    assert calls == [warp.modifier_id], 'Native exact pressure evicted an unchanged compact prefix'
    assert all(canvas._modifier_render_cache[key].cacheKey() == storage
               for key, storage in prefixes.items() if key in canvas._modifier_render_cache)
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    assert preview(canvas, False, live=True) == restored
    assert calls == [warp.modifier_id, warp.modifier_id]


def cached_prefix(canvas):
    key = ('live-effect-draft-stage', ('contract',), ('proved-prefix',))
    image = QImage(4, 4, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    canvas._modifier_render_cache_budget = 128
    canvas._modifier_cache_put(key, image)
    return key, image


@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
def test_identical_retained_native_completion_skips_only_oversized_ram_alias(canvas, image_format):
    prefix_key, prefix = cached_prefix(canvas)
    source = native_image(13, 11, image_format)
    key, scope = ('completed-native-stage', image_format.value), ('native-stage',)
    old = QImage(2, 2, image_format)
    old.fill(QColor('blue'))
    canvas._modifier_cache_put(key, old)
    assert canvas._effect_jobs.retained_put(('result', scope), key, source, force=True)
    canvas._modifier_cache_put(key, QImage(source))
    assert key not in canvas._modifier_render_cache
    assert canvas._modifier_cache_get(prefix_key).cacheKey() == prefix.cacheKey()
    assert canvas._effect_jobs.result(scope, key).cacheKey() == source.cacheKey()
    assert canvas._modifier_render_cache_bytes == prefix.sizeInBytes()


@pytest.mark.parametrize('retained', ['absent', 'different-key', 'independent-storage', 'different-format'])
def test_unproved_native_alias_keeps_exclusive_ordinary_admission(canvas, retained):
    prefix_key, _prefix = cached_prefix(canvas)
    source = native_image(13, 11)
    key = ('current-native-stage',)
    alternate = source.copy() if retained == 'independent-storage' else (
        source.convertToFormat(QImage.Format_RGBA8888) if retained == 'different-format' else source)
    if retained != 'absent':
        retained_key = ('other-native-stage',) if retained == 'different-key' else key
        assert canvas._effect_jobs.retained_put(('result', 'native'), retained_key, alternate, force=True)
    canvas._modifier_cache_put(key, source)
    assert prefix_key not in canvas._modifier_render_cache
    assert canvas._modifier_cache_get(key).cacheKey() == source.cacheKey()
    assert canvas._modifier_render_cache_bytes == source.sizeInBytes()


@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
def test_priority_bytes_stay_in_same_lru_and_old_prefixes_age_out(canvas, image_format):
    source = native_image(13, 11, image_format)
    canvas._modifier_render_cache_budget = 4 * source.sizeInBytes()
    keys = [('live-effect-draft-stage', 'current-source', index) for index in range(4)]
    for key in keys:
        canvas._modifier_cache_put(key, source)
    for index in range(12):
        tile = native_image(9, 8, image_format)
        key = ('effect-tile', 'current-native', index)
        canvas._modifier_cache_put(key, tile)
        assert bits(canvas._modifier_cache_get(key)) == bits(tile)
        assert canvas._modifier_render_cache_bytes <= canvas._modifier_render_cache_budget
    surviving = {key for key in keys if key in canvas._modifier_render_cache}
    assert surviving == {keys[-1]}
    assert canvas._modifier_render_cache[keys[-1]].format() == image_format
    assert bits(canvas._modifier_render_cache[keys[-1]]) == bits(source)
    assert canvas._modifier_render_cache_bytes == sum(
        image.sizeInBytes() for image in canvas._modifier_render_cache.values())


def test_priority_record_limit_and_reads_use_existing_lru_recency(canvas):
    source = native_image(4, 4)
    canvas._modifier_render_cache_budget = 32 * 1024
    keys = [('live-effect-draft-stage', 'current-source', index) for index in range(70)]
    for key in keys:
        canvas._modifier_cache_put(key, source)
    assert canvas._modifier_cache_get(keys[0]).cacheKey() == source.cacheKey()
    tile = native_image(16, 16)
    for index in range(80):
        canvas._modifier_cache_put(('effect-tile', 'current-native', index), tile)
        assert canvas._modifier_render_cache_bytes <= canvas._modifier_render_cache_budget
    assert {key for key in keys if key in canvas._modifier_render_cache} == {keys[0], *keys[7:]}


def test_priority_measured_ceiling_does_not_reserve_the_whole_quarter(canvas):
    source = native_image(512, 512)
    assert source.sizeInBytes() == 1024 * 1024
    original_budget = canvas._modifier_render_cache_budget
    assert original_budget == 64 * 1024 * 1024
    keys = [('live-effect-draft-stage', 'current-source', index) for index in range(9)]
    for key in keys:
        canvas._modifier_cache_put(key, source)
    for index in range(64):
        canvas._modifier_cache_put(('effect-tile', 'current-native', index), source)
        assert canvas._modifier_render_cache_bytes <= original_budget
    assert {key for key in keys if key in canvas._modifier_render_cache} == set(keys[1:])
    assert canvas._modifier_render_cache_budget == original_budget


@pytest.mark.parametrize('oversized', [False, True])
def test_native_progress_can_displace_priority_when_it_needs_the_budget(canvas, oversized):
    canvas._modifier_render_cache_budget = 1024
    source = native_image(4, 4)
    keys = [('live-effect-draft-stage', 'current-source', index) for index in range(4)]
    for key in keys:
        canvas._modifier_cache_put(key, source)
    native = native_image(16, 32 if oversized else 15)
    key = ('effect-tile', 'large-current-native')
    canvas._modifier_cache_put(key, native)
    assert bits(canvas._modifier_cache_get(key)) == bits(native)
    assert canvas._modifier_render_cache_bytes <= max(1024, native.sizeInBytes())
    assert {value for value in keys if value in canvas._modifier_render_cache} == (
        set() if oversized else {keys[-1]})


@pytest.mark.parametrize('admitted', [False, True], ids=['rejected-regional', 'admitted-frame'])
@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
def test_actual_graph_handoff_survives_raw_consumption_or_admission_rejection(
        canvas, admitted, image_format):
    prefix_key, prefix = cached_prefix(canvas)
    source = native_image(13, 11, image_format)
    frame = QRectF(0, 0, source.width(), source.height())
    jobs, scope, raw_scope = canvas._effect_jobs, ('current-graph',), ('native-worker',)
    raw_key = ('native-stage', image_format.value)
    jobs.retained_budget = 3 * source.sizeInBytes()
    jobs.request(raw_scope, raw_key, lambda _: QImage(source), source.sizeInBytes(), require_exact=True)
    jobs.running[3].result(timeout=5)
    jobs.poll()
    assert raw_key not in canvas._modifier_render_cache
    assert canvas._modifier_cache_get(prefix_key).cacheKey() == prefix.cacheKey()
    if not admitted:
        # The existing pool really rejects a regional alias when its only
        # record is protected. The raw handoff must survive that rejection.
        jobs.retained_limit = 1
        assert jobs.retained_put(('result', raw_scope), raw_key, source, shared=True)
    preflights, graph_writes = [], []
    def forbidden(*_):
        pytest.fail('Completed native preflight rebuilt source dependencies')
    def get(owner, key):
        ordinary = canvas._modifier_cache_get(key)
        if ordinary is not None:
            return ordinary
        retained = jobs.retained_get(('tile-graph', scope, owner), key)
        return None if retained is None else retained[0]
    def put(owner, key, result):
        graph_writes.append((owner, key))
        _put_graph_result(canvas, scope, owner, key, result)
    def completed(_):
        preflights.append(1)
        result = jobs.result(raw_scope, raw_key)
        assert result is not None
        return result
    nodes = [TileNode(frame, ('source', image_format.value), None, None, format=image_format),
             TileNode(frame, ('finished-native-stage', image_format.value), forbidden, forbidden,
                 shared_frame=admitted, format=image_format, cached_output=completed)]
    for _ in range(2):
        result, actual_frame = TileGraph(nodes, forbidden, get, put).output(frame)
        assert actual_frame == frame and bits(result) == bits(source)
        assert result.format() == image_format
    assert preflights == [1] and len(graph_writes) == 1
    assert jobs.submitted == jobs.completed == 1 and not jobs.discarded
    assert (('result', raw_scope) not in jobs.retained) is admitted
    assert (prefix_key in canvas._modifier_render_cache) is admitted
    assert canvas._modifier_render_cache_bytes <= max(
        canvas._modifier_render_cache_budget, source.sizeInBytes())
    assert jobs.retained_bytes <= jobs.retained_budget


@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
def test_skipped_ram_alias_keeps_exact_disk_pipeline_and_excludes_drafts(canvas, tmp_path, image_format):
    prefix_key, _prefix = cached_prefix(canvas)
    source = native_image(13, 11, image_format)
    key = ('finished-native-stage', image_format.value)
    assert canvas._effect_jobs.retained_put(('result', 'native'), key, source, force=True)
    regional = native_image(4, 4, image_format)
    canvas._modifier_render_cache_budget = max(4 * regional.sizeInBytes(), 256)
    backing = canvas._persistent_render_cache = PersistentRenderCache(tmp_path)
    canvas._projection_exact = True
    try:
        with backing.record():
            canvas._modifier_cache_put(key, source)
            assert key not in canvas._modifier_render_cache
            canvas._modifier_cache_put(prefix_key, canvas._modifier_render_cache[prefix_key])
            for index in range(8):
                canvas._modifier_cache_put(('effect-tile', 'recorded-pressure', index), regional)
            assert prefix_key in canvas._modifier_render_cache
        backing.drain()
        exact = backing.lookup('effect', key, wait=True)
        assert exact is not None and exact.format() == image_format
        assert bits(exact) == bits(source)
        assert backing.lookup('effect', prefix_key, wait=True) is None
    finally:
        backing.close()
        canvas._persistent_render_cache = None


@pytest.mark.parametrize('event', ['cancel-exact', 'history'])
def test_obsolete_worker_cannot_publish_an_oversized_alias_or_handoff(canvas, event):
    # This canvas fixture is 1200 px wide, a valid asset rather than a chapter.
    canvas.chapter.document_kind = 'asset'
    prefix_key, _prefix = cached_prefix(canvas)
    canvas._modifier_render_cache_budget = 256
    for index in range(8):
        canvas._modifier_cache_put(('effect-tile', 'obsolete-worker-pressure', index), native_image(4, 4))
    assert prefix_key in canvas._modifier_render_cache
    source = native_image(13, 11)
    entered, release = Event(), Event()
    def compute(_):
        entered.set()
        assert release.wait(5)
        return QImage(source)
    jobs, scope, key = canvas._effect_jobs, ('obsolete-native-worker',), ('obsolete-stage',)
    jobs.request(scope, key, compute, source.sizeInBytes(), require_exact=True)
    try:
        assert entered.wait(2)
        future = jobs.running[3]
        if event == 'cancel-exact':
            assert jobs.cancel_exact()
        else:
            canvas.replace_chapter(canvas.chapter.to_dict())
        release.set()
        future.result(timeout=5)
        jobs.poll()
    finally:
        release.set()
    assert jobs.result(scope, key) is None
    assert key not in canvas._modifier_render_cache
    assert jobs.completed == 0 and jobs.discarded == 1
    assert (prefix_key in canvas._modifier_render_cache) is (event == 'cancel-exact')
