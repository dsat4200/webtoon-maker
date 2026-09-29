"""Sparse raster backend for portable brush strokes.

Geometry/input live in brush_stroke; this module owns image sampling and pixels.
Stroke coverage is accumulated separately from stroke opacity, so overlapping
dabs do not turn a translucent pen opaque. Only touched tiles are retained.

Wet transport, texture response curves and watercolor edges are independent
implementations: their exact CSP numerical behavior has not been measured.
"""
from __future__ import annotations

import base64
import colorsys
import math
import random
import struct
import tempfile
import zlib
from collections import OrderedDict
from dataclasses import replace

import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from scipy.ndimage import gaussian_filter, maximum_filter, minimum_filter

from .brushes import BrushDab, BrushDefinition, BrushInput, BrushTip
from .brush_stroke import BrushStroke
from .brush_correction import corrected_samples, resolved_taper, stroke_length


def image_pixels(image: QImage) -> np.ndarray:
    """Return independent, straight RGBA floats, with no Qt-buffer lifetime."""
    rgba = image.convertToFormat(QImage.Format_RGBA8888)
    pixels = (np.frombuffer(rgba.constBits(), np.uint8)
            .reshape(rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4]
            .reshape(rgba.height(), rgba.width(), 4).astype(np.float32))
    pixels /= 255.
    return pixels


def pixels_image(rgba: np.ndarray) -> QImage:
    pixels = np.ascontiguousarray(np.rint(np.clip(rgba, 0, 1)*255), dtype=np.uint8)
    h, w = pixels.shape[:2]
    return QImage(pixels.data, w, h, w*4, QImage.Format_RGBA8888).copy().convertToFormat(
        QImage.Format_ARGB32_Premultiplied)


def _premultiply(rgba):
    result = rgba.copy()
    result[..., :3] *= result[..., 3:4]
    return result


def _straight(rgba):
    result = rgba.copy()
    result[..., :3] /= np.maximum(result[..., 3:4], 1e-8)
    return np.clip(result, 0, 1)


class _ImageMaterial:
    """Own compact pixels; convert only a sampled region to floating point.

    A large original stamp can exceed 100 MiB even in RGBA8. Expanding its
    entire image to floats per dab is unnecessary and can exhaust memory.
    Conversion before interpolation preserves the existing pixel equations.
    """
    def __init__(self, image, *, premultiplied=False, gray=False):
        self.image = image.convertToFormat(QImage.Format_RGBA8888)
        self.pixels = (np.frombuffer(self.image.constBits(),np.uint8)
                       .reshape(self.image.height(),self.image.bytesPerLine())[:, :self.image.width()*4]
                       .reshape(self.image.height(),self.image.width(),4))
        self.pixels.setflags(write=False)
        self.premultiplied, self.gray = premultiplied, gray
        self.shape = self.pixels.shape[:2] if gray else self.pixels.shape
        self.nbytes = self.pixels.nbytes

    def __getitem__(self, index):
        result = self.pixels[index].astype(np.float32)
        result /= 255.
        if self.gray:
            return np.mean(result[..., :3],axis=-1)*result[..., 3]+(1-result[..., 3])
        if self.premultiplied:
            result[..., :3] *= result[..., 3:4]
        return result


