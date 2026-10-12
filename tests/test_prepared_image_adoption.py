"""Detached published-source preparation preserves native pixels and ownership."""
from concurrent.futures import ThreadPoolExecutor
import threading

import numpy as np
import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColorSpace, QImage

from comic_editor.core.encoded_images import EncodedImage
from comic_editor.core.images import ImageStore


def png(image):
    payload, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(payload)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, 'PNG')
    buffer.close()
    return bytes(payload)


def native_image(precision):
    wide = precision == 'uint16'
    image = QImage(9, 7, QImage.Format_RGBA64 if wide else QImage.Format_RGBA8888)
    image.fill(0)
    image.setColorSpace(QColorSpace(QColorSpace.SRgb))
    values = np.frombuffer(image.bits(), np.uint16 if wide else np.uint8)
    values[:4] = [32769, 16385, 8193, 3] if wide else [127, 63, 31, 3]
    return image


@pytest.mark.parametrize('precision', ['uint8', 'uint16'])
def test_worker_preparation_and_gui_adoption_preserve_original_and_native_without_io(qapp, tmp_path, monkeypatch, precision):
    image = native_image(precision)
    original_bytes = png(image)
    original_pixels = bytes(image.constBits())
    original_profile = bytes(image.colorSpace().iccProfile())
    expected_display = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    store = ImageStore()
    gui = threading.get_ident()
    writes, conversions = [], []
    write_bytes = type(tmp_path).write_bytes
    convert = QImage.convertToFormat

    def observed_write(path, data):
        writes.append(threading.get_ident())
        return write_bytes(path, data)

    def observed_convert(owner, *args, **kwargs):
        conversions.append(threading.get_ident())
        return convert(owner, *args, **kwargs)

    monkeypatch.setattr(type(tmp_path), 'write_bytes', observed_write)
    monkeypatch.setattr(QImage, 'convertToFormat', observed_convert)
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared = worker.submit(ImageStore.prepare_decoded, original_bytes, image,
            encoded_cache=store._encoded_cache).result(timeout=3)
    assert writes and all(thread != gui for thread in writes)
    assert conversions and all(thread != gui for thread in conversions)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('Prepared source adoption performed I/O or pixel conversion')

    with monkeypatch.context() as adoption:
        adoption.setattr(type(tmp_path), 'write_bytes', forbidden)
        adoption.setattr(type(tmp_path), 'read_bytes', forbidden)
        adoption.setattr(EncodedImage, 'from_bytes', forbidden)
        adoption.setattr(QImage, 'convertToFormat', forbidden)
        first = store.put_prepared_decoded('first', 'frame.png', prepared)
        second = store.put_prepared_decoded('second', 'frame.png', prepared)
        assert first._encoded is second._encoded is prepared.encoded
        for identifier in ('first', 'second'):
            native = store.native_image(identifier)
            display = store.image(identifier)
            assert native.format() == image.format()
            assert bytes(native.constBits()) == original_pixels
            assert bytes(native.colorSpace().iccProfile()) == original_profile
            assert bytes(display.constBits()) == bytes(expected_display.constBits())

    assert first.data == second.data == original_bytes
    # Both preparation values and acquired borrowers may detach and mutate;
    # none of those mutations changes the stored original or other borrowers.
    prepared.native.fill(0)
    prepared.display.fill(0)
    borrowed = store.native_image('first')
    borrowed.fill(0)
    assert bytes(store.native_image('first').constBits()) == original_pixels
    assert bytes(store.native_image('second').constBits()) == original_pixels
    assert first.data == second.data == original_bytes


def test_prepared_wide_source_with_one_frame_budget_retains_native_precision(qapp):
    original = native_image('uint16')
    store = ImageStore(decoded_budget=original.sizeInBytes(), encoded_budget=0)
    raw = png(original)
    prepared = ImageStore.prepare_decoded(raw, original, encoded_cache=store._encoded_cache)
    store.put_prepared_decoded('wide', 'wide.png', prepared)
    assert list(store._decoded) == [('native', 'wide')]
    assert store._decoded[('native', 'wide')].format() == QImage.Format_RGBA64
    assert bytes(store.native_image('wide').constBits()) == bytes(original.constBits())
    assert store.source('wide').data == raw


def test_null_decoded_preparation_does_not_publish_source_storage(qapp):
    store = ImageStore()
    with pytest.raises(ValueError, match='null decoded'):
        ImageStore.prepare_decoded(b'invalid', QImage(), encoded_cache=store._encoded_cache)
    assert store._encoded_cache._backing is None
