"""Portable per-series assets and detached document-fragment placement."""
from __future__ import annotations

import json
import math
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage

from .models import (
    BlenderViewObject, BoundGeometry, ChapterDocument, ChildRef, ColorFillGradientObject,
    DocumentObject, GradientObject, ImageObject, LayerNode, RasterObject, ShapeStyle,
    SpeedLineCenterObject, SpeedLinesGradientObject, TextObject,
    VectorDrawingObject, VectorFillObject, new_id, object_from_dict,
)
from .images import ImageStore
from .persistence import atomic_json
from .tiles import TileStore


ASSET_SCHEMA_VERSION = 2
ASSET_FILE = "asset.json"
LIBRARY_SCHEMA_VERSION = 1
LIBRARY_FILE = "library.json"
THUMBNAIL_FILE = "thumbnail.png"
PENDING_FILE = ".save_pending"
LAST_GOOD_DIR = "last_good"
ASSET_PADDING = 64.0


def _copy_layer(layer: LayerNode) -> LayerNode:
    return LayerNode.from_dict(layer.to_dict())


def _copy_object(obj: DocumentObject) -> DocumentObject:
    return object_from_dict(obj.to_dict())


def _translate_bound(bound: BoundGeometry | None, dx: float, dy: float) -> None:
    if bound is None:
        return
    for contour in bound.iter_contours():
        for node in contour.nodes:
            node.x += dx
            node.y += dy
            if node.incoming is not None:
                node.incoming = (node.incoming[0] + dx, node.incoming[1] + dy)
            if node.outgoing is not None:
                node.outgoing = (node.outgoing[0] + dx, node.outgoing[1] + dy)


def _renew_bound_ids(bound: BoundGeometry | None) -> None:
    if bound is None:
        return
    for contour in bound.iter_contours():
        for node in contour.nodes:
            node.node_id = new_id()


def _translate_object(obj: DocumentObject, dx: float, dy: float,
                      document: ChapterDocument) -> None:
    """Move an object between parent coordinate spaces without changing it."""
    moved_by_quad = False
    if isinstance(obj, TextObject) and obj.transform_quad is not None:
        obj.transform_quad = [(x + dx, y + dy) for x, y in obj.transform_quad]
        moved_by_quad = True
    if isinstance(obj, (RasterObject, VectorDrawingObject, ImageObject)):
        if obj.transform_quad is not None:
            obj.transform_quad = [(x + dx, y + dy) for x, y in obj.transform_quad]
            moved_by_quad = True
        elif obj.transform_frame is not None:
            left, top, width, height = obj.transform_frame
            obj.transform_frame = (left + dx, top + dy, width, height)
    if not moved_by_quad:
        obj.x += dx
        obj.y += dy
    if isinstance(obj, GradientObject):
        _translate_bound(obj.line_field.geometry, dx, dy)
        obj.radial_field.origin_x += dx
        obj.radial_field.origin_y += dy
        if obj.radial_field.manual_center is not None:
            x, y = obj.radial_field.manual_center
            obj.radial_field.manual_center = (x + dx, y + dy)
        if obj.shape_field.manual_center is not None:
            x, y = obj.shape_field.manual_center
            obj.shape_field.manual_center = (x + dx, y + dy)
        if isinstance(obj, SpeedLinesGradientObject):
            center = document.objects.get(obj.center_shape_id)
            if isinstance(center, SpeedLineCenterObject):
                _translate_bound(center.geometry, dx, dy)


