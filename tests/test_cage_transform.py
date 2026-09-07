"""Behavioral coverage for cage tool and modifier workflows."""
import copy
import time
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform
from comic_editor.core.cage import CageGrid, map_points
from comic_editor.core.models import (BoundGeometry, ChapterDocument, CageTransformModifier,
    HueSaturationLightnessModifier, ImageObject, RasterObject, ShapeStyle, TextObject,
    VectorDrawingObject, VectorStroke, VectorStrokePoint, modifier_from_dict)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.cage_rendering import warp_image
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_rendering import _qimage_premultiplied


def scene(qapp, width=240, height=160):
    doc = ChapterDocument(document_kind="image", width=width, height=height, background="#00000000")
    page = doc.add_page(bound=BoundGeometry.rectangle(0, 0, width, height), style=ShapeStyle(primary_color=None, outline_thickness=0))
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(width, height)
    canvas.set_document(doc, TileStore())
    canvas.scale = 1
    canvas.center_x, canvas.center_y = width/2, height/2
    return canvas, doc, page


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    canvas._interactive_render = False
    for identifier in canvas.chapter.root_page_ids:
        canvas._render_layer(painter, canvas.chapter.layers[identifier], 1., QRectF(0, 0, image.width(), image.height()))
    painter.end()
    return image


def translated(grid, dx, dy):
    grid.validate_grid()
    grid.points = [(x+dx, y+dy) for x, y in grid.points]
    return grid


def test_grid_affine_exactness_density_and_validation():
    grid = CageGrid(frame=(-12, 23, 70, 80))
    grid.validate_grid()
    rng = np.random.default_rng(1)
    samples = rng.random((300, 2))*(70, 80)+(-12, 23)
    np.testing.assert_allclose(map_points(grid, samples), samples, atol=1e-9)
    affine = np.array(((1.2, .2), (-.1, .8)))
    grid.points = [tuple(p) for p in grid.rest_points() @ affine + (14, -9)]
    expected = samples @ affine + (14, -9)
    for smoothness in (0, 50, 100):
        grid.smoothness = smoothness
        np.testing.assert_allclose(map_points(grid, samples), expected, atol=1e-8)
    grid.resample(7, 5)
    np.testing.assert_allclose(map_points(grid, samples), expected, atol=1e-8)
    grid.points[0] = (float("nan"), 0)
    with pytest.raises(ValueError, match="finite"):
        grid.validate_grid()


