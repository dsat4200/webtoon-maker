"""Single-chord capture and normalization, including modifier-only chords."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import QLineEdit


MODIFIER_LABELS = {
    int(Qt.Key_Control): "Ctrl",
    int(Qt.Key_Alt): "Alt",
    int(Qt.Key_Shift): "Shift",
    int(Qt.Key_Meta): "Meta",
}
MODIFIER_ORDER = tuple(MODIFIER_LABELS)
MODIFIER_FLAGS = {
    int(Qt.Key_Control): Qt.ControlModifier,
    int(Qt.Key_Alt): Qt.AltModifier,
    int(Qt.Key_Shift): Qt.ShiftModifier,
    int(Qt.Key_Meta): Qt.MetaModifier,
}


def chord_text(keys: set[int] | frozenset[int]) -> str:
    """Return one portable, deterministic simultaneous key chord."""
    keys = {int(key) for key in keys if int(key) != int(Qt.Key_unknown)}
    modifiers = [
        MODIFIER_LABELS[key] for key in MODIFIER_ORDER if key in keys
    ]
    regular = sorted(key for key in keys if key not in MODIFIER_LABELS)
    if len(regular) > 1:
        regular = regular[-1:]
    if regular:
        label = QKeySequence(regular[0]).toString(QKeySequence.PortableText)
        if not label:
            return ""
        modifiers.append(label)
    return "+".join(modifiers)


def chord_keys(value: str) -> frozenset[int]:
    """Parse a stored portable chord, with special modifier-only support."""
    value = str(value or "").strip()
    if not value:
        return frozenset()
    tokens = [token.strip() for token in value.split("+") if token.strip()]
    modifier_names = {
        label.casefold(): key for key, label in MODIFIER_LABELS.items()
    }
    if tokens and all(token.casefold() in modifier_names for token in tokens):
        return frozenset(modifier_names[token.casefold()] for token in tokens)
    sequence = QKeySequence(value)
    if sequence.count() < 1:
        return frozenset()
    combination = sequence[0]
    key = int(combination.key())
    result = {
        modifier_key
        for modifier_key, flag in MODIFIER_FLAGS.items()
        if combination.keyboardModifiers() & flag
    }
    if key in MODIFIER_LABELS:
        result.add(key)
    elif key != int(Qt.Key_unknown):
        result.add(key)
    return frozenset(result)


def normalize_chord(value: str) -> str:
    return chord_text(chord_keys(value))


class ChordCaptureEdit(QLineEdit):
    """A replacement-style editor for exactly one simultaneous chord."""

    chordChanged = Signal(str)

    def __init__(self, value: str = "", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setClearButtonEnabled(False)
        self.setPlaceholderText("Click, then press keys")
        self._value = normalize_chord(value)
        self._original = self._value
        self._pressed: set[int] = set()
        self._candidate: set[int] = set()
        self._capturing = False
        self.setText(self._value)

    def chord(self) -> str:
        return self._value

    def setChord(self, value: str) -> None:  # noqa: N802
        self._value = normalize_chord(value)
        self._original = self._value
        self._pressed.clear()
        self._candidate.clear()
        self._capturing = False
        self.setText(self._value)

    def commitCapture(self) -> None:  # noqa: N802
        """Commit the displayed candidate before a dialog accepts."""
        if not self._capturing:
            return
        value = chord_text(self._candidate)
        self._value = value
        self._original = value
        self._pressed.clear()
        self._candidate.clear()
        self._capturing = False
        self.setText(value)
        self.chordChanged.emit(value)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._original = self._value
        self._pressed.clear()
        self._candidate.clear()
        self._capturing = False
        self.setFocus(Qt.MouseFocusReason)
        self.selectAll()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            event.accept()
            return
        key = int(event.key())
        if key == int(Qt.Key_Escape):
            self.setChord(self._original)
            self.clearFocus()
            event.accept()
            return
        if key in {int(Qt.Key_Backspace), int(Qt.Key_Delete)}:
            self.setChord("")
            self.chordChanged.emit("")
            event.accept()
            return
        if not self._capturing:
            self._pressed.clear()
            self._candidate.clear()
            self._capturing = True
        if key not in MODIFIER_LABELS:
            self._candidate = {
                candidate for candidate in self._candidate
                if candidate in MODIFIER_LABELS
            }
        self._pressed.add(key)
        self._candidate.add(key)
        self.setText(chord_text(self._candidate))
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.isAutoRepeat():
            event.accept()
            return
        self._pressed.discard(int(event.key()))
        if self._capturing and not self._pressed:
            self.commitCapture()
        event.accept()
