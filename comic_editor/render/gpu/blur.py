"""GPU-resident legacy blur levels with exact Pillow byte-stage rounding.

Bilinear taps use the CPU reference's 22-bit coefficients. Integer textures
retain both separable pass roundings. Small byte-pair tables preserve Pillow's
premultiplication and scalar blend, while a float table preserves NumPy's final
normalization bits. The graphics owner's LRU accounts for every texture/FBO.
"""
from functools import lru_cache

import numpy as np
from PIL import Image
from PIL import __version__ as PILLOW_VERSION
from PySide6.QtOpenGL import (
    QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat, QOpenGLTexture,
    QOpenGLShader, QOpenGLShaderProgram,
)

from comic_editor.render.blur_regions import RADII, _coefficients
from .point_chain import VERTEX, _Resource


RESIZE = '''#version 430 core
uniform usampler2D sourceTexture;
uniform isampler2D coefficients;
uniform usampler2D premultiplyTable;
uniform usampler2D unpremultiplyTable;
uniform int axis;
uniform int tapCount;
uniform int premultiply;
uniform int unpremultiply;
out uvec4 outputColor;
uvec4 converted(uvec4 p, usampler2D table) {
    return uvec4(texelFetch(table, ivec2(p.r,p.a),0).r,
                 texelFetch(table, ivec2(p.g,p.a),0).r,
                 texelFetch(table, ivec2(p.b,p.a),0).r,p.a);
}
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    ivec4 sum = ivec4(1 << 21);
    for (int i = 0; i < tapCount; ++i) {
        ivec2 tap = texelFetch(coefficients, ivec2(i, axis==0?p.x:p.y), 0).rg;
        uvec4 value = texelFetch(sourceTexture, axis==0?ivec2(tap.x,p.y):ivec2(p.x,tap.x),0);
        if (premultiply != 0) value = converted(value, premultiplyTable);
        sum += ivec4(value) * tap.y;
    }
    uvec4 value = uvec4(clamp(sum >> 22, ivec4(0), ivec4(255)));
    outputColor = unpremultiply != 0 ? converted(value, unpremultiplyTable) : value;
}
'''

NORMALIZE = '''#version 430 core
uniform usampler2D sourceTexture;
uniform usampler2D highTexture;
uniform usampler2D blendTable;
uniform sampler2D normalizationTable;
uniform int blend;
out vec4 outputColor;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    uvec4 low = texelFetch(sourceTexture,p,0);
    if (blend != 0) {
        uvec4 high = texelFetch(highTexture,p,0);
        low = uvec4(texelFetch(blendTable,ivec2(low.r,high.r),0).r,
                    texelFetch(blendTable,ivec2(low.g,high.g),0).r,
                    texelFetch(blendTable,ivec2(low.b,high.b),0).r,
                    texelFetch(blendTable,ivec2(low.a,high.a),0).r);
    }
    outputColor = vec4(texelFetch(normalizationTable,ivec2(low.r,0),0).r,
                       texelFetch(normalizationTable,ivec2(low.g,0),0).r,
                       texelFetch(normalizationTable,ivec2(low.b,0),0).r,
                       texelFetch(normalizationTable,ivec2(low.a,0),0).r);
}
'''


@lru_cache(maxsize=2)
def conversion_table(premultiply):
    values = np.empty((256, 256, 4), np.uint8)
    values[..., :3] = np.arange(256, dtype=np.uint8)[None, :, None]
    values[..., 3] = np.arange(256, dtype=np.uint8)[:, None]
    source, target = ('RGBA', 'RGBa') if premultiply else ('RGBa', 'RGBA')
    result = np.ascontiguousarray(np.asarray(Image.fromarray(values, source).convert(target))[..., 0])
    result.setflags(write=False)
    return result


@lru_cache(maxsize=16)
def blend_table(amount):
    low = np.broadcast_to(np.arange(256, dtype=np.uint8), (256, 256)).copy()
    high = low.T.copy()
    result = np.ascontiguousarray(np.asarray(Image.blend(Image.fromarray(low, 'L'),
                                                        Image.fromarray(high, 'L'), amount)))
    result.setflags(write=False)
    return result


class _BudgetExceeded(Exception):
    pass


