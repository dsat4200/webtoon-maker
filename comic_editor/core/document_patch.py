"""Detached record patches for metadata history, independent of raster pixels."""
from dataclasses import dataclass, field
import copy

from .models import GridSettings, LayerNode, ToneMask, modifier_from_dict, object_from_dict
from .pixel_contract import PixelContract


FACTORIES = {'layers': LayerNode.from_dict, 'objects': object_from_dict,
             'modifiers': modifier_from_dict, 'masks': ToneMask.from_dict}


def scalar_value(chapter, name):
    if name == 'id':
        return chapter.chapter_id
    if name == 'size':
        return [chapter.width, chapter.height]
    if name == 'grid':
        return chapter.grid.to_dict()
    if name == 'pixel_contract':
        return chapter.pixel_contract.to_dict()
    value = getattr(chapter, name)
    if name == 'export_rect' and value is not None:
        return list(value)
    return copy.deepcopy(value)


@dataclass
class RecordSnapshot:
    records: dict
    scalars: dict = field(default_factory=dict)
    selections: dict = field(default_factory=dict, compare=False)
    attributes: dict = field(default_factory=dict, compare=False)
    orders: dict = field(default_factory=dict, compare=False)
    document_identity: int = field(default=0, compare=False)

    @classmethod
    def capture(cls, chapter, *, scalars=(), attributes=None, **groups):
        records = {}
        selections = {}
        attributes = attributes or {}
        orders = {}
        for group, identifiers in groups.items():
            collection = getattr(chapter, group)
            orders[group] = tuple(collection)
            selection = None if identifiers is None else tuple(identifiers)
            selections[group] = selection
            identifiers = tuple(collection) if selection is None else selection
            records[group] = {}
            for identifier in identifiers:
                if identifier not in collection:
                    records[group][identifier] = None
                elif group in attributes:
                    records[group][identifier] = {name: copy.deepcopy(getattr(collection[identifier], name))
                                                  for name in attributes[group]}
                else:
                    records[group][identifier] = copy.deepcopy(collection[identifier].to_dict())
        return cls(records, {name: scalar_value(chapter, name) for name in scalars},
                   selections, attributes, orders, id(chapter))

    def after(self, chapter):
        groups = {group: (None if selection is None else tuple(set(selection) | set(self.records[group])))
                  for group, selection in self.selections.items()}
        result = self.capture(chapter, scalars=self.scalars, attributes=self.attributes, **groups)
        # Removed records remain explicit tombstones even for an all-record
        # selection, so the after snapshot fully describes both directions.
        for group, records in self.records.items():
            for identifier in records:
                result.records[group].setdefault(identifier, None)
        return result


@dataclass
class DocumentPatch:
    records: dict
    scalars: dict
    orders: dict = field(default_factory=dict)
    partial_groups: frozenset = frozenset()

    @classmethod
    def pair(cls, before, after):
        """Compile complete serialized states or focused record snapshots."""
        if isinstance(before, RecordSnapshot):
            if before.document_identity != after.document_identity:
                raise ValueError('History snapshots belong to different documents')
            old, new = before.records, after.records
            old_scalars, new_scalars = before.scalars, after.scalars
            old_order = {group: ids for group, ids in before.orders.items() if ids != after.orders[group]}
            orders = old_order, {group: after.orders[group] for group in old_order}
            partial_groups = frozenset(before.attributes)
        else:
            partial_groups = frozenset()
            if before.get('id') != after.get('id') or before.get('schema_version') != after.get('schema_version'):
                return None
            old = {group: {item['id']: item for item in before.get(group, [])} for group in FACTORIES}
            new = {group: {item['id']: item for item in after.get(group, [])} for group in FACTORIES}
            old_scalars = {key: value for key, value in before.items() if key not in FACTORIES}
            new_scalars = {key: value for key, value in after.items() if key not in FACTORIES}
            old_order = {group: tuple(records) for group, records in old.items()
                         if tuple(records) != tuple(new[group])}
            new_order = {group: tuple(new[group]) for group in old_order}
            orders = old_order, new_order
        old_records, new_records = {}, {}
        for group in old.keys() | new.keys():
            previous, current = old.get(group, {}), new.get(group, {})
            changed = {identifier for identifier in previous.keys() | current.keys()
                       if previous.get(identifier) != current.get(identifier)}
            if changed:
                old_records[group] = {identifier: copy.deepcopy(previous.get(identifier)) for identifier in changed}
                new_records[group] = {identifier: copy.deepcopy(current.get(identifier)) for identifier in changed}
        changed = {key for key in old_scalars.keys() | new_scalars.keys()
                   if old_scalars.get(key) != new_scalars.get(key)}
        return (cls(old_records, {key: copy.deepcopy(old_scalars.get(key)) for key in changed}, orders[0], partial_groups),
                cls(new_records, {key: copy.deepcopy(new_scalars.get(key)) for key in changed}, orders[1], partial_groups))

    @property
    def empty(self):
        return not (self.records or self.scalars or self.orders)

    def apply(self, chapter):
        for group, records in self.records.items():
            collection = getattr(chapter, group)
            for identifier, payload in records.items():
                if payload is None:
                    collection.pop(identifier, None)
                    continue
                if group in self.partial_groups:
                    current = collection[identifier]
                    for name, value in payload.items():
                        setattr(current, name, copy.deepcopy(value))
                    continue
                replacement = FACTORIES[group](copy.deepcopy(payload))
                current = collection.get(identifier)
                if current is not None and type(current) is type(replacement):
                    current.__dict__.clear()
                    current.__dict__.update(replacement.__dict__)
                else:
                    collection[identifier] = replacement
        for group, identifiers in self.orders.items():
            collection = getattr(chapter, group)
            reordered = {identifier: collection[identifier] for identifier in identifiers if identifier in collection}
            reordered.update({identifier: value for identifier, value in collection.items() if identifier not in reordered})
            collection.clear()
            collection.update(reordered)
        for key, value in self.scalars.items():
            value = copy.deepcopy(value)
            if key == 'id':
                chapter.chapter_id = value
            elif key == 'size':
                chapter.width, chapter.height = value
            elif key == 'grid':
                chapter.grid = GridSettings.from_dict(value)
            elif key == 'pixel_contract':
                chapter.pixel_contract = PixelContract.from_dict(value)
            elif key == 'export_rect':
                chapter.export_rect = tuple(value) if value is not None else None
            else:
                setattr(chapter, key, value)
        chapter.validate()
