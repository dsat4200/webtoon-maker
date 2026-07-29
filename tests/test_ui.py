from __future__ import annotations

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, TextObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.tree_model import HierarchyModel


def _chapter():
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(page.layer_id)
    raster = chapter.add_object(layer.layer_id, RasterObject())
    text = chapter.add_object(
        layer.layer_id,
        TextObject(
            layout_mode="free",
            transform_quad=[(0, 0), (360, 0), (360, 120), (0, 120)],
        ),
    )
    return chapter, page, layer, raster, text


def test_contextual_raster_tools(qapp):
    chapter, page, layer, raster, text = _chapter()
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", text.object_id)
    assert not canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.set_selection("object", raster.object_id)
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    assert canvas.set_tool(ToolKind.RASTER_ERASER)


def test_layer_scope_and_page_scope_selection(qapp):
    chapter, page, layer, raster, text = _chapter()
    other = chapter.add_layer(page.layer_id, "Other")
    other_raster = chapter.add_object(other.layer_id, RasterObject(x=400, y=400))
    canvas = CanvasWidget(EditorSettings(page_scope_select=False))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", layer.layer_id)
    assert canvas.hit_test_object(canvas.object_world_rect(other_raster.object_id).center()) is None
    canvas.settings.page_scope_select = True
    assert canvas.hit_test_object(canvas.object_world_rect(other_raster.object_id).center()) == other_raster.object_id


def test_hierarchy_model_exposes_tree_without_arrows(qapp):
    chapter, page, layer, raster, text = _chapter()
    model = HierarchyModel(chapter)
    assert model.rowCount() == 1
    page_index = model.index(0, 0)
    assert model.rowCount(page_index) == 1
    layer_index = model.index(0, 0, page_index)
    assert model.rowCount(layer_index) == 2


def test_tablet_mode_does_not_hide_toolbars(qapp):
    window = MainWindow()
    window.tablet_mode.setChecked(True)
    assert window.tool_toolbar.isVisibleTo(window)
    assert window.hierarchy_dock.isVisibleTo(window)
    window.deleteLater()
