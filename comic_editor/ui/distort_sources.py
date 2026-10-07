"""Portable, exact displacement snapshots of the artwork beneath an owner."""
from __future__ import annotations

import base64
import copy
import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF

from comic_editor.core.models import DistortModifier, GradientObject


def _target(canvas, modifier_id):
    targets = canvas.chapter.modifier_target_ids(modifier_id)
    if not targets:
        return None
    preferred = [
        (canvas.selected_kind, canvas.selected_id),
        ("object", canvas.selected_object_id),
        *getattr(canvas, "selected_entities", ()),
    ]
    return next((ref for ref in preferred if ref in targets), targets[0])


def _paint_events(canvas):
    """Walk the same bottom-to-top hierarchy and promotion phases as the scene.

    Layer fill and outline are separate: an enclosing group's border is painted
    after its clipped children. Children ignoring their parent mask paint last.
    """
    chapter = canvas.chapter
    promoted = canvas._show_on_top_plan().content

    def layer_events(identifier):
        layer = chapter.layers.get(identifier)
        if layer is None:
            return
        children = list(reversed(layer.children))
        outward = set()
        for child in children:
            obj = chapter.objects.get(child.entity_id) if child.kind == "object" else None
            if isinstance(obj, GradientObject) and canvas._is_outward_gradient(obj):
                outward.add(obj.object_id)
                yield "content", ("object", obj.object_id)
        yield "content", ("layer", identifier)
        for outside in (False, True):
            if outside:
                yield "outline", ("layer", identifier)
            for child in children:
                if child.entity_id in outward or canvas._child_ignores_parent_mask(child) != outside:
                    continue
                if child.kind == "layer":
                    yield from layer_events(child.entity_id)
                else:
                    yield "content", ("object", child.entity_id)

    events = [event for page_id in reversed(chapter.root_page_ids)
              for event in layer_events(page_id)]
    if not promoted:
        return events
    return ([event for event in events if event[1] not in promoted]
            + [event for event in events if event[1] in promoted])


def _capture_plan(canvas, target):
    contents, outlines = set(), set()
    found = False
    for event, ref in _paint_events(canvas):
        if ref == target and event == "content":
            found = True
            break
        (contents if event == "content" else outlines).add(ref)
    if not found:
        return None
    branches = {identifier for kind, identifier in contents if kind == "layer"}
    for kind, identifier in contents:
        entity = (canvas.chapter.layers if kind == "layer" else canvas.chapter.objects).get(identifier)
        parent = (entity.parent_id if kind == "layer" else entity.parent_layer_id) if entity else None
        while parent:
            branches.add(parent)
            ancestor = canvas.chapter.layers.get(parent)
            parent = ancestor.parent_id if ancestor else None
    return frozenset(contents), frozenset(outlines), frozenset(branches)


def _capture_renderer(canvas, chapter, plan):
    from dataclasses import replace
    from comic_editor.render.outputs import capture_document
    from comic_editor.render.scene import DetachedSceneBackend, EvaluatedScene

    class BeneathRenderer(EvaluatedScene):
        def _independent_source(self):
            return bool(getattr(self, "_rendering_mask_contributor", 0)
                        or getattr(self, "_rendering_halftone_source", False))

        def _solo_content_visible(self, kind, identifier):
            if not self._independent_source() and (kind, identifier) not in self._capture_content:
                return False
            return super()._solo_content_visible(kind, identifier)

        def _solo_branch_visible(self, identifier):
            if not self._independent_source() and identifier not in self._capture_branches:
                return False
            return super()._solo_branch_visible(identifier)

    snapshot = getattr(canvas, 'snapshot', None) or capture_document(chapter, canvas.tiles, canvas.images)
    document = replace(snapshot.document, overflow=0., underlay=('', 0.), live_preview=False)
    snapshot = replace(snapshot, document=document, chapter=chapter, graphics_worker=None, cache_spec=None,
                       state=dict(snapshot.state, _solo_suspended=True, _text_editing=False))
    backend = DetachedSceneBackend(snapshot)
    backend.scene = BeneathRenderer(snapshot)
    backend.scene._capture_content, _, backend.scene._capture_branches = plan
    backend.scene._interactive_render = False
    return backend


def capture_distort_beneath(canvas, modifier_id: str) -> str:
    """Return a PNG encoded as base64, aligned exactly to the modifier's frame.

    This is an explicit, one-time snapshot. The map does not depend on later
    visibility edits, external files, the viewport zoom, or interactive drafts.
    No owner or missing modifier returns an empty string.
    """
    chapter = canvas.chapter
    if chapter is None:
        return ""
    modifier = chapter.modifiers.get(modifier_id)
    if not isinstance(modifier, DistortModifier):
        return ""
    target = _target(canvas, modifier_id)
    if target is None:
        return ""
    plan = _capture_plan(canvas, target)
    if plan is None:
        return ""
    if len(modifier.frame) != 4 or not all(math.isfinite(value) for value in modifier.frame):
        raise ValueError("The displacement map needs a valid frame.")
    frame = QRectF(*modifier.frame)
    if frame.isEmpty():
        return ""
    width, height = max(1, math.ceil(frame.width())), max(1, math.ceil(frame.height()))
    if width * height > 64 * 1024 * 1024:
        raise ValueError("The displacement map frame is too large to capture. Reduce its size.")
    detached = copy.deepcopy(chapter)
    # Retain all records for geometry/masks/explicit color-source dependencies.
    # A prefix ending inside an ancestor does not include its later border.
    for identifier, layer in detached.layers.items():
        if ("layer", identifier) in plan[0] and ("layer", identifier) not in plan[1]:
            layer.border_color = "#00000000"
    from comic_editor.render.service import DocumentRenderService, RenderRequest
    from comic_editor.render.pixels import pixel_scope, display_image
    backend = _capture_renderer(canvas, detached, plan)
    try:
        request = RenderRequest(tuple(frame.getRect()), 1., (width, height), ('beneath', modifier_id),
            backend.snapshot.document.revision, clip_document=False,
            output_transform=(width/frame.width(), 0., 0., height/frame.height(),
                              -frame.x()*width/frame.width(), -frame.y()*height/frame.height()))
        result = DocumentRenderService(backend).render_region(backend.snapshot.document, request)
        if not result.exact:
            raise RuntimeError(result.error or 'Could not capture displacement source')
        with pixel_scope(backend.snapshot.document.pixel_contract, environment=backend.pixel_environment):
            # Embedded maps retain byte data representing the pinned display
            # appearance; they never depend on a later view/config change.
            image = display_image(result.image, backend.snapshot.document.pixel_contract)
    finally:
        backend.close()
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.WriteOnly):
        raise OSError("Could not encode the displacement map image.")
    try:
        if not image.save(buffer, "PNG"):
            raise OSError("Could not encode the displacement map image.")
    finally:
        buffer.close()
    return base64.b64encode(bytes(data)).decode("ascii")
