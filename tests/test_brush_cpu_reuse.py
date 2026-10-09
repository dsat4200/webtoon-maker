"""CPU reuse keeps every original dab and native live publication bit."""
from dataclasses import fields, replace
import math
import struct

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core import brush_raster
from comic_editor.core.brushes import BrushDab, BrushDefinition, BrushDynamics, BrushInput, default_brushes
from comic_editor.core.brush_raster import RasterBrushStroke, prepare_brush_materials
from comic_editor.core.brush_stroke import BrushStroke
from comic_editor.core.tiles import TileStore
from references.brush_native_primitives_20261008 import NativeBrushStroke, NativeRasterBrushStroke
from test_brush_deferred_flush import definitions
from test_brush_native_sources import native_image


def scalar_bits(value):
    return (type(value), struct.pack('!d', value)) if isinstance(value, float) else (type(value), value)


def dab_bits(dab):
    return tuple(scalar_bits(getattr(dab, field.name)) for field in fields(BrushDab))


def path():
    return [BrushInput(-8.25+index*7.1, 29.5+9*math.sin(index*.31), pressure=.1+index*.047,
                      tilt_x=index*3, tilt_y=-index*2, rotation=index*13, time=index/120)
            for index in range(18)]


brushes = definitions()
brushes.update({brush.id: brush for brush in default_brushes()})
brushes['dynamic_spray'] = BrushDefinition(size=13, spacing=.12, spray=True, particle_size=4,
    particle_density=3, taper_start=13, taper_end=9, taper_parameters=('size', 'opacity', 'particle_size'),
    dynamics={'size': BrushDynamics(pressure=True, minimum=.07),
              'spacing': BrushDynamics(random=.31), 'angle': BrushDynamics(tilt=True),
              'opacity': BrushDynamics(pressure=True, minimum=.2)})
brushes['continuous'] = BrushDefinition(size=8, continuous=True, continuous_rate=120, spacing=.08)


@pytest.mark.parametrize('brush', list(brushes.values()), ids=list(brushes))
def test_scheduler_emits_the_original_exact_dab_fields_and_rng(brush):
    streams = []
    states = []
    for kind in (NativeBrushStroke, BrushStroke):
        scheduler = kind(brush, seed=771)
        dabs = []
        for index, sample in enumerate(path()):
            dabs.extend((scheduler.begin if index == 0 else scheduler.add)(sample))
        dabs.extend(scheduler.finish())
        streams.append([dab_bits(dab) for dab in dabs])
        states.append((scheduler.rng.getstate(), scheduler.spacing_rng.getstate(), scheduler.index,
                       scalar_bits(scheduler.distance), scalar_bits(scheduler.next_distance)))
    assert streams[0] == streams[1] and states[0] == states[1]


@pytest.mark.parametrize('minimum,parameters', [(-1., ('opacity',)), (1., ('opacity',)),
                                              (0., ('size',)), (.23456789123, ('density',))])
def test_direct_pending_dabs_preserve_signed_zero_types_and_nan_payload(minimum, parameters):
    brush = BrushDefinition(minimum_pixel=False, taper_minimum=minimum, taper_parameters=parameters)
    outputs = []
    for kind in (NativeBrushStroke, BrushStroke):
        scheduler = kind(brush)
        scheduler.pending.extend([BrushDab(1., 2., 3., opacity=-0., density=0.),
                                  BrushDab(1., 2., 3, opacity=float('nan'), density=float('inf'))])
        outputs.append([dab_bits(dab) for dab in scheduler._drain(final=True)])
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize('bounds', [(-4.3, -3.2, 9.1, 6.8), (61.2, 61.7, 6.3, 7.4),
                                  (-67.4, -66.9, 10.8, 7.2), (2**24-3.25, 2**24+.25, 7.5, 4.75),
                                  (-2**25-3.25, -2**25+.25, 7.5, 4.75)])
def test_broadcast_regions_keep_original_integer_grid_and_float32_phase(bounds):
    strokes = [kind(TileStore(64), 'paint', BrushDefinition(), QColor('red'), {})
               for kind in (NativeRasterBrushStroke, RasterBrushStroke)]
    regions = [list(stroke._regions(QRectF(*bounds))) for stroke in strokes]
    assert len(regions[0]) == len(regions[1])
    for first, second in zip(*regions):
        assert first[:3] == second[:3]
        for a, b in zip(first[3:], second[3:]):
            assert a.dtype == b.dtype == np.float32 and a.shape == b.shape
            assert a.tobytes() == b.tobytes()


@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied,
                                  QImage.Format_RGBA64_Premultiplied,
                                  QImage.Format_RGBA32FPx4_Premultiplied])
@pytest.mark.parametrize('name', ['dry', 'dynamic_spray', 'continuous', 'color_ribbon', 'dual',
                                'running', 'smear', 'live_edge', 'corrected', 'dual_ending',
                                'stroke_texture', 'dual_wet_texture_edge'])
def test_each_live_native_buffer_matches_original_primitives_and_history(monkeypatch, format, name):
    brush = brushes[name]
    materials = prepare_brush_materials(brush)
    frames, originals, transactions, planes = [], [], [], []
    for kind, scheduler in ((NativeRasterBrushStroke, NativeBrushStroke), (RasterBrushStroke, BrushStroke)):
        store, before = TileStore(64), {}
        source = native_image(format) if format != QImage.Format_ARGB32_Premultiplied else QImage(
            64, 64, QImage.Format_ARGB32_Premultiplied)
        if format == QImage.Format_ARGB32_Premultiplied:
            source.fill(QColor('#75335981'))
        for key in ((-1, 0), (0, 0), (1, 0)):
            store.set_tile('paint', key, QImage(source))
        original = {key: QImage(image) for key, image in store.iter_tiles('paint')}
        current_frames, current_planes = [], []
        with monkeypatch.context() as patch:
            # Corrected final replay must use the same independent primitives.
            patch.setattr(brush_raster, 'RasterBrushStroke', kind)
            patch.setattr(brush_raster, 'BrushStroke', scheduler)
            stroke = kind(store, 'paint', brush, QColor('#8F4499DD'), before, seed=771, materials=materials)
            for index, sample in enumerate(path()):
                (stroke.begin if index == 0 else stroke.add)(sample)
                current_frames.append({key: (image.format(), image.colorSpace(), bytes(image.constBits()))
                    for key, image in store.iter_tiles('paint')})
                current_planes.append({key: pixels.tobytes() for key, pixels in stroke.main.pixels.items()})
            stroke.finish()
            current_frames.append({key: (image.format(), image.colorSpace(), bytes(image.constBits()))
                for key, image in store.iter_tiles('paint')})
        frames.append(current_frames)
        planes.append(current_planes)
        originals.append(original)
        transactions.append(before)
        for key, image in before.items():
            store.set_tile('paint', key, image)
        assert {key: QImage(image) for key, image in store.iter_tiles('paint')} == original
    assert frames[0] == frames[1] and planes[0] == planes[1]
    assert transactions[0] == transactions[1] and originals[0] == originals[1]
