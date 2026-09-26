"""Temporary isolation preserves exact work without mixing subtree visibility."""
import pytest
from PySide6.QtCore import QPointF, QRectF, QSize
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, DistortModifier, ImageObject,
    ParameterMaskBinding, RasterObject, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=512, height=384, document_kind="asset", background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 512, 384))
    page.fill_color, page.border_width = None, 0
    group = chapter.add_layer(page.layer_id, "Group", BoundGeometry.rectangle(0, 0, 512, 384))
    group.fill_color, group.border_width = None, 0
    background = chapter.add_object(group.layer_id, ImageObject(x=20, y=20, pixel_width=300, pixel_height=220))
    foreground = chapter.add_object(group.layer_id, RasterObject())
    tiles = TileStore()
    tiles.paint_dab(foreground.object_id, QPointF(400, 180), 40, QColor("blue"))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, tiles)
    source = QImage(300, 220, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("red"))
    canvas.images.put_decoded(background.object_id, "background.png", b"", source)
    yield canvas, group, background, foreground
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def exact_frame(canvas):
    # Bypass composed projection reuse to prove that underlying exact effects
    # survive a visibility switch even when a fresh screen region is requested.
    image, exact = canvas._render_document_region(
        QRectF(0, 0, 512, 384), 1., QSize(512, 384), (0, 0, 0), exact=True)
    assert exact
    return image


@pytest.mark.parametrize("owner", ["object", "layer"])
def test_return_from_solo_reuses_expensive_exact_background(scene, monkeypatch, owner):
    from comic_editor.ui import distort_rendering
    canvas, group, background, foreground = scene
    effect = DistortModifier(modifier_type="distort_deform", frame=(0, 0, 512, 384))
    canvas.chapter.add_modifier(effect, [("object", background.object_id)])
    if owner == "layer":
        canvas.chapter.add_modifier(BlurModifier(strength=2), [("layer", group.layer_id)])
    calls = []
    original = distort_rendering.render_distort
    monkeypatch.setattr(distort_rendering, "render_distort", lambda *args, **kwargs:
                        (calls.append(args[2].modifier_id), original(*args, **kwargs))[1])
    normal = exact_frame(canvas)
    assert calls
    canvas.set_solo_entities({("object", foreground.object_id)})
    isolated = exact_frame(canvas)
    assert isolated.pixelColor(80, 80).alpha() == 0
    assert isolated.pixelColor(400, 180).blue() > 240
    assert isolated != normal
    completed = len(calls)
    canvas.set_solo_entities(set())
    assert exact_frame(canvas) == normal
    assert len(calls) == completed
    # The filtered subtree also remains reusable on a repeated hover.
    canvas.set_solo_entities({("object", foreground.object_id)})
    assert exact_frame(canvas) == isolated
    assert len(calls) == completed


def test_cached_solo_subtree_does_not_filter_mask_contributors_or_export(scene):
    canvas, group, background, foreground = scene
    effect = BlurModifier(strength=2)
    canvas.chapter.add_modifier(effect, [("layer", group.layer_id)])
    contributor = canvas.chapter.add_object(group.layer_id, RasterObject(mask_only=True))
    canvas.tiles.paint_dab(contributor.object_id, QPointF(400, 180), 50, QColor("black"))
    mask = ToneMask(contributors=[("object", contributor.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    foreground.opacity_mask = ParameterMaskBinding(mask.mask_id, 0., 1.)
    normal = exact_frame(canvas)
    canvas.set_solo_entities({("object", foreground.object_id)})
    isolated = exact_frame(canvas)
    assert isolated.pixelColor(400, 180).blue() > 240
    assert isolated.pixelColor(80, 80).alpha() == 0
    exported = canvas.render_export_image()
    assert exported.pixelColor(80, 80).red() > 240
    assert exact_frame(canvas) == isolated
    canvas.set_solo_entities(set())
    assert exact_frame(canvas) == normal
