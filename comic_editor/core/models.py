"""Versioned, UI-independent series/chapter document model."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Literal


SCHEMA_VERSION = 12
CHAPTER_WIDTH = 1080
DEFAULT_CHAPTER_HEIGHT = 3240
GROWTH_MARGIN = 1080


def new_id() -> str:
    return uuid.uuid4().hex


def _point(value: Iterable[float]) -> tuple[float, float]:
    x, y = value
    return float(x), float(y)


def canonical_argb(value: object, fallback: str = "#FF000000") -> str:
    """Return a color in Qt's canonical ``#AARRGGBB`` representation."""
    text = str(value or "").strip()
    if text.startswith("#"):
        digits = text[1:]
        if len(digits) == 3:
            digits = "FF" + "".join(character * 2 for character in digits)
        elif len(digits) == 4:
            digits = "".join(character * 2 for character in digits)
        elif len(digits) == 6:
            digits = "FF" + digits
        if len(digits) == 8:
            try:
                int(digits, 16)
            except ValueError:
                pass
            else:
                return f"#{digits.upper()}"
    normalized_fallback = str(fallback).strip()
    if normalized_fallback != text:
        return canonical_argb(normalized_fallback, "#FF000000")
    return "#FF000000"


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
    roundness_enabled: bool | None = None
    width_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if self.roundness_enabled is None:
            self.roundness_enabled = self.roundness > 0

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
        self.roundness = float(self.roundness)
        if not math.isfinite(self.roundness) or self.roundness < 0:
            self.roundness = 0.0
        self.roundness_enabled = (
            self.roundness > 0
            if self.roundness_enabled is None
            else bool(self.roundness_enabled)
        )
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
            "roundness_enabled": self.roundness_enabled,
            "width_multiplier": self.width_multiplier,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PathNode":
        position = data.get("position", [0, 0])
        roundness = float(data.get("roundness", 0.0))
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
            roundness=roundness,
            roundness_enabled=bool(
                data.get("roundness_enabled", roundness > 0)
            ),
            width_multiplier=float(data.get("width_multiplier", 1.0)),
        )
        result.validate()
        return result


