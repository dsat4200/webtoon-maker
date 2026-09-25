"""Natural distortion output must retain pixels beyond the source rectangle."""
import base64
import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.core.effect_geometry import effect_bounds
from comic_editor.core.distort import DISTORT_TYPES
from comic_editor.core.images import ImageStore
from comic_editor.core.lens_profiles import load_lens_catalog
from comic_editor.core.models import (BoundGeometry, BrightnessContrastModifier, ChapterDocument,
    DistortModifier, HueSaturationLightnessModifier, ImageObject, OutlineModifier, RasterObject)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.baking import apply_raster_modifiers, rasterize
from comic_editor.ui.distort_rendering import _image, _rgba, distort_bounds, render_distort
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.modifier_rendering import apply_modifier_stack


SOURCE = QRectF(0, 0, 64, 48)
PROBE = QRectF(-180, -180, 424, 408)


def source_image():
    y, x = np.indices((48, 64))
    alpha = ((x >= 4) & (x < 60) & (y >= 4) & (y < 44)).astype(float)
    rgb = np.stack((.3 + x / 100, .2 + y / 100, .15 + (x + y) % 7 / 10), axis=-1)
    return _image(np.concatenate((rgb * alpha[..., None], alpha[..., None]), axis=-1))


def modifier(effect, parameters, mapping=None, center=(18., 14.)):
    mapping = mapping or QTransform()
    result = DistortModifier(modifier_type="distort_" + effect,
        frame=mapping.mapRect(SOURCE).getRect(),
        center=mapping.map(QPointF(*center)).toTuple(), radius=64.,
        parameters={"edges": "transparent", "interpolation": "bilinear", **parameters})
    result.validate()
    return result


def placed(image, bounds, target=PROBE):
    """Place a result at its document coordinates without resampling."""
    output = QImage(int(target.width()), int(target.height()), QImage.Format_ARGB32_Premultiplied)
    output.fill(Qt.transparent)
    painter = QPainter(output)
    painter.drawImage(bounds.topLeft() - target.topLeft(), image)
    painter.end()
    return _rgba(output)


def support(pixels, frame=PROBE):
    rows, columns = np.nonzero(pixels[..., 3] > 0)
    assert len(rows)
    return QRectF(frame.x() + int(columns.min()), frame.y() + int(rows.min()),
                  int(columns.max() - columns.min() + 1), int(rows.max() - rows.min() + 1))


def assert_reference_is_complete(reference):
    alpha = _rgba(reference)[..., 3]
    assert not alpha[:3].any() and not alpha[-3:].any()
    assert not alpha[:, :3].any() and not alpha[:, -3:].any()


def assert_natural_matches_oversized(source, mod, mapping=None):
    reference = render_distort(source, SOURCE, mod, mapping, output_bounds=PROBE)
    assert_reference_is_complete(reference)
    bounds = distort_bounds(SOURCE, mod, mapping)
    assert bounds.contains(support(_rgba(reference)))
    actual = render_distort(source, SOURCE, mod, mapping)
    np.testing.assert_allclose(placed(actual, bounds), _rgba(reference), atol=1 / 255.)
    return reference, bounds


def png_bytes(image):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


@pytest.mark.parametrize("effect,parameters", [
    ("ripple", {"amount": 100., "wavelength": 17.}),
    ("ripple", {"amount": -100., "wavelength": 17.}),
    ("pixelate", {"size": 13.}),
    ("mirror", {"mirrors": 1, "input_angle": 35., "output_angle": -70.}),
    ("mirror", {"mirrors": 32, "input_angle": -90., "output_angle": 180.}),
])
@pytest.mark.parametrize("transformed", [False, True], ids=["document", "transformed-parent"])
def test_finite_nonradial_output_retains_oversized_reference(effect, parameters, transformed):
    mapping = QTransform()
    if transformed:
        mapping.translate(35, -10)
        mapping.rotate(21)
        mapping.scale(1.3, .75)
    mod = modifier(effect, parameters, mapping)
    if effect == "pixelate":
        # Both frame edges fall inside partially occupied cells.
        mod.frame = (3., 5., 64., 48.)
    assert_natural_matches_oversized(source_image(), mod, mapping)


