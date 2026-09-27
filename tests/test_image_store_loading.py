"""Embedded images validate on load and decode only when used."""
from io import BytesIO

import pytest
from PIL import Image
from PySide6.QtGui import QColor

from comic_editor.core.images import ImageStore


def test_loading_valid_image_keeps_only_source_bytes_until_requested(tmp_path, monkeypatch):
    directory = tmp_path / "images" / "art"
    directory.mkdir(parents=True)
    payload = BytesIO()
    Image.new("RGBA", (64, 64), (20, 60, 100, 255)).save(payload, "PNG")
    (directory / "source.png").write_bytes(payload.getvalue())
    store = ImageStore()
    decode = ImageStore._decode
    monkeypatch.setattr(ImageStore, "_decode", staticmethod(lambda *_: pytest.fail("decoded during load")))
    store.load_directory(tmp_path / "images", {"art": ("source.png", "image/png")})
    assert store.source("art").data == payload.getvalue()
    assert not store._decoded
    monkeypatch.setattr(ImageStore, "_decode", staticmethod(decode))
    assert store.image("art").pixelColor(0, 0) == QColor(20, 60, 100)


def test_loading_invalid_image_still_rejects_it(tmp_path):
    directory = tmp_path / "images" / "art"
    directory.mkdir(parents=True)
    (directory / "source.png").write_bytes(b"not an image")
    with pytest.raises(ValueError):
        ImageStore().load_directory(tmp_path / "images", {"art": ("source.png", "image/png")})
