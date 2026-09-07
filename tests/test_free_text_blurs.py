from __future__ import annotations

import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPointingDevice, QTabletEvent, QTransform
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu, QSpinBox

from comic_editor.core.assets import extract_asset, instantiate_asset
from comic_editor.core.effect_geometry import effect_bounds, radial_sweep_bounds
from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, ImageObject, ParameterMaskBinding,
    RadialBlurModifier, RasterObject, TextObject, modifier_from_dict,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.modifier_rendering import (
    BlurPyramidCache, _premultiplied_qimage, _qimage_premultiplied,
    _variable_blur, apply_modifier_stack,
)
from comic_editor.ui.radial_blur import RadialRenderCancelled, radial_blur


def document():
    chapter = ChapterDocument(height=360)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 480, 360))
    page.fill_color = None
    page.border_width = 0
    return chapter, page


@pytest.fixture
def canvas(qapp):
    chapter, page = document()
    widget = CanvasWidget(EditorSettings(snap_to_grid=False))
    widget.resize(640, 480)
    widget.set_document(chapter, TileStore())
    widget.center_x, widget.center_y, widget.scale = 240, 180, 1
    widget.set_selection("layer", page.layer_id)
    yield widget
    widget._effect_jobs.cancel()
    widget.hide()
    widget.deleteLater()


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def add_box(canvas, parent=None, rect=(40, 50, 220, 130), text="Do you ever feel like you're being watched?"):
    parent = parent or canvas.chapter.root_page_ids[0]
    x, y, w, h = rect
    return canvas.chapter.add_object(parent, TextObject(text=text, font_size=25,
        margin=0, x=x, y=y, width=w, height=h, layout_mode="free",
        transform_quad=canvas._rect_quad(QRectF(*rect))))


def group(canvas):
    return canvas.chapter.add_layer(canvas.chapter.root_page_ids[0], "Free Text", layer_kind="text_container")


def test_schema_23_defaults_and_legacy_interactions():
    chapter, page = document()
    text = chapter.add_object(page.layer_id, TextObject(layout_mode="free"))
    raster = chapter.add_object(page.layer_id, RasterObject())
    blur = BlurModifier(strength=12, mode="focal", muted=True)
    chapter.add_modifier(blur, [("object", raster.object_id)])
    payload = chapter.to_dict()
    assert payload["schema_version"] == 24
    assert text.transform_behavior == "bounds"
    assert blur.algorithm == "normal"
    payload["schema_version"] = 22
    next(o for o in payload["objects"] if o["id"] == text.object_id).pop("transform_behavior")
    payload["modifiers"][0].pop("algorithm")
    restored = ChapterDocument.from_dict(payload)
    assert restored.objects[text.object_id].transform_behavior == "stretch"
    legacy = restored.modifiers[blur.modifier_id]
    assert legacy.algorithm == "legacy" and legacy.name == "Blur Legacy"
    assert legacy.muted and legacy.strength == 12 and legacy.mode == "focal"


@pytest.mark.parametrize("transformed", [False, True])
def test_strict_to_free_preserves_pixels_and_discards_stale_quad(canvas, transformed):
    parent = canvas.chapter.add_layer(canvas.active_page_id, "Bubble", BoundGeometry.rectangle(40, 40, 320, 220))
    parent.fill_color = None
    parent.border_width = 0
    if transformed:
        parent.transform_frame = (40, 40, 320, 220)
        parent.transform_quad = [(60, 30), (390, 60), (350, 290), (30, 230)]
    obj = canvas.chapter.add_object(parent.layer_id, TextObject(text="Do you ever\nfeel like you're\nbeing watched?", font_size=29,
        width=25, height=800, margin=15, transform_quad=[(0, 0), (40, 0), (40, 900), (0, 900)]))
    before = _qimage_premultiplied(render(canvas))
    rect = canvas._strict_text_rect(obj)
    canvas.set_text_layout_mode(obj, "free")
    after = _qimage_premultiplied(render(canvas))
    assert (obj.width, obj.height) == (rect.width(), rect.height())
    assert obj.transform_quad == canvas._rect_quad(rect)
    assert obj.transform_behavior == "bounds"
    # Separate Qt clipping paths can vary at most one antialiasing unit.
    np.testing.assert_allclose(after, before, atol=1/255)