@pytest.mark.parametrize("method", ["red_green", "sobel"])
@pytest.mark.parametrize("preserve_alpha", [False, True])
@pytest.mark.parametrize("amount", [-40., 40.])
def test_displacement_bounds_include_sampled_content_and_preserve_alpha(method, preserve_alpha, amount):
    values = np.ones((4, 4, 4), np.float32)
    if method == "red_green":
        values[..., :3] = (1., 0., 0.)
    else:
        values[..., :3] = np.linspace(0., 1., 4)[None, :, None]
    mod = modifier("displace", {"method": method, "amount": amount,
        "preserve_alpha": preserve_alpha, "map_source": "embedded",
        "map_png": base64.b64encode(png_bytes(_image(values))).decode("ascii")})
    reference, bounds = assert_natural_matches_oversized(source_image(), mod)
    if preserve_alpha:
        assert bounds == SOURCE
        np.testing.assert_array_equal(_rgba(reference)[..., 3], placed(source_image(), SOURCE)[..., 3])
    else:
        assert not SOURCE.contains(support(_rgba(reference)))


MOVING_GLITCH_MODES = [mode for mode in DISTORT_TYPES["distort_glitch"]["parameters"]["mode"]["choices"]
                       if mode not in {"quantisation", "fuzz", "channel_flip"}]


@pytest.mark.parametrize("mode", MOVING_GLITCH_MODES)
@pytest.mark.parametrize("channels,bidirectional", [(1, True), (1, False), (3, True), (3, False)])
@pytest.mark.parametrize("amount", [-100., 100.])
def test_moving_glitch_modes_fit_bounds_at_channel_direction_extremes(mode, channels, bidirectional, amount):
    mod = modifier("glitch", dict(mode=mode, channels=channels, bidirectional=bidirectional,
        amount=amount, offset_x=-13., offset_y=9., spacing=11., loops=5.,
        horizontal_strength=-60., vertical_strength=40., turbulence=100.,
        slice_offset=-30., seed=23., stagger=True))
    assert_natural_matches_oversized(source_image(), mod)


def test_fractional_light_streak_offset_keeps_one_world_pixel_extent_under_parent_scale():
    mapping = QTransform.fromScale(.1, .1)
    mod = modifier("glitch", {"mode": "light_streaks", "amount": 100.,
        "offset_x": .1, "offset_y": 0.}, mapping)
    opaque_source = _image(np.ones((48, 64, 4)))
    reference, _ = assert_natural_matches_oversized(opaque_source, mod, mapping)
    assert support(_rgba(reference)).left() < -1


@pytest.mark.parametrize("model,attributes", [
    ("poly3", 'k1="0.15"'), ("poly3", 'k1="-0.15"'),
    ("poly5", 'k1="-0.15" k2="0.01"'), ("poly5", 'k1="0.06" k2="-0.01"'),
    ("ptlens", 'a="0.04" b="-0.01" c="0.03"'),
    ("ptlens", 'a="-0.04" b="0.01" c="-0.03"'),
])
@pytest.mark.parametrize("camera_crop", [1., 2.])
def test_lens_correction_polynomial_bounds_include_all_finite_branches(model, attributes, camera_crop):
    xml = f'''<lensdatabase><camera><maker>Example</maker><model>Camera</model>
      <mount>Test</mount><cropfactor>{camera_crop}</cropfactor></camera>
      <lens><maker>Example</maker><model>Lens</model><mount>Test</mount>
      <cropfactor>1</cropfactor><aspect-ratio>4:3</aspect-ratio><calibration>
      <distortion model="{model}" focal="50" {attributes}/>
      </calibration></lens></lensdatabase>'''
    catalog = load_lens_catalog(xml_text=xml)
    mod = modifier("lens_correction", dict(profile_xml=xml, lens_profile=catalog.lenses[0].id,
        camera_profile=catalog.cameras[0].id, focal_length=50.))
    assert_natural_matches_oversized(source_image(), mod)