def _object_local_bounds(obj: DocumentObject, document: ChapterDocument,
                         tiles: TileStore) -> QRectF:
    if isinstance(obj, BlenderViewObject):
        parent = document.layers.get(obj.parent_layer_id)
        if parent is not None and parent.bound is not None:
            return QRectF(*parent.bound.bbox())
    if isinstance(obj, (RasterObject, VectorDrawingObject, ImageObject)) \
            and obj.transform_quad:
        xs = [point[0] for point in obj.transform_quad]
        ys = [point[1] for point in obj.transform_quad]
        return QRectF(
            min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
        )
    if isinstance(obj, RasterObject):
        content = tiles.content_bounds(obj.object_id)
        if content is None:
            content = QRectF(*obj.interaction_rect)
        return content.translated(obj.x, obj.y)
    if isinstance(obj, ImageObject):
        if obj.transform_quad:
            xs = [point[0] for point in obj.transform_quad]
            ys = [point[1] for point in obj.transform_quad]
            return QRectF(
                min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
            )
        return QRectF(obj.x, obj.y, obj.pixel_width, obj.pixel_height)
    if isinstance(obj, TextObject):
        if obj.layout_mode == "free" and obj.transform_quad:
            xs = [point[0] for point in obj.transform_quad]
            ys = [point[1] for point in obj.transform_quad]
            return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        parent = document.layers.get(obj.parent_layer_id)
        if parent and parent.bound is not None:
            left, top, width, height = parent.bound.bbox()
            return QRectF(left, top, width, height)
        return QRectF(obj.x, obj.y, obj.width, obj.height)
    if isinstance(obj, VectorDrawingObject):
        left, top, width, height = obj.derived_bounds()
        padding = max((point.width for stroke in obj.strokes for point in stroke.points), default=1.0) / 2
        return QRectF(obj.x + left - padding, obj.y + top - padding,
                      max(1.0, width + padding * 2), max(1.0, height + padding * 2))
    if isinstance(obj, VectorFillObject):
        left, top, width, height = obj.derived_bounds()
        owner = document.objects.get(obj.owner_drawing_id)
        ox = owner.x if isinstance(owner, VectorDrawingObject) else 0.0
        oy = owner.y if isinstance(owner, VectorDrawingObject) else 0.0
        return QRectF(ox + left, oy + top, max(1.0, width), max(1.0, height))
    if isinstance(obj, GradientObject):
        parent = document.layers.get(obj.parent_layer_id)
        if parent and parent.bound is not None:
            left, top, width, height = parent.bound.bbox()
            return QRectF(left, top, max(1.0, width), max(1.0, height))
    return QRectF(obj.x, obj.y, 80.0, 80.0)


def entity_visual_bounds(document: ChapterDocument, tiles: TileStore,
                         kind: str, entity_id: str) -> QRectF:
    """Return a conservative world-space bound for an entity subtree."""
    if kind == "object":
        obj = document.objects[entity_id]
        wx, wy = document.layer_world_translation(obj.parent_layer_id)
        result = _object_local_bounds(obj, document, tiles).translated(wx, wy)
        if isinstance(obj, VectorDrawingObject):
            for fill_id in obj.fill_child_ids:
                if fill_id in document.objects:
                    result = result.united(entity_visual_bounds(
                        document, tiles, "object", fill_id
                    ))
        return result

    layer = document.layers[entity_id]
    wx, wy = document.layer_world_translation(entity_id)
    result = QRectF()
    found = False
    if layer.layer_kind == "fill" and layer.parent_id:
        parent = document.layers.get(layer.parent_id)
        if parent is not None and parent.bound is not None:
            px, py = document.layer_world_translation(parent.layer_id)
            left, top, width, height = parent.bound.bbox()
            result = QRectF(px + left, py + top, max(1.0, width), max(1.0, height))
            found = True
    if layer.bound is not None:
        left, top, width, height = layer.bound.bbox()
        padding = layer.shape_style.outline_thickness
        if layer.layer_kind == "open_shape":
            maximum = max(
                (node.width_multiplier for node in layer.bound.nodes),
                default=1.0,
            )
            padding += layer.shape_style.base_thickness * maximum / 2
        result = QRectF(
            wx + left - padding, wy + top - padding,
            max(1.0, width + padding * 2), max(1.0, height + padding * 2),
        )
        found = True
    for child in layer.children:
        child_bounds = entity_visual_bounds(
            document, tiles, child.kind, child.entity_id
        )
        result = child_bounds if not found else result.united(child_bounds)
        found = True
    return result if found else QRectF(wx, wy, 1.0, 1.0)


def _collect_subtree(document: ChapterDocument, kind: str,
                     entity_id: str) -> tuple[set[str], set[str]]:
    layers: set[str] = set()
    objects: set[str] = set()

    def add_object(object_id: str) -> None:
        if object_id in objects:
            return
        objects.add(object_id)
        obj = document.objects[object_id]
        if isinstance(obj, VectorDrawingObject):
            for fill_id in obj.fill_child_ids:
                add_object(fill_id)
        if isinstance(obj, SpeedLinesGradientObject) and obj.center_shape_id:
            add_object(obj.center_shape_id)

    def add_layer(layer_id: str) -> None:
        if layer_id in layers:
            return
        layers.add(layer_id)
        for child in document.layers[layer_id].children:
            add_layer(child.entity_id) if child.kind == "layer" else add_object(child.entity_id)

    add_layer(entity_id) if kind == "layer" else add_object(entity_id)
    return layers, objects


