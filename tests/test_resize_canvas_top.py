"""Canvas height placement preserves exact artwork and focused history."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF
from PySide6.QtGui import QColor, QColorSpace, QImage, QPainter
from PySide6.QtWidgets import QDialog

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (ArrayModifier, BlurModifier, BoundGeometry,
    CageTransformModifier, ChapterDocument, ColorFillGradientObject, DistortModifier,
    ImageObject, MirrorModifier, ParameterMaskBinding, RadialBlurModifier, RasterObject,
    TextObject, TextureModifier, TilingModifier, ToneMask)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.scene import DetachedSceneBackend, SceneSnapshotCompiler
from comic_editor.render.service import DocumentRenderService, RenderQuality, RenderRequest
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.canvas_resize import ResizeCanvasDialog, resize_canvas_height


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', grid_overlay_visible=False))
    canvas.resize(160, 160)
    chapter = ChapterDocument(width=1080, height=240)
    page = chapter.add_page('Page', BoundGeometry.rectangle(8, 8, 128, 128))
    page.fill_color, page.border_width = None, 0
    group = chapter.add_layer(page.layer_id, 'Nested', BoundGeometry.rectangle(8, 8, 112, 112))
    group.fill_color, group.border_width = None, 0
    group.translate_x, group.translate_y = 2, 3
    raster = chapter.add_object(group.layer_id, RasterObject(x=16, y=16,
        interaction_rect=(0, 0, 32, 32)))
    image_obj = chapter.add_object(group.layer_id, ImageObject(x=64, y=20,
        pixel_width=32, pixel_height=32))
    tiles, images = TileStore(), ImageStore()
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor('transparent'))
    painter = QPainter(tile)
    painter.fillRect(0, 0, 16, 32, QColor('#f07020'))
    painter.fillRect(16, 0, 16, 32, QColor('#30a0e0'))
    painter.end()
    tiles.set_tile(raster.object_id, (0, 0), tile)
    image = tile.copy(0, 0, 32, 32)
    image.setColorSpace(QColorSpace(QColorSpace.SRgb))
    buffer = QBuffer()
    assert buffer.open(QIODevice.WriteOnly) and image.save(buffer, 'PNG')
    images.put(image_obj.object_id, 'original.png', bytes(buffer.data()), 'image/png')
    buffer.close()
    canvas.set_document(chapter, tiles, images)
    yield canvas, page, group, raster, image_obj
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.close()
    canvas.deleteLater()


def exact(canvas, y=0):
    compiler = SceneSnapshotCompiler()
    capture = compiler.capture(canvas, canvas._render_document_state())
    while not capture.advance(.002):
        pass
    assert not capture.stale and capture.result is not None
    snapshot = capture.result
    backend = DetachedSceneBackend(snapshot)
    try:
        service = DocumentRenderService(backend)
        service.projection.revision = snapshot.document.revision
        request = RenderRequest((0., float(y), 160., 160.), 1., (160, 160),
            ('resize-native', y), snapshot.document.revision, quality=RenderQuality.EXACT)
        result = service.render_region(snapshot.document, request)
        assert result.exact and not result.image.isNull()
        image = result.image
        return (image.format().value, image.width(), image.height(), image.bytesPerLine(),
            image.devicePixelRatio(), bytes(image.colorSpace().iccProfile()), bytes(image.constBits()))
    finally:
        backend.close()


def source_state(canvas, raster, image_obj, mask=None):
    identifiers = [raster.object_id] + ([mask.mask_id] if mask else [])
    return (canvas.tiles.tile_size, tuple((identifier, key,
        canvas.tiles._object_tiles(identifier).version(key),
        bytes(canvas.tiles.tile(identifier, key).constBits()))
        for identifier in identifiers for key in canvas.tiles.object_tiles(identifier)),
        canvas.images.source(image_obj.object_id).data,
        canvas.images.pixel_signature(image_obj.object_id))


@pytest.mark.parametrize('root_mapping', ['translation', 'affine', 'projective', 'singular'])
@pytest.mark.parametrize('masked', [False, True])
def test_top_growth_native_pixels_sources_and_one_undo_redo(scene, root_mapping, masked):
    canvas, page, group, raster, image_obj = scene
    if root_mapping != 'translation':
        page.transform_frame = (8, 8, 128, 128)
        page.transform_quad = {
            'affine': [(10, 12), (138, 12), (138, 140), (10, 140)],
            'projective': [(8, 8), (130, 11), (135, 133), (10, 130)],
            'singular': [(8, 8), (136, 8), (136, 8), (8, 136)],
        }[root_mapping]
    warp = DistortModifier(modifier_type='distort_mesh_warp', frame=(66, 23, 32, 32),
        center=(82, 39), parameters={'rows': 2, 'columns': 2,
                                   'smoothness': 0, 'interpolation': 'nearest'})
    warp.validate()
    warp.points[3] = (.86, .82)
    chapter = canvas.chapter
    chapter.add_modifier(warp, [('object', image_obj.object_id)])
    mask = ToneMask(name='Shared', saved=True)
    mask.gradient = ColorFillGradientObject()
    mask.gradient.ramp.stops[0].color = "#00FFFFFF"
    mask.gradient.ramp.stops[-1].color = "#FFFFFFFF"
    mask.gradient.line_field.geometry = BoundGeometry('line', [(0, 0), (130, 130)])
    chapter.masks[mask.mask_id] = mask
    paint = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    paint.fill(QColor('transparent'))
    painter = QPainter(paint)
    painter.fillRect(10, 10, 80, 65, QColor('#80909090'))
    painter.end()
    canvas.tiles.set_tile(mask.mask_id, (0, 0), paint)
    if masked:
        raster.opacity_mask = ParameterMaskBinding(mask.mask_id, .15, 1.)
        image_obj.opacity_mask = ParameterMaskBinding(mask.mask_id, .15, 1.)
    chapter.export_rect = (4., 6., 132., 138.)
    chapter.export_rect_enabled = False  # Retained explicit crop follows too.
    chapter.validate()
    before = exact(canvas)
    assert len(set(before[-1])) > 4
    warp.muted = True
    try:
        unmodified = exact(canvas)
    finally:
        warp.muted = False
    assert before[-1] != unmodified[-1]
    if masked:
        bindings = raster.opacity_mask, image_obj.opacity_mask
        raster.opacity_mask = image_obj.opacity_mask = None
        try:
            unmasked = exact(canvas)
        finally:
            raster.opacity_mask, image_obj.opacity_mask = bindings
        assert before[-1] != unmasked[-1]
    initial, children = deepcopy(chapter.to_dict()), deepcopy(group.to_dict())
    source = source_state(canvas, raster, image_obj, mask)
    prior_commands = tuple(id(command) for command in canvas.command_stack._undo)
    assert resize_canvas_height(canvas, 300, 'top')
    assert chapter.height == 300 and chapter.export_rect == (4., 66., 132., 138.)
    assert group.to_dict() == children
    assert mask.paint_offset == (0., 60.) and warp.frame == (66., 83., 32., 32.)
    assert len(canvas.command_stack._undo) == len(prior_commands) + 1
    assert tuple(id(command) for command in canvas.command_stack._undo[:-1]) == prior_commands
    assert exact(canvas, 60) == before
    assert source_state(canvas, raster, image_obj, mask) == source
    committed = deepcopy(chapter.to_dict())
    assert ChapterDocument.from_dict(committed).to_dict() == committed
    canvas.command_stack.undo()
    assert chapter.to_dict() == initial and exact(canvas) == before
    canvas.command_stack.redo()
    assert chapter.to_dict() == committed and exact(canvas, 60) == before
    assert source_state(canvas, raster, image_obj, mask) == source


def test_bottom_growth_and_top_selected_shrink_preserve_all_artwork(scene):
    canvas, *_ = scene
    chapter = canvas.chapter
    original = deepcopy(chapter.to_dict())
    original_pixels = exact(canvas)
    assert resize_canvas_height(canvas, 300)
    expected = deepcopy(original)
    expected['size'][1] = 300
    assert chapter.to_dict() == expected and exact(canvas) == original_pixels
    assert resize_canvas_height(canvas, 200, 'top')
    expected['size'][1] = 200
    assert chapter.to_dict() == expected and exact(canvas) == original_pixels
    canvas.command_stack.undo()
    assert chapter.height == 300
    canvas.command_stack.undo()
    assert chapter.to_dict() == original


def test_top_moves_all_world_rigs_once_and_preserves_local_text_and_grids(scene):
    canvas, page, group, raster, image_obj = scene
    chapter = canvas.chapter
    hidden = chapter.add_page('Hidden', BoundGeometry.rectangle(5, 5, 40, 40))
    hidden.visible = False
    text = chapter.add_object(group.layer_id, TextObject(x=24, y=60, width=50, height=25))
    text.transform_quad = [(24, 60), (74, 60), (74, 85), (24, 85)]
    modifiers = [BlurModifier(focal_center=(22, 33)), RadialBlurModifier(center=(22, 33)),
        MirrorModifier(axis_start=(1, 2), axis_end=(5, 9)),
        ArrayModifier(axis_start=(1, 2), axis_end=(5, 9), center=(3, 4)),
        TilingModifier(center=(22, 33)),
        TextureModifier(texture_quad=[(1, 2), (4, 2), (4, 6), (1, 6)]),
        CageTransformModifier(frame=(10, 10, 50, 50))]
    for modifier in modifiers:
        modifier.validate()
        chapter.add_modifier(modifier, [('object', image_obj.object_id)])
    chapter.set_modifier_targets(modifiers[0].modifier_id, [('object', raster.object_id), ('object', image_obj.object_id)])
    mask = ToneMask(saved=True, name='Unbound', paint_offset=(-8, 11))
    mask.gradient = ColorFillGradientObject()
    mask.gradient.line_field.geometry = BoundGeometry('line', [(1, 2), (10, 20)])
    chapter.masks[mask.mask_id] = mask
    chapter.validate()
    before = deepcopy(chapter.to_dict())
    local = deepcopy(text.to_dict())
    assert resize_canvas_height(canvas, 270, 'top')
    assert page.translate_y == 30 and hidden.translate_y == 30
    assert text.to_dict() == local and chapter.grid.to_dict() == before['grid']
    assert modifiers[0].focal_center == (22, 63)
    assert modifiers[1].center == (22, 63) and modifiers[4].center == (22, 63)
    assert modifiers[2].axis_start == (1, 32) and modifiers[2].axis_end == (5, 39)
    assert modifiers[3].axis_start == (1, 32) and modifiers[3].center == (3, 34)
    assert modifiers[5].texture_quad == [(1, 32), (4, 32), (4, 36), (1, 36)]
    assert modifiers[6].frame == (10, 40, 50, 50)
    assert mask.paint_offset == (-8, 41) and chapter.export_rect is None
    assert mask.gradient.line_field.geometry.nodes[0].position == (1, 32)
    canvas.command_stack.undo()
    assert chapter.to_dict() == before
    canvas.command_stack.redo()
    assert chapter.height == 270 and mask.paint_offset == (-8, 41)


def test_rejected_noop_and_image_resize_are_atomic(scene):
    canvas, *_ = scene
    initial = deepcopy(canvas.chapter.to_dict())
    count = len(canvas.command_stack._undo)
    assert not resize_canvas_height(canvas, canvas.chapter.height, 'top')
    for height, side in [(30, 'top'), (260, 'left'), (10_000_001, 'bottom')]:
        with pytest.raises(ValueError):
            resize_canvas_height(canvas, height, side)
        assert canvas.chapter.to_dict() == initial
    canvas.chapter.document_kind = 'image'
    image_model = deepcopy(canvas.chapter.to_dict())
    with pytest.raises(ValueError, match='fixed'):
        resize_canvas_height(canvas, 300, 'top')
    assert canvas.chapter.to_dict() == image_model and len(canvas.command_stack._undo) == count


def test_dialog_defaults_and_growth_only_position(qapp):
    dialog = ResizeCanvasDialog(240, 140)
    assert dialog.windowTitle() == 'Resize Canvas' and dialog.height.value() == 240
    assert dialog.add_position.currentData() == 'bottom' and not dialog.add_position.isEnabled()
    dialog.height.setValue(300)
    assert dialog.add_position.isEnabled()
    dialog.add_position.setCurrentIndex(1)
    assert dialog.add_position.currentData() == 'top'
    dialog.height.setValue(180)
    assert not dialog.add_position.isEnabled() and dialog.height.minimum() == 140
    dialog.reject()
    assert dialog.result() == QDialog.Rejected
    dialog.deleteLater()


@pytest.mark.parametrize('accepted', [False, True])
def test_main_window_resize_dialog_cancel_or_top_one_command(qapp, monkeypatch, accepted):
    from comic_editor.ui.main_window import MainWindow
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    window = MainWindow()
    chapter = ChapterDocument(width=1080, height=200)
    page = chapter.add_page('Page', BoundGeometry.rectangle(10, 10, 70, 70))
    window.chapter = chapter
    window.canvas.set_document(chapter, TileStore(), ImageStore())
    window.hierarchy_model.set_chapter(chapter)
    initial = deepcopy(chapter.to_dict())
    dialog = SimpleNamespace(exec=lambda: QDialog.Accepted if accepted else QDialog.Rejected,
        height=SimpleNamespace(value=lambda: 250),
        add_position=SimpleNamespace(currentData=lambda: 'top'))
    monkeypatch.setattr('comic_editor.ui.main_window.ResizeCanvasDialog', lambda *_: dialog)
    try:
        assert window.trim_action.text() == 'Resize Canvas'
        window._trim_height()
        if accepted:
            assert chapter.height == 250 and page.translate_y == 50
            assert len(window.canvas.command_stack._undo) == 1
            window.canvas.command_stack.undo()
            assert chapter.to_dict() == initial
            window.canvas.command_stack.redo()
            assert chapter.height == 250 and page.translate_y == 50
        else:
            assert chapter.to_dict() == initial and not window.canvas.command_stack.can_undo
    finally:
        window._dirty = False
        window.canvas._scene_controller.reset()
        window.canvas._scene_controller.scheduler.close()
        window.canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
        window.canvas._effect_jobs.cancel()
        window.canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        window.deleteLater()
