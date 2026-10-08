"""Actual first-contact pixels remain visible while detached work is gated."""
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QColorSpace, QImage, QPainter

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, BlurModifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.service import RenderRequest, RenderQuality
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine()).copy()


def ready_paint(canvas):
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#242428'))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    try:
        canvas._paint_ready_document_projection(painter, live_ink=True)
    finally:
        painter.end()
    return image


def native_reference(canvas):
    document = canvas._render_document_state()
    request = RenderRequest((0., 0., 128., 128.), 1., (128, 128), ('feedback-oracle',),
                            document.revision, quality=RenderQuality.INTERACTIVE)
    result = canvas._render_service.render_region(document, request)
    assert not result.image.isNull(), result
    image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#242428'))
    painter = QPainter(image)
    painter.drawImage(0, 0, result.image)
    painter.end()
    return image


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', predictive_ink=False,
        grid_overlay_visible=False, snap_to_grid=False))
    canvas.setFixedSize(128, 128)
    chapter = ChapterDocument(width=128, height=128, document_kind='asset', background='#00000000')
    page = chapter.add_page('Page', BoundGeometry.rectangle(8, 8, 112, 112))
    page.fill_color, page.border_width = None, 0
    back = chapter.add_object(page.layer_id, RasterObject())
    selected = chapter.add_object(page.layer_id, RasterObject())
    front = chapter.add_object(page.layer_id, RasterObject())
    page.children.reverse()  # The model stores frontmost children first.
    tiles = TileStore()
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor('blue'))
    tiles.set_tile(back.object_id, (0, 0), tile)
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(Qt.transparent)
    painter = QPainter(tile)
    painter.fillRect(48, 20, 16, 90, QColor('green'))
    painter.end()
    tiles.set_tile(front.object_id, (0, 0), tile)
    canvas.set_document(chapter, tiles)
    canvas.center_x = canvas.center_y = 64.
    canvas.scale = 1.
    canvas.primary_color = '#ffff0000'
    canvas.settings.brush_size = 16
    canvas.set_selection('object', selected.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    yield canvas, selected, front
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


@pytest.mark.parametrize('erasing', [False, True])
def test_press_move_and_release_show_current_native_pixels_under_foreground_while_worker_busy(
        scene, erasing, monkeypatch, wait_scene, qapp):
    canvas, selected, _front = scene
    if erasing:
        tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        tile.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id, (0, 0), tile)
        canvas._invalidate_scene_cache()
        canvas.set_tool(ToolKind.RASTER_ERASER)
    wait_scene(canvas)
    assert canvas._scene_controller.feedback is not None
    assert canvas._scene_controller.feedback_dirty == set()
    before = ready_paint(canvas)
    history_revision = canvas.command_stack.revision
    gate, entered = Event(), Event()
    scheduler = canvas._scene_controller.scheduler
    original = scheduler._evaluate_admitted
    def blocked(demand, token):
        entered.set()
        assert gate.wait(20)
        return original(demand, token)
    monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
    try:
        canvas._begin_stroke(QPointF(32, 64), 1.)
        first = ready_paint(canvas)
        assert first.pixelColor(32, 64) == QColor('blue' if erasing else 'red')
        assert first.pixelColor(32, 64) != before.pixelColor(32, 64)
        qapp.processEvents()
        assert scheduler.busy
        canvas._continue_stroke(QPointF(100, 64), 1.)
        actual = ready_paint(canvas)
        assert actual.pixelColor(80, 64) == QColor('blue' if erasing else 'red')
        assert actual.pixelColor(56, 64) == QColor('green')
        assert actual.pixelColor(4, 64) == QColor('#242428')
        expected = native_reference(canvas)
        np.testing.assert_array_equal(pixels(actual)[2:126, 8:504], pixels(expected)[2:126, 8:504])
        # Presentation reads only prepared resources and resident edited tiles.
        with monkeypatch.context() as patch:
            patch.setattr(canvas, '_render_scene_layers', lambda *_a, **_k: pytest.fail('GUI scene evaluation'))
            patch.setattr(canvas.tiles.residency, 'get', lambda *_a, **_k: pytest.fail('GUI source decode'))
            assert ready_paint(canvas).pixelColor(80, 64) == actual.pixelColor(80, 64)
        canvas._end_stroke()
        assert ready_paint(canvas).pixelColor(80, 64) == actual.pixelColor(80, 64)
        assert canvas.command_stack.revision == history_revision + 1
    finally:
        gate.set()


