"""One shared gesture for brush-list, settings and comparison previews."""
from __future__ import annotations

import math
from .brushes import BrushInput


def preview_samples(width: float = 320, height: float = 90, count: int = 180) -> list[BrushInput]:
    """A smooth S bend with light ends and a broad middle, lasting 1.5 seconds.

    This is our documented template, not a claim to know CSP's private input
    sequence. Every preview uses this exact device path and a fixed random seed.
    """
    samples = []
    count = max(2,int(count))
    for index in range(count):
        t = index/(count-1)
        samples.append(BrushInput(
            width*(.08+.84*t), height*(.5-.22*math.sin(math.tau*t)),
            max(.015,math.sin(math.pi*t)**.7),
            15+20*math.sin(math.pi*t),10,12*t,1.5*t))
    return samples


def fit_brush_for_preview(definition, height: float):
    """Scale spatial controls together without changing saved brush settings.

    Fit oversized brushes into the preview by a uniform scale of brush spatial
    parameters (including particles, texture, edge, and secondary tip). This is
    preview-only: it never mutates a preset or the editor's drawing size.
    """
    from dataclasses import replace
    def maximum(brush, parameter):
        dynamic = brush.dynamics.get(parameter)
        return max(1., dynamic.tilt_maximum) if dynamic and dynamic.tilt else 1.

    def footprint(brush):
        size = brush.size * maximum(brush, "size")
        thickness = brush.thickness * maximum(brush, "thickness")
        extent = size * max(1., thickness)
        # A rotated long material's across-stroke width, rather than its much
        # longer repeat length, is what must fit the preview's height.
        if brush.ribbon:
            c=abs(math.cos(math.radians(brush.angle)))
            s=abs(math.sin(math.radians(brush.angle)))
            extents=[]
            for tip in brush.tips:
                divisor=max(1,tip.width,tip.height)
                w,h=size*tip.width/divisor,size*tip.height/divisor
                if brush.thickness_axis=="horizontal":
                    w*=thickness
                else:
                    h*=thickness
                extents.append(w*c+h*s)
            extent=max(extents,default=extent)
        if brush.spray:
            particle = brush.particle_size * (size if brush.particle_size_relative else 1)
            particle *= maximum(brush, "particle_size") * max(1., thickness)
            # Particle centers span the brush diameter; their own coverage
            # extends beyond it, sometimes by several times that diameter.
            extent = size + particle
        if brush.dual:
            extent = max(extent, footprint(brush.dual))
        return extent

    extent = footprint(definition)
    factor = min(1,max(1,height*.4)/max(.1,extent))

    def scaled(brush, scale):
        return replace(brush,size=brush.size*scale,
                       particle_size=brush.particle_size*(1 if brush.particle_size_relative else scale),
                       spacing=brush.spacing*(scale if brush.spacing_mode=="fixed" else 1),
                       texture=replace(brush.texture,scale=brush.texture.scale*scale) if brush.texture else None,
                       watercolor_edge=brush.watercolor_edge*scale,
                       watercolor_blur=brush.watercolor_blur*scale,
                       blur_width=brush.blur_width*scale,
                       taper_start=brush.taper_start*(scale if brush.taper_mode!="percentage" else 1),
                       taper_end=brush.taper_end*(scale if brush.taper_mode!="percentage" else 1),
                       dual=scaled(brush.dual,scale) if brush.dual else None)
    return scaled(definition,factor)


def prepare_blender_preview(tiles, object_id, definition, width, height):
    """Give a colorless blender existing paint to move in its shared preview."""
    if definition.mixing_mode == "none" or definition.paint_amount > 0:
        return
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor
    tiles.paint_segment(object_id,QPointF(width*.18,height*.5),
                        QPointF(width*.29,height*.5),height*.62,height*.62,
                        QColor("#6b99bf"))


