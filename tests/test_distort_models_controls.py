"""Distortion settings, persistence, compatibility and undoable control coverage."""
import copy
import uuid

import pytest
from PySide6.QtCore import QByteArray, QPoint, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QFileDialog, QLabel, QLineEdit, QPushButton, QSlider

from comic_editor.core.distort import DISTORT_TYPES, default_parameters, gizmo_kind
from comic_editor.core.models import (
    BlenderComicViewSourceDescriptor, BoundGeometry, ChapterDocument,
    ColorFillGradientObject, DistortModifier, ImageObject, ParameterMaskBinding,
    RasterObject, TextObject, VectorDrawingObject, modifier_from_dict,
)
from comic_editor.core.modifier_presets import apply_modifier_preset, preset_from_modifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.distort_controls import DistortControls, ShearCurveEditor
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.ribbon import RibbonPage


def document():
    chapter = ChapterDocument()
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 600, 600))
    layer = chapter.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(0, 0, 500, 500))
    objects = [chapter.add_object(layer.layer_id, obj) for obj in (
        RasterObject(x=30, y=40, interaction_rect=(0, 0, 120, 90)),
        ImageObject(pixel_width=120, pixel_height=90),
        ImageObject(pixel_width=120, pixel_height=90, source=BlenderComicViewSourceDescriptor(
            project_uuid=uuid.uuid4().hex, view_uuid=uuid.uuid4().hex)),
        VectorDrawingObject(), ColorFillGradientObject(), TextObject(),
    )]
    return chapter, layer, objects


@pytest.mark.parametrize("kind", DISTORT_TYPES)
def test_each_distort_roundtrips_settings_geometry_and_presets(kind):
    modifier = DistortModifier(modifier_type=kind, frame=(30, 40, 120, 90), center=(75, 85), radius=36,
                              intensity=68, parameter_masks={"intensity": ParameterMaskBinding(mask_id="mask", black_value=12, white_value=87)})
    encoded = modifier.to_dict()
    assert modifier_from_dict(encoded).to_dict() == encoded
    assert encoded["name"] == DISTORT_TYPES[kind]["name"]
    preset = preset_from_modifier("Distortion", modifier)
    target = DistortModifier(modifier_type=kind, name="Local", muted=True)
    loaded = apply_modifier_preset(target, preset)
    assert loaded.parameters == modifier.parameters
    assert loaded.frame == modifier.frame
    assert loaded.points == modifier.points
    assert loaded.name == "Local" and loaded.muted
    assert loaded.modifier_id == target.modifier_id
    assert loaded.parameter_masks == {}


@pytest.mark.parametrize("kind", DISTORT_TYPES)
def test_distort_target_guards_add_link_and_document_reload(kind):
    chapter, layer, objects = document()
    modifier = DistortModifier(modifier_type=kind)
    valid = [("object", obj.object_id) for obj in objects[:3]]
    chapter.add_modifier(modifier, valid)
    assert ChapterDocument.from_dict(chapter.to_dict()).modifier_target_ids(modifier.modifier_id) == valid
    for invalid in [("layer", layer.layer_id), *[("object", obj.object_id) for obj in objects[3:]]]:
        with pytest.raises(ValueError, match="raster drawings, images, and Blender renders only"):
            chapter.add_modifier(DistortModifier(modifier_type=kind), [invalid])
        with pytest.raises(ValueError, match="raster drawings, images, and Blender renders only"):
            chapter.set_modifier_targets(modifier.modifier_id, valid + [invalid])
        target = chapter.modifier_target(*invalid)
        target.modifier_ids.append(modifier.modifier_id)
        with pytest.raises(ValueError, match="raster drawings, images, and Blender renders only"):
            chapter.validate()
        target.modifier_ids.remove(modifier.modifier_id)


