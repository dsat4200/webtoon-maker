"""Posterize initializes from visible gradient colors before its own stage."""
import numpy as np
import pytest
from PySide6.QtGui import QColor

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, ColorGradientRamp,
    ColorGradientStop, LineGradientField, PathNode, PosterizeModifier,
    PosterizeRange, PosterizeValueModifier, RadialGradientField, ShapeGradientField,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.posterize_controls import PosterizeSampler


def ramp(first="#FFFF0000", last=None):
    return ColorGradientRamp(stops=[ColorGradientStop(position=0, color=first),
                                   ColorGradientStop(position=1, color=last or first)])


def gradient(field_type="line", reverse=False, colors=None):
    return ColorFillGradientObject(field_type=field_type, ramp=colors or ramp(),
        line_field=LineGradientField(geometry=BoundGeometry.path([
            PathNode(x=40, y=60), PathNode(x=120, y=60)])),
        radial_field=RadialGradientField(origin_x=80, origin_y=60,
            radius_x=25, radius_y=20, ellipse_enabled=True,
            reverse_direction=reverse, distance=20),
        shape_field=ShapeGradientField(reverse_direction=reverse, distance=20))


@pytest.fixture
def gradient_scene(qapp):
    chapter = ChapterDocument(width=180, height=140, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 180, 140))
    page.fill_color, page.border_width = None, 0
    parent = chapter.add_layer(page.layer_id, "Gradient shape", BoundGeometry.rectangle(40, 30, 80, 60))
    parent.fill_color, parent.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    yield canvas, chapter, parent
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


@pytest.mark.parametrize("field_type,reverse", [("line", False), ("radial", False),
    ("parent_shape", False), ("radial", True), ("parent_shape", True)])
@pytest.mark.parametrize("value_mode", [False, True])
def test_direct_gradient_modes_produce_real_palette_statistics(gradient_scene, field_type, reverse, value_mode):
    canvas, chapter, parent = gradient_scene
    obj = chapter.add_object(parent.layer_id, gradient(field_type, reverse))
    sampler = PosterizeSampler(value_mode=value_mode)
    statistics = sampler.sample(canvas, [("object", obj.object_id)])
    assert statistics.counts.sum() > 100
    assert np.argmax(statistics.counts) == (54 if value_mode else 0)
    expected = "#FF363636" if value_mode else "#FFFF0000"
    assert statistics.average(0, 256 if value_mode else 360) == expected
    assert {item.color for item in statistics.initialize(3)} == {expected}
    if reverse:
        pixels = sampler._samples[0][0]
        # Outward shape and radial fields extend beyond the ordinary parent.
        assert pixels.shape[0] > 60 and pixels.shape[1] > 80
        assert np.any(pixels[..., 3] == 0)


def test_hue_initialization_observes_both_ends_of_colored_gradient(gradient_scene):
    canvas, chapter, parent = gradient_scene
    obj = chapter.add_object(parent.layer_id, gradient(colors=ramp("#FFFF0000", "#FF00FF00")))
    statistics = PosterizeSampler().sample(canvas, [("object", obj.object_id)])
    assert statistics.counts[:30].sum() > 100
    assert statistics.counts[90:121].sum() > 100
    palette = statistics.initialize(2)
    assert len({item.color for item in palette}) == 2
    colors = [QColor(item.color) for item in palette]
    assert any(color.red() > color.green() for color in colors)
    assert any(color.green() > color.red() for color in colors)
    assert all(color.blue() == 0 for color in colors)


def test_value_initialization_observes_gradient_brightness_distribution(gradient_scene):
    canvas, chapter, parent = gradient_scene
    obj = chapter.add_object(parent.layer_id, gradient(colors=ramp("#FF000000", "#FFFFFFFF")))
    statistics = PosterizeSampler(value_mode=True).sample(canvas, [("object", obj.object_id)])
    assert np.count_nonzero(statistics.counts) > 30
    average = QColor(statistics.average(0, 256))
    assert 126 <= average.red() <= 129
    palette = statistics.initialize(3)
    values = [QColor(item.color).red() for item in palette]
    assert values == sorted(values)
    assert values[-1] - values[0] > 100


def test_transparent_gradient_pixels_do_not_add_hidden_hues(gradient_scene):
    canvas, chapter, parent = gradient_scene
    colors = ColorGradientRamp(stops=[
        ColorGradientStop(position=0, color="#80FF0000"),
        ColorGradientStop(position=.5, color="#80FF0000"),
        ColorGradientStop(position=.5, color="#0000FF00"),
        ColorGradientStop(position=1, color="#0000FF00"),
    ])
    obj = chapter.add_object(parent.layer_id, gradient(colors=colors))
    statistics = PosterizeSampler().sample(canvas, [("object", obj.object_id)])
    assert statistics.counts[120] == 0
    assert 1000 < statistics.counts.sum() < 1400
    assert statistics.counts[0] > .99 * statistics.counts.sum()
    assert statistics.average(0, 360) == "#FFFF0000"


@pytest.mark.parametrize("field_type,reverse", [("line", False), ("radial", False),
    ("parent_shape", False), ("radial", True), ("parent_shape", True)])
