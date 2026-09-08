"""Bounded, cached OpenGL raster filters for live modifier previews.

Uploads contain premultiplied RGBA, so blur and cell sampling cannot introduce
color fringes around transparent artwork. The private context is restored after
each draw; an unavailable driver returns ``None`` for the CPU fallback.
"""
from __future__ import annotations

import logging
import math

import numpy as np
from PySide6.QtCore import QSize, QThread
from PySide6.QtGui import QColor, QGuiApplication, QImage, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject, QOpenGLFunctions_3_3_Core,
    QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture, QOpenGLVertexArrayObject)


VERTEX = """#version 330 core
layout(location=0) in vec2 position;
layout(location=1) in vec2 coordinate;
out vec2 uv;
out vec3 triangleBarycentric;
flat out vec2 triangleCenter;
flat out float triangleRadius;
void main() { uv=coordinate; triangleBarycentric=vec3(0); triangleCenter=vec2(0); triangleRadius=0; gl_Position=vec4(position,0,1); }
"""

TRIANGLE_VERTEX = """#version 330 core
layout(location=0) in vec2 position;
layout(location=1) in vec2 center;
layout(location=2) in vec3 barycentric;
uniform vec2 imageSize;
uniform float triangleExtent;
uniform float dotRotation;
out vec2 uv;
out vec3 triangleBarycentric;
flat out vec2 triangleCenter;
flat out float triangleRadius;
void main() {
    vec2 delta=position-center;
    float c=cos(dotRotation),s=sin(dotRotation);
    vec2 p=center+vec2(c*delta.x-s*delta.y,s*delta.x+c*delta.y)*triangleExtent;
    uv=p/imageSize;
    triangleCenter=center;
    triangleRadius=length(delta)*.5;
    triangleBarycentric=barycentric*triangleExtent+(1-triangleExtent)/3;
    gl_Position=vec4(uv*vec2(2,-2)+vec2(-1,1),0,1);
}
"""

BLUR = """#version 330 core
uniform sampler2D source;
uniform vec2 direction;
uniform float sigma;
uniform int sourceFlipped;
in vec2 uv;
out vec4 color;
void main() {
    vec4 total=vec4(0); float weight=0;
    // The target is reduced for wide kernels; 17 taps cover four sigmas.
    for(int i=-8;i<=8;i++) {
        float x=float(i), w=exp(-0.5*x*x/max(sigma*sigma,0.01));
        vec2 p=clamp(uv+x*direction,vec2(0),vec2(1));
        if(sourceFlipped!=0) p.y=1-p.y;
        total+=texture(source,p)*w;
        weight+=w;
    }
    color=total/weight;
}
"""

