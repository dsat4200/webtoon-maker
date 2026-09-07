"""Optional OpenGL 3.3 cage mesh renderer with a CPU-safe failure path.

A private offscreen context keeps scene-cache rendering independent of the
widget's paint engine. Only the most recent texture/FBO are retained; every
allocation is capped. Context ownership is restored before returning to Qt.
"""
import logging
import math
import numpy as np
from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication, QImage, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject, QOpenGLFunctions_3_3_Core,
    QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture, QOpenGLVertexArrayObject)


VERTEX = """#version 330 core
layout(location=0) in vec2 position;
layout(location=1) in vec2 coordinate;
out vec2 uv;
void main() { uv=coordinate; gl_Position=vec4(position,0.0,1.0); }
"""
FRAGMENT = """#version 330 core
uniform sampler2D source;
uniform int smoothSampling;
in vec2 uv;
out vec4 color;
vec4 samplePixel(ivec2 p) {
    ivec2 size=textureSize(source,0);
    if(any(lessThan(p,ivec2(0))) || any(greaterThanEqual(p,size))) return vec4(0);
    vec4 c=texelFetch(source,p,0);
    return vec4(c.rgb*c.a,c.a);
}
vec4 weights(float t) {
    return vec4(-0.5*t+t*t-0.5*t*t*t,
                1.0-2.5*t*t+1.5*t*t*t,
                0.5*t+2.0*t*t-1.5*t*t*t,
                -0.5*t*t+0.5*t*t*t);
}
void main() {
    vec2 p=uv*vec2(textureSize(source,0));
    if(smoothSampling==0) color=samplePixel(ivec2(floor(p+vec2(0.0001))));
    else {
        p-=vec2(0.5); ivec2 base=ivec2(floor(p)); vec2 f=fract(p);
        if(smoothSampling==1) {
            color=mix(mix(samplePixel(base),samplePixel(base+ivec2(1,0)),f.x),
                      mix(samplePixel(base+ivec2(0,1)),samplePixel(base+ivec2(1,1)),f.x),f.y);
        } else {
            vec4 wx=weights(f.x), wy=weights(f.y); color=vec4(0);
            for(int y=0;y<4;y++) for(int x=0;x<4;x++) color+=samplePixel(base+ivec2(x-1,y-1))*wx[x]*wy[y];
            color=clamp(color,0.0,1.0); color.rgb=min(color.rgb,vec3(color.a));
        }
    }
}
"""


