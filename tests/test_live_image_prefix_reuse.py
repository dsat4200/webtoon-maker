"""Current compact image prefixes survive Qt handles without becoming exact."""
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    DistortModifier, HueSaturationLightnessModifier, ImageObject,
    ParameterMaskBinding, ToneMask,
)
from comic_editor.ui import distort_pipeline, effect_pipeline
from test_navigator_patterns import canvas, preview, source_image


def scene(canvas):
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        ImageObject(x=100, y=200, pixel_width=640, pixel_height=240))
    canvas.images.put_decoded(obj.object_id, 'colors.png', b'', source_image())
    warp = DistortModifier(modifier_type='distort_twirl', frame=(100, 200, 640, 240),
        center=(420, 320), radius=200, parameters={'angle': 30})
    warp.validate()
    color = HueSaturationLightnessModifier(hue=90)
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    color.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0, 100)
    for modifier in (warp, color):
        canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    return obj, warp, color, mask


def clear_sources(canvas):
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0


def test_late_mask_paint_reuses_current_prefix_and_updates_pixels(canvas, monkeypatch):
    obj, warp, color, mask = scene(canvas)
    calls, original = [], distort_pipeline.render_distort_stage
    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', observed)
    before = preview(canvas, False, live=True)
    assert len(calls) == 1
    # Force a new Qt source handle, retaining identical original semantic content.
    clear_sources(canvas)
    canvas.tiles.paint_dab(mask.mask_id, QPointF(380, 310), 100, QColor('white'))
    after = preview(canvas, False, live=True)
    assert len(calls) == 1 and after != before
    monkeypatch.setattr(effect_pipeline, '_draft_image_prefix', lambda *_: False)
    assert preview(canvas, False, live=True) == after
    assert len(calls) == 2
    exact = preview(canvas, False)
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    clear_sources(canvas)
    assert preview(canvas, False) == exact


@pytest.mark.parametrize('change', ['source', 'geometry', 'parent', 'early_parameter',
                                  'early_mask', 'endpoint'])
def test_changed_prefix_dependencies_recompute_current_draft(canvas, monkeypatch, change):
    obj, warp, color, mask = scene(canvas)
    upstream_mask = ToneMask()
    canvas.chapter.masks[upstream_mask.mask_id] = upstream_mask
    warp.parameter_masks['intensity'] = ParameterMaskBinding(upstream_mask.mask_id, 0, 100)
    canvas.tiles.paint_dab(upstream_mask.mask_id, QPointF(400, 310), 150, QColor('white'))
    calls, original = [], distort_pipeline.render_distort_stage
    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', observed)
    preview(canvas, False, live=True)
    assert len(calls) == 1
    # The source/rig invalidation proof also survives the exact-worker pressure
    # which previously displaced unchanged compact prefixes after mask painting.
    canvas._modifier_render_cache_budget = canvas._modifier_render_cache_bytes
    pressure = QImage(1024, 512, QImage.Format_ARGB32_Premultiplied)
    pressure.fill(QColor('green'))
    assert pressure.sizeInBytes() > canvas._modifier_render_cache_budget
    jobs = canvas._effect_jobs
    jobs.request(('native-pressure',), ('native-pressure-output',),
        lambda _: QImage(pressure), pressure.sizeInBytes(), require_exact=True)
    jobs.running[3].result(timeout=5)
    jobs.poll()
    # Sub-budget exact regions also must not hide a semantic dependency edit.
    prefix_bytes = sum(image.sizeInBytes() for key, image in canvas._modifier_render_cache.items()
                       if key[:1] == ('live-effect-draft-stage',))
    canvas._modifier_render_cache_budget = 4 * prefix_bytes
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor('green'))
    assert tile.sizeInBytes() < canvas._modifier_render_cache_budget
    for index in range(16):
        canvas._modifier_cache_put(('effect-tile', 'dependency-pressure', index), tile)
    if change == 'source':
        image = source_image()
        image.fill(QColor('green'))
        canvas.images.put_decoded(obj.object_id, 'replacement.png', b'', image)
    elif change == 'geometry':
        obj.x += 10
    elif change == 'parent':
        canvas.chapter.layers[obj.parent_layer_id].translate_x += 12
    elif change == 'early_parameter':
        warp.parameters['angle'] = -20
    elif change == 'early_mask':
        canvas.tiles.paint_dab(upstream_mask.mask_id, QPointF(400, 310), 150, QColor('black'))
    else:
        warp.parameter_masks['intensity'].black_value = 50
    clear_sources(canvas)
    actual = preview(canvas, False, live=True)
    assert len(calls) == 2
    monkeypatch.setattr(effect_pipeline, '_draft_image_prefix', lambda *_: False)
    assert preview(canvas, False, live=True) == actual


def test_derived_mask_never_reuses_model_stable_prefix(canvas, monkeypatch):
    obj, warp, _color, mask = scene(canvas)
    warp.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 0, 100)
    contributor = canvas.chapter.add_object(obj.parent_layer_id,
        ImageObject(x=100, y=200, pixel_width=640, pixel_height=240))
    canvas.images.put_decoded(contributor.object_id, 'mask.png', b'', source_image())
    mask.contributors.append(('object', contributor.object_id))
    calls, original = [], distort_pipeline.render_distort_stage
    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', observed)
    preview(canvas, False, live=True)
    preview(canvas, False, live=True)
    assert len(calls) == 2
    assert not any(key[:1] == ('live-effect-draft-stage',)
                   for key in canvas._modifier_render_cache)


@pytest.mark.parametrize('same_format', [False, True])
def test_precision_change_cannot_reuse_previous_draft_pixels(canvas, monkeypatch, same_format):
    from dataclasses import replace
    from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope
    scene(canvas)
    calls, original = [], distort_pipeline.render_distort_stage
    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, 'render_distort_stage', observed)
    first = FLOAT_PIXELS if same_format else LEGACY_PIXELS
    second = replace(FLOAT_PIXELS, working_space='linear_srgb') if same_format else FLOAT_PIXELS
    with pixel_scope(first):
        preview(canvas, False, live=True)
        prefix_bytes = sum(image.sizeInBytes() for key, image in canvas._modifier_render_cache.items()
                           if key[:1] == ('live-effect-draft-stage',))
        canvas._modifier_render_cache_budget = 4 * prefix_bytes
        tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        tile.fill(QColor('green'))
        for index in range(16):
            canvas._modifier_cache_put(('effect-tile', 'contract-pressure', index), tile)
    with pixel_scope(second):
        actual = preview(canvas, False, live=True)
        assert len(calls) == 2
        monkeypatch.setattr(effect_pipeline, '_draft_image_prefix', lambda *_: False)
        assert preview(canvas, False, live=True) == actual


@pytest.mark.parametrize('source', [('mirror-source', 'layer', 'parent'),
                                   ('original-without-proof',), ('raw',)])
def test_unknown_and_derived_sources_cannot_use_image_draft_memo(canvas, source):
    _obj, warp, _color, _mask = scene(canvas)
    canvas._interactive_render = canvas._bounded_effect_preview = True
    assert not effect_pipeline._draft_image_prefix(canvas,
        ('live-effect-draft-source', .25, source), [warp])
