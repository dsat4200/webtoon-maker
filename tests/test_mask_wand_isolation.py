"""Wand sampling isolates chosen artwork without changing the editor view."""
from contextlib import contextmanager

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HalftoneModifier, ParameterMaskBinding,
    RasterObject, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.halftone_source import render_color_source


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    chapter = ChapterDocument(width=128, height=96, background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 128, 96))
    page.fill_color, page.border_width = None, 0
    group = chapter.add_layer(page.layer_id, "Selected layer", BoundGeometry.rectangle(0, 0, 128, 96))
    group.fill_color, group.border_width = None, 0
    other = chapter.add_layer(page.layer_id, "Other layer", BoundGeometry.rectangle(0, 0, 128, 96))
    other.fill_color, other.border_width = None, 0
    sibling = chapter.add_object(group.layer_id, RasterObject(name="Sibling"))
    selected = chapter.add_object(group.layer_id, RasterObject(name="Selected"))
    barrier = chapter.add_object(other.layer_id, RasterObject(name="Barrier", show_on_top=True))
    tiles = TileStore(tile_size=128)
    for obj, rect, color in ((selected, QRectF(0, 0, 128, 96), "white"),
                             (sibling, QRectF(0, 64, 40, 32), "blue"),
                             (barrier, QRectF(60, 0, 8, 96), "black")):
        pixels = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
        pixels.fill(Qt.transparent)
        painter = QPainter(pixels)
        painter.fillRect(rect, QColor(color))
        painter.end()
        tiles.set_tile(obj.object_id, (0, 0), pixels)
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, tiles)
    yield canvas, group, selected, sibling, barrier
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


@contextmanager
def sampling(canvas, entries):
    previous = getattr(canvas, "_mask_wand_sample_entities", None)
    canvas._mask_wand_sample_entities = frozenset(entries)
    try:
        yield
    finally:
        canvas._mask_wand_sample_entities = previous