@pytest.mark.parametrize("interpolation", ["nearest", "bilinear", "bicubic"])
def test_warp_identity_translation_and_flip(interpolation):
    image = QImage(32, 24, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.fillRect(3, 4, 9, 11, QColor("#c03080"))
    painter.end()
    grid = CageGrid(frame=(0, 0, 32, 24), interpolation=interpolation)
    grid.validate_grid()
    identity, bounds = warp_image(image, QRectF(0, 0, 32, 24), grid)
    np.testing.assert_allclose(_qimage_premultiplied(identity), _qimage_premultiplied(image), atol=1/255)
    translated(grid, 14, -3)
    warped, bounds = warp_image(image, QRectF(0, 0, 32, 24), grid)
    assert bounds == QRectF(14, -3, 32, 24)
    np.testing.assert_allclose(_qimage_premultiplied(warped), _qimage_premultiplied(image), atol=1/255)
    grid.points = [(32-x, y) for x, y in grid.rest_points()]
    flipped, _ = warp_image(image, QRectF(0, 0, 32, 24), grid)
    np.testing.assert_allclose(_qimage_premultiplied(flipped), _qimage_premultiplied(image)[:, ::-1], atol=1/255)


def test_compatibility_atomic_shared_and_round_trip(qapp):
    canvas, doc, page = scene(qapp)
    layer = doc.add_layer(page.layer_id)
    image = doc.add_object(page.layer_id, ImageObject(name="Photo"))
    raster = doc.add_object(page.layer_id, RasterObject(name="Ink"))
    cage = CageTransformModifier()
    before = doc.to_dict()
    with pytest.raises(ValueError, match="Ink"):
        doc.add_modifier(cage, [("object", image.object_id), ("object", raster.object_id)])
    assert doc.to_dict() == before
    refs = [("object", image.object_id), ("layer", layer.layer_id)]
    doc.add_modifier(cage, refs)
    assert doc.modifier_target_ids(cage.modifier_id) == list(reversed(refs))
    with pytest.raises(ValueError, match="Ink"):
        doc.set_modifier_targets(cage.modifier_id, refs+[("object", raster.object_id)])
    loaded = ChapterDocument.from_dict(doc.to_dict())
    assert loaded.modifiers[cage.modifier_id].to_dict() == cage.to_dict()
    canvas.close()


def test_removed_tile_metadata_keeps_shape_artwork_on_load(qapp):
    canvas, doc, page = scene(qapp)
    shape = doc.add_layer(page.layer_id, bound=BoundGeometry.rectangle(10, 10, 30, 30),
                         style=ShapeStyle(primary_color=None, outline_thickness=3))
    ink = doc.add_object(shape.layer_id, RasterObject())
    canvas.tiles.paint_dab(ink.object_id, QPointF(25, 25), 8, QColor("red"))
    expected = render(canvas)
    data = doc.to_dict()
    saved_shape = next(layer for layer in data["layers"] if layer["id"] == shape.layer_id)
    saved_shape["repeating_texture"] = True
    saved_shape["tile_settings"] = {"tiling": "hex", "scale": 2, "rotation": 35}
    loaded = ChapterDocument.from_dict(data)
    canvas.set_document(loaded, canvas.tiles)
    assert render(canvas) == expected
    assert loaded.layers[shape.layer_id].children == shape.children
    assert loaded.objects[ink.object_id].to_dict() == ink.to_dict()
    assert "repeating_texture" not in loaded.layers[shape.layer_id].to_dict()
    assert "tile_settings" not in loaded.layers[shape.layer_id].to_dict()
    canvas.close()






def test_cage_cropped_output_keeps_displaced_source_and_intensity(qapp):
    from comic_editor.ui.effect_pipeline import render_stages
    canvas, doc, page = scene(qapp)
    shape = doc.add_layer(page.layer_id, bound=BoundGeometry.rectangle(0, 0, 30, 30))
    cage = translated(CageTransformModifier(frame=(0, 0, 30, 30), intensity=50), 100, 0)
    doc.add_modifier(cage, [("layer", shape.layer_id)])
    source = QImage(30, 30, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("red"))
    cropped, bounds = render_stages(canvas, source, QRectF(0, 0, 30, 30), [cage],
        QTransform(), required=QRectF(105, 5, 10, 10))
    assert bounds == QRectF(105, 5, 10, 10)
    assert cropped.pixelColor(5, 5).alpha() in (127, 128)
    assert cropped.pixelColor(5, 5).red() == 255
    canvas.close()






def test_cage_raster_and_vector_commit_cancel_undo(qapp):
    canvas, doc, page = scene(qapp)
    raster = doc.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 100, 80)))
    vector = doc.add_object(page.layer_id, VectorDrawingObject(strokes=[VectorStroke(points=[VectorStrokePoint(x=20, y=50), VectorStrokePoint(x=60, y=50)])]))
    canvas.tiles.paint_dab(raster.object_id, QPointF(30, 30), 10, QColor("red"))
    refs = [("object", raster.object_id), ("object", vector.object_id)]
    canvas.set_selection_set(refs)
    before = render(canvas)
    assert canvas.set_tool(ToolKind.CAGE_TRANSFORM)
    translated(canvas._active_cage(), 45, 0)
    canvas._cage_changed()
    preview = render(canvas)
    assert preview.pixelColor(75, 30).red() > 200
    assert not doc.modifiers
    assert canvas.finish_cage(False)
    assert render(canvas) == before
    assert canvas.set_tool(ToolKind.CAGE_TRANSFORM)
    translated(canvas._active_cage(), 45, 0)
    assert canvas.finish_cage(True)
    assert not doc.modifiers
    assert doc.objects[vector.object_id].strokes[0].points[0].x == pytest.approx(65)
    assert render(canvas).pixelColor(75, 30).red() > 200
    canvas.command_stack.undo()
    assert render(canvas) == before
    canvas.command_stack.redo()
    assert render(canvas).pixelColor(75, 30).red() > 200
    canvas.close()