@pytest.mark.parametrize("values", [
    {"radius": 0}, {"frame": (0, 0, 0, 100)}, {"center": (float("nan"), 1)},
    {"parameters": {"angle": float("inf")}}, {"parameters": {"wrong": 1}},
    {"points": [(1, 2)], "source_points": []},
])
def test_invalid_distortion_data_is_rejected(values):
    with pytest.raises(ValueError):
        DistortModifier(**values).validate()


@pytest.fixture
def editor(qapp):
    chapter, layer, objects = document()
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", objects[0].object_id)
    controls = ModifierControls(canvas)
    yield canvas, controls, layer, objects
    canvas._effect_jobs.cancel()
    controls.deleteLater()
    canvas.deleteLater()


def test_distort_menu_only_appears_for_eligible_selections(editor):
    canvas, controls, layer, objects = editor
    assert [action.text() for action in controls.distort_menu.actions()] == [spec["name"] for spec in DISTORT_TYPES.values()]
    for obj in objects:
        canvas.set_selection("object", obj.object_id)
        controls.refresh()
        assert controls.distort_menu.menuAction().isVisible() == isinstance(obj, (RasterObject, ImageObject))
    canvas.set_selection("layer", layer.layer_id)
    controls.refresh()
    assert not controls.distort_menu.menuAction().isVisible()


@pytest.mark.parametrize("kind", DISTORT_TYPES)
def test_each_distort_creates_usable_card_and_world_geometry(editor, kind):
    canvas, controls, _layer, objects = editor
    controls.add_modifier(kind)
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    bounds = canvas.entity_world_rect("object", objects[0].object_id)
    assert modifier.frame == pytest.approx((bounds.x(), bounds.y(), bounds.width(), bounds.height()))
    assert modifier.center == pytest.approx((bounds.center().x(), bounds.center().y()))
    assert modifier.radius == min(bounds.width(), bounds.height()) / 2
    assert controls._cards[modifier.modifier_id].findChild(QComboBox, "distortChoice_interpolation") is not None
    assert controls._cards[modifier.modifier_id].findChild(QComboBox, "distortChoice_edges") is not None


def test_number_control_changes_parameters_in_one_undoable_action(editor, monkeypatch):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_twirl")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    changes = []
    monkeypatch.setattr(canvas, "push_model_change", lambda before, after, label: changes.append((before, after, label)))
    value = controls.findChild(QDoubleSpinBox, "distortValue_angle")
    value.setValue(45)
    assert modifier.parameters["angle"] == 45
    assert len(changes) == 1
    restored = ChapterDocument.from_dict(changes[0][0])
    assert restored.modifiers[modifier.modifier_id].parameters["angle"] == 0


def test_mesh_dimensions_reset_grid_atomically_and_sync_preserves_sources(editor, monkeypatch):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_mesh_warp")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    changes = []
    monkeypatch.setattr(canvas, "push_model_change", lambda before, after, label: changes.append((before, after)))
    controls.findChild(QDoubleSpinBox, "distortValue_columns").setValue(5)
    assert modifier.parameters["columns"] == 5
    assert len(modifier.points) == len(modifier.source_points) == 20
    assert len(changes) == 1
    modifier.points[0] = (.2, .2)
    controls.refresh()
    controls.findChild(QPushButton, "distortSynchronize").click()
    assert modifier.points == modifier.source_points
    assert len(changes) == 2


def test_shear_curve_add_drag_and_remove_are_persisted(editor, qapp):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_shear")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    curve = controls.findChild(ShearCurveEditor, "distortCurve_horizontal_curve")
    curve.resize(240, 125)
    curve.show()
    QTest.mouseClick(curve, Qt.LeftButton, pos=QPoint(120, 40))
    assert len(modifier.parameters["horizontal_curve"]) == 3
    assert modifier.parameters["horizontal_curve"][1][1] > 0
    encoded = modifier.to_dict()
    assert modifier_from_dict(encoded).parameters["horizontal_curve"] == modifier.parameters["horizontal_curve"]
    QTest.mouseClick(curve, Qt.RightButton, pos=QPoint(120, 40))
    assert modifier.parameters["horizontal_curve"] == [[0., 0.], [1., 0.]]


