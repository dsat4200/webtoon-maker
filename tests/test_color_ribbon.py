from __future__ import annotations

import math

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QSizePolicy

from comic_editor.ui.color_picker import (
    ColorSwatchButton,
    ColorHistoryWidget,
    HsvAlphaPicker,
    PaletteEditorWidget,
    PrimarySecondaryColorPanel,
    canonical_argb,
    qcolor_from_argb,
)
from comic_editor.ui.ribbon import RibbonWidget


def test_canonical_argb_accepts_rgb_argb_and_qcolor():
    assert canonical_argb("#123456") == "#FF123456"
    assert canonical_argb("80123456") == "#80123456"
    assert canonical_argb(QColor(1, 2, 3, 4)) == "#04010203"
    assert canonical_argb("not a color", "#80AABBCC") == "#80AABBCC"
    assert qcolor_from_argb("#40010203").alpha() == 64


def test_color_history_grid_deduplicates_caps_and_emits(qapp):
    history = ColorHistoryWidget()
    history.set_colors([
        "#112233", "#FF112233", *(
            f"#FF{index:06X}" for index in range(30)
        ),
    ])
    assert history.colors()[0] == "#FF112233"
    assert len(history.colors()) == 24
    activated: list[str] = []
    history.colorActivated.connect(activated.append)
    history._buttons[0].click()
    assert activated == ["#FF112233"]


