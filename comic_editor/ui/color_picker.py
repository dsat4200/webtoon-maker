"""Reusable ARGB color controls used by the editor and its ribbon.

Persistence deliberately lives outside this module.  The widgets exchange
stable palette/swatch IDs and canonical ``#AARRGGBB`` strings through signals,
which keeps them straightforward to use with both dataclasses and dictionaries.
"""
from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QConicalGradient,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


def qcolor_from_argb(
    value: str | QColor | int | None,
    default: str | QColor = "#FF000000",
) -> QColor:
    """Return a valid QColor from an ARGB string, QColor, or QRgb integer."""

    if isinstance(value, QColor):
        color = QColor(value)
    elif isinstance(value, int):
        color = QColor.fromRgba(value)
    else:
        text = "" if value is None else str(value).strip()
        if text and not text.startswith("#") and len(text) in (6, 8):
            text = f"#{text}"
        color = QColor(text)
    if color.isValid():
        return color

    fallback = QColor(default) if not isinstance(default, QColor) else QColor(default)
    return fallback if fallback.isValid() else QColor(0, 0, 0, 255)


def canonical_argb(
    value: str | QColor | int | None,
    default: str | QColor = "#FF000000",
) -> str:
    """Normalize a color to uppercase Qt ``#AARRGGBB`` form."""

    return qcolor_from_argb(value, default).name(
        QColor.NameFormat.HexArgb
    ).upper()


def _paint_checkerboard(
    painter: QPainter, rect: QRectF, cell_size: float = 6.0
) -> None:
    """Paint a clipped gray/white alpha checkerboard."""

    painter.save()
    painter.setClipRect(rect)
    left = int(math.floor(rect.left() / cell_size))
    right = int(math.ceil(rect.right() / cell_size))
    top = int(math.floor(rect.top() / cell_size))
    bottom = int(math.ceil(rect.bottom() / cell_size))
    colors = (QColor(224, 224, 224), QColor(158, 158, 158))
    painter.setPen(Qt.PenStyle.NoPen)
    for row in range(top, bottom + 1):
        for column in range(left, right + 1):
            painter.setBrush(colors[(row + column) & 1])
            painter.drawRect(
                QRectF(
                    column * cell_size,
                    row * cell_size,
                    cell_size + 0.5,
                    cell_size + 0.5,
                )
            )
    painter.restore()


