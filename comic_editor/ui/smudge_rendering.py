"""Ordered wet-brush smearing with bounded, reusable stroke checkpoints.

The brush carries a small premultiplied color reservoir along the document-space
curve. Each dab deposits carried paint and picks up the surface underneath it;
later strokes therefore operate on the results of earlier strokes. Output crops
never alter pickup history, so separately requested document tiles agree.
"""
from __future__ import annotations

import hashlib
import json
import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform


def smudge_bounds(bounds, modifier, transform):
    from comic_editor.core.smudge import stroke_cubic

    inverse, valid = transform.inverted()
    if not valid:
        return QRectF(bounds)
    result = QRectF(bounds)
    for stroke in modifier.parameters.get("strokes", ()):
        points = np.asarray(stroke_cubic(stroke), np.float64)
        radius = max(float(point.get("radius", 32.)) for point in stroke["points"])
        low, high = points.min(axis=0) - radius, points.max(axis=0) + radius
        result = result.united(inverse.mapRect(QRectF(*low, *(high - low))))
    return result


def _map(transform, x, y):
    if transform.isAffine():
        return (transform.m11() * x + transform.m21() * y + transform.m31(),
                transform.m12() * x + transform.m22() * y + transform.m32())
    denominator = transform.m13() * x + transform.m23() * y + transform.m33()
    return ((transform.m11() * x + transform.m21() * y + transform.m31()) / denominator,
            (transform.m12() * x + transform.m22() * y + transform.m32()) / denominator)


