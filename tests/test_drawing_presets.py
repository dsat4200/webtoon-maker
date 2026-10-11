"""Independent pencil/eraser presets and native pressure-sensitive erasure."""
import json

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QTransform
from PySide6.QtWidgets import QMessageBox

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.pressure import BrushPreset, PressureCurve
from comic_editor.core.settings import EditorSettings, load_settings, save_settings
from comic_editor.core.tiles import TileStore
from comic_editor.core.tiling import TilingGeometry
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.pencil_settings_dialog import PencilSettingsDialog, DrawingPresetSettingsDialog
from comic_editor.ui.settings_dialog import SettingsDialog
from comic_editor.ui.tool_ribbon_pages import ToolSettingsControls


def constant_curve(value):
    return PressureCurve(value, value, .5, value)


def test_legacy_shape_migrates_once_and_independent_shapes_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr("comic_editor.core.settings.settings_path", lambda: path)
    path.write_text(json.dumps({"settings_version": 23, "eraser_square": True}), encoding="utf-8")
    settings = load_settings()
    assert settings.active_eraser_preset_settings().shape == "square"
    settings.eraser_presets.append(BrushPreset(name="Soft", shape="circle").to_dict())
    settings.active_eraser_preset = "Soft"
    settings.eraser_square = False
    save_settings(settings)
    restored = load_settings()
    assert restored.active_eraser_preset_settings().shape == "circle"
    restored.active_eraser_preset = "Linear"
    assert restored.active_eraser_preset_settings().shape == "square"
    assert restored.active_brush_preset().shape == "circle"


def test_manager_can_rename_create_and_delete_any_preset(qapp, monkeypatch):
    dialog = PencilSettingsDialog([BrushPreset().to_dict()], "Linear", tool="Eraser")
    published = []
    dialog.committedPresets.connect(lambda items, name: published.append((items, name)))
    monkeypatch.setattr(dialog, "_name", lambda *args: "Soft")
    dialog._rename()
    assert [item["name"] for item in published[-1][0]] == ["Soft"]
    assert not dialog.delete_button.isEnabled()
    monkeypatch.setattr(dialog, "_name", lambda *args: "Square")
    dialog._create()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    dialog._delete()
    assert published[-1][1] == "Soft"
    dialog.deleteLater()


def test_both_tools_offer_identical_pressure_options_and_card_settings(qapp):
    preset = BrushPreset(name="Custom", shape="square", pressure_opacity=False,
                         density=1.37, stroke_start_ratio=.42, stroke_end_ratio=.71).to_dict()
    pencil = DrawingPresetSettingsDialog(preset, tool="Pencil")
    eraser = DrawingPresetSettingsDialog(preset, tool="Eraser")
    assert pencil.result_preset() == eraser.result_preset() == preset
    settings = EditorSettings(eraser_presets=[preset], active_eraser_preset="Custom")
    controls = ToolSettingsControls(settings)
    received = []
    controls.presetSettingsRequested.connect(lambda kind, name: received.append((kind, name)))
    controls.eraser_cards.cards["Custom"][1].click()
    assert received == [("eraser", "Custom")]
    assert controls.eraser_presets_button.text() == "Manage presets…"
    assert controls.eraser_shape.isHidden()
    assert controls.pencil_sizes_button.isHidden()
    pencil.deleteLater()
    eraser.deleteLater()
    controls.deleteLater()


def test_sizes_are_staged_in_settings_pencil_eraser_tab(qapp):
    settings = EditorSettings()
    dialog = SettingsDialog(settings)
    assert dialog.tabs.tabText(dialog.tabs.indexOf(dialog.pencil_eraser_tab)) == "Pencil/Eraser"
    dialog.drawing_sizes[("eraser", "small")].setValue(19)
    assert settings.eraser_size_px["small"] == 8
    dialog.apply_user_settings(settings)
    assert settings.eraser_size_px["small"] == 19
    dialog.deleteLater()


