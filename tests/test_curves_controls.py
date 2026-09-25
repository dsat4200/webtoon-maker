"""Curves graph gestures, precise fields, channel isolation and deferred sampling."""
import copy
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QVBoxLayout, QWidget

from comic_editor.core.curves import CURVE_CHANNELS, evaluate_curve
from comic_editor.core.models import CurvesModifier
from comic_editor.ui.curves_controls import CurvesControls, IDENTITY


class CanvasStub(QObject):
    documentChanged = Signal(object)
    chapterReplaced = Signal()

    def __init__(self, modifier):
        super().__init__()
        self.chapter = SimpleNamespace(modifiers={modifier.modifier_id: modifier})
        self.histograms = []
        self.pickers = []

    def curves_histogram(self, *args):
        self.histograms.append(args)
        return np.arange(256)

    def start_curves_picker(self, *args):
        self.pickers.append(args)


class OwnerStub(QWidget):
    def __init__(self, modifier):
        super().__init__()
        self.canvas = CanvasStub(modifier)
        self._parameter_before = None
        self.commits = []

    def state(self):
        return {key: value.to_dict() for key, value in self.canvas.chapter.modifiers.items()}

    def begin_parameter_drag(self):
        if self._parameter_before is None:
            self._parameter_before = copy.deepcopy(self.state())

    def set_parameter(self, modifier_id, attribute, value, commit):
        before = copy.deepcopy(self.state()) if commit and self._parameter_before is None else None
        modifier = self.canvas.chapter.modifiers[modifier_id]
        setattr(modifier, attribute, copy.deepcopy(value))
        modifier.validate()
        self.canvas.documentChanged.emit(None)
        if before is not None and before != self.state():
            self.commits.append((before, copy.deepcopy(self.state())))

    def finish_parameter_drag(self):
        before, self._parameter_before = self._parameter_before, None
        if before is not None and before != self.state():
            self.commits.append((before, copy.deepcopy(self.state())))


@pytest.fixture
def editor(qapp):
    modifier = CurvesModifier()
    owner = OwnerStub(modifier)
    controls = CurvesControls(owner, modifier)
    QVBoxLayout(owner).addWidget(controls)
    owner.resize(320, 460)
    owner.show()
    qapp.processEvents()
    yield owner, modifier, controls
    owner.close()
    owner.deleteLater()


def add_point(controls, x=.5, y=.7):
    QTest.mouseClick(controls.graph, Qt.LeftButton, pos=controls.graph.point_position((x, y)).toPoint())


def wait_for_histograms(owner, count, qapp):
    for _ in range(100):
        if len(owner.canvas.histograms) >= count:
            break
        QTest.qWait(10)
        qapp.processEvents()
    assert len(owner.canvas.histograms) == count


def test_graph_click_drag_release_updates_fields_and_commits_once(editor):
    owner, modifier, controls = editor
    graph = controls.graph
    start = graph.point_position((.5, .7)).toPoint()
    finish = graph.point_position((.63, .83)).toPoint()
    QTest.mousePress(graph, Qt.LeftButton, pos=start)
    QTest.mouseMove(graph, graph.point_position((.55, .75)).toPoint())
    QTest.mouseRelease(graph, Qt.LeftButton, pos=finish)
    assert len(owner.commits) == 1
    assert len(modifier.curves["rgb:master"]) == 3
    expected = graph.point_value(finish)
    assert modifier.curves["rgb:master"][1] == pytest.approx(expected)
    assert controls.x_value.value() == pytest.approx(expected[0], abs=.0001)
    assert controls.y_value.value() == pytest.approx(expected[1], abs=.0001)
    assert owner._parameter_before is None


def test_drawn_graph_uses_shared_spline_with_flat_endpoint_extension(editor):
    _, modifier, controls = editor
    points = [(0.1, .2), (.5, .9), (.9, .6)]
    modifier.curves["rgb:master"] = points
    controls.sync_from_modifier()
    path = controls.graph.curve_path()
    for x in (0., .25, .5, 1.):
        expected = controls.graph.point_position((x, evaluate_curve(points, np.array([x]))[0]))
        element = min((path.elementAt(i) for i in range(path.elementCount())),
                      key=lambda item: abs(item.x - expected.x()))
        assert element.x == pytest.approx(expected.x())
        assert element.y == pytest.approx(expected.y(), abs=.00001)