@dataclass
class AssetManifest:
    asset_id: str = field(default_factory=new_id)
    name: str = "Asset"
    root_kind: Literal["layer", "object"] = "layer"
    root_id: str = ""
    document: ChapterDocument = field(default_factory=lambda: ChapterDocument(
        name="Asset", width=512, height=512, background="#00000000",
        document_kind="asset",
    ))
    visual_bounds: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    schema_version: int = ASSET_SCHEMA_VERSION

    def validate(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Asset name cannot be empty")
        if self.root_kind not in {"layer", "object"}:
            raise ValueError("Asset root must be a layer or object")
        values = self.document.layers if self.root_kind == "layer" else self.document.objects
        if self.root_id not in values:
            raise ValueError("Asset root is missing")
        self.document.document_kind = "asset"
        self.document.validate()
        self.visual_bounds = tuple(float(value) for value in self.visual_bounds)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "asset_schema_version": self.schema_version,
            "id": self.asset_id,
            "name": self.name,
            "root": {"kind": self.root_kind, "id": self.root_id},
            "visual_bounds": list(self.visual_bounds),
            "document": self.document.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], warnings: list[str] | None = None,
    ) -> "AssetManifest":
        schema = int(data.get("asset_schema_version", 1))
        if schema > ASSET_SCHEMA_VERSION:
            raise ValueError(f"Unsupported future asset schema: {schema}")
        root = data.get("root") or {}
        result = cls(
            asset_id=str(data["id"]), name=str(data.get("name", "Asset")),
            root_kind=str(root.get("kind", "layer")),
            root_id=str(root.get("id", "")),
            visual_bounds=tuple(data.get("visual_bounds", [0, 0, 1, 1])),
            document=ChapterDocument.from_dict(
                data["document"], warnings=warnings
            ),
            schema_version=schema,
        )
        result.validate()
        result.schema_version = ASSET_SCHEMA_VERSION
        return result


@dataclass
class AssetFolder:
    """A nested folder in the per-series asset library."""

    folder_id: str = field(default_factory=new_id)
    name: str = "Folder"
    parent_id: str | None = None
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.folder_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssetFolder":
        return cls(
            folder_id=str(data.get("id") or new_id()),
            name=str(data.get("name") or "Folder").strip() or "Folder",
            parent_id=(
                str(data["parent_id"]) if data.get("parent_id") else None
            ),
            order=int(data.get("order", 0)),
        )


