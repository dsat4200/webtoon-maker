import copy
import time
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainterPath, QTransform
from PySide6.QtWidgets import QLineEdit, QPushButton, QComboBox

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ShapeStyle, ScreamModifier, WobbleModifier,
    DotDashModifier, BlurModifier, ParameterMaskBinding, ToneMask, RasterObject,
    ArrayModifier, MirrorModifier, TilingModifier, CageTransformModifier,
    VectorDrawingObject, VectorStroke, VectorStrokePoint, modifier_from_dict,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.stroke_geometry import sample_path, deform_loop, perlin, dot_dash_path, stroke_path
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=360)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 480, 360))
    page.fill_color, page.border_width = None, 0
    layer = chapter.add_layer(page.layer_id, "Bubble", BoundGeometry.circle(240, 180, 100),
        style=ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF182838", outline_thickness=8))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(640, 480)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", layer.layer_id)
    yield canvas, chapter, page, layer
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def pixels(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4).copy()


def attach(scene, modifier):
    canvas, chapter, page, layer = scene
    chapter.add_modifier(modifier, [("layer", layer.layer_id)])
    return modifier


@pytest.mark.parametrize("factory", [ScreamModifier, WobbleModifier, DotDashModifier])
def test_render_roundtrip_strength_mute_geometry_and_cache(scene, factory):
    canvas, chapter, page, layer = scene
    before = pixels(render(canvas))
    geometry = copy.deepcopy(layer.bound.to_dict())
    modifier = attach(scene, factory())
    started = time.perf_counter()
    rendered = pixels(render(canvas))
    first = time.perf_counter()-started
    assert not np.array_equal(before, rendered)
    assert layer.bound.to_dict() == geometry
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.modifiers[modifier.modifier_id].to_dict() == modifier.to_dict()
    started = time.perf_counter()
    assert np.array_equal(rendered, pixels(render(canvas)))
    cached = time.perf_counter()-started
    assert cached < max(.15, first*.6)
    modifier.intensity = 0
    assert np.array_equal(before, pixels(render(canvas)))
    modifier.intensity, modifier.muted = 100, True
    assert np.array_equal(before, pixels(render(canvas)))


def test_stacking_order_and_interleaved_blur(scene):
    canvas, chapter, _, layer = scene
    scream = attach(scene, ScreamModifier(height=16, width=40))
    wobble = attach(scene, WobbleModifier(position=18, noise_scale=28))
    dots = attach(scene, DotDashModifier(distance=10, mode="dash", length=18))
    first = pixels(render(canvas))
    layer.modifier_ids = [dots.modifier_id, wobble.modifier_id, scream.modifier_id]
    assert np.mean(abs(first.astype(float)-pixels(render(canvas)))) > .05
    layer.modifier_ids = [dots.modifier_id]
    blur = attach(scene, BlurModifier(strength=4))
    after = pixels(render(canvas))
    layer.modifier_ids.reverse()
    assert np.mean(abs(after.astype(float)-pixels(render(canvas)))) > .05


def test_parameter_masks_and_seed(scene):
    canvas, chapter, _, layer = scene
    modifier = attach(scene, WobbleModifier(position=20, strength=65))
    first = pixels(render(canvas))
    modifier.seed += 1
    assert not np.array_equal(first, pixels(render(canvas)))
    modifier.seed -= 1
    assert np.array_equal(first, pixels(render(canvas)))
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    masked = pixels(render(canvas))
    modifier.muted = True
    assert np.array_equal(masked, pixels(render(canvas)))
    modifier.muted = False
    modifier.parameter_masks["intensity"].black_value = 100
    assert np.array_equal(first, pixels(render(canvas)))


def test_compatibility_and_ui(scene, qapp):
    canvas, chapter, page, layer = scene
    controls = ModifierControls(canvas)
    controls.refresh()
    assert controls.stroke_menu.menuAction().isVisible()
    controls.add_modifier("stroke_dot_dash")
    modifier = chapter.modifiers[layer.modifier_ids[-1]]
    assert isinstance(modifier, DotDashModifier)
    field = controls.findChild(QLineEdit, "strokePattern")
    field.setText("-- - ")
    field.editingFinished.emit()
    assert modifier.pattern == "-- - "
    raster = chapter.add_object(page.layer_id, RasterObject())
    with pytest.raises(ValueError, match="closed"):
        chapter.add_modifier(WobbleModifier(), [("object", raster.object_id)])
    canvas.set_selection("object", raster.object_id)
    controls.refresh()
    assert not controls.stroke_menu.menuAction().isVisible()
    layer.compound_enabled = True
    assert not chapter.stroke_modifier_target("layer", layer.layer_id)
    layer.compound_enabled = False
    layer.bound.closed = False
    assert not chapter.stroke_modifier_target("layer", layer.layer_id)
    controls.deleteLater()


def test_closed_vector_supported_open_vector_rejected(scene):
    canvas, chapter, page, layer = scene
    vector = chapter.add_object(page.layer_id, VectorDrawingObject(strokes=[VectorStroke(closed=True,
        points=[VectorStrokePoint(x=x, y=y, width=12) for x, y in [(40, 40), (100, 40), (100, 120), (40, 120)]])]))
    modifier = DotDashModifier(roundness=0, distance=8)
    chapter.add_modifier(modifier, [("object", vector.object_id)])
    before = copy.deepcopy(vector.strokes[0].to_dict())
    image = render(canvas)
    assert np.count_nonzero(pixels(image)[:, :, 3]) > 0
    assert vector.strokes[0].to_dict() == before
    vector.strokes[0].closed = False
    assert not chapter.stroke_modifier_target("object", vector.object_id)


