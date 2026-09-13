"""Show-on-top settings and crowns keep selection, visibility and undo intact."""
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPointingDevice, QTabletEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, ImageObject,
    RasterObject, TextObject, VectorDrawingObject,
)
from comic_editor.core.tiles import TileStore
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.tree_model import (
    EyeVisibilityDelegate, HierarchyModel, MaskOnlyRowDelegate, SoloRowDelegate,
)


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.main_window.save_settings", lambda _: None)
    widget = MainWindow()
    chapter = ChapterDocument(height=500)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 500))
    group = chapter.add_layer(page.layer_id, "Group", BoundGeometry.rectangle(20, 20, 500, 300))
    for cls in (RasterObject, VectorDrawingObject, ImageObject, TextObject, ColorFillGradientObject):
        chapter.add_object(group.layer_id, cls(name=cls.__name__))
    widget._set_chapter(chapter, TileStore())
    yield widget
    widget.autosave_timer.stop()
    widget.canvas._effect_jobs.cancel()
    widget._dirty = False
    widget.hide()
    widget.deleteLater()


@pytest.mark.parametrize("kind", ["page", "layer", "RasterObject", "VectorDrawingObject",
                                  "ImageObject", "TextObject", "ColorFillGradientObject"])
def test_settings_flag_exact_target_with_one_undo_and_preserve_eye(window, kind):
    chapter = window.chapter
    if kind in {"page", "layer"}:
        target = next(layer for layer in chapter.layers.values() if layer.is_page == (kind == "page"))
        entity_kind, identifier = "layer", target.layer_id
        checkbox = window.layer_settings.show_on_top
    else:
        target = next(obj for obj in chapter.objects.values() if obj.name == kind)
        entity_kind, identifier = "object", target.object_id
        checkbox = window.selection_settings.object_show_on_top
    target.visible = False
    window.canvas.set_selection(entity_kind, identifier)
    window.selection_settings.refresh()
    assert not checkbox.isHidden()
    assert not checkbox.isChecked()
    assert window.layer_settings.show_on_top.isHidden() == (entity_kind == "object")
    assert window.selection_settings.object_show_on_top.isHidden() == (entity_kind == "layer")
    before = window.canvas.chapter.to_dict()
    revision = window.canvas.command_stack.revision
    checkbox.click()
    assert target.show_on_top and not target.visible
    assert window.canvas.command_stack.revision == revision+1
    assert sum(item.show_on_top for item in window.canvas.chapter.layers.values()) == (entity_kind == "layer")
    assert sum(item.show_on_top for item in window.canvas.chapter.objects.values()) == (entity_kind == "object")
    window.canvas.command_stack.undo()
    assert window.canvas.chapter.to_dict() == before
    assert not checkbox.isChecked()
    window.canvas.command_stack.redo()
    assert checkbox.isChecked()


@pytest.mark.parametrize("column,delegate", [(0, EyeVisibilityDelegate), (1, SoloRowDelegate),
                                            (2, MaskOnlyRowDelegate)])