def test_export_accepts_pending_cage_as_one_undoable_edit(qapp, tmp_path):
    window = MainWindow()
    canvas, doc, page = scene(qapp)
    raster = doc.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 100, 80)))
    canvas.tiles.paint_dab(raster.object_id, QPointF(30, 30), 10, QColor("red"))
    window.chapter = doc
    window.canvas.set_document(doc, canvas.tiles)
    window.hierarchy_model.set_chapter(doc)
    window.canvas.set_selection("object", raster.object_id)
    assert window.canvas.set_tool(ToolKind.CAGE_TRANSFORM)
    translated(window.canvas._active_cage(), 45, 0)
    destination = tmp_path/"accepted-cage.png"
    assert window._write_export_image(destination)
    assert window.canvas._cage_session is None
    exported = QImage(str(destination))
    assert exported.pixelColor(75, 30).red() > 200
    assert exported.pixelColor(30, 30).alpha() == 0
    window.canvas.command_stack.undo()
    assert render(window.canvas).pixelColor(30, 30).red() > 200
    canvas.close()
    window.deleteLater()


def test_shape_cage_warps_children_and_keeps_modifier_source(qapp):
    canvas, doc, page = scene(qapp)
    shape = doc.add_layer(page.layer_id, bound=BoundGeometry.rectangle(10, 10, 60, 70), style=ShapeStyle(primary_color="#ff00ff00", outline_thickness=0))
    raster = doc.add_object(shape.layer_id, RasterObject())
    canvas.tiles.paint_dab(raster.object_id, QPointF(40, 40), 10, QColor("red"))
    original = canvas.tiles.object_tiles(raster.object_id)
    cage = translated(CageTransformModifier(frame=(10, 10, 60, 70)), 90, 0)
    doc.add_modifier(cage, [("layer", shape.layer_id)])
    image = render(canvas)
    assert image.pixelColor(130, 40).red() > 200
    assert image.pixelColor(40, 40).alpha() == 0
    assert image.pixelColor(110, 20).green() > 200
    assert not raster.modifier_ids
    assert canvas.tiles.object_tiles(raster.object_id) == original
    canvas.close()


def test_ribbon_routing_selection_memory_errors(qapp, monkeypatch):
    window = MainWindow()
    canvas, doc, page = scene(qapp)
    shape = doc.add_layer(page.layer_id, bound=BoundGeometry.rectangle(10, 10, 30, 30))
    raster = doc.add_object(page.layer_id, RasterObject(name="Incompatible ink"))
    window.chapter = doc
    window.canvas.set_document(doc, canvas.tiles)
    window.hierarchy_model.set_chapter(doc)
    messages = []
    monkeypatch.setattr("comic_editor.ui.main_window.QMessageBox.warning", lambda *args: messages.append(args))
    window.canvas.set_selection("layer", shape.layer_id)
    window.canvas.set_tool(ToolKind.CAGE_TRANSFORM)
    assert window.canvas.modifier_mode
    cage_id = window.canvas.active_modifier_id
    assert cage_id in shape.modifier_ids
    assert window.canvas._active_cage() is not None
    window._select_ribbon_page("tool_settings")
    assert window.canvas._active_cage() is None
    window.canvas.clear_selection()
    window.canvas.set_selection("layer", shape.layer_id)
    window._select_ribbon_page("modifiers")
    assert window.canvas.active_modifier_id == cage_id
    assert window.canvas._active_cage() is doc.modifiers[cage_id]
    window.canvas.set_selection_set([("layer", shape.layer_id), ("object", raster.object_id)])
    window.modifier_controls.add_modifier("cage_transform")
    assert messages and "Incompatible ink" in messages[-1][-1]
    assert ("object", raster.object_id) in window.hierarchy_model.error_highlights
    canvas.close()
    window.deleteLater()


@pytest.mark.parametrize("modifier_type", ["mirror", "cage_transform", "blur", "hsl", "outline"])
def test_modifier_controls_reject_entire_incompatible_selection(qapp, modifier_type):
    canvas, doc, page = scene(qapp)
    controls = ModifierControls(canvas)
    messages = []
    canvas.operationError.connect(lambda title, message: messages.append(message))
    canvas.set_selection("layer", page.layer_id)
    before = doc.to_dict()
    controls.add_modifier(modifier_type)
    assert messages and page.name in messages[-1]
    assert doc.to_dict() == before
    shape = doc.add_layer(page.layer_id, bound=BoundGeometry.rectangle(10, 10, 30, 30))
    canvas.set_selection_set([("layer", shape.layer_id), ("layer", page.layer_id)])
    before = doc.to_dict()
    controls.add_modifier(modifier_type)
    assert doc.to_dict() == before
    assert not shape.modifier_ids
    controls.deleteLater()
    canvas.close()


