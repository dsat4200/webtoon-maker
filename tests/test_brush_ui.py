from __future__ import annotations

import gc
import base64
import io
import json
import time
from dataclasses import replace

import pytest

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QImage, QPainterPath
from PySide6.QtTest import QTest

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushTexture, BrushTip, default_brushes
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, VectorDrawingObject
from comic_editor.core.settings import EditorSettings, load_settings, save_settings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.brush_controls import BrushControls, BrushSettingsDialog
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def make_canvas():
    chapter = ChapterDocument()
    page = chapter.add_page("Brush validation", BoundGeometry.rectangle(0, 0, 1080, 1080))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 100, 100)))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(640, 480)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", raster.object_id)
    return canvas, chapter, raster


def pixels(store, object_id):
    return {key: bytes(image.constBits()) for key, image in store.object_tiles(object_id).items()}


def render_preview(qapp,preview):
    from comic_editor.ui.brush_preview_queue import preview_queue
    preview.window().show()
    qapp.processEvents()
    preview._timer.stop()
    preview._render()
    queue=preview_queue()
    deadline=time.perf_counter()+5
    while time.perf_counter()<deadline:
        qapp.processEvents()
        active=queue.active is not None and queue._owner(queue.active) is preview
        if not active and id(preview) not in queue.pending:
            assert preview.pixmap() is not None and not preview.pixmap().isNull()
            return
        QTest.qWait(1)
    pytest.fail('Cooperative brush preview did not complete')


def test_brush_is_raster_only_and_keeps_selection_tool(qapp):
    canvas, chapter, raster = make_canvas()
    assert canvas.set_tool(ToolKind.BRUSH)
    second = chapter.add_object(raster.parent_layer_id, RasterObject())
    canvas.set_selection("object", second.object_id)
    assert canvas.tool == ToolKind.BRUSH
    vector = chapter.add_object(raster.parent_layer_id, VectorDrawingObject())
    canvas.set_selection("object", vector.object_id)
    assert not canvas.set_tool(ToolKind.BRUSH)
    canvas.set_selection("object", raster.object_id)
    canvas.active_tone_mask_id = "mask"
    assert not canvas.set_tool(ToolKind.BRUSH)
    canvas.active_tone_mask_id = ""
    canvas.close()


def test_brush_extends_frame_single_commit_and_exact_undo_redo(qapp):
    canvas, _chapter, raster = make_canvas()
    canvas.set_tool(ToolKind.BRUSH)
    canvas.settings.brush_size_px = 20
    frame_before = raster.interaction_rect
    committed = []
    canvas.documentChanged.connect(committed.append)
    canvas._tool_press(canvas.camera_transform().map(QPointF(80, 80)), 1)
    canvas._tool_move(canvas.camera_transform().map(QPointF(280, 140)), .8)
    assert not committed
    canvas._tool_release()
    assert len(committed) == 1
    assert QRectF(*raster.interaction_rect).contains(QPointF(280, 140))
    assert len(canvas.command_stack._undo) == 1
    after = pixels(canvas.tiles, raster.object_id)
    frame_after = raster.interaction_rect
    assert len(after) >= 2
    canvas.command_stack.undo()
    assert pixels(canvas.tiles, raster.object_id) == {}
    assert raster.interaction_rect == frame_before
    canvas.command_stack.redo()
    assert pixels(canvas.tiles, raster.object_id) == after
    assert raster.interaction_rect == frame_after
    canvas.close()


def test_brush_cancel_restores_pixels_frame_and_gc(qapp):
    canvas, _chapter, raster = make_canvas()
    canvas.set_tool(ToolKind.BRUSH)
    before_frame = raster.interaction_rect
    enabled = gc.isenabled()
    canvas._begin_paint_brush(QPointF(-40, -20), 1)
    assert canvas.tiles.content_bounds(raster.object_id) is not None
    canvas.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert not canvas._drawing and canvas._paint_brush_stroke is None
    assert canvas.tiles.content_bounds(raster.object_id) is None
    assert raster.interaction_rect == before_frame
    assert not canvas.command_stack.can_undo
    assert gc.isenabled() == enabled
    canvas.close()


def test_switching_target_finishes_captured_brush(qapp):
    canvas, chapter, raster = make_canvas()
    canvas.set_tool(ToolKind.BRUSH)
    second = chapter.add_object(raster.parent_layer_id, RasterObject())
    canvas._begin_paint_brush(QPointF(30, 30), 1)
    canvas.set_selection("object", second.object_id)
    assert canvas._paint_brush_stroke is None
    assert canvas.command_stack.can_undo
    assert canvas.tiles.content_bounds(raster.object_id) is not None
    assert canvas.tiles.content_bounds(second.object_id) is None
    canvas.command_stack.undo()
    assert canvas.tiles.content_bounds(raster.object_id) is None
    canvas.close()


def test_document_switch_rolls_back_uncommitted_brush(qapp):
    canvas, _chapter, raster = make_canvas()
    store = canvas.tiles
    frame = raster.interaction_rect
    canvas.set_tool(ToolKind.BRUSH)
    canvas._begin_paint_brush(QPointF(300, 300), 1)
    canvas.set_document(ChapterDocument(), TileStore())
    assert store.content_bounds(raster.object_id) is None
    assert raster.interaction_rect == frame
    assert not canvas._paint_brush_timer.isActive()
    canvas.close()


def test_brush_input_axes_and_stationary_timer(qapp, monkeypatch):
    canvas, _chapter, raster = make_canvas()
    canvas.settings.active_brush_id = "airbrush"
    canvas._device_supports_pressure = True
    canvas._paint_brush_input_axes = (24, -12, 45)
    canvas.set_tool(ToolKind.BRUSH)
    canvas._begin_paint_brush(QPointF(40, 40), .25)
    sample = canvas._paint_brush_sample
    assert (sample.pressure, sample.tilt_x, sample.tilt_y, sample.rotation) == (.25, 24, -12, 45)
    assert canvas._paint_brush_timer.isActive()
    before = pixels(canvas.tiles, raster.object_id)
    monkeypatch.setattr("comic_editor.ui.brush_features.time.monotonic", lambda: sample.time + .25)
    canvas._tick_paint_brush()
    assert pixels(canvas.tiles, raster.object_id) != before
    canvas._finish_paint_brush()
    assert not canvas._paint_brush_timer.isActive()
    canvas.close()


