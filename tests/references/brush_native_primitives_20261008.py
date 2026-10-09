"""Original native brush primitive equations, frozen before CPU reuse changes.

This independent reference keeps the original grid construction and per-dab
color calculation. Production sampling/RNG/publication is otherwise shared.
"""
from dataclasses import replace
import colorsys
import math
import numpy as np
from comic_editor.core.brushes import BrushDab
from comic_editor.core.brush_raster import RasterBrushStroke
from comic_editor.core.brush_stroke import BrushStroke


class NativeRasterBrushStroke(RasterBrushStroke):
    def _tip_colors(self,dab,definition):
        main,sub=self.color,self.sub_color
        target=getattr(definition,"color_change_target","main")
        if dab.hue or dab.saturation or dab.luminosity:
            def shifted(color):
                h,s,v=colorsys.rgb_to_hsv(*color[:3])
                result=color.copy()
                result[:3]=colorsys.hsv_to_rgb((h+dab.hue)%1,np.clip(s+dab.saturation,0,1),
                                              np.clip(v+dab.luminosity,0,1))
                return result
            if target in {"main","both"}:
                main=shifted(main)
            if target in {"sub","both"}:
                sub=shifted(sub)
        mix=np.clip(dab.sub_color_mix,0,1)
        result=main*(1-mix)+sub*mix
        if definition.mixing_space == "perceptual" and 0 < mix < 1:
            # Keep the same declared linear-light approximation used by wet
            # mixing; CSP also applies its mixing space to color variation.
            result[:3]=(main[:3]**2.2*(1-mix)+sub[:3]**2.2*mix)**(1/2.2)
        return result,sub

    def _regions(self, bounds):
        n = self.tiles.tile_size
        left, top = math.floor(bounds.left()), math.floor(bounds.top())
        right, bottom = math.ceil(bounds.right()), math.ceil(bounds.bottom())
        for ky in range(top//n, (bottom-1)//n+1):
            for kx in range(left//n, (right-1)//n+1):
                x0, y0 = max(left, kx*n), max(top, ky*n)
                x1, y1 = min(right, (kx+1)*n), min(bottom, (ky+1)*n)
                if x0 >= x1 or y0 >= y1:
                    continue
                yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
                yield (kx, ky), slice(y0-ky*n, y1-ky*n), slice(x0-kx*n, x1-kx*n), xx+.5, yy+.5


class NativeBrushStroke(BrushStroke):
    def _drain(self, final=False) -> list[BrushDab]:
        result = []
        b = self.definition
        tail = b.taper_end if b.taper_mode != "fade" else 0.
        while self.pending and (final or self.pending[0].distance <= self.distance-tail):
            dab = self.pending.popleft()
            final_length = self.distance if final else self.path_length
            def factor(parameter):
                return self._envelope(dab.distance,final_length,parameter)
            updates = {name:getattr(dab,name)*factor(name) for name in b.taper_parameters
                       if name in {"size","opacity","density","thickness","texture_density",
                                   "paint_amount","paint_density","blur","hardness","color_stretch"}}
            if b.spray:
                if "particle_density" in b.taper_parameters and dab.particle_index>=round(dab.particle_count*factor("particle_density")):
                    continue
                if "size" in b.taper_parameters:
                    updates["x"]=dab.center_x+(dab.x-dab.center_x)*factor("size")
                    updates["y"]=dab.center_y+(dab.y-dab.center_y)*factor("size")
                    if not b.particle_size_relative:
                        updates.pop("size",None)
                if "particle_size" in b.taper_parameters:
                    updates["size"]=updates.get("size",dab.size)*factor("particle_size")
            if b.minimum_pixel:
                updates["size"]=max(1.,updates.get("size",dab.size))
            result.append(replace(dab,**updates) if updates else dab)
        return result
