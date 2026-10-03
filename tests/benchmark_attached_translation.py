"""Native effect work avoided by attached translation (offscreen CPU probe)."""
import json
import os
from pathlib import Path
import statistics
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (BoundGeometry, CageTransformModifier, ChapterDocument,
    ColorFillGradientObject, ImageObject, ParameterMaskBinding, PathNode, ToneMask)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import translation_cache


def probe(reuse):
    chapter = ChapterDocument(width=640, height=480, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 640, 480))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    obj = chapter.add_object(page.layer_id, ImageObject(x=100, y=100, pixel_width=256, pixel_height=192))
    data = np.random.default_rng(40).integers(0, 256, (192, 256, 4), dtype=np.uint8)
    data[..., 3] = 255
    image = QImage(data.data, 256, 192, 1024, QImage.Format_RGBA8888).copy()
    canvas.images.put_decoded(obj.object_id, "source.png", b"", image)
    cage = CageTransformModifier(frame=(100, 100, 256, 192), columns=4, rows=4)
    cage.validate()
    x, y = cage.points[5]
    cage.points[5] = (x + 16, y - 8)
    chapter.add_modifier(cage, [("object", obj.object_id)])
    mask = ToneMask(gradient=ColorFillGradientObject(mask_only=True))
    mask.gradient.ramp.stops[0].color = "#00FFFFFF"
    mask.gradient.ramp.stops[-1].color = "#FFFFFFFF"
    mask.gradient.line_field.geometry = BoundGeometry.path([PathNode(x=100, y=100), PathNode(x=356, y=292)])
    chapter.masks[mask.mask_id] = mask
    cage.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    obj.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    canvas.tiles.paint_dab(mask.mask_id, QPointF(125, 125), 40, QColor("white"))
    output = QImage(640, 480, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(output)
    canvas.set_selection("object", obj.object_id)
    canvas._model_before = chapter.to_dict()
    canvas._transform_start_quad = canvas.object_world_quad(obj.object_id)
    canvas._transform_drag_mode = "translate"
    counts = dict(captures=0, effect_fields=0, mask_fields=0)
    for method, key in (("_render_object_content", "captures"), ("_modifier_mask_fields", "effect_fields"), ("render_tone_mask_field", "mask_fields")):
        original = getattr(canvas, method)
        def count(*args, _original=original, _key=key, **kwargs):
            counts[_key] += 1
            return _original(*args, **kwargs)
        setattr(canvas, method, count)
    original_get = translation_cache.get
    if not reuse:
        translation_cache.get = lambda *_: None
    elapsed = []
    try:
        for index in range(12):
            canvas._transform_preview_quad = [(x + index + 1, y + index + 1) for x, y in canvas._transform_start_quad]
            started = time.perf_counter()
            canvas.render_preview(output)
            elapsed.append((time.perf_counter() - started) * 1000)
    finally:
        translation_cache.get = original_get
        canvas._effect_jobs.cancel()
        canvas.deleteLater()
    return dict(median_ms=round(statistics.median(elapsed), 3), **counts)


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    print(json.dumps(dict(reuse_disabled=probe(False), reuse_enabled=probe(True)), indent=2))
