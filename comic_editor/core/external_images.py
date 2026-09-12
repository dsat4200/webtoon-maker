"""Create a portable, native-size editing project beside an external image."""
from __future__ import annotations

import os
import hashlib
import json
import math
from pathlib import Path
import tempfile

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QLineF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen

from .images import ImageStore
from .models import BoundGeometry, ChapterDocument, ChapterReference, ImageObject
from .persistence import SeriesRepository
from .tiles import TileStore


EXPORT_FORMATS = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
    ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF",
    ".tga": "TGA", ".webp": "WEBP",
}


def import_uv_overlay(chapter: ChapterDocument, images: ImageStore, filename: str | Path) -> bool:
    """Import/refresh the optional Blender UV guide without touching painted data.

    Only normalized edge coordinates are accepted from the sidecar: no paths,
    executable content or external resources are followed. The PNG is embedded
    once and subsequent opens with unchanged UVs do no rendering work.
    """
    path = Path(filename).expanduser().resolve()
    sidecar = path.with_name(path.name + ".webtoon.json")
    if not sidecar.is_file():
        return False
    if sidecar.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("The Blender UV map is too large (32 MB maximum).")
    payload = sidecar.read_bytes()
    metadata = json.loads(payload)
    if (not isinstance(metadata, dict) or metadata.get("schema") != "webtoon.texture.v1"
            or metadata.get("image") != path.name):
        raise ValueError("The Blender UV metadata does not match this texture.")
    overlay = metadata.get("uv_overlay")
    if overlay is None:
        return False
    if not isinstance(overlay, dict):
        raise ValueError("Invalid Blender UV map.")
    segments = overlay.get("segments", [])
    if not isinstance(segments, list) or len(segments) > 250_000:
        raise ValueError("The Blender UV map exceeds the 250,000 edge limit.")
    if not segments:
        return False
    width, height = int(chapter.width), int(chapter.height)
    digest = hashlib.sha256(payload + f"/{width}/{height}".encode("ascii")).hexdigest()[:24]
    name = f"uv-map-{digest}.png"
    existing = next((obj for obj in chapter.objects.values()
                     if isinstance(obj, ImageObject) and obj.reference_role == "uv_map"), None)
    if existing is not None and existing.source_filename == name:
        return False
    for segment in segments:
        if not isinstance(segment, list) or len(segment) != 4:
            raise ValueError("Invalid Blender UV edge.")
        try:
            coordinates = [float(value) for value in segment]
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("Invalid Blender UV coordinate.") from error
        if not all(math.isfinite(value) and abs(value) <= 1_000_000 for value in coordinates):
            raise ValueError("Invalid Blender UV coordinate.")
        segment[:] = coordinates
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise ValueError("Could not allocate the UV map image.")
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Keep temporary Qt wrappers bounded even for dense production meshes.
        for color, stroke_width in ((QColor(0, 0, 0, 180), 2.5),
                                    (QColor(255, 255, 255, 220), 1.0)):
            painter.setPen(QPen(color, stroke_width))
            for start in range(0, len(segments), 4096):
                painter.drawLines([
                    QLineF(u1 * width, (1 - v1) * height, u2 * width, (1 - v2) * height)
                    for u1, v1, u2, v2 in segments[start:start + 4096]
                ])
    finally:
        painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise ValueError("Could not encode the Blender UV map.")
    obj = existing or ImageObject(
        name=str(overlay.get("name") or "UV Map")[:160], reference_role="uv_map",
    )
    source = images.put_decoded(obj.object_id, name, bytes(data), image, "image/png")
    obj.source.filename, obj.source.mime_type = source.filename, source.mime_type
    obj.sync_source_metadata()
    obj.pixel_width, obj.pixel_height = width, height
    obj.transform_frame = (0, 0, width, height)
    obj.transform_quad = [(0, 0), (width, 0), (width, height), (0, height)]
    if existing is None:
        if not chapter.root_page_ids:
            raise ValueError("The texture project has no page for its UV map.")
        chapter.add_object(chapter.root_page_ids[0], obj, index=0)
    return True


def prepare_image_project(filename: str | Path) -> Path:
    """Validate before writing; publish a complete project without clobbering one."""
    path = Path(filename).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"Not an image file: {path}")
    if path.suffix.lower() not in EXPORT_FORMATS:
        raise ValueError("External editing supports PNG, JPEG, BMP, TIFF, TGA and WebP images.")
    root = path.with_suffix("")
    if root == path:
        raise ValueError("The image needs a file extension.")
    if root.exists():
        repository = SeriesRepository(root)
        if repository.exists:
            series = repository.load_series()
            if len(series.chapters) == 1:
                chapter, _tiles = repository.load_chapter(series.chapters[0].chapter_id)
                if (chapter.document_kind == "image" and chapter.external_image_path
                        and Path(chapter.external_image_path).name.casefold() == path.name.casefold()):
                    return root
        raise FileExistsError(
            f"The project folder already exists and is not this image's editing project:\n{root}"
        )

    images = ImageStore()
    obj = ImageObject(name=path.name)
    source = images.put(obj.object_id, path.name, path.read_bytes())
    image = images.image(obj.object_id)
    width, height = image.width(), image.height()
    obj.source.filename, obj.source.mime_type = source.filename, source.mime_type
    obj.sync_source_metadata()
    obj.pixel_width, obj.pixel_height = width, height
    obj.transform_frame = (0, 0, width, height)
    obj.transform_quad = [(0, 0), (width, 0), (width, height), (0, height)]
    chapter = ChapterDocument(
        name=path.stem, width=width, height=height, document_kind="image",
        external_image_path=str(path), background="#00000000",
    )
    page = chapter.add_page(path.stem, BoundGeometry.rectangle(0, 0, width, height))
    page.fill_color = None
    page.border_width = 0
    chapter.add_object(page.layer_id, obj)
    import_uv_overlay(chapter, images, path)
    chapter.validate()
    # A failure leaves neither a partial companion project nor a modified source.
    with tempfile.TemporaryDirectory(prefix=".webtoon-image-", dir=path.parent) as temporary:
        staging = Path(temporary) / root.name
        repository = SeriesRepository(staging)
        series = repository.create(path.stem)
        series.chapters.append(ChapterReference(chapter.chapter_id, chapter.name))
        repository.save_chapter(chapter, TileStore(), images)
        repository.save_series(series)
        os.rename(staging, root)
    return root
