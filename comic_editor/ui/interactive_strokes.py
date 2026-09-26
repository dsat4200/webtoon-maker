"""Responsive stroke-material previews with detached exact rendering."""
import math

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from comic_editor.core.stroke_geometry import StrokeLoop
from comic_editor.ui.stroke_rendering import opacity_noise, warp_material
from comic_editor.ui.effect_regions import projection_requires_exact


def _render(image, background, bounds, before, after, opacity):
    image, background = warp_material(image, background, bounds, before, after)
    return opacity_noise(image, background, bounds, after, opacity), background


def _pack(image, background):
    # EffectJobs hands off one QImage. Keep both exact material channels in
    # that same result so later stroke stages cannot mix draft and exact fill.
    result = QImage(image.width(), image.height() * 2,
                    QImage.Format_ARGB32_Premultiplied)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode_Source)
    painter.drawImage(0, 0, image)
    painter.drawImage(0, image.height(), background)
    painter.end()
    return result


def _unpack(image):
    height = image.height() // 2
    return (image.copy(0, 0, image.width(), height),
            image.copy(0, height, image.width(), height))


def cached_stroke(canvas, scope, cache_key):
    key = ("stroke-warp", cache_key)
    result = canvas._effect_jobs.result(scope, key) if scope is not None else None
    if result is None:
        result = canvas._modifier_cache_get(key)
    return _unpack(result) if result is not None else None


def _draft(image, background, bounds, before, after, opacity):
    scale = min(1., 192 / max(image.width(), image.height()),
                math.sqrt(16384 / (image.width() * image.height())))
    width, height = max(1, round(image.width() * scale)), max(1, round(image.height() * scale))
    ratios = np.array([width / bounds.width(), height / bounds.height()])
    origin = np.array([bounds.x(), bounds.y()])

    def reduced(loops):
        return [StrokeLoop((loop.points - origin) * ratios, loop.width * scale)
                for loop in loops]

    small = image.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    fill = background.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return _render(small, fill, QRectF(0, 0, width, height),
                   reduced(before), reduced(after), opacity)


def render_interactive_stroke(canvas, image, background, bounds, before, after,
                              opacity, *, cache_key, scope, provisional=False):
    """Return material, fill, and whether they still await exact pixels."""
    jobs = canvas._effect_jobs
    key = ("stroke-warp", cache_key)
    if not provisional and scope is not None:
        cached = cached_stroke(canvas, scope, cache_key)
        if cached is not None:
            return (*cached, False)
    interactive = (canvas._interactive_render and not canvas._render_base_alpha
                   and canvas._rendering_mask_contributor <= 0)
    exact = projection_requires_exact(canvas)
    if not interactive or exact or image.width() * image.height() <= 16384:
        material, fill = _render(image, background, bounds, before, after, opacity)
        if exact and not provisional:
            packed = _pack(material, fill)
            canvas._modifier_cache_put(key, packed)
            if scope is not None:
                jobs.retained_put(("result", scope), key, packed)
        return material, fill, provisional
    navigator = getattr(canvas, "_effect_preview_channel", "canvas") == "navigator"
    asynchronous = not provisional and not navigator and scope is not None
    if asynchronous:
        incoming, fill, area = QImage(image), QImage(background), QRectF(bounds)
        old = [StrokeLoop(loop.points.copy(), loop.width) for loop in before]
        new = [StrokeLoop(loop.points.copy(), loop.width) for loop in after]
        amounts = [np.array(value, copy=True) for value in opacity]

        def compute(cancelled, incoming=incoming, fill=fill, area=area,
                    old=old, new=new, amounts=amounts):
            if cancelled():
                return None
            material, base = _render(incoming, fill, area, old, new, amounts)
            return None if cancelled() else _pack(material, base)

        size = (int(image.sizeInBytes()) * 40
                + sum(loop.points.nbytes for loop in old + new)
                + sum(value.nbytes for value in amounts))
        asynchronous = jobs.request(scope, key, compute, size, allow_oversized=True)
    if not (asynchronous or navigator or provisional):
        return (*_render(image, background, bounds, before, after, opacity), False)
    draft_key = ("stroke-warp-draft", cache_key,
                 int(image.cacheKey()), int(background.cacheKey()))
    draft = canvas._modifier_cache_get(draft_key)
    if draft is None:
        draft = _pack(*_draft(image, background, bounds, before, after, opacity))
        canvas._modifier_cache_put(draft_key, draft)
    material, fill = _unpack(draft)
    return (material.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation),
            fill.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation), True)
