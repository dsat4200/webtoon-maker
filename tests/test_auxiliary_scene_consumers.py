"""Visible histogram controls request detached work, preserving native input."""
from concurrent.futures import ThreadPoolExecutor
import time

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, CurvesModifier, ImageObject, PosterizeModifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.outputs import capture_document
from comic_editor.render.source_sampling import curves_histogram, posterize_statistics
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.curves_features import CurvesSampler
from comic_editor.ui.posterize_controls import PosterizeSampler


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    owner = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    chapter = ChapterDocument(width=32, height=24, document_kind='asset', background='#00000000')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 32, 24))
    obj = chapter.add_object(page.layer_id, ImageObject(x=8, y=8, pixel_width=8, pixel_height=4))
    native = QImage(8, 4, QImage.Format_ARGB32_Premultiplied)
    native.fill(QColor(64, 128, 192, 128))
    sources = ImageStore()
    data, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(data)
    assert buffer.open(QIODevice.WriteOnly) and native.save(buffer, 'PNG')
    sources.put(obj.object_id, 'original.png', bytes(data))
    curves, posterize = CurvesModifier(), PosterizeModifier()
    chapter.add_modifier(curves, [('object', obj.object_id)])
    chapter.add_modifier(posterize, [('object', obj.object_id)])
    owner.set_document(chapter, TileStore(), sources)
    owner.set_selection('object', obj.object_id, activate_default_tool=False)
    yield owner, obj, curves, posterize
    if getattr(owner, '_scene_consumers', None) is not None:
        owner._scene_consumers.shutdown()
        owner._scene_consumers.executor.shutdown(wait=True, cancel_futures=True)
    owner._scene_controller.reset()
    owner._scene_controller.scheduler.close()
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.deleteLater()


def test_detached_histogram_and_statistics_match_isolated_live_inputs(canvas):
    owner, obj, curves, posterize = canvas
    expected = CurvesSampler().histogram(owner, curves.modifier_id, 'rgb', 'red')
    statistics = PosterizeSampler().sample(owner, [('object', obj.object_id)], posterize.modifier_id)
    snapshot = capture_document(owner.chapter, owner.tiles, owner.images)
    snapshot.state.update(selected_kind='object', selected_id=obj.object_id,
                          selected_object_id=obj.object_id, selected_entities=[('object', obj.object_id)])
    original = tuple(snapshot.chapter.objects[obj.object_id].modifier_ids)
    with ThreadPoolExecutor(max_workers=1) as worker:
        actual = worker.submit(curves_histogram, snapshot, curves.modifier_id, 'rgb', 'red').result(timeout=15)
        result = worker.submit(posterize_statistics, snapshot, (('object', obj.object_id),),
                               posterize.modifier_id).result(timeout=15)
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(result.counts, statistics.counts)
    assert tuple(snapshot.chapter.objects[obj.object_id].modifier_ids) == original


def test_real_posterize_control_never_evaluates_live_widget(canvas, monkeypatch):
    from comic_editor.ui.modifier_controls import ModifierControls
    from comic_editor.ui.posterize_controls import PosterizeControls
    owner, obj, _curves, posterize = canvas
    controls = ModifierControls(owner)
    monkeypatch.setattr(owner, '_render_object', lambda *_a, **_k: pytest.fail('GUI source render'))
    control = PosterizeControls(posterize, controls)
    deadline = time.monotonic()+15
    try:
        while control.wheel.statistics.counts.sum() == 0 and time.monotonic() < deadline:
            QTest.qWait(5)
        assert control.wheel.statistics.counts.sum() > 0
        assert owner._scene_consumers.active is None
        assert obj.modifier_ids == [_curves.modifier_id, posterize.modifier_id]
    finally:
        control.deleteLater()
        controls.deleteLater()