@pytest.mark.parametrize("width", [220, 350])
def test_pencil_and_eraser_cards_fit_narrow_sidebar(qapp, width):
    controls = ToolSettingsControls(EditorSettings())
    controls.setFixedSize(width, 600)
    controls.show()
    for tool, cards in (("raster_pencil", controls.pencil_cards), ("raster_eraser", controls.eraser_cards)):
        controls.set_context(tool, False, True)
        qapp.processEvents()
        for button, settings in cards.cards.values():
            assert settings.isVisible()
            assert controls.rect().contains(settings.mapTo(controls, settings.rect().bottomRight()))
    controls.hide()
    controls.deleteLater()


@pytest.mark.parametrize("format", [QImage.Format_ARGB32_Premultiplied, QImage.Format_RGBA64_Premultiplied])
@pytest.mark.parametrize("tiled", [False, True])
def test_partial_erasure_retains_original_format_and_exact_undo_source(format, tiled):
    store = TileStore(64)
    original = QImage(64, 64, format)
    original.fill(QColor.fromRgbF(.2314, .5432, .8765, .7891))
    original_bytes = bytes(original.constBits())
    store.set_tile("ink", (0, 0), original)
    before = {}
    context = (TilingGeometry("square", (32., 32.), 64.), QTransform()) if tiled else None
    store.paint_dab("ink", QPointF(32, 32), 12, QColor("black"), .3333,
                    erase=True, antialias=False, before=before, tiling=context)
    result = store.tile("ink", (0, 0))
    assert result.format() == format
    assert result.pixelColor(32, 32).alphaF() == pytest.approx(.7891 * (1-.3333), abs=2/255 if result.depth() == 32 else 3/65535)
    assert bytes(before[(0, 0)].constBits()) == original_bytes
    assert bytes(original.constBits()) == original_bytes
    for key, image in before.items():
        store.set_tile("ink", key, image)
    assert bytes(store.tile("ink", (0, 0)).constBits()) == original_bytes


@pytest.mark.parametrize("strength", [0., .3333])
def test_actual_eraser_contact_uses_its_pressure_preserves_precision_and_undo(qapp, strength):
    preset = BrushPreset(name="Soft", pressure_size=True, pressure_opacity=True,
                         size_curve=constant_curve(.5), opacity_curve=constant_curve(strength),
                         antialiasing=False, shape="square")
    settings = EditorSettings(snap_to_grid=False, predictive_ink=False,
        eraser_presets=[preset.to_dict()], active_eraser_preset="Soft", eraser_square=True)
    canvas = CanvasWidget(settings)
    chapter = ChapterDocument(width=128, height=128, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 128, 128))
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    store = TileStore()
    original = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    original.fill(QColor.fromRgbF(.1234, .4567, .7891, 1.))
    store.set_tile(obj.object_id, (0, 0), original)
    canvas.set_document(chapter, store)
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._device_supports_pressure = True
    canvas.primary_color = "#00000000"
    frame_before = obj.interaction_rect
    canvas._begin_stroke(QPointF(64, 64), 0.)
    canvas._end_stroke()
    result = store.tile(obj.object_id, (0, 0))
    assert result.pixelColor(64, 64).alphaF() == pytest.approx(1-strength, abs=2/65535)
    # Half-size square eraser reaches the square's corner, but no farther.
    assert result.pixelColor(70, 70).alphaF() == pytest.approx(1-strength, abs=2/65535)
    assert result.pixelColor(72, 64).alphaF() == 1.
    if strength:
        canvas.command_stack.undo()
        assert bytes(store.tile(obj.object_id, (0, 0)).constBits()) == bytes(original.constBits())
        canvas.command_stack.redo()
        assert bytes(store.tile(obj.object_id, (0, 0)).constBits()) == bytes(result.constBits())
    else:
        assert bytes(result.constBits()) == bytes(original.constBits())
        assert obj.interaction_rect == frame_before
        assert not canvas.command_stack.can_undo
    canvas.deleteLater()