def test_brush_library_migrates_persists_and_is_separate_from_pencil(monkeypatch, tmp_path):
    from comic_editor.core import settings as settings_module
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "settings_path", lambda: path)
    path.write_text(json.dumps({"settings_version": 22, "active_pencil_preset": "Linear"}), encoding="utf-8")
    settings = load_settings()
    assert settings.settings_version == 23
    assert settings.hotkeys["brush"] == "Shift+B"
    custom = replace(default_brushes()[5], id="custom", name="Imported chain")
    settings.brush_presets.append(custom.to_dict())
    settings.active_brush_id = "custom"
    settings.brush_size_px = 48
    settings.brush_opacity = .72
    save_settings(settings)
    restored = load_settings()
    brush = restored.active_paint_brush()
    assert brush.tips == custom.tips
    assert (brush.size, brush.opacity) == (48, .72)
    assert restored.active_brush_preset().name == "Linear"


def test_detailed_settings_preserve_resources_and_curves(qapp):
    definition = replace(default_brushes()[5], dynamics={
        "size": BrushDynamics(pressure=True, pressure_curve=((0, 0), (.25, .7), (1, 1)))
    })
    dialog = BrushSettingsDialog(definition)
    dialog.controls["density"].setValue(.37)
    edited = dialog.result_definition()
    assert edited.density == .37
    assert edited.tips == definition.tips
    assert edited.dynamics["size"].pressure_curve == definition.dynamics["size"].pressure_curve
    assert definition.density == 1
    assert dialog.preview._timer.isActive()
    dialog.close()


def test_dynamics_curve_changes_live_preview_without_mutating_original(qapp):
    definition = default_brushes()[0]
    dialog = BrushSettingsDialog(definition)
    curve = ((0, .1), (.4, .9), (1, 1))
    dialog.dynamic_controls["size"]["pressure_curve"]._set_curve(curve)
    assert dialog.result_definition().dynamics["size"].pressure_curve == curve
    assert dialog.preview._definition.dynamics["size"].pressure_curve == curve
    assert definition.dynamics["size"].pressure_curve != curve
    dialog.close()


def test_post_correction_and_percentage_taper_update_live_preview(qapp):
    definition = replace(default_brushes()[0], taper_start=250, taper_end=175)
    dialog = BrushSettingsDialog(definition)
    dialog.controls["post_correction"].setValue(.65)
    units = dialog.controls["taper_mode"]
    units.setCurrentIndex(units.findData("percentage"))
    for key in ("taper_start", "taper_end"):
        assert dialog.controls[key].maximum() == 100
        assert dialog.controls[key].suffix() == " %"
    dialog.controls["taper_start"].setValue(12)
    dialog.controls["taper_end"].setValue(24)
    preview = dialog.preview._definition
    assert (preview.post_correction, preview.taper_mode, preview.taper_start, preview.taper_end) == (.65, "percentage", 12, 24)
    units.setCurrentIndex(units.findData("length"))
    assert dialog.controls["taper_start"].maximum() == 10000
    assert dialog.controls["taper_start"].suffix() == " px"
    assert definition.taper_start == 250
    dialog.close()


def test_brush_optional_modes_and_unavailable_controls_are_explicit(qapp):
    definition = replace(default_brushes()[0], correct_velocity=True,
                         dynamics={"angle": BrushDynamics(pressure=True)})
    dialog = BrushSettingsDialog(definition)
    assert not dialog.controls["correct_velocity"].isEnabled()
    assert "no effect" in dialog.controls["correct_velocity"].toolTip()
    assert dialog.result_definition().correct_velocity
    for key in ("flip_x", "flip_y"):
        combo = dialog.controls[key]
        index = combo.findData("reverse")
        assert combo.itemText(index) == "Reverse leg"
        combo.setCurrentIndex(index)
    particle = dialog.controls["particle_direction"]
    particle.setCurrentIndex(particle.findData("stroke"))
    taper = dialog.controls["taper_mode"]
    taper.setCurrentIndex(taper.findData("fade"))
    dialog.controls["taper_end"].setValue(75)
    assert not dialog.controls["taper_start"].isEnabled()
    assert dialog._ending_taper_label.text() == "Fade length"
    assert dialog.controls["taper_end"].suffix() == " px"
    pressure_curve = ((0, 0), (.5, .8), (1, 1))
    dialog.controls["global_pressure_curve"]._set_curve(pressure_curve)
    dialog.controls["ribbon"].setChecked(True)
    assert not dialog.controls["direction"].isEnabled()
    assert not dialog.dynamic_controls["angle"]["pressure"].isEnabled()
    assert not dialog.controls["mixing_mode"].isEnabled()
    assert not dialog.controls["spray"].isEnabled()
    assert not dialog.controls["continuous"].isEnabled()
    assert not dialog.ribbon_paint_note.isHidden()
    assert dialog.controls["angle"].isEnabled()
    assert not dialog.ribbon_dynamics_note.isHidden()
    result = dialog.result_definition()
    assert result.dynamics["angle"].pressure
    assert (result.flip_x, result.flip_y, result.particle_direction) == ("reverse", "reverse", "stroke")
    assert (result.taper_mode, result.taper_end) == ("fade", 75)
    assert result.global_pressure_curve == pressure_curve
    assert dialog.preview._definition.global_pressure_curve == pressure_curve
    dialog.controls["ribbon"].setChecked(False)
    assert dialog.controls["direction"].isEnabled()
    assert dialog.controls["mixing_mode"].isEnabled()
    assert dialog.dynamic_controls["angle"]["pressure"].isEnabled()
    assert dialog.ribbon_dynamics_note.isHidden()
    dialog.close()


