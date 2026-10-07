"""Current draft silhouettes reuse the existing canvas distance cache safely."""
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, OutlineModifier, ParameterMaskBinding,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.pixels import (
    FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope, working_image,
)
from comic_editor.ui import interactive_effects
from comic_editor.ui.cache_dependencies import cache_put, exact_cache_allowed
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_rendering import apply_modifier_stack


CONTRACTS = [LEGACY_PIXELS, replace(FLOAT_PIXELS, precision='float16'), FLOAT_PIXELS]
ORIGIN = (19.375, -11.625)


@pytest.fixture
def canvas(qapp):
    scene = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    chapter = ChapterDocument(width=1080, height=400)
    chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 1080, 400))
    scene.set_document(chapter, TileStore())
    scene._interactive_render = True
    scene._projection_exact = False
    scene._effect_preview_channel = 'canvas'
    yield scene
    scene._effect_jobs.cancel()
    scene._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    scene.close()
    scene.deleteLater()


def values(contract):
    yy, xx = np.mgrid[:211, :399]
    alpha = np.where(((xx-151)/93)**2+((yy-102)/64)**2 < 1, .73, 0.).astype(np.float32)
    alpha[80:121, 150:186] = 0
    alpha[48:55, 110:165] = 1/65535
    rgb = np.stack((np.full_like(alpha, .4), xx/399, yy/211), axis=-1)*alpha[..., None]
    if contract.floating:
        rgb[..., 0] = 1.27*alpha
        rgb[..., 1] = -.041*alpha
    return np.concatenate((rgb, alpha[..., None]), axis=-1).astype(np.float32)


def native(image):
    return image.size(), image.format(), image.bytesPerLine(), bytes(image.constBits())


def uncached_draft(image, effect, fields):
    draft = interactive_effects._draft(image, [effect], ORIGIN, fields, QTransform(), False)
    return draft.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)


def live(canvas, image, effect, fields=None, *, tag='same-current-source'):
    return interactive_effects.render_interactive_stack(
        canvas, image, [effect], ORIGIN, fields or {},
        cache_key=('interactive-stack', tag), scope=('object', 'outline-proof', 'canvas'),
        world_to_image=QTransform(), upstream_provisional=True)


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
def test_new_qt_capture_of_same_pixels_reuses_actual_canvas_distance_cache(canvas, contract):
    canvas.chapter.pixel_contract = contract
    cache = canvas._outline_distance_cache
    budget = cache.budget
    source = working_image(values(contract), contract)
    effect = OutlineModifier(thickness=7.125)
    original_source = native(source)
    with pixel_scope(contract):
        expected = native(uncached_draft(source, effect, {}))
        initial = cache.computations
        actual, provisional = live(canvas, source, effect)
        assert provisional and native(actual) == expected
        assert cache.computations == initial+1
        # A new Qt capture makes the whole-draft key miss. Reusing only the
        # current silhouette must still avoid another distance transform.
        recaptured = source.copy()
        assert recaptured.cacheKey() != source.cacheKey()
        repeated, provisional = live(canvas, recaptured, effect)
        assert provisional and native(repeated) == expected
        assert cache.computations == initial+1
    assert native(source) == original_source
    assert canvas._outline_distance_cache is cache and cache.budget == budget
    assert 0 < cache.bytes <= budget
    assert canvas._effect_jobs.submitted == 0 and not canvas._effect_jobs.retained


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
@pytest.mark.parametrize('change', ['alpha', 'bounds', 'thickness', 'mask', 'mask_endpoints', 'brush'])
def test_live_cache_handoff_uses_current_source_mask_and_parameters(canvas, contract, change):
    canvas.chapter.pixel_contract = contract
    cache = canvas._outline_distance_cache
    source_values = values(contract)
    source = working_image(source_values, contract)
    effect = OutlineModifier(thickness=7.125)
    fields = {}
    if change.startswith('mask'):
        effect.parameter_masks['opacity'] = ParameterMaskBinding('paint-proof', 17, 81)
        yy, xx = np.mgrid[:source.height(), :source.width()]
        fields[(effect.modifier_id, 'opacity')] = ((xx+2*yy)%193/192).astype(np.float32)
    source_before = native(source)
    with pixel_scope(contract):
        original, provisional = live(canvas, source, effect, fields)
        assert provisional
        before = cache.computations
        changed = source.copy()
        if change == 'alpha':
            source_values[13:71, 310:371] = [.31, .12, .017, .51]
            changed = working_image(source_values, contract)
        elif change == 'bounds':
            changed = source.copy(7, 11, source.width()-23, source.height()-19)
        elif change == 'thickness':
            effect.thickness = 21.25
        elif change == 'mask':
            fields[(effect.modifier_id, 'opacity')] = 1-fields[(effect.modifier_id, 'opacity')]
        elif change == 'mask_endpoints':
            effect.parameter_masks['opacity'].black_value = 57.25
        elif change == 'brush':
            effect.style = 'brush'
        effect_before = deepcopy(effect.to_dict())
        fields_before = {key: value.copy() for key, value in fields.items()}
        expected = native(uncached_draft(changed, effect, fields))
        actual, provisional = live(canvas, changed, effect, fields, tag=change)
        assert provisional and native(actual) == expected
        assert native(actual) != native(original), 'Mutation fixture must change pixels'
        # New coverage or dimensions must recompute. Changing opacity fields
        # leaves the source silhouette unchanged and safely reuses its field.
        if change in {'alpha', 'bounds'}:
            assert cache.computations > before
        elif change in {'thickness', 'mask', 'mask_endpoints'}:
            assert cache.computations == before
        assert effect.to_dict() == effect_before
        for key, value in fields.items(): np.testing.assert_array_equal(value, fields_before[key])
    assert native(source) == source_before
    assert canvas._effect_jobs.submitted == 0


