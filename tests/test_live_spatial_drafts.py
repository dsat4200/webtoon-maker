"""Live compact preparation stays separate from native and durable results."""
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.models import (DistortModifier, ImageObject, ParameterMaskBinding,
    PosterizeModifier, PosterizeValueModifier, RadialBlurModifier, ToneMask)
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.ui.cache_dependencies import cache_put, exact_cache_allowed
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.thumbnail_effects import capture_scale, live_effect_draft, scaled_modifiers
from test_navigator_patterns import canvas, source_image


@pytest.mark.parametrize('field,value', [
    ('_interactive_render', False), ('_projection_exact', True), ('_bounded_effect_preview', False),
    ('_effect_preview_channel', 'export'), ('_effect_preview_channel', 'navigator'),
    ('_render_base_alpha', True), ('_rendering_mask_contributor', 1),
    ('_rendering_halftone_source', True), ('_render_cage_source', True),
    ('_tiling_capture_geometry', object()),
])
def test_compact_live_scope_excludes_exact_and_special_sources(canvas, field, value):
    canvas._interactive_render = canvas._bounded_effect_preview = True
    canvas._projection_exact = False
    assert live_effect_draft(canvas)
    assert capture_scale(canvas, QRectF(0, 0, 640, 240), [DistortModifier()]) < 1
    setattr(canvas, field, value)
    assert not live_effect_draft(canvas)
    if value != 'navigator':
        assert capture_scale(canvas, QRectF(0, 0, 640, 240), [DistortModifier()]) == 1


def test_compact_radial_draft_computes_current_blur_instead_of_unblurred_base(canvas, monkeypatch):
    from comic_editor.ui import radial_blur
    source, bounds = source_image().scaled(160, 60), QRectF(0, 0, 160, 60)
    modifier = RadialBlurModifier(center=(80, 30), angle=35)
    canvas.chapter.modifiers[modifier.modifier_id] = modifier
    canvas._interactive_render = canvas._bounded_effect_preview = True
    calls, original = [], radial_blur.radial_blur
    def observed(pixels, *args, **kwargs):
        calls.append(pixels.shape)
        return original(pixels, *args, **kwargs)
    monkeypatch.setattr(radial_blur, 'radial_blur', observed)
    result, output = render_stages(canvas, source, bounds, [modifier], QTransform(),
        source_key=('live-effect-draft-source', .25, ('raw',)), provisional=True,
        request_scope=('object', 'radial', 'canvas'))
    assert calls and max(shape[0] * shape[1] for shape in calls) <= 32768
    assert result != source and not output.isEmpty()
    assert canvas._effect_jobs.submitted == 0


def test_live_draft_source_and_mask_keys_cannot_become_durable_exact_entries(canvas, tmp_path):
    from comic_editor.core.models import ImageObject
    from test_navigator_patterns import preview
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        ImageObject(x=100, y=200, pixel_width=640, pixel_height=240))
    canvas.images.put_decoded(obj.object_id, 'colors.png', b'', source_image())
    warp = DistortModifier(modifier_type='distort_twirl', frame=(100, 200, 640, 240),
        center=(420, 320), radius=200, parameters={'angle': 30})
    warp.validate()
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    obj.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    canvas.chapter.add_modifier(warp, [('object', obj.object_id)])
    cache = canvas._persistent_render_cache = PersistentRenderCache(tmp_path)
    try:
        with cache.record():
            preview(canvas, False, live=True)
            draft_sources = [key for key in canvas._modifier_source_cache
                             if key[:1] == ('live-effect-draft-source',)]
            draft_effects = [key for key in canvas._modifier_render_cache if 'live-effect-draft' in repr(key)]
            assert draft_sources and draft_effects
            assert any(key[:1] == ('live-effect-draft-opacity-mask',) for key in draft_effects)
            canvas._projection_exact = True
            canvas._bounded_effect_preview = False
            for pool, kind, keys in ((canvas._modifier_source_cache, 'source', draft_sources),
                                     (canvas._modifier_render_cache, 'effect', draft_effects)):
                for key in keys:
                    assert not exact_cache_allowed(canvas, key)
                    cache_put(canvas, kind, key, pool[key])
        cache.drain()
        assert not cache.entries and not cache.writes
    finally:
        cache.close()
        canvas._persistent_render_cache = None


@pytest.mark.parametrize('posterize_class', [PosterizeModifier, PosterizeValueModifier])
def test_masked_posterize_after_spatial_draft_keeps_scaled_preparation_and_native_pixels(canvas, monkeypatch, posterize_class):
    from test_navigator_patterns import preview
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        ImageObject(x=100, y=200, pixel_width=640, pixel_height=240))
    canvas.images.put_decoded(obj.object_id, 'colors.png', b'', source_image())
    warp = DistortModifier(modifier_type='distort_twirl', frame=(100, 200, 640, 240),
        center=(420, 320), radius=200, parameters={'angle': 30})
    warp.validate()
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    posterize = posterize_class(simplify_enabled=True, simplify_radius=7)
    posterize.parameter_masks['intensity'] = ParameterMaskBinding(mask.mask_id, 10, 90)
    canvas.chapter.add_modifier(warp, [('object', obj.object_id)])
    canvas.chapter.add_modifier(posterize, [('object', obj.object_id)])
    expected, model = preview(canvas, False), canvas.chapter.to_dict()
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    fields, original = [], canvas._modifier_mask_fields
    def observed(modifiers, width, height, *args, **kwargs):
        fields.append(width * height)
        return original(modifiers, width, height, *args, **kwargs)
    monkeypatch.setattr(canvas, '_modifier_mask_fields', observed)
    assert not preview(canvas, False, live=True).isNull()
    assert fields and max(fields) < 100000
    assert any(key[:1] == ('live-effect-draft-source',) for key in canvas._modifier_source_cache)
    assert canvas.chapter.to_dict() == model and canvas._effect_jobs.submitted == 0
    assert preview(canvas, False) == expected
    scaled = scaled_modifiers([posterize], .25)[0]
    assert scaled.simplify_radius == 1.75
    assert scaled.parameter_masks['intensity'] == posterize.parameter_masks['intensity']


