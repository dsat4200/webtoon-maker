from __future__ import annotations

import base64
from multiprocessing import shared_memory
import uuid

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage

from comic_editor.integrations.blender_source import (
    HEADER, HEADER_SIZE, MAGIC, PROTOCOL_VERSION, SLOT_COUNT,
    BlenderSourceClient,
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


def test_view_metadata_uses_separate_validated_thumbnail_messages(qapp):
    client = BlenderSourceClient()
    sent = []
    changes = []
    client._send = lambda message, **_kwargs: sent.append(message) or True
    client.viewsChanged.connect(lambda views: changes.append(views))

    client._handle({
        "type": "VIEWS_CHANGED",
        "project_uuid": PROJECT_UUID,
        "views": [{
            "view_uuid": VIEW_UUID,
            "name": "Panel 1",
            "revision": 4,
            "width": 1280,
            "height": 720,
            "dirty": False,
        }],
    })

    assert len(changes) == 1
    assert changes[-1][0].thumbnail.isNull()
    assert sent[-1]["type"] == "GET_THUMBNAIL"
    assert sent[-1]["view_uuid"] == VIEW_UUID

    client._handle({
        "type": "THUMBNAIL",
        "project_uuid": PROJECT_UUID,
        "view_uuid": VIEW_UUID,
        "revision": 4,
        "thumbnail_png": base64.b64encode(_png()).decode("ascii"),
    })
    assert len(changes) == 2
    assert not changes[-1][0].thumbnail.isNull()
    assert changes[-1][0].thumbnail.size() == QImage(64, 64, QImage.Format_ARGB32).size()
    client.deleteLater()


def test_shared_memory_frame_is_copied_premultiplied_and_always_acknowledged(
    qapp,
):
    width = height = 64
    stride = width * 4
    slot_bytes = stride * height
    memory = shared_memory.SharedMemory(
        create=True, size=HEADER_SIZE + SLOT_COUNT * slot_bytes
    )
    try:
        HEADER.pack_into(
            memory.buf, 0, MAGIC, PROTOCOL_VERSION, width, height, stride,
            SLOT_COUNT, slot_bytes, b"test-nonce".ljust(32, b"\0"),
        )
        client = BlenderSourceClient()
        client._active_project_uuid = PROJECT_UUID
        client._active_view_uuid = VIEW_UUID
        client._active_revision = 5
        acknowledgements = []
        client._send = (
            lambda message, **_kwargs: acknowledgements.append(message) or True
        )
        received = []
        client.frameReady.connect(
            lambda project, view, revision, sequence, frame_kind, image:
            received.append((
                project, view, revision, sequence, frame_kind, QImage(image)
            ))
        )
        client._open_stream({
            "project_uuid": PROJECT_UUID,
            "view_uuid": VIEW_UUID,
            "revision": 5,
            "shared_memory": memory.name,
            "header_size": HEADER_SIZE,
            "width": width,
            "height": height,
            "stride": stride,
            "slot_count": SLOT_COUNT,
            "slot_bytes": slot_bytes,
            "pixel_format": "RGBA8_TOP_DOWN_STRAIGHT",
            "frame_kind": "committed",
        })
        assert client._memory is not None

        raw = bytes((20, 100, 220, 128)) * (width * height)
        memory.buf[HEADER_SIZE:HEADER_SIZE + slot_bytes] = raw
        message = {
            "project_uuid": PROJECT_UUID,
            "view_uuid": VIEW_UUID,
            "revision": 5,
            "sequence": 10,
            "slot": 0,
            "width": width,
            "height": height,
            "stride": stride,
            "frame_kind": "committed",
        }
        client._read_frame(message)

        assert len(received) == 1
        assert received[0][:4] == (PROJECT_UUID, VIEW_UUID, 5, 10)
        assert received[0][4] == "committed"
        assert received[0][5].format() == QImage.Format_ARGB32_Premultiplied
        color = received[0][5].pixelColor(0, 0)
        assert color.alpha() == 128
        assert abs(color.red() - 20) <= 1
        assert abs(color.green() - 100) <= 1
        assert abs(color.blue() - 220) <= 1
        assert acknowledgements[-1] == {
            "type": "FRAME_CONSUMED", "slot": 0, "sequence": 10,
        }

        # The emitted QImage owns its pixels; reusing the provider slot cannot
        # mutate the image already accepted by the editor.
        memory.buf[HEADER_SIZE:HEADER_SIZE + slot_bytes] = b"\0" * slot_bytes
        assert received[0][5].pixelColor(0, 0).alpha() == 128

        # Duplicate/out-of-order frames are discarded but still acknowledged,
        # otherwise Blender would permanently lose that triple-buffer slot.
        client._read_frame(message)
        assert len(received) == 1
        assert acknowledgements[-1]["sequence"] == 10
        client._close_memory()
        client.deleteLater()
    finally:
        memory.close()
        memory.unlink()


def test_stream_descriptor_rejects_unsupported_pixel_layout(qapp):
    client = BlenderSourceClient()
    errors = []
    client.errorOccurred.connect(errors.append)
    client._active_project_uuid = PROJECT_UUID
    client._active_view_uuid = VIEW_UUID
    client._open_stream({
        "project_uuid": PROJECT_UUID,
        "view_uuid": VIEW_UUID,
        "revision": 1,
        "shared_memory": "does-not-matter",
        "header_size": HEADER_SIZE,
        "width": 64,
        "height": 64,
        "stride": 64 * 4,
        "slot_count": SLOT_COUNT,
        "slot_bytes": 64 * 64 * 4,
        "pixel_format": "BGRA8",
        "frame_kind": "committed",
    })
    assert errors == ["Blender sent an invalid stream descriptor"]
    assert client._memory is None
    client.deleteLater()


def test_same_view_activation_does_not_resend_activate_or_restart_stream(qapp):
    client = BlenderSourceClient()
    sent = []
    client._send = lambda message, **_kwargs: sent.append(message) or True
    client._requested_view_uuid = VIEW_UUID
    client._active_view_uuid = VIEW_UUID
    client._stream = {"view_uuid": VIEW_UUID}

    client.activate_view(VIEW_UUID)

    assert sent == []
    client.deleteLater()


def test_dirty_switch_uses_save_discard_cancel_and_accepts_legacy_aliases(qapp):
    client = BlenderSourceClient()
    sent = []
    client._send = lambda message, **_kwargs: sent.append(message) or True

    for requested, expected in (
        ("save", "save"), ("discard", "discard"), ("cancel", "cancel"),
        ("update", "save"), ("revert", "discard"),
    ):
        client.resolve_dirty_switch(requested)
        assert sent[-1]["type"] == "RESOLVE_DIRTY"
        assert sent[-1]["resolution"] == expected

    try:
        client.resolve_dirty_switch("render")
    except ValueError as error:
        assert "save, discard, or cancel" in str(error)
    else:
        raise AssertionError("invalid dirty-switch resolution was accepted")
    client.deleteLater()
