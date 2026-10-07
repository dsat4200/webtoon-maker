"""Metadata Undo may retain deterministic drafts, never stale native artwork."""
from dataclasses import replace

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.document_patch import DocumentPatch
from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ImageObject, ParameterMaskBinding, ToneMask
from comic_editor.core.pixel_contract import FLOAT_PIXELS, LEGACY_PIXELS
from comic_editor.core.tiles import TileStore
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.pixels import pixel_scope
from comic_editor.ui import distort_pipeline, effect_pipeline, live_image_prefix_history
from test_live_image_prefix_reuse import clear_sources, scene
from test_navigator_patterns import canvas, source_image


@pytest.fixture(autouse=True)
def valid_chapter(canvas):
    # The navigator fixture deliberately uses an arbitrary-width render device;
    # metadata history additionally validates the actual chapter model.
    canvas.chapter.width = 1080
    page = canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
    page.bound = BoundGeometry.rectangle(0, 0, 1080, canvas.chapter.height)


def native_view(canvas, *, live=True):
    canvas._interactive_render = canvas._bounded_effect_preview = live
    canvas._effect_preview_channel = 'canvas'
    image = QImage(canvas.chapter.width, canvas.chapter.height,
                   canvas.chapter.pixel_contract.image_format)
    canvas.render_preview(image)
    return image