class HsvAlphaPicker(QWidget):
    """Hue ring with an inner SV square and vertical alpha strip."""

    colorChanged = Signal(str)
    interactionStarted = Signal()
    interactionFinished = Signal(str)

    def __init__(
        self,
        color: str | QColor = "#FF000000",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("hsvAlphaPicker")
        self.setMinimumSize(120, 120)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._color = qcolor_from_argb(color)
        hue, saturation, value, alpha = self._color.getHsvF()
        self._hue = hue if hue >= 0 else 0.0
        self._saturation = max(0.0, saturation)
        self._value = max(0.0, value)
        self._alpha = max(0.0, alpha)
        self._drag_mode: str | None = None

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(260, 260)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(120, 120)

    def color(self) -> QColor:
        return QColor(self._color)

    def color_argb(self) -> str:
        return canonical_argb(self._color)

    def setColor(
        self, color: str | QColor, *, emit: bool = False
    ) -> None:  # noqa: N802
        incoming = qcolor_from_argb(color)
        hue, saturation, value, alpha = incoming.getHsvF()
        if hue >= 0:
            self._hue = hue
        self._saturation = max(0.0, saturation)
        self._value = max(0.0, value)
        self._alpha = max(0.0, alpha)
        previous = canonical_argb(self._color)
        self._sync_color()
        self.update()
        if emit and canonical_argb(self._color) != previous:
            self.colorChanged.emit(canonical_argb(self._color))

    def set_color(
        self, color: str | QColor, *, emit: bool = False
    ) -> None:
        self.setColor(color, emit=emit)

    def hue_ring_bounds(self) -> tuple[QRectF, QRectF]:
        outer, inner, _sv, _alpha = self._layout_geometry()
        return outer, inner

    def sv_rect(self) -> QRectF:
        return self._layout_geometry()[2]

    def alpha_rect(self) -> QRectF:
        return self._layout_geometry()[3]

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer, inner, sv_rect, alpha_rect = self._layout_geometry()
        center = outer.center()

        hue_gradient = QConicalGradient(center, 0)
        for step in range(13):
            fraction = step / 12
            hue_gradient.setColorAt(
                fraction, QColor.fromHsvF((1.0 - fraction) % 1.0, 1, 1)
            )
        ring = QPainterPath()
        ring.setFillRule(Qt.FillRule.OddEvenFill)
        ring.addEllipse(outer)
        ring.addEllipse(inner)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(hue_gradient))
        painter.drawPath(ring)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(40, 40, 44), 1.5))
        painter.drawEllipse(outer)
        painter.drawEllipse(inner)

        hue_color = QColor.fromHsvF(self._hue, 1.0, 1.0)
        painter.fillRect(sv_rect, hue_color)
        white = QLinearGradient(sv_rect.topLeft(), sv_rect.topRight())
        white.setColorAt(0, QColor(255, 255, 255, 255))
        white.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(sv_rect, white)
        black = QLinearGradient(sv_rect.topLeft(), sv_rect.bottomLeft())
        black.setColorAt(0, QColor(0, 0, 0, 0))
        black.setColorAt(1, QColor(0, 0, 0, 255))
        painter.fillRect(sv_rect, black)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(38, 38, 42), 1.5))
        painter.drawRect(sv_rect)

        _paint_checkerboard(painter, alpha_rect, max(3.0, alpha_rect.width() / 3))
        alpha_gradient = QLinearGradient(
            alpha_rect.topLeft(), alpha_rect.bottomLeft()
        )
        opaque = QColor.fromHsvF(
            self._hue, self._saturation, self._value, 1.0
        )
        transparent = QColor(opaque)
        transparent.setAlpha(0)
        alpha_gradient.setColorAt(0, opaque)
        alpha_gradient.setColorAt(1, transparent)
        painter.fillRect(alpha_rect, alpha_gradient)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(38, 38, 42), 1.5))
        painter.drawRect(alpha_rect)

        ring_radius = (outer.width() + inner.width()) / 4
        radians = self._hue * math.tau
        hue_point = QPointF(
            center.x() + math.cos(radians) * ring_radius,
            center.y() - math.sin(radians) * ring_radius,
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(245, 245, 245), 3))
        painter.drawEllipse(hue_point, 5.5, 5.5)
        painter.setPen(QPen(QColor(25, 25, 28), 1))
        painter.drawEllipse(hue_point, 7, 7)

        sv_point = QPointF(
            sv_rect.left() + self._saturation * sv_rect.width(),
            sv_rect.bottom() - self._value * sv_rect.height(),
        )
        painter.setPen(QPen(QColor(250, 250, 250), 2.5))
        painter.drawEllipse(sv_point, 5.5, 5.5)
        painter.setPen(QPen(QColor(30, 30, 34), 1))
        painter.drawEllipse(sv_point, 7, 7)

        marker_y = alpha_rect.top() + (1.0 - self._alpha) * alpha_rect.height()
        painter.setPen(
            QPen(
                QColor(50, 50, 54),
                6,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.SquareCap,
            )
        )
        painter.drawLine(
            QPointF(alpha_rect.left() - 3, marker_y),
            QPointF(alpha_rect.right() + 3, marker_y),
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        mode = self._mode_at(event.position())
        if mode is None:
            super().mousePressEvent(event)
            return
        self._drag_mode = mode
        self.interactionStarted.emit()
        self._update_from_point(mode, event.position())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_mode is None:
            super().mouseMoveEvent(event)
            return
        self._update_from_point(self._drag_mode, event.position())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._drag_mode is not None
        ):
            self._update_from_point(self._drag_mode, event.position())
            self._drag_mode = None
            self.interactionFinished.emit(canonical_argb(self._color))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _layout_geometry(self) -> tuple[QRectF, QRectF, QRectF, QRectF]:
        side = max(1.0, min(self.width(), self.height()) - 8.0)
        center = QPointF(self.width() / 2, self.height() / 2)
        outer_radius = side * 0.48
        ring_width = max(13.0, side * 0.09)
        inner_radius = max(1.0, outer_radius - ring_width)
        outer = QRectF(
            center.x() - outer_radius,
            center.y() - outer_radius,
            outer_radius * 2,
            outer_radius * 2,
        )
        inner = QRectF(
            center.x() - inner_radius,
            center.y() - inner_radius,
            inner_radius * 2,
            inner_radius * 2,
        )

        alpha_width = max(13.0, side * 0.065)
        gap = max(5.0, side * 0.025)
        sv_side = min(
            inner_radius * 1.34,
            inner.width() - alpha_width - gap - 8,
        )
        sv_side = max(40.0, sv_side)
        combined = sv_side + gap + alpha_width
        start_x = center.x() - combined / 2
        sv = QRectF(
            start_x,
            center.y() - sv_side / 2,
            sv_side,
            sv_side,
        )
        alpha = QRectF(
            sv.right() + gap,
            sv.top(),
            alpha_width,
            sv.height(),
        )
        return outer, inner, sv, alpha

    def _mode_at(self, point: QPointF) -> str | None:
        _outer, _inner, sv, alpha = self._layout_geometry()
        if alpha.adjusted(-3, -3, 3, 3).contains(point):
            return "alpha"
        if sv.contains(point):
            return "sv"
        outer, inner, _sv, _alpha = self._layout_geometry()
        center = outer.center()
        distance = math.hypot(point.x() - center.x(), point.y() - center.y())
        if inner.width() / 2 <= distance <= outer.width() / 2:
            return "hue"
        return None

    def _update_from_point(self, mode: str, point: QPointF) -> None:
        outer, _inner, sv, alpha = self._layout_geometry()
        if mode == "hue":
            center = outer.center()
            angle = math.atan2(
                -(point.y() - center.y()), point.x() - center.x()
            )
            self._hue = (angle / math.tau) % 1.0
        elif mode == "sv":
            self._saturation = max(
                0.0, min(1.0, (point.x() - sv.left()) / max(1.0, sv.width()))
            )
            self._value = max(
                0.0, min(1.0, (sv.bottom() - point.y()) / max(1.0, sv.height()))
            )
        elif mode == "alpha":
            self._alpha = max(
                0.0,
                min(
                    1.0,
                    (alpha.bottom() - point.y()) / max(1.0, alpha.height()),
                ),
            )
        else:
            return
        self._sync_color()
        self.update()
        self.colorChanged.emit(canonical_argb(self._color))

    def _sync_color(self) -> None:
        self._color = QColor.fromHsvF(
            self._hue, self._saturation, self._value, self._alpha
        )


