"""Create a portable, native-size editing project beside an external image."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .images import ImageStore
from .models import BoundGeometry, ChapterDocument, ChapterReference, ImageObject
from .persistence import SeriesRepository
from .tiles import TileStore


EXPORT_FORMATS = {
    ".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
    ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF",
    ".tga": "TGA", ".webp": "WEBP",
}


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
