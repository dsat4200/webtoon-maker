"""Local texture categories and portable, original-byte texture selections."""
from __future__ import annotations

import base64
from collections import OrderedDict
import hashlib
from io import BytesIO
from pathlib import Path
from threading import RLock

from PIL import Image, ImageOps

DEFAULT_TEXTURE_DIRECTORY = r"C:\Users\hopper\Documents\Assets\Texturelabs_AllTextures"
TEXTURE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".tga"})


def texture_categories(directory: str) -> dict[str, list[Path]]:
    root = Path(directory).expanduser()
    if not directory or not root.is_dir():
        return {}
    categories = {}
    # Each immediate subfolder owns its nested files; avoid following directory
    # symlinks into another library or cycling through linked folders.
    for folder in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if folder.is_dir() and not folder.is_symlink():
            files = sorted((p for p in folder.rglob("*") if p.is_file()
                            and p.suffix.lower() in TEXTURE_EXTENSIONS), key=lambda p: str(p).casefold())
            categories[folder.name] = files
    loose = sorted((p for p in root.iterdir() if p.is_file()
                    and p.suffix.lower() in TEXTURE_EXTENSIONS), key=lambda p: p.name.casefold())
    if loose:
        categories["(Root folder)"] = loose
    return categories


def texture_thumbnail(path: Path, size=(144, 96)) -> bytes:
    with Image.open(path) as source:
        source.draft("RGB", size)
        source.thumbnail(size, Image.Resampling.LANCZOS)
        image = ImageOps.exif_transpose(source).convert("RGBA")
        output = BytesIO()
        image.save(output, "PNG")
        return output.getvalue()


def import_texture(path: Path) -> str:
    data = path.read_bytes()
    with Image.open(BytesIO(data)) as source:
        source.verify()
    return base64.b64encode(data).decode("ascii")


_DIGESTS = OrderedDict()
_IMAGES = OrderedDict()
_IMAGE_BYTES = 0
_CACHE_LOCK = RLock()


def texture_digest(data: str) -> str:
    key = len(data), hash(data)
    with _CACHE_LOCK:
        digest = _DIGESTS.get(key)
        if digest is None:
            digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
            _DIGESTS[key] = digest
            while len(_DIGESTS) > 32:
                _DIGESTS.popitem(last=False)
    return digest


def texture_image(data: str, width: int, height: int):
    """Decode at the needed display size, retaining the original stored bytes."""
    from PySide6.QtGui import QImage
    if not data:
        return QImage()
    global _IMAGE_BYTES
    key = texture_digest(data), width, height
    with _CACHE_LOCK:
        cached = _IMAGES.get(key)
        if cached is not None:
            _IMAGES.move_to_end(key)
            return QImage(cached)
    try:
        with Image.open(BytesIO(base64.b64decode(data, validate=True))) as source:
            source.draft("RGB", (width, height))
            source.thumbnail((width, height), Image.Resampling.LANCZOS)
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image = image.resize((width, height), Image.Resampling.BILINEAR)
            decoded = QImage(image.tobytes(), width, height, width * 4, QImage.Format_RGBA8888).copy()
            size = int(decoded.sizeInBytes())
            if size <= 64 * 1024 * 1024:
                with _CACHE_LOCK:
                    previous = _IMAGES.pop(key, None)
                    if previous is not None:
                        _IMAGE_BYTES -= int(previous.sizeInBytes())
                    _IMAGES[key] = decoded
                    _IMAGE_BYTES += size
                    while _IMAGE_BYTES > 64 * 1024 * 1024:
                        _, old = _IMAGES.popitem(last=False)
                        _IMAGE_BYTES -= int(old.sizeInBytes())
            return QImage(decoded)
    except (OSError, ValueError):
        return QImage()
