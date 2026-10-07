from __future__ import annotations

import os
import gc
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFontDatabase
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
def wait_outputs(qapp):
    """Pump owner-thread captures while a detached output completes."""
    import time
    def wait(window, timeout=20):
        deadline = time.monotonic() + timeout
        while window._output_jobs.busy and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.002)
        assert not window._output_jobs.busy, 'Detached output did not finish'
        assert all(error is None for _job, error in window._output_jobs.completed)
    return wait


@pytest.fixture
def wait_scene(qapp):
    """Present the matching detached scene before asserting visible pixels."""
    import time
    from PySide6.QtGui import QImage
    def wait(canvas, timeout=20):
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            canvas.render(image)
            qapp.processEvents()
            # Qt's native test wait can retain the Python GIL. Let the detached
            # evaluator execute while the owner continues staged publication.
            time.sleep(.002)
            controller = canvas._scene_controller
            assert not controller.error, controller.error
            document = canvas._render_document_state()
            if (controller.capture is None and not controller.scheduler.busy
                    and controller.snapshot is not None and controller.snapshot.document == document):
                canvas.render(image)
                if document.live_preview:
                    if controller.preview is not None and controller.preview[0] == document:
                        return image
                elif not canvas._projection_frame_pending:
                    return image
        pytest.fail("Matching detached scene did not finish")
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

