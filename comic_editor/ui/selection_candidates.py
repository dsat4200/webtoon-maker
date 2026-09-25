"""Selection chooser with temporary isolation and direct tablet input."""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QMenu


class SelectionCandidateMenu(QMenu):
    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.chapter = canvas.chapter
        self.previous_solo = canvas.solo_entities
        self._tablet_pressed_action = None
        self.hovered.connect(self._preview_action)
        self.aboutToHide.connect(self.restore_preview)

    def _preview_action(self, action):
        if self.canvas.chapter is not self.chapter:
            self.close()
            return
        candidate = action.data() if action is not None and action.isEnabled() else None
        self.canvas.set_solo_entities({tuple(candidate)} if candidate else self.previous_solo)

    def restore_preview(self):
        self._tablet_pressed_action = None
        if self.canvas.chapter is self.chapter:
            self.canvas.set_solo_entities(self.previous_solo)

    def leaveEvent(self, event):  # noqa: N802
        self.restore_preview()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        super().mouseMoveEvent(event)
        self._preview_action(self.actionAt(event.position().toPoint()))

    def tabletEvent(self, event):  # noqa: N802
        # A pen can remain grabbed by the canvas that opened this popup. The
        # application's event filter forwards those events here in global coords.
        position = self.mapFromGlobal(event.globalPosition().toPoint())
        action = self.actionAt(position) if self.rect().contains(position) else None
        if action is not None and (not action.isEnabled() or action.isSeparator()):
            action = None
        self.setActiveAction(action)
        self._preview_action(action)
        if event.type() == QEvent.TabletPress:
            if event.button() == Qt.LeftButton or event.pressure() > 0:
                self._tablet_pressed_action = action
                if not self.rect().contains(position):
                    self.close()
        elif event.type() == QEvent.TabletRelease:
            pressed, self._tablet_pressed_action = self._tablet_pressed_action, None
            # The release of the Ctrl-tap that opened the menu is not a choice.
            if pressed is not None:
                self.close()
                if pressed is action:
                    action.trigger()
        event.accept()
