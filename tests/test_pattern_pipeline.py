"""Pattern integration: stable coordinates, GPU reuse, masks and raster baking."""
from dataclasses import replace
from threading import Event, get_ident

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import (BoundGeometry, ChapterDocument,
    HalftoneModifier, PixelateModifier, ParameterMaskBinding, RasterObject)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.baking import apply_raster_modifiers
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.modifier_rendering import apply_pattern_modifier, _qimage_premultiplied


@pytest.fixture
def pattern_scene(qapp):
    chapter = ChapterDocument(width=240, height=180, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 240, 180))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 240, 180)))
    for x in range(35, 200, 12):
        canvas.tiles.paint_dab(obj.object_id, QPointF(x, 80), 32,
            QColor(20 + x, 70, 190, 210), square=True, antialias=False)
    canvas.set_selection("object", obj.object_id)
    yield canvas, chapter, obj
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def sample_image():
    image = QImage(120, 90, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#ffaaaaaa"))
    painter = QPainter(image)
    painter.fillRect(4, 13, 65, 29, QColor("#ff274876"))
    painter.fillRect(77, 52, 30, 33, QColor("#80d06413"))
    painter.end()
    return image


@pytest.mark.parametrize("modifier", [HalftoneModifier(blur=2, spacing=35),
    HalftoneModifier(grid_type="ring", blur=0), PixelateModifier(pixel_size=11, blur=3)])
def test_viewport_crop_does_not_move_grid_or_change_blur(pattern_scene, modifier):
    canvas, _, _ = pattern_scene
    image = sample_image()
    frame = QRectF(-20, 30, 120, 90)
    full, _ = render_stages(canvas, image, frame, [modifier], QTransform())
    clipped, frame2 = render_stages(canvas, image, frame, [modifier], QTransform(),
                                  required=QRectF(7, 46, 39, 43))
    expected = full.copy(27, 16, 39, 43)
    assert frame2 == QRectF(7, 46, 39, 43)
    assert clipped == expected


def test_exact_projection_uses_regional_halftone_pixels(pattern_scene, monkeypatch):
    canvas, _, _ = pattern_scene
    image = sample_image()
    frame = QRectF(-20, 30, 120, 90)
    modifier = HalftoneModifier(grid_type="square", dot_style="circle",
                                color_mode="two", base_resolution=1000,
                                spacing=15.4, blur=5, intensity=70)
    complete, _ = render_stages(canvas, image, frame, [modifier], QTransform())
    canvas._interactive_render = True
    canvas._effect_region_requests = True
    canvas._projection_exact = True
    import comic_editor.ui.effect_pipeline as pipeline
    import comic_editor.ui.modifier_rendering as rendering
    monkeypatch.setattr(pipeline, "REGIONAL_HALFTONE_MIN_PIXELS", 0)
    monkeypatch.setattr(rendering, "apply_pattern_modifier", lambda *_a, **_k:
                        pytest.fail("The entire pattern frame was evaluated"))
    requested = QRectF(7, 46, 39, 43)
    regional, placement = render_stages(canvas, image, frame, [modifier],
        QTransform(), required=requested, request_scope=("test", "halftone"))
    assert placement == requested
    assert regional == complete.copy(27, 16, 39, 43)


def test_padded_halftone_snapshots_admit_two_exact_workers_with_unchanged_budget(pattern_scene, monkeypatch):
    import comic_editor.ui.pattern_rendering as patterns
    from comic_editor.ui.async_projection import ProjectionPending
    canvas, _, _ = pattern_scene
    canvas._interactive_render = canvas._effect_region_requests = True
    canvas._projection_exact = canvas._projection_defer_effects = True
    image = QImage(1527, 6127, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(96, 43, 121, 173))
    frame = QRectF(0, 0, 1527, 6127)
    modifier = HalftoneModifier(grid_type="square", dot_style="circle", color_mode="two",
        base_resolution=1000, spacing=10, blur=5, size=1.65, rotation=-38.2, intensity=70)
    release, entered = Event(), [Event(), Event()]
    captured = []
    ordinary = patterns.render_halftone_snapshot
    gui = get_ident()
    def blocked(snapshot, effect, **kwargs):
        assert get_ident() != gui
        number = len(captured)
        captured.append((snapshot, effect))
        entered[number].set()
        assert release.wait(20)
        return ordinary(snapshot, effect, **kwargs)
    monkeypatch.setattr(patterns, "render_halftone_snapshot", blocked)
    requested = [QRectF(140, 1120, 1028, 1028), QRectF(140, 2148, 1028, 1028)]
    arguments = dict(source_key=("native-source",), request_scope=("test", "parallel-halftone"))
    try:
        for region in requested:
            with pytest.raises(ProjectionPending):
                render_stages(canvas, image, frame, [modifier], QTransform(), required=region, **arguments)
        assert all(event.wait(2) for event in entered)
        jobs = canvas._effect_jobs
        assert len(jobs.running_jobs) == 2 and not jobs.pending
        assert jobs.budget == 256 * 1024 * 1024
        assert 8 * image.sizeInBytes() > jobs.budget
        assert jobs.bytes_in_flight <= jobs.budget
        assert all(snapshot.frame_size == (1527, 6127)
                   and snapshot.image.sizeInBytes() < image.sizeInBytes() / 4
                   for snapshot, _effect in captured)
        modifier.intensity = 30
        assert all(effect.intensity == 70 for _snapshot, effect in captured)
    finally:
        release.set()
    for job in canvas._effect_jobs.running_jobs:
        job[3].result(timeout=20)
    canvas._effect_jobs.poll()
    # The mutable UI effect was detached; results retain exactly their captured
    # native sampling and scalar blend. They publish under their original keys.
    captured_effect = replace(modifier, intensity=70)
    for region, (snapshot, effect) in zip(requested, captured):
        result, placement = render_stages(canvas, image, frame, [captured_effect],
            QTransform(), required=region, **arguments)
        filtered = ordinary(snapshot, effect)
        original = snapshot.image.copy(*snapshot.output)
        from comic_editor.ui.modifier_rendering import _premultiplied_qimage
        expected = _premultiplied_qimage(_qimage_premultiplied(original) * .3
                                        + _qimage_premultiplied(filtered) * .7)
        assert placement == region and result == expected
    assert canvas._effect_jobs.completed == 2 and canvas._effect_jobs.submitted == 2


@pytest.mark.parametrize("guard", ["intensity_mask", "target_layer"])
def test_masked_and_target_halftones_keep_their_existing_complete_sampling_path(pattern_scene, monkeypatch, guard):
    import comic_editor.ui.effect_pipeline as pipeline
    import comic_editor.ui.pattern_rendering as patterns
    canvas, _, _ = pattern_scene
    canvas._interactive_render = canvas._effect_region_requests = canvas._projection_exact = True
    monkeypatch.setattr(pipeline, "REGIONAL_HALFTONE_MIN_PIXELS", 0)
    monkeypatch.setattr(patterns, "capture_halftone_region", lambda *_a, **_k:
        pytest.fail("A field-dependent pattern was detached as a scalar regional snapshot"))
    modifier = HalftoneModifier(base_resolution=1000, spacing=15.4, blur=5)
    if guard == "intensity_mask":
        modifier.parameter_masks["intensity"] = ParameterMaskBinding("missing", 0, 100)
    else:
        modifier.color_mode = "target_layer"
    image = sample_image()
    result, bounds = render_stages(canvas, image, QRectF(0, 0, 120, 90), [modifier],
        QTransform(), required=QRectF(7, 9, 39, 43), request_scope=("test", guard))
    assert not result.isNull() and bounds == QRectF(7, 9, 39, 43)


def test_document_reset_discards_an_old_exact_halftone_snapshot(pattern_scene, monkeypatch):
    import comic_editor.ui.effect_pipeline as pipeline
    import comic_editor.ui.pattern_rendering as patterns
    from comic_editor.ui.async_projection import ProjectionPending
    from comic_editor.ui.radial_blur import RadialRenderCancelled
    canvas, chapter, _ = pattern_scene
    canvas._interactive_render = canvas._effect_region_requests = True
    canvas._projection_exact = canvas._projection_defer_effects = True
    monkeypatch.setattr(pipeline, "REGIONAL_HALFTONE_MIN_PIXELS", 0)
    image, frame = sample_image(), QRectF(0, 0, 120, 90)
    modifier = HalftoneModifier(base_resolution=1000, spacing=15.4, blur=5)
    entered, release = Event(), Event()
    ordinary = patterns.render_halftone_snapshot
    def blocked(snapshot, effect, **kwargs):
        entered.set()
        assert release.wait(5)
        return ordinary(snapshot, effect, **kwargs)
    monkeypatch.setattr(patterns, "render_halftone_snapshot", blocked)
    try:
        with pytest.raises(ProjectionPending):
            render_stages(canvas, image, frame, [modifier], QTransform(),
                required=QRectF(7, 9, 39, 43), request_scope=("test", "old-snapshot"))
        assert entered.wait(2)
        job = canvas._effect_jobs.running
        canvas.set_document(chapter, canvas.tiles)
        assert job[2].is_set()
    finally:
        release.set()
    with pytest.raises(RadialRenderCancelled):
        job[3].result(timeout=5)
    canvas._effect_jobs.poll()
    assert canvas._effect_jobs.discarded == 1
    assert canvas._effect_jobs.completed == 0
    assert not canvas._effect_jobs.retained and not canvas._modifier_render_cache


def test_slider_edit_reuses_original_gpu_source(pattern_scene, monkeypatch):
    canvas, _, _ = pattern_scene
    class RecordingRenderer:
        def __init__(self):
            self.keys = []
        def render(self, image, modifier, **kwargs):
            self.keys.append(image.cacheKey())
            return image.copy()
    renderer = RecordingRenderer()
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _: renderer)
    image = sample_image()
    modifier = PixelateModifier()
    for size in (7, 9):
        modifier.pixel_size = size
        render_stages(canvas, image, QRectF(0, 0, 120, 90), [modifier], QTransform())
    assert renderer.keys == [image.cacheKey(), image.cacheKey()]


