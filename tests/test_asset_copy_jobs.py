"""Asset actions own frozen inputs and publish without GUI rendering or IO."""
from dataclasses import replace
import threading
import time

import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QIODevice, QThread
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QInputDialog, QMessageBox

from comic_editor.core.assets import AssetRepository, extract_asset
from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject, ShapeStyle
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore
from comic_editor.render.outputs import capture_document, entity_crop
from comic_editor.render.pixels import FLOAT_PIXELS, premultiplied_pixels
from comic_editor.ui.main_window import MainWindow


def wait_for(predicate):
    deadline = time.monotonic()+30
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.002)
    assert predicate()


def create_window(tmp_path):
    repository = SeriesRepository(tmp_path/'Series')
    series = repository.create('Series')
    repository.create_chapter(series, 'Chapter')
    window = MainWindow()
    assert window.open_series(repository.root)
    return window


def dispose(window):
    jobs = getattr(window, '_asset_copy_jobs', None)
    if jobs is not None:
        jobs.drain()
        jobs.shutdown()
        jobs.executor.shutdown(wait=True)
    for session in window.sessions.values():
        session.dirty = False
    window._dirty = False
    window.deleteLater()


def test_copy_action_uses_detached_native_preparation_and_serial_publication(qapp, tmp_path, monkeypatch):
    from comic_editor.ui import asset_copy_jobs
    window = create_window(tmp_path)
    started, release = threading.Event(), threading.Event()
    threads = []
    original = asset_copy_jobs.prepare_asset
    context = window.active_session.context
    source = next(layer for layer in window.chapter.layers.values() if not layer.is_page)
    source.name = 'Captured source'
    monkeypatch.setattr(QInputDialog, 'getText', lambda *a, **k: ('Owned asset', True))
    monkeypatch.setattr(window.canvas, 'render_asset_thumbnail',
        lambda *a, **k: (_ for _ in ()).throw(AssertionError('GUI thumbnail render')))
    def prepare(*arguments):
        threads.append(QThread.currentThread())
        started.set()
        assert release.wait(15)
        return original(*arguments)
    publish = AssetRepository.create
    def create(repository, *arguments, **keywords):
        threads.append(QThread.currentThread())
        return publish(repository, *arguments, **keywords)
    monkeypatch.setattr(asset_copy_jobs, 'prepare_asset', prepare)
    monkeypatch.setattr(AssetRepository, 'create', create)
    try:
        window._copy_selected_as_asset('layer', source.layer_id)
        assert window._asset_copy_jobs.busy and not started.is_set()
        wait_for(started.is_set)
        source.name = 'Newer edit'
        window._mark_dirty(None)
        release.set()
        wait_for(lambda: not window._asset_copy_jobs.busy)
        created = context.assets.find_by_name('Owned asset')
        assert created is not None
        assert created.document.layers[created.root_id].name == 'Captured source'
        assert source.name == 'Newer edit' and window._dirty
        assert len(threads) == 2 and all(thread != window.thread() for thread in threads)
    finally:
        release.set()
        dispose(window)


def test_asset_clone_validates_private_records_and_retained_snapshot_stays_unchanged(qapp, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from comic_editor.core.models import (RasterObject, CurvesModifier, LayerNode,
                                          ParameterMaskBinding, ToneMask)
    from comic_editor.core.settings import EditorSettings
    from comic_editor.render.asset_sources import prepare_asset
    from comic_editor.render.scene import SceneSnapshotCompiler
    from comic_editor.ui.canvas import CanvasWidget
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    chapter = ChapterDocument(width=64, height=64, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 64, 64))
    root = chapter.add_layer(page.layer_id, bound=BoundGeometry.rectangle(0, 0, 64, 64))
    raster = chapter.add_object(root.layer_id, RasterObject(interaction_rect=(0, 0, 64, 64)))
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    modifier = CurvesModifier(parameter_masks={'intensity': ParameterMaskBinding(mask.mask_id)})
    chapter.add_modifier(modifier, [('object', raster.object_id)])
    tiles = TileStore()
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(0xff224488)
    tiles.set_tile(raster.object_id, (0, 0), image)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, tiles)
    try:
        capture = SceneSnapshotCompiler().capture(canvas, canvas._render_document_state())
        while not capture.advance(.004):
            pass
        snapshot = capture.result
        records = (*snapshot.chapter.layers.values(), *snapshot.chapter.objects.values(),
                   *snapshot.chapter.modifiers.values(), *snapshot.chapter.masks.values())
        frozen = {id(record) for record in records}
        before = [(id(record), repr(record)) for record in records]
        serialized = []
        for record_type in (LayerNode, RasterObject, CurvesModifier, ToneMask):
            original = record_type.to_dict
            def private_only(record, original=original):
                assert id(record) not in frozen, 'Asset clone serializer mutated a shared snapshot record'
                serialized.append(type(record))
                return original(record)
            monkeypatch.setattr(record_type, 'to_dict', private_only)
        with ThreadPoolExecutor(max_workers=2) as workers:
            preparation = workers.submit(prepare_asset, snapshot, 'layer', root.layer_id, 'Owned clone')
            retained = workers.submit(lambda: [(id(record), repr(record)) for record in records])
            manifest, copied, _images, thumbnail = preparation.result(10)
            assert retained.result(10) == before
        assert [(id(record), repr(record)) for record in records] == before
        assert {LayerNode, RasterObject, CurvesModifier, ToneMask} <= set(serialized)
        assert manifest.root_id == root.layer_id and not thumbnail.isNull()
        assert copied.tile(raster.object_id, (0, 0)).format() == image.format()
    finally:
        canvas._scene_controller.reset()
        canvas._scene_controller.scheduler.close()
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()


