"""Slider edits retain unrelated exact tiles and replace every affected pixel."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from shiboken6 import isValid

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    ArrayModifier, BlurModifier, BoundGeometry, BrightnessContrastModifier,
    ChapterDocument, CurvesModifier, HalftoneModifier, HueSaturationLightnessModifier,
    ImageObject, OutlineModifier, ParameterMaskBinding, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_edit_bounds import modifier_edit_bounds
from comic_editor.ui.cache_dependencies import exact_cache_allowed


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=2048)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 2048))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.setMinimumSize(1, 1)
    canvas.resize(1080, 2048)
    canvas.set_document(chapter, TileStore(), ImageStore(), reset_view=False)
    canvas.center_x, canvas.center_y, canvas.scale = 540, 1024, 1.
    canvas._projection_async_enabled = False
    controls = ModifierControls(canvas)

    def image(parent=page, x=60, y=70):
        target = chapter.add_object(parent.layer_id, ImageObject(
            x=x, y=y, pixel_width=140, pixel_height=110))
        source = QImage(140, 110, QImage.Format_ARGB32_Premultiplied)
        source.fill(QColor(120, 70, 45, 200))
        canvas.images.put_decoded(target.object_id, "test.png", b"", source)
        return target

    target = image()
    other = image(x=720, y=1550)
    canvas.set_selection("object", target.object_id, activate_default_tool=False)
    yield canvas, controls, page, target, other, image
    canvas._effect_jobs.cancel()
    if isValid(controls):
        controls.deleteLater()
    canvas.deleteLater()


def snapshots(canvas):
    canvas._collect_document_projection()
    return {address: QImage(tile.image)
            for address, tile in canvas._document_projection.tiles.items() if tile.valid}


def assert_exact_oracle(canvas, actual):
    canvas._render_service.invalidate()
    assert snapshots(canvas) == actual


def test_selection_style_refresh_skips_identical_qt_styles_and_keeps_transitions(scene, monkeypatch):
    canvas, controls, _page, target, _other, _image = scene
    first, second = BrightnessContrastModifier(), OutlineModifier()
    for modifier in (first, second):
        canvas.chapter.add_modifier(modifier, [('object', target.object_id)])
    canvas.modifier_mode = True
    canvas.active_modifier_id = first.modifier_id
    controls.refresh()
    calls = []
    for identifier, card in controls._cards.items():
        original = card.setStyleSheet
        def set_style(style, _identifier=identifier, _original=original):
            calls.append(_identifier)
            return _original(style)
        monkeypatch.setattr(card, 'setStyleSheet', set_style)
    controls._refresh_selection_style()
    assert calls == []
    canvas.active_modifier_id = second.modifier_id
    controls._refresh_selection_style()
    assert set(calls) == {first.modifier_id, second.modifier_id}
    assert '#65bcff' in controls._cards[second.modifier_id].styleSheet()
    assert '#65bcff' not in controls._cards[first.modifier_id].styleSheet()
    calls.clear()
    controls._refresh_selection_style()
    assert calls == []
    canvas.modifier_mode = False
    controls._refresh_selection_style()
    assert calls == [second.modifier_id]
    assert '#65bcff' not in controls._cards[second.modifier_id].styleSheet()


@pytest.mark.parametrize("modifier,attribute,value", [
    (BrightnessContrastModifier(), "brightness", 35),
    (HueSaturationLightnessModifier(), "hue", 75),
    (BlurModifier(strength=4), "strength", 12),
    (OutlineModifier(thickness=12), "thickness", 3),
])
def test_slider_preserves_unrelated_projection_tiles_and_exact_pixels(scene, modifier, attribute, value):
    canvas, controls, _page, target, _other, _image = scene
    canvas.chapter.add_modifier(modifier, [("object", target.object_id)])
    original = snapshots(canvas)
    retained = {address: tile for address, tile in canvas._document_projection.tiles.items()
                if tile.request.world_rect.top() >= 1024}
    signals = []
    canvas.documentChanged.connect(signals.append)
    revision = canvas._document_projection.revision
    controls.begin_parameter_drag(modifier.modifier_id)
    controls.set_parameter(modifier.modifier_id, attribute, value, False)
    assert canvas._document_projection.revision == revision + 1
    assert signals[-1] is not None and signals[-1].contains(QRectF(60, 70, 140, 110))
    assert all(tile.valid for tile in retained.values())
    assert any(not tile.valid for tile in canvas._document_projection.tiles.values())
    actual = snapshots(canvas)
    assert actual != original
    controls.finish_parameter_drag()
    assert len(signals) == 1, "History commit must not invalidate the complete document again"
    assert all(canvas._document_projection.tiles[address] is tile
               for address, tile in retained.items())
    assert_exact_oracle(canvas, actual)
    canvas.command_stack.undo()
    assert snapshots(canvas) == original
    canvas.command_stack.redo()
    assert snapshots(canvas) == actual


def test_outline_contraction_clears_old_halo_across_tile_boundary(scene):
    canvas, controls, _page, target, _other, _image = scene
    target.x, target.y = 210, 945
    modifier = OutlineModifier(thickness=25)
    canvas.chapter.add_modifier(modifier, [("object", target.object_id)])
    before = snapshots(canvas)
    dirty = []
    canvas.documentChanged.connect(dirty.append)
    controls.set_parameter(modifier.modifier_id, "thickness", 1, True)
    assert dirty[-1].contains(QRectF(185, 920, 190, 160))
    after = snapshots(canvas)
    assert after != before
    assert_exact_oracle(canvas, after)


def test_shared_modifier_notifies_both_owners(scene):
    canvas, controls, _page, target, other, _image = scene
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [("object", target.object_id), ("object", other.object_id)])
    before = snapshots(canvas)
    dirty = []
    canvas.documentChanged.connect(dirty.append)
    controls.set_parameter(modifier.modifier_id, "brightness", 40, True)
    assert dirty[-1].contains(QRectF(60, 70, 140, 110))
    assert dirty[-1].contains(QRectF(720, 1550, 140, 110))
    after = snapshots(canvas)
    assert after != before
    assert_exact_oracle(canvas, after)


@pytest.mark.parametrize("dependency", ["object_mask", "ancestor_mask", "halftone", "ancestor_effect", "compound", "unknown"])
def test_remote_or_unknown_dependencies_keep_full_invalidation(scene, dependency):
    canvas, controls, page, target, other, _image = scene
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [("object", target.object_id)])
    if dependency in {"object_mask", "ancestor_mask"}:
        mask = ToneMask(contributors=[("object", target.object_id)] if dependency == "object_mask"
                        else [("layer", page.layer_id)])
        canvas.chapter.masks[mask.mask_id] = mask
        other.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    elif dependency == "halftone":
        canvas.chapter.add_modifier(HalftoneModifier(color_mode="target_layer", target_layer_id=target.object_id),
                                   [("object", other.object_id)])
    elif dependency == "ancestor_effect":
        canvas.chapter.add_modifier(CurvesModifier(), [("layer", page.layer_id)])
    elif dependency == "compound":
        page.compound_enabled = True
    else:
        canvas.chapter.add_modifier(ArrayModifier(count=2), [("object", target.object_id)])
    snapshots(canvas)
    dirty = []
    canvas.documentChanged.connect(dirty.append)
    controls.set_parameter(modifier.modifier_id, "brightness", 40, False)
    assert dirty[-1] is None
    assert all(not tile.valid for tile in canvas._document_projection.tiles.values())


def test_reparented_owner_uses_its_new_ancestor_transform(scene):
    canvas, controls, page, target, _other, _image = scene
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [("object", target.object_id)])
    assert modifier_edit_bounds(canvas, modifier.modifier_id).contains(QRectF(60, 70, 140, 110))
    parent = canvas.chapter.add_layer(page.layer_id, "Moved parent", BoundGeometry.rectangle(0, 0, 600, 600))
    parent.translate_x, parent.translate_y = 300, 900
    canvas.chapter.move_entity("object", target.object_id, parent.layer_id, 0)
    before = snapshots(canvas)
    dirty = []
    canvas.documentChanged.connect(dirty.append)
    controls.set_parameter(modifier.modifier_id, "brightness", 40, False)
    assert dirty[-1].contains(QRectF(360, 970, 140, 110))
    after = snapshots(canvas)
    assert after != before
    assert_exact_oracle(canvas, after)


@pytest.mark.parametrize("drag", [False, True])
def test_muted_parameter_edits_still_notify_persistence_and_record_history(scene, drag):
    canvas, controls, _page, target, _other, _image = scene
    modifier = BrightnessContrastModifier(muted=True)
    canvas.chapter.add_modifier(modifier, [("object", target.object_id)])
    dirty = []
    canvas.documentChanged.connect(dirty.append)
    if drag:
        controls.begin_parameter_drag(modifier.modifier_id)
    controls.set_parameter(modifier.modifier_id, "brightness", 40, not drag)
    if drag:
        controls.finish_parameter_drag()
    assert dirty, "Muted settings still belong to the saved document"
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].brightness == 0
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[modifier.modifier_id].brightness == 40


def presentation(canvas):
    result = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor("#242428"))
    painter = QPainter(result)
    try:
        canvas._paint_document_projection(painter, interactive=True)
    finally:
        painter.end()
    return result


def test_live_slider_presents_changed_pixels_without_publishing_exact_tiles(scene, monkeypatch):
    canvas, controls, _page, target, _other, _image = scene
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [("object", target.object_id)])
    before = presentation(canvas)
    exact = snapshots(canvas)
    controls.begin_parameter_drag(modifier.modifier_id)
    controls.set_parameter(modifier.modifier_id, "brightness", 40, False)
    assert canvas._projection_has_live_preview()
    # A live value must use the established transient scene kernels, not
    # synchronously regenerate projection tiles for every pointer sample.
    with monkeypatch.context() as patch:
        patch.setattr(canvas, "_collect_document_projection", lambda *_args, **_kwargs:
                      pytest.fail("Live slider must not publish finished projection tiles"))
        draft = presentation(canvas)
    assert draft != before
    assert all(tile.image == exact[address]
               for address, tile in canvas._document_projection.tiles.items())
    with monkeypatch.context() as patch:
        patch.setattr(canvas, "_projection_exact", True)
        assert not exact_cache_allowed(canvas, ("modifier-source", "test"))
    controls.finish_parameter_drag()
    assert not canvas._projection_has_live_preview()
    final = presentation(canvas)
    assert final != before
    actual = snapshots(canvas)
    assert_exact_oracle(canvas, actual)


@pytest.mark.parametrize("interruption", ["refresh", "destroy", "history", "chapter", "clear"])
def test_parameter_gesture_cannot_survive_its_inspector_or_document(scene, interruption):
    canvas, controls, _page, target, _other, _image = scene
    modifier = BrightnessContrastModifier()
    canvas.chapter.add_modifier(modifier, [("object", target.object_id)])
    # Establish an undoable revision before interrupting a later live edit.
    controls.set_parameter(modifier.modifier_id, "brightness", 10, True)
    controls.begin_parameter_drag(modifier.modifier_id)
    controls.set_parameter(modifier.modifier_id, "brightness", 40, False)
    assert canvas._projection_has_live_preview()
    if interruption == "refresh":
        controls.refresh()
    elif interruption == "destroy":
        controls.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    elif interruption == "history":
        canvas.command_stack.undo()
        revision = canvas.command_stack.revision
        controls.finish_parameter_drag()
        assert canvas.command_stack.revision == revision
        assert modifier.brightness == 0
    elif interruption == "chapter":
        chapter = ChapterDocument(height=2048)
        chapter.add_page("Replacement", BoundGeometry.rectangle(0, 0, 1080, 2048))
        canvas.set_document(chapter, TileStore())
    else:
        canvas.clear_document()
    assert not getattr(canvas, "_modifier_parameter_drag_id", None)
    assert not canvas._projection_has_live_preview()


def test_mask_endpoint_slider_uses_same_transient_scope_and_exact_release(scene):
    canvas, controls, _page, target, _other, _image = scene
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    modifier = BlurModifier(strength=4, parameter_masks={
        "strength": ParameterMaskBinding(mask.mask_id, 4, 8)})
    canvas.chapter.add_modifier(modifier, [("object", target.object_id)])
    controls.set_mask_endpoints(modifier.modifier_id, "strength", 6, 12, False)
    assert canvas._projection_has_live_preview()
    controls.finish_parameter_drag()
    assert not canvas._projection_has_live_preview()
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].parameter_masks["strength"].black_value == 4
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[modifier.modifier_id].parameter_masks["strength"].black_value == 6
