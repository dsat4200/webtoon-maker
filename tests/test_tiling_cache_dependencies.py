"""Tiled pixels depend on artwork and placement, not transient capture images."""
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, HueSaturationLightnessModifier, ParameterMaskBinding, RasterObject,
    TilingModifier, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import tiling_features


@pytest.fixture(params=["raster", "layer"])
def tiled(request, qapp):
    chapter = ChapterDocument(width=128, height=128, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 128, 128))
    page.fill_color, page.border_width = None, 0
    parent = chapter.add_layer(page.layer_id, "Parent", BoundGeometry.rectangle(4, 4, 112, 112))
    parent.fill_color, parent.border_width = None, 0
    layer = chapter.add_layer(parent.layer_id, "Pattern", BoundGeometry.rectangle(8, 8, 100, 100))
    layer.fill_color, layer.border_width = None, 0
    drawing = chapter.add_object(layer.layer_id, RasterObject())
    target = drawing if request.param == "raster" else layer
    kind = "object" if request.param == "raster" else "layer"
    modifier = TilingModifier(center=(20, 20), side=32)
    chapter.add_modifier(modifier, [(kind, getattr(target, "object_id", layer.layer_id))])
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(chapter, TileStore())
    canvas.tiles.paint_dab(drawing.object_id, QPointF(16, 16), 8, QColor("red"), square=True, antialias=False)
    canvas.tiles.paint_dab(drawing.object_id, QPointF(24, 24), 6, QColor("blue"), square=True, antialias=False)
    canvas._interactive_render = True
    yield canvas, target, drawing, modifier, parent
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def clear_scene_caches(canvas, *, outputs=True):
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    if outputs:
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0


@pytest.mark.parametrize("pressure", ["source-only", "source-and-output"])
def test_unchanged_tile_reuses_output_before_source_capture(tiled, monkeypatch, pressure):
    canvas, target, drawing, modifier, parent = tiled
    expected, bounds = canvas._tiling_stage(target)
    target.name = "Renamed pattern"
    modifier.name = "Renamed tile"
    modifier.expanded = not modifier.expanded
    unrelated = canvas.chapter.add_object(parent.parent_id, RasterObject())
    canvas.set_selection("object", unrelated.object_id)
    canvas._render_excluded_object_id = unrelated.object_id
    canvas.tiles.paint_dab(unrelated.object_id, QPointF(120, 120), 4, QColor("green"))
    clear_scene_caches(canvas, outputs=pressure == "source-and-output")

    def unexpected_capture(*args, **kwargs):
        pytest.fail("An unchanged tile recaptured or repeated pixels")

    monkeypatch.setattr(canvas, "_tiling_source", unexpected_capture)
    monkeypatch.setattr(canvas, "_tiling_raster_pixels", unexpected_capture)
    monkeypatch.setattr(tiling_features, "repeat_image", unexpected_capture)
    actual, actual_bounds = canvas._tiling_stage(target)
    assert actual == expected and actual_bounds == bounds


def test_previously_displayed_export_tile_is_retained_on_cache_hit(tiled, monkeypatch):
    canvas, target, _drawing, _modifier, _parent = tiled
    canvas._interactive_render = False
    expected, bounds = canvas._tiling_stage(target)
    assert not canvas._effect_jobs.retained
    canvas._interactive_render = True
    assert canvas._tiling_stage(target)[0] == expected
    clear_scene_caches(canvas)
    monkeypatch.setattr(tiling_features, "repeat_image", lambda *args, **kwargs:
        pytest.fail("A displayed exact tile was repeated after LRU eviction"))
    actual, actual_bounds = canvas._tiling_stage(target)
    assert actual == expected and actual_bounds == bounds


@pytest.mark.parametrize("dependency", ["pixels", "tile-geometry", "parent-transform", "mask"])
def test_tile_pixel_dependencies_invalidate_and_match_fresh_output(tiled, dependency):
    canvas, target, drawing, modifier, parent = tiled
    if dependency == "mask":
        mask = ToneMask()
        canvas.chapter.masks[mask.mask_id] = mask
        modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    expected, old_bounds = canvas._tiling_stage(target)
    if dependency == "pixels":
        canvas.tiles.paint_dab(drawing.object_id, QPointF(16, 16), 8, QColor("yellow"), square=True, antialias=False)
    elif dependency == "tile-geometry":
        modifier.side = 24
    elif dependency == "parent-transform":
        parent.translate_x = 6
    else:
        canvas.tiles.paint_dab(mask.mask_id, QPointF(64, 64), 140, QColor("white"), square=True, antialias=False)
    actual, bounds = canvas._tiling_stage(target)
    clear_scene_caches(canvas)
    canvas._effect_jobs.cancel()
    fresh, fresh_bounds = canvas._tiling_stage(target)
    assert actual == fresh and bounds == fresh_bounds
    assert actual != expected or bounds != old_bounds


