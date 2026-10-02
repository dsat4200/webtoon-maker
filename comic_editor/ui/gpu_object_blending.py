"""Bounded OpenGL object blends using the existing offscreen raster engine."""
import logging

from PySide6.QtGui import QImage, QOpenGLContext

from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer


GPU_MODES = {mode: index for index, mode in enumerate((
    "linear_burn", "linear_dodge", "luminosity", "color", "hue", "saturation",
    "luma_modulate", "texture_multiply", "texture_screen", "texture_contrast",
    "alpha_modulate", "height_modulate"))}

FRAGMENT = """#version 330 core
uniform sampler2D source, original;
uniform int blendMode;
uniform vec2 coreOffset;
uniform float density;
in vec2 uv;
out vec4 color;
float lum(vec3 c) { return dot(c,vec3(.3,.59,.11)); }
float minimum(vec3 c) { return min(c.r,min(c.g,c.b)); }
float maximum(vec3 c) { return max(c.r,max(c.g,c.b)); }
float sat(vec3 c) { return maximum(c)-minimum(c); }
vec3 setSat(vec3 c,float s) { return (c-minimum(c))*s/max(sat(c),1e-7); }
vec3 setLum(vec3 c,float l) {
    c+=l-lum(c);
    float low=minimum(c),high=maximum(c),light=lum(c);
    if(low<0) c=light+(c-light)*light/max(light-low,1e-7);
    if(high>1) c=light+(c-light)*(1-light)/max(high-light,1e-7);
    return c;
}
vec3 overlay(vec3 b,vec3 f) {
    return mix(1-2*(1-b)*(1-f),2*b*f,lessThanEqual(b,vec3(.5)));
}
void main() {
    ivec2 point=ivec2(floor(uv*vec2(textureSize(original,0))));
    ivec2 sp=point+ivec2(coreOffset);
    vec4 b=texelFetch(original,point,0),s=texelFetch(source,sp,0);
    vec3 cb=b.rgb/max(b.a,1e-8),cs=s.rgb/max(s.a,1e-8),blended;
    float alpha=s.a+b.a*(1-s.a);
    if(blendMode==0) blended=max(vec3(0),cb+cs-1);
    else if(blendMode==1) blended=min(vec3(1),cb+cs);
    else if(blendMode==2) blended=setLum(cb,lum(cs));
    else if(blendMode==3) blended=setLum(cs,lum(cb));
    else if(blendMode==4) blended=setLum(setSat(cs,sat(cb)),lum(cb));
    else if(blendMode==5) blended=setLum(setSat(cb,sat(cs)),lum(cb));
    else if(blendMode==6) blended=cb*lum(cs);
    else if(blendMode==7) blended=cb*cs;
    else if(blendMode==8) blended=cb+cs-cb*cs;
    else if(blendMode==9) blended=overlay(cb,vec3(lum(cs)));
    else if(blendMode==10) { color=b*(1-s.a+s.a*lum(cs)); return; }
    else {
        float dx=lum(texelFetch(source,sp+ivec2(1,0),0).rgb)-lum(texelFetch(source,sp-ivec2(1,0),0).rgb);
        float dy=lum(texelFetch(source,sp+ivec2(0,1),0).rgb)-lum(texelFetch(source,sp-ivec2(0,1),0).rgb);
        blended=clamp(cb+clamp((dx+dy)*density,-.5,.5),0,1);
    }
    vec3 rgb;
    if(blendMode>=6) { rgb=b.rgb*(1-s.a)+blended*s.a*b.a; alpha=b.a; }
    else rgb=s.rgb*(1-b.a)+b.rgb*(1-s.a)+blended*s.a*b.a;
    color=vec4(clamp(rgb,vec3(0),vec3(alpha)),alpha);
}
"""


class GpuObjectBlendRenderer(GpuPatternRenderer):
    def __init__(self, *, allow_offscreen=False):
        super().__init__(allow_offscreen=allow_offscreen, fragment=FRAGMENT)

    def composite(self, destination, source, mode, core, density):
        if not self.available or mode not in GPU_MODES or max(source.width(), source.height()) > 1026:
            return None
        previous = QOpenGLContext.currentContext()
        previous_surface = previous.surface() if previous else None
        try:
            if not self.context.makeCurrent(self.surface):
                raise RuntimeError("Could not activate the object blend context")
            # Retain at most two capture-sized textures and one output FBO.
            for texture in (self.source, self.color_source_texture):
                if texture is not None:
                    texture.destroy()
            self.source = self._texture(self._pixels(source), linear=False)
            self.color_source_texture = self._texture(self._pixels(destination), linear=False)
            self.uploads += 2
            self.output = self._framebuffer(self.output, destination.width(), destination.height())
            self._draw(self.output, self.program,
                       {"source": self.source, "original": self.color_source_texture},
                       {"blendMode": GPU_MODES[mode], "coreOffset": (core.x(), core.y()), "density": density})
            self.draws += 1
            return self.output.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
        except Exception as error:
            self.available = False
            self.reason = str(error)
            logging.getLogger(__name__).warning("Object blend acceleration disabled: %s", error)
            return None
        finally:
            self.context.doneCurrent()
            if previous and previous_surface:
                previous.makeCurrent(previous_surface)


def renderer_for(canvas):
    if getattr(canvas.settings, "canvas_renderer", "auto") == "raster":
        return None
    renderer = getattr(canvas, "_gpu_object_blend_renderer", None)
    if renderer is None:
        renderer = GpuObjectBlendRenderer()
        canvas._gpu_object_blend_renderer = renderer
        canvas.destroyed.connect(renderer.close)
    return renderer if renderer.available else None
