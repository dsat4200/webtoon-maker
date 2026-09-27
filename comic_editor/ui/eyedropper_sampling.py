"""Exact, local chapter sampling without viewport overlays or draft effects."""
from collections import OrderedDict
import math

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.ui.document_projection import ProjectionAddress


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


def _presented_color(canvas, x: int, y: int):
    """Read a finished native-resolution document tile already on the canvas.

    Projection tiles contain the composited artwork without selection or grid
    overlays. Only a complete, current view is eligible; a pending repaint or
    a live editing preview must use the direct compositor instead.
    """
    completed = getattr(canvas, "_projection_completed_view", None)
    if (completed is None or completed[2] != canvas._document_projection.revision
            or canvas._projection_has_live_preview()
            or not canvas._uses_document_projection()):
        return None
    configuration, phases, _revision = completed
    if configuration != canvas._projection_configuration():
        return None
    # The exact sampler does not render the editing underlay, selected
    # mask-only artwork, or the optional page overflow presentation.
    view_overflow, underlay, selected_mask_only = (
        configuration[6], configuration[7], configuration[9])
    if view_overflow or underlay[0] or selected_mask_only:
        return None
    address = ProjectionAddress(0, x // canvas._document_projection.tile_size,
                                y // canvas._document_projection.tile_size)
    pixels = []
    for phase, tiles in phases:
        tile = next((tile for tile in tiles if tile.key == (phase, address)), None)
        if tile is None:
            return None
        source = tile.source_rect
        if source is None:
            return None
        px = int(source.x()) + x - int(tile.world_rect.x())
        py = int(source.y()) + y - int(tile.world_rect.y())
        pixels.append((tile.image, px, py))
    if not pixels:
        return None
    if len(pixels) == 1:
        image, px, py = pixels[0]
        return image.pixelColor(px, py).name(QColor.NameFormat.HexArgb).upper()
    # Show-on-top presentation has separate base and promoted-artwork tiles.
    # Compose their one-pixel crops in the same order as the visible canvas.
    result = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
    result.fill(0)
    painter = QPainter(result)
    try:
        for image, px, py in pixels:
            painter.drawImage(0, 0, image, px, py, 1, 1)
    finally:
        painter.end()
    return result.pixelColor(0, 0).name(QColor.NameFormat.HexArgb).upper()


class EyedropperSampler:
    """Reuse finished chapter pixels, then cache exact local renders as needed.

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
            color = _presented_color(canvas, x, y)
            if color is not None:
                return color
            image = render_sample_region(canvas, key[0] * self.tile_size,
                                         key[1] * self.tile_size, self.tile_size)
        self.tiles[key] = image
        while len(self.tiles) > self.max_tiles:
            self.tiles.popitem(last=False)
        return image.pixelColor(x % self.tile_size + 1, y % self.tile_size + 1).name(
            QColor.NameFormat.HexArgb).upper()
