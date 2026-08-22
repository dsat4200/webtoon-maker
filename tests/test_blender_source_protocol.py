from __future__ import annotations

import base64
import uuid

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage

from comic_editor.integrations.blender_source import (
    PROTOCOL_VERSION, BlenderSourceClient, ComicViewInfo,
)


PROJECT_UUID = uuid.UUID(int=401).hex
VIEW_UUID = uuid.UUID(int=402).hex


def _png() -> bytes:
    image = QImage(64, 64, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#7e57c2"))
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


def _view_message(**overrides):
    value = {
        "view_uuid": VIEW_UUID,
        "name": "Panel 1",
        "revision": 4,
        "width": 1280,
        "height": 720,
        "dirty": False,
        "frame_path": r"C:\frames\4.png",
    }
    value.update(overrides)
    return value


def test_view_metadata_carries_published_frame_path():
    view = ComicViewInfo.from_message(PROJECT_UUID, _view_message())
    assert view.project_uuid == PROJECT_UUID
    assert view.view_uuid == VIEW_UUID
    assert view.revision == 4
    assert view.frame_path == r"C:\frames\4.png"
    assert view.thumbnail.isNull()


@pytest.mark.parametrize(
    "overrides",
    [
        {"width": 63},
        {"height": 4097},
        {"width": 4096, "height": 4096 + 1},
        {"view_uuid": "not-a-uuid"},
    ],
)
def test_view_metadata_rejects_invalid_values(overrides):
    with pytest.raises((TypeError, ValueError)):
        ComicViewInfo.from_message(PROJECT_UUID, _view_message(**overrides))


def test_thumbnail_refresh_preserves_frame_path(qapp):
    client = BlenderSourceClient()
    sent = []
    changes = []
    client._send = lambda message, **_kwargs: sent.append(message) or True
    client.viewsChanged.connect(lambda views: changes.append(views))

    client._handle({
        "type": "VIEWS_CHANGED",
        "project_uuid": PROJECT_UUID,
        "views": [_view_message()],
    })
    assert changes[-1][0].frame_path == r"C:\frames\4.png"
    assert sent[-1]["type"] == "GET_THUMBNAIL"

    client._handle({
        "type": "THUMBNAIL",
        "project_uuid": PROJECT_UUID,
        "view_uuid": VIEW_UUID,
        "revision": 4,
        "thumbnail_png": base64.b64encode(_png()).decode("ascii"),
    })
    assert not changes[-1][0].thumbnail.isNull()
    assert changes[-1][0].frame_path == r"C:\frames\4.png"
    client.deleteLater()


def test_same_view_activation_is_idempotent(qapp):
    client = BlenderSourceClient()
    sent = []
    client._send = lambda message, **_kwargs: sent.append(message) or True
    client._requested_view_uuid = VIEW_UUID
    client._active_view_uuid = VIEW_UUID

    assert not client.activate_view(VIEW_UUID)
    assert sent == []
    client.deleteLater()


def test_activation_and_dirty_switch_use_only_control_messages(qapp):
    client = BlenderSourceClient()
    sent = []
    client._send = lambda message, **_kwargs: sent.append(message) or True

    assert client.activate_view(VIEW_UUID)
    assert sent[-1]["type"] == "ACTIVATE_VIEW"
    for requested, expected in (
        ("save", "save"),
        ("discard", "discard"),
        ("cancel", "cancel"),
        ("update", "save"),
        ("revert", "discard"),
    ):
        client.resolve_dirty_switch(requested)
        assert sent[-1]["type"] == "RESOLVE_DIRTY"
        assert sent[-1]["resolution"] == expected
    with pytest.raises(ValueError, match="save, discard, or cancel"):
        client.resolve_dirty_switch("render")
    assert not hasattr(client, "start_stream")
    assert not hasattr(client, "render_once")
    client.deleteLater()


def test_protocol_mismatch_reports_coordinated_upgrade(qapp):
    assert PROTOCOL_VERSION == 3
    client = BlenderSourceClient()
    errors = []
    client.errorOccurred.connect(errors.append)
    client._handle({
        "type": "ERROR",
        "code": "PROTOCOL_MISMATCH",
        "protocol": 2,
        "message": "old extension",
    })
    assert errors
    assert "update" in errors[-1].lower()
    assert "extension" in errors[-1].lower()
    client.deleteLater()
