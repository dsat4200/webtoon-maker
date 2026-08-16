from __future__ import annotations

import os
import sys
import time

import pytest
from PySide6.QtWidgets import QWidget

from comic_editor.core.models import (
    BlenderViewObject, BoundGeometry, ChapterDocument,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.ui.blender_viewport import BlenderViewportController
from comic_editor.ui.canvas import CanvasWidget


pytestmark = pytest.mark.skipif(
    sys.platform != "win32"
    or os.environ.get("WEBTOON_BLENDER_INTEGRATION") != "1",
    reason="Set WEBTOON_BLENDER_INTEGRATION=1 for the real Blender smoke test",
)


def _wait(qapp, predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_real_blender_attaches_round_trips_state_and_hides(qapp):
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    shape = chapter.add_layer(
        page.layer_id, "3D panel", BoundGeometry.circle(300, 260, 180)
    )
    frame = chapter.add_object(shape.layer_id, BlenderViewObject())
    host = QWidget()
    host.resize(900, 700)
    canvas = CanvasWidget(EditorSettings(), host)
    canvas.setGeometry(0, 0, 900, 700)
    canvas.set_document(chapter, canvas.tiles, reset_view=False)
    canvas.center_x = 300
    canvas.center_y = 260
    canvas.scale = 1
    controller = BlenderViewportController(host, canvas)
    host.show()
    canvas.set_selection("object", frame.object_id)

    try:
        assert _wait(
            qapp,
            lambda: (
                controller.process.state == "ready"
                and controller.external.hwnd
                and controller.external.last_geometry is not None
                and controller.overlay.isVisible()
            ),
        )
        assert frame.view_state is not None
        original = controller.external.last_geometry.native_global_rect
        canvas.scale = 1.2
        canvas.cameraChanged.emit()
        assert _wait(
            qapp,
            lambda: (
                controller.external.last_geometry is not None
                and controller.external.last_geometry.native_global_rect != original
            ),
            timeout=5.0,
        )
        canvas.rotation = 5
        canvas.cameraChanged.emit()
        assert _wait(qapp, lambda: not controller.overlay.isVisible(), timeout=2.0)
    finally:
        controller.shutdown()
        host.close()
