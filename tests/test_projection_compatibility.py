"""Retained viewport images must not change exports or saved source data."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, CurvesModifier, ImageObject, OutlineModifier,
    ParameterMaskBinding, RasterObject, ToneMask,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


def pixels(image):
    raw = np.frombuffer(image.constBits(), np.uint8).reshape(
        image.height(), image.bytesPerLine(),
    )
    return raw[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


def export(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height,
                   QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return pixels(image)


@pytest.fixture
def document(qapp, monkeypatch):
    # Far content lies beyond both the viewport and its 1,024px capture block.
    chapter = ChapterDocument(height=2304, background="#FFFFFFFF")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 2304))
    layer = chapter.add_layer(page.layer_id, "Art", BoundGeometry.rectangle(0, 0, 1080, 2304))
    for item in (page, layer):
        item.fill_color, item.border_width = None, 0
    raster = chapter.add_object(layer.layer_id, RasterObject(
        interaction_rect=(0, 0, 384, 2304),
    ))
    tiles = TileStore()
    for point in (QPointF(80, 100), QPointF(100, 1900)):
        tiles.paint_dab(raster.object_id, point, 32, QColor("#FFBA4371"))
    outline = OutlineModifier(thickness=5, antialiasing=False)
    chapter.add_modifier(outline, [("object", raster.object_id)])
    mask = ToneMask(name="Saved parameter mask", saved=True)
    chapter.masks[mask.mask_id] = mask
    for point in (QPointF(80, 100), QPointF(100, 1900)):
        tiles.paint_dab(mask.mask_id, point, 48, QColor("white"))
    curves = CurvesModifier(
        curves={"rgb:master": [(0, 0), (.5, .75), (1, 1)]},
        parameter_masks={"intensity": ParameterMaskBinding(mask.mask_id, 25, 100)},
    )
    chapter.add_modifier(curves, [("layer", layer.layer_id)])
    image_obj = chapter.add_object(layer.layer_id, ImageObject(
        x=240, y=1880, pixel_width=48, pixel_height=48,
        source_filename="original.png", source_mime_type="image/png",
    ))
    source = QImage(48, 48, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("#FF2C8BC0"))
    source.setPixelColor(7, 9, QColor("#FF23CF71"))
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.WriteOnly)
    assert source.save(buffer, "PNG")
    buffer.close()
    images = ImageStore()
    images.put(image_obj.object_id, image_obj.source_filename, bytes(payload), "image/png")
    canvases = []

    def make_canvas(value=chapter, tile_store=tiles, image_store=images, *, projection=True):
        canvas = CanvasWidget(EditorSettings(
            canvas_renderer="raster", grid_overlay_visible=False,
            snap_to_grid=False, predictive_ink=False,
        ))
        canvas.setMinimumSize(1, 1)
        canvas.resize(384, 256)
        canvas.set_document(value, tile_store, image_store)
        canvas.center_x, canvas.center_y, canvas.scale = 192., 128., 1.
        canvas._document_projection_enabled = projection
        canvases.append(canvas)
        return canvas

    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _canvas: None)
    yield SimpleNamespace(
        chapter=chapter, tiles=tiles, images=images, raster=raster,
        mask=mask, image=image_obj, make_canvas=make_canvas,
    )
    for canvas in canvases:
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.close()
        canvas.deleteLater()


def warm_view(canvas):
    canvas._ensure_scene_cache()
    assert canvas._document_projection.tiles
    assert all(tile.valid for tile in canvas._document_projection.tiles.values())
    assert not canvas._effect_jobs.running and not canvas._effect_jobs.pending
    assert not canvas._interactive_render
    assert not canvas._effect_region_requests
    assert not canvas._projection_exact


def edit_near_and_far(document, canvas):
    for point in (QPointF(115, 120), QPointF(135, 1920)):
        document.tiles.paint_dab(document.raster.object_id, point, 20, QColor("#FF283AE1"))
        canvas._document_visual_changed(QRectF(point.x() - 24, point.y() - 24, 48, 48))


@pytest.mark.parametrize("state", ["warm", "dirty_pending", "dirty_rebuilt"])
def test_full_export_matches_uncached_render_after_projection_use(document, state):
    canvas = document.make_canvas()
    warm_view(canvas)
    if state != "warm":
        edit_near_and_far(document, canvas)
        assert any(not tile.valid for tile in canvas._document_projection.tiles.values())
        if state == "dirty_rebuilt":
            warm_view(canvas)

    # A fresh renderer has never seen a cropped viewport or retained projection.
    reference = document.make_canvas(projection=False)
    expected = export(reference)
    assert np.any(expected[1880:1950, 60:170, :3] != 255)
    assert np.any(expected[1880:1928, 240:288, :3] != 255)
    np.testing.assert_array_equal(export(canvas), expected)

    # Camera state and retained low-resolution display data cannot leak to export.
    canvas.center_y, canvas.rotation, canvas.scale = 180., 17., .75
    if state != "dirty_pending":
        warm_view(canvas)
    np.testing.assert_array_equal(export(canvas), expected)


@pytest.mark.parametrize("autosave", [False, True])
def test_projection_use_preserves_save_load_source_data_and_full_export(document, tmp_path, autosave):
    canvas = document.make_canvas()
    warm_view(canvas)
    edit_near_and_far(document, canvas)
    # Save while a visible cached block is invalid and the far edit is offscreen.
    assert any(not tile.valid for tile in canvas._document_projection.tiles.values())
    expected_model = document.chapter.to_dict()
    expected_export = export(document.make_canvas(projection=False))
    expected_tiles = {
        object_id: {key: image.copy() for key, image in document.tiles.object_tiles(object_id).items()}
        for object_id in (document.raster.object_id, document.mask.mask_id)
    }
    expected_source = document.images.source(document.image.object_id)
    repository = SeriesRepository(tmp_path / "Series")
    repository.create("Projection compatibility")
    # Complete saves may not depend on whether display work touched dirty flags.
    document.tiles.dirty.clear()
    repository.save_chapter(document.chapter, document.tiles, document.images, autosave=autosave)
    loaded, saved_tiles, saved_images = repository.load_chapter(
        document.chapter.chapter_id, recover=autosave, include_images=True,
    )

    assert loaded.to_dict() == expected_model
    assert document.chapter.to_dict() == expected_model
    for object_id, expected in expected_tiles.items():
        actual = saved_tiles.object_tiles(object_id)
        assert actual.keys() == expected.keys()
        for key in expected:
            np.testing.assert_array_equal(pixels(actual[key]), pixels(expected[key]))
    assert saved_images.source(document.image.object_id) == expected_source
    reloaded = document.make_canvas(loaded, saved_tiles, saved_images)
    np.testing.assert_array_equal(export(reloaded), expected_export)
    warm_view(reloaded)
    np.testing.assert_array_equal(export(reloaded), expected_export)