def test_flagged_selected_rows_paint_bright_red_in_every_column(window, column, delegate):
    obj = next(iter(window.chapter.objects.values()))
    obj.show_on_top = True
    model = window.hierarchy_model
    model.set_current_entity("object", obj.object_id)
    model.set_solo_entities({("object", obj.object_id)})
    index = model.index_for_entity("object", obj.object_id).siblingAtColumn(column)
    assert index.data(HierarchyModel.ShowOnTopRole)
    assert index.data(Qt.BackgroundRole) == QColor("#f53346")
    assert "crown" in index.data(Qt.ToolTipRole)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 300, 28)
    option.state = QStyle.State_Enabled | QStyle.State_Selected | QStyle.State_Active
    image = QImage(300, 28, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    delegate(window.tree).paint(painter, option, index)
    painter.end()
    assert image.pixelColor(200, 14) == QColor("#f53346")
    if column == 0:
        # The crown's wide lower bar occupies a point outside a five-pointed
        # star, including when this row also belongs to the solo selection.
        center = EyeVisibilityDelegate.star_rect(option.rect).center()
        assert image.pixelColor(center+QPoint(4, 4)) == QColor("#ffe14a")
        assert EyeVisibilityDelegate.eye_rect(option.rect, index).left() == 24


@pytest.mark.parametrize("input_type", ["mouse", "tablet"])
def test_crown_click_removes_only_show_on_top_and_preserves_current_selection(window, qapp, input_type):
    left, right = list(window.chapter.objects.values())[:2]
    left.show_on_top = True
    window.canvas.set_solo_entities({("object", left.object_id)})
    window.canvas.set_selection("object", right.object_id)
    window._refresh_hierarchy()
    window.resize(1280, 900)
    window.show()
    window.tree.expandAll()
    qapp.processEvents()
    model = window.hierarchy_model
    index = model.index_for_entity("object", left.object_id)
    crown = EyeVisibilityDelegate.star_rect(window.tree.visualRect(index))
    revision = window.canvas.command_stack.revision
    viewport = window.tree.viewport()
    if input_type == "mouse":
        QTest.mouseClick(viewport, Qt.LeftButton, pos=crown.center())
    else:
        local = QPointF(crown.center())
        global_position = QPointF(viewport.mapToGlobal(crown.center()))
        for event_type, pressure, buttons in [(QEvent.TabletPress, .5, Qt.LeftButton),
                                               (QEvent.TabletRelease, 0., Qt.NoButton)]:
            event = QTabletEvent(event_type, QPointingDevice.primaryPointingDevice(),
                                 local, global_position, pressure, 0, 0, 0., 0., 0.,
                                 Qt.NoModifier, Qt.LeftButton, buttons)
            assert window._handle_solo_star_event(viewport, event)
    assert not window.canvas.chapter.objects[left.object_id].show_on_top
    assert window.canvas.command_stack.revision == revision+1
    assert window.canvas.solo_entities == {("object", left.object_id)}
    assert window.canvas.selected_id == right.object_id
    assert left.visible
    window.canvas.command_stack.undo()
    assert window.canvas.chapter.objects[left.object_id].show_on_top
    assert window.canvas.selected_id == right.object_id


def test_eye_next_to_non_solo_crown_keeps_its_hit_target(window, qapp):
    obj = next(iter(window.chapter.objects.values()))
    obj.show_on_top = True
    window.canvas.set_selection("object", obj.object_id)
    window.resize(1280, 900)
    window.show()
    window.tree.expandAll()
    qapp.processEvents()
    index = window.hierarchy_model.index_for_entity("object", obj.object_id)
    assert not index.data(HierarchyModel.SoloRole)
    eye = EyeVisibilityDelegate.eye_rect(window.tree.visualRect(index), index)
    QTest.mouseClick(window.tree.viewport(), Qt.LeftButton, pos=eye.center())
    obj = window.canvas.chapter.objects[obj.object_id]
    assert not obj.visible
    assert obj.show_on_top
    assert window.canvas.solo_entities == set()


@pytest.mark.parametrize("other_row", [False, True])
def test_show_on_top_keeps_pending_text_edit_separate_from_property_undo(window, other_row):
    text = next(obj for obj in window.chapter.objects.values() if isinstance(obj, TextObject))
    other = next(iter(window.chapter.objects.values()))
    other.show_on_top = other_row
    text.text = "abc"
    window.canvas.set_selection("object", text.object_id)
    window.canvas.command_stack.clear()
    window.canvas._begin_text_session(text)
    window.canvas._text_cursor_position = window.canvas._text_selection_anchor = 3
    QTest.keyClicks(window.canvas, "x")
    assert text.text == "abcx"
    if other_row:
        window._disable_show_on_top("object", other.object_id)
    else:
        window.selection_settings.object_show_on_top.click()
    assert len(window.canvas.command_stack._undo) == 2
    window.canvas.command_stack.undo()
    restored = window.canvas.chapter.objects[text.object_id]
    assert restored.text == "abcx" and not restored.show_on_top
    assert window.canvas.chapter.objects[other.object_id].show_on_top == other_row
    window.canvas.command_stack.undo()
    assert window.canvas.chapter.objects[text.object_id].text == "abc"
