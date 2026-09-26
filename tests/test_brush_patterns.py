"""Flat patterns keep their phase across dabs, strokes and tile boundaries."""
from dataclasses import replace

import numpy as np
import pytest

from comic_editor.core.brushes import default_brushes
from test_brush_raster import atlas,rendered


@pytest.mark.parametrize("identifier",["pixel-dots","pixel-stripes","pixel-checker"])
def test_flat_pattern_does_not_shift_or_fill_in_during_overlapping_strokes(identifier):
    brush=next(b for b in default_brushes() if b.id==identifier)
    store,_,_=rendered(brush,((16,32),(112,32)))
    first=atlas(store)
    # Offset the brush's path while covering the same interior pixels.
    rendered(brush,((112,34),(16,34)),tiles=store)
    second=atlas(store)
    np.testing.assert_array_equal(first[25:40,24:104],second[25:40,24:104])
    coverage=second[25:40,24:104,3]
    assert set(np.unique(coverage))=={0.,1.}
    # Phase remains continuous across the TileStore boundary at x=64.
    np.testing.assert_array_equal(second[25:40,56:64,3],second[25:40,64:72,3])


def test_default_chain_has_ink_through_each_repeat_boundary():
    brush=replace(next(b for b in default_brushes() if b.id=="chain"),size=48)
    store,_,_=rendered(brush,((8,32),(392,32)))
    coverage=atlas(store,(0,0,400,64))[...,3]
    assert np.all(coverage[:,12:388].max(axis=0)>.7)
    # Every source-length repeat has identical interior pixels on a straight path.
    for offset in (48,96,144,192,240):
        np.testing.assert_allclose(coverage[:,32:64],coverage[:,32+offset:64+offset],atol=1/255)
