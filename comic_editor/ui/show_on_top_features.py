"""Render promoted artwork in a final hierarchy pass without moving records."""
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class TopPlan:
    entries: frozenset
    content: frozenset
    branches: frozenset


class ShowOnTopFeatures:
    def _show_on_top_plan(self):
        chapter = self.chapter
        if chapter is None:
            return TopPlan(frozenset(), frozenset(), frozenset())
        entries = {
            (kind, identifier)
            for kind, records in (("layer", chapter.layers), ("object", chapter.objects))
            for identifier, item in records.items() if item.show_on_top
        }
        content, branches = set(entries), set()
        pending = [identifier for kind, identifier in entries if kind == "layer"]
        visited = set()
        while pending:
            identifier = pending.pop()
            if identifier in visited:
                continue
            visited.add(identifier)
            layer = chapter.layers.get(identifier)
            if layer is None:
                continue
            for child in layer.children:
                content.add((child.kind, child.entity_id))
                if child.kind == "layer":
                    pending.append(child.entity_id)
        for kind, identifier in content:
            if kind == "layer":
                branches.add(identifier)
        for kind, identifier in entries:
            item = (chapter.layers if kind == "layer" else chapter.objects)[identifier]
            parent = item.parent_id if kind == "layer" else item.parent_layer_id
            while parent and parent not in branches:
                branches.add(parent)
                ancestor = chapter.layers.get(parent)
                parent = ancestor.parent_id if ancestor else None
        return TopPlan(frozenset(entries), frozenset(content), frozenset(branches))

    def _show_on_top_bypassed(self):
        return bool(getattr(self, "_rendering_mask_contributor", 0)
                    or getattr(self, "_rendering_halftone_source", False))

    def _is_show_on_top(self, kind, identifier):
        plan = getattr(self, "_active_top_plan", None)
        if plan is not None:
            return (kind, identifier) in plan.content
        chapter = self.chapter
        item = (chapter.layers if kind == "layer" else chapter.objects).get(identifier) if chapter else None
        while item is not None:
            if item.show_on_top:
                return True
            parent = item.parent_id if kind == "layer" else item.parent_layer_id
            kind = "layer"
            item = chapter.layers.get(parent)
        return False

    def _show_on_top_content_visible(self, kind, identifier):
        phase = getattr(self, "_show_on_top_phase", None)
        if phase is None or self._show_on_top_bypassed():
            return True
        return self._is_show_on_top(kind, identifier) == (phase == "top")

    def _show_on_top_branch_visible(self, layer_id):
        phase = getattr(self, "_show_on_top_phase", None)
        if phase is None or self._show_on_top_bypassed():
            return True
        plan = self._active_top_plan
        return layer_id in plan.branches if phase == "top" else ("layer", layer_id) not in plan.content

    def _show_on_top_solo_branch(self, layer_id):
        plan = getattr(self, "_active_top_plan", None) or self._show_on_top_plan()
        return layer_id in plan.branches

    def _show_on_top_standalone_contributor(self, layer):
        # A promoted operand has its own visible artwork above the ordinary
        # compound. Contributors inside a promoted compound remain combined.
        return bool(getattr(self, "_show_on_top_phase", None) == "top"
                    and not self._show_on_top_bypassed()
                    and self._is_show_on_top("layer", layer.layer_id)
                    and not self._is_show_on_top("layer", layer.parent_id))

    def _show_on_top_signature(self):
        phase = getattr(self, "_show_on_top_phase", None)
        if phase is None or self._show_on_top_bypassed():
            return ()
        return phase, tuple(sorted(self._active_top_plan.entries))

    def _effect_request_scope(self, kind, identifier):
        scope = (kind, identifier, getattr(self, "_effect_preview_channel", "canvas"))
        phase = getattr(self, "_show_on_top_phase", None)
        # The worker retains only the latest request per scope. Distinct
        # hierarchy passes must not cancel each other's source/effect work.
        return (*scope, "show-on-top", phase) if phase is not None and not self._show_on_top_bypassed() else scope

    def _show_on_top_ordered(self, candidates):
        content = self._show_on_top_plan().content
        return sorted(candidates, key=lambda item: item not in content)

    def _show_on_top_live_ink(self):
        return bool((self.settings.predictive_ink and self._predictive is not None
                     or self._vector_gesture_mode == "pencil" and self._vector_samples)
                    and self._show_on_top_plan().entries)

    @contextmanager
    def _show_on_top_scene(self):
        previous = (getattr(self, "_active_top_plan", None),
                    getattr(self, "_show_on_top_phase", None))
        plan = self._show_on_top_plan()
        self._active_top_plan = plan
        self._show_on_top_phase = None
        try:
            yield ("base", "top") if plan.entries else (None,)
        finally:
            self._active_top_plan, self._show_on_top_phase = previous

    def _render_scene_layers(self, painter, visible_world, *, underlay=False,
                             page_contents_only=False, live_ink=False):
        with self._show_on_top_scene() as phases:
            for phase in phases:
                self._show_on_top_phase = phase
                for page_id in reversed(self.chapter.root_page_ids):
                    page = self.chapter.layers[page_id]
                    if not page_contents_only:
                        self._render_layer(painter, page, 1., visible_world)
                        continue
                    if not page.visible or page.opacity <= 0 or not self._solo_branch_visible(page_id):
                        continue
                    # The overflow view bypasses only the page's outer clip.
                    painter.save()
                    transform = self.layer_world_transform(page_id)
                    painter.setTransform(transform, True)
                    inverse, valid = transform.inverted()
                    local_visible = inverse.mapRect(visible_world) if valid else visible_world
                    for child in reversed(page.children):
                        if child.kind == "layer":
                            self._render_layer(painter, self.chapter.layers[child.entity_id],
                                               page.opacity, visible_world)
                        else:
                            self._render_object(painter, self.chapter.objects[child.entity_id],
                                                page.opacity, local_visible)
                    painter.restore()
                if underlay:
                    self._render_selected_drawing_underlay(painter, visible_world)
                if live_ink:
                    # These transient strokes normally paint above the scene.
                    # The phase filter places ordinary ink below promoted art.
                    self._draw_predictive_ink(painter)
                    self._draw_live_vector_gesture(painter)