def test_intensity_mask_overrides_scalar_and_preserves_unaffected_pixels(qapp):
    image = sample_image()
    modifier = PixelateModifier(brightness=100, intensity=0)
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("mask", 0, 100)
    field = np.zeros((90, 120), np.float32)
    field[:, 60:] = 1
    output = apply_pattern_modifier(image, modifier, {(modifier.modifier_id, "intensity"): field})
    source, result = _qimage_premultiplied(image), _qimage_premultiplied(output)
    np.testing.assert_allclose(source[:, :60], result[:, :60], atol=1/255)
    assert np.abs(source[:, 60:] - result[:, 60:]).max() > .1


@pytest.mark.parametrize("modifier", [HalftoneModifier(blur=0, transparent_background=True),
    PixelateModifier(pixel_size=9, blur=1, saturation=-60)])
def test_transformed_raster_bake_and_undo_preserve_preview(pattern_scene, modifier):
    canvas, chapter, obj = pattern_scene
    obj.transform_frame = (0, 0, 240, 180)
    obj.transform_quad = [(10, 6), (210, 18), (222, 164), (18, 156)]
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    def preview():
        image = QImage(240, 180, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(image)
        return image
    before = preview()
    apply_raster_modifiers(canvas, modifier.modifier_id)
    after = preview()
    assert after == before
    assert canvas.chapter.objects[obj.object_id].modifier_source_frame is not None
    canvas.command_stack.undo()
    assert preview() == before
    canvas.command_stack.redo()
    assert preview() == after


def color_layer(chapter, parent_id=None, color="#ff0000"):
    layer = chapter.add_layer(parent_id or chapter.root_page_ids[0], "Colors",
                              BoundGeometry.rectangle(0, 0, 240, 180))
    layer.fill_color, layer.border_width = color, 0
    return layer


def test_target_color_capture_alignment_hidden_source_and_transform(pattern_scene):
    from comic_editor.ui.halftone_source import render_color_source
    canvas, chapter, _ = pattern_scene
    parent = color_layer(chapter, color=None)
    parent.translate_x, parent.translate_y = 13, 9
    layer = color_layer(chapter, parent.layer_id)
    layer.bound = BoundGeometry.rectangle(0, 0, 24, 18)
    layer.translate_x, layer.translate_y = 7, 11
    layer.visible = False
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=layer.layer_id)
    incoming = sample_image()
    mapping = QTransform.fromTranslate(10, 5)
    result = render_color_source(canvas, modifier, incoming, QRectF(0, 0, 120, 90), mapping)
    assert result.pixelColor(11, 16) == QColor("#ff0000")
    assert result.pixelColor(5, 10).alpha() == 0
    assert result.pixelColor(34, 34).alpha() == 0
    assert layer.visible is False
    assert not canvas._rendering_halftone_source


