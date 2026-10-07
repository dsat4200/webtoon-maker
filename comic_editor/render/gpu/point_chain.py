"""Floating-point point-operation chains in a private OpenGL context.

Resources use top-first logical rows consistently, including framebuffer rows.
Only the final readback crosses to CPU memory. No Qt image conversion is used
for upload/readback, so HDR, sub-byte color, and alpha retain float precision.
"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
import ctypes

import numpy as np
from PySide6.QtCore import QThread
from PySide6.QtGui import QGuiApplication, QOpenGLContext, QOffscreenSurface, QSurfaceFormat, QVector3D
from PySide6.QtOpenGL import (
    QOpenGLFunctions_4_3_Core, QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
    QOpenGLTexture, QOpenGLShader, QOpenGLShaderProgram, QOpenGLVertexArrayObject,
)
from .residency import GRAPHICS_RESIDENCY


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

QUANTIZE = '''#version 430 core
uniform sampler2D sourceTexture;
uniform sampler2D normalizationTable;
uniform int integerOutput;
out vec4 outputColor;
void main() {
    precise vec4 source = texelFetch(sourceTexture, ivec2(gl_FragCoord.xy), 0);
    precise vec4 scaled = source * 255.;
    ivec4 v = ivec4(clamp(scaled, 0., 255.));
    outputColor = vec4(texelFetch(normalizationTable,ivec2(v.r,0),0).r,
                       texelFetch(normalizationTable,ivec2(v.g,0),0).r,
                       texelFetch(normalizationTable,ivec2(v.b,0),0).r,
                       texelFetch(normalizationTable,ivec2(v.a,0),0).r);
}
'''

NATIVE_COPY = '''#version 430 core
uniform sampler2D sourceTexture;
uniform int offsetX;
uniform int offsetY;
out vec4 outputColor;
void main() {
    ivec2 source = ivec2(gl_FragCoord.xy) - ivec2(offsetX,offsetY);
    ivec2 extent = textureSize(sourceTexture,0);
    outputColor = (all(greaterThanEqual(source,ivec2(0))) && all(lessThan(source,extent)))
        ? texelFetch(sourceTexture,source,0) : vec4(0.);
}
'''


@dataclass
class _Resource:
    owner: object
    texture: int
    width: int
    height: int
    bytes: int
    kind: str = 'float'


class GpuPointChain:
    """A bounded source/result cache; all GL work stays on its owning thread."""
    def __init__(self, *, budget=128 * 1024 * 1024, surface=None, share_context=None,
                 context=None):
        self.budget = max(0, int(budget))
        self.bytes = 0
        self.resources = OrderedDict()
        self.context = self.surface = self.functions = self.vao = self.program = None
        self.lut_program = None
        self.quantize_program = None
        self.blur = None
        self.available = False
        self.reason = ''
        self.uploads = self.readbacks = self.draws = self.hits = self.compiles = 0
        self.owner_thread = QThread.currentThread()
        self.leases = {}
        self.residency_token = GRAPHICS_RESIDENCY.token()
        app = QGuiApplication.instance()
        if app is None or (surface is None and app.thread() != self.owner_thread):
            self.reason = 'GPU initialization requires the application thread'
            return
        fmt = QSurfaceFormat()
        fmt.setVersion(4, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.context = context or QOpenGLContext()
        if context is None:
            if share_context is not None:
                self.context.setShareContext(share_context)
            self.context.setFormat(surface.format() if surface is not None else fmt)
        if context is None and not self.context.create():
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
            self.available = False
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
        while (self.bytes + size > self.budget or
               not GRAPHICS_RESIDENCY.change(self.residency_token,self.bytes+size)):
            leased = set(self.leases.values())
            victim = next((key for key in self.resources if key not in protected and key not in leased), None)
            if victim is None:
                return False
            resource = self.resources.pop(victim)
            self.bytes -= resource.bytes
            GRAPHICS_RESIDENCY.change(self.residency_token,self.bytes)
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

    def _apply(self, pixels, parameters, *, source_key, lut=None, resident=False):
        if not self.available or (lut is None and not 1 <= len(parameters) <= 16):
            return None
        source_resource = pixels if isinstance(pixels, _Resource) else None
        if source_resource is not None:
            height, width = source_resource.height, source_resource.width
        else:
            pixels = np.asarray(pixels)
            if pixels.ndim != 3 or pixels.shape[2] != 4:
                raise ValueError('GPU point operations require an H × W × RGBA array')
            height, width = pixels.shape[:2]
        size = width * height * 16
        table_size = int(lut[1].nbytes) if lut is not None else 0
        if min(width, height) <= 0 or max(width, height) > self.max_size or size * 2 + table_size > self.budget:
            return None
        parameters = tuple(tuple(float(np.float32(value)) for value in stage) for stage in parameters)
        input_key = (source_key if source_resource is not None else
                     ('source', source_key, width, height, 'rgba32f'))
        output_key = (('point-lut', input_key, lut[0]) if lut is not None
                      else ('brightness', input_key, parameters))
        with self._current():
            result = self._get(output_key)
            if result is None:
                source = source_resource or self._get(input_key)
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
            return (result, output_key) if resident else self.read_resource(result)

    def read_resource(self, result):
        """Explicit CPU edge; called only on the graphics owner."""
        with self._current():
            temporary = None
            if isinstance(result.owner,QOpenGLTexture):
                temporary = QOpenGLFramebufferObject(1,1)
                temporary.bind()
                self.functions.glFramebufferTexture2D(0x8D40,0x8CE0,0x0DE1,result.texture,0)
            else:
                result.owner.bind()
            output = np.empty((result.height, result.width, 4), np.float32)
            # Pixel pack state belongs to the context, including state changed
            # by Qt helpers. Never let a row stride, skip, or PBO reinterpret
            # the CPU allocation. Use an explicit pointer instead of a generated
            # Python buffer overload for this writable native boundary.
            gl = self.functions
            pack_names = (0x0D05, 0x0D02, 0x0D03, 0x0D04)
            pack = {name: gl.glGetIntegerv(name) for name in pack_names}
            pack_buffer = gl.glGetIntegerv(0x88ED)
            try:
                gl.glBindBuffer(0x88EB, 0)  # GL_PIXEL_PACK_BUFFER
                for name,value in zip(pack_names,(4,0,0,0)):
                    gl.glPixelStorei(name,value)
                call = getattr(ctypes,'WINFUNCTYPE',ctypes.CFUNCTYPE)
                read = call(None,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,
                    ctypes.c_uint,ctypes.c_uint,ctypes.c_void_p)(
                    self.context.getProcAddress(b'glReadPixels'))
                read(0,0,result.width,result.height,0x1908,0x1406,output.ctypes.data)
            finally:
                for name,value in pack.items():
                    gl.glPixelStorei(name,value)
                gl.glBindBuffer(0x88EB,pack_buffer)
            if temporary is not None:
                temporary.release()
                del temporary
            else:
                result.owner.release()
            self.readbacks += 1
            return output

    def quantize(self, source, key):
        """Keep the reference's final byte boundary without a CPU round trip."""
        from .blur import GpuBlur
        if self.blur is None:
            self.blur = GpuBlur(self)
        output_key = ('byte-normalized', key)
        with self._current():
            result = self._get(output_key)
            if result is not None:
                self.hits += 1
                return result, output_key
            if self.quantize_program is None:
                self.quantize_program = QOpenGLShaderProgram()
                if not (self.quantize_program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX)
                        and self.quantize_program.addShaderFromSourceCode(QOpenGLShader.Fragment, QUANTIZE)
                        and self.quantize_program.link()):
                    raise RuntimeError(self.quantize_program.log())
                self.compiles += 1
            table_key = ('blur-normalization-table',)
            table = self.blur._upload(table_key, (np.arange(256,dtype=np.float32)/255.).reshape(1,256),
                QOpenGLTexture.R32F, QOpenGLTexture.Red, QOpenGLTexture.Float32, [key])
            result = self.blur._draw(output_key, (source.width,source.height), self.quantize_program,
                [('sourceTexture',source),('normalizationTable',table)], {}, [key,table_key], floating=True)
            return result, output_key

    def segment(self, pixels, stages, *, source_key, canonical_input=False):
        """Evaluate a compatible segment; no intermediate or final readback."""
        from comic_editor.render.device import PointTableStage, ScalarBlurStage, ByteQuantizeStage, NativeCopyStage
        from .blur import GpuBlur, _BudgetExceeded
        with self._current():
            if isinstance(pixels, _Resource):
                result, key = pixels, source_key
            else:
                pixels = np.asarray(pixels)
                if pixels.dtype == np.uint8:
                    pixels = pixels.astype(np.float32)/255.
                    canonical_input = True
                key = ('source',source_key,pixels.shape[1],pixels.shape[0],'rgba32f')
                result = self._get(key)
                if result is None:
                    if not self._reserve(pixels.nbytes):
                        return None
                    result = self._upload(key,pixels)
            canonical = bool(canonical_input)
            try:
                for stage in stages:
                    if isinstance(stage, PointTableStage):
                        if not canonical:
                            return None
                        value = self._apply(result, (), source_key=key,
                                            lut=(stage.identity,stage.table), resident=True)
                        canonical = stage.quantized_output
                    elif isinstance(stage, ScalarBlurStage):
                        if float(stage.strength) <= 1e-6:
                            continue
                        if self.blur is None:
                            self.blur = GpuBlur(self)
                        value = self.blur.apply(result, stage.strength,source_key=key,
                                                algorithm=stage.algorithm,resident=True)
                        canonical = True
                    elif isinstance(stage, ByteQuantizeStage):
                        value = self.quantize(result,key)
                        canonical = True
                    elif isinstance(stage, NativeCopyStage):
                        value = self.native_copy(result,key,stage.size,stage.origin)
                    else:
                        raise ValueError('Unsupported device segment stage')
                    if value is None:
                        return None
                    result, key = value
                return result,key,canonical
            except _BudgetExceeded:
                return None

    def native_copy(self, source, key, size, origin):
        """Preserve integer grids when a spatial stage expands or crops a frame."""
        from .blur import GpuBlur
        if min(size) <= 0 or max(size) > self.max_size:
            return None
        if size == (source.width,source.height) and origin == (0,0):
            return source,key
        if self.blur is None:
            self.blur = GpuBlur(self)
        with self._current():
            program = getattr(self,'native_copy_program',None)
            if program is None:
                program = QOpenGLShaderProgram()
                if not (program.addShaderFromSourceCode(QOpenGLShader.Vertex,VERTEX)
                        and program.addShaderFromSourceCode(QOpenGLShader.Fragment,NATIVE_COPY)
                        and program.link()):
                    raise RuntimeError(program.log())
                self.native_copy_program = program
                self.compiles += 1
            output_key = ('native-copy',key,tuple(size),tuple(origin))
            result = self.blur._draw(output_key,size,program,[('sourceTexture',source)],
                dict(offsetX=origin[0],offsetY=origin[1]),[key],floating=True)
            return result,output_key

    def close(self):
        if self.context is None or self.surface is None:
            return
        if QThread.currentThread() != self.owner_thread:
            raise RuntimeError('GPU cleanup must stay on its owning thread')
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous is not None else None
        activated = False
        try:
            # A failed kernel/context guard must not suppress a best-effort
            # native cleanup when Qt can still activate this owner's context.
            activated = self.context.makeCurrent(self.surface)
            if activated:
                self.functions.glFinish()
                if self.blur is not None:
                    self.blur.close()
                    self.blur = None
                while self.resources:
                    _key, resource = self.resources.popitem()
                    if hasattr(resource.owner, 'destroy'):
                        resource.owner.destroy()
                    del resource
                if self.vao is not None:
                    self.vao.destroy()
        finally:
            # A lost context can prevent explicit GL deletion. Retire Qt's
            # resource guards and break kernel references on this same owner,
            # never leave a cycle for a later GUI-thread garbage collection.
            if self.blur is not None:
                self.blur.close()
                self.blur = None
            self.resources.clear()
            self.bytes = 0
            GRAPHICS_RESIDENCY.change(self.residency_token,0)
            self.vao = None
            self.program = None
            self.lut_program = None
            self.quantize_program = None
            self.native_copy_program = None
            self.functions = None
            self.available = False
            if activated:
                self.context.doneCurrent()
                if previous is not self.context and previous is not None and previous_surface is not None:
                    previous.makeCurrent(previous_surface)
