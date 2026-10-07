"""Conservative painted regions for modifier parameter edits.

Only presentation invalidation changes here. Source/effect identities and exact
rendering keep their ordinary dependency checks and sampling contracts.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QRectF
from shiboken6 import isValid

from comic_editor.ui.halftone_source import references_entity


def modifier_edit_bounds(canvas, modifier_id: str) -> QRectF | None:
    """Return every linked owner's painted extent, or request full invalidation.

    An ancestor effect or compound can spread a changed descendant beyond its
    own extent. Masks and external color sampling can affect otherwise unrelated
    owners. Keep the established full invalidation for all such dependencies and
    for stacks whose existing render-bounds calculation cannot prove a footprint.
    """
    chapter = canvas.chapter
    if chapter is None:
        return None
    targets = chapter.modifier_target_ids(modifier_id)
    if not targets:
        return None
    for kind, identifier in targets:
        target = chapter.modifier_target(kind, identifier)
        if target is None:
            return None
        parent_id = target.parent_id if kind == "layer" else target.parent_layer_id
        ancestors = chapter.ancestor_layers(parent_id) if parent_id else []
        if (kind == "layer" and target.compound_enabled) or any(
            ancestor.compound_enabled or canvas._has_active_modifiers(ancestor.modifier_ids)
            for ancestor in ancestors
        ):
            return None
        if references_entity(canvas, kind, identifier):
            return None
        affected = {(kind, identifier), *(('layer', layer.layer_id) for layer in ancestors)}
        pending = [identifier] if kind == "layer" else []
        seen = set()
        while pending:
            layer_id = pending.pop()
            if layer_id in seen:
                return None
            seen.add(layer_id)
            for child in chapter.layers[layer_id].children:
                affected.add((child.kind, child.entity_id))
                if child.kind == "layer":
                    pending.append(child.entity_id)
        if any(affected.intersection(mask.contributors) for mask in chapter.masks.values()):
            return None

    # Parameter edits need fresh bounds both before and after mutation. Cached
    # culling bounds may describe the preceding slider value or a moved owner.
    bounds = canvas._render_bounds
    bounds.clear()
    bounds.prepare()
    result = QRectF()
    for kind, identifier in targets:
        painted = bounds.entity_bounds(kind, identifier)
        if painted is None or painted.isEmpty() or not all(
            math.isfinite(value) for value in painted.getRect()
        ):
            return None
        result = result.united(painted)
    # Include antialiasing support independently of camera/display density.
    return result.adjusted(-2., -2., 2., 2.)


def modifier_edit_dirty(before: QRectF | None, after: QRectF | None) -> QRectF | None:
    """Keep vacated old halos as well as newly painted modifier output dirty."""
    return before.united(after) if before is not None and after is not None else None


def clear_parameter_preview(canvas, owner) -> bool:
    """Release only the inspector that owns this transient presentation scope."""
    if not isValid(canvas) or getattr(canvas, "_modifier_parameter_drag_owner", None) is not owner:
        return False
    canvas._modifier_parameter_drag_id = None
    canvas._modifier_parameter_drag_owner = None
    canvas._mesh_warp_parameter_drag_id = None
    canvas._smudge_parameter_drag_id = None
    # Live presentation bypasses finished projection tiles. Source edits have
    # already invalidated their affected tiles, so release only retires the
    # screen image containing the draft, retaining unrelated exact artwork.
    canvas._invalidate_scene_cache(projection=False)
    canvas.update()
    return True
