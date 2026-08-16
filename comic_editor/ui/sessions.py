"""In-memory editor sessions backing series and asset project tabs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from comic_editor.core.assets import AssetManifest, AssetRepository
from comic_editor.core.models import ChapterDocument, SeriesDocument
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore
from comic_editor.core.images import ImageStore
from comic_editor.ui.canvas import CanvasSessionState


@dataclass
class ProjectContext:
    repository: SeriesRepository
    series: SeriesDocument
    assets: AssetRepository

    @classmethod
    def create(cls, repository: SeriesRepository,
               series: SeriesDocument) -> "ProjectContext":
        return cls(repository, series, AssetRepository(repository.root))


@dataclass
class EditorSession:
    key: str
    kind: Literal["series", "asset"]
    context: ProjectContext
    chapter: ChapterDocument
    tiles: TileStore
    images: ImageStore = field(default_factory=ImageStore)
    asset_manifest: AssetManifest | None = None
    canvas_state: CanvasSessionState | None = None
    dirty: bool = False
    last_autosave: float = 0.0
    expanded_entities: set[str] = field(default_factory=set)
    manual_ribbon_page: str = ""

    @property
    def name(self) -> str:
        return (
            self.asset_manifest.name
            if self.kind == "asset" and self.asset_manifest is not None
            else self.context.series.name
        )

    @property
    def tab_text(self) -> str:
        return f"{self.name}{' *' if self.dirty else ''}"
