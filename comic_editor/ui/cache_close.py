"""Optional asynchronous publication of resident exact caches at project close."""
from collections import deque

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QMessageBox, QProgressDialog


class ProjectCacheClose:
    def __init__(self, window):
        self.window = window
        self.dialog = None
        self._sessions = deque()
        self._complete = None
        self._canceling = False
        self._read_only = []

    @property
    def busy(self):
        return self._complete is not None

    def request(self, sessions, complete, *, exiting=False):
        """Finish closing immediately, or after an explicitly accepted disk save.

        Unsaved sessions whose edits were discarded must be omitted by the caller.
        Exact cache publication is optional and independent of source saves.
        """
        if self.busy:
            if self.dialog is not None:
                self.dialog.raise_()
            return
        cache = self.window.disk_cache
        eligible = [session for session in sessions
                    if session.kind == "series" and cache.can_persist(session)]
        if not eligible:
            complete()
            return
        action = "exiting" if exiting else "closing this project"
        answer = QMessageBox.question(
            self.window, "Save preview cache?",
            f"Save the memory preview cache to disk before {action}?\n\n"
            "It will load into memory next time you open the project. "
            "Your artwork is saved separately.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.No,
        )
        if answer == QMessageBox.Cancel:
            return
        if answer != QMessageBox.Yes:
            complete()
            return
        self._sessions = deque(eligible)
        self._complete = complete
        self._canceling = False
        for owner in (self.window.canvas, self.window.canvas.command_stack, self.window.hierarchy_model):
            attribute = "document_read_only" if owner is self.window.canvas else "read_only"
            self._read_only.append((owner, attribute, getattr(owner, attribute, False)))
            setattr(owner, attribute, True)
        self.dialog = QProgressDialog("Saving preview cache…", "Cancel", 0, 0, self.window)
        self.dialog.setWindowTitle("Save preview cache")
        self.dialog.setWindowModality(Qt.WindowModal)
        self.dialog.setMinimumDuration(0)
        self.dialog.setAutoClose(False)
        self.dialog.setAutoReset(False)
        self.dialog.canceled.connect(self.cancel)
        self.dialog.show()
        self._next()

    def _next(self):
        if not self.busy:
            return
        if self._canceling:
            self._finish(False)
            return
        if not self._sessions:
            self._finish(True)
            return
        session = self._sessions.popleft()
        self.dialog.setLabelText(f"Saving preview cache for {session.name}…")
        if not self.window.disk_cache.persist_for_close(session, self._saved):
            self._saved(False, "Unable to start saving the preview cache.")

    def _saved(self, success, message=""):
        if not self.busy:
            return
        if not success or self._canceling:
            self._finish(False)
            if message and not self._canceling:
                QMessageBox.warning(self.window, "Preview cache not saved", message)
            return
        QTimer.singleShot(0, self._next)

    def cancel(self):
        if not self.busy or self._canceling:
            return
        self._canceling = True
        self.window.disk_cache.cancel_persistence()

    def _finish(self, success):
        complete, self._complete = self._complete, None
        self._sessions.clear()
        dialog, self.dialog = self.dialog, None
        if dialog is not None:
            dialog.canceled.disconnect(self.cancel)
            dialog.close()
            dialog.deleteLater()
        for owner, attribute, previous in self._read_only:
            setattr(owner, attribute, previous)
        self._read_only.clear()
        active = self.window.active_session
        if success and complete is not None:
            complete()
        # Closing an inactive tab leaves the current editing context alive.
        # Cancellation also resumes the context whose publications were deferred.
        if ((not success or (self.window.active_session is active and not self.window._cache_exit_approved))
                and not getattr(self.window.canvas, "document_read_only", False)):
            self.window.blender_sources.resume_for_context()
            self.window.canvas._finish_pending_external_drop_if_ready()
            sources = getattr(self.window, "_deferred_clipboard_sources", None)
            if sources is not None:
                self.window._deferred_clipboard_sources = None
                self.window._clipboard_images_resolved(sources)
