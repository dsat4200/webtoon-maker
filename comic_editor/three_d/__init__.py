"""Blender-linked 3D document, sync, and rendering support."""
from __future__ import annotations

from .documents import (
    COORDINATE_SYSTEM,
    MATRIX_ORDER,
    THREE_D_DOCUMENT_VERSION,
    BlenderChapterDocument,
    CacheManifest,
    ComicFrameDocument,
    DrawingMaterial3D,
    IDENTITY_MATRIX4,
    Matrix4,
    matrix4,
)
from .repository import (
    BLENDER_DIR,
    BlenderSidecarData,
    BlenderSidecarRepository,
)

__all__ = [
    "BLENDER_DIR",
    "COORDINATE_SYSTEM",
    "MATRIX_ORDER",
    "THREE_D_DOCUMENT_VERSION",
    "BlenderChapterDocument",
    "BlenderSidecarData",
    "BlenderSidecarRepository",
    "CacheManifest",
    "ComicFrameDocument",
    "DrawingMaterial3D",
    "IDENTITY_MATRIX4",
    "Matrix4",
    "matrix4",
]
