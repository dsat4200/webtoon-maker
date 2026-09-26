"""Finished Distort regions reuse immutable source setup with exact pixels."""
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.ui import distort_pipeline, distort_rendering as rendering
from comic_editor.ui.distort_rendering import PreparedDistortCache
from test_distort_rendering import BOUNDS, _ACTIVE, modifier, source_image


@pytest.mark.parametrize("interpolation", ["nearest", "bilinear", "bicubic"])
@pytest.mark.parametrize("edges", ["transparent", "white", "clamp", "wrap", "mirror"])
def test_cached_sampler_matches_uncached_for_every_sampling_mode(interpolation, edges):
    source = source_image()
    cache = PreparedDistortCache()
    mod = modifier("twirl", angle=85, interpolation=interpolation, edges=edges)
    for rect in (QRectF(-4, -3, 25, 30), QRectF(20, 15, 36, 29)):
        expected = rendering.render_distort(source, BOUNDS, mod, output_bounds=rect)
        actual = rendering.render_distort(source, BOUNDS, mod, output_bounds=rect,
                                           preparation_cache=cache)
        assert actual == expected
    assert cache.misses == 1 and cache.hits == 1
    prepared = cache.source(source, interpolation, edges)
    assert not prepared.pixels.flags.writeable
    assert all(not values.flags.writeable for values in prepared.sampler.filtered)
    with pytest.raises(ValueError):
        prepared.pixels[0, 0, 0] = 1


@pytest.mark.parametrize("effect,parameters", [*_ACTIVE, ("mesh_warp", {}),
    ("deform", {}), ("glitch", {"mode": "quantisation", "amount": 50.})])
def test_prepared_sources_preserve_all_effect_specific_preprocessing(effect, parameters):
    source, cache = source_image(), PreparedDistortCache()
    mod = modifier(effect, **parameters)
    if effect == "mesh_warp":
        mod.points[5] = (.6, .4)
    for rect in (BOUNDS, QRectF(12, 4, 25, 28)):
        expected = rendering.render_distort(source, BOUNDS, mod, output_bounds=rect)
        actual = rendering.render_distort(source, BOUNDS, mod, output_bounds=rect,
                                           preparation_cache=cache)
        assert actual == expected


def test_channel_views_are_counted_once_and_bicubic_coefficients_are_counted():
    source = source_image()
    cache = PreparedDistortCache()
    prepared = cache.source(source)
    assert cache.bytes == prepared.pixels.nbytes == 64 * 48 * 4 * 4
    cache.clear()
    prepared = cache.source(source, "bicubic", "transparent")
    expected = prepared.pixels.nbytes + 4 * (64 + 24) * (48 + 24) * 8
    assert cache.bytes == expected
    assert cache.bytes == prepared.pixels.nbytes + sum(v.nbytes for v in prepared.sampler.filtered)


def test_pixel_revisions_and_sampling_parameters_cannot_reuse_stale_setup():
    source = source_image()
    shared = QImage(source)
    cache = PreparedDistortCache()
    original = cache.source(source)
    assert cache.source(shared) is original
    shared.setPixelColor(20, 20, QColor("red"))
    changed = cache.source(shared)
    assert changed is not original
    np.testing.assert_array_equal(changed.pixels[20, 20], [1., 0., 0., 1.])
    assert cache.source(source) is original
    assert cache.source(source, "nearest") is not original
    assert cache.source(source, edges="clamp") is not original


def test_lru_budget_and_oversized_sources_keep_retention_bounded():
    images = [source_image() for _ in range(3)]
    size = 64 * 48 * 4 * 4
    cache = PreparedDistortCache(2 * size)
    first, second = cache.source(images[0]), cache.source(images[1])
    assert cache.source(images[0]) is first
    cache.source(images[2])
    assert cache.bytes == 2 * size and cache.evictions == 1
    assert cache.source(images[1]) is not second
    misses = cache.misses
    oversized = cache.source(images[0].scaled(256, 256))
    assert oversized.pixels.nbytes > cache.budget
    assert cache.bytes == 2 * size and cache.misses == misses + 1
    cache.clear()
    assert cache.bytes == 0 and not cache._entries


def test_metadata_count_is_bounded_even_for_tiny_sources():
    cache = PreparedDistortCache(entry_limit=3)
    for _ in range(12):
        source = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
        source.fill(QColor("red"))
        cache.source(source)
        assert len(cache._entries) <= 3
        assert cache.bytes <= 3 * 16


