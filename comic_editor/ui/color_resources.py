"""GUI readiness for immutable color resources built by a detached owner."""
import time

from PySide6.QtCore import QObject, QTimer

from comic_editor.render.pixels import (ColorEnvironment, schedule_color_environment,
                                        scoped_color_environment)


BUILTIN = ColorEnvironment(('builtin-srgb-v1',))


class CanvasColorResources(QObject):
    # Periodic detached validation also detects external config/LUT edits when
    # the widget is idle. GUI lookups read only already captured metadata.
    CHECK_SECONDS = .5

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.binding = None
        self.serial = 0
        self.environment = None
        self.error = None
        self.future = None
        self.contract = None
        self.checked = 0.
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.poll)
        self.destroyed.connect(self._cancel_future)

    def shutdown(self):
        self.timer.stop()
        self._cancel_future()

    def _cancel_future(self, *_args):
        if self.future is not None:
            self.future.cancel()

    @property
    def ticket(self):
        return ('async-color', self.serial)

    def request(self, contract):
        canvas = self.canvas
        binding = (id(canvas.chapter), id(canvas.tiles), id(canvas.images), contract.signature)
        if binding != self.binding:
            self._cancel_future()
            self.binding, self.contract = binding, contract
            self.environment, self.error, self.future = None, None, None
            self.checked = 0.
            self.serial += 1
        self._start()
        return self.ticket

    def _start(self):
        if self.future is None and time.monotonic()-self.checked >= self.CHECK_SECONDS:
            self.future = schedule_color_environment(self.contract)
        if not self.timer.isActive():
            self.timer.start()

    def poll(self):
        canvas = self.canvas
        chapter = canvas.chapter
        if chapter is None or not chapter.pixel_contract.floating or not chapter.pixel_contract.ocio_config:
            self._cancel_future()
            self.binding = None
            self.future = None
            self.timer.stop()
            return
        self.request(chapter.pixel_contract)
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        self.checked = time.monotonic()
        previous, previous_error = self.environment, self.error
        previous_state = (getattr(previous, 'signature', None),
            (type(previous_error).__name__, str(previous_error)) if previous_error is not None else None)
        try:
            environment = future.result()
            self.environment, self.error = environment, None
            if (previous is not None and previous.signature != environment.signature or
                    previous_error is not None):
                self.serial += 1
        except Exception as error:
            self.error = error
            if previous is not None and (previous_error is None or
                    (type(previous_error), str(previous_error)) != (type(error), str(error))):
                self.serial += 1
        # Initial pending -> ready preserves the immutable consumer ticket.
        # A new environment advances it, retiring work for the old resource
        # revision without changing any editable document record or pixel grid.
        state = (getattr(self.environment, 'signature', None),
            (type(self.error).__name__, str(self.error)) if self.error is not None else None)
        if state != previous_state:
            coordinator = getattr(canvas, '_disk_cache_controller', None)
            if coordinator is not None and coordinator.backing is not None:
                coordinator._reset_status()
            canvas.update()
            canvas.visualChanged.emit(None)

    def ready(self, ticket):
        if ticket != self.ticket:
            return None
        if self.error is not None:
            raise self.error
        return self.environment


def canvas_color_resources(canvas):
    owner = getattr(canvas, '_color_resources', None)
    if owner is None:
        owner = canvas._color_resources = CanvasColorResources(canvas)
    return owner


def projection_color_identity(canvas, contract):
    if not contract.floating or not contract.ocio_config:
        return BUILTIN.signature
    return canvas_color_resources(canvas).request(contract)


def captured_canvas_environment(canvas, contract, ticket):
    if not contract.floating or not contract.ocio_config:
        return BUILTIN
    owner = canvas_color_resources(canvas)
    owner.request(contract)
    return owner.ready(ticket)


def semantic_color_identity(canvas, contract):
    environment = metadata_color_environment(canvas, contract)
    return environment.signature if environment is not None else ('color-resources-pending',)


def metadata_color_environment(canvas, contract):
    """Already captured resources for dependency/status inspection."""
    captured = scoped_color_environment(contract)
    if captured is not None:
        return captured
    if not contract.floating or not contract.ocio_config:
        return BUILTIN
    # Only the editor has this readiness owner. Detached evaluators always use
    # pixel_scope, while explicit non-editor references may capture directly.
    if hasattr(canvas, '_scene_controller'):
        owner = canvas_color_resources(canvas)
        owner.request(contract)
        # Capture owns the diagnostic. Metadata/cache status lookups must not
        # turn an invalid external resource into an exception during paint.
        return owner.environment if owner.error is None else None
    from comic_editor.render.pixels import capture_color_environment
    return capture_color_environment(contract)