class ColorWellButton(QAbstractButton):
    """Checker-backed color well used for primary/secondary routing."""

    colorChanged = Signal(str)

    def __init__(
        self,
        color: str | QColor = "#FF000000",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._color = qcolor_from_argb(color)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(38, 30)

    def color(self) -> QColor:
        return QColor(self._color)

    def color_argb(self) -> str:
        return canonical_argb(self._color)

    def setColor(
        self, color: str | QColor, *, emit: bool = False
    ) -> None:  # noqa: N802
        normalized = qcolor_from_argb(color)
        changed = canonical_argb(normalized) != canonical_argb(self._color)
        self._color = normalized
        self.update()
        if emit and changed:
            self.colorChanged.emit(canonical_argb(self._color))

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        _paint_checkerboard(painter, rect, 5)
        painter.fillRect(rect, self._color)
        pen = QColor(80, 190, 245) if self.isChecked() else QColor(70, 70, 76)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(pen, 3 if self.isChecked() else 1.5))
        painter.drawRoundedRect(rect, 3, 3)


class PrimarySecondaryColorPanel(QWidget):
    """Compact picker whose active well routes edits to primary or secondary."""

    activeSlotChanged = Signal(str)
    colorChanged = Signal(str, str)
    primaryColorChanged = Signal(str)
    secondaryColorChanged = Signal(str)
    colorsSwapped = Signal(str, str)

    def __init__(
        self,
        primary: str | QColor = "#FF000000",
        secondary: str | QColor = "#FFFFFFFF",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("primarySecondaryColorPanel")
        self._active_slot = "primary"
        self._primary = canonical_argb(primary)
        self._secondary = canonical_argb(secondary)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(3)
        self.picker = HsvAlphaPicker(self._primary, self)
        layout.addWidget(self.picker, 1)

        self.footer = QWidget(self)
        self.footer.setObjectName("colorPickerFooter")
        self.footer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.footer.setMinimumSize(190, 62)
        footer_layout = QVBoxLayout(self.footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(3)

        wells = QHBoxLayout()
        wells.setContentsMargins(0, 0, 0, 0)
        self.primary_well = ColorWellButton(self._primary, self.footer)
        self.primary_well.setToolTip("Primary color")
        self.primary_well.setChecked(True)
        self.secondary_well = ColorWellButton(self._secondary, self.footer)
        self.secondary_well.setToolTip("Secondary color")
        self.swap_colors = QPushButton(self.footer)
        self.swap_colors.setFixedSize(28, 28)
        self.swap_colors.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_BrowserReload
        ))
        self.swap_colors.setToolTip("Swap primary and secondary colors")
        wells.addStretch(1)
        wells.addWidget(QLabel("Primary", self.footer))
        wells.addWidget(self.primary_well)
        wells.addSpacing(4)
        wells.addWidget(self.swap_colors)
        wells.addSpacing(4)
        wells.addWidget(QLabel("Secondary", self.footer))
        wells.addWidget(self.secondary_well)
        wells.addStretch(1)

        hex_row = QHBoxLayout()
        hex_row.setContentsMargins(0, 0, 0, 0)
        hex_row.addWidget(QLabel("Hex", self.footer))
        self.hex_field = QLineEdit(self.active_color(), self.footer)
        self.hex_field.setMaxLength(9)
        self.hex_field.setMinimumWidth(82)
        self.hex_field.setPlaceholderText("#AARRGGBB")
        self.hex_copy = QPushButton("Copy", self.footer)
        self.hex_paste = QPushButton("Paste", self.footer)
        self.hex_copy.setFixedWidth(44)
        self.hex_paste.setFixedWidth(44)
        self.hex_copy.setToolTip("Copy the active color hex code")
        self.hex_paste.setToolTip("Paste a #RRGGBB or #AARRGGBB color")
        hex_row.addWidget(self.hex_field, 1)
        hex_row.addWidget(self.hex_copy)
        hex_row.addWidget(self.hex_paste)
        footer_layout.addLayout(hex_row)
        footer_layout.addLayout(wells)
        layout.addWidget(self.footer, 0)

        self.primary_well.clicked.connect(
            lambda: self.set_active_slot("primary")
        )
        self.secondary_well.clicked.connect(
            lambda: self.set_active_slot("secondary")
        )
        self.swap_colors.clicked.connect(self._swap_colors)
        self.picker.colorChanged.connect(self._picker_changed)
        self.hex_field.editingFinished.connect(self._hex_edited)
        self.hex_copy.clicked.connect(
            lambda: QApplication.clipboard().setText(self.active_color())
        )
        self.hex_paste.clicked.connect(self._paste_hex)

    def active_slot(self) -> str:
        return self._active_slot

    def active_color(self) -> str:
        return self._primary if self._active_slot == "primary" else self._secondary

    def primary_color(self) -> str:
        return self._primary

    def secondary_color(self) -> str:
        return self._secondary

    def set_active_slot(self, slot: str) -> None:
        if slot not in ("primary", "secondary"):
            raise ValueError("Color slot must be 'primary' or 'secondary'")
        changed = slot != self._active_slot
        self._active_slot = slot
        self.primary_well.setChecked(slot == "primary")
        self.secondary_well.setChecked(slot == "secondary")
        self.picker.setColor(self.active_color())
        self.hex_field.setText(self.active_color())
        if changed:
            self.activeSlotChanged.emit(slot)

    def set_primary_color(
        self, color: str | QColor, *, emit: bool = False
    ) -> None:
        self._set_slot_color("primary", color, emit=emit)

    def set_secondary_color(
        self, color: str | QColor, *, emit: bool = False
    ) -> None:
        self._set_slot_color("secondary", color, emit=emit)

    def set_colors(
        self,
        primary: str | QColor,
        secondary: str | QColor,
        *,
        emit: bool = False,
    ) -> None:
        self._set_slot_color("primary", primary, emit=emit)
        self._set_slot_color("secondary", secondary, emit=emit)

    def apply_color(
        self, color: str | QColor, *, emit: bool = True
    ) -> None:
        self._set_slot_color(self._active_slot, color, emit=emit)

    def _swap_colors(self) -> None:
        primary, secondary = self._secondary, self._primary
        self.set_colors(primary, secondary, emit=False)
        self.colorsSwapped.emit(primary, secondary)

    def _picker_changed(self, color: str) -> None:
        self._set_slot_color(self._active_slot, color, emit=True, sync_picker=False)

    @staticmethod
    def _valid_hex(text: str) -> str | None:
        candidate = text.strip()
        if not candidate.startswith("#"):
            candidate = f"#{candidate}"
        digits = candidate[1:]
        if len(digits) not in {6, 8} or any(
            character not in "0123456789abcdefABCDEF"
            for character in digits
        ):
            return None
        if len(digits) == 6:
            candidate = f"#FF{digits}"
        return candidate.upper()

    def _hex_edited(self) -> None:
        value = self._valid_hex(self.hex_field.text())
        if value is None:
            self.hex_field.setText(self.active_color())
            return
        self.apply_color(value)

    def _paste_hex(self) -> None:
        value = self._valid_hex(QApplication.clipboard().text())
        if value is None:
            self.hex_field.setText(self.active_color())
            return
        self.apply_color(value)

    def _set_slot_color(
        self,
        slot: str,
        color: str | QColor,
        *,
        emit: bool,
        sync_picker: bool = True,
    ) -> None:
        value = canonical_argb(color)
        if slot == "primary":
            changed = value != self._primary
            self._primary = value
            self.primary_well.setColor(value)
        elif slot == "secondary":
            changed = value != self._secondary
            self._secondary = value
            self.secondary_well.setColor(value)
        else:
            raise ValueError("Color slot must be 'primary' or 'secondary'")
        if sync_picker and self._active_slot == slot:
            self.picker.setColor(value)
        if self._active_slot == slot:
            self.hex_field.setText(value)
        if emit and changed:
            self.colorChanged.emit(slot, value)
            if slot == "primary":
                self.primaryColorChanged.emit(value)
            else:
                self.secondaryColorChanged.emit(value)


