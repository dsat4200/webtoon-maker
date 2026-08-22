from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, QPointF, Qt
from PySide6.QtWidgets import QDialog

from comic_editor.core import settings as settings_module
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, TextObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.settings_dialog import SettingsDialog
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


def test_settings_action_grid_tab_and_global_toggle_do_not_dirty_document(
    qapp, monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        settings_module, "settings_path", lambda: tmp_path / "settings.json"
    )
    window = MainWindow()
    chapter, _page, _layer, _raster, _text = _chapter()
    window._set_chapter(chapter, TileStore())
    try:
        actions = window.file_toolbar.actions()
        assert actions.index(window.settings_action) == (
            actions.index(window.hotkeys_action) + 1
        )
        dialog = SettingsDialog(window.settings, chapter, window)
        assert dialog.tabs.tabText(0) == "Grid"
        assert dialog.document_override.isEnabled()
        dialog.reject()

        before = chapter.to_dict()
        before_commands = len(window.canvas.command_stack._undo)
        before_visible = window.settings.grid_overlay_visible
        window._toggle_grid()

        assert window.settings.grid_overlay_visible is not before_visible
        assert chapter.to_dict() == before
        assert len(window.canvas.command_stack._undo) == before_commands
        assert window._dirty is False
        assert settings_module.load_settings().grid_overlay_visible is (
            not before_visible
        )
    finally:
        window.deleteLater()


def test_settings_dialog_accept_applies_one_document_grid_change(
    qapp, monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        settings_module, "settings_path", lambda: tmp_path / "settings.json"
    )
    window = MainWindow()
    chapter, _page, _layer, _raster, _text = _chapter()
    window._set_chapter(chapter, TileStore())

    class _Checked:
        @staticmethod
        def isChecked():
            return True

    class _AcceptedSettingsDialog:
        def __init__(self, settings, document, parent):
            del settings, document, parent
            self.document_override = _Checked()

        @staticmethod
        def exec():
            return QDialog.DialogCode.Accepted

        @staticmethod
        def apply_user_settings(settings):
            settings.grid_size_px = 72
            settings.grid_divisions = 3
            settings.grid_color = "#123456"
            settings.grid_opacity = 0.4
            settings.clamp()

        @staticmethod
        def document_grid():
            from comic_editor.core.models import GridSettings
            return GridSettings(
                size=96, divisions=6, color="#abcdef", opacity=0.6
            )

    monkeypatch.setattr(
        "comic_editor.ui.main_window.SettingsDialog", _AcceptedSettingsDialog
    )
    before_commands = len(window.canvas.command_stack._undo)
    try:
        window._edit_settings()
        assert window.settings.grid_size_px == 72
        assert chapter.grid_override_enabled is True
        assert chapter.grid.size == 96
        assert chapter.grid.divisions == 6
        assert len(window.canvas.command_stack._undo) == before_commands + 1
        assert window._dirty is True
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.grid_override_enabled is False
        window.canvas.command_stack.redo()
        assert window.canvas.chapter.grid_override_enabled is True
        assert window.canvas.chapter.grid.size == 96
    finally:
        window.deleteLater()


def test_contextual_raster_tools(qapp):
    chapter, page, layer, raster, text = _chapter()
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", text.object_id)
    assert not canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.set_selection("object", raster.object_id)
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    assert canvas.set_tool(ToolKind.RASTER_ERASER)


def test_legacy_page_scope_setting_does_not_limit_chapter_selection(qapp):
    chapter, page, layer, raster, text = _chapter()
    other = chapter.add_layer(page.layer_id, "Other")
    other_raster = chapter.add_object(other.layer_id, RasterObject(x=400, y=400))
    canvas = CanvasWidget(EditorSettings(page_scope_select=False))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", layer.layer_id)
    assert (
        canvas.hit_test_object(
            canvas.object_world_rect(other_raster.object_id).center()
        ) == other_raster.object_id
    )
    canvas.settings.page_scope_select = True
    assert (
        canvas.hit_test_object(
            canvas.object_world_rect(other_raster.object_id).center()
        ) == other_raster.object_id
    )


