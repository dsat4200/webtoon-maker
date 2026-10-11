"""Batch inspection preserves ordinary native cache keys and validation."""
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import BoundGeometry, CurvesModifier, ImageObject, RasterObject
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.resident_cache import ResidentRenderCache
from comic_editor.render.service import RenderPending
from comic_editor.ui.cache_dependencies import RenderDependencies
from test_detached_scene import canvas


@pytest.fixture
def dependency_scene(canvas, tmp_path):
    chapter = canvas.chapter
    chapter.width = chapter.height = 768
    page = chapter.layers[chapter.root_page_ids[0]]
    page.bound = BoundGeometry.rectangle(0, 0, 768, 768)
    modified = chapter.add_layer(page.layer_id, "Filtered", BoundGeometry.rectangle(0, 0, 768, 768))
    raster = chapter.add_object(modified.layer_id, RasterObject(interaction_rect=(0, 0, 768, 768)))
    chapter.add_modifier(CurvesModifier(), [("layer", modified.layer_id)])
    plain = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 768, 768)))
    image_obj = chapter.add_object(page.layer_id, ImageObject(pixel_width=768, pixel_height=768))
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    for obj in (plain, raster):
        for address in ((0, 0), (1, 1), (2, 2)):
            canvas.tiles.set_tile(obj.object_id, address, image)
    payload = QByteArray()
    buffer = QBuffer(payload)
    assert buffer.open(QIODevice.WriteOnly) and image.save(buffer, "PNG")
    buffer.close()
    canvas.images.put_decoded(image_obj.object_id, "source.png", bytes(payload), image)
    backing = PersistentRenderCache(tmp_path / "cache", contract=chapter.pixel_contract.signature)
    dependencies = RenderDependencies(canvas, backing)
    canvas._render_dependencies = dependencies
    canvas.tiles.render_fingerprint = dependencies.tiles
    canvas.images.render_fingerprint = dependencies.image
    requests = tuple(canvas._document_projection.requests(QRectF(0, 0, 768, 768), 1.))
    configuration = (*canvas._projection_configuration(), None)
    try:
        yield canvas, dependencies, requests, configuration, modified, raster, plain, image_obj
    finally:
        canvas._render_dependencies = None
        canvas.tiles.render_fingerprint = canvas.images.render_fingerprint = None
        backing.close()


def test_batch_native_keys_equal_single_lookup_and_reuse_shared_signatures(dependency_scene, monkeypatch):
    canvas, dependencies, requests, configuration, modified, *_ = dependency_scene
    expected = tuple(dependencies.projection_key(request, configuration) for request in requests)
    original = canvas._modifier_layer_signature
    calls = []
    def signature(layer_id):
        calls.append(layer_id)
        return original(layer_id)
    monkeypatch.setattr(canvas, "_modifier_layer_signature", signature)
    assert dependencies.projection_keys(requests, configuration) == expected
    assert calls.count(modified.layer_id) == 1
    # A later ordinary lookup inspects its dependencies again.
    assert dependencies.projection_key(requests[0], configuration) == expected[0]
    assert calls.count(modified.layer_id) == 2


def test_batch_retains_regional_source_invalidation_and_model_edits(dependency_scene):
    canvas, dependencies, requests, configuration, _modified, _raster, plain, _image = dependency_scene
    before = dependencies.projection_keys(requests, configuration)
    canvas.tiles.tile(plain.object_id, (0, 0)).fill(QColor("blue"))
    changed = dependencies.projection_keys(requests, configuration)
    assert changed != before
    assert changed == tuple(dependencies.projection_key(request, configuration) for request in requests)
    affected = [request.capture_rect.intersects(QRectF(0, 0, 256, 256)) for request in requests]
    assert all((old != new) == intersects for old, new, intersects in zip(before, changed, affected))
    plain.opacity = .4
    edited = dependencies.projection_keys(requests, configuration)
    assert edited != changed
    assert edited == tuple(dependencies.projection_key(request, configuration) for request in requests)


