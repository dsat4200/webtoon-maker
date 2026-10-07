"""Reuse dependency signatures only while one owning-thread capture is active.

This memo does not participate in artwork cache identity or invalidation. Every
capture recomputes dependencies from the live model, including unsignaled edits.
Recursive branches may change compatibility state; keep their results separate.
"""
from contextlib import contextmanager
from functools import wraps
from comic_editor.render.geometry_cache import geometry_scope


_MISSING = object()
_LIMIT = 2048


@contextmanager
def signature_scope(canvas):
    previous = getattr(canvas, "_render_signature_memo", _MISSING)
    canvas._render_signature_memo = {}
    try:
        with geometry_scope():
            yield
    finally:
        if previous is _MISSING:
            del canvas._render_signature_memo
        else:
            # A nested capture can run reentrant code. Its writes may not have
            # emitted a model signal, so do not reuse the outer capture's data.
            previous.clear()
            canvas._render_signature_memo = previous


def _context(canvas):
    return (
        id(canvas.chapter), id(canvas.tiles), id(canvas.images),
        canvas._document_projection.revision,
        getattr(canvas, "_history_generation", 0),
        getattr(canvas, "_interactive_render", False),
        getattr(canvas, "selected_kind", ""),
        getattr(canvas, "selected_id", ""),
        getattr(canvas, "_disk_cache_capture", False),
        getattr(canvas, "_rendering_halftone_source", False),
        getattr(canvas, "_render_excluded_object_id", ""),
        getattr(canvas, "_render_exclude_text", False),
        getattr(canvas, "_rendering_mask_contributor", 0),
        getattr(canvas, "_render_base_alpha", False),
        getattr(canvas, "_suppress_outline_for_mask", False),
        getattr(canvas, "_effect_preview_channel", "canvas"),
        getattr(canvas, "_show_on_top_phase", None),
        id(getattr(canvas, "_active_top_plan", None)),
        getattr(canvas, "_solo_suspended", False),
        getattr(canvas, "_render_cage_source", False),
        getattr(canvas, "_rendering_compound_references", False),
        getattr(canvas, "_rendering_outward_gradient", False),
        frozenset(getattr(canvas, "_render_modifier_sources", ())),
        frozenset(getattr(canvas, "_compound_stroke_building", ())),
        repr(getattr(canvas, "_tiling_capture_geometry", None)),
    )


def capture_signature(kind):
    """Keep ordinary calls uncached; reuse complete tuples within a capture."""
    def decorate(original):
        @wraps(original)
        def memoized(canvas, value, **kwargs):
            memo = getattr(canvas, "_render_signature_memo", None)
            if memo is None:
                return original(canvas, value, **kwargs)
            if kind == "parameters":
                value = tuple(value)
                identity = value
            elif kind == "object":
                identity = (id(value), kwargs.get("pixel_signature"))
            else:
                identity = value
            key = (kind, identity, _context(canvas))
            cached = memo.get(key, _MISSING)
            if cached is not _MISSING:
                return cached
            result = original(canvas, value, **kwargs)
            if len(memo) >= _LIMIT:
                memo.pop(next(iter(memo)))
            memo[key] = result
            return result
        return memoized
    return decorate
