"""Tiled, premultiplied circular integration in document coordinates."""
from __future__ import annotations

import math
import numpy as np
from scipy.ndimage import map_coordinates
from PySide6.QtCore import QPointF


class RadialRenderCancelled(Exception):
    pass


def _map(transform, x, y):
    denominator = transform.m13()*x + transform.m23()*y + transform.m33()
    denominator = np.where(np.abs(denominator) < 1e-12, np.nan, denominator)
    return ((transform.m11()*x+transform.m21()*y+transform.m31())/denominator,
            (transform.m12()*x+transform.m22()*y+transform.m32())/denominator)


def radial_blur(original, center, angle, world_to_image, *, cancelled=None, spacing=.75,
                output_shape=None, output_origin=(0., 0.), output_to_world=None):
    """Average a symmetric angular shutter; extra memory is bounded per tile.

    Sample density follows output-pixel arc length, not an arbitrary fixed
    sample cap. Out-of-source samples are transparent, never clamped edges.
    Coordinates refer to pixel centers through the complete projective mapping.
    A transient caller may supply an independent output-center mapping;
    the default native input/output grid and arithmetic remain unchanged.
    """
    angle = np.clip(np.asarray(angle, dtype=np.float32), 0., 360.)
    if (float(np.max(angle)) <= 1e-6 and output_shape is None
            and output_origin == (0., 0.) and output_to_world is None):
        return original.copy()
    inverse, valid = world_to_image.inverted()
    if not valid:
        return original.copy()
    shared_rgba = _shared_rgba_input(original)
    from comic_editor.ui.shape_contours import transform_stretch
    height, width = output_shape or original.shape[:2]
    result = np.zeros((height, width, 4), dtype=original.dtype)
    center_x, center_y = center
    density_mapping = world_to_image
    if output_to_world is not None:
        density_mapping, valid_output = output_to_world.inverted()
        if not valid_output:
            raise ValueError("Radial output mapping is singular")
    for top in range(0, height, 96):
        for left in range(0, width, 96):
            if cancelled is not None and cancelled():
                raise RadialRenderCancelled()
            bottom, right = min(top+96, height), min(left+96, width)
            yy, xx = np.mgrid[top:bottom, left:right].astype(np.float64)
            if output_to_world is None:
                world_x, world_y = _map(inverse, xx+.5+output_origin[0], yy+.5+output_origin[1])
            else:
                world_x, world_y = _map(output_to_world, xx+.5, yy+.5)
            dx, dy = world_x-center_x, world_y-center_y
            angles = angle if angle.ndim == 0 else angle[top:bottom, left:right]
            radians = angles*(math.pi/180)
            radius = float(np.nanmax(np.hypot(dx, dy)))
            # Conservative affine/projective scale over this tile's world box.
            from PySide6.QtCore import QRectF
            bounds = QRectF(QPointF(float(np.nanmin(world_x)), float(np.nanmin(world_y))),
                            QPointF(float(np.nanmax(world_x)), float(np.nanmax(world_y))))
            stretch = transform_stretch(density_mapping, bounds)
            count = max(2, math.ceil(radius*float(np.max(radians))*stretch/spacing))
            tile = result[top:bottom, left:right]
            for sample in range(count):
                if sample % 16 == 0 and cancelled is not None and cancelled():
                    raise RadialRenderCancelled()
                theta = radians*((sample+.5)/count-.5)
                sine, cosine = np.sin(theta), np.cos(theta)
                sx, sy = _map(world_to_image, center_x+dx*cosine-dy*sine,
                             center_y+dx*sine+dy*cosine)
                coordinates = np.stack((sy-.5, sx-.5))
                tile += _rgba_linear_sample(original,coordinates,shared_rgba)
            tile /= count
            # A zero-angle mask region is an exact identity, including its
            # floating-point values, regardless of neighboring sample counts.
            zero = np.broadcast_to(angles <= 1e-6, tile.shape[:2])
            if np.any(zero):
                if output_to_world is None:
                    coordinates = np.stack((yy+output_origin[1], xx+output_origin[0]))
                else:
                    identity_x, identity_y = _map(world_to_image, world_x, world_y)
                    coordinates = np.stack((identity_y-.5, identity_x-.5))
                identity = _rgba_linear_sample(original,coordinates,shared_rgba)
                tile[zero] = identity[zero]
    # Roundoff must never create invalid premultiplied output.
    result = np.clip(result, 0., 1.)
    result[..., :3] = np.minimum(result[..., :3], result[..., 3:4])
    return result


