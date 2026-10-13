"""Canceled exact regions stop between native stages without publishing a frame."""
from threading import Event

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import (
    BlurModifier, BrightnessContrastModifier, HueSaturationLightnessModifier, RasterObject,
)
from comic_editor.render.admission import WorkCancelled
from comic_editor.render.blur_regions import RegionalBlur
from comic_editor.render.modifier_rendering import apply_modifier_stack
from comic_editor.render.scheduler import _scene_cancellation
from comic_editor.render.tile_effects import tile_output
from test_effect_regions import scene, image, crop, register
from test_tile_graph_effects import enable


def arguments():
    return dict(required=QRectF(0, 0, 490, 300),
                source_identity=('original-native-source',),
                request_scope=('object', 'cancelled-native-region', 'canvas'))


def native_bytes(value):
    return (value.format(), value.size(), value.bytesPerLine(),
            bytes(value.colorSpace().iccProfile()), bytes(value.constBits()))


def test_cancel_during_first_actual_blur_tile_stops_later_tiles_and_complete_output(
        scene, monkeypatch):
    source, bounds = image(), QRectF(0, 0, 530, 403)
    original_pixels = native_bytes(source)
    modifier = BlurModifier(strength=7)
    register(scene, [modifier])
    enable(scene)
    token, calls = Event(), []
    original = RegionalBlur.apply
    def blur(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        calls.append(args[0])
        token.set()
        return result
    monkeypatch.setattr(RegionalBlur, 'apply', blur)
    with _scene_cancellation(scene, token.is_set), pytest.raises(WorkCancelled):
        tile_output(scene, source, bounds, [modifier], QTransform(), **arguments())
    assert len(calls) == 1
    assert not any(key[0] == 'tile-output' for key in scene._modifier_render_cache)
    # Native source/pyramid neighbors completed before cancellation retain
    # their own exact keys; the canceled blur output itself was never admitted.
    assert not any(key[0] == 'effect-tile' and len(key[1]) > 1
        and key[1][0] == ('original-native-source',) and len(key[1]) == 4
        for key in scene._modifier_render_cache)
    assert native_bytes(source) == original_pixels
    assert '_scene_cancelled' not in vars(scene)
    token.clear()
    monkeypatch.setattr(RegionalBlur, 'apply', original)
    with _scene_cancellation(scene, token.is_set):
        actual = tile_output(scene, source, bounds, [modifier], QTransform(), **arguments())
    assert actual is not None and not token.is_set()
    assert native_bytes(source) == original_pixels


def test_cancel_after_source_capture_prevents_conversion_and_source_publication(scene):
    source, bounds = image(), QRectF(0, 0, 530, 403)
    modifier, token, captures = BlurModifier(strength=7), Event(), []
    register(scene, [modifier])
    enable(scene)
    def capture(region):
        captures.append(QRectF(region))
        result = crop(source, bounds, region)[0]
        token.set()
        return result
    with _scene_cancellation(scene, token.is_set), pytest.raises(WorkCancelled):
        tile_output(scene, None, bounds, [modifier], QTransform(), capture=capture,
                    float_pipeline=True, **arguments())
    assert len(captures) == 1
    assert not scene._modifier_render_cache


def test_cancel_inside_one_cache_write_finishes_matching_retained_entry_before_unwind(
        scene, monkeypatch):
    source, bounds = image(), QRectF(0, 0, 530, 403)
    modifier, token, inserted, retained = BlurModifier(strength=7), Event(), [], []
    register(scene, [modifier])
    enable(scene)
    original_put = scene._modifier_cache_put
    original_retained = scene._effect_jobs.retained_put
    def cache_put(key, value):
        original_put(key, value)
        inserted.append((key, value.cacheKey()))
        token.set()
    def retained_put(scope, key, value, *args, **kwargs):
        original_retained(scope, key, value, *args, **kwargs)
        retained.append((key, value.cacheKey()))
    monkeypatch.setattr(scene, '_modifier_cache_put', cache_put)
    monkeypatch.setattr(scene._effect_jobs, 'retained_put', retained_put)
    with _scene_cancellation(scene, token.is_set), pytest.raises(WorkCancelled):
        tile_output(scene, source, bounds, [modifier], QTransform(), **arguments())
    assert len(inserted) == 1 and retained == inserted
    assert not any(key[0] == 'tile-output' for key in scene._modifier_render_cache)


@pytest.mark.parametrize('float_pipeline', [False, True])
@pytest.mark.parametrize('algorithm', ['normal', 'legacy'])
def test_uncancelled_native_bits_match_full_original_stage_rounding(
        scene, float_pipeline, algorithm):
    from comic_editor.render.effect_pipeline import render_stages
    modifiers = [BrightnessContrastModifier(brightness=12),
                 BlurModifier(strength=7, algorithm=algorithm),
                 HueSaturationLightnessModifier(hue=30)]
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    original_pixels = native_bytes(source)
    mapping = QTransform().translate(13, 22).rotate(19).scale(1.5, .5)
    if float_pipeline:
        full = apply_modifier_stack(source, modifiers, mapping.map(bounds.topLeft()).toTuple())
        frame = bounds
    else:
        full, frame = render_stages(scene, source, bounds, modifiers, mapping)
    enable(scene)
    token = Event()
    with _scene_cancellation(scene, token.is_set):
        actual = tile_output(scene, source, bounds, modifiers, mapping,
            float_pipeline=float_pipeline, **arguments())
    expected = crop(full, frame, arguments()['required'])
    assert actual is not None and actual[1] == expected[1]
    assert native_bytes(actual[0]) == native_bytes(expected[0])
    assert native_bytes(source) == original_pixels and not token.is_set()


@pytest.mark.parametrize('completed', [False, True])
def test_canceled_generic_target_never_paints_or_starts_fallback(scene, monkeypatch, completed):
    token = Event()
    page = scene.chapter.root_page_ids[0]
    obj = scene.chapter.add_object(page, RasterObject(interaction_rect=(0, 0, 530, 403)))
    scene.tiles.set_tile(obj.object_id, (0, 0), image(256, 256))
    scene.chapter.add_modifier(BlurModifier(strength=7), [('object', obj.object_id)])
    enable(scene)
    result = QImage(600, 450, QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor('green'))
    before = native_bytes(result)
    def generic(*args, **kwargs):
        token.set()
        return (image(20, 20), QRectF(0, 0, 20, 20)) if completed else False
    monkeypatch.setattr('comic_editor.render.tile_effects.generic_target_output', generic)
    monkeypatch.setattr(scene, '_modifier_object_signature',
        lambda *_: pytest.fail('Canceled generic target started full-frame fallback'))
    painter = QPainter(result)
    try:
        with _scene_cancellation(scene, token.is_set), pytest.raises(WorkCancelled):
            scene._render_modified_object(painter, obj, 1., QRectF(0, 0, 600, 450))
    finally:
        painter.end()
    assert native_bytes(result) == before
    assert not scene._modifier_render_cache
