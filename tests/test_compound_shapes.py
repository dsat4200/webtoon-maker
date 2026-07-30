from __future__ import annotations

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, PathContour, PathNode, RasterObject,
    TextObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.inspector import ContextInspector


def _compound_canvas():
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1080, 1080)
    )
    root = chapter.add_layer(
        page.layer_id, "Compound",
        BoundGeometry.rectangle(100, 100, 300, 300),
    )
    root.compound_enabled = True
    root.fill_color = "#ff0000"
    canvas = CanvasWidget(EditorSettings(page_scope_select=True))
    canvas.set_document(chapter, TileStore(), reset_view=False)
    return canvas, chapter, page, root


def test_compound_fields_and_multiple_contours_round_trip():
    chapter = ChapterDocument()
    page = chapter.add_page()
    bound = BoundGeometry(
        nodes=[
            PathNode(x=0, y=0), PathNode(x=100, y=0),
            PathNode(x=100, y=100), PathNode(x=0, y=100),
        ],
        closed=True,
        primitive="custom",
        additional_contours=[PathContour(nodes=[
            PathNode(x=25, y=25), PathNode(x=75, y=25),
            PathNode(x=75, y=75), PathNode(x=25, y=75),
        ])],
    )
    layer = chapter.add_layer(page.layer_id, "Compound", bound)
    layer.compound_enabled = True
    layer.compound_operation = "subtract"
    obj = chapter.add_object(
        layer.layer_id, RasterObject(geometry_reference="compound")
    )

    loaded = ChapterDocument.from_dict(chapter.to_dict())

    restored = loaded.layers[layer.layer_id]
    assert loaded.schema_version == 9
    assert restored.compound_enabled
    assert restored.compound_operation == "subtract"
    assert len(restored.bound.additional_contours) == 1
    assert (
        loaded.objects[obj.object_id].geometry_reference == "compound"
    )


def test_compound_add_subtract_ignore_visibility_and_nested_geometry(qapp):
    canvas, chapter, page, root = _compound_canvas()
    added = chapter.add_layer(
        root.layer_id, "Add",
        BoundGeometry.rectangle(350, 100, 250, 300),
    )
    cut = chapter.add_layer(
        root.layer_id, "Cut", BoundGeometry.circle(250, 250, 60),
    )
    cut.compound_operation = "subtract"
    ignored = chapter.add_layer(
        root.layer_id, "Ignored",
        BoundGeometry.rectangle(700, 100, 100, 100),
    )
    ignored.compound_operation = "ignore"

    path = canvas.layer_effective_path(root.layer_id)
    assert path.contains(QPointF(550, 200))
    assert not path.contains(QPointF(250, 250))
    assert not path.contains(QPointF(750, 150))

    added.visible = False
    canvas._clear_compound_path_cache()
    assert not canvas.layer_effective_path(root.layer_id).contains(
        QPointF(550, 200)
    )

    ignored.compound_enabled = True
    nested = chapter.add_layer(
        ignored.layer_id, "Nested add",
        BoundGeometry.rectangle(780, 100, 100, 100),
    )
    canvas._clear_compound_path_cache()
    assert canvas.layer_effective_path(ignored.layer_id).contains(
        QPointF(850, 150)
    )
    assert not canvas.layer_effective_path(root.layer_id).contains(
        QPointF(850, 150)
    )


def test_open_shape_contributes_core_stroke_without_child_outline(qapp):
    canvas, chapter, page, root = _compound_canvas()
    line = chapter.add_layer(
        root.layer_id, "Line",
        BoundGeometry.path([
            PathNode(x=400, y=500), PathNode(x=700, y=500),
        ]),
        layer_kind="open_shape",
    )
    line.shape_style.base_thickness = 20
    line.shape_style.outline_thickness = 50
    canvas._clear_compound_path_cache()
    path = canvas.layer_effective_path(root.layer_id)

    assert path.contains(QPointF(550, 505))
    assert not path.contains(QPointF(550, 530))


def test_compound_renders_parent_style_without_internal_outline(qapp):
    canvas, chapter, page, root = _compound_canvas()
    root.border_color = "#000000"
    root.border_width = 8
    chapter.add_layer(
        root.layer_id, "Add",
        BoundGeometry.rectangle(350, 100, 250, 300),
    )
    image = QImage(1080, 1080, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)

    interior_seam = image.pixelColor(350, 250)
    outer_edge = image.pixelColor(100, 250)
    assert interior_seam.red() > 180 and interior_seam.green() < 80
    assert outer_edge.lightness() < 80


def test_geometry_reference_changes_strict_text_bounds(qapp):
    canvas, chapter, page, root = _compound_canvas()
    child = chapter.add_layer(
        root.layer_id, "Small",
        BoundGeometry.rectangle(150, 150, 80, 60),
    )
    text = chapter.add_object(
        child.layer_id,
        TextObject(layout_mode="strict", margin=0),
    )
    direct = canvas._strict_text_rect(text)
    text.geometry_reference = "compound"
    compound = canvas._strict_text_rect(text)

    assert direct.width() == 80
    assert direct.height() == 60
    assert compound.width() == 300
    assert compound.height() == 300


