"""Persistent, reproducible brush previews for the preset grid.

Thumbnails use a fixed ink color. Palette changes belong to the live settings
preview and never invalidate the library's saved images.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import fields, is_dataclass, replace
import hashlib
import json
from pathlib import Path
import re

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage

from .brush_storage import atomic_write


THUMBNAIL_WIDTH = 160
THUMBNAIL_HEIGHT = 64
THUMBNAIL_COLOR = '#20252b'
# Bump when the preview path or raster renderer changes its visual output.
THUMBNAIL_RENDER_VERSION = 1
BUNDLED_THUMBNAILS = Path(__file__).parent / 'data/brush-thumbnails'
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')
_PNG_DIGESTS = OrderedDict()


def _png_digest(value):
    # Do not retain large source PNG strings or duplicate their entire encoded
    # bytes every time a settings panel refreshes. Immutable strings cache hash.
    stamp = (len(value), hash(value))
    if stamp not in _PNG_DIGESTS:
        digest = hashlib.sha256()
        for offset in range(0, len(value), 65536):
            digest.update(value[offset:offset+65536].encode('utf-8'))
        _PNG_DIGESTS[stamp] = digest.hexdigest()
        while len(_PNG_DIGESTS) > 512:
            _PNG_DIGESTS.popitem(last=False)
    return _PNG_DIGESTS[stamp]


def thumbnail_key(definition):
    """Stable across app restarts; names, IDs and source notes are not pixels."""
    def rendered(value, name=''):
        if name == 'png' and isinstance(value, str):
            return {'png_sha256': _png_digest(value)}
        if is_dataclass(value):
            return {f.name: rendered(getattr(value, f.name), f.name)
                    for f in fields(value) if f.name not in {'id', 'name', 'source', 'warnings'}}
        if isinstance(value, dict):
            return {k: rendered(v, k) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [rendered(v) for v in value]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return value

    data = [THUMBNAIL_RENDER_VERSION, THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT,
            THUMBNAIL_COLOR, rendered(definition)]
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode('utf-8')).hexdigest()


def thumbnail_definition(definition):
    """Keep only rendering data in the cooperative queue and its memory cache."""
    return replace(definition, id='thumbnail', name='Brush', source={}, warnings=(),
                   dual=thumbnail_definition(definition.dual) if definition.dual else None)


def thumbnail_path(key, preferences):
    if not isinstance(key, str) or not _DIGEST.fullmatch(key):
        raise ValueError('Invalid brush thumbnail key.')
    return Path(preferences).parent / 'brush-thumbnails' / (key + '.png')


def load_thumbnail(key, preferences):
    for path in (thumbnail_path(key, preferences), BUNDLED_THUMBNAILS / (key + '.png')):
        if not path.is_file():
            continue
        image = QImage(str(path))
        if not image.isNull() and image.width() == THUMBNAIL_WIDTH and image.height() == THUMBNAIL_HEIGHT:
            return image
    return None


def save_thumbnail(key, image, preferences):
    if image.isNull() or image.width() != THUMBNAIL_WIDTH or image.height() != THUMBNAIL_HEIGHT:
        raise ValueError('Invalid brush thumbnail dimensions.')
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, 'PNG'):
        raise OSError('The brush thumbnail could not be encoded.')
    data = bytes(buffer.data())
    atomic_write(thumbnail_path(key, preferences), lambda stream: stream.write(data))
