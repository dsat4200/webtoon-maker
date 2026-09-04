from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
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


@pytest.fixture(autouse=True)
def flush_deferred_qt_deletes(qapp):
    """Keep full-suite UI tests from retaining every deleteLater() window."""
    yield
    QCoreApplication.sendPostedEvents(
        None, QEvent.Type.DeferredDelete
    )
    qapp.processEvents()

