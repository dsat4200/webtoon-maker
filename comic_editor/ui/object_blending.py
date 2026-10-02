"""Blend isolated object output into raster scene captures in bounded blocks.

Normal keeps the existing zero-allocation path. Qt handles its native blend
modes; the remaining modes use vectorized 256-row windows. Retained document
tiles cache the finished composite, so navigation does not repeat this work.
"""
from __future__ import annotations

import math
from contextlib import contextmanager

import numpy as np
from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.core.blend_modes import composite_blend, luminance
from comic_editor.core.models import VectorDrawingObject


CAPTURE_SIDE = 512
NATIVE_MODES = {mode: getattr(QPainter.CompositionMode, "CompositionMode_" + name)
                for mode, name in (
                    ("multiply", "Multiply"), ("screen", "Screen"), ("overlay", "Overlay"),
                    ("soft_light", "SoftLight"), ("hard_light", "HardLight"),
                    ("color_burn", "ColorBurn"), ("color_dodge", "ColorDodge"),
                    ("darken", "Darken"), ("lighten", "Lighten"),
                    ("difference", "Difference"), ("exclusion", "Exclusion"))}


@contextmanager
def suspend_object_blend(canvas, identifier):
    """Capture an object's source for thumbnails/baking without a backdrop."""
    previous = getattr(canvas, "_blend_capture_objects", set())
    canvas._blend_capture_objects = previous | {identifier}
    try:
        yield
    finally:
        canvas._blend_capture_objects = previous


def _pixels(image):
    rgba = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    pixels = np.ndarray((rgba.height(), rgba.width(), 4), dtype=np.uint8,
                        buffer=rgba.constBits(), strides=(rgba.bytesPerLine(), 4, 1))
    return rgba, pixels


def _custom_composite(destination, source, mode, core, density):
    """Keep float temporaries bounded even when a caller changes capture size."""
    back_image, back = _pixels(destination)
    source_image, front = _pixels(source)
    output = np.empty_like(back)
    x, y, width, height = core.getRect()
    for start in range(0, height, 256):
        stop = min(height, start + 256)
        # Contiguous channel planes make repeated color/luminance operations
        # substantially faster than strided RGBA channel slices.
        b = np.moveaxis(np.ascontiguousarray(np.moveaxis(back[start:stop], -1, 0), dtype=np.float32) / 255, 0, -1)
        f = np.moveaxis(np.ascontiguousarray(np.moveaxis(front[y + start:y + stop, x:x + width], -1, 0), dtype=np.float32) / 255, 0, -1)
        shade = None
        if mode == "height_modulate":
            # One-pixel halo prevents seams between capture blocks. Work on
            # premultiplied height to avoid fringes around transparent texels.
            neighborhood = front[y + start - 1:y + stop + 1].astype(np.float32) / 255
            heights = luminance(neighborhood[..., :3])
            dx = heights[1:-1, x + 1:x + width + 1] - heights[1:-1, x - 1:x + width - 1]
            dy = heights[2:, x:x + width] - heights[:-2, x:x + width]
            shade = np.clip((dx + dy) * density, -.5, .5)
        output[start:stop] = np.rint(composite_blend(b, f, mode, height_shade=shade) * 255).astype(np.uint8)
    return QImage(output.data, width, height, output.strides[0], QImage.Format_RGBA8888_Premultiplied).copy()


