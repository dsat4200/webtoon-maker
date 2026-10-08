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
from comic_editor.core.pixel_contract import LEGACY_PIXELS
from comic_editor.render.device import DeviceImage
from comic_editor.render.pixels import display_image, color_environment,pixel_scope


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
    image: QImage | DeviceImage
    world_rect: QRectF
    source_rect: QRectF | None = None
    pixel_contract: object = LEGACY_PIXELS
    pixel_environment: object = None


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
    sampling: object = None


class _BorrowedTexture:
    """Hold a lease; texture ownership and deletion stay on the producer."""
    def __init__(self, image, functions):
        self.image,self.functions = image.copy(image.rect()),functions
        if not isinstance(self.image,DeviceImage):
            raise RuntimeError('Graphics storage expired before presentation borrowed it')

    def bind(self, unit=0):
        self.functions.glActiveTexture(0x84C0+unit)
        self.functions.glBindTexture(0x0DE1,self.image.texture)
        self.functions.glTexParameteri(0x0DE1,0x2802,0x812F)
        self.functions.glTexParameteri(0x0DE1,0x2803,0x812F)

    def setMinMagFilters(self, minimum, maximum):
        self.bind()
        self.functions.glTexParameteri(0x0DE1,0x2801,int(getattr(minimum,'value',minimum)))
        self.functions.glTexParameteri(0x0DE1,0x2800,int(getattr(maximum,'value',maximum)))

    def destroy(self):
        # Presentation owns an independent view, so releasing the caller's
        # view cannot expire a borrowed texture still retained in this cache.
        if self.image is not None:
            self.image.release()
        self.image = None


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
        origin = getattr(tile.image,'source_origin',(0,0))
        width = getattr(tile.image,'texture_width',tile.image.width())
        height = getattr(tile.image,'texture_height',tile.image.height())
        vertices.append((
            point.x() * 2 / viewport.width() - 1,
            1 - point.y() * 2 / viewport.height(),
            (pixel.x()+origin[0]) / width, (pixel.y()+origin[1]) / height,
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
        self._geometry = OrderedDict()
        self._geometry_configuration = None
        self.geometry_limit = 4096
        self.geometry_builds = self.geometry_hits = 0
        self.geometry_uploads = 0
        self._uploaded_geometry = ()
        self._max_texture_size = None
        self._owner = weakref.ref(owner) if owner is not None else None

    def _prepare_geometry(self, tiles, camera, viewport, clip_world):
        # Ink changes pixel revisions frequently while these screen positions
        # remain fixed. Keep geometry independent of texture content/revisions.
        configuration = (tuple(getattr(camera, f'm{row}{column}')()
            for row in range(1, 4) for column in range(1, 4)),
            viewport.width(), viewport.height())
        if configuration != self._geometry_configuration:
            self._geometry.clear()
            self._geometry_configuration = configuration
        prepared = []
        for tile in tiles:
            # Exact and live feedback batches may alternate document clips
            # under the same camera. Their independent positions share this
            # bounded LRU instead of retiring one another on every draw.
            key = (None if clip_world is None else tuple(clip_world.getRect()),
                   tuple(tile.world_rect.getRect()),
                   None if tile.source_rect is None else tuple(tile.source_rect.getRect()),
                   tile.image.width(), tile.image.height(),
                   getattr(tile.image,'texture_width',tile.image.width()),
                   getattr(tile.image,'texture_height',tile.image.height()),
                   getattr(tile.image,'source_origin',(0,0)))
            vertices = self._geometry.pop(key, None)
            if vertices is None:
                vertices = tile_vertices(tile, camera, viewport, clip_world)
                vertices.setflags(write=False)
                self.geometry_builds += 1
            else:
                self.geometry_hits += 1
            self._geometry[key] = vertices
            while len(self._geometry) > self.geometry_limit:
                self._geometry.popitem(last=False)
            if len(vertices):
                prepared.append((tile, vertices))
        return prepared

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
        self._max_texture_size = int(self.functions.glGetIntegerv(0x0D33))
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
        return (tile.key, int(tile.image.cacheKey()),tile.pixel_contract.signature,
                tile.pixel_environment.signature if tile.pixel_environment is not None
                else color_environment(tile.pixel_contract))

    def _remove(self, key):
        entry = self._textures.pop(key)
        entry.texture.destroy()
        self.texture_bytes -= entry.byte_count

    def _upload(self, tile):
        if isinstance(tile.image,DeviceImage):
            self._textures[self._key(tile)] = _TextureEntry(_BorrowedTexture(tile.image,self.functions),0)
            return
        with pixel_scope(tile.pixel_contract,environment=tile.pixel_environment):
            rgba = display_image(tile.image,tile.pixel_contract).convertToFormat(QImage.Format_RGBA8888_Premultiplied)
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
        prepared = self._prepare_geometry(tiles, camera, viewport, clip_world)
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
            from comic_editor.render.gpu.sync import GlSync
            for tile in needed.values():
                if isinstance(tile.image,DeviceImage):
                    image = tile.image
                    worker = image.owner
                    if (image.isNull() or worker.render_context is None
                            or not QOpenGLContext.areSharing(self.context,worker.render_context)
                            or tile.pixel_contract != image.contract):
                        self.reason = 'A device image needs its owning shared context'
                        return False
                    if not GlSync(self.context).ready(image.fence):
                        self.reason = 'A device image is not ready for presentation'
                        return False
            if any(max(tile.image.width(), tile.image.height()) > self._max_texture_size
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
            geometry = tuple(vertices for _, vertices in prepared)
            if (len(geometry) != len(self._uploaded_geometry)
                    or any(current is not previous for current, previous in zip(geometry, self._uploaded_geometry))):
                vertices = np.concatenate(geometry).tobytes()
                self.buffer.allocate(vertices, len(vertices))
                self._uploaded_geometry = geometry
                self.geometry_uploads += 1
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
                entry = self._textures[self._key(tile)]
                texture = entry.texture
                if entry.sampling != sampling:
                    texture.setMinMagFilters(sampling, sampling)
                    entry.sampling = sampling
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
            self._uploaded_geometry = ()
            self._max_texture_size = None
            if switched and activated and isValid(context):
                context.doneCurrent()
                if (previous is not None and isValid(previous)
                        and previous_surface is not None and isValid(previous_surface)):
                    previous.makeCurrent(previous_surface)



# Presentation-only native frames: never artwork/exact/durable cache entries.
_NATIVE_RASTER_MOSAIC_BYTES = 4 * 1024 * 1024
_NATIVE_RASTER_MOSAIC_SIDE = 2048
_NATIVE_RASTER_MOSAIC_TILES = 32


def _discard_native_raster_mosaic(owner):
    if owner is not None:
        owner._native_raster_mosaic = None


def _native_raster_mosaic(owner, tiles, camera, viewport_size, native_bounds,
                          context, *, smooth=True, presentation_dpr=1.):
    """Join a small complete current native batch without changing any pixels.

    The caller proves that the owned batch is already-current exact artwork.
    Only Legacy ARGB32 native tiles are supported initially. All other formats,
    missing coverage, large views and non-axis-aligned cameras keep their prior
    presentation path. There is at most one4MiB surface, no retained tile handles
    and no scene/source/effect evaluation or native/durable result admission.
    """
    import math

    def reject():
        _discard_native_raster_mosaic(owner)
        return None
    if (owner is None or context is None or native_bounds is None
            or not math.isfinite(presentation_dpr) or presentation_dpr <= 0
            or not 2 <= len(tiles) <= _NATIVE_RASTER_MOSAIC_TILES):
        return reject()
    matrix = (camera.m11(), camera.m12(), camera.m13(), camera.m21(), camera.m22(),
              camera.m23(), camera.m31(), camera.m32(), camera.m33())
    if (not all(math.isfinite(value) for value in matrix)
            or not camera.isAffine() or camera.m11() <= 0 or camera.m22() <= 0
            or camera.m12() != 0 or camera.m21() != 0 or camera.m33() != 1):
        return reject()
    inverse, valid = camera.inverted()
    if not valid or viewport_size.width() <= 0 or viewport_size.height() <= 0:
        return reject()
    bounds_values = tuple(native_bounds.getRect())
    if not all(math.isfinite(v) and v == round(v) for v in bounds_values):
        return reject()
    visible = inverse.mapRect(QRectF(0, 0, viewport_size.width(), viewport_size.height())).intersected(native_bounds)
    # Initial native-frame equivalence is proven for integral capture frames.
    # Fractional frames keep the original path; do not invent a sampling shift.
    if not all(math.isfinite(v) and v == round(v) for v in visible.getRect()):
        return reject()
    region = visible.toAlignedRect().intersected(native_bounds.toAlignedRect())
    width, height = region.width(), region.height()
    if (region.isEmpty() or width > _NATIVE_RASTER_MOSAIC_SIDE
            or height > _NATIVE_RASTER_MOSAIC_SIDE
            or width * height * 4 > _NATIVE_RASTER_MOSAIC_BYTES):
        return reject()
    placements, signatures, areas = [], [], 0
    space, environment, contract = None, None, None
    for tile in tiles:
        image = tile.image
        if (not isinstance(image, QImage) or image.isNull()
                or tile.pixel_contract != LEGACY_PIXELS
                or image.format() != QImage.Format_ARGB32_Premultiplied
                or image.devicePixelRatio() != 1.
                or not isinstance(tile.key, tuple) or len(tile.key) != 2 or tile.key[0] is not None):
            return reject()
        world = QRectF(tile.world_rect)
        source = QRectF(tile.source_rect) if tile.source_rect is not None else QRectF(image.rect())
        values = tuple(world.getRect()) + tuple(source.getRect())
        if (world.isEmpty() or source.isEmpty()
                or not all(math.isfinite(v) and v == round(v) for v in values)
                or source.width() != world.width() or source.height() != world.height()
                or not QRectF(image.rect()).contains(source)):
            return reject()
        clipped = world.toAlignedRect().intersected(region)
        if clipped.isEmpty():
            continue
        if any(clipped.intersects(other[1]) for other in placements):
            return reject()
        current_space = image.colorSpace()
        current_environment = (getattr(tile.pixel_environment, 'signature', None)
                               if tile.pixel_environment is not None else None)
        if tile.pixel_environment is not None and not isinstance(current_environment, tuple):
            return reject()
        if not placements:
            space, environment, contract = current_space, current_environment, tile.pixel_contract
        elif current_space != space or current_environment != environment or tile.pixel_contract != contract:
            return reject()
        sx = round(source.x()) + clipped.x() - round(world.x())
        sy = round(source.y()) + clipped.y() - round(world.y())
        placements.append((image, clipped, sx, sy))
        areas += clipped.width() * clipped.height()
        signatures.append((tile.key, image.cacheKey(), image.width(), image.height(), image.format(),
                           image.devicePixelRatio(), values, current_space, current_environment,
                           tile.pixel_contract.signature))
    if len(placements) < 2 or areas != width * height:
        return reject()
    key = (context, tuple(signatures), tuple(region.getRect()), matrix,
           viewport_size.width(), viewport_size.height(), bool(smooth), presentation_dpr)
    cached = getattr(owner, '_native_raster_mosaic', None)
    if cached is not None and cached[0] == key:
        return cached[1], QRectF(region)
    # Retire before allocating the replacement; the private owner retains only
    # one bounded surface. Source tiles remain owned by the ordinary tile graph.
    _discard_native_raster_mosaic(owner)
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    if image.isNull() or image.sizeInBytes() > _NATIVE_RASTER_MOSAIC_BYTES:
        return None
    image.setDevicePixelRatio(1.)
    image.setColorSpace(space)
    target = np.frombuffer(image.bits(), np.uint8).reshape(height, image.bytesPerLine())
    for native, clipped, sx, sy in placements:
        pixels = np.frombuffer(native.constBits(), np.uint8).reshape(native.height(), native.bytesPerLine())
        x, y = clipped.x()-region.x(), clipped.y()-region.y()
        target[y:y+clipped.height(), x*4:(x+clipped.width())*4] = pixels[sy:sy+clipped.height(), sx*4:(sx+clipped.width())*4]
    del target, pixels
    owner._native_raster_mosaic = key, image
    return image, QRectF(region)


def _draw_native_raster_mosaic(painter, mosaic, camera, contract, environment, *, smooth):
    """Draw one ordinary native QImage, restoring every caller painter state."""
    image, world = mosaic
    painter.save()
    try:
        painter.setTransform(camera)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, bool(smooth))
        with pixel_scope(contract, environment=environment):
            painter.drawImage(world, display_image(image, contract), QRectF(image.rect()))
    finally:
        painter.restore()


def draw_document_tiles(painter: QPainter, tiles: Iterable[PresentedTile],
                        camera: QTransform, viewport_size: QSizeF, *, owner=None,
                        smooth=True, clip_world: QRectF | None = None,
                        native_context=None, native_bounds: QRectF | None = None) -> PresentationStats:
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
    if any(isinstance(tile.image,DeviceImage) for tile in tiles):
        if owner is not None and getattr(owner, '_native_raster_mosaic', None) is not None:
            _discard_native_raster_mosaic(owner)
        # A GUI fallback may not synchronously materialize a device image. Its
        # consumer must request the CPU edge through the background scheduler.
        controller = getattr(owner,'_scene_controller',None)
        if controller is not None:
            controller.ensure_cpu_tiles(tiles)
        return PresentationStats('pending',0)
    # Only the ordinary owned Raster widget opts in. Arbitrary QImage/export
    # painters, GPU widgets and caller-supplied clips retain their old path.
    # Detached/nonopted callers do not even import the Canvas UI module.
    if native_context is not None and owner is not None:
        from comic_editor.ui.canvas import RasterCanvasWidget
        eligible = (isinstance(owner, RasterCanvasWidget) and painter.device() is owner
                    and not painter.hasClipping()
                    and painter.compositionMode() == QPainter.CompositionMode_SourceOver
                    and clip_world is None)
        if eligible:
            mosaic = _native_raster_mosaic(owner, tiles, camera, viewport_size,
                native_bounds, native_context, smooth=smooth,
                presentation_dpr=owner.devicePixelRatioF())
            if mosaic is not None:
                try:
                    _draw_native_raster_mosaic(painter, mosaic, camera, tiles[0].pixel_contract,
                                               tiles[0].pixel_environment, smooth=smooth)
                except BaseException:
                    _discard_native_raster_mosaic(owner)
                    raise
                return PresentationStats('raster', len(tiles))
        elif getattr(owner, '_native_raster_mosaic', None) is not None:
            _discard_native_raster_mosaic(owner)
    elif owner is not None and getattr(owner, '_native_raster_mosaic', None) is not None:
        _discard_native_raster_mosaic(owner)
    painter.save()
    painter.setTransform(camera)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, bool(smooth))
    for tile in tiles:
        rectangles = _tile_rectangles(tile, clip_world)
        if rectangles is not None:
            world, source = rectangles
            with pixel_scope(tile.pixel_contract,environment=tile.pixel_environment):
                painter.drawImage(world, display_image(tile.image,tile.pixel_contract), source)
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
