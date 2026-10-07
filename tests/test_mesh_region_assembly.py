"""Pending mesh regions retain exact native progress in the existing budget."""
from collections import Counter

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (BoundGeometry, ChapterDocument, DistortModifier,
                                      ParameterMaskBinding, ToneMask)
from comic_editor.core.pixel_contract import LEGACY_PIXELS, PixelContract
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.pixels import pixel_scope, working_image
from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.distort_regions import render_mesh_regions
from comic_editor.ui.effect_pipeline import render_stages


CONTRACTS = [LEGACY_PIXELS,
             PixelContract(version=2, precision='float16', working_space='linear_srgb'),
             PixelContract(version=2, precision='float32', working_space='linear_srgb')]


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    chapter = ChapterDocument(width=600, height=240, document_kind='asset')
    chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 600, 240))
    canvas.set_document(chapter, TileStore())
    canvas._projection_exact = canvas._projection_defer_effects = True
    canvas._interactive_render = True
    yield canvas
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def source(contract):
    yy, xx = np.mgrid[:65, :513]
    alpha = ((xx * 3 + yy) % 193) / 192.
    data = np.stack((xx / 400., yy / 45., (xx % 7) / 4., np.ones_like(xx)), axis=-1)
    return working_image((data * alpha[..., None]).astype(np.float32), contract)


def setup(scene, contract=LEGACY_PIXELS):
    scene.chapter.pixel_contract = contract
    bounds = QRectF(0, 0, 513, 65)
    modifier = DistortModifier(modifier_type='distort_mesh_warp', frame=bounds.getRect(),
        points=[(.04, .03), (.94, .02), (.08, .91), (.99, .97)],
        source_points=[(0., 0.), (1., 0.), (0., 1.), (1., 1.)],
        parameters={'rows': 2, 'columns': 2, 'smoothness': 0.,
                    'interpolation': 'bilinear', 'edges': 'transparent'})
    modifier.validate()
    scene.chapter.modifiers[modifier.modifier_id] = modifier
    return source(contract), bounds, modifier


def stage_key(scene, bounds, modifier, source_revision=0):
    return ('stage', ('source', source_revision), scene._rect_signature(bounds),
            scene._rect_signature(bounds), str(modifier.to_dict()),
            scene._modifier_parameter_signature([modifier.modifier_id]),
            (0., 0.), False, (), (1., 0., 0., 0., 1., 0., 0., 0., 1.))


def regional(scene, image, bounds, modifier, source_revision=0, target=None):
    target = bounds if target is None else target
    key = list(stage_key(scene, bounds, modifier, source_revision))
    key[3] = scene._rect_signature(target)
    return render_mesh_regions(scene, image, bounds, bounds, target, modifier,
        QTransform(), tuple(key),
        ('object', 'assembly-test', modifier.modifier_id))


def controlled_tiles(scene, monkeypatch, *, ready=None):
    """Real canonical pipeline pixels, with deterministic detached-work readiness."""
    from comic_editor.ui import distort_pipeline
    original = distort_pipeline.render_distort_stage
    ready = {0} if ready is None else ready
    calls = Counter()
    def stage(*args, **kwargs):
        target = args[4]
        address = int(target.left()) // 256
        if address not in ready:
            raise ProjectionPending(args[9], args[8])
        calls[address] += 1
        before = scene._projection_defer_effects
        scene._projection_defer_effects = False
        try:
            return original(*args, **kwargs)
        finally:
            scene._projection_defer_effects = before
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', stage)
    return ready, calls


def discard_ordinary_tiles(jobs):
    for scope in tuple(jobs.retained):
        jobs.retained_remove(scope)


