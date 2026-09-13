"""Shared raster area selections without merging the selected drawings."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF

from comic_editor.core.commands import CallbackCommand
from comic_editor.core.models import RasterObject


class MultiRasterSelectionFeatures:
    def _drawing_selection_raster_targets(self) -> list[RasterObject]:
        if self.chapter is None:
            return []
        targets = {}
        visited = set()

        def add_object(obj):
            if not isinstance(obj, RasterObject) or not obj.visible or obj.mask_only:
                return
            if not self._solo_content_visible("object", obj.object_id):
                return
            parent_id = obj.parent_layer_id
            while parent_id:
                parent = self.chapter.layers.get(parent_id)
                if parent is None or not parent.visible or parent.mask_only:
                    return
                parent_id = parent.parent_id
            if self._drawing_local_to_world_transform(obj).isInvertible():
                targets[obj.object_id] = obj

        def visit(layer_id):
            if layer_id in visited:
                return
            visited.add(layer_id)
            layer = self.chapter.layers.get(layer_id)
            if layer is None or not layer.visible or layer.mask_only:
                return
            for child in layer.children:
                if child.kind == "layer":
                    visit(child.entity_id)
                else:
                    add_object(self.chapter.objects.get(child.entity_id))

        for kind, identifier in self.selected_entities:
            if kind == "layer":
                visit(identifier)
            elif kind == "object":
                obj = self.chapter.objects.get(identifier)
                if not isinstance(obj, RasterObject):
                    return []
                add_object(obj)
            else:
                return []
        return list(targets.values())

    def _prepare_multi_raster_selection(self, anchor: RasterObject) -> None:
        self._selection_raster_states.clear()
        if len(self.selected_entities) < 2:
            return
        world_path = self._drawing_local_to_world_transform(anchor).map(
            self._drawing_selection_path
        )
        for obj in self._drawing_selection_raster_targets():
            inverse, valid = self._drawing_local_to_world_transform(obj).inverted()
            if not valid:
                continue
            self._selection_raster_states[obj.object_id] = {
                "before_tiles": self.tiles.object_tiles(obj.object_id),
                "source_path": inverse.map(world_path),
                "overlay_tiles": None,
            }
        self._selection_raster_snapshot = self._selection_snapshot()

    def _finish_multi_raster_selection(self, anchor: RasterObject) -> None:
        before_selection = self._selection_raster_snapshot
        start, destination = self._selection_transform_start_quad, self._selection_transform_quad
        anchor_mapping = self._drawing_local_to_world_transform(anchor)
        commands = []
        dirty = QRectF()
        for identifier, state in self._selection_raster_states.items():
            obj = self.chapter.objects.get(identifier)
            if not isinstance(obj, RasterObject):
                continue
            mapping = self._drawing_local_to_world_transform(obj)
            command = self._commit_raster_selection_transform(
                obj, state["before_tiles"], state["overlay_tiles"],
                source_path=state["source_path"], record=False,
            )
            if command is not None:
                commands.append(command)
                footprint = mapping.map(command.before_state["path"]).boundingRect().united(
                    mapping.map(command.after_state["path"]).boundingRect()
                )
                dirty = dirty.united(self.modifier_expanded_dirty(identifier, footprint))
        inverse, valid = anchor_mapping.inverted()
        if valid and start and destination:
            movement = self._quad_to_quad_transform(start, destination)
            self._drawing_selection_path = inverse.map(movement.map(
                anchor_mapping.map(self._drawing_selection_path)
            ))
        after_selection = self._selection_snapshot()
        before_quad, after_quad = list(start or []), list(destination or [])
        pivot = QPointF(self._selection_pivot) if self._selection_pivot is not None else None
        pivot_custom = self._selection_pivot_custom
        self._selection_raster_states.clear()
        self._selection_raster_snapshot = None

        def restore(forward):
            self._selection_raster_states.clear()
            self._selection_before_tiles = None
            self._selection_overlay_tiles = None
            for command in commands:
                command.redo() if forward else command.undo()
            self._restore_selection_snapshot(after_selection if forward else before_selection)
            self._selection_transform_quad = list(after_quad if forward else before_quad)
            self._selection_pivot = QPointF(pivot) if pivot is not None else None
            self._selection_pivot_custom = pivot_custom
            self.documentChanged.emit(dirty)
            self.update()

        if commands:
            self.command_stack.push(CallbackCommand(
                "Transform raster selections", lambda: restore(True), lambda: restore(False),
            ), already_done=True)
            self.documentChanged.emit(dirty)