def _sample(pixels, x, y):
    """Bilinear sampling of mutable paint, with transparent exterior pixels."""
    h, w = pixels.shape[:2]
    x, y = np.broadcast_arrays(x, y)
    ix, iy = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = (x - ix).astype(np.float32), (y - iy).astype(np.float32)
    result = np.zeros((*x.shape, 4), np.float32)
    flat = pixels.reshape(-1, 4) if pixels.flags.c_contiguous else None
    if (flat is not None and ix.size and ix.min() >= 0 and ix.max() < w - 1
            and iy.min() >= 0 and iy.max() < h - 1):
        for dx, dy, weight in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                               (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
            result += flat.take((iy + dy) * w + ix + dx, axis=0) * weight[..., None]
        return result
    for dx, dy, weight in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                           (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
        xx, yy = ix + dx, iy + dy
        valid = (xx >= 0) & (xx < w) & (yy >= 0) & (yy < h)
        clipped_y, clipped_x = np.clip(yy, 0, h-1), np.clip(xx, 0, w-1)
        sample = (flat.take(clipped_y * w + clipped_x, axis=0) if flat is not None
                  else pixels[clipped_y, clipped_x])
        result += sample * (weight * valid)[..., None]
    return result


def _samples(stroke, world_pixel):
    from comic_editor.core.smudge import stroke_cubic, pressure_at
    from comic_editor.core.pressure import PressureCurve

    settings = stroke["pressure_settings"]
    curves = {name: PressureCurve.from_dict(settings[name+"_curve"])
              for name in ("radius", "flow", "strength")
              if settings["pressure_enabled"] and settings["pressure_"+name]}
    def parameters(t):
        first, last = stroke["points"]
        pressure = pressure_at(stroke, t)
        return {name: (first[name]*(1.-t)+last[name]*t)
                * (curves[name].evaluate_fast(pressure) if name in curves else 1.)
                for name in ("radius", "flow", "strength")}

    cubic = np.asarray(stroke_cubic(stroke), np.float64)
    length_bound = np.linalg.norm(np.diff(cubic, axis=0), axis=1).sum()
    count = max(16, min(4096, math.ceil(length_bound / max(world_pixel * 2., 1.))))
    ts = np.linspace(0., 1., count + 1)
    t = ts[:, None]
    curve = ((1-t)**3*cubic[0] + 3*(1-t)**2*t*cubic[1]
             + 3*(1-t)*t*t*cubic[2] + t**3*cubic[3])
    distance = np.r_[0., np.cumsum(np.linalg.norm(np.diff(curve, axis=0), axis=1))]
    if distance[-1] <= 1e-8:
        return
    traveled, previous = 0., None
    while traveled < distance[-1]:
        parameter = float(np.interp(traveled, distance, ts))
        values = parameters(parameter)
        center = np.array([np.interp(traveled, distance, curve[:, axis]) for axis in (0, 1)])
        yield center, values, 0. if previous is None else traveled - previous
        previous = traveled
        traveled = min(distance[-1], traveled + max(world_pixel * .75, values["radius"] * .25))
    yield curve[-1], parameters(1.), distance[-1] - previous


def _stroke(pixels, stroke, bounds, scale, transform, cancelled):
    if any(all(point[name] == 0 for point in stroke["points"]) for name in ("flow", "strength")):
        return True
    inverse, _ = transform.inverted()
    # A normalized reservoir follows radius changes without discarding picked
    # up paint. Its bounded resolution also keeps very broad soft brushes cheap.
    center = np.asarray(stroke["points"][0]["position"], np.float64)
    lx, ly = _map(inverse, *center)
    ux, uy = _map(inverse, *(center + (1., 0.)))
    vx, vy = _map(inverse, *(center + (0., 1.)))
    density = max(math.hypot(ux-lx, uy-ly), math.hypot(vx-lx, vy-ly)) * scale
    radius = max(float(point.get("radius", 32.)) for point in stroke["points"])
    side = max(9, min(129, math.ceil(radius * density * 2.) | 1))
    grid = np.linspace(-1., 1., side)
    gx, gy = np.meshgrid(grid, grid)
    reservoir = None
    for center, values, distance in _samples(stroke, 1. / max(density, 1e-6)):
        if cancelled is not None and cancelled():
            return False
        radius = max(.1, values["radius"])
        flow, strength = values["flow"] / 100., values["strength"] / 100.
        x, y = _map(inverse, center[0] + gx * radius, center[1] + gy * radius)
        pickup = _sample(pixels, (x-bounds.x())*scale-.5, (y-bounds.y())*scale-.5)
        if reservoir is None:
            reservoir = pickup
            continue
        if flow > 0 and strength > 0:
            local = inverse.mapRect(QRectF(center[0]-radius, center[1]-radius, radius*2, radius*2))
            left = max(0, math.floor((local.left()-bounds.x())*scale))
            top = max(0, math.floor((local.top()-bounds.y())*scale))
            right = min(pixels.shape[1], math.ceil((local.right()-bounds.x())*scale))
            bottom = min(pixels.shape[0], math.ceil((local.bottom()-bounds.y())*scale))
            if right > left and bottom > top:
                x, y = np.meshgrid(bounds.x()+(np.arange(left, right)+.5)/scale,
                                   bounds.y()+(np.arange(top, bottom)+.5)/scale)
                wx, wy = _map(transform, x, y)
                u, v = (wx-center[0])/radius, (wy-center[1])/radius
                coverage = np.maximum(0., 1. - u*u - v*v).astype(np.float32) ** 2
                carried = _sample(reservoir, (u+1)*(side-1)/2., (v+1)*(side-1)/2.)
                amount = coverage * (1. - math.exp(-flow*strength*distance/max(radius*.25, 1e-6)))
                surface = pixels[top:bottom, left:right]
                surface += (carried-surface) * amount[..., None]
        # Strong pressure carries its original pickup farther; lower strength
        # mixes the colors encountered along the path into the wet brush.
        pickup_amount = 1. - math.exp(-distance/max(radius, 1e-6)*(1.-strength)*3.)
        reservoir += (pickup-reservoir) * pickup_amount
    return True


def _base_image(image, bounds, work, scale):
    from comic_editor.ui.distort_rendering import _rgba

    size = max(1, math.ceil(work.width()*scale)), max(1, math.ceil(work.height()*scale))
    if work == bounds and size == (image.width(), image.height()):
        return _rgba(image)
    from comic_editor.render.pixels import current_contract
    result = QImage(*size, current_contract().image_format)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(QRectF((bounds.x()-work.x())*scale, (bounds.y()-work.y())*scale,
                                bounds.width()*scale, bounds.height()*scale), image)
    finally:
        painter.end()
    return _rgba(result)


def _rebase_pixels(pixels, old_work, work, scale):
    """Move an unchanged stroke prefix onto another aligned working frame."""
    left = (old_work.x() - work.x()) * scale
    top = (old_work.y() - work.y()) * scale
    if abs(left - round(left)) > 1e-8 or abs(top - round(top)) > 1e-8:
        return None
    left, top = round(left), round(top)
    height = max(1, math.ceil(work.height() * scale))
    width = max(1, math.ceil(work.width() * scale))
    result = np.zeros((height, width, 4), pixels.dtype)
    source_x, source_y = max(0, -left), max(0, -top)
    target_x, target_y = max(0, left), max(0, top)
    columns = min(pixels.shape[1] - source_x, width - target_x)
    rows = min(pixels.shape[0] - source_y, height - target_y)
    if columns > 0 and rows > 0:
        result[target_y:target_y+rows, target_x:target_x+columns] = (
            pixels[source_y:source_y+rows, source_x:source_x+columns]
        )
    return result


def render_smudge(image, bounds, modifier, transform, target, cancelled=None, *,
                  pixel_scale=1., preparation_cache=None):
    """Return a faithful crop of the complete ordered smear, or cancellation."""
    from comic_editor.ui.distort_rendering import _byte_pixels, _image
    from comic_editor.render.pixels import current_contract

    work = QRectF(smudge_bounds(bounds, modifier, transform).toAlignedRect())
    scale = pixel_scale
    if pixel_scale < 1.:
        scale = min(scale, 512./max(work.width(), work.height()),
                    math.sqrt(262144./max(1., work.width()*work.height())))
    if work.width()*work.height()*scale*scale > 64*1024*1024:
        raise ValueError("Smudge result is too large; reduce the layer or stroke extent")
    placement = tuple(getattr(transform, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4))
    prefix_source_key = ("smudge-prefix", int(image.cacheKey()), bounds.getRect(), scale, placement,
                         current_contract().signature)
    source_key = (*prefix_source_key, work.getRect())
    cache = preparation_cache
    prefix = hashlib.sha256()
    opacity = min(1., max(0., float(modifier.parameters.get("opacity", 100.)) / 100.))
    strokes = modifier.parameters.get("strokes", ()) if opacity > 0 else ()
    keys = []
    for stroke in strokes:
        prefix.update(json.dumps(stroke, sort_keys=True, separators=(",", ":")).encode())
        keys.append((prefix_source_key, prefix.digest()))
    # Start at the longest surviving prefix. Walking from the original source
    # would evict useful late checkpoints while rebuilding earlier ones when
    # a large image has more stroke history than the cache can retain.
    result, first = None, 0
    if cache is not None:
        for index in reversed(range(len(keys))):
            checkpoint = cache._get(keys[index])
            if checkpoint is not None:
                old_work, pixels = checkpoint
                result = (pixels if old_work == work
                          else _rebase_pixels(pixels, old_work, work, scale))
            if result is not None:
                first = index + 1
                break
    original = None
    if result is None or opacity < 1.:
        original = cache._get((source_key, "source")) if cache is not None else None
        if original is None:
            original = _base_image(image, bounds, work, scale)
            if cache is not None:
                cache._put((source_key, "source"), original, (original,))
        if result is None:
            result = original
    retain_strokes = cache is not None and result.nbytes <= cache.budget
    for index in range(first, len(strokes)):
        if cancelled is not None and cancelled():
            return None
        # A source larger than the preparation budget cannot produce float
        # checkpoints. Reuse its working array between strokes, preserving the
        # original only when opacity blending still needs it.
        if retain_strokes or index == first and (first > 0 or opacity < 1.):
            result = result.copy()
        if not _stroke(result, strokes[index], work, scale, transform, cancelled):
            return None
        if cache is not None:
            cache._put(keys[index], (QRectF(work), result), (result,))
    if cancelled is not None and cancelled():
        return None
    width, height = max(1, math.ceil(target.width()*pixel_scale)), max(1, math.ceil(target.height()*pixel_scale))
    left, top = (target.x()-work.x())*scale, (target.y()-work.y())*scale
    # Native canonical output regions share the working pixel grid. Avoid
    # resampling every unaffected pixel when the request is a simple crop.
    if (scale == pixel_scale and abs(left-round(left)) < 1e-8 and abs(top-round(top)) < 1e-8
            and abs(target.width()*scale-width) < 1e-8 and abs(target.height()*scale-height) < 1e-8
            and left >= 0 and top >= 0 and round(left)+width <= result.shape[1]
            and round(top)+height <= result.shape[0]):
        region = np.s_[round(top):round(top)+height, round(left):round(left)+width]
        if current_contract().floating:
            final = result[region]
            if opacity < 1.:
                final = original[region] + (final-original[region])*opacity
            return _image(final)
        byte_key = source_key, "pixels", prefix.digest(), opacity
        encoded = cache._get(byte_key) if cache is not None else None
        if encoded is not None:
            return _image(encoded[region])
        if cache is not None:
            complete = result if opacity == 1. else original+(result-original)*opacity
            encoded = _byte_pixels(complete)
            # A display encoding is cheap to rebuild. Do not evict expensive
            # source/stroke checkpoints merely to retain its byte conversion.
            if cache.bytes+encoded.nbytes <= cache.budget and len(cache._entries) < cache.entry_limit:
                cache._put(byte_key, encoded, (encoded,))
            return _image(encoded[region])
        final = result[region]
        if opacity < 1.:
            final = original[region] + (final-original[region])*opacity
        return _image(final)
    output = np.empty((height, width, 4), np.float32)
    for top in range(0, height, 128):
        if cancelled is not None and cancelled():
            return None
        bottom = min(height, top+128)
        xx, yy = np.meshgrid((target.x()-work.x()+(np.arange(width)+.5)*target.width()/width)*scale-.5,
                             (target.y()-work.y()+(np.arange(top, bottom)+.5)*target.height()/height)*scale-.5)
        smeared = _sample(result, xx, yy)
        if opacity < 1.:
            base = _sample(original, xx, yy)
            smeared = base + (smeared-base)*opacity
        output[top:bottom] = smeared
    return _image(output)