FRAGMENT = """#version 330 core
uniform sampler2D source;
uniform sampler2D original;
uniform sampler2D gradientMap;
uniform sampler2D customMap;
uniform sampler2D intensityMap;
uniform sampler2D triangleMap;
uniform vec2 imageSize;
uniform int effectType, gridType, dotStyle, colorMode;
uniform int polygonSides, starShape, invertTone, transparentBackground, hasIntensity;
uniform int sourceFlipped;
uniform int trianglePass, customOriginal, evenMergeTone;
uniform float amount, pixelSize, brightness, contrast, saturation;
uniform float spacing, rotation, dotRotation, gammaValue, clampLow, clampHigh;
uniform float levelLow, levelHigh, dotSize, scaleFactor, starInner, cornerRound;
uniform float lineWidth, lineLevelScale, seed;
uniform uint seedBits;
uniform float pointSpacing, stippleJitter, maxNecks, mergeStrength, minNeckWidth;
uniform float collideMin, collideMax;
uniform vec4 foregroundColor, backgroundColor;
in vec2 uv;
in vec3 triangleBarycentric;
flat in vec2 triangleCenter;
flat in float triangleRadius;
out vec4 color;
const float PI=3.14159265358979323846;
const float TAU=6.28318530717958647692;
vec2 rotatePoint(vec2 p,float a) { float c=cos(a),s=sin(a); return vec2(c*p.x-s*p.y,s*p.x+c*p.y); }
vec4 sampleAt(vec2 p) {
    vec2 point=clamp(p/imageSize,vec2(0),vec2(1));
    if(sourceFlipped!=0) point.y=1-point.y;
    return texture(source,point);
}
vec3 straight(vec4 c) { return c.a>0.00001 ? c.rgb/c.a : vec3(1); }
float luminance(vec3 c) { return dot(c,vec3(.2126,.7152,.0722)); }
float toneAt(vec4 c) {
    float l=pow(clamp(luminance(straight(c)),0,1),1/max(gammaValue,.01));
    l=clamp((l-.5)*exp2(contrast)+.5,0,1);
    l=clamp((l-clampLow)/max(clampHigh-clampLow,.0001),0,1);
    float t=invertTone!=0 ? l : 1-l;
    return t<levelLow || t>levelHigh || c.a<.00001 ? -1 : t;
}
uint hashValue(uint h) {
    h=(h^(h>>16u))*2246822519u;
    h=(h^(h>>13u))*3266489917u;
    return h^(h>>16u);
}
vec2 hash2(vec2 p) {
    uint base=uint(int(p.x))*1664525u+uint(int(p.y))*1013904223u+seedBits*2246822519u;
    return vec2(hashValue(base)&16777215u,hashValue(base^1757159915u)&16777215u)/16777216.;
}
float polygonDistance(vec2 p,float radius,float sides) {
    float sector=TAU/sides;
    float a=atan(p.y,p.x)+PI*.5;
    return cos(floor(.5+a/sector)*sector-a)*length(p)-radius;
}
float smoothUnion(float a,float b,float k) {
    float h=clamp(.5+.5*(b-a)/max(k,.00001),0,1);
    return mix(b,a,h)-k*h*(1-h);
}
float dotCoverage(vec2 local,float radius) {
    local=rotatePoint(local,-dotRotation);
    // Analytical derivatives remain stable when a neighboring fragment wraps
    // into the next grid cell (fwidth across that discontinuity flickers).
    float aa=max((abs(local.x)+abs(local.y))/max(length(local),.00001),.7),d;
    if(dotStyle==3) {
        vec2 q=abs(local)-radius*(1-cornerRound);
        d=length(max(q,vec2(0)))+min(max(q.x,q.y),0)-radius*cornerRound;
    }
    else if(dotStyle==2 || dotStyle==4) {
        float sides=dotStyle==2 ? 3 : float(polygonSides);
        d=polygonDistance(local,radius*1.41421356*cos(PI/sides),sides);
        d=mix(d,length(local)-radius,cornerRound);
        if(starShape!=0 && dotStyle==4) {
            float angle=atan(local.y,local.x)+PI*.5;
            float wave=abs(fract(angle/TAU*sides+.5)*2-1);
            float edge=radius*mix(1,starInner,wave);
            d=length(local)-edge;
        }
    } else if(dotStyle==5) d=max(abs(local.y)-radius*.2,abs(local.x)-radius);
    else if(dotStyle==6) {
        vec2 point=local/(max(radius,.001)*2)+.5;
        if(any(lessThan(point,vec2(0))) || any(greaterThan(point,vec2(1)))) return 0;
        return texture(customMap,point).a;
    } else if(dotStyle==7) {
        float angle=atan(local.y,local.x);
        float wobble=1+.14*sin(angle*3+seed)+.08*cos(angle*5-seed);
        d=length(local)-radius*wobble;
    } else if(dotStyle==9) {
        vec2 p=abs(local); d=pow(pow(p.x,4)+pow(p.y,4),.25)-radius;
    } else d=length(local)-radius;
    return 1-smoothstep(-aa*.5,aa*.5,d);
}
vec4 inkColor(vec4 sourceColor,float tone) {
    if(colorMode==2) return vec4(straight(sourceColor),1);
    if(colorMode==1) return texture(gradientMap,vec2(clamp(1-tone,0,1),.5));
    return foregroundColor;
}
void main() {
    vec2 p=uv*imageSize;
    vec4 base=texture(original,uv),effect;
    if(effectType==1) {
        vec2 cell=(floor(p/max(pixelSize,1))+.5)*max(pixelSize,1);
        vec4 sampled=sampleAt(cell);
        vec3 rgb=straight(sampled);
        rgb=clamp((rgb-.5)*(1+contrast/100)+.5+brightness/100,0,1);
        rgb=clamp(mix(vec3(luminance(rgb)),rgb,1+saturation/100),0,1);
        effect=vec4(rgb*sampled.a,sampled.a);
    } else {
        vec2 middle=imageSize*.5;
        vec2 q=rotatePoint(p-middle,-rotation);
        float coverage=0,tone=0;
        vec4 customColor=vec4(0);
        vec4 triangleInk=vec4(0);
        vec4 chosen=sampleAt(p);
        float joinedDistance=1e20;
        int joins=0;
        if(trianglePass!=0) {
            chosen=sampleAt(triangleCenter);
            tone=toneAt(chosen);
            float shrink=dotSize*sqrt(max(0,mix(1,tone,scaleFactor)));
            if(shrink<=.00001 || tone<0) discard;
            float edge=min(triangleBarycentric.x,min(triangleBarycentric.y,triangleBarycentric.z));
            float threshold=(1-shrink)/3;
            float aa=max(fwidth(edge),.0001);
            coverage=tone<0 ? 0 : smoothstep(threshold-aa*.5,threshold+aa*.5,edge);
            if(cornerRound>0) {
                float d=(threshold-edge)/max(length(vec2(dFdx(edge),dFdy(edge))),.00001);
                d=mix(d,length(p-triangleCenter)-triangleRadius*shrink,cornerRound);
                coverage=tone<0 ? 0 : 1-smoothstep(-.5,.5,d);
            }
            if(coverage<=0) discard;
        } else if(dotStyle==8 && gridType!=3 && gridType!=4) {
            triangleInk=texture(triangleMap,vec2(uv.x,1-uv.y));
            coverage=triangleInk.a;
        } else if(gridType==3 || gridType==4) {
            float coordinate=gridType==3 ? q.y : length(q);
            float band=floor(coordinate/spacing+.5)*spacing;
            float along=gridType==3 ? q.x : atan(q.y,q.x)*max(abs(band),spacing);
            along=floor(along/max(pointSpacing,.25)+.5)*max(pointSpacing,.25);
            vec2 center=gridType==3 ? vec2(along,band) : vec2(cos(along/max(abs(band),spacing)),sin(along/max(abs(band),spacing)))*band;
            chosen=sampleAt(rotatePoint(center,rotation)+middle);
            tone=toneAt(chosen);
            float width=lineWidth*spacing*.5*mix(1,clamp(max(tone,0)*lineLevelScale,0,1),scaleFactor);
            float aa=max(fwidth(coordinate),.7);
            coverage=tone<0 || width<=.00001 ? 0 : 1-smoothstep(width-aa*.5,width+aa*.5,abs(coordinate-band));
        } else {
            // A bounded neighborhood permits oversized dots and stipple jitter
            // without traversing a CPU list of every mark on each slider tick.
            vec2 cell=floor(q/spacing+.5);
            if(gridType==1) cell=vec2(floor(q.x/spacing+.5),floor(q.y/(spacing*.8660254)+.5));
            float radialRing=floor(length(q)/spacing+.5);
            for(int iy=-2;iy<=2;iy++) for(int ix=-2;ix<=2;ix++) {
                if(dotSize<=1.5 && (abs(ix)>1 || abs(iy)>1)) continue;
                vec2 index=cell+vec2(ix,iy),center=index*spacing;
                if(gridType==1) center=vec2(index.x+mod(index.y,2)*.5,index.y*.8660254)*spacing;
                else if(gridType==2) {
                    float ring=max(0,radialRing+float(iy));
                    if((ring==0 && ix!=0) || radialRing+float(iy)<0) continue;
                    float count=max(1,floor(TAU*ring+.5));
                    float a=floor(atan(q.y,q.x)/TAU*count+.5)+float(ix);
                    center=vec2(cos(a/count*TAU),sin(a/count*TAU))*ring*spacing;
                } else if(gridType==5) {
                    float density=max(0,toneAt(sampleAt(rotatePoint(center,rotation)+middle)));
                    float collision=mix(collideMin,collideMax,clamp(density,0,1));
                    center+=(hash2(index)-.5)*spacing*stippleJitter/(1+.4*collision);
                }
                vec4 sampled=sampleAt(rotatePoint(center,rotation)+middle);
                float t=toneAt(sampled);
                float radius=spacing*(dotStyle==0 || dotStyle==7 ? .7071068 : .5)*dotSize*sqrt(max(0,mix(1,t,scaleFactor)));
                float mark=t<0 || radius<.00001 ? 0 : dotCoverage(q-center,radius);
                if((dotStyle==7 || dotStyle==9) && t>=0 && radius>.00001) {
                    vec2 local=rotatePoint(q-center,-dotRotation);
                    float d=length(local)-radius;
                    float smoothing=spacing*.18*mergeStrength*minNeckWidth;
                    if(dotStyle==9) {
                        if(evenMergeTone!=0) radius=spacing*.5*dotSize*sqrt(max(0,mix(1,toneAt(sampleAt(p)),scaleFactor)));
                        vec2 rounded=abs(local)-radius*(1-cornerRound);
                        d=length(max(rounded,vec2(0)))+min(max(rounded.x,rounded.y),0)-radius*cornerRound;
                        smoothing=spacing*.15*cornerRound;
                    }
                    bool merge=dotStyle==9 || joins<int(maxNecks);
                    joinedDistance=merge ? smoothUnion(joinedDistance,d,smoothing) : min(joinedDistance,d);
                    if(d<spacing*.5) joins++;
                }
                if(mark>coverage) {
                    coverage=mark; chosen=sampled; tone=max(t,0);
                    if(dotStyle==6 && customOriginal!=0) customColor=texture(customMap,rotatePoint(q-center,-dotRotation)/(max(radius,.001)*2)+.5);
                }
            }
            if(dotStyle==7 || dotStyle==9) {
                float aa=max(fwidth(joinedDistance),.7);
                coverage=1-smoothstep(-aa*.5,aa*.5,joinedDistance);
            }
        }
        vec4 ink=inkColor(chosen,tone);
        if(dotStyle==8 && trianglePass==0 && gridType!=3 && gridType!=4) ink=triangleInk.a>.00001 ? vec4(triangleInk.rgb/triangleInk.a,1) : vec4(0);
        if(dotStyle==6 && customOriginal!=0 && customColor.a>.00001) ink=vec4(customColor.rgb/customColor.a,1);
        vec4 bg=transparentBackground!=0 ? vec4(0) : backgroundColor;
        ink.rgb*=ink.a; bg.rgb*=bg.a;
        if(trianglePass!=0) { color=ink*coverage; return; }
        vec4 mark=ink*coverage;
        effect=(mark+bg*(1-mark.a))*base.a;
    }
    float blend=hasIntensity!=0 ? texture(intensityMap,uv).r : amount;
    color=mix(base,effect,clamp(blend,0,1));
}
"""


