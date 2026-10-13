"""Private live-canvas draft policy; exact/native callers retain their policy.

This policy belongs to one detached preview evaluation. Its cancellation event
is the worker's demand token, not a document/editor callback. Source frames
retain their complete document-space bounds. Guarded cage target/ancestor
sources may draw into a bounded private draft device before the deformation;
exact, mask, floating and other special captures retain native samples. These
temporary input/output samples never leave the preview owner or enter durable
exact caches.
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


@dataclass(frozen=True)
class VisibilityCanvasPreviewPolicy(LiveCanvasPreviewPolicy):
    """A current one-shot committed presentation, isolated from native caches."""
    def matches(self, snapshot, document, request):
        return (document == snapshot.document and not document.live_preview
            and not document.contact_only and snapshot.cache_spec is None
            and document.identity == self.document_identity
            and document.revision == self.revision
            and request.quality.value == 'interactive'
            and request.key == ('detached-preview', self.serial)
            and request.target is None and request.revision == self.revision)


def live_canvas_policy(scene):
    policy = getattr(scene, "_live_canvas_preview_policy", None)
    if not isinstance(policy, LiveCanvasPreviewPolicy):
        return None
    snapshot = getattr(scene, "snapshot", None)
    if (snapshot is None or not (snapshot.document.live_preview
                or isinstance(policy, VisibilityCanvasPreviewPolicy))
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


def live_geometry_source_policy(scene, target=None):
    """Bound source captures only inside an explicitly owned geometry draft.

    Original files/tiles, native masks and exact outputs retain their ordinary
    sampling. The smaller device spans the same complete local source frame;
    this private evaluator cannot transfer its derived caches to native work.
    """
    from comic_editor.render.geometry_capture import GeometrySceneSnapshot
    snapshot = getattr(scene, 'snapshot', None)
    visibility = isinstance(getattr(scene, '_live_canvas_preview_policy', None),
                            VisibilityCanvasPreviewPolicy)
    if (snapshot is None or snapshot.cache_spec is not None
            or not visibility and (not isinstance(snapshot, GeometrySceneSnapshot)
                or snapshot.interaction_preview_owner is None
                or snapshot.capture_lease is None or snapshot.capture_lease.lease.released)):
        return None
    if target is not None:
        from comic_editor.core.models import RasterObject, LayerNode
        if isinstance(target, RasterObject):
            # A tiny device still has to read every original in a complete
            # source frame. Large sparse drawings retain the native viewport
            # dependency route instead of decoding hundreds of offscreen cells.
            native = scene.tiles._tiles.get(target.object_id)
            if native is not None and len(native) > 32:
                return None
        elif isinstance(target, LayerNode):
            pending, visited, keys = [target.layer_id], set(), 0
            while pending:
                identifier = pending.pop()
                if identifier in visited:
                    continue
                visited.add(identifier)
                if len(visited) > 128:
                    return None
                for child in scene.chapter.layers[identifier].children:
                    if child.kind == 'layer':
                        pending.append(child.entity_id)
                    else:
                        native = scene.tiles._tiles.get(child.entity_id)
                        keys += len(native) if native is not None else 0
                        if keys > 128:
                            return None
    return live_canvas_policy(scene)


def live_cage_source_policy(scene):
    """Only an explicitly bounded live ancestor may pass its policy inward."""
    policy = live_canvas_policy(scene)
    if policy is not None:
        return policy
    policy = getattr(scene, '_cage_preview_source_policy', None)
    snapshot = getattr(scene, 'snapshot', None)
    if (not isinstance(policy, LiveCanvasPreviewPolicy) or snapshot is None
            or not (snapshot.document.live_preview
                or isinstance(policy, VisibilityCanvasPreviewPolicy)) or snapshot.document.contact_only
            or snapshot.document.identity != policy.document_identity
            or snapshot.document.revision != policy.revision
            or snapshot.document.pixel_contract.floating
            or snapshot.document.overflow or snapshot.document.underlay != ('', 0.)
            or getattr(scene, '_effect_preview_channel', 'canvas') != 'canvas'):
        return None
    for name in ('_render_base_alpha', '_rendering_mask_contributor', '_render_cage_source',
                 '_rendering_halftone_source', '_rendering_compound_references',
                 '_rendering_outward_gradient', '_tiling_capture_geometry'):
        if getattr(scene, name, None):
            return None
    return policy