def test_spray_angle_controls_keep_independent_and_combined_random_modes(qapp):
    definition = BrushDefinition(spray=True, particle_direction="fixed", particle_angle=15,
                                  particle_angle_random=.1)
    dialog = BrushSettingsDialog(definition)
    direction = dialog.controls["particle_direction"]
    assert direction.itemText(direction.findData("fixed")) == "Independent"
    assert direction.findData("whole_spray") >= 0
    direction.setCurrentIndex(direction.findData("center"))
    dialog.controls["particle_angle"].setValue(90)
    dialog.controls["particle_angle_random"].setValue(.35)
    result = dialog.result_definition()
    assert (result.particle_direction, result.particle_angle, result.particle_angle_random) == ("center", 90, .35)
    preview = dialog.preview._definition
    assert (preview.particle_direction, preview.particle_angle, preview.particle_angle_random) == ("center", 90, .35)
    assert (definition.particle_direction, definition.particle_angle, definition.particle_angle_random) == ("fixed", 15, .1)
    dialog.close()


def test_dynamics_selector_and_per_parameter_taper_keep_independent_values(qapp):
    definition = replace(default_brushes()[0], taper_minimum=.15,
                         taper_parameters=("size", "opacity"), taper_minima={"size": .1, "opacity": .6})
    dialog = BrushSettingsDialog(definition)
    assert dialog.dynamics_selector.count() == 22
    assert dialog.dynamics_selector.currentText() == "Brush size"
    assert dialog.dynamics_selector.itemText(dialog.dynamics_selector.findData("hue")) == "Hue variation"
    for channel in ("hue", "texture_density", "paint_amount"):
        index = dialog.dynamics_selector.findData(channel)
        dialog.dynamics_selector.setCurrentIndex(index)
        assert dialog.dynamics_stack.currentIndex() == index
        dialog.dynamic_controls[channel]["pressure"].setChecked(True)
        dialog.dynamic_controls[channel]["minimum"].setValue(.35)
    assert dialog.taper_enabled.isChecked()
    assert dialog.taper_minimum_editor.value() == .1
    dialog.taper_minimum_editor.setValue(.2)
    dialog.taper_target.setCurrentIndex(dialog.taper_target.findData("opacity"))
    assert dialog.taper_enabled.isChecked()
    assert dialog.taper_minimum_editor.value() == .6
    dialog.taper_minimum_editor.setValue(.7)
    dialog.taper_target.setCurrentIndex(dialog.taper_target.findData("density"))
    assert not dialog.taper_enabled.isChecked()
    assert dialog.taper_minimum_editor.value() == .15
    dialog.taper_enabled.setChecked(True)
    dialog.taper_minimum_editor.setValue(.4)
    edited = dialog.result_definition()
    assert edited.taper_parameters == ("size", "opacity", "density")
    assert edited.taper_minima == {"size": .2, "opacity": .7, "density": .4}
    assert edited.dynamics["hue"].minimum == edited.dynamics["texture_density"].minimum == .35
    assert "color_stretch" not in edited.dynamics  # Do not add inactive channels during a settings edit.
    for channel in ("size", "opacity", "density"):
        dialog.taper_target.setCurrentIndex(dialog.taper_target.findData(channel))
        dialog.taper_enabled.setChecked(False)
    assert dialog.result_definition().taper_parameters == ()
    assert dialog.preview._definition.taper_parameters == ()
    assert definition.taper_minima == {"size": .1, "opacity": .6}
    dialog.close()


def test_small_relative_particles_and_imported_precision_survive_settings_dialog(qapp):
    definition = replace(default_brushes()[0], particle_size_relative=True, particle_size=.0037,
                         size=27.34567, spacing=.00125, opacity=.333333333,
                         dynamics={"size": BrushDynamics(pressure=True, minimum=.123456789)})
    dialog = BrushSettingsDialog(definition)
    control = dialog.controls["particle_size"]
    assert control.minimum() == .001
    assert control.decimals() >= 4
    assert control.value() == .0037
    assert control.suffix() == " ×"
    edited = dialog.result_definition()
    for name in ("particle_size", "size", "spacing", "opacity"):
        assert getattr(edited, name) == getattr(definition, name)
    assert edited.dynamics["size"].minimum == .123456789
    control.setValue(.2)
    dialog.controls["particle_size_relative"].setChecked(False)
    assert control.value() == .2  # Changing units never scales the stored number.
    assert control.minimum() == .1 and control.suffix() == " px"
    assert dialog.result_definition().particle_size == .2
    dialog.close()


def test_dialog_preserves_linked_dual_size_unknown_texture_and_taper_order(qapp):
    definition = BrushDefinition(
        size=27.34567, dual=BrushDefinition(size=13.23456789), dual_link_size=True,
        texture=BrushTexture(mode="unrecognized-future-mode", angle=375.1234567),
        taper_parameters=("opacity", "size"), taper_minima={"opacity": .654321},
    )
    dialog = BrushSettingsDialog(definition)
    assert dialog.result_definition().to_dict() == definition.to_dict()
    settings = EditorSettings(brush_presets=[definition.to_dict()], active_brush_id=definition.id,
                              brush_size_px=definition.size)
    assert settings.active_paint_brush().dual.size == definition.dual.size
    assert dialog.texture_controls["mode"].currentData() == "unrecognized-future-mode"
    dialog.close()


def test_dialog_preserves_untouched_authored_name_whitespace(qapp):
    definition = BrushDefinition(name="Imported brush ")
    dialog = BrushSettingsDialog(definition)
    assert dialog.result_definition().to_dict() == definition.to_dict()
    dialog.name.setText("  Renamed brush  ")
    assert dialog.result_definition().name == "Renamed brush"
    dialog.name.setText(" ")
    assert dialog.result_definition().name == definition.name
    dialog.close()