def observe_warp(monkeypatch):
    calls, original = [], distort_pipeline.render_distort_stage
    def observed(*args, **kwargs):
        calls.append(args[5].modifier_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', observed)
    return calls


def fresh_reference(canvas, monkeypatch):
    canvas._effect_jobs.cancel()
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    clear_sources(canvas)
    canvas._blur_pyramid_cache.clear()
    canvas._outline_distance_cache.clear()
    prepared = getattr(canvas, '_distort_preparation_cache', None)
    if prepared is not None:
        prepared.clear()
    monkeypatch.setattr(effect_pipeline, '_draft_image_prefix', lambda *_: False)
    return native_view(canvas)


def prefix_records(canvas):
    return {key: QImage(image) for key, image in canvas._modifier_render_cache.items()
            if key[:1] == ('live-effect-draft-stage',)}


def test_actual_binding_undo_reuses_upstream_pixels_and_matches_fresh_native_view(canvas, monkeypatch):
    obj, warp, color, mask = scene(canvas)
    canvas.tiles.paint_dab(mask.mask_id, QPointF(380, 310), 100, QColor('white'))
    calls = observe_warp(monkeypatch)
    before = native_view(canvas)
    old_chapter, old_object = canvas.chapter, obj
    state = canvas.chapter.to_dict()
    color.parameter_masks.clear()
    canvas.push_model_change(state, canvas.chapter.to_dict(), 'Remove parameter mask')
    changed = native_view(canvas)
    assert changed != before and calls == [warp.modifier_id]
    cached = prefix_records(canvas)
    assert cached
    canvas.command_stack.undo()
    assert canvas.chapter is old_chapter and canvas.chapter.objects[obj.object_id] is old_object
    assert not canvas._modifier_source_cache
    restored = native_view(canvas)
    assert restored == before and calls == [warp.modifier_id]
    assert any(key in canvas._modifier_render_cache
               and canvas._modifier_render_cache[key].cacheKey() == image.cacheKey()
               for key, image in cached.items())
    assert fresh_reference(canvas, monkeypatch) == restored
    assert calls == [warp.modifier_id, warp.modifier_id]


@pytest.mark.parametrize('change', ['source', 'geometry', 'parent', 'early_parameter',
                                  'early_mask', 'endpoint', 'mask_id', 'object_id'])
def test_history_restore_rechecks_current_pixel_dependencies(canvas, monkeypatch, change):
    obj, warp, _color, _mask = scene(canvas)
    upstream = ToneMask()
    canvas.chapter.masks[upstream.mask_id] = upstream
    warp.parameter_masks['intensity'] = ParameterMaskBinding(upstream.mask_id, 0, 100)
    canvas.tiles.paint_dab(upstream.mask_id, QPointF(400, 310), 150, QColor('white'))
    calls = observe_warp(monkeypatch)
    before = native_view(canvas)
    old = canvas.chapter.to_dict()
    if change == 'source':
        image = source_image()
        image.fill(QColor('green'))
        canvas.images.put_decoded(obj.object_id, 'replacement.png', b'', image)
    elif change == 'geometry':
        obj.x += 20
    elif change == 'parent':
        canvas.chapter.layers[obj.parent_layer_id].translate_x += 20
    elif change == 'early_parameter':
        warp.parameters['angle'] = -60
    elif change == 'early_mask':
        canvas.tiles.paint_dab(upstream.mask_id, QPointF(400, 310), 150, QColor('black'))
    elif change == 'endpoint':
        warp.parameter_masks['intensity'].white_value = 20
    elif change == 'mask_id':
        replacement = ToneMask()
        canvas.chapter.masks[replacement.mask_id] = replacement
        warp.parameter_masks['intensity'].mask_id = replacement.mask_id
    else:
        replacement = canvas.chapter.add_object(obj.parent_layer_id,
            ImageObject(x=obj.x, y=obj.y, pixel_width=obj.pixel_width,
                        pixel_height=obj.pixel_height, modifier_ids=list(obj.modifier_ids)))
        canvas.images.copy_source_to(obj.object_id, canvas.images, replacement.object_id)
        canvas.chapter.delete_entity('object', obj.object_id)
    new = canvas.chapter.to_dict()
    patch = DocumentPatch.pair(old, new)[1]
    canvas._restore_history_state(patch, document_patch=True)
    current = native_view(canvas)
    assert (current == before) if change == 'object_id' else (current != before)
    assert len(calls) == 2, 'A changed upstream input reused old draft pixels'
    assert fresh_reference(canvas, monkeypatch) == current
    assert len(calls) == 3


@pytest.mark.parametrize('contract', [LEGACY_PIXELS, replace(FLOAT_PIXELS, precision='float16'), FLOAT_PIXELS],
                         ids=['rgba8', 'float16', 'float32'])
@pytest.mark.parametrize('shape', ['bound', 'compound'])
def test_fit_parent_subpixel_shape_edit_recomputes_draft_with_unchanged_capture_frame(
        canvas, monkeypatch, contract, shape):
    obj, warp, _color, _mask = scene(canvas)
    if shape == 'bound':
        parent = canvas.chapter.layers[obj.parent_layer_id]
        edited = parent
        parent.bound = BoundGeometry.rectangle(100.1, 200.1, 639.6, 239.6)
    else:
        parent = canvas.chapter.add_layer(obj.parent_layer_id, 'Compound',
            BoundGeometry.rectangle(200.1, 200.1, 539.6, 239.6))
        parent.fill_color, parent.border_width, parent.compound_enabled = None, 0, True
        edited = canvas.chapter.add_layer(parent.layer_id, 'Left operand',
            BoundGeometry.rectangle(100.1, 200.1, 100.0, 239.6))
        edited.fill_color, edited.border_width = None, 0
        canvas.chapter.move_entity('object', obj.object_id, parent.layer_id, 0)
    obj.placement_mode, obj.fit_mode = 'fit_parent', 'stretch'
    canvas.chapter.pixel_contract = contract
    inputs, keys, original = [], [], distort_pipeline.render_distort_stage
    def observed(*args, **kwargs):
        inputs.append(bytes(args[1].constBits()))
        keys.append(args[8])
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', observed)
    original_source = canvas.images.native_image(obj.object_id)
    source_bytes = bytes(original_source.constBits())
    with pixel_scope(contract):
        # Actual float documents convert their original once synchronously;
        # this proof concerns fitted-source identity, not worker readiness.
        native_view(canvas, live=False)
        inputs.clear()
        keys.clear()
        before = native_view(canvas)
        bounds = canvas.object_world_rect(obj.object_id).toAlignedRect()
        assert not prefix_records(canvas)
        edited.bound = BoundGeometry.rectangle(100.7, 200.1,
            639.0 if shape == 'bound' else 99.4, 239.6)
        if shape == 'compound':
            # Compound path compilation follows the editor's normal metadata
            # notification; bare bounds do not need that compiled-path reset.
            canvas.documentChanged.emit(None)
        assert canvas.object_world_rect(obj.object_id).toAlignedRect() == bounds
        clear_sources(canvas)
        current = native_view(canvas)
        assert current != before and len(inputs) == 2
        assert inputs[0] != inputs[1], 'The fitted source itself must change in this native oracle'
        assert keys[0] != keys[1], 'Different fitted source pixels require different ordinary stage keys'
        assert not prefix_records(canvas)
        assert fresh_reference(canvas, monkeypatch) == current
    assert current.format() == contract.image_format
    assert bytes(canvas.images.native_image(obj.object_id).constBits()) == source_bytes


@pytest.mark.parametrize('change', ['chapter', 'images', 'tiles', 'contract', 'size', 'nested_history'])
def test_synchronous_restore_callbacks_cannot_reintroduce_an_old_context(canvas, change):
    scene(canvas)
    native_view(canvas)
    assert prefix_records(canvas)
    state = canvas.chapter.to_dict()
    patch = DocumentPatch.pair(state, state)[1]
    def callback(*_):
        if change == 'chapter':
            from comic_editor.core.models import ChapterDocument
            canvas.chapter = ChapterDocument.from_dict(state)
        elif change == 'images':
            canvas.images = ImageStore()
        elif change == 'tiles':
            canvas.tiles = TileStore()
        elif change == 'contract':
            canvas.chapter.pixel_contract = FLOAT_PIXELS
        elif change == 'size':
            canvas.chapter.width += 1
        else:
            canvas._history_generation += 1
    canvas.chapterReplaced.connect(callback)
    canvas._restore_history_state(patch, document_patch=True)
    assert not prefix_records(canvas)


def test_full_chapter_replacement_keeps_the_original_clear_behavior(canvas):
    scene(canvas)
    native_view(canvas)
    assert prefix_records(canvas)
    canvas._restore_history_state(canvas.chapter.to_dict())
    assert not canvas._modifier_render_cache and not canvas._modifier_source_cache


def test_history_retained_drafts_cannot_feed_exact_pixels_or_disk_entries(canvas, tmp_path):
    scene(canvas)
    canvas._projection_exact = True
    exact = native_view(canvas, live=False)
    canvas._projection_exact = False
    native_view(canvas)
    drafts = prefix_records(canvas)
    assert drafts
    # A deliberately poisoned transient value makes accidental exact adoption
    # observable independently of the draft renderer's own pixel calculation.
    for key, image in drafts.items():
        poisoned = image.copy()
        poisoned.fill(QColor('black'))
        canvas._modifier_render_cache[key] = poisoned
    state = canvas.chapter.to_dict()
    patch = DocumentPatch.pair(state, state)[1]
    backing = canvas._persistent_render_cache = PersistentRenderCache(tmp_path)
    canvas._interactive_render = canvas._bounded_effect_preview = False
    canvas._projection_exact = True
    try:
        with backing.record():
            canvas._restore_history_state(patch, document_patch=True)
        assert prefix_records(canvas)
        backing.drain()
        assert all(backing.lookup('effect', key, wait=True) is None for key in drafts)
        assert native_view(canvas, live=False) == exact
    finally:
        backing.close()
        canvas._persistent_render_cache = None
        canvas._projection_exact = False


@pytest.mark.parametrize('policy', ['actual_float', 'scoped_float', 'different_float_space'])
def test_float_policy_is_never_retained_by_legacy_history_optimization(canvas, policy):
    scene(canvas)
    if policy == 'actual_float':
        native_view(canvas)
        canvas.chapter.pixel_contract = FLOAT_PIXELS
    else:
        contract = FLOAT_PIXELS if policy == 'scoped_float' else replace(FLOAT_PIXELS, working_space='linear_srgb')
        with pixel_scope(contract):
            native_view(canvas)
    assert prefix_records(canvas)
    saved = live_image_prefix_history.snapshot(canvas)
    assert saved is None or not saved[2]


@pytest.mark.parametrize('ceiling', ['records', 'quarter', 'eight_mib'])
def test_history_retention_uses_only_newest_bounded_records_and_owned_cow_handles(canvas, ceiling):
    scene(canvas)
    native_view(canvas)
    template = next(iter(prefix_records(canvas)))
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    dimension = 512 if ceiling == 'eight_mib' else 4
    image = QImage(dimension, dimension, LEGACY_PIXELS.image_format)
    image.fill(QColor('red'))
    canvas._modifier_render_cache_budget = (64 * 1024 * 1024 if ceiling == 'eight_mib'
                                           else 32 * 1024 if ceiling == 'records' else 16 * image.sizeInBytes())
    amount = 70 if ceiling == 'records' else 9
    keys = []
    for index in range(amount):
        key = (*template[:2], (*template[2], ('history-retention-budget', index)))
        canvas._modifier_cache_put(key, image)
        keys.append(key)
    saved = live_image_prefix_history.snapshot(canvas)
    maximum = 64 if ceiling == 'records' else 8 if ceiling == 'eight_mib' else 4
    assert len(saved[2]) == maximum
    assert [key for key, _ in saved[2]] == list(reversed(keys[-maximum:]))
    # Mutating the cache's wrapper detaches from the independently owned saved handle.
    newest, saved_image = saved[2][0]
    original_bytes = bytes(saved_image.constBits())
    canvas._modifier_render_cache[newest].fill(QColor('blue'))
    assert bytes(saved_image.constBits()) == original_bytes
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._history_generation = saved[1]
    live_image_prefix_history.restore(canvas, saved)
    assert list(canvas._modifier_render_cache) == keys[-maximum:]
    assert canvas._modifier_render_cache_bytes == maximum * image.sizeInBytes()
    assert canvas._modifier_render_cache_bytes <= min(8 * 1024 * 1024,
                                                     canvas._modifier_render_cache_budget // 4)
    assert bytes(canvas._modifier_cache_get(newest).constBits()) == original_bytes


@pytest.mark.parametrize('callback', ['budget', 'newer_result'])
def test_retention_rechecks_callback_budget_and_preserves_newer_same_key_storage(canvas, callback):
    scene(canvas)
    native_view(canvas)
    saved = live_image_prefix_history.snapshot(canvas)
    assert saved[2]
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._history_generation = saved[1]
    if callback == 'budget':
        canvas._modifier_render_cache_budget = 0
    else:
        key, image = saved[2][0]
        newer = image.copy()
        newer.fill(QColor('blue'))
        canvas._modifier_cache_put(key, newer)
    live_image_prefix_history.restore(canvas, saved)
    if callback == 'budget':
        assert not canvas._modifier_render_cache
    else:
        assert canvas._modifier_cache_get(key).cacheKey() == newer.cacheKey()
