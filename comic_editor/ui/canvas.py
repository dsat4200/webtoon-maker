"""Tiled vertical document viewport, renderer, and drawing tools."""
from __future__ import annotations

import math
from enum import Enum

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QColor, QFont, QFontMetricsF, QGuiApplication,
    QImage, QInputDevice, QInputMethodEvent,
    QMouseEvent, QOffscreenSurface, QOpenGLContext, QPainter, QPainterPath,
    QPainterPathStroker, QPalette,
    QPen, QPolygonF, QSurfaceFormat, QTextBlockFormat, QTextCursor, QTextDocument,
    QTransform,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QWidget

from comic_editor.core.commands import CallbackCommand, CommandStack, TilePatchCommand
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, DocumentObject, LayerNode, PathNode,
    RasterObject, ShapeStyle, TextObject,
)
from comic_editor.core.pressure import BrushPreset
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore


class ToolKind(Enum):
    OBJECT_SELECT = "object_select"
    RASTER_PENCIL = "raster_pencil"
    RASTER_ERASER = "raster_eraser"
    TEXT_EDIT = "text_edit"
    TRANSFORM = "transform"
    SHAPE_EDIT = "shape_edit"
    BOUND_EDIT = "shape_edit"
    BOX_BOUND = "box_bound"
    CIRCLE_BOUND = "circle_bound"
    SHAPE_CREATE = "shape_create"
    POLYGON_BOUND = "shape_create"
    RASTER_CREATE = "raster_create"