class ColorSwatchButton(QAbstractButton):
    """Square palette swatch with disambiguated single/double-click actions."""

    swatchActivated = Signal(str, str)
    editRequested = Signal(str)
    removeRequested = Signal(str)

    def __init__(
        self,
        swatch_id: str,
        color: str | QColor,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.swatch_id = str(swatch_id)
        self._color = qcolor_from_argb(color)
        self._double_click_release = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(canonical_argb(self._color))

        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.timeout.connect(self._emit_activation)

        remove_action = QAction("Remove color", self)
        remove_action.triggered.connect(
            lambda: self.removeRequested.emit(self.swatch_id)
        )
        self.addAction(remove_action)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(28, 28)

    def color(self) -> QColor:
        return QColor(self._color)

    def color_argb(self) -> str:
        return canonical_argb(self._color)

    def setColor(self, color: str | QColor) -> None:  # noqa: N802
        self._color = qcolor_from_argb(color)
        self.setToolTip(canonical_argb(self._color))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        _paint_checkerboard(painter, rect, 4)
        painter.fillRect(rect, self._color)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(
                QColor(120, 190, 245)
                if self.hasFocus()
                else QColor(58, 58, 64),
                2 if self.hasFocus() else 1,
            )
        )
        painter.drawRect(rect)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._single_click_timer.stop()
            self._double_click_release = True
            QTimer.singleShot(0, self._clear_double_click_release)
            self.setDown(False)
            self.editRequested.emit(self.swatch_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        inside = self.rect().contains(event.position().toPoint())
        super().mouseReleaseEvent(event)
        if event.button() != Qt.MouseButton.LeftButton or not inside:
            return
        if self._double_click_release:
            self._double_click_release = False
            return
        self._queue_activation()

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        super().keyReleaseEvent(event)
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._queue_activation()

    def _queue_activation(self) -> None:
        interval = QApplication.styleHints().mouseDoubleClickInterval()
        self._single_click_timer.start(
            max(1, interval)
        )

    def _clear_double_click_release(self) -> None:
        self._double_click_release = False

    def _emit_activation(self) -> None:
        self.swatchActivated.emit(
            self.swatch_id, canonical_argb(self._color)
        )


class ColorPickerPopup(QDialog):
    """Small Apply/Cancel wrapper around :class:`HsvAlphaPicker`."""

    colorApplied = Signal(str)

    def __init__(
        self,
        color: str | QColor = "#FF000000",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("colorPickerPopup")
        self.setWindowTitle("Choose color")
        layout = QVBoxLayout(self)
        self.picker = HsvAlphaPicker(color, self)
        layout.addWidget(self.picker)
        self.quick_colors = QWidget(self)
        quick_layout = QHBoxLayout(self.quick_colors)
        quick_layout.setContentsMargins(0, 0, 0, 0)
        quick_layout.addWidget(QLabel("Use", self.quick_colors))
        self.primary_quick = QPushButton("Primary", self.quick_colors)
        self.secondary_quick = QPushButton("Secondary", self.quick_colors)
        quick_layout.addWidget(self.primary_quick)
        quick_layout.addWidget(self.secondary_quick)
        quick_layout.addStretch(1)
        self.primary_quick.clicked.connect(
            lambda: self.picker.setColor(self._primary_quick_color)
        )
        self.secondary_quick.clicked.connect(
            lambda: self.picker.setColor(self._secondary_quick_color)
        )
        self._primary_quick_color = "#FF000000"
        self._secondary_quick_color = "#FFFFFFFF"
        layout.addWidget(self.quick_colors)
        self.quick_colors.hide()
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.buttons.button(
            QDialogButtonBox.StandardButton.Apply
        ).clicked.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def color(self) -> QColor:
        return self.picker.color()

    def color_argb(self) -> str:
        return self.picker.color_argb()

    def setColor(self, color: str | QColor) -> None:  # noqa: N802
        self.picker.setColor(color)

    def setQuickColors(
        self, primary: str | QColor, secondary: str | QColor,
    ) -> None:  # noqa: N802
        self._primary_quick_color = canonical_argb(primary)
        self._secondary_quick_color = canonical_argb(secondary)
        self.quick_colors.show()
        self.primary_quick.setStyleSheet(
            f"background-color: {QColor(self._primary_quick_color).name()};"
        )
        self.secondary_quick.setStyleSheet(
            f"background-color: {QColor(self._secondary_quick_color).name()};"
        )

    def accept(self) -> None:
        self.colorApplied.emit(self.color_argb())
        super().accept()


def _field(record: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(record, dict) and name in record:
            return record[name]
        if hasattr(record, name):
            return getattr(record, name)
    return default


def _normalize_palettes(records: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for palette_index, palette in enumerate(records or []):
        palette_id = str(
            _field(
                palette,
                "palette_id",
                "id",
                default=f"palette-{palette_index}",
            )
        )
        swatches: list[dict[str, str]] = []
        for swatch_index, swatch in enumerate(
            _field(palette, "swatches", "colors", default=[]) or []
        ):
            if isinstance(swatch, (str, QColor, int)):
                swatch_id = f"{palette_id}-swatch-{swatch_index}"
                color = swatch
            else:
                swatch_id = str(
                    _field(
                        swatch,
                        "swatch_id",
                        "id",
                        default=f"{palette_id}-swatch-{swatch_index}",
                    )
                )
                color = _field(swatch, "color", "argb", default="#FF000000")
            swatches.append(
                {
                    "swatch_id": swatch_id,
                    "color": canonical_argb(color),
                }
            )
        normalized.append(
            {
                "palette_id": palette_id,
                "name": str(
                    _field(
                        palette,
                        "name",
                        default=f"Palette {palette_index + 1}",
                    )
                ),
                "swatches": swatches,
            }
        )
    return normalized


class PaletteEditorWidget(QWidget):
    """Palette selector, inline name editor, and stable-ID swatch grid."""

    paletteSelectionChanged = Signal(str)
    addPaletteRequested = Signal()
    removePaletteRequested = Signal(str)
    paletteNameChanged = Signal(str, str)
    swatchActivated = Signal(str, str, str)
    swatchColorChangeRequested = Signal(str, str, str)
    addSwatchRequested = Signal(str, str)
    removeSwatchRequested = Signal(str, str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("paletteEditor")
        self._palettes: list[dict[str, Any]] = []
        self._active_palette_id: str | None = None
        self._updating = False
        self._picker_target: tuple[str, str | None] | None = None
        self._new_swatch_color = "#FF000000"
        self._swatch_buttons: dict[str, ColorSwatchButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        selector_row = QHBoxLayout()
        self.palette_combo = QComboBox(self)
        self.palette_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.palette_combo.currentIndexChanged.connect(
            self._combo_changed
        )
        selector_row.addWidget(self.palette_combo, 1)
        self.add_palette_button = QToolButton(self)
        self.add_palette_button.setText("+")
        self.add_palette_button.setToolTip("Add palette")
        self.add_palette_button.clicked.connect(self.addPaletteRequested)
        selector_row.addWidget(self.add_palette_button)
        self.remove_palette_button = QToolButton(self)
        self.remove_palette_button.setText("−")
        self.remove_palette_button.setToolTip("Remove palette")
        self.remove_palette_button.clicked.connect(
            self._request_remove_palette
        )
        selector_row.addWidget(self.remove_palette_button)
        layout.addLayout(selector_row)

        self.name_edit = QLineEdit(self)
        self.name_edit.setPlaceholderText("Palette name")
        self.name_edit.setToolTip("Palette name")
        self.name_edit.editingFinished.connect(self._name_finished)
        layout.addWidget(self.name_edit)

        self.swatch_container = QWidget(self)
        self.swatch_grid = QGridLayout(self.swatch_container)
        self.swatch_grid.setContentsMargins(0, 0, 0, 0)
        self.swatch_grid.setHorizontalSpacing(3)
        self.swatch_grid.setVerticalSpacing(3)
        layout.addWidget(self.swatch_container)

        self.add_swatch_button = QToolButton(self.swatch_container)
        self.add_swatch_button.setText("+")
        self.add_swatch_button.setToolTip("Add color")
        self.add_swatch_button.setFixedSize(28, 28)
        self.add_swatch_button.clicked.connect(self._open_add_picker)

        self.color_popup = ColorPickerPopup(parent=self)
        self.color_popup.colorApplied.connect(self._picker_applied)

        self.set_palettes([])

    def set_palettes(
        self,
        palettes: Any,
        active_palette_id: str | None = None,
        *,
        emit: bool = False,
    ) -> None:
        self._palettes = _normalize_palettes(palettes)
        ids = [palette["palette_id"] for palette in self._palettes]
        if active_palette_id not in ids:
            active_palette_id = ids[0] if ids else None
        changed = active_palette_id != self._active_palette_id
        self._active_palette_id = active_palette_id
        self._rebuild_selector_and_grid()
        if emit and changed and active_palette_id is not None:
            self.paletteSelectionChanged.emit(active_palette_id)

    def palettes(self) -> list[dict[str, Any]]:
        return [
            {
                "palette_id": palette["palette_id"],
                "name": palette["name"],
                "swatches": [
                    dict(swatch) for swatch in palette["swatches"]
                ],
            }
            for palette in self._palettes
        ]

    def active_palette_id(self) -> str | None:
        return self._active_palette_id

    def active_palette(self) -> dict[str, Any] | None:
        return next(
            (
                palette
                for palette in self._palettes
                if palette["palette_id"] == self._active_palette_id
            ),
            None,
        )

    def set_active_palette(
        self, palette_id: str, *, emit: bool = False
    ) -> bool:
        ids = [palette["palette_id"] for palette in self._palettes]
        if palette_id not in ids:
            return False
        changed = palette_id != self._active_palette_id
        self._active_palette_id = palette_id
        self._updating = True
        self.palette_combo.setCurrentIndex(ids.index(palette_id))
        self._updating = False
        self._rebuild_grid()
        if emit and changed:
            self.paletteSelectionChanged.emit(palette_id)
        return True

    def set_new_swatch_color(self, color: str | QColor) -> None:
        self._new_swatch_color = canonical_argb(color)

    def update_swatch_color(
        self, swatch_id: str, color: str | QColor
    ) -> bool:
        palette = self.active_palette()
        if palette is None:
            return False
        for swatch in palette["swatches"]:
            if swatch["swatch_id"] == swatch_id:
                swatch["color"] = canonical_argb(color)
                button = self._swatch_buttons.get(swatch_id)
                if button is not None:
                    button.setColor(swatch["color"])
                return True
        return False

    def set_palette_name(self, palette_id: str, name: str) -> bool:
        for palette in self._palettes:
            if palette["palette_id"] != palette_id:
                continue
            palette["name"] = str(name)
            index = [
                entry["palette_id"] for entry in self._palettes
            ].index(palette_id)
            self.palette_combo.setItemText(index, str(name))
            if self._active_palette_id == palette_id:
                self.name_edit.setText(str(name))
            return True
        return False

    def _rebuild_selector_and_grid(self) -> None:
        self._updating = True
        self.palette_combo.clear()
        for palette in self._palettes:
            self.palette_combo.addItem(
                palette["name"], palette["palette_id"]
            )
        if self._active_palette_id is not None:
            index = self.palette_combo.findData(self._active_palette_id)
            self.palette_combo.setCurrentIndex(index)
        self._updating = False
        self._rebuild_grid()

    def _clear_grid(self) -> None:
        for button in self._swatch_buttons.values():
            self.swatch_grid.removeWidget(button)
            button.deleteLater()
        self._swatch_buttons.clear()
        self.swatch_grid.removeWidget(self.add_swatch_button)

    def _rebuild_grid(self) -> None:
        self._clear_grid()
        palette = self.active_palette()
        has_palette = palette is not None
        self.name_edit.setEnabled(has_palette)
        self.add_swatch_button.setEnabled(has_palette)
        self.remove_palette_button.setEnabled(len(self._palettes) > 1)
        if palette is None:
            self.name_edit.clear()
            self.add_swatch_button.hide()
            return

        self.name_edit.setText(palette["name"])
        columns = 6
        for index, swatch in enumerate(palette["swatches"]):
            button = ColorSwatchButton(
                swatch["swatch_id"], swatch["color"], self.swatch_container
            )
            button.setFixedSize(28, 28)
            button.swatchActivated.connect(
                lambda swatch_id, color, palette_id=palette[
                    "palette_id"
                ]: self.swatchActivated.emit(
                    palette_id, swatch_id, color
                )
            )
            button.editRequested.connect(self._open_edit_picker)
            button.removeRequested.connect(self._request_remove_swatch)
            self._swatch_buttons[swatch["swatch_id"]] = button
            self.swatch_grid.addWidget(
                button, index // columns, index % columns
            )
        add_index = len(palette["swatches"])
        self.add_swatch_button.show()
        self.swatch_grid.addWidget(
            self.add_swatch_button,
            add_index // columns,
            add_index % columns,
        )

    def _combo_changed(self, index: int) -> None:
        if self._updating or index < 0:
            return
        palette_id = self.palette_combo.itemData(index)
        if palette_id is None:
            return
        self._active_palette_id = str(palette_id)
        self._rebuild_grid()
        self.paletteSelectionChanged.emit(self._active_palette_id)

    def _name_finished(self) -> None:
        palette = self.active_palette()
        if palette is None:
            return
        name = self.name_edit.text().strip() or "Palette"
        if name == palette["name"]:
            self.name_edit.setText(name)
            return
        palette["name"] = name
        index = self.palette_combo.findData(palette["palette_id"])
        if index >= 0:
            self.palette_combo.setItemText(index, name)
        self.paletteNameChanged.emit(palette["palette_id"], name)

    def _request_remove_palette(self) -> None:
        if self._active_palette_id is not None and len(self._palettes) > 1:
            self.removePaletteRequested.emit(self._active_palette_id)

    def _request_remove_swatch(self, swatch_id: str) -> None:
        if self._active_palette_id is not None:
            self.removeSwatchRequested.emit(
                self._active_palette_id, swatch_id
            )

    def _open_edit_picker(self, swatch_id: str) -> None:
        palette = self.active_palette()
        if palette is None:
            return
        swatch = next(
            (
                item
                for item in palette["swatches"]
                if item["swatch_id"] == swatch_id
            ),
            None,
        )
        if swatch is None:
            return
        self._picker_target = ("edit", swatch_id)
        self.color_popup.setColor(swatch["color"])
        self.color_popup.open()

    def _open_add_picker(self) -> None:
        if self._active_palette_id is None:
            return
        self._picker_target = ("add", None)
        self.color_popup.setColor(self._new_swatch_color)
        self.color_popup.open()

    def _picker_applied(self, color: str) -> None:
        target = self._picker_target
        self._picker_target = None
        palette_id = self._active_palette_id
        if target is None or palette_id is None:
            return
        action, swatch_id = target
        if action == "edit" and swatch_id is not None:
            self.update_swatch_color(swatch_id, color)
            self.swatchColorChangeRequested.emit(
                palette_id, swatch_id, color
            )
        elif action == "add":
            self.addSwatchRequested.emit(palette_id, color)
