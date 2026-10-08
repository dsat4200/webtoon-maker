"""Native crop/move oracles and ordinary shared durable renderer migration."""
import copy
import hashlib
import json
import threading

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.core.models import SharpnessModifier
from comic_editor.render import cache as cache_storage
from comic_editor.render.effect_pipeline import empty_image
from comic_editor.render.modifier_rendering import apply_modifier_stack
from comic_editor.render.pixels import pixel_scope
from comic_editor.render.projection import DocumentProjection
from comic_editor.render.scheduler import SceneScheduler
from comic_editor.ui import translation_cache
from test_performance_monitor_detached import (make_owner, freeze, large_alias_scene,
    evaluate_whole_alias_scene, source_payload)

OLD_RENDERER = 'native-artwork-20261007-refactor-visibility-source-1'


def full_original_sharpness_oracle(window, snapshot, obj, effect):
    """One original full source/kernel, then integer placement and native gutters."""
    assert not snapshot.document.pixel_contract.floating
    assert not effect.parameter_masks and obj.opacity_mask is None
    assert obj.transform_quad is None and obj.opacity == 1 and obj.opacity_locked
    assert window.canvas.layer_world_transform(obj.parent_layer_id).isIdentity()
    assert snapshot.document.background == '#00000000'
    assert obj.x == int(obj.x) and obj.y == int(obj.y)
    source_bounds = window.canvas.tiles.content_bounds(obj.object_id)
    assert source_bounds is not None and source_bounds == QRectF(source_bounds.toAlignedRect())
    with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
        assert source_bounds.width() * source_bounds.height() > 256 * 256
        source = empty_image(source_bounds)
        painter = QPainter(source)
        try:
            for (x, y), tile in window.canvas.tiles.iter_tiles(obj.object_id, source_bounds):
                assert tile.format() == source.format()
                painter.drawImage(QPointF(x * obj.tile_size - source_bounds.x(),
                    y * obj.tile_size - source_bounds.y()), tile)
        finally:
            painter.end()
        filtered = apply_modifier_stack(source, [copy.deepcopy(effect)],
            (source_bounds.x() + obj.x, source_bounds.y() + obj.y), {},
            world_to_image=QTransform.fromTranslate(-source_bounds.x(), -source_bounds.y()),
            nearest=True, pixel_origin=(0, 0))
        assert filtered is not None and filtered.size() == source.size()
        document = snapshot.document
        whole = empty_image(QRectF(0, 0, document.width, document.height))
        painter = QPainter(whole)
        try:
            painter.drawImage(QPointF(obj.x + source_bounds.x(), obj.y + source_bounds.y()), filtered)
        finally:
            painter.end()
        requests = DocumentProjection().requests(QRectF(0, 0, document.width, document.height), 1.)
        result = {}
        for request in requests:
            assert request.scale == 1 and request.tile_size == 256 and request.gutter == 2
            tile = whole.copy(request.capture_rect.toAlignedRect())
            assert tile.width() == tile.height() == request.pixel_size == 260
            result[request.address] = (tile.size(), tile.format(), tile.bytesPerLine(), bytes(tile.constBits()))
        return result


def independent_lookup_disabled_reference(snapshot, obj, monkeypatch, serial):
    """The reference bypasses only the suspect translation optimization."""
    ordinary_get = translation_cache.get
    def reference_get(canvas, key):
        if isinstance(key, tuple) and len(key) == 3 and key[:2] == ('translated-exact-output', obj.object_id):
            return None
        return ordinary_get(canvas, key)
    scheduler = SceneScheduler()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(translation_cache, 'get', reference_get)
            return evaluate_whole_alias_scene(scheduler, snapshot, serial)
    finally:
        scheduler.close()
        scheduler.executor.shutdown(wait=True, cancel_futures=False)


@pytest.mark.parametrize('crops', [
    [(32, 40, 160, 170)],
    [(32, 40, 160, 170), (1080, 920, 130, 130), (32, 40, 160, 170)]])
def test_partial_crops_then_whole_equal_full_original_sharpness_and_cold_reference(
        make_owner, monkeypatch, crops):
    window, monitor, _unused = make_owner()
    obj, effect = large_alias_scene(window, local_halo=True)
    assert not monitor.enabled
    snapshot = freeze(window.canvas)
    before_model, before_source = copy.deepcopy(window.canvas.chapter.to_dict()), source_payload(window.canvas)
    oracle = full_original_sharpness_oracle(window, snapshot, obj, effect)
    expected = independent_lookup_disabled_reference(snapshot, obj, monkeypatch, 401)
    assert expected == oracle, 'Original full Sharpness kernel and native tile pipeline disagree'
    assert len(expected) == 30
    admitted, lock = [], threading.Lock()
    ordinary_put = translation_cache.put
    def observe_put(canvas, key, image, revision):
        if key is not None and key[:2] == ('translated-exact-output', obj.object_id):
            with lock:
                admitted.append((image.width(), image.height()))
        return ordinary_put(canvas, key, image, revision)
    monkeypatch.setattr(translation_cache, 'put', observe_put)
    scheduler = SceneScheduler()
    try:
        for index, rect in enumerate(crops):
            actual_roi = evaluate_whole_alias_scene(scheduler, snapshot, 402 + index,
                requested_world=QRectF(*rect))
            assert set(actual_roi) < set(expected)
            assert actual_roi == {address: expected[address] for address in actual_roi}
        actual = evaluate_whole_alias_scene(scheduler, snapshot, 410)
        assert actual == oracle == expected
        assert evaluate_whole_alias_scene(scheduler, snapshot, 411) == oracle
        assert not admitted, 'All actual grouped outputs are partial frames and cannot become full-frame aliases'
    finally:
        scheduler.close()
        scheduler.executor.shutdown(wait=True, cancel_futures=False)
    assert window.canvas.chapter.to_dict() == before_model and source_payload(window.canvas) == before_source


