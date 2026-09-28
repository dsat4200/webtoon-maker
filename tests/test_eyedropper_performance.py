"""Exact local sampling must keep pixels while excluding unrelated rendering."""
import math

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HalftoneModifier, ImageObject,
    MirrorModifier, OutlineModifier, RasterObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.eyedropper_sampling import EyedropperSampler


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=500, height=4000, background="#40000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 500, 4000))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False,
                                         grid_overlay_visible=True))
    canvas.resize(500, 500)
    canvas.set_document(chapter, TileStore())
    yield canvas, chapter, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def unculled_color(canvas, point):
    """The previous exact 3x3 compositor, retained as an independent reference."""
    x, y = math.floor(point.x()), math.floor(point.y())
    image = QImage(3, 3, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(canvas.chapter.background))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setTransform(QTransform.fromTranslate(1 - x, 1 - y))
    previous = canvas._interactive_render
    canvas._interactive_render = False
    try:
        for page_id in reversed(canvas.chapter.root_page_ids):
            canvas._render_layer(painter, canvas.chapter.layers[page_id], 1.0,
                                 QRectF(x - 1, y - 1, 3, 3))
    finally:
        canvas._interactive_render = previous
        painter.end()
    return image.pixelColor(1, 1).name(QColor.HexArgb).upper()


