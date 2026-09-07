"""Cage tool transactions, selection, and on-canvas mesh/transform controls."""
import copy
import math
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from comic_editor.core.cage import CageGrid, map_points, tessellate, homography, project
from comic_editor.core.models import CageTransformModifier, RasterObject, VectorDrawingObject
from comic_editor.ui.cage_rendering import transform_points, warp_image
from comic_editor.ui.effect_pipeline import aligned, empty_image
from comic_editor.ui.icons import iconoir


class CageFeatures:
    def _init_cage_features(self):
        self.modifier_mode = False
        self._modifier_selection = {}
        self._cage_session = None
        self._cage_edit_before = None
        self._cage_drag = None
        self._cage_selected_points = set()
        self._cage_selection_owner = None
        self._cage_pending = None
        self._render_cage_source = False
        self._cage_timer = QTimer(self)
        self._cage_timer.setSingleShot(True)
        self._cage_timer.setInterval(16)
        self._cage_timer.timeout.connect(self._flush_cage_move)

    def _remember_modifier(self, identifier):
        for ref in self.selected_entities:
            self._modifier_selection[tuple(ref)] = identifier
        self.active_modifier_id = identifier
        self.modifierSelectionChanged.emit(identifier)

    def _restore_modifier_selection(self):
        candidates = [self._modifier_selection.get(tuple(ref), "") for ref in self.selected_entities]
        self.active_modifier_id = candidates[-1] if candidates and len(set(candidates)) == 1 else ""
        if self.chapter is not None and self.active_modifier_id not in self.chapter.modifiers:
            self.active_modifier_id = ""
        self.modifierSelectionChanged.emit(self.active_modifier_id)

    def _active_cage(self):
        if self.chapter is None:
            return None
        if self._cage_session is not None:
            return self._cage_session["grid"]
        if not self.modifier_mode:
            return None
        modifier = self.chapter.modifiers.get(self.active_modifier_id)
        if isinstance(modifier, CageTransformModifier) and not modifier.muted and any(
            ref in self.chapter.modifier_target_ids(modifier.modifier_id) for ref in self.selected_entities
        ):
            return modifier
        return None

    def report_incompatible(self, title, message, targets):
        self.incompatibleSelection.emit(list(targets))
        self.operationError.emit(title, message)

    def begin_cage_tool(self):
        if not self.chapter or not self.selected_entities:
            self.report_incompatible("Cage Transform", "Select raster/vector drawings, or images and shapes, to transform.", [])
            return False
        if self._cage_session is not None:
            return True
        targets = list(self.selected_entities)
        drawings = [ref for ref in targets if ref[0] == "object" and isinstance(
            self.chapter.objects.get(ref[1]), (RasterObject, VectorDrawingObject))]
        if drawings and len(drawings) != len(targets):
            bad = [ref for ref in targets if ref not in drawings]
            message = self.chapter.modifier_compatibility_message(CageTransformModifier(), bad)
            names = [self.chapter.layers[r[1]].name if r[0] == "layer" else self.chapter.objects[r[1]].name for r in bad]
            self.report_incompatible("Cage Transform", "Drawings cannot share a cage operation with other object types: " + ", ".join(names) + ". Select only raster/vector drawings, or only images and shapes.", bad)
            return False
        if not drawings:
            self.cageModifierRequested.emit()
            return False  # modifier workflow keeps the existing canvas tool
        from comic_editor.ui.baking import snapshot, visual_bounds
        bounds = None
        for kind, identifier in targets:
            candidate = visual_bounds(self, kind, identifier)
            bounds = candidate if bounds is None else bounds.united(candidate)
        if bounds is None or bounds.isEmpty():
            self.report_incompatible("Cage Transform", "The selected drawings contain no artwork to transform.", targets)
            return False
        self._commit_text_edit()
        grid = CageGrid(frame=self._rect_signature(bounds))
        grid.validate_grid()
        self._cage_session = {"grid": grid, "targets": targets,
            "before": snapshot(self, {ref[1] for ref in targets}), "sources": {}}
        self._cage_selected_points.clear()
        self.incompatibleSelection.emit([])
        self.cageChanged.emit()
        self._invalidate_scene_cache()
        self.update()
        return True

    def _start_cage_edit(self):
        cage = self._active_cage()
        if cage is not None and self._cage_session is None and self._cage_edit_before is None:
            self._cage_edit_before = (cage.modifier_id, copy.deepcopy(cage.to_dict()), self.chapter.to_dict())

    def set_cage_parameter(self, name, value):
        cage = self._active_cage()
        if cage is None:
            return
        self._start_cage_edit()
        if name in {"columns", "rows"}:
            cage.resample(value if name == "columns" else cage.columns,
                          value if name == "rows" else cage.rows)
            self._cage_selected_points.clear()
        else:
            setattr(cage, name, value)
        cage.validate_grid()
        self._cage_changed()

    def _cage_changed(self):
        self._invalidate_scene_cache()
        self._compound_path_cache.clear()
        self.update()
        self.visualChanged.emit(None)
        self.cageChanged.emit()

    def flip_cage(self, vertical=False):
        cage = self._active_cage()
        if cage is None:
            return
        self._start_cage_edit()
        axis = 1 if vertical else 0
        points = np.asarray(cage.points).copy()
        indices = sorted(self._cage_selected_points) or list(range(len(points)))
        points[indices, axis] = 2*cage.pivot[axis]-points[indices, axis]
        cage.points = [tuple(p) for p in points]
        self._cage_changed()

    def commit_active_cage(self):
        """Explicit save/export accepts the current preview before persistence."""
        if self._cage_session is None and self._cage_edit_before is None:
            return True
        return self.finish_cage(True)

    def finish_cage(self, commit=True):
        if self._cage_session is not None or self._cage_edit_before is not None:
            self._effect_jobs.cancel()
        self._cage_timer.stop()
        self._flush_cage_move()
        self._cage_drag = None
        session = self._cage_session
        if session is not None:
            if commit:
                try:
                    self._commit_cage_drawings(session)
                except (ValueError, MemoryError, OSError) as error:
                    self.operationError.emit("Cage Transform", str(error))
                    return False
            self._cage_session = None
            from comic_editor.ui.canvas import ToolKind
            self.tool = ToolKind.TRANSFORM
            self.toolChanged.emit(self.tool)
        if self._cage_edit_before is not None:
            identifier, original, before = self._cage_edit_before
            self._cage_edit_before = None
            if commit:
                after = self.chapter.to_dict()
                if before != after:
                    self.push_model_change(before, after, "Cage Transform")
                    self.documentChanged.emit(None)
            else:
                from comic_editor.core.models import modifier_from_dict
                if identifier in self.chapter.modifiers:
                    self.chapter.modifiers[identifier] = modifier_from_dict(original)
        self._cage_changed()
        self.interactionFinished.emit()
        return True

    def _cage_object_preview(self, painter, obj, parent_opacity, visible):
        session = self._cage_session
        if not session or ("object", obj.object_id) not in session["targets"]:
            return False
        key = obj.object_id
        mapping = self.layer_world_transform(obj.parent_layer_id)
        source = session["sources"].get(key)
        if source is None:
            bounds = aligned(mapping.inverted()[0].mapRect(self.object_world_rect(obj.object_id)))
            image = empty_image(bounds)
            p = QPainter(image)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.translate(-bounds.left(), -bounds.top())
            self._render_cage_source = True
            try:
                self._render_object_content(p, obj, bounds)
            finally:
                self._render_cage_source = False
                p.end()
            source = image, bounds
            session["sources"][key] = source
        image, bounds = source
        grid = session["grid"]
        cache_key = ("cage-tool", key, int(image.cacheKey()), repr(grid.grid_dict()))
        warped = self._modifier_cache_get(cache_key)
        from comic_editor.ui.cage_rendering import mesh_for_image
        _, destination, _ = mesh_for_image(grid, bounds, mapping)
        low, high = destination.min(axis=0), destination.max(axis=0)
        output_bounds = aligned(QRectF(*low, *(high-low)))
        if warped is None:
            incoming, cage = QImage(image), copy.deepcopy(grid)
            placement, source_bounds, destination_bounds = QTransform(mapping), QRectF(bounds), QRectF(output_bounds)
            def compute(cancelled=None, pixel_scale=1.):
                result = warp_image(incoming, source_bounds, cage, placement, destination_bounds, cancelled, pixel_scale=pixel_scale)
                if result is None:
                    return None
                return result[0]
            asynchronous = self._interactive_render and not self._render_modifier_sources and output_bounds.width()*output_bounds.height() > 128*128
            from comic_editor.ui.gpu_textures import renderer_for
            gpu = renderer_for(self)
            warped = gpu.cage(incoming, source_bounds, cage, placement, destination_bounds) if gpu is not None else None
            if warped is not None:
                self._modifier_cache_put(cache_key, warped)
            elif asynchronous and self._effect_jobs.request(("cage-tool", key), cache_key, compute,
                    10*int(image.sizeInBytes())+16*math.ceil(output_bounds.width()*output_bounds.height())):
                draft_key = ("cage-draft", cache_key)
                warped = self._modifier_cache_get(draft_key)
                if warped is None:
                    warped = compute(pixel_scale=min(1., 192/max(output_bounds.width(), output_bounds.height())))
                    self._modifier_cache_put(draft_key, warped)
            else:
                warped = compute()
                self._modifier_cache_put(cache_key, warped)
        painter.save()
        painter.drawImage(output_bounds, warped)
        painter.restore()
        return True

    def _commit_cage_drawings(self, session):
        from comic_editor.ui.baking import snapshot, commit
        grid = session["grid"]
        if np.allclose(grid.points, grid.rest_points(), atol=1e-9, rtol=0):
            return
        prepared = []
        # Prepare every target before touching any document or resource.
        for _, identifier in session["targets"]:
            obj = self.chapter.objects[identifier]
            mapping = self._drawing_local_to_world_transform(obj)
            inverse, valid = mapping.inverted()
            if not valid:
                raise ValueError(f"{obj.name} has a singular placement")
            if isinstance(obj, RasterObject):
                bounds = aligned(self.tiles.content_bounds(identifier) or QRectF(*obj.interaction_rect))
                image = empty_image(bounds)
                painter = QPainter(image)
                for (x, y), tile in self.tiles.iter_tiles(identifier):
                    painter.drawImage(QPointF(x*obj.tile_size, y*obj.tile_size)-bounds.topLeft(), tile)
                painter.end()
                image, bounds = warp_image(image, bounds, grid, mapping)
                tiles = {}
                size = obj.tile_size
                for y in range(math.floor(bounds.top()/size), math.ceil(bounds.bottom()/size)):
                    for x in range(math.floor(bounds.left()/size), math.ceil(bounds.right()/size)):
                        tile = empty_image(QRectF(0, 0, size, size))
                        painter = QPainter(tile)
                        painter.drawImage(bounds.topLeft()-QPointF(x*size, y*size), image)
                        painter.end()
                        if self.tiles._alpha_bbox(tile) is not None:
                            tiles[x, y] = tile
                prepared.append((obj, tiles, bounds))
            else:
                from comic_editor.ui.cage_vectors import warp_vector
                replacement = warp_vector(obj, grid, mapping)
                prepared.append((obj, replacement, None))
        self._cage_session = None
        for obj, value, bounds in prepared:
            if isinstance(obj, RasterObject):
                self.tiles.replace_object_tiles(obj.object_id, value)
                obj.interaction_rect = self._rect_signature(QRectF(*obj.interaction_rect).united(bounds))
                if obj.modifier_source_frame is not None:
                    obj.modifier_source_frame = self._rect_signature(bounds)
            else:
                obj.__dict__.update(value.__dict__)
        selection = session["targets"]
        after = snapshot(self, {ref[1] for ref in selection})
        commit(self, session["before"], after, "Cage Transform drawings", selection, selection)

    def _cage_controls(self, cage):
        points = [self.document_to_widget(QPointF(*p)) for p in cage.points]
        indices = sorted(self._cage_selected_points) or list(range(len(points)))
        chosen = [points[i] for i in indices]
        rect = QRectF(QPointF(min(p.x() for p in chosen), min(p.y() for p in chosen)),
                      QPointF(max(p.x() for p in chosen), max(p.y() for p in chosen))).adjusted(-24, -24, 24, 24)
        handles = [rect.topLeft(), QPointF(rect.center().x(), rect.top()), rect.topRight(),
                   QPointF(rect.right(), rect.center().y()), rect.bottomRight(),
                   QPointF(rect.center().x(), rect.bottom()), rect.bottomLeft(), QPointF(rect.left(), rect.center().y())]
        rotate = QPointF(rect.center().x(), rect.top()-26)
        actions = {name: QRectF(rect.center().x()-100+i*40, rect.bottom()+12, 36, 28)
                   for i, name in enumerate(("ok", "cancel", "flip_x", "flip_y", "uniform"))}
        return points, rect, handles, rotate, actions

    def _draw_cage_handles(self, painter):
        cage = self._active_cage()
        if cage is None:
            return False
        owner = getattr(cage, "modifier_id", id(cage))
        if owner != self._cage_selection_owner:
            self._cage_selected_points.clear()
            self._cage_selection_owner = owner
        points, rect, handles, rotate, actions = self._cage_controls(cage)
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor("#63bbff"), 1.25))
        # Curved guide lines follow the exact evaluator, including smoothness.
        for axis, count in ((0, cage.columns), (1, cage.rows)):
            for k in range(count):
                uv = np.empty((49, 2))
                uv[:, axis] = k/(count-1)
                uv[:, 1-axis] = np.linspace(0, 1, len(uv))
                source = uv*np.asarray(cage.frame[2:])+np.asarray(cage.frame[:2])
                if cage.source_quad is not None:
                    source = project(homography(cage.source_quad), uv)
                path = QPainterPath()
                curve = [self.document_to_widget(QPointF(*p)) for p in map_points(cage, source)]
                path.moveTo(curve[0])
                for p in curve[1:]:
                    path.lineTo(p)
                painter.drawPath(path)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#8accff"), 1., Qt.DashLine))
        painter.drawRect(rect)
        painter.drawLine(handles[1], rotate)
        painter.setPen(QPen(QColor("#8accff"), 1.5))
        painter.setBrush(QColor("#17344d"))
        for p in handles:
            painter.drawRect(QRectF(p.x()-4, p.y()-4, 8, 8))
        painter.drawEllipse(rotate, 6, 6)
        for i, p in enumerate(points):
            painter.setBrush(QColor("#65bcff" if i in self._cage_selected_points else "#162d40"))
            painter.drawEllipse(p, 4.5, 4.5)
        pivot = self.document_to_widget(QPointF(*cage.pivot))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#ffcb70"), 1.5))
        painter.drawEllipse(pivot, 6, 6)
        painter.drawLine(pivot-QPointF(10, 0), pivot+QPointF(10, 0))
        painter.drawLine(pivot-QPointF(0, 10), pivot+QPointF(0, 10))
        for name, box in actions.items():
            painter.setPen(QPen(QColor("#65bcff"), 1))
            painter.setBrush(QColor("#245780" if name == "uniform" and cage.uniform else "#1d2e3e"))
            painter.drawRoundedRect(box, 4, 4)
            if name in {"flip_x", "flip_y"}:
                iconoir("align-horizontal-centers" if name == "flip_x" else "align-vertical-centers").paint(painter, box.adjusted(7, 3, -7, -3).toRect())
            else:
                painter.setPen(Qt.white)
                painter.drawText(box, Qt.AlignCenter, {"ok": "OK", "cancel": "×", "uniform": "1:1" if cage.uniform else "Free"}[name])
        if self._cage_drag and self._cage_drag["mode"] == "marquee":
            painter.setBrush(QColor(70, 150, 230, 35))
            painter.drawRect(QRectF(self._cage_drag["widget"], self._cage_drag.get("current", self._cage_drag["widget"])).normalized())
        painter.restore()
        return True

    def _begin_cage_handle(self, widget, modifiers):
        cage = self._active_cage()
        if cage is None:
            return False
        points, rect, handles, rotate, actions = self._cage_controls(cage)
        for name, box in actions.items():
            if box.contains(widget):
                if name in {"ok", "cancel"}:
                    self.finish_cage(name == "ok")
                elif name == "uniform":
                    self.set_cage_parameter("uniform", not cage.uniform)
                else:
                    self.flip_cage(name == "flip_y")
                return True
        world = self.widget_to_document(widget)
        distance = lambda p: math.hypot(p.x()-widget.x(), p.y()-widget.y())
        pivot = self.document_to_widget(QPointF(*cage.pivot))
        hit = min(range(len(points)), key=lambda i: distance(points[i]))
        additive = bool(modifiers & (Qt.ShiftModifier | Qt.ControlModifier))
        handle = next((i for i, p in enumerate(handles) if distance(p) < 9), None)
        if distance(pivot) < 9:
            mode = "pivot"
        elif distance(points[hit]) < 9:
            if additive:
                if hit in self._cage_selected_points:
                    self._cage_selected_points.remove(hit)
                else:
                    self._cage_selected_points.add(hit)
            elif hit not in self._cage_selected_points:
                self._cage_selected_points = {hit}
            mode = "points"
        elif distance(rotate) < 10:
            mode = "rotate"
        elif handle is not None:
            mode = "scale"
        elif additive or not rect.contains(widget):
            mode = "marquee"
            if not additive:
                self._cage_selected_points.clear()
        else:
            mode = "translate"
        self._start_cage_edit()
        self._cage_drag = {"mode": mode, "widget": QPointF(widget), "world": world,
                           "points": np.asarray(cage.points).copy(), "pivot": tuple(cage.pivot),
                           "selected": set(self._cage_selected_points), "handle": handle,
                           "rect": rect, "uniform": cage.uniform or bool(modifiers & Qt.ShiftModifier)}
        self.update()
        return True

    def _move_cage_handle(self, widget):
        if self._cage_drag is None:
            return False
        self._cage_pending = QPointF(widget)
        if not self._cage_timer.isActive():
            self._cage_timer.start()
        return True

    def _flush_cage_move(self):
        widget, self._cage_pending = self._cage_pending, None
        drag, cage = self._cage_drag, self._active_cage()
        if widget is None or drag is None or cage is None:
            return
        world = self.widget_to_document(widget)
        delta = np.asarray((world.x()-drag["world"].x(), world.y()-drag["world"].y()))
        if drag["mode"] == "marquee":
            drag["current"] = widget
            rect = QRectF(drag["widget"], widget).normalized()
            self._cage_selected_points = drag["selected"] | {i for i, p in enumerate(cage.points) if rect.contains(self.document_to_widget(QPointF(*p)))}
            self.update()
            return
        if drag["mode"] == "pivot":
            cage.pivot = tuple(np.asarray(drag["pivot"])+delta)
        else:
            points = drag["points"].copy()
            indices = sorted(drag["selected"]) or list(range(len(points)))
            pivot = np.asarray(drag["pivot"])
            if drag["mode"] in {"points", "translate"}:
                points[indices] += delta
                if drag["mode"] == "translate" and not drag["selected"]:
                    cage.pivot = tuple(pivot+delta)
            elif drag["mode"] == "rotate":
                start = np.asarray(drag["world"].toTuple())-pivot
                end = np.asarray(world.toTuple())-pivot
                angle = math.atan2(end[1], end[0])-math.atan2(start[1], start[0])
                c, s = math.cos(angle), math.sin(angle)
                points[indices] = (points[indices]-pivot) @ np.asarray(((c, s), (-s, c)))+pivot
            elif drag["mode"] == "scale":
                if not drag["uniform"]:
                    rect = drag["rect"]
                    source = QPolygonF([rect.topLeft(), rect.topRight(), rect.bottomRight(), rect.bottomLeft()])
                    destination = QPolygonF(source)
                    handle = drag["handle"]
                    corners = [handle//2] if handle % 2 == 0 else [handle//2, (handle//2+1) % 4]
                    offset = widget-drag["widget"]
                    for corner in corners:
                        destination[corner] += offset
                    transform = QTransform()
                    if QTransform.quadToQuad(source, destination, transform):
                        camera = self.camera_transform()
                        inverse, valid = camera.inverted()
                        if valid:
                            points[indices] = transform_points(camera*transform*inverse, points[indices])
                    cage.points = [tuple(p) for p in points]
                    self._cage_changed()
                    return
                start = np.asarray(drag["world"].toTuple())-pivot
                end = np.asarray(world.toTuple())-pivot
                factors = np.divide(end, start, out=np.ones(2), where=np.abs(start) > 1e-6)
                handle = drag["handle"]
                if handle in {1, 5}:
                    factors[0] = 1
                if handle in {3, 7}:
                    factors[1] = 1
                if drag["uniform"]:
                    factors[:] = factors[np.argmax(np.abs(factors-1))]
                points[indices] = (points[indices]-pivot)*factors+pivot
            cage.points = [tuple(p) for p in points]
        self._cage_changed()

    def _finish_cage_handle(self):
        if self._cage_drag is None:
            return False
        self._cage_timer.stop()
        self._flush_cage_move()
        self._cage_drag = None
        self.update()
        return True