def test_target_texture_reused_for_hsl_edits_and_recaptured_after_source_edit(pattern_scene, monkeypatch):
    canvas, chapter, obj = pattern_scene
    layer = color_layer(chapter)
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=layer.layer_id)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    class RecordingRenderer:
        def __init__(self):
            self.images = []
        def render(self, image, modifier, **kwargs):
            self.images.append(kwargs["color_source"])
            return image.copy()
    renderer = RecordingRenderer()
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _: renderer)
    incoming = sample_image()
    frame = QRectF(0, 0, 120, 90)
    for hue in (0, 45):
        modifier.target_hue = hue
        render_stages(canvas, incoming, frame, [modifier], QTransform())
    assert renderer.images[0].cacheKey() == renderer.images[1].cacheKey()
    signature = canvas._modifier_object_signature(obj)
    layer.fill_color = "#00ff00"
    assert canvas._modifier_object_signature(obj) != signature
    render_stages(canvas, incoming, frame, [modifier], QTransform())
    assert renderer.images[2].cacheKey() != renderer.images[1].cacheKey()
    assert renderer.images[2].pixelColor(30, 30) == QColor("#00ff00")


def test_target_source_pixel_edit_invalidates_dependent_stage(pattern_scene):
    canvas, chapter, obj = pattern_scene
    layer = color_layer(chapter, color=None)
    source = chapter.add_object(layer.layer_id, RasterObject())
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=layer.layer_id,
                               blur=0, spacing=25, transparent_background=True)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    image = sample_image()
    frame = QRectF(0, 0, 120, 90)
    canvas.tiles.paint_dab(source.object_id, QPointF(60, 45), 150, QColor("red"), square=True)
    before, _ = render_stages(canvas, image, frame, [modifier], QTransform())
    canvas.tiles.paint_dab(source.object_id, QPointF(60, 45), 150, QColor("blue"), square=True)
    after, _ = render_stages(canvas, image, frame, [modifier], QTransform())
    assert before != after
    np.testing.assert_array_equal(_qimage_premultiplied(before)[..., 3],
                                  _qimage_premultiplied(after)[..., 3])
    dirty = canvas.modifier_expanded_dirty(source.object_id, QRectF(40, 30, 4, 4))
    assert dirty.contains(QRectF(0, 0, chapter.width, chapter.height))


