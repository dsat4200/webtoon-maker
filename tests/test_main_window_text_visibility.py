"""Keep live text visible through real window editing and typography controls."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtTest import QTest

from comic_editor.core.models import BoundGeometry, ChapterDocument, OutlineModifier, TextObject
from comic_editor.core.tiles import TileStore
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def text_window(qapp, monkeypatch, text_outline_font_family):
    monkeypatch.setattr("comic_editor.ui.main_window.save_settings", lambda _: None)
    window = MainWindow()
    window.resize(1280, 900)
    window.settings.grid_overlay_visible = False
    chapter = ChapterDocument(height=480)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 480))
    page.fill_color, page.border_width = None, 0
    window._set_chapter(chapter, TileStore())
    window._test_font_family = text_outline_font_family
    window.show()
    qapp.processEvents()
    yield window
    window.autosave_timer.stop()
    window.canvas._effect_jobs.cancel()
    window.hide()
    window.deleteLater()


def create_text(window, effect, ignore_parent_mask=False):
    canvas, chapter = window.canvas, window.canvas.chapter
    parent = chapter.layers[chapter.root_page_ids[0]]
    if ignore_parent_mask:
        parent = chapter.add_layer(parent.layer_id, "Ignored clip", BoundGeometry.rectangle(10, 10, 30, 30))
        parent.fill_color, parent.border_width = None, 0
    if effect == "container":
        parent = chapter.add_layer(parent.layer_id, "Text", layer_kind="text_container")
        parent.ignore_parent_mask = ignore_parent_mask
    rect = QRectF(90, 100, 360, 140)
    obj = chapter.add_object(parent.layer_id, TextObject(
        text="Before update", font_family=window._test_font_family,
        font_size=40, layout_mode="free", margin=0,
        x=rect.x(), y=rect.y(), width=rect.width(), height=rect.height(),
        transform_quad=canvas._rect_quad(rect),
    ))
    obj.ignore_parent_mask = ignore_parent_mask and effect != "container"
    if effect != "plain":
        target = ("layer", parent.layer_id) if effect == "container" else ("object", obj.object_id)
        chapter.add_modifier(OutlineModifier(
            thickness=5, color="#FFFF0000", muted=effect == "muted",
            intensity=0 if effect == "zero" else 100,
        ), [target])
    window._hierarchy_changed()
    canvas.set_selection("object", obj.object_id)
    canvas.center_x, canvas.center_y, canvas.scale = 350, 240, 1
    canvas._invalidate_scene_cache()
    return obj


def capture(canvas, qapp):
    canvas._text_caret_timer.stop()
    canvas._text_caret_visible = False
    qapp.processEvents()
    return canvas.grab().toImage()


@pytest.mark.parametrize("effect", ["plain", "object", "container", "muted", "zero"])
@pytest.mark.parametrize("ignore_parent_mask", [False, True])
def test_window_typed_text_matches_committed_visibility(text_window, qapp, effect, ignore_parent_mask):
    window, canvas = text_window, text_window.canvas
    obj = create_text(window, effect, ignore_parent_mask)
    canvas.setFocus()
    resting = capture(canvas, qapp)
    assert canvas.start_text_edit()
    editing = capture(canvas, qapp)
    assert editing == resting
    QTest.keyClick(canvas, Qt.Key_A, Qt.ControlModifier)
    QTest.keyClicks(canvas, "Updated text")
    active = capture(canvas, qapp)
    assert canvas.has_active_text_edit()
    assert obj.text == "Updated text"
    assert active != editing
    canvas.commit_active_text_edit()
    assert capture(canvas, qapp) == active


@pytest.mark.parametrize("effect", ["plain", "object", "container", "muted", "zero"])
@pytest.mark.parametrize("ignore_parent_mask", [False, True])
def test_window_backspace_delete_and_reflow_stay_visible(text_window, qapp, effect, ignore_parent_mask):
    window, canvas = text_window, text_window.canvas
    obj = create_text(window, effect, ignore_parent_mask)
    canvas.setFocus()
    assert canvas.start_text_edit(select_all=True)
    QTest.keyClicks(canvas, "Several words wrap onto a second line")
    wrapped = capture(canvas, qapp)
    assert canvas._text_document(obj, obj.width).size().height() > obj.font_size*2
    # Remove the end in one ongoing editing session, changing line layout.
    for _ in range(20):
        QTest.keyClick(canvas, Qt.Key_Backspace)
    backspaced = capture(canvas, qapp)
    assert obj.text == "Several words wra"
    assert backspaced != wrapped
    canvas.commit_active_text_edit()
    assert capture(canvas, qapp) == backspaced

    assert canvas.start_text_edit()
    QTest.keyClick(canvas, Qt.Key_Home, Qt.ControlModifier)
    for _ in range(8):
        QTest.keyClick(canvas, Qt.Key_Delete)
    deleted = capture(canvas, qapp)
    assert obj.text == "words wra"
    assert deleted != backspaced
    canvas.commit_active_text_edit()
    assert capture(canvas, qapp) == deleted


@pytest.mark.parametrize("effect", ["plain", "object", "container"])
def test_window_font_size_update_keeps_text_visible(text_window, qapp, effect):
    window, canvas = text_window, text_window.canvas
    obj = create_text(window, effect)
    canvas.setFocus()
    assert canvas.start_text_edit()
    QTest.keyClick(canvas, Qt.Key_End)
    QTest.keyClicks(canvas, "!")
    initial = capture(canvas, qapp)
    spin = window.text_object_controls.font_size
    spin.setFocus()
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin.lineEdit(), "54")
    QTest.keyClick(spin.lineEdit(), Qt.Key_Return)
    updated = capture(canvas, qapp)
    assert obj.font_size == 54
    assert updated != initial
    canvas.setFocus()
    assert canvas.start_text_edit()
    active = capture(canvas, qapp)
    canvas.commit_active_text_edit()
    assert capture(canvas, qapp) == active
