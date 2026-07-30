"""Standalone entry point for the vertical comic editor."""
from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from comic_editor.ui.main_window import MainWindow


def main() -> int:
    QCoreApplication.setAttribute(Qt.AA_CompressTabletEvents, False)
    synthesize = getattr(
        Qt.ApplicationAttribute,
        "AA_SynthesizeMouseForUnhandledTabletEvents",
        None,
    )
    if synthesize is not None:
        QCoreApplication.setAttribute(synthesize, True)
    surface = QSurfaceFormat()
    surface.setRenderableType(QSurfaceFormat.OpenGL)
    surface.setVersion(3, 3)
    surface.setProfile(QSurfaceFormat.CoreProfile)
    surface.setSamples(0)
    surface.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(surface)

    app = QApplication(sys.argv)
    app.setApplicationName("Vertical Comic Editor")
    app.setOrganizationName("VerticalComicEditor")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

