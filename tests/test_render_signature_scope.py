"""Capture reuse must preserve dependency changes and branch render context."""
from contextlib import nullcontext

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HueSaturationLightnessModifier,
    ImageObject, ParameterMaskBinding, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.service import RenderRequest
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.render_signatures import signature_scope
from comic_editor.ui.show_on_top_features import TopPlan


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=384, height=256, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 384, 256))
    page.fill_color, page.border_width = None, 0
    layer = chapter.add_layer(page.layer_id, "Panel", BoundGeometry.rectangle(0, 0, 384, 256))
    layer.fill_color, layer.border_width = None, 0
    obj = chapter.add_object(layer.layer_id, ImageObject(x=30, y=30, pixel_width=256, pixel_height=192))
    image = QImage(256, 192, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(80, 140, 200, 220))
    images = ImageStore()
    images.put_decoded(obj.object_id, "image.png", b"", image)
    effect = HueSaturationLightnessModifier(hue=19)
    chapter.add_modifier(effect, [("object", obj.object_id)])
    blur = BlurModifier(strength=2)
    chapter.add_modifier(blur, [("layer", layer.layer_id)])
    mask = ToneMask(name="Mask")
    chapter.masks[mask.mask_id] = mask
    blur.parameter_masks["strength"] = ParameterMaskBinding(mask.mask_id, 0, 3)
    tiles = TileStore()
    tiles.paint_dab(mask.mask_id, QPointF(140, 120), 180, QColor("white"))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, tiles, images)
    yield canvas, layer, obj, effect, mask
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def request(canvas):
    document = canvas._render_document_state()
    return document, RenderRequest((0., 0., 384., 256.), 1., (384, 256), ("scope",), document.revision)


def test_repeated_subtree_dependencies_are_built_once_per_capture(scene, monkeypatch):
    canvas, layer, obj, _effect, _mask = scene
    original = obj.to_dict
    calls = []
    monkeypatch.setattr(obj, "to_dict", lambda: (calls.append(True), original())[1])
    document, value = request(canvas)
    results = []

    def paint(_painter, _visible, **_kwargs):
        results.extend(canvas._modifier_layer_signature(layer.layer_id) for _ in range(8))

    monkeypatch.setattr(canvas, "_render_scene_layers", paint)
    assert canvas._render_service.render_region(document, value).exact
    assert len(calls) == 1
    assert all(value == results[0] for value in results)
    assert not hasattr(canvas, "_render_signature_memo")
    canvas._render_service.render_region(document, value)
    assert len(calls) == 2


@pytest.mark.parametrize("change", ["object", "effect", "paint"])
def test_unsignaled_dependency_edits_are_visible_in_next_capture_and_direct_calls(scene, change):
    canvas, layer, obj, effect, mask = scene
    with signature_scope(canvas):
        before = canvas._modifier_layer_signature(layer.layer_id)
    if change == "object":
        obj.opacity = .5
    elif change == "effect":
        effect.hue = 40
    else:
        canvas.tiles.paint_dab(mask.mask_id, QPointF(300, 180), 30, QColor("white"))
    direct = canvas._modifier_layer_signature(layer.layer_id)
    assert direct != before
    with signature_scope(canvas):
        assert canvas._modifier_layer_signature(layer.layer_id) == direct


def test_nested_capture_restores_scope_and_rebuilds_outer_unsignaled_dependencies(scene):
    canvas, layer, obj, _effect, _mask = scene
    document, value = request(canvas)
    backend = canvas._render_service.backend
    with backend.capture(document, value, document.bounds):
        outer = canvas._render_signature_memo
        before = canvas._modifier_layer_signature(layer.layer_id)
        with pytest.raises(RuntimeError, match="nested"):
            with backend.capture(document, value, document.bounds):
                assert canvas._render_signature_memo is not outer
                obj.opacity = .5
                raise RuntimeError("nested")
        assert canvas._render_signature_memo is outer
        assert canvas._modifier_layer_signature(layer.layer_id) != before
    assert not hasattr(canvas, "_render_signature_memo")


def test_capture_branches_and_reentrant_invalidation_do_not_reuse_old_signatures(scene):
    canvas, layer, obj, effect, _mask = scene
    with signature_scope(canvas):
        ordinary = canvas._modifier_layer_signature(layer.layer_id)
        canvas._rendering_halftone_source = True
        try:
            color_source = canvas._modifier_layer_signature(layer.layer_id)
            assert color_source != ordinary
        finally:
            canvas._rendering_halftone_source = False
        assert canvas._modifier_layer_signature(layer.layer_id) == ordinary
        effect.hue = 80
        canvas._render_service.invalidate(QRectF(0, 0, 384, 256))
        assert canvas._modifier_layer_signature(layer.layer_id) != ordinary


def test_solo_and_promoted_passes_keep_distinct_subtree_dependencies(scene):
    canvas, layer, obj, _effect, _mask = scene
    canvas._solo_entities = {("layer", layer.layer_id)}
    entries = frozenset({("object", obj.object_id)})
    canvas._active_top_plan = TopPlan(entries, entries, frozenset({layer.layer_id}))
    with signature_scope(canvas):
        canvas._show_on_top_phase = "base"
        ordinary = canvas._modifier_layer_signature(layer.layer_id)
        canvas._show_on_top_phase = "top"
        promoted = canvas._modifier_layer_signature(layer.layer_id)
        assert promoted != ordinary
        canvas._show_on_top_phase = "base"
        assert canvas._modifier_layer_signature(layer.layer_id) == ordinary
        with canvas.without_solo():
            assert canvas._modifier_layer_signature(layer.layer_id) != ordinary


def test_scoped_and_uncached_exact_pixels_match_after_dependency_edits(scene, monkeypatch):
    canvas, _layer, obj, effect, mask = scene
    for change in range(3):
        obj.x += 3
        effect.hue += 11
        canvas.tiles.paint_dab(mask.mask_id, QPointF(200 + change * 20, 120), 20, QColor("white"))
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        canvas._modifier_source_cache.clear()
        canvas._modifier_source_cache_bytes = 0
        document, value = request(canvas)
        scoped = canvas._render_service.render_region(document, value)
        with monkeypatch.context() as patch:
            patch.setattr("comic_editor.ui.scene_render_backend.signature_scope", lambda _canvas: nullcontext())
            reference = canvas._render_service.render_region(document, value)
        assert scoped.exact and reference.exact
        np.testing.assert_array_equal(np.frombuffer(scoped.image.constBits(), np.uint8),
                                      np.frombuffer(reference.image.constBits(), np.uint8))