def test_object_select_hits_shape_border_and_activates_shape_edit(qapp):
    chapter, page, layer, raster, text = _chapter()
    other = chapter.add_layer(
        page.layer_id, "Border Shape",
        BoundGeometry.rectangle(400, 300, 200, 160),
    )
    canvas = CanvasWidget(EditorSettings(page_scope_select=True))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", page.layer_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.OBJECT_SELECT)

    hits = canvas.hit_test_entities(QPointF(400, 380))
    assert hits[0] == {"kind": "layer", "id": other.layer_id}
    assert all(
        hit != {"kind": "layer", "id": other.layer_id}
        for hit in canvas.hit_test_entities(QPointF(500, 380))
    )
    point = canvas.document_to_widget(QPointF(400, 380))
    canvas._tool_press(point, 1)
    assert canvas.selected_id == other.layer_id
    assert canvas.tool == ToolKind.SHAPE_EDIT


def test_object_select_checks_sibling_shape_before_promoting_to_page(qapp):
    chapter, page, selected, raster, text = _chapter()
    sibling = chapter.add_layer(
        page.layer_id, "Sibling",
        BoundGeometry.rectangle(400, 300, 200, 160),
    )
    canvas = CanvasWidget(EditorSettings(page_scope_select=True))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", selected.layer_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.OBJECT_SELECT)

    canvas._tool_press(canvas.document_to_widget(QPointF(400, 380)), 1)
    assert canvas.selected_id == sibling.layer_id
    assert canvas.tool == ToolKind.SHAPE_EDIT

    canvas.set_tool(ToolKind.OBJECT_SELECT)
    canvas._tool_press(canvas.document_to_widget(QPointF(850, 650)), 1)
    assert canvas.selected_id == page.layer_id


def test_mixed_shape_object_hits_preserve_front_to_back_order(qapp):
    chapter, page, layer, raster, text = _chapter()
    shape = chapter.add_layer(
        page.layer_id, "Shape",
        BoundGeometry.rectangle(400, 300, 200, 160),
    )
    overlay = chapter.add_object(
        page.layer_id,
        TextObject(
            name="Overlay", layout_mode="free",
            transform_quad=[
                (380, 340), (460, 340), (460, 420), (380, 420),
            ],
        ),
    )
    chapter.move_entity("object", overlay.object_id, page.layer_id, 0)
    canvas = CanvasWidget(EditorSettings(page_scope_select=True))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", page.layer_id, activate_default_tool=False)
    assert canvas.hit_test_entities(QPointF(400, 380))[:2] == [
        {"kind": "object", "id": overlay.object_id},
        {"kind": "layer", "id": shape.layer_id},
    ]


def test_shape_border_precedes_its_descendants_in_both_scopes(qapp):
    chapter = ChapterDocument()
    page = chapter.add_page()
    shape = chapter.add_layer(
        page.layer_id, "Parent Shape",
        BoundGeometry.rectangle(100, 100, 240, 240),
    )
    raster = chapter.add_object(
        shape.layer_id,
        RasterObject(
            x=50, y=50, interaction_rect=(0, 0, 300, 300)
        ),
    )
    text = chapter.add_object(
        shape.layer_id,
        TextObject(
            layout_mode="free",
            transform_quad=[
                (50, 50), (350, 50), (350, 350), (50, 350),
            ],
        ),
    )
    nested = chapter.add_layer(
        shape.layer_id, "Nested Shape",
        BoundGeometry.rectangle(100, 100, 240, 240),
    )
    canvas = CanvasWidget(EditorSettings(page_scope_select=True))
    canvas.set_document(chapter, TileStore())
    border = QPointF(102, 200)
    interior = QPointF(200, 200)

    canvas.set_selection("layer", page.layer_id, activate_default_tool=False)
    for scale in (0.5, 2.0):
        canvas.scale = scale
        hits = canvas.hit_test_entities(border)
        assert hits[:4] == [
            {"kind": "layer", "id": shape.layer_id},
            {"kind": "object", "id": raster.object_id},
            {"kind": "object", "id": text.object_id},
            {"kind": "layer", "id": nested.layer_id},
        ]
    assert canvas.hit_test_entities(interior)[0] == {
        "kind": "object", "id": raster.object_id,
    }

    canvas.settings.page_scope_select = False
    canvas.set_selection(
        "layer", shape.layer_id, activate_default_tool=False
    )
    hits = canvas.hit_test_entities(border)
    assert hits[0] == {"kind": "layer", "id": shape.layer_id}
    assert hits[1] == {"kind": "object", "id": raster.object_id}
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    canvas._tool_press(canvas.document_to_widget(border), 1)
    assert canvas.selected_id == shape.layer_id
    assert canvas.tool == ToolKind.SHAPE_EDIT