def test_numeric_edits_and_arrow_keys_clamp_distinct_inputs(editor):
    owner, modifier, controls = editor
    add_point(controls)
    controls.x_value.setValue(.65)
    controls.y_value.setValue(.4)
    assert modifier.curves["rgb:master"][1] == pytest.approx((.65, .4))
    count = len(owner.commits)
    QTest.keyClick(controls.graph, Qt.Key_Up)
    assert modifier.curves["rgb:master"][1][1] == pytest.approx(.4 + 1 / 255.)
    assert len(owner.commits) == count + 1
    controls.x_value.setValue(1.)
    assert modifier.curves["rgb:master"][1][0] < modifier.curves["rgb:master"][2][0]
    controls.select_point(0)
    controls.x_value.setValue(.15)
    assert modifier.curves["rgb:master"][0][0] == .15


@pytest.mark.parametrize("remove", ["delete", "backspace", "right-click"])
def test_remove_node_keeps_at_least_two_points(editor, remove):
    owner, modifier, controls = editor
    add_point(controls)
    if remove == "right-click":
        QTest.mouseClick(controls.graph, Qt.RightButton,
                         pos=controls.graph.point_position(controls.graph.points[1]).toPoint())
    else:
        QTest.keyClick(controls.graph, Qt.Key_Delete if remove == "delete" else Qt.Key_Backspace)
    assert len(modifier.curves["rgb:master"]) == 2
    assert len(owner.commits) == 2
    QTest.keyClick(controls.graph, Qt.Key_Delete)
    assert len(controls.graph.points) == 2


def test_escape_restores_curve_without_leaving_an_undo_entry(editor):
    owner, modifier, controls = editor
    graph = controls.graph
    QTest.mousePress(graph, Qt.LeftButton, pos=graph.point_position((.45, .7)).toPoint())
    QTest.mouseMove(graph, graph.point_position((.55, .8)).toPoint())
    assert modifier.curves
    QTest.keyClick(graph, Qt.Key_Escape)
    QTest.mouseRelease(graph, Qt.LeftButton, pos=graph.point_position((.55, .8)).toPoint())
    assert modifier.curves == {}
    assert owner.commits == []
    assert not graph.dragging
    assert owner._parameter_before is None


def test_selecting_existing_node_does_not_round_its_value_or_add_undo(editor):
    owner, modifier, controls = editor
    modifier.curves["rgb:master"] = [(0., 0.), (.33333, .76543), (1., 1.)]
    controls.sync_from_modifier()
    QTest.mouseClick(controls.graph, Qt.LeftButton,
                     pos=controls.graph.point_position((.33333, .76543)).toPoint())
    assert modifier.curves["rgb:master"][1] == (.33333, .76543)
    assert controls.graph.selected_index == 1
    assert owner.commits == []


def test_escape_after_adding_beyond_moved_endpoint_restores_valid_selection(editor):
    owner, modifier, controls = editor
    original = [(0.2, .1), (.8, .9)]
    modifier.curves["rgb:master"] = original
    controls.sync_from_modifier()
    graph = controls.graph
    QTest.mousePress(graph, Qt.LeftButton, pos=graph.point_position((.95, .5)).toPoint())
    assert graph.selected_index == 2
    QTest.keyClick(graph, Qt.Key_Escape)
    assert graph.points == original
    assert graph.selected_index < len(graph.points)
    assert owner.commits == []


@pytest.mark.parametrize("mode", list(CURVE_CHANNELS))
def test_color_modes_expose_all_channels_and_keep_other_curves(editor, mode):
    owner, modifier, controls = editor
    modifier.curves["rgb:red"] = [(0, .1), (1, .9)]
    controls.mode_combo.setCurrentIndex(controls.mode_combo.findData(mode))
    available = [controls.channel_combo.itemData(index) for index in range(controls.channel_combo.count())]
    assert available == list(CURVE_CHANNELS[mode])
    controls.set_channel("alpha")
    add_point(controls, .4, .3)
    assert f"{mode}:alpha" in modifier.curves
    assert modifier.curves["rgb:red"] == [(0., .1), (1., .9)]
    count = len(owner.commits)
    controls.set_channel("master")
    assert len(owner.commits) == count
    assert controls.graph.points == IDENTITY


def test_reset_only_selected_channel_is_one_edit(editor):
    owner, modifier, controls = editor
    modifier.curves = {"rgb:master": [(0., .3), (1., .7)], "rgb:red": [(0., .1), (1., .9)]}
    controls.sync_from_modifier()
    controls.reset_button.click()
    assert modifier.curves == {"rgb:red": [(0., .1), (1., .9)]}
    assert controls.graph.points == IDENTITY
    assert len(owner.commits) == 1


