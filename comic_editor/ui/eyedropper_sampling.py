"""Exact, local chapter sampling without viewport overlays or draft effects."""
from collections import OrderedDict
import math

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform


def _pixel(canvas, world):
    chapter = canvas.chapter
    if chapter is None or not math.isfinite(world.x()) or not math.isfinite(world.y()):
        return None
    x, y = math.floor(world.x()), math.floor(world.y())
    return (x, y) if 0 <= x < chapter.width and 0 <= y < chapter.height else None


def render_sample_region(canvas, x, y, size=1):
    """Render integer-aligned pixels with a one-pixel antialiasing guard.

    Culling is enabled independently of interactive rendering. This retains
    full-resolution effects and normal mask-only visibility, while independent
    effect, mask, mirror and halftone source captures still bypass culling.
    """
    image = QImage(size + 2, size + 2, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(canvas.chapter.background))
    bounds = canvas._render_bounds
    previous_interactive = canvas._interactive_render
    previous_exact = bounds.exact_sampling
    previous_margin = bounds.margin
    painter = QPainter(image)
    try:
        canvas._interactive_render = False
        bounds.exact_sampling = True
        bounds.prepare()
        bounds.margin = 2.0  # Full-resolution pixels, independent of camera zoom.
        painter.setRenderHint(QPainter.Antialiasing, True)
        transform = QTransform()
        transform.translate(1 - x, 1 - y)
        painter.setTransform(transform)
        visible = QRectF(x - 1, y - 1, size + 2, size + 2)
        canvas._render_scene_layers(painter, visible)
    finally:
        painter.end()
        bounds.exact_sampling = previous_exact
        bounds.margin = previous_margin
        canvas._interactive_render = previous_interactive
    return image


def sample_color(canvas, world: QPointF):
    """Uncached public sampling honors even direct in-memory artwork changes."""
    point = _pixel(canvas, world)
    if point is None:
        return None
    # This public helper is also used by callers making direct model/tile edits
    # before emitting a visual-change signal. Gesture sampling has its own cache
    # and reuses prepared bounds, but this entry point must see those edits.
    canvas._render_bounds.clear()
    image = render_sample_region(canvas, *point)
    return image.pixelColor(1, 1).name(QColor.NameFormat.HexArgb).upper()


class EyedropperSampler:
    """Small full-resolution tile cache, reset at each gesture and visual edit.

    The viewport image contains grid/selection overlays and zoomed pixels, so
    it is deliberately not used. Eight 66x66 RGBA images retain at most 140 KiB.
    """
    tile_size = 64
    max_tiles = 8

    def __init__(self, canvas):
        self.canvas = canvas
        self.tiles = OrderedDict()
        self.identity = None
        for signal in (canvas.documentChanged, canvas.hierarchyChanged,
                       canvas.visualChanged, canvas.soloChanged):
            signal.connect(self.clear)

    def clear(self, *_args):
        self.tiles.clear()
        self.identity = None

    def sample(self, world):
        canvas = self.canvas
        point = _pixel(canvas, world)
        if point is None:
            return None
        identity = (id(canvas.chapter), id(canvas.tiles), canvas._solo_signature())
        if self.identity != identity:
            self.clear()
            self.identity = identity
        x, y = point
        key = (x // self.tile_size, y // self.tile_size)
        image = self.tiles.pop(key, None)
        if image is None:
            image = render_sample_region(canvas, key[0] * self.tile_size,
                                         key[1] * self.tile_size, self.tile_size)
        self.tiles[key] = image
        while len(self.tiles) > self.max_tiles:
            self.tiles.popitem(last=False)
        return image.pixelColor(x % self.tile_size + 1, y % self.tile_size + 1).name(
            QColor.NameFormat.HexArgb).upper()
