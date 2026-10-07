"""Control flow for exact projection dependencies that are not ready yet.

These exceptions never represent draft pixels. The GUI retries a capture after
an immutable worker result arrives, then publishes only a complete view.
"""


from comic_editor.render.service import RenderPending, RenderFailed


class ProjectionPending(RenderPending):
    def __init__(self, scope=None, key=None):
        super().__init__("Exact projection effect is pending")
        self.scope, self.key = scope, key


class ProjectionFailed(RenderFailed):
    def __init__(self, scope=None, key=None, message="Exact effect rendering failed"):
        super().__init__(str(message))
        self.scope, self.key = scope, key


def projection_deferred(canvas):
    """Opt in only ordinary interactive, exact document captures."""
    statistics = bool(getattr(canvas, "_posterize_statistics_capture", False)
                      and getattr(canvas, "_effect_preview_channel", "canvas") == "posterize-statistics")
    return bool(getattr(canvas, "_projection_exact", False)
                and getattr(canvas, "_projection_defer_effects", False)
                and canvas._interactive_render
                and not canvas._render_base_alpha
                and canvas._rendering_mask_contributor <= 0
                and not getattr(canvas, "_rendering_halftone_source", False)
                and (statistics or not getattr(canvas, "_render_modifier_sources", ()))
                and not getattr(canvas, "_render_cage_source", False)
                and getattr(canvas, "_tiling_capture_geometry", None) is None
                and (statistics or not getattr(canvas, "_rendering_compound_references", False))
                and (statistics or not getattr(canvas, "_rendering_outward_gradient", False))
                and (statistics or getattr(canvas, "_effect_preview_channel", "canvas") in {"canvas", "overflow"}))


def projection_result_or_pending(canvas, scope, key):
    """Consume exact pixels or avoid recopying an already queued snapshot.

    ``EffectJobs.result`` also reports terminal failures and adopts a completed
    worker before its next timer tick. A missing request returns None so the
    caller can prepare detached inputs and queue it.
    """
    jobs = canvas._effect_jobs
    result = jobs.result(scope, key)
    if result is not None:
        return result
    if jobs.has_running(scope, key):
        raise ProjectionPending(scope, key)
    pending = jobs.pending.get(scope)
    if pending is not None and pending[1] == key:
        raise ProjectionPending(scope, key)
    if getattr(jobs, "waiting", {}).get(scope) == key:
        raise ProjectionPending(scope, key)
    return None
