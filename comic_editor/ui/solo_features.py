"""Temporary, per-tab isolation without changing document visibility flags."""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QRectF


class SoloFeatures:
    @property
    def solo_entities(self) -> set[tuple[str, str]]:
        chapter = self.chapter
        if chapter is None:
            return set()
        return {
            (kind, identifier)
            for kind, identifier in self._solo_entities
            if identifier in (chapter.layers if kind == "layer" else chapter.objects)
        }

    def set_solo_entities(self, entities) -> None:
        previous = self.solo_entities
        self._solo_entities = {tuple(entity) for entity in entities
                               if entity[0] in {"layer", "object"}}
        self._solo_entities = self.solo_entities
        if self._solo_entities == previous:
            return
        # Pending work belongs to the previous visibility, but exact images
        # remain reusable: layer source/output keys include the solo signature,
        # and object pixels plus independent masks do not change with isolation.
        self._effect_jobs.cancel(clear_retained=False)
        # Projection configuration includes solo. Keep the previous scene's
        # valid tiles available for returning from a temporary hover preview.
        self._invalidate_scene_cache(projection=False)
        self.soloChanged.emit(self.solo_entities)
        self.visualChanged.emit(QRectF())
        self.update()

    def toggle_solo(self, kind: str, identifier: str) -> None:
        entries = self.solo_entities
        key = (kind, identifier)
        if key in entries:
            entries.remove(key)
        else:
            entries.add(key)
        self.set_solo_entities(entries)

    @contextmanager
    def without_solo(self):
        """Export and independent source captures retain normal visibility."""
        previous = getattr(self, "_solo_suspended", False)
        self._solo_suspended = True
        try:
            yield
        finally:
            self._solo_suspended = previous

    def _solo_filter_suspended(self):
        return (getattr(self, "_solo_suspended", False)
                or getattr(self, "_rendering_halftone_source", False)
                or getattr(self, "_rendering_mask_contributor", 0))

    def _mask_wand_isolation_active(self):
        return (getattr(self, "_mask_wand_sample_entities", None) is not None
                and not self._solo_filter_suspended())

    def _solo_filter_entries(self):
        if self._solo_filter_suspended():
            return set()
        samples = getattr(self, "_mask_wand_sample_entities", None)
        if samples is not None:
            return samples
        return self.solo_entities

    def _solo_signature(self) -> tuple:
        entries = tuple(sorted(self._solo_filter_entries()))
        # Ordinary solo retains promoted artwork. A wand sample must exclude
        # unrelated promoted objects too, so its subtree pixels need a distinct
        # source identity even when the selected entries match ordinary solo.
        return ("mask-wand", entries) if self._mask_wand_isolation_active() else entries

    def _solo_content_visible(self, kind: str, identifier: str) -> bool:
        if not self._show_on_top_content_visible(kind, identifier):
            return False
        entries = self._solo_filter_entries()
        sampling = self._mask_wand_isolation_active()
        if ((not entries and not sampling) or (kind, identifier) in entries
                or (not sampling and self._is_show_on_top(kind, identifier))):
            return True
        entity = (self.chapter.layers.get(identifier) if kind == "layer"
                  else self.chapter.objects.get(identifier))
        if entity is None:
            return False
        parent = entity.parent_id if kind == "layer" else entity.parent_layer_id
        while parent:
            if ("layer", parent) in entries:
                return True
            ancestor = self.chapter.layers.get(parent)
            parent = ancestor.parent_id if ancestor else None
        return False

    def _solo_branch_visible(self, layer_id: str) -> bool:
        if not self._show_on_top_branch_visible(layer_id):
            return False
        if self._solo_content_visible("layer", layer_id):
            return True
        if not self._mask_wand_isolation_active() and self._show_on_top_solo_branch(layer_id):
            return True
        for kind, identifier in self._solo_filter_entries():
            entity = (self.chapter.layers.get(identifier) if kind == "layer"
                      else self.chapter.objects.get(identifier))
            if entity is None:
                continue
            parent = entity.parent_id if kind == "layer" else entity.parent_layer_id
            while parent:
                if parent == layer_id:
                    return True
                ancestor = self.chapter.layers.get(parent)
                parent = ancestor.parent_id if ancestor else None
        return False
