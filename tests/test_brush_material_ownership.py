"""Authored materials prepare independently before interactive sampling."""
import base64
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image
from PySide6.QtGui import QColor, QImage

from comic_editor.core import brush_raster as raster
from comic_editor.core.brushes import (BrushDefinition, BrushDynamics, BrushInput,
                                      BrushTexture, BrushTip)
from comic_editor.core.tiles import TileStore


def png(seed, shape):
    pixels = np.random.default_rng(seed).integers(0, 256, (*shape, 4), dtype=np.uint8)
    pixels[0, :, 3] = 0
    pixels[-1, :, 3] = 255
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, 'PNG')
    return base64.b64encode(stream.getvalue()).decode('ascii')


def definition():
    tip = BrushTip('Native artwork', 31, 19, png(713, (19, 31)),
                   shape='image', mode='dual_color')
    texture = BrushTexture(png=png(291, (13, 11)), density=.8, angle=21,
                           scale=.7, contrast=.1, per_dab=True)
    second = BrushDefinition(size=11, tips=(replace(tip, mode='color'),),
        texture=replace(texture, per_dab=False), spacing=.19, direction='stroke')
    return BrushDefinition(size=24, tips=(tip, replace(tip, mode='mask')),
        texture=texture, dual=second, spacing=.16, thickness=.7,
        opacity=.7, sub_color=(31, 129, 203, 155), flip_x='random', flip_y='alternate',
        hue_jitter=.1, saturation_jitter=.07, repeat_mode='random',
        dynamics={'size': BrushDynamics(pressure=True, minimum=.01),
                  'density': BrushDynamics(pressure=True, minimum=.2)})


def draw(brush, materials=None):
    store, before = TileStore(64), {}
    stroke = raster.RasterBrushStroke(store, 'paint', brush, QColor('#7c391d'), before,
                                      seed=379, materials=materials)
    packets = [BrushInput(24, 28, pressure=.02, time=0),
               BrushInput(49, 38, pressure=.4, time=.03),
               BrushInput(76, 30, pressure=1., time=.06),
               BrushInput(104, 36, pressure=.1, time=.09)]
    stroke.begin(packets[0])
    for packet in packets[1:]:
        stroke.add(packet)
    stroke.finish()
    return dict(store.iter_tiles('paint')), before, stroke.bounds


@pytest.mark.parametrize('antialiasing', [0, 2])
@pytest.mark.parametrize('mode', ['stamp', 'ribbon', 'spray', 'replay'])
def test_prepared_materials_preserve_exact_pixels_and_input_order(monkeypatch, antialiasing, mode):
    brush = replace(definition(), antialiasing=antialiasing)
    if mode == 'ribbon':
        brush = replace(brush, ribbon=True)
    elif mode == 'spray':
        brush = replace(brush, spray=True, particle_size=7, particle_density=3,
                        particle_angle_random=.4)
    elif mode == 'replay':
        brush = replace(brush, post_correction=.4, taper_end=8)
    monkeypatch.setattr(raster, '_material_cache', raster._MaterialCache())
    expected, before, bounds = draw(brush)
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared = worker.submit(raster.prepare_brush_materials, brush).result()
    def no_live_decode(*_args, **_kwargs):
        pytest.fail('Prepared stroke consulted the mutable live material cache')
    monkeypatch.setattr(raster._material_cache, 'get', no_live_decode)
    monkeypatch.setattr(QImage, 'fromData', no_live_decode)
    actual, actual_before, actual_bounds = draw(brush, prepared)
    assert actual.keys() == expected.keys() and actual_before.keys() == before.keys()
    assert actual_bounds == bounds
    assert actual
    for key in expected:
        assert actual[key] == expected[key]


def test_worker_material_owner_never_shares_live_cache_and_covers_every_mip(monkeypatch):
    brush, gui = definition(), threading.get_ident()
    original = QImage.fromData
    calls = []
    def decode(*args, **kwargs):
        assert threading.get_ident() != gui
        calls.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(QImage, 'fromData', decode)
    monkeypatch.setattr(raster._material_cache, 'get', lambda *_a, **_k:
                        pytest.fail('Worker accessed mutable live material cache'))
    with ThreadPoolExecutor(max_workers=1) as worker:
        owner = worker.submit(raster.prepare_brush_materials, brush).result()
    assert len(calls) == 2  # repeated primary/secondary resources decode once
    for mip in range(5):
        material = owner.get(brush.tips[0].png, True, mip=mip)
        assert material.shape[:2] == (max(1, 19//(2**mip)), max(1, 31//(2**mip)))
        assert not material.pixels.flags.writeable
    assert owner.get(brush.texture.png, gray=True).shape == (13, 11)
    with pytest.raises(TypeError):
        owner._values['changed'] = None


def test_material_admission_estimate_reads_only_small_header_without_source_hash(monkeypatch):
    class NoHash(str):
        def __hash__(self):
            pytest.fail('GUI hashed a complete authored PNG')
    brush = definition()
    brush = replace(brush, tips=(replace(brush.tips[0], png=NoHash(brush.tips[0].png)),),
                    texture=replace(brush.texture, png=NoHash(brush.texture.png)), dual=None)
    original = base64.b64decode
    def decode(header, *args, **kwargs):
        assert len(header) <= 32
        return original(header, *args, **kwargs)
    monkeypatch.setattr(base64, 'b64decode', decode)
    assert raster.brush_material_working_bytes(brush) > 19*31*4
    assert raster.brush_material_working_bytes(BrushDefinition()) == 0


def test_installed_owner_is_retained_and_cannot_change_after_sampling():
    brush = definition()
    owner = raster.prepare_brush_materials(brush)
    stroke = raster.RasterBrushStroke(TileStore(64), 'paint', brush, QColor('black'), {})
    stroke.install_materials(owner)
    stroke.begin(BrushInput(20, 20))
    with pytest.raises(RuntimeError, match='after sampling'):
        stroke.install_materials(owner)
    stroke.finish()
    with pytest.raises(ValueError, match='not prepared'):
        owner.get(brush.tips[0].png, True, mip=19)