class _IntegerFramebuffer:
    """Attach a typed texture through GL; Qt's FBO allocation assumes RGBA.

    A tiny Qt owner supplies the framebuffer name and context-safe lifetime.
    Its original 1 × 1 color texture is accounted for alongside our attachment.
    Qt's byte-image conversion is never used for either texture.
    """
    def __init__(self, renderer, width, height):
        self.framebuffer = QOpenGLFramebufferObject(1, 1)
        self.image = QOpenGLTexture(QOpenGLTexture.Target2D)
        self.image.setFormat(QOpenGLTexture.RGBA8U)
        self.image.setSize(width, height)
        self.image.setMipLevels(1)
        self.image.allocateStorage(QOpenGLTexture.RGBA_Integer, QOpenGLTexture.UInt8)
        self.image.setMinMagFilters(QOpenGLTexture.Nearest, QOpenGLTexture.Nearest)
        self.framebuffer.bind()
        renderer.functions.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, self.image.textureId(), 0)
        self.valid = renderer.functions.glCheckFramebufferStatus(0x8D40) == 0x8CD5
        self.framebuffer.release()

    def isValid(self):
        return self.valid

    def texture(self):
        return self.image.textureId()

    def bind(self):
        return self.framebuffer.bind()

    def release(self):
        return self.framebuffer.release()

    def destroy(self):
        self.image.destroy()
        self.framebuffer = None