def test_signed_color_offsets_roundtrip_and_edit_in_display_units(qapp):
    curve = ((0, .17), (.35, .83), (1, .95))
    dynamics = BrushDynamics(pressure=True, minimum=-.73456789,
                             pressure_curve=curve, tilt=True, tilt_minimum=-.4,
                             tilt_curve=curve, velocity=True, velocity_minimum=-.2,
                             velocity_curve=curve, random=-.65)
    definition = BrushDefinition(
        hue_shift=.034927777777, saturation_shift=-.423456789,
        luminosity_shift=.234567891, sub_color_amount=.356789123,
        color_change_target="sub",
        hue_jitter=.12, saturation_jitter=.23, luminosity_jitter=.34,
        sub_color_mix=.45, dynamics={"hue_shift": dynamics},
    )
    dialog = BrushSettingsDialog(definition)
    assert dialog.result_definition().to_dict() == definition.to_dict()
    assert dialog.controls["hue_shift"].minimum() == -360
    assert dialog.controls["hue_shift"].maximum() == 360
    assert dialog.controls["hue_shift"].suffix() == "°"
    assert dialog.controls["saturation_shift"].minimum() == -100
    assert dialog.controls["sub_color_amount"].minimum() == 0
    target = dialog.controls["color_change_target"]
    assert target.currentData() == "sub" and target.currentText() == "Secondary color"
    for channel in ("hue_shift", "saturation_shift", "luminosity_shift"):
        fields = dialog.dynamic_controls[channel]
        for key in ("minimum", "tilt_minimum", "velocity_minimum", "random"):
            assert fields[key].minimum() == -1
    for channel in ("hue", "saturation", "luminosity", "sub_color_amount"):
        assert dialog.dynamic_controls[channel]["minimum"].minimum() == 0
        assert dialog.dynamic_controls[channel]["random"].minimum() == 0
    dialog.controls["hue_shift"].setValue(-90)
    dialog.controls["saturation_shift"].setValue(-35)
    dialog.controls["luminosity_shift"].setValue(25)
    dialog.controls["sub_color_amount"].setValue(60)
    target.setCurrentIndex(target.findData("both"))
    fields = dialog.dynamic_controls["saturation_shift"]
    fields["pressure"].setChecked(True)
    fields["minimum"].setValue(-.8)
    fields["random"].setValue(-.5)
    edited = dialog.result_definition()
    assert (edited.hue_shift, edited.saturation_shift, edited.luminosity_shift,
            edited.sub_color_amount) == (-.25, -.35, .25, .6)
    assert (edited.hue_jitter, edited.saturation_jitter, edited.luminosity_jitter,
            edited.sub_color_mix) == (.12, .23, .34, .45)
    assert edited.dynamics["hue_shift"] == dynamics
    assert edited.color_change_target == "both"
    assert edited.dynamics["saturation_shift"].minimum == -.8
    assert edited.dynamics["saturation_shift"].random == -.5
    assert dialog.preview._definition.hue_shift == -.25
    assert definition.hue_shift == .034927777777
    dialog.close()


def test_brush_preview_uses_palette_colors_for_main_and_subcolor(qapp):
    from comic_editor.core.brush_preview import render_brush_preview
    controls = BrushControls(EditorSettings())
    controls.resize(220, 550)
    controls.set_preview_colors(QColor("red"), QColor("blue"))
    render_preview(qapp, controls.preview)
    first = QImage(controls.preview.pixmap().toImage())
    expected = render_brush_preview(controls.preview._definition, first.width(), first.height(),
                                    color=QColor("red"), sub_color=QColor("blue"))
    assert bytes(first.constBits()) == bytes(expected.constBits())
    controls.set_preview_colors(QColor("blue"), QColor("red"))
    render_preview(qapp, controls.preview)
    assert bytes(first.constBits()) != bytes(controls.preview.pixmap().toImage().constBits())
    assert controls.presets._color == QColor("blue")
    assert controls.presets._sub_color == QColor("red")
    controls.close()


def test_main_window_palette_changes_update_brush_preview(qapp):
    from comic_editor.ui.main_window import MainWindow
    window = MainWindow()
    try:
        page = window.tool_settings_controls.brush_page
        window.color_panel.set_colors("#FFFF0000", "#FF0000FF", emit=True)
        assert page.preview._color == QColor("red")
        assert page.preview._sub_color == QColor("blue")
        window.color_panel.set_active_slot("secondary")
        assert page.preview._color == QColor("blue")
        assert page.preview._sub_color == QColor("blue")
    finally:
        window.autosave_timer.stop()
        window._autosave_jobs.shutdown()
        window.canvas._effect_jobs.cancel()
        window.deleteLater()


def test_pending_brush_preview_waits_until_live_stroke_finishes(qapp, monkeypatch):
    from comic_editor.ui.main_window import MainWindow
    window = MainWindow()
    try:
        chapter = ChapterDocument()
        page = chapter.add_page()
        raster = chapter.add_object(page.layer_id, RasterObject())
        window._set_chapter(chapter, TileStore())
        canvas = window.canvas
        canvas.set_selection("object", raster.object_id)
        window._activate_tool(ToolKind.BRUSH)
        window.show()
        qapp.processEvents()
        preview = window.tool_settings_controls.brush_page.preview
        assert preview.isVisible()
        rendered = []
        monkeypatch.setattr(preview, "_request_render", lambda: rendered.append(True))
        canvas._begin_paint_brush(QPointF(30, 30), 1)
        preview._render_when_visible()
        assert rendered == []
        assert preview._timer.isActive()
        canvas._finish_paint_brush()
        preview._render_when_visible()
        assert rendered == [True]
    finally:
        window.autosave_timer.stop()
        window._autosave_jobs.shutdown()
        window.canvas._effect_jobs.cancel()
        window._dirty = False
        window.hide()
        window.deleteLater()


