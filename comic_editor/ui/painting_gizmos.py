"""Bounded, transient command retention for stable editing gizmos.

Commands replay the original QPainter overloads on the current presentation
device. No pixels, paint devices, artwork, or native GPU handles are retained.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from PySide6.QtCore import QLine, QLineF, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygon, QPolygonF,
    QRegion, QTransform,
)


MAX_GIZMO_COMMANDS = 512
MAX_GIZMO_BYTES = 8 * 1024 * 1024

_DRAW_METHODS = frozenset({
    'drawArc', 'drawChord', 'drawEllipse', 'drawLine', 'drawLines', 'drawPath',
    'drawPie', 'drawPoint', 'drawPoints', 'drawPolygon', 'drawPolyline',
    'drawRect', 'drawRects', 'drawRoundedRect', 'drawText', 'fillPath',
    'fillRect', 'strokePath',
})
_STATE_METHODS = frozenset({
    'save', 'restore', 'setBackground', 'setBackgroundMode', 'setBrush',
    'setBrushOrigin', 'setClipPath', 'setClipRect', 'setClipRegion',
    'setClipping', 'setCompositionMode', 'setFont', 'setLayoutDirection',
    'setOpacity', 'setPen', 'setRenderHint', 'setRenderHints', 'setTransform',
    'setViewTransformEnabled', 'setViewport', 'setWindow', 'setWorldTransform',
    'setWorldMatrixEnabled', 'resetTransform', 'translate', 'rotate', 'scale', 'shear',
})
_QUERY_METHODS = frozenset({
    'background', 'backgroundMode', 'brush', 'brushOrigin', 'clipBoundingRect',
    'clipPath', 'clipRegion', 'combinedTransform', 'compositionMode', 'device',
    'deviceTransform', 'font', 'fontInfo', 'fontMetrics', 'hasClipping',
    'isActive', 'layoutDirection', 'opacity', 'pen', 'renderHints',
    'testRenderHint', 'transform', 'viewTransformEnabled', 'viewport',
    'window', 'worldMatrixEnabled', 'worldTransform',
})
_SMALL_VALUES = (QColor, QTransform, QPoint, QPointF, QRect, QRectF,
                 QSize, QSizeF, QLine, QLineF)


class _Declined(Exception):
    pass


def _value_bytes(value: object) -> int:
    """Conservatively count geometry before copying potentially large values."""
    if value is None or isinstance(value, (bool, int, float, Enum)):
        return 32
    if isinstance(value, str):
        return 64 + 4 * len(value)
    if isinstance(value, (tuple, list)):
        total = 64 + 16 * len(value)
        if total > MAX_GIZMO_BYTES:
            raise _Declined
        for item in value:
            total += _value_bytes(item)
            if total > MAX_GIZMO_BYTES:
                raise _Declined
        return total
    if isinstance(value, QPainterPath):
        return 128 + 32 * value.elementCount()
    if isinstance(value, (QPolygon, QPolygonF)):
        return 128 + 32 * value.size()
    if isinstance(value, QRegion):
        return 128 + 32 * value.rectCount()
    if isinstance(value, QBrush):
        if value.style() == Qt.BrushStyle.TexturePattern:
            raise _Declined
        gradient = value.gradient()
        return 256 + (64 * len(gradient.stops()) if gradient is not None else 0)
    if isinstance(value, QPen):
        return 256 + _value_bytes(value.brush()) + 32 * len(value.dashPattern())
    if isinstance(value, QFont):
        return 256 + 4 * len(value.toString()) + _value_bytes(value.families())
    if isinstance(value, _SMALL_VALUES):
        return 256
    # Images, pixmaps, paint devices, QObject/native handles, and new argument
    # types must take the original direct drawing path until qualified.
    raise _Declined


def _copy_value(value: object) -> object:
    if isinstance(value, (tuple, list)):
        return type(value)(_copy_value(item) for item in value)
    if isinstance(value, (QPainterPath, QPolygon, QPolygonF, QRegion, QBrush, QPen, QFont,
                          *_SMALL_VALUES)):
        return type(value)(value)
    return value  # Only immutable values reach here after _value_bytes.


@dataclass(frozen=True, slots=True)
class PaintingGizmoCommands:
    _commands: tuple[tuple[str, tuple, tuple], ...]
    byte_count: int

    @property
    def command_count(self) -> int:
        return len(self._commands)

    def replay(self, painter: QPainter) -> None:
        """Replay identical primitive overloads without changing caller state."""
        depth = 0
        painter.save()
        try:
            for name, args, keyword_items in self._commands:
                getattr(painter, name)(*args, **dict(keyword_items))
                if name == 'save':
                    depth += 1
                elif name == 'restore':
                    depth -= 1
        finally:
            while depth:
                painter.restore()
                depth -= 1
            painter.restore()


class _Recorder:
    def __init__(self, painter: QPainter):
        self._painter = painter
        self.commands: list[tuple[str, tuple, tuple]] = []
        self.byte_count = 0
        self.depth = 0
        self.declined = False

    def _decline(self):
        self.declined = True
        raise _Declined

    def __getattr__(self, name: str):
        if name in _QUERY_METHODS:
            return getattr(self._painter, name)
        if name not in _STATE_METHODS and name not in _DRAW_METHODS:
            self._decline()

        def record(*args, **kwargs):
            if len(self.commands) >= MAX_GIZMO_COMMANDS:
                self._decline()
            if name == 'restore' and self.depth == 0:
                self._decline()
            try:
                cost = 192 + sum(_value_bytes(value) for value in args)
                cost += sum(_value_bytes(key) + _value_bytes(value)
                            for key, value in kwargs.items())
                if self.byte_count + cost > MAX_GIZMO_BYTES:
                    self._decline()
                copied_args = tuple(_copy_value(value) for value in args)
                copied_keywords = tuple((key, _copy_value(value)) for key, value in kwargs.items())
            except _Declined:
                self._decline()
            self.commands.append((name, copied_args, copied_keywords))
            self.byte_count += cost
            if name == 'save':
                self.depth += 1
            elif name == 'restore':
                self.depth -= 1
            if name in _STATE_METHODS:
                return getattr(self._painter, name)(*args, **kwargs)
            return None

        return record


def record_painting_gizmos(
        painter: QPainter, draw: Callable[[QPainter], None],
) -> PaintingGizmoCommands | None:
    """Record geometry with the actual painter's state and readonly queries.

    The callback does not paint. Unsupported operations, unbalanced state,
    inactive painters, or either budget decline retention; the caller should
    then invoke its existing direct drawing callback. Callback errors propagate.
    """
    if not painter.isActive():
        return None
    recorder = _Recorder(painter)
    painter.save()
    try:
        try:
            draw(recorder)
        except _Declined:
            return None
        if recorder.declined or recorder.depth:
            return None
        return PaintingGizmoCommands(tuple(recorder.commands), recorder.byte_count)
    finally:
        while recorder.depth:
            painter.restore()
            recorder.depth -= 1
        painter.restore()
