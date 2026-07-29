"""Versioned, UI-independent series/chapter document model."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Literal


SCHEMA_VERSION = 4
CHAPTER_WIDTH = 1080
DEFAULT_CHAPTER_HEIGHT = 3240
GROWTH_MARGIN = 1080


def new_id() -> str:
    return uuid.uuid4().hex


def _point(value: Iterable[float]) -> tuple[float, float]:
    x, y = value
    return float(x), float(y)


@dataclass
class GridSettings:
    enabled: bool = True
    size: int = 120
    divisions: int = 4
    origin_x: float = 0.0
    origin_y: float = 0.0
    color: str = "#5d7d9c"
    opacity: float = 0.25

    def validate(self) -> None:
        self.size = max(8, min(CHAPTER_WIDTH, int(self.size)))
        self.divisions = max(1, min(16, int(self.divisions)))
        self.opacity = max(0.0, min(1.0, float(self.opacity)))

    def snap(self, x: float, y: float) -> tuple[float, float]:
        step = self.size / self.divisions
        return (
            self.origin_x + round((x - self.origin_x) / step) * step,
            self.origin_y + round((y - self.origin_y) / step) * step,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "size": self.size, "divisions": self.divisions,
            "origin": [self.origin_x, self.origin_y], "color": self.color,
            "opacity": self.opacity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GridSettings":
        data = data or {}
        origin = data.get("origin", [0, 0])
        result = cls(
            enabled=bool(data.get("enabled", True)),
            size=int(data.get("size", 120)),
            divisions=int(data.get("divisions", 4)),
            origin_x=float(origin[0]), origin_y=float(origin[1]),
            color=str(data.get("color", "#5d7d9c")),
            opacity=float(data.get("opacity", 0.25)),
        )
        result.validate()
        return result


@dataclass
class PathNode:
    node_id: str = field(default_factory=new_id)
    x: float = 0.0
    y: float = 0.0
    point_type: Literal["vector", "bezier"] = "vector"
    incoming: tuple[float, float] | None = None
    outgoing: tuple[float, float] | None = None
    handles_locked: bool = True
    roundness: float = 0.0
    width_multiplier: float = 1.0

    @property
    def position(self) -> tuple[float, float]:
        return self.x, self.y

    @position.setter
    def position(self, value: Iterable[float]) -> None:
        self.x, self.y = _point(value)

    def validate(self) -> None:
        self.x, self.y = float(self.x), float(self.y)
        if self.point_type not in {"vector", "bezier"}:
            raise ValueError("Unknown shape point type")
        if self.point_type == "vector":
            self.incoming = self.outgoing = None
            self.handles_locked = True
        else:
            self.incoming = _point(self.incoming) if self.incoming else None
            self.outgoing = _point(self.outgoing) if self.outgoing else None
        self.roundness = max(0.0, float(self.roundness))
        self.width_multiplier = round(max(
            0.1, min(10.0, float(self.width_multiplier))
        ) * 10) / 10

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.node_id, "position": [self.x, self.y],
            "point_type": self.point_type,
            "incoming": list(self.incoming) if self.incoming else None,
            "outgoing": list(self.outgoing) if self.outgoing else None,
            "handles_locked": self.handles_locked,
            "roundness": self.roundness,
            "width_multiplier": self.width_multiplier,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PathNode":
        position = data.get("position", [0, 0])
        result = cls(
            node_id=str(data.get("id") or new_id()),
            x=float(position[0]), y=float(position[1]),
            point_type=str(data.get("point_type", "vector")),
            incoming=(
                _point(data["incoming"]) if data.get("incoming") is not None
                else None
            ),
            outgoing=(
                _point(data["outgoing"]) if data.get("outgoing") is not None
                else None
            ),
            handles_locked=bool(data.get("handles_locked", True)),
            roundness=float(data.get("roundness", 0.0)),
            width_multiplier=float(data.get("width_multiplier", 1.0)),
        )
        result.validate()
        return result


@dataclass
class ShapeStyle:
    primary_color: str | None = None
    base_thickness: float = 8.0
    outline_color: str = "#111111"
    outline_thickness: float = 0.0
    start_cap: Literal["point", "square", "round"] = "round"
    end_cap: Literal["point", "square", "round"] = "round"

    def validate(self) -> None:
        self.base_thickness = max(0.1, min(1000.0, float(self.base_thickness)))
        self.outline_thickness = max(
            0.0, min(500.0, float(self.outline_thickness))
        )
        if self.start_cap not in {"point", "square", "round"}:
            self.start_cap = "round"
        if self.end_cap not in {"point", "square", "round"}:
            self.end_cap = "round"

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "primary_color": self.primary_color,
            "base_thickness": self.base_thickness,
            "outline_color": self.outline_color,
            "outline_thickness": self.outline_thickness,
            "start_cap": self.start_cap,
            "end_cap": self.end_cap,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ShapeStyle":
        data = data or {}
        result = cls(
            primary_color=data.get("primary_color"),
            base_thickness=float(data.get("base_thickness", 8.0)),
            outline_color=str(data.get("outline_color", "#111111")),
            outline_thickness=float(data.get("outline_thickness", 0.0)),
            start_cap=str(data.get("start_cap", "round")),
            end_cap=str(data.get("end_cap", "round")),
        )
        result.validate()
        return result


@dataclass(init=False)
class BoundGeometry:
    """Unified closed/open vector path in layer-local coordinates."""

    nodes: list[PathNode]
    closed: bool
    primitive: Literal["rectangle", "ellipse", "custom"]

    def __init__(
        self, kind: str = "rect",
        points: Iterable[Iterable[float]] | None = None,
        *,
        nodes: list[PathNode] | None = None,
        closed: bool | None = None,
        primitive: str | None = None,
    ):
        supplied = list(points or [])
        if nodes is not None:
            self.nodes = nodes
        elif kind == "circle" and len(supplied) >= 2:
            center, radius_point = _point(supplied[0]), _point(supplied[1])
            radius = math.dist(center, radius_point)
            self.nodes = self._ellipse_nodes(
                center[0] - radius, center[1] - radius, radius * 2, radius * 2
            )
        else:
            if not supplied:
                supplied = [(0.0, 0.0), (1080.0, 1080.0)]
            if kind == "rect" and len(supplied) == 2:
                left, top = _point(supplied[0])
                right, bottom = _point(supplied[1])
                supplied = [
                    (left, top), (right, top), (right, bottom), (left, bottom)
                ]
            self.nodes = [
                PathNode(x=float(point[0]), y=float(point[1]))
                for point in supplied
            ]
        self.closed = (
            bool(closed) if closed is not None else kind not in {"line", "open"}
        )
        self.primitive = str(primitive or {
            "rect": "rectangle", "circle": "ellipse",
        }.get(kind, "custom"))
        self.validate()

    @staticmethod
    def _ellipse_nodes(
        x: float, y: float, width: float, height: float,
    ) -> list[PathNode]:
        cx, cy = x + width / 2, y + height / 2
        kx, ky = width * 0.2761423749, height * 0.2761423749
        return [
            PathNode(
                x=cx, y=y, point_type="bezier",
                incoming=(cx - kx, y), outgoing=(cx + kx, y),
            ),
            PathNode(
                x=x + width, y=cy, point_type="bezier",
                incoming=(x + width, cy - ky), outgoing=(x + width, cy + ky),
            ),
            PathNode(
                x=cx, y=y + height, point_type="bezier",
                incoming=(cx + kx, y + height), outgoing=(cx - kx, y + height),
            ),
            PathNode(
                x=x, y=cy, point_type="bezier",
                incoming=(x, cy + ky), outgoing=(x, cy - ky),
            ),
        ]

    @classmethod
    def rectangle(
        cls, x: float, y: float, width: float, height: float,
    ) -> "BoundGeometry":
        return cls(
            nodes=[
                PathNode(x=x, y=y), PathNode(x=x + width, y=y),
                PathNode(x=x + width, y=y + height),
                PathNode(x=x, y=y + height),
            ],
            closed=True, primitive="rectangle",
        )

    @classmethod
    def circle(
        cls, cx: float, cy: float, radius: float,
    ) -> "BoundGeometry":
        radius = abs(radius)
        return cls(
            nodes=cls._ellipse_nodes(
                cx - radius, cy - radius, radius * 2, radius * 2
            ),
            closed=True, primitive="ellipse",
        )

    @classmethod
    def polygon(
        cls, points: Iterable[Iterable[float]],
    ) -> "BoundGeometry":
        return cls(
            nodes=[
                PathNode(x=float(point[0]), y=float(point[1]))
                for point in points
            ],
            closed=True, primitive="custom",
        )

    @classmethod
    def path(
        cls, nodes: list[PathNode], closed: bool = False,
    ) -> "BoundGeometry":
        return cls(nodes=nodes, closed=closed, primitive="custom")

    @property
    def kind(self) -> str:
        if self.primitive == "rectangle":
            return "rect"
        if self.primitive == "ellipse":
            return "circle"
        return "polygon" if self.closed else "path"

    @property
    def points(self) -> list[tuple[float, float]]:
        return [node.position for node in self.nodes]

    @points.setter
    def points(self, values: Iterable[Iterable[float]]) -> None:
        values = [_point(value) for value in values]
        if len(values) == len(self.nodes):
            for node, value in zip(self.nodes, values):
                dx, dy = value[0] - node.x, value[1] - node.y
                node.position = value
                if node.incoming:
                    node.incoming = (
                        node.incoming[0] + dx, node.incoming[1] + dy
                    )
                if node.outgoing:
                    node.outgoing = (
                        node.outgoing[0] + dx, node.outgoing[1] + dy
                    )
        else:
            self.nodes = [PathNode(x=x, y=y) for x, y in values]
            self.primitive = "custom"

    def validate(self) -> None:
        if self.primitive not in {"rectangle", "ellipse", "custom"}:
            raise ValueError("Unknown shape primitive")
        minimum = 3 if self.closed else 2
        if len(self.nodes) < minimum:
            raise ValueError(
                f"{'Closed shapes' if self.closed else 'Open paths'} "
                f"require at least {minimum} points"
            )
        ids: set[str] = set()
        for index, node in enumerate(self.nodes):
            node.validate()
            if node.node_id in ids:
                raise ValueError("Duplicate shape point ID")
            ids.add(node.node_id)
            if node.point_type == "bezier":
                needs_incoming = self.closed or index > 0
                needs_outgoing = self.closed or index < len(self.nodes) - 1
                if needs_incoming != (node.incoming is not None):
                    raise ValueError("Malformed incoming Bézier handle")
                if needs_outgoing != (node.outgoing is not None):
                    raise ValueError("Malformed outgoing Bézier handle")
                if (
                    node.handles_locked
                    and node.incoming is not None and node.outgoing is not None
                    and math.dist(
                        node.incoming,
                        (
                            node.x * 2 - node.outgoing[0],
                            node.y * 2 - node.outgoing[1],
                        ),
                    ) > 1e-4
                ):
                    raise ValueError("Locked Bézier handles must be symmetric")
        if self.primitive in {"rectangle", "ellipse"} and len(self.nodes) != 4:
            raise ValueError("Primitive shapes require four points")

    def bbox(self) -> tuple[float, float, float, float]:
        values = [
            point
            for node in self.nodes
            for point in (node.position, node.incoming, node.outgoing)
            if point is not None
        ]
        xs, ys = [point[0] for point in values], [point[1] for point in values]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        return left, top, right - left, bottom - top

    @property
    def radius(self) -> float:
        _, _, width, height = self.bbox()
        return min(width, height) / 2

    def contains(self, x: float, y: float) -> bool:
        if not self.closed:
            return False
        # Exact curve containment belongs to the renderer. This polygon
        # fallback keeps the UI-independent model useful for validation.
        inside = False
        previous = self.nodes[-1].position
        for node in self.nodes:
            current = node.position
            x1, y1 = previous
            x2, y2 = current
            crosses = (y1 > y) != (y2 > y)
            if crosses and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
            previous = current
        return inside

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "type": "path", "closed": self.closed,
            "primitive": self.primitive,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BoundGeometry":
        if data.get("type") == "path" or data.get("nodes") is not None:
            return cls(
                nodes=[
                    PathNode.from_dict(item) for item in data.get("nodes", [])
                ],
                closed=bool(data.get("closed", True)),
                primitive=str(data.get("primitive", "custom")),
            )
        kind = str(data.get("type", "rect"))
        return cls(kind, [_point(point) for point in data.get("points", [])])


@dataclass
class ChildRef:
    kind: Literal["layer", "object"]
    entity_id: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.entity_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChildRef":
        return cls(str(data["kind"]), str(data["id"]))


@dataclass
class LayerNode:
    layer_id: str = field(default_factory=new_id)
    name: str = "Layer"
    is_page: bool = False
    layer_kind: Literal["bounded", "open_shape", "fill"] = "bounded"
    parent_id: str | None = None
    children: list[ChildRef] = field(default_factory=list)
    visible: bool = True
    opacity: float = 1.0
    translate_x: float = 0.0
    translate_y: float = 0.0
    bound: BoundGeometry | None = field(default_factory=BoundGeometry)
    shape_style: ShapeStyle = field(default_factory=ShapeStyle)
    grid_override: GridSettings | None = None
    last_raster_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.layer_id, "name": self.name, "is_page": self.is_page,
            "layer_kind": self.layer_kind,
            "parent_id": self.parent_id,
            "children": [item.to_dict() for item in self.children],
            "visible": self.visible, "opacity": self.opacity,
            "translation": [self.translate_x, self.translate_y],
            "bound": self.bound.to_dict() if self.bound is not None else None,
            "shape_style": self.shape_style.to_dict(),
            "grid_override": self.grid_override.to_dict() if self.grid_override else None,
            "last_raster_id": self.last_raster_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LayerNode":
        translation = data.get("translation", [0, 0])
        result = cls(
            layer_id=str(data["id"]), name=str(data.get("name", "Layer")),
            is_page=bool(data.get("is_page", False)), parent_id=data.get("parent_id"),
            layer_kind=str(data.get("layer_kind", "bounded")),
            children=[ChildRef.from_dict(item) for item in data.get("children", [])],
            visible=bool(data.get("visible", True)),
            opacity=float(data.get("opacity", 1.0)),
            translate_x=float(translation[0]), translate_y=float(translation[1]),
            bound=(
                BoundGeometry.from_dict(data["bound"])
                if data.get("bound") is not None else None
            ),
            shape_style=(
                ShapeStyle.from_dict(data.get("shape_style"))
                if data.get("shape_style") is not None
                else ShapeStyle(
                    primary_color=data.get("fill_color"),
                    outline_color=str(data.get("border_color", "#111111")),
                    outline_thickness=float(data.get("border_width", 0.0)),
                )
            ),
            grid_override=GridSettings.from_dict(data["grid_override"])
            if data.get("grid_override") else None,
            last_raster_id=data.get("last_raster_id"),
        )
        legacy_radius = float(data.get("vertex_radius", 0.0))
        if legacy_radius and result.bound and result.bound.primitive != "ellipse":
            result.vertex_radius = legacy_radius
        return result

    @property
    def fill_color(self) -> str | None:
        return self.shape_style.primary_color

    @fill_color.setter
    def fill_color(self, value: str | None) -> None:
        self.shape_style.primary_color = value

    @property
    def border_width(self) -> float:
        return self.shape_style.outline_thickness

    @border_width.setter
    def border_width(self, value: float) -> None:
        self.shape_style.outline_thickness = float(value)

    @property
    def border_color(self) -> str:
        return self.shape_style.outline_color

    @border_color.setter
    def border_color(self, value: str) -> None:
        self.shape_style.outline_color = value

    @property
    def vertex_radius(self) -> float:
        if not self.bound:
            return 0.0
        values = [
            node.roundness for node in self.bound.nodes
            if node.point_type == "vector"
        ]
        return max(values, default=0.0)

    @vertex_radius.setter
    def vertex_radius(self, value: float) -> None:
        if self.bound:
            for node in self.bound.nodes:
                if node.point_type == "vector":
                    node.roundness = max(0.0, float(value))


@dataclass
class DocumentObject:
    object_id: str = field(default_factory=new_id)
    object_type: str = "object"
    name: str = "Object"
    parent_layer_id: str = ""
    x: float = 0.0
    y: float = 0.0
    visible: bool = True
    opacity: float = 1.0
    opacity_locked: bool = True

    def common_dict(self) -> dict[str, Any]:
        return {
            "id": self.object_id, "type": self.object_type, "name": self.name,
            "parent_layer_id": self.parent_layer_id, "position": [self.x, self.y],
            "visible": self.visible, "opacity": self.opacity,
            "opacity_locked": self.opacity_locked,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.common_dict()


@dataclass
class RasterObject(DocumentObject):
    object_type: str = "raster"
    name: str = "Raster"
    tile_size: int = 256
    interaction_rect: tuple[float, float, float, float] = (0.0, 0.0, 120.0, 120.0)

    def to_dict(self) -> dict[str, Any]:
        result = self.common_dict()
        result["tile_size"] = self.tile_size
        result["interaction_rect"] = list(self.interaction_rect)
        return result


@dataclass
class TextObject(DocumentObject):
    object_type: str = "text"
    name: str = "Text"
    text: str = "Text"
    width: float = 360.0
    height: float = 120.0
    font_family: str = "Segoe UI"
    font_size: float = 32.0
    bold: bool = False
    italic: bool = False
    kerning: float = 0.0
    layout_mode: Literal["free", "strict"] = "strict"
    horizontal_alignment: Literal["left", "center", "right"] = "center"
    vertical_alignment: Literal["top", "middle", "bottom"] = "middle"
    margin: float = 24.0
    transform_quad: list[tuple[float, float]] | None = None
    legacy_alignment: str | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        result = self.common_dict()
        result.update({
            "text": self.text, "size": [self.width, self.height],
            "font_family": self.font_family, "font_size": self.font_size,
            "bold": self.bold, "italic": self.italic, "kerning": self.kerning,
            "layout_mode": self.layout_mode,
            "horizontal_alignment": self.horizontal_alignment,
            "vertical_alignment": self.vertical_alignment,
            "margin": self.margin,
            "transform_quad": (
                [[x, y] for x, y in self.transform_quad]
                if self.transform_quad is not None else None
            ),
        })
        return result


ObjectEntity = RasterObject | TextObject | DocumentObject


def object_from_dict(data: dict[str, Any]) -> ObjectEntity:
    position = data.get("position", [0, 0])
    common = dict(
        object_id=str(data["id"]), name=str(data.get("name", "Object")),
        parent_layer_id=str(data["parent_layer_id"]), x=float(position[0]),
        y=float(position[1]), visible=bool(data.get("visible", True)),
        opacity=float(data.get("opacity", 1.0)),
        opacity_locked=bool(data.get("opacity_locked", True)),
    )
    object_type = str(data.get("type", "object"))
    if object_type == "raster":
        raw_rect = data.get("interaction_rect", [0, 0, 120, 120])
        return RasterObject(
            **common, tile_size=int(data.get("tile_size", 256)),
            interaction_rect=(
                float(raw_rect[0]), float(raw_rect[1]),
                max(1.0, float(raw_rect[2])), max(1.0, float(raw_rect[3])),
            ),
        )
    if object_type == "text":
        size = data.get("size", [360, 120])
        legacy_alignment = data.get("alignment_mode")
        transform_quad = data.get("transform_quad")
        return TextObject(
            **common, text=str(data.get("text", "Text")),
            width=float(size[0]), height=float(size[1]),
            font_family=str(data.get("font_family", "Segoe UI")),
            font_size=float(data.get("font_size", 32)),
            bold=bool(data.get("bold", False)), italic=bool(data.get("italic", False)),
            kerning=float(data.get("kerning", 0)),
            layout_mode=str(data.get(
                "layout_mode", "free" if legacy_alignment is not None else "strict"
            )),
            horizontal_alignment=str(data.get("horizontal_alignment", "center")),
            vertical_alignment=str(data.get("vertical_alignment", "middle")),
            margin=float(data.get("margin", 24.0)),
            transform_quad=(
                [_point(point) for point in transform_quad]
                if transform_quad is not None else None
            ),
            legacy_alignment=str(legacy_alignment) if legacy_alignment is not None else None,
        )
    return DocumentObject(**common, object_type=object_type)


@dataclass
class ChapterDocument:
    chapter_id: str = field(default_factory=new_id)
    name: str = "Chapter 1"
    width: int = CHAPTER_WIDTH
    height: int = DEFAULT_CHAPTER_HEIGHT
    background: str = "#ffffff"
    grid: GridSettings = field(default_factory=GridSettings)
    root_page_ids: list[str] = field(default_factory=list)
    layers: dict[str, LayerNode] = field(default_factory=dict)
    objects: dict[str, ObjectEntity] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version > SCHEMA_VERSION:
            raise ValueError(
                f"Chapter schema {self.schema_version} is newer than supported {SCHEMA_VERSION}"
            )
        if self.width != CHAPTER_WIDTH:
            raise ValueError(f"Chapter width must be {CHAPTER_WIDTH}")
        self.height = max(1, int(self.height))
        self.grid.validate()
        if len(set(self.root_page_ids)) != len(self.root_page_ids):
            raise ValueError("Duplicate root page")
        referenced: set[tuple[str, str]] = set()
        for page_id in self.root_page_ids:
            page = self.layers.get(page_id)
            if page is None or not page.is_page or page.parent_id is not None:
                raise ValueError("Root entries must be parentless page layers")
            referenced.add(("layer", page_id))
        for layer in self.layers.values():
            if layer.layer_kind not in {"bounded", "open_shape", "fill"}:
                raise ValueError("Unknown layer kind")
            if layer.layer_kind == "fill":
                if layer.is_page or layer.parent_id is None:
                    raise ValueError("Fill layers require a parent")
                if layer.bound is not None or layer.children:
                    raise ValueError("Fill layers are boundless leaves")
                if not layer.fill_color:
                    raise ValueError("Fill layers require a fill color")
                layer.grid_override = None
                layer.border_width = 0.0
                layer.vertex_radius = 0.0
            elif layer.bound is None:
                raise ValueError("Bounded layers require geometry")
            else:
                layer.bound.validate()
                if layer.layer_kind == "open_shape":
                    if layer.is_page or layer.bound.closed or layer.children:
                        raise ValueError(
                            "Open shape layers are childless open paths"
                        )
                elif not layer.bound.closed:
                    raise ValueError("Bounded layers require closed geometry")
            layer.shape_style.validate()
            layer.border_width = max(0.0, float(layer.border_width))
            layer.vertex_radius = max(0.0, float(layer.vertex_radius))
            layer.opacity = max(0.0, min(1.0, float(layer.opacity)))
            if layer.is_page and layer.layer_id not in self.root_page_ids:
                raise ValueError("Page layers must be chapter roots")
            if not layer.is_page and layer.parent_id not in self.layers:
                raise ValueError(f"Layer {layer.layer_id} has no valid parent")
            if (
                layer.parent_id
                and self.layers[layer.parent_id].layer_kind in {
                    "fill", "open_shape"
                }
            ):
                raise ValueError("Leaf layers cannot contain entities")
            if layer.parent_id and self.layers[layer.parent_id].is_page is False:
                pass
            for child in layer.children:
                key = (child.kind, child.entity_id)
                if key in referenced:
                    raise ValueError(f"Entity appears more than once: {child.entity_id}")
                referenced.add(key)
                if child.kind == "layer":
                    candidate = self.layers.get(child.entity_id)
                    if candidate is None or candidate.is_page or candidate.parent_id != layer.layer_id:
                        raise ValueError("Invalid child layer")
                elif child.kind == "object":
                    candidate_object = self.objects.get(child.entity_id)
                    if candidate_object is None or candidate_object.parent_layer_id != layer.layer_id:
                        raise ValueError("Invalid child object")
                    if layer.is_page:
                        raise ValueError("Objects cannot belong directly to page layers")
                else:
                    raise ValueError(f"Unknown child kind: {child.kind}")
        for object_id, obj in self.objects.items():
            parent = self.layers.get(obj.parent_layer_id)
            if (
                parent is None or parent.is_page
                or parent.layer_kind in {"fill", "open_shape"}
            ):
                raise ValueError(f"Object {object_id} requires a non-page parent layer")
            obj.opacity = max(0.0, min(1.0, float(obj.opacity)))
            if isinstance(obj, RasterObject):
                left, top, width, height = obj.interaction_rect
                obj.interaction_rect = (
                    float(left), float(top), max(1.0, float(width)),
                    max(1.0, float(height)),
                )
            if isinstance(obj, TextObject):
                if obj.layout_mode not in {"free", "strict"}:
                    raise ValueError("Unknown text layout mode")
                if obj.horizontal_alignment not in {"left", "center", "right"}:
                    raise ValueError("Unknown horizontal text alignment")
                if obj.vertical_alignment not in {"top", "middle", "bottom"}:
                    raise ValueError("Unknown vertical text alignment")
                if obj.transform_quad is not None and len(obj.transform_quad) != 4:
                    raise ValueError("Text transform quad must have four points")
        expected = {("layer", key) for key in self.layers} | {
            ("object", key) for key in self.objects
        }
        if referenced != expected:
            raise ValueError("Document contains unreachable entities")
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(layer_id: str) -> None:
            if layer_id in visiting:
                raise ValueError("Layer hierarchy contains a cycle")
            if layer_id in visited:
                return
            visiting.add(layer_id)
            for child in self.layers[layer_id].children:
                if child.kind == "layer":
                    visit(child.entity_id)
            visiting.remove(layer_id)
            visited.add(layer_id)
        for page_id in self.root_page_ids:
            visit(page_id)

    def add_page(
        self, name: str = "Page", bound: BoundGeometry | None = None,
        x: float = 0, y: float = 0,
    ) -> LayerNode:
        layer = LayerNode(
            name=name, is_page=True, translate_x=x, translate_y=y,
            bound=bound or BoundGeometry.rectangle(0, 0, CHAPTER_WIDTH, 1080),
        )
        self.layers[layer.layer_id] = layer
        self.root_page_ids.append(layer.layer_id)
        self.ensure_height_for(layer.layer_id)
        return layer

    def add_layer(
        self, parent_id: str, name: str = "Layer", bound: BoundGeometry | None = None,
        index: int | None = None,
        layer_kind: Literal["bounded", "open_shape"] = "bounded",
        style: ShapeStyle | None = None,
    ) -> LayerNode:
        parent = self.layers[parent_id]
        if parent.layer_kind in {"fill", "open_shape"}:
            raise ValueError("Leaf layers cannot contain entities")
        layer = LayerNode(
            name=name, parent_id=parent_id, layer_kind=layer_kind,
            bound=bound or BoundGeometry.rectangle(0, 0, 720, 720),
            shape_style=style or ShapeStyle(),
        )
        self.layers[layer.layer_id] = layer
        reference = ChildRef("layer", layer.layer_id)
        if index is None:
            parent.children.append(reference)
        else:
            parent.children.insert(
                max(0, min(int(index), len(parent.children))), reference
            )
        return layer

    def add_fill_layer(
        self, parent_id: str, name: str = "Fill", color: str = "#111111",
    ) -> LayerNode:
        parent = self.layers[parent_id]
        if parent.layer_kind in {"fill", "open_shape"}:
            raise ValueError("Leaf layers cannot contain entities")
        layer = LayerNode(
            name=name, parent_id=parent_id, layer_kind="fill",
            bound=None, shape_style=ShapeStyle(primary_color=color),
        )
        self.layers[layer.layer_id] = layer
        parent.children.insert(0, ChildRef("layer", layer.layer_id))
        return layer

    def add_object(self, parent_id: str, obj: ObjectEntity) -> ObjectEntity:
        parent = self.layers[parent_id]
        if parent.is_page or parent.layer_kind in {"fill", "open_shape"}:
            raise ValueError("Objects cannot belong directly to page layers")
        obj.parent_layer_id = parent_id
        self.objects[obj.object_id] = obj
        parent.children.append(ChildRef("object", obj.object_id))
        if isinstance(obj, RasterObject):
            parent.last_raster_id = obj.object_id
        return obj

    def parent_ref_list(self, kind: str, entity_id: str) -> list[ChildRef] | None:
        if kind == "layer" and entity_id in self.root_page_ids:
            return None
        parent_id = (
            self.layers[entity_id].parent_id if kind == "layer"
            else self.objects[entity_id].parent_layer_id
        )
        return self.layers[parent_id].children if parent_id else None

    def move_entity(
        self, kind: Literal["layer", "object"], entity_id: str,
        new_parent_id: str | None, index: int,
    ) -> None:
        if kind == "layer" and self.layers[entity_id].is_page:
            if new_parent_id is not None:
                raise ValueError("Page layers cannot be reparented")
            old = self.root_page_ids.index(entity_id)
            self.root_page_ids.pop(old)
            if old < index:
                index -= 1
            self.root_page_ids.insert(max(0, min(index, len(self.root_page_ids))), entity_id)
            return
        if new_parent_id is None:
            raise ValueError("Only page layers can be roots")
        new_parent = self.layers[new_parent_id]
        if new_parent.layer_kind in {"fill", "open_shape"}:
            raise ValueError("Leaf layers cannot contain entities")
        if kind == "object" and new_parent.is_page:
            raise ValueError("Objects cannot belong directly to page layers")
        if kind == "layer":
            cursor: str | None = new_parent_id
            while cursor:
                if cursor == entity_id:
                    raise ValueError("Cannot move a layer into its own subtree")
                cursor = self.layers[cursor].parent_id
            entity = self.layers[entity_id]
            old_parent = self.layers[entity.parent_id]
        else:
            entity = self.objects[entity_id]
            old_parent = self.layers[entity.parent_layer_id]
        old_ref = next(item for item in old_parent.children if item.kind == kind and item.entity_id == entity_id)
        old_index = old_parent.children.index(old_ref)
        old_parent.children.remove(old_ref)
        if old_parent is new_parent and old_index < index:
            index -= 1
        new_parent.children.insert(max(0, min(index, len(new_parent.children))), old_ref)
        if kind == "layer":
            entity.parent_id = new_parent_id
        else:
            entity.parent_layer_id = new_parent_id

    def delete_entity(self, kind: str, entity_id: str) -> set[str]:
        deleted_objects: set[str] = set()
        if kind == "object":
            obj = self.objects.pop(entity_id)
            parent = self.layers[obj.parent_layer_id]
            parent.children = [r for r in parent.children if r.entity_id != entity_id]
            deleted_objects.add(entity_id)
            return deleted_objects
        layer = self.layers[entity_id]
        for child in list(layer.children):
            deleted_objects.update(self.delete_entity(child.kind, child.entity_id))
        if layer.is_page:
            self.root_page_ids.remove(entity_id)
        else:
            parent = self.layers[layer.parent_id]
            parent.children = [r for r in parent.children if r.entity_id != entity_id]
        del self.layers[entity_id]
        return deleted_objects

    def layer_world_translation(self, layer_id: str) -> tuple[float, float]:
        x = y = 0.0
        cursor: str | None = layer_id
        while cursor:
            layer = self.layers[cursor]
            x += layer.translate_x
            y += layer.translate_y
            cursor = layer.parent_id
        return x, y

    def page_for_layer(self, layer_id: str) -> LayerNode:
        cursor = self.layers[layer_id]
        while cursor.parent_id:
            cursor = self.layers[cursor.parent_id]
        return cursor

    def ancestor_layers(self, layer_id: str) -> list[LayerNode]:
        result: list[LayerNode] = []
        cursor: str | None = layer_id
        while cursor:
            result.append(self.layers[cursor])
            cursor = self.layers[cursor].parent_id
        return list(reversed(result))

    def effective_grid(self, layer_id: str) -> GridSettings:
        result = self.grid
        for layer in self.ancestor_layers(layer_id):
            if layer.grid_override is not None:
                result = layer.grid_override
        return result

    def effective_object_opacity(self, object_id: str) -> float:
        obj = self.objects[object_id]
        parent = self.layers[obj.parent_layer_id]
        local = parent.opacity if obj.opacity_locked else obj.opacity
        value = local
        cursor = parent.parent_id
        while cursor:
            value *= self.layers[cursor].opacity
            cursor = self.layers[cursor].parent_id
        return max(0.0, min(1.0, value))

    def set_layer_opacity(self, layer_id: str, value: float) -> None:
        layer = self.layers[layer_id]
        layer.opacity = max(0.0, min(1.0, value))
        for child in layer.children:
            if child.kind == "object":
                obj = self.objects[child.entity_id]
                if obj.opacity_locked:
                    obj.opacity = layer.opacity

    def ensure_height_for(self, layer_id: str) -> bool:
        layer = self.layers[layer_id]
        if layer.bound is None:
            return False
        wx, wy = self.layer_world_translation(layer_id)
        _, top, _, height = layer.bound.bbox()
        bottom = wy + top + height
        if layer.layer_kind == "open_shape":
            maximum = max(
                (node.width_multiplier for node in layer.bound.nodes),
                default=1.0,
            )
            bottom += (
                layer.shape_style.base_thickness * maximum / 2
                + layer.shape_style.outline_thickness
            )
        needed = int(math.ceil(bottom))
        if needed <= self.height:
            return False
        self.height = needed + GROWTH_MARGIN
        return True

    def minimum_safe_height(self) -> int:
        bottom = 1.0
        for page_id in self.root_page_ids:
            page = self.layers[page_id]
            if page.bound is None:
                continue
            _, top, _, height = page.bound.bbox()
            bottom = max(bottom, page.translate_y + top + height)
        return int(math.ceil(bottom))

    def trim_height(self, height: int) -> None:
        minimum = self.minimum_safe_height()
        if height < minimum:
            raise ValueError(f"Chapter cannot be shorter than visible page bounds ({minimum}px)")
        self.height = int(height)

    def iter_render_order(self) -> Iterator[tuple[str, LayerNode | ObjectEntity]]:
        def walk(layer: LayerNode) -> Iterator[tuple[str, LayerNode | ObjectEntity]]:
            yield "layer", layer
            for child in reversed(layer.children):
                if child.kind == "layer":
                    yield from walk(self.layers[child.entity_id])
                else:
                    yield "object", self.objects[child.entity_id]
        for page_id in reversed(self.root_page_ids):
            yield from walk(self.layers[page_id])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "id": self.chapter_id,
            "name": self.name, "size": [self.width, self.height],
            "background": self.background, "grid": self.grid.to_dict(),
            "root_page_ids": self.root_page_ids,
            "layers": [layer.to_dict() for layer in self.layers.values()],
            "objects": [obj.to_dict() for obj in self.objects.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChapterDocument":
        schema = int(data.get("schema_version", 1))
        if schema > SCHEMA_VERSION:
            raise ValueError(f"Unsupported future chapter schema: {schema}")
        size = data.get("size", [CHAPTER_WIDTH, DEFAULT_CHAPTER_HEIGHT])
        layers = [LayerNode.from_dict(item) for item in data.get("layers", [])]
        objects = [object_from_dict(item) for item in data.get("objects", [])]
        result = cls(
            chapter_id=str(data["id"]), name=str(data.get("name", "Chapter")),
            width=int(size[0]), height=int(size[1]),
            background=str(data.get("background", "#ffffff")),
            grid=GridSettings.from_dict(data.get("grid")),
            root_page_ids=[str(item) for item in data.get("root_page_ids", [])],
            layers={item.layer_id: item for item in layers},
            objects={item.object_id: item for item in objects},
            schema_version=schema,
        )
        for obj in result.objects.values():
            if not isinstance(obj, TextObject):
                continue
            obj.margin = max(0.0, float(obj.margin))
            if obj.layout_mode not in {"free", "strict"}:
                obj.layout_mode = "strict"
            if obj.horizontal_alignment not in {"left", "center", "right"}:
                obj.horizontal_alignment = "center"
            if obj.vertical_alignment not in {"top", "middle", "bottom"}:
                obj.vertical_alignment = "middle"
            if obj.transform_quad is not None and len(obj.transform_quad) != 4:
                obj.transform_quad = None
            if obj.legacy_alignment is not None:
                parent = result.layers[obj.parent_layer_id]
                if obj.legacy_alignment == "layer":
                    left, top, width, height = parent.bound.bbox()
                    origin_x = left + (width - obj.width) / 2 + obj.x
                    origin_y = top + (height - obj.height) / 2 + obj.y
                else:
                    grid = result.effective_grid(obj.parent_layer_id)
                    origin_x, origin_y = grid.snap(obj.x, obj.y)
                obj.x = 0.0
                obj.y = 0.0
                obj.layout_mode = "free"
                obj.transform_quad = [
                    (origin_x, origin_y),
                    (origin_x + obj.width, origin_y),
                    (origin_x + obj.width, origin_y + obj.height),
                    (origin_x, origin_y + obj.height),
                ]
                obj.legacy_alignment = None
        result.validate()
        result.schema_version = SCHEMA_VERSION
        return result


@dataclass
class ChapterReference:
    chapter_id: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.chapter_id, "name": self.name}


@dataclass
class SeriesDocument:
    series_id: str = field(default_factory=new_id)
    name: str = "Untitled Series"
    chapters: list[ChapterReference] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "id": self.series_id,
            "name": self.name, "chapters": [item.to_dict() for item in self.chapters],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SeriesDocument":
        schema = int(data.get("schema_version", 1))
        if schema > SCHEMA_VERSION:
            raise ValueError(f"Unsupported future series schema: {schema}")
        return cls(
            series_id=str(data["id"]), name=str(data.get("name", "Untitled Series")),
            chapters=[
                ChapterReference(str(item["id"]), str(item.get("name", "Chapter")))
                for item in data.get("chapters", [])
            ],
            schema_version=SCHEMA_VERSION,
        )
