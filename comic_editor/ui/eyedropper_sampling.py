"""Exact, local chapter sampling without viewport overlays or draft effects."""
from collections import OrderedDict
import math

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.ui.document_projection import ProjectionAddress
from comic_editor.render.pixels import current_contract, working_color, display_image, pixel_scope


def _pixel(canvas, world):
    chapter = canvas.chapter
    if chapter is None or not math.isfinite(world.x()) or not math.isfinite(world.y()):
        return None
    x, y = math.floor(world.x()), math.floor(world.y())
    return (x, y) if 0 <= x < chapter.width and 0 <= y < chapter.height else None


def render_sample_region(canvas, x, y, size=1):
    """Render integer-aligned pixels with a one-pixel antialiasing guard.

    Exact region requests retain full-resolution effects without evaluating
    distant pattern pixels. Selected mask-only artwork and live previews use
    the ordinary compositor so sampling keeps their established visibility.
    """
    image = QImage(size + 2, size + 2, current_contract().image_format)
    image.fill(working_color(canvas.chapter.background))
    bounds = canvas._render_bounds
    previous = (canvas._interactive_render, canvas._effect_region_requests,
                canvas._projection_exact, getattr(canvas, "_effect_viewport_world", None),
                getattr(canvas, "_projection_defer_effects", False))
    previous_exact = bounds.exact_sampling
    previous_margin = bounds.margin
    painter = QPainter(image)
    try:
        visible = QRectF(x - 1, y - 1, size + 2, size + 2)
        selected = (canvas.chapter.objects if canvas.selected_kind == "object"
                    else canvas.chapter.layers).get(canvas.selected_id)
        regional = (not (selected is not None and selected.mask_only)
                    and not canvas._projection_has_live_preview())
        canvas._interactive_render = regional
        canvas._effect_region_requests = regional
        canvas._projection_exact = regional
        canvas._effect_viewport_world = visible
        canvas._projection_defer_effects = False
        bounds.exact_sampling = True
        bounds.prepare()
        bounds.margin = 2.0  # Full-resolution pixels, independent of camera zoom.
        painter.setRenderHint(QPainter.Antialiasing, True)
        transform = QTransform()
        transform.translate(1 - x, 1 - y)
        painter.setTransform(transform)
        canvas._render_scene_layers(painter, visible)
    finally:
        painter.end()
        bounds.exact_sampling = previous_exact
        bounds.margin = previous_margin
        (canvas._interactive_render, canvas._effect_region_requests,
         canvas._projection_exact, canvas._effect_viewport_world,
         canvas._projection_defer_effects) = previous
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
    with pixel_scope(canvas.chapter.pixel_contract):
        image = display_image(render_sample_region(canvas, *point), canvas.chapter.pixel_contract)
    return image.pixelColor(1, 1).name(QColor.NameFormat.HexArgb).upper()