def test_shape_selection_from_outliner_uses_shape_edit(qapp):
    chapter, page, layer, raster, text = _chapter()
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", layer.layer_id)
    assert canvas.tool == ToolKind.SHAPE_EDIT


def test_hierarchy_model_exposes_tree_without_arrows(qapp):
    chapter, page, layer, raster, text = _chapter()
    model = HierarchyModel(chapter)
    assert model.rowCount() == 1
    page_index = model.index(0, 0)
    assert model.rowCount(page_index) == 1
    layer_index = model.index(0, 0, page_index)
    assert model.rowCount(layer_index) == 2


def test_hierarchy_allows_objects_to_drop_directly_on_page(qapp):
    chapter, page, layer, raster, text = _chapter()
    model = HierarchyModel(chapter)
    page_index = model.index(0, 0)
    layer_index = model.index(0, 0, page_index)
    raster_index = model.index(0, 0, layer_index)
    mime = model.mimeData([raster_index])
    mutations = []
    model.mutationCommitted.connect(
        lambda before, after, label: mutations.append(label)
    )
    assert model.canDropMimeData(
        mime, Qt.MoveAction, -1, 0, page_index
    )
    assert model.dropMimeData(
        mime, Qt.MoveAction, -1, 0, page_index
    )
    assert raster.parent_layer_id == page.layer_id
    assert mutations == ["Reorder hierarchy"]


def test_outliner_reset_preserves_expansion_and_selection_by_entity_id(qapp):
    window = MainWindow()
    chapter, page, first, raster, text = _chapter()
    second = chapter.add_layer(page.layer_id, "Second")
    chapter.add_object(second.layer_id, RasterObject(name="Second raster"))
    window._set_chapter(chapter, TileStore())
    try:
        for layer_id in (page.layer_id, first.layer_id, second.layer_id):
            index = window.hierarchy_model.index_for_entity("layer", layer_id)
            window.tree.setExpanded(index, True)
        selected = window.hierarchy_model.index_for_entity(
            "object", text.object_id
        )
        window.tree.selectionModel().select(
            selected,
            QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
        )

        chapter.move_entity("object", text.object_id, second.layer_id, 0)
        window.hierarchy_model.rebuild()
        assert all(
            window.tree.isExpanded(
                window.hierarchy_model.index_for_entity("layer", layer_id)
            )
            for layer_id in (page.layer_id, first.layer_id, second.layer_id)
        )
        rows = window.tree.selectionModel().selectedRows(0)
        assert len(rows) == 1
        assert window.hierarchy_model.item_for_index(rows[0]).entity_id == text.object_id

        chapter.move_entity("object", text.object_id, first.layer_id, 1)
        window.hierarchy_model.rebuild()
        rows = window.tree.selectionModel().selectedRows(0)
        assert window.hierarchy_model.item_for_index(rows[0]).entity_id == text.object_id
    finally:
        window.deleteLater()


def test_text_labels_are_content_derived_until_custom_rename(qapp):
    chapter, page, layer, raster, text = _chapter()
    text.name = "Legacy serialized name"
    text.text = "  Hello \n   wide webcomic world  "
    model = HierarchyModel(chapter)
    page_index = model.index(0, 0)
    layer_index = model.index(0, 0, page_index)
    text_index = model.index(1, 0, layer_index)
    assert model.data(text_index, Qt.DisplayRole) == "Hello wide webco"
    assert model.flags(text_index) & Qt.ItemIsEditable
    assert model.setData(text_index, "Renamed", Qt.EditRole)
    assert text.name == "Renamed"
    assert text.custom_name is True
    assert model.data(text_index, Qt.DisplayRole) == "Renamed"


