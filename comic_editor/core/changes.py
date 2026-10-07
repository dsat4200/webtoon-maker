"""Structured edit notifications and incremental scene dependency generations.

This is an owner-thread index, not another renderer or durable cache identity.
Semantic renderer keys still validate actual inputs. Generations let consumers
skip unrelated preparation; old and new dependency closures survive rebinding.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace


EntityRef = tuple[str, str]
GROUP_KINDS = {"layers": "layer", "objects": "object", "modifiers": "modifier", "masks": "mask"}
KIND_GROUPS = {kind: group for group, kind in GROUP_KINDS.items()}
EDITOR_FIELDS = frozenset({"name", "expanded", "saved"})
LINK_FIELDS = frozenset({"*", "children", "parent_id", "parent_layer_id", "modifier_ids",
                         "opacity_mask", "parameter_masks", "contributors", "target_layer_id"})
DESCENDANT_FIELDS = frozenset({"*", "children", "bound", "shape_style", "translation", "translate_x", "translate_y",
    "transform_frame", "transform_quad", "visible", "opacity", "opacity_mask",
    "modifier_ids", "compound_enabled", "compound_operation", "ignore_parent_mask", "mask_only"})


@dataclass(frozen=True)
class EntityChange:
    entity: EntityRef
    fields: frozenset[str] = frozenset({"*"})
    old_bounds: tuple[float, float, float, float] | None = None
    new_bounds: tuple[float, float, float, float] | None = None
    structural: bool = False

    @property
    def affects_artwork(self):
        return bool(self.fields - EDITOR_FIELDS) or self.structural

    def reversed(self):
        return replace(self, old_bounds=self.new_bounds, new_bounds=self.old_bounds)


@dataclass(frozen=True)
class ResourceChange:
    entity: EntityRef
    resource: str
    address: tuple[int, int] | None = None
    old_generation: object = None
    new_generation: object = None

    def reversed(self):
        return replace(self, old_generation=self.new_generation, new_generation=self.old_generation)


@dataclass(frozen=True)
class OrderChange:
    collection: str
    before: tuple[str, ...]
    after: tuple[str, ...]

    def reversed(self):
        return replace(self, before=self.after, after=self.before)


@dataclass(frozen=True)
class ChangeSet:
    entities: tuple[EntityChange, ...] = ()
    resources: tuple[ResourceChange, ...] = ()
    document_fields: frozenset[str] = frozenset()
    transient: bool = False
    conservative: bool = False
    label: str = ""
    orders: tuple[OrderChange, ...] = ()

    @property
    def committed(self):
        return not self.transient

    @property
    def refs(self):
        return frozenset(item.entity for item in (*self.entities, *self.resources))

    @property
    def empty(self):
        return not (self.entities or self.resources or self.document_fields or self.conservative or self.orders)

    @property
    def structural(self):
        return bool(self.orders or self.document_fields & {"root_page_ids", "id", "schema_version"}
                    or any(item.structural for item in self.entities))

    @property
    def hierarchy_changed(self):
        """Mask/modifier registry edits do not replace drawable tree rows."""
        return bool(self.conservative or self.document_fields & {"root_page_ids", "id", "schema_version"}
                    or any(order.collection in {"layers", "objects"} for order in self.orders)
                    or any(item.structural and item.entity[0] in {"layer", "object"}
                           for item in self.entities))

    def reversed(self):
        return replace(self, entities=tuple(item.reversed() for item in self.entities),
                       resources=tuple(item.reversed() for item in self.resources),
                       orders=tuple(item.reversed() for item in self.orders))

    def with_bounds(self, bounds):
        """Attach owner-computed old/new influence bounds without importing Qt."""
        return replace(self, entities=tuple(replace(item, old_bounds=bounds.get(item.entity, (None, None))[0],
            new_bounds=bounds.get(item.entity, (None, None))[1]) for item in self.entities))


class DependencyIndex:
    """Reverse composition/mask/modifier links, updated from changed records only.

    Composition propagates children to ancestors. Ancestor geometry/style edits
    explicitly seed descendants; it is not a permanent reverse edge, which
    would invalidate all siblings for every raster dab. Cycles are traversed
    safely; the document model remains responsible for rejecting illegal masks.
    """
    def __init__(self, chapter=None):
        self.chapter = None
        self.epoch = 0
        self.revision = 0
        self.generations = defaultdict(int)
        self.dependencies = {}
        self.reverse = defaultdict(set)
        self.children = {}
        self.refresh_count = 0
        if chapter is not None:
            self.bind(chapter)

    def bind(self, chapter):
        self.chapter = chapter
        self.epoch += 1
        self.revision = 0
        self.generations.clear()
        self.dependencies.clear()
        self.reverse.clear()
        self.children.clear()
        if chapter is not None:
            self._rebuild_links()

    def _rebuild_links(self):
        self.dependencies.clear()
        self.reverse.clear()
        self.children.clear()
        for group, kind in GROUP_KINDS.items():
            for identifier in getattr(self.chapter, group):
                self._refresh((kind, identifier))

    def canonical_ref(self, ref):
        # Tile history is shared by raster objects and mask paint; stores do
        # not own a document and cannot classify the identifier themselves.
        if self.chapter is not None and ref[0] == "object" and ref[1] not in self.chapter.objects and ref[1] in self.chapter.masks:
            return ("mask", ref[1])
        return ref

    def _set_dependencies(self, ref, dependencies):
        old = self.dependencies.get(ref, set())
        for source in old - dependencies:
            self.reverse[source].discard(ref)
            if not self.reverse[source]:
                self.reverse.pop(source, None)
        for source in dependencies - old:
            self.reverse[source].add(ref)
        self.dependencies[ref] = dependencies

    def _refresh(self, ref):
        kind, identifier = ref
        record = getattr(self.chapter, KIND_GROUPS[kind]).get(identifier)
        dependencies = set()
        if record is not None:
            dependencies.update(("modifier", mid) for mid in getattr(record, "modifier_ids", ()))
            opacity = getattr(record, "opacity_mask", None)
            if opacity is not None:
                dependencies.add(("mask", opacity.mask_id))
            dependencies.update(("mask", binding.mask_id)
                for binding in getattr(record, "parameter_masks", {}).values())
            if kind == "layer":
                children = tuple((child.kind, child.entity_id) for child in record.children)
                self.children[ref] = children
                dependencies.update(children)
            elif kind == "mask":
                dependencies.update(record.contributors)
            elif kind == "modifier":
                source = getattr(record, "target_layer_id", "")
                if source:
                    dependencies.add(("layer", source))
        elif kind == "layer":
            self.children.pop(ref, None)
        self._set_dependencies(ref, dependencies)
        self.refresh_count += 1

    def _descendants(self, ref):
        found, todo = set(), list(self.children.get(ref, ()))
        while todo:
            child = todo.pop()
            if child in found:
                continue
            found.add(child)
            todo.extend(self.children.get(child, ()))
        return found

    def affected(self, change):
        if change.conservative or change.document_fields - EDITOR_FIELDS or change.orders:
            return set(self.dependencies)
        seeds = {self.canonical_ref(item.entity) for item in change.entities if item.affects_artwork}
        seeds.update(self.canonical_ref(item.entity) for item in change.resources)
        for item in change.entities:
            if item.entity[0] == "layer" and item.fields & DESCENDANT_FIELDS:
                seeds.update(self._descendants(item.entity))
        geometry_edit = any((item.entity[0] == "layer" and item.fields & DESCENDANT_FIELDS)
                            or (item.entity[0] == "modifier" and item.affects_artwork)
                            for item in change.entities)
        found, todo = set(), list(seeds)
        while todo:
            ref = todo.pop()
            if ref in found:
                continue
            found.add(ref)
            todo.extend(self.reverse.get(ref, ()))
            if geometry_edit and ref[0] == "layer":
                layer = self.chapter.layers.get(ref[1])
                if layer is not None and layer.compound_enabled:
                    todo.extend(self._descendants(ref))
        return found

    def publish(self, change, chapter=None):
        if chapter is not None and chapter is not self.chapter:
            self.bind(chapter)
        if self.chapter is None:
            return set()
        # Preserve old dependencies for removals and remapped sources.
        affected = self.affected(change)
        if change.conservative:
            self._rebuild_links()
        else:
            for item in change.entities:
                if item.structural or item.fields & LINK_FIELDS:
                    self._refresh(item.entity)
        affected.update(self.affected(change))
        self.revision += 1
        for ref in affected:
            self.generations[ref] += 1
        return affected

    def generation(self, kind, identifier):
        return self.generations[(kind, identifier)]

    def signature(self, refs):
        return (self.epoch, tuple((ref, self.generations[ref]) for ref in sorted(refs)))
