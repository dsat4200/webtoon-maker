"""Present retained document tiles without a camera-sized intermediate image.

The caller owns tile generation, revision keys, and resolution selection. This
module only places completed tiles under a camera transform. GPU resources live
in the widget's context and survive camera changes; raster presentation uses the
same inputs. Neither path changes document data or reads pixels back from GL.
"""
from __future__ import annotations

from collections import OrderedDict
import ctypes
from dataclasses import dataclass
import logging
from typing import Hashable, Iterable
import weakref

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QColor, QImage, QOpenGLContext, QPainter, QPainterPath,
    QPainterPathStroker, QPen, QPolygonF, QTransform,
)
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLFunctions_3_3_Core, QOpenGLShader,
    QOpenGLShaderProgram, QOpenGLTexture, QOpenGLVertexArrayObject,
)
from shiboken6 import isValid


@dataclass(frozen=True)
class PresentedTile:
    """A document rectangle and its completed pixels, optionally with gutters.

    ``source_rect`` is in image pixels (ignoring QImage.devicePixelRatio). Its
    pixels map to ``world_rect``; image pixels outside it supply filter gutters.
    ``key`` identifies a logical tile and must not include camera state or its
    image revision. QImage.cacheKey supplies the revision; replacing a tile
    retires obsolete texture pixels while unrelated tiles remain resident.
    """

    key: Hashable
    image: QImage
    world_rect: QRectF
    source_rect: QRectF | None = None


@dataclass(frozen=True)
class PresentationStats:
    backend: str
    tiles: int
    uploads: int = 0
    texture_bytes: int = 0


@dataclass
class _TextureEntry:
    texture: QOpenGLTexture
    byte_count: int


VERTEX = """#version 330 core
layout(location=0) in vec2 position;
layout(location=1) in vec2 coordinate;
out vec2 uv;
void main() { uv=coordinate; gl_Position=vec4(position,0,1); }
"""
FRAGMENT = """#version 330 core
uniform sampler2D source;
uniform float opacity;
in vec2 uv;
out vec4 color;
void main() { color=texture(source,uv)*opacity; }
"""


def _tile_rectangles(tile: PresentedTile, clip_world: QRectF | None):
    """Clip the destination and crop the source in the same proportion."""
    world = QRectF(tile.world_rect)
    source = (QRectF(tile.source_rect) if tile.source_rect is not None
              else QRectF(0, 0, tile.image.width(), tile.image.height()))
    if tile.image.isNull() or world.isEmpty() or source.isEmpty():
        return None
    clipped = world if clip_world is None else world.intersected(clip_world)
    if clipped.isEmpty():
        return None
    sx, sy = source.width() / world.width(), source.height() / world.height()
    return clipped, QRectF(
        source.x() + (clipped.x() - world.x()) * sx,
        source.y() + (clipped.y() - world.y()) * sy,
        clipped.width() * sx, clipped.height() * sy,
    )


def tile_vertices(tile: PresentedTile, camera: QTransform, viewport: QSizeF,
                  clip_world: QRectF | None = None) -> np.ndarray:
    """Map one tile to two triangles, retaining precision at large page offsets."""
    rectangles = _tile_rectangles(tile, clip_world)
    if rectangles is None or viewport.width() <= 0 or viewport.height() <= 0:
        return np.empty((0, 4), dtype=np.float32)
    world, source = rectangles
    points = (world.topLeft(), world.topRight(), world.bottomLeft(), world.bottomRight())
    pixels = (source.topLeft(), source.topRight(), source.bottomLeft(), source.bottomRight())
    vertices = []
    for index in (0, 1, 2, 2, 1, 3):
        point, pixel = camera.map(points[index]), pixels[index]
        vertices.append((
            point.x() * 2 / viewport.width() - 1,
            1 - point.y() * 2 / viewport.height(),
            pixel.x() / tile.image.width(), pixel.y() / tile.image.height(),
        ))
    return np.asarray(vertices, dtype=np.float32)