def test_shear_graph_draws_rendered_curve_and_commits_release_position(editor, monkeypatch):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_shear")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    modifier.parameters["horizontal_curve"] = [[0., 0.], [.5, 1.], [1., 0.]]
    curve = controls.findChild(ShearCurveEditor, "distortCurve_horizontal_curve")
    curve.resize(240, 125)
    curve.show()
    path = curve.curve_path()
    quarter_x = curve.area().left() + curve.area().width() * .25
    sample = min((path.elementAt(index) for index in range(path.elementCount())), key=lambda item: abs(item.x - quarter_x))
    assert sample.x == pytest.approx(quarter_x)
    assert sample.y == pytest.approx(curve.point_position((.25, .75)).y())
    assert sample.y != pytest.approx(curve.point_position((.25, .5)).y())
    changes = []
    monkeypatch.setattr(canvas, "push_model_change", lambda before, after, label: changes.append((before, after)))
    start = curve.point_position((.5, 1.)).toPoint()
    end = curve.point_position((.65, .4)).toPoint()
    QTest.mousePress(curve, Qt.LeftButton, pos=start)
    QTest.mouseRelease(curve, Qt.LeftButton, pos=end)
    position, offset = modifier.parameters["horizontal_curve"][1]
    assert position == pytest.approx((end.x() - curve.area().left()) / curve.area().width())
    assert offset == pytest.approx((curve.area().center().y() - end.y()) * 2 / curve.area().height())
    assert len(changes) == 1


def test_equations_switch_coordinate_identity_and_glitch_exposes_all_modes(editor):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_equations")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    combo = controls.findChild(QComboBox, "distortChoice_coordinates")
    combo.setCurrentIndex(combo.findData("polar"))
    assert modifier.parameters["x_expression"] == "r"
    assert modifier.parameters["y_expression"] == "t"
    controls.add_modifier("distort_glitch")
    combo = controls._cards[canvas.active_modifier_id].findChild(QComboBox, "distortChoice_mode")
    assert combo.count() == 23
    assert {combo.itemData(index) for index in range(combo.count())} >= {"shred", "warp", "data_blocks", "channel_flip"}


def test_displacement_map_is_embedded_and_presets_remain_portable(editor, tmp_path, monkeypatch):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_displace")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    filename = tmp_path / "map.png"
    image = QImage(18, 12, QImage.Format_RGBA8888)
    image.fill(QColor("#805040"))
    assert image.save(str(filename))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: (str(filename), ""))
    controls.findChild(QPushButton, "distortLoadMap").click()
    filename.unlink()
    restored = QImage.fromData(QByteArray.fromBase64(modifier.parameters["map_png"].encode("ascii")), "PNG")
    assert restored.size() == image.size()
    assert restored.pixelColor(0, 0) == image.pixelColor(0, 0)
    assert modifier.parameters["map_source"] == "embedded"
    preset = preset_from_modifier("Embedded", modifier)
    assert apply_modifier_preset(DistortModifier(modifier_type="distort_displace"), preset).parameters == modifier.parameters


def test_load_beneath_commits_captured_map(editor, monkeypatch):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_displace")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    calls = []
    monkeypatch.setattr(canvas, "capture_distort_beneath", lambda mid: (calls.append(mid), "encoded-test-map")[1], raising=False)
    controls.findChild(QPushButton, "distortLoadBeneath").click()
    assert calls == [modifier.modifier_id]
    assert modifier.parameters["map_png"] == "encoded-test-map"
    assert modifier.parameters["map_source"] == "embedded"


def test_equations_reject_invalid_input_without_changing_last_working_expression(editor):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_equations")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    expression = controls.findChild(QLineEdit, "distortExpression_x_expression")
    error = controls.findChild(QLabel, "distortExpressionError_x_expression")
    expression.setText("__import__('os')")
    expression.editingFinished.emit()
    assert modifier.parameters["x_expression"] == "x"
    assert error.text() and not error.isHidden()
    expression.setText("x + 10*sin(y/8)")
    expression.editingFinished.emit()
    assert modifier.parameters["x_expression"] == "x + 10*sin(y/8)"
    assert error.isHidden()


