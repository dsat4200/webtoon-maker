"""Undo/redo commands, including sparse raster tile patches."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from PySide6.QtGui import QImage


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

    def _apply(self, values: dict[tuple[int, int], QImage | None]) -> None:
        for key, image in values.items():
            self.tile_store.set_tile(self.object_id, key, image)
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
        self.changed_callback: Callable[[], None] | None = None

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def push(self, command: Command, already_done: bool = False) -> None:
        if not already_done:
            command.redo()
        self._undo.append(command)
        if len(self._undo) > self.limit:
            self._undo.pop(0)
        self._redo.clear()
        self._notify()

    def undo(self) -> None:
        if not self._undo:
            return
        command = self._undo.pop()
        command.undo()
        self._redo.append(command)
        self._notify()

    def redo(self) -> None:
        if not self._redo:
            return
        command = self._redo.pop()
        command.redo()
        self._undo.append(command)
        self._notify()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._notify()

    def _notify(self) -> None:
        if self.changed_callback:
            self.changed_callback()