@pytest.mark.parametrize("amount", [-1e-12, -1e-14])
def test_extremely_distant_lens_branch_is_rejected_before_image_allocation(amount):
    mod = modifier("lens_distortion", {"amount": amount})
    with pytest.raises(ValueError, match="too large"):
        render_distort(source_image(), SOURCE, mod)


def test_extremely_distant_lens_bounds_cannot_wrap_qt_integer_rectangle():
    mod = modifier("lens_distortion", {"amount": -1e-14})
    with pytest.raises(ValueError, match="too large"):
        distort_bounds(SOURCE, mod)


@pytest.mark.parametrize("effect,parameters,center", [
    ("twirl", {"angle": 250.}, (18., 14.)),
    ("pinch_punch", {"amount": 80.}, (18., 14.)),
    ("spherical", {"amount": 90.}, (18., 14.)),
    ("lens_distortion", {"amount": -35.}, (18., 14.)),
    ("lens_distortion", {"amount": 65.}, (-35., 14.)),
])
@pytest.mark.parametrize("transformed", [False, True], ids=["document", "transformed-parent"])
def test_radial_natural_bounds_include_every_visible_output_pixel(effect, parameters, center, transformed):
    mapping = QTransform()
    if transformed:
        mapping.translate(35, -10)
        mapping.rotate(21)
        mapping.scale(1.3, .75)
    mod = modifier(effect, parameters, mapping, center)
    source = source_image()
    reference = render_distort(source, SOURCE, mod, mapping, output_bounds=PROBE)
    assert_reference_is_complete(reference)
    expected = _rgba(reference)
    visible = support(expected)
    assert not SOURCE.contains(visible), "The fixture must move visible pixels outside the image"

    bounds = distort_bounds(SOURCE, mod, mapping)
    assert bounds.contains(visible)
    actual = render_distort(source, SOURCE, mod, mapping)
    np.testing.assert_allclose(placed(actual, bounds), expected, atol=1 / 255.)


@pytest.fixture
def canvas(qapp):
    document = ChapterDocument(height=400)
    page = document.add_page("Page", BoundGeometry.rectangle(0, 0, 400, 400))
    page.fill_color, page.border_width = None, 0
    widget = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    widget.set_document(document, TileStore(), ImageStore())
    yield widget, document, page
    widget._effect_jobs.cancel()
    widget.deleteLater()


def stack(mapping=None):
    return [modifier("twirl", {"angle": 250.}, mapping),
            modifier("lens_distortion", {"amount": -35.}, mapping),
            modifier("pinch_punch", {"amount": 80.}, mapping),
            HueSaturationLightnessModifier(hue=-25., saturation=37., lightness=13.)]


def oversized_stack(source, source_bounds, modifiers, probe=PROBE):
    image, bounds = source, source_bounds
    for mod in modifiers:
        if isinstance(mod, DistortModifier):
            image = render_distort(image, bounds, mod, output_bounds=probe)
            bounds = probe
        else:
            image = apply_modifier_stack(image, [mod], bounds.topLeft().toTuple())
    assert_reference_is_complete(image)
    return image


def scene_pixels(widget):
    output = QImage(400, 400, QImage.Format_ARGB32_Premultiplied)
    output.fill(Qt.transparent)
    painter = QPainter(output)
    try:
        widget._render_scene_layers(painter, QRectF(0, 0, 400, 400))
    finally:
        painter.end()
    return _rgba(output)


@pytest.mark.parametrize("required", [None, QRectF(-20, -15, 45, 35)], ids=["full", "viewport"])
def test_stacked_radial_effects_and_hsl_retain_expanded_intermediate_pixels(canvas, required):
    widget, _, _ = canvas
    modifiers = stack()
    source = source_image()
    reference = oversized_stack(source, SOURCE, modifiers)
    expected = _rgba(reference)
    assert not SOURCE.contains(support(expected))
    assert effect_bounds(SOURCE, modifiers).contains(support(expected))

    result, bounds = render_stages(widget, source, SOURCE, modifiers, QTransform(), required=required)
    if required is None:
        np.testing.assert_allclose(placed(result, bounds), expected, atol=2 / 255.)
    else:
        np.testing.assert_allclose(placed(result, bounds, required),
                                  placed(reference, PROBE, required), atol=2 / 255.)