def test_statistics_source_key_records_actual_isolated_contents(canvas):
    from comic_editor.ui.scene_render_backend import source_capture_key
    key = ('raw-source', 'same-model')
    assert source_capture_key(canvas, key) == key
    canvas._posterize_statistics_capture = True
    assert source_capture_key(canvas, key) == key
    canvas._effect_preview_channel = 'posterize-statistics'
    ordinary = source_capture_key(canvas, key)
    canvas._rendering_compound_references = True
    compound = source_capture_key(canvas, key)
    canvas._rendering_outward_gradient = True
    outward = source_capture_key(canvas, key)
    assert len({ordinary, compound, outward, key}) == 4
    assert source_capture_key(canvas, None) is None


def test_live_mask_does_not_adopt_unsupported_parent_output_as_translation_alias(canvas, monkeypatch):
    from comic_editor.core.models import BlurModifier, OutlineModifier
    from comic_editor.ui import translation_cache
    from comic_editor.ui.viewport_masking import mask_output
    from PySide6.QtGui import QPainter
    from comic_editor.core.models import BoundGeometry
    parent = canvas.chapter.add_layer(canvas.chapter.root_page_ids[0], 'Parent',
        BoundGeometry.rectangle(0, 0, 1200, 900))
    child = canvas.chapter.add_object(parent.layer_id, ImageObject(pixel_width=640, pixel_height=240))
    canvas.images.put_decoded(child.object_id, 'colors.png', b'', source_image())
    canvas.chapter.add_modifier(OutlineModifier(thickness=5), [('object', child.object_id)])
    canvas.chapter.add_modifier(BlurModifier(mode='focal'), [('layer', parent.layer_id)])
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    binding = ParameterMaskBinding(mask.mask_id, 0, 1)
    canvas._interactive_render = canvas._bounded_effect_preview = True
    # The parent keeps its native source frame, but a supported child's live
    # result is still provisional. Its masked output cannot become an alias.
    assert capture_scale(canvas, QRectF(0, 0, 1200, 900),
                         canvas._active_modifier_instances(parent.modifier_ids)) == 1
    monkeypatch.setattr(translation_cache, 'output_key', lambda *_a, **_k:
                        pytest.fail('live masked output requested an exact translation alias'))
    source = source_image().scaled(160, 60)
    target = QImage(300, 225, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(target)
    try:
        processed, _bounds = mask_output(canvas, source, QRectF(0, 0, 640, 240), QTransform(),
            binding, QRectF(0, 0, 640, 240), painter, target=parent,
            source_key=('native-unsupported-parent',))
        assert processed is not None
    finally:
        painter.end()
    assert any(key[:1] == ('live-effect-draft-opacity-mask',) for key in canvas._modifier_render_cache)


def test_isolated_compound_child_source_cannot_replace_ordinary_layer_pixels(canvas, monkeypatch):
    from comic_editor.core.models import BoundGeometry, OutlineModifier
    from test_navigator_patterns import preview
    parent = canvas.chapter.add_layer(canvas.chapter.root_page_ids[0], 'Compound',
        BoundGeometry.rectangle(0, 0, 900, 600))
    parent.fill_color, parent.border_width, parent.compound_enabled = None, 0, True
    obj = canvas.chapter.add_object(parent.layer_id,
        ImageObject(x=100, y=200, pixel_width=640, pixel_height=240, geometry_reference='compound'))
    canvas.images.put_decoded(obj.object_id, 'colors.png', b'', source_image())
    canvas.chapter.add_modifier(OutlineModifier(thickness=5), [('layer', parent.layer_id)])
    original = canvas._render_layer
    def contents(painter, layer, opacity, visible):
        if ('layer', parent.layer_id) in canvas._render_modifier_sources:
            # The isolation flags can include otherwise excluded references.
            # Model that content difference without changing any saved record;
            # exercise the real source/processed cache builders around it.
            painter.fillRect(QRectF(0, 0, 900, 600),
                QColor('red' if canvas._posterize_statistics_capture else 'blue'))
        else:
            original(painter, layer, opacity, visible)
    monkeypatch.setattr(canvas, '_render_layer', contents)
    canvas._posterize_statistics_capture = False
    normal = preview(canvas, False)
    canvas._effect_preview_channel = 'posterize-statistics'
    canvas._posterize_statistics_capture = canvas._rendering_compound_references = True
    isolated = QImage(300, 225, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(isolated)
    assert isolated != normal
    assert any(key[:1] == ('posterize-statistics-source',) for key in canvas._modifier_source_cache)
    canvas._posterize_statistics_capture = canvas._rendering_compound_references = False
    assert preview(canvas, False) == normal
