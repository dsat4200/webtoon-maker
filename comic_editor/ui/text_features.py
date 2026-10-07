"""Free text placement and non-destructive layout/frame manipulation."""
import copy
import math
from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPen, QPolygonF, QTransform
from comic_editor.core.models import TextObject, object_from_dict
from comic_editor.core.document_patch import RecordSnapshot, DocumentPatch
from comic_editor.core.changes import ChangeSet, EntityChange, GROUP_KINDS
from comic_editor.ui.tool_sessions import LatestValueInput, ToolSession


class TextFeatures:
    def _text_record_snapshot(self, obj, fields):
        """Retain only the authored fields participating in this text edit."""
        return RecordSnapshot.capture(self.chapter, objects=(obj.object_id,),
                                      attributes={'objects': tuple(fields)})

    @staticmethod
    def _text_dirty_union(first: QRectF, second: QRectF) -> QRectF:
        # Empty denotes a full invalidation and must survive a frame resize.
        return QRectF() if first.isEmpty() or second.isEmpty() else first.united(second)

    def _text_frame_dirty(self, kind, identifier):
        if kind == "object":
            return self._text_visual_dirty(self.chapter.objects.get(identifier))
        layer = self.chapter.layers.get(identifier)
        if layer is None:
            return QRectF()
        dirty = QRectF()
        for ref in layer.children:
            child_dirty = self._text_visual_dirty(self.chapter.objects.get(ref.entity_id))
            if child_dirty.isEmpty():
                return QRectF()
            dirty = dirty.united(child_dirty)
        return dirty

    def _text_visual_dirty(self, obj: TextObject | None, before: dict | RecordSnapshot | None = None) -> QRectF:
        """Invalidate the clipped text frame and its existing effect dependants."""
        if not isinstance(obj, TextObject) or self.chapter is None:
            return QRectF()
        world = self.object_world_rect(obj.object_id)
        if world is None:
            return QRectF()
        if before is not None:
            record = (before.records.get('objects', {}).get(obj.object_id) if isinstance(before, RecordSnapshot)
                      else next((item for item in before["objects"]
                                 if item["id"] == obj.object_id), None))
            if record is not None:
                if isinstance(before, RecordSnapshot) and 'objects' in before.attributes:
                    original = copy.copy(obj)
                    for name, value in record.items():
                        setattr(original, name, copy.deepcopy(value))
                else:
                    original = object_from_dict(record)
                quad = (self._rect_quad(self._strict_text_rect(original))
                        if original.layout_mode == "strict" else self._text_quad(original))
                mapping = self.layer_world_transform(original.parent_layer_id)
                world = world.united(mapping.map(QPolygonF([QPointF(*p) for p in quad])).boundingRect())
        # Text is clipped to this frame even when its glyph layout changes.
        # Existing nonlocal/mask/linked-color rules conservatively fall back
        # to the whole document; finite effect support is expanded in place.
        from comic_editor.core.models import (
            BlurModifier, BrightnessContrastModifier, CurvesModifier,
            HueSaturationLightnessModifier, OutlineModifier, SolidColorOverlayModifier,
        )
        owners = [obj, *self.chapter.ancestor_layers(obj.parent_layer_id)]
        finite = (BlurModifier, BrightnessContrastModifier, CurvesModifier,
                  HueSaturationLightnessModifier, OutlineModifier, SolidColorOverlayModifier)
        if any(not isinstance(modifier, finite)
                or isinstance(modifier, OutlineModifier) and modifier.style != "solid"
                for owner in owners for modifier in self._active_modifier_instances(owner.modifier_ids)):
            return QRectF()
        dirty = self.modifier_expanded_dirty(obj.object_id, world)
        if dirty.contains(QRectF(0, 0, self.chapter.width, self.chapter.height)):
            # Empty signal rectangles invalidate every retained region,
            # including overflow and remote mask/color dependants.
            return QRectF()
        return dirty.adjusted(-2, -2, 2, 2)

    def _init_text_features(self):
        self._text_placement = None
        self._free_text_drag = None
        self._free_text_pending = None
        self._free_text_input = LatestValueInput(self._apply_free_text_drag)
        self._free_text_tool_session = None
        self._free_text_timer = QTimer(self)
        self._free_text_timer.setSingleShot(True)
        self._free_text_timer.timeout.connect(self._flush_free_text_drag)
        self._text_color_popup = None

    def _text_gizmo_color(self, obj):
        from comic_editor.core.text_styles import text_color_at

        position = (
            min(self._text_cursor_position, self._text_selection_anchor)
            if self.has_active_text_edit() else 0
        )
        return text_color_at(obj, position)

    def _open_text_color_picker(self):
        """Keep canvas selection stable while the shared picker owns focus."""
        from comic_editor.core.text_styles import apply_text_color
        from comic_editor.ui.color_picker import ColorPickerPopup

        obj = self._selected_text_for_gizmos()
        if obj is None:
            return
        if self._text_color_popup is not None:
            self._text_color_popup.raise_()
            self._text_color_popup.activateWindow()
            return
        chapter = self.chapter
        object_id = obj.object_id
        position, anchor = self._text_cursor_position, self._text_selection_anchor
        was_editing = self.has_active_text_edit()
        start, end = sorted((position, anchor)) if was_editing else (0, 0)
        if start == end:
            start, end = 0, len(obj.text)
        popup = ColorPickerPopup(self._text_gizmo_color(obj), self)
        self._text_color_popup = popup
        popup.setWindowTitle("Change text color")
        popup.setAttribute(Qt.WA_DeleteOnClose)

        def target():
            if self.chapter is not chapter:
                return None
            candidate = self.chapter.objects.get(object_id)
            return candidate if isinstance(candidate, TextObject) else None

        def apply(color):
            current = target()
            if current is None:
                return
            # Separate previous typing from the single color undo command.
            self.commit_active_text_edit()
            before = self._text_record_snapshot(current, ('text_color', 'color_runs'))
            apply_text_color(current, start, end, color)
            self._finish_text_property_change(before, "Change text color")

        def finished(_result):
            self._text_color_popup = None
            current = target()
            if current is None or self.selected_object_id != object_id:
                return
            if was_editing and self._editing_text_object() is not None:
                # Eyedropper sampling briefly switches tools; restore the
                # original selection after either Apply or Cancel as well.
                self._begin_text_session(current)
                self._text_cursor_position = min(position, len(current.text))
                self._text_selection_anchor = min(anchor, len(current.text))
            self.setFocus(Qt.OtherFocusReason)
            self.update()

        popup.colorApplied.connect(apply)
        popup.finished.connect(finished)
        popup.open()

    def set_text_layout_mode(self, obj, mode):
        """The only strict/free transition: capture layout before changing it."""
        if self.chapter.layers[obj.parent_layer_id].layer_kind == "text_container":
            mode = "free"
        if mode not in {"strict", "free"}:
            raise ValueError("Unknown text layout mode")
        if obj.layout_mode == "strict" and mode == "free":
            rect = self._strict_text_rect(obj)
            obj.x, obj.y, obj.width, obj.height = rect.x(), rect.y(), rect.width(), rect.height()
            obj.transform_quad = self._rect_quad(rect)
            obj.transform_behavior = "bounds"
        obj.layout_mode = mode

    def apply_text_properties(self, obj, properties):
        for key, value in properties.items():
            if key != "layout_mode":
                setattr(obj, key, value)
        if "layout_mode" in properties:
            self.set_text_layout_mode(obj, properties["layout_mode"])

    def prepare_text_move(self, entities, new_parent):
        """Resolve shape-aware strict layouts before hierarchy reparenting."""
        parent = self.chapter.layers.get(new_parent)
        if parent is not None and parent.layer_kind == "text_container":
            for kind, entity_id in entities:
                obj = self.chapter.objects.get(entity_id) if kind == "object" else None
                if isinstance(obj, TextObject):
                    self.set_text_layout_mode(obj, "free")

    def _selected_text_container(self):
        if self.chapter is None:
            return None
        layer = self.chapter.layers.get(self.selected_id) if self.selected_kind == "layer" else None
        if self.selected_kind == "object":
            obj = self.chapter.objects.get(self.selected_id)
            layer = self.chapter.layers.get(obj.parent_layer_id) if obj else None
        return layer if layer and layer.layer_kind == "text_container" else None

    def _text_container_bounds(self, layer):
        rect = QRectF()
        for ref in layer.children:
            obj = self.chapter.objects.get(ref.entity_id)
            if isinstance(obj, TextObject):
                quad = self._text_quad(obj)
                box = QPolygonF([QPointF(*p) for p in quad]).boundingRect()
                rect = box if rect.isNull() else rect.united(box)
        return rect if not rect.isEmpty() else QRectF(0, 0, 1, 1)

    def _text_frame_target(self):
        if self.chapter is None or len(self.selected_entities) != 1:
            return None
        if self.selected_kind == "object":
            obj = self.chapter.objects.get(self.selected_id)
            if isinstance(obj, TextObject) and obj.layout_mode == "free":
                frame = QRectF(0, 0, obj.width, obj.height)
                mapping = self._quad_transform(frame, self._text_quad(obj)) * self.layer_world_transform(obj.parent_layer_id)
                return obj, frame, mapping, obj.transform_behavior
        layer = self.chapter.layers.get(self.selected_id) if self.selected_kind == "layer" else None
        if layer and layer.layer_kind == "text_container":
            return layer, self._text_container_bounds(layer), self.layer_world_transform(layer.layer_id), layer.text_transform_behavior
        return None

    def _text_behavior_rect(self):
        target = self._text_frame_target()
        if target is None:
            return QRectF()
        _, frame, mapping, _ = target
        box = self.camera_transform().map(mapping.map(QPolygonF([QPointF(*p) for p in self._rect_quad(frame)]))).boundingRect()
        return QRectF(box.right()+12, box.bottom()+8, 92, 28)

    def set_text_transform_behavior(self, behavior):
        target = self._text_frame_target()
        if target is None or behavior not in {"bounds", "stretch"}:
            return
        self._commit_text_edit()
        entity = target[0]
        attribute = 'transform_behavior' if isinstance(entity, TextObject) else 'text_transform_behavior'
        group = 'objects' if isinstance(entity, TextObject) else 'layers'
        identifier = entity.object_id if isinstance(entity, TextObject) else entity.layer_id
        before = RecordSnapshot.capture(self.chapter, **{group: (identifier,)}, attributes={group: (attribute,)})
        setattr(entity, attribute, behavior)
        from comic_editor.ui.record_edits import commit_records
        if commit_records(self, before, 'Change text transform behavior'):
            self.interactionFinished.emit()
            self.update()

    def _text_behavior_hit(self, widget_point):
        if not self._text_behavior_rect().contains(widget_point):
            return False
        behavior = self._text_frame_target()[3]
        self.set_text_transform_behavior("stretch" if behavior == "bounds" else "bounds")
        return True

    def _draw_text_feature_overlays(self, painter):
        target = self._text_frame_target()
        if target is not None:
            entity, frame, mapping, behavior = target
            if not isinstance(entity, TextObject):
                quad = [mapping.map(QPointF(*p)).toTuple() for p in self._rect_quad(frame)]
                painter.save()
                painter.setPen(QPen(QColor("#39c7ff"), 1/self.scale, Qt.DashLine))
                painter.setBrush(Qt.NoBrush)
                painter.drawPolygon(QPolygonF([QPointF(*p) for p in quad]))
                self._draw_transform_controls(painter, quad)
                painter.restore()
            rect = self._text_behavior_rect()
            painter.save()
            painter.setTransform(QTransform())
            painter.setPen(QPen(QColor("#ffaa38"), 1))
            painter.setBrush(QColor("#3e2e18"))
            painter.drawRoundedRect(rect, 5, 5)
            painter.drawText(rect, Qt.AlignCenter, "Bounds" if behavior == "bounds" else "Stretch")
            painter.restore()
        state = self._text_placement
        if state and state.get("start") is not None:
            painter.save()
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#39c7ff"), 1/self.scale, Qt.DashLine))
            painter.drawRect(QRectF(state["start"], state["end"]).normalized())
            painter.restore()

    def begin_text_placement(self, parent_id, *, new_container=False):
        from comic_editor.ui.canvas import ToolKind
        self._commit_text_edit()
        self._text_placement = None
        previous = self.tool
        self.set_tool(ToolKind.TEXT_EDIT)
        self._text_placement = {"parent": parent_id, "new": new_container,
                                "previous_tool": previous, "start": None, "end": None}
        self.setFocus()
        self.setCursor(Qt.CrossCursor)
        self.update()

    def _text_placement_press(self, world):
        if self._text_placement is None:
            return False
        state = self._text_placement
        state["start"] = state["end"] = self._snap(world, state["parent"])
        self.update()
        return True

    def _text_placement_move(self, world):
        state = self._text_placement
        if state is None:
            return False
        if state["start"] is not None:
            state["end"] = self._snap(world, state["parent"])
            self.update()
        return True

    def _finish_text_placement(self):
        state = self._text_placement
        if state is None:
            return False
        if state["start"] is None:
            return True
        parent_id = state["parent"]
        inverse, valid = self.layer_world_transform(parent_id).inverted()
        if not valid:
            return True
        start, end = inverse.map(state["start"]), inverse.map(state["end"])
        rect = QRectF(start, end).normalized()
        if math.dist(self.document_to_widget(state["start"]).toTuple(), self.document_to_widget(state["end"]).toTuple()) < 3:
            rect = QRectF(start.x(), start.y(), 360, 120)
        rect.setWidth(max(1., rect.width())); rect.setHeight(max(1., rect.height()))
        obj = TextObject(x=rect.x(), y=rect.y(), width=rect.width(), height=rect.height(),
                         layout_mode="free", transform_quad=self._rect_quad(rect))
        preset = next((p for p in self.settings.text_presets if p["name"] == self.settings.active_text_preset), self.settings.text_presets[0])
        for key in ("font_family", "font_size", "bold", "italic", "kerning", "line_spacing",
                    "horizontal_alignment", "vertical_alignment", "margin"):
            setattr(obj, key, preset[key])
        # Structural placement retains the parent order plus tombstones for
        # the new records; unrelated drawings never enter this transaction.
        before = RecordSnapshot.capture(self.chapter, layers=(parent_id,),
                                        objects=(obj.object_id,), scalars=('size',))
        if state['new']:
            parent_id = self.chapter.add_layer(parent_id, 'Free Text', layer_kind='text_container').layer_id
            before.records['layers'][parent_id] = None
        self.chapter.add_object(parent_id, obj)
        self._text_placement = None
        self.push_model_change(before, before.after(self.chapter), "Add free text container" if state["new"] else "Add text box")
        self._emit_typed_hierarchy_changed(self._last_published_change)
        self.set_selection("object", obj.object_id)
        self.start_text_edit(select_all=True)
        self._emit_typed_document_changed(None, self._last_published_change)
        self.update()
        return True

    def _cancel_text_features(self, *, restore=False):
        state = self._text_placement
        self._text_placement = None
        self._free_text_timer.stop()
        self._free_text_pending = None
        self._free_text_input.cancel()
        if self._free_text_tool_session is not None:
            self._free_text_tool_session.cancel()
        self._free_text_tool_session = None
        drag, self._free_text_drag = self._free_text_drag, None
        if restore and drag:
            old, _ = DocumentPatch.pair(drag["before"], drag["before"].after(self.chapter))
            self._restore_history_state(old, document_patch=True)
        if restore and state:
            self.set_tool(state["previous_tool"])
        self.update()
        return state is not None or drag is not None

    def _begin_free_text_transform(self, world):
        from comic_editor.ui.canvas import ToolKind
        if self.tool not in {ToolKind.TEXT_EDIT, ToolKind.TRANSFORM, ToolKind.OBJECT_SELECT}:
            return False
        target = self._text_frame_target()
        if target is None:
            return False
        entity, frame, mapping, behavior = target
        quad = [mapping.map(QPointF(*p)).toTuple() for p in self._rect_quad(frame)]
        mode, handle = self._text_transform_control_hit(quad, world)
        if not mode and self.tool == ToolKind.TRANSFORM:
            mode, handle = self._transform_control_hit(quad, world)
        if not mode:
            return False
        live_effects = (isinstance(entity, TextObject)
                        and self._object_has_effect_modifiers(entity.object_id))
        if isinstance(entity, TextObject) and not live_effects and (behavior == "stretch" or mode != "handle"):
            # Existing cached text transforms are ideal for moving/rotating
            # and stretching. Only Bounds resizing needs live layout work.
            return False
        self._commit_text_edit()
        parent_id = entity.parent_layer_id if isinstance(entity, TextObject) else entity.parent_id
        from comic_editor.ui.attached_translation import moving_entities, ownership
        moving = moving_entities(self.chapter, [(self.selected_kind, self.selected_id)])
        modifiers, masks = ownership(self.chapter)
        before = RecordSnapshot.capture(self.chapter,
            layers=[identifier for kind, identifier in moving if kind == "layer"],
            objects=[identifier for kind, identifier in moving if kind == "object"],
            modifiers=[identifier for identifier, owners in modifiers.items() if owners and owners <= moving],
            masks=[identifier for identifier, owners in masks.items() if owners and owners <= moving])
        self._free_text_drag = {"id": self.selected_id, "kind": self.selected_kind,
            "before": before, "frame": QRectF(frame), "mapping": QTransform(mapping),
            "parent": parent_id, "mode": mode, "handle": handle, "behavior": behavior,
            "press": QPointF(world), "quad": quad,
            "pivot": QPointF(self._transform_pivot or mapping.map(frame.center()))}
        self._free_text_input.cancel()
        self._free_text_tool_session = ToolSession(lambda sample: None,
            self._free_text_input.queue, self._finish_free_text_drag_impl)
        self._free_text_tool_session.begin(QPointF(world))
        return True

    def _queue_free_text_drag(self, world):
        if self._free_text_drag is None:
            return False
        self._free_text_pending = QPointF(world)
        self._free_text_tool_session.update(self._free_text_pending)
        if not self._free_text_timer.isActive():
            self._free_text_timer.start(16)
        return True

    def _flush_free_text_drag(self):
        self._free_text_timer.stop()
        world, self._free_text_pending = self._free_text_pending, None
        if world is not None:
            self._free_text_input.finish(world)

    def _apply_free_text_drag(self, world):
        state = self._free_text_drag
        if world is None or state is None or self.chapter is None:
            return
        entity = self.chapter.objects[state["id"]] if state["kind"] == "object" else self.chapter.layers[state["id"]]
        inverse, valid = state["mapping"].inverted()
        parent_inverse, parent_valid = self.layer_world_transform(state["parent"]).inverted()
        if not valid or not parent_valid:
            return
        frame, mode = state["frame"], state["mode"]
        quad = list(state["quad"])
        if mode == "pivot":
            self._transform_pivot, self._transform_pivot_custom = QPointF(world), True
            self.update()
            return
        resized = None
        if mode == "handle" and state["behavior"] == "bounds":
            point = inverse.map(self._snap(world, state["parent"]))
            handle = state["handle"]
            resized = QRectF(frame)
            if handle in {0, 3, 7}: resized.setLeft(min(point.x(), frame.right()-1))
            if handle in {1, 2, 5}: resized.setRight(max(point.x(), frame.left()+1))
            if handle in {0, 1, 4}: resized.setTop(min(point.y(), frame.bottom()-1))
            if handle in {2, 3, 6}: resized.setBottom(max(point.y(), frame.top()+1))
            if self.settings.transform_mode == "uniform":
                anchors = self._quad_handles(self._rect_quad(frame))
                opposite = [2, 3, 0, 1, 6, 7, 4, 5][handle]
                origin = QPointF(*anchors[opposite])
                factor = max(.001, math.dist(point.toTuple(), origin.toTuple())/max(1e-9, math.dist(anchors[handle], anchors[opposite])))
                resized = QRectF(origin+(frame.topLeft()-origin)*factor, origin+(frame.bottomRight()-origin)*factor)
            quad = [state["mapping"].map(QPointF(*p)).toTuple() for p in self._rect_quad(resized)]
        elif mode == "translate":
            delta = self._snap(world, state["parent"])-self._snap(state["press"], state["parent"])
            quad = [(x+delta.x(), y+delta.y()) for x, y in quad]
        elif mode == "rotate":
            pivot = state["pivot"]
            theta = math.atan2(world.y()-pivot.y(), world.x()-pivot.x())-math.atan2(state["press"].y()-pivot.y(), state["press"].x()-pivot.x())
            rotation = QTransform().translate(pivot.x(), pivot.y()).rotate(math.degrees(theta)).translate(-pivot.x(), -pivot.y())
            quad = [rotation.map(QPointF(*p)).toTuple() for p in quad]
        else:
            handle = state["handle"]
            point = self._snap(world, state["parent"])
            if self.settings.transform_mode == "uniform":
                anchors = self._quad_handles(quad)
                origin = QPointF(*anchors[[2, 3, 0, 1, 6, 7, 4, 5][handle]])
                factor = math.dist(point.toTuple(), origin.toTuple())/max(1e-9, math.dist(anchors[handle], origin.toTuple()))
                quad = [(origin+(QPointF(*p)-origin)*factor).toTuple() for p in quad]
            elif handle < 4:
                quad[handle] = point.toTuple()
            else:
                edge = handle-4
                delta = point-QPointF(*self._edge_midpoints(quad)[edge])
                for index in (edge, (edge+1)%4): quad[index] = (QPointF(*quad[index])+delta).toTuple()
        if not self._quad_is_valid(quad):
            return
        dirty = self._text_frame_dirty(state["kind"], state["id"])
        if mode == "translate":
            previous = state.get("result_quad", state["quad"])
            change = self._quad_to_quad_transform(previous, quad)
            self._transform_single_target_focal_modifiers(state["kind"], state["id"], change)
        if isinstance(entity, TextObject):
            if resized is not None:
                entity.width, entity.height = resized.width(), resized.height()
            entity.transform_quad = [parent_inverse.map(QPointF(*p)).toTuple() for p in quad]
            entity.x, entity.y = entity.transform_quad[0]
        elif resized is not None:
            sx, sy = resized.width()/frame.width(), resized.height()/frame.height()
            originals = state["before"].records["objects"]
            for ref in entity.children:
                child = self.chapter.objects[ref.entity_id]
                original = object_from_dict(originals[ref.entity_id])
                source = QRectF(0, 0, original.width, original.height)
                placement = self._quad_transform(source, self._text_quad(original))
                origin = placement.map(QPointF())
                destination = QPointF(resized.left()+(origin.x()-frame.left())*sx,
                                      resized.top()+(origin.y()-frame.top())*sy)
                child.width, child.height = original.width*sx, original.height*sy
                child.transform_quad = [(placement.map(QPointF(*p))+destination-origin).toTuple()
                                        for p in self._rect_quad(QRectF(0, 0, child.width, child.height))]
                child.x, child.y = child.transform_quad[0]
        else:
            entity.transform_frame = (frame.x(), frame.y(), frame.width(), frame.height())
            entity.transform_quad = [parent_inverse.map(QPointF(*p)).toTuple() for p in quad]
            entity.translate_x = entity.translate_y = 0.
        state["result_quad"] = quad
        new_dirty = self._text_frame_dirty(state["kind"], state["id"])
        publish = getattr(self, "_publish_change_set", None)
        change_set = None
        if publish is not None:
            changes = tuple(EntityChange((GROUP_KINDS[group], identifier),
                frozenset({"transform_quad", "layout", "rig"}),
                dirty.getRect() if not dirty.isEmpty() else None,
                new_dirty.getRect() if not new_dirty.isEmpty() else None)
                for group, records in state["before"].records.items() for identifier in records)
            change_set = ChangeSet(changes, transient=True,
                conservative=dirty.isEmpty() or new_dirty.isEmpty() or bool(state["before"].records["masks"]),
                label="Text transform")
            publish(change_set, action="transient")
        if change_set is not None:
            self._emit_typed_document_changed(self._text_dirty_union(dirty, new_dirty), change_set)
        else:
            self.documentChanged.emit(self._text_dirty_union(dirty, new_dirty))
        self.update()

    def _finish_free_text_drag(self):
        session = self._free_text_tool_session
        try:
            return session.commit() if session is not None else self._finish_free_text_drag_impl()
        finally:
            self._free_text_tool_session = None

    def _finish_free_text_drag_impl(self):
        self._flush_free_text_drag()
        state, self._free_text_drag = self._free_text_drag, None
        if state is None:
            return False
        if state["mode"] != "pivot":
            final_dirty = None
            if state["mode"] != "translate" and "result_quad" in state and not (state["behavior"] == "bounds" and state["mode"] == "handle"):
                dirty = self._text_frame_dirty(state["kind"], state["id"])
                change = self._quad_to_quad_transform(state["quad"], state["result_quad"])
                self._transform_single_target_focal_modifiers(state["kind"], state["id"], change)
                final_dirty = self._text_dirty_union(dirty, self._text_frame_dirty(state["kind"], state["id"]))
            after = state["before"].after(self.chapter)
            _, new = DocumentPatch.pair(state["before"], after)
            if not new.empty:
                self.push_model_change(state["before"], after, "Transform text bounds" if state["behavior"] == "bounds" else "Stretch text")
                if final_dirty is not None:
                    self._emit_typed_document_changed(final_dirty, self._last_published_change)
        self.interactionFinished.emit()
        self.update()
        return True