def test_context_inspector_persists_geometry_reference(qapp):
    canvas, chapter, page, root = _compound_canvas()
    child = chapter.add_layer(
        root.layer_id, "Small",
        BoundGeometry.rectangle(150, 150, 80, 60),
    )
    raster = chapter.add_object(child.layer_id, RasterObject())
    canvas.set_selection("object", raster.object_id)
    inspector = ContextInspector(
        canvas, canvas.settings, lambda _settings: None
    )
    try:
        inspector.refresh()
        target = inspector.geometry_reference.findData("compound")
        inspector.geometry_reference.setCurrentIndex(target)
        assert raster.geometry_reference == "compound"
        assert chapter.objects[raster.object_id].geometry_reference == "compound"
    finally:
        inspector.deleteLater()


def test_raster_compound_reference_bypasses_direct_parent_mask(qapp):
    canvas, chapter, page, root = _compound_canvas()
    child = chapter.add_layer(
        root.layer_id, "Small",
        BoundGeometry.rectangle(150, 150, 80, 60),
    )
    raster = chapter.add_object(
        child.layer_id,
        RasterObject(interaction_rect=(0, 0, 500, 500)),
    )
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(300, 300), 30, QColor("#000000")
    )
    direct = QImage(1080, 1080, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(direct)
    assert direct.pixelColor(300, 300).red() > 180

    raster.geometry_reference = "compound"
    compound = QImage(1080, 1080, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(compound)
    assert compound.pixelColor(300, 300).lightness() < 80


def test_compound_selection_and_creation_placement(qapp):
    canvas, chapter, page, root = _compound_canvas()
    child = chapter.add_layer(
        root.layer_id, "Child",
        BoundGeometry.rectangle(200, 150, 100, 100),
    )
    canvas.set_selection("layer", root.layer_id)
    assert canvas._target_placement_for_new_bound() == (root.layer_id, 0)

    canvas.set_selection("layer", child.layer_id)
    parent, index = canvas._target_placement_for_new_bound()
    assert parent == root.layer_id
    assert root.children[index].entity_id == child.layer_id

    canvas.set_selection("layer", root.layer_id)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._tool_press(
        canvas.document_to_widget(QPointF(200, 200)), 1
    )
    assert canvas.selected_id == child.layer_id


def test_additional_contour_nodes_are_shape_editable(qapp):
    canvas, chapter, page, root = _compound_canvas()
    root.compound_enabled = False
    root.bound = BoundGeometry(
        nodes=[
            PathNode(x=100, y=100), PathNode(x=400, y=100),
            PathNode(x=400, y=400), PathNode(x=100, y=400),
        ],
        closed=True, primitive="custom",
        additional_contours=[PathContour(nodes=[
            PathNode(x=200, y=200), PathNode(x=300, y=200),
            PathNode(x=300, y=300), PathNode(x=200, y=300),
        ])],
    )
    selected = root.bound.additional_contours[0].nodes[0]
    canvas.set_selection("layer", root.layer_id)
    canvas._selected_shape_node_id = selected.node_id

    hit = canvas._shape_hit_test(
        root.bound, QPointF(selected.x, selected.y)
    )

    assert hit["node_id"] == selected.node_id
    assert canvas._selected_shape_node(root.bound) is selected
    assert "type" in canvas._shape_gizmo_positions(root.bound, selected)


def test_flatten_preserves_holes_ignored_branches_and_object_positions(qapp):
    canvas, chapter, page, root = _compound_canvas()
    contributor = chapter.add_layer(
        root.layer_id, "Contributor",
        BoundGeometry.rectangle(350, 100, 250, 300),
    )
    cut = chapter.add_layer(
        root.layer_id, "Cut", BoundGeometry.circle(250, 250, 60),
    )
    cut.compound_operation = "subtract"
    ignored = chapter.add_layer(
        contributor.layer_id, "Ignored",
        BoundGeometry.rectangle(500, 500, 100, 100),
    )
    ignored.compound_operation = "ignore"
    contributor.translate_x = 20
    raster = chapter.add_object(
        contributor.layer_id,
        RasterObject(x=10, y=20, interaction_rect=(0, 0, 20, 20)),
    )
    before_path = canvas.layer_effective_path(root.layer_id)
    before_world = (
        contributor.translate_x + raster.x,
        contributor.translate_y + raster.y,
    )

    assert canvas.flatten_compound_layer(root.layer_id)

    assert not root.compound_enabled
    assert root.layer_kind == "bounded"
    assert contributor.layer_id not in canvas.chapter.layers
    assert ignored.layer_id in canvas.chapter.layers
    restored_raster = canvas.chapter.objects[raster.object_id]
    assert restored_raster.parent_layer_id == root.layer_id
    assert (restored_raster.x, restored_raster.y) == before_world
    after_path = canvas.bound_path(canvas.chapter.layers[root.layer_id].bound)
    for point in (
        QPointF(120, 120), QPointF(250, 250), QPointF(570, 200),
    ):
        assert after_path.contains(point) == before_path.contains(point)
    assert canvas.chapter.layers[root.layer_id].bound.additional_contours
    canvas.command_stack.undo()
    assert canvas.chapter.layers[root.layer_id].compound_enabled