def test_batch_stat_memo_ends_before_next_source_validation(dependency_scene, monkeypatch):
    canvas, dependencies, requests, configuration, *_rest, image_obj = dependency_scene
    source_path = canvas.images.source(image_obj.object_id)._encoded.pin.path
    original = Path.stat
    calls = []
    def stat(path, *args, **kwargs):
        if path == source_path:
            calls.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", stat)
    before = dependencies.projection_keys(requests, configuration)
    assert len(calls) == 1
    # This is the fixture's private encoded source, never user artwork.
    source_path.write_bytes(source_path.read_bytes() + b"changed-source")
    after = dependencies.projection_keys(requests, configuration)
    assert len(calls) == 2
    assert after != before
    assert after == tuple(dependencies.projection_key(request, configuration) for request in requests)


def test_pending_batch_discards_partial_memos_for_retry(dependency_scene, monkeypatch):
    canvas, dependencies, requests, configuration, modified, *_ = dependency_scene
    original = canvas._modifier_layer_signature
    calls = []
    def signature(layer_id):
        calls.append(layer_id)
        return original(layer_id)
    monkeypatch.setattr(canvas, "_modifier_layer_signature", signature)
    original_key = dependencies.projection_key
    def pending(request, config):
        if request == requests[1]:
            raise RenderPending("Saved source is being checked")
        return original_key(request, config)
    monkeypatch.setattr(dependencies, "projection_key", pending)
    with pytest.raises(RenderPending):
        dependencies.projection_keys(requests, configuration)
    monkeypatch.setattr(dependencies, "projection_key", original_key)
    canvas.chapter.layers[modified.layer_id].opacity = .3
    retry = dependencies.projection_keys(requests, configuration)
    assert calls.count(modified.layer_id) == 2
    assert retry == tuple(original_key(request, configuration) for request in requests)


def test_batches_do_not_reuse_other_configuration_or_live_transform(dependency_scene):
    canvas, dependencies, requests, configuration, _modified, raster, *_ = dependency_scene
    before = dependencies.projection_keys(requests, configuration)
    top = (*configuration[:-1], "top")
    assert dependencies.projection_keys(requests, top) == tuple(
        dependencies.projection_key(request, top) for request in requests)
    assert dependencies.projection_keys(requests, top) != before
    canvas.selected_kind, canvas.selected_id, canvas.selected_object_id = "object", raster.object_id, raster.object_id
    canvas._transform_preview_quad = [(10., 0.), (778., 0.), (778., 768.), (10., 768.)]
    during = dependencies.projection_keys(requests, configuration)
    assert during != before
    assert during == tuple(dependencies.projection_key(request, configuration) for request in requests)
    canvas._transform_preview_quad = None
    assert dependencies.projection_keys(requests, configuration) == before


def test_dirty_pixel_identity_survives_save_adoption_new_owner_and_disk_reopen(canvas, tmp_path):
    page = canvas.chapter.root_page_ids[0]
    raster = canvas.chapter.add_object(page, RasterObject(interaction_rect=(0, 0, 64, 64)))
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    canvas.tiles.set_tile(raster.object_id, (0, 0), image)
    root = tmp_path / "cache"
    resident = ResidentRenderCache(budget=1024 * 1024).view(str(root))
    backing = PersistentRenderCache(root, resident=resident)
    reopened = None
    try:
        first = RenderDependencies(canvas, backing)
        before = first.tiles(canvas.tiles, raster.object_id)
        assert before[0][1][0] == "pixels"
        # Saving assigns encoded backing while keeping the same source revision.
        path = tmp_path / "saved.png"
        assert image.save(str(path), "PNG")
        owner = canvas.tiles._tiles[raster.object_id]
        version = owner.version((0, 0))
        owner.commit((0, 0), path)
        assert owner.version((0, 0)) == version
        second = RenderDependencies(canvas, backing)
        assert not second.tile_memo
        after = second.tiles(canvas.tiles, raster.object_id)
        assert after == before
        record = backing.source_digests[f"tile/{raster.object_id}/0_0"]
        assert tuple(record["pixels"]) == before[0][1]
        # Only explicit disk publication makes that ordinary sealed identity
        # durable; the runtime pixel registry is absent from a fresh process.
        with backing.record():
            backing.retain("source", ("pixel-identity-probe", after), image)
        backing.drain()
        reopened = PersistentRenderCache(root)
        assert reopened.resident is None
        third = RenderDependencies(canvas, reopened)
        assert third.tiles(canvas.tiles, raster.object_id) == before
        canvas.tiles.tile(raster.object_id, (0, 0)).fill(QColor("blue"))
        assert second.tiles(canvas.tiles, raster.object_id) != before
    finally:
        if reopened is not None:
            reopened.close()
        backing.close()