def test_unrelated_artwork_change_retires_prepared_contact_resources(scene, wait_scene):
    canvas, selected, front = scene
    wait_scene(canvas)
    assert canvas._scene_controller.feedback is not None
    canvas._begin_stroke(QPointF(32, 64), 1.)
    assert canvas._scene_controller.feedback_dirty
    front.opacity = .5
    canvas._publish_change_set(ChangeSet((EntityChange(('object', front.object_id), frozenset({'opacity'})),)))
    assert canvas._scene_controller.feedback is None
    assert not canvas._scene_controller.feedback_dirty
    canvas._end_stroke()


def test_selected_effect_declines_unprocessed_contact_resources(scene, wait_scene):
    canvas, selected, _front = scene
    canvas.chapter.add_modifier(BlurModifier(strength=3), [('object', selected.object_id)])
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    assert canvas._scene_controller.feedback is None


def test_integer_source_placement_and_nested_clip_match_native_scene(scene, wait_scene):
    canvas, selected, _front = scene
    from comic_editor.core.models import ChildRef
    page = canvas.chapter.layers[selected.parent_layer_id]
    page.children = [ref for ref in page.children if ref.entity_id != selected.object_id]
    layer = canvas.chapter.add_layer(page.layer_id, 'Ink clip', BoundGeometry.rectangle(16, 16, 96, 96), index=1)
    layer.translate_x, layer.translate_y = 3., 5.
    layer.fill_color, layer.border_width = None, 0
    layer.children = [ChildRef('object', selected.object_id)]
    selected.parent_layer_id = layer.layer_id
    selected.x, selected.y = 7., 11.
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    assert canvas._scene_controller.feedback.origin == (10, 16)
    canvas._begin_stroke(QPointF(32, 64), 1.)
    canvas._continue_stroke(QPointF(116, 64), 1.)
    actual, expected = ready_paint(canvas), native_reference(canvas)
    np.testing.assert_array_equal(pixels(actual)[2:126, 8:504], pixels(expected)[2:126, 8:504])
    assert actual.pixelColor(80, 64) == QColor('red')
    assert actual.pixelColor(118, 64) == QColor('blue')
    canvas._end_stroke()


