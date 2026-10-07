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
from comic_editor.ui.render_signatures import signature_scope


def source_capture_key(canvas, key):
    """Include the actual source route and isolated statistics contents."""
    if key is None:
        return None
    from comic_editor.render.source_context import source_color_context, contextual_source_key
    chapter = getattr(canvas, 'chapter', None)
    key = contextual_source_key(key, source_color_context(getattr(chapter, 'pixel_contract', None)))
    if (key is not None and getattr(canvas, '_posterize_statistics_capture', False)
            and getattr(canvas, '_effect_preview_channel', 'canvas') == 'posterize-statistics'):
        return ('posterize-statistics-source',
                bool(getattr(canvas, '_rendering_compound_references', False)),
                bool(getattr(canvas, '_rendering_outward_gradient', False)), key)
    from comic_editor.ui.acquired_source_preview import context
    preview = context(canvas)
    return contextual_source_key(key, preview["token"]) if preview is not None else key


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
                and document.pixel_contract == canvas.chapter.pixel_contract
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
            "_bounded_effect_preview": (request.quality is RenderQuality.INTERACTIVE
                                        and request.key != ('stroke-preview',)),
            "_stroke_projection_active": (request.quality is RenderQuality.INTERACTIVE
                                           and request.key == ('stroke-preview',)),
            "_live_underlay_object_id": document.underlay[0],
            "_live_underlay_amount": document.underlay[1],
        }
        missing = object()
        previous = {name: getattr(canvas, name, missing) for name in values}
        provisional = getattr(canvas, "_effect_provisional_revision", 0)
        preview_capture = False
        try:
            for name, value in values.items():
                setattr(canvas, name, value)
            canvas._projection_captured_live_preview = (
                getattr(canvas, "_projection_captured_live_preview", False) or document.live_preview)
            from comic_editor.ui.acquired_source_preview import context
            preview_capture = request.quality is RenderQuality.INTERACTIVE and context(canvas) is not None
            with signature_scope(canvas):
                canvas._render_bounds.prepare()
                yield state
        finally:
            # All fallible source work is inside try; flag restoration
            # cannot be bypassed by a deleted pin or a changed source context.
            state.provisional = (provisional != getattr(canvas, "_effect_provisional_revision", 0)
                or preview_capture)
            for name, value in previous.items():
                if value is missing:
                    delattr(canvas, name)
                else:
                    setattr(canvas, name, value)

    def paint(self, painter, visible, *, phase=None, page_contents_only=False):
        if page_contents_only:
            self.canvas._render_scene_layers(painter, visible, page_contents_only=True)
        else:
            values = {"live_ink": True} if getattr(self.canvas, "_capture_live_ink", False) else {}
            self.canvas._render_scene_layers(painter, visible, underlay=True, only_phase=phase, **values)

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
