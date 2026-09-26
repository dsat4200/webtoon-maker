"""Compact material storage preserves the original float sampling pipeline."""
import base64
import gc
import io
import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage

from comic_editor.core import brush_raster as raster
from comic_editor.core.brushes import BrushDefinition, BrushInput, BrushTexture, BrushTip
from comic_editor.core.tiles import TileStore


def _png(pixels):
    stream = io.BytesIO()
    Image.fromarray(pixels).save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _rgba(seed=92, shape=(19, 31)):
    pixels = np.random.default_rng(seed).integers(0, 256, (*shape, 4), dtype=np.uint8)
    pixels[0, :, 3] = 0  # invisible RGB must not bleed through premultiplied sampling
    pixels[-1, :, 3] = 255
    return pixels


def _legacy_material(png, premultiplied=False, *, gray=False, mip=0):
    """Reference implementation from before lazy float material sampling."""
    image = QImage.fromData(base64.b64decode(png), "PNG")
    if mip:
        image = image.scaled(max(1, image.width()//(2**mip)),
                             max(1, image.height()//(2**mip)),
                             Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    rgba = image.convertToFormat(QImage.Format_RGBA8888)
    result = (np.frombuffer(rgba.constBits(), np.uint8)
              .reshape(rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4]
              .reshape(rgba.height(), rgba.width(), 4).astype(np.float32)/255)
    if gray:
        result = np.mean(result[..., :3], axis=-1)*result[..., 3] + (1-result[..., 3])
    elif premultiplied:
        result[..., :3] *= result[..., 3:4]
    result.setflags(write=False)
    return result


@pytest.mark.parametrize("mode", ["straight", "premultiplied", "gray"])
@pytest.mark.parametrize("nearest", [False, True])
@pytest.mark.parametrize("wrap", [False, True])
@pytest.mark.parametrize("mip", [0, 1, 3])
def test_selected_material_pixels_match_float_reference_exactly(mode, nearest, wrap, mip):
    png = _png(_rgba())
    options = {"premultiplied": mode == "premultiplied", "gray": mode == "gray", "mip": mip}
    actual = raster._MaterialCache().get(png, **options)
    reference = _legacy_material(png, **options)
    height, width = reference.shape[:2]
    rng = np.random.default_rng(197)
    x = rng.uniform(-width-1, 2*width+1, (9, 13)).astype(np.float32)
    y = rng.uniform(-height-1, 2*height+1, (9, 13)).astype(np.float32)
    x[0, :5] = (-.51, -.5, 0, width-1, width-.5)
    y[0, :5] = (0, -.5, 0, height-1, height-.5)
    np.testing.assert_array_equal(
        raster._sample(actual, x, y, wrap=wrap, nearest=nearest),
        raster._sample(reference, x, y, wrap=wrap, nearest=nearest))


def test_premultiplication_and_gray_conversion_happen_before_interpolation():
    pixels = np.asarray([[[255, 0, 0, 0], [0, 0, 255, 255]]], dtype=np.uint8)
    cache = raster._MaterialCache()
    x, y = np.asarray([[.5]], np.float32), np.asarray([[0]], np.float32)
    premultiplied = raster._sample(cache.get(_png(pixels), True), x, y)
    np.testing.assert_array_equal(premultiplied, np.asarray([[[0, 0, .5, .5]]], np.float32))
    gray = raster._sample(cache.get(_png(pixels), gray=True), x, y)
    assert gray[0, 0] == pytest.approx((1+1/3)/2, abs=1e-7)


def test_held_material_survives_cache_clear_eviction_and_garbage_collection():
    cache = raster._MaterialCache(budget=18000)
    png = _png(_rgba())
    material = cache.get(png, True)
    x, y = np.meshgrid(np.arange(31), np.arange(19))
    expected = raster._sample(material, x, y)
    for seed in range(20):
        cache.get(_png(_rgba(seed)), True)
        assert cache.bytes <= cache.budget
    cache.clear()
    del png
    gc.collect()
    np.testing.assert_array_equal(raster._sample(material, x, y), expected)
    # Returned selected pixels are independent; editing them must not change
    # the retained Qt-backed image or subsequent samples.
    selected = material[np.asarray([2, 4]), np.asarray([3, 8])]
    try:
        selected[:] = 0
    except ValueError:
        pass  # a read-only selection is also safe
    np.testing.assert_array_equal(raster._sample(material, x, y), expected)


@pytest.mark.parametrize("mode", ["straight", "premultiplied", "gray"])
def test_material_retains_only_readonly_rgba8_and_never_expands_whole_image(monkeypatch, mode):
    pixels = _rgba(shape=(257, 513))
    png = _png(pixels)
    def eager_conversion_is_forbidden(image):
        raise AssertionError("A material was expanded to a full floating-point image")
    monkeypatch.setattr(raster, "image_pixels", eager_conversion_is_forbidden)
    material = raster._MaterialCache().get(png, premultiplied=mode == "premultiplied", gray=mode == "gray")
    assert material.pixels.dtype == np.uint8
    assert material.nbytes == pixels.size
    assert material.image.sizeInBytes() == pixels.size
    assert not material.pixels.flags.writeable
    with pytest.raises(ValueError):
        material.pixels[0, 0] = 0
    with pytest.raises(ValueError):
        material.pixels.setflags(write=True)
    selected = material[np.asarray([1, 250]), np.asarray([2, 500])]
    assert selected.dtype == np.float32
    assert selected.shape == ((2,) if mode == "gray" else (2, 4))


def test_material_owner_is_independent_of_the_callers_image_after_detach():
    png = _png(_rgba())
    image = QImage.fromData(base64.b64decode(png), "PNG").convertToFormat(QImage.Format_RGBA8888)
    material = raster._ImageMaterial(image, premultiplied=True)
    x, y = np.meshgrid(np.arange(31), np.arange(19))
    expected = raster._sample(material, x, y)
    image.fill(QColor("green"))  # Qt detaches the caller's copy, not the retained storage
    del image
    gc.collect()
    np.testing.assert_array_equal(raster._sample(material, x, y), expected)


@pytest.mark.parametrize("png_mode", ["RGBA", "RGB", "L", "P"])
def test_cached_original_supplies_exact_mips_and_modes_without_png_decode(monkeypatch, png_mode):
    image = Image.fromarray(_rgba(shape=(67, 41)))
    if png_mode == "P":
        image = image.quantize(colors=64)
    elif png_mode != "RGBA":
        image = image.convert(png_mode)
    stream = io.BytesIO()
    image.save(stream, "PNG")
    png = base64.b64encode(stream.getvalue()).decode("ascii")
    cases = [(mip, premultiplied, gray) for mip in (0, 1, 3)
             for premultiplied, gray in ((False, False), (True, False), (False, True))]
    expected = {(mip, premultiplied, gray): _legacy_material(png, premultiplied, gray=gray, mip=mip)
                for mip, premultiplied, gray in cases}
    cache = raster._MaterialCache()
    cache.get(png, True)

    def redundant_decode(*args, **kwargs):
        raise AssertionError("The original image is already decoded in the cache")

    monkeypatch.setattr(QImage, "fromData", redundant_decode)
    for mip, premultiplied, gray in cases:
        material = cache.get(png, premultiplied, gray=gray, mip=mip)
        np.testing.assert_array_equal(material[:, :], expected[mip, premultiplied, gray])
        assert cache.bytes <= cache.budget


def test_source_png_storage_is_counted_once_across_cached_levels_and_modes():
    png = _png(_rgba(shape=(71, 43)))
    cache = raster._MaterialCache()
    for mip in (0, 1, 2):
        cache.get(png, mip=mip)
        cache.get(png, True, mip=mip)
    assert cache.bytes == len(png)+sum(material.nbytes for material, _ in cache.values.values())
    assert cache._png_refs[png][1] == len(cache.values)
    same_png = png.encode().decode()
    assert same_png == png and same_png is not png
    cache.get(same_png, gray=True)
    assert all(key[0] is png for key in cache.values)
    cache.clear()
    assert cache.bytes == 0 and not cache._png_refs


def test_smaller_mip_is_not_resized_from_an_existing_reduced_mip():
    png = _png(_rgba(shape=(71, 43)))
    cache = raster._MaterialCache()
    cache.get(png, True, mip=1)
    actual = cache.get(png, True, mip=3)
    np.testing.assert_array_equal(actual[:, :], _legacy_material(png, True, mip=3))


@pytest.mark.parametrize("mode", ["straight", "premultiplied", "gray"])
@pytest.mark.parametrize("nearest", [False, True])
@pytest.mark.parametrize("wrap", [False, True])
def test_lossless_row_blocks_preserve_original_sampling_exactly(mode, nearest, wrap):
    png = _png(_rgba(shape=(131, 43)))
    image = QImage.fromData(base64.b64decode(png), "PNG")
    options = {"premultiplied": mode == "premultiplied", "gray": mode == "gray"}
    material = raster._CompressedImageMaterial(image, **options)
    expected = _legacy_material(png, **options)
    rng = np.random.default_rng(744)
    x = rng.uniform(-10, 55, (9, 23)).astype(np.float32)
    y = rng.uniform(-5, 142, (9, 23)).astype(np.float32)
    # Include samples on either side of a compressed-row boundary.
    y[0, :4] = (63, 63.5, 64, 64.5)
    np.testing.assert_array_equal(raster._sample(material, x, y, nearest=nearest, wrap=wrap),
                                  raster._sample(expected, x, y, nearest=nearest, wrap=wrap))
    np.testing.assert_array_equal(material[:, :], expected)
    assert not hasattr(material, "pixels")  # no full decoded image retained
    assert material.block_rows*material.width*4 <= 256*1024


def test_lossless_row_blocks_reconstruct_exact_original_for_direct_mips():
    png = _png(_rgba(shape=(131, 43)))
    material = raster._CompressedImageMaterial(QImage.fromData(base64.b64decode(png), "PNG"))
    for mip in (0, 1, 3):
        image = material.image
        if mip:
            image = image.scaled(max(1, image.width()//(2**mip)), max(1, image.height()//(2**mip)),
                                 Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        np.testing.assert_array_equal(raster._ImageMaterial(image)[:, :], _legacy_material(png, mip=mip))


def test_large_compressible_original_stays_lossless_in_small_retained_cache(monkeypatch):
    pixels = np.zeros((1100, 2000, 4), np.uint8)
    pixels[400:430, 300:480] = (133, 19, 89, 127)
    pixels[850:880, 1750:1820] = (19, 89, 133, 215)
    png = _png(pixels)
    cache = raster._MaterialCache(budget=512*1024)
    material = cache.get(png, True)
    assert isinstance(material, raster._CompressedImageMaterial)
    assert cache.bytes <= cache.budget < pixels.nbytes
    assert len(cache.values) == 1

    def repeated_decode(*args, **kwargs):
        raise AssertionError("A retained compressed original must not decode its PNG again")

    monkeypatch.setattr(QImage, "fromData", repeated_decode)
    assert cache.get(png, True) is material
    y, x = np.asarray([0, 415, 865, 1099]), np.asarray([0, 310, 1770, 1999])
    expected = pixels[y, x].astype(np.float32)/255
    expected[:, :3] *= expected[:, 3:4]
    np.testing.assert_array_equal(material[y, x], expected)
    cache.clear()
    np.testing.assert_array_equal(material[y, x], expected)


def test_cache_evicts_compact_images_by_bytes_and_keeps_current_material_alive():
    cache = raster._MaterialCache(budget=7000)
    for color in range(3):
        pixels = np.zeros((32, 32, 4), dtype=np.uint8)
        pixels[..., color] = 255
        pixels[..., 3] = 255
        material = cache.get(_png(pixels), True)
        assert cache.bytes <= cache.budget
        assert len(cache.values) == 1
        assert material.nbytes == 4096
        np.testing.assert_array_equal(material[np.asarray([16]), np.asarray([16])],
                                      pixels[16:17, 16].astype(np.float32)/255)
    cache.clear()
    assert cache.bytes == 0
    assert not cache.values
    assert material[16, 16][2] == 1


class _LegacyCache:
    def __init__(self):
        self.values = {}

    def get(self, png, premultiplied=False, *, gray=False, mip=0):
        key = png, premultiplied, gray, mip
        if key not in self.values:
            self.values[key] = _legacy_material(png, premultiplied, gray=gray, mip=mip)
        return self.values[key]


def _render(brush, cache, monkeypatch):
    monkeypatch.setattr(raster, "_material_cache", cache)
    tiles = TileStore(32)
    stroke = raster.RasterBrushStroke(tiles, "paint", brush, QColor("#a43657"), {}, seed=77)
    stroke.begin(BrushInput(20, 30, pressure=.4))
    stroke.add(BrushInput(60, 42, pressure=.8, time=.2))
    stroke.add(BrushInput(87, 27, pressure=1, time=.4))
    stroke.finish()
    return {key: bytes(image.constBits()) for key, image in tiles.iter_tiles("paint")}


@pytest.mark.parametrize("aa", range(4))
@pytest.mark.parametrize("ribbon", [False, True])
@pytest.mark.parametrize("mode", ["mask", "dual_color", "color"])
@pytest.mark.parametrize("compressed", [False, True])
def test_stamp_ribbon_and_texture_rendering_match_float_reference(monkeypatch, aa, ribbon, mode, compressed):
    pixels = _rgba(shape=(31, 53))
    tip = BrushTip("Alpha material", 53, 31, _png(pixels), mode=mode, shape="image")
    texture = BrushTexture(png=_png(_rgba(31, (9, 13))), density=.65,
                           per_dab=bool(aa % 2), scale=.8, angle=23)
    brush = BrushDefinition(size=17, spacing=.2, antialiasing=aa,
                            ribbon=ribbon, angle=90 if ribbon else 27,
                            tips=(tip,), texture=texture, opacity=.7,
                            sub_color=(38, 175, 199, 180), flip_x="alternate")
    expected = _render(brush, _LegacyCache(), monkeypatch)
    if compressed:
        monkeypatch.setattr(raster, "_ImageMaterial", raster._CompressedImageMaterial)
    actual = _render(brush, raster._MaterialCache(), monkeypatch)
    assert actual.keys() == expected.keys()
    assert actual == expected