def test_mask_reference_to_selected_source_declines_prepared_passes(scene, wait_scene):
    canvas, selected, _front = scene
    from comic_editor.core.models import ToneMask
    mask = ToneMask(contributors=[('object', selected.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    assert canvas._scene_controller.feedback is None


@pytest.mark.parametrize('case', ['curve_border', 'opacity', 'negative_seams', 'promoted_front', 'promoted_back'])
def test_prepared_contact_matches_fixed_origin_reference_at_native_edges(
        scene, case, wait_scene, monkeypatch, qapp):
    canvas, selected, front = scene
    page = canvas.chapter.layers[selected.parent_layer_id]
    back = canvas.chapter.objects[page.children[-1].entity_id]
    if case == 'curve_border':
        page.bound = BoundGeometry.circle(64.25, 64.75, 54.5)
        page.border_width, page.border_color = 4., '#ffcc8800'
    elif case == 'opacity':
        selected.opacity = .5
        back.opacity, front.opacity = .4, .35
    elif case == 'negative_seams':
        selected.x, selected.y = -239., -227.
    elif case == 'promoted_front':
        front.show_on_top = True
    else:
        back.show_on_top = True
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    assert canvas._scene_controller.feedback is not None
    gate = Event()
    scheduler = canvas._scene_controller.scheduler
    original = scheduler._evaluate_admitted
    def blocked(demand, token):
        assert gate.wait(20)
        return original(demand, token)
    monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
    try:
        canvas._begin_stroke(QPointF(16, 64), 1.)
        first = ready_paint(canvas)
        qapp.processEvents()
        assert scheduler.busy
        canvas._continue_stroke(QPointF(118, 64), 1.)
        actual, expected = ready_paint(canvas), native_reference(canvas)
        np.testing.assert_array_equal(pixels(actual)[2:126, 8:504], pixels(expected)[2:126, 8:504])
        if case != 'promoted_back':
            assert first.pixelColor(16, 64).red() > 0
        if case == 'negative_seams':
            assert canvas._scene_controller.feedback.origin == (-239, -227)
            assert len(canvas._scene_controller.feedback_patches.entries) >= 4
        canvas._end_stroke()
    finally:
        gate.set()


def test_fully_pruned_eraser_tile_reveals_prepared_prefix_while_worker_busy(
        scene, wait_scene, monkeypatch, qapp):
    canvas, selected, _front = scene
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(Qt.transparent)
    painter = QPainter(tile)
    painter.fillRect(24, 60, 8, 8, QColor('red'))
    painter.end()
    canvas.tiles.set_tile(selected.object_id, (0, 0), tile)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    assert ready_paint(canvas).pixelColor(28, 64) == QColor('red')
    gate = Event()
    scheduler = canvas._scene_controller.scheduler
    original = scheduler._evaluate_admitted
    def blocked(demand, token):
        assert gate.wait(20)
        return original(demand, token)
    monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
    try:
        canvas._begin_stroke(QPointF(28, 64), 1.)
        assert ready_paint(canvas).pixelColor(28, 64) == QColor('blue')
        qapp.processEvents()
        assert scheduler.busy
        canvas._end_stroke()
        assert (0, 0) not in canvas.tiles._tiles[selected.object_id].entries
        actual, expected = ready_paint(canvas), native_reference(canvas)
        np.testing.assert_array_equal(pixels(actual)[2:126, 8:504], pixels(expected)[2:126, 8:504])
    finally:
        gate.set()


def test_unchanged_feedback_patches_reuse_composition_and_release_with_basis(
        scene, wait_scene, monkeypatch):
    canvas, _selected, _front = scene
    import comic_editor.ui.raster_feedback as feedback_ui
    wait_scene(canvas)
    composed = []
    original = feedback_ui._compose_tile
    def tracked(*args):
        composed.append(args[2].key)
        return original(*args)
    monkeypatch.setattr(feedback_ui, '_compose_tile', tracked)
    canvas._begin_stroke(QPointF(32, 64), 1.)
    ready_paint(canvas)
    assert composed
    first_count = len(composed)
    ready_paint(canvas)
    assert len(composed) == first_count
    canvas._continue_stroke(QPointF(80, 64), 1.)
    ready_paint(canvas)
    assert len(composed) > first_count
    cache = canvas._scene_controller.feedback_patches
    assert 0 < cache.byte_count <= cache.budget
    canvas._end_stroke()
    canvas._scene_controller.retire_feedback()
    assert not cache.entries and cache.byte_count == 0


def test_prediction_only_rebuilds_its_native_coverage_and_removes_leaving_pen(
        scene, wait_scene, monkeypatch):
    import comic_editor.ui.raster_feedback as feedback_ui
    canvas, selected, _front = scene
    canvas.chapter.width = 768
    page = canvas.chapter.layers[selected.parent_layer_id]
    page.bound = BoundGeometry.rectangle(8, 8, 752, 112)
    canvas.setFixedSize(768, 128)
    canvas.center_x = 384.
    canvas.settings.predictive_ink = True
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    canvas._begin_stroke(QPointF(300, 64), 1.)
    prepared = canvas._scene_controller.feedback
    cache = canvas._scene_controller.feedback_patches
    composed = []
    original = feedback_ui._compose_tile
    def tracked(*args):
        composed.append(args[2].key)
        return original(*args)
    monkeypatch.setattr(feedback_ui, '_compose_tile', tracked)
    def feedback_frame(current_cache):
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('#242428'))
        painter = QPainter(image)
        feedback_ui.present_raster_feedback(canvas, painter, prepared, {(1, 0)}, current_cache)
        painter.end()
        return image
    canvas._predictive = QPointF(300, 64), QPointF(304, 64), 16., QColor('red')
    feedback_frame(cache)
    assert set(composed) == {(0, 0), (1, 0), (2, 0)}
    composed.clear()
    canvas._predictive = QPointF(302, 64), QPointF(308, 64), 16., QColor('red')
    actual = feedback_frame(cache)
    assert composed == [(1, 0)]
    fresh = feedback_frame(feedback_ui.RasterFeedbackPatchCache())
    np.testing.assert_array_equal(pixels(actual), pixels(fresh))
    composed.clear()
    # The AA pen reaches both sides of a native tile seam and its gutters.
    canvas._predictive = QPointF(254, 64), QPointF(258, 64), 16., QColor('red')
    actual = feedback_frame(cache)
    assert set(composed) == {(0, 0), (1, 0)}
    fresh = feedback_frame(feedback_ui.RasterFeedbackPatchCache())
    np.testing.assert_array_equal(pixels(actual), pixels(fresh))
    composed.clear()
    canvas._predictive = QPointF(530, 64), QPointF(536, 64), 16., QColor('red')
    actual = feedback_frame(cache)
    assert set(composed) == {(0, 0), (1, 0), (2, 0)}
    fresh = feedback_frame(feedback_ui.RasterFeedbackPatchCache())
    np.testing.assert_array_equal(pixels(actual), pixels(fresh))
    canvas._predictive = None
    actual = feedback_frame(cache)
    fresh = feedback_frame(feedback_ui.RasterFeedbackPatchCache())
    np.testing.assert_array_equal(pixels(actual), pixels(fresh))
    canvas._end_stroke()


def test_feedback_culls_offscreen_patches_before_composition_but_keeps_gutter(
        scene, wait_scene, monkeypatch):
    from dataclasses import replace
    import comic_editor.ui.raster_feedback as feedback_ui
    canvas, _selected, _front = scene
    wait_scene(canvas)
    canvas._begin_stroke(QPointF(32, 64), 1.)
    prepared = canvas._scene_controller.feedback
    original = prepared.tiles[0]
    far = replace(original, key=(1, 0), bounds=(254., -2., 260., 260.))
    prepared = replace(prepared, tiles=(original, far))
    monkeypatch.setattr(canvas, 'visible_document_rect', lambda: QRectF(0, 0, 128, 128))
    composed = []
    compose = feedback_ui._compose_tile
    def tracked(*args):
        composed.append(args[2].key)
        return compose(*args)
    monkeypatch.setattr(feedback_ui, '_compose_tile', tracked)
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    feedback_ui.present_raster_feedback(canvas, painter, prepared, {(0, 0)},
                                       feedback_ui.RasterFeedbackPatchCache())
    assert composed == [(0, 0)]
    composed.clear()
    monkeypatch.setattr(canvas, 'visible_document_rect', lambda: QRectF(128, 0, 127, 128))
    feedback_ui.present_raster_feedback(canvas, painter, prepared, {(0, 0)},
                                       feedback_ui.RasterFeedbackPatchCache())
    assert set(composed) == {(0, 0), (1, 0)}
    painter.end()
    canvas._end_stroke()


def test_native_pixel_changes_reuse_prepared_planes_and_exact_ready_stops_overlay(
        scene, wait_scene, monkeypatch):
    canvas, _selected, _front = scene
    wait_scene(canvas)
    controller = canvas._scene_controller
    prepared = controller.feedback
    demands = []
    submit = controller.scheduler.submit
    def tracked(demand):
        demands.append(demand)
        return submit(demand)
    monkeypatch.setattr(controller.scheduler, 'submit', tracked)
    for x in (32, 80):
        canvas._begin_stroke(QPointF(x, 64), 1.)
        ready_paint(canvas)
        canvas._end_stroke()
        wait_scene(canvas)
        assert controller.feedback is prepared
    assert demands and all(not demand.feedback_target for demand in demands)
    assert controller.feedback_dirty
    document = canvas._render_document_state()
    assert controller._feedback_covers(document, (0., 0., 128., 128.))
    from dataclasses import replace
    controller.feedback = replace(prepared, tiles=())
    assert not controller._feedback_covers(document, (0., 0., 128., 128.))
    controller.feedback = prepared
    image = ready_paint(canvas)
    assert not canvas._projection_frame_pending
    assert not canvas._projection_provisional_visible
    np.testing.assert_array_equal(pixels(image)[2:126, 8:504], pixels(native_reference(canvas))[2:126, 8:504])


@pytest.mark.parametrize('case', ['effect', 'floating', 'noninteger', 'transformed', 'missing'])
def test_unavailable_contact_pixels_show_received_gesture_as_pending(
        scene, case, wait_scene, monkeypatch, qapp):
    canvas, selected, _front = scene
    if case == 'effect':
        canvas.chapter.add_modifier(BlurModifier(strength=3), [('object', selected.object_id)])
    elif case == 'floating':
        from comic_editor.core.pixel_contract import FLOAT_PIXELS
        canvas.chapter.pixel_contract = FLOAT_PIXELS
    elif case == 'noninteger':
        selected.x = .25
    elif case == 'transformed':
        selected.transform_frame = (0., 0., 128., 128.)
        # Rotated/skewed maps retain ordinary evaluation. Positive axis-aligned
        # transformed rasters now have separately verified native feedback.
        selected.transform_quad = [(0., 0.), (128., 8.), (120., 128.), (-8., 120.)]
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    if case == 'missing':
        assert canvas._scene_controller.feedback is not None
        canvas._scene_controller.retire_feedback()
    else:
        assert canvas._scene_controller.feedback is None
    gate = Event()
    scheduler = canvas._scene_controller.scheduler
    original = scheduler._evaluate_admitted
    def blocked(demand, token):
        assert gate.wait(20)
        return original(demand, token)
    monkeypatch.setattr(scheduler, '_evaluate_admitted', blocked)
    try:
        canvas._begin_stroke(QPointF(32, 64), 1.)
        actual = ready_paint(canvas)
        assert canvas._raster_feedback_pending_visible
        assert not canvas._raster_feedback_contact_covered
        # A received position moves immediately even before artwork evaluation.
        canvas._continue_stroke(QPointF(80, 64), 1.)
        moved = ready_paint(canvas)
        assert canvas._raster_contact_point == QPointF(80, 64)
        assert canvas._raster_feedback_pending_visible
        assert not np.array_equal(pixels(actual), pixels(moved))
        qapp.processEvents()
        assert scheduler.busy and canvas._projection_frame_pending
        canvas._end_stroke()
    finally:
        gate.set()
    wait_scene(canvas)
    ready_paint(canvas)
    assert not canvas._raster_feedback_pending_visible


def gutter_cache_case(format=QImage.Format_ARGB32_Premultiplied, offset=(1, 0)):
    from comic_editor.ui.raster_feedback import RasterFeedbackPatchCache
    source = QImage(12, 12, QImage.Format_ARGB32_Premultiplied)
    source.fill(Qt.transparent)
    tile = SimpleNamespace(key=(-3, -5), source=source)
    prepared = SimpleNamespace(tile_size=8, gutter=2)
    key = tile.key[0] + offset[0], tile.key[1] + offset[1]
    image = QImage(8, 8, format)
    image.fill(QColor(199, 59, 93, 127))
    image.setColorSpace(QColorSpace.fromIccProfile(QColorSpace(QColorSpace.DisplayP3).iccProfile()))
    cache = RasterFeedbackPatchCache()
    signature = (id(prepared), None, cache.source_signature(prepared, tile, {key: image}))
    presented = source.copy()
    cache.put(tile.key, signature, presented)
    return cache, prepared, tile, key, image, presented


@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64, QImage.Format_RGBA64_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4,
    QImage.Format_RGBA32FPx4_Premultiplied])
