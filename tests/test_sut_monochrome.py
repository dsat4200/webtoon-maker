"""Packed monochrome materials contain separate black/white coverage planes."""
from pathlib import Path
import struct
import zlib

import numpy as np
import pytest

from comic_editor.core.sut_import import SutImportError, _offscreen_image, import_sut


def packed_material(black,white,*,width=13,height=9):
    u32 = lambda number: struct.pack('>I',number)
    raster = np.packbits(black,bitorder='big').tobytes()+np.packbits(white,bitorder='big').tobytes()
    compressed = zlib.compress(raster)
    begin = u32(19)+'BlockDataBeginChunk'.encode('utf-16-be')
    end = u32(17)+'BlockDataEndChunk'.encode('utf-16-be')
    tile = (begin+struct.pack('>4I',0,16384,256,256)+u32(1)
            +u32(len(compressed)+4)+struct.pack('<I',len(compressed))+compressed+end)
    block = u32(len(tile)+4)+tile
    values = (width,height,1,1,17,1,1,1,8192,1,32,1,32,65536,256,256,8,8,1,0)
    attribute = u32(16)*4+u32(9)+'Parameter'.encode('utf-16-be')+struct.pack('>20I',*values)
    return attribute,block


def test_packed_black_white_and_transparency_keep_exact_registered_pixels():
    black,white = (np.zeros((256,256),np.uint8) for _ in range(2))
    black[2,3] = 1
    white[4,8] = 1
    white[8,12] = 1
    image = _offscreen_image(*packed_material(black,white))
    assert image.size == (13,9)
    assert image.getpixel((3,2)) == (0,0,0,255)
    assert image.getpixel((8,4)) == (255,255,255,255)
    assert image.getpixel((12,8)) == (255,255,255,255)
    assert image.getpixel((0,0)) == (0,0,0,0)
    assert np.count_nonzero(np.asarray(image)[...,3]) == 3


def test_invalid_packed_plane_length_and_overlapping_coverage_are_rejected():
    black = np.ones((256,256),np.uint8)
    with pytest.raises(SutImportError,match='Overlapping'):
        _offscreen_image(*packed_material(black,black))
    with pytest.raises(SutImportError,match='tile pixels'):
        _offscreen_image(*packed_material(black,black[:128]))


def test_optional_downloaded_flat_texture_decodes_full_monochrome_tip():
    path = Path('.artifacts/brush-investigation/installed/additional-request-reconstructed/2303665-01.registered.reconstructed.sut')
    if not path.exists():
        pytest.skip('Private downloaded fixture is not distributed')
    brush = import_sut(path)
    assert (brush.tips[0].width,brush.tips[0].height,brush.tips[0].mode) == (345,427,'dual_color')
    assert brush.source['materials'][0]['LayerColorTypeIndex'] == 2