def test_container_placement_cancel_creation_and_typing_undo(canvas):
    before = canvas.chapter.to_dict()
    canvas.begin_text_placement(canvas.active_page_id, new_container=True)
    QTest.keyClick(canvas, Qt.Key_Escape)
    assert canvas.chapter.to_dict() == before
    canvas.begin_text_placement(canvas.active_page_id, new_container=True)
    start, end = QPointF(35, 45), QPointF(265, 165)
    QTest.mousePress(canvas, Qt.LeftButton, pos=canvas.document_to_widget(start).toPoint())
    QTest.mouseRelease(canvas, Qt.LeftButton, pos=canvas.document_to_widget(end).toPoint())
    obj = canvas.chapter.objects[canvas.selected_id]
    parent = canvas.chapter.layers[obj.parent_layer_id]
    assert parent.layer_kind == "text_container" and parent.bound is None
    assert (obj.width, obj.height) == (230, 120)
    assert canvas.tool == ToolKind.TEXT_EDIT and canvas._text_selection_range() == [0, 4]
    QTest.keyClicks(canvas, "Independent")
    canvas.commit_active_text_edit()
    canvas.command_stack.undo()
    assert canvas.chapter.objects[obj.object_id].text == "Text"
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    canvas.command_stack.redo()
    canvas.chapter.validate()


