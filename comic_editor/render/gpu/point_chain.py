"""Floating-point point-operation chains in a private OpenGL context.

Resources use top-first logical rows consistently, including framebuffer rows.
Only the final readback crosses to CPU memory. No Qt image conversion is used
for upload/readback, so HDR, sub-byte color, and alpha retain float precision.
"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QThread
from PySide6.QtGui import QGuiApplication, QOpenGLContext, QOffscreenSurface, QSurfaceFormat, QVector3D
from PySide6.QtOpenGL import (
    QOpenGLFunctions_4_3_Core, QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
    QOpenGLTexture, QOpenGLShader, QOpenGLShaderProgram, QOpenGLVertexArrayObject,
)


VERTEX = '''#version 430 core
void main() {
    vec2 v = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    gl_Position = vec4(v * 2. - 1., 0., 1.);
}
'''

BRIGHTNESS = '''#version 430 core
uniform sampler2D sourceTexture;
uniform int stageCount;
uniform vec3 parameters[16];
out vec4 outputColor;
void main() {
    precise vec4 c = texelFetch(sourceTexture, ivec2(gl_FragCoord.xy), 0);
    for (int i = 0; i < stageCount; ++i) {
        if (parameters[i].z <= 0.) continue;
        precise vec3 rgb = c.a > 1e-6 ? c.rgb / c.a : vec3(0.);
        rgb -= vec3(.5);
        rgb *= parameters[i].x;
        rgb += vec3(.5 + parameters[i].y);
        rgb = clamp(rgb, 0., 1.);
        rgb *= c.a;
        precise vec3 difference = rgb - c.rgb;
        difference *= parameters[i].z;
        c.rgb += difference;
    }
    outputColor = c;
}
'''

POINT_LUT = '''#version 430 core
uniform sampler2D sourceTexture;
uniform sampler2D curveTable;
out vec4 outputColor;
void main() {
    vec4 source = texelFetch(sourceTexture, ivec2(gl_FragCoord.xy), 0);
    ivec4 value = ivec4(round(source * 255.));
    outputColor = vec4(
        texelFetch(curveTable, ivec2(value.r, value.a), 0).r,
        texelFetch(curveTable, ivec2(value.g, value.a), 0).g,
        texelFetch(curveTable, ivec2(value.b, value.a), 0).b,
        texelFetch(curveTable, ivec2(0, value.a), 0).a);
}
'''


@dataclass
class _Resource:
    owner: object
    texture: int
    width: int
    height: int
    bytes: int


class GpuPointChain:
    """A bounded source/result cache; all GL work stays on its owning thread."""
    def __init__(self, *, budget=128 * 1024 * 1024, surface=None):
        self.budget = max(0, int(budget))
        self.bytes = 0
        self.resources = OrderedDict()
        self.context = self.surface = self.functions = self.vao = self.program = None
        self.lut_program = None
        self.blur = None
        self.available = False
        self.reason = ''
        self.uploads = self.readbacks = self.draws = self.hits = self.compiles = 0
        self.owner_thread = QThread.currentThread()
        app = QGuiApplication.instance()
        if app is None or (surface is None and app.thread() != self.owner_thread):
            self.reason = 'GPU initialization requires the application thread'
            return
        fmt = QSurfaceFormat()
        fmt.setVersion(4, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.context = QOpenGLContext()
        self.context.setFormat(surface.format() if surface is not None else fmt)
        if not self.context.create():
            self.reason = 'OpenGL 4.3 context unavailable'
            return
        actual = self.context.format()
        if (actual.majorVersion(), actual.minorVersion()) < (4, 3):
            self.reason = 'OpenGL 4.3 is required for precise point operations'
            return
        self.surface = surface
        if self.surface is None:
            self.surface = QOffscreenSurface()
            self.surface.setFormat(actual)
            self.surface.create()
        try:
            with self._current():
                self.functions = QOpenGLFunctions_4_3_Core()
                if not self.functions.initializeOpenGLFunctions():
                    raise RuntimeError('Could not initialize OpenGL functions')
                self.max_size = int(self.functions.glGetIntegerv(0x0D33))
                self.vao = QOpenGLVertexArrayObject()
                if not self.vao.create():
                    raise RuntimeError('Could not create point-operation vertex array')
                self.program = QOpenGLShaderProgram()
                if not (self.program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX)
                        and self.program.addShaderFromSourceCode(QOpenGLShader.Fragment, BRIGHTNESS)
                        and self.program.link()):
                    raise RuntimeError(self.program.log())
                self.compiles = 1
                self.lut_program = QOpenGLShaderProgram()
                if not (self.lut_program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX)
                        and self.lut_program.addShaderFromSourceCode(QOpenGLShader.Fragment, POINT_LUT)
                        and self.lut_program.link()):
                    raise RuntimeError(self.lut_program.log())
                self.compiles += 1
                self.available = True
        except RuntimeError as error:
            self.reason = str(error)

    @contextmanager
    def _current(self):
        if QThread.currentThread() != self.owner_thread:
            raise RuntimeError('GPU resources must stay on their owning thread')
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous is not None else None
        if not self.context.makeCurrent(self.surface):
            raise RuntimeError('Could not activate point-operation context')
        try:
            yield
        finally:
            self.context.doneCurrent()
            if previous is not None and previous_surface is not None:
                previous.makeCurrent(previous_surface)

    def _reserve(self, size, protected=()):
        if size > self.budget:
            return False
        while self.bytes + size > self.budget:
            victim = next((key for key in self.resources if key not in protected), None)
            if victim is None:
                return False
            resource = self.resources.pop(victim)
            self.bytes -= resource.bytes
            if hasattr(resource.owner, 'destroy'):
                resource.owner.destroy()
            # A framebuffer's texture belongs to its Qt framebuffer owner.
            del resource
        return True

    def _get(self, key):
        value = self.resources.get(key)
        if value is not None:
            self.resources.move_to_end(key)
        return value

    def apply(self, pixels, parameters, *, source_key):
        """Execute up to sixteen scalar brightness/contrast stages in one pass.

        ``source_key`` must identify immutable input pixels, not just their size.
        Values are (gain, offset, intensity), already rounded to float32 by the
        CPU reference. Unsupported or over-budget requests return None.
        """
        return self._apply(pixels, parameters, source_key=source_key)

    def apply_lut(self, pixels, palette, *, source_key, palette_key):
        """Preserve the CPU reference bits for channel-independent byte input.

        The caller compiles final float values for every channel-byte/alpha-byte
        pair. Sampling this table fuses operations without introducing per-stage
        byte rounding or relying on driver floating-point division accuracy.
        """
        if palette.shape != (256, 256, 4) or palette.dtype != np.float32:
            raise ValueError('Point tables require 256 × 256 × RGBA float32 values')
        return self._apply(pixels, (), source_key=source_key, lut=(palette_key, palette))

    def apply_blur(self, pixels, strength, *, source_key, algorithm='normal'):
        if not self.available:
            return None
        if self.blur is None:
            from .blur import GpuBlur
            self.blur = GpuBlur(self)
        return self.blur.apply(pixels, strength, source_key=source_key, algorithm=algorithm)

    def _upload(self, key, data):
        height, width = data.shape[:2]
        texture = QOpenGLTexture(QOpenGLTexture.Target2D)
        texture.setFormat(QOpenGLTexture.RGBA32F)
        texture.setSize(width, height)
        texture.setMipLevels(1)
        texture.allocateStorage(QOpenGLTexture.RGBA, QOpenGLTexture.Float32)
        texture.setMinMagFilters(QOpenGLTexture.Nearest, QOpenGLTexture.Nearest)
        texture.setData(QOpenGLTexture.RGBA, QOpenGLTexture.Float32, data)
        value = _Resource(texture, texture.textureId(), width, height, int(data.nbytes))
        self.resources[key] = value
        self.bytes += value.bytes
        self.uploads += 1
        return value

    def _apply(self, pixels, parameters, *, source_key, lut=None):
        if not self.available or (lut is None and not 1 <= len(parameters) <= 16):
            return None
        pixels = np.asarray(pixels)
        if pixels.ndim != 3 or pixels.shape[2] != 4:
            raise ValueError('GPU point operations require an H × W × RGBA array')
        height, width = pixels.shape[:2]
        size = width * height * 16
        table_size = int(lut[1].nbytes) if lut is not None else 0
        if min(width, height) <= 0 or max(width, height) > self.max_size or size * 2 + table_size > self.budget:
            return None
        parameters = tuple(tuple(float(np.float32(value)) for value in stage) for stage in parameters)
        input_key = ('source', source_key, width, height, 'rgba32f')
        output_key = (('point-lut', input_key, lut[0]) if lut is not None
                      else ('brightness', input_key, parameters))
        with self._current():
            result = self._get(output_key)
            if result is None:
                source = self._get(input_key)
                if source is None:
                    if not self._reserve(size):
                        return None
                    source = self._upload(input_key, np.ascontiguousarray(pixels, np.float32))
                protected = [input_key]
                if lut is not None:
                    table_key = ('table', lut[0])
                    table = self._get(table_key)
                    if table is None:
                        if not self._reserve(table_size, protected):
                            return None
                        table = self._upload(table_key, lut[1])
                    protected.append(table_key)
                if not self._reserve(size, protected):
                    return None
                fmt = QOpenGLFramebufferObjectFormat()
                fmt.setInternalTextureFormat(0x8814)  # GL_RGBA32F
                framebuffer = QOpenGLFramebufferObject(width, height, fmt)
                if not framebuffer.isValid():
                    return None
                framebuffer.bind()
                program = self.lut_program if lut is not None else self.program
                program.bind()
                self.functions.glUniform1i(program.uniformLocation('sourceTexture'), 0)
                if lut is not None:
                    self.functions.glUniform1i(program.uniformLocation('curveTable'), 1)
                    self.functions.glActiveTexture(0x84C1)
                    self.functions.glBindTexture(0x0DE1, table.texture)
                else:
                    self.functions.glUniform1i(program.uniformLocation('stageCount'), len(parameters))
                    for index, values in enumerate(parameters):
                        program.setUniformValue(f'parameters[{index}]', QVector3D(*values))
                self.functions.glActiveTexture(0x84C0)
                self.functions.glBindTexture(0x0DE1, source.texture)
                self.vao.bind()
                self.functions.glDisable(0x0BE2)  # GL_BLEND; the kernel owns premultiplied arithmetic
                self.functions.glViewport(0, 0, width, height)
                self.functions.glDrawArrays(4, 0, 3)
                self.vao.release()
                program.release()
                result = _Resource(framebuffer, framebuffer.texture(), width, height, size)
                self.resources[output_key] = result
                self.bytes += size
                self.draws += 1
            else:
                self.hits += 1
            result.owner.bind()
            output = np.empty((height, width, 4), np.float32)
            # Qt's toImage() converts floating-point FBOs to byte images. Read
            # explicitly, preserving the top-first logical row convention.
            self.functions.glReadPixels(0, 0, width, height, 0x1908, 0x1406, output)
            result.owner.release()
            self.readbacks += 1
            return output

    def close(self):
        if self.context is None or self.surface is None:
            return
        with self._current():
            if self.blur is not None:
                self.blur.close()
                self.blur = None
            while self.resources:
                _key, resource = self.resources.popitem()
                if hasattr(resource.owner, 'destroy'):
                    resource.owner.destroy()
                del resource
            self.bytes = 0
            if self.vao is not None:
                self.vao.destroy()
                self.vao = None
            self.program = None
            self.lut_program = None
            self.functions = None
        self.available = False