def test_dual_brush_enable_linked_sizes_and_child_depth(qapp):
    definition = default_brushes()[0]
    dialog = BrushSettingsDialog(definition)
    dialog.dual_enabled.setChecked(True)
    dialog.controls["dual_link_size"].setChecked(True)
    dialog.controls["dual_apply_rgb"].setChecked(True)
    dialog.controls["size"].setValue(definition.size * 2)
    result = dialog.result_definition()
    assert result.dual is not None
    assert result.dual.size == definition.size * 2
    assert result.dual_apply_rgb
    child = BrushSettingsDialog(result.dual, allow_dual=False)
    assert child.result_definition().dual is None
    assert not hasattr(child, "dual_enabled")
    for key in ("blending_mode", "watercolor_edge", "post_correction"):
        assert not child.controls[key].isEnabled()
    dialog.dual_enabled.setChecked(False)
    assert dialog.result_definition().dual is None
    child.close()
    dialog.close()


def test_brush_preview_changes_with_settings_and_is_not_document_paint(qapp):
    settings = EditorSettings()
    controls = BrushControls(settings)
    controls.resize(350, 400)
    render_preview(qapp, controls.preview)
    first = QImage(controls.preview.pixmap().toImage())
    settings.brush_opacity = .15
    controls.refresh()
    render_preview(qapp, controls.preview)
    second = controls.preview.pixmap().toImage()
    assert not first.isNull() and not second.isNull()
    assert bytes(first.constBits()) != bytes(second.constBits())
    assert controls.presets.count() == len(default_brushes())
    controls.close()


def test_main_window_routes_brush_to_live_settings(qapp):
    from comic_editor.ui.main_window import MainWindow
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page()
    raster = chapter.add_object(page.layer_id, RasterObject())
    vector = chapter.add_object(page.layer_id, VectorDrawingObject())
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("object", raster.object_id)
    assert window._activate_tool(ToolKind.BRUSH)
    assert window.tool_buttons[ToolKind.BRUSH].isEnabled()
    assert not window.tool_buttons[ToolKind.BRUSH].isHidden()
    assert window.tool_settings_controls.stack.currentWidget() is window.tool_settings_controls.brush_page
    window.canvas.set_selection("object", vector.object_id)
    assert window.tool_buttons[ToolKind.BRUSH].isHidden()
    assert not window._activate_tool(ToolKind.BRUSH)
    window.autosave_timer.stop()
    window.canvas._effect_jobs.cancel()
    window.deleteLater()


@pytest.mark.parametrize("width", [180, 220])
def test_brush_controls_fit_standard_narrow_sidebar(qapp, width):
    controls = BrushControls(EditorSettings())
    controls.setFixedWidth(width)
    controls.resize(width, 550)
    controls.show()
    qapp.processEvents()
    render_preview(qapp, controls.preview)
    for widget in (controls.presets, controls.size, controls.opacity, controls.preview,
                   controls.edit_button, controls.import_button, controls.duplicate_button):
        assert widget.geometry().left() >= 0
        assert widget.geometry().right() < width
    assert controls.preview.pixmap().width() <= controls.preview.width()
    controls.close()


def test_transparent_active_secondary_erases_only_selected_raster(qapp):
    canvas, chapter, raster = make_canvas()
    other = chapter.add_object(raster.parent_layer_id, RasterObject())
    for obj in (raster, other):
        canvas.tiles.paint_dab(obj.object_id, QPointF(50, 50), 80, QColor("blue"))
    other_before = pixels(canvas.tiles, other.object_id)
    original = pixels(canvas.tiles, raster.object_id)
    canvas.set_tool(ToolKind.BRUSH)
    canvas.primary_color = "#FFFF0000"
    canvas.secondary_color = "#00000000"
    canvas.active_color_slot = "secondary"
    canvas.settings.brush_size_px = 24
    canvas._begin_paint_brush(QPointF(50, 50), 1)
    canvas._finish_paint_brush()
    assert canvas.tiles.tile(raster.object_id, (0, 0)).pixelColor(50, 50).alpha() == 0
    assert pixels(canvas.tiles, other.object_id) == other_before
    canvas.command_stack.undo()
    assert pixels(canvas.tiles, raster.object_id) == original
    canvas.close()


def test_active_secondary_is_drawing_color_and_transparent_subcolor_is_not_eraser(qapp):
    from PIL import Image
    canvas, _chapter, raster = make_canvas()
    canvas.set_tool(ToolKind.BRUSH)
    canvas.primary_color, canvas.secondary_color = "#FF0000FF", "#FFFF0000"
    canvas.active_color_slot = "secondary"
    canvas._begin_paint_brush(QPointF(30, 30), 1)
    canvas._finish_paint_brush()
    assert canvas.tiles.tile(raster.object_id, (0, 0)).pixelColor(30, 30).name() == "#ff0000"
    material = Image.new("RGBA", (2, 1), "black")
    material.putpixel((1, 0), (255, 255, 255, 255))
    stream = io.BytesIO()
    material.save(stream, format="PNG")
    definition = BrushDefinition(id="dual-color-test", size=40, antialiasing=0,
        tips=(BrushTip("Two color", 2, 1, base64.b64encode(stream.getvalue()).decode(), "dual_color", "image"),))
    canvas.settings.brush_presets.append(definition.to_dict())
    canvas.settings.active_brush_id = definition.id
    canvas.settings.brush_size_px = 40
    canvas.tiles.paint_dab(raster.object_id, QPointF(80, 80), 80, QColor("blue"))
    canvas.active_color_slot = "primary"
    canvas.primary_color, canvas.secondary_color = "#FFFF0000", "#00FFFFFF"
    canvas._begin_paint_brush(QPointF(80, 80), 1)
    canvas._finish_paint_brush()
    image = canvas.tiles.tile(raster.object_id, (0, 0))
    assert image.pixelColor(70, 80).name() == "#ff0000"
    assert image.pixelColor(90, 80) == QColor("blue")
    canvas.close()