def test_vector_interior_bends_without_losing_editability(qapp):
    from comic_editor.ui.cage_vectors import warp_vector
    obj = VectorDrawingObject(strokes=[VectorStroke(points=[
        VectorStrokePoint(x=0, y=50, width=3), VectorStrokePoint(x=100, y=50, width=5)])])
    grid = CageGrid(frame=(0, 0, 100, 100), columns=3, rows=3)
    grid.validate_grid()
    grid.points[4] = (50, 80)
    result = warp_vector(obj, grid, QTransform())
    assert len(result.strokes[0].points) > 2
    assert max(p.y for p in result.strokes[0].points) > 79
    assert result.strokes[0].points[0].point_id == obj.strokes[0].points[0].point_id
    assert result.strokes[0].points[-1].point_id == obj.strokes[0].points[-1].point_id
    result.validate_vector()


def test_cage_gizmos_move_selected_points_pivot_and_cancel(qapp):
    canvas, doc, page = scene(qapp)
    layer = doc.add_layer(page.layer_id, bound=BoundGeometry.rectangle(40, 40, 100, 80))
    cage = CageTransformModifier(frame=(40, 40, 100, 80))
    doc.add_modifier(cage, [("layer", layer.layer_id)])
    canvas.set_selection("layer", layer.layer_id)
    canvas.modifier_mode = True
    canvas._remember_modifier(cage.modifier_id)
    original = copy.deepcopy(cage.points)
    # Shift adds a second node; dragging that node moves both.
    point = canvas.document_to_widget(QPointF(*cage.points[0]))
    assert canvas._begin_cage_handle(point, Qt.NoModifier)
    canvas._finish_cage_handle()
    point = canvas.document_to_widget(QPointF(*cage.points[1]))
    canvas._begin_cage_handle(point, Qt.ShiftModifier)
    canvas._move_cage_handle(point+QPointF(10, 15))
    canvas._finish_cage_handle()
    assert np.asarray(cage.points)[:2] == pytest.approx(np.asarray(original)[:2]+(10, 15))
    assert cage.points[2:] == original[2:]
    canvas.finish_cage(False)
    assert doc.modifiers[cage.modifier_id].points == original
    canvas.close()


def test_cage_source_mapping_survives_moving_rotating_and_assets(qapp):
    from comic_editor.core.assets import extract_asset, instantiate_asset
    canvas, doc, page = scene(qapp)
    layer = doc.add_layer(page.layer_id, bound=BoundGeometry.rectangle(10, 10, 40, 40))
    cage = translated(CageTransformModifier(frame=(10, 10, 40, 40)), 20, 0)
    doc.add_modifier(cage, [("layer", layer.layer_id)])
    transform = QTransform().translate(30, 20).rotate(25)
    old = copy.deepcopy(cage)
    canvas._transform_single_target_focal_modifiers("layer", layer.layer_id, transform)
    samples = old.rest_points()
    from comic_editor.ui.cage_rendering import transform_points
    expected = transform_points(transform, map_points(old, samples))
    np.testing.assert_allclose(map_points(cage, transform_points(transform, samples)), expected, atol=1e-6)
    loaded = modifier_from_dict(cage.to_dict())
    np.testing.assert_allclose(loaded.points, cage.points)
    canvas.close()


