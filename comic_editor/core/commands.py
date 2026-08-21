"""Undo/redo commands, including sparse raster tile patches."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from PySide6.QtGui import QImage

from .models import object_from_dict


class Command(Protocol):
    label: str
    def redo(self) -> None: ...
    def undo(self) -> None: ...


@dataclass
class CallbackCommand:
    label: str
    redo_callback: Callable[[], None]
    undo_callback: Callable[[], None]

    def redo(self) -> None:
        self.redo_callback()

    def undo(self) -> None:
        self.undo_callback()


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

    def _apply(
        self, values: dict[tuple[int, int], QImage | None], state: object,
    ) -> None:
        for key, image in values.items():
            self.tile_store.set_tile(self.object_id, key, image)
        if self.state_callback is not None:
            self.state_callback(state)
        if self.changed_callback:
            self.changed_callback()

    def redo(self) -> None:
        self._apply(self.after, self.after_state)

    def undo(self) -> None:
        self._apply(self.before, self.before_state)


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


class CommandStack:
    def __init__(self, limit: int = 200) -> None:
        self.limit = max(1, int(limit))
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._revision = 0
        self.changed_callback: Callable[[], None] | None = None

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
        if not already_done:
            command.redo()
        self._undo.append(command)
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        self._revision += 1
        self._notify()

    def undo(self) -> None:
        if not self._undo:
            return
        command = self._undo.pop()
        command.undo()
        self._redo.append(command)
        self._revision += 1
        self._notify()

    def redo(self) -> None:
        if not self._redo:
            return
        command = self._redo.pop()
        command.redo()
        self._undo.append(command)
        self._revision += 1
        self._notify()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._revision += 1
        self._notify()

    def _notify(self) -> None:
        if self.changed_callback:
            self.changed_callback()

