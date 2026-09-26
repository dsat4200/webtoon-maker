"""Inherited scene clips reject invisible effects while escaped artwork survives."""
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import (
    BoundGeometry, CageTransformModifier, ChapterDocument, DistortModifier, ImageObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    chapter = ChapterDocument(width=256, height=256, background="#00000000")
    page = chapter.add_page("Clipped parent", BoundGeometry.rectangle(0, 0, 128, 64))
    page.fill_color, page.border_width = None, 0
    result = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    result.set_document(chapter, TileStore())
    yield result, page
    result._effect_jobs.cancel()
    result._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    result.deleteLater()


def add_image(canvas, parent, x, y, color, **kwargs):
    obj = canvas.chapter.add_object(parent.layer_id,
        ImageObject(x=x, y=y, pixel_width=40, pixel_height=32, **kwargs))
    image = QImage(40, 32, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    canvas.images.put_decoded(obj.object_id, "sample.png", b"", image)
    return obj


def clear_effects(canvas):
    canvas._effect_jobs.cancel()
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._distort_preparation_cache = None


@pytest.mark.parametrize("owner", ["object", "layer"])
@pytest.mark.parametrize("transformed,promoted,interactive", [
    (False, False, False), (True, False, False), (True, True, True),
])
def test_clipped_effect_sibling_skips_work_but_escaped_child_matches_oracle(
    canvas, monkeypatch, owner, transformed, promoted, interactive,
):
    from comic_editor.ui import distort_rendering
    canvas, page = canvas
    parent = page
    if owner == "layer":
        parent = canvas.chapter.add_layer(page.layer_id, "Effect layer",
            BoundGeometry.rectangle(0, 0, 128, 64))
        parent.fill_color, parent.border_width = None, 0
    clipped = add_image(canvas, parent, 20, 100, "red")
    effect = (DistortModifier(modifier_type="distort_mesh_warp", frame=(20, 100, 40, 32),
        points=[(0, 0), (1, 0), (0, 1), (1.2, 1)],
        source_points=[(0, 0), (1, 0), (0, 1), (1, 1)],
        parameters={"rows": 2, "columns": 2}) if owner == "object"
        else CageTransformModifier(frame=(0, 0, 128, 160)))
    canvas.chapter.add_modifier(effect, [(owner, clipped.object_id if owner == "object" else parent.layer_id)])
    escaped = add_image(canvas, page, 20, 112, "green",
                        ignore_parent_mask=True, show_on_top=promoted)
    if transformed:
        page.transform_frame = (0, 0, 128, 64)
        page.transform_quad = [(230, 20), (230, 180), (182, 180), (182, 20)]
    world = canvas.layer_world_transform(page.layer_id)
    requested = world.mapRect(QRectF(0, 96, 128, 96))
    calls = []
    original = (distort_rendering.render_distort if owner == "object"
                else canvas._render_modified_layer)

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    if owner == "object":
        monkeypatch.setattr(distort_rendering, "render_distort", counted)
    else:
        monkeypatch.setattr(canvas, "_render_modified_layer", counted)

    def capture():
        image = QImage(128, 96, QImage.Format_ARGB32_Premultiplied)
        with canvas._mask_wand_reference_render():
            canvas._interactive_render = interactive
            assert canvas._render_bounds.usable()
            assert canvas._render_bounds.layer_visible(page, requested)
            canvas.render_preview(image, source_rect=requested)
        return image

    with monkeypatch.context() as patch:
        patch.setattr(canvas._render_bounds, "painter_clip_visible", lambda *_: True)
        expected = capture()
    if not interactive:
        assert calls, "The unculled oracle must exercise the clipped mesh kernel"
    clear_effects(canvas)
    calls.clear()
    actual = capture()
    assert calls == []
    assert actual == expected
    assert any(actual.pixelColor(x, y).green() > 0 and actual.pixelColor(x, y).alpha() > 0
               for x in range(actual.width()) for y in range(actual.height()))
    assert escaped.ignore_parent_mask and clipped.visible


@pytest.mark.parametrize("flag,value", [
    ("_render_modifier_sources", {("object", "source")}),
    ("_rendering_mask_contributor", 1),
    ("_rendering_halftone_source", True),
    ("_render_base_alpha", True),
    ("_render_cage_source", True),
    ("_rendering_compound_references", True),
    ("_tiling_capture_geometry", object()),
])
def test_independent_sources_ignore_inherited_clip_rejection(canvas, flag, value):
    canvas, page = canvas
    image = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    painter.setClipRect(QRectF(0, 0, 10, 10))
    try:
        with canvas._mask_wand_reference_render():
            requested = QRectF(20, 20, 10, 10)
            assert not canvas._render_bounds.painter_clip_visible(painter, requested)
            previous = getattr(canvas, flag)
            setattr(canvas, flag, value)
            try:
                assert canvas._render_bounds.painter_clip_visible(painter, requested)
                assert canvas._render_bounds.layer_clip_visible(painter, page, requested)
            finally:
                setattr(canvas, flag, previous)
    finally:
        painter.end()


def test_clip_guard_uses_device_pixels_and_keeps_projective_fallback(canvas):
    canvas, _ = canvas
    image = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        with canvas._mask_wand_reference_render():
            painter.scale(.1, .01)
            painter.setClipRect(QRectF(0, 0, 50, 9))
            assert canvas._render_bounds.painter_clip_visible(painter, QRectF(0, 10, 50, 2))
            painter.setClipping(False)
            painter.setTransform(QTransform(1., 0., .002, 0., 1., 0., 0., 0., 1.))
            painter.setClipRect(QRectF(0, 0, 10, 10))
            assert canvas._render_bounds.painter_clip_visible(painter, QRectF(20, 20, 10, 10))
    finally:
        painter.end()


@pytest.mark.parametrize("edge_offset", [-.5, .25, 1.5, 2.5])
def test_antialiased_clip_boundary_matches_unculled_pixels(canvas, monkeypatch, edge_offset):
    canvas, page = canvas
    page.translate_x, page.translate_y = .35, .45
    add_image(canvas, page, 20, 48, "red")
    add_image(canvas, page, 20, 112, "green", ignore_parent_mask=True)
    requested = QRectF(.35, 64.45 + edge_offset, 128, 4)

    def capture():
        result = QImage(128, 4, QImage.Format_ARGB32_Premultiplied)
        with canvas._mask_wand_reference_render():
            canvas.render_preview(result, source_rect=requested)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(canvas._render_bounds, "painter_clip_visible", lambda *_: True)
        expected = capture()
    assert capture() == expected


def test_descendant_clip_uses_live_parent_transform_after_cached_page_move(canvas):
    canvas, page = canvas
    child = canvas.chapter.add_layer(page.layer_id, "Nested artwork")
    bounds = canvas._render_bounds
    canvas._interactive_render = True
    bounds.prepare()
    original = bounds._transform(page.layer_id)
    assert original.isIdentity()
    canvas._geometry_transform_target = ("layer_group", page.layer_id)
    canvas._transform_start_quad = [(0., 0.), (128., 0.), (128., 64.), (0., 64.)]
    canvas._transform_preview_quad = [(x + 80, y + 120)
                                      for x, y in canvas._transform_start_quad]
    bounds.prepare()
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        painter.setTransform(canvas.layer_world_transform(page.layer_id))
        painter.setClipRect(QRectF(0, 0, 128, 64))
        # This final output strip intersects the translated parent mask.
        # Mapping it with the old parent matrix shifts it a second time and
        # wrongly removes the whole nested branch from this capture block.
        assert bounds.layer_clip_visible(painter, child, QRectF(132, 140, 64, 24))
        assert bounds._transform(page.layer_id) == canvas.layer_world_transform(page.layer_id)
        assert bounds.transforms[page.layer_id] == original
    finally:
        painter.end()
    canvas._clear_transform_preview()
    bounds.prepare()
    assert bounds._transform(page.layer_id) == original