@pytest.mark.parametrize('contract', CONTRACTS, ids=['uint8', 'float16', 'float32'])
@pytest.mark.parametrize('intensity', [100., 37.])
def test_pending_progress_survives_record_pressure_and_matches_cold_native(scene, monkeypatch, contract, intensity):
    with pixel_scope(contract):
        image, bounds, modifier = setup(scene, contract)
        modifier.intensity = intensity
        # A fresh, nondeferred source/pipeline is the full native oracle.
        scene._projection_defer_effects = False
        expected, expected_bounds = render_stages(scene, image, bounds, [modifier], QTransform())
        assert expected_bounds == bounds
        scene._modifier_render_cache.clear()
        scene._modifier_render_cache_bytes = 0
        scene._projection_defer_effects = True
        ready, calls = controlled_tiles(scene, monkeypatch)
        jobs = scene._effect_jobs
        jobs.retained_limit = 2
        jobs.retained_budget = int(expected.sizeInBytes()) + 256 * 65 * image.depth() // 8 + 4096
        with pytest.raises(ProjectionPending):
            regional(scene, image, bounds, modifier)
        entry = next(iter(jobs._regions.values()))
        assert entry.covered == {(0, 0)} and not entry.complete
        assert jobs.retained_bytes <= jobs.retained_budget
        for index in range(6):
            pixel = QImage(1, 1, image.format())
            pixel.fill(index)
            jobs.retained_put(('unrelated', index), ('unrelated', index), pixel)
        assert next(iter(jobs._regions.values())) is entry
        ready.add(1)
        with pytest.raises(ProjectionPending):
            regional(scene, image, bounds, modifier)
        assert entry.covered == {(0, 0), (1, 0)}
        assert calls[0] == 1
        discard_ordinary_tiles(jobs)
        ready.add(2)
        actual, provisional = regional(scene, image, bounds, modifier)
        assert not provisional and actual == expected
        assert calls[0] == calls[1] == 1
        assert entry.complete and jobs.retained_bytes <= jobs.retained_budget
        discard_ordinary_tiles(jobs)
        again, provisional = regional(scene, image, bounds, modifier)
        assert again == expected and not provisional and calls[0] == calls[1] == 1


@pytest.mark.parametrize('change', ['history', 'revision', 'chapter', 'tiles', 'images', 'pixel-contract', 'cancel'])
def test_context_change_cannot_publish_previous_partial_pixels(scene, monkeypatch, change):
    image, bounds, modifier = setup(scene)
    ready, calls = controlled_tiles(scene, monkeypatch)
    with pytest.raises(ProjectionPending):
        regional(scene, image, bounds, modifier)
    previous = next(iter(scene._effect_jobs._regions.values()))
    discard_ordinary_tiles(scene._effect_jobs)
    if change == 'history':
        scene._history_generation = getattr(scene, '_history_generation', 0) + 1
    elif change == 'revision':
        scene._document_projection.invalidate()
    elif change == 'chapter':
        scene.chapter = ChapterDocument.from_dict(scene.chapter.to_dict())
    elif change == 'tiles':
        scene.tiles = TileStore()
    elif change == 'images':
        scene.images = ImageStore()
    elif change == 'pixel-contract':
        scene.chapter.pixel_contract = CONTRACTS[1]
    else:
        scene._effect_jobs.cancel()
    ready.update({1, 2})
    actual, provisional = regional(scene, image, bounds, modifier)
    assert not provisional and not actual.isNull() and calls[0] == 2
    assert all(entry is not previous for entry in scene._effect_jobs._regions.values())


@pytest.mark.parametrize('change', ['source', 'parameter'])
def test_changed_semantic_key_does_not_complete_old_partial(scene, monkeypatch, change):
    image, bounds, modifier = setup(scene)
    ready, calls = controlled_tiles(scene, monkeypatch)
    with pytest.raises(ProjectionPending):
        regional(scene, image, bounds, modifier)
    previous = next(iter(scene._effect_jobs._regions.values()))
    discard_ordinary_tiles(scene._effect_jobs)
    revision = 0
    if change == 'source':
        revision = 1
        image = QImage(image)
        image.fill(0xff11aaff)
    else:
        modifier.intensity = 19.
    ready.update({1, 2})
    actual, provisional = regional(scene, image, bounds, modifier, revision)
    assert not provisional and not actual.isNull() and calls[0] == 2
    assert not previous.complete