def test_downsampled_draft_cannot_replace_finished_source_setup():
    source = source_image().scaled(640, 480)
    bounds, rect = QRectF(0, 0, 640, 480), QRectF(100, 100, 60, 60)
    mod = modifier("twirl", angle=75)
    cache = PreparedDistortCache()
    expected = rendering.render_distort(source, bounds, mod, output_bounds=rect)
    first = rendering.render_distort(source, bounds, mod, output_bounds=rect,
                                     preparation_cache=cache)
    draft = rendering.render_distort(source, bounds, mod, output_bounds=rect,
                                     pixel_scale=.25, preparation_cache=cache)
    assert draft == rendering.render_distort(source, bounds, mod, output_bounds=rect, pixel_scale=.25)
    assert cache.misses == 2
    assert rendering.render_distort(source, bounds, mod, output_bounds=rect,
                                    preparation_cache=cache) == first == expected
    assert cache.misses == 2 and cache.hits == 1


def test_mesh_key_covers_every_tessellation_dependency(monkeypatch):
    calls = []
    original = rendering._mesh
    def tracked(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(rendering, "_mesh", tracked)
    mod, frame = modifier("mesh_warp"), np.array([0., 0., 64., 48.])
    cache = PreparedDistortCache()
    first = cache.mesh(mod, frame, mod.parameters)
    assert cache.mesh(mod, frame.copy(), dict(mod.parameters)) is first
    assert len(calls) == 1
    assert all(not value.flags.writeable for value in first)
    mod.points[5] = (.6, .4)
    assert cache.mesh(mod, frame, mod.parameters) is not first
    mod.source_points[5] = (.4, .6)
    cache.mesh(mod, frame, mod.parameters)
    mod.parameters["smoothness"] = 0.
    cache.mesh(mod, frame, mod.parameters)
    frame[0] += 1
    cache.mesh(mod, frame, mod.parameters)
    other = modifier("mesh_warp", rows=3, columns=5)
    cache.mesh(other, frame, other.parameters)
    assert len(calls) == 6
    assert cache.bytes == sum(entry[1] for entry in cache._entries.values())


def test_only_exact_synchronous_projection_receives_the_canvas_cache(monkeypatch):
    source = source_image().scaled(320, 240)
    bounds = QRectF(0, 0, 320, 240)
    mod = modifier("twirl", angle=10)
    pending, observed = [], []
    original = rendering.render_distort
    def track(*args, **kwargs):
        observed.append(kwargs.get("preparation_cache"))
        return original(*args, **kwargs)
    monkeypatch.setattr(rendering, "render_distort", track)
    def request(_scope, _key, compute, *_args, **_kwargs):
        pending.append(compute)
        return True
    canvas = SimpleNamespace(_interactive_render=True, _render_base_alpha=False,
        _rendering_mask_contributor=0, _projection_exact=False,
        _effect_jobs=SimpleNamespace(request=request),
        _modifier_cache_get=lambda _: None, _modifier_cache_put=lambda *_: None)
    def stage():
        return distort_pipeline.render_distort_stage(canvas, source, source, bounds, bounds,
            mod, QTransform(), {}, ("key",), ("scope",), False, False)
    stage()
    assert observed == [None] and len(pending) == 1
    canvas._projection_exact = True
    # The detached queued closure must stay independent of later GUI state.
    pending.pop()(lambda: False)
    assert observed == [None, None]
    stage()
    assert isinstance(observed[-1], PreparedDistortCache)
    cache = observed[-1]
    stage()
    assert observed[-1] is cache and cache.hits == 1


@pytest.mark.parametrize("action", ["set_document", "restore_session", "clear_document", "history"])
def test_chapter_lifecycle_releases_prepared_numpy_storage(qapp, monkeypatch, action):
    from comic_editor.core.models import ChapterDocument
    from comic_editor.core.settings import EditorSettings
    from comic_editor.core.tiles import TileStore
    from comic_editor.ui.canvas import CanvasWidget
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(ChapterDocument(width=64, height=48, document_kind="asset"), TileStore())
    session = canvas.capture_session_state()
    cache = canvas._distort_preparation_cache = PreparedDistortCache()
    cache.source(source_image())
    assert cache.bytes > 0
    try:
        if action == "set_document":
            canvas.set_document(ChapterDocument(width=64, height=48, document_kind="asset"), TileStore())
        elif action == "restore_session":
            canvas.restore_session_state(session)
        elif action == "history":
            canvas._restore_history_state(canvas.chapter.to_dict())
        else:
            canvas.clear_document()
        assert cache.bytes == 0 and not cache._entries
    finally:
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()
