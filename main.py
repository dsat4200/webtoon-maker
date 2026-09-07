"""Standalone entry point for the vertical comic editor."""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer, Qt
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication, QMessageBox

from comic_editor.launch import FileLaunchBroker


def main() -> int:
    parser = argparse.ArgumentParser(description="Open images or series projects in Webtoon Maker.")
    parser.add_argument("files", nargs="*", help="Image files, series folders, or series.json files")
    args = parser.parse_args()
    paths = [str(Path(path).expanduser().resolve()) for path in args.files]
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
    broker = FileLaunchBroker(app)
    try:
        if not broker.start(paths):
            return 0
    except OSError as error:
        QMessageBox.critical(None, "Webtoon Maker", str(error))
        return 1
    app.aboutToQuit.connect(broker.close)
    window = None
    pending: list[str] = list(paths)
    opening = False

    def open_files(files: list[str]) -> None:
        nonlocal opening
        pending.extend(files)
        if opening or window is None:
            return
        opening = True
        try:
            if window.isMinimized():
                window.showNormal()
            window.raise_()
            window.activateWindow()
            while pending:
                window.open_path(pending.pop(0))
        finally:
            opening = False

    broker.files_requested.connect(open_files)
    from comic_editor.ui.main_window import MainWindow
    window = MainWindow()
    window.show()
    QTimer.singleShot(0, lambda: open_files([]))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

