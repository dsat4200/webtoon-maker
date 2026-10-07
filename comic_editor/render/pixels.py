"""Versioned pixel contracts and explicit, cached color transforms.

Version one preserves encoded-sRGB RGBA8 stage semantics. Version two keeps
working images at float precision; color transforms always operate on straight
RGB and reapply the original coverage. Presentation/export are explicit edges.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from pathlib import Path
import tempfile
from threading import RLock

import numpy as np
from PySide6.QtGui import QColor, QColorSpace, QImage
from comic_editor.core.pixel_arrays import (
    normalized_bytes, truncated_bytes, image_has_high_precision, native_rgba_pixels,
)
from comic_editor.core.pixel_contract import PixelContract, LEGACY_PIXELS, FLOAT_PIXELS


_contract = ContextVar('render_pixel_contract', default=LEGACY_PIXELS)
_environment = ContextVar('render_color_environment', default=None)
_color_environments = OrderedDict()
_color_lock = RLock()
_color_capture_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='color-resources')


@dataclass(frozen=True)
class ColorEnvironment:
    """Captured color resources; callers treat the OCIO objects as read-only."""
    signature: tuple
    config: object = None
    context: object = None


def current_contract():
    return _contract.get()


def scoped_color_environment(contract):
    captured = _environment.get()
    return captured[1] if captured is not None and captured[0] == contract.ocio_config else None


def schedule_color_environment(contract):
    """No editor state crosses the serial color-resource owner."""
    def capture():
        from comic_editor.render.admission import RENDER_ADMISSION
        with RENDER_ADMISSION.reserve('color-resources', 32 * 1024 * 1024, priority=0):
            for attempt in range(3):
                try:
                    environment = capture_color_environment(contract)
                    # Compile the authored, display and output edges on this
                    # owner too. Presentation uses the captured processors and
                    # does not lazily load an external configuration or LUT.
                    with pixel_scope(contract, environment=environment):
                        for source, destination, display, view in (
                                ('srgb', contract.working_space, '', ''),
                                (contract.working_space, 'srgb', contract.display, contract.view),
                                (contract.working_space, contract.export_space, '', '')):
                            color_processor(contract.ocio_config, source, destination,
                                            display, view).getDefaultCPUProcessor()
                    return environment
                except ValueError as error:
                    if 'changed while' not in str(error) or attempt == 2:
                        raise
    return _color_capture_executor.submit(capture)


@contextmanager
def pixel_scope(contract, *, environment=None):
    environment = environment or capture_color_environment(contract)
    color_token = _environment.set((contract.ocio_config, environment))
    token = _contract.set(contract)
    try:
        yield contract
    finally:
        _contract.reset(token)
        _environment.reset(color_token)


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


@lru_cache(maxsize=32)
def _config_digest(path, stamp):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def color_environment(contract):
    """Identity of the color configuration used by derived source/display data."""
    captured = _environment.get()
    if captured is not None and captured[0] == contract.ocio_config:
        return captured[1].signature
    return capture_color_environment(contract).signature


def _file_identity(path):
    location = Path(path).resolve()
    stat = location.stat()
    stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    return (str(location), _config_digest(str(location), stamp))


@lru_cache(maxsize=8)
def _file_color_config(path, content):
    import PyOpenColorIO as ocio
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != content:
        raise ValueError('Color configuration changed while it was captured')
    config = ocio.Config.CreateFromStream(raw.decode('utf-8-sig'))
    config.setWorkingDir(str(Path(path).parent))
    return config


def _visit_config_transforms(config, visit):
    """Visit every resource-bearing transform, including inactive color spaces."""
    import PyOpenColorIO as ocio
    for space in list(config.getColorSpaces(ocio.SEARCH_REFERENCE_SPACE_ALL, ocio.COLORSPACE_ALL)):
        for direction in (ocio.COLORSPACE_DIR_TO_REFERENCE, ocio.COLORSPACE_DIR_FROM_REFERENCE):
            transform = space.getTransform(direction)
            if transform is not None:
                space.setTransform(visit(transform), direction)
        config.addColorSpace(space)
    for look in list(config.getLooks()):
        transform, inverse = look.getTransform(), look.getInverseTransform()
        if transform is not None:
            look.setTransform(visit(transform))
        if inverse is not None:
            look.setInverseTransform(visit(inverse))
        config.addLook(look)
    for view in list(config.getViewTransforms()):
        for direction in (ocio.VIEWTRANSFORM_DIR_TO_REFERENCE, ocio.VIEWTRANSFORM_DIR_FROM_REFERENCE):
            transform = view.getTransform(direction)
            if transform is not None:
                view.setTransform(visit(transform), direction)
        config.addViewTransform(view)
    for named in list(config.getNamedTransforms(ocio.NAMEDTRANSFORM_ALL)):
        for direction in (ocio.TRANSFORM_DIR_FORWARD, ocio.TRANSFORM_DIR_INVERSE):
            transform = named.getTransform(direction)
            if transform is not None:
                named.setTransform(visit(transform), direction)
        config.addNamedTransform(named)


def _external_files(config, context):
    import PyOpenColorIO as ocio
    found = set()
    def visit(transform):
        if isinstance(transform, ocio.FileTransform):
            found.add(context.resolveFileLocation(transform.getSrc()))
        elif isinstance(transform, ocio.GroupTransform):
            for child in transform:
                visit(child)
        return transform
    # OCIO getters provide editable copies; visit those in a detached config.
    _visit_config_transforms(copy.deepcopy(config), visit)
    return tuple(sorted(_file_identity(path) for path in found))


def _capture_external_config(signature, base, context):
    """Bake immutable LUT operations, avoiding OCIO's path-keyed file cache."""
    import PyOpenColorIO as ocio
    config = copy.deepcopy(base)
    files = dict(signature[3])
    with tempfile.TemporaryDirectory(prefix='webtoon-color-') as directory:
        pinned = {}
        for path, content in files.items():
            raw = Path(path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != content:
                raise ValueError('Color resource changed while it was captured')
            target = Path(directory) / (content + Path(path).suffix)
            target.write_bytes(raw)
            pinned[path] = str(target)
        def freeze(transform):
            if isinstance(transform, ocio.FileTransform):
                source = copy.deepcopy(transform)
                path = context.resolveFileLocation(source.getSrc())
                source.setSrc(pinned[path])
                # The resulting group owns LUT values, so no file survives the
                # capture. A worker can finish after original resources change.
                return config.getProcessor(context, source, ocio.TRANSFORM_DIR_FORWARD).createGroupTransform()
            if isinstance(transform, ocio.GroupTransform):
                return ocio.GroupTransform([freeze(child) for child in transform],
                                           transform.getDirection())
            return copy.deepcopy(transform)
        _visit_config_transforms(config, freeze)
    return ColorEnvironment(signature, config, context)


def capture_color_environment(contract):
    """Capture config, external LUT contents, and resolved context together."""
    if not contract.floating or not contract.ocio_config:
        return ColorEnvironment(('builtin-srgb-v1',))
    path, content = _file_identity(contract.ocio_config)
    base = _file_color_config(path, content)
    context = copy.deepcopy(base.getCurrentContext())
    context.loadEnvironment()
    signature = (path, content, context.getCacheID(), _external_files(base, context))
    with _color_lock:
        cached = _color_environments.pop(signature, None)
        if cached is None:
            cached = _capture_external_config(signature, base, context)
        _color_environments[signature] = cached
        while len(_color_environments) > 8:
            _color_environments.popitem(last=False)
        return cached


def _active_environment(path):
    captured = _environment.get()
    if captured is not None and captured[0] == path:
        return captured[1]
    return capture_color_environment(PixelContract(version=2, precision='float32', ocio_config=path))


def color_config(path=''):
    environment = _active_environment(path)
    return environment.config or _builtin_color_config()


@lru_cache(maxsize=1)
def _builtin_color_config():
    import PyOpenColorIO as ocio
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


def color_processor(path, source, destination, display='', view=''):
    environment = _active_environment(path)
    return _cached_color_processor(environment.signature, environment.config,
                                   environment.context, source, destination, display, view)


@lru_cache(maxsize=32)
def _cached_color_processor(identity, captured_config, context, source, destination, display='', view=''):
    import PyOpenColorIO as ocio
    config = captured_config or _builtin_color_config()
    if display:
        transform = ocio.DisplayViewTransform(src=source, display=display, view=view)
        return config.getProcessor(context, transform, ocio.TRANSFORM_DIR_FORWARD) if context is not None else config.getProcessor(transform)
    return config.getProcessor(context, source, destination) if context is not None else config.getProcessor(source,destination)


def gpu_color_processor(path, source, destination, display='', view=''):
    """Cache OCIO's GPU program and resources independently of tile execution.

    The descriptor owns its generated LUT values. Callers must upload them in
    the owning OpenGL context and treat this shared descriptor as read-only.
    """
    environment = _active_environment(path)
    return _cached_gpu_color_processor(environment.signature, environment.config,
                                       environment.context, source, destination, display, view)


@lru_cache(maxsize=32)
def _cached_gpu_color_processor(identity, captured_config, context, source, destination, display='', view=''):
    import PyOpenColorIO as ocio
    shader = ocio.GpuShaderDesc.CreateShaderDesc()
    shader.setLanguage(ocio.GPU_LANGUAGE_GLSL_4_0)
    shader.setFunctionName('ocio_process')
    shader.setResourcePrefix('ocio_')
    processor = _cached_color_processor(identity, captured_config, context, source, destination, display, view)
    processor.getDefaultGPUProcessor().extractGpuShaderInfo(shader)
    return shader


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


def working_rgba(color, contract=None):
    """Interpret authored colors as straight encoded sRGB, retaining coverage."""
    contract = contract or current_contract()
    values = np.array(QColor(color).getRgbF(), np.float32).reshape(1, 1, 4)
    if contract.floating:
        values = transform_pixels(values, 'srgb', contract.working_space,
                                  contract=contract, premultiplied=False)
    return values[0, 0]


def working_color(color, contract=None):
    """Convert a document color at a QPainter source edge, never mask data."""
    contract = contract or current_contract()
    if not contract.floating or contract.working_space == 'srgb':
        return QColor(color)
    return QColor.fromRgbF(*map(float, working_rgba(color, contract)))


def import_image(image, contract, *, source_space='srgb'):
    """Preserve source precision and convert straight color before composition.

    An embedded ICC profile takes precedence over the untagged-source default.
    Legacy documents deliberately keep their original decoding/color behavior.
    """
    if not contract.floating or image.isNull():
        return image.convertToFormat(contract.image_format)
    values, premultiplied = native_rgba_pixels(image)
    profile = image.colorSpace()
    if profile == QColorSpace(QColorSpace.SRgb):
        source_space = 'srgb'
    elif profile == QColorSpace(QColorSpace.SRgbLinear):
        source_space = 'linear_srgb'
    elif profile.isValid():
        # Enter float storage by copying source channels directly, before Qt
        # applies an ICC transform. Converting the integer image first can lose
        # straight RGB at low alpha through a premultiplied integer temporary.
        source = working_image(values, FLOAT_PIXELS)
        if not premultiplied:
            source.reinterpretAsFormat(QImage.Format_RGBA32FPx4)
        source.setColorSpace(profile)
        source = source.convertedToColorSpace(QColorSpace(QColorSpace.SRgb))
        if source.isNull():
            raise ValueError('Could not convert the embedded image color profile')
        values, premultiplied = native_rgba_pixels(source)
        source_space = 'srgb'
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
    if not contract.floating or image.isNull():
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
    # Integer output stores straight RGB. Converting a premultiplied float
    # image through Qt's associated integer temporary can erase low-alpha RGB,
    # even though the final PNG has enough precision to represent it. Divide
    # in working precision and quantize once into owned straight storage.
    np.divide(pixels[..., :3], pixels[..., 3:4], out=pixels[..., :3],
              where=pixels[..., 3:4] > 0.)
    dtype, maximum, format = ((np.uint16, 65535., QImage.Format_RGBA64)
                               if high_precision else
                               (np.uint8, 255., QImage.Format_RGBA8888))
    values = np.floor(pixels * np.float32(maximum) + np.float32(.5)).astype(dtype)
    height, width = values.shape[:2]
    result = QImage(width, height, format)
    if result.isNull():
        raise MemoryError('Could not allocate the export image')
    rows = np.frombuffer(result.bits(), dtype).reshape(height, result.bytesPerLine() // np.dtype(dtype).itemsize)
    rows[:, :width*4].reshape(height, width, 4)[:] = values
    if contract.export_space in ('srgb', 'linear_srgb'):
        result.setColorSpace(QColorSpace(QColorSpace.SRgbLinear if contract.export_space == 'linear_srgb' else QColorSpace.SRgb))
    return result