@pytest.mark.parametrize("slot", ["primary", "secondary"])
def test_transparent_color_erases_only_black_part_of_two_color_tip(qapp, slot):
    from PIL import Image
    material = Image.new("RGBA", (2, 1), "black")
    material.putpixel((1, 0), (255, 255, 255, 255))
    stream = io.BytesIO()
    material.save(stream, format="PNG")
    brush = BrushDefinition(id="two-color-erase", size=40, antialiasing=0,
        tips=(BrushTip("Two color", 2, 1, base64.b64encode(stream.getvalue()).decode(), "dual_color", "image"),))
    canvas, _chapter, raster = make_canvas()
    canvas.set_tool(ToolKind.BRUSH)
    canvas.settings.brush_presets.append(brush.to_dict())
    canvas.settings.active_brush_id = brush.id
    canvas.settings.brush_size_px = brush.size
    canvas.tiles.paint_dab(raster.object_id, QPointF(80, 80), 80, QColor("blue"))
    before = pixels(canvas.tiles, raster.object_id)
    canvas.primary_color, canvas.secondary_color = "#FFFF0000", "#FF00FF00"
    setattr(canvas, f"{slot}_color", "#00000000")
    canvas.active_color_slot = slot
    canvas._begin_paint_brush(QPointF(80, 80), 1)
    canvas._finish_paint_brush()
    image = canvas.tiles.tile(raster.object_id, (0, 0))
    assert image.pixelColor(70, 80).alpha() == 0
    assert image.pixelColor(90, 80) == QColor("blue")
    canvas.command_stack.undo()
    assert pixels(canvas.tiles, raster.object_id) == before
    canvas.close()


def test_secondary_drawing_color_paints_both_parts_of_two_color_tip(qapp):
    from PIL import Image
    material = Image.new("RGBA", (2, 1), "black")
    material.putpixel((1, 0), (255, 255, 255, 255))
    stream = io.BytesIO()
    material.save(stream, format="PNG")
    brush = BrushDefinition(id="two-color-secondary", size=40, antialiasing=0,
        tips=(BrushTip("Two color", 2, 1, base64.b64encode(stream.getvalue()).decode(), "dual_color", "image"),))
    canvas, _chapter, raster = make_canvas()
    canvas.set_tool(ToolKind.BRUSH)
    canvas.settings.brush_presets.append(brush.to_dict())
    canvas.settings.active_brush_id = brush.id
    canvas.settings.brush_size_px = brush.size
    canvas.primary_color, canvas.secondary_color = "#FFFF0000", "#FF00FF00"
    canvas.active_color_slot = "secondary"
    canvas._begin_paint_brush(QPointF(80, 80), 1)
    canvas._finish_paint_brush()
    image = canvas.tiles.tile(raster.object_id, (0, 0))
    assert image.pixelColor(70, 80) == QColor("#00ff00")
    assert image.pixelColor(90, 80) == QColor("#00ff00")
    canvas.close()


def test_brush_selection_clips_paint_and_erasure_and_freezes_path(qapp):
    canvas, _chapter, raster = make_canvas()
    canvas.set_tool(ToolKind.BRUSH)
    canvas.settings.brush_size_px = 24
    canvas.tiles.paint_dab(raster.object_id, QPointF(70, 40), 160, QColor("blue"), square=True)
    selected = QPainterPath()
    selected.addRect(QRectF(40, 0, 40, 100))
    canvas._drawing_selection_path = selected
    canvas.primary_color = "#FFFF0000"
    canvas._begin_paint_brush(QPointF(20, 40), 1)
    canvas._drawing_selection_path = QPainterPath()  # Frozen stroke clip survives UI changes.
    canvas._continue_paint_brush(QPointF(110, 40), 1)
    canvas._finish_paint_brush()
    image = canvas.tiles.tile(raster.object_id, (0, 0))
    assert image.pixelColor(55, 40).name() == "#ff0000"
    assert image.pixelColor(20, 40) == QColor("blue")
    assert image.pixelColor(100, 40) == QColor("blue")
    canvas._drawing_selection_path = selected
    canvas.primary_color = "#00000000"
    canvas._begin_paint_brush(QPointF(20, 40), 1)
    canvas._continue_paint_brush(QPointF(110, 40), 1)
    canvas._finish_paint_brush()
    image = canvas.tiles.tile(raster.object_id, (0, 0))
    assert image.pixelColor(55, 40).alpha() == 0
    assert image.pixelColor(20, 40) == QColor("blue")
    assert image.pixelColor(100, 40) == QColor("blue")
    canvas.close()


def test_brush_on_transformed_raster_uses_local_selection_coordinates(qapp):
    canvas, chapter, raster = make_canvas()
    raster.x, raster.y = 100, 200
    raster.transform_frame = (100, 200, 100, 100)
    raster.transform_quad = [(180, 280), (380, 240), (400, 460), (160, 480)]
    parent = chapter.layers[raster.parent_layer_id]
    parent.translate_x, parent.translate_y = 80, 40
    transform_before = (raster.transform_frame, list(raster.transform_quad))
    canvas.scale, canvas.rotation = 1.7, 35
    selection = QPainterPath()
    selection.addRect(QRectF(20, 20, 30, 40))
    canvas._drawing_selection_path = selection
    canvas.set_tool(ToolKind.BRUSH)
    canvas.settings.brush_size_px = 18
    for index, local in enumerate((QPointF(0, 40), QPointF(70, 40))):
        world = canvas._raster_world_point(raster, local)
        widget = canvas.camera_transform().map(world)
        (canvas._tool_press if index == 0 else canvas._tool_move)(widget, 1)
    canvas._tool_release()
    image = canvas.tiles.tile(raster.object_id, (0, 0))
    assert image.pixelColor(35, 40).alpha() > 0
    assert image.pixelColor(10, 40).alpha() == 0
    assert image.pixelColor(60, 40).alpha() == 0
    assert (raster.transform_frame, raster.transform_quad) == transform_before
    canvas.close()