def render(canvas):
    result = QImage(128, 96, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(result)
    return result


def test_sample_object_excludes_sibling_and_promoted_barrier_without_view_changes(scene, monkeypatch):
    canvas, group, selected, sibling, barrier = scene
    normal = render(canvas)
    assert normal.pixelColor(64, 30) == QColor("black")
    assert normal.pixelColor(20, 80) == QColor("blue")
    document = canvas.chapter.to_dict()
    signals = []
    canvas.soloChanged.connect(lambda *_: signals.append("solo"))
    canvas.visualChanged.connect(lambda *_: signals.append("visual"))
    with monkeypatch.context() as patch:
        patch.setattr(canvas, "set_solo_entities", lambda *_: pytest.fail("Sampling changed solo"))
        patch.setattr(canvas._effect_jobs, "cancel", lambda **_: pytest.fail("Sampling canceled work"))
        with sampling(canvas, {("object", selected.object_id)}):
            isolated = render(canvas)
    assert isolated.pixelColor(64, 30) == QColor("white")
    assert isolated.pixelColor(20, 80) == QColor("white")
    assert render(canvas) == normal
    assert canvas.chapter.to_dict() == document and not canvas.solo_entities
    assert not signals


def test_sample_layer_keeps_its_descendants_and_overrides_ordinary_solo(scene):
    canvas, group, selected, sibling, barrier = scene
    canvas.set_solo_entities({("object", barrier.object_id)})
    ordinary = render(canvas)
    with sampling(canvas, {("layer", group.layer_id)}):
        isolated = render(canvas)
    assert isolated.pixelColor(64, 30) == QColor("white")
    assert isolated.pixelColor(20, 80) == QColor("blue")
    assert render(canvas) == ordinary
    assert canvas.solo_entities == {("object", barrier.object_id)}


def test_wand_signature_separates_promoted_content_from_ordinary_solo(scene):
    canvas, group, selected, sibling, barrier = scene
    entries = {("object", selected.object_id)}
    canvas.set_solo_entities(entries)
    ordinary_signature = canvas._solo_signature()
    assert render(canvas).pixelColor(64, 30) == QColor("black")
    with sampling(canvas, entries):
        assert canvas._solo_signature() != ordinary_signature
        assert render(canvas).pixelColor(64, 30) == QColor("white")
        with canvas.without_solo():
            assert canvas._solo_signature() == ()
            assert render(canvas).pixelColor(64, 30) == QColor("black")
    assert canvas._solo_signature() == ordinary_signature


@pytest.mark.parametrize("entries", [frozenset(), frozenset({("object", "removed")})])
def test_empty_or_removed_sample_never_falls_back_to_whole_scene(scene, entries):
    canvas, *_ = scene
    with sampling(canvas, entries):
        assert canvas._solo_signature()
        assert render(canvas).pixelColor(64, 30).alpha() == 0


def test_sample_keeps_independent_mask_contributors(scene):
    canvas, group, selected, sibling, barrier = scene
    barrier.mask_only = True
    mask = ToneMask(contributors=[("object", barrier.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    selected.opacity_mask = ParameterMaskBinding(mask.mask_id, 0., 1.)
    with sampling(canvas, {("object", selected.object_id)}):
        isolated = render(canvas)
    assert isolated.pixelColor(64, 30) == QColor("white")
    assert isolated.pixelColor(40, 30).alpha() == 0


@pytest.mark.parametrize("target", ["object", "layer"])
def test_sample_keeps_independent_halftone_color_sources(scene, target):
    canvas, group, selected, sibling, barrier = scene
    target_id = barrier.object_id if target == "object" else barrier.parent_layer_id
    effect = HalftoneModifier(color_mode="target_layer", target_layer_id=target_id)
    source = QImage(128, 96, QImage.Format_ARGB32_Premultiplied)
    source.fill(Qt.transparent)
    with sampling(canvas, {("object", selected.object_id)}):
        colors = render_color_source(canvas, effect, source, QRectF(0, 0, 128, 96), QTransform())
    assert colors.pixelColor(64, 30) == QColor("black")
    assert colors.pixelColor(40, 30).alpha() == 0


@pytest.mark.parametrize("connected", [True, False])
def test_actual_wand_toggle_ignores_barrier_for_selected_modifier_mask(scene, connected):
    canvas, group, selected, sibling, barrier = scene
    mask = ToneMask(saved=True, name="Modifier mask")
    canvas.chapter.masks[mask.mask_id] = mask
    effect = BlurModifier(strength=0, parameter_masks={
        "intensity": ParameterMaskBinding(mask.mask_id, 0., 100.)})
    canvas.chapter.add_modifier(effect, [("object", selected.object_id)])
    canvas.set_selection("object", selected.object_id)
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas.settings.mask_wand_connected = connected
    canvas.settings.mask_wand_tolerance = 0
    assert canvas.settings.mask_wand_ignore_other_layers is False

    def click_and_read():
        canvas._mask_wand_press(QPointF(20, 30), Qt.NoModifier)
        assert getattr(canvas, "_mask_wand_sample_entities", None) is None
        return canvas.render_tone_mask_field(mask.mask_id, 128, 96,
            QTransform(), QRectF(0, 0, 128, 96))

    ordinary = click_and_read()
    assert ordinary[30, 20] == 1 and ordinary[30, 64] == 0
    assert ordinary[30, 100] == (0 if connected else 1)
    canvas.command_stack.undo()
    canvas.settings.mask_wand_ignore_other_layers = True
    isolated = click_and_read()
    assert isolated[30, 20] == isolated[30, 64] == isolated[30, 100] == 1
    canvas.command_stack.undo()
    canvas.settings.mask_wand_ignore_other_layers = False
    restored = click_and_read()
    assert (restored == ordinary).all()
    assert canvas.active_tone_mask_id == mask.mask_id
    assert canvas.selected_object_id == selected.object_id
    assert not canvas.solo_entities
