"""Opt-in autosave snapshot latency and GUI heartbeat measurements."""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui.main_window import MainWindow


@patch("comic_editor.ui.main_window.save_settings", new=lambda _: None)
def main():
    app = QApplication.instance() or QApplication([])
    rng = np.random.default_rng(17)
    for count in (16, 64, 128):
        with tempfile.TemporaryDirectory(prefix="webtoon-autosave-benchmark-") as directory:
            repository = SeriesRepository(Path(directory)/"Series")
            series = repository.create("Benchmark")
            repository.create_chapter(series, "Autosave")
            window = MainWindow()
            window.open_series(repository.root)
            obj = next(obj for obj in window.chapter.objects.values() if isinstance(obj, RasterObject))
            for index in range(count):
                pixels = rng.integers(0, 256, (256, 256, 4), np.uint8)
                pixels[..., 3] = 255
                image = QImage(pixels.data, 256, 256, pixels.strides[0], QImage.Format_RGBA8888).copy()
                window.canvas.tiles.set_tile(obj.object_id, (index % 4, index//4), image)
            window._mark_dirty(None)
            window.autosave_timer.stop()
            # Settle thumbnail/layout work before measuring recovery I/O.
            QTest.qWait(250)
            ticks = [time.perf_counter()]
            timer = QTimer()
            timer.setInterval(5)
            timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
            timer.start()
            started = time.perf_counter()
            window._autosave()
            snapshot_ms = (time.perf_counter()-started)*1000
            deadline = started+30
            while (window._autosave_jobs.running is not None or window._autosave_jobs.pending) and time.perf_counter() < deadline:
                QTest.qWait(1)
            assert window._autosave_jobs.running is None
            elapsed = (time.perf_counter()-started)*1000
            timer.stop()
            gaps = [(right-left)*1000 for left, right in zip(ticks, ticks[1:])]
            submissions = window._autosave_jobs.submitted
            window.active_session.last_autosave = 0
            window._autosave()
            assert window._autosave_jobs.submitted == submissions
            print(f"{count} noisy 256px tiles: GUI snapshot={snapshot_ms:.2f}ms; "
                  f"background completion={elapsed:.0f}ms; "
                  f"heartbeat max gap={max(gaps, default=0):.2f}ms; "
                  f"ticks={len(gaps)}; unchanged repeat skipped")
            window.autosave_timer.stop()
            for session in window.sessions.values():
                session.dirty = False
            window._dirty = False
            window.close()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()


if __name__ == "__main__":
    main()