class GpuPatternRenderer:
    """One canvas-owned renderer; caches survive changes to modifier sliders."""

    def __init__(self, *, allow_offscreen=False):
        self.available = False
        self.reason = ""
        self.context = self.surface = self.functions = None
        self.program = self.blur_program = self.triangle_program = self.vao = self.buffer = self.triangle_buffer = None
        self.source = self.gradient = self.stamp = self.mask = None
        self.output = self.blur_x = self.blur_y = self.triangle_output = None
        self.source_key = self.blur_key = self.gradient_key = self.stamp_key = None
        self.triangle_key = None
        self.triangle_count = 0
        self.uploads = self.draws = self.blur_draws = 0
        if QGuiApplication.instance() is None:
            self.reason = "No Qt application"
            return
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
            self.program = self._program(FRAGMENT)
            self.blur_program = self._program(BLUR)
            self.triangle_program = self._program(FRAGMENT, TRIANGLE_VERTEX)
            self.vao = QOpenGLVertexArrayObject()
            self.vao.create()
            self.buffer = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            self.buffer.create()
            self.triangle_buffer = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
            self.triangle_buffer.create()
            self.vao.bind()
            self.buffer.bind()
            # Raw uploads use top-first image coordinates; toImage flips the
            # OpenGL framebuffer, so the top vertices must sample v=0.
            vertices = np.array([[-1,-1,0,1],[1,-1,1,1],[-1,1,0,0],[1,1,1,0]], np.float32)
            self.buffer.allocate(vertices.tobytes(), vertices.nbytes)
            self.buffer.release()
            self.vao.release()
            self.available = True
        except Exception as error:
            self.reason = str(error)
        finally:
            if self.context:
                self.context.doneCurrent()
            if previous and previous_surface:
                previous.makeCurrent(previous_surface)

    def _program(self, fragment, vertex=VERTEX):
        result = QOpenGLShaderProgram()
        if (not result.addShaderFromSourceCode(QOpenGLShader.Vertex, vertex)
                or not result.addShaderFromSourceCode(QOpenGLShader.Fragment, fragment)
                or not result.link()):
            raise RuntimeError(result.log())
        return result

    def _texture(self, array, *, linear=True, mipmaps=False):
        array = np.ascontiguousarray(array)
        height, width = array.shape[:2]
        # Tag the already-premultiplied bytes as raw RGBA so Qt's convenient
        # uploader does not unpremultiply them during a format conversion.
        raw = QImage(array.data, width, height, width*4, QImage.Format_RGBA8888)
        texture = QOpenGLTexture(raw, QOpenGLTexture.GenerateMipMaps if mipmaps else QOpenGLTexture.DontGenerateMipMaps)
        texture.setMinMagFilters(QOpenGLTexture.LinearMipMapLinear if mipmaps else QOpenGLTexture.Linear if linear else QOpenGLTexture.Nearest,
                                 QOpenGLTexture.Linear if linear else QOpenGLTexture.Nearest)
        texture.setWrapMode(QOpenGLTexture.ClampToEdge)
        return texture

    @staticmethod
    def _pixels(image):
        rgba = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
        return np.frombuffer(rgba.constBits(), np.uint8).reshape(rgba.height(), rgba.bytesPerLine())[:, :rgba.width()*4].reshape(rgba.height(), rgba.width(), 4).copy()

    def _uniform(self, program, name, value):
        location = program.uniformLocation(name)
        if location < 0:
            return
        gl = self.functions
        if name == "seedBits":
            gl.glUniform1ui(location,int(value)&0xffffffff)
        elif isinstance(value, (int, bool)):
            gl.glUniform1i(location, int(value))
        elif isinstance(value, (tuple, list)):
            if len(value) == 2:
                gl.glUniform2f(location, *map(float, value))
            elif len(value) == 4:
                gl.glUniform4f(location, *map(float, value))
        else:
            gl.glUniform1f(location, float(value))

    def _draw(self, target, program, textures, uniforms, *, triangles=False):
        if not target.isValid() or not target.bind():
            raise RuntimeError("Could not allocate pattern framebuffer")
        gl = self.functions
        gl.glViewport(0, 0, target.width(), target.height())
        gl.glDisable(0x0BE2)  # GL_BLEND
        if triangles:
            gl.glClearColor(0.,0.,0.,0.)
            gl.glClear(0x00004000)
            gl.glEnable(0x0BE2)
            gl.glBlendEquation(0x8008)  # GL_MAX: neighboring antialiased faces form a union
            gl.glBlendFunc(1,1)
        gl.glDisable(0x0B44)  # GL_CULL_FACE
        gl.glDisable(0x0B71)  # GL_DEPTH_TEST
        program.bind()
        for unit, (name, texture) in enumerate(textures.items()):
            self._uniform(program, name, unit)
            gl.glActiveTexture(0x84C0 + unit)
            gl.glBindTexture(0x0DE1, texture if isinstance(texture, int) else texture.textureId())
        for name, value in uniforms.items():
            self._uniform(program, name, value)
        self.vao.bind()
        buffer = self.triangle_buffer if triangles else self.buffer
        buffer.bind()
        program.enableAttributeArray(0)
        program.enableAttributeArray(1)
        stride = 28 if triangles else 16
        program.setAttributeBuffer(0, 0x1406, 0, 2, stride)
        program.setAttributeBuffer(1, 0x1406, 8, 2, stride)
        if triangles:
            program.enableAttributeArray(2)
            program.setAttributeBuffer(2, 0x1406, 16, 3, stride)
        else:
            program.disableAttributeArray(2)
        gl.glDrawArrays(0x0004 if triangles else 0x0005, 0, self.triangle_count if triangles else 4)
        buffer.release()
        self.vao.release()
        program.release()
        target.release()

    def _framebuffer(self, existing, width, height):
        return existing if existing is not None and existing.size() == QSize(width, height) else QOpenGLFramebufferObject(width, height)

    def _filtered_source(self, image, blur):
        if blur <= .001:
            return self.source
        key = (self.source_key, float(blur))
        if key != self.blur_key:
            # Reduce wide kernels; cost never grows with the blur slider.
            downsample = max(1, int(math.ceil(blur / 2.5)))
            width, height = max(1, image.width()//downsample), max(1, image.height()//downsample)
            self.blur_x = self._framebuffer(self.blur_x, width, height)
            self.blur_y = self._framebuffer(self.blur_y, width, height)
            sigma = max(.1, blur/downsample)
            self._draw(self.blur_x, self.blur_program, {"source": self.source},
                       {"sigma": sigma, "direction": (downsample/image.width(), 0.), "sourceFlipped": 0})
            self._draw(self.blur_y, self.blur_program, {"source": int(self.blur_x.texture())},
                       {"sigma": sigma, "direction": (0., 1./height), "sourceFlipped": 1})
            self.blur_key = key
            self.blur_draws += 2
        return int(self.blur_y.texture())

    def render(self, image, modifier, scale=1., *, intensity_mask=None, parameter_fields=None):
        """Return a filtered QImage, or None when a safe CPU fallback is needed.

        ``intensity_mask`` is already mapped to 0..1. Spatial masks for other
        controls currently use the CPU fallback rather than silently ignoring
        them. No GL objects are accessed from effect-worker threads.
        """
        if not self.available or image.isNull():
            return None
        if QThread.currentThread() != self.context.thread():
            return None
        if parameter_fields and any(name != "intensity" and np.ndim(value) > 0 for name, value in parameter_fields.items()):
            return None
        width, height = image.width(), image.height()
        if max(width, height) > 8192 or width*height > 16*1024*1024:
            return None
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous else None
        try:
            if not self.context.makeCurrent(self.surface):
                raise RuntimeError("Could not activate pattern context")
            if self.source_key != int(image.cacheKey()):
                if self.source:
                    self.source.destroy()
                self.source = self._texture(self._pixels(image), mipmaps=True)
                self.source_key = int(image.cacheKey())
                self.blur_key = None
                self.uploads += 1
            is_pixel = type(modifier).__name__ == "PixelateModifier"
            edge = {"short": min(width,height), "long": max(width,height), "width": width, "height": height}.get(getattr(modifier,"fit_mode","short"),min(width,height))
            unit = max(.0001, float(scale)) if is_pixel else edge/max(1.,float(getattr(modifier,"base_resolution",1000)))
            source = self._filtered_source(image, float(getattr(modifier,"blur",0))*unit)
            self.output = self._framebuffer(self.output, width, height)
            textures = {"source": source, "original": self.source}
            uniforms = {"effectType": int(is_pixel), "imageSize": (width,height),
                        "sourceFlipped": int(isinstance(source,int)),
                        "trianglePass": 0,
                        "amount": float(getattr(modifier,"intensity",100))/100,
                        "hasIntensity": int(intensity_mask is not None),
                        "pixelSize": max(1.,float(getattr(modifier,"pixel_size",8))*unit),
                        "brightness": float(getattr(modifier,"brightness",0)),
                        "contrast": float(getattr(modifier,"contrast",0)),
                        "saturation": float(getattr(modifier,"saturation",0))}
            if intensity_mask is not None:
                mask = np.asarray(intensity_mask,dtype=np.float32).squeeze()
                if mask.shape != (height,width):
                    return None
                pixels = np.repeat(np.clip(mask[...,None]*255,0,255).astype(np.uint8),4,axis=2)
                if self.mask:
                    self.mask.destroy()
                self.mask = self._texture(pixels)
                textures["intensityMap"] = self.mask
            if not is_pixel:
                self._halftone_uniforms(modifier, unit, uniforms, textures)
            if (not is_pixel and getattr(modifier,"dot_style","") == "delaunay"
                    and getattr(modifier,"grid_type","") not in {"line","ring"}):
                from comic_editor.ui.pattern_rendering import delaunay_triangles
                triangles = delaunay_triangles(width,height,modifier)
                if self.triangle_key is not triangles:
                    centers = np.repeat(triangles.mean(axis=1)[:,None,:],3,axis=1)
                    barycentric = np.broadcast_to(np.eye(3,dtype=np.float32),(*triangles.shape[:2],3))
                    vertices = np.ascontiguousarray(np.concatenate((triangles,centers,barycentric),axis=2),np.float32)
                    self.triangle_buffer.bind()
                    self.triangle_buffer.allocate(vertices.tobytes(),vertices.nbytes)
                    self.triangle_buffer.release()
                    self.triangle_count = len(triangles)*3
                    self.triangle_key = triangles
                uniforms["trianglePass"] = 1
                uniforms["triangleExtent"] = max(.001,float(getattr(modifier,"size",1)))+2/uniforms["spacing"]
                self.triangle_output = self._framebuffer(self.triangle_output,width,height)
                self._draw(self.triangle_output,self.triangle_program,textures,uniforms,triangles=True)
                textures["triangleMap"] = int(self.triangle_output.texture())
                uniforms["trianglePass"] = 0
            self._draw(self.output, self.program, textures, uniforms)
            result = self.output.toImage()
            self.draws += 1
            return result.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        except Exception as error:
            self.available = False
            self.reason = str(error)
            logging.getLogger(__name__).warning("Pattern acceleration disabled: %s", error)
            return None
        finally:
            self.context.doneCurrent()
            if previous and previous_surface:
                previous.makeCurrent(previous_surface)

    def _halftone_uniforms(self, modifier, unit, uniforms, textures):
        def value(name, default):
            return getattr(modifier, name, default)
        angle = float(value("rotation",0))
        dot_angle = 0. if value("link_rotation",True) else float(value("dot_rotation",0))-angle
        uniforms.update({
            "gridType": {"square":0,"hexagonal":1,"radial":2,"line":3,"ring":4,"stippling":5}.get(value("grid_type","square"),0),
            "dotStyle": {"circle":0,"incircle":1,"triangle":2,"square":3,"polygon":4,"line":5,"custom":6,"blob":7,"delaunay":8,"liquid":9}.get(value("dot_style","circle"),0),
            "colorMode": {"two":0,"gradient":1,"source":2}.get(value("color_mode","two"),0),
            "spacing": max(.5,float(value("spacing",10))*unit),
            "rotation": math.radians(angle), "dotRotation": math.radians(dot_angle),
            "gammaValue": float(value("gamma",1)), "clampLow": float(value("clamp_min",0)),
            "clampHigh": float(value("clamp_max",1)), "levelLow": float(value("level_min",0)),
            "levelHigh": float(value("level_max",1)), "dotSize": float(value("size",1)),
            "scaleFactor": float(value("scale_factor",1)), "invertTone": bool(value("invert",False)),
            "polygonSides": int(value("sides",6)), "starShape": bool(value("star",False)),
            "starInner": float(value("star_inner",.5)), "cornerRound": float(value("corner_rounding",0)),
            "lineWidth": float(value("line_width",1)), "lineLevelScale": float(value("line_level_scale",1)),
            "pointSpacing": float(value("point_spacing",5))*unit,
            "stippleJitter": .5+1/(1+float(value("smoothing_iterations",100))/100),
            "collideMin": float(value("collide_min",.25)), "collideMax": float(value("collide_max",1)),
            "maxNecks": float(value("max_necks",4)), "mergeStrength": float(value("merge_strength",1)),
            "minNeckWidth": float(value("min_neck_width",.5)), "evenMergeTone": bool(value("even_merge_tone",False)),
            "customOriginal": value("custom_render_mode","silhouette") == "original",
            "seed": float(value("stipple_seed",0)),
            "seedBits": int(value("stipple_seed",0)),
            "foregroundColor": QColor(value("foreground","#FF000000")).getRgbF(),
            "backgroundColor": QColor(value("background","#FFFFFFFF")).getRgbF(),
            "transparentBackground": bool(value("transparent_background",False)),
        })
        if value("color_mode","two") == "gradient":
            gradient_key = (repr(value("gradient_stops",[[0,"#FF000000"],[1,"#FFFFFFFF"]])),value("gradient_interpolation","rgb"))
            if self.gradient_key != gradient_key:
                from comic_editor.ui.pattern_rendering import gradient_lut
                pixels = gradient_lut(value("gradient_stops",[[0,"#FF000000"],[1,"#FFFFFFFF"]]),value("gradient_interpolation","rgb"))
                pixels = np.asarray(pixels)
                if np.issubdtype(pixels.dtype,np.floating):
                    pixels = np.clip(pixels*255,0,255).astype(np.uint8)
                if self.gradient:
                    self.gradient.destroy()
                self.gradient = self._texture(pixels.reshape(1,-1,4))
                self.gradient_key = gradient_key
            textures["gradientMap"] = self.gradient
        if value("dot_style","circle") == "custom":
            svg = value("custom_svg","")
            if self.stamp_key != svg:
                from comic_editor.ui.pattern_rendering import custom_stamp
                stamp = custom_stamp(svg)
                if stamp is None or stamp.isNull():
                    raise ValueError("Custom SVG has no drawable shape")
                if self.stamp:
                    self.stamp.destroy()
                self.stamp = self._texture(self._pixels(stamp))
                self.stamp_key = svg
            textures["customMap"] = self.stamp

    def close(self):
        self.available = False
        if self.context is None or self.surface is None:
            return
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous else None
        if self.context.makeCurrent(self.surface):
            for texture in (self.source,self.gradient,self.stamp,self.mask):
                if texture:
                    texture.destroy()
            if self.buffer:
                self.buffer.destroy()
            if self.triangle_buffer:
                self.triangle_buffer.destroy()
            if self.vao:
                self.vao.destroy()
            self.source = self.gradient = self.stamp = self.mask = None
            self.output = self.blur_x = self.blur_y = self.triangle_output = None
            self.program = self.blur_program = self.triangle_program = self.buffer = self.triangle_buffer = self.vao = None
            self.triangle_key = None
            self.context.doneCurrent()
        if previous and previous_surface:
            previous.makeCurrent(previous_surface)


def renderer_for(canvas):
    if getattr(canvas.settings,"canvas_renderer","auto") == "raster":
        return None
    renderer = getattr(canvas,"_gpu_pattern_renderer",None)
    if renderer is None:
        renderer = GpuPatternRenderer()
        canvas._gpu_pattern_renderer = renderer
        canvas.destroyed.connect(renderer.close)
    return renderer if renderer.available else None