def test_cancel_and_noop_brush_emit_no_document_changes_or_undo(qapp):
    canvas, _chapter, raster = make_canvas()
    canvas.set_tool(ToolKind.BRUSH)
    commits = []
    canvas.documentChanged.connect(commits.append)
    frame = raster.interaction_rect
    canvas._begin_paint_brush(QPointF(30, 30), 1)
    canvas._cancel_paint_brush()
    assert commits == []
    canvas.primary_color = "#00000000"
    canvas._begin_paint_brush(QPointF(400, 400), 1)  # Erasing empty space is a no-op.
    canvas._finish_paint_brush()
    assert commits == []
    assert not canvas.command_stack.can_undo
    assert raster.interaction_rect == frame
    canvas.close()


@pytest.mark.parametrize("transition", ["clear", "multiple", "tool"])
def test_brush_transaction_finishes_on_selection_and_tool_transitions(qapp, transition):
    canvas, chapter, raster = make_canvas()
    other = chapter.add_object(raster.parent_layer_id, RasterObject())
    canvas.set_tool(ToolKind.BRUSH)
    canvas._begin_paint_brush(QPointF(30, 30), 1)
    if transition == "clear":
        canvas.clear_selection()
    elif transition == "multiple":
        canvas.set_selection_set([("object", raster.object_id), ("object", other.object_id)])
    else:
        canvas.set_tool(ToolKind.RASTER_PENCIL)
    assert canvas._paint_brush_stroke is None
    assert len(canvas.command_stack._undo) == 1
    assert canvas.tiles.content_bounds(raster.object_id) is not None
    assert canvas.tiles.content_bounds(other.object_id) is None
    canvas.close()


def test_brush_save_finishes_contact_and_roundtrips_pixels(qapp, tmp_path):
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.ui.main_window import MainWindow
    repository = SeriesRepository(tmp_path / "brush-save")
    series = repository.create("Brush save")
    repository.create_chapter(series, "Page")
    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        canvas = window.canvas
        raster = next(obj for obj in canvas.chapter.objects.values() if isinstance(obj, RasterObject))
        canvas.set_selection("object", raster.object_id)
        canvas.set_tool(ToolKind.BRUSH)
        canvas._begin_paint_brush(QPointF(30, 30), 1)
        canvas._continue_paint_brush(QPointF(200, 70), .5)
        assert window.save()
        assert canvas._paint_brush_stroke is None
        loaded, tiles = repository.load_chapter(canvas.chapter.chapter_id)
        assert pixels(tiles, raster.object_id) == pixels(canvas.tiles, raster.object_id)
        assert loaded.objects[raster.object_id].interaction_rect == raster.interaction_rect
    finally:
        window.autosave_timer.stop()
        window._autosave_jobs.shutdown()
        window.canvas._effect_jobs.cancel()
        window.deleteLater()


def test_autosave_defers_active_brush_until_committed(qapp, tmp_path, monkeypatch):
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.ui.main_window import MainWindow
    repository = SeriesRepository(tmp_path / "brush-autosave")
    series = repository.create("Brush autosave")
    repository.create_chapter(series, "Page")
    window = MainWindow()
    submitted = []
    monkeypatch.setattr(window._autosave_jobs, "submit", submitted.append)
    try:
        assert window.open_series(repository.root)
        canvas = window.canvas
        raster = next(obj for obj in canvas.chapter.objects.values() if isinstance(obj, RasterObject))
        canvas.set_selection("object", raster.object_id)
        canvas.set_tool(ToolKind.BRUSH)
        window._mark_dirty(None)  # An earlier committed edit was waiting for recovery.
        canvas._begin_paint_brush(QPointF(30, 30), 1)
        window._autosave()
        assert not submitted
        assert window.autosave_timer.isActive()
        assert canvas._paint_brush_stroke is not None
        canvas._finish_paint_brush()
        window._autosave()
        assert len(submitted) == 1
        assert submitted[0].revision == window.active_session.edit_revision
    finally:
        window.autosave_timer.stop()
        window._autosave_jobs.shutdown()
        window.canvas._effect_jobs.cancel()
        window.deleteLater()


@pytest.mark.parametrize("selected_region", [False, True])
def test_clear_during_brush_contact_cannot_restore_cached_paint(qapp, selected_region):
    from comic_editor.ui.main_window import MainWindow
    window = MainWindow()
    try:
        chapter = ChapterDocument()
        page = chapter.add_page()
        raster = chapter.add_object(page.layer_id, RasterObject())
        store = TileStore()
        store.paint_dab(raster.object_id, QPointF(30, 30), 30, QColor("red"))
        original = pixels(store, raster.object_id)
        window._set_chapter(chapter, store)
        canvas = window.canvas
        canvas.set_selection("object", raster.object_id)
        canvas.set_tool(ToolKind.BRUSH)
        if selected_region:
            selection = QPainterPath()
            selection.addRect(QRectF(0, 0, 100, 100))
            canvas._drawing_selection_path = selection
        canvas._begin_paint_brush(QPointF(30, 30), 1)
        canvas._continue_paint_brush(QPointF(60, 30), 1)
        painted = pixels(store, raster.object_id)
        assert painted != original
        window._clear_canvas()
        assert canvas._paint_brush_stroke is None
        assert store.content_bounds(raster.object_id) is None
        canvas._continue_paint_brush(QPointF(90, 30), 1)
        canvas._finish_paint_brush()
        assert store.content_bounds(raster.object_id) is None
        assert len(canvas.command_stack._undo) == 2
        canvas.command_stack.undo()
        assert pixels(store, raster.object_id) == painted
        canvas.command_stack.undo()
        assert pixels(store, raster.object_id) == original
        canvas.command_stack.redo()
        assert pixels(store, raster.object_id) == painted
        canvas.command_stack.redo()
        assert store.content_bounds(raster.object_id) is None
    finally:
        window.autosave_timer.stop()
        window._autosave_jobs.shutdown()
        window.canvas._effect_jobs.cancel()
        window.deleteLater()