def _presented_color(canvas, x: int, y: int):
    """Read a finished native-resolution document tile already on the canvas.

    Projection tiles contain the composited artwork without selection or grid
    overlays. A finished tile in a partially loaded view is exact too; live
    editing previews still use the direct compositor.
    """
    if canvas._projection_has_live_preview() or not canvas._uses_document_projection():
        return None
    configuration = canvas._projection_configuration()
    # The exact sampler does not render the editing underlay, selected
    # mask-only artwork, or the optional page overflow presentation.
    if configuration[6] or configuration[7][0] or configuration[9]:
        return None
    address = ProjectionAddress(0, x // canvas._document_projection.tile_size,
                                y // canvas._document_projection.tile_size)
    pixels = None
    for view in (getattr(canvas, "_projection_progress_view", None),
                 getattr(canvas, "_projection_completed_view", None)):
        if (view is None or view[0] != configuration
                or view[2] != canvas._document_projection.revision):
            continue
        candidate = []
        for phase, tiles in view[1]:
            tile = next((tile for tile in tiles if tile.key == (phase, address)), None)
            if tile is None or tile.source_rect is None:
                break
            source = tile.source_rect
            px = int(source.x()) + x - int(tile.world_rect.x())
            py = int(source.y()) + y - int(tile.world_rect.y())
            if not isinstance(tile.image, QImage):
                canvas._scene_controller.ensure_cpu_tiles([tile])
                break
            candidate.append((tile, px, py))
        else:
            if candidate:
                pixels = candidate
                break
    if not pixels:
        return None
    if len(pixels) == 1:
        tile, px, py = pixels[0]
        with pixel_scope(tile.pixel_contract, environment=tile.pixel_environment):
            image = display_image(tile.image.copy(px, py, 1, 1), tile.pixel_contract)
        return image.pixelColor(0, 0).name(QColor.NameFormat.HexArgb).upper()
    # Show-on-top presentation has separate base and promoted-artwork tiles.
    # Compose their one-pixel crops in the same order as the visible canvas.
    contract, environment = pixels[0][0].pixel_contract, pixels[0][0].pixel_environment
    result = QImage(1, 1, contract.image_format)
    result.fill(0)
    painter = QPainter(result)
    try:
        for tile, px, py in pixels:
            painter.drawImage(0, 0, tile.image, px, py, 1, 1)
    finally:
        painter.end()
    with pixel_scope(contract, environment=environment):
        result = display_image(result, contract)
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
        self.serial = 0
        self.pending = False
        self.finished = None
        self.last_color = None
        self.last_error = None
        for signal in (canvas.documentChanged, canvas.hierarchyChanged,
                       canvas.visualChanged, canvas.soloChanged):
            signal.connect(self.clear)

    def clear(self, *_args):
        finished, self.finished = self.finished, None
        if finished is not None:
            finished(None)
        self.tiles.clear()
        self.identity = None
        self.serial += 1
        self.pending = False
        self.last_color = None

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
            with pixel_scope(canvas.chapter.pixel_contract):
                image = display_image(render_sample_region(canvas, key[0] * self.tile_size,
                                         key[1] * self.tile_size, self.tile_size), canvas.chapter.pixel_contract)
        self.tiles[key] = image
        while len(self.tiles) > self.max_tiles:
            self.tiles.popitem(last=False)
        return image.pixelColor(x % self.tile_size + 1, y % self.tile_size + 1).name(
            QColor.NameFormat.HexArgb).upper()

    def request(self, world, accept):
        """Production gestures retain ready pixels and stage cold source work."""
        from comic_editor.render.source_sampling import eyedropper_region
        from comic_editor.ui.scene_consumers import scene_consumers
        canvas, point = self.canvas, _pixel(self.canvas, world)
        if point is None:
            return False
        identity = (id(canvas.chapter), id(canvas.tiles), canvas._solo_signature())
        if self.identity != identity:
            self.clear()
            self.identity = identity
        self.serial += 1
        serial, (x, y) = self.serial, point
        key = x//self.tile_size, y//self.tile_size
        image = self.tiles.pop(key, None)
        color = _presented_color(canvas, x, y) if image is None else None
        if image is not None:
            self.tiles[key] = image
            color = image.pixelColor(x%self.tile_size+1, y%self.tile_size+1).name(
                QColor.NameFormat.HexArgb).upper()
        if color is not None:
            self.pending = False
            self.last_color = color
            accept(color)
            return True
        self.pending = True
        def ready(image, error):
            if serial != self.serial or identity != self.identity:
                return
            self.pending = False
            self.last_error = error
            if error is None:
                self.tiles[key] = image
                while len(self.tiles) > self.max_tiles:
                    self.tiles.popitem(last=False)
                color = image.pixelColor(x%self.tile_size+1, y%self.tile_size+1).name(
                    QColor.NameFormat.HexArgb).upper()
                self.last_color = color
                accept(color)
            finished, self.finished = self.finished, None
            if finished is not None:
                finished(self.last_color)
        scene_consumers(canvas).request(('eyedropper',), eyedropper_region,
            (key[0]*self.tile_size, key[1]*self.tile_size, self.tile_size), ready)
        return True

    def finish(self, accept):
        if self.pending:
            self.finished = accept
        else:
            accept(self.last_color)