def test_provisional_capture_never_becomes_exact_tiling_output(tiled, monkeypatch):
    canvas, target, _drawing, _modifier, _parent = tiled
    name = "_tiling_raster_pixels" if isinstance(target, RasterObject) else "_tiling_source"
    original = getattr(canvas, name)
    calls = []

    def provisional_capture(*args, **kwargs):
        result = original(*args, **kwargs)
        canvas._effect_provisional_revision = getattr(canvas, "_effect_provisional_revision", 0) + 1
        calls.append(True)
        return result

    monkeypatch.setattr(canvas, name, provisional_capture)
    for _ in range(2):
        canvas._tiling_stage(target)
        assert not any(key[0] == "tiling-output" for key in canvas._modifier_render_cache)
        assert not canvas._effect_jobs.retained
    assert len(calls) == 2


def test_excluding_tiled_descendant_invalidates_layer_output(tiled):
    canvas, target, drawing, _modifier, _parent = tiled
    if isinstance(target, RasterObject):
        target = canvas.chapter.layers[drawing.parent_layer_id]
        canvas.chapter.remove_modifier(_modifier.modifier_id)
        canvas.chapter.add_modifier(_modifier, [("layer", target.layer_id)])
    expected, _ = canvas._tiling_stage(target)
    canvas._render_excluded_object_id = drawing.object_id
    actual, bounds = canvas._tiling_stage(target)
    clear_scene_caches(canvas)
    canvas._effect_jobs.cancel()
    fresh, fresh_bounds = canvas._tiling_stage(target)
    assert actual == fresh and bounds == fresh_bounds
    assert actual != expected


def test_unrelated_exclusion_reuses_modifiers_after_tiling(tiled, monkeypatch):
    from comic_editor.ui import effect_pipeline
    canvas, target, _drawing, _modifier, parent = tiled
    kind = "object" if isinstance(target, RasterObject) else "layer"
    identifier = target.object_id if kind == "object" else target.layer_id
    canvas.chapter.add_modifier(HueSaturationLightnessModifier(hue=40), [(kind, identifier)])
    unrelated = canvas.chapter.add_object(parent.parent_id, RasterObject())

    def render():
        image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(image)
        return image

    expected = render()
    assert not canvas._effect_jobs.submitted
    clear_scene_caches(canvas)
    canvas._render_excluded_object_id = unrelated.object_id
    monkeypatch.setattr(effect_pipeline, "apply_modifier_stack", lambda *args, **kwargs:
        pytest.fail("Tiled modifier recomputed for an excluded unrelated drawing"))
    assert render() == expected


@pytest.mark.parametrize("downstream", [False, True], ids=["tiling", "tiling-hsl"])
def test_dirty_tile_repaint_keeps_the_full_viewport_effect_window(tiled, monkeypatch, downstream):
    from comic_editor.ui import effect_pipeline
    canvas, target, _drawing, _modifier, _parent = tiled
    if downstream:
        kind = "object" if isinstance(target, RasterObject) else "layer"
        identifier = target.object_id if kind == "object" else target.layer_id
        canvas.chapter.add_modifier(HueSaturationLightnessModifier(hue=40), [(kind, identifier)])
    viewport = QRectF(0, 0, 128, 128)
    dirty = QRectF(20, 20, 30, 25)
    canvas._effect_viewport_world = viewport

    def render(region):
        image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setClipRect(region)
        try:
            assert canvas._render_tiled_target(painter, target, 1., region)
        finally:
            painter.end()
        return image

    expected = render(viewport)
    clear_scene_caches(canvas)

    def unexpected_work(*args, **kwargs):
        pytest.fail("A dirty repaint changed the unchanged tile's effect window")

    monkeypatch.setattr(canvas, "_tiling_source", unexpected_work)
    monkeypatch.setattr(canvas, "_tiling_raster_pixels", unexpected_work)
    monkeypatch.setattr(tiling_features, "repeat_image", unexpected_work)
    monkeypatch.setattr(effect_pipeline, "apply_modifier_stack", unexpected_work)
    actual = render(dirty)
    assert actual.copy(dirty.toRect()) == expected.copy(dirty.toRect())


@pytest.mark.parametrize("context", ["nested-source", "export", "mask", "navigator"])
def test_tiling_capture_windows_do_not_inherit_the_main_viewport(tiled, monkeypatch, context):
    canvas, target, _drawing, _modifier, _parent = tiled
    canvas._effect_viewport_world = QRectF(0, 0, 128, 128)
    required = QRectF(20, 20, 30, 25)
    if context == "nested-source":
        canvas._render_modifier_sources.add(("layer", "outer"))
    elif context == "export":
        canvas._interactive_render = False
    elif context == "mask":
        canvas._rendering_mask_contributor = 1
    else:
        canvas._effect_preview_channel = "navigator"
    image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    windows = []

    def capture(_target, requested):
        windows.append(QRectF(requested))
        return image, required

    monkeypatch.setattr(canvas, "_tiling_stage", capture)
    painter = QPainter(image)
    try:
        canvas._render_tiled_target(painter, target, 1., required)
    finally:
        painter.end()
    assert windows == [required]
