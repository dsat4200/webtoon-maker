from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def flush_deferred_qt_deletes(qapp):
    """Keep full-suite UI tests from retaining every deleteLater() window."""
    yield
    QCoreApplication.sendPostedEvents(
        None, QEvent.Type.DeferredDelete
    )
    qapp.processEvents()

