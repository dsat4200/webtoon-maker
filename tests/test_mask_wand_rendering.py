"""Wand reference acceleration preserves native image/mesh/masked-HSL pixels."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, DistortModifier, HueSaturationLightnessModifier,
    ImageObject, ParameterMaskBinding, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import distort_rendering
from test_distort_integration import png_bytes


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False,
                                          mask_wand_tolerance=0))
    chapter = ChapterDocument(width=512, height=256, document_kind="image")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 512, 256))
    page.fill_color, page.border_width = None, 0
    obj = chapter.add_object(page.layer_id, ImageObject(pixel_width=512, pixel_height=256))
    image = QImage(512, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#707070"))
    painter = QPainter(image)
    painter.fillRect(20, 30, 200, 190, QColor("#e04030"))
    painter.fillRect(300, 30, 180, 190, QColor("#e04030"))
    painter.fillRect(235, 20, 30, 210, QColor(20, 60, 210, 170))
    painter.end()
    images = ImageStore()
    images.put(obj.object_id, "embedded.png", png_bytes(image), "image/png")
    mesh = DistortModifier(modifier_type="distort_mesh_warp", frame=(0, 0, 512, 256),
                           parameters={"rows": 4, "columns": 4, "smoothness": 35})
    mesh.validate()
    mesh.points[5] = (.38, .29)
    chapter.add_modifier(mesh, [("object", obj.object_id)])
    mask = ToneMask(saved=True)
    chapter.masks[mask.mask_id] = mask
    hsl = HueSaturationLightnessModifier(parameter_masks={
        "hue": ParameterMaskBinding(mask.mask_id, 0, 120)})
    chapter.add_modifier(hsl, [("object", obj.object_id)])
    tiles = TileStore()
    tiles.paint_dab(mask.mask_id, QPointF(195, 100), 12, QColor("white"),
                    square=True, antialias=False)
    mask.touch()
    canvas.set_document(chapter, tiles, images)
    canvas.set_selection("object", obj.object_id, activate_default_tool=False)
    canvas.set_tone_mask_mode(mask.mask_id)
    yield SimpleNamespace(canvas=canvas, obj=obj, image=image, mesh=mesh, mask=mask)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


@contextmanager
def legacy_reference(canvas, entities=None):
    names = ("_interactive_render", "_projection_exact", "_projection_defer_effects", "_exact_reference_render")
    previous = tuple(getattr(canvas, name, False) for name in names)
    old_exact = canvas._render_bounds.exact_sampling
    old_entities = getattr(canvas, "_mask_wand_sample_entities", None)
    for name in names:
        setattr(canvas, name, False)
    canvas._render_bounds.exact_sampling = False
    canvas._mask_wand_sample_entities = entities
    try:
        yield
    finally:
        for name, value in zip(names, previous):
            setattr(canvas, name, value)
        canvas._render_bounds.exact_sampling = old_exact
        canvas._mask_wand_sample_entities = old_entities


def clear_render_caches(canvas):
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._effect_jobs.cancel()
    cache = getattr(canvas, "_distort_preparation_cache", None)
    if cache is not None:
        cache.clear()


def reference_tiles(canvas):
    result = []
    for x in (0, 256):
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(image, source_rect=QRectF(x, 0, 256, 256))
        result.append(image)
    return result


def mask_snapshot(scene):
    return {key: QImage(value) for key, value in
            scene.canvas.tiles.object_tiles(scene.mask.mask_id).items()}


def wand_snapshot(scene):
    scene.canvas._mask_wand_press(QPointF(96, 120), Qt.NoModifier)
    result = mask_snapshot(scene)
    scene.canvas.command_stack.undo()
    return result


def test_native_reference_pixels_and_wand_selection_equal_old_renderer(scene, monkeypatch):
    canvas = scene.canvas
    with legacy_reference(canvas):
        expected = reference_tiles(canvas)
    clear_render_caches(canvas)
    with canvas._mask_wand_reference_render():
        assert reference_tiles(canvas) == expected
    actual = wand_snapshot(scene)
    clear_render_caches(canvas)
    with monkeypatch.context() as patch:
        patch.setattr(canvas, "_mask_wand_reference_render", lambda entities=None: legacy_reference(canvas, entities))
        assert wand_snapshot(scene) == actual
    assert not canvas._effect_jobs.running and not canvas._effect_jobs.pending


def test_adjacent_reference_tiles_reuse_source_conversion(scene, monkeypatch):
    calls = []
    original = distort_rendering._rgba
    def converted(image):
        calls.append(image.cacheKey())
        return original(image)
    monkeypatch.setattr(distort_rendering, "_rgba", converted)
    with scene.canvas._mask_wand_reference_render():
        reference_tiles(scene.canvas)
    cache = scene.canvas._distort_preparation_cache
    assert len(calls) == 1
    assert cache.hits >= 1 and cache.bytes <= cache.budget


@pytest.mark.parametrize("prepare_fails", [False, True])
def test_reference_context_restores_state_even_when_capture_fails(scene, monkeypatch, prepare_fails):
    canvas = scene.canvas
    canvas._interactive_render = True
    canvas._projection_exact = False
    canvas._projection_defer_effects = True
    canvas._exact_reference_render = False
    canvas._render_bounds.exact_sampling = False
    canvas._render_bounds.margin = 17
    previous_entities = frozenset({("object", "previous")})
    canvas._mask_wand_sample_entities = previous_entities
    def failed():
        raise RuntimeError("capture failed")
    if prepare_fails:
        monkeypatch.setattr(canvas._render_bounds, "prepare", failed)
    with pytest.raises(RuntimeError, match="capture failed"):
        with canvas._mask_wand_reference_render(frozenset({("object", scene.obj.object_id)})):
            assert not canvas._interactive_render and canvas._projection_exact
            assert not canvas._projection_defer_effects
            assert canvas._exact_reference_render
            assert canvas._render_bounds.exact_sampling
            failed()
    assert canvas._interactive_render and not canvas._projection_exact
    assert canvas._projection_defer_effects
    assert not canvas._exact_reference_render
    assert not canvas._render_bounds.exact_sampling and canvas._render_bounds.margin == 17
    assert canvas._mask_wand_sample_entities == previous_entities


@pytest.mark.parametrize("edit", ["source", "mask", "mesh"])
def test_reference_and_selection_never_reuse_stale_dependencies(scene, monkeypatch, edit):
    canvas = scene.canvas
    with canvas._mask_wand_reference_render():
        before = reference_tiles(canvas)
    wand_snapshot(scene)
    if edit == "source":
        painter = QPainter(scene.image)
        painter.fillRect(110, 25, 15, 200, QColor("blue"))
        painter.end()
        canvas.images.put(scene.obj.object_id, "edited.png", png_bytes(scene.image), "image/png")
    elif edit == "mask":
        canvas.tiles.paint_dab(scene.mask.mask_id, QPointF(135, 120), 65,
                              QColor("white"), square=True, antialias=False)
        scene.mask.touch()
        canvas._mask_tiles_changed()
    else:
        scene.mesh.points = [(x + .1, y) for x, y in scene.mesh.points]
        scene.mesh.validate()
    with canvas._mask_wand_reference_render():
        actual = reference_tiles(canvas)
    assert actual != before
    selected = wand_snapshot(scene)
    clear_render_caches(canvas)
    with legacy_reference(canvas):
        assert reference_tiles(canvas) == actual
    with monkeypatch.context() as patch:
        patch.setattr(canvas, "_mask_wand_reference_render", lambda entities=None: legacy_reference(canvas, entities))
        assert wand_snapshot(scene) == selected