@dataclass
class PathContour:
    """An additional editable subpath in a custom bound."""

    nodes: list[PathNode] = field(default_factory=list)
    closed: bool = True

    def validate(self) -> None:
        minimum = 3 if self.closed else 2
        if len(self.nodes) < minimum:
            raise ValueError(
                f"{'Closed contours' if self.closed else 'Open contours'} "
                f"require at least {minimum} points"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "closed": self.closed,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PathContour":
        result = cls(
            nodes=[
                PathNode.from_dict(item) for item in data.get("nodes", [])
            ],
            closed=bool(data.get("closed", True)),
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
        self.base_thickness = max(
            1, min(1000, math.floor(float(self.base_thickness) + 0.5))
        )
        self.outline_thickness = max(
            0, min(500, math.floor(float(self.outline_thickness) + 0.5))
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
    additional_contours: list[PathContour]

    def __init__(
        self, kind: str = "rect",
        points: Iterable[Iterable[float]] | None = None,
        *,
        nodes: list[PathNode] | None = None,
        closed: bool | None = None,
        primitive: str | None = None,
        additional_contours: list[PathContour] | None = None,
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
        self.additional_contours = list(additional_contours or [])
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

    def iter_contours(self) -> Iterator[PathContour]:
        yield PathContour(self.nodes, self.closed)
        yield from self.additional_contours

    def contour_for_node(self, node_id: str) -> PathContour | None:
        return next((
            contour for contour in self.iter_contours()
            if any(node.node_id == node_id for node in contour.nodes)
        ), None)

    def handle_requirements(
        self, node: PathNode,
    ) -> tuple[bool, bool]:
        """Return whether this node may use incoming and outgoing handles."""
        contour = self.contour_for_node(node.node_id)
        if contour is None:
            return False, False
        index = next(
            index for index, candidate in enumerate(contour.nodes)
            if candidate.node_id == node.node_id
        )
        return (
            contour.closed or index > 0,
            contour.closed or index < len(contour.nodes) - 1,
        )

    @staticmethod
    def _normalize_contour_handles(contour: PathContour) -> None:
        nodes = contour.nodes
        if not nodes:
            return
        last_index = len(nodes) - 1
        for index, node in enumerate(nodes):
            if node.point_type != "bezier":
                node.incoming = node.outgoing = None
                node.handles_locked = True
                continue
            needs_incoming = contour.closed or index > 0
            needs_outgoing = contour.closed or index < last_index
            if not (needs_incoming and needs_outgoing):
                node.handles_locked = False
            if not needs_incoming:
                node.incoming = None
            if not needs_outgoing:
                node.outgoing = None
            if needs_incoming and node.incoming is None:
                if node.outgoing is not None:
                    node.incoming = (
                        node.x * 2 - node.outgoing[0],
                        node.y * 2 - node.outgoing[1],
                    )
                else:
                    previous = nodes[index - 1]
                    node.incoming = (
                        node.x + (previous.x - node.x) / 3,
                        node.y + (previous.y - node.y) / 3,
                    )
            if needs_outgoing and node.outgoing is None:
                if node.incoming is not None:
                    node.outgoing = (
                        node.x * 2 - node.incoming[0],
                        node.y * 2 - node.incoming[1],
                    )
                else:
                    following = nodes[(index + 1) % len(nodes)]
                    node.outgoing = (
                        node.x + (following.x - node.x) / 3,
                        node.y + (following.y - node.y) / 3,
                    )

    def normalize_bezier_handles(self) -> None:
        """Restore the handle topology required by this path after an edit."""
        for contour in self.iter_contours():
            self._normalize_contour_handles(contour)

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
        for contour_index, contour in enumerate(self.iter_contours()):
            contour.validate()
            for index, node in enumerate(contour.nodes):
                node.validate()
                if node.node_id in ids:
                    raise ValueError("Duplicate shape point ID")
                ids.add(node.node_id)
                if node.point_type == "bezier":
                    needs_incoming = contour.closed or index > 0
                    needs_outgoing = (
                        contour.closed or index < len(contour.nodes) - 1
                    )
                    location = (
                        f"contour {contour_index}, point {index} "
                        f"({node.node_id})"
                    )
                    if needs_incoming != (node.incoming is not None):
                        raise ValueError(
                            f"Malformed incoming Bézier handle at {location}"
                        )
                    if needs_outgoing != (node.outgoing is not None):
                        raise ValueError(
                            f"Malformed outgoing Bézier handle at {location}"
                        )
                    if (
                        node.handles_locked
                        and node.incoming is not None
                        and node.outgoing is not None
                        and math.dist(
                            node.incoming,
                            (
                                node.x * 2 - node.outgoing[0],
                                node.y * 2 - node.outgoing[1],
                            ),
                        ) > 1e-4
                    ):
                        raise ValueError(
                            f"Locked Bézier handles must be symmetric at "
                            f"{location}"
                        )
        if self.primitive in {"rectangle", "ellipse"} and len(self.nodes) != 4:
            raise ValueError("Primitive shapes require four points")
        if self.primitive in {"rectangle", "ellipse"} and self.additional_contours:
            raise ValueError("Primitive shapes cannot have additional contours")

    def bbox(self) -> tuple[float, float, float, float]:
        values = [
            point
            for contour in self.iter_contours()
            for node in contour.nodes
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
        for contour in self.iter_contours():
            if not contour.closed:
                continue
            previous = contour.nodes[-1].position
            for node in contour.nodes:
                current = node.position
                x1, y1 = previous
                x2, y2 = current
                crosses = (y1 > y) != (y2 > y)
                if (
                    crosses
                    and x < (
                        (x2 - x1) * (y - y1) / (y2 - y1) + x1
                    )
                ):
                    inside = not inside
                previous = current
        return inside

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "type": "path", "closed": self.closed,
            "primitive": self.primitive,
            "nodes": [node.to_dict() for node in self.nodes],
            "additional_contours": [
                contour.to_dict() for contour in self.additional_contours
            ],
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
                additional_contours=[
                    PathContour.from_dict(item)
                    for item in data.get("additional_contours", [])
                ],
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
    compound_enabled: bool = False
    compound_operation: Literal["add", "subtract", "ignore"] = "add"
    ignore_parent_mask: bool = False

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
            "compound_enabled": self.compound_enabled,
            "compound_operation": self.compound_operation,
            "ignore_parent_mask": self.ignore_parent_mask,
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
            compound_enabled=bool(data.get("compound_enabled", False)),
            compound_operation=str(data.get("compound_operation", "add")),
            ignore_parent_mask=bool(data.get("ignore_parent_mask", False)),
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
            if node.point_type == "vector" and node.roundness_enabled
        ]
        return max(values, default=0.0)

    @vertex_radius.setter
    def vertex_radius(self, value: float) -> None:
        if self.bound:
            for node in self.bound.nodes:
                if node.point_type == "vector":
                    node.roundness = max(0.0, float(value))
                    node.roundness_enabled = node.roundness > 0


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
    geometry_reference: Literal["direct", "compound"] = "direct"
    ignore_parent_mask: bool = False
    underlay_opacity: float = 0.0

    def common_dict(self) -> dict[str, Any]:
        return {
            "id": self.object_id, "type": self.object_type, "name": self.name,
            "parent_layer_id": self.parent_layer_id, "position": [self.x, self.y],
            "visible": self.visible, "opacity": self.opacity,
            "opacity_locked": self.opacity_locked,
            "geometry_reference": self.geometry_reference,
            "ignore_parent_mask": self.ignore_parent_mask,
            "underlay_opacity": self.underlay_opacity,
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

    @property
    def display_name(self) -> str:
        """Short content-derived label used by UI surfaces."""
        return " ".join(self.text.split())[:16].rstrip() or "Text"

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


@dataclass
class ColorGradientStop:
    """One stable, independently editable stop in a color ramp."""

    stop_id: str = field(default_factory=new_id)
    position: float = 0.0
    color: str = "#FF000000"

    def validate(self) -> None:
        try:
            position = float(self.position)
        except (TypeError, ValueError):
            position = 0.0
        self.position = (
            max(0.0, min(1.0, position))
            if math.isfinite(position) else 0.0
        )
        self.color = canonical_argb(self.color)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.stop_id,
            "position": self.position,
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ColorGradientStop":
        result = cls(
            stop_id=str(data.get("id") or new_id()),
            position=float(data.get("position", 0.0)),
            color=str(data.get("color", "#FF000000")),
        )
        result.validate()
        return result


@dataclass
class ColorGradientRamp:
    """Reusable color-map data independent of the field that samples it."""

    stops: list[ColorGradientStop] = field(default_factory=lambda: [
        ColorGradientStop(position=0.0, color="#FF000000"),
        ColorGradientStop(position=1.0, color="#FFFFFFFF"),
    ])
    interpolation: Literal["linear"] = "linear"

    def validate(self) -> None:
        if self.interpolation != "linear":
            self.interpolation = "linear"
        while len(self.stops) < 2:
            self.stops.append(ColorGradientStop(
                position=1.0 if self.stops else 0.0,
                color=(
                    self.stops[-1].color
                    if self.stops else "#FF000000"
                ),
            ))
        ids: set[str] = set()
        for stop in self.stops:
            stop.validate()
            if stop.stop_id in ids:
                stop.stop_id = new_id()
            ids.add(stop.stop_id)
        # Python's stable sort preserves insertion order for hard transitions.
        self.stops.sort(key=lambda stop: stop.position)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "value_type": "color",
            "interpolation": self.interpolation,
            "stops": [stop.to_dict() for stop in self.stops],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ColorGradientRamp":
        data = data or {}
        result = cls(
            stops=[
                ColorGradientStop.from_dict(item)
                for item in data.get("stops", [])
            ],
            interpolation=str(data.get("interpolation", "linear")),
        )
        result.validate()
        return result

    def copy(self) -> "ColorGradientRamp":
        return type(self).from_dict(self.to_dict())


@dataclass
class LineGradientField:
    geometry: BoundGeometry = field(default_factory=lambda: BoundGeometry.path([
        PathNode(x=270.0, y=540.0),
        PathNode(x=810.0, y=540.0),
    ]))
    direction_mode: Literal["parallel", "perpendicular"] = "parallel"
    reverse_direction: bool = False
    perpendicular_distance: float = 120.0

    def validate(self) -> None:
        self.geometry.closed = False
        self.geometry.primitive = "custom"
        self.geometry.validate()
        if len(self.geometry.nodes) < 2:
            raise ValueError("Line gradients require at least two points")
        if self.direction_mode not in {"parallel", "perpendicular"}:
            self.direction_mode = "parallel"
        self.reverse_direction = bool(self.reverse_direction)
        try:
            distance = float(self.perpendicular_distance)
        except (TypeError, ValueError):
            distance = 120.0
        self.perpendicular_distance = (
            distance if math.isfinite(distance) and abs(distance) >= 0.001
            else 120.0
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "geometry": self.geometry.to_dict(),
            "direction_mode": self.direction_mode,
            "reverse_direction": self.reverse_direction,
            "perpendicular_distance": self.perpendicular_distance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LineGradientField":
        data = data or {}
        result = cls(
            geometry=BoundGeometry.from_dict(
                data.get("geometry") or {
                    "closed": False,
                    "primitive": "custom",
                    "nodes": [
                        {"position": [270, 540]},
                        {"position": [810, 540]},
                    ],
                }
            ),
            direction_mode=str(data.get("direction_mode", "parallel")),
            reverse_direction=bool(data.get("reverse_direction", False)),
            perpendicular_distance=float(
                data.get("perpendicular_distance", 120.0)
            ),
        )
        result.validate()
        return result


@dataclass
class RadialGradientField:
    origin_x: float = 540.0
    origin_y: float = 540.0
    radius_x: float = 270.0
    radius_y: float = 270.0
    rotation: float = 0.0
    ellipse_enabled: bool = False
    center_auto: bool = True
    manual_center: tuple[float, float] | None = None
    reverse_direction: bool = False
    uniform: bool = False
    distance: float = 120.0

    def validate(self) -> None:
        values = (
            self.origin_x, self.origin_y, self.radius_x,
            self.radius_y, self.rotation,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("Radial gradient contains non-finite geometry")
        self.origin_x = float(self.origin_x)
        self.origin_y = float(self.origin_y)
        self.radius_x = max(0.001, abs(float(self.radius_x)))
        self.radius_y = max(0.001, abs(float(self.radius_y)))
        self.rotation = float(self.rotation) % 360.0
        self.ellipse_enabled = bool(self.ellipse_enabled)
        self.center_auto = bool(self.center_auto)
        self.manual_center = (
            _point(self.manual_center)
            if self.manual_center is not None else None
        )
        if self.manual_center is None:
            self.center_auto = True
        self.reverse_direction = bool(self.reverse_direction)
        self.uniform = bool(self.uniform)
        try:
            distance = float(self.distance)
        except (TypeError, ValueError):
            distance = 120.0
        self.distance = (
            max(0.001, distance) if math.isfinite(distance) else 120.0
        )

    def center(self) -> tuple[float, float]:
        if self.center_auto or self.manual_center is None:
            return self.origin_x, self.origin_y
        return self.manual_center

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "origin": [self.origin_x, self.origin_y],
            "radii": [self.radius_x, self.radius_y],
            "rotation": self.rotation,
            "ellipse_enabled": self.ellipse_enabled,
            "center_auto": self.center_auto,
            "manual_center": (
                list(self.manual_center)
                if self.manual_center is not None else None
            ),
            "reverse_direction": self.reverse_direction,
            "uniform": self.uniform,
            "distance": self.distance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RadialGradientField":
        data = data or {}
        origin = data.get("origin", [540.0, 540.0])
        radii = data.get("radii", [270.0, 270.0])
        result = cls(
            origin_x=float(origin[0]), origin_y=float(origin[1]),
            radius_x=float(radii[0]), radius_y=float(radii[1]),
            rotation=float(data.get("rotation", 0.0)),
            ellipse_enabled=bool(data.get("ellipse_enabled", False)),
            center_auto=bool(data.get("center_auto", True)),
            manual_center=(
                _point(data["manual_center"])
                if data.get("manual_center") is not None else None
            ),
            reverse_direction=bool(data.get("reverse_direction", False)),
            uniform=bool(data.get("uniform", False)),
            distance=float(data.get(
                "distance", data.get("outward_distance", 120.0)
            )),
        )
        result.validate()
        return result


@dataclass
class ShapeGradientField:
    center_auto: bool = True
    manual_center: tuple[float, float] | None = None
    reverse_direction: bool = False
    uniform: bool = False
    distance: float = 120.0

    def validate(self) -> None:
        self.center_auto = bool(self.center_auto)
        self.manual_center = (
            _point(self.manual_center)
            if self.manual_center is not None else None
        )
        if self.manual_center is None:
            self.center_auto = True
        self.reverse_direction = bool(self.reverse_direction)
        self.uniform = bool(self.uniform)
        try:
            distance = float(self.distance)
        except (TypeError, ValueError):
            distance = 120.0
        self.distance = (
            max(0.001, distance) if math.isfinite(distance) else 120.0
        )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "center_auto": self.center_auto,
            "manual_center": (
                list(self.manual_center)
                if self.manual_center is not None else None
            ),
            "reverse_direction": self.reverse_direction,
            "uniform": self.uniform,
            "distance": self.distance,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ShapeGradientField":
        data = data or {}
        result = cls(
            center_auto=bool(data.get("center_auto", True)),
            manual_center=(
                _point(data["manual_center"])
                if data.get("manual_center") is not None else None
            ),
            reverse_direction=bool(data.get("reverse_direction", False)),
            uniform=bool(data.get("uniform", False)),
            distance=float(data.get(
                "distance", data.get("outward_distance", 120.0)
            )),
        )
        result.validate()
        return result


@dataclass
class GradientObject(DocumentObject):
    """Base object: owns field geometry, while subtypes own mapped values."""

    object_type: str = "gradient"
    name: str = "Gradient"
    gradient_type: str = "gradient"
    field_type: Literal["line", "radial", "parent_shape"] = "line"
    line_field: LineGradientField = field(default_factory=LineGradientField)
    radial_field: RadialGradientField = field(
        default_factory=RadialGradientField
    )
    shape_field: ShapeGradientField = field(
        default_factory=ShapeGradientField
    )
    gradient_revision: int = 0

    def validate_gradient(self) -> None:
        if self.field_type not in {"line", "radial", "parent_shape"}:
            self.field_type = "line"
        self.line_field.validate()
        self.radial_field.validate()
        self.shape_field.validate()
        self.gradient_revision = max(0, int(self.gradient_revision))

    def touch_revision(self) -> int:
        self.gradient_revision += 1
        return self.gradient_revision

    def gradient_dict(self) -> dict[str, Any]:
        self.validate_gradient()
        result = self.common_dict()
        result.update({
            "gradient_type": self.gradient_type,
            "field_type": self.field_type,
            "line_field": self.line_field.to_dict(),
            "radial_field": self.radial_field.to_dict(),
            "shape_field": self.shape_field.to_dict(),
            "gradient_revision": self.gradient_revision,
        })
        return result

    def to_dict(self) -> dict[str, Any]:
        return self.gradient_dict()


@dataclass
class ColorFillGradientObject(GradientObject):
    gradient_type: str = "color_fill"
    name: str = "Color Gradient"
    ramp: ColorGradientRamp = field(default_factory=ColorGradientRamp)
    loaded_preset_id: str = ""

    def validate_gradient(self) -> None:
        super().validate_gradient()
        self.gradient_type = "color_fill"
        self.ramp.validate()
        self.loaded_preset_id = str(self.loaded_preset_id or "")

    def to_dict(self) -> dict[str, Any]:
        result = self.gradient_dict()
        result.update({
            "ramp": self.ramp.to_dict(),
            "loaded_preset_id": self.loaded_preset_id,
        })
        return result


@dataclass
class VectorStrokePoint:
    """An editable anchor plus the hidden cubic controls for one vector stroke."""

    point_id: str = field(default_factory=new_id)
    x: float = 0.0
    y: float = 0.0
    incoming: tuple[float, float] | None = None
    outgoing: tuple[float, float] | None = None
    width: float = 1.0
    opacity: float = 1.0

    @property
    def position(self) -> tuple[float, float]:
        return self.x, self.y

    @position.setter
    def position(self, value: Iterable[float]) -> None:
        self.x, self.y = _point(value)

    def validate(self) -> None:
        self.x, self.y = float(self.x), float(self.y)
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise ValueError(f"Vector point {self.point_id} has a non-finite anchor")
        self.incoming = _point(self.incoming) if self.incoming is not None else None
        self.outgoing = _point(self.outgoing) if self.outgoing is not None else None
        for label, control in (
            ("incoming", self.incoming), ("outgoing", self.outgoing),
        ):
            if control is not None and not all(math.isfinite(value) for value in control):
                raise ValueError(
                    f"Vector point {self.point_id} has a non-finite {label} control"
                )
        self.width = max(1.0, min(1000.0, float(self.width)))
        if not math.isfinite(self.width):
            self.width = 1.0
        self.opacity = max(0.0, min(1.0, float(self.opacity)))
        if not math.isfinite(self.opacity):
            self.opacity = 1.0

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.point_id,
            "position": [self.x, self.y],
            "incoming": list(self.incoming) if self.incoming is not None else None,
            "outgoing": list(self.outgoing) if self.outgoing is not None else None,
            "width": self.width,
            "opacity": self.opacity,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VectorStrokePoint":
        position = data.get("position", [0.0, 0.0])
        result = cls(
            point_id=str(data.get("id") or new_id()),
            x=float(position[0]),
            y=float(position[1]),
            incoming=(
                _point(data["incoming"]) if data.get("incoming") is not None
                else None
            ),
            outgoing=(
                _point(data["outgoing"]) if data.get("outgoing") is not None
                else None
            ),
            width=float(data.get("width", 1.0)),
            opacity=float(data.get("opacity", 1.0)),
        )
        result.validate()
        return result


@dataclass
class VectorStroke:
    stroke_id: str = field(default_factory=new_id)
    color: str = "#FF000000"
    closed: bool = False
    start_cap: Literal["point", "square", "round"] = "round"
    end_cap: Literal["point", "square", "round"] = "round"
    points: list[VectorStrokePoint] = field(default_factory=list)

    def validate(self) -> None:
        self.color = canonical_argb(self.color)
        if self.start_cap not in {"point", "square", "round"}:
            self.start_cap = "round"
        if self.end_cap not in {"point", "square", "round"}:
            self.end_cap = "round"
        if not self.points:
            raise ValueError(f"Vector stroke {self.stroke_id} has no points")
        ids: set[str] = set()
        for point in self.points:
            point.validate()
            if point.point_id in ids:
                raise ValueError(
                    f"Vector stroke {self.stroke_id} has duplicate point IDs"
                )
            ids.add(point.point_id)
        if len(self.points) == 1:
            self.closed = False

    def derived_bounds(self) -> tuple[float, float, float, float]:
        """Conservative local bounds including controls and variable width."""
        if not self.points:
            return 0.0, 0.0, 0.0, 0.0
        coordinates: list[tuple[float, float]] = []
        maximum_radius = 0.5
        for point in self.points:
            coordinates.append(point.position)
            if point.incoming is not None:
                coordinates.append(point.incoming)
            if point.outgoing is not None:
                coordinates.append(point.outgoing)
            maximum_radius = max(maximum_radius, point.width / 2)
        left = min(point[0] for point in coordinates) - maximum_radius
        top = min(point[1] for point in coordinates) - maximum_radius
        right = max(point[0] for point in coordinates) + maximum_radius
        bottom = max(point[1] for point in coordinates) + maximum_radius
        return left, top, right - left, bottom - top

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.stroke_id,
            "color": self.color,
            "closed": self.closed,
            "start_cap": self.start_cap,
            "end_cap": self.end_cap,
            "points": [point.to_dict() for point in self.points],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VectorStroke":
        result = cls(
            stroke_id=str(data.get("id") or new_id()),
            color=str(data.get("color", "#FF000000")),
            closed=bool(data.get("closed", False)),
            start_cap=str(data.get("start_cap", "round")),
            end_cap=str(data.get("end_cap", "round")),
            points=[
                VectorStrokePoint.from_dict(item)
                for item in data.get("points", [])
            ],
        )
        result.validate()
        return result


@dataclass
class VectorDrawingObject(DocumentObject):
    object_type: str = "vector_drawing"
    name: str = "Vector Drawing"
    strokes: list[VectorStroke] = field(default_factory=list)
    fill_child_ids: list[str] = field(default_factory=list)
    drawing_revision: int = 0

    def validate_vector(self) -> None:
        ids: set[str] = set()
        for stroke in self.strokes:
            stroke.validate()
            if stroke.stroke_id in ids:
                raise ValueError(
                    f"Vector drawing {self.object_id} has duplicate stroke IDs"
                )
            ids.add(stroke.stroke_id)
        if len(set(self.fill_child_ids)) != len(self.fill_child_ids):
            raise ValueError(
                f"Vector drawing {self.object_id} references a fill more than once"
            )
        self.fill_child_ids = [str(item) for item in self.fill_child_ids]
        self.drawing_revision = max(0, int(self.drawing_revision))

    def derived_bounds(self) -> tuple[float, float, float, float]:
        bounds = [
            stroke.derived_bounds() for stroke in self.strokes
            if stroke.points
        ]
        if not bounds:
            return 0.0, 0.0, 0.0, 0.0
        left = min(bound[0] for bound in bounds)
        top = min(bound[1] for bound in bounds)
        right = max(bound[0] + bound[2] for bound in bounds)
        bottom = max(bound[1] + bound[3] for bound in bounds)
        return left, top, right - left, bottom - top

    def touch_revision(self) -> int:
        self.drawing_revision += 1
        return self.drawing_revision

    def to_dict(self) -> dict[str, Any]:
        self.validate_vector()
        result = self.common_dict()
        result.update({
            "strokes": [stroke.to_dict() for stroke in self.strokes],
            "fill_child_ids": list(self.fill_child_ids),
            "drawing_revision": self.drawing_revision,
        })
        return result


@dataclass
class VectorFillObject(DocumentObject):
    object_type: str = "vector_fill"
    name: str = "Vector Fill"
    owner_drawing_id: str = ""
    geometry: BoundGeometry = field(
        default_factory=lambda: BoundGeometry.rectangle(0, 0, 1, 1)
    )
    fill_color: str = "#FF000000"
    source_seed: tuple[float, float] | None = None
    source_lasso: list[tuple[float, float]] = field(default_factory=list)
    fill_settings: dict[str, Any] = field(default_factory=dict)

    def validate_vector_fill(self) -> None:
        if not self.owner_drawing_id:
            raise ValueError(f"Vector fill {self.object_id} has no owner")
        self.geometry.validate()
        if not self.geometry.closed or any(
            not contour.closed for contour in self.geometry.additional_contours
        ):
            raise ValueError("Vector fills require closed contours")
        self.fill_color = canonical_argb(self.fill_color)
        self.source_seed = (
            _point(self.source_seed) if self.source_seed is not None else None
        )
        self.source_lasso = [_point(point) for point in self.source_lasso]
        self.fill_settings = dict(self.fill_settings)

    def derived_bounds(self) -> tuple[float, float, float, float]:
        return self.geometry.bbox()

    def to_dict(self) -> dict[str, Any]:
        self.validate_vector_fill()
        result = self.common_dict()
        result.update({
            "owner_drawing_id": self.owner_drawing_id,
            "geometry": self.geometry.to_dict(),
            "fill_color": self.fill_color,
            "source_seed": (
                list(self.source_seed) if self.source_seed is not None else None
            ),
            "source_lasso": [list(point) for point in self.source_lasso],
            "fill_settings": dict(self.fill_settings),
        })
        return result


ObjectEntity = (
    RasterObject | TextObject | GradientObject
    | VectorDrawingObject | VectorFillObject
    | DocumentObject
)


def object_from_dict(data: dict[str, Any]) -> ObjectEntity:
    position = data.get("position", [0, 0])
    common = dict(
        object_id=str(data["id"]), name=str(data.get("name", "Object")),
        parent_layer_id=str(data.get("parent_layer_id", "")), x=float(position[0]),
        y=float(position[1]), visible=bool(data.get("visible", True)),
        opacity=float(data.get("opacity", 1.0)),
        opacity_locked=bool(data.get("opacity_locked", True)),
        geometry_reference=str(data.get("geometry_reference", "direct")),
        ignore_parent_mask=bool(data.get("ignore_parent_mask", False)),
        underlay_opacity=float(data.get("underlay_opacity", 0.0)),
    )
    object_type = str(data.get("type", "object"))
    if object_type == "gradient":
        gradient_type = str(data.get("gradient_type", "color_fill"))
        gradient_common = dict(
            **common,
            field_type=str(data.get("field_type", "line")),
            line_field=LineGradientField.from_dict(data.get("line_field")),
            radial_field=RadialGradientField.from_dict(
                data.get("radial_field")
            ),
            shape_field=ShapeGradientField.from_dict(
                data.get("shape_field")
            ),
            gradient_revision=int(data.get("gradient_revision", 0)),
        )
        if gradient_type == "color_fill":
            result = ColorFillGradientObject(
                **gradient_common,
                ramp=ColorGradientRamp.from_dict(data.get("ramp")),
                loaded_preset_id=str(data.get("loaded_preset_id", "")),
            )
            result.validate_gradient()
            return result
        result = GradientObject(
            **gradient_common, gradient_type=gradient_type
        )
        result.validate_gradient()
        return result
    if object_type == "vector_drawing":
        return VectorDrawingObject(
            **common,
            strokes=[
                VectorStroke.from_dict(item)
                for item in data.get("strokes", [])
            ],
            fill_child_ids=[
                str(item) for item in data.get("fill_child_ids", [])
            ],
            drawing_revision=int(data.get("drawing_revision", 0)),
        )
    if object_type == "vector_fill":
        return VectorFillObject(
            **common,
            owner_drawing_id=str(data.get("owner_drawing_id", "")),
            geometry=BoundGeometry.from_dict(
                data.get("geometry") or data.get("bound") or {}
            ),
            fill_color=str(data.get("fill_color", "#FF000000")),
            source_seed=(
                _point(data["source_seed"])
                if data.get("source_seed") is not None else None
            ),
            source_lasso=[
                _point(point) for point in data.get("source_lasso", [])
            ],
            fill_settings=dict(data.get("fill_settings") or {}),
        )
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
            layer.ignore_parent_mask = bool(layer.ignore_parent_mask)
            if layer.layer_kind not in {"bounded", "open_shape", "fill"}:
                raise ValueError("Unknown layer kind")
            if layer.compound_operation not in {"add", "subtract", "ignore"}:
                raise ValueError("Unknown compound operation")
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
                layer.compound_enabled = False
                layer.ignore_parent_mask = False
            elif layer.bound is None:
                raise ValueError("Bounded layers require geometry")
            else:
                layer.bound.validate()
                if layer.layer_kind == "open_shape":
                    if layer.is_page or layer.bound.closed:
                        raise ValueError(
                            "Open shape layers require open paths"
                        )
                elif not layer.bound.closed:
                    raise ValueError("Bounded layers require closed geometry")
            layer.shape_style.validate()
            layer.border_width = max(0.0, float(layer.border_width))
            layer.opacity = max(0.0, min(1.0, float(layer.opacity)))
            if layer.is_page and layer.layer_id not in self.root_page_ids:
                raise ValueError("Page layers must be chapter roots")
            if layer.is_page:
                layer.border_width = min(40.0, layer.border_width)
                layer.compound_enabled = False
                layer.ignore_parent_mask = False
            if not layer.is_page and layer.parent_id not in self.layers:
                raise ValueError(f"Layer {layer.layer_id} has no valid parent")
            if (
                layer.parent_id
                and self.layers[layer.parent_id].layer_kind == "fill"
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
                    if (
                        candidate_object is None
                        or isinstance(candidate_object, VectorFillObject)
                        or candidate_object.parent_layer_id != layer.layer_id
                    ):
                        raise ValueError("Invalid child object")
                else:
                    raise ValueError(f"Unknown child kind: {child.kind}")
        owned_fill_ids: set[str] = set()
        for obj in self.objects.values():
            try:
                underlay = float(obj.underlay_opacity)
            except (TypeError, ValueError):
                underlay = 0.0
            obj.underlay_opacity = (
                max(0.0, min(1.0, underlay))
                if math.isfinite(underlay) else 0.0
            )
            if isinstance(obj, GradientObject):
                obj.validate_gradient()
            if not isinstance(obj, VectorDrawingObject):
                continue
            obj.validate_vector()
            for fill_id in obj.fill_child_ids:
                if fill_id in owned_fill_ids:
                    raise ValueError(
                        f"Vector fill {fill_id} is owned by more than one drawing"
                    )
                fill = self.objects.get(fill_id)
                if not isinstance(fill, VectorFillObject):
                    raise ValueError(
                        f"Vector drawing {obj.object_id} references an invalid fill"
                    )
                if fill.owner_drawing_id != obj.object_id:
                    raise ValueError(
                        f"Vector fill {fill_id} has a mismatched owner"
                    )
                if fill.parent_layer_id != obj.parent_layer_id:
                    raise ValueError(
                        f"Vector fill {fill_id} does not inherit its owner's layer"
                    )
                owned_fill_ids.add(fill_id)
        for object_id, obj in self.objects.items():
            if isinstance(obj, VectorFillObject):
                obj.validate_vector_fill()
                owner = self.objects.get(obj.owner_drawing_id)
                if not isinstance(owner, VectorDrawingObject):
                    raise ValueError(
                        f"Vector fill {object_id} requires a Vector Drawing owner"
                    )
                parent = self.layers.get(owner.parent_layer_id)
                if object_id not in owned_fill_ids:
                    raise ValueError(
                        f"Vector fill {object_id} is not referenced by its owner"
                    )
            else:
                parent = self.layers.get(obj.parent_layer_id)
            if (
                parent is None
                or parent.layer_kind == "fill"
            ):
                raise ValueError(f"Object {object_id} requires a container layer")
            obj.opacity = max(0.0, min(1.0, float(obj.opacity)))
            obj.ignore_parent_mask = bool(obj.ignore_parent_mask)
            if isinstance(obj, VectorFillObject):
                obj.ignore_parent_mask = bool(owner.ignore_parent_mask)
            if obj.geometry_reference not in {"direct", "compound"}:
                obj.geometry_reference = "direct"
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
            ("object", key) for key, obj in self.objects.items()
            if not isinstance(obj, VectorFillObject)
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
        x: float = 0, y: float = 0, index: int | None = None,
        style: ShapeStyle | None = None,
    ) -> LayerNode:
        page_style = style or ShapeStyle(outline_thickness=4.0)
        page_style.validate()
        page_style.outline_thickness = min(
            40.0, page_style.outline_thickness
        )
        layer = LayerNode(
            name=name, is_page=True, translate_x=x, translate_y=y,
            bound=bound or BoundGeometry.rectangle(0, 0, CHAPTER_WIDTH, 1080),
            shape_style=page_style,
        )
        self.layers[layer.layer_id] = layer
        if index is None:
            self.root_page_ids.append(layer.layer_id)
        else:
            self.root_page_ids.insert(
                max(0, min(int(index), len(self.root_page_ids))),
                layer.layer_id,
            )
        self.ensure_height_for(layer.layer_id)
        return layer

    def add_layer(
        self, parent_id: str, name: str = "Layer", bound: BoundGeometry | None = None,
        index: int | None = None,
        layer_kind: Literal["bounded", "open_shape"] = "bounded",
        style: ShapeStyle | None = None,
    ) -> LayerNode:
        parent = self.layers[parent_id]
        if parent.layer_kind == "fill":
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
        if parent.layer_kind == "fill":
            raise ValueError("Leaf layers cannot contain entities")
        layer = LayerNode(
            name=name, parent_id=parent_id, layer_kind="fill",
            bound=None, shape_style=ShapeStyle(primary_color=color),
        )
        self.layers[layer.layer_id] = layer
        parent.children.insert(0, ChildRef("layer", layer.layer_id))
        return layer

    def add_object(
        self, parent_id: str, obj: ObjectEntity, index: int | None = None,
    ) -> ObjectEntity:
        if isinstance(obj, VectorFillObject):
            return self.add_vector_fill(parent_id, obj, index)
        parent = self.layers[parent_id]
        if parent.layer_kind == "fill":
            raise ValueError("Objects require a container layer")
        if isinstance(obj, GradientObject) and self.gradient_children(
            parent_id, obj.field_type
        ):
            raise ValueError(
                f"This shape already has a {obj.field_type} gradient"
            )
        obj.parent_layer_id = parent_id
        self.objects[obj.object_id] = obj
        reference = ChildRef("object", obj.object_id)
        if index is None:
            parent.children.append(reference)
        else:
            parent.children.insert(
                max(0, min(int(index), len(parent.children))), reference
            )
        if isinstance(obj, RasterObject):
            parent.last_raster_id = obj.object_id
        return obj

    def gradient_children(
        self, parent_id: str, field_type: str | None = None,
        *, excluding: str = "",
    ) -> list[GradientObject]:
        """Return direct gradient children in frontmost-first hierarchy order."""
        parent = self.layers.get(parent_id)
        if parent is None:
            return []
        result: list[GradientObject] = []
        for child in parent.children:
            if child.kind != "object" or child.entity_id == excluding:
                continue
            candidate = self.objects.get(child.entity_id)
            if (
                isinstance(candidate, GradientObject)
                and (field_type is None or candidate.field_type == field_type)
            ):
                result.append(candidate)
        return result

    def add_vector_fill(
        self, owner_id: str, fill: VectorFillObject,
        index: int | None = None,
    ) -> VectorFillObject:
        """Attach a fill to a Vector Drawing without adding a layer child ref."""
        owner = self.objects.get(owner_id)
        if not isinstance(owner, VectorDrawingObject):
            raise ValueError("Vector fills require a Vector Drawing owner")
        if fill.object_id in self.objects:
            raise ValueError(f"Duplicate object ID: {fill.object_id}")
        fill.owner_drawing_id = owner_id
        fill.parent_layer_id = owner.parent_layer_id
        fill.validate_vector_fill()
        self.objects[fill.object_id] = fill
        if index is None:
            owner.fill_child_ids.append(fill.object_id)
        else:
            owner.fill_child_ids.insert(
                max(0, min(int(index), len(owner.fill_child_ids))),
                fill.object_id,
            )
        owner.touch_revision()
        return fill

    def vector_fill_children(
        self, drawing_id: str,
    ) -> list[VectorFillObject]:
        drawing = self.objects.get(drawing_id)
        if not isinstance(drawing, VectorDrawingObject):
            return []
        return [
            fill for fill_id in drawing.fill_child_ids
            if isinstance((fill := self.objects.get(fill_id)), VectorFillObject)
        ]

    def reorder_vector_fill(
        self, owner_id: str, fill_id: str, index: int,
    ) -> None:
        owner = self.objects.get(owner_id)
        fill = self.objects.get(fill_id)
        if (
            not isinstance(owner, VectorDrawingObject)
            or not isinstance(fill, VectorFillObject)
            or fill.owner_drawing_id != owner_id
            or fill_id not in owner.fill_child_ids
        ):
            raise ValueError("Vector fills can only reorder within their owner")
        old_index = owner.fill_child_ids.index(fill_id)
        owner.fill_child_ids.pop(old_index)
        if old_index < index:
            index -= 1
        owner.fill_child_ids.insert(
            max(0, min(int(index), len(owner.fill_child_ids))), fill_id
        )
        owner.touch_revision()

    def parent_ref_list(self, kind: str, entity_id: str) -> list[ChildRef] | None:
        if kind == "layer" and entity_id in self.root_page_ids:
            return None
        if (
            kind == "object"
            and isinstance(self.objects.get(entity_id), VectorFillObject)
        ):
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
        if (
            kind == "object"
            and isinstance(self.objects.get(entity_id), VectorFillObject)
        ):
            if new_parent_id is None:
                raise ValueError("Vector fills require their existing owner")
            self.reorder_vector_fill(new_parent_id, entity_id, index)
            return
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
        if new_parent.layer_kind == "fill":
            raise ValueError("Leaf layers cannot contain entities")
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
            if (
                isinstance(entity, GradientObject)
                and self.gradient_children(
                    new_parent_id, entity.field_type,
                    excluding=entity.object_id,
                )
            ):
                raise ValueError(
                    f"This shape already has a {entity.field_type} gradient"
                )
        old_world = (
            self.layer_world_translation(old_parent.layer_id)
            if kind == "object" else (0.0, 0.0)
        )
        new_world = (
            self.layer_world_translation(new_parent_id)
            if kind == "object" else (0.0, 0.0)
        )
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
            if isinstance(entity, GradientObject):
                dx = old_world[0] - new_world[0]
                dy = old_world[1] - new_world[1]
                for contour in entity.line_field.geometry.iter_contours():
                    for node in contour.nodes:
                        node.x += dx
                        node.y += dy
                        if node.incoming is not None:
                            node.incoming = (
                                node.incoming[0] + dx,
                                node.incoming[1] + dy,
                            )
                        if node.outgoing is not None:
                            node.outgoing = (
                                node.outgoing[0] + dx,
                                node.outgoing[1] + dy,
                            )
                radial = entity.radial_field
                radial.origin_x += dx
                radial.origin_y += dy
                if radial.manual_center is not None:
                    radial.manual_center = (
                        radial.manual_center[0] + dx,
                        radial.manual_center[1] + dy,
                    )
                shape = entity.shape_field
                if shape.manual_center is not None:
                    shape.manual_center = (
                        shape.manual_center[0] + dx,
                        shape.manual_center[1] + dy,
                    )
                entity.touch_revision()
            if isinstance(entity, VectorDrawingObject):
                for fill in self.vector_fill_children(entity.object_id):
                    fill.parent_layer_id = new_parent_id

    def delete_entity(self, kind: str, entity_id: str) -> set[str]:
        deleted_objects: set[str] = set()
        if kind == "object":
            obj = self.objects[entity_id]
            if isinstance(obj, VectorFillObject):
                owner = self.objects.get(obj.owner_drawing_id)
                if isinstance(owner, VectorDrawingObject):
                    owner.fill_child_ids = [
                        item for item in owner.fill_child_ids
                        if item != entity_id
                    ]
                    owner.touch_revision()
                del self.objects[entity_id]
                deleted_objects.add(entity_id)
                return deleted_objects
            if isinstance(obj, VectorDrawingObject):
                for fill_id in list(obj.fill_child_ids):
                    deleted_objects.update(
                        self.delete_entity("object", fill_id)
                    )
            self.objects.pop(entity_id)
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

    def closest_compound_ancestor(
        self, layer_id: str, include_self: bool = False,
    ) -> LayerNode | None:
        cursor: str | None = layer_id if include_self else self.layers[layer_id].parent_id
        while cursor:
            layer = self.layers[cursor]
            if layer.compound_enabled:
                return layer
            cursor = layer.parent_id
        return None

    def contributing_compound_ancestor(
        self, layer_id: str,
    ) -> LayerNode | None:
        cursor = self.layers[layer_id]
        while cursor.parent_id:
            if cursor.compound_operation == "ignore":
                return None
            parent = self.layers[cursor.parent_id]
            if parent.compound_enabled:
                return parent
            cursor = parent
        return None

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
                if isinstance(obj, VectorDrawingObject):
                    for fill in self.vector_fill_children(obj.object_id):
                        if fill.opacity_locked:
                            fill.opacity = layer.opacity

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
                    obj = self.objects[child.entity_id]
                    if isinstance(obj, VectorDrawingObject):
                        for fill_id in reversed(obj.fill_child_ids):
                            yield "object", self.objects[fill_id]
                    yield "object", obj
        for page_id in reversed(self.root_page_ids):
            yield from walk(self.layers[page_id])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "id": self.chapter_id,
            "name": self.name, "size": [self.width, self.height],
            "background": self.background, "grid": self.grid.to_dict(),
            "root_page_ids": list(self.root_page_ids),
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
class PaletteSwatch:
    swatch_id: str = field(default_factory=new_id)
    color: str = "#FF000000"

    def to_dict(self) -> dict[str, str]:
        self.color = canonical_argb(self.color)
        return {"id": self.swatch_id, "color": self.color}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str) -> "PaletteSwatch":
        if isinstance(data, str):
            return cls(color=canonical_argb(data))
        return cls(
            swatch_id=str(data.get("id") or new_id()),
            color=canonical_argb(data.get("color", "#FF000000")),
        )


@dataclass
class ColorPalette:
    palette_id: str = field(default_factory=new_id)
    name: str = "Default"
    swatches: list[PaletteSwatch] = field(default_factory=list)

    def validate(self) -> None:
        self.name = str(self.name).strip() or "Palette"
        ids: set[str] = set()
        for swatch in self.swatches:
            if swatch.swatch_id in ids:
                swatch.swatch_id = new_id()
            ids.add(swatch.swatch_id)
            swatch.color = canonical_argb(swatch.color)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.palette_id,
            "name": self.name,
            "swatches": [swatch.to_dict() for swatch in self.swatches],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ColorPalette":
        raw_swatches = data.get("swatches", data.get("colors", []))
        result = cls(
            palette_id=str(data.get("id") or new_id()),
            name=str(data.get("name", "Palette")),
            swatches=[
                PaletteSwatch.from_dict(item) for item in raw_swatches
            ],
        )
        result.validate()
        return result


@dataclass
class ColorGradientRampPreset:
    preset_id: str = field(default_factory=new_id)
    name: str = "Default"
    ramp: ColorGradientRamp = field(default_factory=ColorGradientRamp)

    def validate(self) -> None:
        self.name = str(self.name).strip() or "Gradient"
        self.ramp.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "id": self.preset_id,
            "name": self.name,
            "ramp": self.ramp.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any],
    ) -> "ColorGradientRampPreset":
        result = cls(
            preset_id=str(data.get("id") or new_id()),
            name=str(data.get("name", "Gradient")),
            ramp=ColorGradientRamp.from_dict(data.get("ramp")),
        )
        result.validate()
        return result


def default_gradient_ramp_preset(
    primary: str = "#FF000000", secondary: str = "#FFFFFFFF",
) -> ColorGradientRampPreset:
    return ColorGradientRampPreset(
        name="Default",
        ramp=ColorGradientRamp(stops=[
            ColorGradientStop(position=0.0, color=canonical_argb(primary)),
            ColorGradientStop(position=1.0, color=canonical_argb(secondary)),
        ]),
    )


def default_color_palette() -> ColorPalette:
    return ColorPalette(
        name="Default",
        swatches=[
            PaletteSwatch(color="#FF000000"),
            PaletteSwatch(color="#FFFFFFFF"),
        ],
    )


@dataclass
class SeriesDocument:
    series_id: str = field(default_factory=new_id)
    name: str = "Untitled Series"
    chapters: list[ChapterReference] = field(default_factory=list)
    primary_color: str = "#FF000000"
    secondary_color: str = "#FFFFFFFF"
    palettes: list[ColorPalette] = field(
        default_factory=lambda: [default_color_palette()]
    )
    active_palette_id: str = ""
    gradient_ramp_presets: list[ColorGradientRampPreset] = field(
        default_factory=lambda: [default_gradient_ramp_preset()]
    )
    schema_version: int = SCHEMA_VERSION

    def validate(self) -> None:
        self.primary_color = canonical_argb(self.primary_color)
        self.secondary_color = canonical_argb(
            self.secondary_color, "#FFFFFFFF"
        )
        if not self.palettes:
            self.palettes = [default_color_palette()]
        palette_ids: set[str] = set()
        for palette in self.palettes:
            if palette.palette_id in palette_ids:
                palette.palette_id = new_id()
            palette_ids.add(palette.palette_id)
            palette.validate()
        if self.active_palette_id not in palette_ids:
            self.active_palette_id = self.palettes[0].palette_id
        if not self.gradient_ramp_presets:
            self.gradient_ramp_presets = [
                default_gradient_ramp_preset(
                    self.primary_color, self.secondary_color
                )
            ]
        preset_ids: set[str] = set()
        for preset in self.gradient_ramp_presets:
            if preset.preset_id in preset_ids:
                preset.preset_id = new_id()
            preset_ids.add(preset.preset_id)
            preset.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version, "id": self.series_id,
            "name": self.name, "chapters": [item.to_dict() for item in self.chapters],
            "primary_color": self.primary_color,
            "secondary_color": self.secondary_color,
            "active_palette_id": self.active_palette_id,
            "palettes": [palette.to_dict() for palette in self.palettes],
            "gradient_ramp_presets": [
                preset.to_dict() for preset in self.gradient_ramp_presets
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SeriesDocument":
        schema = int(data.get("schema_version", 1))
        if schema > SCHEMA_VERSION:
            raise ValueError(f"Unsupported future series schema: {schema}")
        palettes = [
            ColorPalette.from_dict(item)
            for item in data.get("palettes", [])
        ]
        primary = canonical_argb(
            data.get("primary_color", data.get("brush_color", "#FF000000"))
        )
        secondary = canonical_argb(
            data.get("secondary_color", "#FFFFFFFF"), "#FFFFFFFF"
        )
        gradient_presets = [
            ColorGradientRampPreset.from_dict(item)
            for item in data.get("gradient_ramp_presets", [])
        ]
        result = cls(
            series_id=str(data["id"]), name=str(data.get("name", "Untitled Series")),
            chapters=[
                ChapterReference(str(item["id"]), str(item.get("name", "Chapter")))
                for item in data.get("chapters", [])
            ],
            primary_color=primary,
            secondary_color=secondary,
            palettes=palettes or [default_color_palette()],
            active_palette_id=str(data.get("active_palette_id", "")),
            gradient_ramp_presets=(
                gradient_presets
                or [default_gradient_ramp_preset(primary, secondary)]
            ),
            schema_version=SCHEMA_VERSION,
        )
        result.validate()
        return result
