"""Private live-canvas draft policy; exact/native callers retain their policy.

This policy belongs to one detached preview evaluation. Its cancellation event
is the worker's demand token, not a document/editor callback. Source captures
keep their complete native frame. Only existing transient kernel helpers reduce
temporary input/output samples; those values never leave the preview owner.
"""
from dataclasses import dataclass, field
from typing import Callable
import math


@dataclass(frozen=True)
class LiveCanvasPreviewPolicy:
    document_identity: tuple
    revision: int
    serial: int
    cancelled: Callable[[], bool] = field(compare=False, repr=False)
    edge: int = 224
    pixels: int = 32768

    @property
    def signature(self):
        return ("live-canvas-draft-policy", 4, self.edge, self.pixels, 512, 256)

    def matches(self, snapshot, document, request):
        return (document == snapshot.document and document.live_preview
            and not document.contact_only
            and document.identity == self.document_identity
            and document.revision == self.revision
            and request.quality.value == "interactive"
            and request.key == ("detached-preview", self.serial)
            and request.target is None and request.revision == self.revision)

    def check_cancelled(self):
        if self.cancelled():
            from comic_editor.render.admission import WorkCancelled
            raise WorkCancelled()

    def scale(self, width, height):
        return min(1., self.edge / max(1, width, height),
                   math.sqrt(self.pixels / max(1, width * height)))


def live_canvas_policy(scene):
    policy = getattr(scene, "_live_canvas_preview_policy", None)
    if not isinstance(policy, LiveCanvasPreviewPolicy):
        return None
    snapshot = getattr(scene, "snapshot", None)
    if (snapshot is None or not snapshot.document.live_preview
            or snapshot.document.contact_only
            or snapshot.document.identity != policy.document_identity
            or snapshot.document.revision != policy.revision
            or getattr(scene, "_effect_preview_channel", "canvas") != "canvas"
            or snapshot.document.pixel_contract.floating
            or snapshot.document.overflow or snapshot.document.underlay != ("", 0.)):
        return None
    # Pure source/mask/coupled captures keep native sampling. This first policy
    # intentionally excludes floating documents and unsupported special modes.
    for name in ("_render_base_alpha", "_rendering_mask_contributor",
                 "_render_modifier_sources", "_render_cage_source",
                 "_rendering_halftone_source", "_rendering_compound_references",
                 "_rendering_outward_gradient", "_tiling_capture_geometry"):
        if getattr(scene, name, None):
            return None
    return policy


def check_live_canvas_cancelled(scene):
    policy = live_canvas_policy(scene)
    if policy is not None:
        policy.check_cancelled()
    return policy
