"""Retained hierarchy phases insert prediction without redrawing the scene."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import BoundGeometry, ChapterDocument, OutlineModifier, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from test_dirty_modifier_reuse import watch_effect_work


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    chapter = ChapterDocument(width=480, height=480, background="#ffffffff")
    page = chapter.add_page("Page", BoundGeometry.rectangle(40, 40, 400, 400))
    page.fill_color, page.border_width = None, 0
    ordinary = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 480, 480)))
    promoted = chapter.add_object(page.layer_id, RasterObject(show_on_top=True))
    tiles = TileStore()
    tiles.paint_dab(ordinary.object_id, QPointF(180, 240), 180, QColor("blue"))
    tiles.paint_dab(promoted.object_id, QPointF(260, 240), 100, QColor("red"))
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False, predictive_ink=True,
                                         snap_to_grid=False, canvas_renderer="raster"))
    canvas.setFixedSize(480, 480)
    canvas.set_document(chapter, tiles)
    canvas.center_x = canvas.center_y = 240
    canvas.scale = 1.
    canvas.set_selection("object", ordinary.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._document_projection_enabled = True
    yield canvas, ordinary, promoted
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def render(canvas, *, legacy=False, live=True):
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#242428"))
    if legacy:
        canvas._projection_exact = True
        try:
            canvas._render_scene_cache_rect(canvas.rect(), target=image, live_ink=True)
        finally:
            canvas._projection_exact = False
    else:
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        try:
            canvas._paint_document_projection(painter, live_ink=live)
        finally:
            painter.end()
    return image


@pytest.mark.parametrize("promoted", [False, True])
@pytest.mark.parametrize("underlay", [0., .35])
@pytest.mark.parametrize("overflow", [0., .4])
def test_prediction_matches_finished_legacy_order(scene, promoted, underlay, overflow):
    canvas, ordinary, top = scene
    obj = top if promoted else ordinary
    obj.underlay_opacity = underlay
    canvas.chapter.view_overflow = overflow
    canvas.set_selection("object", obj.object_id)
    canvas._predictive = (QPointF(20, 240), QPointF(360, 240), 24, QColor("#ff33bb22"))
    expected = render(canvas, legacy=True)
    actual = render(canvas)
    assert actual == expected
    assert canvas._show_on_top_phase is None
    assert canvas._active_top_plan is None
    assert canvas._projection_capture_phase is None
    if not promoted:
        assert actual.pixelColor(260, 240) == QColor("red")


def test_prediction_reuses_both_phases_without_legacy_or_effect_work(scene, monkeypatch, wait_scene):
    canvas, _, top = scene
    canvas.chapter.add_modifier(OutlineModifier(thickness=5), [("object", top.object_id)])
    canvas._predictive = (QPointF(80, 240), QPointF(160, 240), 24, QColor("green"))
    canvas.grab()
    wait_scene(canvas)
    calls, requests, _ = watch_effect_work(canvas, monkeypatch)
    def forbidden(*args, **kwargs):
        raise AssertionError("Prediction recaptured the reusable document")
    monkeypatch.setattr(canvas, "_render_scene_cache_rect", forbidden)
    monkeypatch.setattr(canvas, "_render_document_region", forbidden)
    renders = canvas._document_projection.renders
    windows = canvas._projection_windows
    for x in (180, 220, 280, 320):
        canvas._predictive = (QPointF(x-30, 240), QPointF(x, 240), 24, QColor("green"))
        canvas.grab()
    assert not calls and not requests
    assert canvas._document_projection.renders == renders
    assert canvas._projection_windows is windows
    assert canvas._document_projection.snapshot()["configurations"] == 2


def test_phase_tiles_and_alternate_selections_share_the_global_budget(scene):
    canvas, ordinary, top = scene
    canvas._document_projection.budget = 3 * 260 * 260 * 4
    ordinary.underlay_opacity = .2
    top.underlay_opacity = .3
    for obj in (ordinary, top, ordinary, top):
        canvas.set_selection("object", obj.object_id)
        canvas._predictive = (QPointF(100, 240), QPointF(300, 240), 20, QColor("green"))
        render(canvas)
        projection = canvas._document_projection
        assert projection.bytes <= projection.budget
        assert projection.snapshot()["configurations"] <= projection.configuration_limit


def test_ordinary_combined_pixels_do_not_change_when_prediction_ends(scene):
    canvas, _, _ = scene
    before = render(canvas, live=False)
    canvas._predictive = (QPointF(100, 240), QPointF(300, 240), 20, QColor("green"))
    assert render(canvas) != before
    canvas._predictive = None
    assert render(canvas, live=False) == before


def test_translucent_top_artwork_keeps_prediction_below_each_promoted_layer(scene):
    canvas, _, top = scene
    top.opacity = .6
    other = canvas.chapter.add_object(top.parent_layer_id, RasterObject(show_on_top=True, opacity=.4))
    canvas.tiles.paint_dab(other.object_id, QPointF(240, 240), 80, QColor("yellow"))
    canvas._predictive = (QPointF(100, 240), QPointF(300, 240), 20, QColor("green"))
    expected, actual = render(canvas, legacy=True), render(canvas)
    delta = np.abs(np.frombuffer(actual.constBits(), np.uint8).astype(np.int16)
                   - np.frombuffer(expected.constBits(), np.uint8).astype(np.int16))
    # A transparent retained pass introduces one additional RGBA8 composite;
    # rounding may differ by one unit, but every source/opacity stays intact.
    assert delta.max(initial=0) <= 1
