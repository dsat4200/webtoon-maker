from __future__ import annotations

import base64
import uuid

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage

from comic_editor.integrations.blender_source import (
    PROTOCOL_VERSION, BlenderProviderInfo, BlenderSourceClient, ComicViewInfo,
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


@pytest.mark.parametrize("metadata", [
    {},  # Extension 0.5.1 and earlier do not report these fields.
    {"extension_version": None, "blender_version": [], "capabilities": 3},
    {
        "extension_version": "0.6.0", "blender_version": "5.2.1 LTS",
        "capabilities": ["layered_actions", "published_png"],
    },
])
def test_provider_metadata_is_optional_and_cleared_on_disconnect(qapp, metadata):
    client = BlenderSourceClient()
    sent, providers = [], []
    client._send = lambda message, **_kwargs: sent.append(message) or True
    client.providerInfoChanged.connect(providers.append)
    client._handle({"type": "HELLO", "protocol": 3, **metadata})

    assert client.state == "connected"
    assert sent[-1]["type"] == "GET_VIEWS"
    assert isinstance(client.provider_info, BlenderProviderInfo)
    if metadata.get("extension_version"):
        assert client.provider_info.extension_version == "0.6.0"
        assert client.provider_info.blender_version == "5.2.1 LTS"
        assert "layered_actions" in client.provider_info.capabilities
    else:
        assert client.provider_info == BlenderProviderInfo()

    client._socket_disconnected()
    assert client.provider_info is None
    assert providers[-1] is None
    client.deleteLater()


@pytest.mark.parametrize("protocol", [2, None, {}, "invalid"])
def test_invalid_hello_preserves_upgrade_error_after_disconnect(qapp, protocol):
    client = BlenderSourceClient()
    events = []
    client.connectionStateChanged.connect(lambda state: events.append(("state", state)))
    client.errorOccurred.connect(lambda message: events.append(("error", message)))
    client._set_state("connecting")
    client._handle({"type": "HELLO", "protocol": protocol})

    assert client.state == "error"
    assert not client._authorized
    assert events[-1][0] == "error"
    assert "update" in events[-1][1]
    client.deleteLater()


def test_failed_send_does_not_prevent_retrying_view_activation(qapp):
    client = BlenderSourceClient()
    client._send = lambda *_args, **_kwargs: False
    assert not client.activate_view(VIEW_UUID)

    client._send = lambda *_args, **_kwargs: True
    assert client.activate_view(VIEW_UUID)
    assert not client.activate_view(VIEW_UUID)
    client.deleteLater()


@pytest.mark.parametrize("code", ["COMMAND_FAILED", "SAVE_FAILED"])
def test_legacy_animation_error_explains_upgrade_and_allows_switch_retry(qapp, code):
    client = BlenderSourceClient()
    sent, errors = [], []
    client._send = lambda message, **_kwargs: sent.append(message) or True
    client.errorOccurred.connect(errors.append)
    assert client.activate_view(VIEW_UUID)
    request_id = sent[-1]["request_id"]
    client._handle({
        "type": "ERROR", "code": code, "request_id": request_id,
        "message": "'Action' object has no attribute 'fcurves'",
    })

    assert "Webtoon Comic Views 0.6.0" in errors[-1]
    assert "restart Blender" in errors[-1]
    assert client.activate_view(VIEW_UUID)
    assert sent[-1]["request_id"] != request_id
    client.deleteLater()


def test_unrelated_request_error_does_not_release_pending_view_switch(qapp):
    client = BlenderSourceClient()
    sent = []
    client._send = lambda message, **_kwargs: sent.append(message) or True
    assert client.activate_view(VIEW_UUID)
    request_id = sent[-1]["request_id"]
    client._handle({
        "type": "ERROR", "code": "COMMAND_FAILED", "request_id": request_id + 1,
        "message": "Thumbnail unavailable",
    })

    assert not client.activate_view(VIEW_UUID)
    client.deleteLater()