class GpuBlur:
    """Kernels owned by an existing private context, sharing its resource budget."""
    def __init__(self, renderer):
        self.renderer = renderer
        self.resize_program = self.normalize_program = None

    def close(self):
        self.resize_program = self.normalize_program = None

    def _programs(self):
        if self.resize_program is not None:
            return
        programs = []
        for fragment in (RESIZE, NORMALIZE):
            program = QOpenGLShaderProgram()
            if not (program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX)
                    and program.addShaderFromSourceCode(QOpenGLShader.Fragment, fragment)
                    and program.link()):
                raise RuntimeError(program.log())
            programs.append(program)
        self.resize_program, self.normalize_program = programs
        self.renderer.compiles += len(programs)

    def _upload(self, key, values, internal, pixel_format, pixel_type, protected):
        r = self.renderer
        result = r._get(key)
        if result is not None:
            return result
        if not r._reserve(values.nbytes, protected):
            raise _BudgetExceeded
        height, width = values.shape[:2]
        texture = QOpenGLTexture(QOpenGLTexture.Target2D)
        texture.setFormat(internal)
        texture.setSize(width, height)
        texture.setMipLevels(1)
        texture.allocateStorage(pixel_format, pixel_type)
        texture.setMinMagFilters(QOpenGLTexture.Nearest, QOpenGLTexture.Nearest)
        texture.setData(pixel_format, pixel_type, values)
        result = _Resource(texture, texture.textureId(), width, height, int(values.nbytes))
        r.resources[key] = result
        r.bytes += result.bytes
        r.uploads += 1
        return result

    def _draw(self, key, size, program, samplers, uniforms, protected, *, floating=False):
        r = self.renderer
        result = r._get(key)
        if result is not None:
            return result
        width, height = size
        bytes_needed = width * height * (16 if floating else 4) + (0 if floating else 4)
        if not r._reserve(bytes_needed, protected):
            raise _BudgetExceeded
        if floating:
            fmt = QOpenGLFramebufferObjectFormat()
            fmt.setInternalTextureFormat(0x8814)  # GL_RGBA32F
            framebuffer = QOpenGLFramebufferObject(width, height, fmt)
        else:
            framebuffer = _IntegerFramebuffer(r, width, height)
        if not framebuffer.isValid():
            raise RuntimeError('Could not allocate a blur framebuffer')
        framebuffer.bind()
        program.bind()
        for unit, (name, resource) in enumerate(samplers):
            r.functions.glUniform1i(program.uniformLocation(name), unit)
            r.functions.glActiveTexture(0x84C0 + unit)
            r.functions.glBindTexture(0x0DE1, resource.texture)
        for name, value in uniforms.items():
            r.functions.glUniform1i(program.uniformLocation(name), int(value))
        r.vao.bind()
        r.functions.glDisable(0x0BE2)
        r.functions.glViewport(0, 0, width, height)
        r.functions.glDrawArrays(4, 0, 3)
        r.vao.release()
        program.release()
        framebuffer.release()
        result = _Resource(framebuffer, framebuffer.texture(), width, height, bytes_needed)
        r.resources[key] = result
        r.bytes += bytes_needed
        r.draws += 1
        return result

    def _resize(self, source_key, size, algorithm, protected):
        r = self.renderer
        source = r._get(source_key)
        if (source.width, source.height) == size:
            return source_key
        output_key = ('blur-resize', source_key, size, algorithm)
        if r._get(output_key) is not None:
            return output_key
        protected = [*protected, source_key]
        tables = []
        if algorithm == 'legacy':
            for premultiply in (True, False):
                key = ('blur-alpha-table', premultiply)
                tables.append(self._upload(key, conversion_table(premultiply), QOpenGLTexture.R8U,
                    QOpenGLTexture.Red_Integer, QOpenGLTexture.UInt8, protected))
                protected.append(key)
        else:
            tables = [source, source]  # Valid unsigned samplers; unused by this mode.
        axes = [axis for axis, dimensions in enumerate(((source.width, size[0]), (source.height, size[1])))
                if dimensions[0] != dimensions[1]]
        for pass_index, axis in enumerate(axes):
            dimension = source.width if axis == 0 else source.height
            destination = size[axis]
            coefficient_key = ('blur-coefficients', dimension, destination)
            coefficients = r._get(coefficient_key)
            if coefficients is None:
                indexes, weights = _coefficients(dimension, destination, 0, destination)
                values = np.ascontiguousarray(np.stack((indexes, weights), axis=-1), np.int32)
                coefficients = self._upload(coefficient_key, values, QOpenGLTexture.RG32I,
                    QOpenGLTexture.RG_Integer, QOpenGLTexture.Int32, protected)
            pass_key = output_key if pass_index == len(axes)-1 else (output_key, 'horizontal')
            pass_size = (destination, source.height) if axis == 0 else (source.width, destination)
            source = self._draw(pass_key, pass_size, self.resize_program,
                [('sourceTexture', source), ('coefficients', coefficients),
                 ('premultiplyTable', tables[0]), ('unpremultiplyTable', tables[1])],
                dict(axis=axis, tapCount=coefficients.width,
                     premultiply=algorithm == 'legacy' and pass_index == 0,
                     unpremultiply=algorithm == 'legacy' and pass_index == len(axes)-1),
                [*protected, coefficient_key])
            protected.append(pass_key)
        return output_key

    def apply(self, pixels, strength, *, source_key, algorithm='normal'):
        # The fixed-point/pass-order contract is verified against these releases.
        # A future or different Pillow resampler keeps the CPU reference path.
        if PILLOW_VERSION not in ('12.2.0', '12.3.0'):
            return None
        r = self.renderer
        pixels = np.asarray(pixels)
        if pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 4:
            raise ValueError('Legacy GPU blur requires H × W × RGBA byte pixels')
        if algorithm not in ('normal', 'legacy') or np.ndim(strength) != 0:
            return None
        height, width = pixels.shape[:2]
        radius = float(np.clip(np.float32(strength), 0., 100.))
        if (not r.available or radius <= 1e-6 or min(width, height) <= 0
                or max(width, height) > r.max_size or width * height * 36 + 256*256*3 > r.budget):
            return None
        index = max(0, min(int(np.searchsorted(RADII, radius, side='right'))-1, len(RADII)-2))
        amount = (radius - float(RADII[index])) / (float(RADII[index+1]) - float(RADII[index]))
        incoming_key = ('blur-source', source_key, width, height)
        output_key = ('blur-float', incoming_key, algorithm, radius)
        with r._current():
            result = r._get(output_key)
            if result is not None:
                r.hits += 1
            else:
                try:
                    self._programs()
                    self._upload(incoming_key, np.ascontiguousarray(pixels), QOpenGLTexture.RGBA8U,
                        QOpenGLTexture.RGBA_Integer, QOpenGLTexture.UInt8, [])
                    protected = [incoming_key]
                    levels, size = [incoming_key], (width, height)
                    for _ in range(index + (2 if amount > 1e-6 else 1) - 1):
                        size = (max(1, (size[0]+1)//2), max(1, (size[1]+1)//2))
                        key = self._resize(levels[-1], size, algorithm, protected)
                        levels.append(key)
                        protected.append(key)
                    low_key = self._resize(levels[index], (width, height), algorithm, protected)
                    protected.append(low_key)
                    high_key = (self._resize(levels[index+1], (width, height), algorithm, protected)
                                if amount > 1e-6 else low_key)
                    protected.append(high_key)
                    blend_key = ('blur-blend-table', amount if amount > 1e-6 else 0.)
                    table = self._upload(blend_key, blend_table(amount if amount > 1e-6 else 0.),
                        QOpenGLTexture.R8U, QOpenGLTexture.Red_Integer, QOpenGLTexture.UInt8, protected)
                    protected.append(blend_key)
                    normal_key = ('blur-normalization-table',)
                    normal = self._upload(normal_key, (np.arange(256, dtype=np.float32)/255.).reshape(1, 256),
                        QOpenGLTexture.R32F, QOpenGLTexture.Red, QOpenGLTexture.Float32, protected)
                    protected.append(normal_key)
                    result = self._draw(output_key, (width, height), self.normalize_program,
                        [('sourceTexture', r._get(low_key)), ('highTexture', r._get(high_key)),
                         ('blendTable', table), ('normalizationTable', normal)],
                        dict(blend=amount > 1e-6), protected, floating=True)
                except _BudgetExceeded:
                    return None
            result.owner.bind()
            output = np.empty((height, width, 4), np.float32)
            r.functions.glReadPixels(0, 0, width, height, 0x1908, 0x1406, output)
            result.owner.release()
            r.readbacks += 1
            return output
