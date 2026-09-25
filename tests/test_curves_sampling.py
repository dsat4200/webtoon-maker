"""Curves samples the isolated stack input in its actual document placement."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BoundGeometry, BrightnessContrastModifier, ChapterDocument, CurvesModifier, ImageObject,
    ParameterMaskBinding, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import curves_features
from comic_editor.ui.curves_features import CurvesSampler
from comic_editor.ui.modifier_rendering import apply_modifier_stack


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=300, height=260, document_kind="asset", background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 300, 260))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    yield canvas, chapter, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def add_image(scene, colors, *, parent=None, size=None):
    canvas, chapter, page = scene
    width, height = size or (len(colors), 1)
    source = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    source.fill(Qt.transparent)
    for y in range(height):
        for x in range(width):
            source.setPixelColor(x, y, colors[min(len(colors) - 1, x * len(colors) // width)])
    obj = chapter.add_object((parent or page).layer_id,
        ImageObject(x=40, y=40, pixel_width=width, pixel_height=height))
    canvas.images.put_decoded(obj.object_id, "sample.png", b"", source)
    return obj, source


def attach(scene, obj, modifier=None):
    canvas, chapter, _page = scene
    modifier = modifier or CurvesModifier()
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    canvas.set_selection("object", obj.object_id)
    return modifier


def test_histograms_use_independent_alpha_weighted_bins(scene):
    obj, _ = add_image(scene, [QColor(255, 0, 0, 255), QColor(0, 255, 0, 128),
                               QColor(0, 0, 255, 64), QColor(255, 255, 255, 0)])
    modifier = attach(scene, obj)
    sampler = CurvesSampler()
    red = sampler.histogram(scene[0], modifier.modifier_id, "rgb", "red")
    expected = np.zeros(256)
    expected[255], expected[0] = 1, 192 / 255
    np.testing.assert_allclose(red, expected, atol=1e-7)
    alpha = sampler.histogram(scene[0], modifier.modifier_id, "rgb", "alpha")
    expected[:] = 0
    expected[[255, 128, 64]] = [1, 128/255, 64/255]
    np.testing.assert_allclose(alpha, expected, atol=1e-7)
    master = sampler.histogram(scene[0], modifier.modifier_id, "rgb", "master")
    expected[:] = 0
    total = 1 + 192/255
    expected[[0, 255]] = [2*total/3, total/3]
    np.testing.assert_allclose(master, expected, atol=1e-7)


def test_histogram_and_pixel_exclude_current_and_downstream_stages(scene, monkeypatch):
    canvas, chapter, _page = scene
    obj, source = add_image(scene, [QColor(64, 128, 192)], size=(12, 8))
    upstream = BrightnessContrastModifier(brightness=12)
    chapter.add_modifier(upstream, [("object", obj.object_id)])
    modifier = attach(scene, obj, CurvesModifier(curves={"rgb:master": [(0, 1), (1, 0)]}))
    downstream = BrightnessContrastModifier(brightness=-60)
    chapter.add_modifier(downstream, [("object", obj.object_id)])
    captures = []
    original_capture = curves_features._capture
    def capture(*args, **kwargs):
        captures.append(1)
        return original_capture(*args, **kwargs)
    monkeypatch.setattr(curves_features, "_capture", capture)
    sampler = CurvesSampler()
    actual = sampler.histogram(canvas, modifier.modifier_id, "rgb", "master")
    expected_color = apply_modifier_stack(source, [upstream], (0, 0)).pixelColor(3, 3)
    expected = np.zeros(256)
    for value in expected_color.getRgb()[:3]:
        expected[value] += 32
    np.testing.assert_allclose(actual, expected, atol=1e-6)
    modifier.curves = {"rgb:master": [(0, .25), (1, .75)]}
    downstream.brightness = 60
    np.testing.assert_array_equal(sampler.histogram(canvas, modifier.modifier_id, "rgb", "master"), actual)
    assert len(captures) == 1
    sampled, alpha = sampler.pixel(canvas, modifier.modifier_id, QPointF(46, 44))
    np.testing.assert_allclose(sampled, np.array(expected_color.getRgb()[:3]) / 255, atol=1/65535)
    assert alpha == 1
    assert obj.modifier_ids == [upstream.modifier_id, modifier.modifier_id, downstream.modifier_id]
    upstream.brightness = 25
    changed = sampler.histogram(canvas, modifier.modifier_id, "rgb", "master")
    assert len(captures) == 3  # One pixel request plus a new upstream input capture.
    assert not np.array_equal(actual, changed)


def test_histogram_range_and_master_channel_mapping_reuse_input_capture(scene, monkeypatch):
    canvas, _chapter, _page = scene
    obj, _ = add_image(scene, [QColor(51, 102, 204)], size=(2, 2))
    modifier = attach(scene, obj, CurvesModifier(input_min=.2, input_max=.8))
    sampler = CurvesSampler()
    histogram = sampler.histogram(canvas, modifier.modifier_id, "rgb", "red")
    assert histogram[0] == pytest.approx(4)
    original_samples = sampler.samples
    modifier.curves = {"rgb:master": [(0, 1), (1, 0)]}
    histogram = sampler.histogram(canvas, modifier.modifier_id, "rgb", "red")
    assert histogram[255] == pytest.approx(4)
    assert sampler.samples is original_samples
    modifier.input_min, modifier.input_max = 0, 2
    modifier.curves = {}
    histogram = sampler.histogram(canvas, modifier.modifier_id, "rgb", "red")
    assert histogram[25] == pytest.approx(4)
    assert sampler.samples is original_samples


def test_sampler_obeys_image_and_parent_affine_transforms(scene):
    canvas, chapter, page = scene
    parent = chapter.add_layer(page.layer_id, "Transformed", BoundGeometry.rectangle(0, 0, 100, 100))
    parent.fill_color, parent.border_width = None, 0
    parent.transform_frame = (0, 0, 100, 100)
    parent.transform_quad = [(20, 30), (220, 30), (220, 230), (20, 230)]
    obj, _ = add_image(scene, [QColor(51, 102, 204)], parent=parent, size=(10, 10))
    obj.transform_quad = [(40, 20), (50, 22), (52, 32), (42, 30)]
    modifier = attach(scene, obj)
    sampler = CurvesSampler()
    # Object center -> (46,26), then parent scale2 + (20,30) -> (112,82).
    sampled, alpha = sampler.pixel(canvas, modifier.modifier_id, QPointF(112.2, 82.2))
    np.testing.assert_allclose(sampled, [.2, .4, .8], atol=1/65535)
    assert alpha == 1
    assert sampler.pixel(canvas, modifier.modifier_id, QPointF(45, 45)) is None
    histogram = sampler.histogram(canvas, modifier.modifier_id, "rgb", "red")
    # Determinant .96 in the image transform, then determinant4 in the parent.
    assert histogram.sum() == pytest.approx(384, abs=4)
    # Fractional edge coverage can round unpremultiplied RGB by one bin.
    assert histogram[51] > 360


def test_sampler_cache_tracks_source_pixels_and_target_placement(scene):
    canvas, _chapter, _page = scene
    obj, source = add_image(scene, [QColor("red")], size=(10, 10))
    modifier = attach(scene, obj)
    sampler = CurvesSampler()
    before = sampler.histogram(canvas, modifier.modifier_id, "rgb", "red")
    original = sampler.samples
    source.fill(QColor("blue"))
    canvas.images.put_decoded(obj.object_id, "new.png", b"", source)
    after = sampler.histogram(canvas, modifier.modifier_id, "rgb", "red")
    assert sampler.samples is not original and before[255] == 100 and after[0] == 100
    original = sampler.samples
    obj.x += 50
    sampler.histogram(canvas, modifier.modifier_id, "rgb", "red")
    assert sampler.samples is not original
    assert sampler.pixel(canvas, modifier.modifier_id, QPointF(45, 45)) is None
    color, _ = sampler.pixel(canvas, modifier.modifier_id, QPointF(95, 45))
    np.testing.assert_allclose(color, [0, 0, 1])


def test_sampling_restores_document_and_capture_flags_after_render_failure(scene, monkeypatch):
    canvas, _chapter, _page = scene
    obj, _ = add_image(scene, [QColor("red")])
    modifier = attach(scene, obj)
    obj.visible, obj.mask_only, obj.opacity = False, True, .37
    obj.opacity_mask = ParameterMaskBinding("mask", 25, 25)
    canvas._interactive_render = True
    before = (obj.modifier_ids, obj.visible, obj.mask_only, canvas._interactive_render,
              canvas._rendering_compound_references, canvas._rendering_outward_gradient,
              obj.opacity, obj.opacity_mask)
    monkeypatch.setattr(canvas, "_render_object", lambda *_: (_ for _ in ()).throw(RuntimeError("test")))
    sampler = CurvesSampler()
    with pytest.raises(RuntimeError, match="test"):
        sampler.histogram(canvas, modifier.modifier_id, "rgb", "master")
    after = (obj.modifier_ids, obj.visible, obj.mask_only, canvas._interactive_render,
             canvas._rendering_compound_references, canvas._rendering_outward_gradient,
             obj.opacity, obj.opacity_mask)
    assert after == before
    assert not sampler.busy


@pytest.mark.parametrize("target_kind", ["object", "layer"])
def test_input_sampling_excludes_owner_opacity_mask_but_keeps_child_opacity(scene, target_kind):
    canvas, chapter, page = scene
    parent = chapter.add_layer(page.layer_id, "Group", BoundGeometry.rectangle(30, 30, 30, 30))
    parent.fill_color, parent.border_width = None, 0
    obj, _ = add_image(scene, [QColor(255, 0, 0)], parent=parent, size=(10, 10))
    obj.opacity_locked = False
    if target_kind == "object":
        owner, ref, expected_alpha = obj, ("object", obj.object_id), 1.
    else:
        obj.opacity = .5
        owner, ref, expected_alpha = parent, ("layer", parent.layer_id), 127 / 255
    owner.opacity = .2
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    owner.opacity_mask = ParameterMaskBinding(mask.mask_id, 25, 25)
    modifier = CurvesModifier()
    chapter.add_modifier(modifier, [ref])
    canvas.set_selection(*ref)
    before_mask = owner.opacity_mask
    sampler = CurvesSampler()
    rgb, alpha = sampler.pixel(canvas, modifier.modifier_id, QPointF(45, 45))
    np.testing.assert_allclose(rgb, [1, 0, 0])
    assert alpha == pytest.approx(expected_alpha, abs=1/65535)
    histogram = sampler.histogram(canvas, modifier.modifier_id, "rgb", "alpha")
    expected = np.zeros(256)
    expected[min(255, int(expected_alpha * 256))] = expected_alpha * 100
    np.testing.assert_allclose(histogram, expected, atol=1e-5)
    assert owner.opacity == .2 and owner.opacity_mask is before_mask
    if target_kind == "layer":
        assert obj.opacity == .5


@pytest.mark.parametrize("mode,channel,index", [
    ("gray", "master", 54), ("rgb", "red", 255), ("cmyk", "cyan", 0),
    ("cmyk", "magenta", 255), ("cmyk", "black", 0),
    ("lab", "lightness", 136), ("lab", "a", 208), ("lab", "b", 195),
])
def test_mode_channel_histograms_match_known_red_coordinates(scene, mode, channel, index):
    canvas, _chapter, _page = scene
    obj, _ = add_image(scene, [QColor(255, 0, 0, 128)])
    modifier = attach(scene, obj)
    histogram = CurvesSampler().histogram(canvas, modifier.modifier_id, mode, channel)
    expected = np.zeros(256)
    expected[index] = 128 / 255
    np.testing.assert_allclose(histogram, expected, atol=1e-7)


def test_linked_pixel_sampling_prefers_explicit_primary_selection(scene):
    canvas, chapter, _page = scene
    first, _ = add_image(scene, [QColor("red")], size=(10, 10))
    second, _ = add_image(scene, [QColor("blue")], size=(10, 10))
    first_ref, second_ref = ("object", first.object_id), ("object", second.object_id)
    modifier = CurvesModifier()
    chapter.add_modifier(modifier, [first_ref, second_ref])
    assert canvas.set_selection_set([first_ref, second_ref], primary=first_ref)
    sampler = CurvesSampler()
    sampled, _ = sampler.pixel(canvas, modifier.modifier_id, QPointF(45, 45))
    np.testing.assert_allclose(sampled, [1, 0, 0])
    histogram = sampler.histogram(canvas, modifier.modifier_id, "rgb", "red")
    assert histogram[0] == 100 and histogram[255] == 100
