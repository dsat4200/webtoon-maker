"""Embedded images validate on load and decode only when used."""
from io import BytesIO

import pytest
from PIL import Image
from PySide6.QtGui import QColor

from comic_editor.core.images import ImageStore, ImageSource


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


def test_pixel_signature_identifies_immutable_bytes_without_decode_or_read(monkeypatch):
    payload = BytesIO()
    Image.new('RGBA', (8, 8), 'red').save(payload, 'PNG')
    store = ImageStore(decoded_budget=0)
    store.put('art', 'source.png', payload.getvalue(), 'image/png')
    saved = store.snapshot()
    signature = store.pixel_signature('art')
    monkeypatch.setattr(ImageStore, '_decode', staticmethod(lambda *_: pytest.fail('decoded for revision')))
    monkeypatch.setattr(store._encoded_cache, 'read', lambda *_: pytest.fail('read bytes for revision'))
    assert store.pixel_signature('art') == signature
    store.relabel('art', 'renamed.png')
    assert store.pixel_signature('art') == signature
    other = ImageStore()
    store.copy_source_to('art', other, 'copy')
    assert other.pixel_signature('copy') == signature
    store.remove('art')
    assert store.pixel_signature('art') == ()
    store.restore(saved)
    assert store.pixel_signature('art') == signature


def test_incremental_imported_image_save_detects_other_publishers(tmp_path):
    def payload(color):
        buffer = BytesIO()
        Image.new('RGBA', (8, 8), color).save(buffer, 'PNG')
        return buffer.getvalue()
    root = tmp_path/'images'
    red, blue = payload('red'), payload('blue')
    writer = ImageStore()
    writer.put('art', 'source.png', red, 'image/png')
    writer.save_directory(root, {'art'}, complete=True, incremental=True)
    reader = ImageStore()
    reader.load_directory(root, {'art': ('source.png', 'image/png')})
    writer.put('art', 'source.png', blue, 'image/png')
    writer.save_directory(root, {'art'}, complete=True, incremental=True)
    reader.save_directory(root, {'art'}, complete=True, incremental=True)
    assert (root/'art'/'source.png').read_bytes() == red
    assert writer.source('art').data == blue


def test_encoded_residency_is_bounded_and_original_files_survive_replacement(tmp_path):
    import numpy as np
    values, metadata = {}, {}
    root = tmp_path/'images'
    for index in range(8):
        identifier = str(index)
        data = np.random.default_rng(index).integers(0, 256, (89, 97, 4), np.uint8)
        buffer = BytesIO()
        Image.fromarray(data, 'RGBA').save(buffer, 'PNG')
        values[identifier] = buffer.getvalue()
        directory = root/identifier
        directory.mkdir(parents=True)
        (directory/'original.png').write_bytes(values[identifier])
        metadata[identifier] = 'original.png', 'image/png'
    store = ImageStore(encoded_budget=64*1024)
    store.load_directory(root, metadata)
    assert store._encoded_cache.bytes <= 64*1024 < sum(map(len, values.values()))
    # Atomic replacement and deletion cannot change the imported revision.
    replaced = root/'0'/'replacement.tmp'
    replaced.write_bytes(values['7'])
    replaced.replace(root/'0'/'original.png')
    (root/'1'/'original.png').unlink()
    for identifier, expected in values.items():
        assert store.source(identifier).data == expected
        assert store._encoded_cache.bytes <= store._encoded_cache.budget
    copy = tmp_path/'resaved'
    store.save_directory(copy, set(values), complete=True)
    assert all((copy/identifier/'original.png').read_bytes() == data for identifier, data in values.items())


def test_image_history_and_clone_share_immutable_pins_without_reading_bytes(tmp_path, monkeypatch):
    import copy
    from pathlib import Path
    directory = tmp_path/'images'/'art'
    directory.mkdir(parents=True)
    buffer = BytesIO()
    Image.new('RGBA', (8,8), 'red').save(buffer, 'PNG')
    (directory/'source.png').write_bytes(buffer.getvalue())
    store = ImageStore(encoded_budget=0)
    store.load_directory(tmp_path/'images', {'art': ('source.png','image/png')})
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_bytes', lambda *_: pytest.fail('History read encoded bytes'))
        snapshot = copy.deepcopy(store.snapshot())
        cloned = store.clone()
        store.relabel('art', 'renamed.png')
        assert cloned.source('art') is snapshot['art']
        store.restore(snapshot)
    assert store.source('art').data == buffer.getvalue()
    assert store._encoded_cache.bytes == 0


def test_copy_validated_source_reuses_decoded_frame_and_detaches_edits(monkeypatch):
    buffer = BytesIO()
    Image.new('RGBA', (31, 37), 'red').save(buffer, 'PNG')
    source, target = ImageStore(), ImageStore()
    original = source.put('original', 'source.png', buffer.getvalue())
    monkeypatch.setattr(ImageStore, '_decode', staticmethod(lambda *_: pytest.fail('Copied image decoded again')))
    source.copy_source_to('original', target, 'copy')
    assert target.source('copy') is original
    assert target.dirty == {'copy'}
    image = target.image('copy')
    assert image.pixelColor(0, 0) == QColor('red')
    image.fill(QColor('blue'))
    assert source.image('original').pixelColor(0, 0) == QColor('red')
    assert target.image('copy').pixelColor(0, 0) == QColor('red')


def test_image_sources_keep_byte_value_equality_across_private_backings():
    left = ImageSource('source.png', 'image/png', b'original bytes')
    right = ImageSource('source.png', 'image/png', b'original bytes')
    assert left._encoded is not right._encoded
    assert left == right and hash(left) == hash(right)
    assert left != ImageSource('source.png', 'image/png', b'changed bytes')
    assert left != ImageSource('renamed.png', 'image/png', b'original bytes')
    assert left != ImageSource('source.png', 'image/jpeg', b'original bytes')
    assert left != object()


def test_identical_validated_import_skips_decode_and_repin(monkeypatch):
    buffer = BytesIO()
    Image.new('RGBA', (31, 37), 'red').save(buffer, 'PNG')
    store = ImageStore()
    original = store.put('art', 'source.png', buffer.getvalue())
    store.dirty.clear()
    monkeypatch.setattr(ImageStore, '_decode', staticmethod(lambda *_: pytest.fail('Identical import decoded again')))
    assert store.put('art', 'source.png', buffer.getvalue()) is original
    assert store.put('art', 'source.png', buffer.getvalue(), 'image/png') is original
    assert store.dirty == {'art'}
    assert store.image('art').pixelColor(0, 0) == QColor('red')


@pytest.mark.parametrize('change', ['bytes', 'filename', 'mime'])
def test_changed_import_still_validates_and_updates_display(change, monkeypatch):
    red, blue = BytesIO(), BytesIO()
    Image.new('RGBA', (31, 37), 'red').save(red, 'PNG')
    Image.new('RGBA', (31, 37), 'blue').save(blue, 'PNG')
    store = ImageStore()
    original = store.put('art', 'source.png', red.getvalue())
    decoder, calls = ImageStore._decode, []
    def decode(data):
        calls.append(data)
        return decoder(data)
    monkeypatch.setattr(ImageStore, '_decode', staticmethod(decode))
    updated = store.put('art', 'changed.png' if change == 'filename' else 'source.png',
                        blue.getvalue() if change == 'bytes' else red.getvalue(),
                        'image/custom' if change == 'mime' else '')
    assert updated is not original and len(calls) == 1
    assert store.image('art').pixelColor(0, 0) == QColor('blue' if change == 'bytes' else 'red')