def test_text_settings_ribbon_layout_visibility_and_quad_restore(qapp):
    window = MainWindow()
    chapter, page, layer, raster, text = _chapter()
    text.text = "Inspector label content"
    original_quad = list(text.transform_quad)
    window._set_chapter(chapter, TileStore())
    try:
        window.canvas.set_selection("object", text.object_id)
        controls = window.text_object_controls
        assert not hasattr(window, "inspector")
        assert window.ribbon.current_key() == "tool_settings"
        assert not window.text_object_group.isHidden()
        assert not window.text_typography_group.isHidden()
        assert not window.text_layout_group.isHidden()
        assert not hasattr(controls, "transform_mode")
        assert controls.margin.isHidden()

        controls.layout_mode.setCurrentIndex(
            controls.layout_mode.findData("strict")
        )
        assert not controls.margin.isHidden()
        assert text.transform_quad == original_quad

        controls._set_alignment("right", "bottom")
        assert (text.horizontal_alignment, text.vertical_alignment) == (
            "right", "bottom"
        )
        controls.layout_mode.setCurrentIndex(
            controls.layout_mode.findData("free")
        )
        assert text.transform_quad == original_quad
        assert controls.margin.isHidden()
    finally:
        window.deleteLater()


def test_new_text_and_raster_insert_above_selected_object(qapp):
    window = MainWindow()
    chapter, page, layer, raster, text = _chapter()
    window._set_chapter(chapter, TileStore())
    try:
        window.canvas.set_selection("object", text.object_id)
        before_ids = set(chapter.objects)
        selected_index = next(
            index for index, reference in enumerate(layer.children)
            if reference.entity_id == text.object_id
        )
        window._add_text()
        new_text_id = next(iter(set(chapter.objects) - before_ids))
        assert window.canvas.selected_id == new_text_id
        assert window.canvas.tool == ToolKind.TEXT_EDIT
        assert window.canvas.has_active_text_edit()
        assert window.canvas._text_selection_range() == [0, 4]
        assert chapter.objects[new_text_id].parent_layer_id == layer.layer_id
        assert layer.children[selected_index].entity_id == new_text_id
        assert layer.children[selected_index + 1].entity_id == text.object_id
        window.canvas.command_stack.undo()
        assert new_text_id not in window.canvas.chapter.objects
        window.canvas.command_stack.redo()
        assert new_text_id in window.canvas.chapter.objects

        chapter = window.canvas.chapter
        layer = chapter.layers[layer.layer_id]
        raster = chapter.objects[raster.object_id]
        window.canvas.set_selection("object", raster.object_id)
        before_ids = set(chapter.objects)
        selected_index = next(
            index for index, reference in enumerate(layer.children)
            if reference.entity_id == raster.object_id
        )
        window._add_raster()
        window.canvas._create_raster_from_world_rect(
            (40, 40), (120, 120)
        )
        new_raster_id = next(iter(set(chapter.objects) - before_ids))
        assert (
            chapter.objects[new_raster_id].parent_layer_id
            == layer.layer_id
        )
        assert layer.children[selected_index].entity_id == new_raster_id
        assert layer.children[selected_index + 1].entity_id == raster.object_id
    finally:
        window.deleteLater()


def test_tablet_mode_does_not_hide_toolbars(qapp):
    window = MainWindow()
    window.tablet_mode.setChecked(True)
    assert window.tool_toolbar.isVisibleTo(window)
    assert window.hierarchy_dock.isVisibleTo(window)
    window.deleteLater()


def test_closed_window_unregisters_application_event_filter(qapp):
    window = MainWindow()
    assert window._application_event_filter_installed
    window._dirty = False
    window.close()
    qapp.processEvents()
    assert not window._application_event_filter_installed
    window.deleteLater()