def test_complete_frame_translation_alias_reuses_exact_moved_native_pixels(make_owner, monkeypatch):
    window, monitor, _unused = make_owner()
    obj, previous = large_alias_scene(window)
    effect = SharpnessModifier(modifier_id=previous.modifier_id, radius=1.25, strength=83, threshold=0)
    window.canvas.chapter.modifiers[effect.modifier_id] = effect
    assert not monitor.enabled
    snapshot = freeze(window.canvas)
    before_source = source_payload(window.canvas)
    expected = independent_lookup_disabled_reference(snapshot, obj, monkeypatch, 420)
    assert expected == full_original_sharpness_oracle(window, snapshot, obj, effect)
    hits, phase, lock = [], ['before_move'], threading.Lock()
    ordinary_get = translation_cache.get
    def observe_get(canvas, key):
        result = ordinary_get(canvas, key)
        if result is not None and isinstance(key, tuple) and key[:2] == ('translated-exact-output', obj.object_id):
            with lock:
                hits.append((phase[0], result.width(), result.height()))
        return result
    monkeypatch.setattr(translation_cache, 'get', observe_get)
    scheduler = SceneScheduler()
    try:
        assert evaluate_whole_alias_scene(scheduler, snapshot, 421) == expected
        assert evaluate_whole_alias_scene(scheduler, snapshot, 422) == expected
        assert any(row[0] == 'before_move' and row[1:] == (321, 289) for row in hits)
        # Fixture-only committed integer placement; original raster grids/pixels stay unchanged.
        obj.x += 17
        obj.y += 23
        after_model = copy.deepcopy(window.canvas.chapter.to_dict())
        moved = freeze(window.canvas)
        assert not moved.document.live_preview
        moved_expected = independent_lookup_disabled_reference(moved, obj, monkeypatch, 423)
        assert moved_expected == full_original_sharpness_oracle(window, moved, obj, effect)
        phase[0] = 'after_move'
        assert evaluate_whole_alias_scene(scheduler, moved, 424) == moved_expected
        assert any(row[0] == 'after_move' and row[1:] == (321, 289) for row in hits)
        assert window.canvas.chapter.to_dict() == after_model
    finally:
        scheduler.close()
        scheduler.executor.shutdown(wait=True, cancel_futures=False)
    assert source_payload(window.canvas) == before_source


@pytest.mark.parametrize('kind', ['effect', 'retained', 'projection'])
def test_old_bad_translation_and_final_tiles_reject_shared_renderer_version_without_deletion(
        tmp_path, monkeypatch, kind):
    key = (('translated-exact-output', 'unchanged-subject', 'full-frame-semantic-key')
        if kind == 'effect' else ('retained', ('output', ('translation', 'subject', 'translated-exact-output', 'canvas')),
             ('translated-exact-output', 'subject', 'full'))
        if kind == 'retained' else ('chapter', 0, 0, 0, 256, 2, 'unchanged-scene-dependencies'))
    image = QImage(9, 7, QImage.Format_ARGB32_Premultiplied)
    image.fill(0xffff00ff)
    with monkeypatch.context() as patch:
        patch.setattr(cache_storage, 'RENDERER_VERSION', OLD_RENDERER)
        old = cache_storage.PersistentRenderCache(tmp_path)
        old_identity = old.descriptor(kind, key).identity
        try:
            with old.record():
                old.retain(kind, key, image, state=(1, QRectF(0, 0, 9, 7)) if kind == 'retained' else None)
            old.drain()
            assert old.lookup(kind, key, wait=True) is not None
        finally:
            old.close()
    before = {path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.rglob('*') if path.is_file()}
    reopened = cache_storage.PersistentRenderCache(tmp_path)
    try:
        assert cache_storage.RENDERER_VERSION != OLD_RENDERER
        assert reopened.descriptor(kind, key).identity != old_identity
        assert not reopened.entries and reopened.lookup(kind, key, wait=True) is None
        assert {path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in tmp_path.rglob('*') if path.is_file()} == before
        # Only an explicit ordinary recording builds current exact entries.
        current = QImage(9, 7, QImage.Format_ARGB32_Premultiplied)
        current.fill(0xff2080d0)
        with reopened.record():
            reopened.retain(kind, key, current, state=(1, QRectF(0, 0, 9, 7)) if kind == 'retained' else None)
        reopened.drain()
        restored = reopened.lookup(kind, key, wait=True)
        assert restored.format() == current.format() and restored.size() == current.size()
        assert bytes(restored.constBits()) == bytes(current.constBits())
        assert json.loads((tmp_path / 'index.json').read_text('utf-8'))['renderer'] == cache_storage.RENDERER_VERSION
    finally:
        reopened.close()