def test_reset_all_clears_all_curves_and_range_in_one_undo_without_other_modifiers(editor):
    owner, modifier, controls = editor
    modifier.curves = {"rgb:master": [(0., .3), (1., .7)],
        "rgb:red": [(0., .1), (1., .9)], "lab:alpha": [(0., .5), (1., 1.)]}
    modifier.input_min, modifier.input_max = 2., 4.
    other = CurvesModifier(curves={"rgb:blue": [(0., .2), (1., .6)]})
    owner.canvas.chapter.modifiers[other.modifier_id] = other
    unchanged = copy.deepcopy(other.to_dict())
    controls.set_channel("red")
    controls.reset_all_button.click()
    assert modifier.curves == {}
    assert (modifier.input_min, modifier.input_max) == (0., 1.)
    assert controls.channel == "red"
    assert other.to_dict() == unchanged
    assert len(owner.commits) == 1
    before, after = owner.commits[0]
    assert before[modifier.modifier_id]["input_min"] == 2.
    assert before[modifier.modifier_id]["curves"]["lab:alpha"] == [[0., .5], [1., 1.]]
    assert after[modifier.modifier_id]["curves"] == {}


def test_input_range_and_blend_mode_use_model_contract(editor):
    owner, modifier, controls = editor
    controls.maximum.setValue(4.)
    controls.minimum.setValue(2.)
    assert (modifier.input_min, modifier.input_max) == (2., 4.)
    controls.maximum.setValue(1.)
    assert modifier.input_max > modifier.input_min
    controls.blend_combo.setCurrentIndex(controls.blend_combo.findData("soft_light"))
    assert modifier.blend_mode == "soft_light"
    assert owner._parameter_before is None


@pytest.mark.parametrize("mode", ["add_point", "black_point", "white_point", "gray_point", "white_balance"])
def test_picker_receives_current_mode_channel_and_modifier(editor, mode):
    owner, modifier, controls = editor
    controls.set_channel("blue")
    controls.picker_buttons[mode].click()
    assert owner.canvas.pickers == [(modifier.modifier_id, mode, "rgb", "blue")]
    assert owner.commits == []


def test_histogram_debounces_changes_and_waits_for_drag_release(editor, qapp):
    owner, _, controls = editor
    controls._histogram_timer.stop()
    owner.canvas.histograms.clear()
    for _ in range(4):
        owner.canvas.documentChanged.emit(None)
    wait_for_histograms(owner, 1, qapp)
    np.testing.assert_array_equal(controls.graph.histogram, np.arange(256))
    graph = controls.graph
    QTest.mousePress(graph, Qt.LeftButton, pos=graph.point_position((.5, .7)).toPoint())
    controls.refresh_histogram()
    QTest.qWait(210)
    assert len(owner.canvas.histograms) == 1
    QTest.mouseRelease(graph, Qt.LeftButton, pos=graph.point_position((.6, .7)).toPoint())
    wait_for_histograms(owner, 2, qapp)
    controls.hide()
    owner.canvas.documentChanged.emit(None)
    assert not controls._histogram_timer.isActive()


def test_external_picker_edit_and_chapter_swap_refresh_live_model(editor):
    owner, modifier, controls = editor
    replacement = CurvesModifier(modifier_id=modifier.modifier_id,
        curves={"rgb:master": [(0., .2), (.7, .4), (1., .8)]})
    owner.canvas.chapter.modifiers[modifier.modifier_id] = replacement
    owner.canvas.chapterReplaced.emit()
    assert controls.modifier is replacement
    assert controls.graph.points == replacement.curves["rgb:master"]
    owner.canvas.chapter = None
    owner.canvas.chapterReplaced.emit()
    assert not controls.isEnabled()
    assert not controls._histogram_timer.isActive()


def test_chapter_swap_during_drag_discards_old_undo_snapshot(editor):
    owner, modifier, controls = editor
    QTest.mousePress(controls.graph, Qt.LeftButton,
                     pos=controls.graph.point_position((.5, .7)).toPoint())
    replacement = CurvesModifier(modifier_id=modifier.modifier_id)
    owner.canvas.chapter = SimpleNamespace(modifiers={modifier.modifier_id: replacement})
    owner.canvas.chapterReplaced.emit()
    QTest.mouseRelease(controls.graph, Qt.LeftButton,
                       pos=controls.graph.point_position((.6, .8)).toPoint())
    assert replacement.curves == {}
    assert owner._parameter_before is None
    assert not controls.graph.dragging
    assert owner.commits == []


def test_compact_graph_controls_fit_320_pixel_panel(editor):
    owner, _, controls = editor
    assert owner.minimumSizeHint().width() <= 320
    assert controls.graph.width() >= 160
    assert controls.grab().width() <= 320