@pytest.mark.parametrize('offset', [(1, 0), (-1, 0), (1, -1)])
def test_native_gutter_dependency_reuses_interior_edit_and_detects_edge_bits_and_profile(format, offset):
    from comic_editor.ui.raster_feedback import NativeGutterDependency
    cache, prepared, tile, key, image, presented = gutter_cache_case(format, offset)
    dependency = cache.entries[tile.key][0][2][0][1]
    assert isinstance(dependency, NativeGutterDependency)
    assert dependency.pixels.format() == format and dependency.pixels.colorSpace() == image.colorSpace()
    x, y, width, height = dependency.rectangle
    np.testing.assert_array_equal(pixels(dependency.pixels), pixels(image.copy(x, y, width, height)))
    before = pixels(dependency.pixels)
    edited = image.copy()
    bits = np.frombuffer(edited.bits(), np.uint8).reshape(8, edited.bytesPerLine())
    bits[4, 4 * edited.depth() // 8] ^= 1  # Strictly outside each contributed strip/corner.
    signature = (id(prepared), None, cache.source_signature(prepared, tile, {key: edited}))
    assert cache.get(tile.key, signature) is presented
    assert signature[2][0][1] is dependency
    assert dependency.source_key == edited.cacheKey()
    assert cache.source_signature(prepared, tile, {key: edited})[0][1] is dependency
    np.testing.assert_array_equal(pixels(dependency.pixels), before)
    edge = edited.copy()
    bits = np.frombuffer(edge.bits(), np.uint8).reshape(8, edge.bytesPerLine())
    bits[y, x * edge.depth() // 8] ^= 1
    signature = (id(prepared), None, cache.source_signature(prepared, tile, {key: edge}))
    assert cache.get(tile.key, signature) is None
    assert signature[2][0][1] != dependency
    np.testing.assert_array_equal(pixels(signature[2][0][1].pixels), pixels(edge.copy(x, y, width, height)))
    changed_profile = edited.copy()
    changed_profile.setColorSpace(QColorSpace(QColorSpace.SRgbLinear))
    signature = (id(prepared), None, cache.source_signature(prepared, tile, {key: changed_profile}))
    assert cache.get(tile.key, signature) is None
    # The central tile always depends on its complete native image revision.
    central = cache.source_signature(prepared, tile, {tile.key: edited})
    assert central == ((tile.key, edited.cacheKey()),)


@pytest.mark.parametrize('format,dtype,channel,first,second', [
    (QImage.Format_RGBA32FPx4, np.uint32, 0, 0, 0x80000000),
    (QImage.Format_RGBA32FPx4_Premultiplied, np.uint32, 0, 0x7fc00001, 0x7fc00002),
    (QImage.Format_RGBA16FPx4_Premultiplied, np.uint16, 0, 0x7e01, 0x7e02),
    (QImage.Format_RGBX8888, np.uint8, 3, 0, 255),
    (QImage.Format_RGBX64, np.uint16, 3, 65535, 65534),
])
def test_native_gutter_equality_preserves_float_payloads_signed_zero_and_rgbx_bits(
        format, dtype, channel, first, second):
    cache, prepared, tile, key, image, presented = gutter_cache_case(format)
    source = image.copy()
    np.frombuffer(source.bits(), dtype).reshape(8, 8, 4)[0, 0, channel] = first
    signature = (id(prepared), None, cache.source_signature(prepared, tile, {key: source}))
    cache.put(tile.key, signature, presented)
    changed = source.copy()
    np.frombuffer(changed.bits(), dtype).reshape(8, 8, 4)[0, 0, channel] = second
    actual = (id(prepared), None, cache.source_signature(prepared, tile, {key: changed}))
    assert cache.get(tile.key, actual) is None
    assert actual[2][0][1] != signature[2][0][1]


@pytest.mark.parametrize('case', ['dpr', 'width', 'height', 'rgb32', 'indexed', 'null', 'source_grid'])
def test_unexpected_native_gutter_sources_keep_conservative_whole_image_dependency(case):
    from comic_editor.ui.raster_feedback import NativeGutterDependency
    cache, prepared, tile, key, image, _presented = gutter_cache_case()
    if case == 'dpr':
        image.setDevicePixelRatio(2.)
    elif case in ('width', 'height'):
        image = image.copy(0, 0, 7 if case == 'width' else 8, 7 if case == 'height' else 8)
    elif case in ('rgb32', 'indexed'):
        image = image.convertToFormat(QImage.Format_RGB32 if case == 'rgb32' else QImage.Format_Indexed8)
    elif case == 'null':
        image = QImage()
    else:
        tile.source = tile.source.copy(0, 0, 11, 12)
    dependency = cache.source_signature(prepared, tile, {key: image})[0][1]
    assert not isinstance(dependency, NativeGutterDependency)
    assert dependency == image.cacheKey()


def test_native_gutter_crops_count_toward_budget_and_pruning_cannot_reuse_old_pixels():
    cache, prepared, tile, key, image, presented = gutter_cache_case()
    entry = cache.entries[tile.key]
    crop = entry[0][2][0][1].pixels
    assert cache.byte_count == presented.sizeInBytes() + crop.sizeInBytes()
    pruned = (id(prepared), None, cache.source_signature(prepared, tile, {key: None}))
    assert cache.get(tile.key, pruned) is None
    cache.put(tile.key, pruned, presented)
    assert cache.byte_count == presented.sizeInBytes()
    assert cache.get(tile.key, pruned) is presented
    restored = (id(prepared), None, cache.source_signature(prepared, tile, {key: image}))
    assert cache.get(tile.key, restored) is None
    cache.budget = presented.sizeInBytes()
    cache.put(tile.key, restored, presented)
    assert not cache.entries and cache.byte_count == 0


@pytest.mark.parametrize('erasing', [False, True])
@pytest.mark.parametrize('fractional', [False, True])
def test_current_native_gutter_reuse_matches_fresh_composition_after_interior_and_edge_contact(
        scene, wait_scene, monkeypatch, erasing, fractional):
    import comic_editor.ui.raster_feedback as feedback_ui
    canvas, selected, _front = scene
    canvas.chapter.width = 768
    page = canvas.chapter.layers[selected.parent_layer_id]
    page.bound = BoundGeometry.rectangle(8.25, 8.75, 752.5, 110.5)
    page.border_width = 2.
    selected.x, selected.y, selected.opacity = -17., -11., .5
    canvas.setFixedSize(768, 128)
    canvas.center_x, canvas.center_y = 384., 64.
    if fractional:
        canvas.scale, canvas.rotation = .875, 7.25
    if erasing:
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id, (1, 0), image)
        canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    prepared = canvas._scene_controller.feedback
    assert prepared is not None and prepared.origin == (-17, -11)
    cache = canvas._scene_controller.feedback_patches
    composed = []
    original = feedback_ui._compose_tile
    def tracked(*args):
        composed.append(args[2].key)
        return original(*args)
    monkeypatch.setattr(feedback_ui, '_compose_tile', tracked)
    def frame(current_cache):
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('#242428'))
        painter = QPainter(image)
        feedback_ui.present_raster_feedback(canvas, painter, prepared,
            canvas._scene_controller.feedback_dirty, current_cache)
        painter.end()
        return image
    canvas._begin_stroke(QPointF(283, 53), 1.)
    first = frame(cache)
    assert {(0, 0), (1, 0), (2, 0)}.issubset(composed)
    composed.clear()
    canvas._continue_stroke(QPointF(303, 53), 1.)
    actual = frame(cache)
    assert composed == [(1, 0)]
    np.testing.assert_array_equal(pixels(actual), pixels(frame(feedback_ui.RasterFeedbackPatchCache())))
    assert not np.array_equal(pixels(actual), pixels(first))
    composed.clear()
    canvas._continue_stroke(QPointF(239, 53), 1.)
    actual = frame(cache)
    assert {(0, 0), (1, 0)}.issubset(composed)
    np.testing.assert_array_equal(pixels(actual), pixels(frame(feedback_ui.RasterFeedbackPatchCache())))
    canvas._end_stroke()