def test_reentrant_cancellation_discards_private_progress_before_publish(scene, monkeypatch):
    image, bounds, modifier = setup(scene)
    from comic_editor.ui import distort_pipeline
    original = distort_pipeline.render_distort_stage
    def cancel(*args, **kwargs):
        before = scene._projection_defer_effects
        scene._projection_defer_effects = False
        try:
            result = original(*args, **kwargs)
        finally:
            scene._projection_defer_effects = before
        if args[4].left() == 256:
            scene._effect_jobs.cancel()
        return result
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', cancel)
    with pytest.raises(ProjectionPending) as error:
        regional(scene, image, bounds, modifier)
    assert error.value.scope == 'stale-frame-assembly'
    assert not scene._effect_jobs._regions


def test_provisional_mask_fields_never_become_exact_coverage(scene, monkeypatch):
    image, bounds, modifier = setup(scene)
    def fields(*_):
        scene._effect_provisional_revision = getattr(scene, '_effect_provisional_revision', 0) + 1
        return {}
    monkeypatch.setattr(scene, '_modifier_mask_fields', fields)
    with pytest.raises(ProjectionPending):
        regional(scene, image, bounds, modifier)
    assert not scene._effect_jobs._regions
    assert not scene._effect_jobs.pending and not scene._effect_jobs.running_jobs


def test_partial_work_never_reaches_memory_or_disk_recording_entry_point(scene, monkeypatch):
    image, bounds, modifier = setup(scene)
    controlled_tiles(scene, monkeypatch)
    from comic_editor.ui import cache_dependencies
    captured = []
    def record(canvas, namespace, key, value, **kwargs):
        captured.append(value.width())
        assert value.width() <= 256
    monkeypatch.setattr(cache_dependencies, 'cache_put', record)
    with pytest.raises(ProjectionPending):
        regional(scene, image, bounds, modifier)
    assert captured and bounds.width() not in captured


def test_failed_dependency_preserves_only_private_exact_progress(scene, monkeypatch):
    image, bounds, modifier = setup(scene)
    ready, _ = controlled_tiles(scene, monkeypatch)
    from comic_editor.ui import distort_pipeline
    current = distort_pipeline.render_distort_stage
    def failed(*args, **kwargs):
        if args[4].left() == 256:
            raise ProjectionFailed(args[9], args[8], 'failed native mesh dependency')
        return current(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', failed)
    with pytest.raises(ProjectionFailed):
        regional(scene, image, bounds, modifier)
    entry = next(iter(scene._effect_jobs._regions.values()))
    assert not entry.complete and entry.covered == {(0, 0)}


@pytest.mark.parametrize('contract', CONTRACTS, ids=['uint8', 'float16', 'float32'])
def test_bound_mask_revision_starts_new_private_coverage_and_matches_cold_native(scene, monkeypatch, contract):
    with pixel_scope(contract):
        image, bounds, modifier = setup(scene, contract)
        mask = ToneMask()
        scene.chapter.masks[mask.mask_id] = mask
        modifier.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0, 100)
        def fields(modifiers, width, height, world_to_image, _bounds):
            inverse = world_to_image.inverted()[0]
            yy, xx = np.mgrid[:height, :width]
            world_x = inverse.m11() * (xx + .5) + inverse.m21() * (yy + .5) + inverse.dx()
            field = np.clip(world_x / 700 + mask.revision * .3, 0, 1).astype(np.float32)
            return {(modifier.modifier_id, 'intensity'): field}
        monkeypatch.setattr(scene, '_modifier_mask_fields', fields)
        scene._projection_defer_effects = False
        original, _ = render_stages(scene, image, bounds, [modifier], QTransform())
        scene._modifier_render_cache.clear()
        scene._modifier_render_cache_bytes = 0
        scene._projection_defer_effects = True
        ready, calls = controlled_tiles(scene, monkeypatch)
        with pytest.raises(ProjectionPending):
            regional(scene, image, bounds, modifier)
        previous = next(iter(scene._effect_jobs._regions.values()))
        discard_ordinary_tiles(scene._effect_jobs)
        mask.revision += 1
        ready.update({1, 2})
        actual, provisional = regional(scene, image, bounds, modifier)
        assert not provisional and not previous.complete and calls[0] == 2
        scene._modifier_render_cache.clear()
        scene._modifier_render_cache_bytes = 0
        scene._projection_defer_effects = False
        expected, expected_bounds = render_stages(scene, image, bounds, [modifier], QTransform())
        assert expected_bounds == bounds and actual == expected and actual != original


