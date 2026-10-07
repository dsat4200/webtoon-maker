"""Undo/redo commands, including sparse raster tile patches."""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol

from PySide6.QtGui import QImage

from .models import object_from_dict
from .tile_history import HistoryTileMap, TileHistoryCache
from .changes import ChangeSet, EntityChange, ResourceChange


class Command(Protocol):
    label: str
    def redo(self) -> None: ...
    def undo(self) -> None: ...


@dataclass
class CallbackCommand:
    label: str
    redo_callback: Callable[[], None]
    undo_callback: Callable[[], None]
    forward_change: ChangeSet | None = None
    backward_change: ChangeSet | None = None

    def redo(self) -> None:
        self.redo_callback()

    def undo(self) -> None:
        self.undo_callback()

    def change_set(self, action="redo"):
        return self.backward_change if action == "undo" else self.forward_change


@dataclass
class TilePatchCommand:
    label: str
    tile_store: object
    object_id: str
    before: dict[tuple[int, int], QImage | None]
    after: dict[tuple[int, int], QImage | None]
    changed_callback: Callable[[], None] | None = None
    before_state: object | None = None
    after_state: object | None = None
    state_callback: Callable[[object], None] | None = None
    forward_change: ChangeSet | None = None

    def __post_init__(self):
        cache = getattr(self.tile_store, '_history_cache', None)
        if cache is None:
            cache = self.tile_store._history_cache = TileHistoryCache()
        self.before = HistoryTileMap(cache, self.before)
        self.after = HistoryTileMap(cache, self.after)

    def _apply(
        self, values: dict[tuple[int, int], QImage | None], state: object,
    ) -> None:
        for key, image, bounds in values.items_with_bounds(self.tile_store._alpha_bbox):
            self.tile_store.set_tile(self.object_id, key, image, _known_alpha_bounds=bounds)
        if self.state_callback is not None:
            self.state_callback(state)
        if self.changed_callback:
            self.changed_callback()

    def redo(self) -> None:
        self._apply(self.after, self.after_state)

    def undo(self) -> None:
        self._apply(self.before, self.before_state)

    def change_set(self, action="redo"):
        values = self.before if action == "undo" else self.after
        owner = self.tile_store._tiles.get(self.object_id)
        resources = tuple(ResourceChange(("object", self.object_id), "raster", key,
            new_generation=owner.version(key) if owner is not None else None) for key in values)
        fields = frozenset({"pixels", "interaction_rect"}) if self.state_callback is not None else frozenset({"pixels"})
        if self.forward_change is not None:
            change = self.forward_change.reversed() if action == "undo" else self.forward_change
            # Undo/redo changes source generations. Publish the generation
            # actually installed, while retaining the command's oriented bounds.
            return replace(change, resources=resources)
        return ChangeSet((EntityChange(("object", self.object_id), fields),), resources, label=self.label)


@dataclass
class ObjectPatchCommand:
    """Undo a focused set of object records without snapshotting a chapter."""

    label: str
    chapter: object
    before: dict[str, dict[str, Any] | None]
    after: dict[str, dict[str, Any] | None]
    changed_callback: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        self.before = copy.deepcopy(self.before)
        self.after = copy.deepcopy(self.after)

    def _apply(self, values: dict[str, dict[str, Any] | None]) -> None:
        for object_id, payload in values.items():
            if payload is None:
                self.chapter.objects.pop(object_id, None)
                continue
            replacement = object_from_dict(copy.deepcopy(payload))
            current = self.chapter.objects.get(object_id)
            if current is not None and type(current) is type(replacement):
                current.__dict__.clear()
                current.__dict__.update(replacement.__dict__)
            else:
                self.chapter.objects[object_id] = replacement
        if self.changed_callback:
            self.changed_callback()

    def redo(self) -> None:
        self._apply(self.after)

    def undo(self) -> None:
        self._apply(self.before)

    def change_set(self, action="redo"):
        old, new = (self.after, self.before) if action == "undo" else (self.before, self.after)
        changes = []
        for identifier in old.keys() | new.keys():
            before, after = old.get(identifier), new.get(identifier)
            fields = (frozenset({"*"}) if before is None or after is None else
                      frozenset(name for name in before.keys() | after.keys() if before.get(name) != after.get(name)))
            if fields:
                changes.append(EntityChange(("object", identifier), fields,
                    structural=before is None or after is None or bool(fields & {"parent_layer_id", "type"})))
        return ChangeSet(tuple(changes), label=self.label)


class CommandStack:
    def __init__(self, limit: int = 200) -> None:
        self.limit = max(1, int(limit))
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._revision = 0
        self.read_only = False
        self.changed_callback: Callable[[], None] | None = None
        self.change_callback: Callable[[ChangeSet, str, int], None] | None = None
        self.applying_change: ChangeSet | None = None

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def top_undo_command(self) -> Command | None:
        """Return the latest undo command without exposing history storage."""
        return self._undo[-1] if self._undo else None

    @property
    def revision(self) -> int:
        return self._revision

    def push(self, command: Command, already_done: bool = False) -> None:
        if self.read_only:
            return
        if not already_done:
            self._apply(command, "redo")
        self._undo.append(command)
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        self._revision += 1
        self._notify(command, "push")

    def undo(self) -> None:
        if self.read_only or not self._undo:
            return
        command = self._undo[-1]
        self._apply(command, "undo")
        self._undo.pop()
        self._redo.append(command)
        self._revision += 1
        self._notify(command, "undo")

    def redo(self) -> None:
        if self.read_only or not self._redo:
            return
        command = self._redo[-1]
        self._apply(command, "redo")
        self._redo.pop()
        self._undo.append(command)
        self._revision += 1
        self._notify(command, "redo")

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._revision += 1
        self._notify()

    def _notify(self, command=None, action="clear") -> None:
        if self.change_callback is not None:
            self.change_callback(self._describe(command, action), action, self._revision)
        if self.changed_callback:
            self.changed_callback()

    @staticmethod
    def _describe(command, action):
        describe = getattr(command, "change_set", None)
        change = describe(action) if describe is not None else None
        return change if change is not None else ChangeSet(
            conservative=True, label=getattr(command, "label", ""))

    def _apply(self, command, action):
        # Legacy callbacks notify observers during mutation. Let those
        # observers defer broad invalidation until the typed post-change event.
        previous = self.applying_change
        self.applying_change = self._describe(command, action)
        try:
            getattr(command, action)()
        finally:
            self.applying_change = previous