def test_expanded_distortion_brightness_and_blurred_outline_match_oversized_stack(canvas):
    widget, _, _ = canvas
    modifiers = [modifier("twirl", {"angle": 250.}),
                 modifier("lens_distortion", {"amount": -35.}),
                 BrightnessContrastModifier(brightness=23., contrast=-31.),
                 OutlineModifier(thickness=4., blur_radius=3., blur_strength=80.)]
    source = source_image()
    reference = oversized_stack(source, SOURCE, modifiers)
    result, bounds = render_stages(widget, source, SOURCE, modifiers, QTransform())
    assert not SOURCE.contains(support(_rgba(reference)))
    np.testing.assert_allclose(placed(result, bounds), _rgba(reference), atol=2 / 255.)


def test_image_object_stack_can_paint_outside_original_image_frame(canvas):
    widget, document, page = canvas
    source = source_image()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    assert source.save(buffer, "PNG")
    obj = document.add_object(page.layer_id, ImageObject(x=140, y=140, pixel_width=64, pixel_height=48))
    widget.images.put(obj.object_id, "source.png", bytes(data), "image/png")
    mapping = QTransform.fromTranslate(140, 140)
    modifiers = stack(mapping)
    for mod in modifiers:
        document.add_modifier(mod, [("object", obj.object_id)])

    image_frame = SOURCE.translated(140, 140)
    probe = PROBE.translated(140, 140)
    reference = oversized_stack(source, image_frame, modifiers, probe)
    output_bounds = QRectF(0, 0, 400, 400)
    expected = placed(reference, probe, output_bounds)
    assert not image_frame.contains(support(expected, output_bounds))

    np.testing.assert_allclose(scene_pixels(widget), expected, atol=2 / 255.)


@pytest.mark.parametrize("kind", ["image", "raster"])
def test_baking_expanded_stack_and_undo_preserve_visible_pixels(canvas, kind):
    widget, document, page = canvas
    source = source_image()
    if kind == "image":
        obj = document.add_object(page.layer_id, ImageObject(x=140, y=140, pixel_width=64, pixel_height=48))
        widget.images.put(obj.object_id, "source.png", png_bytes(source), "image/png")
    else:
        obj = document.add_object(page.layer_id, RasterObject(x=140, y=140,
            interaction_rect=SOURCE.getRect(), modifier_source_frame=SOURCE.getRect()))
        tile = QImage(obj.tile_size, obj.tile_size, QImage.Format_ARGB32_Premultiplied)
        tile.fill(Qt.transparent)
        painter = QPainter(tile)
        painter.drawImage(0, 0, source)
        painter.end()
        widget.tiles.set_tile(obj.object_id, (0, 0), tile)
    modifiers = stack(QTransform.fromTranslate(140, 140))
    for mod in modifiers:
        document.add_modifier(mod, [("object", obj.object_id)])
    widget.set_selection("object", obj.object_id)
    before = scene_pixels(widget)
    assert not SOURCE.translated(140, 140).contains(support(before, QRectF(0, 0, 400, 400)))

    if kind == "image":
        rasterize(widget, "object", obj.object_id)
        replacement = widget.chapter.objects[obj.object_id]
        assert replacement.pixel_width > source.width()
        assert replacement.pixel_height > source.height()
        assert not replacement.modifier_ids
    else:
        apply_raster_modifiers(widget, modifiers[-2].modifier_id)
        replacement = widget.chapter.objects[obj.object_id]
        assert replacement.modifier_ids == [modifiers[-1].modifier_id]
        assert QRectF(*replacement.modifier_source_frame).contains(SOURCE)
        assert QRectF(*replacement.modifier_source_frame) != SOURCE
    np.testing.assert_allclose(scene_pixels(widget), before, atol=2 / 255.)
    widget.command_stack.undo()
    assert widget.chapter.objects[obj.object_id].modifier_ids == [mod.modifier_id for mod in modifiers]
    np.testing.assert_allclose(scene_pixels(widget), before, atol=2 / 255.)
    widget.command_stack.redo()
    np.testing.assert_allclose(scene_pixels(widget), before, atol=2 / 255.)