def test_hue_ring_pixels_follow_marker_angle_convention(qapp):
    picker = HsvAlphaPicker()
    picker.resize(260, 260)
    image = QImage(picker.size(), QImage.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    picker.render(image)
    outer, inner = picker.hue_ring_bounds()
    center = outer.center()
    radius = (outer.width() + inner.width()) / 4
    for hue in (0, 1 / 6, 1 / 3, 1 / 2, 2 / 3, 5 / 6):
        point = QPoint(
            round(center.x() + math.cos(hue * math.tau) * radius),
            round(center.y() - math.sin(hue * math.tau) * radius),
        )
        actual = image.pixelColor(point)
        expected = QColor.fromHsvF(hue, 1, 1)
        assert abs(actual.red() - expected.red()) <= 4
        assert abs(actual.green() - expected.green()) <= 4
        assert abs(actual.blue() - expected.blue()) <= 4


def test_primary_secondary_panel_hex_edit_copy_and_paste(qapp):
    panel = PrimarySecondaryColorPanel("#FF000000", "#FFFFFFFF")
    panel.hex_field.setText("#80112233")
    panel.hex_field.editingFinished.emit()
    assert panel.primary_color() == "#80112233"

    panel.hex_copy.click()
    assert qapp.clipboard().text() == "#80112233"
    qapp.clipboard().setText("AABBCC")
    panel.set_active_slot("secondary")
    panel.hex_paste.click()
    assert panel.secondary_color() == "#FFAABBCC"

    panel.hex_field.setText("not-a-color")
    panel.hex_field.editingFinished.emit()
    assert panel.hex_field.text() == "#FFAABBCC"
    assert panel.footer.minimumHeight() >= 62
    assert panel.hex_field.parentWidget() is panel.footer
    panel.resize(260, 300)
    panel.show()
    qapp.processEvents()
    assert panel.hex_field.geometry().center().y() < (
        panel.primary_well.geometry().center().y()
    )


def test_ribbon_context_pages_keep_stable_order(qapp):
    ribbon = RibbonWidget()
    color = ribbon.add_page("color", "Color")
    palette = color.add_group("Palette", minimum_width=220)
    ribbon.add_page("settings", "Tool Settings")
    ribbon.add_page("vector", "Vector Tools", visible=False)

    assert ribbon.page_keys() == ["color", "settings", "vector"]
    assert ribbon.page_keys(visible_only=True) == ["color", "settings"]
    assert ribbon.current_key() == "color"
    assert ribbon.orientation == Qt.Orientation.Horizontal
    assert color.horizontalScrollBarPolicy() == (
        Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    assert color.verticalScrollBarPolicy() == (
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert palette.minimumWidth() == 220
    assert palette.sizePolicy().horizontalPolicy() == (
        QSizePolicy.Policy.Minimum
    )
    assert palette.sizePolicy().verticalPolicy() == (
        QSizePolicy.Policy.Expanding
    )
    assert ribbon.select_page("settings")
    ribbon.set_page_visible("vector", True)
    assert ribbon.current_key() == "settings"
    assert ribbon.page_keys(visible_only=True) == [
        "color",
        "settings",
        "vector",
    ]
    assert ribbon.select_page("vector")
    ribbon.set_page_visible("vector", False)
    assert ribbon.current_key() == "color"
    assert not ribbon.select_page("vector")


def test_hsv_picker_alpha_strip_and_sv_square_emit_canonical_color(qapp):
    picker = HsvAlphaPicker("#FFFF0000")
    picker.resize(260, 260)
    picker.show()
    qapp.processEvents()
    emitted: list[str] = []
    finished: list[str] = []
    picker.colorChanged.connect(emitted.append)
    picker.interactionFinished.connect(finished.append)

    alpha = picker.alpha_rect()
    QTest.mousePress(
        picker,
        Qt.MouseButton.LeftButton,
        pos=QPoint(int(alpha.center().x()), int(alpha.bottom() - 1)),
    )
    QTest.mouseRelease(
        picker,
        Qt.MouseButton.LeftButton,
        pos=QPoint(int(alpha.center().x()), int(alpha.bottom() - 1)),
    )
    assert picker.color().alpha() < 8
    assert emitted[-1].startswith("#0")
    assert finished[-1] == picker.color_argb()

    square = picker.sv_rect()
    QTest.mouseClick(
        picker,
        Qt.MouseButton.LeftButton,
        pos=QPoint(int(square.right() - 1), int(square.top() + 1)),
    )
    color = picker.color()
    assert color.red() > 240
    assert color.green() < 20
    assert color.blue() < 20


def test_primary_secondary_panel_routes_picker_and_palette_colors(qapp):
    panel = PrimarySecondaryColorPanel("#FF000000", "#FFFFFFFF")
    events: list[tuple[str, str]] = []
    panel.colorChanged.connect(lambda slot, color: events.append((slot, color)))

    panel.apply_color("#80445566")
    assert panel.primary_color() == "#80445566"
    assert panel.secondary_color() == "#FFFFFFFF"
    assert events[-1] == ("primary", "#80445566")

    panel.set_active_slot("secondary")
    panel.apply_color("#40112233")
    assert panel.secondary_color() == "#40112233"
    assert panel.picker.color_argb() == "#40112233"
    assert events[-1] == ("secondary", "#40112233")


def test_primary_secondary_swap_button_updates_both_slots_once(qapp):
    panel = PrimarySecondaryColorPanel("#40112233", "#80445566")
    swaps: list[tuple[str, str]] = []
    changes: list[tuple[str, str]] = []
    panel.colorsSwapped.connect(
        lambda primary, secondary: swaps.append((primary, secondary))
    )
    panel.colorChanged.connect(
        lambda slot, color: changes.append((slot, color))
    )
    panel.swap_colors.click()
    assert panel.primary_color() == "#80445566"
    assert panel.secondary_color() == "#40112233"
    assert swaps == [("#80445566", "#40112233")]
    assert changes == []


def test_swatch_single_click_applies_and_double_click_only_edits(qapp):
    swatch = ColorSwatchButton("ink", "#80112233")
    swatch.resize(30, 30)
    swatch.show()
    applied: list[tuple[str, str]] = []
    edited: list[str] = []
    swatch.swatchActivated.connect(
        lambda swatch_id, color: applied.append((swatch_id, color))
    )
    swatch.editRequested.connect(edited.append)

    QTest.mouseDClick(swatch, Qt.MouseButton.LeftButton)
    QTest.qWait(qapp.styleHints().mouseDoubleClickInterval() + 100)
    assert edited == ["ink"]
    assert applied == []

    QTest.mouseClick(swatch, Qt.MouseButton.LeftButton)
    QTest.qWait(qapp.styleHints().mouseDoubleClickInterval() + 100)
    assert applied == [("ink", "#80112233")]


def test_palette_editor_uses_stable_ids_and_emits_mutations(qapp):
    editor = PaletteEditorWidget()
    editor.set_palettes(
        [
            {
                "id": "default",
                "name": "Default",
                "swatches": [
                    {"id": "black", "color": "#000000"},
                    {"id": "glass", "color": "#80112233"},
                ],
            },
            {
                "palette_id": "warm",
                "name": "Warm",
                "colors": [
                    {"swatch_id": "red", "argb": "#FFFF0000"},
                ],
            },
        ],
        "default",
    )
    assert editor.active_palette_id() == "default"
    assert editor.palettes()[0]["swatches"][1] == {
        "swatch_id": "glass",
        "color": "#80112233",
    }
    assert editor.remove_palette_button.isEnabled()

    selected: list[str] = []
    renamed: list[tuple[str, str]] = []
    removed: list[tuple[str, str]] = []
    editor.paletteSelectionChanged.connect(selected.append)
    editor.paletteNameChanged.connect(
        lambda palette_id, name: renamed.append((palette_id, name))
    )
    editor.removeSwatchRequested.connect(
        lambda palette_id, swatch_id: removed.append(
            (palette_id, swatch_id)
        )
    )

    editor.palette_combo.setCurrentIndex(1)
    assert selected == ["warm"]
    editor.name_edit.setText("Ink")
    editor.name_edit.editingFinished.emit()
    assert renamed == [("warm", "Ink")]
    assert editor.palette_combo.itemText(1) == "Ink"

    editor._swatch_buttons["red"].removeRequested.emit("red")
    assert removed == [("warm", "red")]
    assert editor.update_swatch_color("red", "#4000FF00")
    assert editor._swatch_buttons["red"].color_argb() == "#4000FF00"


def test_palette_editor_disables_removing_last_palette(qapp):
    editor = PaletteEditorWidget()
    editor.set_palettes(
        [{"id": "only", "name": "Only", "swatches": []}]
    )
    assert not editor.remove_palette_button.isEnabled()
