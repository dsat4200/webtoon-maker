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
        # Cancel sources captured with the previous isolation before repainting.
        self._effect_jobs.cancel()
        self._modifier_render_cache.clear()
        self._modifier_render_cache_bytes = 0
        self._modifier_source_cache.clear()
        self._modifier_source_cache_bytes = 0
        self._invalidate_scene_cache()
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

    def _solo_filter_entries(self):
        if (getattr(self, "_solo_suspended", False)
                or getattr(self, "_rendering_halftone_source", False)
                or getattr(self, "_rendering_mask_contributor", 0)):
            return set()
        return self.solo_entities

    def _solo_signature(self) -> tuple:
        return tuple(sorted(self._solo_filter_entries()))

    def _solo_content_visible(self, kind: str, identifier: str) -> bool:
        entries = self._solo_filter_entries()
        if not entries or (kind, identifier) in entries:
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
        if self._solo_content_visible("layer", layer_id):
            return True
        for kind, identifier in self._solo_filter_entries():
            entity = (self.chapter.layers[identifier] if kind == "layer"
                      else self.chapter.objects[identifier])
            parent = entity.parent_id if kind == "layer" else entity.parent_layer_id
            while parent:
                if parent == layer_id:
                    return True
                ancestor = self.chapter.layers.get(parent)
                parent = ancestor.parent_id if ancestor else None
        return False