def test_partial_survives_byte_pressure_without_expanding_existing_budget(scene, monkeypatch):
    image, bounds, modifier = setup(scene)
    ready, calls = controlled_tiles(scene, monkeypatch)
    jobs = scene._effect_jobs
    jobs.retained_budget = int(image.sizeInBytes()) + 256 + 192 * 3
    with pytest.raises(ProjectionPending):
        regional(scene, image, bounds, modifier)
    entry = next(iter(jobs._regions.values()))
    pressure = QImage(300, 100, image.format())
    pressure.fill(0xffaabbcc)
    assert not jobs.retained_put(('pressure',), ('pressure',), pressure)
    assert next(iter(jobs._regions.values())) is entry
    assert jobs.retained_bytes <= jobs.retained_budget
    ready.update({1, 2})
    actual, provisional = regional(scene, image, bounds, modifier)
    assert not provisional and entry.complete and sum(calls.values()) == 3
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    scene._projection_defer_effects = False
    expected, _ = render_stages(scene, image, bounds, [modifier], QTransform())
    assert actual == expected and jobs.retained_bytes <= jobs.retained_budget


def test_mesh_and_tile_graph_share_the_existing_private_context(scene, monkeypatch):
    from comic_editor.ui.distort_regions import _assembly_context
    image, bounds, modifier = setup(scene)
    controlled_tiles(scene, monkeypatch)
    store = scene._effect_jobs.private_regions(_assembly_context(scene), lambda: _assembly_context(scene))
    neighbor = store.begin(('tile-region-assembly', 'neighbor'), QRectF(0, 0, 4, 4), image.format(), 1)
    with pytest.raises(ProjectionPending):
        regional(scene, image, bounds, modifier)
    assert store.get(('tile-region-assembly', 'neighbor')) is neighbor
    assert len(scene._effect_jobs._regions) == 2


def test_reentrant_document_revision_cannot_publish_old_coverage(scene, monkeypatch):
    image, bounds, modifier = setup(scene)
    from comic_editor.ui import distort_pipeline
    original = distort_pipeline.render_distort_stage
    def invalidate(*args, **kwargs):
        before = scene._projection_defer_effects
        scene._projection_defer_effects = False
        try:
            result = original(*args, **kwargs)
        finally:
            scene._projection_defer_effects = before
        if args[4].left() == 256:
            scene._document_projection.invalidate()
        return result
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', invalidate)
    with pytest.raises(ProjectionPending) as error:
        regional(scene, image, bounds, modifier)
    assert error.value.scope == 'stale-frame-assembly'
    assert all(not entry.complete for entry in scene._effect_jobs._regions.values())


