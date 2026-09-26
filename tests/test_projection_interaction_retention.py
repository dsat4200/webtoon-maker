"""UI-only changes preserve artwork; canceled previews never leave stale pixels."""
import pytest
from PySide6.QtCore import QRectF

from comic_editor.core.models import BoundGeometry
from comic_editor.ui.canvas import ToolKind
from test_projection_invalidation import scene, frame


def test_tools_and_ordinary_selection_do_not_recapture_document(scene):
    canvas, obj, group = scene
    expected = frame(canvas)
    renders = canvas._document_projection.renders
    for tool in (ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.OBJECT_SELECT):
        assert canvas.set_tool(tool)
        assert frame(canvas) == expected
    canvas.set_selection("layer", group.layer_id, activate_default_tool=False)
    assert frame(canvas) == expected
    canvas.set_selection_set([("layer", group.layer_id), ("object", obj.object_id)])
    assert frame(canvas) == expected
    canvas.clear_selection()
    assert frame(canvas) == expected
    assert canvas._document_projection.renders == renders


@pytest.mark.parametrize("kind", ["object", "layer"])
def test_mask_only_selection_uses_distinct_restorable_pixels(scene, kind):
    canvas, obj, group = scene
    target = obj if kind == "object" else group
    target.mask_only = True
    identifier = obj.object_id if kind == "object" else group.layer_id
    canvas.set_selection(kind, identifier, activate_default_tool=False)
    shown = frame(canvas)
    canvas.clear_selection()
    hidden = frame(canvas)
    assert shown != hidden
    renders = canvas._document_projection.renders
    canvas.set_selection(kind, identifier, activate_default_tool=False)
    assert frame(canvas) == shown
    canvas.clear_selection()
    assert frame(canvas) == hidden
    assert canvas._document_projection.renders == renders


def test_underlay_selection_uses_distinct_restorable_pixels(scene):
    canvas, obj, group = scene
    group.bound = BoundGeometry.rectangle(0, 0, 100, 512)
    obj.underlay_opacity = .5
    shown = frame(canvas)
    canvas.clear_selection()
    hidden = frame(canvas)
    assert shown != hidden
    renders = canvas._document_projection.renders
    canvas.set_selection("object", obj.object_id, activate_default_tool=False)
    assert frame(canvas) == shown
    assert canvas._document_projection.renders == renders


@pytest.mark.parametrize("action", ["tool", "selection", "selection_set", "clear"])
def test_canceling_a_captured_transform_preview_invalidates_artwork(scene, action):
    canvas, obj, group = scene
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas._transform_start_quad = [(0, 0), (2048, 0), (2048, 512), (0, 512)]
    canvas._transform_preview_quad = [(100, 0), (2148, 0), (2148, 512), (100, 512)]
    preview = frame(canvas)
    assert canvas._projection_captured_live_preview
    if action == "tool":
        canvas.set_tool(ToolKind.OBJECT_SELECT)
    elif action == "selection":
        canvas.set_selection("layer", group.layer_id, activate_default_tool=False)
    elif action == "selection_set":
        canvas.set_selection_set([("object", obj.object_id), ("layer", group.layer_id)])
    else:
        canvas.clear_selection()
    actual = frame(canvas)
    assert actual != preview
    assert actual == frame(canvas, fresh=True)


def test_fixed_capture_keeps_effect_region_stable_when_dirty_subset_changes(scene, monkeypatch):
    canvas, _, _ = scene
    observed = []
    original = canvas._render_scene_layers
    def record(painter, visible, **kwargs):
        observed.append((QRectF(canvas._effect_viewport_world), QRectF(visible)))
        return original(painter, visible, **kwargs)
    monkeypatch.setattr(canvas, "_render_scene_layers", record)
    requests = canvas._document_projection.requests(QRectF(0, 0, 768, 256), 1)
    canvas._render_document_tiles(requests[:1])
    canvas._render_document_tiles(requests[1:])
    assert observed[0][0] == observed[1][0] == QRectF(0, 0, 1026, 512)
    assert observed[0][1] != observed[1][1]


def test_solo_round_trip_retains_pixels_and_edits_refresh_inactive_scope(scene):
    canvas, obj, _ = scene
    normal = frame(canvas)
    canvas.set_solo_entities({("object", obj.object_id)})
    frame(canvas)
    renders = canvas._document_projection.renders
    canvas.set_solo_entities(set())
    assert frame(canvas) == normal
    assert canvas._document_projection.renders == renders
    canvas.set_solo_entities({("object", obj.object_id)})
    from PySide6.QtCore import QPointF
    canvas._begin_stroke(QPointF(700, 120), 1.)
    canvas._end_stroke()
    frame(canvas)
    canvas.set_solo_entities(set())
    updated = frame(canvas)
    assert updated != normal
    assert updated == frame(canvas, fresh=True)
