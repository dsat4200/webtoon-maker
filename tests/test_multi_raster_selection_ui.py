"""Drawing selection tools are available for compatible outliner groups."""
import pytest

from comic_editor.core.models import ChapterDocument, RasterObject, VectorDrawingObject
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def multi_raster_window(qapp):
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page()
    first = chapter.add_layer(page.layer_id, "First")
    second = chapter.add_layer(page.layer_id, "Second")
    a = chapter.add_object(first.layer_id, RasterObject(name="Ink"))
    b = chapter.add_object(second.layer_id, RasterObject(name="Color"))
    vector = chapter.add_object(second.layer_id, VectorDrawingObject())
    window._set_chapter(chapter, TileStore())
    yield window, first, second, a, b, vector
    window.autosave_timer.stop()
    window.canvas._effect_jobs.cancel()
    window.deleteLater()


@pytest.mark.parametrize("selection_kind", ["objects", "layers", "layer_and_object"])
@pytest.mark.parametrize("tool", [ToolKind.DRAW_SELECT_RECT, ToolKind.DRAW_SELECT_LASSO])
def test_multiselection_exposes_and_activates_area_tools(multi_raster_window, selection_kind, tool):
    window, first, second, a, b, _vector = multi_raster_window
    refs = {
        "objects": [("object", a.object_id), ("object", b.object_id)],
        "layers": [("layer", first.layer_id), ("layer", second.layer_id)],
        "layer_and_object": [("layer", first.layer_id), ("object", b.object_id)],
    }[selection_kind]
    assert window.canvas.set_selection_set(refs, primary=refs[0])
    assert not window.drawing_selection_category.isHidden()
    button = window.drawing_selection_buttons[tool]
    assert not button.isHidden()
    assert button.isEnabled()

    assert window._activate_tool(tool)
    assert window.canvas.tool == tool
    assert button.isChecked()
    assert window.canvas.selected_entities == refs
    assert window.canvas.selected_id == refs[0][1]
    stroke = window.drawing_selection_buttons[ToolKind.DRAW_SELECT_STROKE]
    assert stroke.isHidden()
    assert not stroke.isEnabled()
    assert not window._activate_tool(ToolKind.DRAW_SELECT_STROKE)
    assert window.canvas.tool == tool


def test_explicit_nonraster_selection_keeps_area_tools_unavailable(multi_raster_window):
    window, _first, _second, a, _b, vector = multi_raster_window
    refs = [("object", a.object_id), ("object", vector.object_id)]
    assert window.canvas.set_selection_set(refs, primary=refs[0])
    assert window.drawing_selection_category.isHidden()
    assert not window._activate_tool(ToolKind.DRAW_SELECT_LASSO)
    assert window.canvas.tool == ToolKind.TRANSFORM


def test_switching_to_single_vector_restores_stroke_selection(multi_raster_window):
    window, _first, _second, a, b, vector = multi_raster_window
    window.canvas.set_selection_set([("object", a.object_id), ("object", b.object_id)])
    assert window.drawing_selection_buttons[ToolKind.DRAW_SELECT_STROKE].isHidden()

    window.canvas.set_selection("object", vector.object_id)
    stroke = window.drawing_selection_buttons[ToolKind.DRAW_SELECT_STROKE]
    assert not stroke.isHidden()
    assert stroke.isEnabled()
    assert window._activate_tool(ToolKind.DRAW_SELECT_STROKE)