def test_real_modifier_apply_prepares_detached_and_publishes_one_undo(canvas, monkeypatch):
    from comic_editor.core.models import RasterObject
    from comic_editor.ui.modifier_controls import ModifierControls
    owner, image, curves, posterize = canvas
    raster = RasterObject(parent_layer_id=image.parent_layer_id, interaction_rect=(0, 0, 8, 4))
    owner.chapter.add_object(image.parent_layer_id, raster)
    tile = QImage(owner.tiles.tile_size, owner.tiles.tile_size, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor('#804080c0'))
    owner.tiles.set_tile(raster.object_id, (0, 0), tile)
    raster.modifier_ids = [curves.modifier_id]
    owner.set_selection('object', raster.object_id, activate_default_tool=False)
    controls = ModifierControls(owner)
    monkeypatch.setattr(owner, '_drawing_local_to_world_transform',
                        lambda *_a: pytest.fail('GUI bake preparation'))
    before = owner.command_stack.revision
    try:
        controls.apply_modifier(curves.modifier_id)
        assert raster.modifier_ids == [curves.modifier_id]
        deadline = time.monotonic()+15
        while raster.modifier_ids and time.monotonic() < deadline:
            QTest.qWait(5)
        assert raster.modifier_ids == []
        assert owner.command_stack.revision == before+1
        assert owner.tiles.tile(raster.object_id, (0, 0)).pixelColor(0, 0) == tile.pixelColor(0, 0)
        owner.command_stack.undo()
        assert owner.chapter.objects[raster.object_id].modifier_ids == [curves.modifier_id]
    finally:
        controls.deleteLater()


def test_cold_eyedropper_release_commits_final_detached_sample_once(canvas, monkeypatch):
    from comic_editor.ui.canvas import ToolKind
    owner, obj, *_ = canvas
    point = QPointF(10, 10)
    expected = owner.sample_composited_color(point)
    committed, gestures = [], []
    owner.set_tool(ToolKind.EYEDROPPER)
    owner.colorSampleCommitted.connect(committed.append)
    owner.eyedropperGestureChanged.connect(gestures.append)
    monkeypatch.setattr(owner, '_render_object', lambda *_a, **_k: pytest.fail('GUI cold eyedropper'))
    assert owner._sample_eyedropper(point)
    owner._eyedropper_sampling = True
    owner._tool_release()
    assert committed == []
    deadline = time.monotonic()+15
    while not committed and time.monotonic() < deadline:
        QTest.qWait(5)
    assert committed == [expected]
    assert gestures == [False]
    assert owner._eyedropper_last_color == ''


def test_float_overview_transforms_display_once_and_preserves_native_results(monkeypatch):
    from types import SimpleNamespace
    from comic_editor.core.pixel_contract import PixelContract
    from comic_editor.render.overview import exact_overview
    from comic_editor.render.pixels import (capture_color_environment, display_image, pixel_scope,
                                           premultiplied_pixels, working_image)
    from comic_editor.render.projection import ProjectionAddress, ProjectionRequest
    from comic_editor.render.service import RenderDocument
    contract = PixelContract(version=2, precision='float32', working_space='linear_srgb')
    environment = capture_color_environment(contract)
    request = ProjectionRequest(ProjectionAddress(0, 0, 0), tile_size=2, gutter=1)
    values = np.full((request.pixel_size, request.pixel_size, 4), (.08, .16, .24, .5), np.float32)
    native = working_image(values, contract)
    before, key = premultiplied_pixels(native), native.cacheKey()
    with pixel_scope(contract, environment=environment):
        expected = display_image(native, contract)
    metadata = RenderDocument(('overview',), (), 0, 2, 2, '#00000000', pixel_contract=contract)
    demand = SimpleNamespace(visible=(0., 0., 2., 2.), presentation_size=(8, 8), center=(1., 1.),
        requests=[request], snapshot=SimpleNamespace(document=metadata, pixel_environment=environment))
    calls = []
    def edge(image, policy):
        calls.append(policy)
        return display_image(image, policy)
    monkeypatch.setattr('comic_editor.render.overview.display_image', edge)
    backend = SimpleNamespace(lookup_tile=lambda *_a: None)
    service = SimpleNamespace(render_tiles=lambda *_a, **_k:
        SimpleNamespace(tiles={request.address: (native, True)}, error=None, pending=False))
    overview = exact_overview(demand, backend, service, lambda: False)
    assert calls == [contract]
    assert overview.image.pixelColor(4, 4) == expected.pixelColor(0, 0)
    assert native.cacheKey() == key
    np.testing.assert_array_equal(premultiplied_pixels(native), before)


