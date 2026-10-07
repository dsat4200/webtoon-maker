"""Owned drawing clipboard values, independent of editor and render owners."""
from dataclasses import dataclass

from PySide6.QtGui import QImage, QPainterPath, QTransform

from comic_editor.core.models import VectorStroke


@dataclass
class RasterSelectionClipboard:
    tiles: dict[tuple[int, int], QImage]
    selection_path: QPainterPath
    source_to_world: QTransform
    source_name: str
    tile_size: int


@dataclass
class VectorSelectionClipboard:
    strokes: list[VectorStroke]
    source_to_world: QTransform
    source_name: str


DrawingSelectionClipboard = RasterSelectionClipboard | VectorSelectionClipboard