_C_ABI = 0x5247424142490001
_C_SOURCE_SHA256 = '877c58e04fd1b2976f4b77868158e06be136f16384807ae1292f58a80f7dcfa8'
_C_BUILD_CONTRACT = 'scalar-double-separate-y-x-corners-00-01-10-11-channel-0-1-2-3-f32-unrolled-no-fastmath-v2'
_C_UNTRIED = object()
_C_LIBRARY = _C_UNTRIED
from threading import RLock as _C_RLock
_C_LIBRARY_LOCK = _C_RLock()


def _sampler_library():
    """Optional verified owned binary, one loaded handle, no pixel cache.

    Missing/unsupported/failed component always retains original SciPy. No
    runtime compiler/download/install. Binary and source metadata are shipped
    together only after independent native/platform correctness acceptance.
    """
    global _C_LIBRARY
    with _C_LIBRARY_LOCK:
        if _C_LIBRARY is not _C_UNTRIED:
            return _C_LIBRARY
        _C_LIBRARY = None
        import ctypes
        import hashlib
        import json
        from pathlib import Path
        import sys
        if sys.platform != 'win32' or sys.byteorder != 'little' or ctypes.sizeof(ctypes.c_void_p) != 8:
            return None
        try:
            home = Path(__file__).resolve().parent
            record = json.loads((home/'radial_rgba_build.json').read_text('utf8'))
            source = home/'radial_rgba_linear.c'
            binary = home/'radial_rgba_win64.dll'
            digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            if (record['abi'] != hex(_C_ABI) or record['platform'] != 'win-amd64'
                    or record['build_contract'] != _C_BUILD_CONTRACT
                    or record['source_sha256'] != _C_SOURCE_SHA256
                    or digest(source) != _C_SOURCE_SHA256
                    or digest(binary) != record['binary_sha256']):
                return None
            library = ctypes.CDLL(str(binary))
            library.radial_rgba_abi.argtypes = []
            library.radial_rgba_abi.restype = ctypes.c_uint64
            if library.radial_rgba_abi() != _C_ABI:
                return None
            library.radial_rgba_linear.argtypes = [ctypes.POINTER(ctypes.c_float),ctypes.c_uint64,ctypes.c_uint64,
                ctypes.POINTER(ctypes.c_double),ctypes.POINTER(ctypes.c_double),ctypes.c_uint64,ctypes.POINTER(ctypes.c_float)]
            library.radial_rgba_linear.restype = ctypes.c_int
            _C_LIBRARY = library
        except Exception:
            # A broken optional component must not break document rendering.
            _C_LIBRARY = None
        return _C_LIBRARY


def _shared_rgba_input(original):
    from scipy import __version__
    if (__version__ != '1.18.0' or type(original) is not np.ndarray
            or original.dtype != np.dtype(np.float32) or original.ndim != 3
            or original.shape[2] != 4 or min(original.shape[:2]) <= 0
            or max(original.shape[:2]) > 2147483647
            or not original.flags.c_contiguous or not original.flags.aligned
            or _sampler_library() is None):
        return False
    # Charge the one complete native input scan to the whole kernel call.
    flat = original.reshape(-1)
    return all(np.isfinite(flat[start:start+524288]).all()
               for start in range(0,flat.size,524288))


def _rgba_linear_sample(original,coordinates,eligible):
    """Optional owned C-ABI sample, original SciPy for every unsupported case."""
    library = _sampler_library() if eligible else None
    if (library is not None and type(coordinates) is np.ndarray
            and coordinates.dtype == np.dtype(np.float64)
            and coordinates.ndim == 3 and coordinates.shape[0] == 2
            and min(coordinates.shape[1:]) > 0
            and coordinates.shape[1]*coordinates.shape[2] <= 96*96
            and coordinates.flags.c_contiguous and coordinates.flags.aligned
            and np.isfinite(coordinates).all() and not np.any(np.abs(coordinates) >= 2**52)):
        import ctypes
        output = np.empty((*coordinates.shape[1:],4),dtype=np.float32)
        points = coordinates.shape[1]*coordinates.shape[2]
        # Every array and native handle remains strongly owned by this call.
        # CDLL releases the GIL; no pointer is retained by the C function.
        source_pointer = original.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        y_pointer = coordinates.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
        x_pointer = ctypes.cast(coordinates.ctypes.data+points*8,ctypes.POINTER(ctypes.c_double))
        output_pointer = output.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        code = library.radial_rgba_linear(source_pointer,original.shape[0],original.shape[1],
                                         y_pointer,x_pointer,points,output_pointer)
        if code == 0:
            return output
        # A rejected C batch is never returned/admitted, even if its temporary
        # output is uninitialized; ordinary SciPy computes the final sample.
    return np.stack([map_coordinates(original[...,channel],coordinates,
        order=1,mode='grid-constant',cval=0.,prefilter=False) for channel in range(4)],axis=-1)