def test_evicted_dirty_native_source_declines_cached_gutter_feedback_without_gui_read(
        scene, wait_scene, monkeypatch):
    import comic_editor.ui.raster_feedback as feedback_ui
    canvas, selected, _front = scene
    wait_scene(canvas)
    canvas._begin_stroke(QPointF(32, 64), 1.)
    ready_paint(canvas)
    owner = canvas.tiles._tiles[selected.object_id]
    owner.residency.evict(owner, (0, 0))
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner.residency, 'get', lambda *_a, **_k: pytest.fail('GUI cold source read'))
            assert feedback_ui.present_raster_feedback(canvas, painter, canvas._scene_controller.feedback,
                {(0, 0)}, canvas._scene_controller.feedback_patches) == 0
    finally:
        painter.end()
    canvas._end_stroke()


@pytest.mark.parametrize('case', ['pencil', 'eraser', 'release', 'cold', 'missing',
    'ineligible_tool', 'released_source', 'selection', 'coverage', 'configuration', 'inactive_drawing'])
def test_scene_capture_contact_budget_preserves_cold_release_and_exact_pixels(
        scene, wait_scene, monkeypatch, case):
    from dataclasses import replace
    canvas, selected, _front = scene
    if case == 'eraser':
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id, (0, 0), image)
        canvas.set_tool(ToolKind.RASTER_ERASER)
        canvas._invalidate_scene_cache()
    wait_scene(canvas)
    before = ready_paint(canvas)
    canvas._begin_stroke(QPointF(32, 64), 1.)
    current = ready_paint(canvas)
    assert canvas._raster_feedback_contact_covered
    assert current.pixelColor(32, 64) != before.pixelColor(32, 64)
    controller = canvas._scene_controller
    if case == 'release':
        canvas._end_stroke()
        ready_paint(canvas)
    assert controller.capture is not None
    capture = controller.capture
    budgets = []
    with monkeypatch.context() as patch:
        # Observe the budget actually passed by the scheduler without finishing
        # its metadata iterator. The original iterator then converges normally.
        patch.setattr(capture, 'advance', lambda seconds: (budgets.append(seconds), False)[1])
        if case == 'cold':
            patch.setattr(canvas, '_raster_tile_input', SimpleNamespace(busy=True, released=False))
        elif case == 'released_source':
            patch.setattr(canvas, '_raster_tile_input', SimpleNamespace(busy=False, released=True))
        elif case == 'missing':
            patch.setattr(controller, 'feedback', None)
        elif case == 'ineligible_tool':
            patch.setattr(canvas, 'tool', ToolKind.BRUSH)
        elif case == 'selection':
            patch.setattr(canvas, 'selected_id', 'another-source')
        elif case == 'coverage':
            patch.setattr(controller, 'feedback', replace(controller.feedback, tiles=()))
        elif case == 'configuration':
            patch.setattr(controller, 'feedback', SimpleNamespace(identifier=selected.object_id,
                document=SimpleNamespace(configuration=('different-presentation',))))
        elif case == 'inactive_drawing':
            patch.setattr(canvas, '_drawing', False)
        controller.advance()
        assert budgets == [.002 if case in ('pencil', 'eraser') else .004]
        assert controller.timer.interval() == 8
    if case != 'release':
        canvas._end_stroke()
    wait_scene(canvas)
    assert controller.capture is None and not controller.scheduler.busy
    assert not canvas._projection_frame_pending
    np.testing.assert_array_equal(pixels(ready_paint(canvas))[2:126, 8:504],
                                  pixels(native_reference(canvas))[2:126, 8:504])
