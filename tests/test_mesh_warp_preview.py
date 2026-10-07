"""Mesh gestures show transient updates and return to exact artwork on release."""
import time
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QSlider

from comic_editor.core.models import DistortModifier
from comic_editor.ui.modifier_controls import ModifierControls
from test_distort_pointer_events import canvas, pointer, escape


@pytest.fixture(autouse=True)
def disconnected_canvas(monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)


@pytest.fixture(autouse=True)
def matching_scene(canvas, wait_scene):
    canvas._test_wait_scene = wait_scene


def capture(canvas):
    canvas._test_wait_scene(canvas)
    return canvas.grab()


def wait_presented(qapp, predicate):
    deadline = time.monotonic() + 20
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert predicate()


def mesh(canvas):
    canvas._document_projection_enabled = True
    # Assert the detached presentation after its matching result arrives;
    # drafts must never replace finished document tiles.
    canvas.tiles.paint_dab(canvas.selected_object_id, QPointF(160, 150), 140, QColor("red"))
    modifier = DistortModifier(modifier_type="distort_mesh_warp", frame=(60, 70, 200, 160),
                              parameters={"rows": 6, "columns": 4, "smoothness": 50})
    canvas.chapter.add_modifier(modifier, [("object", canvas.selected_object_id)])
    canvas.active_modifier_id = modifier.modifier_id
    return modifier


@pytest.mark.parametrize("stylus", [False, True])
def test_handle_preview_updates_without_polluting_finished_tiles_and_release_is_exact(canvas, stylus):
    modifier = mesh(canvas)
    capture(canvas)
    old = {key: tile.image.cacheKey() for key, tile in canvas._document_projection.tiles.items()}
    start = canvas.widget_to_document(dict(canvas._distort_handle_points(modifier))[0])
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "move", start + QPointF(25, 18))
    first = capture(canvas).toImage()
    assert canvas._mesh_warp_preview_presented
    assert canvas._projection_frame_pending
    assert not canvas._effect_jobs.running and not canvas._effect_jobs.pending
    assert {key: tile.image.cacheKey() for key, tile in canvas._document_projection.tiles.items()} == old
    pointer(canvas, stylus, "move", start + QPointF(48, 29))
    assert capture(canvas).toImage() != first
    pointer(canvas, stylus, "release", start + QPointF(60, 34))
    final = capture(canvas).toImage()
    assert not canvas._mesh_warp_preview_presented
    assert not canvas._projection_frame_pending
    assert canvas._mesh_warp_preview_modifier() is None
    canvas._document_projection.clear()
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._effect_jobs.cancel()
    assert capture(canvas).toImage() == final


def test_escape_restores_original_mesh_and_clears_transient_presentation(canvas):
    modifier = mesh(canvas)
    original = canvas.chapter.to_dict()
    capture(canvas)
    start = canvas.widget_to_document(dict(canvas._distort_handle_points(modifier))[0])
    pointer(canvas, False, "press", start)
    pointer(canvas, False, "move", start + QPointF(25, 18))
    capture(canvas)
    assert canvas._mesh_warp_preview_presented
    escape(canvas)
    capture(canvas)
    assert canvas.chapter.to_dict() == original
    assert not canvas._mesh_warp_preview_presented
    assert canvas._mesh_warp_preview_modifier() is None


def test_smoothness_slider_marks_only_active_drag_and_finishes_exact(canvas):
    modifier = mesh(canvas)
    controls = ModifierControls(canvas)
    controls.refresh()
    slider = controls.findChild(QSlider, "distortSlider_smoothness")
    assert slider is not None
    capture(canvas)
    slider.setSliderDown(True)
    slider.setValue(6500)
    capture(canvas)
    assert canvas._mesh_warp_preview_modifier() is modifier
    assert canvas._mesh_warp_preview_presented
    slider.setSliderDown(False)
    capture(canvas)
    assert canvas._mesh_warp_preview_modifier() is None
    assert not canvas._mesh_warp_preview_presented
    assert not canvas._projection_frame_pending
    assert modifier.parameters["smoothness"] == 65
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].parameters["smoothness"] == 50
    controls.deleteLater()


def test_rebuilding_controls_finishes_destroyed_slider_preview(canvas):
    modifier = mesh(canvas)
    controls = ModifierControls(canvas)
    controls.refresh()
    slider = controls.findChild(QSlider, "distortSlider_smoothness")
    slider.setSliderDown(True)
    slider.setValue(6500)
    capture(canvas)
    assert canvas._mesh_warp_preview_presented
    # No sliderReleased signal: selection changes can replace its whole card.
    controls.refresh()
    assert controls._parameter_before is None
    assert controls._mesh_warp_parameter_chapter is None
    assert canvas._mesh_warp_preview_modifier() is None
    capture(canvas)
    assert not canvas._mesh_warp_preview_presented
    assert not canvas._projection_frame_pending
    assert modifier.parameters["smoothness"] == 65
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].parameters["smoothness"] == 50
    controls.deleteLater()


def test_chapter_replacement_discards_slider_snapshot_without_new_history(canvas, monkeypatch):
    mesh(canvas)
    original = canvas.chapter.to_dict()
    controls = ModifierControls(canvas)
    controls.refresh()
    slider = controls.findChild(QSlider, "distortSlider_smoothness")
    slider.setSliderDown(True)
    slider.setValue(6500)
    capture(canvas)
    monkeypatch.setattr(controls, "_push", lambda *_: pytest.fail("committed across chapter replacement"))
    canvas.replace_chapter(original)
    assert controls._parameter_before is None
    assert controls._mesh_warp_parameter_chapter is None
    assert canvas._mesh_warp_preview_modifier() is None
    assert canvas.chapter.to_dict() == original
    capture(canvas)
    assert not canvas._mesh_warp_preview_presented
    controls.deleteLater()


@pytest.mark.parametrize("stylus", [False, True])
def test_stationary_handle_release_repaints_exact_without_grab(canvas, qapp, stylus):
    modifier = mesh(canvas)
    canvas.show()
    qapp.processEvents()
    start = canvas.widget_to_document(dict(canvas._distort_handle_points(modifier))[0])
    final = start + QPointF(25, 18)
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "move", final)
    qapp.processEvents()
    wait_presented(qapp, lambda: canvas._mesh_warp_preview_presented)
    assert canvas._mesh_warp_preview_presented
    # Drain the last move repaint before releasing at precisely its position.
    qapp.processEvents()
    pointer(canvas, stylus, "release", final)
    qapp.processEvents()
    wait_presented(qapp, lambda: not canvas._projection_frame_pending)
    assert not canvas._mesh_warp_preview_presented
    assert not canvas._projection_frame_pending
    assert canvas._mesh_warp_preview_modifier() is None
