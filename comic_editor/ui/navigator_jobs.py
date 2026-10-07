"""Navigator publication using the same detached scene evaluator as the canvas."""
from PySide6.QtCore import QObject, QTimer

from comic_editor.render.scheduler import SceneDemand, SceneScheduler


class NavigatorJobs(QObject):
    def __init__(self, preview):
        super().__init__(preview)
        self.preview = preview
        self.scheduler = SceneScheduler(handoff_budget=4 * 1024 * 1024)
        self.capture = None
        self.document = None
        self.size = None
        self.serial = 0
        self.sent = False
        self.error = ""
        self.timer = QTimer(self)
        self.timer.setInterval(8)
        self.timer.timeout.connect(self.advance)
        scheduler = self.scheduler
        self.destroyed.connect(lambda: scheduler.close())

    def cancel(self):
        self.scheduler.cancel()
        self.capture = self.document = None
        self.sent = False
        self.timer.stop()

    def request(self):
        canvas = self.preview.canvas
        document = canvas._render_document_state()
        size = self.preview.content_rect().size()
        if size.isEmpty():
            return
        if self.document != document or self.size != size:
            self.cancel()
            self.serial += 1
            self.document, self.size = document, size
            self.capture = canvas._scene_snapshot_compiler.capture(canvas, document)
            self.error = ""
        if self.error:
            return
        if not self.timer.isActive():
            self.timer.start(8)

    def advance(self):
        preview, document = self.preview, self.document
        canvas = preview.canvas
        if (document is None or not preview.isVisible() or preview._interaction_active()
                or canvas.chapter is None or canvas._document_projection.revision != document.revision
                or (id(canvas.chapter), id(canvas.tiles), id(canvas.images)) != document.identity):
            self.cancel()
            preview._schedule_refresh()
            return
        for completion in self.scheduler.poll():
            if completion.demand.serial != self.serial:
                continue
            if completion.error:
                self.error = completion.error
            if completion.preview is not None:
                preview._cache = completion.preview.image
                preview._cache_chapter = canvas.chapter
                preview._dirty_full = False
                preview._dirty_bands.clear()
                preview.update()
        if self.capture is not None:
            try:
                finished = self.capture.advance(.004)
            except Exception as error:
                self.error = f'{type(error).__name__}: {error}'
                self.capture = None
                self.sent = True
                self.scheduler.cancel()
                self.timer.stop()
                preview.update()
                return
            if not finished:
                return
            if self.capture.stale:
                self.cancel()
                preview._schedule_refresh()
                return
            snapshot = self.capture.result
            self.capture = None
            self.scheduler.submit(SceneDemand(self.serial, snapshot, (), (None,),
                (document.width / 2, document.height / 2),
                (0., 0., float(document.width), float(document.height)),
                (self.size.width(), self.size.height()), "navigator"))
            self.sent = True
        if self.sent and not self.scheduler.busy:
            self.timer.stop()
