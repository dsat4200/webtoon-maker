"""Whole-number brush controls preserve untouched imports and narrow layouts."""
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractSpinBox

from comic_editor.core.brushes import BrushDefinition, BrushDynamics
from comic_editor.core.settings import EditorSettings
from comic_editor.ui.brush_controls import BrushControls, BrushSettingsDialog


def precise_brush():
    return BrushDefinition(size=17.23456789, opacity=.234567891,
                           dual=BrushDefinition(size=5.76543219, opacity=.876543219),
                           dual_link_size=True,
                           dynamics={"opacity": BrushDynamics(pressure=True, minimum=.123456789)})


@pytest.mark.parametrize("secondary", [False, True])
def test_settings_sliders_preserve_no_edit_precision_and_have_no_arrows(qapp, secondary):
    brush = precise_brush().dual if secondary else precise_brush()
    dialog = BrushSettingsDialog(brush, allow_dual=not secondary)
    for key in ("size", "opacity"):
        control = dialog.controls[key]
        assert control.buttonSymbols() == QAbstractSpinBox.NoButtons
        assert control._brush_slider.minimum() == 0
        assert control._brush_slider.maximum() == (200 if key == "size" else 100)
        assert control.decimals() == 0
        assert control.singleStep() == 1
    assert dialog.controls["opacity"].suffix() == " %"
    assert dialog.result_definition().to_dict() == brush.to_dict()
    dialog.close()


def test_settings_sliders_and_typed_values_update_live_preview_and_linked_child(qapp):
    brush = precise_brush()
    dialog = BrushSettingsDialog(brush)
    size = dialog.controls["size"]
    opacity = dialog.controls["opacity"]
    size._brush_slider.setValue(200)
    assert size.value() == 200
    assert dialog.preview._definition.size == 200
    size.setValue(34.5)
    assert size.value() == 35
    assert dialog.result_definition().dual.size == pytest.approx(brush.dual.size * 35 / brush.size)
    opacity._brush_slider.setValue(34)
    assert opacity.value() == 34
    assert dialog.result_definition().opacity == .34
    assert dialog.preview._definition.opacity == .34
    assert dialog.result_definition().dynamics == brush.dynamics
    assert dialog.result_definition().dual.opacity == brush.dual.opacity
    dialog.close()


def test_sidebar_keyboard_and_refresh_do_not_round_the_other_value(qapp):
    brush = precise_brush()
    settings = EditorSettings(brush_presets=[brush.to_dict()], active_brush_id=brush.id,
                              brush_size_px=brush.size, brush_opacity=brush.opacity)
    controls = BrushControls(settings)
    changes = []
    controls.settingsChanged.connect(lambda: changes.append(True))
    controls.settingsChanged.connect(controls.refresh)
    controls.show()
    qapp.processEvents()
    assert controls.size.buttonSymbols() == QAbstractSpinBox.NoButtons
    assert controls.opacity.buttonSymbols() == QAbstractSpinBox.NoButtons
    controls.opacity_slider.setValue(42)
    assert settings.brush_opacity == .42
    assert settings.brush_size_px == brush.size
    settings.brush_opacity = brush.opacity
    controls.refresh()
    edits_before = len(changes)
    controls.size.setFocus()
    controls.size.selectAll()
    QTest.keyClicks(controls.size, "29.75")
    assert settings.brush_size_px == brush.size
    QTest.keyClick(controls.size, Qt.Key_Tab)
    assert settings.brush_size_px == 30
    assert settings.brush_opacity == brush.opacity
    assert controls.preview._definition.size == 30
    assert len(changes) > edits_before
    slider_position = controls.size_slider.value()
    edits_before = len(changes)
    controls.refresh()
    assert controls.size_slider.value() == slider_position
    assert len(changes) == edits_before
    assert settings.brush_opacity == brush.opacity
    assert settings.brush_presets[0] == brush.to_dict()
    controls.close()


def test_large_size_stays_editable_above_the_slider_range(qapp):
    brush = BrushDefinition(size=932.25)
    settings = EditorSettings(brush_presets=[brush.to_dict()], active_brush_id=brush.id,
                              brush_size_px=brush.size)
    controls = BrushControls(settings)
    assert controls.size_slider.maximum() == 200
    assert controls.size_slider.value() == 200
    assert controls.size.value() == 932
    assert settings.brush_size_px == 932.25
    controls.size.setValue(350.6)
    assert settings.brush_size_px == 351
    assert controls.size_slider.value() == 200
    controls.size_slider.setValue(120)
    assert settings.brush_size_px == 120
    assert settings.brush_presets[0] == brush.to_dict()
    controls.close()


