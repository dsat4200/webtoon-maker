"""Posterize eligibility and editable shared stacks on color gradients."""
import json

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QInputDialog

from comic_editor.core.models import (
    ArrayModifier, BlurModifier, BoundGeometry, CageTransformModifier,
    ChapterDocument, ColorFillGradientObject, ColorGradientRamp, ColorGradientStop,
    GradientObject, HalftoneModifier, HueSaturationLightnessModifier, ImageObject,
    LineGradientField, MirrorModifier, OutlineModifier, PathNode, PixelateModifier,
    PosterizeModifier, PosterizeValueModifier, RadialBlurModifier, RasterObject,
    ScreamModifier, SpeedLinesGradientObject, TilingModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls


@pytest.fixture
def gradient_editor(qapp):
    chapter = ChapterDocument(name="Gradient posterization", height=256)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 256, 256))
    layer = chapter.add_layer(page.layer_id, "Art", BoundGeometry.rectangle(0, 0, 256, 256))
    gradient = chapter.add_object(layer.layer_id, ColorFillGradientObject(
        line_field=LineGradientField(BoundGeometry.path([PathNode(x=0, y=128), PathNode(x=256, y=128)])),
        ramp=ColorGradientRamp(stops=[ColorGradientStop(position=0., color="#FFFF0000"),
                                      ColorGradientStop(position=1., color="#FF0000FF")]),
    ))
    raster = chapter.add_object(layer.layer_id, RasterObject(interaction_rect=(0, 0, 256, 256)))
    other = chapter.add_object(layer.layer_id, ColorFillGradientObject(field_type="radial"))
    tiles = TileStore()
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("green"))
    tiles.set_tile(raster.object_id, (0, 0), image)
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(chapter, tiles)
    canvas.set_selection("object", gradient.object_id)
    controls = ModifierControls(canvas)
    controls.refresh()
    yield canvas, controls, gradient, raster, other
    canvas._effect_jobs.cancel()
    controls.deleteLater()
    canvas.deleteLater()


@pytest.mark.parametrize("factory", [PosterizeModifier, PosterizeValueModifier, HalftoneModifier])
def test_gradient_posterize_registry_shared_links_and_round_trip(gradient_editor, factory):
    canvas, _, gradient, raster, _ = gradient_editor
    chapter = canvas.chapter
    refs = [("object", gradient.object_id), ("object", raster.object_id)]
    modifier = factory(muted=True, expanded=False, intensity=45)
    assert chapter.modifier_target(*refs[0]) is gradient
    chapter.add_modifier(modifier, refs)
    restored = ChapterDocument.from_dict(json.loads(json.dumps(chapter.to_dict())))
    assert restored.modifiers[modifier.modifier_id].to_dict() == modifier.to_dict()
    assert restored.objects[gradient.object_id].modifier_ids == [modifier.modifier_id]
    assert restored.objects[raster.object_id].modifier_ids == [modifier.modifier_id]


@pytest.mark.parametrize("factory", [HueSaturationLightnessModifier, BlurModifier,
    OutlineModifier, MirrorModifier, ArrayModifier, RadialBlurModifier,
    CageTransformModifier, TilingModifier, PixelateModifier, ScreamModifier])
def test_gradient_rejects_other_modifier_types_including_tiling(gradient_editor, factory):
    canvas, _, gradient, raster, _ = gradient_editor
    chapter = canvas.chapter
    modifier = factory()
    gradient_ref = ("object", gradient.object_id)
    assert chapter.incompatible_modifier_targets(modifier, [gradient_ref]) == [gradient_ref]
    with pytest.raises(ValueError, match="Color gradients support Posterize"):
        chapter.add_modifier(modifier, [gradient_ref])
    assert not gradient.modifier_ids
    # A modifier compatible with an existing drawing cannot acquire a gradient
    # through linking, either. Tiling's separate compatibility checks must not
    # overwrite the gradient restriction.
    if not chapter.incompatible_modifier_targets(modifier, [("object", raster.object_id)]):
        chapter.add_modifier(modifier, [("object", raster.object_id)])
        with pytest.raises(ValueError, match="Color gradients support Posterize"):
            chapter.set_modifier_targets(modifier.modifier_id, [("object", raster.object_id), gradient_ref])


