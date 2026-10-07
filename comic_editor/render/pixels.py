"""Versioned pixel contracts and explicit, cached color transforms.

Version one preserves encoded-sRGB RGBA8 stage semantics. Version two keeps
working images at float precision; color transforms always operate on straight
RGB and reapply the original coverage. Presentation/export are explicit edges.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from PySide6.QtGui import QColorSpace, QImage
from comic_editor.core.pixel_arrays import normalized_bytes, truncated_bytes, image_has_high_precision
from comic_editor.core.pixel_contract import PixelContract, LEGACY_PIXELS, FLOAT_PIXELS


_contract = ContextVar('render_pixel_contract', default=LEGACY_PIXELS)


def current_contract():
    return _contract.get()


@contextmanager
def pixel_scope(contract):
    token = _contract.set(contract)
    try:
        yield contract
    finally:
        _contract.reset(token)


def premultiplied_pixels(image):
    """Read a detached float array, respecting Qt's stride and storage type."""
    if image.isNull():
        return np.empty((0, 0, 4), np.float32)
    if image_has_high_precision(image):
        converted = image.convertToFormat(QImage.Format_RGBA32FPx4_Premultiplied)
        rows = np.frombuffer(converted.constBits(), np.float32).reshape(converted.height(), converted.bytesPerLine()//4)
        return rows[:, :converted.width()*4].reshape(converted.height(), converted.width(), 4).copy()
    converted = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    rows = np.frombuffer(converted.constBits(), np.uint8).reshape(converted.height(), converted.bytesPerLine())
    return normalized_bytes(rows[:, :converted.width()*4].reshape(converted.height(), converted.width(), 4))


def working_image(pixels, contract=None):
    contract = contract or current_contract()
    pixels = np.asarray(pixels)
    if pixels.ndim != 3 or pixels.shape[-1] != 4:
        raise ValueError('Working pixels must be an H × W × RGBA array')
    if not pixels.shape[0] or not pixels.shape[1]:
        return QImage()
    if not contract.floating:
        data = truncated_bytes(pixels)
        height, width = data.shape[:2]
        return QImage(data.data,width,height,data.strides[0],QImage.Format_RGBA8888_Premultiplied).copy().convertToFormat(QImage.Format_ARGB32_Premultiplied)
    dtype = np.float16 if contract.precision == 'float16' else np.float32
    height, width = pixels.shape[:2]
    image = QImage(width, height, contract.image_format)
    if image.isNull():
        raise MemoryError('Could not allocate the working image')
    # The float frame owns its storage from creation. Copy into Qt's stride
    # rather than creating a temporary image that borrows a NumPy memoryview.
    rows = np.frombuffer(image.bits(), dtype=dtype).reshape(height, image.bytesPerLine()//np.dtype(dtype).itemsize)
    rows[:, :width*4].reshape(height, width, 4)[:] = pixels
    return image


@lru_cache(maxsize=4)
def color_config(path=''):
    import PyOpenColorIO as ocio
    if path:
        return ocio.Config.CreateFromFile(path)
    # Use a small application-owned configuration rather than an evolving
    # studio default. Alpha is a data channel and never receives a gamma curve.
    config = ocio.Config()
    config.setMajorVersion(2)
    config.setMinorVersion(0)
    linear = ocio.ColorSpace(name='linear_srgb', bitDepth=ocio.BIT_DEPTH_F32)
    encoded = ocio.ColorSpace(name='srgb', bitDepth=ocio.BIT_DEPTH_F32,
        toReference=ocio.ExponentWithLinearTransform(gamma=[2.4,2.4,2.4,1.], offset=[.055,.055,.055,0.]))
    config.addColorSpace(linear)
    config.addColorSpace(encoded)
    config.setRole(ocio.ROLE_DEFAULT,'srgb')
    config.setRole(ocio.ROLE_SCENE_LINEAR,'linear_srgb')
    config.addDisplayView('sRGB','Standard','srgb','')
    config.validate()
    return config


@dataclass(frozen=True)
class _LoadedColorConfig:
    path: str
    identity: str
    config: object = field(compare=False, hash=False, repr=False)


def _loaded_color_config(path):
    config = color_config(path)
    return _LoadedColorConfig(path, config.getCacheID(), config)


def working_representation(contract):
    """Identify the scoped working source using the currently loaded config."""
    return contract.signature, _loaded_color_config(contract.ocio_config).identity


@lru_cache(maxsize=32)
def _color_processor(loaded, source, destination, display='', view=''):
    import PyOpenColorIO as ocio
    config = loaded.config
    if display:
        transform = ocio.DisplayViewTransform(src=source, display=display, view=view)
        return config.getProcessor(transform)
    return config.getProcessor(source,destination)


def color_processor(path, source, destination, display='', view=''):
    return _color_processor(_loaded_color_config(path), source, destination, display, view)


@lru_cache(maxsize=32)
def _gpu_color_processor(loaded, source, destination, display='', view=''):
    """Cache OCIO's GPU program and resources independently of tile execution.

    The descriptor owns its generated LUT values. Callers must upload them in
    the owning OpenGL context and treat this shared descriptor as read-only.
    """
    import PyOpenColorIO as ocio
    shader = ocio.GpuShaderDesc.CreateShaderDesc()
    shader.setLanguage(ocio.GPU_LANGUAGE_GLSL_4_0)
    shader.setFunctionName('ocio_process')
    shader.setResourcePrefix('ocio_')
    processor = _color_processor(loaded, source, destination, display, view)
    processor.getDefaultGPUProcessor().extractGpuShaderInfo(shader)
    return shader


def gpu_color_processor(path, source, destination, display='', view=''):
    """Reuse a GPU descriptor only for the same currently loaded OCIO config."""
    return _gpu_color_processor(_loaded_color_config(path), source, destination, display, view)


color_processor.cache_clear = _color_processor.cache_clear
color_processor.cache_info = _color_processor.cache_info
color_processor.cache_parameters = _color_processor.cache_parameters
gpu_color_processor.cache_clear = _gpu_color_processor.cache_clear
gpu_color_processor.cache_info = _gpu_color_processor.cache_info
gpu_color_processor.cache_parameters = _gpu_color_processor.cache_parameters


def transform_pixels(pixels, source, destination, *, contract=FLOAT_PIXELS, display=False,
                     premultiplied=True):
    if source == destination and not display:
        return np.array(pixels, dtype=np.float32, copy=True)
    result = np.array(pixels, dtype=np.float32, copy=True, order='C')
    alpha = result[...,3:4].copy()
    if premultiplied:
        np.divide(result[...,:3],alpha,out=result[...,:3],where=alpha>0)
        result[...,:3] = np.where(alpha>0,result[...,:3],0.)
    processor = color_processor(contract.ocio_config,source,destination,
        contract.display if display else '',contract.view if display else '')
    processor.getDefaultCPUProcessor().applyRGBA(result)
    if premultiplied:
        result[...,:3] *= alpha
    result[...,3:4] = alpha
    return result


def _float_import_source(image, premultiplied):
    """Enter float storage before premultiplying integer straight-alpha data.

    Qt's integer-to-float conversion can pass straight images through an
    integer premultiplied intermediate. At low coverage that loses native
    color samples, even when the requested destination is straight float.
    Keep the integer channel grid and normalize directly into owned float
    storage before either color conversion or composition.
    """
    storage = (QImage.Format_RGBA32FPx4_Premultiplied if premultiplied
               else QImage.Format_RGBA32FPx4)
    if premultiplied or image.format() in (
            QImage.Format_RGBX16FPx4, QImage.Format_RGBA16FPx4,
            QImage.Format_RGBX32FPx4, QImage.Format_RGBA32FPx4):
        return image.convertToFormat(storage)
    wide = image_has_high_precision(image)
    source = image.convertToFormat(QImage.Format_RGBA64 if wide else QImage.Format_RGBA8888)
    dtype = np.uint16 if wide else np.uint8
    rows = np.frombuffer(source.constBits(), dtype).reshape(
        source.height(), source.bytesPerLine()//np.dtype(dtype).itemsize)
    channels = rows[:, :source.width()*4].reshape(source.height(), source.width(), 4)
    result = QImage(source.width(), source.height(), storage)
    if result.isNull():
        raise MemoryError('Could not allocate the float source image')
    output = np.frombuffer(result.bits(), np.float32).reshape(
        result.height(), result.bytesPerLine()//4)
    values = output[:, :result.width()*4].reshape(result.height(), result.width(), 4)
    np.divide(channels, np.float32(65535 if wide else 255), out=values)
    result.setColorSpace(image.colorSpace())
    return result


def import_image(image, contract, *, source_space='srgb'):
    """Preserve source precision and convert straight color before composition.

    An embedded ICC profile takes precedence over the untagged-source default.
    Legacy documents deliberately keep their original decoding/color behavior.
    """
    if not contract.floating or image.isNull():
        return image.convertToFormat(contract.image_format)
    premultiplied = image.format().name.endswith('_Premultiplied')
    storage = (QImage.Format_RGBA32FPx4_Premultiplied if premultiplied
               else QImage.Format_RGBA32FPx4)
    source = _float_import_source(image, premultiplied)
    profile = source.colorSpace()
    if profile == QColorSpace(QColorSpace.SRgb):
        source_space = 'srgb'
    elif profile == QColorSpace(QColorSpace.SRgbLinear):
        source_space = 'linear_srgb'
    elif profile.isValid():
        source = source.convertedToColorSpace(QColorSpace(QColorSpace.SRgb)).convertToFormat(storage)
        if source.isNull():
            raise ValueError('Could not convert the embedded image color profile')
        source_space = 'srgb'
    rows = np.frombuffer(source.constBits(), np.float32).reshape(source.height(), source.bytesPerLine()//4)
    values = rows[:, :source.width()*4].reshape(source.height(), source.width(), 4)
    pixels = transform_pixels(values, source_space, contract.working_space,
                              contract=contract, premultiplied=premultiplied)
    if not premultiplied:
        pixels[..., :3] *= pixels[..., 3:4]
    pixels[..., :3] *= (pixels[..., 3:4] > 0.)
    return working_image(pixels, contract)


def _bounded_output_pixels(pixels):
    """Clip finite display/export channels to their representable coverage."""
    result = np.array(pixels, dtype=np.float32, copy=True, order='C')
    np.nan_to_num(result, copy=False, nan=0., posinf=1., neginf=0.)
    np.clip(result[..., 3], 0., 1., out=result[..., 3])
    np.clip(result[..., :3], 0., result[..., 3:4], out=result[..., :3])
    return result


def display_image(image, contract):
    if not contract.floating:
        return QImage(image)
    pixels = transform_pixels(premultiplied_pixels(image),contract.working_space,'srgb',contract=contract,display=True)
    # Quantize once at the presentation edge. Float buffers remain reusable.
    result = working_image(_bounded_output_pixels(pixels), LEGACY_PIXELS)
    if not contract.ocio_config:
        result.setColorSpace(QColorSpace(QColorSpace.SRgb))
    return result


def export_image(image, contract, *, high_precision=True):
    if not contract.floating or image.isNull():
        return QImage(image)
    pixels = transform_pixels(premultiplied_pixels(image),contract.working_space,contract.export_space,contract=contract)
    pixels = _bounded_output_pixels(pixels)
    if high_precision:
        # Quantize straight channels once. Qt's conversion through an integer
        # premultiplied intermediate loses low-alpha native color samples.
        alpha = pixels[..., 3:4]
        np.divide(pixels[..., :3], alpha, out=pixels[..., :3], where=alpha > 0.)
        np.multiply(pixels, np.float32(65535), out=pixels)
        np.rint(pixels, out=pixels)
        channels = pixels.astype(np.uint16)
        height, width = channels.shape[:2]
        result = QImage(width, height, QImage.Format_RGBA64)
        if result.isNull():
            raise MemoryError('Could not allocate the high precision export image')
        rows = np.frombuffer(result.bits(), np.uint16).reshape(height, result.bytesPerLine()//2)
        rows[:, :width*4].reshape(height, width, 4)[:] = channels
    else:
        result = working_image(pixels, LEGACY_PIXELS)
    if contract.export_space in ('srgb', 'linear_srgb'):
        result.setColorSpace(QColorSpace(QColorSpace.SRgbLinear if contract.export_space == 'linear_srgb' else QColorSpace.SRgb))
    return result