def test_container_ui_add_second_box_and_mode_settings(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.main_window.save_settings", lambda _: None)
    window = MainWindow()
    chapter, page = document()
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", page.layer_id)
    try:
        window._add_free_text_container()
        canvas = window.canvas
        canvas._text_placement_press(QPointF(40, 50))
        canvas._finish_text_placement()
        first = chapter.objects[canvas.selected_id]
        window._add_text()
        assert canvas._text_placement["parent"] == first.parent_layer_id
        canvas._text_placement_press(QPointF(100, 220))
        canvas._finish_text_placement()
        second = chapter.objects[canvas.selected_id]
        assert first.object_id != second.object_id and first.parent_layer_id == second.parent_layer_id
        assert not window.text_object_controls.layout_mode.isEnabled()
        canvas.set_selection("layer", first.parent_layer_id)
        window._refresh_actions()
        assert not window.add_raster_button.isEnabled()
        assert not window.add_vector_button.isEnabled()
        window.selection_settings.refresh()
        chapter.validate()
    finally:
        window.hide()
        window.deleteLater()


@pytest.mark.parametrize("container", [False, True])
def test_bounds_resize_reflows_without_glyph_scale_and_is_undoable(canvas, container):
    parent = group(canvas) if container else None
    first = add_box(canvas, parent.layer_id if parent else None)
    if container:
        second = add_box(canvas, parent.layer_id, (280, 80, 140, 100), "Another box")
    canvas.set_selection("layer" if parent else "object", parent.layer_id if parent else first.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.commit_active_text_edit()
    before = canvas.chapter.to_dict()
    target, frame, mapping, _ = canvas._text_frame_target()
    original_width = first.width
    original_height = canvas._text_document(first, first.width).size().height()
    anchor = mapping.map(frame.bottomRight())
    assert canvas._begin_free_text_transform(anchor)
    final = mapping.map(QPointF(frame.left()+frame.width()*.6, frame.bottom()))
    canvas._queue_free_text_drag(final)
    canvas._finish_free_text_drag()
    assert first.width == pytest.approx(original_width*.6)
    assert first.font_size == 25
    assert canvas._text_document(first, first.width).size().height() > original_height
    glyph_map = canvas._quad_transform(QRectF(0, 0, first.width, first.height), first.transform_quad)
    assert glyph_map.m11() == pytest.approx(1)
    assert glyph_map.m22() == pytest.approx(1)
    if container:
        assert second.width == pytest.approx(84)
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    canvas.command_stack.redo()
    canvas.chapter.validate()


def test_mode_toggle_preserves_pixels_and_group_stretch(canvas):
    parent = group(canvas)
    text = add_box(canvas, parent.layer_id)
    canvas.set_selection("layer", parent.layer_id)
    before = _qimage_premultiplied(render(canvas))
    canvas.set_text_transform_behavior("stretch")
    np.testing.assert_array_equal(_qimage_premultiplied(render(canvas)), before)
    frame = canvas._text_container_bounds(parent)
    assert canvas._begin_free_text_transform(frame.bottomRight())
    canvas._queue_free_text_drag(frame.bottomRight()+QPointF(100, 20))
    canvas._finish_free_text_drag()
    assert parent.transform_quad is not None and text.width == 220
    assert canvas.layer_world_transform(parent.layer_id).m11() != 1


def test_container_reparent_preserves_world_positions_and_rejects_non_text(canvas):
    chapter = canvas.chapter
    parent = group(canvas)
    parent.translate_x, parent.translate_y = 70, 20
    text = add_box(canvas)
    original = canvas.object_world_quad(text.object_id)
    chapter.move_entity("object", text.object_id, parent.layer_id, 0)
    np.testing.assert_allclose(canvas.object_world_quad(text.object_id), original)
    raster = chapter.add_object(canvas.active_page_id, RasterObject())
    before = chapter.to_dict()
    with pytest.raises(ValueError, match="Text objects only"):
        chapter.move_entity("object", raster.object_id, parent.layer_id, 0)
    assert chapter.to_dict() == before
    destination = chapter.add_layer(canvas.active_page_id, "Other", BoundGeometry.rectangle(0, 0, 450, 350))
    destination.translate_x = 25
    chapter.move_entity("layer", parent.layer_id, destination.layer_id, 0)
    np.testing.assert_allclose(canvas.object_world_quad(text.object_id), original)
    chapter.validate()


def test_text_whitespace_priority_over_selected_raster_body(canvas):
    text = add_box(canvas, text="Hi")
    raster = canvas.chapter.add_object(canvas.active_page_id, RasterObject(interaction_rect=(0, 0, 450, 300)))
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    position = canvas.document_to_widget(QPointF(200, 150)).toPoint()
    QTest.mouseClick(canvas, Qt.LeftButton, pos=position)
    assert canvas.selected_id == text.object_id


def pixels():
    rgba = np.zeros((64, 64, 4), np.float32)
    rgba[15:45, 18:48] = (.24, .015, .20, .25)
    return rgba


@pytest.mark.parametrize("mode", ["full", "focal"])
def test_normal_blur_valid_premultiplication_and_legacy_distortion(mode):
    source = _premultiplied_qimage(pixels())
    normal = _qimage_premultiplied(apply_modifier_stack(source, [BlurModifier(strength=16, mode=mode)], (0, 0)))
    legacy = _qimage_premultiplied(apply_modifier_stack(source, [BlurModifier(strength=16, mode=mode, algorithm="legacy")], (0, 0)))
    assert np.all(normal[..., :3] <= normal[..., 3:4]+1/255)
    assert np.any(legacy[..., :3] > legacy[..., 3:4]+.01)
    for background in (0., .2, 1.):
        composite = normal[..., :3] + background*(1-normal[..., 3:4])
        assert composite.min() >= 0 and composite.max() <= 1+1/255


def test_blur_cache_separates_algorithms_and_legacy_numeric_output():
    cache = BlurPyramidCache()
    source = pixels()
    normal = _variable_blur(source, 12., cache, algorithm="normal")
    legacy = _variable_blur(source, 12., cache, algorithm="legacy")
    assert not np.array_equal(normal, legacy)
    # Legacy was Pillow's straight-RGBA pyramid. Lock the original branch.
    from PIL import Image
    source_image = Image.fromarray(np.clip(source*255, 0, 255).astype(np.uint8), mode="RGBA")
    from comic_editor.ui.modifier_rendering import BLUR_PYRAMID_RADII
    index = np.searchsorted(BLUR_PYRAMID_RADII, 12., side="right")-1
    levels = [source_image]
    for _ in BLUR_PYRAMID_RADII[1:]:
        levels.append(levels[-1].resize((max(1, (levels[-1].width+1)//2), max(1, (levels[-1].height+1)//2)), Image.Resampling.BILINEAR))
    fraction = float((12.-BLUR_PYRAMID_RADII[index])/(BLUR_PYRAMID_RADII[index+1]-BLUR_PYRAMID_RADII[index]))
    expected = Image.blend(levels[index].resize((64, 64), Image.Resampling.BILINEAR), levels[index+1].resize((64, 64), Image.Resampling.BILINEAR), fraction)
    np.testing.assert_allclose(legacy, np.asarray(expected, dtype=np.float32)/255, atol=1/255)


@pytest.mark.parametrize("angle", [0., 1., 15., 180., 360.])
def test_radial_matches_fine_reference_and_premultiplication(angle):
    source = pixels()[::2, ::2].copy()
    mapping = QTransform.fromTranslate(-7, 3)
    actual = radial_blur(source, (12, 10), angle, mapping)
    reference = radial_blur(source, (12, 10), angle, mapping, spacing=.12)
    np.testing.assert_allclose(actual, reference, atol=.012)
    assert np.all(actual[..., :3] <= actual[..., 3:4])
    if angle == 0:
        np.testing.assert_array_equal(actual, source)


def test_radial_masked_angles_crop_and_cancellation():
    source = pixels()
    angles = np.full((64, 64), 30, np.float32)
    angles[:, :32] = 0
    full = radial_blur(source, (40, 20), angles, QTransform())
    np.testing.assert_array_equal(full[:, :32], source[:, :32])
    crop = radial_blur(source, (40, 20), angles[10:50, 10:50], QTransform(), output_shape=(40, 40), output_origin=(10, 10))
    np.testing.assert_allclose(crop, full[10:50, 10:50], atol=.006)
    with pytest.raises(RadialRenderCancelled):
        radial_blur(source, (40, 20), 360, QTransform(), cancelled=lambda: True)


def test_radial_swept_bounds_mask_extremes_and_persistence():
    rect = QRectF(20, 5, 10, 10)
    modifier = RadialBlurModifier(center=(0, 0), angle=180,
        parameter_masks={"angle": ParameterMaskBinding("mask", 0, 360)})
    restored = modifier_from_dict(modifier.to_dict())
    assert restored.to_dict() == modifier.to_dict()
    bounds = effect_bounds(rect, [modifier], QTransform())
    assert bounds.contains(QPointF(-30, 0)) and bounds.contains(QPointF(0, -30))
    modifier.muted = True
    assert effect_bounds(rect, [modifier], QTransform()) == rect
    for angle in (0, 15, 180, 360):
        bound = radial_sweep_bounds(rect, (0, 0), angle)
        for theta in np.linspace(-angle/2, angle/2, 101):
            rotated = QTransform().rotate(theta).mapRect(rect)
            assert bound.adjusted(-1e-6, -1e-6, 1e-6, 1e-6).contains(rotated)


def test_radial_render_partial_equals_full_and_source_reuse(canvas):
    layer = canvas.chapter.add_layer(canvas.active_page_id, "Dot", BoundGeometry.rectangle(210, 150, 20, 20))
    layer.fill_color, layer.border_width = "#80FF00FF", 0
    radial = RadialBlurModifier(center=(190, 165), angle=80)
    canvas.chapter.add_modifier(radial, [("layer", layer.layer_id)])
    first = render(canvas)
    source_count = len(canvas._modifier_source_cache)
    radial.angle = 100
    second = render(canvas)
    assert len(canvas._modifier_source_cache) == source_count
    assert np.any(_qimage_premultiplied(second) != _qimage_premultiplied(first))
    canvas.render_preview(first, QRect(140, 100, 140, 140))
    np.testing.assert_array_equal(_qimage_premultiplied(first), _qimage_premultiplied(second))


def test_radial_gizmo_final_position_undo_and_selection(canvas):
    layer = group(canvas)
    add_box(canvas, layer.layer_id)
    radial = RadialBlurModifier(center=(100, 100))
    canvas.chapter.add_modifier(radial, [("layer", layer.layer_id)])
    canvas.set_selection("layer", layer.layer_id)
    canvas.modifier_mode = True
    canvas.active_modifier_id = radial.modifier_id
    start, _ = canvas._radial_handle_points(radial)
    assert canvas._begin_radial_handle(start)
    for x in range(1, 20):
        canvas._queue_radial_handle(start+QPointF(x, 10))
    canvas._finish_modifier_handle()
    assert radial.center == (119, 110)
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[radial.modifier_id].center == (100, 100)
    assert canvas._active_radial_modifier() is None


def test_async_radial_discards_stale_and_document_replacement(canvas):
    from threading import Event
    gate = Event()
    image = _premultiplied_qimage(pixels())
    jobs = canvas._effect_jobs
    jobs.request(("test",), ("old",), lambda cancel: (gate.wait(.1), image)[1], 100)
    jobs.request(("test",), ("new",), lambda cancel: image, 100)
    gate.set()
    deadline = time.monotonic()+3
    while (jobs.running or jobs.pending) and time.monotonic() < deadline:
        jobs.poll()
        QTest.qWait(5)
    assert canvas._modifier_cache_get(("old",)) is None
    assert canvas._modifier_cache_get(("new",)) is not None
    assert jobs.discarded == 1 and jobs.completed == 1
    jobs.request(("test",), ("replaced",), lambda cancel: image, 100)
    canvas.replace_chapter(canvas.chapter.to_dict())
    QTest.qWait(50)
    jobs.poll()
    assert canvas._modifier_cache_get(("replaced",)) is None


def tablet(target, kind, point, pressed):
    event = QTabletEvent(kind, QPointingDevice.primaryPointingDevice(),
        point, QPointF(target.mapToGlobal(point.toPoint())), 1. if pressed else 0.,
        0., 0., 0., 0., 0., Qt.NoModifier, Qt.LeftButton,
        Qt.LeftButton if pressed else Qt.NoButton)
    QCoreApplication.sendEvent(target, event)


@pytest.mark.parametrize("stylus", [False, True])
def test_bounds_actual_release_under_projective_parent(canvas, stylus):
    parent = group(canvas)
    parent.transform_frame = (0, 0, 400, 300)
    parent.transform_quad = [(20, 10), (430, 30), (400, 340), (0, 280)]
    text = add_box(canvas, parent.layer_id)
    canvas.set_selection("object", text.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    mapping = canvas.layer_world_transform(parent.layer_id)
    start = canvas.document_to_widget(mapping.map(QPointF(260, 180)))
    end = canvas.document_to_widget(mapping.map(QPointF(200, 230)))
    if stylus:
        tablet(canvas, QEvent.TabletPress, start, True)
        tablet(canvas, QEvent.TabletRelease, end, False)
    else:
        QTest.mousePress(canvas, Qt.LeftButton, pos=start.toPoint())
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=end.toPoint())
    assert text.width == pytest.approx(160, abs=1)
    assert text.height == pytest.approx(180, abs=1)
    assert text.font_size == 25
    canvas.command_stack.undo()
    assert canvas.chapter.objects[text.object_id].width == 220


def test_free_text_asset_preserves_children_transforms_and_radial(canvas):
    parent = group(canvas)
    parent.translate_x, parent.translate_y = 17, 23
    first = add_box(canvas, parent.layer_id)
    second = add_box(canvas, parent.layer_id, (280, 80, 100, 70), "Two")
    second.transform_behavior, second.font_size = "stretch", 18
    radial = RadialBlurModifier(center=(200, 130), angle=30)
    canvas.chapter.add_modifier(radial, [("layer", parent.layer_id)])
    manifest, tiles = extract_asset(canvas.chapter, canvas.tiles, "layer", parent.layer_id, "Free Text")
    manifest.validate()
    kind, identifier, _ = instantiate_asset(manifest, tiles, canvas.chapter, canvas.tiles, canvas.active_page_id, 500, 170)
    clone = canvas.chapter.layers[identifier]
    assert kind == "layer" and clone.layer_kind == "text_container" and clone.bound is None
    children = [canvas.chapter.objects[r.entity_id] for r in clone.children]
    assert {obj.text for obj in children} == {first.text, second.text}
    assert {obj.transform_behavior for obj in children} == {"bounds", "stretch"}
    assert {obj.font_size for obj in children} == {25, 18}
    cloned_effect = canvas.chapter.modifiers[clone.modifier_ids[0]]
    assert cloned_effect.angle == 30 and cloned_effect.modifier_id != radial.modifier_id
    canvas.chapter.validate()


def test_strict_hierarchy_drop_resolves_layout_before_reparent(canvas):
    from comic_editor.ui.tree_model import HierarchyModel
    parent = group(canvas)
    parent.translate_x = 30
    box = canvas.chapter.add_object(canvas.active_page_id, TextObject(width=15, height=500))
    rect = canvas._strict_text_rect(box)
    model = HierarchyModel(canvas.chapter)
    model.prepare_text_move = canvas.prepare_text_move
    mime = model.mimeData([model.index_for_entity("object", box.object_id)])
    assert model.dropMimeData(mime, Qt.MoveAction, 0, 0, model.index_for_entity("layer", parent.layer_id))
    assert box.layout_mode == "free" and box.transform_behavior == "bounds"
    assert box.width == rect.width() and box.height == rect.height()
    np.testing.assert_allclose(canvas.object_world_quad(box.object_id), canvas._rect_quad(rect))


@pytest.mark.parametrize("input_kind", ["mouse", "keyboard", "stylus"])
def test_blurs_submenu_leaf_actions_once(qapp, monkeypatch, input_kind):
    monkeypatch.setattr("comic_editor.ui.main_window.save_settings", lambda _: None)
    window = MainWindow()
    chapter, page = document()
    raster = chapter.add_object(page.layer_id, RasterObject())
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("object", raster.object_id)
    from comic_editor.ui.modifier_controls import ModifierControls
    controls = ModifierControls(window.canvas, window)
    try:
        window.show()
        controls.refresh()
        menu = controls.add_button.menu()
        blurs = next(a.menu() for a in menu.actions() if a.text() == "Blurs")
        assert [a.text() for a in blurs.actions()] == ["Blur", "Blur Legacy", "Radial Blur"]
        menu.popup(window.mapToGlobal(window.rect().center()))
        menu.setActiveAction(blurs.menuAction())
        QTest.keyClick(menu, Qt.Key_Right)
        qapp.processEvents()
        assert blurs.isVisible()
        action = blurs.actions()[2]
        point = QPointF(blurs.actionGeometry(action).center())
        if input_kind == "stylus":
            tablet(blurs, QEvent.TabletPress, point, True)
            tablet(blurs, QEvent.TabletRelease, point, False)
        elif input_kind == "keyboard":
            blurs.setActiveAction(action)
            QTest.keyClick(blurs, Qt.Key_Return)
        else:
            QTest.mouseClick(blurs, Qt.LeftButton, pos=point.toPoint())
        qapp.processEvents()
        assert len(chapter.modifiers) == 1
        effect = next(iter(chapter.modifiers.values()))
        assert isinstance(effect, RadialBlurModifier)
        card = controls._cards[effect.modifier_id]
        angle = next(w for w in card.findChildren(QSpinBox) if w.maximum() == 360)
        angle.setValue(60)
        angle.editingFinished.emit()
        assert effect.angle == 60
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.modifiers[effect.modifier_id].angle == 15
    finally:
        for popup in window.findChildren(QMenu):
            popup.close()
        window.hide()
        window.deleteLater()


def test_container_rasterize_preserves_opacity_clipping_and_undo(canvas):
    from comic_editor.ui.baking import rasterize, rasterize_reason
    shape = canvas.chapter.add_layer(canvas.active_page_id, "Clip", BoundGeometry.rectangle(80, 50, 250, 140))
    shape.fill_color, shape.border_width, shape.opacity = None, 0, .7
    parent = canvas.chapter.add_layer(shape.layer_id, "Text group", layer_kind="text_container")
    parent.opacity = .6
    text = add_box(canvas, parent.layer_id)
    canvas.set_selection("layer", parent.layer_id)
    before = _qimage_premultiplied(render(canvas))
    assert np.any(before[..., 3] > 0)
    assert rasterize_reason(canvas, "object", text.object_id)
    rasterize(canvas, "layer", parent.layer_id)
    assert isinstance(canvas.chapter.objects[parent.layer_id], ImageObject)
    np.testing.assert_allclose(_qimage_premultiplied(render(canvas)), before, atol=1/255)
    canvas.command_stack.undo()
    np.testing.assert_array_equal(_qimage_premultiplied(render(canvas)), before)
    canvas.chapter.validate()


@pytest.mark.parametrize("transformed", [False, True])
@pytest.mark.parametrize("later_blur", [False, True])
def test_radial_raster_apply_and_mask_consistency(canvas, transformed, later_blur):
    from comic_editor.ui.baking import apply_raster_modifiers
    from comic_editor.core.models import ToneMask
    obj = canvas.chapter.add_object(canvas.active_page_id, RasterObject(interaction_rect=(100, 100, 100, 100)))
    if transformed:
        obj.transform_frame = (100, 100, 100, 100)
        obj.transform_quad = [(120, 80), (250, 100), (260, 210), (100, 200)]
    canvas.tiles.paint_dab(obj.object_id, QPointF(145, 145), 20, QColor("#80FF00FF"), square=True, antialias=False)
    radial = RadialBlurModifier(center=(120, 140), angle=65)
    canvas.chapter.add_modifier(radial, [("object", obj.object_id)])
    if later_blur:
        blur = BlurModifier(strength=3)
        canvas.chapter.add_modifier(blur, [("object", obj.object_id)])
    mask = ToneMask(contributors=[("object", obj.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    before = _qimage_premultiplied(render(canvas))
    field = canvas.render_tone_mask_field(mask.mask_id, canvas.chapter.width, canvas.chapter.height,
        QTransform(), QRectF(0, 0, canvas.chapter.width, canvas.chapter.height))
    np.testing.assert_allclose(field, before[..., 3], atol=1/255)
    canvas.set_selection("object", obj.object_id)
    apply_raster_modifiers(canvas, radial.modifier_id)
    after = _qimage_premultiplied(render(canvas))
    np.testing.assert_allclose(after, before, atol=1/255)
    assert canvas.chapter.objects[obj.object_id].modifier_ids == ([blur.modifier_id] if later_blur else [])
    restored = ChapterDocument.from_dict(canvas.chapter.to_dict())
    assert restored.objects[obj.object_id].modifier_source_frame is not None
    canvas.replace_chapter(restored.to_dict())
    np.testing.assert_allclose(_qimage_premultiplied(render(canvas)), after, atol=1/255)
    canvas.command_stack.undo()
    np.testing.assert_array_equal(_qimage_premultiplied(render(canvas)), before)


def test_live_radial_finishes_to_same_result_as_export(canvas):
    obj = canvas.chapter.add_object(canvas.active_page_id, RasterObject(interaction_rect=(100, 100, 100, 100)))
    canvas.tiles.paint_dab(obj.object_id, QPointF(145, 145), 20, QColor("#80FF00FF"), square=True, antialias=False)
    radial = RadialBlurModifier(center=(120, 140), angle=65)
    canvas.chapter.add_modifier(radial, [("object", obj.object_id)])
    canvas._interactive_render = True
    first = render(canvas)
    assert canvas._effect_jobs.submitted == 1
    deadline = time.monotonic()+3
    while (canvas._effect_jobs.running or canvas._effect_jobs.pending) and time.monotonic() < deadline:
        canvas._effect_jobs.poll()
        time.sleep(.005)  # Unlike QTest.qWait, release the GIL to the worker.
    live = _qimage_premultiplied(render(canvas))
    canvas._interactive_render = False
    final = _qimage_premultiplied(render(canvas))
    assert canvas._effect_jobs.completed == 1
    assert np.any(live != _qimage_premultiplied(first))
    np.testing.assert_array_equal(live, final)


@pytest.mark.parametrize("stylus", [False, True])
def test_radial_center_actual_release_snaps_and_commits_once(canvas, stylus):
    parent = group(canvas)
    add_box(canvas, parent.layer_id)
    radial = RadialBlurModifier(center=(100, 100))
    canvas.chapter.add_modifier(radial, [("layer", parent.layer_id)])
    canvas.set_selection("layer", parent.layer_id)
    canvas.modifier_mode = True
    canvas.active_modifier_id = radial.modifier_id
    canvas.settings.snap_to_grid = True
    start = canvas.document_to_widget(QPointF(100, 100))
    end = canvas.document_to_widget(QPointF(127, 153))
    expected = canvas._snap(QPointF(127, 153), parent.layer_id).toTuple()
    if stylus:
        tablet(canvas, QEvent.TabletPress, start, True)
        tablet(canvas, QEvent.TabletRelease, end, False)
    else:
        QTest.mousePress(canvas, Qt.LeftButton, pos=start.toPoint())
        QTest.mouseRelease(canvas, Qt.LeftButton, pos=end.toPoint())
    assert radial.center == expected
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[radial.modifier_id].center == (100, 100)
    assert not canvas.command_stack.can_undo


def test_radial_single_and_shared_centers_follow_transform_rules(canvas):
    first, second = group(canvas), group(canvas)
    add_box(canvas, first.layer_id)
    add_box(canvas, second.layer_id)
    sole, shared = RadialBlurModifier(center=(100, 100)), RadialBlurModifier(center=(100, 100))
    canvas.chapter.add_modifier(sole, [("layer", first.layer_id)])
    canvas.chapter.add_modifier(shared, [("layer", first.layer_id), ("layer", second.layer_id)])
    canvas._transform_single_target_focal_modifiers("layer", first.layer_id, QTransform.fromTranslate(30, 40))
    assert sole.center == (130, 140)
    assert shared.center == (100, 100)


def test_effect_worker_stops_when_its_canvas_is_destroyed(qapp):
    from PySide6.QtCore import QObject
    from threading import Event
    from comic_editor.ui.effect_jobs import EffectJobs
    owner = QObject()
    jobs = EffectJobs(owner)
    entered = Event()
    def work(cancelled):
        entered.set()
        deadline = time.monotonic()+2
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(.005)
        return cancelled()
    jobs.request(("test",), ("shutdown",), work, 1)
    future = jobs.running[3]
    assert entered.wait(1)
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert future.result(timeout=1) is True


@pytest.mark.parametrize("kind", ["object", "layer"])
def test_zero_radial_does_not_change_existing_legacy_stack(canvas, kind):
    shape = canvas.chapter.add_layer(canvas.active_page_id, "Shape", BoundGeometry.rectangle(60, 60, 220, 180))
    shape.fill_color = None
    obj = canvas.chapter.add_object(shape.layer_id, RasterObject(interaction_rect=(100, 100, 100, 100),
        transform_frame=(100, 100, 100, 100), transform_quad=[(100, 80), (260, 100), (250, 210), (80, 180)]))
    canvas.tiles.paint_dab(obj.object_id, QPointF(145, 145), 20, QColor("#80FF00FF"), square=True, antialias=False)
    target = (kind, obj.object_id if kind == "object" else shape.layer_id)
    canvas.chapter.add_modifier(BlurModifier(algorithm="legacy", strength=4), [target])
    before = _qimage_premultiplied(render(canvas))
    canvas.chapter.add_modifier(RadialBlurModifier(angle=0, center=(140, 130)), [target])
    np.testing.assert_array_equal(_qimage_premultiplied(render(canvas)), before)
