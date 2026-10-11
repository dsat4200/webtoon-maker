"""Compact, selectable pencil and eraser cards with per-preset settings."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal, QSize, QRectF
from PySide6.QtGui import QIcon, QImage, QPainter, QColor, QPalette, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from comic_editor.core.pressure import BrushPreset
from comic_editor.ui.icons import iconoir


class DrawingPresetCards(QWidget):
    selected = Signal(str)
    settingsRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signature = None
        self.cards = {}
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(5)

    def refresh(self, presets, active_name):
        signature = (repr(presets), self.palette().color(QPalette.Text).rgba())
        if signature != self._signature:
            self._signature = signature
            while self.layout.count():
                item = self.layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            self.cards = {}
            for item in presets:
                preset = BrushPreset.from_dict(item)
                row = QWidget(self)
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(3)
                button = QToolButton(row)
                button.setText(preset.name)
                button.setToolTip(f"Select {preset.name}")
                button.setCheckable(True)
                button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
                button.setIcon(self._preview(preset))
                button.setIconSize(QSize(208, 48))
                button.setMinimumHeight(83)
                button.setMinimumWidth(0)
                button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                button.clicked.connect(lambda checked=False, name=preset.name: self.selected.emit(name))
                settings = QToolButton(row)
                settings.setIcon(iconoir("edit-pencil", 18))
                settings.setAccessibleName(f"Settings for {preset.name}")
                settings.setToolTip(f"Settings for {preset.name}")
                settings.setFixedWidth(28)
                settings.clicked.connect(lambda checked=False, name=preset.name: self.settingsRequested.emit(name))
                row_layout.addWidget(button, 1)
                row_layout.addWidget(settings)
                self.layout.addWidget(row)
                self.cards[preset.name] = (button, settings)
            self.layout.addStretch(1)
        for name, (button, settings) in self.cards.items():
            button.setChecked(name == active_name)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = max(48, min(208, self.width() - 45))
        for button, settings in self.cards.values():
            button.setIconSize(QSize(width, 48))

    def _preview(self, preset):
        # These illustrative UI swatches are independent of document rendering.
        image = QImage(208, 48, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, preset.antialiasing)
        painter.setPen(Qt.NoPen)
        color = self.palette().color(QPalette.Text)
        for i in range(193):
            t = i / 192
            pressure = math.sin(math.pi * t)
            size = 9 * (preset.size_curve.evaluate_fast(pressure) if preset.pressure_size else 1)
            opacity = preset.opacity_curve.evaluate_fast(pressure) if preset.pressure_opacity else 1
            shade = QColor(color)
            shade.setAlphaF(min(1., opacity))
            painter.setBrush(shade)
            rect = QRectF(8+i-size/2, 24-12*math.sin(t*math.pi*2)-size/2, size, size)
            if preset.shape == "square":
                painter.drawRect(rect)
            else:
                painter.drawEllipse(rect)
        painter.end()
        return QIcon(QPixmap.fromImage(image))
