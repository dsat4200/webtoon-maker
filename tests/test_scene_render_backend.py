"""Production adapter must preserve legacy state and explicit request semantics."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor

from comic_editor.core.models import BoundGeometry, ChapterDocument
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.service import RenderQuality, RenderRequest, RenderStatus
from comic_editor.ui.async_projection import ProjectionFailed, ProjectionPending
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda _: None)
    chapter = ChapterDocument(width=64, height=64, document_kind="asset")
    chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 64, 64))
    owner = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    owner.set_document(chapter, TileStore())
    yield owner
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.deleteLater()


@pytest.mark.parametrize("outcome", ["exact", "pending", "failed", "provisional", "exception", "prepare"])
def test_capture_restores_ambient_state_on_every_exit(canvas, monkeypatch, outcome):
    document = canvas._render_document_state()
    value = RenderRequest((0., 0., 64., 64.), 1., (64, 64), ("isolated",), document.revision,
                          phase="base", quality=RenderQuality.EXACT, defer_effects=True)
    ambient = dict(_interactive_render=False, _effect_viewport_world=QRectF(1, 2, 3, 4),
        _vector_render_scale_override=3., _effect_region_requests=False,
        _projection_tile_key=("ambient",), _effect_preview_channel="navigator",
        _projection_exact=False, _projection_defer_effects=False,
        _bounded_effect_preview=True,
        _live_underlay_object_id="ambient-underlay", _live_underlay_amount=.25)
    for name, saved in ambient.items():
        setattr(canvas, name, saved)

    def scene(painter, visible, **kwargs):
        assert canvas._projection_tile_key == ("base", ("isolated",))
        assert canvas._projection_exact and canvas._projection_defer_effects
        assert not canvas._bounded_effect_preview
        assert canvas._effect_preview_channel == "canvas"
        assert canvas._effect_viewport_world == document.bounds
        assert canvas._live_underlay_object_id == document.underlay[0]
        assert kwargs == dict(underlay=True, only_phase="base")
        if outcome == "pending":
            raise ProjectionPending("source", "key")
        if outcome == "failed":
            raise ProjectionFailed("source", "key", "broken dependency")
        if outcome == "exception":
            raise RuntimeError("unexpected scene exception")
        if outcome == "provisional":
            canvas._effect_provisional_revision = getattr(canvas, "_effect_provisional_revision", 0) + 1
        painter.fillRect(visible, QColor("red"))
    monkeypatch.setattr(canvas, "_render_scene_layers", scene)
    if outcome == "prepare":
        def fail_prepare():
            raise RuntimeError("unexpected prepare exception")
        monkeypatch.setattr(canvas._render_bounds, "prepare", fail_prepare)
    if outcome in {"exception", "prepare"}:
        with pytest.raises(RuntimeError, match="unexpected"):
            canvas._render_service.render_region(document, value)
    else:
        result = canvas._render_service.render_region(document, value)
        assert result.status.value == outcome
        assert result.exact == (outcome == "exact")
    assert {name: getattr(canvas, name) for name in ambient} == ambient


@pytest.mark.parametrize('quality,key,bounded', [
    (RenderQuality.EXACT, ('native-preview', 0, 0), False),
    (RenderQuality.INTERACTIVE, ('native-preview', 0, 0), True),
    (RenderQuality.INTERACTIVE, ('interaction-preview',), True),
    (RenderQuality.INTERACTIVE, ('stroke-preview',), False),
])
def test_bounded_effect_flag_is_explicit_and_scoped(canvas, monkeypatch, quality, key, bounded):
    document = canvas._render_document_state()
    value = RenderRequest((0., 0., 64., 64.), 1., (64, 64), key, document.revision, quality=quality)
    assert not hasattr(canvas, '_bounded_effect_preview')
    def paint(painter, visible, **_):
        assert canvas._bounded_effect_preview == bounded
        assert canvas._effect_preview_channel == 'canvas'
        painter.fillRect(visible, QColor('red'))
    monkeypatch.setattr(canvas, '_render_scene_layers', paint)
    assert not canvas._render_service.render_region(document, value).image.isNull()
    assert not hasattr(canvas, '_bounded_effect_preview')


def test_backend_cannot_read_live_canvas_from_worker_thread(canvas):
    document = canvas._render_document_state()
    value = RenderRequest((0., 0., 64., 64.), 1., (64, 64), ("worker",), document.revision)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(canvas._render_service.render_region, document, value)
        with pytest.raises(RuntimeError, match="canvas thread"):
            future.result(timeout=5)


def test_snapshot_from_replaced_document_is_rejected_without_scene_work(canvas, monkeypatch):
    document = canvas._render_document_state()
    replacement = ChapterDocument(width=64, height=64, document_kind="asset")
    replacement.add_page()
    canvas.set_document(replacement, TileStore())
    monkeypatch.setattr(canvas, "_render_scene_layers", lambda *_a, **_k: pytest.fail("Stale scene painted"))
    # Even a coincidentally matching revision cannot identify a new document.
    document = replace(document, revision=canvas._document_projection.revision)
    value = RenderRequest((0., 0., 64., 64.), 1., (64, 64), ("old-doc",), document.revision)
    assert canvas._render_service.render_region(document, value).status is RenderStatus.STALE