def test_ready_eyedropper_queues_device_cpu_edge_and_displays_float_once(canvas, monkeypatch):
    from dataclasses import replace
    from comic_editor.core.pixel_contract import PixelContract
    from comic_editor.render.device import DeviceImage
    from comic_editor.render.pixels import capture_color_environment, display_image, pixel_scope, working_image
    from comic_editor.render.projection import ProjectionAddress
    from comic_editor.ui.document_presentation import PresentedTile
    from comic_editor.ui.eyedropper_sampling import _presented_color
    owner, *_ = canvas
    configuration = (None,)*6+(False, (None,), None, None)
    monkeypatch.setattr(owner, '_projection_has_live_preview', lambda: False)
    monkeypatch.setattr(owner, '_uses_document_projection', lambda: True)
    monkeypatch.setattr(owner, '_projection_configuration', lambda: configuration)
    queued = []
    monkeypatch.setattr(owner._scene_controller, 'ensure_cpu_tiles', queued.extend)
    class Graphics:
        closed, available = False, True
        materialize = staticmethod(lambda *_a: pytest.fail('GUI device readback'))
        release = staticmethod(lambda *_a: None)
    graphics = Graphics()
    image = DeviceImage(graphics, 123, ('native',), 32, 24, 1, 0)
    tile = PresentedTile((None, ProjectionAddress(0, 0, 0)), image,
                         QRectF(0, 0, 32, 24), QRectF(0, 0, 32, 24))
    owner._projection_completed_view = configuration, [(None, [tile])], owner._document_projection.revision
    assert _presented_color(owner, 10, 10) is None
    assert queued == [tile]
    contract = PixelContract(version=2, precision='float32', working_space='linear_srgb')
    environment = capture_color_environment(contract)
    native = working_image(np.full((24, 32, 4), (.08, .16, .24, .5), np.float32), contract)
    key = native.cacheKey()
    ready = replace(tile, image=native, pixel_contract=contract, pixel_environment=environment)
    owner._projection_completed_view = configuration, [(None, [ready])], owner._document_projection.revision
    with pixel_scope(contract, environment=environment):
        expected = display_image(native, contract).pixelColor(10, 10).name(QColor.HexArgb).upper()
    assert _presented_color(owner, 10, 10) == expected
    assert native.cacheKey() == key
    image.release()


def test_native_float_curves_probe_retains_sub_byte_alpha_and_straight_rgb(canvas):
    from comic_editor.core.pixel_contract import FLOAT_PIXELS
    from comic_editor.render.pixels import working_image
    from comic_editor.render.source_sampling import curves_pixel
    owner, obj, curves, _ = canvas
    owner.chapter.pixel_contract = FLOAT_PIXELS
    source = np.full((4, 8, 4), (.23e-9, .44e-9, .77e-9, 1e-9), np.float32)
    image = working_image(source, FLOAT_PIXELS)
    owner.images.put_decoded(obj.object_id, 'native.png', b'', image)
    snapshot = capture_document(owner.chapter, owner.tiles, owner.images)
    prefix = tuple(snapshot.chapter.objects[obj.object_id].modifier_ids)
    with ThreadPoolExecutor(max_workers=1) as worker:
        sampled = worker.submit(curves_pixel, snapshot, curves.modifier_id, (10., 10.),
                                (('object', obj.object_id),)).result(timeout=15)
    assert sampled is not None
    np.testing.assert_allclose(sampled[0], (.23, .44, .77), atol=1e-7, rtol=0.)
    assert sampled[1] == pytest.approx(1e-9, rel=1e-7)
    assert tuple(snapshot.chapter.objects[obj.object_id].modifier_ids) == prefix


def test_curves_picker_retains_final_release_without_gui_probe_or_full_history(canvas, monkeypatch):
    owner, obj, curves, _ = canvas
    before = owner.command_stack.revision
    owner.start_curves_picker(curves.modifier_id, 'add_point', 'rgb', 'master')
    picker = owner._curves_picker
    monkeypatch.setattr(owner, '_render_object', lambda *_a, **_k: pytest.fail('GUI Curves probe'))
    monkeypatch.setattr(ChapterDocument, 'to_dict', lambda *_a: pytest.fail('Whole chapter history'))
    start = owner.document_to_widget(QPointF(10, 10))
    picker.press(start)
    picker.move(start-QPointF(0, 15))
    picker.release(start-QPointF(0, 40))
    assert curves.curves == {}
    deadline = time.monotonic()+15
    while picker.state is not None and time.monotonic() < deadline:
        QTest.qWait(5)
    assert picker.state is None
    points = owner.chapter.modifiers[curves.modifier_id].curves['rgb:master']
    assert points[1][1] == pytest.approx(points[1][0]+.2)
    assert owner.command_stack.revision == before+1
    owner.command_stack.undo()
    assert owner.chapter.modifiers[curves.modifier_id].curves == {}
