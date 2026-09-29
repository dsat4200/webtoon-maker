"""Compatibility imports for the renderer-owned document projection."""
from comic_editor.render.projection import (
    RESOLUTION_SCALES, DocumentProjection, ProjectionAddress, ProjectionRequest,
    ProjectionTile,
)

__all__ = ["RESOLUTION_SCALES", "DocumentProjection", "ProjectionAddress",
           "ProjectionRequest", "ProjectionTile"]