def extract_asset(
    document: ChapterDocument, tiles: TileStore, kind: str,
    entity_id: str, name: str, *, source_images: ImageStore | None = None,
    include_images: bool = False,
) -> tuple[AssetManifest, TileStore] | tuple[AssetManifest, TileStore, ImageStore]:
    """Copy one entity subtree into a fitted, self-contained asset document."""
    if kind == "object" and isinstance(
        document.objects.get(entity_id),
        (SpeedLinesGradientObject, SpeedLineCenterObject),
    ):
        raise ValueError("Speed Lines are no longer supported")
    if kind == "object" and isinstance(document.objects.get(entity_id), VectorFillObject):
        entity_id = document.objects[entity_id].owner_drawing_id
    if kind not in {"layer", "object"}:
        raise ValueError("Only layers and objects can be copied as assets")
    if kind == "layer" and entity_id not in document.layers:
        raise KeyError(entity_id)
    if kind == "object" and entity_id not in document.objects:
        raise KeyError(entity_id)

    layer_ids, object_ids = _collect_subtree(document, kind, entity_id)
    if any(
        isinstance(document.objects[object_id], BlenderViewObject)
        for object_id in object_ids
    ):
        raise ValueError("Blender frames cannot be stored as reusable assets")
    asset = ChapterDocument(
        name=name.strip() or "Asset", width=512, height=512,
        background="#00000000", document_kind="asset",
    )
    container = LayerNode(
        name="Asset Canvas", is_page=True,
        bound=BoundGeometry.rectangle(0, 0, 512, 512),
        shape_style=ShapeStyle(primary_color=None, outline_thickness=0),
    )
    asset.layers[container.layer_id] = container
    asset.root_page_ids = [container.layer_id]
    for layer_id in layer_ids:
        asset.layers[layer_id] = _copy_layer(document.layers[layer_id])
    for object_id in object_ids:
        asset.objects[object_id] = _copy_object(document.objects[object_id])

    source_parent_world = (0.0, 0.0)
    if kind == "layer":
        root = asset.layers[entity_id]
        source = document.layers[entity_id]
        if source.parent_id:
            source_parent_world = document.layer_world_translation(source.parent_id)
        if root.is_page:
            root.is_page = False
        root.parent_id = container.layer_id
        root.translate_x += source_parent_world[0]
        root.translate_y += source_parent_world[1]
    else:
        root = asset.objects[entity_id]
        source_parent_world = document.layer_world_translation(root.parent_layer_id)
        root.parent_layer_id = container.layer_id
        _translate_object(root, *source_parent_world, asset)
        if isinstance(root, VectorDrawingObject):
            for fill_id in root.fill_child_ids:
                asset.objects[fill_id].parent_layer_id = container.layer_id
        if isinstance(root, SpeedLinesGradientObject) and root.center_shape_id:
            asset.objects[root.center_shape_id].parent_layer_id = container.layer_id
    container.children = [ChildRef(kind, entity_id)]

    asset_tiles = TileStore(tiles.tile_size)
    asset_images = ImageStore()
    for object_id in object_ids:
        if isinstance(asset.objects[object_id], RasterObject):
            asset_tiles.replace_object_tiles(object_id, tiles.object_tiles(object_id))
        if isinstance(asset.objects[object_id], ImageObject) and source_images is not None:
            source_images.copy_source_to(object_id, asset_images, object_id)

    bounds = entity_visual_bounds(asset, asset_tiles, kind, entity_id)
    dx, dy = ASSET_PADDING - bounds.left(), ASSET_PADDING - bounds.top()
    if kind == "layer":
        asset.layers[entity_id].translate_x += dx
        asset.layers[entity_id].translate_y += dy
    else:
        _translate_object(asset.objects[entity_id], dx, dy, asset)
    width = max(256, int(math.ceil(bounds.width() + ASSET_PADDING * 2)))
    height = max(256, int(math.ceil(bounds.height() + ASSET_PADDING * 2)))
    asset.width, asset.height = width, height
    container.bound = BoundGeometry.rectangle(0, 0, width, height)
    fitted = entity_visual_bounds(asset, asset_tiles, kind, entity_id)
    manifest = AssetManifest(
        name=name, root_kind=kind, root_id=entity_id, document=asset,
        visual_bounds=(fitted.x(), fitted.y(), fitted.width(), fitted.height()),
    )
    manifest.validate()
    asset_tiles.dirty.clear()
    asset_images.dirty.clear()
    return (
        (manifest, asset_tiles, asset_images)
        if include_images else (manifest, asset_tiles)
    )


def _renew_internal_ids(layer: LayerNode | None, obj: DocumentObject | None) -> None:
    if layer is not None:
        _renew_bound_ids(layer.bound)
        return
    if obj is None:
        return
    if isinstance(obj, VectorDrawingObject):
        for stroke in obj.strokes:
            stroke.stroke_id = new_id()
            for point in stroke.points:
                point.point_id = new_id()
    if isinstance(obj, VectorFillObject):
        _renew_bound_ids(obj.geometry)
    if isinstance(obj, GradientObject):
        _renew_bound_ids(obj.line_field.geometry)
        for attribute in ("ramp", "color_ramp", "thickness_ramp"):
            ramp = getattr(obj, attribute, None)
            if ramp is not None:
                for stop in ramp.stops:
                    stop.stop_id = new_id()
    if isinstance(obj, SpeedLineCenterObject):
        _renew_bound_ids(obj.geometry)


