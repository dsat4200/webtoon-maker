"""Native brush source reads and writes preserve precision outside the stroke."""
import numpy as np
import pytest
from PySide6.QtGui import QColor, QColorSpace, QImage

from comic_editor.core.brushes import BrushDefinition, BrushInput
from comic_editor.core.brush_raster import RasterBrushStroke, image_pixels
from comic_editor.core.pixel_arrays import native_rgba_pixels
from comic_editor.core.tiles import TileStore


def native_image(format):
    image = QImage(64, 64, format)
    image.setColorSpace(QColorSpace(QColorSpace.SRgb))
    associated = format.name.endswith('_Premultiplied')
    if format in (QImage.Format_RGBA64, QImage.Format_RGBA64_Premultiplied):
        rows = np.frombuffer(image.bits(), np.uint16).reshape(64, image.bytesPerLine() // 2)
        rgba = rows[:, :64*4].reshape(64, 64, 4)
        rgba[:] = [1, 2, 3, 7] if associated else [12345, 23456, 34567, 7]
    else:
        dtype = np.float16 if image.depth() == 64 else np.float32
        rows = np.frombuffer(image.bits(), dtype).reshape(64, image.bytesPerLine() // np.dtype(dtype).itemsize)
        rgba = rows[:, :64*4].reshape(64, 64, 4)
        values = np.array([-.125, .400049, 1.375, .000003], np.float32)
        if associated:
            values[:3] *= values[3]
        rgba[:] = values
    return image


@pytest.mark.parametrize('format', [QImage.Format_RGBA64, QImage.Format_RGBA64_Premultiplied,
    QImage.Format_RGBA16FPx4, QImage.Format_RGBA16FPx4_Premultiplied,
    QImage.Format_RGBA32FPx4, QImage.Format_RGBA32FPx4_Premultiplied])
@pytest.mark.parametrize('edge', [False, True])
def test_brush_keeps_native_low_alpha_history_format_and_untouched_bits(format, edge):
    source = native_image(format)
    originals = bytes(source.constBits())
    tiles = TileStore(64)
    tiles.set_tile('paint', (0, 0), source)
    before = {}
    brush = BrushDefinition(size=8, opacity=.4, spacing=.12,
        watercolor_edge=2 if edge else 0, watercolor_after=edge,
        watercolor_blur=1 if edge else 0, watercolor_opacity=.5)
    stroke = RasterBrushStroke(tiles, 'paint', brush, QColor('#4488CC'), before)
    expected, associated = native_rgba_pixels(source)
    sampled = image_pixels(source)
    np.testing.assert_array_equal(sampled[..., 3], expected[..., 3])
    if not associated:
        np.testing.assert_array_equal(sampled, expected)
    base = stroke._base_tile((0, 0))
    if associated:
        np.testing.assert_allclose(base, expected, rtol=1e-6, atol=1e-12)
    else:
        np.testing.assert_array_equal(base[..., :3], expected[..., :3] * expected[..., 3:4])
    stroke.begin(BrushInput(16, 16, time=0))
    stroke.add(BrushInput(28, 16, time=.1))
    stroke.finish()
    output = tiles.tile('paint', (0, 0))
    assert output.format() == source.format() and output.colorSpace() == source.colorSpace()
    assert before[(0, 0)].format() == source.format()
    assert bytes(before[(0, 0)].constBits()) == originals
    original_rows = np.frombuffer(originals, np.uint8).reshape(64, source.bytesPerLine())
    output_rows = np.frombuffer(output.constBits(), np.uint8).reshape(64, output.bytesPerLine())
    np.testing.assert_array_equal(output_rows[48:], original_rows[48:])
    pixel_bytes = source.depth() // 8
    np.testing.assert_array_equal(output_rows[:, 48*pixel_bytes:], original_rows[:, 48*pixel_bytes:])
    assert bytes(output.constBits()) != originals