def test_lens_controls_offer_compatible_profiles_and_embed_import(editor, tmp_path, monkeypatch):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_lens_correction")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    camera = controls.findChild(QComboBox, "distortCameraProfile")
    assert camera.count() > 1
    camera.setCurrentIndex(1)
    assert modifier.parameters["camera_profile"] == camera.itemData(1)
    lens = controls._cards[modifier.modifier_id].findChild(QComboBox, "distortLensProfile")
    assert lens.count() > 1
    lens.setCurrentIndex(1)
    assert modifier.parameters["lens_profile"] == lens.itemData(1)
    xml = '''<lensdatabase version="2"><lens><maker>Custom</maker><model>Imported</model><mount>Custom</mount><cropfactor>1</cropfactor><calibration><distortion model="poly3" focal="50" k1="0.03"/></calibration></lens></lensdatabase>'''
    filename = tmp_path / "profile.xml"
    filename.write_text(xml, encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args, **_kwargs: (str(filename), ""))
    controls._cards[modifier.modifier_id].findChild(QPushButton, "distortImportLensProfile").click()
    filename.unlink()
    assert modifier.parameters["profile_xml"] == xml
    assert modifier.parameters["lens_profile"]
    assert modifier_from_dict(modifier.to_dict()).parameters == modifier.parameters


@pytest.mark.parametrize("mode", ["blast", "blast_color", "shred", "shred_color", "warp"])
def test_glitch_axis_strength_modes_match_video_controls(editor, mode):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_glitch")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    combo = controls._cards[modifier.modifier_id].findChild(QComboBox, "distortChoice_mode")
    combo.setCurrentIndex(combo.findData(mode))
    card = controls._cards[modifier.modifier_id]
    assert card.findChild(QDoubleSpinBox, "distortValue_amount") is None
    assert card.findChild(QDoubleSpinBox, "distortValue_offset_x") is None
    horizontal = card.findChild(QDoubleSpinBox, "distortValue_horizontal_strength")
    vertical = card.findChild(QDoubleSpinBox, "distortValue_vertical_strength")
    assert horizontal.value() == vertical.value() == 0
    assert horizontal.suffix() == vertical.suffix() == "%"
    horizontal.setValue(-1)
    vertical.setValue(-12)
    assert modifier.parameters["horizontal_strength"] == -1
    assert modifier.parameters["vertical_strength"] == -12
    if mode.startswith("shred"):
        spacing = card.findChild(QDoubleSpinBox, "distortValue_spacing")
        assert spacing.value() == 256
        assert spacing.maximum() == 1024


def test_radius_slider_has_full_range_coalesces_drag_and_refreshes_from_gizmo(editor, monkeypatch):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_twirl")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    card = controls._cards[modifier.modifier_id]
    radius = card.findChild(QDoubleSpinBox, "distortRadius")
    slider = card.findChild(QSlider, "distortRadiusSlider")
    changes = []
    monkeypatch.setattr(canvas, "push_model_change", lambda before, after, label: changes.append((before, after)))
    slider.setSliderDown(True)
    slider.setValue(4500)
    slider.setValue(5000)
    slider.setValue(5500)
    assert modifier.radius == radius.value() == pytest.approx(251.19)
    assert not changes
    slider.setSliderDown(False)
    assert len(changes) == 1
    slider.setValue(slider.minimum())
    assert modifier.radius == radius.minimum() == .01
    slider.setValue(slider.maximum())
    assert modifier.radius == radius.maximum() == 1000000.
    radius.setValue(100)
    assert slider.value() == 5000
    modifier.radius = 1000
    controls.refresh()
    card = controls._cards[modifier.modifier_id]
    assert card.findChild(QDoubleSpinBox, "distortRadius").value() == 1000
    assert card.findChild(QSlider, "distortRadiusSlider").value() == 6250


