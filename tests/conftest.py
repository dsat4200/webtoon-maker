from __future__ import annotations

import os
import gc
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QRectF
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from comic_editor.core import settings


@pytest.fixture(scope="session", autouse=True)
def isolated_app_settings(tmp_path_factory):
    """Keep UI tests away from user preferences and other pytest processes."""
    path = tmp_path_factory.mktemp("app-settings") / "settings.json"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "settings_path", lambda: path)
        yield


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def await_completed_projection(qapp):
    """Establish an exact resting baseline before testing immediate UI edits."""
    def wait(canvas):
        for _ in range(200):
            qapp.processEvents()
            completed = getattr(canvas, "_projection_completed_view", None)
            if (completed is not None
                    and completed[0] == canvas._projection_configuration()
                    and completed[2] == canvas._document_projection.revision
                    and not canvas._projection_frame_pending
                    and canvas._completed_projection_covers(
                        completed, canvas.visible_document_rect().intersected(
                            QRectF(0, 0, canvas.chapter.width, canvas.chapter.height)))):
                return
            QTest.qWait(10)
        pytest.fail("The initial artwork did not publish a complete current view")
    return wait


@pytest.fixture
def text_outline_font_family(qapp):
    """Load real glyphs when Qt offscreen cannot discover Windows system fonts."""
    for candidate in (Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/segoeui.ttf")):
        if not candidate.is_file():
            continue
        identifier = QFontDatabase.addApplicationFont(str(candidate))
        families = QFontDatabase.applicationFontFamilies(identifier)
        if families:
            try:
                yield families[0]
            finally:
                QFontDatabase.removeApplicationFont(identifier)
            return
    families = QFontDatabase.families()
    if not families:
        pytest.skip("No font available for text outline glyph regressions")
    yield families[0]


@pytest.fixture(autouse=True)
def flush_deferred_qt_deletes(qapp):
    """Retire Qt and Python object cycles between unrelated UI operations."""
    yield
    QCoreApplication.sendPostedEvents(
        None, QEvent.Type.DeferredDelete
    )
    qapp.processEvents()
    # Signal cycles can retain Python-owned Qt trees beyond deleteLater().
    # Collect them at a boundary with no active painter, decoder, or dialog;
    # then flush any native deferred deletions their cleanup scheduled.
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()