class GpuTilePresenter:
    """Bounded tile textures in the *current* context; no offscreen renderer.

    Call ``draw`` between QPainter.beginNativePainting/endNativePainting, or
    with a framebuffer already bound in a test. Context loss discards only GPU
    resources; the owner's retained tile images can be uploaded again.
    """

    def __init__(self, owner=None, *, byte_limit=256 * 1024 * 1024):
        self.byte_limit = max(0, int(byte_limit))
        self.texture_bytes = 0
        self.uploads = self.draws = 0
        self.reason = ""
        self._ready = False
        self.context = self.functions = self.program = self.buffer = self.vao = None
        self._textures: OrderedDict[tuple, _TextureEntry] = OrderedDict()
        self._owner = weakref.ref(owner) if owner is not None else None

    def _initialize(self):
        context = QOpenGLContext.currentContext()
        if context is None:
            raise RuntimeError("No current OpenGL context")
        if self.context is context and self._ready:
            return
        if self.context is not None:
            self.close()
        self.context = context
        self.functions = QOpenGLFunctions_3_3_Core()
        if not self.functions.initializeOpenGLFunctions():
            raise RuntimeError("OpenGL 3.3 unavailable")
        self.program = QOpenGLShaderProgram()
        if (not self.program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX)
                or not self.program.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAGMENT)
                or not self.program.link()):
            raise RuntimeError(self.program.log())
        self.vao = QOpenGLVertexArrayObject()
        self.buffer = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        if not self.vao.create() or not self.buffer.create():
            raise RuntimeError("Could not allocate presentation geometry")
        context.aboutToBeDestroyed.connect(self.close)
        self._ready = True

    @staticmethod
    def _key(tile):
        # QImage's token changes when a tile is replaced or detached for edits.
        return tile.key, int(tile.image.cacheKey())

    def _remove(self, key):
        entry = self._textures.pop(key)
        entry.texture.destroy()
        self.texture_bytes -= entry.byte_count

    def _upload(self, tile):
        rgba = tile.image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
        # Preserve premultiplication: Qt's convenience texture constructor would
        # otherwise convert premultiplied images to straight RGBA before upload.
        raw = QImage(rgba.constBits(), rgba.width(), rgba.height(),
                     rgba.bytesPerLine(), QImage.Format_RGBA8888)
        texture = QOpenGLTexture(raw, QOpenGLTexture.DontGenerateMipMaps)
        if not texture.isCreated():
            raise RuntimeError("Could not allocate a document tile texture")
        texture.setWrapMode(QOpenGLTexture.ClampToEdge)
        entry = _TextureEntry(texture, tile.image.width() * tile.image.height() * 4)
        self._textures[self._key(tile)] = entry
        self.texture_bytes += entry.byte_count
        self.uploads += 1

    def _save_state(self):
        gl = self.functions
        values = {name: gl.glGetIntegerv(name) for name in (
            0x8B8D,  # GL_CURRENT_PROGRAM
            0x85B5,  # GL_VERTEX_ARRAY_BINDING
            0x8894,  # GL_ARRAY_BUFFER_BINDING
            0x84E0,  # GL_ACTIVE_TEXTURE
            0x80C9, 0x80C8, 0x80CB, 0x80CA,  # blend src/dst RGB/alpha
            0x8009, 0x883D,  # blend equations RGB/alpha
        )}
        values["enabled"] = {name: gl.glIsEnabled(name) for name in (
            0x0B44, 0x0B71, 0x0B90, 0x0C11, 0x0BE2,
        )}
        values[0x0BA2] = self._integer_vector(0x0BA2, 4)  # GL_VIEWPORT
        values["color_mask"] = self._integer_vector(0x0C23, 4)
        gl.glActiveTexture(0x84C0)
        values["texture0"] = gl.glGetIntegerv(0x8069)
        return values

    def _integer_vector(self, name, count):
        # PySide's generated glGetIntegerv wrapper allocates one integer even
        # for multi-value queries in some releases, causing memory corruption.
        # Resolve through Qt and supply correctly sized storage explicitly.
        convention = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        query = convention(None, ctypes.c_uint, ctypes.POINTER(ctypes.c_int))(
            self.context.getProcAddress(b"glGetIntegerv")
        )
        values = (ctypes.c_int * count)()
        query(name, values)
        return list(values)

    def _restore_state(self, values):
        gl = self.functions
        gl.glViewport(*values[0x0BA2])
        gl.glColorMask(*values["color_mask"])
        gl.glBlendFuncSeparate(values[0x80C9], values[0x80C8],
                               values[0x80CB], values[0x80CA])
        gl.glBlendEquationSeparate(values[0x8009], values[0x883D])
        for name, enabled in values["enabled"].items():
            (gl.glEnable if enabled else gl.glDisable)(name)
        gl.glActiveTexture(0x84C0)
        gl.glBindTexture(0x0DE1, values["texture0"])
        gl.glActiveTexture(values[0x84E0])
        gl.glUseProgram(values[0x8B8D])
        gl.glBindVertexArray(values[0x85B5])
        gl.glBindBuffer(0x8892, values[0x8894])

    def draw(self, tiles: Iterable[PresentedTile], camera: QTransform,
             viewport: QSizeF, *, device_pixel_ratio=1.0, smooth=True,
             clip_world: QRectF | None = None, opacity=1.0) -> bool:
        """Draw into the current framebuffer; False requests raster fallback.

        Memory and resource checks finish before the first draw, so a normal
        unavailable/over-budget fallback never composites translucent tiles twice.
        """
        if viewport.width() <= 0 or viewport.height() <= 0:
            return True
        prepared = [(tile, tile_vertices(tile, camera, viewport, clip_world)) for tile in tiles]
        prepared = [(tile, vertices) for tile, vertices in prepared if len(vertices)]
        if not prepared:
            return True
        needed = {self._key(tile): tile for tile, _ in prepared}
        size = sum(tile.image.width() * tile.image.height() * 4 for tile in needed.values())
        if size > self.byte_limit:
            self.reason = "Visible tile textures exceed the presentation budget"
            return False
        state = None
        try:
            self._initialize()
            state = self._save_state()
            max_texture_size = self.functions.glGetIntegerv(0x0D33)
            if any(max(tile.image.width(), tile.image.height()) > max_texture_size
                   for tile in needed.values()):
                self.reason = "A document tile exceeds the driver's texture limit"
                return False
            logical_keys = {tile.key for tile in needed.values()}
            for key in list(self._textures):
                if key[0] in logical_keys and key not in needed:
                    self._remove(key)
            missing = sum(tile.image.width() * tile.image.height() * 4
                          for key, tile in needed.items() if key not in self._textures)
            for key in list(self._textures):
                if self.texture_bytes + missing <= self.byte_limit:
                    break
                if key not in needed:
                    self._remove(key)
            for key, tile in needed.items():
                if key not in self._textures:
                    self._upload(tile)
                self._textures.move_to_end(key)
            gl = self.functions
            self.vao.bind()
            self.buffer.bind()
            vertices = np.concatenate([vertices for _, vertices in prepared]).tobytes()
            self.buffer.allocate(vertices, len(vertices))
            if not self.program.bind():
                raise RuntimeError("Could not bind tile presentation program")
            self.program.enableAttributeArray(0)
            self.program.enableAttributeArray(1)
            self.program.setAttributeBuffer(0, 0x1406, 0, 2, 16)
            self.program.setAttributeBuffer(1, 0x1406, 8, 2, 16)
            gl.glViewport(0, 0, round(viewport.width() * device_pixel_ratio),
                          round(viewport.height() * device_pixel_ratio))
            gl.glDisable(0x0B44)  # GL_CULL_FACE
            gl.glDisable(0x0B71)  # GL_DEPTH_TEST
            gl.glDisable(0x0B90)  # GL_STENCIL_TEST
            gl.glDisable(0x0C11)  # GL_SCISSOR_TEST; caller supplies document clipping
            gl.glEnable(0x0BE2)   # GL_BLEND
            gl.glBlendEquation(0x8006)  # GL_FUNC_ADD
            gl.glBlendFunc(1, 0x0303)  # ONE, ONE_MINUS_SRC_ALPHA (premultiplied)
            gl.glColorMask(True, True, True, True)
            gl.glUniform1i(self.program.uniformLocation("source"), 0)
            gl.glUniform1f(self.program.uniformLocation("opacity"), float(opacity))
            sampling = QOpenGLTexture.Linear if smooth else QOpenGLTexture.Nearest
            for index, (tile, _) in enumerate(prepared):
                texture = self._textures[self._key(tile)].texture
                texture.setMinMagFilters(sampling, sampling)
                texture.bind(0)
                gl.glDrawArrays(0x0004, index * 6, 6)  # GL_TRIANGLES
            self.draws += 1
            self.reason = ""
            return True
        except Exception as error:
            self.reason = str(error)
            logging.getLogger(__name__).warning("Tile presentation fallback: %s", error)
            return False
        finally:
            if self.buffer is not None:
                self.buffer.release()
            if self.vao is not None:
                self.vao.release()
            if self.program is not None:
                self.program.release()
            if state is not None:
                self._restore_state(state)

    def close(self):
        """Release resources in their owning context, including widget teardown."""
        context = self.context
        if context is None:
            return
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous is not None and isValid(previous) else None
        switched = previous is not context
        activated = not switched
        try:
            # Qt may deliver its context destruction callback after the owning
            # widget's Python wrapper has become invalid (notably app shutdown).
            # In that case the context can still release its own resources; if
            # the context is gone too, Qt has already destroyed the GL objects.
            if not isValid(context):
                return
            if switched:
                owner = self._owner() if self._owner is not None else None
                if owner is not None and isValid(owner):
                    owner.makeCurrent()
                    activated = QOpenGLContext.currentContext() is context
                else:
                    surface = context.surface()
                    activated = bool(surface is not None and isValid(surface)
                                     and context.makeCurrent(surface))
                if not activated:
                    return
            for key in list(self._textures):
                self._remove(key)
            if self.buffer is not None:
                self.buffer.destroy()
            if self.vao is not None:
                self.vao.destroy()
        finally:
            try:
                if isValid(context):
                    context.aboutToBeDestroyed.disconnect(self.close)
            except (RuntimeError, TypeError):
                pass
            self._textures.clear()
            self.texture_bytes = 0
            self.program = self.buffer = self.vao = self.functions = None
            self.context = None
            self._ready = False
            if switched and activated and isValid(context):
                context.doneCurrent()
                if (previous is not None and isValid(previous)
                        and previous_surface is not None and isValid(previous_surface)):
                    previous.makeCurrent(previous_surface)


