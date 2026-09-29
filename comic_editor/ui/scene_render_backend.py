"""Owning-thread bridge from explicit requests to the existing scene kernels.

All temporary canvas compatibility flags live here. The document render service
does not depend on this adapter or on a QWidget. This backend is synchronous;
it must never be sent to an effect worker with the live document attached.
"""
from contextlib import contextmanager
import weakref

from PySide6.QtCore import QThread
from PySide6.QtGui import QPainterPath

from comic_editor.render.service import CaptureState, RenderQuality


class CanvasSceneBackend:
    def __init__(self, canvas):
        self._owner = weakref.ref(canvas)

    @property
    def canvas(self):
        owner = self._owner()
        if owner is None:
            raise RuntimeError("Scene backend owner is no longer available")
        return owner

    def matches(self, document):
        canvas = self._owner()
        if canvas is not None and QThread.currentThread() != canvas.thread():
            raise RuntimeError("The live scene backend must run on the canvas thread")
        return (canvas is not None and canvas.chapter is not None
                and document.identity == (id(canvas.chapter), id(canvas.tiles), id(canvas.images))
                and document.configuration == canvas._projection_configuration())

    @contextmanager
    def capture(self, document, request, effect_region):
        canvas = self.canvas
        state = CaptureState()
        values = {
            "_interactive_render": True,
            "_effect_viewport_world": effect_region,
            "_vector_render_scale_override": request.scale,
            "_effect_region_requests": True,
            "_projection_tile_key": (request.phase, request.key),
            "_effect_preview_channel": "canvas",
            "_projection_exact": request.quality is RenderQuality.EXACT,
            "_projection_defer_effects": request.defer_effects,
            "_live_underlay_object_id": document.underlay[0],
            "_live_underlay_amount": document.underlay[1],
        }
        missing = object()
        previous = {name: getattr(canvas, name, missing) for name in values}
        provisional = getattr(canvas, "_effect_provisional_revision", 0)
        try:
            for name, value in values.items():
                setattr(canvas, name, value)
            canvas._projection_captured_live_preview = (
                getattr(canvas, "_projection_captured_live_preview", False) or document.live_preview)
            canvas._render_bounds.prepare()
            yield state
        finally:
            state.provisional = provisional != getattr(canvas, "_effect_provisional_revision", 0)
            for name, value in previous.items():
                if value is missing:
                    delattr(canvas, name)
                else:
                    setattr(canvas, name, value)

    def paint(self, painter, visible, *, phase=None, page_contents_only=False):
        if page_contents_only:
            self.canvas._render_scene_layers(painter, visible, page_contents_only=True)
        else:
            self.canvas._render_scene_layers(painter, visible, underlay=True, only_phase=phase)

    def page_area(self):
        canvas = self.canvas
        area = QPainterPath()
        for identifier in canvas.chapter.root_page_ids:
            page = canvas.chapter.layers[identifier]
            if page.visible:
                area = area.united(canvas.layer_world_transform(identifier).map(
                    canvas.layer_effective_path(identifier)))
        return area

    @contextmanager
    def overflow_channel(self):
        canvas = self.canvas
        previous = canvas._effect_preview_channel
        underlay = canvas._live_underlay_object_id, canvas._live_underlay_amount
        canvas._effect_preview_channel = "overflow"
        canvas._live_underlay_object_id, canvas._live_underlay_amount = "", 0.
        try:
            yield
        finally:
            canvas._effect_preview_channel = previous
            canvas._live_underlay_object_id, canvas._live_underlay_amount = underlay
