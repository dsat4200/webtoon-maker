"""Persistent text color spans and shared canvas/effect rendering."""
from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTextCursor

from comic_editor.core.assets import extract_asset, instantiate_asset
from comic_editor.core.models import BoundGeometry, ChapterDocument, OutlineModifier, TextObject, object_from_dict
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.text_styles import (
    apply_text_color, normalized_color_runs, qt_position_to_text_index,
    replace_text_range, text_color_at, text_index_to_qt_position, text_indexes_to_qt_positions,
)
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


def test_partial_color_overwrites_only_selection_and_whole_color_clears_runs():
    obj = TextObject(text="abcdefgh")
    assert apply_text_color(obj, 1, 6, "#f00")
    apply_text_color(obj, 3, 7, "#0000ff")
    apply_text_color(obj, 4, 5, obj.text_color)
    assert [text_color_at(obj, index) for index in range(8)] == [
        "#FF111111", "#FFFF0000", "#FFFF0000", "#FF0000FF",
        "#FF111111", "#FF0000FF", "#FF0000FF", "#FF111111",
    ]
    assert apply_text_color(obj, 0, len(obj.text), "#80ff8800")
    assert obj.text_color == "#80FF8800" and obj.color_runs == []
    assert not apply_text_color(obj, 0, len(obj.text), "#80ff8800")
    empty = TextObject(text="")
    apply_text_color(empty, 0, 0, "#00ff00")
    replace_text_range(empty, 0, 0, "Green")
    assert text_color_at(empty, 2) == "#FF00FF00"


def test_insert_replace_and_delete_retain_neighboring_colors():
    obj = TextObject(text="abcdef")
    apply_text_color(obj, 1, 3, "#f00")
    apply_text_color(obj, 4, 6, "#00f")
    replace_text_range(obj, 2, 2, "XY")
    assert obj.text == "abXYcdef"
    assert obj.color_runs == [
        {"start": 1, "end": 5, "color": "#FFFF0000"},
        {"start": 6, "end": 8, "color": "#FF0000FF"},
    ]
    replace_text_range(obj, 3, 7, "Z")
    assert obj.text == "abXZf"
    assert [text_color_at(obj, index) for index in range(5)] == [
        "#FF111111", "#FFFF0000", "#FFFF0000", "#FFFF0000", "#FF0000FF",
    ]
    replace_text_range(obj, 1, 4, "")
    assert obj.text == "af"
    assert obj.color_runs == [{"start": 1, "end": 2, "color": "#FF0000FF"}]


def test_loaded_colors_normalize_and_legacy_text_retains_ink():
    obj = object_from_dict({"id": "styled", "type": "text", "text": "123456", "text_color": "#123", "color_runs": [
        {"start": -2, "end": 99, "color": "#f00"},
        {"start": 2, "end": 4, "color": "#123"},
        {"start": "invalid", "end": 3, "color": "#fff"}, None,
    ]})
    assert obj.text_color == "#FF112233"
    assert obj.color_runs == [
        {"start": 0, "end": 2, "color": "#FFFF0000"},
        {"start": 4, "end": 6, "color": "#FFFF0000"},
    ]
    assert object_from_dict({"id": "legacy", "type": "text"}).text_color == "#FF111111"
    # Snapshots must not share mutable range dictionaries with live objects.
    snapshot = obj.to_dict()
    obj.color_runs[0]["end"] = 1
    assert snapshot["color_runs"][0]["end"] == 2


def test_sparse_normalization_merges_equal_runs_without_per_character_storage():
    text = "a" * 1_000_000
    runs = normalized_color_runs(text, [
        {"start": 1, "end": 500_000, "color": "#f00"},
        {"start": 500_000, "end": 999_999, "color": "#f00"},
    ], "#111")
    assert runs == [{"start": 1, "end": 999_999, "color": "#FFFF0000"}]


def test_unicode_span_indexes_and_qt_positions_round_trip():
    text = "A\U0001f600B\U0001f680C"
    assert [text_index_to_qt_position(text, index) for index in range(6)] == [0, 1, 3, 4, 6, 7]
    for index in range(len(text) + 1):
        assert qt_position_to_text_index(text, text_index_to_qt_position(text, index)) == index
    assert qt_position_to_text_index(text, 2) == 1
    assert text_indexes_to_qt_positions(text, [5, 1, 0, 2, 4, 3]) == dict(enumerate([0, 1, 3, 4, 6, 7]))


def test_colors_persist_to_disk_and_copy_between_chapters_as_assets(tmp_path):
    repository = SeriesRepository(tmp_path / "Colors")
    series = repository.create("Colors")
    chapter, tiles = repository.create_chapter(series, "Source")
    page_id = chapter.root_page_ids[0]
    obj = chapter.add_object(page_id, TextObject(text="Keep color", layout_mode="free"))
    apply_text_color(obj, 0, len(obj.text), "#6600AAFF")
    apply_text_color(obj, 5, 10, "#FFFF4400")
    repository.save_chapter(chapter, tiles)
    loaded, loaded_tiles = repository.load_chapter(chapter.chapter_id)
    assert loaded.objects[obj.object_id].to_dict() == obj.to_dict()
    manifest, asset_tiles = extract_asset(loaded, loaded_tiles, "object", obj.object_id, "Text")
    target = ChapterDocument()
    target_page = target.add_page()
    _, root_id, _ = instantiate_asset(manifest, asset_tiles, target, TileStore(), target_page.layer_id, 20, 20)
    cloned = target.objects[root_id]
    assert cloned.object_id != obj.object_id
    assert (cloned.text, cloned.text_color, cloned.color_runs) == (obj.text, obj.text_color, obj.color_runs)