def draw_document_tiles(painter: QPainter, tiles: Iterable[PresentedTile],
                        camera: QTransform, viewport_size: QSizeF, *, owner=None,
                        smooth=True, clip_world: QRectF | None = None) -> PresentationStats:
    """Present tiles, preserving the painter for subsequent tool/grid overlays.

    An arbitrary existing painter clip or nonstandard composition mode uses the
    raster fallback. The native path supports axis-aligned document clipping via
    ``clip_world`` and lets the viewport clip rotated camera geometry naturally.
    """
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    tiles = tuple(tiles)
    if (isinstance(owner, QOpenGLWidget) and painter.device() is owner
            and not painter.hasClipping()
            and painter.compositionMode() == QPainter.CompositionMode_SourceOver):
        presenter = getattr(owner, "_document_tile_presenter", None)
        if presenter is None:
            presenter = GpuTilePresenter(owner)
            owner._document_tile_presenter = presenter
        uploads_before = presenter.uploads
        painter.beginNativePainting()
        try:
            rendered = presenter.draw(
                tiles, camera, QSizeF(viewport_size), smooth=smooth,
                device_pixel_ratio=owner.devicePixelRatioF(),
                clip_world=clip_world, opacity=painter.opacity(),
            )
        finally:
            painter.endNativePainting()
        if rendered:
            return PresentationStats("gpu", len(tiles), presenter.uploads - uploads_before,
                                     presenter.texture_bytes)
    painter.save()
    painter.setTransform(camera)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, bool(smooth))
    for tile in tiles:
        rectangles = _tile_rectangles(tile, clip_world)
        if rectangles is not None:
            world, source = rectangles
            painter.drawImage(world, tile.image, source)
    painter.restore()
    return PresentationStats("raster", len(tiles))


