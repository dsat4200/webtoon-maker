import copy
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QTransform

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ShapeStyle, ScreamModifier, WobbleModifier,
    DotDashModifier, RasterObject, ToneMask, ParameterMaskBinding, BlurModifier,
    TilingModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.compound_strokes import appearance
from comic_editor.ui.modifier_controls import ModifierControls


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=450)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 450))
    page.fill_color, page.border_width = None, 0
    style = ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF112233", outline_thickness=8)
    root = chapter.add_layer(page.layer_id, "Compound", BoundGeometry.rectangle(100, 100, 200, 200), style=style)
    root.compound_enabled = True
    child = chapter.add_layer(root.layer_id, "Operand", BoundGeometry.rectangle(250, 160, 140, 80), style=copy.deepcopy(style))
    child.compound_operation = "add"
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(640, 480)
    canvas.set_document(chapter, TileStore())
    yield canvas, chapter, root, child
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def pixels(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4).copy()


def render(canvas):
    canvas._clear_compound_path_cache()
    image = QImage(1080, 450, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return pixels(image)


def path_pixels(path):
    image = QImage(650, 450, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.fillPath(path, Qt.white)
    painter.end()
    return pixels(image)


def attach(scene, owner, modifier):
    canvas, chapter, _, _ = scene
    chapter.add_modifier(modifier, [("layer", owner.layer_id)])
    canvas._clear_compound_path_cache()
    return modifier


@pytest.mark.parametrize("owner_name", ["root", "child"])
@pytest.mark.parametrize("operation", ["add", "subtract", "ignore"])
@pytest.mark.parametrize("factory", [ScreamModifier, WobbleModifier])
def test_only_own_geometry_changes_and_boolean_operation_is_preserved(scene, owner_name, operation, factory):
    canvas, chapter, root, child = scene
    child.compound_operation = operation
    owner = root if owner_name == "root" else child
    original_root, original_child = copy.deepcopy(root.bound.to_dict()), copy.deepcopy(child.bound.to_dict())
    plain_root, plain_child = canvas.bound_path(root.bound), canvas.bound_path(child.bound)
    modifier = attach(scene, owner, factory())
    own = appearance(canvas, owner).path
    root_path = own if owner is root else plain_root
    child_path = own if owner is child else plain_child
    expected = root_path.united(child_path) if operation == "add" else root_path.subtracted(child_path) if operation == "subtract" else root_path
    assert np.array_equal(path_pixels(canvas.layer_effective_path(root.layer_id)), path_pixels(expected))
    other = child if owner is root else root
    assert appearance(canvas, other) is None
    assert root.bound.to_dict() == original_root
    assert child.bound.to_dict() == original_child
    # Geometry must also be the actual rendered clipping/fill, not just a helper.
    result = render(canvas)
    assert result[200, 180, 3] == 255
    modifier.muted = True
    assert not np.array_equal(result, render(canvas))


@pytest.mark.parametrize("owner_name", ["root", "child"])
@pytest.mark.parametrize("modifier", [DotDashModifier(distance=12), WobbleModifier(position=0, strength=100)])
def test_outline_appearance_is_attributed_only_to_its_owner(scene, owner_name, modifier):
    canvas, _, root, child = scene
    before = render(canvas)
    attach(scene, root if owner_name == "root" else child, copy.deepcopy(modifier))
    after = render(canvas)
    own_probe = (slice(140, 260), slice(100, 108)) if owner_name == "root" else (slice(160, 168), slice(310, 380))
    other_probe = (slice(160, 168), slice(310, 380)) if owner_name == "root" else (slice(140, 260), slice(100, 108))
    assert np.array_equal(before[other_probe], after[other_probe])
    assert not np.array_equal(before[own_probe], after[own_probe])
    assert np.array_equal(after, render(canvas))


@pytest.mark.parametrize("owner_name", ["root", "child"])
def test_child_artwork_is_not_warped_or_faded(scene, owner_name):
    canvas, chapter, root, child = scene
    # A high-contrast image lies near the owner's edge, where the previous
    # whole-composite warp would distort it. The new boundary only clips it.
    parent = root if owner_name == "root" else child
    left, top = (245, 180) if parent is root else (320, 180)
    raster = chapter.add_object(parent.layer_id, RasterObject())
    for y in range(top, top+40, 4):
        for x in range(left, left+40, 4):
            canvas.tiles.paint_dab(raster.object_id, QPointF(x+2, y+2), 4,
                QColor("red" if (x+y)%8 else "blue"), square=True, antialias=False)
    before = render(canvas)
    original = copy.deepcopy(raster.to_dict())
    attach(scene, parent, ScreamModifier(height=25, width=35))
    attach(scene, parent, WobbleModifier(position=0, strength=90))
    after = render(canvas)
    assert np.array_equal(before[top+2:top+38, left+2:left+38], after[top+2:top+38, left+2:left+38])
    assert raster.to_dict() == original


@pytest.mark.parametrize("operation", ["add", "subtract", "ignore"])
def test_nested_compound_owner_does_not_deform_its_children(scene, operation):
    canvas, chapter, root, child = scene
    child.compound_enabled = True
    child.compound_operation = operation
    grandchild = chapter.add_layer(child.layer_id, "Inner cut", BoundGeometry.circle(345, 200, 18))
    grandchild.compound_operation = "subtract"
    attach(scene, child, ScreamModifier(height=20))
    own = appearance(canvas, child).path
    nested = own.subtracted(canvas.bound_path(grandchild.bound))
    outer = canvas.bound_path(root.bound)
    expected = outer.united(nested) if operation == "add" else outer.subtracted(nested) if operation == "subtract" else outer
    assert np.array_equal(path_pixels(canvas.layer_effective_path(root.layer_id)), path_pixels(expected))
    assert appearance(canvas, grandchild) is None
    render(canvas)


def test_mask_edits_invalidate_compound_geometry_and_undo_restores_it(scene):
    canvas, chapter, root, child = scene
    canvas.set_selection("layer", child.layer_id)
    controls = ModifierControls(canvas)
    controls.add_modifier("stroke_scream")
    mid = child.modifier_ids[-1]
    after = render(canvas)
    canvas.command_stack.undo()
    before = render(canvas)
    assert not np.array_equal(before, after)
    canvas.command_stack.redo()
    assert np.array_equal(render(canvas), after)
    chapter = canvas.chapter
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    modifier = chapter.modifiers[mid]
    modifier.parameter_masks["height"] = ParameterMaskBinding(mask.mask_id, 0, 30)
    empty = render(canvas)
    canvas.tiles.paint_dab(mask.mask_id, QPointF(320, 200), 300, QColor("white"), square=True)
    mask.revision += 1
    assert not np.array_equal(empty, render(canvas))
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.modifiers[mid].to_dict() == modifier.to_dict()
    controls.deleteLater()


@pytest.mark.parametrize("operation", ["add", "subtract", "ignore"])
def test_nested_transforms_keep_modified_operands_in_the_right_space(scene, operation):
    canvas, _, root, child = scene
    root.translate_x, root.translate_y = 25, 12
    child.translate_x, child.translate_y = -30, 18
    child.transform_frame = (250, 160, 140, 80)
    child.transform_quad = [(260, 130), (400, 155), (380, 255), (240, 220)]
    child.compound_operation = operation
    attach(scene, child, ScreamModifier(height=15))
    own = canvas._layer_parent_transform(child).map(appearance(canvas, child).path)
    plain = canvas.bound_path(root.bound)
    expected = plain.united(own) if operation == "add" else plain.subtracted(own) if operation == "subtract" else plain
    actual_pixels, expected_pixels = path_pixels(canvas.layer_effective_path(root.layer_id)), path_pixels(expected)
    # Qt's projective world->local round trip can round one boundary pixel
    # differently from the direct local transform used by this reference.
    assert np.count_nonzero(np.any(actual_pixels != expected_pixels, axis=2)) <= 1
    render(canvas)


def test_rasterizing_compound_preserves_local_effects_and_undo(scene):
    from comic_editor.ui.baking import rasterize
    canvas, _, root, child = scene
    attach(scene, root, ScreamModifier(height=20))
    attach(scene, child, DotDashModifier())
    before = render(canvas)
    rasterize(canvas, "layer", root.layer_id)
    after = render(canvas)
    assert np.array_equal(before, after)
    canvas.command_stack.undo()
    assert np.array_equal(before, render(canvas))


def test_tiling_and_raster_effects_do_not_reapply_the_stroke_to_children(scene):
    canvas, chapter, root, child = scene
    attach(scene, root, TilingModifier(center=(180, 180), side=50))
    before = render(canvas)
    attach(scene, root, ScreamModifier(height=24))
    attach(scene, root, DotDashModifier())
    after = render(canvas)
    # The child's outer top edge stays straight and solid, including when
    # the parent has a separate tiling stage ahead of its local stroke.
    assert np.array_equal(before[160:168, 320:380], after[160:168, 320:380])
    assert not np.array_equal(before, after)
    blur = attach(scene, root, BlurModifier(strength=2))
    blurred = render(canvas)
    assert not np.array_equal(after, blurred)
    assert np.array_equal(blurred, render(canvas))


def test_compound_stroke_stack_order_and_source_width_flags(scene):
    canvas, _, root, child = scene
    root.bound.nodes[0].outline_multiplier = 2
    root.bound.nodes[2].outline_enabled = False
    first = attach(scene, root, DotDashModifier(mode="dash", length=12))
    second = attach(scene, root, ScreamModifier(height=25))
    original = copy.deepcopy(root.bound.to_dict())
    before = render(canvas)
    value = appearance(canvas, root)
    assert max(node.outline_multiplier for node in value.bound.nodes) > 1.9
    assert any(not node.outline_enabled for node in value.bound.nodes)
    root.modifier_ids = [second.modifier_id, first.modifier_id]
    assert not np.array_equal(before, render(canvas))
    assert root.bound.to_dict() == original