@pytest.mark.parametrize("interruption", ["application", "window", "ungrab"])
def test_focus_or_capture_loss_finishes_brush_and_stops_stationary_paint(qapp, interruption):
    from comic_editor.ui.main_window import MainWindow
    window = MainWindow()
    try:
        chapter = ChapterDocument()
        page = chapter.add_page()
        raster = chapter.add_object(page.layer_id, RasterObject())
        store = TileStore()
        window._set_chapter(chapter, store)
        canvas = window.canvas
        canvas.settings.active_brush_id = "airbrush"
        canvas.set_selection("object", raster.object_id)
        canvas.set_tool(ToolKind.BRUSH)
        canvas._pen_contact_active = canvas._tablet_tool_active = True
        canvas._begin_paint_brush(QPointF(30, 30), 1)
        assert canvas._paint_brush_timer.isActive()
        QCoreApplication.sendEvent(canvas, QEvent(QEvent.Leave))
        assert canvas._paint_brush_stroke is not None
        assert canvas._paint_brush_timer.isActive()
        if interruption == "application":
            QCoreApplication.sendEvent(qapp, QEvent(QEvent.ApplicationDeactivate))
        elif interruption == "window":
            QCoreApplication.sendEvent(window, QEvent(QEvent.WindowDeactivate))
        else:
            QCoreApplication.sendEvent(canvas, QEvent(QEvent.UngrabMouse))
        assert canvas._paint_brush_stroke is None
        assert not canvas._paint_brush_timer.isActive()
        assert not canvas._pen_contact_active and not canvas._tablet_tool_active
        assert len(canvas.command_stack._undo) == 1
        painted = pixels(store, raster.object_id)
        canvas._tick_paint_brush()
        canvas._continue_paint_brush(QPointF(90, 30), 1)
        assert pixels(store, raster.object_id) == painted
        canvas.command_stack.undo()
        assert store.content_bounds(raster.object_id) is None
        canvas.command_stack.redo()
        assert pixels(store, raster.object_id) == painted
    finally:
        window.autosave_timer.stop()
        window._autosave_jobs.shutdown()
        window.canvas._effect_jobs.cancel()
        window.deleteLater()


def test_project_switch_commits_brush_to_its_owner_and_restores_undo(qapp, tmp_path):
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.ui.main_window import MainWindow
    repositories = []
    for name in ("First", "Second"):
        repository = SeriesRepository(tmp_path / name)
        series = repository.create(name)
        repository.create_chapter(series, "Page")
        repositories.append(repository)
    window = MainWindow()
    try:
        assert window.open_series(repositories[0].root)
        first = window.active_session
        canvas = window.canvas
        raster = next(obj for obj in canvas.chapter.objects.values() if isinstance(obj, RasterObject))
        canvas.set_selection("object", raster.object_id)
        canvas.set_tool(ToolKind.BRUSH)
        canvas._begin_paint_brush(QPointF(30, 30), 1)
        assert window.open_series(repositories[1].root)
        assert canvas._paint_brush_stroke is None
        assert first.tiles.content_bounds(raster.object_id) is not None
        assert canvas.tiles.content_bounds(raster.object_id) is None
        window._activate_editor_session(first)
        assert canvas.tool == ToolKind.BRUSH
        assert canvas.command_stack.can_undo
        window._undo()
        assert first.tiles.content_bounds(raster.object_id) is None
    finally:
        window.autosave_timer.stop()
        window._autosave_jobs.shutdown()
        window.canvas._effect_jobs.cancel()
        window.deleteLater()


def test_device_packet_time_and_pen_orientation_survive_dispatch_delay(qapp, monkeypatch):
    canvas, _chapter, _raster = make_canvas()
    canvas.rotation = 90
    canvas.set_tool(ToolKind.BRUSH)

    class Packet:
        def __init__(self, milliseconds):
            self.milliseconds = milliseconds

        def timestamp(self):
            return self.milliseconds

        def xTilt(self):
            return 30

        def yTilt(self):
            return 0

        def rotation(self):
            return 90

    monkeypatch.setattr("comic_editor.ui.brush_features.time.monotonic", lambda: 100)
    canvas._capture_paint_brush_packet(Packet(1000), tablet=True)
    canvas._begin_paint_brush(QPointF(30, 30), 1)
    first = canvas._paint_brush_sample
    assert first.tilt_x == pytest.approx(0, abs=1e-6)
    assert first.tilt_y == pytest.approx(-30)
    assert min(first.rotation, abs(first.rotation - 360)) == pytest.approx(0, abs=1e-6)
    monkeypatch.setattr("comic_editor.ui.brush_features.time.monotonic", lambda: 140)
    canvas._capture_paint_brush_packet(Packet(1016), tablet=True)
    canvas._continue_paint_brush(QPointF(40, 30), 1)
    assert canvas._paint_brush_sample.time - first.time == pytest.approx(.016)
    canvas._finish_paint_brush()
    canvas.close()


def test_renderer_error_restores_transaction_without_dirty_signal(qapp, monkeypatch):
    from comic_editor.core.brush_raster import RasterBrushStroke
    canvas, _chapter, raster = make_canvas()
    canvas.set_tool(ToolKind.BRUSH)
    commits = []
    canvas.documentChanged.connect(commits.append)
    frame = raster.interaction_rect
    enabled = gc.isenabled()
    original = RasterBrushStroke.add

    def failing(stroke, sample):
        original(stroke, sample)
        raise ValueError("Simulated material failure")

    monkeypatch.setattr(RasterBrushStroke, "add", failing)
    canvas._begin_paint_brush(QPointF(30, 30), 1)
    with pytest.raises(ValueError, match="material failure"):
        canvas._continue_paint_brush(QPointF(400, 300), 1)
    assert not canvas._drawing and canvas._paint_brush_stroke is None
    assert canvas.tiles.content_bounds(raster.object_id) is None
    assert raster.interaction_rect == frame
    assert not canvas.command_stack.can_undo and commits == []
    assert gc.isenabled() == enabled
    canvas.close()