def image_object(canvas, chapter, parent, x, y, color="#A0EF2345"):
    obj = chapter.add_object(parent.layer_id, ImageObject(x=x, y=y, pixel_width=40, pixel_height=40))
    image = QImage(40, 40, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    canvas.images.put_decoded(obj.object_id, "sample.png", b"", image)
    return obj


def test_offscreen_effects_are_not_rendered_for_one_sample(scene, monkeypatch):
    canvas, chapter, page = scene
    foreground = image_object(canvas, chapter, page, 30, 30)
    expected = unculled_color(canvas, QPointF(50, 50))
    for index in range(30):
        far = image_object(canvas, chapter, page, 30, 500 + index * 100)
        chapter.add_modifier(HalftoneModifier(), [("object", far.object_id)])
    calls = []
    original = canvas._render_object_content
    monkeypatch.setattr(canvas, "_render_object_content", lambda painter, obj, bounds:
        (calls.append(obj.object_id), original(painter, obj, bounds))[1])
    monkeypatch.setattr(canvas, "_render_modified_object", lambda *_a, **_k:
        pytest.fail("An offscreen halftone image reached exact effect rendering"))
    assert canvas.sample_composited_color(QPointF(50, 50)) == expected
    assert calls == [foreground.object_id]


@pytest.mark.parametrize("modifier,point", [
    (BlurModifier(strength=5), QPointF(77.5, 105.5)),
    (OutlineModifier(thickness=12), QPointF(77.5, 105.5)),
    (MirrorModifier(axis_start=(80, 0), axis_end=(80, 400)), QPointF(60.5, 105.5)),
    (HalftoneModifier(), QPointF(100.5, 105.5)),
])
def test_exact_effect_color_matches_unculled_pixel_and_cached_tile(scene, modifier, point, monkeypatch):
    canvas, chapter, page = scene
    obj = image_object(canvas, chapter, page, 90, 90)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    expected = unculled_color(canvas, point)
    for scale, rotation in ((0.2, 30), (1.0, 90), (3.0, -17)):
        canvas.scale, canvas.rotation = scale, rotation
        assert canvas.sample_composited_color(point) == expected
        assert EyedropperSampler(canvas).sample(point) == expected
    canvas._document_projection_enabled = True
    canvas.scale, canvas.rotation = 1., 0.
    canvas.center_x, canvas.center_y = 250., 200.
    canvas._projection_phase_batch((None,))
    assert canvas._projection_completed_view is not None
    with monkeypatch.context() as patch:
        import comic_editor.ui.eyedropper_sampling as sampling
        patch.setattr(sampling, "render_sample_region", lambda *_a, **_k:
                      pytest.fail("A finished effect pixel was rendered again"))
        assert EyedropperSampler(canvas).sample(point) == expected


def test_sampling_ignores_grid_selection_and_selected_mask_only_content(scene, monkeypatch):
    canvas, chapter, page = scene
    color_layer = chapter.add_layer(page.layer_id, "Color", BoundGeometry.rectangle(20, 20, 180, 180))
    color_layer.fill_color, color_layer.border_width = "#80FF5500", 0
    mask_layer = chapter.add_layer(page.layer_id, "Mask", BoundGeometry.rectangle(20, 20, 180, 180))
    mask_layer.fill_color, mask_layer.mask_only = "#FF0000FF", True
    canvas.set_selection("layer", mask_layer.layer_id)
    expected = unculled_color(canvas, QPointF(80, 80))
    canvas._interactive_render = True
    monkeypatch.setattr(canvas._effect_jobs, "request", lambda *_a, **_k:
        pytest.fail("Exact sampling must never request an asynchronous draft"))
    try:
        assert canvas.sample_composited_color(QPointF(80, 80)) == expected
        assert canvas._interactive_render is True
        assert canvas._render_bounds.exact_sampling is False
    finally:
        canvas._interactive_render = False


def test_gesture_reuses_bounded_tiles_and_invalidates_on_edit_and_solo(scene, monkeypatch):
    canvas, chapter, page = scene
    red = chapter.add_layer(page.layer_id, "Red", BoundGeometry.rectangle(0, 0, 400, 300))
    red.fill_color, red.border_width = "#FFFF0000", 0
    blue = chapter.add_layer(page.layer_id, "Blue", BoundGeometry.rectangle(0, 0, 400, 300))
    blue.fill_color, blue.border_width = "#FF0000FF", 0
    canvas.set_tool(ToolKind.EYEDROPPER)
    canvas._sample_eyedropper(QPointF(40, 40))
    canvas._eyedropper_sampling = True
    sampler = canvas._eyedropper_sampler
    first = canvas._eyedropper_last_color
    import comic_editor.ui.eyedropper_sampling as sampling
    original = sampling.render_sample_region
    calls = []
    monkeypatch.setattr(sampling, "render_sample_region", lambda *a, **k:
        (calls.append(a[1:]), original(*a, **k))[1])
    signals = []
    canvas.colorSampled.connect(signals.append)
    for offset in range(15):
        assert canvas._sample_eyedropper(QPointF(40 + offset, 40))
    assert calls == []
    assert signals == []  # Repeated pixels do not churn color-panel controls.
    for index in range(20):
        sampler.sample(QPointF(40, 100 + index * 100))
    assert len(sampler.tiles) == sampler.max_tiles
    red.fill_color = blue.fill_color = "#FF00FF00"
    canvas.documentChanged.emit(QRectF(0, 0, 400, 300))
    assert sampler.sample(QPointF(40, 40)) == "#FF00FF00"
    red.fill_color = "#FFFF0000"
    canvas.set_solo_entities({("layer", red.layer_id)})
    assert sampler.sample(QPointF(40, 40)) == "#FFFF0000"
    assert first != "#FF00FF00"
    canvas._tool_release()


def test_gesture_reads_finished_composite_without_rendering_layers(scene, monkeypatch):
    canvas, chapter, page = scene
    back = chapter.add_layer(page.layer_id, "Back", BoundGeometry.rectangle(0, 0, 500, 400))
    back.fill_color, back.border_width = "#FF224466", 0
    front = chapter.add_layer(page.layer_id, "Front", BoundGeometry.rectangle(0, 0, 500, 400))
    front.fill_color, front.border_width = "#8088CC22", 0
    point = QPointF(100, 100)
    expected = canvas.sample_composited_color(point)
    canvas._document_projection_enabled = True
    canvas.scale, canvas.center_x, canvas.center_y = 1., 250., 250.
    canvas._projection_phase_batch((None,))
    assert canvas._projection_completed_view is not None
    sampler = EyedropperSampler(canvas)
    with monkeypatch.context() as patch:
        import comic_editor.ui.eyedropper_sampling as sampling
        patch.setattr(sampling, "render_sample_region", lambda *_a, **_k:
                      pytest.fail("A finished composite was rendered again"))
        assert sampler.sample(point) == expected
        assert sampler.sample(QPointF(101, 100)) == expected
    front.fill_color = "#FFFF0000"
    canvas.documentChanged.emit(QRectF(0, 0, 500, 400))
    assert sampler.sample(point) == canvas.sample_composited_color(point)


def test_gesture_reads_finished_tile_while_other_tiles_are_pending(scene, monkeypatch):
    canvas, chapter, page = scene
    layer = chapter.add_layer(page.layer_id, "Color", BoundGeometry.rectangle(0, 0, 500, 400))
    layer.fill_color, layer.border_width = "#FF224466", 0
    canvas._document_projection_enabled = True
    canvas.scale, canvas.center_x, canvas.center_y = 1., 250., 250.
    canvas._projection_phase_batch((None,))
    finished = canvas._projection_completed_view
    assert finished is not None
    canvas._projection_progress_view = finished
    canvas._projection_completed_view = None
    monkeypatch.setattr("comic_editor.ui.eyedropper_sampling.render_sample_region",
                        lambda *_a, **_k: pytest.fail("A finished tile was rendered again"))
    assert EyedropperSampler(canvas).sample(QPointF(100, 100)) == "#FF224466"


def test_presented_composite_is_skipped_when_view_is_not_native_resolution(scene, monkeypatch):
    canvas, chapter, page = scene
    layer = chapter.add_layer(page.layer_id, "Color", BoundGeometry.rectangle(0, 0, 500, 400))
    layer.fill_color, layer.border_width = "#FF123456", 0
    canvas._document_projection_enabled = True
    canvas.scale, canvas.center_x, canvas.center_y = 2., 250., 250.
    canvas._projection_phase_batch((None,))
    assert canvas._projection_completed_view is not None
    import comic_editor.ui.eyedropper_sampling as sampling
    original = sampling.render_sample_region
    calls = []
    monkeypatch.setattr(sampling, "render_sample_region", lambda *a, **k:
                        (calls.append(True), original(*a, **k))[1])
    assert EyedropperSampler(canvas).sample(QPointF(250, 250)) == "#FF123456"
    assert calls


def test_selected_mask_only_projection_is_not_used_for_chapter_sampling(scene):
    canvas, chapter, page = scene
    visible = chapter.add_layer(page.layer_id, "Visible", BoundGeometry.rectangle(0, 0, 500, 400))
    visible.fill_color, visible.border_width = "#FFFF0000", 0
    mask = chapter.add_layer(page.layer_id, "Mask", BoundGeometry.rectangle(0, 0, 500, 400))
    mask.fill_color, mask.border_width, mask.mask_only = "#FF0000FF", 0, True
    canvas.set_selection("layer", mask.layer_id)
    canvas._document_projection_enabled = True
    canvas.scale, canvas.center_x, canvas.center_y = 1., 250., 250.
    canvas._projection_phase_batch((None,))
    assert canvas._projection_completed_view is not None
    assert EyedropperSampler(canvas).sample(QPointF(100, 100)) == "#FFFF0000"


def test_public_sampling_does_not_cache_unsignalled_pixel_changes(scene):
    canvas, chapter, page = scene
    raster = chapter.add_object(page.layer_id, RasterObject())
    point = QPointF(100, 100)
    canvas.tiles.paint_dab(raster.object_id, point, 20, QColor("red"))
    assert canvas.sample_composited_color(point) == "#FFFF0000"
    canvas.tiles.paint_dab(raster.object_id, point, 20, QColor("blue"))
    assert canvas.sample_composited_color(point) == "#FF0000FF"
    canvas.tiles.paint_dab(raster.object_id, QPointF(300, 300), 20, QColor("green"))
    assert canvas.sample_composited_color(QPointF(300, 300)) == "#FF008000"
    assert canvas.sample_composited_color(QPointF(-1, 0)) is None
    assert canvas.sample_composited_color(QPointF(float("nan"), 0)) is None


def test_culling_scope_is_restored_if_rendering_raises(scene, monkeypatch):
    canvas, _chapter, _page = scene
    monkeypatch.setattr(canvas, "_render_layer", lambda *_a:
        (_ for _ in ()).throw(ValueError("render failed")))
    with pytest.raises(ValueError, match="render failed"):
        canvas.sample_composited_color(QPointF(100, 100))
    assert not canvas._interactive_render
    assert not canvas._render_bounds.exact_sampling
    assert not canvas._effect_region_requests
    assert not canvas._projection_exact