def instantiate_asset(
    manifest: AssetManifest, source_tiles: TileStore,
    target: ChapterDocument, target_tiles: TileStore,
    parent_id: str, world_x: float, world_y: float, *,
    source_images: ImageStore | None = None,
    target_images: ImageStore | None = None,
) -> tuple[str, str, set[str]]:
    """Instantiate an independent asset copy centered on a world point."""
    root_entity = (
        manifest.document.layers.get(manifest.root_id)
        if manifest.root_kind == "layer"
        else manifest.document.objects.get(manifest.root_id)
    )
    if isinstance(root_entity, (SpeedLinesGradientObject, SpeedLineCenterObject)):
        raise ValueError("Speed Lines are no longer supported")
    parent = target.layers.get(parent_id)
    if parent is None or parent.layer_kind == "fill":
        raise ValueError("Asset destination must be a container layer")
    source = manifest.document
    layer_ids, object_ids = _collect_subtree(source, manifest.root_kind, manifest.root_id)
    layer_map = {old: new_id() for old in layer_ids}
    object_map = {old: new_id() for old in object_ids}

    cloned_layers: dict[str, LayerNode] = {}
    for old_id in layer_ids:
        layer = _copy_layer(source.layers[old_id])
        layer.layer_id = layer_map[old_id]
        layer.parent_id = layer_map.get(layer.parent_id, parent_id)
        layer.children = [ChildRef(
            child.kind,
            layer_map[child.entity_id] if child.kind == "layer" else object_map[child.entity_id],
        ) for child in layer.children]
        if layer.last_raster_id:
            layer.last_raster_id = object_map.get(layer.last_raster_id)
        _renew_internal_ids(layer, None)
        cloned_layers[layer.layer_id] = layer

    cloned_objects: dict[str, DocumentObject] = {}
    for old_id in object_ids:
        obj = _copy_object(source.objects[old_id])
        obj.object_id = object_map[old_id]
        obj.parent_layer_id = layer_map.get(obj.parent_layer_id, parent_id)
        if isinstance(obj, VectorDrawingObject):
            obj.fill_child_ids = [object_map[item] for item in obj.fill_child_ids]
        if isinstance(obj, VectorFillObject):
            obj.owner_drawing_id = object_map[obj.owner_drawing_id]
        if isinstance(obj, SpeedLinesGradientObject) and obj.center_shape_id:
            obj.center_shape_id = object_map[obj.center_shape_id]
        if isinstance(obj, SpeedLineCenterObject):
            obj.owner_gradient_id = object_map[obj.owner_gradient_id]
        _renew_internal_ids(None, obj)
        cloned_objects[obj.object_id] = obj

    root_id = (
        layer_map[manifest.root_id]
        if manifest.root_kind == "layer" else object_map[manifest.root_id]
    )
    # The library name is the user-facing identity of the dropped asset.
    # Preserve child names, but make the cloned root explicitly custom-named
    # so hierarchy display logic cannot replace it with a generic type label.
    if manifest.root_kind == "layer":
        cloned_layers[root_id].name = manifest.name
        cloned_layers[root_id].custom_name = True
    else:
        cloned_objects[root_id].name = manifest.name
        cloned_objects[root_id].custom_name = True
    target.layers.update(cloned_layers)
    target.objects.update(cloned_objects)
    parent.children.insert(0, ChildRef(manifest.root_kind, root_id))

    parent_world = target.layer_world_translation(parent_id)
    bx, by, bw, bh = manifest.visual_bounds
    dx = world_x - (bx + bw / 2 + parent_world[0])
    dy = world_y - (by + bh / 2 + parent_world[1])
    if manifest.root_kind == "layer":
        cloned_layers[root_id].translate_x += dx
        cloned_layers[root_id].translate_y += dy
    else:
        _translate_object(cloned_objects[root_id], dx, dy, target)

    for old_id, new_object_id in object_map.items():
        if isinstance(source.objects[old_id], RasterObject):
            target_tiles.replace_object_tiles(
                new_object_id, source_tiles.object_tiles(old_id)
            )
        if (
            isinstance(source.objects[old_id], ImageObject)
            and source_images is not None and target_images is not None
        ):
            source_images.copy_source_to(old_id, target_images, new_object_id)
    try:
        target.validate()
    except Exception:
        parent.children = [child for child in parent.children if child.entity_id != root_id]
        for layer_id in cloned_layers:
            target.layers.pop(layer_id, None)
        for object_id in cloned_objects:
            target.objects.pop(object_id, None)
            target_tiles.remove_object(object_id)
            if target_images is not None:
                target_images.remove(object_id)
        raise
    return manifest.root_kind, root_id, set(cloned_objects)


