"""Root-only native diagnostics; writes exact differences without tolerances."""
from pathlib import Path
import json
import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QPainter
from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.tools import ToolKind
from comic_editor.ui.document_presentation import draw_document_border
from test_raster_contact_feedback import scene, pixels, ready_paint, native_reference
from test_transformed_raster_contact_feedback import configure_transforms

OUT = Path('.artifacts/refactor-integration-20261007/transformed-raster-feedback-pixel-diagnostic-r2/results')

def summarize(actual, expected):
    a, b = pixels(actual).reshape(actual.height(), actual.width(), 4), pixels(expected).reshape(expected.height(), expected.width(), 4)
    changed = np.any(a != b, axis=2)
    coords = np.argwhere(changed)
    return {'pixel_count': int(changed.sum()), 'channel_count': int((a != b).sum()),
            'interior_2px_count': int(changed[2:-2, 2:-2].sum()),
            'interior_2px_coords': [{'xy': [int(x), int(y)], 'actual': a[y,x].tolist(), 'expected': b[y,x].tolist()}
                                  for y,x in coords if 2 <= y < actual.height()-2 and 2 <= x < actual.width()-2][:300],
            'all_first_40': [{'xy': [int(x),int(y)], 'actual': a[y,x].tolist(), 'expected': b[y,x].tolist()}
                             for y,x in coords[:40]]}

def presented_native(canvas):
    image = native_reference(canvas)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    try:
        draw_document_border(painter, canvas._render_document_state().bounds,
                             canvas.camera_transform(), canvas.size(), owner=canvas)
    finally:
        painter.end()
    return image

@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.BRUSH, ToolKind.RASTER_ERASER])
def test_diagnose_exact_native_transformed_feedback_before_and_during_contact(scene, tool, wait_scene):
    canvas, selected, _front = scene
    configure_transforms(canvas, selected)
    if tool == ToolKind.RASTER_ERASER:
        image = QImage(256,256,QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id,(0,0),image)
    canvas.set_tool(tool)
    wait_scene(canvas)
    OUT.mkdir(parents=True,exist_ok=True)
    result = {'tool':tool.value, 'states':{}}
    def capture(name):
        actual, expected, with_border = ready_paint(canvas), native_reference(canvas), presented_native(canvas)
        actual.save(str(OUT / f'{tool.value}-{name}-actual.png'))
        expected.save(str(OUT / f'{tool.value}-{name}-native.png'))
        with_border.save(str(OUT / f'{tool.value}-{name}-native-with-border.png'))
        result['states'][name] = {'native':summarize(actual,expected), 'native_with_border':summarize(actual,with_border)}
    capture('before')
    try:
        if tool == ToolKind.BRUSH:
            canvas._begin_paint_brush(QPointF(32,64),1.,_definition=BrushDefinition(size=16,antialiasing=0,spacing=.08))
        else:
            canvas._begin_stroke(QPointF(32,64),1.)
        capture('first')
        for x in (72,88,104):
            (canvas._continue_paint_brush if tool == ToolKind.BRUSH else canvas._continue_stroke)(QPointF(x,64),1.)
            capture(f'moved-{x}')
    finally:
        (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
    (OUT / f'{tool.value}.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