@pytest.mark.parametrize('contract', CONTRACTS, ids=['uint8', 'float16', 'float32'])
@pytest.mark.parametrize('masked', [False, True])
def test_overlapping_targets_reuse_shared_native_coverage_after_tile_eviction(scene, monkeypatch, contract, masked):
    with pixel_scope(contract):
        image, bounds, modifier = setup(scene, contract)
        if masked:
            mask = ToneMask()
            scene.chapter.masks[mask.mask_id] = mask
            modifier.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0, 100)
            def fields(modifiers, width, height, world_to_image, _bounds):
                inverse = world_to_image.inverted()[0]
                yy, xx = np.mgrid[:height, :width]
                world_x = inverse.m11() * (xx + .5) + inverse.m21() * (yy + .5) + inverse.dx()
                return {(modifier.modifier_id, 'intensity'):
                        np.clip(world_x / 650, 0, 1).astype(np.float32)}
            monkeypatch.setattr(scene, '_modifier_mask_fields', fields)
        scene._projection_defer_effects = False
        expected, _ = render_stages(scene, image, bounds, [modifier], QTransform())
        scene._modifier_render_cache.clear()
        scene._modifier_render_cache_bytes = 0
        scene._projection_defer_effects = True
        ready, calls = controlled_tiles(scene, monkeypatch)
        jobs = scene._effect_jobs
        jobs.retained_limit = 2
        jobs.retained_budget = int(image.sizeInBytes()) + 256 + 192 * 3 + 4096
        first_rect = QRectF(0, 0, 256, 65)
        first, provisional = regional(scene, image, bounds, modifier, target=first_rect)
        entry = next(iter(jobs._regions.values()))
        assert not provisional and first == expected.copy(first_rect.toAlignedRect())
        assert entry.bounds == bounds and entry.covered == {(0, 0)} and not entry.complete
        assert first.cacheKey() != entry.image.cacheKey()  # Detached exact crop.
        for index in range(6):
            pressure = QImage(1, 1, image.format())
            pressure.fill(0)
            jobs.retained_put(('pressure', index), ('pressure', index), pressure)
        discard_ordinary_tiles(jobs)
        middle_rect = QRectF(0, 0, 512, 65)
        with pytest.raises(ProjectionPending):
            regional(scene, image, bounds, modifier, target=middle_rect)
        ready.add(1)
        middle, provisional = regional(scene, image, bounds, modifier, target=middle_rect)
        assert not provisional and middle == expected.copy(middle_rect.toAlignedRect())
        assert not entry.complete and entry.covered == {(0, 0), (1, 0)}
        assert len(jobs._regions) == 1 and calls[0] == calls[1] == 1
        first.fill(0xffaa2233)
        assert middle == expected.copy(middle_rect.toAlignedRect())
        discard_ordinary_tiles(jobs)
        ready.add(2)
        final_rect = QRectF(200, 0, 313, 65)
        final, provisional = regional(scene, image, bounds, modifier, target=final_rect)
        assert not provisional and final == expected.copy(final_rect.toAlignedRect())
        assert entry.complete and len(jobs._regions) == 1 and sum(calls.values()) == 3
        assert jobs.retained_bytes <= jobs.retained_budget


@pytest.mark.parametrize('change', ['source', 'parameter', 'mask'])
def test_changed_dependency_cannot_reuse_cross_target_native_coverage(scene, monkeypatch, change):
    image, bounds, modifier = setup(scene)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0, 100)
    ready, calls = controlled_tiles(scene, monkeypatch)
    first_rect = QRectF(0, 0, 256, 65)
    regional(scene, image, bounds, modifier, target=first_rect)
    previous = next(iter(scene._effect_jobs._regions.values()))
    discard_ordinary_tiles(scene._effect_jobs)
    source_revision = 0
    if change == 'source':
        source_revision = 1
        image = QImage(image)
        image.fill(0xff449988)
    elif change == 'parameter':
        modifier.intensity = 21.
    else:
        mask.revision += 1
    ready.add(1)
    actual, provisional = regional(scene, image, bounds, modifier, source_revision,
                                  target=QRectF(0, 0, 512, 65))
    assert not provisional and not actual.isNull() and calls[0] == 2
    assert previous.covered == {(0, 0)} and not previous.complete