def _border_patches(polygon: QPolygonF, viewport: QSizeF, ratio: float):
    """Disjoint, tightly cropped raster patches for a cosmetic screen outline.

    The screen is partitioned in physical pixels before cropping each patch to
    the stroked path. Corners and adjacent patches therefore never blend twice.
    The 3px search stroke encloses the 1px pen plus antialiasing coverage.
    """
    ratio = max(.01, float(ratio))
    physical = QTransform.fromScale(ratio, ratio).map(polygon)
    path = QPainterPath()
    path.addPolygon(physical)
    path.closeSubpath()
    stroker = QPainterPathStroker()
    stroker.setWidth(3 * ratio)
    coverage = stroker.createStroke(path)
    viewport_rect = QRect(0, 0, round(viewport.width() * ratio),
                          round(viewport.height() * ratio))
    if not coverage.intersects(QRectF(viewport_rect)):
        return []
    bounds = coverage.boundingRect().toAlignedRect().intersected(viewport_rect)
    if bounds.isEmpty():
        return []
    patches = []
    side = 128
    for y in range(bounds.top() // side * side, bounds.bottom() + 1, side):
        for x in range(bounds.left() // side * side, bounds.right() + 1, side):
            cell = QRect(x, y, side, side).intersected(viewport_rect)
            clipping = QPainterPath()
            clipping.addRect(QRectF(cell))
            cropped = coverage.intersected(clipping).boundingRect().toAlignedRect().intersected(cell)
            if cropped.isEmpty():
                continue
            image = QImage(cropped.size(), QImage.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(ratio)
            image.fill(Qt.transparent)
            raster = QPainter(image)
            raster.setRenderHint(QPainter.Antialiasing, True)
            raster.translate(-cropped.x() / ratio, -cropped.y() / ratio)
            raster.setPen(QPen(QColor("#44444d"), 1))
            raster.setBrush(Qt.NoBrush)
            raster.drawPolygon(polygon)
            raster.end()
            patches.append((QPointF(cropped.x() / ratio, cropped.y() / ratio), image))
    return patches


def draw_document_border(painter: QPainter, chapter_rect: QRectF, camera: QTransform,
                         viewport_size: QSizeF, *, owner=None) -> None:
    """Keep the chapter decoration's raster AA on a non-multisampled GPU widget.

    This caches only the thin screen-space border, never document artwork. Qt's
    GL paint engine aliases thin vector strokes when the widget has no MSAA;
    small raster coverage patches preserve the established canvas appearance.
    """
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    polygon = camera.map(QPolygonF(chapter_rect))
    painter.save()
    try:
        painter.setTransform(QTransform())
        if not (isinstance(owner, QOpenGLWidget) and painter.device() is owner):
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(QPen(QColor("#44444d"), 1))
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(polygon)
            return
        ratio = owner.devicePixelRatioF()
        viewport = QSizeF(viewport_size)
        key = (tuple((point.x(), point.y()) for point in polygon), ratio,
               viewport.width(), viewport.height())
        cache = getattr(owner, "_document_border_cache", None)
        if cache is None or cache[0] != key:
            cache = key, _border_patches(polygon, viewport, ratio)
            owner._document_border_cache = cache
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        for position, image in cache[1]:
            painter.drawImage(position, image)
    finally:
        painter.restore()