class _CanvasLogic:
    documentChanged = Signal(object)
    selectionChanged = Signal(str, str)
    hierarchyChanged = Signal()
    chapterReplaced = Signal(object)
    cameraChanged = Signal()
    interactionFinished = Signal()
    toolChanged = Signal(object)
    textEditingChanged = Signal(bool)
    selectionCandidatesRequested = Signal(object, object)
    primitiveConversionRequested = Signal(str)

    def __init__(self, settings: EditorSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.chapter: ChapterDocument | None = None
        self.tiles = TileStore()
        self.command_stack = CommandStack()
        self.tool = ToolKind.OBJECT_SELECT
        self.selected_kind = ""
        self.selected_id = ""
        self.active_page_id = ""
        self.active_layer_id = ""
        self.selected_object_id = ""
        self.center_x = 540.0
        self.center_y = 540.0
        self.scale = 0.6
        self.rotation = 0.0
        self._nav_mode: str | None = None
        self._nav_anchor = QPointF()
        self._nav_anchor_center = QPointF()
        self._nav_anchor_scale = 1.0
        self._nav_anchor_rotation = 0.0
        self._drawing = False
        self._last_draw_point = QPointF()
        self._last_pressure = 1.0
        self._stroke_before: dict[tuple[int, int], QImage | None] = {}
        self._model_before: dict | None = None
        self._drag_start_doc = QPointF()
        self._drag_start_value: object = None
        self._active_handle: int | None = None
        self._selected_bound_vertex: int | None = None
        self._bound_drag_mode: str | None = None
        self._bound_start_points: list[tuple[float, float]] = []
        self._outside_click_candidate = False
        self._press_widget_point = QPointF()
        self._press_document_point = QPointF()
        self._transform_start_quad: list[tuple[float, float]] | None = None
        self._transform_preview_quad: list[tuple[float, float]] | None = None
        self._transform_handle_index: int | None = None
        self._transform_drag_mode: str | None = None
        self._transform_static_cache = QImage()
        self._render_excluded_object_id = ""
        self._raster_transform_snapshots: dict[str, tuple[dict, dict]] = {}
        self._text_editing = False
        self._text_cursor_position = 0
        self._text_selection_anchor = 0
        self._text_dragging = False
        self._text_before_state: dict | None = None
        self._text_local_history: list[tuple[str, int, int]] = []
        self._strict_margin_start: float | None = None
        self._strict_margin_edge: int | None = None
        self._strict_margin_press = QPointF()
        self._creation_points: list[tuple[float, float]] = []
        self._creation_nodes: list[PathNode] = []
        self._creation_node_dragged = False
        self._creation_press_widget = QPointF()
        self._creation_selected_node_id = ""
        self._creation_active_control: str | None = None
        self._creation_close_candidate = False
        self._creation_style: ShapeStyle | None = None
        self._raster_creation_parent_id = ""
        self._selected_shape_node_id = ""
        self._active_shape_control: str | None = None
        self._shape_hover_insert: tuple[int, float, QPointF] | None = None
        self._shape_hover_target: dict | None = None
        self._pending_primitive_insert: (
            tuple[str, int, float, QPointF, QPointF] | None
        ) = None
        self._tablet_tool_active = False
        self._touch_points: list[QPointF] = []
        self._touch_anchor_center = QPointF()
        self._touch_anchor_distance = 1.0
        self._touch_anchor_angle = 0.0
        self._touch_anchor_scale = 1.0
        self._touch_anchor_rotation = 0.0
        self._preset = settings.active_brush_preset()
        self._predictive: tuple[QPointF, QPointF, float, QColor] | None = None
        self.setMinimumSize(480, 480)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.setAttribute(Qt.WA_TabletTracking, True)
        self.setMouseTracking(True)

    # ---- document and commands -----------------------------------------
    def set_document(
        self, chapter: ChapterDocument, tiles: TileStore, reset_view: bool = True,
    ) -> None:
        self.chapter = chapter
        self.tiles = tiles
        self._ensure_raster_frames()
        self.command_stack.clear()
        self.selected_kind = ""
        self.selected_id = ""
        self.active_page_id = ""
        self.active_layer_id = ""
        self.selected_object_id = ""
        self._clear_transform_preview()
        if reset_view:
            self.reset_view()
        self.update()
        self.hierarchyChanged.emit()

    def replace_chapter(self, state: dict) -> None:
        self._commit_text_edit()
        self._clear_transform_preview()
        self.chapter = ChapterDocument.from_dict(state)
        valid = (
            self.selected_id in self.chapter.layers
            if self.selected_kind == "layer"
            else self.selected_id in self.chapter.objects
        )
        if not valid:
            self.selected_kind = ""
            self.selected_id = ""
            self.selected_object_id = ""
        self._sync_selection_levels()
        self.chapterReplaced.emit(self.chapter)
        self.hierarchyChanged.emit()
        self.selectionChanged.emit(self.selected_kind, self.selected_id)
        self.update()

    def push_model_change(self, before: dict, after: dict, label: str) -> None:
        self.command_stack.push(
            CallbackCommand(
                label,
                lambda: self.replace_chapter(after),
                lambda: self.replace_chapter(before),
            ),
            already_done=True,
        )

    def set_selection(
        self, kind: str, entity_id: str, activate_default_tool: bool = True,
    ) -> None:
        if self.chapter is None:
            return
        if entity_id != self.selected_object_id:
            self._clear_transform_preview()
        if entity_id != self.selected_id:
            self._selected_shape_node_id = ""
            self._shape_hover_insert = None
        if kind == "layer" and entity_id not in self.chapter.layers:
            return
        if kind == "object" and entity_id not in self.chapter.objects:
            return
        if self._text_editing and not (
            kind == "object" and entity_id == self.selected_object_id
        ):
            self._commit_text_edit()
        self.selected_kind, self.selected_id = kind, entity_id
        if kind == "object":
            obj = self.chapter.objects[entity_id]
            self.selected_object_id = entity_id
            self.active_layer_id = obj.parent_layer_id
            self.active_page_id = self.chapter.page_for_layer(obj.parent_layer_id).layer_id
            if activate_default_tool and isinstance(obj, RasterObject):
                self.tool = ToolKind.RASTER_PENCIL
                self.chapter.layers[obj.parent_layer_id].last_raster_id = obj.object_id
            elif activate_default_tool and isinstance(obj, TextObject):
                self.tool = ToolKind.TEXT_EDIT
        else:
            self.selected_object_id = ""
            self.active_layer_id = entity_id
            self.active_page_id = self.chapter.page_for_layer(entity_id).layer_id
        self.toolChanged.emit(self.tool)
        self.selectionChanged.emit(kind, entity_id)
        self.update()

    def set_tool(self, tool: ToolKind) -> bool:
        if self.tool == ToolKind.TRANSFORM and tool != ToolKind.TRANSFORM:
            self._clear_transform_preview()
        if self.tool == ToolKind.SHAPE_CREATE and tool != ToolKind.SHAPE_CREATE:
            self._creation_nodes.clear()
            self._creation_selected_node_id = ""
            self._creation_active_control = None
            self._creation_style = None
            self._shape_hover_target = None
            self._shape_hover_insert = None
        if tool != ToolKind.TEXT_EDIT:
            self._commit_text_edit()
        if tool == ToolKind.BOUND_EDIT and self.selected_object_id:
            selected = self.chapter.objects[self.selected_object_id]
            if not isinstance(selected, RasterObject):
                self.set_selection(
                    "layer", selected.parent_layer_id,
                    activate_default_tool=False,
                )
        if (
            tool == ToolKind.BOUND_EDIT
            and self.selected_kind == "layer"
            and self.chapter.layers[self.selected_id].layer_kind == "fill"
        ):
            parent_id = self.chapter.layers[self.selected_id].parent_id
            if parent_id:
                self.set_selection(
                    "layer", parent_id, activate_default_tool=False
                )
        if tool in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}:
            if self.selected_kind != "object" or self.chapter is None:
                return False
            if not isinstance(self.chapter.objects.get(self.selected_id), RasterObject):
                return False
        if tool == ToolKind.TEXT_EDIT:
            if self.chapter is None or not self.active_page_id:
                return False
            if self.selected_object_id and not isinstance(
                self.chapter.objects.get(self.selected_object_id), TextObject
            ):
                self.set_selection(
                    "layer", self.active_page_id, activate_default_tool=False
                )
        self.tool = tool
        self.toolChanged.emit(tool)
        self.update()
        return True

    def _sync_selection_levels(self) -> None:
        if self.chapter is None:
            return
        if self.selected_kind == "object" and self.selected_id in self.chapter.objects:
            obj = self.chapter.objects[self.selected_id]
            self.selected_object_id = obj.object_id
            self.active_layer_id = obj.parent_layer_id
            self.active_page_id = self.chapter.page_for_layer(obj.parent_layer_id).layer_id
        elif self.selected_kind == "layer" and self.selected_id in self.chapter.layers:
            self.selected_object_id = ""
            self.active_layer_id = self.selected_id
            self.active_page_id = self.chapter.page_for_layer(self.selected_id).layer_id

    def _ensure_raster_frames(self) -> None:
        if self.chapter is None:
            return
        for obj in self.chapter.objects.values():
            if not isinstance(obj, RasterObject):
                continue
            left, top, width, height = obj.interaction_rect
            frame = QRectF(left, top, width, height)
            content = self.tiles.content_bounds(obj.object_id)
            if content is not None:
                frame = frame.united(content)
            obj.interaction_rect = (
                frame.left(), frame.top(), max(1.0, frame.width()),
                max(1.0, frame.height()),
            )

    # ---- transforms ----------------------------------------------------
    def camera_transform(self) -> QTransform:
        transform = QTransform()
        transform.translate(self.width() / 2, self.height() / 2)
        transform.rotate(self.rotation)
        transform.scale(self.scale, self.scale)
        transform.translate(-self.center_x, -self.center_y)
        return transform

    def widget_to_document(self, point: QPointF) -> QPointF:
        inverse, valid = self.camera_transform().inverted()
        return inverse.map(point) if valid else QPointF()

    def document_to_widget(self, point: QPointF) -> QPointF:
        return self.camera_transform().map(point)

    def visible_document_rect(self) -> QRectF:
        inverse, valid = self.camera_transform().inverted()
        if not valid:
            return QRectF()
        polygon = inverse.map(QPolygonF(QRectF(self.rect())))
        return polygon.boundingRect()

    def reset_view(self) -> None:
        self.rotation = 0.0
        self.center_x = 540.0
        available = max(200, self.width() - 40)
        self.scale = max(0.05, min(1.0, available / 1080))
        self.center_y = max(1.0, self.height() / (2 * self.scale))
        self._snap_camera()
        self.update()
        self.cameraChanged.emit()

    def scroll_to_fraction(self, fraction: float) -> None:
        if not self.chapter:
            return
        visible_height = self.height() / max(0.05, self.scale)
        half = visible_height / 2
        self.center_y = half + max(0.0, min(1.0, fraction)) * max(
            0.0, self.chapter.height - visible_height
        )
        self._snap_camera()
        self.update()
        self.cameraChanged.emit()

    def viewport_fraction(self) -> tuple[float, float]:
        if not self.chapter:
            return 0.0, 1.0
        visible = min(self.chapter.height, self.height() / max(0.05, self.scale))
        top = max(0.0, self.center_y - visible / 2)
        return top / self.chapter.height, visible / self.chapter.height

    def _snap_camera(self) -> None:
        # Snapping in device space avoids blurred translations at any zoom.
        self.center_x = round(self.center_x * self.scale) / max(self.scale, 0.05)
        self.center_y = round(self.center_y * self.scale) / max(self.scale, 0.05)

    # ---- rendering -----------------------------------------------------
    @staticmethod
    def bound_path(bound: BoundGeometry, vertex_radius: float = 0.0) -> QPainterPath:
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)
        if bound.primitive == "ellipse":
            x, y, width, height = bound.bbox()
            path.addEllipse(QRectF(x, y, width, height))
            return path
        nodes = bound.nodes
        if not nodes:
            return path

        def rounding(index: int) -> tuple[QPointF, QPointF]:
            node = nodes[index]
            position = QPointF(node.x, node.y)
            may_round = (
                node.roundness > 0
                and (node.point_type == "vector" or not node.handles_locked)
                and (bound.closed or 0 < index < len(nodes) - 1)
            )
            if not may_round:
                return position, position
            previous = nodes[index - 1 if index else len(nodes) - 1]
            following = nodes[(index + 1) % len(nodes)]
            before = QPointF(previous.x - node.x, previous.y - node.y)
            after = QPointF(following.x - node.x, following.y - node.y)
            before_length = max(1e-6, math.hypot(before.x(), before.y()))
            after_length = max(1e-6, math.hypot(after.x(), after.y()))
            distance = min(
                node.roundness, before_length / 2, after_length / 2
            )
            return (
                position + before * (distance / before_length),
                position + after * (distance / after_length),
            )

        rounded = [rounding(index) for index in range(len(nodes))]

        def segment(start_index: int, end_index: int) -> None:
            start_node, end_node = nodes[start_index], nodes[end_index]
            target = rounded[end_index][0]
            outgoing = start_node.outgoing
            incoming = end_node.incoming
            if outgoing is not None or incoming is not None:
                control_a = (
                    QPointF(*outgoing)
                    if outgoing is not None
                    else QPointF(rounded[start_index][1])
                )
                control_b = (
                    QPointF(*incoming)
                    if incoming is not None else QPointF(target)
                )
                path.cubicTo(control_a, control_b, target)
            else:
                path.lineTo(target)
            if rounded[end_index][0] != rounded[end_index][1]:
                path.quadTo(
                    QPointF(end_node.x, end_node.y), rounded[end_index][1]
                )

        path.moveTo(rounded[0][1])
        for index in range(1, len(nodes)):
            segment(index - 1, index)
        if bound.closed:
            segment(len(nodes) - 1, 0)
            path.closeSubpath()
        return path

    @classmethod
    def open_shape_mesh(
        cls, bound: BoundGeometry, base_width: float,
        extra_width: float = 0.0,
        start_cap: str = "round", end_cap: str = "round",
    ) -> QPainterPath:
        """Build a filled, variable-width ribbon for an open path."""
        curve = cls.bound_path(bound)
        if curve.isEmpty():
            return QPainterPath()
        samples: list[tuple[QPointF, float]] = []
        approximate_length = 0.0
        for first, second in zip(bound.nodes, bound.nodes[1:]):
            chain = [
                first.position,
                first.outgoing or first.position,
                second.incoming or second.position,
                second.position,
            ]
            approximate_length += sum(
                math.dist(a, b) for a, b in zip(chain, chain[1:])
            )
        steps = max(16, min(
            1024, max(len(bound.nodes) * 16, math.ceil(approximate_length / 6))
        ))
        for index in range(steps + 1):
            percent = index / steps
            point = curve.pointAtPercent(percent)
            node_position = percent * max(1, len(bound.nodes) - 1)
            segment = min(len(bound.nodes) - 2, int(node_position))
            fraction = node_position - segment
            smooth = fraction * fraction * (3 - 2 * fraction)
            first = bound.nodes[segment].width_multiplier
            second = bound.nodes[segment + 1].width_multiplier
            multiplier = first + (second - first) * smooth
            samples.append((
                point, max(0.1, base_width * multiplier + extra_width)
            ))
        left: list[QPointF] = []
        right: list[QPointF] = []
        for index, (point, width) in enumerate(samples):
            previous = samples[max(0, index - 1)][0]
            following = samples[min(len(samples) - 1, index + 1)][0]
            dx, dy = following.x() - previous.x(), following.y() - previous.y()
            length = max(1e-6, math.hypot(dx, dy))
            normal = QPointF(-dy / length, dx / length) * (width / 2)
            left.append(point + normal)
            right.append(point - normal)
        start_tangent = samples[1][0] - samples[0][0]
        end_tangent = samples[-1][0] - samples[-2][0]
        for points, tangent, cap, direction in (
            ((left, right), start_tangent, start_cap, -1),
            ((left, right), end_tangent, end_cap, 1),
        ):
            length = max(1e-6, math.hypot(tangent.x(), tangent.y()))
            unit = QPointF(tangent.x() / length, tangent.y() / length)
            target_index = 0 if direction < 0 else -1
            width = samples[target_index][1]
            if cap == "square":
                offset = unit * (direction * width / 2)
                left[target_index] += offset
                right[target_index] += offset
        mesh = QPainterPath()
        mesh.setFillRule(Qt.WindingFill)
        if start_cap == "point":
            tangent = start_tangent
            length = max(1e-6, math.hypot(tangent.x(), tangent.y()))
            tip = samples[0][0] - tangent * (
                samples[0][1] / 2 / length
            )
            mesh.moveTo(tip)
        else:
            mesh.moveTo(left[0])
        for point in left:
            mesh.lineTo(point)
        if end_cap == "point":
            tangent = end_tangent
            length = max(1e-6, math.hypot(tangent.x(), tangent.y()))
            mesh.lineTo(samples[-1][0] + tangent * (
                samples[-1][1] / 2 / length
            ))
        for point in reversed(right):
            mesh.lineTo(point)
        mesh.closeSubpath()
        if start_cap == "round":
            width = samples[0][1]
            mesh.addEllipse(samples[0][0], width / 2, width / 2)
        if end_cap == "round":
            width = samples[-1][1]
            mesh.addEllipse(samples[-1][0], width / 2, width / 2)
        return mesh

    @classmethod
    def layer_shape_path(cls, layer: LayerNode) -> QPainterPath:
        if layer.layer_kind == "open_shape":
            return cls.open_shape_mesh(
                layer.bound, layer.shape_style.base_thickness,
                layer.shape_style.outline_thickness * 2,
                layer.shape_style.start_cap, layer.shape_style.end_cap,
            )
        return cls.bound_path(layer.bound, layer.vertex_radius)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#242428"))
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.chapter is None:
            painter.setPen(QColor("#8e8e96"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Create or open a series to begin")
            return
        if (
            not self._transform_static_cache.isNull()
            and self._transform_preview_quad is not None
            and isinstance(
                self.chapter.objects.get(self.selected_object_id), RasterObject
            )
        ):
            painter.drawImage(0, 0, self._transform_static_cache)
            painter.setTransform(self.camera_transform())
            painter.save()
            painter.setClipRect(
                QRectF(0, 0, self.chapter.width, self.chapter.height)
            )
            self._render_selected_raster_preview(
                painter, self.visible_document_rect()
            )
            self._draw_selection(painter)
            painter.restore()
            return
        painter.setTransform(self.camera_transform())
        painter.fillRect(
            QRectF(0, 0, self.chapter.width, self.chapter.height),
            QColor(self.chapter.background),
        )
        painter.save()
        painter.setClipRect(QRectF(0, 0, self.chapter.width, self.chapter.height))
        visible = self.visible_document_rect()
        for page_id in reversed(self.chapter.root_page_ids):
            self._render_layer(painter, self.chapter.layers[page_id], 1.0, visible)
        self._draw_grid(painter, visible)
        self._draw_predictive_ink(painter)
        self._draw_selection(painter)
        self._draw_creation_preview(painter)
        painter.restore()
        painter.setTransform(QTransform())
        painter.setPen(QPen(QColor("#44444d"), 1))
        chapter_poly = self.camera_transform().map(QPolygonF(QRectF(
            0, 0, self.chapter.width, self.chapter.height
        )))
        painter.drawPolygon(chapter_poly)

    def render_preview(self, image: QImage, clip: QRect | None = None) -> None:
        if self.chapter is None or image.isNull():
            return
        painter = QPainter(image)
        if clip is not None:
            painter.setClipRect(clip)
            painter.fillRect(clip, QColor(self.chapter.background))
        else:
            painter.fillRect(image.rect(), QColor(self.chapter.background))
        transform = QTransform()
        transform.scale(image.width() / self.chapter.width, image.height() / self.chapter.height)
        painter.setTransform(transform)
        visible = QRectF(0, 0, self.chapter.width, self.chapter.height)
        for page_id in reversed(self.chapter.root_page_ids):
            self._render_layer(painter, self.chapter.layers[page_id], 1.0, visible)
        painter.end()

    def _render_layer(
        self, painter: QPainter, layer: LayerNode, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        if not layer.visible or layer.opacity <= 0:
            return
        if layer.layer_kind == "fill":
            parent = self.chapter.layers.get(layer.parent_id)
            if parent is None or parent.bound is None:
                return
            painter.save()
            painter.setOpacity(parent_opacity * layer.opacity)
            painter.fillPath(
                self.bound_path(parent.bound, parent.vertex_radius),
                QColor(layer.fill_color or "#111111"),
            )
            painter.restore()
            return
        painter.save()
        painter.translate(layer.translate_x, layer.translate_y)
        if layer.layer_kind == "open_shape":
            style = layer.shape_style
            painter.setOpacity(parent_opacity * layer.opacity)
            if style.outline_thickness > 0:
                painter.fillPath(
                    self.open_shape_mesh(
                        layer.bound, style.base_thickness,
                        style.outline_thickness * 2,
                        style.start_cap, style.end_cap,
                    ),
                    QColor(style.outline_color),
                )
            painter.fillPath(
                self.open_shape_mesh(
                    layer.bound, style.base_thickness, 0,
                    style.start_cap, style.end_cap,
                ),
                QColor(style.primary_color or "#111111"),
            )
            painter.restore()
            return
        layer_path = self.bound_path(layer.bound, layer.vertex_radius)
        painter.setClipPath(layer_path, Qt.IntersectClip)
        opacity = parent_opacity * layer.opacity
        if layer.fill_color:
            painter.save()
            painter.setOpacity(opacity)
            painter.fillPath(layer_path, QColor(layer.fill_color))
            painter.restore()
        world_x, world_y = self.chapter.layer_world_translation(layer.layer_id)
        local_visible = visible_world.translated(-world_x, -world_y)
        for child in reversed(layer.children):
            if child.kind == "layer":
                self._render_layer(
                    painter, self.chapter.layers[child.entity_id], opacity, visible_world
                )
            else:
                self._render_object(
                    painter, self.chapter.objects[child.entity_id], opacity,
                    local_visible,
                )
        if layer.border_width > 0:
            painter.save()
            painter.setOpacity(opacity)
            painter.setClipPath(layer_path, Qt.IntersectClip)
            pen = QPen(
                QColor(layer.border_color), layer.border_width * 2,
                Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin,
            )
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(layer_path)
            painter.restore()
        painter.restore()

    def _render_object(
        self, painter: QPainter, obj: DocumentObject, parent_opacity: float,
        local_visible: QRectF,
    ) -> None:
        if obj.object_id == self._render_excluded_object_id:
            return
        if not obj.visible:
            return
        painter.save()
        painter.setOpacity(parent_opacity if obj.opacity_locked else parent_opacity * obj.opacity)
        if isinstance(obj, RasterObject):
            if (
                obj.object_id == self.selected_object_id
                and self.tool == ToolKind.TRANSFORM
                and self._transform_preview_quad is not None
            ):
                source = QRectF(*obj.interaction_rect).translated(obj.x, obj.y)
                transform = self._quad_transform(source, self._transform_preview_quad)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                painter.setTransform(transform, True)
                for (tile_x, tile_y), image in self.tiles.iter_tiles(obj.object_id):
                    painter.drawImage(
                        obj.x + tile_x * obj.tile_size,
                        obj.y + tile_y * obj.tile_size,
                        image,
                    )
                painter.restore()
                return
            painter.translate(obj.x, obj.y)
            object_visible = local_visible.translated(-obj.x, -obj.y)
            for (tile_x, tile_y), image in self.tiles.iter_tiles(obj.object_id, object_visible):
                painter.drawImage(tile_x * obj.tile_size, tile_y * obj.tile_size, image)
        elif isinstance(obj, TextObject):
            self._draw_text_object(painter, obj)
        painter.restore()

    def _text_document(self, obj: TextObject, width: float) -> QTextDocument:
        document = QTextDocument()
        document.setDocumentMargin(0)
        font = QFont(obj.font_family)
        font.setPixelSize(max(1, round(obj.font_size)))
        font.setBold(obj.bold)
        font.setItalic(obj.italic)
        font.setLetterSpacing(QFont.AbsoluteSpacing, obj.kerning)
        document.setDefaultFont(font)
        document.setPlainText(obj.text)
        document.setTextWidth(max(1.0, width))
        cursor = QTextCursor(document)
        cursor.select(QTextCursor.Document)
        block = QTextBlockFormat()
        block.setAlignment({
            "left": Qt.AlignLeft,
            "center": Qt.AlignHCenter,
            "right": Qt.AlignRight,
        }[obj.horizontal_alignment])
        cursor.mergeBlockFormat(block)
        return document

    def _strict_text_rect(self, obj: TextObject) -> QRectF:
        parent = self.chapter.layers[obj.parent_layer_id]
        left, top, width, height = parent.bound.bbox()
        margin = min(max(0.0, obj.margin), max(0.0, min(width, height) / 2 - 1))
        return QRectF(
            left + margin, top + margin,
            max(1.0, width - margin * 2), max(1.0, height - margin * 2),
        )

    @staticmethod
    def _rect_quad(rect: QRectF) -> list[tuple[float, float]]:
        return [
            (rect.left(), rect.top()), (rect.right(), rect.top()),
            (rect.right(), rect.bottom()), (rect.left(), rect.bottom()),
        ]

    def _text_quad(self, obj: TextObject) -> list[tuple[float, float]]:
        if obj.transform_quad is None:
            obj.transform_quad = self._rect_quad(QRectF(obj.x, obj.y, obj.width, obj.height))
        return list(obj.transform_quad)

    @staticmethod
    def _quad_transform(
        source: QRectF, quad: list[tuple[float, float]],
    ) -> QTransform:
        source_quad = QPolygonF([
            source.topLeft(), source.topRight(), source.bottomRight(), source.bottomLeft()
        ])
        destination = QPolygonF([QPointF(*point) for point in quad])
        transform = QTransform.quadToQuad(source_quad, destination)
        return transform if isinstance(transform, QTransform) else QTransform()

    def _text_vertical_offset(
        self, obj: TextObject, document: QTextDocument, available_height: float,
    ) -> float:
        content_height = min(available_height, document.size().height())
        if obj.vertical_alignment == "bottom":
            return max(0.0, available_height - content_height)
        if obj.vertical_alignment == "middle":
            return max(0.0, (available_height - content_height) / 2)
        return 0.0

    def _draw_text_object(self, painter: QPainter, obj: TextObject) -> None:
        if obj.layout_mode == "strict":
            rect = self._strict_text_rect(obj)
            document = self._text_document(obj, rect.width())
            offset = self._text_vertical_offset(obj, document, rect.height())
            painter.save()
            painter.setClipRect(rect)
            painter.translate(rect.left(), rect.top() + offset)
            self._draw_text_document(painter, obj, document)
            painter.restore()
            return
        source = QRectF(0, 0, max(1.0, obj.width), max(1.0, obj.height))
        document = self._text_document(obj, source.width())
        transform = self._quad_transform(source, self._text_quad(obj))
        painter.save()
        painter.setTransform(transform, True)
        painter.setClipRect(source)
        self._draw_text_document(painter, obj, document)
        painter.restore()

    def _draw_text_document(
        self, painter: QPainter, obj: TextObject, document: QTextDocument,
    ) -> None:
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.Text, QColor("#111111"))
        editing = self._text_editing and obj.object_id == self.selected_object_id
        if editing and self._text_cursor_position != self._text_selection_anchor:
            selection = QAbstractTextDocumentLayout.Selection()
            cursor = QTextCursor(document)
            cursor.setPosition(self._text_selection_anchor)
            cursor.setPosition(self._text_cursor_position, QTextCursor.KeepAnchor)
            selection.cursor = cursor
            selection.format.setBackground(QColor(70, 145, 210, 150))
            selection.format.setForeground(QColor("#ffffff"))
            context.selections = [selection]
        document.documentLayout().draw(painter, context)
        if editing and self.hasFocus() and self._text_cursor_position == self._text_selection_anchor:
            caret = self._text_caret_rect(document, self._text_cursor_position)
            pen = QPen(QColor("#111111"), 1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(caret.topLeft(), caret.bottomLeft())

    @staticmethod
    def _text_caret_rect(document: QTextDocument, position: int) -> QRectF:
        cursor = QTextCursor(document)
        cursor.setPosition(max(0, min(position, len(document.toPlainText()))))
        block = cursor.block()
        layout = block.layout()
        relative = cursor.position() - block.position()
        line = layout.lineForTextPosition(relative)
        if not line.isValid() and layout.lineCount():
            line = layout.lineAt(layout.lineCount() - 1)
        block_rect = document.documentLayout().blockBoundingRect(block)
        x = line.cursorToX(relative) if line.isValid() else 0.0
        if isinstance(x, tuple):
            x = x[0]
        y = block_rect.top() + (line.y() if line.isValid() else 0.0)
        height = line.height() if line.isValid() else QFontMetricsF(document.defaultFont()).height()
        return QRectF(float(x), y, 1.0, height)

    def _draw_grid(self, painter: QPainter, visible: QRectF) -> None:
        if self.selected_kind == "layer" and self.selected_id in self.chapter.layers:
            grid = self.chapter.effective_grid(self.selected_id)
        elif self.selected_kind == "object" and self.selected_id in self.chapter.objects:
            grid = self.chapter.effective_grid(
                self.chapter.objects[self.selected_id].parent_layer_id
            )
        else:
            grid = self.chapter.grid
        if not grid.enabled:
            return
        step = grid.size / grid.divisions
        color = QColor(grid.color)
        color.setAlphaF(grid.opacity)
        pen = QPen(color, 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        left = max(0, math.floor((visible.left() - grid.origin_x) / step) * step + grid.origin_x)
        right = min(self.chapter.width, visible.right())
        top = max(0, math.floor((visible.top() - grid.origin_y) / step) * step + grid.origin_y)
        bottom = min(self.chapter.height, visible.bottom())
        x = left
        while x <= right:
            painter.drawLine(QPointF(x, max(0, visible.top())), QPointF(x, bottom))
            x += step
        y = top
        while y <= bottom:
            painter.drawLine(QPointF(max(0, visible.left()), y), QPointF(right, y))
            y += step

    def _draw_predictive_ink(self, painter: QPainter) -> None:
        if not self.settings.predictive_ink or self._predictive is None:
            return
        if self.chapter is None or self.selected_kind != "object":
            return
        obj = self.chapter.objects.get(self.selected_id)
        if not isinstance(obj, RasterObject):
            return
        painter.save()
        for layer in self.chapter.ancestor_layers(obj.parent_layer_id):
            wx, wy = self.chapter.layer_world_translation(layer.layer_id)
            transform = QTransform()
            transform.translate(wx, wy)
            if layer.bound is not None:
                painter.setClipPath(
                    transform.map(self.bound_path(layer.bound, layer.vertex_radius)),
                    Qt.IntersectClip,
                )
        start, end, size, color = self._predictive
        preview = QColor(color)
        preview.setAlpha(110)
        pen = QPen(preview, size, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(start, end)
        painter.restore()

    def _draw_selection(self, painter: QPainter) -> None:
        if (
            self.tool == ToolKind.TEXT_EDIT
            and not isinstance(
                self.chapter.objects.get(self.selected_object_id), TextObject
            )
            and self.active_page_id
        ):
            painter.save()
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(
                QColor("#70c8ff"), 1.5 / max(self.scale, 0.05), Qt.DashLine
            ))
            visible = self.visible_document_rect()
            for object_id in self._objects_front_to_back(self.active_page_id):
                obj = self.chapter.objects[object_id]
                rect = self.object_world_rect(object_id)
                if (
                    isinstance(obj, TextObject) and obj.visible and rect is not None
                    and rect.intersects(visible)
                ):
                    painter.drawPolygon(QPolygonF([
                        QPointF(*point)
                        for point in self.object_world_quad(object_id)
                    ]))
            painter.restore()
        if not self.selected_id:
            return
        painter.save()
        painter.setBrush(Qt.NoBrush)
        if self.selected_object_id and self.active_layer_id in self.chapter.layers:
            active = self.chapter.layers[self.active_layer_id]
            world_x, world_y = self.chapter.layer_world_translation(active.layer_id)
            painter.save()
            painter.translate(world_x, world_y)
            orange = QPen(
                QColor("#f2a23a"), 2 / max(self.scale, 0.05), Qt.DotLine
            )
            painter.setPen(orange)
            if active.bound is not None:
                painter.drawPath(self.bound_path(active.bound, active.vertex_radius))
            painter.restore()
        pen = QPen(QColor("#36b7ff"), 2 / max(self.scale, 0.05), Qt.DashLine)
        painter.setPen(pen)
        if self.selected_kind == "layer":
            layer = self.chapter.layers.get(self.selected_id)
            if layer:
                world_x, world_y = self.chapter.layer_world_translation(layer.layer_id)
                painter.translate(world_x, world_y)
                if layer.bound is not None:
                    painter.drawPath(self.bound_path(layer.bound, layer.vertex_radius))
                if self.tool == ToolKind.BOUND_EDIT and layer.bound is not None:
                    self._draw_shape_edit_handles(painter, layer)
        else:
            quad = self._selected_world_quad()
            if quad:
                selected_object = self.chapter.objects.get(self.selected_id)
                hide_raster_frame = (
                    isinstance(selected_object, RasterObject)
                    and self.tool in {
                        ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER
                    }
                )
                polygon = QPolygonF([QPointF(*point) for point in quad])
                if not hide_raster_frame:
                    painter.drawPolygon(polygon)
                if self.tool == ToolKind.TRANSFORM:
                    radius = 7 / max(self.scale, 0.05)
                    painter.setBrush(QColor("#f5f5f5"))
                    for point in self._quad_handles(quad):
                        painter.drawEllipse(QPointF(*point), radius, radius)
                if (
                    self.tool == ToolKind.BOUND_EDIT
                    and isinstance(selected_object, RasterObject)
                ):
                    radius = 6 / max(self.scale, 0.05)
                    painter.setBrush(QColor("#ffffff"))
                    for point in self._quad_handles(quad):
                        painter.drawRect(QRectF(
                            point[0] - radius, point[1] - radius,
                            radius * 2, radius * 2,
                        ))
                obj = self.chapter.objects.get(self.selected_id)
                if (
                    self.tool == ToolKind.TEXT_EDIT
                    and isinstance(obj, TextObject)
                    and obj.layout_mode == "strict"
                ):
                    radius = 5 / max(self.scale, 0.05)
                    painter.setBrush(QColor("#ffcc66"))
                    rect = self.object_world_rect(obj.object_id)
                    for point in self._edge_midpoints(self._rect_quad(rect)):
                        painter.drawRect(QRectF(
                            point[0] - radius, point[1] - radius,
                            radius * 2, radius * 2,
                        ))
        painter.restore()

    def _draw_path_node_handle(
        self, painter: QPainter, node: PathNode, selected: bool,
        hovered: bool = False,
    ) -> None:
        scale = max(self.scale, 0.05)
        point_color = QColor("#FF7417" if selected else "#0097D7")
        gizmo_color = QColor("#FFBE00" if selected else "#9BDDF0")
        point_radius = 6 / scale
        control_radius = 4.5 / scale
        painter.save()
        painter.setBrush(QColor("#ffffff"))
        if hovered and not selected:
            painter.setPen(QPen(QColor("#9BDDF0"), 2 / scale))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(
                QPointF(node.x, node.y),
                point_radius + 3 / scale,
                point_radius + 3 / scale,
            )
            painter.setBrush(QColor("#ffffff"))
        if node.point_type == "bezier":
            painter.setPen(QPen(gizmo_color, 2 / scale))
            for control in (node.incoming, node.outgoing):
                if control is None:
                    continue
                painter.drawLine(QPointF(node.x, node.y), QPointF(*control))
                painter.drawEllipse(
                    QPointF(*control), control_radius, control_radius
                )
            painter.setPen(QPen(point_color, 3 / scale))
            painter.drawEllipse(
                QPointF(node.x, node.y), point_radius, point_radius
            )
        else:
            painter.setPen(QPen(point_color, 3 / scale))
            x, y = node.x, node.y
            painter.drawPolygon(QPolygonF([
                QPointF(x, y - point_radius),
                QPointF(x + point_radius, y),
                QPointF(x, y + point_radius),
                QPointF(x - point_radius, y),
            ]))
        painter.restore()

    def _shape_gizmo_positions(
        self, bound: BoundGeometry, node: PathNode,
    ) -> dict[str, QPointF]:
        scale = max(self.scale, 0.05)
        index = bound.nodes.index(node)
        previous = bound.nodes[index - 1] if index else bound.nodes[0]
        following = (
            bound.nodes[(index + 1) % len(bound.nodes)]
            if bound.closed or index + 1 < len(bound.nodes) else bound.nodes[-1]
        )
        tangent = QPointF(
            following.x - previous.x, following.y - previous.y
        )
        length = max(1e-6, math.hypot(tangent.x(), tangent.y()))
        normal = QPointF(-tangent.y() / length, tangent.x() / length)
        position = QPointF(node.x, node.y)
        side = normal * ((24 + node.width_multiplier * 10) / scale)
        opposite = normal * (-max(24 / scale, node.roundness))
        result = {
            "thickness": position + side,
            "type": position + QPointF(44 / scale, -34 / scale),
        }
        may_round = (
            0 < index < len(bound.nodes) - 1 or bound.closed
        ) and (
            node.point_type == "vector"
            or (node.point_type == "bezier" and not node.handles_locked)
        )
        if may_round:
            result["roundness"] = position + opposite
        if node.point_type == "bezier" and 0 < index < len(bound.nodes) - 1:
            result["lock"] = position + QPointF(50 / scale, 4 / scale)
        if not bound.closed and index in {0, len(bound.nodes) - 1}:
            result["cap"] = position + QPointF(44 / scale, 38 / scale)
        return result

    def _rectangle_radius_positions(
        self, bound: BoundGeometry,
    ) -> list[QPointF]:
        """Return per-corner radius handles on each interior bisector."""
        scale = max(self.scale, 0.05)
        center = QPointF(
            sum(node.x for node in bound.nodes) / len(bound.nodes),
            sum(node.y for node in bound.nodes) / len(bound.nodes),
        )
        positions: list[QPointF] = []
        for index, node in enumerate(bound.nodes):
            previous = bound.nodes[index - 1]
            following = bound.nodes[(index + 1) % len(bound.nodes)]
            toward_previous = QPointF(
                previous.x - node.x, previous.y - node.y
            )
            toward_following = QPointF(
                following.x - node.x, following.y - node.y
            )
            previous_length = max(
                1e-6, math.hypot(toward_previous.x(), toward_previous.y())
            )
            following_length = max(
                1e-6, math.hypot(toward_following.x(), toward_following.y())
            )
            direction = (
                toward_previous / previous_length
                + toward_following / following_length
            )
            direction_length = math.hypot(direction.x(), direction.y())
            if direction_length < 1e-6:
                direction = center - QPointF(node.x, node.y)
                direction_length = max(
                    1e-6, math.hypot(direction.x(), direction.y())
                )
            direction /= direction_length
            to_center = center - QPointF(node.x, node.y)
            if QPointF.dotProduct(direction, to_center) < 0:
                direction *= -1
            maximum = min(previous_length, following_length) / 2
            radius = min(maximum, max(0.0, node.roundness))
            distance = max(18 * math.sqrt(2) / scale, radius * math.sqrt(2))
            positions.append(
                QPointF(node.x, node.y) + direction * distance
            )
        return positions

    def _rectangle_edit_handles(
        self, bound: BoundGeometry,
    ) -> list[tuple[float, float]]:
        if self.settings.rectangle_edit_mode == "normal":
            return self._bound_handles(bound)
        points = [QPointF(node.x, node.y) for node in bound.nodes]
        return [
            *(point.toTuple() for point in points),
            *(
                ((points[index] + points[(index + 1) % 4]) / 2).toTuple()
                for index in range(4)
            ),
        ]

    def _nearest_shape_insert(
        self, bound: BoundGeometry, local: QPointF,
    ) -> tuple[int, float, QPointF] | None:
        best: tuple[float, int, float, QPointF] | None = None
        segment_count = (
            len(bound.nodes) if bound.closed else len(bound.nodes) - 1
        )
        for segment in range(max(0, segment_count)):
            for step in range(33):
                percent = step / 32
                point = self._shape_segment_point(bound, segment, percent)
                distance = math.dist(
                    (local.x(), local.y()), (point.x(), point.y())
                )
                if best is None or distance < best[0]:
                    best = distance, segment, percent, point
        if best is None or best[0] > 10 / max(self.scale, 0.05):
            return None
        _distance, segment, percent, point = best
        return segment, percent, point

    @staticmethod
    def _shape_segment_point(
        bound: BoundGeometry, segment: int, percent: float,
    ) -> QPointF:
        start = bound.nodes[segment]
        end = bound.nodes[(segment + 1) % len(bound.nodes)]
        p0 = QPointF(start.x, start.y)
        p3 = QPointF(end.x, end.y)
        if start.outgoing is None and end.incoming is None:
            return p0 * (1 - percent) + p3 * percent
        p1 = QPointF(*(start.outgoing or start.position))
        p2 = QPointF(*(end.incoming or end.position))
        inverse = 1 - percent
        return (
            p0 * (inverse ** 3)
            + p1 * (3 * inverse * inverse * percent)
            + p2 * (3 * inverse * percent * percent)
            + p3 * (percent ** 3)
        )

    def _shape_hit_test(
        self, bound: BoundGeometry, local: QPointF,
        include_insert: bool = True,
    ) -> dict | None:
        """Resolve one shape target using the same priority as rendering."""
        scale = max(self.scale, 0.05)
        tolerance = 14 / scale
        selected = self._selected_shape_node(bound)
        if bound.primitive == "rectangle":
            for index, position in enumerate(
                self._rectangle_radius_positions(bound)
            ):
                if math.dist(
                    (local.x(), local.y()), (position.x(), position.y())
                ) <= 11 / scale:
                    return {
                        "kind": "radius", "index": index,
                        "node_id": bound.nodes[index].node_id,
                        "position": position,
                    }
        elif selected is not None and bound.primitive == "custom":
            for name, position in self._shape_gizmo_positions(
                bound, selected
            ).items():
                if math.dist(
                    (local.x(), local.y()), (position.x(), position.y())
                ) <= 12 / scale:
                    return {
                        "kind": "gizmo", "name": name,
                        "node_id": selected.node_id, "position": position,
                    }
        if bound.primitive == "custom":
            for node in bound.nodes:
                for name, control in (
                    ("incoming", node.incoming), ("outgoing", node.outgoing)
                ):
                    if control is not None and math.dist(
                        (local.x(), local.y()), control
                    ) <= tolerance:
                        return {
                            "kind": "control", "name": name,
                            "node_id": node.node_id,
                            "position": QPointF(*control),
                        }
        if (
            bound.primitive != "ellipse"
            and not (
                bound.primitive == "rectangle"
                and self.settings.rectangle_edit_mode == "normal"
            )
        ):
            for index, node in enumerate(bound.nodes):
                if math.dist(
                    (local.x(), local.y()), node.position
                ) <= tolerance:
                    return {
                        "kind": (
                            "rectangle_point"
                            if bound.primitive == "rectangle" else "node"
                        ),
                        "index": index, "node_id": node.node_id,
                        "position": QPointF(node.x, node.y),
                    }
        if bound.primitive in {"rectangle", "ellipse"}:
            if bound.primitive == "rectangle":
                handles = self._rectangle_edit_handles(bound)
                start = (
                    0 if self.settings.rectangle_edit_mode == "normal" else 4
                )
            else:
                handles = self._bound_handles(bound)
                start = 0
            for index, position in enumerate(
                handles[start:], start
            ):
                if math.dist(
                    (local.x(), local.y()), position
                ) <= tolerance:
                    return {
                        "kind": (
                            "rectangle_edge"
                            if (
                                bound.primitive == "rectangle"
                                and self.settings.rectangle_edit_mode == "free"
                            )
                            else "primitive_handle"
                        ),
                        "index": index,
                        "position": QPointF(*position),
                    }
        if include_insert:
            insertion = self._nearest_shape_insert(bound, local)
            if insertion is not None:
                return {
                    "kind": "insert", "insert": insertion,
                    "position": insertion[2],
                }
        if bound.closed and self.bound_path(bound).contains(local):
            return {"kind": "interior", "position": QPointF(local)}
        return None

    def _draw_shape_edit_handles(
        self, painter: QPainter, layer: LayerNode,
    ) -> None:
        bound = layer.bound
        hover = self._shape_hover_target or {}
        if bound.primitive in {"rectangle", "ellipse"}:
            scale = max(self.scale, 0.05)
            selected_index = next((
                index for index, node in enumerate(bound.nodes)
                if node.node_id == self._selected_shape_node_id
            ), -1)
            handles = (
                self._rectangle_edit_handles(bound)
                if bound.primitive == "rectangle"
                else self._bound_handles(bound)
            )
            for index, point in enumerate(handles):
                selected = index < 4 and index == selected_index
                hovered = (
                    hover.get("kind") in {
                        "primitive_handle", "rectangle_point",
                        "rectangle_edge",
                    }
                    and hover.get("index") == index
                )
                color = QColor("#FF7417" if selected else "#0097D7")
                radius = 6 / scale if index < 4 else 4.5 / scale
                painter.setPen(QPen(
                    QColor("#9BDDF0") if hovered and not selected else color,
                    (4 if hovered else 3) / scale,
                ))
                painter.setBrush(QColor("#ffffff"))
                painter.drawRect(QRectF(
                    point[0] - radius, point[1] - radius,
                    radius * 2, radius * 2,
                ))
            if bound.primitive == "rectangle":
                for index, point in enumerate(
                    self._rectangle_radius_positions(bound)
                ):
                    selected = (
                        self._active_shape_control
                        == f"primitive_roundness:{index}"
                        or selected_index == index
                    )
                    hovered = (
                        hover.get("kind") == "radius"
                        and hover.get("index") == index
                    )
                    color = QColor(
                        "#FFBE00" if selected else "#9BDDF0"
                    )
                    painter.setPen(QPen(
                        color, (3 if hovered or selected else 2) / scale
                    ))
                    painter.drawLine(
                        QPointF(*bound.nodes[index].position), point
                    )
                    painter.setBrush(QColor("#ffffff"))
                    painter.drawEllipse(point, 4.5 / scale, 4.5 / scale)
            if self._shape_hover_insert is not None:
                point = self._shape_hover_insert[2]
                radius = 3 / max(self.scale, 0.05)
                painter.setPen(QPen(
                    QColor("#9BDDF0"), 2 / max(self.scale, 0.05)
                ))
                painter.setBrush(QColor("#ffffff"))
                painter.drawEllipse(point, radius, radius)
            return
        for node in bound.nodes:
            selected = node.node_id == self._selected_shape_node_id
            hovered = (
                hover.get("kind") == "node"
                and hover.get("node_id") == node.node_id
            )
            self._draw_path_node_handle(painter, node, selected, hovered)
            if selected:
                self._draw_selected_shape_gizmos(
                    painter, bound, node, layer.shape_style
                )
        if self._shape_hover_insert is not None:
            point = self._shape_hover_insert[2]
            radius = 3 / max(self.scale, 0.05)
            painter.setPen(QPen(QColor("#9BDDF0"), 2 / max(self.scale, 0.05)))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(point, radius, radius)

    def _draw_selected_shape_gizmos(
        self, painter: QPainter, bound: BoundGeometry, node: PathNode,
        style: ShapeStyle | None = None,
    ) -> None:
        scale = max(self.scale, 0.05)
        positions = self._shape_gizmo_positions(bound, node)
        painter.setPen(QPen(QColor("#FFBE00"), 2 / scale))
        painter.setBrush(QColor("#ffffff"))
        for name, point in positions.items():
            painter.drawLine(QPointF(node.x, node.y), point)
            radius = 4.5 / scale
            if name in {"type", "lock", "cap"}:
                painter.drawRect(QRectF(
                    point.x() - radius, point.y() - radius,
                    radius * 2, radius * 2,
                ))
                if name == "type":
                    size = 2.5 / scale
                    painter.drawPolygon(QPolygonF([
                        point + QPointF(0, -size),
                        point + QPointF(size, 0),
                        point + QPointF(0, size),
                        point + QPointF(-size, 0),
                    ]))
                elif name == "lock":
                    painter.drawLine(
                        point + QPointF(-2 / scale, 0),
                        point + QPointF(2 / scale, 0),
                    )
                    if not node.handles_locked:
                        painter.drawLine(
                            point + QPointF(0, -2 / scale),
                            point + QPointF(0, 2 / scale),
                        )
                elif name == "cap":
                    cap = (
                        style.start_cap
                        if style and node is bound.nodes[0]
                        else style.end_cap if style else "round"
                    )
                    painter.drawLine(
                        point + QPointF(0, -2.5 / scale),
                        point + QPointF(0, 2.5 / scale),
                    )
                    if cap == "round":
                        painter.drawArc(QRectF(
                            point.x() - 2 / scale,
                            point.y() - 2.5 / scale,
                            4 / scale, 5 / scale,
                        ), 90 * 16, 180 * 16)
                    elif cap == "point":
                        painter.drawLine(
                            point + QPointF(0, -2.5 / scale),
                            point + QPointF(2.5 / scale, 0),
                        )
                        painter.drawLine(
                            point + QPointF(2.5 / scale, 0),
                            point + QPointF(0, 2.5 / scale),
                        )
            else:
                painter.drawEllipse(point, radius, radius)

    def _draw_creation_preview(self, painter: QPainter) -> None:
        if not self._creation_points and not self._creation_nodes:
            return
        painter.save()
        pen = QPen(QColor("#ffb347"), 2 / max(self.scale, 0.05), Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QColor(255, 179, 71, 30))
        if self.tool == ToolKind.SHAPE_CREATE and self._creation_nodes:
            geometry = (
                BoundGeometry.path(self._creation_nodes, False)
                if len(self._creation_nodes) >= 2 else None
            )
            painter.setPen(QPen(
                QColor("#111111"), 2 / max(self.scale, 0.05),
                Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin,
            ))
            painter.setBrush(Qt.NoBrush)
            if geometry is not None:
                painter.drawPath(self.bound_path(geometry))
            for node in self._creation_nodes:
                selected = (
                    node.node_id == self._creation_selected_node_id
                )
                hovered = (
                    self._shape_hover_target is not None
                    and self._shape_hover_target.get("kind") == "node"
                    and self._shape_hover_target.get("node_id")
                    == node.node_id
                )
                self._draw_path_node_handle(
                    painter, node, selected, hovered
                )
                if selected and geometry is not None:
                    self._draw_selected_shape_gizmos(
                        painter, geometry, node,
                        self._creation_style or ShapeStyle(),
                    )
            if self._shape_hover_insert is not None:
                point = self._shape_hover_insert[2]
                radius = 3 / max(self.scale, 0.05)
                painter.setPen(QPen(
                    QColor("#9BDDF0"), 2 / max(self.scale, 0.05)
                ))
                painter.setBrush(QColor("#ffffff"))
                painter.drawEllipse(point, radius, radius)
        elif len(self._creation_points) >= 2:
            first, second = self._creation_points[0], self._creation_points[-1]
            if self.tool in {ToolKind.BOX_BOUND, ToolKind.RASTER_CREATE}:
                painter.drawRect(QRectF(QPointF(*first), QPointF(*second)).normalized())
            else:
                radius = math.dist(first, second)
                painter.drawEllipse(QPointF(*first), radius, radius)
        painter.restore()

    # ---- selection and geometry ---------------------------------------
    def object_world_rect(self, object_id: str) -> QRectF | None:
        quad = self.object_world_quad(object_id)
        if not quad:
            return None
        return QPolygonF([QPointF(*point) for point in quad]).boundingRect()

    def object_world_quad(self, object_id: str) -> list[tuple[float, float]] | None:
        obj = self.chapter.objects.get(object_id)
        if obj is None:
            return None
        layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
        if isinstance(obj, TextObject):
            local_quad = (
                self._rect_quad(self._strict_text_rect(obj))
                if obj.layout_mode == "strict" else self._text_quad(obj)
            )
            return [(x + layer_x, y + layer_y) for x, y in local_quad]
        if isinstance(obj, RasterObject):
            bounds = QRectF(*obj.interaction_rect)
            local = QRectF(
                obj.x + bounds.x(), obj.y + bounds.y(),
                bounds.width(), bounds.height(),
            )
            return [
                (x + layer_x, y + layer_y)
                for x, y in self._rect_quad(local)
            ]
        return [
            (layer_x + obj.x, layer_y + obj.y),
            (layer_x + obj.x + 80, layer_y + obj.y),
            (layer_x + obj.x + 80, layer_y + obj.y + 80),
            (layer_x + obj.x, layer_y + obj.y + 80),
        ]

    def _selected_world_quad(self) -> list[tuple[float, float]] | None:
        if (
            self.tool == ToolKind.TRANSFORM
            and self._transform_preview_quad is not None
            and self.selected_object_id
        ):
            obj = self.chapter.objects[self.selected_object_id]
            layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
            return [
                (x + layer_x, y + layer_y)
                for x, y in self._transform_preview_quad
            ]
        return self.object_world_quad(self.selected_id)

    def selected_widget_rect(self) -> QRect:
        if self.chapter is None or not self.selected_id:
            return QRect()
        if self.selected_kind == "object":
            rect = self.object_world_rect(self.selected_id)
        else:
            layer = self.chapter.layers.get(self.selected_id)
            if not layer:
                return QRect()
            if layer.bound is None and layer.parent_id:
                layer = self.chapter.layers[layer.parent_id]
            if layer.bound is None:
                return QRect()
            wx, wy = self.chapter.layer_world_translation(layer.layer_id)
            x, y, width, height = layer.bound.bbox()
            rect = QRectF(wx + x, wy + y, width, height)
            if layer.layer_kind == "open_shape":
                padding = (
                    layer.shape_style.base_thickness
                    * max(
                        node.width_multiplier for node in layer.bound.nodes
                    ) / 2
                    + layer.shape_style.outline_thickness
                )
                rect = rect.adjusted(-padding, -padding, padding, padding)
        if rect is None:
            return QRect()
        polygon = self.camera_transform().map(QPolygonF(rect))
        return polygon.boundingRect().toAlignedRect()

    def _point_inside_layer_masks(self, layer_id: str, point: QPointF) -> bool:
        for layer in self.chapter.ancestor_layers(layer_id):
            wx, wy = self.chapter.layer_world_translation(layer.layer_id)
            if not layer.visible:
                return False
            if layer.bound is not None:
                path = self.bound_path(layer.bound, layer.vertex_radius)
                if not path.contains(QPointF(point.x() - wx, point.y() - wy)):
                    return False
        return True

    def _objects_front_to_back(self, page_id: str) -> list[str]:
        result: list[str] = []

        def walk(layer_id: str) -> None:
            layer = self.chapter.layers[layer_id]
            for child in layer.children:
                if child.kind == "object":
                    result.append(child.entity_id)
                else:
                    walk(child.entity_id)

        walk(page_id)
        return result

    def hit_test_objects(
        self, point: QPointF, text_only: bool = False,
    ) -> list[str]:
        if self.chapter is None:
            return []
        candidates: list[str] = []
        if self.settings.page_scope_select:
            layer_id = self._selected_layer_id()
            page_id = self.active_page_id or (
                self.chapter.page_for_layer(layer_id).layer_id if layer_id else ""
            )
            if not page_id:
                return []
            candidates = self._objects_front_to_back(page_id)
        else:
            layer_id = self._selected_layer_id()
            if not layer_id:
                return []
            candidates = [
                ref.entity_id for ref in self.chapter.layers[layer_id].children
                if ref.kind == "object"
            ]
        hits: list[str] = []
        for object_id in candidates:
            obj = self.chapter.objects[object_id]
            if text_only and not isinstance(obj, TextObject):
                continue
            quad = self.object_world_quad(object_id)
            path = QPainterPath()
            if quad:
                path.addPolygon(QPolygonF([QPointF(*candidate) for candidate in quad]))
            if obj.visible and quad and path.contains(point) and self._point_inside_layer_masks(
                obj.parent_layer_id, point
            ):
                hits.append(object_id)
        return hits

    def hit_test_object(self, point: QPointF) -> str | None:
        hits = self.hit_test_objects(point)
        return hits[0] if hits else None

    def _selected_layer_id(self) -> str | None:
        if self.selected_kind == "layer":
            return self.selected_id
        if self.selected_kind == "object" and self.selected_id in self.chapter.objects:
            return self.chapter.objects[self.selected_id].parent_layer_id
        return None

    def _target_parent_for_new_layer(self) -> str | None:
        placement = self._target_placement_for_new_bound()
        return placement[0] if placement else None

    def _target_placement_for_new_bound(self) -> tuple[str, int] | None:
        if self.chapter is None:
            return None
        if self.selected_kind == "layer" and self.selected_id in self.chapter.layers:
            selected = self.chapter.layers[self.selected_id]
            if selected.is_page:
                return selected.layer_id, 0
            if selected.parent_id:
                siblings = self.chapter.layers[selected.parent_id].children
                index = next((
                    item_index for item_index, reference in enumerate(siblings)
                    if reference.kind == "layer"
                    and reference.entity_id == selected.layer_id
                ), 0)
                return selected.parent_id, index
        if (
            self.selected_kind == "object"
            and self.selected_id in self.chapter.objects
        ):
            return self.chapter.objects[self.selected_id].parent_layer_id, 0
        if self.active_page_id in self.chapter.layers:
            return self.active_page_id, 0
        return None

    def _snap(self, point: QPointF, layer_id: str | None = None) -> QPointF:
        if not self.settings.snap_to_grid or self.chapter is None:
            return QPointF(round(point.x()), round(point.y()))
        grid = self.chapter.effective_grid(layer_id) if layer_id else self.chapter.grid
        x, y = grid.snap(point.x(), point.y())
        return QPointF(x, y)

    # ---- input ---------------------------------------------------------
    def _navigation_mode(self) -> str | None:
        modifiers = QGuiApplication.keyboardModifiers()
        if modifiers == (Qt.AltModifier | Qt.ShiftModifier):
            return "zoom"
        if modifiers == Qt.AltModifier:
            return "pan"
        if modifiers == Qt.ShiftModifier:
            return "rotate"
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._is_touch_mouse(event) or self._tablet_tool_active:
            event.accept()
            return
        if event.button() != Qt.LeftButton or self.chapter is None:
            return
        nav = self._navigation_mode()
        if nav:
            self._begin_navigation(nav, event.position())
            return
        self._tool_press(event.position(), 1.0)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._is_touch_mouse(event) or self._tablet_tool_active:
            event.accept()
            return
        if self._nav_mode:
            self._update_navigation(event.position())
            return
        self._tool_move(event.position(), 1.0)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._is_touch_mouse(event) or self._tablet_tool_active:
            event.accept()
            return
        if self._nav_mode:
            self._end_navigation()
            return
        if event.button() == Qt.LeftButton:
            self._tool_release()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.tool == ToolKind.SHAPE_CREATE and len(self._creation_nodes) >= 2:
            if (
                len(self._creation_nodes) >= 3
                and math.dist(
                    self._creation_nodes[-1].position,
                    self._creation_nodes[-2].position,
                ) <= 12 / max(self.scale, 0.05)
            ):
                self._creation_nodes.pop()
            self._finish_shape(False)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._handle_text_key(event):
            return
        if self.tool == ToolKind.SHAPE_CREATE:
            if (
                event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and len(self._creation_nodes) >= 2
            ):
                self._finish_shape(False)
                return
            if event.key() == Qt.Key_Escape:
                self._creation_nodes.clear()
                self._creation_points.clear()
                self._creation_selected_node_id = ""
                self._creation_active_control = None
                self._creation_style = None
                self._shape_hover_target = None
                self._shape_hover_insert = None
                self.update()
                return
            if event.key() == Qt.Key_Backspace and self._creation_nodes:
                removed = self._creation_nodes.pop()
                if removed.node_id == self._creation_selected_node_id:
                    self._creation_selected_node_id = (
                        self._creation_nodes[-1].node_id
                        if self._creation_nodes else ""
                    )
                self.update()
                return
        if self.tool == ToolKind.RASTER_CREATE and event.key() == Qt.Key_Escape:
            self._creation_points.clear()
            self._raster_creation_parent_id = ""
            self.set_tool(ToolKind.OBJECT_SELECT)
            return
        if event.key() == Qt.Key_Escape:
            self.set_tool(ToolKind.OBJECT_SELECT)
        if (
            self.tool == ToolKind.BOUND_EDIT and self.selected_kind == "layer"
            and self._selected_shape_node_id and self.chapter is not None
        ):
            layer = self.chapter.layers[self.selected_id]
            node = self._selected_shape_node(layer.bound)
            minimum = 3 if layer.bound.closed else 2
            if (
                event.key() == Qt.Key_Delete and node is not None
                and len(layer.bound.nodes) > minimum
            ):
                before = self.chapter.to_dict()
                layer.bound.primitive = "custom"
                layer.bound.nodes.remove(node)
                self._normalize_shape_endpoint_handles(layer.bound)
                self._selected_shape_node_id = ""
                self._push_immediate_shape_change(before, "Delete shape point")
                return
        super().keyPressEvent(event)

    @staticmethod
    def _normalize_shape_endpoint_handles(bound: BoundGeometry) -> None:
        if bound.closed or not bound.nodes:
            return
        first, last = bound.nodes[0], bound.nodes[-1]
        if first.point_type == "bezier":
            first.incoming = None
        if last.point_type == "bezier":
            last.outgoing = None

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:  # noqa: N802
        if self._editing_text_object() is not None and event.commitString():
            self._replace_text_selection(event.commitString())
            event.accept()
            return
        super().inputMethodEvent(event)

    def inputMethodQuery(self, query):  # noqa: N802
        if query == Qt.ImEnabled:
            return self.tool == ToolKind.TEXT_EDIT and bool(self.selected_object_id)
        if query == Qt.ImCursorRectangle:
            obj = self._editing_text_object()
            if obj is not None:
                document, origin, transform = self._text_edit_layout(obj)
                caret = transform.mapRect(self._text_caret_rect(
                    document, self._text_cursor_position
                )).translated(origin)
                return self.camera_transform().mapRect(caret)
        return super().inputMethodQuery(query)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self.chapter is None:
            return
        if event.modifiers() & Qt.ControlModifier:
            anchor_doc = self.widget_to_document(event.position())
            factor = math.pow(1.0015, event.angleDelta().y())
            self.scale = max(0.05, min(8.0, self.scale * factor))
            mapped = self.document_to_widget(anchor_doc)
            delta = mapped - event.position()
            self.center_x += delta.x() / self.scale
            self.center_y += delta.y() / self.scale
        else:
            self.center_y -= event.angleDelta().y() / max(0.05, self.scale)
        self._snap_camera()
        self.update()
        self.cameraChanged.emit()
        event.accept()

    def tabletEvent(self, event) -> None:  # noqa: N802
        if self.chapter is None:
            event.accept()
            return
        nav = self._navigation_mode()
        if event.type() == QEvent.TabletPress:
            if nav:
                self._begin_navigation(nav, event.position())
            elif event.button() == Qt.LeftButton or event.pressure() > 0:
                self._tablet_tool_active = True
                self._tool_press(event.position(), event.pressure())
        elif event.type() == QEvent.TabletMove:
            if self._nav_mode:
                self._update_navigation(event.position())
            elif self._tablet_tool_active:
                self._tool_move(event.position(), event.pressure())
        elif event.type() == QEvent.TabletRelease:
            if self._nav_mode:
                self._end_navigation()
            elif self._tablet_tool_active:
                self._tablet_tool_active = False
                self._tool_release()
        event.accept()

    def event(self, event) -> bool:
        if self.settings.tablet_mode and event.type() in {
            QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd, QEvent.TouchCancel
        }:
            return self._touch_event(event)
        return super().event(event)

    @staticmethod
    def _is_touch_mouse(event: QMouseEvent) -> bool:
        device = event.pointingDevice()
        return bool(
            (device and device.type() == QInputDevice.DeviceType.TouchScreen)
            or event.source() != Qt.MouseEventSource.MouseEventNotSynthesized
        )

    def _begin_navigation(self, mode: str, point: QPointF) -> None:
        self._nav_mode = mode
        self._nav_anchor = point
        self._nav_anchor_center = QPointF(self.center_x, self.center_y)
        self._nav_anchor_scale = self.scale
        self._nav_anchor_rotation = self.rotation

    def _update_navigation(self, point: QPointF) -> None:
        delta = point - self._nav_anchor
        if self._nav_mode == "pan":
            angle = math.radians(-self.rotation)
            dx = (delta.x() * math.cos(angle) - delta.y() * math.sin(angle)) / self.scale
            dy = (delta.x() * math.sin(angle) + delta.y() * math.cos(angle)) / self.scale
            self.center_x = self._nav_anchor_center.x() - dx
            self.center_y = self._nav_anchor_center.y() - dy
        elif self._nav_mode == "zoom":
            self.scale = max(0.05, min(8.0, self._nav_anchor_scale * (1 + delta.x() * 0.005)))
        elif self._nav_mode == "rotate":
            center = QPointF(self.rect().center())
            start_angle = math.atan2(self._nav_anchor.y() - center.y(), self._nav_anchor.x() - center.x())
            current = math.atan2(point.y() - center.y(), point.x() - center.x())
            self.rotation = self._nav_anchor_rotation + math.degrees(current - start_angle)
        self._snap_camera()
        self.update()
        self.cameraChanged.emit()

    def _end_navigation(self) -> None:
        self._nav_mode = None
        self._snap_camera()
        self.interactionFinished.emit()

    def _touch_event(self, event) -> bool:
        points = [item.position() for item in event.points()]
        if event.type() == QEvent.TouchBegin:
            self._touch_points = points
            self._touch_anchor_center = QPointF(self.center_x, self.center_y)
            self._touch_anchor_scale = self.scale
            self._touch_anchor_rotation = self.rotation
            if len(points) >= 2:
                self._touch_anchor_distance = max(1.0, math.dist(
                    (points[0].x(), points[0].y()), (points[1].x(), points[1].y())
                ))
                self._touch_anchor_angle = math.atan2(
                    points[1].y() - points[0].y(), points[1].x() - points[0].x()
                )
            event.accept()
            return True
        if event.type() == QEvent.TouchUpdate and points and self._touch_points:
            old_center = sum((p.x() for p in self._touch_points), 0.0) / len(self._touch_points)
            old_y = sum((p.y() for p in self._touch_points), 0.0) / len(self._touch_points)
            center = sum((p.x() for p in points), 0.0) / len(points)
            center_y = sum((p.y() for p in points), 0.0) / len(points)
            # Tablet mode follows viewport-scroll semantics: moving a finger
            # right advances the camera right. Desktop Alt-drag remains
            # canvas-grab navigation and intentionally uses the opposite sign.
            self._apply_touch_pan_delta(center - old_center, center_y - old_y)
            if len(points) >= 2:
                distance = max(1.0, math.dist(
                    (points[0].x(), points[0].y()), (points[1].x(), points[1].y())
                ))
                angle = math.atan2(points[1].y() - points[0].y(), points[1].x() - points[0].x())
                self.scale = max(0.05, min(8.0, self.scale * distance / self._touch_anchor_distance))
                self.rotation += math.degrees(angle - self._touch_anchor_angle)
                self._touch_anchor_distance = distance
                self._touch_anchor_angle = angle
            self._touch_points = points
            self._snap_camera()
            self.update()
            self.cameraChanged.emit()
            event.accept()
            return True
        self._touch_points.clear()
        self.interactionFinished.emit()
        event.accept()
        return True

    def _apply_touch_pan_delta(self, delta_x: float, delta_y: float) -> None:
        """Apply tablet navigation deltas without changing desktop grab-pan."""
        self.center_x += delta_x / self.scale
        self.center_y -= delta_y / self.scale

    def _creation_selected_node(self) -> PathNode | None:
        return next((
            node for node in self._creation_nodes
            if node.node_id == self._creation_selected_node_id
        ), None)

    def _creation_hit_test(self, point: QPointF) -> dict | None:
        if not self._creation_nodes:
            return None
        if len(self._creation_nodes) == 1:
            node = self._creation_nodes[0]
            tolerance = 14 / max(self.scale, 0.05)
            for name, control in (
                ("incoming", node.incoming), ("outgoing", node.outgoing)
            ):
                if control is not None and math.dist(
                    (point.x(), point.y()), control
                ) <= tolerance:
                    return {
                        "kind": "control", "name": name,
                        "node_id": node.node_id,
                        "position": QPointF(*control),
                    }
            if math.dist(
                (point.x(), point.y()), node.position
            ) <= tolerance:
                return {
                    "kind": "node", "index": 0,
                    "node_id": node.node_id,
                    "position": QPointF(node.x, node.y),
                }
            return None
        geometry = BoundGeometry.path(self._creation_nodes, False)
        previous = self._selected_shape_node_id
        self._selected_shape_node_id = self._creation_selected_node_id
        try:
            return self._shape_hit_test(geometry, point)
        finally:
            self._selected_shape_node_id = previous

    def _update_creation_hover(self, point: QPointF) -> None:
        hit = self._creation_hit_test(point)
        self._shape_hover_target = hit
        self._shape_hover_insert = (
            hit["insert"] if hit and hit["kind"] == "insert" else None
        )
        labels = {
            "gizmo": "Edit selected point",
            "control": "Drag Bézier control",
            "node": "Click to select; drag to move point",
            "insert": "Click to insert a vector point; drag for Bézier",
        }
        self.setToolTip(labels.get(hit["kind"], "") if hit else "")
        self.update()

    def _begin_creation_shape_interaction(
        self, point: QPointF, widget_point: QPointF,
    ) -> bool:
        hit = self._creation_hit_test(point)
        if hit is None:
            return False
        self._shape_hover_target = hit
        kind = hit["kind"]
        if kind == "gizmo":
            node = self._creation_selected_node()
            if node is None:
                return False
            name = hit["name"]
            geometry = BoundGeometry.path(self._creation_nodes, False)
            if name == "type":
                self._toggle_shape_node_type(geometry, node)
            elif name == "lock":
                self._toggle_shape_node_lock(geometry, node)
            elif name == "cap":
                style = self._creation_style or ShapeStyle()
                values = ["point", "square", "round"]
                attribute = (
                    "start_cap"
                    if node is self._creation_nodes[0] else "end_cap"
                )
                current = getattr(style, attribute)
                setattr(
                    style, attribute,
                    values[(values.index(current) + 1) % len(values)],
                )
                self._creation_style = style
            else:
                self._creation_active_control = name
                self._creation_press_widget = QPointF(widget_point)
                self._drag_start_doc = QPointF(point)
            self.update()
            return True
        if kind == "control":
            self._creation_selected_node_id = hit["node_id"]
            self._creation_active_control = hit["name"]
            self._creation_press_widget = QPointF(widget_point)
            return True
        if kind == "node":
            self._creation_selected_node_id = hit["node_id"]
            self._creation_active_control = "draft_node"
            self._creation_press_widget = QPointF(widget_point)
            self._creation_node_dragged = False
            self._creation_close_candidate = (
                hit["node_id"] == self._creation_nodes[0].node_id
                and len(self._creation_nodes) >= 3
            )
            return True
        if kind == "insert":
            index, percent, _insert_point = hit["insert"]
            geometry = BoundGeometry.path(self._creation_nodes, False)
            node = self._split_shape_segment(geometry, index, percent)
            self._creation_nodes.insert(index + 1, node)
            self._creation_selected_node_id = node.node_id
            self._creation_active_control = "draft_insert"
            self._creation_press_widget = QPointF(widget_point)
            self._drag_start_doc = QPointF(point)
            self._creation_node_dragged = False
            self._shape_hover_insert = None
            self.update()
            return True
        return False

    def _update_creation_shape_interaction(
        self, point: QPointF, widget_point: QPointF,
    ) -> bool:
        control = self._creation_active_control
        node = self._creation_selected_node()
        if not control or node is None:
            return False
        moved = math.dist(
            (widget_point.x(), widget_point.y()),
            (
                self._creation_press_widget.x(),
                self._creation_press_widget.y(),
            ),
        ) > 3
        if moved:
            self._creation_node_dragged = True
            self._creation_close_candidate = False
        if control == "new_point":
            if moved:
                node.point_type = "bezier"
                node.handles_locked = True
                target = (point.x(), point.y())
                if node is self._creation_nodes[0]:
                    node.outgoing = target
                else:
                    node.incoming = target
            self.update()
            return True
        if control in {"draft_node", "draft_insert"}:
            if control == "draft_insert" and moved:
                node.point_type = "bezier"
                node.handles_locked = True
                node.incoming = (point.x(), point.y())
                node.outgoing = (
                    node.x * 2 - point.x(), node.y * 2 - point.y()
                )
            elif control == "draft_node" and moved:
                snapped = self._snap(
                    point, self._target_parent_for_new_layer()
                )
                dx, dy = snapped.x() - node.x, snapped.y() - node.y
                node.x, node.y = snapped.x(), snapped.y()
                if node.incoming:
                    node.incoming = (
                        node.incoming[0] + dx, node.incoming[1] + dy
                    )
                if node.outgoing:
                    node.outgoing = (
                        node.outgoing[0] + dx, node.outgoing[1] + dy
                    )
            self.update()
            return True
        if control in {"incoming", "outgoing"}:
            setattr(node, control, (point.x(), point.y()))
            if node.handles_locked:
                other = "outgoing" if control == "incoming" else "incoming"
                setattr(node, other, (
                    node.x * 2 - point.x(), node.y * 2 - point.y()
                ))
        elif control == "thickness":
            geometry = BoundGeometry.path(self._creation_nodes, False)
            position = self._shape_gizmo_positions(geometry, node)["thickness"]
            direction = position - QPointF(node.x, node.y)
            length = max(1e-6, math.hypot(direction.x(), direction.y()))
            direction /= length
            distance = QPointF.dotProduct(
                point - QPointF(node.x, node.y), direction
            )
            node.width_multiplier = round(max(
                0.1, min(10.0, (distance * self.scale - 24) / 10)
            ) * 10) / 10
        elif control == "roundness":
            node.roundness = max(
                0.0, math.dist(node.position, (point.x(), point.y()))
            )
        self.update()
        return True

    # ---- tool actions --------------------------------------------------
    def _tool_press(self, widget_point: QPointF, pressure: float) -> None:
        point = self.widget_to_document(widget_point)
        self._press_widget_point = QPointF(widget_point)
        self._press_document_point = QPointF(point)
        if self.tool in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}:
            obj = self.chapter.objects.get(self.selected_object_id)
            quad = (
                self.object_world_quad(obj.object_id)
                if isinstance(obj, RasterObject) else None
            )
            path = QPainterPath()
            if quad:
                path.addPolygon(QPolygonF([QPointF(*candidate) for candidate in quad]))
            if quad and path.contains(point):
                self._begin_stroke(point, pressure)
            else:
                self._request_object_selection(point, widget_point)
            return
        if self.tool == ToolKind.TEXT_EDIT and not isinstance(
            self.chapter.objects.get(self.selected_object_id), TextObject
        ):
            hits = self.hit_test_objects(point, text_only=True)
            if hits:
                self.set_selection("object", hits[0])
            return
        if (
            self.tool == ToolKind.SHAPE_EDIT
            and self.selected_kind == "layer"
            and self._begin_shape_edit(point)
        ):
            return
        self._outside_click_candidate = (
            False if self.tool in {
                ToolKind.BOX_BOUND, ToolKind.CIRCLE_BOUND,
                ToolKind.SHAPE_CREATE, ToolKind.RASTER_CREATE,
            }
            else self._is_clear_outside_active_layer(point)
        )
        if self._outside_click_candidate:
            return
        if self.tool == ToolKind.OBJECT_SELECT:
            self._request_object_selection(point, widget_point)
            return
        if self.tool == ToolKind.RASTER_CREATE:
            target = self._raster_creation_parent_id or self._target_parent_for_new_layer()
            snapped = self._snap(point, target)
            self._creation_points = [
                (snapped.x(), snapped.y()), (snapped.x(), snapped.y())
            ]
            return
        if self.tool == ToolKind.TEXT_EDIT and self.selected_object_id:
            if self._begin_text_pointer(point):
                return
        if self.tool == ToolKind.TRANSFORM and self.selected_object_id:
            obj = self.chapter.objects[self.selected_object_id]
            if isinstance(obj, TextObject) and obj.layout_mode != "free":
                return
            world_quad = self.object_world_quad(self.selected_object_id)
            if not world_quad:
                return
            layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
            local_quad = [(x - layer_x, y - layer_y) for x, y in world_quad]
            tolerance = 14 / max(self.scale, 0.05)
            handles = self._quad_handles(world_quad)
            distances = [
                math.dist((point.x(), point.y()), candidate)
                for candidate in handles
            ]
            if distances and min(distances) <= tolerance:
                self._transform_handle_index = distances.index(min(distances))
                self._transform_drag_mode = "handle"
            else:
                path = QPainterPath()
                path.addPolygon(QPolygonF([QPointF(*candidate) for candidate in world_quad]))
                if not path.contains(point):
                    return
                self._transform_handle_index = None
                self._transform_drag_mode = "translate"
            self._model_before = self.chapter.to_dict()
            self._drag_start_doc = point
            self._transform_start_quad = local_quad
            self._transform_preview_quad = list(local_quad)
            if isinstance(obj, RasterObject):
                self._build_raster_transform_cache()
            return
        if (
            self.tool == ToolKind.BOUND_EDIT
            and self.selected_object_id
            and isinstance(
                self.chapter.objects.get(self.selected_object_id), RasterObject
            )
        ):
            quad = self.object_world_quad(self.selected_object_id)
            handles = self._quad_handles(quad)
            tolerance = 14 / max(self.scale, 0.05)
            distances = [
                math.dist((point.x(), point.y()), candidate)
                for candidate in handles
            ]
            if distances and min(distances) <= tolerance:
                self._active_handle = distances.index(min(distances))
                self._model_before = self.chapter.to_dict()
                self._drag_start_doc = QPointF(point)
                self._drag_start_value = tuple(
                    self.chapter.objects[self.selected_object_id].interaction_rect
                )
            return
        if self.tool in {ToolKind.BOX_BOUND, ToolKind.CIRCLE_BOUND}:
            target = self._target_parent_for_new_layer()
            snapped = self._snap(point, target)
            self._creation_points = [(snapped.x(), snapped.y()), (snapped.x(), snapped.y())]
            return
        if self.tool == ToolKind.SHAPE_CREATE:
            if self._begin_creation_shape_interaction(point, widget_point):
                return
            target = self._target_parent_for_new_layer()
            snapped = self._snap(point, target)
            if self._creation_nodes:
                previous = self._creation_nodes[-1]
                if (
                    previous.point_type == "bezier"
                    and previous.incoming is not None
                    and previous.outgoing is None
                ):
                    previous.outgoing = (
                        previous.x * 2 - previous.incoming[0],
                        previous.y * 2 - previous.incoming[1],
                    )
            node = PathNode(x=snapped.x(), y=snapped.y())
            self._creation_nodes.append(node)
            self._creation_selected_node_id = node.node_id
            if self._creation_style is None:
                self._creation_style = ShapeStyle(
                    primary_color=self.settings.brush_color,
                    base_thickness=float(self.settings.pencil_size()),
                    outline_color="#111111",
                )
            self._creation_active_control = "new_point"
            self._creation_press_widget = QPointF(widget_point)
            self._creation_node_dragged = False
            self._creation_close_candidate = False
            self.update()

    def _tool_move(self, widget_point: QPointF, pressure: float) -> None:
        point = self.widget_to_document(widget_point)
        if self._outside_click_candidate:
            if math.dist(
                (widget_point.x(), widget_point.y()),
                (self._press_widget_point.x(), self._press_widget_point.y()),
            ) > 4:
                self._outside_click_candidate = False
            return
        if self._text_dragging:
            self._update_text_pointer(point)
            return
        if self._drawing:
            self._continue_stroke(point, pressure)
            return
        if self.tool == ToolKind.SHAPE_CREATE and self._creation_nodes:
            if self._creation_active_control:
                self._update_creation_shape_interaction(point, widget_point)
            else:
                self._update_creation_hover(point)
            return
        if (
            self.tool == ToolKind.TRANSFORM
            and self._model_before is not None
            and self._transform_start_quad is not None
        ):
            self._update_transform_preview(point)
            self.update()
            return
        if (
            self.tool == ToolKind.BOUND_EDIT
            and self.selected_kind == "layer"
            and self._active_shape_control is not None
        ):
            self._update_shape_edit(point)
            return
        if (
            self.tool == ToolKind.BOUND_EDIT
            and self._active_handle is not None
            and self.selected_object_id
            and isinstance(
                self.chapter.objects.get(self.selected_object_id), RasterObject
            )
        ):
            obj = self.chapter.objects[self.selected_object_id]
            layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
            local_point = QPointF(
                point.x() - layer_x - obj.x,
                point.y() - layer_y - obj.y,
            )
            if self.settings.snap_to_grid:
                snapped = self._snap(point, obj.parent_layer_id)
                local_point = QPointF(
                    snapped.x() - layer_x - obj.x,
                    snapped.y() - layer_y - obj.y,
                )
            rect = self._resize_axis_rect(
                QRectF(*self._drag_start_value), self._active_handle, local_point
            )
            content = self.tiles.content_bounds(obj.object_id)
            if content is not None:
                rect = rect.united(content)
            obj.interaction_rect = (
                rect.left(), rect.top(), max(1.0, rect.width()),
                max(1.0, rect.height()),
            )
            self.documentChanged.emit(QRectF())
            self.update()
            return
        if self.tool in {
            ToolKind.BOX_BOUND, ToolKind.CIRCLE_BOUND, ToolKind.RASTER_CREATE
        } and self._creation_points:
            target = (
                self._raster_creation_parent_id
                if self.tool == ToolKind.RASTER_CREATE
                else self._target_parent_for_new_layer()
            )
            snapped = self._snap(point, target)
            self._creation_points[-1] = (snapped.x(), snapped.y())
            self.update()
        if (
            self.tool == ToolKind.BOUND_EDIT
            and self.selected_kind == "layer"
            and self._active_shape_control is None
        ):
            self._update_shape_hover(point)

    def _tool_release(self) -> None:
        if self._outside_click_candidate:
            self._outside_click_candidate = False
            page_id = self.active_page_id
            if page_id and page_id in self.chapter.layers:
                self.set_selection("layer", page_id)
                self.set_tool(ToolKind.OBJECT_SELECT)
            self.interactionFinished.emit()
            return
        if self._drawing:
            self._end_stroke()
            return
        if self.tool == ToolKind.SHAPE_CREATE:
            close = (
                self._creation_close_candidate
                and not self._creation_node_dragged
            )
            self._creation_active_control = None
            self._creation_close_candidate = False
            self._creation_node_dragged = False
            if close:
                self._finish_shape(True)
            self.interactionFinished.emit()
            return
        if self._text_dragging:
            self._text_dragging = False
            self._strict_margin_edge = None
            self._strict_margin_start = None
            self.update()
            return
        if (
            self.tool == ToolKind.TRANSFORM
            and self._model_before is not None
            and self._transform_preview_quad is not None
            and self.selected_object_id
        ):
            self._commit_object_transform()
            self.interactionFinished.emit()
            return
        if self._model_before is not None:
            before, self._model_before = self._model_before, None
            self._active_handle = None
            self._active_shape_control = None
            self._bound_drag_mode = None
            self._bound_start_points = []
            after = self.chapter.to_dict()
            if before != after:
                self.push_model_change(before, after, "Edit geometry")
                self.hierarchyChanged.emit()
            self.interactionFinished.emit()
            return
        if self.tool in {
            ToolKind.BOX_BOUND, ToolKind.CIRCLE_BOUND, ToolKind.RASTER_CREATE
        } and len(self._creation_points) >= 2:
            first, second = self._creation_points[0], self._creation_points[-1]
            self._creation_points.clear()
            if math.dist(first, second) < 2:
                self.update()
                return
            if self.tool == ToolKind.RASTER_CREATE:
                self._create_raster_from_world_rect(first, second)
                return
            if self.tool == ToolKind.BOX_BOUND:
                left, right = sorted((first[0], second[0]))
                top, bottom = sorted((first[1], second[1]))
                bound = BoundGeometry.rectangle(left, top, right - left, bottom - top)
            else:
                bound = BoundGeometry.circle(first[0], first[1], math.dist(first, second))
            self._create_layer_from_world_bound(bound)

    @staticmethod
    def _resize_axis_rect(
        source: QRectF, handle: int, point: QPointF,
    ) -> QRectF:
        left, top, right, bottom = (
            source.left(), source.top(), source.right(), source.bottom()
        )
        if handle in (0, 3, 7):
            left = point.x()
        if handle in (1, 2, 5):
            right = point.x()
        if handle in (0, 1, 4):
            top = point.y()
        if handle in (2, 3, 6):
            bottom = point.y()
        return QRectF(
            QPointF(min(left, right), min(top, bottom)),
            QPointF(max(left, right), max(top, bottom)),
        )

    def begin_raster_creation(self, parent_id: str) -> bool:
        if (
            self.chapter is None or parent_id not in self.chapter.layers
            or self.chapter.layers[parent_id].is_page
            or self.chapter.layers[parent_id].layer_kind == "fill"
        ):
            return False
        self._raster_creation_parent_id = parent_id
        self._creation_points.clear()
        return self.set_tool(ToolKind.RASTER_CREATE)

    def _create_raster_from_world_rect(
        self, first: tuple[float, float], second: tuple[float, float],
    ) -> None:
        parent_id = self._raster_creation_parent_id
        self._raster_creation_parent_id = ""
        if parent_id not in self.chapter.layers:
            return
        world = QRectF(QPointF(*first), QPointF(*second)).normalized()
        if world.width() < 2 or world.height() < 2:
            self.update()
            return
        before = self.chapter.to_dict()
        layer_x, layer_y = self.chapter.layer_world_translation(parent_id)
        count = sum(
            isinstance(item, RasterObject)
            for item in self.chapter.objects.values()
        ) + 1
        obj = RasterObject(
            name=f"Raster {count}",
            x=world.left() - layer_x, y=world.top() - layer_y,
            interaction_rect=(0.0, 0.0, world.width(), world.height()),
        )
        self.chapter.add_object(parent_id, obj)
        after = self.chapter.to_dict()
        self.push_model_change(before, after, "Add raster object")
        self.set_selection("object", obj.object_id, activate_default_tool=False)
        self.set_tool(ToolKind.RASTER_PENCIL)
        self.hierarchyChanged.emit()
        self.documentChanged.emit(QRectF(world))
        self.interactionFinished.emit()
        self.update()

    def _is_clear_outside_active_layer(self, point: QPointF) -> bool:
        if (
            self.chapter is None or not self.active_layer_id
            or self.active_layer_id not in self.chapter.layers
        ):
            return False
        layer = self.chapter.layers[self.active_layer_id]
        world_x, world_y = self.chapter.layer_world_translation(layer.layer_id)
        local = QPointF(point.x() - world_x, point.y() - world_y)
        if layer.bound is None:
            return False
        path = self.layer_shape_path(layer)
        if path.contains(local):
            return False
        if layer.layer_kind == "open_shape":
            return True
        stroker = QPainterPathStroker()
        stroker.setWidth(24.0 / max(self.scale, 0.05))
        return not stroker.createStroke(path).contains(local)

    def _request_object_selection(
        self, point: QPointF, widget_point: QPointF,
    ) -> None:
        hits = self.hit_test_objects(point)
        if (
            len(hits) > 1
            and QGuiApplication.keyboardModifiers() & Qt.ControlModifier
        ):
            self.selectionCandidatesRequested.emit(
                hits, self.mapToGlobal(widget_point.toPoint())
            )
            return
        if hits:
            self.set_selection("object", hits[0], activate_default_tool=True)
            return
        page_id = self.active_page_id
        if page_id and page_id in self.chapter.layers:
            self.set_selection("layer", page_id, activate_default_tool=False)
            self.set_tool(ToolKind.OBJECT_SELECT)

    def _selected_shape_node(self, bound: BoundGeometry) -> PathNode | None:
        return next((
            node for node in bound.nodes
            if node.node_id == self._selected_shape_node_id
        ), None)

    def _begin_shape_edit(self, world_point: QPointF) -> bool:
        layer = self.chapter.layers[self.selected_id]
        if layer.layer_kind == "fill" or layer.bound is None:
            return False
        wx, wy = self.chapter.layer_world_translation(layer.layer_id)
        local = QPointF(world_point.x() - wx, world_point.y() - wy)
        bound = layer.bound
        hit = self._shape_hit_test(bound, local)
        self._shape_hover_target = hit
        self._shape_hover_insert = (
            hit["insert"] if hit and hit["kind"] == "insert" else None
        )
        if hit is None:
            return False
        kind = hit["kind"]
        if kind == "gizmo":
            selected = self._selected_shape_node(bound)
            if selected is None:
                return False
            name = hit["name"]
            if name == "type":
                before = self.chapter.to_dict()
                self._toggle_shape_node_type(bound, selected)
                self._push_immediate_shape_change(before, "Change point type")
            elif name == "lock":
                before = self.chapter.to_dict()
                self._toggle_shape_node_lock(bound, selected)
                self._push_immediate_shape_change(before, "Toggle Bézier lock")
            elif name == "cap":
                before = self.chapter.to_dict()
                self._cycle_shape_cap(layer, selected)
                self._push_immediate_shape_change(before, "Change line cap")
            else:
                self._model_before = self.chapter.to_dict()
                self._active_shape_control = name
                self._drag_start_doc = QPointF(world_point)
            return True
        if kind == "radius":
            index = hit["index"]
            self._selected_shape_node_id = bound.nodes[index].node_id
            self._model_before = self.chapter.to_dict()
            self._active_shape_control = f"primitive_roundness:{index}"
            self._drag_start_value = bound.to_dict()
            return True
        if kind == "control":
            self._selected_shape_node_id = hit["node_id"]
            self._model_before = self.chapter.to_dict()
            self._active_shape_control = hit["name"]
            return True
        if kind == "primitive_handle":
            index = hit["index"]
            if index < 4:
                self._selected_shape_node_id = bound.nodes[index].node_id
            self._model_before = self.chapter.to_dict()
            self._active_shape_control = f"primitive:{index}"
            self._drag_start_value = bound.to_dict()
            return True
        if kind == "rectangle_point":
            index = hit["index"]
            self._selected_shape_node_id = bound.nodes[index].node_id
            self._model_before = self.chapter.to_dict()
            self._active_shape_control = f"rectangle_point:{index}"
            self._drag_start_value = bound.to_dict()
            self._drag_start_doc = QPointF(world_point)
            return True
        if kind == "rectangle_edge":
            index = hit["index"] - 4
            self._model_before = self.chapter.to_dict()
            self._active_shape_control = f"rectangle_edge:{index}"
            self._drag_start_value = bound.to_dict()
            self._drag_start_doc = QPointF(world_point)
            return True
        if kind == "node":
            self._selected_shape_node_id = hit["node_id"]
            selected = self._selected_shape_node(bound)
            self._model_before = self.chapter.to_dict()
            self._active_shape_control = "node"
            self._drag_start_value = selected.to_dict()
            self.update()
            return True
        if kind == "insert":
            index, percent, insert_point = hit["insert"]
            if bound.primitive in {"rectangle", "ellipse"}:
                self._pending_primitive_insert = (
                    layer.layer_id, index, percent, QPointF(insert_point),
                    QPointF(world_point),
                )
                self.primitiveConversionRequested.emit(bound.primitive)
                return True
            self._insert_shape_node(
                index, percent, insert_point, world_point
            )
            return True
        if kind == "interior":
            self._model_before = self.chapter.to_dict()
            self._active_shape_control = "translate"
            self._drag_start_doc = QPointF(world_point)
            self._drag_start_value = bound.to_dict()
            return True
        return False

    @staticmethod
    def _split_shape_segment(
        bound: BoundGeometry, index: int, percent: float,
    ) -> PathNode:
        start = bound.nodes[index]
        end = bound.nodes[(index + 1) % len(bound.nodes)]
        percent = max(0.001, min(0.999, percent))
        if start.outgoing is None and end.incoming is None:
            x = start.x * (1 - percent) + end.x * percent
            y = start.y * (1 - percent) + end.y * percent
            return PathNode(x=x, y=y)

        def interpolate(
            first: tuple[float, float], second: tuple[float, float],
        ) -> tuple[float, float]:
            return (
                first[0] * (1 - percent) + second[0] * percent,
                first[1] * (1 - percent) + second[1] * percent,
            )

        p0 = start.position
        p1 = start.outgoing or start.position
        p2 = end.incoming or end.position
        p3 = end.position
        q0, q1, q2 = (
            interpolate(p0, p1),
            interpolate(p1, p2),
            interpolate(p2, p3),
        )
        r0, r1 = interpolate(q0, q1), interpolate(q1, q2)
        point = interpolate(r0, r1)
        start.outgoing = q0
        end.incoming = q2
        if start.incoming is not None:
            start.handles_locked = False
        if end.outgoing is not None:
            end.handles_locked = False
        return PathNode(
            x=point[0], y=point[1], point_type="bezier",
            incoming=r0, outgoing=r1, handles_locked=False,
        )

    def _insert_shape_node(
        self, index: int, percent: float, insert_point: QPointF,
        world_point: QPointF,
    ) -> None:
        layer = self.chapter.layers[self.selected_id]
        bound = layer.bound
        before = self.chapter.to_dict()
        bound.primitive = "custom"
        node = self._split_shape_segment(bound, index, percent)
        bound.nodes.insert(index + 1, node)
        self._selected_shape_node_id = node.node_id
        self._model_before = before
        self._active_shape_control = "insert"
        self._drag_start_doc = QPointF(world_point)
        self._shape_hover_insert = None
        self._shape_hover_target = None
        self.update()

    def resolve_primitive_conversion(self, accepted: bool) -> None:
        pending, self._pending_primitive_insert = (
            self._pending_primitive_insert, None
        )
        if pending is None or self.chapter is None:
            return
        layer_id, index, percent, insert_point, world_point = pending
        if not accepted or layer_id not in self.chapter.layers:
            self.update()
            return
        self.selected_kind, self.selected_id = "layer", layer_id
        self._insert_shape_node(
            index, percent, insert_point, world_point
        )

    def _update_shape_edit(self, world_point: QPointF) -> None:
        layer = self.chapter.layers[self.selected_id]
        bound = layer.bound
        wx, wy = self.chapter.layer_world_translation(layer.layer_id)
        local = QPointF(world_point.x() - wx, world_point.y() - wy)
        selected = self._selected_shape_node(bound)
        control = self._active_shape_control or ""
        if control.startswith("primitive:"):
            index = int(control.split(":", 1)[1])
            snapped = self._snap(world_point, layer.layer_id)
            original = BoundGeometry.from_dict(self._drag_start_value)
            effective_index = self._move_bound_handle(
                bound, index, QPointF(snapped.x() - wx, snapped.y() - wy),
                original,
            )
            if effective_index < 4:
                self._selected_shape_node_id = (
                    bound.nodes[effective_index].node_id
                )
        elif control.startswith("rectangle_point:"):
            index = int(control.split(":", 1)[1])
            snapped = self._snap(world_point, layer.layer_id)
            bound.nodes[index].position = (
                snapped.x() - wx, snapped.y() - wy
            )
        elif control.startswith("rectangle_edge:"):
            index = int(control.split(":", 1)[1])
            original = BoundGeometry.from_dict(self._drag_start_value)
            first_index, second_index = index, (index + 1) % 4
            delta = world_point - self._drag_start_doc
            original_midpoint = QPointF(
                (
                    original.nodes[first_index].x
                    + original.nodes[second_index].x
                ) / 2 + wx,
                (
                    original.nodes[first_index].y
                    + original.nodes[second_index].y
                ) / 2 + wy,
            )
            if self.settings.snap_to_grid:
                target = self._snap(
                    original_midpoint + delta, layer.layer_id
                )
                delta = target - original_midpoint
            for node_index in (first_index, second_index):
                source = original.nodes[node_index]
                bound.nodes[node_index].position = (
                    source.x + delta.x(), source.y + delta.y()
                )
        elif control.startswith("primitive_roundness:"):
            index = int(control.split(":", 1)[1])
            original = BoundGeometry.from_dict(self._drag_start_value)
            node = original.nodes[index]
            corner = QPointF(node.x, node.y)
            radius_position = self._rectangle_radius_positions(original)[index]
            direction = radius_position - corner
            length = max(1e-6, math.hypot(direction.x(), direction.y()))
            unit = QPointF(direction.x() / length, direction.y() / length)
            projected = max(
                0.0, QPointF.dotProduct(local - corner, unit)
            )
            previous = original.nodes[index - 1]
            following = original.nodes[(index + 1) % len(original.nodes)]
            maximum = min(
                math.dist(node.position, previous.position),
                math.dist(node.position, following.position),
            ) / 2
            bound.nodes[index].roundness = min(
                maximum,
                projected / math.sqrt(2),
            )
        elif control == "node" and selected is not None:
            snapped = self._snap(world_point, layer.layer_id)
            target = QPointF(snapped.x() - wx, snapped.y() - wy)
            dx, dy = target.x() - selected.x, target.y() - selected.y
            selected.position = (target.x(), target.y())
            if selected.incoming:
                selected.incoming = (
                    selected.incoming[0] + dx, selected.incoming[1] + dy
                )
            if selected.outgoing:
                selected.outgoing = (
                    selected.outgoing[0] + dx, selected.outgoing[1] + dy
                )
        elif control == "insert" and selected is not None:
            if math.dist(
                (world_point.x(), world_point.y()),
                (self._drag_start_doc.x(), self._drag_start_doc.y()),
            ) > 3 / max(self.scale, 0.05):
                selected.point_type = "bezier"
                selected.handles_locked = True
                selected.incoming = (local.x(), local.y())
                selected.outgoing = (
                    selected.x * 2 - local.x(), selected.y * 2 - local.y()
                )
        elif control in {"incoming", "outgoing"} and selected is not None:
            target = (local.x(), local.y())
            setattr(selected, control, target)
            if selected.handles_locked:
                other = "outgoing" if control == "incoming" else "incoming"
                setattr(selected, other, (
                    selected.x * 2 - target[0], selected.y * 2 - target[1]
                ))
        elif control == "thickness" and selected is not None:
            positions = self._shape_gizmo_positions(bound, selected)
            origin = QPointF(selected.x, selected.y)
            initial = positions["thickness"] - origin
            length = max(1e-6, math.hypot(initial.x(), initial.y()))
            direction = QPointF(initial.x() / length, initial.y() / length)
            distance = QPointF.dotProduct(local - origin, direction)
            selected.width_multiplier = round(max(
                0.1, min(10.0, (distance * self.scale - 24) / 10)
            ) * 10) / 10
        elif control == "roundness" and selected is not None:
            selected.roundness = max(
                0.0, math.dist(selected.position, (local.x(), local.y()))
            )
        elif control == "translate":
            original = BoundGeometry.from_dict(self._drag_start_value)
            dx = world_point.x() - self._drag_start_doc.x()
            dy = world_point.y() - self._drag_start_doc.y()
            if self.settings.snap_to_grid:
                anchor = QPointF(
                    wx + original.nodes[0].x + dx,
                    wy + original.nodes[0].y + dy,
                )
                snapped = self._snap(anchor, layer.layer_id)
                dx = snapped.x() - wx - original.nodes[0].x
                dy = snapped.y() - wy - original.nodes[0].y
            bound.nodes = [
                PathNode.from_dict(node.to_dict()) for node in original.nodes
            ]
            for node in bound.nodes:
                node.x += dx
                node.y += dy
                if node.incoming:
                    node.incoming = (
                        node.incoming[0] + dx, node.incoming[1] + dy
                    )
                if node.outgoing:
                    node.outgoing = (
                        node.outgoing[0] + dx, node.outgoing[1] + dy
                    )
        self.documentChanged.emit(QRectF())
        self.update()

    def _update_shape_hover(self, world_point: QPointF) -> None:
        layer = self.chapter.layers.get(self.selected_id)
        if layer is None or layer.bound is None:
            self._shape_hover_target = None
            self._shape_hover_insert = None
            self.setToolTip("")
            return
        wx, wy = self.chapter.layer_world_translation(layer.layer_id)
        local = QPointF(world_point.x() - wx, world_point.y() - wy)
        hit = self._shape_hit_test(layer.bound, local)
        self._shape_hover_target = hit
        self._shape_hover_insert = (
            hit["insert"] if hit and hit["kind"] == "insert" else None
        )
        labels = {
            "gizmo": "Edit selected point",
            "radius": "Drag to adjust this corner's roundness",
            "control": "Drag Bézier control",
            "node": "Click to select; drag to move point",
            "rectangle_point": "Drag to move this rectangle point",
            "rectangle_edge": "Drag to move both edge points",
            "primitive_handle": "Drag to resize primitive",
            "insert": "Click to insert a vector point; drag for Bézier",
            "interior": "Drag to move shape",
        }
        self.setToolTip(labels.get(hit["kind"], "") if hit else "")
        self.update()

    def _toggle_shape_node_type(
        self, bound: BoundGeometry, node: PathNode,
    ) -> None:
        bound.primitive = "custom"
        index = bound.nodes.index(node)
        if node.point_type == "bezier":
            node.point_type = "vector"
            node.incoming = node.outgoing = None
            return
        previous = bound.nodes[index - 1] if index else None
        following = (
            bound.nodes[(index + 1) % len(bound.nodes)]
            if bound.closed or index + 1 < len(bound.nodes) else None
        )
        node.point_type = "bezier"
        node.handles_locked = True
        if previous is not None:
            distance = math.dist(node.position, previous.position) / 3
            dx, dy = previous.x - node.x, previous.y - node.y
            length = max(1e-6, math.hypot(dx, dy))
            node.incoming = (
                node.x + dx / length * distance,
                node.y + dy / length * distance,
            )
        if following is not None:
            reference = node.incoming
            if reference is not None:
                node.outgoing = (
                    node.x * 2 - reference[0], node.y * 2 - reference[1]
                )
            else:
                distance = math.dist(node.position, following.position) / 3
                dx, dy = following.x - node.x, following.y - node.y
                length = max(1e-6, math.hypot(dx, dy))
                node.outgoing = (
                    node.x + dx / length * distance,
                    node.y + dy / length * distance,
                )

    @staticmethod
    def _toggle_shape_node_lock(
        bound: BoundGeometry, node: PathNode,
    ) -> None:
        node.handles_locked = not node.handles_locked
        if node.handles_locked:
            use_outgoing = node.outgoing is not None
            reference = node.outgoing if use_outgoing else node.incoming
            if reference is not None:
                opposite = (
                    node.x * 2 - reference[0], node.y * 2 - reference[1]
                )
                if use_outgoing:
                    node.incoming = opposite
                else:
                    node.outgoing = opposite

    @staticmethod
    def _cycle_shape_cap(layer: LayerNode, node: PathNode) -> None:
        values = ("point", "square", "round")
        if node is layer.bound.nodes[0]:
            current = layer.shape_style.start_cap
            layer.shape_style.start_cap = values[
                (values.index(current) + 1) % len(values)
            ]
        else:
            current = layer.shape_style.end_cap
            layer.shape_style.end_cap = values[
                (values.index(current) + 1) % len(values)
            ]

    def _push_immediate_shape_change(self, before: dict, label: str) -> None:
        after = self.chapter.to_dict()
        if before != after:
            self.push_model_change(before, after, label)
            self.documentChanged.emit(QRectF())
            self.hierarchyChanged.emit()
            self.update()

    def _finish_shape(self, closed: bool) -> None:
        minimum = 3 if closed else 2
        if len(self._creation_nodes) < minimum:
            return
        nodes = [PathNode.from_dict(node.to_dict()) for node in self._creation_nodes]
        if closed:
            first, last = nodes[0], nodes[-1]
            if first.point_type == "bezier" and first.incoming is None:
                reference = first.outgoing or nodes[1].position
                first.incoming = (
                    first.x * 2 - reference[0], first.y * 2 - reference[1]
                )
            if last.point_type == "bezier" and last.outgoing is None:
                reference = last.incoming or nodes[-2].position
                last.outgoing = (
                    last.x * 2 - reference[0], last.y * 2 - reference[1]
                )
        bound = BoundGeometry.path(nodes, closed)
        self._creation_nodes = []
        self._creation_points = []
        self._creation_selected_node_id = ""
        self._creation_active_control = None
        self._shape_hover_target = None
        self._shape_hover_insert = None
        style = self._creation_style or ShapeStyle(
            primary_color=self.settings.brush_color,
            base_thickness=float(self.settings.pencil_size()),
            outline_color="#111111",
        )
        self._creation_style = None
        self._create_layer_from_world_bound(
            bound, style=style,
        )

    def _create_layer_from_world_bound(
        self, bound: BoundGeometry, style: ShapeStyle | None = None,
    ) -> None:
        placement = self._target_placement_for_new_bound()
        if placement is None:
            return
        parent_id, insertion_index = placement
        before = self.chapter.to_dict()
        parent_x, parent_y = self.chapter.layer_world_translation(parent_id)
        local = BoundGeometry.from_dict(bound.to_dict())
        for node in local.nodes:
            node.x -= parent_x
            node.y -= parent_y
            if node.incoming:
                node.incoming = (
                    node.incoming[0] - parent_x,
                    node.incoming[1] - parent_y,
                )
            if node.outgoing:
                node.outgoing = (
                    node.outgoing[0] - parent_x,
                    node.outgoing[1] - parent_y,
                )
        numbered = []
        for candidate in self.chapter.layers.values():
            if candidate.name.startswith("Layer "):
                try:
                    numbered.append(int(candidate.name[6:]))
                except ValueError:
                    pass
        layer = self.chapter.add_layer(
            parent_id, f"Layer {max(numbered, default=0) + 1}", local,
            index=insertion_index,
            layer_kind="bounded" if local.closed else "open_shape",
            style=style,
        )
        after = self.chapter.to_dict()
        self.set_selection("layer", layer.layer_id)
        self.push_model_change(before, after, "Create bounded layer")
        self.hierarchyChanged.emit()
        self.documentChanged.emit(QRectF())
        self.update()

    def _begin_stroke(self, point: QPointF, pressure: float) -> None:
        if self.chapter is None or self.selected_kind != "object":
            return
        obj = self.chapter.objects.get(self.selected_id)
        if not isinstance(obj, RasterObject):
            return
        self._raster_transform_snapshots.pop(obj.object_id, None)
        layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
        local = QPointF(round(point.x() - layer_x - obj.x), round(point.y() - layer_y - obj.y))
        self._drawing = True
        self._last_draw_point = local
        self._last_pressure = pressure if pressure > 0.001 else 1.0
        self._stroke_before = {}
        self._predictive = None
        size, opacity = self._brush_values(self._last_pressure)
        if self.tool == ToolKind.RASTER_PENCIL:
            size *= self._preset.stroke_start_ratio
        dirty = self.tiles.paint_dab(
            obj.object_id, local, size, QColor(self.settings.brush_color), opacity,
            erase=self.tool == ToolKind.RASTER_ERASER,
            square=self.settings.eraser_square and self.tool == ToolKind.RASTER_ERASER,
            antialias=(
                self._preset.antialiasing
                if self.tool == ToolKind.RASTER_PENCIL else False
            ),
            before=self._stroke_before,
        )
        self._emit_raster_dirty(obj, dirty)

    def _continue_stroke(self, point: QPointF, pressure: float) -> None:
        obj = self.chapter.objects[self.selected_id]
        layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
        local = QPointF(round(point.x() - layer_x - obj.x), round(point.y() - layer_y - obj.y))
        actual_pressure = pressure if pressure > 0.001 else 1.0
        size_start, opacity_start = self._brush_values(self._last_pressure)
        size_end, opacity_end = self._brush_values(actual_pressure)
        size = (size_start + size_end) / 2
        dirty = self.tiles.paint_line(
            obj.object_id, self._last_draw_point, local, size,
            QColor(self.settings.brush_color), opacity_start, opacity_end,
            erase=self.tool == ToolKind.RASTER_ERASER,
            square=self.settings.eraser_square and self.tool == ToolKind.RASTER_ERASER,
            antialias=(
                self._preset.antialiasing
                if self.tool == ToolKind.RASTER_PENCIL else False
            ),
            density=(
                self._preset.density
                if self.tool == ToolKind.RASTER_PENCIL else 1.0
            ),
            before=self._stroke_before,
        )
        previous_local = QPointF(self._last_draw_point)
        self._last_draw_point = local
        self._last_pressure = actual_pressure
        if self.settings.predictive_ink:
            world_current = QPointF(
                local.x() + layer_x + obj.x, local.y() + layer_y + obj.y
            )
            delta = local - previous_local
            # Prediction is intentionally short and transient; it never enters
            # the tile store or undo history.
            world_predicted = QPointF(
                world_current.x() + delta.x() * 0.5,
                world_current.y() + delta.y() * 0.5,
            )
            self._predictive = (
                world_current, world_predicted, size,
                QColor(self.settings.brush_color),
            )
        self._emit_raster_dirty(obj, dirty)

    def _end_stroke(self) -> None:
        obj = self.chapter.objects[self.selected_id]
        if (
            self.tool == ToolKind.RASTER_PENCIL
            and self._preset.stroke_end_ratio < 0.999
        ):
            size, opacity = self._brush_values(self._last_pressure)
            dirty = self.tiles.paint_dab(
                obj.object_id, self._last_draw_point,
                size * self._preset.stroke_end_ratio,
                QColor(self.settings.brush_color), opacity,
                antialias=self._preset.antialiasing,
                before=self._stroke_before,
            )
            self._emit_raster_dirty(obj, dirty)
        keys = set(self._stroke_before)
        self.tiles.prune_empty(obj.object_id, keys)
        after = self.tiles.snapshot(obj.object_id, keys)
        command = TilePatchCommand(
            "Raster stroke", self.tiles, obj.object_id,
            self._stroke_before, after,
            lambda: (self.update(), self.documentChanged.emit(QRectF())),
        )
        self.command_stack.push(command, already_done=True)
        self._stroke_before = {}
        self._drawing = False
        self._predictive = None
        self.interactionFinished.emit()

    def _brush_values(self, pressure: float) -> tuple[float, float]:
        erasing = self.tool == ToolKind.RASTER_ERASER
        base = (
            self.settings.active_eraser_pixels()
            if erasing else self.settings.pencil_size()
        )
        if erasing:
            return float(base), 1.0
        size = float(base)
        opacity = 1.0
        if self._preset.pressure_size:
            size *= self._preset.size_curve.evaluate_fast(pressure)
        if self._preset.pressure_opacity:
            opacity = self._preset.opacity_curve.evaluate_fast(pressure)
        return max(0.5, size), opacity

    def refresh_brush_settings(self) -> None:
        self._preset = self.settings.active_brush_preset()
        self.update()

    def _emit_raster_dirty(self, obj: RasterObject, local: QRectF) -> None:
        frame = QRectF(*obj.interaction_rect).united(local)
        obj.interaction_rect = (
            frame.left(), frame.top(), max(1.0, frame.width()),
            max(1.0, frame.height()),
        )
        layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
        world = local.translated(layer_x + obj.x, layer_y + obj.y)
        bottom = math.ceil(world.bottom())
        if bottom > self.chapter.height:
            self.chapter.height = bottom + 1080
            self.hierarchyChanged.emit()
        self.documentChanged.emit(world)
        self.update()

    @staticmethod
    def _edge_midpoints(
        quad: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        return [
            (
                (quad[index][0] + quad[(index + 1) % 4][0]) / 2,
                (quad[index][1] + quad[(index + 1) % 4][1]) / 2,
            )
            for index in range(4)
        ]

    @classmethod
    def _quad_handles(
        cls, quad: list[tuple[float, float]],
    ) -> list[tuple[float, float]]:
        return list(quad) + cls._edge_midpoints(quad)

    @staticmethod
    def _quad_is_valid(quad: list[tuple[float, float]]) -> bool:
        if len(quad) != 4:
            return False
        cross_products: list[float] = []
        area = 0.0
        for index in range(4):
            current = quad[index]
            following = quad[(index + 1) % 4]
            third = quad[(index + 2) % 4]
            area += current[0] * following[1] - following[0] * current[1]
            cross_products.append(
                (following[0] - current[0]) * (third[1] - following[1])
                - (following[1] - current[1]) * (third[0] - following[0])
            )
        nonzero = [value for value in cross_products if abs(value) > 1e-4]
        return (
            abs(area) >= 8.0
            and len(nonzero) == 4
            and (all(value > 0 for value in nonzero) or all(value < 0 for value in nonzero))
        )

    def _snap_transform_point(
        self, point: tuple[float, float], layer_id: str,
    ) -> tuple[float, float]:
        if not self.settings.transform_snap_to_grid:
            return point
        layer_x, layer_y = self.chapter.layer_world_translation(layer_id)
        grid = self.chapter.effective_grid(layer_id)
        snapped_x, snapped_y = grid.snap(
            point[0] + layer_x, point[1] + layer_y
        )
        return snapped_x - layer_x, snapped_y - layer_y

    def _update_transform_preview(self, point: QPointF) -> None:
        obj = self.chapter.objects[self.selected_object_id]
        layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
        local_point = (point.x() - layer_x, point.y() - layer_y)
        start = list(self._transform_start_quad)
        dx = point.x() - self._drag_start_doc.x()
        dy = point.y() - self._drag_start_doc.y()
        if self._transform_drag_mode == "translate":
            candidate = [(x + dx, y + dy) for x, y in start]
            if self.settings.transform_snap_to_grid:
                center = (
                    sum(x for x, _ in candidate) / 4,
                    sum(y for _, y in candidate) / 4,
                )
                snapped = self._snap_transform_point(center, obj.parent_layer_id)
                correction = snapped[0] - center[0], snapped[1] - center[1]
                candidate = [
                    (x + correction[0], y + correction[1]) for x, y in candidate
                ]
        elif self.settings.transform_mode == "uniform":
            handle = self._transform_handle_index
            anchors = start + self._edge_midpoints(start)
            opposite = [2, 3, 0, 1, 6, 7, 4, 5][handle]
            origin = anchors[opposite]
            initial = anchors[handle]
            target = self._snap_transform_point(local_point, obj.parent_layer_id)
            initial_distance = math.dist(origin, initial)
            scale = math.dist(origin, target) / max(initial_distance, 1e-6)
            candidate = [
                (
                    origin[0] + (x - origin[0]) * scale,
                    origin[1] + (y - origin[1]) * scale,
                )
                for x, y in start
            ]
        else:
            handle = self._transform_handle_index
            target = self._snap_transform_point(local_point, obj.parent_layer_id)
            if handle < 4:
                candidate = list(start)
                candidate[handle] = target
            else:
                edge = handle - 4
                adjacent = (edge, (edge + 1) % 4)
                midpoint = self._edge_midpoints(start)[edge]
                edge_dx, edge_dy = target[0] - midpoint[0], target[1] - midpoint[1]
                candidate = list(start)
                for index in adjacent:
                    candidate[index] = (
                        start[index][0] + edge_dx,
                        start[index][1] + edge_dy,
                    )
        if self._quad_is_valid(candidate):
            self._transform_preview_quad = candidate

    def _commit_object_transform(self) -> None:
        object_id = self.selected_object_id
        obj = self.chapter.objects[object_id]
        before_model = self._model_before
        destination = list(self._transform_preview_quad)
        self._model_before = None
        self._transform_start_quad = None
        self._transform_preview_quad = None
        self._transform_handle_index = None
        self._transform_drag_mode = None
        self._transform_static_cache = QImage()
        if isinstance(obj, TextObject):
            obj.transform_quad = destination
            obj.x = obj.y = 0
            after = self.chapter.to_dict()
            if before_model != after:
                self.push_model_change(before_model, after, "Transform text")
                self.hierarchyChanged.emit()
            self.documentChanged.emit(QRectF())
            self.update()
            return
        if not isinstance(obj, RasterObject):
            return
        before_tiles = self.tiles.object_tiles(object_id)
        pre_transform_state = obj.to_dict()
        try:
            after_tiles = self.tiles.projective_transform(
                object_id, obj.x, obj.y, destination,
                QRectF(*obj.interaction_rect),
            )
        except ValueError:
            self.update()
            return
        self._raster_transform_snapshots[object_id] = (
            pre_transform_state, before_tiles
        )
        obj.x = obj.y = 0
        destination_bounds = QPolygonF([
            QPointF(*point) for point in destination
        ]).boundingRect()
        obj.interaction_rect = (
            destination_bounds.left(), destination_bounds.top(),
            max(1.0, destination_bounds.width()),
            max(1.0, destination_bounds.height()),
        )
        self.tiles.replace_object_tiles(object_id, after_tiles)
        after_model = self.chapter.to_dict()

        def apply(model: dict, values: dict) -> None:
            self.replace_chapter(model)
            self.tiles.replace_object_tiles(object_id, values)
            self.documentChanged.emit(QRectF())
            self.update()

        def redo_transform() -> None:
            self._raster_transform_snapshots[object_id] = (
                pre_transform_state, before_tiles
            )
            apply(after_model, after_tiles)

        def undo_transform() -> None:
            self._raster_transform_snapshots.pop(object_id, None)
            apply(before_model, before_tiles)

        self.command_stack.push(
            CallbackCommand(
                "Transform raster",
                redo_transform,
                undo_transform,
            ),
            already_done=True,
        )
        self.hierarchyChanged.emit()
        self.documentChanged.emit(QRectF())
        self.update()

    def _clear_transform_preview(self) -> None:
        self._model_before = None
        self._transform_start_quad = None
        self._transform_preview_quad = None
        self._transform_handle_index = None
        self._transform_drag_mode = None
        self._transform_static_cache = QImage()
        self._render_excluded_object_id = ""

    def _build_raster_transform_cache(self) -> None:
        if (
            self.chapter is None or not self.selected_object_id
            or self.width() <= 0 or self.height() <= 0
        ):
            return
        image = QImage(
            self.width(), self.height(), QImage.Format_ARGB32_Premultiplied
        )
        image.fill(QColor("#242428"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setTransform(self.camera_transform())
        painter.fillRect(
            QRectF(0, 0, self.chapter.width, self.chapter.height),
            QColor(self.chapter.background),
        )
        painter.save()
        painter.setClipRect(
            QRectF(0, 0, self.chapter.width, self.chapter.height)
        )
        self._render_excluded_object_id = self.selected_object_id
        visible = self.visible_document_rect()
        for page_id in reversed(self.chapter.root_page_ids):
            self._render_layer(
                painter, self.chapter.layers[page_id], 1.0, visible
            )
        self._render_excluded_object_id = ""
        self._draw_grid(painter, visible)
        painter.restore()
        painter.end()
        self._transform_static_cache = image

    def _render_selected_raster_preview(
        self, painter: QPainter, visible: QRectF,
    ) -> None:
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, RasterObject):
            return
        painter.save()
        opacity = 1.0
        for layer in self.chapter.ancestor_layers(obj.parent_layer_id):
            if not layer.visible or layer.opacity <= 0 or layer.bound is None:
                painter.restore()
                return
            painter.translate(layer.translate_x, layer.translate_y)
            painter.setClipPath(
                self.bound_path(layer.bound, layer.vertex_radius),
                Qt.IntersectClip,
            )
            opacity *= layer.opacity
        world_x, world_y = self.chapter.layer_world_translation(
            obj.parent_layer_id
        )
        self._render_object(
            painter, obj, opacity, visible.translated(-world_x, -world_y)
        )
        painter.restore()

    def can_undo_raster_transform(self) -> bool:
        return bool(
            self.selected_object_id
            and self.selected_object_id in self._raster_transform_snapshots
        )

    def undo_raster_transform(self) -> bool:
        object_id = self.selected_object_id
        snapshot = self._raster_transform_snapshots.pop(object_id, None)
        if snapshot is None or not isinstance(
            self.chapter.objects.get(object_id), RasterObject
        ):
            return False
        restore_state, restore_tiles = snapshot
        current_state = self.chapter.objects[object_id].to_dict()
        current_tiles = self.tiles.object_tiles(object_id)

        def apply(state: dict, values: dict) -> None:
            obj = self.chapter.objects.get(object_id)
            if not isinstance(obj, RasterObject):
                return
            position = state.get("position", [0, 0])
            obj.x, obj.y = float(position[0]), float(position[1])
            rect = state.get("interaction_rect", [0, 0, 120, 120])
            obj.interaction_rect = tuple(float(value) for value in rect)
            self.tiles.replace_object_tiles(object_id, values)
            self.documentChanged.emit(QRectF())
            self.update()

        apply(restore_state, restore_tiles)
        self.command_stack.push(
            CallbackCommand(
                "Undo raster transform",
                lambda: apply(restore_state, restore_tiles),
                lambda: apply(current_state, current_tiles),
            ),
            already_done=True,
        )
        self.interactionFinished.emit()
        return True

    def _editing_text_object(self) -> TextObject | None:
        if (
            self.chapter is None
            or self.tool != ToolKind.TEXT_EDIT
            or not self.selected_object_id
        ):
            return None
        obj = self.chapter.objects.get(self.selected_object_id)
        return obj if isinstance(obj, TextObject) else None

    def _text_edit_layout(
        self, obj: TextObject,
    ) -> tuple[QTextDocument, QPointF, QTransform]:
        layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
        if obj.layout_mode == "strict":
            rect = self._strict_text_rect(obj)
            document = self._text_document(obj, rect.width())
            offset = self._text_vertical_offset(obj, document, rect.height())
            return (
                document,
                QPointF(layer_x + rect.left(), layer_y + rect.top() + offset),
                QTransform(),
            )
        source = QRectF(0, 0, max(1.0, obj.width), max(1.0, obj.height))
        return (
            self._text_document(obj, source.width()),
            QPointF(layer_x, layer_y),
            self._quad_transform(source, self._text_quad(obj)),
        )

    def _text_local_point(
        self, obj: TextObject, world: QPointF,
    ) -> tuple[QPointF, QTextDocument] | None:
        document, origin, transform = self._text_edit_layout(obj)
        inverse, valid = transform.inverted()
        if not valid:
            return None
        return inverse.map(world - origin), document

    def _begin_text_pointer(self, point: QPointF) -> bool:
        obj = self._editing_text_object()
        if obj is None:
            return False
        if obj.layout_mode == "strict":
            world_rect = self.object_world_rect(obj.object_id)
            handles = self._edge_midpoints(self._rect_quad(world_rect))
            distances = [math.dist((point.x(), point.y()), item) for item in handles]
            if distances and min(distances) <= 12 / max(self.scale, 0.05):
                self._begin_text_session(obj)
                self._strict_margin_edge = distances.index(min(distances))
                self._strict_margin_start = obj.margin
                self._strict_margin_press = QPointF(point)
                self._text_dragging = True
                return True
        mapped = self._text_local_point(obj, point)
        if mapped is None:
            return False
        local, document = mapped
        object_path = QPainterPath()
        object_path.addPolygon(QPolygonF([
            QPointF(*candidate) for candidate in self.object_world_quad(obj.object_id)
        ]))
        if not object_path.contains(point):
            return False
        local.setX(max(0.0, min(local.x(), document.textWidth())))
        local.setY(max(0.0, min(local.y(), document.size().height())))
        self._begin_text_session(obj)
        position = document.documentLayout().hitTest(local, Qt.FuzzyHit)
        self._text_cursor_position = max(0, position)
        self._text_selection_anchor = self._text_cursor_position
        self._text_dragging = True
        self.setFocus(Qt.MouseFocusReason)
        self.update()
        return True

    def _update_text_pointer(self, point: QPointF) -> None:
        obj = self._editing_text_object()
        if obj is None:
            return
        if self._strict_margin_edge is not None:
            parent = self.chapter.layers[obj.parent_layer_id]
            layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
            left, top, width, height = parent.bound.bbox()
            local_x, local_y = point.x() - layer_x, point.y() - layer_y
            candidates = [
                local_y - top, left + width - local_x,
                top + height - local_y, local_x - left,
            ]
            obj.margin = max(
                0.0, min(candidates[self._strict_margin_edge], min(width, height) / 2 - 1)
            )
            self.documentChanged.emit(QRectF())
            self.update()
            return
        mapped = self._text_local_point(obj, point)
        if mapped is None:
            return
        local, document = mapped
        self._text_cursor_position = max(
            0, document.documentLayout().hitTest(local, Qt.FuzzyHit)
        )
        self.update()

    def _begin_text_session(self, obj: TextObject) -> None:
        if self._text_editing:
            return
        self._text_editing = True
        self.textEditingChanged.emit(True)
        self._text_before_state = self.chapter.to_dict()
        self._text_cursor_position = min(self._text_cursor_position, len(obj.text))
        self._text_selection_anchor = self._text_cursor_position
        self._text_local_history = []

    def _commit_text_edit(self) -> None:
        if not self._text_editing or self.chapter is None:
            return
        before = self._text_before_state
        after = self.chapter.to_dict()
        self._text_editing = False
        self.textEditingChanged.emit(False)
        self._text_before_state = None
        self._text_dragging = False
        self._strict_margin_edge = None
        self._strict_margin_start = None
        self._text_local_history = []
        if before is not None and before != after:
            self.push_model_change(before, after, "Edit text")
            self.hierarchyChanged.emit()
            self.documentChanged.emit(QRectF())
            self.interactionFinished.emit()
        self.update()

    def _text_selection_range(self) -> tuple[int, int]:
        return sorted((self._text_cursor_position, self._text_selection_anchor))

    def _remember_text_state(self, obj: TextObject) -> None:
        self._text_local_history.append((
            obj.text, self._text_cursor_position, self._text_selection_anchor
        ))
        if len(self._text_local_history) > 100:
            self._text_local_history.pop(0)

    def _replace_text_selection(self, value: str) -> None:
        obj = self._editing_text_object()
        if obj is None:
            return
        self._begin_text_session(obj)
        self._remember_text_state(obj)
        start, end = self._text_selection_range()
        obj.text = obj.text[:start] + value + obj.text[end:]
        self._text_cursor_position = start + len(value)
        self._text_selection_anchor = self._text_cursor_position
        self.documentChanged.emit(QRectF())
        self.update()

    def _handle_text_key(self, event) -> bool:
        obj = self._editing_text_object()
        if obj is None:
            return False
        modifiers = event.modifiers()
        control = bool(modifiers & Qt.ControlModifier)
        shift = bool(modifiers & Qt.ShiftModifier)
        if control and event.key() == Qt.Key_A:
            self._begin_text_session(obj)
            self._text_selection_anchor = 0
            self._text_cursor_position = len(obj.text)
            self.update()
            return True
        if control and event.key() in (Qt.Key_C, Qt.Key_X):
            start, end = self._text_selection_range()
            QGuiApplication.clipboard().setText(obj.text[start:end])
            if event.key() == Qt.Key_X and start != end:
                self._replace_text_selection("")
            return True
        if control and event.key() == Qt.Key_V:
            self._replace_text_selection(QGuiApplication.clipboard().text())
            return True
        if control and event.key() == Qt.Key_Z:
            if self._text_local_history:
                obj.text, self._text_cursor_position, self._text_selection_anchor = (
                    self._text_local_history.pop()
                )
                self.documentChanged.emit(QRectF())
                self.update()
            return True
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._replace_text_selection("\n")
            return True
        if event.key() in (Qt.Key_Backspace, Qt.Key_Delete):
            start, end = self._text_selection_range()
            if start == end:
                if event.key() == Qt.Key_Backspace and start > 0:
                    self._text_selection_anchor = start - 1
                elif event.key() == Qt.Key_Delete and end < len(obj.text):
                    self._text_cursor_position = end + 1
            if self._text_cursor_position != self._text_selection_anchor:
                self._replace_text_selection("")
            return True
        moves = {
            Qt.Key_Left: QTextCursor.Left, Qt.Key_Right: QTextCursor.Right,
            Qt.Key_Up: QTextCursor.Up, Qt.Key_Down: QTextCursor.Down,
            Qt.Key_Home: QTextCursor.StartOfLine, Qt.Key_End: QTextCursor.EndOfLine,
        }
        if event.key() in moves:
            document = self._text_document(obj, (
                self._strict_text_rect(obj).width()
                if obj.layout_mode == "strict" else obj.width
            ))
            cursor = QTextCursor(document)
            cursor.setPosition(self._text_cursor_position)
            cursor.movePosition(moves[event.key()])
            self._text_cursor_position = cursor.position()
            if not shift:
                self._text_selection_anchor = self._text_cursor_position
            self.update()
            return True
        if event.key() == Qt.Key_Escape:
            self._commit_text_edit()
            self.set_tool(ToolKind.OBJECT_SELECT)
            return True
        text = event.text()
        if text and not control and text >= " ":
            self._replace_text_selection(text)
            return True
        return False

    @staticmethod
    def _bound_handles(bound: BoundGeometry) -> list[tuple[float, float]]:
        if bound.primitive in {"rectangle", "ellipse"}:
            left, top, width, height = bound.bbox()
            right, bottom = left + width, top + height
            center_x, center_y = (left + right) / 2, (top + bottom) / 2
            return [
                (left, top), (right, top), (right, bottom), (left, bottom),
                (center_x, top), (right, center_y),
                (center_x, bottom), (left, center_y),
            ]
        return [node.position for node in bound.nodes]

    @staticmethod
    def _move_bound_handle(
        bound: BoundGeometry, index: int, point: QPointF,
        original: BoundGeometry | None = None,
    ) -> int:
        if bound.primitive == "custom":
            bound.nodes[index].position = (point.x(), point.y())
            return index
        source = original or bound
        left, top, width, height = source.bbox()
        right, bottom = left + width, top + height
        x, y = point.x(), point.y()
        if index in (0, 3, 7):
            left = x
        if index in (1, 2, 5):
            right = x
        if index in (0, 1, 4):
            top = y
        if index in (2, 3, 6):
            bottom = y
        crossed_x = left > right
        crossed_y = top > bottom
        left, right = sorted((left, right))
        top, bottom = sorted((top, bottom))
        effective_index = index
        if crossed_x:
            effective_index = {0: 1, 1: 0, 2: 3, 3: 2,
                               5: 7, 7: 5}.get(effective_index, effective_index)
        if crossed_y:
            effective_index = {0: 3, 3: 0, 1: 2, 2: 1,
                               4: 6, 6: 4}.get(effective_index, effective_index)
        if bound.primitive == "ellipse":
            generated = BoundGeometry._ellipse_nodes(
                left, top, right - left, bottom - top
            )
            for current, replacement in zip(bound.nodes, generated):
                current.x, current.y = replacement.x, replacement.y
                current.incoming = replacement.incoming
                current.outgoing = replacement.outgoing
                current.point_type = replacement.point_type
                current.handles_locked = replacement.handles_locked
            return effective_index
        old_left, old_top, old_width, old_height = source.bbox()
        new_width, new_height = right - left, bottom - top
        for node, source_node in zip(bound.nodes, source.nodes):
            nx = (source_node.x - old_left) / max(old_width, 1e-6)
            ny = (source_node.y - old_top) / max(old_height, 1e-6)
            node.x, node.y = (
                left + nx * new_width, top + ny * new_height
            )
            node.roundness = min(
                source_node.roundness, min(new_width, new_height) / 2
            )
        return effective_index


class RasterCanvasWidget(_CanvasLogic, QWidget):
    """Universal software fallback."""


class GpuCanvasWidget(_CanvasLogic, QOpenGLWidget):
    """OpenGL-backed presentation using the same sparse document renderer."""

    def __init__(self, settings: EditorSettings, parent=None):
        super().__init__(settings, parent)
        self.setUpdateBehavior(QOpenGLWidget.PartialUpdate)


_GPU_AVAILABLE: bool | None = None


def gpu_available() -> bool:
    global _GPU_AVAILABLE
    if _GPU_AVAILABLE is not None:
        return _GPU_AVAILABLE
    if QApplication.instance() is None:
        _GPU_AVAILABLE = False
        return False
    import os
    if os.environ.get("QT_QPA_PLATFORM", "").lower() in {"offscreen", "minimal"}:
        _GPU_AVAILABLE = False
        return False
    surface = QOffscreenSurface()
    surface.setFormat(QSurfaceFormat.defaultFormat())
    surface.create()
    context = QOpenGLContext()
    context.setFormat(surface.requestedFormat())
    created = context.create()
    current = bool(created and surface.isValid() and context.makeCurrent(surface))
    if current:
        fmt = context.format()
        current = (fmt.majorVersion(), fmt.minorVersion()) >= (3, 3)
        context.doneCurrent()
    surface.destroy()
    _GPU_AVAILABLE = current
    return current


def create_canvas(settings: EditorSettings, parent=None):
    requested = settings.canvas_renderer
    use_gpu = requested != "raster" and gpu_available()
    return (GpuCanvasWidget if use_gpu else RasterCanvasWidget)(settings, parent)


# Stable public name for callers/tests that explicitly want the raster widget.
CanvasWidget = RasterCanvasWidget
