"""A real Qt pen sequence edits Curves without drawing into the source."""
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPointingDevice, QTabletEvent

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, CurvesModifier, ImageObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


def test_tablet_curves_picker_commits_release_position_once_without_painting(qapp):
    chapter = ChapterDocument(width=160, height=160, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 160, 160))
    obj = chapter.add_object(page.layer_id, ImageObject(x=40, y=40, pixel_width=80, pixel_height=80))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.resize(320, 320)
    canvas.set_document(chapter, TileStore(), ImageStore())
    source = QImage(80, 80, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(128, 128, 128, 180))
    canvas.images.put_decoded(obj.object_id, "source.png", b"", source)
    modifier = CurvesModifier()
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    canvas.set_selection("object", obj.object_id)
    try:
        tool, selection = canvas.tool, list(canvas.selected_entities)
        revision = canvas.command_stack.revision
        canvas.start_curves_picker(modifier.modifier_id, "add_point", "rgb", "master")
        start = canvas.document_to_widget(QPointF(80, 80))
        for phase, dy in ((QEvent.TabletPress, 0), (QEvent.TabletMove, -15), (QEvent.TabletRelease, -40)):
            position = start + QPointF(0, dy)
            button = Qt.NoButton if phase == QEvent.TabletMove else Qt.LeftButton
            buttons = Qt.NoButton if phase == QEvent.TabletRelease else Qt.LeftButton
            event = QTabletEvent(phase, QPointingDevice.primaryPointingDevice(), position,
                QPointF(canvas.mapToGlobal(position.toPoint())), 0 if phase == QEvent.TabletRelease else .65,
                0., 0., 0., 0., 0., Qt.NoModifier, button, buttons)
            QCoreApplication.sendEvent(canvas, event)
            assert event.isAccepted()
        points = canvas.chapter.modifiers[modifier.modifier_id].curves["rgb:master"]
        assert len(points) == 3
        assert points[1][1] == pytest.approx(points[1][0] + .2)
        assert canvas.command_stack.revision == revision + 1
        assert canvas.tool == tool and canvas.selected_entities == selection
        assert canvas._curves_picker.state is None
        assert canvas.images.image(obj.object_id) == source
        assert not canvas.tiles.object_tiles(obj.object_id)
        canvas.command_stack.undo()
        assert canvas.chapter.modifiers[modifier.modifier_id].curves == {}
        canvas.command_stack.redo()
        assert canvas.chapter.modifiers[modifier.modifier_id].curves["rgb:master"] == points
    finally:
        canvas.cancel_curves_picker()
        canvas._effect_jobs.cancel()
        canvas.deleteLater()