def iter_brush_preview(definition, width: int = 360, height: int = 120,
                       color=None, sub_color=None):
    """Yield between raster operations, then yield the completed preview image.

    Closing this iterator cancels its private stroke and releases its working
    tiles. Final-path previews skip discarded live paint but retain the exact
    correction/taper replay inputs used by RasterBrushStroke.finish().
    """
    from dataclasses import replace
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QColor, QImage, QPainter
    from .brush_raster import RasterBrushStroke
    from .brush_correction import corrected_samples, resolved_taper, stroke_length
    from .tiles import TileStore

    width, height = max(32,int(width)), max(24,int(height))
    brush=fit_brush_for_preview(definition,height)
    if sub_color is not None:
        brush=replace(brush,sub_color=QColor(sub_color).getRgb())
    tiles=TileStore()
    foreground=QColor(color or "#20252b")
    if foreground.alpha()==0:
        # Erasing needs ink underneath to be visible in a preview.
        from PySide6.QtCore import QRectF
        tiles.paint_segment("preview",QPointF(0,height/2),QPointF(width,height/2),
                            height*.72,height*.72,QColor("#8daac2"))
        brush=replace(brush,blending_mode="erase",mixing_mode="none",
                      sub_color=(*brush.sub_color[:3],0))
        foreground=QColor("black")
    else:
        prepare_blender_preview(tiles,"preview",brush,width,height)
    stroke=RasterBrushStroke(tiles,"preview",brush,foreground,{},seed=42,defer_flush=True)
    samples=preview_samples(width,height)
    try:
        yield None
        if stroke._replay_inputs is not None:
            replay_inputs=[]
            for index,sample in enumerate(samples):
                stage=stroke.scheduler.begin if index==0 else stroke.scheduler.add
                stage(sample)
                replay_inputs.append(sample.sanitized() if stroke._replay_raw_inputs else stroke.scheduler.last)
                if stroke.dual_scheduler is not None:
                    stage=stroke.dual_scheduler.begin if index==0 else stroke.dual_scheduler.add
                    stage(sample)
                yield None
            if stroke._replay_raw_inputs:
                samples=replay_inputs
                path_length=stroke.scheduler.distance
                dual_length=stroke.dual_scheduler.distance if stroke.dual_scheduler else path_length
            else:
                samples=corrected_samples(replay_inputs,brush.post_correction)
                path_length=stroke_length(samples)
                brush=resolved_taper(brush,path_length)
                dual_length=path_length
            stroke._clear_working()
            stroke=RasterBrushStroke(tiles,"preview",brush,foreground,{},seed=42,
                                     defer_flush=True,_path_length=path_length)
            if stroke.dual_scheduler is not None:
                stroke.dual_scheduler.path_length=dual_length
            yield None
        # Use the renderer's existing primitives, with a cancellation point
        # between dabs. Static previews publish only their completed frame.
        for index,sample in enumerate(samples):
            for plane,scheduler in ((stroke.main,stroke.scheduler),(stroke.secondary,stroke.dual_scheduler)):
                if plane is None:
                    continue
                stage=scheduler.begin if index==0 else scheduler.add
                for dab in stage(sample):
                    stroke._render_dabs(plane,(dab,))
                    yield None
            yield None
        for plane,scheduler in ((stroke.main,stroke.scheduler),(stroke.secondary,stroke.dual_scheduler)):
            if plane is None:
                continue
            for dab in scheduler.finish():
                stroke._render_dabs(plane,(dab,))
                yield None
            stroke._finish_ribbon(plane)
            yield None
        stroke._finished=True
        stroke._edge_finished=True
        if brush.watercolor_edge>0:
            stroke._dirty.update(stroke.main.keys())
            if stroke.secondary is not None:
                stroke._dirty.update(stroke.secondary.keys())
        stroke._flush()
        yield None
        image=QImage(width,height,QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor("#b8b8b8"))
        painter=QPainter(image)
        painter.fillRect(width//2,0,width-width//2,height,QColor("#4e4e4e"))
        for key,tile in tiles._tiles.get("preview",{}).items():
            painter.drawImage(QPointF(key[0]*tiles.tile_size,key[1]*tiles.tile_size),tile)
        painter.end()
        yield image
    finally:
        stroke._clear_working()


def render_brush_preview(definition, width: int = 360, height: int = 120,
                         color=None, sub_color=None):
    """Synchronous helper for exported artifacts and noninteractive callers."""
    result=None
    for image in iter_brush_preview(definition,width,height,color,sub_color):
        if image is not None:
            result=image
    return result
