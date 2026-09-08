"""Repeatable no-window GPU benchmark and visual/semantic pattern checks.

Run ``python tests/benchmark_pattern_effects.py --width 1920 --height 1080``.
The Qt application creates no windows. A real OpenGL driver is required; the
normal offscreen/minimal Qt platforms deliberately select the CPU fallback.
Results and a contact sheet are saved under .artifacts/pattern-modifiers.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import statistics
import sys
import time

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QTransform
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comic_editor.core.models import HalftoneModifier, PixelateModifier
from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer
from comic_editor.ui.pattern_rendering import apply_pattern_effect


def pixels(image):
    converted = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.frombuffer(converted.constBits(), np.uint8).reshape(converted.height(), converted.bytesPerLine())[:, :converted.width()*4].reshape(converted.height(), converted.width(), 4).copy()


def source_image(width, height):
    y, x = np.mgrid[:height,:width].astype(np.float32)
    x /= max(1,width-1)
    y /= max(1,height-1)
    colors = np.stack((.1+.85*x,.1+.8*y,.85-.5*x*y,np.ones_like(x)),axis=2)
    data = np.ascontiguousarray(np.rint(colors*255).astype(np.uint8))
    result = QImage(data.data,width,height,width*4,QImage.Format_RGBA8888).copy()
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#101521"))
    painter.drawEllipse(QRectF(width*.1,height*.16,width*.37,height*.65))
    painter.setBrush(QColor("#faf8e9"))
    painter.drawEllipse(QRectF(width*.2,height*.23,width*.21,height*.37))
    painter.setPen(QPen(QColor("#352551"),max(2.,height*.08)))
    painter.drawLine(int(width*.53),int(height*.79),int(width*.84),int(height*.2))
    painter.end()
    return result


def target_image(width,height):
    """A different aligned palette; the empty band exercises source fallback."""
    y,x=np.mgrid[:height,:width].astype(np.float32)
    x/=max(1,width-1)
    y/=max(1,height-1)
    colors=np.stack((.9-.75*y,.2+.6*x,.18+.7*y,np.where(x<.15,0.,1.)),axis=2)
    data=np.ascontiguousarray(np.rint(colors*255).astype(np.uint8))
    return QImage(data.data,width,height,width*4,QImage.Format_RGBA8888).copy()


def target_for(modifier,image):
    return image if getattr(modifier,"color_mode","") == "target_layer" else None


def variants():
    modes = [(name.title(),HalftoneModifier(grid_type=name))
             for name in ("square","hexagonal","radial","line","ring","stippling")]
    modes += [("Pixelate",PixelateModifier(pixel_size=12)),
              ("Pixelate + blur",PixelateModifier(pixel_size=12,blur=10)),
              ("Delaunay",HalftoneModifier(dot_style="delaunay")),
              ("Blob",HalftoneModifier(dot_style="blob")),
              ("Liquid",HalftoneModifier(dot_style="liquid",corner_rounding=.5)),
              ("Polygon / source",HalftoneModifier(dot_style="polygon",color_mode="source",transparent_background=True)),
              ("Gradient / OKLCH",HalftoneModifier(color_mode="gradient",gradient_interpolation="oklch",
                  gradient_stops=[[0,"#FF23B5AD"],[.5,"#FFFFD166"],[1,"#FF58296B"]])),
              ("Target layer / HSL",HalftoneModifier(color_mode="target_layer",target_hue=35,target_saturation=10))]
    return modes


def semantic_checks(renderer):
    image=source_image(160,128)
    target=target_image(160,128)
    identity=renderer.render(image,PixelateModifier(pixel_size=1))
    assert identity is not None,renderer.reason
    assert np.max(np.abs(pixels(identity).astype(np.int16)-pixels(image).astype(np.int16)))<=1,"Pixel size 1 must preserve source colors"
    zero=renderer.render(image,HalftoneModifier(),intensity_mask=np.zeros((128,160),np.float32))
    assert np.array_equal(pixels(zero),pixels(image)),"Zero intensity mask must preserve the original"
    gray=renderer.render(image,PixelateModifier(saturation=-100))
    gray_data=pixels(gray).astype(np.int16)
    assert np.max(np.abs(gray_data[...,0]-gray_data[...,1]))<=1,"Saturation -100 must be grayscale"
    translucent=QImage(160,128,QImage.Format_ARGB32_Premultiplied)
    translucent.fill(QColor(120,75,190,113))
    for _,modifier in variants()[:11]:
        result=renderer.render(translucent,modifier)
        assert np.max(np.abs(pixels(result)[...,3].astype(np.int16)-113))<=1,"Opaque-background effects must preserve source alpha"
    for modifier in (HalftoneModifier(grid_type="line",line_width=0),
                     HalftoneModifier(grid_type="ring",line_width=0),
                     HalftoneModifier(dot_style="delaunay",size=0)):
        modifier.transparent_background=True
        assert not np.any(pixels(renderer.render(translucent,modifier))[...,3]),"Zero-size marks must be fully transparent"
    comparisons=[]
    for name,modifier in [*variants()[:11],variants()[-1]]:
        color_source=target_for(modifier,target)
        gpu=pixels(renderer.render(image,modifier,color_source=color_source)).astype(np.float32)/255
        cpu=pixels(apply_pattern_effect(image,modifier,color_source=color_source)).astype(np.float32)/255
        difference=np.abs(gpu-cpu)
        tone_difference=float(np.max(np.abs(gpu[...,:3].mean((0,1))-cpu[...,:3].mean((0,1)))))
        comparisons.append({"name":name,"mean_absolute_error":round(float(difference.mean()),5),
                            "mean_color_difference":round(tone_difference,5)})
        assert tone_difference<.03,f"{name} CPU/GPU average tone differs by {tone_difference:.3f}"
        assert float(difference.mean())<.05,f"{name} CPU/GPU pixel differences exceed the antialiasing tolerance"
    return comparisons


def pipeline_timings(source,frames):
    from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
    from comic_editor.core.settings import EditorSettings
    from comic_editor.core.tiles import TileStore
    from comic_editor.ui.canvas import CanvasWidget
    from comic_editor.ui.effect_pipeline import render_stages
    canvas=CanvasWidget(EditorSettings(canvas_renderer="auto"))
    bounds=QRectF(0,0,source.width(),source.height())
    chapter=ChapterDocument(width=source.width(),height=source.height(),document_kind="asset")
    page=chapter.add_page("Benchmark",BoundGeometry.rectangle(0,0,source.width(),source.height()))
    page.fill_color,page.border_width=None,0
    color_layer=chapter.add_layer(page.layer_id,"Hidden target colors",
                                 BoundGeometry.rectangle(0,0,source.width(),source.height()))
    color_layer.fill_color,color_layer.border_width="#e45520",0
    color_layer.visible=False
    owner=chapter.add_object(page.layer_id,RasterObject(interaction_rect=(0,0,source.width(),source.height())))
    canvas.set_document(chapter,TileStore())
    result=[]
    try:
        for name,modifier in (("Square",HalftoneModifier()),
                              ("Radial",HalftoneModifier(grid_type="radial")),
                              ("Pixelate + blur",PixelateModifier(blur=10)),
                              ("Target layer / HSL",HalftoneModifier(color_mode="target_layer",
                                                                   target_layer_id=color_layer.layer_id))):
            chapter.add_modifier(modifier,[("object",owner.object_id)])
            start=time.perf_counter()
            render_stages(canvas,source,bounds,[modifier],QTransform())
            first_ms=(time.perf_counter()-start)*1000
            samples=[]
            for frame in range(max(2,frames)):
                if getattr(modifier,"color_mode","") == "target_layer":
                    modifier.target_hue=(frame+1)*5.
                else:
                    modifier.contrast=(frame+1)*.025
                start=time.perf_counter()
                rendered,_=render_stages(canvas,source,bounds,[modifier],QTransform())
                assert not rendered.isNull()
                samples.append((time.perf_counter()-start)*1000)
            result.append({"name":name,"first_ms":round(first_ms,3),"median_ms":round(statistics.median(samples),3),
                           "max_ms":round(max(samples),3)})
        gpu=getattr(canvas,"_gpu_pattern_renderer",None)
        assert gpu is not None and gpu.available,"The canvas pipeline must use the GPU"
        assert gpu.uploads==1,"Parameter edits should reuse the upstream image upload"
        assert gpu.color_uploads==1,"HSL edits should reuse the aligned target-layer capture and upload"
        assert color_layer.visible is False,"Capturing target colors must preserve its hidden state"
        return {"source_uploads":gpu.uploads,"target_uploads":gpu.color_uploads,"timings":result}
    finally:
        gpu=getattr(canvas,"_gpu_pattern_renderer",None)
        if gpu is not None:
            gpu.close()
        canvas.close()
        canvas.deleteLater()


def contact_sheet(renderer,folder):
    tile_width,tile_height,label_height=320,240,32
    modes=[("Source",None),*variants()]
    columns=3
    rows=(len(modes)+columns-1)//columns
    sheet=QImage(columns*tile_width,rows*(tile_height+label_height),QImage.Format_ARGB32_Premultiplied)
    sheet.fill(QColor("#e9e8e5"))
    painter=QPainter(sheet)
    painter.setFont(QFont("Segoe UI",10))
    image=source_image(tile_width,tile_height)
    target=target_image(tile_width,tile_height)
    for index,(name,modifier) in enumerate(modes):
        column,row=index%columns,index//columns
        x,y=column*tile_width,row*(tile_height+label_height)
        if isinstance(modifier,HalftoneModifier):
            modifier=replace(modifier,base_resolution=320)
        result=image if modifier is None else renderer.render(image,modifier,color_source=target_for(modifier,target))
        if result is None:
            raise RuntimeError(renderer.reason)
        painter.drawImage(x,y,result)
        painter.setPen(QColor("#22242a"))
        painter.drawText(QRectF(x+9,y+tile_height,tile_width-18,label_height),Qt.AlignVCenter,name)
    painter.end()
    path=folder/"contact-sheet.png"
    assert sheet.save(str(path))
    return path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width",type=int,default=1920)
    parser.add_argument("--height",type=int,default=1080)
    parser.add_argument("--frames",type=int,default=20)
    parser.add_argument("--allow-offscreen",action="store_true")
    args=parser.parse_args()
    app=QApplication.instance() or QApplication([])
    renderer=GpuPatternRenderer(allow_offscreen=args.allow_offscreen)
    if not renderer.available:
        raise SystemExit(f"GPU unavailable: {renderer.reason}")
    folder=Path(__file__).resolve().parents[1]/".artifacts"/"pattern-modifiers"
    folder.mkdir(parents=True,exist_ok=True)
    try:
        renderer.context.makeCurrent(renderer.surface)
        driver={name:renderer.functions.glGetString(code) for name,code in
                (("vendor",0x1F00),("renderer",0x1F01),("version",0x1F02))}
        renderer.context.doneCurrent()
        checks=semantic_checks(renderer)
        source=source_image(args.width,args.height)
        target=target_image(args.width,args.height)
        timings=[]
        before_uploads=renderer.uploads
        before_color_uploads=renderer.color_uploads
        for name,modifier in variants():
            start=time.perf_counter()
            color_source=target_for(modifier,target)
            result=renderer.render(source,modifier,color_source=color_source)
            first_ms=(time.perf_counter()-start)*1000
            assert result is not None,renderer.reason
            samples=[]
            for frame in range(max(2,args.frames)):
                if color_source is not None:
                    modifier.target_hue=frame*5.
                else:
                    modifier.contrast=frame*.025
                start=time.perf_counter()
                result=renderer.render(source,modifier,color_source=color_source)
                assert result is not None,renderer.reason
                samples.append((time.perf_counter()-start)*1000)
            timings.append({"name":name,"first_ms":round(first_ms,3),
                            "median_ms":round(statistics.median(samples),3),"max_ms":round(max(samples),3)})
        report={"driver":driver,"width":args.width,"height":args.height,
                "frames_per_variant":max(2,args.frames),"source_uploads_during_timings":renderer.uploads-before_uploads,
                "target_uploads_during_timings":renderer.color_uploads-before_color_uploads,
                "timings_include":"source upload when needed, blur when changed, shader draw, synchronous QImage readback",
                "checks":checks,"timings":timings}
        report["canvas_pipeline"]=pipeline_timings(source,args.frames)
        report["contact_sheet"]=str(contact_sheet(renderer,folder))
        (folder/"benchmark.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(json.dumps(report,indent=2))
    finally:
        renderer.close()


if __name__=="__main__":
    main()