class AssetRepository:
    """A portable asset library rooted inside one series folder."""

    def __init__(self, series_root: str | Path):
        self.series_root = Path(series_root).expanduser().resolve()
        self.root = self.series_root / "assets"
        self.last_load_warnings: list[str] = []

    @property
    def library_path(self) -> Path:
        return self.root / LIBRARY_FILE

    def _load_library(self) -> tuple[dict[str, AssetFolder], dict[str, str]]:
        """Read and normalize folder metadata without failing old libraries."""
        if not self.library_path.is_file():
            return {}, {}
        try:
            data = json.loads(self.library_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}, {}
        if not isinstance(data, dict):
            return {}, {}
        folders: dict[str, AssetFolder] = {}
        for raw in data.get("folders", []):
            if not isinstance(raw, dict):
                continue
            folder = AssetFolder.from_dict(raw)
            if folder.folder_id in folders:
                continue
            folders[folder.folder_id] = folder
        # Ignore broken parent links and cycles rather than making a library
        # impossible to open after a partial write or manual edit.
        for folder in folders.values():
            seen: set[str] = set()
            parent = folder.parent_id
            while parent is not None:
                if parent not in folders or parent in seen:
                    folder.parent_id = None
                    break
                seen.add(parent)
                parent = folders[parent].parent_id
        raw_memberships = data.get("memberships") or {}
        if not isinstance(raw_memberships, dict):
            raw_memberships = {}
        memberships = {
            str(asset_id): str(folder_id)
            for asset_id, folder_id in raw_memberships.items()
            if str(folder_id) in folders
        }
        return folders, memberships

    def _save_library(
        self, folders: dict[str, AssetFolder], memberships: dict[str, str],
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.library_path, {
            "library_schema_version": LIBRARY_SCHEMA_VERSION,
            "folders": [
                folder.to_dict()
                for folder in sorted(
                    folders.values(), key=lambda item: (item.order, item.name.casefold(), item.folder_id)
                )
            ],
            "memberships": dict(sorted(memberships.items())),
        })

    def list_folders(self, parent_id: str | None = None) -> list[AssetFolder]:
        folders, _memberships = self._load_library()
        return sorted(
            (folder for folder in folders.values() if folder.parent_id == parent_id),
            key=lambda item: (item.order, item.name.casefold(), item.folder_id),
        )

    def get_folder(self, folder_id: str) -> AssetFolder | None:
        folders, _memberships = self._load_library()
        return folders.get(str(folder_id))

    def folder_for_asset(self, asset_id: str) -> str | None:
        _folders, memberships = self._load_library()
        return memberships.get(str(asset_id))

    def assets_in_folder(
        self, folder_id: str | None = None, *, recursive: bool = False,
    ) -> list[AssetManifest]:
        assets = self.list_assets()
        folders, memberships = self._load_library()
        allowed: set[str | None] = {folder_id}
        if recursive and folder_id is not None:
            pending = [folder_id]
            while pending:
                current = pending.pop()
                children = [
                    folder.folder_id for folder in folders.values()
                    if folder.parent_id == current
                ]
                allowed.update(children)
                pending.extend(children)
        return [
            asset for asset in assets
            if memberships.get(asset.asset_id) in allowed
        ]

    def _ensure_folder_name(
        self, name: str, folders: dict[str, AssetFolder],
        parent_id: str | None, excluding: str = "",
    ) -> str:
        clean = str(name).strip()
        if not clean:
            raise ValueError("Folder name cannot be empty")
        if any(
            folder.folder_id != excluding
            and folder.parent_id == parent_id
            and folder.name.casefold() == clean.casefold()
            for folder in folders.values()
        ):
            raise ValueError(f"A folder named {clean!r} already exists here")
        return clean

    def create_folder(
        self, name: str, parent_id: str | None = None,
    ) -> AssetFolder:
        folders, memberships = self._load_library()
        if parent_id is not None and parent_id not in folders:
            raise KeyError(parent_id)
        clean = self._ensure_folder_name(name, folders, parent_id)
        order = max(
            (folder.order for folder in folders.values()
             if folder.parent_id == parent_id), default=-1
        ) + 1
        folder = AssetFolder(name=clean, parent_id=parent_id, order=order)
        folders[folder.folder_id] = folder
        self._save_library(folders, memberships)
        return folder

    def rename_folder(self, folder_id: str, name: str) -> AssetFolder:
        folders, memberships = self._load_library()
        folder = folders.get(str(folder_id))
        if folder is None:
            raise FileNotFoundError(f"Folder {folder_id!r} does not exist")
        folder.name = self._ensure_folder_name(
            name, folders, folder.parent_id, folder.folder_id
        )
        self._save_library(folders, memberships)
        return folder

    def move_asset(self, asset_id: str, folder_id: str | None) -> None:
        if not any(asset.asset_id == str(asset_id) for asset in self.list_assets()):
            raise FileNotFoundError(f"Asset {asset_id!r} does not exist")
        folders, memberships = self._load_library()
        if folder_id is not None and str(folder_id) not in folders:
            raise KeyError(folder_id)
        if folder_id is None:
            memberships.pop(str(asset_id), None)
        else:
            memberships[str(asset_id)] = str(folder_id)
        self._save_library(folders, memberships)

    def delete_folder(self, folder_id: str, *, recursive: bool = True) -> list[str]:
        folders, memberships = self._load_library()
        folder_id = str(folder_id)
        if folder_id not in folders:
            raise FileNotFoundError(f"Folder {folder_id!r} does not exist")
        if not recursive and any(
            folder.parent_id == folder_id for folder in folders.values()
        ):
            raise ValueError("Folder is not empty")
        doomed = {folder_id}
        pending = [folder_id]
        while pending:
            current = pending.pop()
            children = {
                folder.folder_id for folder in folders.values()
                if folder.parent_id == current
            }
            doomed.update(children)
            pending.extend(children)
        doomed_assets = [
            asset_id for asset_id, parent in memberships.items()
            if parent in doomed
        ]
        for asset_id in doomed_assets:
            root = self.asset_root(asset_id).resolve()
            if root.parent != self.root.resolve():
                raise OSError("Invalid asset path")
            if root.exists():
                shutil.rmtree(root)
        for asset_id in doomed_assets:
            memberships.pop(asset_id, None)
        for item in doomed:
            folders.pop(item, None)
        self._save_library(folders, memberships)
        return doomed_assets

    def delete(self, asset_id: str) -> None:
        asset_id = str(asset_id)
        if not any(asset.asset_id == asset_id for asset in self.list_assets()):
            raise FileNotFoundError(f"Asset {asset_id!r} does not exist")
        root = self.asset_root(asset_id).resolve()
        if root.parent != self.root.resolve():
            raise OSError("Invalid asset path")
        if root.exists():
            shutil.rmtree(root)
        folders, memberships = self._load_library()
        memberships.pop(asset_id, None)
        self._save_library(folders, memberships)

    def asset_root(self, asset_id: str) -> Path:
        return self.root / asset_id

    def thumbnail_path(self, asset_id: str) -> Path:
        return self.asset_root(asset_id) / THUMBNAIL_FILE

    def list_assets(self) -> list[AssetManifest]:
        self.last_load_warnings = []
        if not self.root.is_dir():
            return []
        result: list[AssetManifest] = []
        for path in self.root.glob(f"*/{ASSET_FILE}"):
            local_warnings: list[str] = []
            try:
                result.append(AssetManifest.from_dict(
                    json.loads(path.read_text(encoding="utf-8")),
                    warnings=local_warnings,
                ))
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                if local_warnings:
                    self.last_load_warnings.extend(
                        [f"Asset {path.parent.name}: {message}" for message in local_warnings]
                    )
                continue
            self.last_load_warnings.extend(
                [f"Asset {path.parent.name}: {message}" for message in local_warnings]
            )
        return sorted(result, key=lambda asset: (asset.name.casefold(), asset.asset_id))

    def find_by_name(self, name: str) -> AssetManifest | None:
        """Return the asset with a trimmed, case-insensitive name match."""
        folded = name.strip().casefold()
        if not folded:
            return None
        return next(
            (asset for asset in self.list_assets() if asset.name.casefold() == folded),
            None,
        )

    def _ensure_unique_name(self, name: str, excluding: str = "") -> str:
        name = name.strip()
        if not name:
            raise ValueError("Asset name cannot be empty")
        if any(
            asset.asset_id != excluding and asset.name.casefold() == name.casefold()
            for asset in self.list_assets()
        ):
            raise ValueError(f"An asset named {name!r} already exists")
        return name

    def create(
        self, manifest: AssetManifest, tiles: TileStore, thumbnail: QImage,
        images: ImageStore | None = None, *, folder_id: str | None = None,
    ) -> AssetManifest:
        manifest.name = self._ensure_unique_name(manifest.name)
        if (self.asset_root(manifest.asset_id) / ASSET_FILE).exists():
            raise FileExistsError("Asset already exists")
        if folder_id is not None and self.get_folder(folder_id) is None:
            raise KeyError(folder_id)
        self.save(manifest, tiles, thumbnail, images=images)
        if folder_id is not None:
            self.move_asset(manifest.asset_id, folder_id)
        return manifest

    def replace(
        self, asset_id: str, manifest: AssetManifest, tiles: TileStore,
        thumbnail: QImage, images: ImageStore | None = None,
    ) -> AssetManifest:
        """Replace an asset's contents while preserving its stable identity."""
        existing = next(
            (asset for asset in self.list_assets() if asset.asset_id == asset_id),
            None,
        )
        if existing is None:
            raise FileNotFoundError(f"Asset {asset_id!r} does not exist")
        manifest.asset_id = existing.asset_id
        manifest.name = existing.name
        self.save(manifest, tiles, thumbnail, images=images)
        return manifest

    def load(
        self, asset_id: str, recover: bool = False, *,
        include_images: bool = False,
    ) -> tuple[AssetManifest, TileStore] | tuple[
        AssetManifest, TileStore, ImageStore
    ]:
        root = self.asset_root(asset_id)
        if not recover:
            self._recover_interrupted_save(root)
        source = root / "autosave" if recover else root
        self.last_load_warnings = []
        manifest = AssetManifest.from_dict(json.loads(
            (source / ASSET_FILE).read_text(encoding="utf-8")
        ), warnings=self.last_load_warnings)
        tiles = TileStore()
        raster_ids = {
            object_id for object_id, obj in manifest.document.objects.items()
            if isinstance(obj, RasterObject)
        }
        tiles.load_directory(source / "raster", raster_ids)
        images = ImageStore()
        images.load_directory(source / "images", {
            object_id: (obj.source_filename, obj.source_mime_type)
            for object_id, obj in manifest.document.objects.items()
            if isinstance(obj, ImageObject)
        })
        return (manifest, tiles, images) if include_images else (manifest, tiles)

    def save(
        self, manifest: AssetManifest, tiles: TileStore,
        thumbnail: QImage | None = None, images: ImageStore | None = None,
        autosave: bool = False,
    ) -> None:
        images = images or ImageStore()
        manifest.name = self._ensure_unique_name(manifest.name, manifest.asset_id)
        manifest.validate()
        raster_ids = {
            object_id for object_id, obj in manifest.document.objects.items()
            if isinstance(obj, RasterObject)
        }
        image_ids = {
            object_id for object_id, obj in manifest.document.objects.items()
            if isinstance(obj, ImageObject)
        }
        root = self.asset_root(manifest.asset_id)
        if autosave:
            destination = root / "autosave"
            tiles.save_directory(destination / "raster", raster_ids, complete=True)
            images.save_directory(destination / "images", image_ids, complete=True)
            atomic_json(destination / ASSET_FILE, manifest.to_dict())
            atomic_json(destination / "recovery.json", {"saved_at": time.time()})
            return
        root.mkdir(parents=True, exist_ok=True)
        manifest_path = root / ASSET_FILE
        backup = root / LAST_GOOD_DIR
        if manifest_path.is_file():
            if backup.exists():
                shutil.rmtree(backup)
            backup.mkdir(parents=True)
            shutil.copy2(manifest_path, backup / ASSET_FILE)
            if (root / "raster").is_dir():
                shutil.copytree(root / "raster", backup / "raster")
            if (root / "images").is_dir():
                shutil.copytree(root / "images", backup / "images")
            if (root / THUMBNAIL_FILE).is_file():
                shutil.copy2(root / THUMBNAIL_FILE, backup / THUMBNAIL_FILE)
        atomic_json(root / PENDING_FILE, {"started_at": time.time()})
        try:
            tiles.save_directory(root / "raster", raster_ids, complete=True)
            images.save_directory(root / "images", image_ids, complete=True)
            if thumbnail is not None:
                temporary = root / f".{THUMBNAIL_FILE}.tmp"
                if not thumbnail.save(str(temporary), "PNG"):
                    raise OSError("Unable to save asset thumbnail")
                temporary.replace(root / THUMBNAIL_FILE)
            atomic_json(manifest_path, manifest.to_dict())
            (root / PENDING_FILE).unlink(missing_ok=True)
            tiles.dirty.clear()
            images.dirty.clear()
        except Exception:
            raise
        autosave_root = root / "autosave"
        if autosave_root.exists():
            shutil.rmtree(autosave_root)

    def rename(self, asset_id: str, name: str) -> AssetManifest:
        name = self._ensure_unique_name(name, asset_id)
        manifest, _tiles = self.load(asset_id)
        manifest.name = name
        atomic_json(self.asset_root(asset_id) / ASSET_FILE, manifest.to_dict())
        return manifest

    def has_recovery(self, asset_id: str) -> bool:
        root = self.asset_root(asset_id)
        manual = root / ASSET_FILE
        recovery = root / "autosave" / ASSET_FILE
        return recovery.is_file() and (
            not manual.is_file() or recovery.stat().st_mtime > manual.stat().st_mtime
        )

    @staticmethod
    def _recover_interrupted_save(root: Path) -> None:
        pending = root / PENDING_FILE
        if not pending.exists():
            return
        backup = root / LAST_GOOD_DIR
        if not (backup / ASSET_FILE).is_file():
            raise OSError("The first asset save was interrupted and has no recoverable revision")
        for name in ("raster", "images"):
            target = root / name
            if target.exists():
                shutil.rmtree(target)
            if (backup / name).is_dir():
                shutil.copytree(backup / name, target)
        for name in (ASSET_FILE, THUMBNAIL_FILE):
            if (backup / name).is_file():
                shutil.copy2(backup / name, root / name)
        pending.unlink(missing_ok=True)
