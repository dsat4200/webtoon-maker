"""The optimized opacity pass must retain every legacy output byte."""
import numpy as np
import pytest
from PySide6.QtGui import QColorSpace, QImage

from comic_editor.ui.modifier_rendering import (
    _premultiplied_qimage, _qimage_premultiplied, apply_opacity_mask,
)


def legacy_mask(image, mask, black, white):
    """Independent full-image reference from the original mask operation."""
    current = _qimage_premultiplied(image)
    normalized = np.asarray(mask, dtype=np.float32)
    opacity = np.clip(float(black) + np.clip(normalized, 0., 1.) * (float(white) - float(black)), 0., 1.)[..., None]
    current[..., :3] *= opacity
    current[..., 3:4] *= opacity
    return _premultiplied_qimage(np.clip(current, 0., 1.))


def image_bytes(image):
    return bytes(image.constBits())


def image_fixture(width=257, height=193):
    rng = np.random.default_rng(612)
    values = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
    values[..., :3] = np.minimum(values[..., :3], values[..., 3:4])
    # Deliberate byte values and fractional alpha exercise truncation boundaries.
    values[0, :256, :3] = np.arange(256, dtype=np.uint8)[:, None]
    values[0, :256, 3] = 255
    return QImage(values.data, width, height, width * 4, QImage.Format.Format_RGBA8888_Premultiplied).copy()


@pytest.mark.parametrize("image_format", [QImage.Format.Format_ARGB32_Premultiplied,
    QImage.Format.Format_RGBA8888_Premultiplied, QImage.Format.Format_RGBA8888,
    QImage.Format.Format_RGB32, QImage.Format.Format_RGB888, QImage.Format.Format_Grayscale8])
@pytest.mark.parametrize("black,white", [(0., 1.), (1., 0.), (.17, .83), (.4, .4),
    (1., 1.), (0., 0.), (-1., 2.)])
def test_fractional_mask_is_byte_identical_across_formats_and_bindings(image_format, black, white):
    source = image_fixture().convertToFormat(image_format)
    rng = np.random.default_rng(271)
    mask = rng.uniform(-.2, 1.2, (source.height(), source.width())).astype(np.float32)
    source_before, mask_before = image_bytes(source), mask.copy()
    expected = legacy_mask(source, mask, black, white)
    actual = apply_opacity_mask(source, mask, black, white)
    assert actual.format() == QImage.Format.Format_ARGB32_Premultiplied
    assert image_bytes(actual) == image_bytes(expected)
    assert image_bytes(source) == source_before
    np.testing.assert_array_equal(mask, mask_before)


@pytest.mark.parametrize("kind", ["clear", "opaque", "binary", "sparse_edges", "float64", "strided"])
def test_sparse_mask_paths_and_noncontiguous_fields_preserve_rounding(kind):
    source = image_fixture()
    height, width = source.height(), source.width()
    y, x = np.indices((height, width))
    mask = (x > width // 2).astype(np.float32)
    if kind == "clear":
        mask.fill(0.)
    elif kind == "opaque":
        mask.fill(1.)
    elif kind == "sparse_edges":
        mask[:, width // 2 - 2:width // 2 + 3] = np.asarray((.01, .1, .5, .9, .99), np.float32)
    elif kind == "float64":
        mask = ((x + y) / (width + height)).astype(np.float64)
    elif kind == "strided":
        mask = np.random.default_rng(391).random((height, width * 2), dtype=np.float32)[:, ::2]
        assert not mask.flags.c_contiguous
    actual = apply_opacity_mask(source, mask, 0., 1.)
    assert image_bytes(actual) == image_bytes(legacy_mask(source, mask, 0., 1.))


def test_nextafter_opacities_match_legacy_at_integer_rounding_boundaries():
    source = image_fixture()
    fractions = np.arange(256, dtype=np.float32) / 255.
    near = np.r_[fractions, np.nextafter(fractions, np.float32(0.)), np.nextafter(fractions, np.float32(1.))]
    mask = np.resize(near, (source.height(), source.width()))
    assert image_bytes(apply_opacity_mask(source, mask, 0., 1.)) == image_bytes(legacy_mask(source, mask, 0., 1.))


def test_null_and_wrong_shape_return_original_without_changing_it():
    empty = QImage()
    assert apply_opacity_mask(empty, np.zeros((1, 1)), 0., 1.) is empty
    source = image_fixture()
    previous = image_bytes(source)
    assert apply_opacity_mask(source, np.zeros((3, 4)), 0., 1.) is source
    assert image_bytes(source) == previous


def test_mask_output_remains_premultiplied_with_transparent_pixels():
    source = image_fixture()
    mask = np.random.default_rng(504).random((source.height(), source.width()), dtype=np.float32)
    result = _qimage_premultiplied(apply_opacity_mask(source, mask, .2, .8))
    assert np.all(result[..., :3] <= result[..., 3:4])
    assert np.all(result[..., :3][result[..., 3] == 0] == 0)


def test_nan_mask_pixels_retain_legacy_transparency():
    source = image_fixture()
    mask = np.ones((source.height(), source.width()), np.float32)
    mask[::9, ::11] = np.nan
    with np.errstate(invalid="ignore"):
        assert image_bytes(apply_opacity_mask(source, mask, 0., 1.)) == image_bytes(legacy_mask(source, mask, 0., 1.))


def test_result_keeps_legacy_document_pixel_coordinates_and_color_metadata():
    source = image_fixture()
    source.setDevicePixelRatio(2.)
    source.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.SRgb))
    mask = np.full((source.height(), source.width()), .7, np.float32)
    expected = legacy_mask(source, mask, 0., 1.)
    actual = apply_opacity_mask(source, mask, 0., 1.)
    assert actual.devicePixelRatio() == expected.devicePixelRatio() == 1.
    assert actual.colorSpace() == expected.colorSpace()
    assert image_bytes(actual) == image_bytes(expected)
