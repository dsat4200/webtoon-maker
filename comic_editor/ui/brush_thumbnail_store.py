"""Prepare persistent preset thumbnails independently of an open picker."""
from collections import OrderedDict

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core import brush_thumbnails, settings as settings_module
from .brush_preview_queue import preview_queue


class BrushThumbnailStore(QObject):
    changed = Signal(str)  # Render fingerprint; every alias uses the same image.
    idle = Signal()

    def __init__(self, parent=None, *, preferences=None, queue=None):
        super().__init__(parent)
        self.preferences = preferences or settings_module.settings_path()
        self.queue = queue or preview_queue()
        self.keys = {}
        self.pixmaps = {}
        self.errors = {}
        self.pending = OrderedDict()
        self.current = None
        self._records = {}
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._prepare_next)

    def sync(self, presets):
        """Called after import/edit and at startup, never by scrolling the grid."""
        keys, records = {}, {}
        for data in presets:
            identifier = data.get('id')
            record = self._records.get(identifier)
            definition = BrushDefinition.from_dict(data)
            key = brush_thumbnails.thumbnail_key(definition)
            if record is not None and record[0] == key:
                definition = record[1]
            else:
                definition = brush_thumbnails.thumbnail_definition(definition)
            keys[identifier] = key
            records[identifier] = (key, definition)
        needed = set(keys.values())
        self.pending = OrderedDict((key, brush) for key, brush in self.pending.items() if key in needed)
        if self.current is not None and self.current not in needed:
            self.queue.cancel(self)
            self.current = None
        old_keys = self.keys
        self.keys, self._records = keys, records
        self.pixmaps = {key: pixmap for key, pixmap in self.pixmaps.items() if key in needed}
        self.errors = {key: error for key, error in self.errors.items() if key in needed}
        for identifier, (key, definition) in records.items():
            if key in self.pixmaps or key == self.current or key in self.pending or key in self.errors:
                continue
            image = brush_thumbnails.load_thumbnail(key, self.preferences)
            if image is not None:
                self.pixmaps[key] = QPixmap.fromImage(image)
            else:
                self.pending[key] = definition
                if identifier in old_keys and old_keys[identifier] != key:
                    self.pending.move_to_end(key, last=False)
        if self.current is None and self.pending:
            self._timer.start(0)

    def icon(self, identifier):
        pixmap = self.pixmaps.get(self.keys.get(identifier))
        return QIcon(pixmap) if pixmap is not None else QIcon()

    def error(self, identifier):
        return self.errors.get(self.keys.get(identifier), '')

    def _prepare_next(self):
        if self.current is not None or not self.pending:
            return
        key, definition = self.pending.popitem(last=False)
        self.current = key
        self.queue.submit(self, definition, brush_thumbnails.THUMBNAIL_WIDTH,
                          brush_thumbnails.THUMBNAIL_HEIGHT, brush_thumbnails.THUMBNAIL_COLOR,
                          context=key, priority=2)

    def _preview_is_visible(self):
        # Cache warming must continue when the brush page or picker is hidden.
        return True

    def _preview_ready(self, pixmap, error, context):
        if context != self.current:
            return
        self.current = None
        if pixmap is not None:
            self.pixmaps[context] = pixmap
            try:
                brush_thumbnails.save_thumbnail(context, pixmap.toImage(), self.preferences)
            except (OSError, ValueError) as failure:
                # Drawing and the current picker remain usable if the cache
                # folder is read only. A future app session can try again.
                self.errors[context] = f'Thumbnail could not be saved: {failure}'
        else:
            self.errors[context] = error or 'Preview unavailable.'
        self.changed.emit(context)
        if self.pending:
            self._timer.start(0)
        else:
            self.idle.emit()

    def stop(self):
        self._timer.stop()
        self.queue.cancel(self)
        self.current = None
        self.pending.clear()