@pytest.mark.parametrize("factory", [GradientObject, SpeedLinesGradientObject])
def test_base_and_removed_gradient_types_remain_ineligible(factory):
    chapter = ChapterDocument()
    gradient = factory()
    chapter.objects[gradient.object_id] = gradient
    ref = ("object", gradient.object_id)
    assert chapter.modifier_target(*ref) is None
    assert chapter.incompatible_modifier_targets(PosterizeModifier(), [ref]) == [ref]


def test_invalid_legacy_gradient_links_are_removed_without_losing_posterize(gradient_editor):
    canvas, _, gradient, raster, _ = gradient_editor
    chapter = canvas.chapter
    posterize, blur = PosterizeModifier(), BlurModifier()
    chapter.add_modifier(posterize, [("object", gradient.object_id)])
    chapter.add_modifier(blur, [("object", raster.object_id)])
    gradient.modifier_ids.append(blur.modifier_id)
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.objects[gradient.object_id].modifier_ids == [posterize.modifier_id]
    assert restored.objects[raster.object_id].modifier_ids == [blur.modifier_id]


@pytest.mark.parametrize("kind", ["posterize", "posterize_value", "halftone"])
@pytest.mark.parametrize("field_type", ["line", "radial", "parent_shape"])
def test_actual_controls_add_gradient_posterize_and_undo(gradient_editor, monkeypatch, kind, field_type):
    canvas, controls, gradient, _, _ = gradient_editor
    gradient.field_type = field_type
    monkeypatch.setattr(QInputDialog, "getInt", lambda *_args: (4, True))
    assert controls.add_button.isEnabled()
    assert controls.summary.text() == "Gradient modifiers"
    visible = {action.text() for action in controls.add_button.menu().actions() if action.isVisible()}
    assert visible == {"Posterize…", "Posterize Value…", "Halftone"}
    controls.add_modifier(kind)
    modifier = canvas.chapter.modifiers[gradient.modifier_ids[-1]]
    assert modifier.modifier_type == kind
    if kind != "halftone":
        assert len(modifier.ranges) == 4
    assert modifier.modifier_id in controls._cards
    canvas.command_stack.undo()
    assert canvas.chapter.objects[gradient.object_id].modifier_ids == []
    canvas.command_stack.redo()
    assert canvas.chapter.objects[gradient.object_id].modifier_ids == [modifier.modifier_id]


@pytest.mark.parametrize("kind", ["posterize", "posterize_value", "halftone"])
def test_mixed_gradient_selection_add_and_link_are_undoable(gradient_editor, monkeypatch, kind):
    canvas, controls, gradient, raster, other = gradient_editor
    refs = [("object", gradient.object_id), ("object", raster.object_id)]
    assert canvas.set_selection_set(refs)
    monkeypatch.setattr(QInputDialog, "getInt", lambda *_args: (3, True))
    controls.add_modifier(kind)
    modifier = canvas.chapter.modifiers[gradient.modifier_ids[-1]]
    assert raster.modifier_ids == gradient.modifier_ids
    assert controls.common_ids() == [modifier.modifier_id]
    controls.toggle_link_mode(modifier.modifier_id)
    assert controls.toggle_link_target("object", other.object_id)
    controls.commit_link_mode()
    assert other.modifier_ids == [modifier.modifier_id]
    canvas.command_stack.undo()
    assert canvas.chapter.objects[other.object_id].modifier_ids == []
    canvas.command_stack.redo()
    restored = ChapterDocument.from_dict(canvas.chapter.to_dict())
    assert restored.objects[other.object_id].modifier_ids == [modifier.modifier_id]
    canvas.set_selection("object", raster.object_id)
    controls.refresh()
    assert any(action.text() == "Halftone" and action.isVisible() for action in controls.add_button.menu().actions())