def test_cage_worker_refines_and_discards_old_request(qapp):
    from comic_editor.ui.effect_pipeline import render_stages
    canvas, doc, page = scene(qapp, 320, 320)
    layer = doc.add_layer(page.layer_id, bound=BoundGeometry.rectangle(0, 0, 320, 320))
    grid = translated(CageTransformModifier(frame=(0, 0, 320, 320)), 0, 0)
    doc.add_modifier(grid, [("layer", layer.layer_id)])
    image = QImage(320, 320, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    canvas._interactive_render = True
    bounds = QRectF(0, 0, 320, 320)
    grid.points[5] = (145, 110)
    draft, rect = render_stages(canvas, image, bounds, [grid], QTransform(), request_scope=("layer", layer.layer_id, "canvas"))
    assert canvas._effect_jobs.submitted == 1
    assert not draft.isNull()
    grid.points[5] = (165, 120)
    render_stages(canvas, image, bounds, [grid], QTransform(), request_scope=("layer", layer.layer_id, "canvas"))
    deadline = time.monotonic()+5
    while (canvas._effect_jobs.running or canvas._effect_jobs.pending) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.005)
    assert canvas._effect_jobs.completed == 1
    assert canvas._effect_jobs.discarded >= 1
    assert canvas._effect_jobs.bytes_in_flight == 0
    canvas._interactive_render = False
    expected, expected_bounds = render_stages(canvas, image, bounds, [grid], QTransform())
    assert not expected.isNull()
    canvas.close()


def test_image_cage_preserves_original_and_updates_source(qapp):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from comic_editor.core.assets import extract_asset
    canvas, doc, page = scene(qapp)
    obj = doc.add_object(page.layer_id, ImageObject(pixel_width=32, pixel_height=32,
        transform_frame=(0, 0, 32, 32), transform_quad=[(20, 20), (52, 20), (52, 52), (20, 52)]))
    def source(color):
        image = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(color))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        assert image.save(buffer, "PNG")
        buffer.close()
        canvas.images.put_decoded(obj.object_id, "original.png", bytes(data), image)
        return bytes(data)
    original = source("red")
    cage = translated(CageTransformModifier(frame=(20, 20, 32, 32)), 90, 0)
    doc.add_modifier(cage, [("object", obj.object_id)])
    assert render(canvas).pixelColor(120, 30).red() > 250
    assert render(canvas).pixelColor(30, 30).alpha() == 0
    assert canvas.images.source(obj.object_id).data == original
    replacement = source("blue")
    assert render(canvas).pixelColor(120, 30).blue() > 250
    assert canvas.images.source(obj.object_id).data == replacement
    manifest, tiles, images = extract_asset(doc, canvas.tiles, "object", obj.object_id, "Caged image", source_images=canvas.images, include_images=True)
    cloned = next(iter(manifest.document.modifiers.values()))
    assert isinstance(cloned, CageTransformModifier)
    assert images.source(obj.object_id).data == replacement
    assert canvas.images.source(obj.object_id).data == replacement
    canvas.close()


def test_compound_cage_transports_contributor_geometry_and_children(qapp):
    canvas, doc, page = scene(qapp)
    compound = doc.add_layer(page.layer_id, bound=BoundGeometry.rectangle(5, 5, 15, 15), style=ShapeStyle(primary_color="green", outline_thickness=0))
    compound.compound_enabled = True
    child = doc.add_layer(compound.layer_id, bound=BoundGeometry.rectangle(25, 25, 30, 30))
    ink = doc.add_object(child.layer_id, RasterObject())
    canvas.tiles.paint_dab(ink.object_id, QPointF(40, 40), 8, QColor("red"))
    cage = translated(CageTransformModifier(frame=(25, 25, 30, 30)), 100, 0)
    doc.add_modifier(cage, [("layer", child.layer_id)])
    image = render(canvas)
    assert image.pixelColor(140, 40).red() > 200
    assert image.pixelColor(40, 40).alpha() == 0
    assert image.pixelColor(130, 30).green() > 70
    canvas.close()




def test_modifier_selection_requires_mode_and_survives_tabs(qapp):
    canvas, doc, page = scene(qapp)
    shape = doc.add_layer(page.layer_id)
    canvas.set_selection("layer", shape.layer_id)
    controls = ModifierControls(canvas)
    controls.add_modifier("hsl")
    identifier = canvas.active_modifier_id
    canvas.active_modifier_id = ""
    controls.toggle_modifier(identifier)
    assert canvas.active_modifier_id == ""
    canvas.modifier_mode = True
    controls.toggle_modifier(identifier)
    assert canvas.active_modifier_id == identifier
    captured = canvas.capture_session_state()
    canvas.set_document(ChapterDocument(), TileStore())
    canvas.restore_session_state(captured)
    assert canvas.active_modifier_id == identifier
    controls.deleteLater()
    canvas.close()