@pytest.mark.parametrize("mode", DISTORT_TYPES["distort_glitch"]["parameters"]["mode"]["choices"])
def test_glitch_center_hint_matches_only_spatial_modes(editor, mode):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_glitch")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    combo = controls._cards[modifier.modifier_id].findChild(QComboBox, "distortChoice_mode")
    combo.setCurrentIndex(combo.findData(mode))
    needs_center = mode in {"aberration_distortion", "ripple", "ripple_color"}
    assert gizmo_kind("distort_glitch", modifier.parameters) == ("center" if needs_center else "none")
    assert bool(controls._cards[modifier.modifier_id].findChild(QLabel, "distortCenterHint")) == needs_center


@pytest.mark.parametrize("mode", ["quantisation", "fuzz", "channel_flip"])
def test_glitch_separable_effects_expose_meaningful_channel_controls(editor, mode):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_glitch")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    modifier.parameters.update(mode=mode, channel_order="bgr")
    controls.refresh()
    card = controls._cards[modifier.modifier_id]
    count = card.findChild(QDoubleSpinBox, "distortValue_channels")
    order = card.findChild(QComboBox, "distortChoice_channel_order")
    if mode == "channel_flip":
        assert count is None
        assert order.currentText() == "Blue"
        assert [order.itemText(index) for index in range(order.count())] == ["Red", "Green", "Blue"]
        order.setCurrentIndex(1)
        assert modifier.parameters["channel_order"][0] == "g"
        assert card.findChild(QDoubleSpinBox, "distortValue_offset_x") is None
        assert card.findChild(QDoubleSpinBox, "distortValue_spacing") is None
    else:
        assert count.minimum() == 1 and count.maximum() == 3
        assert order.count() == 6
        assert card.findChild(QDoubleSpinBox, "distortValue_offset_x") is not None


@pytest.mark.parametrize("mode", ["aberration_distortion", "aberration_offset", "slice", "sawtooth", "ripple", "waves",
                                  "slice_color", "sawtooth_color", "ripple_color", "waves_color", "quantisation", "channel_flip"])
def test_glitch_deterministic_modes_hide_unused_seed(editor, mode):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier("distort_glitch")
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    modifier.parameters["mode"] = mode
    controls.refresh()
    assert controls._cards[modifier.modifier_id].findChild(QDoubleSpinBox, "distortValue_seed") is None


@pytest.mark.parametrize("kind,mode", [(kind, None) for kind in DISTORT_TYPES] + [
    ("distort_glitch", mode) for mode in DISTORT_TYPES["distort_glitch"]["parameters"]["mode"]["choices"]])
def test_distort_cards_fit_narrow_vertical_ribbon_and_scroll_to_last_control(editor, qapp, kind, mode):
    canvas, controls, _layer, _objects = editor
    page = RibbonPage("modifiers", "Modifiers", orientation=Qt.Vertical)
    page.setFixedSize(340, 400)
    group = page.add_group("Modifier Stack")
    group.add_widget(controls)
    page.show()
    try:
        controls.add_modifier(kind)
        if mode is not None:
            card = controls._cards[canvas.active_modifier_id]
            combo = card.findChild(QComboBox, "distortChoice_mode")
            combo.setCurrentIndex(combo.findData(mode))
        qapp.processEvents()
        card = controls._cards[canvas.active_modifier_id]
        assert page.groups_container.minimumSizeHint().width() <= page.viewport().width()
        last = (card.findChild(QSlider, "distortRadiusSlider")
                or card.findChild(QComboBox, "distortChoice_edges"))
        page.ensureWidgetVisible(last, 0, last.height() + 5)
        qapp.processEvents()
        position = last.mapTo(page.viewport(), QPoint(0, 0))
        assert position.x() >= 0
        assert position.x() + last.width() <= page.viewport().width()
        assert 0 <= position.y() < page.viewport().height()
        assert position.y() + last.height() <= page.viewport().height()
    finally:
        controls.setParent(None)
        page.close()
        page.deleteLater()