def test_replacing_active_asset_preserves_edits_made_after_capture(qapp, tmp_path, monkeypatch):
    from comic_editor.ui import asset_copy_jobs
    window = create_window(tmp_path)
    context = window.active_session.context
    source = next(layer for layer in window.chapter.layers.values() if not layer.is_page)
    manifest, tiles = extract_asset(window.chapter, window.canvas.tiles, 'layer', source.layer_id, 'Hero')
    thumbnail = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    thumbnail.fill(0)
    context.assets.create(manifest, tiles, thumbnail)
    window._open_asset(manifest.asset_id)
    session = window.active_session
    source = window.chapter.layers[manifest.root_id]
    source.name = 'Captured source'
    started, release = threading.Event(), threading.Event()
    original = asset_copy_jobs.prepare_asset
    def prepare(*arguments):
        started.set()
        assert release.wait(15)
        return original(*arguments)
    monkeypatch.setattr(asset_copy_jobs, 'prepare_asset', prepare)
    monkeypatch.setattr(QInputDialog, 'getText', lambda *a, **k: ('Hero', True))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.Yes)
    try:
        window._copy_selected_as_asset('layer', source.layer_id)
        wait_for(started.is_set)
        before = window.chapter.to_dict()
        source.name = 'Newer edit'
        window.canvas.push_model_change(before, window.chapter.to_dict(), 'Newer edit')
        release.set()
        wait_for(lambda: not window._asset_copy_jobs.busy)
        stored, _tiles = context.assets.load(manifest.asset_id)
        assert stored.document.layers[stored.root_id].name == 'Captured source'
        assert session.chapter is window.chapter is window.canvas.chapter
        assert source.name == 'Newer edit' and session.dirty and window._dirty
        assert window.canvas.command_stack.can_undo
    finally:
        release.set()
        dispose(window)


def test_frozen_copy_finishes_in_origin_project_after_switching_documents(qapp, tmp_path, monkeypatch):
    from comic_editor.ui import asset_copy_jobs
    window = create_window(tmp_path)
    context = window.active_session.context
    source = next(layer for layer in window.chapter.layers.values() if not layer.is_page)
    started, release = threading.Event(), threading.Event()
    original = asset_copy_jobs.prepare_asset
    def prepare(*arguments):
        started.set()
        assert release.wait(15)
        return original(*arguments)
    monkeypatch.setattr(asset_copy_jobs, 'prepare_asset', prepare)
    monkeypatch.setattr(QInputDialog, 'getText', lambda *a, **k: ('Origin asset', True))
    try:
        window._copy_selected_as_asset('layer', source.layer_id)
        wait_for(started.is_set)
        other = SeriesRepository(tmp_path/'Other')
        series = other.create('Other')
        other.create_chapter(series, 'Other chapter')
        assert window.open_series(other.root)
        current = window.chapter
        pages = []
        monkeypatch.setattr(window.ribbon, 'select_page', pages.append)
        release.set()
        wait_for(lambda: not window._asset_copy_jobs.busy)
        assert context.assets.find_by_name('Origin asset') is not None
        assert window.chapter is current
        assert window.active_session.context.assets.list_assets() == []
        assert 'asset_library' not in pages
    finally:
        release.set()
        dispose(window)


def test_float_asset_extraction_preserves_policy_original_bytes_and_native_output(qapp, tmp_path):
    contract = replace(FLOAT_PIXELS, working_space='linear_srgb')
    chapter = ChapterDocument(width=16, height=16, document_kind='asset',
        pixel_contract=contract, background='#00000000')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 16, 16),
        style=ShapeStyle(primary_color=None, outline_thickness=0))
    obj = chapter.add_object(page.layer_id, ImageObject(x=2, y=3, pixel_width=3, pixel_height=2))
    original = QImage(3, 2, QImage.Format_RGBA64)
    np.frombuffer(original.bits(), np.uint16).reshape(2, 3, 4)[:] = (32768, 16384, 8192, 1)
    data = QByteArray()
    output = QBuffer(data)
    output.open(QIODevice.WriteOnly)
    assert original.save(output, 'PNG')
    output.close()
    encoded = bytes(data)
    images, tiles = ImageStore(), TileStore()
    images.put(obj.object_id, 'native.png', encoded)
    manifest, sources, copied_images = extract_asset(chapter, tiles,
        'object', obj.object_id, 'Float asset', source_images=images, include_images=True)
    assert manifest.document.pixel_contract == contract
    expected = entity_crop(capture_document(chapter, tiles, images), 'object', obj.object_id)
    actual = entity_crop(capture_document(manifest.document, sources, copied_images),
                         'object', obj.object_id)
    np.testing.assert_array_equal(premultiplied_pixels(actual), premultiplied_pixels(expected))
    assert premultiplied_pixels(actual)[..., 3].max() == np.float32(1/65535)
    repository = AssetRepository(tmp_path/'Series')
    thumbnail = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    thumbnail.fill(0)
    repository.create(manifest, sources, thumbnail, images=copied_images)
    reloaded, saved_tiles, saved_images = repository.load(manifest.asset_id, include_images=True)
    assert reloaded.document.pixel_contract == contract
    assert saved_images.source(obj.object_id).data == encoded
    restored = entity_crop(capture_document(reloaded.document, saved_tiles, saved_images),
                           'object', obj.object_id)
    np.testing.assert_array_equal(premultiplied_pixels(restored), premultiplied_pixels(expected))