def test_slider_drag_updates_live_but_publishes_settings_once_on_release(qapp):
    settings = EditorSettings()
    controls = BrushControls(settings)
    changes = []
    controls.settingsChanged.connect(lambda: changes.append(True))
    controls.opacity_slider.setSliderDown(True)
    for position in (25, 40, 61):
        controls.opacity_slider.setValue(position)
        assert settings.brush_opacity == pytest.approx(position / 100)
        assert controls.preview._definition.opacity == pytest.approx(position / 100)
    assert not changes
    controls.opacity_slider.setSliderDown(False)
    assert len(changes) == 1
    controls.close()


def test_main_window_slider_commit_saves_once_and_preserves_active_stroke(qapp, tmp_path, monkeypatch):
    from comic_editor.core import settings as settings_module
    from comic_editor.core.models import ChapterDocument, RasterObject
    from comic_editor.core.tiles import TileStore
    from comic_editor.ui import main_window
    from comic_editor.ui.canvas import ToolKind

    monkeypatch.setattr(settings_module, "settings_path", lambda: tmp_path / "slider-preferences.json")
    window = main_window.MainWindow()
    try:
        chapter = ChapterDocument()
        layer = chapter.add_page()
        raster = chapter.add_object(layer.layer_id, RasterObject())
        window._set_chapter(chapter, TileStore())
        canvas = window.canvas
        canvas.set_selection("object", raster.object_id)
        window._activate_tool(ToolKind.BRUSH)
        brush = precise_brush()
        window.settings.brush_presets = [brush.to_dict()]
        window.settings.active_brush_id = brush.id
        window.settings.brush_size_px = brush.size
        window.settings.brush_opacity = brush.opacity
        window.tool_settings_controls.refresh()
        window.show()
        qapp.processEvents()
        controls = window.tool_settings_controls.brush_page
        saves = []
        real_save = main_window.save_settings

        def saved(settings):
            saves.append((settings.brush_size_px, settings.brush_opacity))
            real_save(settings)

        monkeypatch.setattr(main_window, "save_settings", saved)
        canvas._begin_paint_brush(QPointF(30, 30), 1)
        active = canvas._paint_brush_stroke
        controls.opacity_slider.setSliderDown(True)
        for position in (30, 45, 61):
            controls.opacity_slider.setValue(position)
            assert window.settings.brush_opacity == pytest.approx(position / 100)
            assert controls.preview._definition.opacity == pytest.approx(position / 100)
            assert active.definition.opacity == brush.opacity
        assert saves == []
        controls.opacity_slider.setSliderDown(False)
        # Exercise the real connected MainWindow handler: clamp, persistence,
        # all-panel refresh and canvas refresh; only persistence is observed.
        assert saves == [(brush.size, .61)]
        assert canvas._paint_brush_stroke is active
        assert active.definition.opacity == brush.opacity
        assert window.settings.brush_size_px == brush.size
        canvas._continue_paint_brush(QPointF(90, 30), 1)
        canvas._finish_paint_brush()
        assert len(canvas.command_stack._undo) == 1

        controls.size.setFocus()
        controls.size.selectAll()
        QTest.keyClicks(controls.size, "29.75")
        assert len(saves) == 1 and window.settings.brush_size_px == brush.size
        QTest.keyClick(controls.size, Qt.Key_Tab)
        assert saves == [(brush.size, .61), (30, .61)]
        loaded = settings_module.load_settings()
        assert (loaded.brush_size_px, loaded.brush_opacity) == (30, .61)
        assert loaded.brush_presets[0] == brush.to_dict()
        canvas._begin_paint_brush(QPointF(30, 70), 1)
        assert canvas._paint_brush_stroke.definition.size == 30
        assert canvas._paint_brush_stroke.definition.opacity == .61
        assert canvas._paint_brush_stroke.definition.dual.size == pytest.approx(
            brush.dual.size * 30 / brush.size)
        canvas._finish_paint_brush()
    finally:
        window.autosave_timer.stop()
        window._autosave_jobs.shutdown()
        window.canvas._effect_jobs.cancel()
        window.deleteLater()


@pytest.mark.parametrize("width", [180, 220])
def test_slider_rows_fit_narrow_brush_sidebar(qapp, width):
    controls = BrushControls(EditorSettings())
    controls.setFixedWidth(width)
    controls.resize(width, 600)
    controls.show()
    qapp.processEvents()
    for widget in (controls.size, controls.opacity, controls.size_slider, controls.opacity_slider):
        top_left = widget.mapTo(controls, QPoint())
        assert top_left.x() >= 0
        assert top_left.x() + widget.width() <= width
        assert widget.width() >= 40
    controls.close()