class GpuTextureRenderer:
    def __init__(self, *, allow_offscreen=False):
        self.available = False
        self.reason = ""
        self.context = self.surface = self.program = self.functions = None
        self.texture = self.framebuffer = self.buffer = self.vao = None
        self.texture_key = None
        self.draws = self.uploads = 0
        if not allow_offscreen and QGuiApplication.platformName() in {"offscreen", "minimal"}:
            self.reason = "Headless Qt backend"
            return
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous else None
        try:
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.CoreProfile)
            self.context = QOpenGLContext()
            self.context.setFormat(fmt)
            if not self.context.create():
                raise RuntimeError("OpenGL context unavailable")
            self.surface = QOffscreenSurface()
            self.surface.setFormat(self.context.format())
            self.surface.create()
            if not self.context.makeCurrent(self.surface):
                raise RuntimeError("Offscreen OpenGL surface unavailable")
            self.functions = QOpenGLFunctions_3_3_Core()
            if not self.functions.initializeOpenGLFunctions():
                raise RuntimeError("OpenGL 3.3 unavailable")
            self.program = QOpenGLShaderProgram()
            if not self.program.addShaderFromSourceCode(QOpenGLShader.Vertex, VERTEX) or not self.program.addShaderFromSourceCode(QOpenGLShader.Fragment, FRAGMENT) or not self.program.link():
                raise RuntimeError(self.program.log())
            self.vao = QOpenGLVertexArrayObject()
            self.vao.create()
            self.buffer = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            self.buffer.create()
            self.available = True
        except Exception as error:
            self.reason = str(error)
        finally:
            if self.context:
                self.context.doneCurrent()
            if previous and previous_surface:
                previous.makeCurrent(previous_surface)

    def draw(self, image, vertices, width, height, *, smooth=True):
        if not self.available or image.isNull():
            return None
        if min(width, height) <= 0 or max(width, height, image.width(), image.height()) > 8192 or width*height > 16*1024*1024 or image.sizeInBytes() > 64*1024*1024:
            return None
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous else None
        try:
            if not self.context.makeCurrent(self.surface):
                raise RuntimeError("Could not activate the texture context")
            if self.texture_key != int(image.cacheKey()):
                if self.texture is not None:
                    self.texture.destroy()
                # Qt uploads straight RGBA. The shader premultiplies individual
                # texels before filtering to avoid transparent color fringes.
                self.texture = QOpenGLTexture(image.convertToFormat(QImage.Format_RGBA8888), QOpenGLTexture.DontGenerateMipMaps)
                self.texture.setMinMagFilters(QOpenGLTexture.Nearest, QOpenGLTexture.Nearest)
                self.texture_key = int(image.cacheKey())
                self.uploads += 1
            if self.framebuffer is None or self.framebuffer.size() != QSize(width, height):
                self.framebuffer = QOpenGLFramebufferObject(width, height)
            if not self.framebuffer.isValid() or not self.framebuffer.bind():
                raise RuntimeError("Could not allocate the texture framebuffer")
            gl = self.functions
            gl.glViewport(0, 0, width, height)
            gl.glDisable(0x0BE2)  # GL_BLEND: the result already has premultiplied alpha
            gl.glDisable(0x0B44)  # GL_CULL_FACE: flips/folded mesh faces are intentional
            gl.glClearColor(0., 0., 0., 0.)
            gl.glClear(0x00004000)
            self.program.bind()
            # Explicit integer GL entry points avoid PySide's overloaded
            # setUniformValue selecting the float overload for integer uniforms.
            gl.glUniform1i(self.program.uniformLocation("source"), 0)
            gl.glUniform1i(self.program.uniformLocation("smoothSampling"), int(smooth))
            self.texture.bind(0)
            self.vao.bind()
            self.buffer.bind()
            data = np.ascontiguousarray(vertices, dtype=np.float32).tobytes()
            self.buffer.allocate(data, len(data))
            self.program.enableAttributeArray(0)
            self.program.enableAttributeArray(1)
            self.program.setAttributeBuffer(0, 0x1406, 0, 2, 16)
            self.program.setAttributeBuffer(1, 0x1406, 8, 2, 16)
            gl.glDrawArrays(0x0004, 0, len(vertices))
            self.buffer.release()
            self.vao.release()
            self.texture.release()
            self.program.release()
            result = self.framebuffer.toImage()
            self.framebuffer.release()
            self.draws += 1
            return result.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        except Exception as error:
            self.available = False
            self.reason = str(error)
            logging.getLogger(__name__).warning("Texture acceleration disabled: %s", error)
            return None
        finally:
            self.context.doneCurrent()
            if previous and previous_surface:
                previous.makeCurrent(previous_surface)

    def cage(self, image, bounds, grid, placement, output_bounds):
        from comic_editor.ui.cage_rendering import mesh_for_image
        source, destination, faces = mesh_for_image(grid, bounds, placement)
        size = (output_bounds.width(), output_bounds.height())
        positions = (destination-(output_bounds.x(), output_bounds.y()))/size*(2, -2)+(-1, 1)
        uv = (source-(bounds.x(), bounds.y()))/(bounds.width(), bounds.height())
        vertices = np.concatenate((positions, uv), axis=1)[faces].reshape(-1, 4)
        return self.draw(image, vertices, math.ceil(size[0]), math.ceil(size[1]), smooth={"nearest": 0, "bilinear": 1, "bicubic": 2}[grid.interpolation])

    def close(self):
        self.available = False
        if self.context is None or self.surface is None:
            return
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous else None
        if self.context.makeCurrent(self.surface):
            if self.texture:
                self.texture.destroy()
            if self.buffer:
                self.buffer.destroy()
            if self.vao:
                self.vao.destroy()
            self.framebuffer = self.texture = self.program = self.buffer = self.vao = None
            self.context.doneCurrent()
        if previous and previous_surface:
            previous.makeCurrent(previous_surface)


def renderer_for(canvas):
    if getattr(canvas.settings, "canvas_renderer", "auto") == "raster":
        return None
    renderer = getattr(canvas, "_gpu_texture_renderer", None)
    if renderer is None:
        renderer = GpuTextureRenderer()
        canvas._gpu_texture_renderer = renderer
        canvas.destroyed.connect(renderer.close)
    return renderer if renderer.available else None