def test_periodic_geometry_and_axis_aligned_dots():
    path = QPainterPath()
    path.addEllipse(QRectF(0, 0, 200, 200))
    loop = sample_path(path, 10)[0]
    def parameters(modifier):
        return lambda name: np.full(len(loop.points), getattr(modifier, name))
    for modifier in [ScreamModifier(roundness=100), WobbleModifier(position=20, strength=70)]:
        result, opacity = deform_loop(loop, modifier, parameters(modifier))
        edge = np.linalg.norm(result.points[0]-result.points[-1])
        assert edge < 10
        assert abs(opacity[0]-opacity[-1]) < .1
    modifier = DotDashModifier(roundness=0, distance=30)
    squares = dot_dash_path(loop, modifier, parameters(modifier)).toSubpathPolygons()
    assert len(squares) > 3
    for square in squares:
        assert len(set(round(p.x(), 5) for p in square)) == 2
        assert len(set(round(p.y(), 5) for p in square)) == 2
    x = np.linspace(-20, 20, 1000)
    assert np.array_equal(perlin(x, x*.7, 123), perlin(x, x*.7, 123))
    assert not np.array_equal(perlin(x, x*.7, 123), perlin(x, x*.7, 124))


def test_numeric_validation_and_patterns():
    for factory in [ScreamModifier, WobbleModifier, DotDashModifier]:
        modifier = factory()
        for name, bounds in modifier.parameter_ranges().items():
            modifier.parameter_masks[name] = ParameterMaskBinding("mask", *bounds)
        assert modifier_from_dict(modifier.to_dict()).to_dict() == modifier.to_dict()
        modifier.intensity = float("nan")
        with pytest.raises(ValueError, match="finite"):
            modifier.validate()
    with pytest.raises(ValueError, match="spaces and dashes"):
        DotDashModifier(pattern="abc").validate()


@pytest.mark.parametrize("roundness", [0, 50, 100])
def test_short_rounded_dashes_respect_length(roundness):
    path = stroke_path(np.array([[0., 0.], [2., 0.]]), 10, roundness)
    assert path.boundingRect().width() == pytest.approx(2)
    assert path.boundingRect().height() == pytest.approx(10)


@pytest.mark.parametrize("factory,attribute,value", [
    (ScreamModifier, "height", 35), (ScreamModifier, "width", 48),
    (ScreamModifier, "roundness", 85), (WobbleModifier, "position", 30),
    (WobbleModifier, "strength", 75), (WobbleModifier, "noise_scale", 25),
    (WobbleModifier, "noise_offset", 79), (DotDashModifier, "distance", 24),
    (DotDashModifier, "length", 8), (DotDashModifier, "roundness", 45),
])
def test_every_numeric_parameter_uses_mask_endpoints(scene, factory, attribute, value):
    canvas, chapter, _, layer = scene
    modifier = factory()
    if isinstance(modifier, DotDashModifier):
        modifier.mode = "dash"
    setattr(modifier, attribute, value)
    attach(scene, modifier)
    expected = pixels(render(canvas))
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks[attribute] = ParameterMaskBinding(mask.mask_id, value, value)
    setattr(modifier, attribute, modifier.parameter_ranges()[attribute][0])
    assert np.array_equal(expected, pixels(render(canvas)))


@pytest.mark.parametrize("modifier", [
    ArrayModifier(axis_start=(100, 100), axis_end=(340, 100), count=1),
    MirrorModifier(axis_start=(400, 0), axis_end=(400, 400)),
    TilingModifier(center=(240, 180), side=100),
    CageTransformModifier(frame=(120, 60, 240, 240)),
])
def test_stroke_after_spatial_modifier(scene, modifier):
    canvas, chapter, _, layer = scene
    attach(scene, copy.deepcopy(modifier))
    before = pixels(render(canvas))
    stroke = attach(scene, ScreamModifier(height=15))
    after = pixels(render(canvas))
    assert not np.array_equal(before, after)
    assert np.array_equal(after, pixels(render(canvas)))


def test_transforms_mask_coordinates_and_local_fill(scene):
    canvas, chapter, _, layer = scene
    layer.translate_x, layer.translate_y = 60, 20
    layer.fill_color = "#FF61A4F8"
    modifier = attach(scene, DotDashModifier())
    image = render(canvas)
    assert image.pixelColor(300, 200).name() == "#61a4f8"
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    canvas.tiles.paint_dab(mask.mask_id, QPointF(300, 200), 500, QColor('white'), square=True)
    modifier.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0, 100)
    assert np.array_equal(pixels(image), pixels(render(canvas)))


def test_undo_redo_randomize_and_link(scene, qapp):
    canvas, chapter, page, layer = scene
    controls = ModifierControls(canvas)
    controls.add_modifier("stroke_wobble")
    mid = layer.modifier_ids[-1]
    initial = chapter.modifiers[mid].seed
    button = next(button for button in controls.findChildren(QPushButton) if button.text() == "Randomize seed")
    button.click()
    randomized = canvas.chapter.modifiers[mid].seed
    assert randomized != initial
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[mid].seed == initial
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[mid].seed == randomized
    other = canvas.chapter.add_layer(page.layer_id, "Other", BoundGeometry.circle(500, 180, 60))
    canvas.chapter.set_modifier_targets(mid, [("layer", layer.layer_id), ("layer", other.layer_id)])
    assert other.modifier_ids == [mid]
    controls.deleteLater()
