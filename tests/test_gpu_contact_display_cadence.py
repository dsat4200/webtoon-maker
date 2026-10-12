"""Warm GPU display deadlines cannot outlive their native contact ownership."""
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QRect, QRectF
from PySide6.QtGui import QRegion
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from comic_editor.ui.canvas import GpuCanvasWidget, ToolKind
from comic_editor.ui.scene_controller import SceneController


class SingleShot:
    def __init__(self):
        self.starts = []
        self.active = False

    def isActive(self):
        return self.active

    def start(self, milliseconds):
        self.starts.append(milliseconds)
        self.active = True


@pytest.fixture
def warm_contact(qapp):
    class SchedulingCanvas(GpuCanvasWidget):
        def __init__(self):
            # Exercise the production ownership and damage scheduler without
            # creating a graphics context, worker, source image or user window.
            QOpenGLWidget.__init__(self)
            self.resize(128, 128)

        def _projection_has_live_preview(self, *, include_ink=False):
            return False

        def event(self, event):
            return QOpenGLWidget.event(self, event)

    canvas = SchedulingCanvas()
    obj = SimpleNamespace(mask_only=False, interaction_rect=None, underlay_opacity=0.)
    contract, native, prepared, context = object(), object(), object(), object()
    chapter = SimpleNamespace(width=128, height=128, background="#FFFFFFFF", view_overflow=0.,
                              pixel_contract=contract, objects={"source": obj})
    tiles, images = SimpleNamespace(_tiles={"source": native}), object()
    canvas.chapter, canvas.tiles, canvas.images = chapter, tiles, images
    canvas._drawing = canvas._raster_contact_active = True
    canvas._nav_mode = None
    canvas.tool = ToolKind.RASTER_PENCIL
    canvas.rotation, canvas.scale = 0., 1.
    canvas.center_x = canvas.center_y = 64.
    canvas.selected_kind, canvas.selected_id = "object", "source"
    canvas.selected_object_id, canvas.selected_entities = "source", [("object", "source")]
    canvas._solo_entities = set()
    canvas.settings = SimpleNamespace(predictive_ink=True)
    canvas._gpu_contact_pacing_context = context
    canvas._raster_feedback_pending_visible = False
    gate = SimpleNamespace(canvas=canvas, chapter=chapter, tiles=tiles, identifier="source",
                           closed=False, released=False, error=None, busy=False, current=lambda: True)
    canvas._raster_tile_input = gate
    canvas._scene_controller = SimpleNamespace(canvas=canvas, feedback=prepared, _contact_reuse_gate=gate,
                                               _filtered_state=None)
    canvas._visual_frame_timer = SingleShot()
    canvas._visual_pending_widget, canvas._visual_pending_world = QRegion(), QRectF()
    canvas._last_painting_frame_start = 100.
    key = canvas._gpu_contact_pacing_key(obj)
    canvas._gpu_contact_pacing_token = ((gate, chapter, tiles, images, obj, native, prepared,
                                       contract, context), key)
    assert canvas._gpu_contact_pacing_current()
    yield canvas
    canvas.deleteLater()


def test_warm_contact_damage_keeps_one_deadline_and_publishes_latest_union(warm_contact, monkeypatch):
    canvas = warm_contact
    now = [100.]
    monkeypatch.setattr("comic_editor.ui.canvas.time.perf_counter", lambda: now[0])
    canvas._queue_widget_dirty(QRect(5, 6, 2, 3))
    assert canvas._visual_frame_timer.starts == [16]
    assert canvas._visual_pending_widget == QRegion(canvas.rect())
    # Later source/overlay packets cannot keep restarting the pending deadline.
    now[0] = 100.007
    canvas._queue_widget_dirty(QRect(110, 112, 8, 9))
    assert canvas._visual_frame_timer.starts == [16]
    painted = []
    monkeypatch.setattr(canvas, "update", lambda region: painted.append(QRegion(region)))
    canvas._flush_visual_dirty()
    assert painted == [QRegion(canvas.rect())]
    assert canvas._visual_pending_widget.isEmpty()


def test_warm_contact_deadline_is_relative_to_last_frame_without_an_extra_delay(warm_contact, monkeypatch):
    canvas = warm_contact
    monkeypatch.setattr("comic_editor.ui.canvas.time.perf_counter", lambda: 100.008)
    canvas._queue_widget_dirty(canvas.rect())
    assert 7 <= canvas._visual_frame_timer.starts[-1] <= 9
    canvas._visual_frame_timer.active = False
    canvas._visual_pending_widget = QRegion()
    monkeypatch.setattr("comic_editor.ui.canvas.time.perf_counter", lambda: 100.050)
    canvas._queue_widget_dirty(canvas.rect())
    assert canvas._visual_frame_timer.starts[-1] == 0


@pytest.mark.parametrize("full, bounded", [(True, False), (False, True), (False, False)])
def test_adoption_uses_existing_full_or_bounded_native_coverage(warm_contact, monkeypatch, full, bounded):
    canvas, controller = warm_contact, warm_contact._scene_controller
    canvas._gpu_contact_pacing_token = None
    document = SimpleNamespace(live_preview=True, revision=3,
        identity=(id(canvas.chapter), id(canvas.tiles), id(canvas.images)))
    canvas._document_projection = SimpleNamespace(revision=3)
    prepared = SimpleNamespace(identifier=canvas.selected_id)
    controller.feedback = prepared
    controller.desired = (document, (), (), (0., 0., 128., 128.))
    controller._feedback_covers = lambda *_args: full
    controller._feedback_contact_covers = lambda *_args: bounded
    controller._covered_contact_gate = MethodType(SceneController._covered_contact_gate, controller)
    canvas._raster_feedback_contact_covered = True
    context = canvas._gpu_contact_pacing_context
    monkeypatch.setattr(canvas, "context", lambda: context)
    canvas._adopt_gpu_contact_pacing()
    assert canvas._gpu_contact_pacing_current() == (full or bounded)
    monkeypatch.setattr("comic_editor.ui.canvas.time.perf_counter", lambda: 100.)
    canvas._queue_widget_dirty(canvas.rect())
    assert canvas._visual_frame_timer.starts == ([16] if full or bounded else [0])


@pytest.mark.parametrize("transition", ["release", "cold", "camera", "source", "context", "feedback"])
def test_owner_or_readiness_transition_promotes_pending_damage_immediately(warm_contact, monkeypatch, transition):
    canvas = warm_contact
    monkeypatch.setattr("comic_editor.ui.canvas.time.perf_counter", lambda: 100.)
    canvas._queue_widget_dirty(QRect(5, 6, 2, 3))
    assert canvas._visual_frame_timer.starts == [16]
    if transition == "release":
        canvas._drawing = False
        canvas._raster_tile_input.released = True
    elif transition == "cold":
        canvas._raster_tile_input.busy = True
    elif transition == "camera":
        canvas.center_x += 1.
    elif transition == "source":
        canvas.tiles._tiles[canvas.selected_id] = object()
    elif transition == "context":
        canvas._gpu_contact_pacing_context = object()
    else:
        canvas._scene_controller.feedback = object()
    assert not canvas._gpu_contact_pacing_current()
    canvas._queue_widget_dirty(QRect(110, 112, 8, 9))
    assert canvas._gpu_contact_pacing_token is None
    assert canvas._visual_frame_timer.starts == [16, 0]
    assert canvas._visual_pending_widget == QRegion(canvas.rect())
