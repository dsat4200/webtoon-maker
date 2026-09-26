"""Launch a saved, isolated brush validation project in the real editor.

Run normally to reopen your test drawings; --new starts a separate sheet.
--build-only creates the project and reference sheet without opening a window.
Optional --sut PATH adds local brush files; their assets stay in local demo
preferences and are not redistributed with the program.
--session PATH reopens a named local playground, or creates it if missing.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from dataclasses import replace

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QMessageBox, QPushButton, QToolBar

from comic_editor.core import settings as settings_module
from comic_editor.core.brush_storage import BrushAssetError
from comic_editor.core.brushes import BrushDefinition, default_brushes
from comic_editor.core.brush_preview import (preview_samples, fit_brush_for_preview,
                                            prepare_blender_preview)
from comic_editor.core.brush_raster import RasterBrushStroke
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ChapterReference, RasterObject, ShapeStyle, TextObject,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore


ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / ".artifacts" / "brush-playground"
COLORS = {"foliage":"#326b34","splatter":"#991b2e","watercolor":"#356abc",
          "wet-paint":"#de9e27","airbrush":"#644da3"}
EXERCISES = {
    "round-pen":"Draw an S. Vary pressure. Cross the same stroke, then make a second pass.",
    "pencil":"Draw lightly, then press harder. Shade across your first line.",
    "airbrush":"Hold still briefly. Move slowly, then quickly. Look for smooth buildup.",
    "watercolor":"Paint through the blue and yellow patches. Compare pickup and edges.",
    "wet-paint":"Pull paint through both patches. Reverse direction and change pressure.",
    "chain":"Draw a long S, a tight curve, then a loop. Inspect pattern continuity.",
    "foliage":"Make a short stroke, a long arc, and separate taps. Compare scattered tips.",
    "splatter":"Tap, vary pressure, then drag. Try another size in Brush settings.",
    "calligraphy":"Make horizontal and vertical strokes, then vary pen pressure.",
    "dual-pencil":"Cross strokes and vary pressure. Compare the two-tip texture.",
    "color-stamp":"Change the drawing color. The original pink and yellow tip should retain its colors.",
    "pixel-dots":"Draw across an earlier stroke. The flat dot pattern should stay aligned and keep its holes.",
    "pixel-stripes":"Draw vertical and diagonal strokes. The horizontal stripes should stay fixed on the canvas.",
    "pixel-checker":"Shade with overlapping strokes. Zoom in to inspect the repeating pixel checker.",
}


def exercise_colors(brush):
    """Useful drawing colors; the zipper pair follows the supplied CSP image."""
    name=brush.name.casefold()
    if name == "vislon close":
        return QColor("white"),QColor("black")
    color=COLORS.get(brush.id)
    if color is None:
        color=("#991b2e" if any(word in name for word in ("blood", "血")) else
               "#326b34" if any(word in name for word in ("leaf", "leaves", "葉")) else
               "#356abc" if any(word in name for word in ("watercolor", "水彩", "油彩")) else "#20252b")
    return QColor(color),QColor(*brush.sub_color)


def exercise_instruction(brush):
    if brush.id in EXERCISES:
        return EXERCISES[brush.id]
    if brush.mixing_mode != "none":
        if brush.paint_amount == 0:
            return "This blender moves existing paint. Drag through the blue and yellow patches into the empty space."
        return "Paint through both color patches, then reverse direction. Try light and heavy pressure."
    if brush.ribbon:
        return "Draw a long S, a tight curve, and a loop. Check how the repeating tips join."
    if brush.spray:
        return "Tap, vary pressure, and draw a slow arc. Compare the spread and direction of the tips."
    name = brush.name.casefold()
    if any(word in name for word in ("leaf", "leaves", "葉")):
        return "Draw an arc with light and heavy pressure. Change the two drawing colors and try again."
    if any(word in name for word in ("blood", "血")):
        return "Try separate taps and fast sweeping strokes. Vary pressure and brush size."
    return "Draw light and heavy strokes, shade across them, then try a fast curved line."


def exercise_size(brush):
    """Use one drawing size for both the reference stroke and its empty pad."""
    fitted = fit_brush_for_preview(brush,100)
    return float(max(1,round(fitted.size)))


def exercise_brush(brush):
    """Resolve the same size control as live drawing, preserving fixed lengths.

    The fitted thumbnail is only a size estimate. Its visual zoom also changes
    texture and other physical lengths, which must not leak into this sample:
    the sample and drawing pad share document pixels and the imported preset.
    """
    return brush.with_size(exercise_size(brush))


def _text(chapter, parent, text, x, y, width, height, size, bold=False):
    return chapter.add_object(parent, TextObject(text=text,x=x,y=y,width=width,height=height,
        font_size=size,bold=bold,margin=0,layout_mode="free",horizontal_alignment="left",vertical_alignment="top"))


def build_sheet(brushes: list[BrushDefinition]):
    height = 180+((len(brushes)+1)//2)*510
    chapter=ChapterDocument(name="Brush validation",height=height,background="#fff2f3f5")
    chapter.grid.enabled=False
    chapter.grid_override_enabled=True
    page=chapter.add_page("Brush playground",BoundGeometry.rectangle(0,0,1080,height),
                          style=ShapeStyle(primary_color="#fff2f3f5",outline_thickness=0))
    _text(chapter,page.layer_id,"BRUSH PLAYGROUND",32,25,1016,46,32,True)
    _text(chapter,page.layer_id,"Choose an exercise above. Draw below the sample. Edit Brush settings to compare the live preview.",
          32,83,1016,66,18)
    tiles=TileStore()
    cells=[]
    for index,brush in enumerate(brushes):
        x,y=28+(index%2)*526,165+(index//2)*510
        layer=chapter.add_layer(page.layer_id,brush.name,
            BoundGeometry.rectangle(x,y,498,482),style=ShapeStyle(primary_color="#ffffffff",
            outline_color="#ffd8dde3",outline_thickness=1))
        _text(chapter,layer.layer_id,f"{index+1:02d}  {brush.name}",x+18,y+15,462,36,22,True)
        instruction=exercise_instruction(brush)
        _text(chapter,layer.layer_id,instruction,x+18,y+58,462,66,15)
        sample=chapter.add_object(layer.layer_id,RasterObject(name="Sample stroke",x=x+18,y=y+128,interaction_rect=(0,0,462,100)))
        color,sub_color=exercise_colors(brush)
        preview=replace(exercise_brush(brush),sub_color=sub_color.getRgb())
        prepare_blender_preview(tiles,sample.object_id,preview,462,100)
        painter=RasterBrushStroke(tiles,sample.object_id,preview,color,{},seed=42,defer_flush=True)
        samples=preview_samples(462,100)
        painter.begin(samples[0])
        for point in samples[1:]:
            painter.add(point)
        painter.finish()
        _text(chapter,layer.layer_id,f"Sample at {preview.size:g} px",
              x+18,y+229,462,18,11)
        target=chapter.add_object(layer.layer_id,RasterObject(name=f"Draw here — {brush.name}",x=x+18,y=y+248,interaction_rect=(0,0,462,213)))
        if brush.mixing_mode != "none":
            for at,patch_color in ((135,"#3a83d1"),(320,"#f3cf3b")):
                tiles.paint_segment(target.object_id,QPointF(at,35),QPointF(at,160),65,65,QColor(patch_color))
        cells.append({"brush_id":brush.id,"name":brush.name,"object_id":target.object_id,
                      "center":[x+249,y+241],"color":color.name(QColor.HexArgb),
                      "sub_color":sub_color.name(QColor.HexArgb),"instruction":instruction,
                      "test_size":exercise_size(brush)})
    return chapter,tiles,cells


def prepare(folder: Path, extra_suts=(), *, imported_only=False):
    folder.mkdir(parents=True,exist_ok=True)
    settings_module.settings_path=lambda:folder/"preferences.json"
    manifest=folder/"playground.json"
    if manifest.exists() and (folder/"project"/"series.json").exists():
        return json.loads(manifest.read_text(encoding="utf-8"))
    brushes=[] if imported_only else default_brushes()
    if extra_suts:
        from comic_editor.core.sut_import import import_sut
        brushes.extend(import_sut(path) for path in extra_suts)
    if not brushes:
        raise ValueError("A new imported-only playground needs at least one --sut brush.")
    settings=settings_module.EditorSettings()
    settings.canvas_renderer="raster"
    settings.grid_overlay_visible=False
    settings.snap_to_grid=False
    settings.brush_presets=[b.to_dict() for b in brushes]
    settings.active_brush_id=brushes[0].id
    settings.brush_size_px=brushes[0].size
    settings.brush_opacity=brushes[0].opacity
    settings_module.save_settings(settings)
    chapter,tiles,cells=build_sheet(brushes)
    repository=SeriesRepository(folder/"project")
    series=repository.create("Brush Playground")
    series.chapters.append(ChapterReference(chapter.chapter_id,chapter.name))
    repository.save_chapter(chapter,tiles)
    repository.save_series(series)
    data={"cells":cells,"chapter_id":chapter.chapter_id,"project":str(repository.root)}
    manifest.write_text(json.dumps(data,indent=2),encoding="utf-8")
    return data


def create_window(folder: Path, data):
    from comic_editor.ui.main_window import MainWindow
    from comic_editor.ui.canvas import ToolKind
    window=MainWindow()
    window.open_path(data["project"])
    window.setWindowTitle(data.get("title", "Brush Playground")+" — Webtoon Maker")
    toolbar=QToolBar("Brush exercises",window)
    toolbar.setObjectName("brushPlaygroundExercises")
    toolbar.setMovable(False)
    toolbar.addWidget(QLabel("  Try a brush:  "))
    exercises=QComboBox(toolbar)
    exercises.setMinimumWidth(230)
    for index,cell in enumerate(data["cells"]):
        exercises.addItem(f"{index+1:02d}  {cell['name']}")
    toolbar.addWidget(exercises)
    overview=QPushButton("Sheet overview",toolbar)
    toolbar.addWidget(overview)
    toolbar.addSeparator()
    toolbar.addWidget(QLabel("  Undo: Ctrl+Z  •  Save drawings: Ctrl+S  •  Brush: Shift+B  "))
    window.addToolBarBreak(Qt.TopToolBarArea)
    window.addToolBar(Qt.TopToolBarArea,toolbar)

    def choose(index):
        if index<0:
            return
        cell=data["cells"][index]
        definition=next((p for p in window.settings.brush_presets if p["id"]==cell["brush_id"]),None)
        if definition:
            controls=window.tool_settings_controls.brush_page
            controls.presets.setCurrentIndex(controls.presets.findData(cell["brush_id"]))
            if "test_size" in cell:
                window.settings.brush_size_px=cell["test_size"]
                controls.refresh()
        window.canvas.set_selection("object",cell["object_id"])
        window._activate_tool(ToolKind.BRUSH)
        window.series.primary_color=QColor(cell["color"]).name(QColor.HexArgb)
        window.series.secondary_color=cell.get("sub_color","#ffffffff")
        window._sync_series_color_ui()
        window.canvas.center_x,window.canvas.center_y=cell["center"]
        # A screen-size brush and a document-size brush agree with the saved
        # reference at 100%. The user may still zoom after choosing an exercise.
        window.canvas.scale=1.0
        window.canvas.rotation=0
        window.canvas._invalidate_scene_cache()
        window.canvas.update()
        window.canvas.cameraChanged.emit()
        window.statusBar().showMessage(cell["instruction"])

    def show_overview():
        canvas=window.canvas
        canvas.center_x,canvas.center_y=540,window.chapter.height/2
        canvas.scale=min(canvas.width()/1120,canvas.height()/(window.chapter.height+40))
        canvas._invalidate_scene_cache()
        canvas.update()
        canvas.cameraChanged.emit()

    exercises.currentIndexChanged.connect(choose)
    overview.clicked.connect(show_overview)
    window._playground_choose=choose
    window._playground_exercises=exercises
    QTimer.singleShot(0,lambda:choose(0))
    return window


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new",action="store_true")
    parser.add_argument("--session",type=Path,help="Create or reopen an isolated playground in this folder")
    parser.add_argument("--build-only",action="store_true")
    parser.add_argument("--snapshot",type=Path,help="Save an offscreen editor view and close")
    parser.add_argument("--sheet-preview",type=Path,help="Save the complete reference sheet and close")
    parser.add_argument("--sut",action="append",default=[])
    parser.add_argument("--imported-only",action="store_true",
                        help="Create a sheet containing only the supplied --sut brushes")
    args=parser.parse_args()
    if args.new and args.session:
        parser.error("Choose --new or --session, not both.")
    if args.build_only or args.snapshot or args.sheet_preview:
        os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
    app=QApplication(sys.argv)
    app.setApplicationName("Webtoon Maker Brush Playground")
    app.setOrganizationName("VerticalComicEditorBrushPlayground")
    if os.environ.get("QT_QPA_PLATFORM")=="offscreen":
        for font_path in ("C:/Windows/Fonts/segoeui.ttf","C:/Windows/Fonts/segoeuib.ttf",
                          "C:/Windows/Fonts/YuGothM.ttc"):
            if Path(font_path).exists():
                QFontDatabase.addApplicationFont(font_path)
        app.setFont(QFont("Segoe UI",9))
    fresh=args.new or args.snapshot or args.sheet_preview or bool(args.sut)
    folder=(args.session.resolve() if args.session else
            ARTIFACTS/(time.strftime("session-%Y%m%d-%H%M%S") if fresh else "current"))
    try:
        data=prepare(folder,args.sut,imported_only=args.imported_only)
        if args.build_only:
            print(data["project"])
            return 0
        window=create_window(folder,data)
    except BrushAssetError as error:
        if args.build_only or args.snapshot or args.sheet_preview:
            if sys.stderr is not None:
                print(str(error),file=sys.stderr)
        else:
            QMessageBox.critical(None,"Brush images unavailable",str(error))
        return 1
    window.show()
    if args.snapshot or args.sheet_preview:
        def capture():
            try:
                if args.snapshot:
                    args.snapshot.parent.mkdir(parents=True,exist_ok=True)
                    window.grab().save(str(args.snapshot))
                if args.sheet_preview:
                    args.sheet_preview.parent.mkdir(parents=True,exist_ok=True)
                    sheet=QImage(1080,window.chapter.height,QImage.Format_ARGB32_Premultiplied)
                    sheet.fill(Qt.white)
                    window.canvas.render_preview(sheet)
                    sheet.save(str(args.sheet_preview))
            finally:
                if window.active_session:
                    window._save_editor_session(window.active_session)
                window.close()
                app.quit()
        QTimer.singleShot(1500,capture)
    return app.exec()


if __name__=="__main__":
    raise SystemExit(main())