def test_target_layer_crop_and_parent_transform_keep_color_sampling_aligned(pattern_scene):
    canvas, chapter, _ = pattern_scene
    parent = color_layer(chapter, color=None)
    layer = color_layer(chapter, parent.layer_id)
    layer.bound = BoundGeometry.rectangle(0, 0, 60, 180)
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=layer.layer_id,
                               target_hue=120, spacing=25, blur=1)
    incoming = sample_image()
    frame = QRectF(0, 0, 120, 90)
    full, _ = render_stages(canvas, incoming, frame, [modifier], QTransform())
    cropped, _ = render_stages(canvas, incoming, frame, [modifier], QTransform(),
                              required=QRectF(20, 15, 80, 60))
    assert cropped == full.copy(20, 15, 80, 60)
    parent.translate_x = 60
    moved, _ = render_stages(canvas, incoming, frame, [modifier], QTransform())
    assert moved != full
    np.testing.assert_array_equal(_qimage_premultiplied(moved)[..., 3],
                                  _qimage_premultiplied(full)[..., 3])


def test_self_reference_is_finite_and_does_not_pollute_normal_render_cache(pattern_scene):
    canvas, chapter, obj = pattern_scene
    layer = color_layer(chapter, color="#788899")
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=layer.layer_id,
                               blur=0, target_hue=90)
    chapter.add_modifier(modifier, [("layer", layer.layer_id)])
    def preview():
        image = QImage(240, 180, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(image)
        return image
    before = preview()
    assert not before.isNull()
    assert not canvas._rendering_halftone_source
    assert canvas._render_modifier_sources == set()
    assert preview() == before
    canvas._modifier_render_cache.clear()
    canvas._modifier_source_cache.clear()
    assert preview() == before


def test_target_layer_transformed_raster_bake_preserves_preview(pattern_scene):
    canvas, chapter, obj = pattern_scene
    layer = color_layer(chapter)
    layer.visible = False
    obj.transform_frame = (0, 0, 240, 180)
    obj.transform_quad = [(10, 6), (210, 18), (222, 164), (18, 156)]
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=layer.layer_id,
                               target_hue=120, transparent_background=True, blur=0)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    def preview():
        image = QImage(240, 180, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(image)
        return image
    before = preview()
    apply_raster_modifiers(canvas, modifier.modifier_id)
    assert preview() == before
    canvas.command_stack.undo()
    assert preview() == before


def test_asset_target_color_references_remap_inside_copy_and_drop_external_links(pattern_scene):
    from comic_editor.core.assets import extract_asset, instantiate_asset
    canvas, chapter, obj = pattern_scene
    layer = color_layer(chapter)
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=layer.layer_id,
                               target_hue=70)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    manifest, tiles = extract_asset(chapter, canvas.tiles, "layer", obj.parent_layer_id, "Colors")
    copied_modifier = next(iter(manifest.document.modifiers.values()))
    assert copied_modifier.target_layer_id == layer.layer_id
    target = ChapterDocument()
    page = target.add_page("Destination")
    instantiate_asset(manifest, tiles, target, TileStore(), page.layer_id, 500, 500)
    restored = next(iter(target.modifiers.values()))
    assert restored.target_layer_id in target.layers
    assert restored.target_layer_id != layer.layer_id
    assert restored.target_hue == 70
    manifest, _ = extract_asset(chapter, canvas.tiles, "object", obj.object_id, "Ink")
    assert next(iter(manifest.document.modifiers.values())).target_layer_id == ""