def render_blended_object(canvas, painter, obj, parent_opacity, local_visible):
    """Called after visibility rejection and before object modifiers are drawn."""
    device = painter.device()
    mapping = painter.combinedTransform()
    inverse, valid = mapping.inverted()
    if not valid:
        return
    requested = QRectF(local_visible)
    # Stable source bounds avoid traversing an entire long export for a small
    # object. Live transforms/ink retain the complete visible request instead.
    if not canvas._projection_has_live_preview():
        from comic_editor.ui.baking import visual_bounds
        bounds = visual_bounds(canvas, "object", obj.object_id)
        parent_inverse, parent_valid = canvas.layer_world_transform(obj.parent_layer_id).inverted()
        if parent_valid and not bounds.isEmpty():
            requested = requested.intersected(parent_inverse.mapRect(bounds).adjusted(-2, -2, 2, 2))
    rectangle = mapping.mapRect(requested).toAlignedRect().intersected(QRect(0, 0, device.width(), device.height()))
    if painter.hasClipping():
        rectangle = rectangle.intersected(mapping.mapRect(painter.clipBoundingRect()).toAlignedRect())
    if rectangle.isEmpty():
        return
    mode = obj.blend_mode
    native = NATIVE_MODES.get(mode)
    if native is None and not isinstance(device, QImage):
        # Document traversal always targets QImages. Interactive transforms
        # bypass detached overlays for blended objects to keep that invariant.
        raise RuntimeError("Custom object blending requires a raster scene capture")
    renderer = None
    if native is None:
        from comic_editor.ui.gpu_object_blending import renderer_for
        renderer = renderer_for(canvas)
    # Larger GPU blocks amortize upload/readback and context switching; CPU
    # float windows still remain at 256 rows, even if the driver falls back.
    capture_side = min(1024, CAPTURE_SIDE * 2) if renderer is not None else CAPTURE_SIDE
    capturing = getattr(canvas, "_blend_capture_objects", None)
    if capturing is None:
        capturing = canvas._blend_capture_objects = set()
    capturing.add(obj.object_id)
    density = max(math.hypot(mapping.m11(), mapping.m12()), math.hypot(mapping.m21(), mapping.m22()))
    try:
        for top in range(rectangle.top(), rectangle.bottom() + 1, capture_side):
            for left in range(rectangle.left(), rectangle.right() + 1, capture_side):
                block = QRect(left, top, min(capture_side, rectangle.right() - left + 1),
                              min(capture_side, rectangle.bottom() - top + 1))
                halo = 1 if mode == "height_modulate" else 0
                capture = block.adjusted(-halo, -halo, halo, halo)
                source = QImage(capture.size(), QImage.Format_ARGB32_Premultiplied)
                if source.isNull():
                    raise MemoryError("Could not allocate object blend block")
                source.fill(Qt.transparent)
                source_painter = QPainter(source)
                try:
                    source_painter.setRenderHints(painter.renderHints())
                    shift = QTransform.fromTranslate(-capture.x(), -capture.y())
                    source_painter.setTransform(mapping * shift)
                    source_visible = inverse.mapRect(QRectF(capture))
                    canvas._render_object(source_painter, obj, parent_opacity, source_visible)
                    if (isinstance(obj, VectorDrawingObject) and not canvas._has_active_modifiers(obj.modifier_ids)
                            and canvas._vector_gesture_mode == "pencil" and canvas.selected_object_id == obj.object_id):
                        source_painter.setOpacity(parent_opacity if obj.opacity_locked else parent_opacity * obj.opacity)
                        canvas._render_modifier_sources.add(("object", obj.object_id))
                        try:
                            canvas._render_modified_vector_pencil_preview(source_painter, obj.parent_layer_id)
                        finally:
                            canvas._render_modifier_sources.discard(("object", obj.object_id))
                finally:
                    source_painter.end()
                # Sparse objects commonly leave whole blocks empty. Inspect
                # source coverage before copying/converting the backdrop.
                words = np.ndarray((source.height(), source.width()), np.uint32,
                                   buffer=source.constBits(), strides=(source.bytesPerLine(), 4))
                if not np.any(words & np.uint32(0xff000000)):
                    continue
                painter.save()
                try:
                    # combinedTransform includes the device pixel ratio. Draw
                    # physical pixels without inheriting the layer transform.
                    painter.resetTransform()
                    ratio = device.devicePixelRatioF()
                    painter.scale(1 / ratio, 1 / ratio)
                    painter.setOpacity(1)
                    if native is not None:
                        painter.setCompositionMode(native)
                        painter.drawImage(block.topLeft(), source)
                    else:
                        core = QRect(halo, halo, block.width(), block.height())
                        destination = device.copy(block)
                        result = renderer.composite(destination, source, mode, core, density) if renderer is not None else None
                        if result is None:
                            result = _custom_composite(destination, source, mode, core, density)
                        painter.setCompositionMode(QPainter.CompositionMode_Source)
                        painter.drawImage(block.topLeft(), result)
                finally:
                    painter.restore()
    finally:
        capturing.discard(obj.object_id)
