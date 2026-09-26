"""Durable, content-addressed PNG storage for application brush preferences.

Portable BrushDefinition objects remain fully embedded. Only their on-disk
preferences representation uses relative references alongside settings.json.
Asset files are immutable and never garbage-collected automatically.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile


_PNG = b'\x89PNG\r\n\x1a\n'
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_MAX_PNG_BYTES = 128 * 1024 * 1024


class BrushAssetError(RuntimeError):
    """Loading must stop instead of replacing unavailable brush art with defaults."""


def atomic_write(path: Path, write) -> None:
    """Flush a unique sibling temporary file, then atomically publish it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _asset_path(preferences: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise BrushAssetError('A saved brush image has an invalid reference. The preferences file was not changed.')
    return preferences.parent / 'brush-assets' / (digest+'.png')


def encode_brush_assets(presets, preferences: Path, cache: dict[str, tuple] | None = None):
    """Write each missing image once and return small JSON-safe brush records."""
    previous = cache or {}
    used: dict[str, tuple] = {}

    def image(value):
        saved = used.get(value) or previous.get(value)
        digest = saved[0] if saved else None
        data = None
        if digest is None:
            try:
                data = base64.b64decode(value, validate=True)
            except (ValueError, binascii.Error):
                return value  # Preserve malformed legacy strings unchanged.
            if not data.startswith(_PNG):
                return value
            digest = hashlib.sha256(data).hexdigest()
        target = _asset_path(preferences, digest)
        try:
            stat = target.stat()
        except FileNotFoundError:
            stat = None
        exists = stat is not None
        unchanged = saved and stat and saved[1:] == (stat.st_mtime_ns, stat.st_size)
        if exists and not unchanged:
            with target.open('rb') as stream:
                exists = hashlib.file_digest(stream, 'sha256').hexdigest() == digest
        if not exists:
            if data is None:
                data = base64.b64decode(value, validate=True)
            atomic_write(target, lambda stream: stream.write(data))
            stat = target.stat()
        used[value] = (digest, stat.st_mtime_ns, stat.st_size)
        return {'$brush_png': digest}

    def visit(value):
        if isinstance(value, dict):
            return {key: image(item) if key == 'png' and isinstance(item, str) and item else visit(item)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [visit(item) for item in value]
        return value

    return visit(presets), used


def decode_brush_assets(presets, preferences: Path):
    """Accept embedded legacy PNGs and validated relative references together."""
    decoded: dict[str, str] = {}
    stamps: dict[str, tuple] = {}

    def image(reference):
        digest = reference.get('$brush_png')
        path = _asset_path(preferences, digest)
        if digest not in decoded:
            try:
                before = path.stat()
                if before.st_size > _MAX_PNG_BYTES:
                    raise OSError('image exceeds the brush asset size limit')
                data = path.read_bytes()
                stat = path.stat()
                if (before.st_mtime_ns, before.st_size) != (stat.st_mtime_ns, stat.st_size):
                    raise OSError('image changed while it was being read')
            except OSError as error:
                raise BrushAssetError(f'A saved brush image is unavailable: {path}. Restore the brush-assets folder with its preferences file. Preferences were not changed.') from error
            if not data.startswith(_PNG) or hashlib.sha256(data).hexdigest() != digest:
                raise BrushAssetError(f'A saved brush image is damaged: {path}. Preferences were not changed.')
            decoded[digest] = base64.b64encode(data).decode('ascii')
            stamps[digest] = (digest, stat.st_mtime_ns, stat.st_size)
        return decoded[digest]

    def visit(value):
        if isinstance(value, dict):
            return {key: image(item) if key == 'png' and isinstance(item, dict) and '$brush_png' in item else visit(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    result = visit(presets)
    return result, {value: stamps[digest] for digest, value in decoded.items()}


def preserve_inline_backup(preferences: Path) -> None:
    """Keep the original single-file preferences before first externalization."""
    if not preferences.is_file():
        return
    with preferences.open('rb') as source:
        if b'"brush_asset_storage": 1' in source.read(4096):
            return
    backup = preferences.with_name(preferences.name+'.inline-backup')
    if backup.exists():
        return
    def copy(stream):
        with preferences.open('rb') as source:
            shutil.copyfileobj(source, stream, length=1024*1024)
    atomic_write(backup, copy)