def test_asset_object_color_source_reference_follows_the_copied_object(pattern_scene):
    from comic_editor.core.assets import extract_asset, instantiate_asset
    canvas, chapter, obj = pattern_scene
    source = chapter.add_object(obj.parent_layer_id, RasterObject(name="Source colors"))
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=source.object_id)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    manifest, tiles = extract_asset(chapter, canvas.tiles, "layer", obj.parent_layer_id, "Colored ink")
    assert next(iter(manifest.document.modifiers.values())).target_layer_id == source.object_id
    target = ChapterDocument()
    page = target.add_page("Destination")
    instantiate_asset(manifest, tiles, target, TileStore(), page.layer_id, 500, 500)
    copied = next(iter(target.modifiers.values()))
    assert copied.target_layer_id in target.objects
    assert copied.target_layer_id != source.object_id
    assert target.objects[copied.target_layer_id].name == "Source colors"


def target_halftone_tone_mask(pattern_scene):
    from comic_editor.core.models import ToneMask
    canvas, chapter, _ = pattern_scene
    contributor = color_layer(chapter, color="#000000")
    target = color_layer(chapter, color="#ffffff")
    chapter.add_modifier(HalftoneModifier(color_mode="target_layer", target_layer_id=target.layer_id,
        base_resolution=100, spacing=16, blur=0), [("layer", contributor.layer_id)])
    chapter.add_modifier(HalftoneModifier(base_resolution=100, spacing=16, blur=0,
        transparent_background=True), [("layer", contributor.layer_id)])
    mask = ToneMask(contributors=[("layer", contributor.layer_id)])
    chapter.masks[mask.mask_id] = mask
    def render():
        return canvas.render_tone_mask_field(mask.mask_id, 100, 100, QTransform(), QRectF(0, 0, 100, 100))
    return canvas, target, mask, render


def test_target_capture_keeps_tone_mask_cache_separate_from_normal_render(pattern_scene):
    from comic_editor.ui.halftone_source import source_scope
    canvas, _, _, render = target_halftone_tone_mask(pattern_scene)
    normal = render()
    with source_scope(canvas):
        captured = render()
    assert normal.mean() < .01
    assert captured.mean() > .99
    np.testing.assert_array_equal(render(), normal)


def test_target_color_edit_invalidates_tone_mask_dependents(pattern_scene):
    canvas, target, mask, render = target_halftone_tone_mask(pattern_scene)
    before = render()
    signature = canvas._tone_mask_signature(mask.mask_id)
    target.fill_color = "#000000"
    assert canvas._tone_mask_signature(mask.mask_id) != signature
    after = render()
    assert before.mean() < .01
    assert after.mean() > .99


def test_target_capture_is_independent_of_outer_outward_gradient_pass(pattern_scene):
    from comic_editor.core.models import (ColorFillGradientObject, ColorGradientRamp,
        ColorGradientStop, ShapeGradientField)
    from comic_editor.ui.halftone_source import render_color_source
    canvas, chapter, _ = pattern_scene
    layer = color_layer(chapter, color=None)
    layer.bound = BoundGeometry.rectangle(25, 25, 50, 50)
    chapter.add_object(layer.layer_id, ColorFillGradientObject(field_type="parent_shape",
        shape_field=ShapeGradientField(reverse_direction=True, distance=20),
        ramp=ColorGradientRamp(stops=[ColorGradientStop(position=0, color="#80FF0000"),
                                     ColorGradientStop(position=1, color="#000000FF")]),
        ignore_parent_mask=True))
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=layer.layer_id)
    incoming = sample_image()
    frame = QRectF(0, 0, incoming.width(), incoming.height())
    normal = render_color_source(canvas, modifier, incoming, frame, QTransform())
    assert normal.pixelColor(20, 50).alpha() > 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._rendering_outward_gradient = True
    try:
        from_outward_pass = render_color_source(canvas, modifier, incoming, frame, QTransform())
        assert canvas._rendering_outward_gradient is True
    finally:
        canvas._rendering_outward_gradient = False
    assert from_outward_pass == normal