class _CompressedImageMaterial:
    """Lossless row blocks for huge, highly compressible original tip images.

    Sampling never needs the complete decoded image. Each read expands at
    most one 256 KiB row block at a time, then converts selected pixels using
    the same floating-point operations as _ImageMaterial. No decoded blocks
    remain resident. A full image is reconstructed only for an exact new mip.
    """
    def __init__(self, image, *, premultiplied=False, gray=False):
        rgba = image.convertToFormat(QImage.Format_RGBA8888)
        self.width, self.height = rgba.width(), rgba.height()
        self.block_rows = max(1, min(64, (256*1024)//(self.width*4)))
        pixels = (np.frombuffer(rgba.constBits(), np.uint8)
                  .reshape(self.height, rgba.bytesPerLine())[:, :self.width*4]
                  .reshape(self.height, self.width, 4))
        self.blocks = tuple(zlib.compress(pixels[y:y+self.block_rows].tobytes(), 1)
                            for y in range(0, self.height, self.block_rows))
        self.premultiplied, self.gray = premultiplied, gray
        self.shape = (self.height, self.width) if gray else (self.height, self.width, 4)
        self.nbytes = sum(len(block)+41 for block in self.blocks)

    @property
    def image(self):
        """Reconstruct original RGBA8 for Qt's unchanged direct mip resize."""
        image = QImage(self.width, self.height, QImage.Format_RGBA8888)
        if image.isNull():
            raise MemoryError("Not enough memory to reconstruct the original brush material")
        pixels = np.frombuffer(image.bits(), np.uint8).reshape(self.height, self.width, 4)
        for index, block in enumerate(self.blocks):
            y = index*self.block_rows
            rows = min(self.block_rows, self.height-y)
            pixels[y:y+rows] = np.frombuffer(zlib.decompress(block), np.uint8).reshape(rows, self.width, 4)
        return image

    def __getitem__(self, index):
        y, x = index if isinstance(index, tuple) else (index, slice(None))
        y_slice, x_slice = isinstance(y, slice), isinstance(x, slice)
        y = np.arange(self.height)[y] if y_slice else np.asarray(y)
        x = np.arange(self.width)[x] if x_slice else np.asarray(x)
        if y_slice and x_slice:
            y, x = y[:, None], x[None, :]
        y, x = np.broadcast_arrays(y, x)
        shape = y.shape
        y, x = y.ravel(), x.ravel()
        y, x = np.where(y < 0, y+self.height, y), np.where(x < 0, x+self.width, x)
        if np.any((y < 0) | (y >= self.height) | (x < 0) | (x >= self.width)):
            raise IndexError("Brush material sample is outside its registered image")
        block_indices = y//self.block_rows
        selected = np.empty((y.size, 4), np.uint8)
        for block_index in np.unique(block_indices):
            start = int(block_index)*self.block_rows
            rows = min(self.block_rows, self.height-start)
            pixels = np.frombuffer(zlib.decompress(self.blocks[int(block_index)]), np.uint8).reshape(rows, self.width, 4)
            mask = block_indices == block_index
            selected[mask] = pixels[y[mask]-start, x[mask]]
        result = selected.reshape((*shape, 4)).astype(np.float32)
        result /= 255.
        if self.gray:
            return np.mean(result[..., :3], axis=-1)*result[..., 3]+(1-result[..., 3])
        if self.premultiplied:
            result[..., :3] *= result[..., 3:4]
        return result


class _MaterialCache:
    """A shared byte budget, not a count of potentially enormous images."""
    def __init__(self, budget=128*1024*1024):
        self.budget, self.bytes = budget, 0
        self.values = OrderedDict()
        self._png_refs = {}

    def _evict_oldest(self):
        key, (_, cost) = self.values.popitem(last=False)
        self.bytes -= cost
        png = key[0]
        remaining = self._png_refs[png][1]-1
        if remaining:
            self._png_refs[png] = png, remaining
        else:
            del self._png_refs[png]
            self.bytes -= len(png)

    def _reserve(self, cost, png):
        while self.values and self.bytes+cost+(0 if png in self._png_refs else len(png)) > self.budget:
            self._evict_oldest()

    def _cached_image(self, png, mip):
        for premultiplied, gray in ((False, False), (True, False), (False, True), (True, True)):
            key = png, premultiplied, gray, mip
            if key in self.values:
                result, cost = self.values.pop(key)
                self.values[key] = result, cost
                return result.image
        return None

    def get(self, png, premultiplied=False, *, gray=False, mip=0):
        # Python caches a string's hash. Rehashing megabytes of PNG with a
        # cryptographic hash for every stamp made big material brushes stall.
        reference = self._png_refs.get(png)
        if reference is not None:
            png = reference[0]
        key = png, premultiplied, gray, mip
        if key in self.values:
            result, cost = self.values.pop(key)
            self.values[key] = result, cost
            return result
        image = self._cached_image(png, mip)
        scale_original = False
        if image is None:
            image = self._cached_image(png, 0) if mip else None
            if image is None:
                data = base64.b64decode(png, validate=True)
                if len(data) >= 24 and data[:8] == b'\x89PNG\r\n\x1a\n':
                    width,height = struct.unpack_from('>II',data,16)
                    # Reserve the transient decode before allocating it. Mips
                    # sharing one source string charge that string only once.
                    self._reserve(width*height*4, png)
                image = QImage.fromData(data, "PNG")
                if image.isNull():
                    raise ValueError("Brush material is not a readable PNG")
            scale_original = bool(mip)
        if scale_original:
            from PySide6.QtCore import Qt
            # Always resize the full original, never another mip: chained
            # smoothing changes source pixels and brush texture detail.
            image = image.scaled(max(1,image.width()//(2**mip)),
                                 max(1,image.height()//(2**mip)),
                                 Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            if image.isNull():
                raise MemoryError("Not enough memory to resize the original brush material")
        material_class = (_CompressedImageMaterial
                          if image.width()*image.height()*4 >= 8*1024*1024
                          and image.width()*4 <= 256*1024
                          and len(png) < image.width()*image.height()
                          else _ImageMaterial)
        result = material_class(image,premultiplied=premultiplied,gray=gray)
        cost = result.nbytes
        if cost+len(png) <= self.budget:
            self._reserve(cost, png)
            self.values[key] = result, cost
            self.bytes += cost
            if png not in self._png_refs:
                self.bytes += len(png)
                self._png_refs[png] = png, 0
            self._png_refs[png] = png, self._png_refs[png][1]+1
        return result

    def clear(self):
        self.values.clear()
        self._png_refs.clear()
        self.bytes = 0


_material_cache = _MaterialCache()


def _material(png):
    return _material_cache.get(png)


def _premultiplied_material(png, mip=0):
    return _material_cache.get(png, True, mip=mip)


def _sample(array, x, y, *, wrap=False, nearest=False):
    """Bilinear sampling with transparent exterior or repeating texture edges."""
    h, w = array.shape[:2]
    if nearest:
        ix, iy = np.floor(x+.5).astype(np.int64), np.floor(y+.5).astype(np.int64)
        if wrap:
            return array[iy % h, ix % w]
        valid = (ix >= 0) & (iy >= 0) & (ix < w) & (iy < h)
        result = array[np.clip(iy, 0, h-1), np.clip(ix, 0, w-1)]
        return result * valid[..., None] if result.ndim > valid.ndim else result * valid
    ix, iy = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = x-ix, y-iy
    corner_pixels = None
    if isinstance(array, _CompressedImageMaterial):
        # Read the four interpolation corners together, so a lossless row
        # block is decompressed once for this sample instead of four times.
        xx = np.stack((ix, ix+1, ix, ix+1))
        yy = np.stack((iy, iy, iy+1, iy+1))
        corner_pixels = array[yy % h, xx % w] if wrap else array[np.clip(yy, 0, h-1), np.clip(xx, 0, w-1)]
    result = None
    for corner, (dx, dy, weight) in enumerate(((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                                               (0, 1, (1-fx)*fy), (1, 1, fx*fy))):
        xx, yy = ix+dx, iy+dy
        if wrap:
            value = corner_pixels[corner] if corner_pixels is not None else array[yy % h, xx % w]
        else:
            valid = (xx >= 0) & (yy >= 0) & (xx < w) & (yy < h)
            value = corner_pixels[corner] if corner_pixels is not None else array[np.clip(yy, 0, h-1), np.clip(xx, 0, w-1)]
            weight = weight * valid
        if value.ndim > weight.ndim:
            weight = weight[..., None]
        value = value * weight
        result = value if result is None else result + value
    return result.astype(np.float32)


def _blend_rgb(back, front, mode):
    mode = mode.lower().replace(" ", "_")
    if mode == "multiply":
        return back * front
    if mode in {"darken", "compare_density"}:
        return np.minimum(back, front)
    if mode == "lighten":
        return np.maximum(back, front)
    if mode in {"darker_color", "lighter_color"}:
        # These choose a complete color, unlike per-channel Darken/Lighten.
        weight = np.asarray((.3,.59,.11),dtype=np.float32)
        take_front = np.sum(front*weight,axis=-1) < np.sum(back*weight,axis=-1)
        if mode == "lighter_color":
            take_front = ~take_front
        return np.where(take_front[...,None],front,back)
    if mode == "screen":
        return back + front - back*front
    if mode == "overlay":
        return np.where(back <= .5, 2*back*front, 1-2*(1-back)*(1-front))
    if mode == "hard_light":
        return np.where(front <= .5, 2*back*front, 1-2*(1-back)*(1-front))
    if mode == "soft_light":
        d = np.where(back <= .25, ((16*back-12)*back+4)*back, np.sqrt(back))
        return np.where(front <= .5, back-(1-2*front)*back*(1-back), back+(2*front-1)*(d-back))
    if mode in {"color_dodge", "glow_dodge"}:
        return np.where(front >= 1, 1, np.minimum(1, back/np.maximum(1-front, 1e-7)))
    if mode in {"color_burn", "burn"}:
        return np.where(front <= 0, 0, 1-np.minimum(1, (1-back)/np.maximum(front, 1e-7)))
    if mode == "linear_burn":
        return np.maximum(0, back+front-1)
    if mode in {"add", "add_glow", "add_(glow)"}:
        return np.minimum(1, back+front)
    if mode == "subtract":
        return np.maximum(0, back-front)
    if mode == "difference":
        return np.abs(back-front)
    if mode == "exclusion":
        return back+front-2*back*front
    if mode == "hard_mix":
        return (back+front >= 1).astype(np.float32)
    if mode == "linear_light":
        return np.clip(back+2*front-1, 0, 1)
    if mode == "pin_light":
        return np.where(front <= .5, np.minimum(back, 2*front), np.maximum(back, 2*front-1))
    if mode == "vivid_light":
        return np.where(front <= .5,
                        1-np.minimum(1, (1-back)/np.maximum(2*front, 1e-7)),
                        np.minimum(1, back/np.maximum(2*(1-front), 1e-7)))
    if mode == "divide":
        return np.minimum(1, back/np.maximum(front, 1e-7))
    if mode in {"hue", "saturation", "color", "luminosity"}:
        # Nonseparable blend modes, using the PDF/W3C luminance convention.
        def lum(c):
            return c[..., 0]*.3+c[..., 1]*.59+c[..., 2]*.11
        def set_lum(c, value):
            c = c + (value-lum(c))[..., None]
            l, low, high = lum(c), c.min(axis=-1), c.max(axis=-1)
            c = np.where((low < 0)[..., None], l[..., None] +
                         (c-l[..., None]) * (l/np.maximum(l-low, 1e-7))[..., None], c)
            return np.where((high > 1)[..., None], l[..., None] +
                            (c-l[..., None])*((1-l)/np.maximum(high-l, 1e-7))[..., None], c)
        def set_sat(c, sat):
            low, high = c.min(axis=-1), c.max(axis=-1)
            return (c-low[..., None])*(sat/np.maximum(high-low, 1e-7))[..., None]
        if mode == "color":
            return set_lum(front, lum(back))
        if mode == "luminosity":
            return set_lum(back, lum(front))
        if mode == "hue":
            return set_lum(set_sat(front, back.max(axis=-1)-back.min(axis=-1)), lum(back))
        return set_lum(set_sat(back, front.max(axis=-1)-front.min(axis=-1)), lum(back))
    return front


def composite_pixels(destination, source, mode="normal"):
    """Composite premultiplied float RGBA arrays, including transparent pixels."""
    sa, da = source[..., 3:4], destination[..., 3:4]
    if mode in {"erase", "clear"}:
        return destination * (1-sa)
    if mode in {"background", "behind"}:
        return destination + source*(1-da)
    if mode == "normal":
        return source + destination*(1-sa)
    cs = source[..., :3]/np.maximum(sa, 1e-8)
    cb = destination[..., :3]/np.maximum(da, 1e-8)
    rgb = (source[..., :3]*(1-da) + destination[..., :3]*(1-sa)
           + _blend_rgb(cb, cs, mode)*sa*da)
    return np.concatenate((np.clip(rgb, 0, 1), sa+da*(1-sa)), axis=-1)


class _StrokePlane:
    def __init__(self, definition, tile_size):
        self.definition = definition
        self.tile_size = tile_size
        self.pixels: dict[tuple[int, int], np.ndarray] = OrderedDict()
        self.opacity: dict[tuple[int, int], np.ndarray] = {}
        self.texture_parameters = {}
        texture_dynamic=definition.dynamics.get("texture_density")
        active_dynamic=texture_dynamic is not None and (
            texture_dynamic.pressure or texture_dynamic.tilt or texture_dynamic.velocity or texture_dynamic.random < 1)
        active_taper=("texture_density" in definition.taper_parameters and
                      (definition.taper_start > 0 or definition.taper_end > 0))
        self.track_texture = bool(definition.texture and (active_dynamic or active_taper))
        self.budget = 32*1024*1024
        self.bytes = 0
        self.spilled = set()
        self._temporary = None
        self.previous_dab = None
        self.wet_pickup = None
        self.ribbon_points = []
        self.ribbon_normal = None
        self.ribbon_phase = 0.
        self.ribbon_cycle = 0
        self.ribbon_tip = 0
        self.ribbon_tip_cycle = -1
        self.ribbon_flips = None
        self.random = random.Random(919)
        self.random_cycle = []

    def allocate(self, key):
        if key in self.pixels:
            self.pixels.move_to_end(key)
        else:
            n = self.tile_size
            if key in self.spilled:
                with open(self._spill_path(key), "rb") as stream:
                    self.pixels[key] = np.load(stream, allow_pickle=False)
                    self.opacity[key] = np.load(stream, allow_pickle=False)
                    if self.track_texture:
                        self.texture_parameters[key] = np.load(stream, allow_pickle=False)
            else:
                self.pixels[key] = np.zeros((n, n, 4), np.float32)
                self.opacity[key] = np.zeros((n, n), np.float32)
                if self.track_texture:
                    self.texture_parameters[key] = np.zeros((n,n,2),np.float32)
                    self.texture_parameters[key][...,1] = 1
            self.bytes += self.pixels[key].nbytes+self.opacity[key].nbytes
            if self.track_texture:
                self.bytes += self.texture_parameters[key].nbytes
            while len(self.pixels) > 1 and self.bytes > self.budget:
                old_key, pixels = self.pixels.popitem(last=False)
                opacity = self.opacity.pop(old_key)
                self.bytes -= pixels.nbytes+opacity.nbytes
                parameters = self.texture_parameters.pop(old_key,None)
                if parameters is not None:
                    self.bytes -= parameters.nbytes
                if self._temporary is None:
                    self._temporary = tempfile.TemporaryDirectory(prefix="webtoon-brush-")
                with open(self._spill_path(old_key), "wb") as stream:
                    np.save(stream, pixels, allow_pickle=False)
                    np.save(stream, opacity, allow_pickle=False)
                    if parameters is not None:
                        np.save(stream,parameters,allow_pickle=False)
                self.spilled.add(old_key)
        return self.pixels[key], self.opacity[key]

    def _spill_path(self, key):
        return f"{self._temporary.name}/{key[0]}_{key[1]}.npy"

    def contains(self, key):
        return key in self.pixels or key in self.spilled

    def keys(self):
        return self.pixels.keys() | self.spilled

    def clear(self):
        self.pixels.clear()
        self.opacity.clear()
        self.texture_parameters.clear()
        self.spilled.clear()
        self.bytes = 0
        self.wet_pickup = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


class RasterBrushStroke:
    """One undoable stroke on one raster object in a TileStore.

    ``before`` belongs to the caller's undo transaction. Existing entries are
    never overwritten; every tile we modify is snapshotted before modification.
    ``defer_flush`` is for offscreen rendering: begin/add retain stroke pixels
    privately and finish publishes the final result once. Interactive callers
    keep the default so every input sample updates the visible canvas.
    """
    def __init__(self, tiles, object_id: str, definition: BrushDefinition,
                 color: QColor, before: dict, seed=0, *, selection_tile=None,
                 defer_flush=False, _path_length=None):
        self.tiles, self.object_id = tiles, object_id
        self.definition, self.before = definition, before
        self.seed=seed
        self.defer_flush=bool(defer_flush)
        self._stroke_color=QColor(color)
        self.color = np.asarray(color.getRgbF(), dtype=np.float32)
        self.sub_color = np.asarray(definition.sub_color, dtype=np.float32)/255.
        def needs_length(brush):
            percentage=brush.taper_mode == "percentage" and (brush.taper_start or brush.taper_end)
            spacing_end=("spacing" in brush.taper_parameters and brush.taper_end > 0
                         and brush.taper_mode != "fade")
            return percentage or spacing_end or (brush.dual is not None and needs_length(brush.dual))
        def has_ending(brush):
            return ((brush.taper_end > 0 and brush.taper_mode != "fade") or
                    (brush.dual is not None and has_ending(brush.dual)))
        corrected_replay=getattr(definition,"post_correction",0) > 0 or needs_length(definition)
        ending_preview=_path_length is None and has_ending(definition)
        self._replay_inputs = [] if (_path_length is None and (corrected_replay or ending_preview)) else None
        # Simple length endings replay raw input through each original scheduler.
        # This preserves independent secondary stabilization. Existing correction
        # replay retains its established shared, primary-stabilized path.
        self._replay_raw_inputs=ending_preview and not corrected_replay
        # Show the live path immediately; a short stroke must not remain blank
        # merely because its entire length is inside the pending ending region.
        def live_definition(brush):
            if _path_length is None:
                if brush.taper_mode == "percentage":
                    return replace(brush,taper_mode="length",taper_start=0.,taper_end=0.)
                if brush.taper_mode != "fade" and brush.taper_end > 0:
                    return replace(brush,taper_end=0.)
            return brush
        live=live_definition(definition)
        self.scheduler = BrushStroke(live, seed=seed, path_length=_path_length)
        self.main = _StrokePlane(live, tiles.tile_size)
        self.main.random.seed(seed ^ 919)
        self.secondary = None
        self.dual_scheduler = None
        if definition.dual is not None:
            second = live_definition(definition.dual)
            # The main brush owns post correction for the shared path. A
            # dormant secondary correction value must not disable its clock.
            second = replace(second,post_correction=0.,
                             continuous=second.continuous and definition.post_correction <= 0)
            if definition.dual_link_size:
                # Stored sizes preserve their ratio when the UI resizes both.
                second = replace(second, size=second.size)
            self.secondary = _StrokePlane(second, tiles.tile_size)
            self.secondary.random.seed(seed ^ 271828)
            self.dual_scheduler = BrushStroke(second, seed=seed ^ 271828, path_length=_path_length)
        self._base: dict[tuple[int, int], np.ndarray] = OrderedDict()
        self._original: dict[tuple[int, int], QImage | None] = {}
        self._base_budget = 32*1024*1024
        self._base_bytes = 0
        self._dirty = set()
        self._dirty_regions = {}
        self._bounds = QRectF()
        self._finished = False
        self._edge_finished = False
        self._wet_snapshot = None
        self._wet_stage_budget = 32*1024*1024
        self._main_alpha_cache = OrderedDict()
        self._texture_values = OrderedDict()
        self.selection_tile = selection_tile
        self._selection_masks = OrderedDict()

    @property
    def bounds(self):
        return QRectF(self._bounds)

    def begin(self, sample: BrushInput) -> QRectF:
        if self._finished:
            return QRectF()
        self._render_dabs(self.main, self.scheduler.begin(sample))
        if self._replay_inputs is not None:
            self._replay_inputs.append(sample.sanitized() if self._replay_raw_inputs else self.scheduler.last)
        if self.secondary is not None:
            self._render_dabs(self.secondary, self.dual_scheduler.begin(sample))
        return QRectF() if self.defer_flush else self._flush()

    def add(self, sample: BrushInput) -> QRectF:
        if self._finished:
            return QRectF()
        self._render_dabs(self.main, self.scheduler.add(sample))
        if self._replay_inputs is not None:
            self._replay_inputs.append(sample.sanitized() if self._replay_raw_inputs else self.scheduler.last)
        if self.secondary is not None:
            self._render_dabs(self.secondary, self.dual_scheduler.add(sample))
        return QRectF() if self.defer_flush else self._flush()

    def finish(self) -> QRectF:
        if self._finished:
            return QRectF()
        if self._replay_inputs is not None:
            return self._finish_replay()
        self._render_dabs(self.main, self.scheduler.finish())
        self._finish_ribbon(self.main)
        if self.secondary is not None:
            self._render_dabs(self.secondary, self.dual_scheduler.finish())
            self._finish_ribbon(self.secondary)
        self._finished = True
        self._edge_finished = True
        if self.definition.watercolor_edge > 0:
            self._dirty.update(self.main.keys())
            if self.secondary is not None:
                self._dirty.update(self.secondary.keys())
        result = self._flush()
        self._clear_working()
        return result

    def _clear_working(self):
        self.main.clear()
        if self.secondary is not None:
            self.secondary.clear()
        self._base.clear()
        self._base_bytes = 0
        self._original.clear()
        self._main_alpha_cache.clear()
        self._texture_values.clear()
        self._selection_masks.clear()
        self._dirty.clear()
        self._dirty_regions.clear()

    def _finish_replay(self):
        if self._replay_raw_inputs:
            samples=self._replay_inputs
            definition=self.definition
            path_length=self.scheduler.distance
            dual_path_length=self.dual_scheduler.distance if self.dual_scheduler else path_length
        else:
            samples=corrected_samples(self._replay_inputs,getattr(self.definition,"post_correction",0))
            path_length=stroke_length(samples)
            definition=resolved_taper(self.definition,path_length)
            dual_path_length=path_length
        self._replay_inputs=None
        previous_bounds=QRectF(self._bounds)
        # Restore our stroke's private baseline. The undo map may intentionally
        # contain older transaction snapshots, so it must never be used here.
        for key,image in self._original.items():
            self.tiles.set_tile(self.object_id,key,image)
        self._clear_working()
        self._finished=True
        replay=RasterBrushStroke(self.tiles,self.object_id,definition,self._stroke_color,
                                 self.before,seed=self.seed,selection_tile=self.selection_tile,
                                 defer_flush=self.defer_flush,
                                 _path_length=path_length)
        if replay.dual_scheduler is not None:
            replay.dual_scheduler.path_length=dual_path_length
        # No intermediate replay frame is visible. Accumulate directly and
        # publish once, avoiding a complete tile conversion for every sample.
        # Wet pickup reads immutable pre-stroke pigment; its private reservoir
        # retains stroke order independently of publishing to the tile store.
        for index,sample in enumerate(samples):
            stage=replay.scheduler.begin if index == 0 else replay.scheduler.add
            replay._render_dabs(replay.main,stage(sample))
            if replay.secondary is not None:
                stage=replay.dual_scheduler.begin if index == 0 else replay.dual_scheduler.add
                replay._render_dabs(replay.secondary,stage(sample))
        replay.finish()
        self._bounds=(replay.bounds if previous_bounds.isEmpty() else previous_bounds.united(replay.bounds))
        return QRectF(self._bounds)

    def _base_tile(self, key):
        if key not in self._base:
            if key not in self._original:
                image = self.tiles.tile(self.object_id, key)
                self._original[key] = QImage(image) if image is not None else None
                if key not in self.before:
                    self.before[key] = self._original[key]
            image = self._original[key]
            # Our private base is independent of any earlier transaction action.
            self._base[key] = (_premultiply(image_pixels(image)) if image is not None
                               else np.zeros((self.tiles.tile_size, self.tiles.tile_size, 4), np.float32))
            self._base_bytes += self._base[key].nbytes
            while len(self._base) > 1 and self._base_bytes > self._base_budget:
                _, pixels = self._base.popitem(last=False)
                self._base_bytes -= pixels.nbytes
        else:
            self._base.move_to_end(key)
        return self._base[key]

    def _selection(self, key):
        if self.selection_tile is None:
            return None
        if key in self._selection_masks:
            self._selection_masks.move_to_end(key)
            return self._selection_masks[key]
        image = self.selection_tile(key)
        n = self.tiles.tile_size
        if image is None or image.isNull():
            mask = np.zeros((n,n),np.float32)
        elif image.format() == QImage.Format_Alpha8:
            if image.width() != n or image.height() != n:
                raise ValueError("Brush selection mask must match tile size")
            mask = np.frombuffer(image.constBits(),np.uint8).reshape(n,image.bytesPerLine())[:,:n].astype(np.float32)/255
        else:
            if image.width() != n or image.height() != n:
                raise ValueError("Brush selection mask must match tile size")
            mask = image_pixels(image)[...,3]
        self._selection_masks[key] = mask
        while len(self._selection_masks) > 32:
            self._selection_masks.popitem(last=False)
        return mask

    def _selected_result(self, key, base, result, region=None):
        selection = self._selection(key)
        if selection is not None and region is not None:
            x0,y0,x1,y1=region
            selection=selection[y0:y1,x0:x1]
        return result if selection is None else base+(result-base)*selection[...,None]

    def _render_dabs(self, plane, dabs):
        for dab in dabs:
            if plane.definition.ribbon:
                self._add_ribbon(plane, dab)
            else:
                self._stamp(plane, dab)
            plane.previous_dab = dab

    @staticmethod
    def _normal(a, b):
        dx, dy = b.x-a.x, b.y-a.y
        length = math.hypot(dx, dy)
        return np.asarray((-dy/length, dx/length), np.float32) if length > 1e-8 else np.asarray((0., 1.), np.float32)

    def _add_ribbon(self, plane, dab):
        points = plane.ribbon_points
        if points and math.hypot(dab.x-points[-1].x, dab.y-points[-1].y) < 1e-6:
            return
        points.append(dab)
        if len(points) < 3:
            return
        a, b, c = points
        first, second = self._normal(a, b), self._normal(b, c)
        normal = first+second
        magnitude = np.linalg.norm(normal)
        if magnitude > .1:
            normal /= magnitude
            normal /= max(.25, float(np.dot(normal, first)))
        else:
            normal = first
        self._ribbon_segment(plane, a, b,
                             first if plane.ribbon_normal is None else plane.ribbon_normal, normal)
        plane.ribbon_normal = normal
        del points[0]

    def _finish_ribbon(self, plane):
        if not plane.definition.ribbon:
            return
        points = plane.ribbon_points
        if len(points) == 1:
            # A click still leaves the material tip, with its declared shape.
            self._stamp(plane, points[0])
        elif len(points) == 2:
            normal = self._normal(*points)
            self._ribbon_segment(plane, points[0], points[1],
                                 normal if plane.ribbon_normal is None else plane.ribbon_normal, normal)
        points.clear()

    @staticmethod
    def _interpolate_dab(a, b, t):
        values = {name: getattr(a, name)+(getattr(b, name)-getattr(a, name))*t for name in
                  ("x", "y", "size", "opacity", "density", "angle", "thickness", "hue",
                   "saturation", "luminosity", "sub_color_mix", "distance", "time", "pressure",
                   "texture_density", "paint_amount", "paint_density", "blur", "hardness", "color_stretch")
                  if hasattr(a,name)}
        return replace(a, **values)

    def _ribbon_index(self, plane):
        count, index = len(plane.definition.tips), plane.ribbon_cycle
        mode = plane.definition.repeat_mode
        if mode in {"once", "one_random"} and index >= count:
            return None
        if mode == "reverse":
            return count-1-index % count
        if mode == "pingpong" and count > 1:
            phase = index % (2*count-2)
            return phase if phase < count else 2*count-2-phase
        if mode == "hold_last":
            return min(index, count-1)
        if mode == "random":
            if plane.ribbon_tip_cycle != index:
                plane.ribbon_tip=plane.random.randrange(count)
                plane.ribbon_tip_cycle=index
            return plane.ribbon_tip
        if mode == "one_random":
            if not plane.random_cycle:
                plane.random_cycle = list(range(count))
                plane.random.shuffle(plane.random_cycle)
            return plane.random_cycle[index]
        return index % count

    def _ribbon_segment(self, plane, a, b, normal_a, normal_b):
        length = math.hypot(b.x-a.x, b.y-a.y)
        if length <= 1e-7:
            return
        done = 0.
        while done < length-1e-7:
            index = self._ribbon_index(plane)
            if index is None:
                return
            if plane.ribbon_flips is None:
                def flip(mode):
                    if mode in {"fixed",True}:
                        return True
                    if mode == "random":
                        return bool(plane.random.getrandbits(1))
                    if mode == "alternate":
                        return bool(plane.ribbon_cycle%2)
                    count=len(plane.definition.tips)
                    return (mode == "reverse" and plane.definition.repeat_mode == "pingpong" and
                            count > 1 and plane.ribbon_cycle%(2*count-2) >= count)
                plane.ribbon_flips=(flip(plane.definition.flip_x),flip(plane.definition.flip_y))
            tip = plane.definition.tips[index]
            middle = self._interpolate_dab(a, b, (done/length+1)*.5)
            _, tile_length = self._ribbon_dimensions(tip, middle, plane.definition)
            tile_length = max(.1, tile_length)
            step = min(length-done, (1-plane.ribbon_phase)*tile_length)
            if step < 1e-8:
                plane.ribbon_cycle += 1
                plane.ribbon_phase = 0.
                plane.ribbon_flips = None
                continue
            start, end = done/length, (done+step)/length
            da, db = self._interpolate_dab(a, b, start), self._interpolate_dab(a, b, end)
            da=replace(da,flip_x=plane.ribbon_flips[0],flip_y=plane.ribbon_flips[1])
            db=replace(db,flip_x=plane.ribbon_flips[0],flip_y=plane.ribbon_flips[1])
            na, nb = normal_a*(1-start)+normal_b*start, normal_a*(1-end)+normal_b*end
            next_phase = min(1., plane.ribbon_phase+step/tile_length)
            self._ribbon_piece(plane, tip, da, db, na, nb, plane.ribbon_phase, next_phase)
            done += step
            plane.ribbon_phase = next_phase
            if plane.ribbon_phase >= 1-1e-7:
                plane.ribbon_phase = 0.
                plane.ribbon_cycle += 1
                plane.ribbon_flips = None

    def _ribbon_piece(self, plane, tip, a, b, normal_a, normal_b, phase_a, phase_b):
        definition = plane.definition
        width_a, _ = self._ribbon_dimensions(tip, a, definition)
        width_b, _ = self._ribbon_dimensions(tip, b, definition)
        center_a, center_b = np.asarray((a.x, a.y)), np.asarray((b.x, b.y))
        left_a = center_a-normal_a*width_a*.5
        right_a = center_a+normal_a*width_a*.5
        left_b = center_b-normal_b*width_b*.5
        right_b = center_b+normal_b*width_b*.5
        vertices = np.stack((left_a, right_a, left_b, right_b))
        low, high = vertices.min(axis=0)-1, vertices.max(axis=0)+1
        bounds = QRectF(float(low[0]), float(low[1]), float(high[0]-low[0]), float(high[1]-low[1]))
        base, along, across = left_a, left_b-left_a, right_a-left_a
        warp = right_b-left_b-across
        middle = self._interpolate_dab(a, b, .5)
        for key, ys, xs, xx, yy in self._regions(bounds):
            px, py = xx-base[0], yy-base[1]
            determinant = across[0]*along[1]-across[1]*along[0]
            if abs(determinant) < 1e-8:
                continue
            u = (px*along[1]-py*along[0])/determinant
            t = (py*across[0]-px*across[1])/determinant
            # Invert a bilinear quad; this maps the image continuously through
            # curved and pressure-varying strips rather than rotating stamps.
            for _ in range(3):
                rx = across[0]*u+along[0]*t+warp[0]*u*t-px
                ry = across[1]*u+along[1]*t+warp[1]*u*t-py
                ux, uy = across[0]+warp[0]*t, across[1]+warp[1]*t
                tx, ty = along[0]+warp[0]*u, along[1]+warp[1]*u
                det = ux*ty-uy*tx
                safe = np.where(np.abs(det) > 1e-8, det, np.where(det < 0, -1e-8, 1e-8))
                u -= (rx*ty-ry*tx)/safe
                t -= (ry*ux-rx*uy)/safe
            if definition.antialiasing:
                aa_width = (.01,.65,1.,1.4)[max(0,min(3,definition.antialiasing))]
                across_coverage = np.clip(np.minimum(u,1-u)*(width_a+width_b)*.5/aa_width+.5,0,1)
                inside = (across_coverage > 0) & (t >= 0) & (t < 1)
            else:
                inside = (u >= 0) & (u < 1) & (t >= 0) & (t < 1)
                across_coverage = inside.astype(np.float32)
            if not np.any(inside):
                continue
            v = phase_a+(phase_b-phase_a)*t
            raw_width,raw_height=self._tip_dimensions(tip,middle,definition)
            if definition.angle:
                u,v=self._ribbon_coordinates(u,v,raw_width,raw_height,definition.angle)
            rgb, alpha = self._tip_pixels(tip, middle, definition, u, v,
                                          raw_width,raw_height,ribbon=True)
            alpha *= inside*across_coverage
            if definition.texture is not None and definition.texture.per_dab:
                alpha = self._texture(alpha, definition.texture, xx, yy,
                                      density_factor=getattr(middle,"texture_density",1))
            self._deposit(plane, key, ys, xs, rgb, alpha, middle)

    def _tip_colors(self,dab,definition):
        main,sub=self.color,self.sub_color
        target=getattr(definition,"color_change_target","main")
        if dab.hue or dab.saturation or dab.luminosity:
            def shifted(color):
                h,s,v=colorsys.rgb_to_hsv(*color[:3])
                result=color.copy()
                result[:3]=colorsys.hsv_to_rgb((h+dab.hue)%1,np.clip(s+dab.saturation,0,1),
                                              np.clip(v+dab.luminosity,0,1))
                return result
            if target in {"main","both"}:
                main=shifted(main)
            if target in {"sub","both"}:
                sub=shifted(sub)
        mix=np.clip(dab.sub_color_mix,0,1)
        result=main*(1-mix)+sub*mix
        if definition.mixing_space == "perceptual" and 0 < mix < 1:
            # Keep the same declared linear-light approximation used by wet
            # mixing; CSP also applies its mixing space to color variation.
            result[:3]=(main[:3]**2.2*(1-mix)+sub[:3]**2.2*mix)**(1/2.2)
        return result,sub

    def _foreground(self,dab,definition=None):
        return self._tip_colors(dab,definition or self.definition)[0]

    @staticmethod
    def _tip_dimensions(tip, dab, definition):
        ratio = max(1, tip.width, tip.height) if tip.shape == "image" else 1
        width = max(.01, dab.size * (tip.width/ratio if tip.shape == "image" else 1))
        height = max(.01, dab.size * (tip.height/ratio if tip.shape == "image" else 1))
        if getattr(definition, "thickness_axis", "horizontal") == "vertical":
            height *= dab.thickness
        else:
            width *= dab.thickness
        return width, height

    def _ribbon_dimensions(self,tip,dab,definition):
        width,height=self._tip_dimensions(tip,dab,definition)
        angle=math.radians(definition.angle)
        c,s=abs(math.cos(angle)),abs(math.sin(angle))
        return width*c+height*s,width*s+height*c

    @staticmethod
    def _ribbon_coordinates(u,v,width,height,angle):
        """Map the rotated registered rectangle without deforming its motifs.

        Noncardinal bounding-box corners remain empty. The native interaction
        between tip angle, direction input and ribbon repetition is unverified;
        stretching each row to fill those corners distorts source artwork.
        """
        rotation=math.radians(angle)
        c,s=math.cos(rotation),math.sin(rotation)
        rotated_width=width*abs(c)+height*abs(s)
        rotated_height=width*abs(s)+height*abs(c)
        xx=(u-.5)*rotated_width
        yy=(v-.5)*rotated_height
        return (c*xx+s*yy)/width+.5,(-s*xx+c*yy)/height+.5

    def _tip_pixels(self, tip, dab, definition, u, v, width, height, *, ribbon=False):
        """u/v span the full registered rectangle, including transparent padding."""
        if dab.flip_x:
            u = 1-u
        if dab.flip_y:
            v = 1-v
        foreground,secondary = self._tip_colors(dab,definition)
        if tip.shape == "image" and tip.png:
            reduction = min(tip.width/max(width,1),tip.height/max(height,1))
            mip = max(0,int(math.floor(math.log2(reduction)))) if reduction > 1 and definition.antialiasing >= 2 else 0
            material = _premultiplied_material(tip.png,mip)
            sx = u*material.shape[1]-.5
            sy = v*material.shape[0]-.5
            if ribbon:
                sine=abs(math.sin(math.radians(definition.angle)))
                if sine > .999:
                    sx=np.clip(sx,0,material.shape[1]-1)
                else:
                    sy = np.clip(sy, 0, material.shape[0]-1)
            sample = _sample(material, sx, sy,
                             nearest=definition.antialiasing == 0)
            alpha = sample[..., 3]
            if tip.mode == "color":
                rgb = sample[..., :3]/np.maximum(alpha[..., None], 1e-8)
                # Color jitter can also affect authored RGB materials.
                if (definition.color_change_target != "sub" and
                        (dab.hue or dab.saturation or dab.luminosity)):
                    rgb = _jitter_rgb(rgb, dab.hue, dab.saturation, dab.luminosity)
            elif tip.mode == "dual_color":
                gray = np.mean(sample[..., :3], axis=-1)/np.maximum(alpha, 1e-8)
                rgb = foreground[:3]*(1-gray[..., None]) + secondary[:3]*gray[..., None]
                alpha = alpha*(foreground[3]*(1-gray)+secondary[3]*gray)
            else:
                rgb = np.broadcast_to(foreground[:3], (*u.shape, 3))
                alpha = alpha*foreground[3]
        else:
            x, y = (u-.5)*2, (v-.5)*2
            radius = np.maximum(np.abs(x), np.abs(y)) if tip.shape == "square" else np.sqrt(x*x+y*y)
            hardness = np.clip(definition.hardness*getattr(dab,"hardness",1), 0, 1)
            if hardness >= .999:
                edge = min(width, height)/2
                aa_width = (.01,.65,1.,1.4)[max(0,min(3,definition.antialiasing))]
                alpha = np.clip((1-radius)*edge/aa_width+.5, 0, 1) if definition.antialiasing else (radius <= 1).astype(np.float32)
            else:
                falloff = np.clip((1-radius)/max(.001, 1-hardness), 0, 1)
                alpha = falloff*falloff*(3-2*falloff)
                if definition.antialiasing == 0:
                    alpha = np.where(radius <= 1, alpha, 0)
            alpha *= foreground[3]
            rgb = np.broadcast_to(foreground[:3], (*u.shape, 3))
        return rgb, np.clip(alpha, 0, 1)

    def _stamp(self, plane, dab):
        definition = plane.definition
        if dab.size <= 0 or dab.density <= 0 or dab.opacity <= 0:
            return
        tip = definition.tips[dab.tip_index % len(definition.tips)]
        width, height = self._tip_dimensions(tip, dab, definition)
        angle = math.radians(dab.angle)
        c, s = math.cos(angle), math.sin(angle)
        ex, ey = abs(c)*width/2+abs(s)*height/2+1, abs(s)*width/2+abs(c)*height/2+1
        bounds = QRectF(dab.x-ex, dab.y-ey, ex*2, ey*2)
        wet=definition.mixing_mode != "none"
        if wet:
            self._prepare_wet(plane,dab,width,height)
        for key, ys, xs, xx, yy in self._regions(bounds):
            dx, dy = xx-dab.x, yy-dab.y
            u, v = (c*dx+s*dy)/width+.5, (-s*dx+c*dy)/height+.5
            rgb, alpha = self._tip_pixels(tip, dab, definition, u, v, width, height)
            if definition.texture is not None and definition.texture.per_dab:
                alpha = self._texture(alpha, definition.texture, xx, yy,
                                      density_factor=getattr(dab,"texture_density",1))
            if not np.any(alpha > 0):
                continue
            if wet:
                rgb, alpha = self._wet_pixels(plane, dab, xx, yy, rgb, alpha)
            # Pickup is already frozen in the small reservoir, so both wet and
            # dry deposition can stream tiles without a full-footprint staging.
            self._deposit(plane,key,ys,xs,rgb,alpha,dab)

    def _regions(self, bounds):
        n = self.tiles.tile_size
        left, top = math.floor(bounds.left()), math.floor(bounds.top())
        right, bottom = math.ceil(bounds.right()), math.ceil(bounds.bottom())
        for ky in range(top//n, (bottom-1)//n+1):
            for kx in range(left//n, (right-1)//n+1):
                x0, y0 = max(left, kx*n), max(top, ky*n)
                x1, y1 = min(right, (kx+1)*n), min(bottom, (ky+1)*n)
                if x0 >= x1 or y0 >= y1:
                    continue
                yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
                yield (kx, ky), slice(y0-ky*n, y1-ky*n), slice(x0-kx*n, x1-kx*n), xx+.5, yy+.5

    def _deposit(self, plane, key, ys, xs, rgb, alpha, dab):
        coverage = np.clip(alpha*dab.density, 0, 1)
        if not np.any(coverage > 0):
            return
        selection = self._selection(key)
        if selection is not None and not np.any(selection[ys,xs]*coverage):
            return
        self._base_tile(key)
        pixels, opacity = plane.allocate(key)
        source = np.concatenate((rgb*coverage[..., None], coverage[..., None]), axis=-1)
        region = pixels[ys, xs]
        mode = "darken" if plane.definition.blend_tips == "darken" else "normal"
        pixels[ys, xs] = composite_pixels(region, source, mode)
        opacity[ys, xs] = np.maximum(opacity[ys, xs], np.where(coverage > 0, dab.opacity, 0))
        if plane.track_texture:
            parameters = plane.texture_parameters[key][ys,xs]
            strongest = coverage >= parameters[...,0]
            parameters[...,1] = np.where(strongest,getattr(dab,"texture_density",1),parameters[...,1])
            parameters[...,0] = np.maximum(parameters[...,0],coverage)
        self._dirty.add(key)
        n=self.tiles.tile_size
        x0,x1,_=xs.indices(n)
        y0,y1,_=ys.indices(n)
        previous=self._dirty_regions.get(key)
        self._dirty_regions[key]=(x0,y0,x1,y1) if previous is None else (
            min(x0,previous[0]),min(y0,previous[1]),max(x1,previous[2]),max(y1,previous[3]))
        self._main_alpha_cache.clear()

    def _texture(self, alpha, texture, xx, yy, *, cache_key=None, density_factor=1):
        if not texture.png or texture.density <= 0:
            return alpha
        value = self._texture_values.get(cache_key) if cache_key is not None else None
        if value is None:
            gray = _material_cache.get(texture.png, gray=True)
            angle, scale = math.radians(texture.angle), max(.01, texture.scale)
            c, s = math.cos(angle), math.sin(angle)
            value = _sample(gray, (c*xx+s*yy)/scale-.5, (-s*xx+c*yy)/scale-.5, wrap=True)
            # Lower contrast approaches a flat midtone without reversing the
            # image. Inversion is a separate setting. Keep the established
            # positive branch; CSP's exact transfer curve is uncalibrated.
            gain = 1+texture.contrast*(3 if texture.contrast >= 0 else 1)
            value = np.clip((value-.5)*gain+.5+texture.brightness, 0, 1)
            if texture.invert:
                value = 1-value
            if texture.emphasize_density:
                value = value*value*(3-2*value)
            if cache_key is not None:
                self._texture_values[cache_key] = value
                while len(self._texture_values) > 32:
                    self._texture_values.popitem(last=False)
        elif cache_key is not None:
            self._texture_values.move_to_end(cache_key)
        mode = texture.mode
        if mode in {"normal", "multiply"}:
            changed = alpha*value
        elif mode == "subtract":
            changed = np.maximum(0, alpha-(1-value))
        elif mode == "compare":
            changed = np.minimum(alpha, value)
        elif mode == "overlay":
            changed = np.where(alpha <= .5, 2*alpha*value, 1-2*(1-alpha)*(1-value))
            changed = np.minimum(alpha, changed)
        elif mode in {"height", "height_linear", "height_(linear)"}:
            changed = np.clip(alpha*2+value-1, 0, 1)*alpha
        elif mode == "outline":
            changed = alpha*(1-(1-value)*(1-alpha))
        elif mode == "color_dodge":
            changed = np.minimum(1, alpha/np.maximum(1-value, 1e-7))
        elif mode == "color_burn":
            # Density-space color burn; like the other texture responses this
            # is an independent approximation pending native CSP comparison.
            changed = np.maximum(0, 1-(1-alpha)/np.maximum(value, 1e-7))
        elif mode == "hard_mix":
            changed = alpha*(alpha+value >= 1)
        else:
            changed = alpha*value
        return np.clip(alpha+(changed-alpha)*np.clip(texture.density*density_factor,0,1), 0, 1)

    def _plane_pixels(self, plane, key, region=None):
        n = self.tiles.tile_size
        x0,y0,x1,y1=region if region is not None else (0,0,n,n)
        if not plane.contains(key):
            return np.zeros((y1-y0, x1-x0, 4), np.float32)
        pixels, opacity = plane.allocate(key)
        source = pixels[y0:y1,x0:x1]*opacity[y0:y1,x0:x1,None]
        texture = plane.definition.texture
        if texture is not None and not texture.per_dab:
            yy, xx = np.mgrid[y0:y1,x0:x1].astype(np.float32)
            alpha = self._texture(source[..., 3], texture, xx+key[0]*n+.5, yy+key[1]*n+.5,
                                  cache_key=(id(plane), key) if region is None else None, density_factor=(
                                      plane.texture_parameters[key][y0:y1,x0:x1,1] if plane.track_texture else 1))
            source = source*(alpha/np.maximum(source[..., 3], 1e-8))[..., None]
        return source

    def _source_tile(self, key, region=None):
        first = self._plane_pixels(self.main, key, region)
        if self.secondary is None:
            return first
        second = self._plane_pixels(self.secondary, key, region)
        a, b = first[..., 3], second[..., 3]
        mode = self.definition.dual_mode
        if mode == "multiply":
            alpha = a*b
        elif mode in {"subtract", "height", "height_linear", "height_(linear)"}:
            alpha = np.maximum(0, a-b) if mode == "subtract" else np.clip(a*2-b, 0, 1)
        elif mode == "darken":
            alpha = np.minimum(a, b)
        elif mode == "lighten":
            alpha = np.maximum(a, b)
        elif mode in {"add", "add_glow", "add_(glow)"}:
            alpha = np.minimum(1, a+b)
        elif mode == "normal":
            alpha = b+a*(1-b)
        else:
            alpha = _blend_rgb(a, b, mode)
        rgb = first[..., :3]/np.maximum(a[..., None], 1e-8)
        rgb = np.where((a > 0)[..., None], rgb, self.color[:3])
        if getattr(self.definition, "dual_apply_rgb", False):
            other = second[..., :3]/np.maximum(b[..., None], 1e-8)
            rgb = _blend_rgb(rgb, other, mode)
        return np.concatenate((rgb*alpha[..., None], alpha[..., None]), axis=-1)

    def _current_tile(self, key):
        if self._wet_snapshot is not None and key in self._wet_snapshot:
            self._wet_snapshot.move_to_end(key)
            return self._wet_snapshot[key]
        if key in self._original or self.main.contains(key):
            base = self._base_tile(key)
            result = self._selected_result(key,base,
                composite_pixels(base,self._source_tile(key),self._canvas_blend_mode()))
        else:
            image = self.tiles.tile(self.object_id, key)
            result = (_premultiply(image_pixels(image)) if image is not None else
                      np.zeros((self.tiles.tile_size, self.tiles.tile_size, 4), np.float32))
        if self._wet_snapshot is not None:
            self._wet_snapshot[key] = result
            limit=max(1,self._wet_stage_budget//(self.tiles.tile_size**2*16))
            while len(self._wet_snapshot) > limit:
                self._wet_snapshot.popitem(last=False)
        return result

    def _canvas_blend_mode(self):
        # CSP retains the chosen Ink mode while Blend/Running color make it
        # inactive. Smear still permits that mode. Do not destroy the saved
        # choice: turning wet mixing off should restore it.
        return ("normal" if self.definition.mixing_mode in {"blend","running"}
                else self.definition.blending_mode)

    def _read_pixels(self, xx, yy, *, original=False):
        n = self.tiles.tile_size
        x, y = np.floor(xx).astype(np.int64), np.floor(yy).astype(np.int64)
        result = np.zeros((*x.shape, 4), np.float32)
        tile_x, tile_y = x//n, y//n
        for ky in range(int(tile_y.min()), int(tile_y.max())+1):
            for kx in range(int(tile_x.min()), int(tile_x.max())+1):
                selected = (tile_x == kx) & (tile_y == ky)
                if np.any(selected):
                    key = (kx, ky)
                    if original:
                        if key in self._original:
                            pixels = self._base_tile(key)
                        else:
                            image = self.tiles.tile(self.object_id, key)
                            pixels = (_premultiply(image_pixels(image)) if image is not None else
                                      np.zeros((n, n, 4), np.float32))
                    else:
                        pixels = self._current_tile(key)
                    result[selected] = pixels[y[selected] % n, x[selected] % n]
        return result

    def _prepare_wet(self,plane,dab,width,height):
        """Retain picked-up pigment in a small brush-local reservoir.

        The retention distance is our independent approximation, not CSP's
        proprietary transport formula. Sampling once before deposition avoids
        tile traversal order and repeated overlapping dabs erasing the pickup.
        """
        definition,previous=plane.definition,plane.previous_dab
        n=64
        v,u=np.mgrid[:n,:n].astype(np.float32)
        dx,dy=((u+.5)/n-.5)*width,((v+.5)/n-.5)*height
        angle=math.radians(dab.angle)
        c,s=math.cos(angle),math.sin(angle)
        xx,yy=dab.x+c*dx-s*dy,dab.y+s*dx+c*dy
        # Local pickup comes from pre-stroke pigment; the reservoir carries its
        # wet history. Reading deposited foreground at every overlapping dab
        # makes a dense stroke converge to that foreground before it can mix,
        # and can completely hide the next underlying color from Smear.
        original=True
        pickup=self._read_pixels(xx,yy,original=original)
        if definition.mixing_mode == "running":
            # Fixed widths use document pixels; Automatic retains the existing
            # size-linked approximation and legacy normalized strength. CSP's
            # automatic radius and exact blur kernel are not publicly defined.
            width = (definition.blur_width if definition.blur_mode == "fixed" else
                     definition.blur*min(20., dab.size*.2))
            radius = width*getattr(dab,"blur",1)
        else:
            radius = 0.
        if radius > 0:
            total = pickup.copy()
            for ox, oy in ((radius, 0), (-radius, 0), (0, radius), (0, -radius)):
                total += self._read_pixels(xx+ox,yy+oy,original=original)
            pickup = total/5
        stretch=np.clip(definition.color_stretch*getattr(dab,"color_stretch",1),0,1)
        if plane.wet_pickup is not None and previous is not None and stretch > 0:
            distance=math.hypot(dab.x-previous.x,dab.y-previous.y)
            retention=math.exp(-distance/max(.25,dab.size*8*stretch))
            carried=plane.wet_pickup*retention
            # Fresh pigment fills unladen bristles immediately. Once loaded,
            # the retained pigment limits replacement by the next color; empty
            # canvas instead lets the load fade over the stretch distance.
            pickup=carried+pickup*(1-carried[...,3:4])
        plane.wet_pickup=np.asarray(pickup,dtype=np.float32)

    def _wet_pixels(self, plane, dab, xx, yy, rgb, alpha):
        definition=plane.definition
        tip=definition.tips[dab.tip_index%len(definition.tips)]
        width,height=self._tip_dimensions(tip,dab,definition)
        angle=math.radians(dab.angle)
        c,s=math.cos(angle),math.sin(angle)
        dx,dy=xx-dab.x,yy-dab.y
        n=plane.wet_pickup.shape[0]
        u=np.clip(((c*dx+s*dy)/width+.5)*n-.5,0,n-1)
        v=np.clip(((-s*dx+c*dy)/height+.5)*n-.5,0,n-1)
        pickup=_sample(plane.wet_pickup,u,v)
        amount = np.clip(definition.paint_amount*getattr(dab,"paint_amount",1),0,1)
        existing_alpha = pickup[..., 3]
        existing = pickup[..., :3]/np.maximum(existing_alpha[..., None], 1e-8)
        load = amount+(1-amount)*existing_alpha
        if definition.mixing_space == "perceptual":
            # Linear-light interpolation is a declared approximation; CSP's
            # proprietary perceptual pigment method is not publicly specified.
            pigment = ((np.maximum(rgb, 0)**2.2)*amount +
                       (np.maximum(existing, 0)**2.2)*(1-amount)*existing_alpha[..., None])
            pigment = np.maximum(pigment/np.maximum(load[..., None], 1e-8), 0)**(1/2.2)
        else:
            pigment = (rgb*amount+existing*(1-amount)*existing_alpha[..., None])/np.maximum(load[..., None], 1e-8)
        strength = load
        if definition.mixing_mode in {"blend", "running"}:
            # Paint density controls the fresh paint's alpha contribution; it
            # must not discard pigment already picked up from the canvas.
            # In particular amount=density=0 is a useful pure blender. At full
            # paint density this retains the previous load/transport behavior.
            density = np.clip(definition.paint_density*getattr(dab,"paint_density",1),0,1)
            strength = existing_alpha+amount*density*(1-existing_alpha)
        return np.clip(pigment, 0, 1), alpha*strength

    def _edge_source(self, key, source, region=None):
        definition, n = self.definition, self.tiles.tile_size
        radius = max(1, math.ceil(definition.watercolor_edge))
        # CSP's blurring width is dormant until Process after brush stroke is
        # enabled. Preserve the stored value so toggling that option restores it.
        blur = max(0, definition.watercolor_blur) if definition.watercolor_after else 0.
        halo = radius + math.ceil(blur*3) + 1
        kx, ky = key
        if region is None:
            # Completion and blurred post-stroke edges retain the full-tile
            # equation, including the established Gaussian halo behavior.
            width = height = n
            left, top = kx*n-halo, ky*n-halo
            right, bottom = (kx+1)*n+halo, (ky+1)*n+halo
            alpha = np.zeros((n+2*halo, n+2*halo), np.float32)
            for iy in range(top//n, (bottom-1)//n+1):
                for ix in range(left//n, (right-1)//n+1):
                    l, t = max(left, ix*n), max(top, iy*n)
                    r, b = min(right, (ix+1)*n), min(bottom, (iy+1)*n)
                    other = self._main_alpha_cache.get((ix, iy))
                    if other is None:
                        other = self._source_tile((ix, iy))[..., 3].copy()
                        self._main_alpha_cache[(ix, iy)] = other
                        while len(self._main_alpha_cache) > 32:
                            self._main_alpha_cache.popitem(last=False)
                    else:
                        self._main_alpha_cache.move_to_end((ix, iy))
                    alpha[t-top:b-top, l-left:r-left] = other[t-iy*n:b-iy*n, l-ix*n:r-ix*n]
        else:
            # A live edge can change only within its radius of changed paint.
            # Read just that output patch and its morphology halo, including
            # neighboring tiles, instead of rebuilding every complete tile.
            x0,y0,x1,y1=region
            width,height=x1-x0,y1-y0
            left,top=kx*n+x0-halo,ky*n+y0-halo
            right,bottom=kx*n+x1+halo,ky*n+y1+halo
            alpha=np.zeros((height+2*halo,width+2*halo),np.float32)
            for iy in range(top//n,(bottom-1)//n+1):
                for ix in range(left//n,(right-1)//n+1):
                    l,t=max(left,ix*n),max(top,iy*n)
                    r,b=min(right,(ix+1)*n),min(bottom,(iy+1)*n)
                    other=self._source_tile((ix,iy),(l-ix*n,t-iy*n,r-ix*n,b-iy*n))
                    alpha[t-top:b-top,l-left:r-left]=other[...,3]
        # Inner/outer outline is measured from the union, not every stamp.
        if not np.any(alpha):
            return source
        outer = maximum_filter(alpha, size=radius*2+1, mode="constant")
        inner = minimum_filter(alpha, size=radius*2+1, mode="constant")
        edge = np.maximum(0, outer-inner)
        if blur:
            edge = gaussian_filter(edge, blur, mode="constant")
        edge = edge[halo:halo+height, halo:halo+width]*definition.watercolor_opacity
        edge = np.clip(edge, 0, 1)
        a = source[..., 3]
        color = source[..., :3]/np.maximum(a[..., None], 1e-8)
        color = np.where((a > 0)[..., None], color, self.color[:3])
        if definition.watercolor_mode == "vivid":
            vividness = getattr(definition, "watercolor_vividness", .5)
            color = _jitter_rgb(color, 0, vividness*.5, -.15*definition.watercolor_darkness)
            edge *= getattr(definition, "watercolor_strength", 1.)
        else:
            color = color*(1-definition.watercolor_darkness*.65)
        pigment = np.concatenate((color*edge[..., None], edge[..., None]), axis=-1)
        return composite_pixels(source, pigment)

    def _flush(self):
        if not self._dirty:
            return QRectF()
        definition = self.definition
        keys = set(self._dirty)
        self._dirty.clear()
        regions,self._dirty_regions=self._dirty_regions,{}
        apply_edges=definition.watercolor_edge and (self._edge_finished or not definition.watercolor_after)
        regional_edges=apply_edges and not self._edge_finished and not definition.watercolor_after
        n = self.tiles.tile_size
        if regional_edges:
            padding=max(1,math.ceil(definition.watercolor_edge))
            expanded={}
            for kx,ky in keys:
                x0,y0,x1,y1=regions.get((kx,ky),(0,0,n,n))
                left,top=kx*n+x0-padding,ky*n+y0-padding
                right,bottom=kx*n+x1+padding,ky*n+y1+padding
                for iy in range(top//n,(bottom-1)//n+1):
                    for ix in range(left//n,(right-1)//n+1):
                        current=(max(0,left-ix*n),max(0,top-iy*n),
                                 min(n,right-ix*n),min(n,bottom-iy*n))
                        previous=expanded.get((ix,iy))
                        expanded[(ix,iy)]=current if previous is None else (
                            min(current[0],previous[0]),min(current[1],previous[1]),
                            max(current[2],previous[2]),max(current[3],previous[3]))
            regions=expanded
            keys=set(expanded)
        elif apply_edges:
            blur = max(0, definition.watercolor_blur) if definition.watercolor_after else 0.
            radius = definition.watercolor_edge+3*blur+2
            extra = set()
            for kx, ky in keys:
                extra.update(self.tiles.keys_for_rect(QRectF(kx*n-radius, ky*n-radius, n+2*radius, n+2*radius)))
            keys.update(extra)
        changed = QRectF()
        for key in sorted(keys):
            region=(0,0,n,n) if apply_edges and not regional_edges else regions.get(key,(0,0,n,n))
            x0,y0,x1,y1=region
            selection = self._selection(key)
            if selection is not None and not np.any(selection[y0:y1,x0:x1]):
                continue
            source = self._source_tile(key,None if apply_edges and not regional_edges else region)
            if apply_edges:
                source = self._edge_source(key, source,region if regional_edges else None)
            if not np.any(source[..., 3] > 0) and key not in self._original:
                continue
            base = self._base_tile(key)[y0:y1,x0:x1]
            result = self._selected_result(key,base,composite_pixels(base,source,self._canvas_blend_mode()),region)
            patch=pixels_image(_straight(result))
            if region==(0,0,n,n):
                output=patch
            else:
                current=self.tiles.tile(self.object_id,key)
                output=QImage(current) if current is not None else self.tiles._empty(n)
                painter=QPainter(output)
                painter.setCompositionMode(QPainter.CompositionMode_Source)
                painter.drawImage(x0,y0,patch)
                painter.end()
            self.tiles.set_tile(self.object_id, key, output)
            rect = QRectF(key[0]*n+x0, key[1]*n+y0, x1-x0, y1-y0)
            changed = rect if changed.isEmpty() else changed.united(rect)
        if not changed.isEmpty():
            self._bounds = changed if self._bounds.isEmpty() else self._bounds.united(changed)
        return changed


def _jitter_rgb(rgb, hue, saturation, brightness):
    """Vectorized HSV jitter for authored color materials."""
    maximum, minimum = rgb.max(axis=-1), rgb.min(axis=-1)
    delta = maximum-minimum
    saturation0 = delta/np.maximum(maximum, 1e-8)
    hue0 = np.zeros(maximum.shape, np.float32)
    mask = delta > 1e-8
    red = mask & (rgb[..., 0] == maximum)
    green = mask & ~red & (rgb[..., 1] == maximum)
    blue = mask & ~red & ~green
    hue0[red] = ((rgb[..., 1]-rgb[..., 2])/np.maximum(delta, 1e-8))[red] % 6
    hue0[green] = ((rgb[..., 2]-rgb[..., 0])/np.maximum(delta, 1e-8)+2)[green]
    hue0[blue] = ((rgb[..., 0]-rgb[..., 1])/np.maximum(delta, 1e-8)+4)[blue]
    h = (hue0/6+hue) % 1
    s, v = np.clip(saturation0+saturation, 0, 1), np.clip(maximum+brightness, 0, 1)
    h6 = h*6
    index, fraction = np.floor(h6).astype(np.int8) % 6, h6-np.floor(h6)
    p, q, t = v*(1-s), v*(1-fraction*s), v*(1-(1-fraction)*s)
    out = np.empty_like(rgb)
    for i, channels in enumerate(((v,t,p),(q,v,p),(p,v,t),(p,q,v),(t,p,v),(v,p,q))):
        selected = index == i
        for channel, values in enumerate(channels):
            out[..., channel][selected] = values[selected]
    return out
