"""Extensible registries for object and bound behavior."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .models import BoundGeometry, DocumentObject, object_from_dict


@dataclass(frozen=True)
class ObjectTypeSpec:
    type_name: str
    loader: Callable[[dict], DocumentObject]
    raster_tools: bool = False
    contextual_tools: tuple[str, ...] = ()
    asset_ready: bool = True


@dataclass(frozen=True)
class BoundTypeSpec:
    type_name: str
    loader: Callable[[dict], BoundGeometry]
    minimum_points: int


class ObjectTypeRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ObjectTypeSpec] = {}

    def register(self, spec: ObjectTypeSpec) -> None:
        if spec.type_name in self._specs:
            raise ValueError(f"Object type already registered: {spec.type_name}")
        self._specs[spec.type_name] = spec

    def spec(self, type_name: str) -> ObjectTypeSpec:
        return self._specs[type_name]

    def load(self, data: dict[str, Any]) -> DocumentObject:
        type_name = str(data.get("type", "object"))
        spec = self._specs.get(type_name)
        return spec.loader(data) if spec else object_from_dict(data)


class BoundTypeRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, BoundTypeSpec] = {}

    def register(self, spec: BoundTypeSpec) -> None:
        if spec.type_name in self._specs:
            raise ValueError(f"Bound type already registered: {spec.type_name}")
        self._specs[spec.type_name] = spec

    def spec(self, type_name: str) -> BoundTypeSpec:
        return self._specs[type_name]


OBJECT_TYPES = ObjectTypeRegistry()
OBJECT_TYPES.register(ObjectTypeSpec("raster", object_from_dict, True, ("pencil", "eraser")))
OBJECT_TYPES.register(ObjectTypeSpec("text", object_from_dict, False, ("text", "transform")))
OBJECT_TYPES.register(ObjectTypeSpec(
    "vector_drawing", object_from_dict, False,
    ("pencil", "eraser", "fill", "vector_edit"),
))
OBJECT_TYPES.register(ObjectTypeSpec(
    "vector_fill", object_from_dict, False, ("fill", "object_select"),
))

BOUND_TYPES = BoundTypeRegistry()
BOUND_TYPES.register(BoundTypeSpec("path", BoundGeometry.from_dict, 2))