@pytest.mark.parametrize('contract', CONTRACTS, ids=['rgba8', 'float16', 'float32'])
def test_draft_distance_reuse_never_poison_exact_or_durable_artwork(canvas, tmp_path, contract):
    canvas.chapter.pixel_contract = contract
    source = working_image(values(contract), contract)
    effect = OutlineModifier(thickness=7.125)
    disk = canvas._persistent_render_cache = PersistentRenderCache(tmp_path)
    try:
        with pixel_scope(contract), disk.record():
            draft, provisional = live(canvas, source, effect)
            assert provisional
            live(canvas, source.copy(), effect)
            drafts = list(canvas._modifier_render_cache)
            assert drafts and all(key[0] == 'interactive-draft' for key in drafts)
            assert not canvas._effect_jobs.retained
            canvas._projection_exact = True
            for key in drafts:
                assert not exact_cache_allowed(canvas, key)
                cache_put(canvas, 'effect', key, canvas._modifier_render_cache[key])
        disk.drain()
        assert not disk.entries and not disk.writes
        canvas._persistent_render_cache = None
        # Completed draft aliases must not satisfy the native request even
        # after an arbitrary overwritten draft payload is left in the LRU.
        for key in drafts:
            poison = QImage(canvas._modifier_render_cache[key])
            poison.fill(Qt.black)
            canvas._modifier_render_cache[key] = poison
        with pixel_scope(contract):
            expected = apply_modifier_stack(source, [deepcopy(effect)], ORIGIN)
            exact, provisional = interactive_effects.render_interactive_stack(
                canvas, source, [effect], ORIGIN,
                cache_key=('interactive-stack', 'same-current-source'),
                scope=('object', 'outline-proof', 'canvas'), world_to_image=QTransform())
            assert not provisional and native(exact) == native(expected)
            assert native(exact) != native(draft)
    finally:
        disk.close()
        canvas._persistent_render_cache = None