@pytest.fixture
def color_canvas(qapp, text_outline_font_family):
    chapter = ChapterDocument(height=260)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 260))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore())
    canvas._color_test_font = text_outline_font_family
    yield canvas
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def add_colored_text(canvas, mode="free"):
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    obj = canvas.chapter.add_object(page.layer_id, TextObject(
        text="HHHH", font_family=canvas._color_test_font, font_size=64,
        x=30, y=40, width=300, height=140, margin=0,
        layout_mode=mode, horizontal_alignment="left", vertical_alignment="top",
        text_color="#FF0000FF",
    ))
    apply_text_color(obj, 0, 2, "#FFFF0000")
    return obj


def pixels(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


def preview(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


@pytest.mark.parametrize("mode", ["strict", "free"])
def test_preview_and_saved_pixels_have_only_selected_glyph_colors(color_canvas, mode, tmp_path):
    obj = add_colored_text(color_canvas, mode)
    image = preview(color_canvas)
    rgba = pixels(image)
    red = (rgba == [255, 0, 0, 255]).all(axis=-1)
    blue = (rgba == [0, 0, 255, 255]).all(axis=-1)
    assert red.sum() > 100 and blue.sum() > 100
    assert np.where(red)[1].max() < np.where(blue)[1].min()
    path = tmp_path / "colored-text.png"
    assert image.save(str(path))
    np.testing.assert_array_equal(pixels(QImage(str(path))), rgba)
    apply_text_color(obj, 0, len(obj.text), "#8000FF00")
    rgba = pixels(preview(color_canvas))
    assert rgba[..., 3].max() == 128
    assert np.all(rgba[rgba[..., 3] > 0][:, :3] == [0, 255, 0])


def test_unicode_document_format_targets_characters_after_emoji(color_canvas):
    obj = add_colored_text(color_canvas)
    obj.text, obj.color_runs = "A\U0001f600BC", []
    apply_text_color(obj, 2, 3, "#FFFF0000")
    document = color_canvas._text_document(obj, 300)
    for index, expected in enumerate(("#ff0000ff", "#ff0000ff", "#ffff0000", "#ff0000ff")):
        cursor = QTextCursor(document)
        cursor.setPosition(text_index_to_qt_position(obj.text, index))
        cursor.setPosition(text_index_to_qt_position(obj.text, index + 1), QTextCursor.KeepAnchor)
        assert cursor.charFormat().foreground().color().name(QColor.HexArgb) == expected


@pytest.mark.parametrize("text,size", [("HHHH", 64), ("Before update!", 54), ("One\nTwo \u05e9\u05dc\u05d5\u05dd", 54)])
def test_editing_overlay_suppresses_explicit_foregrounds_without_losing_selection(color_canvas, text, size):
    obj = add_colored_text(color_canvas)
    obj.text, obj.font_size = text, size
    color_canvas.set_selection("object", obj.object_id)
    color_canvas.start_text_edit()
    color_canvas._text_caret_visible = False
    color_canvas._text_selection_anchor = color_canvas._text_cursor_position = 0
    image = QImage(1080, 260, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    color_canvas._draw_text_object(painter, obj, editing_overlay=True)
    painter.end()
    assert not pixels(image)[..., 3].any()
    color_canvas._text_cursor_position = 2
    painter = QPainter(image)
    color_canvas._draw_text_object(painter, obj, editing_overlay=True)
    painter.end()
    rgba = pixels(image)
    assert rgba[..., 3].max() == 102
    assert rgba[..., 3].sum() > 1000


def test_outline_preserves_styled_ink_and_color_changes_refresh_cached_sources(color_canvas):
    obj = add_colored_text(color_canvas)
    source = pixels(preview(color_canvas))
    modifier = OutlineModifier(thickness=4, color="#FF00FF00")
    color_canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    outlined = pixels(preview(color_canvas))
    opaque = source[..., 3] == 255
    np.testing.assert_array_equal(outlined[opaque], source[opaque])
    old_keys = set(color_canvas._modifier_source_cache)
    apply_text_color(obj, 0, 1, "#FFFF00FF")
    updated = pixels(preview(color_canvas))
    assert set(color_canvas._modifier_source_cache) != old_keys
    assert not np.array_equal(updated, outlined)
    color_canvas._modifier_source_cache.clear()
    color_canvas._modifier_source_cache_bytes = 0
    color_canvas._modifier_render_cache.clear()
    color_canvas._modifier_render_cache_bytes = 0
    np.testing.assert_array_equal(pixels(preview(color_canvas)), updated)
