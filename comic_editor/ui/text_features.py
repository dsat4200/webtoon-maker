"""Free text placement and non-destructive layout/frame manipulation."""
import math
from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPen, QPolygonF, QTransform
from comic_editor.core.models import TextObject, object_from_dict


class TextFeatures:
    def _init_text_features(self):
        self._text_placement = None
        self._free_text_drag = None
        self._free_text_pending = None
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
            before = self.chapter.to_dict()
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
        before = self.chapter.to_dict()
        entity = target[0]
        setattr(entity, "transform_behavior" if isinstance(entity, TextObject) else "text_transform_behavior", behavior)
        after = self.chapter.to_dict()
        if before != after:
            self.push_model_change(before, after, "Change text transform behavior")
            self.documentChanged.emit(None)
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
        before = self.chapter.to_dict()
        if state["new"]:
            parent_id = self.chapter.add_layer(parent_id, "Free Text", layer_kind="text_container").layer_id
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
        self.chapter.add_object(parent_id, obj)
        self._text_placement = None
        self.push_model_change(before, self.chapter.to_dict(), "Add free text container" if state["new"] else "Add text box")
        self.hierarchyChanged.emit()
        self.set_selection("object", obj.object_id)
        self.start_text_edit(select_all=True)
        self.documentChanged.emit(None)
        self.update()
        return True

    def _cancel_text_features(self, *, restore=False):
        state = self._text_placement
        self._text_placement = None
        self._free_text_timer.stop()
        self._free_text_pending = None
        drag, self._free_text_drag = self._free_text_drag, None
        if restore and drag:
            self.replace_chapter(drag["before"])
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
        self._free_text_drag = {"id": self.selected_id, "kind": self.selected_kind,
            "before": self.chapter.to_dict(), "frame": QRectF(frame), "mapping": QTransform(mapping),
            "parent": parent_id, "mode": mode, "handle": handle, "behavior": behavior,
            "press": QPointF(world), "quad": quad,
            "pivot": QPointF(self._transform_pivot or mapping.map(frame.center()))}
        return True

    def _queue_free_text_drag(self, world):
        if self._free_text_drag is None:
            return False
        self._free_text_pending = QPointF(world)
        if not self._free_text_timer.isActive():
            self._free_text_timer.start(16)
        return True

    def _flush_free_text_drag(self):
        self._free_text_timer.stop()
        world, self._free_text_pending = self._free_text_pending, None
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
        if isinstance(entity, TextObject):
            if resized is not None:
                entity.width, entity.height = resized.width(), resized.height()
            entity.transform_quad = [parent_inverse.map(QPointF(*p)).toTuple() for p in quad]
            entity.x, entity.y = entity.transform_quad[0]
        elif resized is not None:
            sx, sy = resized.width()/frame.width(), resized.height()/frame.height()
            originals = {o["id"]: o for o in state["before"]["objects"]}
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
        self.documentChanged.emit(None)
        self.update()

    def _finish_free_text_drag(self):
        self._flush_free_text_drag()
        state, self._free_text_drag = self._free_text_drag, None
        if state is None:
            return False
        if state["mode"] != "pivot":
            if state["kind"] == "layer" and "result_quad" in state and not (state["behavior"] == "bounds" and state["mode"] == "handle"):
                change = self._quad_to_quad_transform(state["quad"], state["result_quad"])
                self._transform_single_target_focal_modifiers("layer", state["id"], change)
            after = self.chapter.to_dict()
            if state["before"] != after:
                self.push_model_change(state["before"], after, "Transform text bounds" if state["behavior"] == "bounds" else "Stretch text")
        self.interactionFinished.emit()
        self.update()
        return True