def test_checked_crop_derives_native_coverage_and_never_completes_partial_frame(scene):
    from comic_editor.ui.distort_regions import _assembly_context
    image, bounds, _ = setup(scene)
    store = scene._effect_jobs.private_regions(_assembly_context(scene), lambda: _assembly_context(scene))
    entry = store.begin(('partial',), bounds, image.format(), 3, tile_size=256)
    entry.image.fill(0xff449988)
    entry.covered.add((0, 0))
    with pytest.raises(ProjectionPending) as error:
        store.crop(('partial',), entry, bounds)
    assert error.value.scope == 'incomplete-frame-crop'
    exact = store.crop(('partial',), entry, QRectF(0, 0, 256, 65))
    assert not entry.complete and exact.size().width() == 256
    assert exact.cacheKey() != entry.image.cacheKey()
    exact.fill(0)
    assert entry.image.pixelColor(0, 0).alpha() == 255
    with pytest.raises(ValueError):
        store.crop(('partial',), entry, QRectF(-1, 0, 256, 65))
    with pytest.raises(ValueError):
        store.crop(('partial',), entry, QRectF(.5, 0, 255, 65))
    scene._effect_jobs.cancel()
    with pytest.raises(ProjectionPending):
        store.crop(('partial',), entry, QRectF(0, 0, 256, 65))


@pytest.mark.parametrize('limit', ['budget', 'pixel-cap'])
def test_full_frame_allocation_limit_keeps_existing_per_target_fallback(scene, monkeypatch, limit):
    image, bounds, modifier = setup(scene)
    ready, _ = controlled_tiles(scene, monkeypatch, ready={0, 1, 2})
    if limit == 'budget':
        scene._effect_jobs.retained_budget = 512 * 32 * 4 + 1024
    else:
        monkeypatch.setattr('comic_editor.ui.distort_rendering._MAX_PIXELS', 256 * 65)
    target = QRectF(20, 0, 300, 32)
    actual, provisional = regional(scene, image, bounds, modifier, target=target)
    entry = next(iter(scene._effect_jobs._regions.values()))
    assert not provisional and not actual.isNull()
    assert entry.bounds == target and entry.tile_size is None
    assert next(iter(scene._effect_jobs._regions))[0] == 'mesh-region-assembly'


def test_shared_native_crop_preserves_transparent_pixels_outside_complete_frame(scene, monkeypatch):
    image, bounds, modifier = setup(scene)
    controlled_tiles(scene, monkeypatch, ready={0, 1, 2})
    target = QRectF(-10, -5, 533, 75)
    actual, provisional = regional(scene, image, bounds, modifier, target=target)
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    scene._projection_defer_effects = False
    expected, _ = render_stages(scene, image, bounds, [modifier], QTransform())
    from comic_editor.ui.effect_pipeline import empty_image
    from PySide6.QtGui import QPainter
    padded = empty_image(target)
    painter = QPainter(padded)
    painter.drawImage(bounds.topLeft() - target.topLeft(), expected)
    painter.end()
    assert not provisional and actual == padded


@pytest.mark.parametrize('padded', [False, True])
def test_failed_crop_allocation_never_publishes_transparent_exact_pixels(scene, monkeypatch, padded):
    image, bounds, modifier = setup(scene)
    controlled_tiles(scene, monkeypatch)
    first = QRectF(0, 0, 256, 65)
    regional(scene, image, bounds, modifier, target=first)
    entry = next(iter(scene._effect_jobs._regions.values()))
    original = entry.image
    class AllocationFailure:
        def copy(self, rect):
            return QImage()
    entry.image = AllocationFailure()
    try:
        requested = QRectF(-10, -5, 266, 75) if padded else first
        with pytest.raises(MemoryError, match='exact frame crop'):
            regional(scene, image, bounds, modifier, target=requested)
    finally:
        entry.image = original
    assert not entry.complete