@pytest.mark.parametrize("value_mode", [False, True])
def test_gradient_sampler_reads_only_upstream_prefix_and_reuses_raw_source(gradient_scene, value_mode, field_type, reverse):
    canvas, chapter, parent = gradient_scene
    obj = chapter.add_object(parent.layer_id, gradient(field_type, reverse))
    targets = [("object", obj.object_id)]
    upstream = PosterizeModifier(ranges=[PosterizeRange(0, "#FF0000FF")])
    current = (PosterizeValueModifier if value_mode else PosterizeModifier)()
    downstream = PosterizeModifier(ranges=[PosterizeRange(0, "#FFFFFF00")])
    for modifier in (upstream, current, downstream):
        chapter.add_modifier(modifier, targets)
    sampler = PosterizeSampler(value_mode=value_mode)
    before_model = chapter.to_dict()
    statistics = sampler.sample(canvas, targets, current.modifier_id)
    assert np.argmax(statistics.counts) == (18 if value_mode else 240)
    assert chapter.to_dict() == before_model
    current.ranges[0].color = "#FFFF00FF"
    assert sampler.sample(canvas, targets, current.modifier_id) is statistics
    raw_samples = sampler._samples
    current.simplify_enabled, current.simplify_strength = True, 50
    sampler.sample(canvas, targets, current.modifier_id)
    assert sampler._samples is raw_samples
    upstream.ranges[0].color = "#FF00FF00"
    changed = sampler.sample(canvas, targets, current.modifier_id)
    assert np.argmax(changed.counts) == (182 if value_mode else 120)


def test_linked_posterize_samples_each_gradient_before_shared_modifier(gradient_scene):
    canvas, chapter, parent = gradient_scene
    red = chapter.add_object(parent.layer_id, gradient())
    second_parent = chapter.add_layer(parent.parent_id, "Second gradient", BoundGeometry.rectangle(40, 30, 80, 60))
    second_parent.fill_color, second_parent.border_width = None, 0
    blue = chapter.add_object(second_parent.layer_id, gradient(colors=ramp("#FF0000FF")))
    targets = [("object", red.object_id), ("object", blue.object_id)]
    modifier = PosterizeModifier(ranges=[PosterizeRange(0, "#FF00FF00")])
    chapter.add_modifier(modifier, targets)
    statistics = PosterizeSampler().sample(canvas, targets, modifier.modifier_id)
    assert statistics.counts[0] > 4000 and statistics.counts[240] > 4000
    assert statistics.counts[120] == 0
    assert {item.color for item in statistics.initialize(2)} == {"#FFFF0000", "#FF0000FF"}


def test_hidden_outward_gradient_is_sampled_and_flags_are_restored(gradient_scene):
    canvas, chapter, parent = gradient_scene
    obj = chapter.add_object(parent.layer_id, gradient("parent_shape", True))
    obj.visible, obj.mask_only = False, True
    canvas._interactive_render = True
    canvas._rendering_outward_gradient = False
    statistics = PosterizeSampler().sample(canvas, [("object", obj.object_id)])
    assert statistics.counts[0] > 100
    assert obj.visible is False and obj.mask_only is True
    assert canvas._interactive_render is True
    assert canvas._rendering_outward_gradient is False


def test_gradient_sampling_restores_prefix_and_outward_state_on_failure(gradient_scene, monkeypatch):
    canvas, chapter, parent = gradient_scene
    obj = chapter.add_object(parent.layer_id, gradient("radial", True))
    obj.visible, obj.mask_only = False, True
    modifier = PosterizeModifier()
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    original_ids = list(obj.modifier_ids)
    canvas._interactive_render = True
    def fail(*_args):
        assert canvas._rendering_outward_gradient is True
        raise ValueError("sampling failed")
    monkeypatch.setattr(canvas, "_render_object", fail)
    with pytest.raises(ValueError, match="sampling failed"):
        PosterizeSampler().sample(canvas, [("object", obj.object_id)], modifier.modifier_id)
    assert obj.modifier_ids == original_ids
    assert obj.visible is False and obj.mask_only is True
    assert canvas._interactive_render is True
    assert canvas._rendering_outward_gradient is False


def test_gradient_ramp_edit_invalidates_sampler_source_cache(gradient_scene):
    canvas, chapter, parent = gradient_scene
    obj = chapter.add_object(parent.layer_id, gradient())
    sampler = PosterizeSampler()
    targets = [("object", obj.object_id)]
    red = sampler.sample(canvas, targets)
    assert sampler.sample(canvas, targets) is red
    obj.ramp.stops[0].color = obj.ramp.stops[1].color = "#FF0000FF"
    blue = sampler.sample(canvas, targets)
    assert blue is not red
    assert blue.average(0, 360) == "#FF0000FF"


def test_same_bounds_parent_geometry_edit_changes_visible_gradient_statistics(gradient_scene):
    canvas, chapter, parent = gradient_scene
    obj = chapter.add_object(parent.layer_id, gradient(colors=ramp("#FFFF0000", "#FF00FF00")))
    sampler = PosterizeSampler()
    targets = [("object", obj.object_id)]
    original_model = obj.to_dict()
    rectangle = sampler.sample(canvas, targets)
    parent.bound = BoundGeometry.path([PathNode(x=40, y=30), PathNode(x=120, y=30),
                                      PathNode(x=40, y=90)], closed=True)
    triangle = sampler.sample(canvas, targets)
    assert obj.to_dict() == original_model
    assert triangle is not rectangle
    assert .45 < triangle.counts.sum() / rectangle.counts.sum() < .55
    assert triangle.average(0, 360) != rectangle.average(0, 360)


def test_normal_gradient_respects_parent_shape_unless_parent_mask_is_ignored(gradient_scene):
    canvas, chapter, parent = gradient_scene
    parent.bound = BoundGeometry.path([PathNode(x=40, y=30), PathNode(x=120, y=30),
                                      PathNode(x=40, y=90)], closed=True)
    obj = chapter.add_object(parent.layer_id, gradient())
    sampler = PosterizeSampler()
    targets = [("object", obj.object_id)]
    clipped = sampler.sample(canvas, targets)
    obj.ignore_parent_mask = True
    unmasked = sampler.sample(canvas, targets)
    assert 1.8 < unmasked.counts.sum() / clipped.counts.sum() < 2.2
