"""Tiled vertical document viewport, renderer, and drawing tools."""
from __future__ import annotations

import math
import time
from enum import Enum

import numpy as np

from PySide6.QtCore import (
    QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QBrush, QColor, QFont, QFontMetricsF,
    QGuiApplication,
    QImage, QInputDevice, QInputMethodEvent,
    QLinearGradient,
    QMouseEvent, QOffscreenSurface, QOpenGLContext, QPainter, QPainterPath,
    QPainterPathStroker, QPalette,
    QPen, QPolygonF, QRadialGradient, QSurfaceFormat, QTextBlockFormat,
    QTextCursor, QTextDocument, QTransform,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication, QWidget

from comic_editor.core.commands import (
    CallbackCommand, CommandStack, ObjectPatchCommand, TilePatchCommand,
)
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ChildRef, ColorFillGradientObject,
    ColorGradientRamp, ColorGradientStop, DocumentObject, GradientObject,
    LineGradientField, LayerNode, RadialGradientField,
    PathContour, PathNode, RasterObject, ShapeStyle, TextObject,
    VectorDrawingObject, VectorFillObject, VectorStroke, VectorStrokePoint,
    canonical_argb, object_from_dict,
)
from comic_editor.core.pressure import BrushPreset
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.vector_geometry import (
    Cubic, CubicSpan, FreehandSample, centerline_hit, connect_cubic_paths,
    corridor_contains, corridor_hits_path, corridor_path_intervals,
    cubic_derivative, cubic_eval, cubic_subsegment,
    distance_to_polyline, erase_stroke_by_corridor, find_face_containing,
    fit_freehand, flatten_stroke, interpolate_stroke_attribute,
    nearest_on_path,
    nearest_on_stroke, path_self_intersections, point_in_polygon,
    path_intersections, simplify_cubic_segments,
    stroke_cubics, tangent_bridge, trace_cubic_faces,
)


class ToolKind(Enum):
    OBJECT_SELECT = "object_select"
    RASTER_PENCIL = "raster_pencil"
    RASTER_ERASER = "raster_eraser"
    FILL = "fill"
    TEXT_EDIT = "text_edit"
    TRANSFORM = "transform"
    SHAPE_EDIT = "shape_edit"
    BOUND_EDIT = "shape_edit"
    VECTOR_EDIT = "vector_edit"
    VECTOR_REDRAW = "vector_redraw"
    VECTOR_CONNECT = "vector_connect"
    VECTOR_SIMPLIFY = "vector_simplify"
    DRAW_SELECT_RECT = "draw_select_rect"
    DRAW_SELECT_LASSO = "draw_select_lasso"
    DRAW_SELECT_STROKE = "draw_select_stroke"
    INSERT_PAGE_GAP = "insert_page_gap"
    BOX_BOUND = "box_bound"
    CIRCLE_BOUND = "circle_bound"
    SHAPE_CREATE = "shape_create"
    POLYGON_BOUND = "shape_create"
    RASTER_CREATE = "raster_create"


RASTER_FRAME_MARGIN = 24.0
SHAPE_CONTROL_SCALE = 1.5


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
    vectorSelectionChanged = Signal(object, object)
    pageCreationFinished = Signal(object, object, str)
    pageCreationInvalid = Signal(str)
    pageGapConfirmationChanged = Signal(bool)

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
        self._stroke_frame_before: tuple[float, float, float, float] | None = None
        self._stroke_erasing = False
        self._pending_raster_transform_press: tuple[
            QPointF, QPointF
        ] | None = None
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
        self._transform_pivot: QPointF | None = None
        self._transform_pivot_custom = False
        self._transform_rotate_start = 0.0
        self._render_excluded_object_id = ""
        self._rendering_compound_references = False
        self._rendering_outward_gradient = False
        self._live_underlay_object_id = ""
        self._live_underlay_amount = 0.0
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
        self._raster_creation_index: int | None = None
        self._gradient_creation_parent_id = ""
        self._gradient_creation_type = ""
        self._gradient_creation_before: dict | None = None
        self._selected_shape_node_id = ""
        self._selected_shape_node_ids: set[str] = set()
        self._shape_drag_nodes: dict[str, dict] = {}
        self._active_shape_control: str | None = None
        self._active_gradient_control: tuple[str, str] | None = None
        self._geometry_transform_target: tuple[str, str] | None = None
        self._shape_control_dragged = False
        self._shape_hover_insert: tuple[int, float, QPointF] | None = None
        self._shape_hover_target: dict | None = None
        self._pending_primitive_insert: (
            tuple[str, int, float, QPointF, QPointF] | None
        ) = None
        self._tablet_tool_active = False
        self._last_gradient_tablet_tap: tuple[float, QPointF] | None = None
        self._tablet_hover_widget: QPointF | None = None
        self._pointer_hover_widget: QPointF | None = None
        self._touch_points: list[QPointF] = []
        self._touch_anchor_points: list[QPointF] = []
        self._touch_anchor_center = QPointF()
        self._touch_anchor_distance = 1.0
        self._touch_anchor_angle = 0.0
        self._touch_anchor_scale = 1.0
        self._touch_anchor_rotation = 0.0
        self._touch_anchor_document = QPointF()
        self._touch_pending_points: list[QPointF] | None = None
        self._touch_frame_timer = QTimer(self)
        self._touch_frame_timer.setSingleShot(True)
        self._touch_frame_timer.timeout.connect(
            self._flush_touch_navigation
        )
        self._navigation_snapshot = QImage()
        self._navigation_snapshot_transform = QTransform()
        self._navigation_snapshot_active = False
        self._pending_raster_press: tuple[QPointF, QPointF, float] | None = None
        self._pending_vector_press: tuple[QPointF, QPointF, float] | None = None
        self._pending_drawing_selection_press: (
            tuple[QPointF, QPointF, float] | None
        ) = None
        self._preset = settings.active_brush_preset()
        self.primary_color = canonical_argb(settings.brush_color)
        self.secondary_color = "#FFFFFFFF"
        self._predictive: tuple[QPointF, QPointF, float, QColor] | None = None
        self._compound_path_cache: dict[str, QPainterPath] = {}
        # Stroke images are deliberately cached independently.  A drawing can
        # contain thousands of strokes, so editing one must not evict every
        # unrelated image.  The tuple key is intentionally permissive because
        # preview revisions use a transient token.
        self._vector_render_cache: dict[
            tuple, tuple[QImage, QRectF]
        ] = {}
        self._gradient_geometry_cache: dict[tuple, object] = {}
        self._gradient_scalar_cache: dict[tuple, tuple[np.ndarray, np.ndarray, QRectF]] = {}
        self._gradient_render_cache: dict[
            tuple, tuple[QImage, QRectF]
        ] = {}
        self._gradient_preview_active = False
        self._selected_vector_stroke_ids: set[str] = set()
        self._selected_vector_point_ids: set[str] = set()
        self._vector_gesture_mode: str | None = None
        self._vector_samples: list[FreehandSample] = []
        self._vector_sweep: list[FreehandSample] = []
        self._vector_simplify_point_ids: set[str] = set()
        self._vector_simplify_anchor_grid: dict[
            tuple[int, int], list[tuple[str, str, tuple[float, float]]]
        ] = {}
        self._vector_simplify_grid_size = 12.0
        self._vector_simplify_last_sample: tuple[float, float] | None = None
        self._vector_simplify_overlay: list[FreehandSample] = []
        self._vector_before: dict[str, dict | None] | None = None
        self._vector_drag_origin = QPointF()
        self._vector_drag_points: dict[
            str, tuple[tuple[float, float], tuple[float, float] | None,
                       tuple[float, float] | None]
        ] = {}
        self._vector_connect_endpoints: list[tuple[str, str]] = []
        self._hover_vector_stroke_id = ""
        self._drawing_selection_path = QPainterPath()
        self._drawing_selection_gesture: list[QPointF] = []
        self._drawing_selection_operation = "replace"
        self._selection_transform_quad: list[tuple[float, float]] | None = None
        self._selection_transform_start_quad: (
            list[tuple[float, float]] | None
        ) = None
        self._selection_pivot: QPointF | None = None
        self._selection_pivot_custom = False
        self._selection_transform_mode: str | None = None
        self._selection_transform_handle: int | None = None
        self._selection_transform_start = QPointF()
        self._selection_rotate_start = 0.0
        self._selection_rotate_quad: list[tuple[float, float]] | None = None
        self._selection_vector_points: dict[str, dict] = {}
        self._selection_vector_preview: dict[str, dict] = {}
        self._selection_vector_preview_revision = 0
        self._selection_before_tiles: dict[tuple[int, int], QImage] | None = None
        self._selection_before_model: dict | None = None
        self._page_creation_anchor_id = ""
        self._page_creation_before: dict | None = None
        self._page_creation_kind = ""
        self._page_creation_draft: BoundGeometry | None = None
        self._page_creation_committing = False
        self._page_creation_gap_bounds: tuple[float, float] | None = None
        self._page_creation_base_height = 0
        self._gradient_creation_parent_id = ""
        self._gradient_creation_type = ""
        self._gradient_creation_before = None
        self._page_gap_prompt_y: float | None = None
        self._page_gap_state: dict | None = None
        self._page_gap_transaction: dict | None = None
        self._page_gap_hover: dict | None = None
        self._page_gap_drag_mode: str | None = None
        self._page_gap_drag_before: dict | None = None
        self._page_gap_drag_start_y = 0.0
        self._page_gap_drag_start_top = 0.0
        self._page_gap_drag_start_bottom = 0.0
        self._page_gap_drag_translations: dict[str, float] = {}
        self.documentChanged.connect(self._clear_compound_path_cache)
        self.hierarchyChanged.connect(self._clear_compound_path_cache)
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
        self._compound_path_cache.clear()
        self._gradient_geometry_cache.clear()
        self._gradient_scalar_cache.clear()
        self._gradient_render_cache.clear()
        self._ensure_raster_frames()
        self.command_stack.clear()
        self.selected_kind = ""
        self.selected_id = ""
        self.active_page_id = ""
        self.active_layer_id = ""
        self.selected_object_id = ""
        self._selected_vector_stroke_ids.clear()
        self._selected_vector_point_ids.clear()
        self._vector_render_cache.clear()
        self._pending_raster_transform_press = None
        self._gradient_render_cache.clear()
        self._gradient_preview_active = False
        self._touch_frame_timer.stop()
        self._touch_pending_points = None
        self._clear_navigation_snapshot()
        self._cancel_vector_gesture(restore=False)
        self._clear_transform_preview()
        self._page_creation_anchor_id = ""
        self._page_creation_before = None
        self._page_creation_kind = ""
        self._page_creation_draft = None
        self._page_creation_committing = False
        self._page_creation_gap_bounds = None
        self._page_creation_base_height = 0
        self._page_gap_transaction = None
        self._clear_page_gap_editor()
        if reset_view:
            self.reset_view()
        self.update()
        self.hierarchyChanged.emit()

    def set_active_colors(self, primary: str, secondary: str) -> None:
        """Set the per-series colors used by contextual drawing tools."""
        self.primary_color = canonical_argb(primary)
        self.secondary_color = canonical_argb(secondary, "#FFFFFFFF")
        self.settings.brush_color = self.primary_color
        self.update()

    def replace_chapter(self, state: dict) -> None:
        self._commit_text_edit()
        self._clear_transform_preview()
        self._page_gap_transaction = None
        self._clear_page_gap_editor()
        self.pageGapConfirmationChanged.emit(False)
        self.chapter = ChapterDocument.from_dict(state)
        self._compound_path_cache.clear()
        self._gradient_geometry_cache.clear()
        self._gradient_scalar_cache.clear()
        self._gradient_render_cache.clear()
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

    def _active_vector_drawing(self) -> VectorDrawingObject | None:
        if self.chapter is None or self.selected_kind != "object":
            return None
        selected = self.chapter.objects.get(self.selected_id)
        if isinstance(selected, VectorDrawingObject):
            return selected
        if isinstance(selected, VectorFillObject):
            owner = self.chapter.objects.get(selected.owner_drawing_id)
            return owner if isinstance(owner, VectorDrawingObject) else None
        return None

    def _vector_local_point(
        self, drawing: VectorDrawingObject, world: QPointF,
    ) -> QPointF:
        layer_x, layer_y = self.chapter.layer_world_translation(
            drawing.parent_layer_id
        )
        return QPointF(
            world.x() - layer_x - drawing.x,
            world.y() - layer_y - drawing.y,
        )

    def _capture_vector_graph(
        self, drawing: VectorDrawingObject,
    ) -> dict[str, dict | None]:
        identifiers = [drawing.object_id, *drawing.fill_child_ids]
        return {
            object_id: (
                self.chapter.objects[object_id].to_dict()
                if object_id in self.chapter.objects else None
            )
            for object_id in identifiers
        }

    def _vector_changed(
        self, hierarchy: bool = False,
        changed_stroke_ids: set[str] | None = None,
    ) -> None:
        """Notify vector consumers and invalidate only affected stroke images.

        ``changed_stroke_ids`` is supplied by live editing paths.  A missing
        value means a structural restore (undo/redo, deletion, or hierarchy
        change), for which clearing the drawing cache is the safe fallback.
        This keeps the old behaviour for model restores while making pointer
        updates cheap for large drawings.
        """
        if changed_stroke_ids is None:
            self._vector_render_cache.clear()
        elif changed_stroke_ids:
            self._vector_render_cache = {
                key: value for key, value in self._vector_render_cache.items()
                if len(key) < 2 or key[1] not in changed_stroke_ids
            }
        drawing = self._active_vector_drawing()
        if drawing is not None:
            live_strokes = {stroke.stroke_id for stroke in drawing.strokes}
            live_points = {
                point.point_id
                for stroke in drawing.strokes
                for point in stroke.points
            }
            self._selected_vector_stroke_ids &= live_strokes
            if self.tool == ToolKind.DRAW_SELECT_STROKE:
                self._selected_vector_point_ids = self._point_ids_for_strokes(
                    drawing, self._selected_vector_stroke_ids
                )
            else:
                self._selected_vector_point_ids &= live_points
        else:
            self._selected_vector_stroke_ids.clear()
            self._selected_vector_point_ids.clear()
        self.vectorSelectionChanged.emit(
            set(self._selected_vector_stroke_ids),
            set(self._selected_vector_point_ids),
        )
        self.documentChanged.emit(QRectF())
        if hierarchy:
            self.hierarchyChanged.emit()
        self.update()

    def _push_vector_change(
        self, before: dict[str, dict | None], label: str,
        *, hierarchy: bool = False,
    ) -> bool:
        drawing_ids = {
            object_id for object_id, payload in before.items()
            if payload and payload.get("type") == "vector_drawing"
        }
        active = self._active_vector_drawing()
        if active is not None:
            drawing_ids.add(active.object_id)
        identifiers = set(before)
        for drawing_id in drawing_ids:
            drawing = self.chapter.objects.get(drawing_id)
            if isinstance(drawing, VectorDrawingObject):
                identifiers.update((drawing_id, *drawing.fill_child_ids))
        before = {
            object_id: before.get(object_id)
            for object_id in identifiers
        }
        after = {
            object_id: (
                self.chapter.objects[object_id].to_dict()
                if object_id in self.chapter.objects else None
            )
            for object_id in identifiers
        }
        if before == after:
            return False
        self.command_stack.push(
            CallbackCommand(
                label,
                lambda: (
                    self._restore_vector_payloads(after),
                    self._vector_changed(hierarchy),
                ),
                lambda: (
                    self._restore_vector_payloads(before),
                    self._vector_changed(hierarchy),
                ),
            ),
            already_done=True,
        )
        self._vector_changed(hierarchy)
        return True

    def _restore_vector_payloads(
        self, payloads: dict[str, dict | None],
    ) -> None:
        for object_id, payload in payloads.items():
            if payload is None:
                self.chapter.objects.pop(object_id, None)
                continue
            replacement = object_from_dict(payload)
            current = self.chapter.objects.get(object_id)
            if (
                isinstance(current, VectorDrawingObject)
                and isinstance(replacement, VectorDrawingObject)
            ):
                existing_strokes = {
                    stroke.stroke_id: stroke for stroke in current.strokes
                }
                restored_strokes: list[VectorStroke] = []
                for replacement_stroke in replacement.strokes:
                    stroke = existing_strokes.get(
                        replacement_stroke.stroke_id
                    )
                    if stroke is None:
                        restored_strokes.append(replacement_stroke)
                        continue
                    existing_points = {
                        point.point_id: point for point in stroke.points
                    }
                    restored_points: list[VectorStrokePoint] = []
                    for replacement_point in replacement_stroke.points:
                        point = existing_points.get(
                            replacement_point.point_id
                        )
                        if point is None:
                            restored_points.append(replacement_point)
                        else:
                            point.__dict__.clear()
                            point.__dict__.update(
                                replacement_point.__dict__
                            )
                            restored_points.append(point)
                    values = dict(replacement_stroke.__dict__)
                    values["points"] = restored_points
                    stroke.__dict__.clear()
                    stroke.__dict__.update(values)
                    restored_strokes.append(stroke)
                values = dict(replacement.__dict__)
                values["strokes"] = restored_strokes
                current.__dict__.clear()
                current.__dict__.update(values)
            elif current is not None and type(current) is type(replacement):
                current.__dict__.clear()
                current.__dict__.update(replacement.__dict__)
            else:
                self.chapter.objects[object_id] = replacement

    def _cancel_vector_gesture(self, *, restore: bool = True) -> None:
        if restore and self._vector_before and self.chapter is not None:
            self._restore_vector_payloads(self._vector_before)
        self._vector_gesture_mode = None
        self._vector_samples.clear()
        self._vector_sweep.clear()
        self._vector_simplify_point_ids.clear()
        self._vector_simplify_anchor_grid.clear()
        self._vector_simplify_last_sample = None
        self._vector_simplify_overlay.clear()
        self._vector_before = None
        self._vector_drag_points.clear()
        self._vector_connect_endpoints.clear()
        self._drawing = False
        self._vector_changed()

    def set_selection(
        self, kind: str, entity_id: str, activate_default_tool: bool = True,
    ) -> None:
        if self.chapter is None:
            return
        previous_tool = self.tool
        if (
            self._page_gap_state is not None
            and self._page_gap_transaction is None
            and not (
                kind == "layer"
                and entity_id == self._page_gap_state.get("owner_id")
            )
        ):
            self._clear_page_gap_editor()
        if entity_id != self.selected_object_id:
            self._clear_transform_preview()
            self._transform_pivot = None
            self._transform_pivot_custom = False
        if entity_id != self.selected_id:
            self._pending_drawing_selection_press = None
            self._pending_raster_transform_press = None
            self._gradient_preview_active = False
            self._selected_shape_node_id = ""
            self._selected_shape_node_ids.clear()
            self._shape_hover_insert = None
            self._selected_vector_stroke_ids.clear()
            self._selected_vector_point_ids.clear()
            self._clear_drawing_selection()
            self.vectorSelectionChanged.emit(set(), set())
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
            elif activate_default_tool and isinstance(
                obj, VectorDrawingObject
            ):
                self.tool = ToolKind.RASTER_PENCIL
            elif activate_default_tool and isinstance(obj, VectorFillObject):
                self.tool = ToolKind.FILL
            elif activate_default_tool and isinstance(obj, GradientObject):
                self.tool = ToolKind.SHAPE_EDIT
            elif activate_default_tool and isinstance(obj, TextObject):
                self.tool = ToolKind.TEXT_EDIT
        else:
            self.selected_object_id = ""
            self.active_layer_id = entity_id
            self.active_page_id = self.chapter.page_for_layer(entity_id).layer_id
            layer = self.chapter.layers[entity_id]
            if (
                activate_default_tool
                and layer.layer_kind != "fill"
                and layer.bound is not None
            ):
                self.tool = ToolKind.SHAPE_EDIT
        if self.tool != previous_tool:
            self.toolChanged.emit(self.tool)
        self.selectionChanged.emit(kind, entity_id)
        self.update()

    def clear_selection(self) -> None:
        """Clear the current entity and notify every selection consumer."""
        if self.chapter is None:
            return
        if (
            self._page_gap_state is not None
            and self._page_gap_transaction is None
        ):
            self._clear_page_gap_editor()
        self._commit_text_edit()
        self._clear_transform_preview()
        self.selected_kind = ""
        self.selected_id = ""
        self.selected_object_id = ""
        self.active_layer_id = ""
        self.active_page_id = ""
        self._selected_shape_node_id = ""
        self._selected_shape_node_ids.clear()
        self._selected_vector_stroke_ids.clear()
        self._selected_vector_point_ids.clear()
        self._pending_raster_transform_press = None
        self._clear_drawing_selection()
        self.vectorSelectionChanged.emit(set(), set())
        self.selectionChanged.emit("", "")
        self.update()

    def set_tool(self, tool: ToolKind) -> bool:
        selected_object = (
            self.chapter.objects.get(self.selected_object_id)
            if self.chapter is not None else None
        )
        creation_tools = {
            ToolKind.BOX_BOUND, ToolKind.CIRCLE_BOUND,
            ToolKind.SHAPE_CREATE,
        }
        if (
            self._page_creation_anchor_id
            and tool not in creation_tools
        ):
            self._cancel_page_creation()
        if (
            tool == ToolKind.SHAPE_EDIT
            and isinstance(selected_object, VectorDrawingObject)
        ):
            tool = ToolKind.VECTOR_EDIT
        if (
            tool == ToolKind.TRANSFORM
            and isinstance(selected_object, RasterObject)
        ):
            return False
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
        if tool not in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}:
            self._pending_raster_press = None
            self._pending_vector_press = None
            self._pending_raster_transform_press = None
        if tool not in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
        }:
            self._pending_drawing_selection_press = None
        if tool == ToolKind.BOUND_EDIT and self.selected_object_id:
            selected = self.chapter.objects[self.selected_object_id]
            if not isinstance(
                selected,
                (RasterObject, VectorDrawingObject, GradientObject),
            ):
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
            if not isinstance(
                self.chapter.objects.get(self.selected_id),
                (RasterObject, VectorDrawingObject),
            ):
                return False
        if tool in {
            ToolKind.VECTOR_EDIT,
            ToolKind.VECTOR_REDRAW,
            ToolKind.VECTOR_CONNECT,
            ToolKind.VECTOR_SIMPLIFY,
        } and self._active_vector_drawing() is None:
            return False
        if tool in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
        }:
            selected = (
                self.chapter.objects.get(self.selected_object_id)
                if self.chapter is not None else None
            )
            if not isinstance(selected, (RasterObject, VectorDrawingObject)):
                return False
            if (
                tool == ToolKind.DRAW_SELECT_STROKE
                and not isinstance(selected, VectorDrawingObject)
            ):
                return False
        if tool == ToolKind.FILL and (
            self.chapter is None
            or (
                self.selected_kind != "layer"
                and self._active_vector_drawing() is None
            )
        ):
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
    def _single_bound_path(
        bound: BoundGeometry, vertex_radius: float = 0.0,
    ) -> QPainterPath:
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)
        if bound.primitive == "ellipse":
            x, y, width, height = bound.bbox()
            path.addEllipse(QRectF(x, y, width, height))
            return path
        nodes = bound.nodes
        if not nodes:
            return path

        def segment_points(index: int) -> tuple[QPointF, QPointF, QPointF, QPointF]:
            start = nodes[index]
            end = nodes[(index + 1) % len(nodes)]
            p0 = QPointF(start.x, start.y)
            p3 = QPointF(end.x, end.y)
            return (
                p0,
                QPointF(*(start.outgoing or start.position)),
                QPointF(*(end.incoming or end.position)),
                p3,
            )

        def split_cubic(
            points: tuple[QPointF, QPointF, QPointF, QPointF], percent: float,
        ) -> tuple[
            tuple[QPointF, QPointF, QPointF, QPointF],
            tuple[QPointF, QPointF, QPointF, QPointF],
        ]:
            p0, p1, p2, p3 = points
            a = p0 * (1 - percent) + p1 * percent
            b = p1 * (1 - percent) + p2 * percent
            c = p2 * (1 - percent) + p3 * percent
            d = a * (1 - percent) + b * percent
            e = b * (1 - percent) + c * percent
            point = d * (1 - percent) + e * percent
            return (p0, a, d, point), (point, e, c, p3)

        def sub_cubic(
            points: tuple[QPointF, QPointF, QPointF, QPointF],
            start: float,
            end: float,
        ) -> tuple[QPointF, QPointF, QPointF, QPointF]:
            if end < 1:
                points = split_cubic(points, end)[0]
            if start > 0:
                relative = start / max(end, 1e-9)
                points = split_cubic(points, relative)[1]
            return points

        def cubic_point(
            points: tuple[QPointF, QPointF, QPointF, QPointF], percent: float,
        ) -> QPointF:
            inverse = 1 - percent
            p0, p1, p2, p3 = points
            return (
                p0 * (inverse ** 3)
                + p1 * (3 * inverse * inverse * percent)
                + p2 * (3 * inverse * percent * percent)
                + p3 * (percent ** 3)
            )

        def cubic_tangent(
            points: tuple[QPointF, QPointF, QPointF, QPointF], percent: float,
        ) -> QPointF:
            inverse = 1 - percent
            p0, p1, p2, p3 = points
            return (
                (p1 - p0) * (3 * inverse * inverse)
                + (p2 - p1) * (6 * inverse * percent)
                + (p3 - p2) * (3 * percent * percent)
            )

        def length_table(
            points: tuple[QPointF, QPointF, QPointF, QPointF],
        ) -> list[tuple[float, float]]:
            result = [(0.0, 0.0)]
            previous = points[0]
            total = 0.0
            for step in range(1, 49):
                percent = step / 48
                current = cubic_point(points, percent)
                total += math.dist(
                    (previous.x(), previous.y()),
                    (current.x(), current.y()),
                )
                result.append((percent, total))
                previous = current
            return result

        def parameter_at_length(
            table: list[tuple[float, float]], target: float,
        ) -> float:
            target = max(0.0, min(table[-1][1], target))
            for index in range(1, len(table)):
                percent, distance = table[index]
                if distance < target:
                    continue
                previous_percent, previous_distance = table[index - 1]
                span = max(distance - previous_distance, 1e-9)
                ratio = (target - previous_distance) / span
                return previous_percent + (percent - previous_percent) * ratio
            return 1.0

        curve_rounding: dict[int, dict[str, object]] = {}
        segment_count = len(nodes) if bound.closed else len(nodes) - 1
        for index, node in enumerate(nodes):
            if not (
                node.roundness_enabled
                and node.roundness > 0
                and node.point_type == "bezier"
                and not node.handles_locked
                and node.incoming is not None
                and node.outgoing is not None
                and (bound.closed or 0 < index < len(nodes) - 1)
            ):
                continue
            incoming_index = index - 1 if index else len(nodes) - 1
            outgoing_index = index
            if not (0 <= incoming_index < segment_count and 0 <= outgoing_index < segment_count):
                continue
            incoming_curve = segment_points(incoming_index)
            outgoing_curve = segment_points(outgoing_index)
            incoming_table = length_table(incoming_curve)
            outgoing_table = length_table(outgoing_curve)
            incoming_length = incoming_table[-1][1]
            outgoing_length = outgoing_table[-1][1]
            radius = min(
                node.roundness, incoming_length / 2, outgoing_length / 2
            )
            if radius <= 1e-6:
                continue
            entry_t = parameter_at_length(
                incoming_table, incoming_length - radius
            )
            exit_t = parameter_at_length(outgoing_table, radius)
            curve_rounding[index] = {
                "entry": cubic_point(incoming_curve, entry_t),
                "exit": cubic_point(outgoing_curve, exit_t),
                "entry_t": entry_t,
                "exit_t": exit_t,
                "incoming_tangent": cubic_tangent(incoming_curve, entry_t),
                "outgoing_tangent": cubic_tangent(outgoing_curve, exit_t),
                "anchor_incoming_tangent": cubic_tangent(incoming_curve, 1.0),
                "anchor_outgoing_tangent": cubic_tangent(outgoing_curve, 0.0),
            }

        def segment_is_cubic(index: int) -> bool:
            start = nodes[index]
            end = nodes[(index + 1) % len(nodes)]
            return start.outgoing is not None or end.incoming is not None

        vector_curve_rounding: dict[int, dict[str, object]] = {}
        for index, node in enumerate(nodes):
            if not (
                node.point_type == "vector"
                and node.roundness_enabled
                and node.roundness > 0
                and (bound.closed or 0 < index < len(nodes) - 1)
            ):
                continue
            incoming_index = index - 1 if index else len(nodes) - 1
            outgoing_index = index
            if not (
                0 <= incoming_index < segment_count
                and 0 <= outgoing_index < segment_count
                and (
                    segment_is_cubic(incoming_index)
                    or segment_is_cubic(outgoing_index)
                )
            ):
                continue
            incoming_curve = segment_points(incoming_index)
            outgoing_curve = segment_points(outgoing_index)
            incoming_table = length_table(incoming_curve)
            outgoing_table = length_table(outgoing_curve)
            incoming_length = incoming_table[-1][1]
            outgoing_length = outgoing_table[-1][1]
            radius = min(
                node.roundness, incoming_length / 2, outgoing_length / 2
            )
            if radius <= 1e-6:
                continue
            entry_t = parameter_at_length(
                incoming_table, incoming_length - radius
            )
            exit_t = parameter_at_length(outgoing_table, radius)
            vector_curve_rounding[index] = {
                "entry": cubic_point(incoming_curve, entry_t),
                "exit": cubic_point(outgoing_curve, exit_t),
                "entry_t": entry_t,
                "exit_t": exit_t,
                "incoming_tangent": cubic_tangent(incoming_curve, entry_t),
                "outgoing_tangent": cubic_tangent(outgoing_curve, exit_t),
            }

        trim_rounding = {
            **vector_curve_rounding,
            **curve_rounding,
        }

        def rounding(index: int) -> tuple[QPointF, QPointF]:
            if index in curve_rounding:
                corner = curve_rounding[index]
                return corner["entry"], corner["exit"]
            if index in vector_curve_rounding:
                corner = vector_curve_rounding[index]
                return corner["entry"], corner["exit"]
            node = nodes[index]
            position = QPointF(node.x, node.y)
            may_round = (
                node.roundness_enabled
                and node.roundness > 0
                and node.point_type == "vector"
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
            if start_index in trim_rounding or end_index in trim_rounding:
                points = segment_points(start_index)
                start_t = float(
                    trim_rounding.get(start_index, {}).get("exit_t", 0.0)
                )
                end_t = float(
                    trim_rounding.get(end_index, {}).get("entry_t", 1.0)
                )
                if start_t > end_t:
                    start_t = end_t = (start_t + end_t) / 2
                p0, p1, p2, p3 = sub_cubic(points, start_t, end_t)
                actual_start = rounded[start_index][1]
                p1 += actual_start - p0
                p2 += target - p3
                path.cubicTo(p1, p2, target)
            elif start_node.outgoing is not None or end_node.incoming is not None:
                control_a = (
                    QPointF(*start_node.outgoing)
                    if start_node.outgoing is not None
                    else QPointF(rounded[start_index][1])
                )
                control_b = (
                    QPointF(*end_node.incoming)
                    if end_node.incoming is not None else QPointF(target)
                )
                path.cubicTo(control_a, control_b, target)
            else:
                path.lineTo(target)
            if end_index in curve_rounding:
                corner = curve_rounding[end_index]
                entry = corner["entry"]
                exit_point = corner["exit"]
                incoming_tangent = corner["incoming_tangent"]
                outgoing_tangent = corner["outgoing_tangent"]
                anchor = QPointF(end_node.x, end_node.y)

                def unit(vector: QPointF) -> QPointF:
                    length = math.hypot(vector.x(), vector.y())
                    return (
                        vector / length
                        if length > 1e-9 else QPointF()
                    )

                incoming_unit = unit(incoming_tangent)
                outgoing_unit = unit(outgoing_tangent)
                incoming_chord = unit(anchor - entry)
                outgoing_chord = unit(exit_point - anchor)
                # The chord bisector is the stable tangent through the point.
                # It follows the actual trimmed curves without inheriting a
                # backwards-facing raw handle that would create a loop.
                shared = incoming_chord + outgoing_chord
                if math.hypot(shared.x(), shared.y()) <= 1e-6:
                    shared = exit_point - entry
                if math.hypot(shared.x(), shared.y()) <= 1e-6:
                    shared = (
                        unit(corner["anchor_incoming_tangent"])
                        + unit(corner["anchor_outgoing_tangent"])
                    )
                shared = unit(shared)
                if math.hypot(shared.x(), shared.y()) <= 1e-6:
                    shared = unit(anchor - entry)

                incoming_span = math.dist(
                    (entry.x(), entry.y()), (anchor.x(), anchor.y())
                )
                outgoing_span = math.dist(
                    (anchor.x(), anchor.y()),
                    (exit_point.x(), exit_point.y()),
                )
                shared_handle = min(incoming_span, outgoing_span) / 2

                def safe_outer_handle(
                    tangent: QPointF, chord: QPointF, span: float,
                ) -> float:
                    projection = QPointF.dotProduct(unit(tangent), unit(chord))
                    return span / 3 * max(0.0, min(1.0, projection))

                incoming_handle = safe_outer_handle(
                    incoming_tangent, anchor - entry, incoming_span
                )
                outgoing_handle = safe_outer_handle(
                    outgoing_tangent, exit_point - anchor, outgoing_span
                )

                # Two local Hermite spans meet at the original point. Equal
                # handles along the shared tangent make that join C1 while
                # the outer handles retain the incident cubic tangents.
                path.cubicTo(
                    entry + incoming_unit * incoming_handle,
                    anchor - shared * shared_handle,
                    anchor,
                )
                path.cubicTo(
                    anchor + shared * shared_handle,
                    exit_point - outgoing_unit * outgoing_handle,
                    exit_point,
                )
            elif end_index in vector_curve_rounding:
                corner = vector_curve_rounding[end_index]
                entry = corner["entry"]
                exit_point = corner["exit"]
                anchor = QPointF(end_node.x, end_node.y)

                def unit(vector: QPointF) -> QPointF:
                    length = math.hypot(vector.x(), vector.y())
                    return vector / length if length > 1e-9 else QPointF()

                incoming_unit = unit(corner["incoming_tangent"])
                outgoing_unit = unit(corner["outgoing_tangent"])
                chord = exit_point - entry
                chord_length = math.hypot(chord.x(), chord.y())
                if math.hypot(incoming_unit.x(), incoming_unit.y()) <= 1e-9:
                    incoming_unit = unit(anchor - entry)
                if math.hypot(outgoing_unit.x(), outgoing_unit.y()) <= 1e-9:
                    outgoing_unit = unit(exit_point - anchor)
                incoming_span = math.dist(
                    (entry.x(), entry.y()), (anchor.x(), anchor.y())
                )
                outgoing_span = math.dist(
                    (anchor.x(), anchor.y()),
                    (exit_point.x(), exit_point.y()),
                )
                maximum_incoming = min(
                    incoming_span * 2 / 3, chord_length * 2 / 3
                )
                maximum_outgoing = min(
                    outgoing_span * 2 / 3, chord_length * 2 / 3
                )

                # Intersect the forward incoming tangent with the reverse
                # outgoing tangent. For ordinary vector corners this is the
                # acute-side corner; curved incidents use the same construction
                # while retaining their actual endpoint derivatives.
                reverse_outgoing = QPointF(
                    -outgoing_unit.x(), -outgoing_unit.y()
                )
                denominator = (
                    incoming_unit.x() * reverse_outgoing.y()
                    - incoming_unit.y() * reverse_outgoing.x()
                )
                incoming_length = outgoing_length = -1.0
                if abs(denominator) > 1e-7:
                    delta = exit_point - entry
                    incoming_ray = (
                        delta.x() * reverse_outgoing.y()
                        - delta.y() * reverse_outgoing.x()
                    ) / denominator
                    outgoing_ray = (
                        delta.x() * incoming_unit.y()
                        - delta.y() * incoming_unit.x()
                    ) / denominator
                    if incoming_ray >= 0 and outgoing_ray >= 0:
                        incoming_length = incoming_ray * 2 / 3
                        outgoing_length = outgoing_ray * 2 / 3
                if incoming_length <= 1e-6 or outgoing_length <= 1e-6:
                    fallback = chord_length / 3
                    incoming_length = min(fallback, incoming_span / 2)
                    outgoing_length = min(fallback, outgoing_span / 2)
                incoming_length = max(
                    0.0, min(maximum_incoming, incoming_length)
                )
                outgoing_length = max(
                    0.0, min(maximum_outgoing, outgoing_length)
                )
                path.cubicTo(
                    entry + incoming_unit * incoming_length,
                    exit_point - outgoing_unit * outgoing_length,
                    exit_point,
                )
            elif rounded[end_index][0] != rounded[end_index][1]:
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
    def bound_path(
        cls, bound: BoundGeometry, vertex_radius: float = 0.0,
    ) -> QPainterPath:
        path = cls._single_bound_path(bound, vertex_radius)
        if not bound.additional_contours:
            return path
        path.setFillRule(Qt.OddEvenFill)
        for contour in bound.additional_contours:
            extra = BoundGeometry(
                nodes=contour.nodes, closed=contour.closed,
                primitive="custom",
            )
            path.addPath(cls._single_bound_path(extra))
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
            length = math.hypot(dx, dy)
            if length <= 1e-6:
                if index == 0:
                    dx = bound.nodes[1].x - bound.nodes[0].x
                    dy = bound.nodes[1].y - bound.nodes[0].y
                elif index == len(samples) - 1:
                    dx = bound.nodes[-1].x - bound.nodes[-2].x
                    dy = bound.nodes[-1].y - bound.nodes[-2].y
                length = math.hypot(dx, dy)
            if length <= 1e-6:
                dx, dy, length = 1.0, 0.0, 1.0
            normal = QPointF(-dy / length, dx / length) * (width / 2)
            left.append(point + normal)
            right.append(point - normal)
        def endpoint_tangent(start: bool) -> QPointF:
            endpoint = samples[0 if start else -1][0]
            candidates = (
                samples[1:] if start else reversed(samples[:-1])
            )
            for candidate, _width in candidates:
                tangent = (
                    candidate - endpoint if start
                    else endpoint - candidate
                )
                if math.hypot(tangent.x(), tangent.y()) > 1e-6:
                    return tangent
            first, second = (
                (bound.nodes[0], bound.nodes[1])
                if start else (bound.nodes[-2], bound.nodes[-1])
            )
            fallback = QPointF(second.x - first.x, second.y - first.y)
            return (
                fallback
                if math.hypot(fallback.x(), fallback.y()) > 1e-6
                else QPointF(1, 0)
            )

        start_tangent = endpoint_tangent(True)
        end_tangent = endpoint_tangent(False)
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
        elif end_cap == "round":
            center = samples[-1][0]
            radius = samples[-1][1] / 2
            length = max(
                1e-6, math.hypot(end_tangent.x(), end_tangent.y())
            )
            tangent = end_tangent / length
            normal = left[-1] - center
            normal_length = max(
                1e-6, math.hypot(normal.x(), normal.y())
            )
            normal = normal / normal_length
            kappa = 0.5522847498307936
            outward = center + tangent * radius
            mesh.cubicTo(
                left[-1] + tangent * (kappa * radius),
                outward + normal * (kappa * radius),
                outward,
            )
            mesh.cubicTo(
                outward - normal * (kappa * radius),
                right[-1] + tangent * (kappa * radius),
                right[-1],
            )
        for point in reversed(right):
            mesh.lineTo(point)
        if start_cap == "round":
            center = samples[0][0]
            radius = samples[0][1] / 2
            length = max(
                1e-6, math.hypot(start_tangent.x(), start_tangent.y())
            )
            tangent = start_tangent / length
            normal = left[0] - center
            normal_length = max(
                1e-6, math.hypot(normal.x(), normal.y())
            )
            normal = normal / normal_length
            outward_tangent = tangent * -1
            outward = center + outward_tangent * radius
            mesh.cubicTo(
                right[0] + outward_tangent * (kappa * radius),
                outward - normal * (kappa * radius),
                outward,
            )
            mesh.cubicTo(
                outward + normal * (kappa * radius),
                left[0] + outward_tangent * (kappa * radius),
                left[0],
            )
        mesh.closeSubpath()
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

    def _clear_compound_path_cache(self, *args) -> None:
        self._compound_path_cache.clear()

    def _layer_operand_path(self, layer: LayerNode) -> QPainterPath:
        if layer.bound is None:
            return QPainterPath()
        if layer.layer_kind == "open_shape":
            return self.open_shape_mesh(
                layer.bound, layer.shape_style.base_thickness, 0,
                layer.shape_style.start_cap, layer.shape_style.end_cap,
            )
        return self.bound_path(layer.bound, layer.vertex_radius)

    def layer_effective_path(self, layer_id: str) -> QPainterPath:
        layer = self.chapter.layers[layer_id]
        if not layer.compound_enabled:
            return self.layer_shape_path(layer)
        cached = self._compound_path_cache.get(layer_id)
        if cached is not None:
            return QPainterPath(cached)
        root_x, root_y = self.chapter.layer_world_translation(layer_id)
        additions = QPainterPath(self._layer_operand_path(layer))
        additions.setFillRule(Qt.OddEvenFill)
        subtractions = QPainterPath()
        subtractions.setFillRule(Qt.OddEvenFill)

        def combine(target: QPainterPath, operand: QPainterPath) -> QPainterPath:
            return QPainterPath(operand) if target.isEmpty() else target.united(operand)

        def collect(parent: LayerNode) -> None:
            nonlocal additions, subtractions
            for reference in parent.children:
                if reference.kind != "layer":
                    continue
                child = self.chapter.layers[reference.entity_id]
                if (
                    not child.visible or child.layer_kind == "fill"
                    or child.compound_operation == "ignore"
                ):
                    continue
                operand = (
                    self.layer_effective_path(child.layer_id)
                    if child.compound_enabled
                    else self._layer_operand_path(child)
                )
                child_x, child_y = self.chapter.layer_world_translation(
                    child.layer_id
                )
                transform = QTransform()
                transform.translate(child_x - root_x, child_y - root_y)
                operand = transform.map(operand)
                if child.compound_operation == "subtract":
                    subtractions = combine(subtractions, operand)
                else:
                    additions = combine(additions, operand)
                if not child.compound_enabled:
                    collect(child)

        collect(layer)
        result = (
            additions.subtracted(subtractions)
            if not subtractions.isEmpty() else additions
        )
        result.setFillRule(Qt.OddEvenFill)
        self._compound_path_cache[layer_id] = QPainterPath(result)
        return result

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#242428"))
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.chapter is None:
            painter.setPen(QColor("#8e8e96"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Create or open a series to begin")
            return
        if self._navigation_snapshot_active and not self._navigation_snapshot.isNull():
            self._paint_navigation_snapshot(painter)
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
            visible = self.visible_document_rect()
            self._set_live_underlay_context()
            self._render_selected_raster_preview(
                painter, visible
            )
            self._render_selected_drawing_underlay(painter, visible)
            self._draw_grid(painter, visible)
            self._draw_selection(painter)
            self._clear_live_underlay_context()
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
        self._set_live_underlay_context()
        for page_id in reversed(self.chapter.root_page_ids):
            self._render_layer(painter, self.chapter.layers[page_id], 1.0, visible)
        self._render_selected_drawing_underlay(painter, visible)
        self._clear_live_underlay_context()
        self._draw_grid(painter, visible)
        self._draw_predictive_ink(painter)
        self._draw_live_vector_gesture(painter)
        self._draw_selection(painter)
        self._draw_page_gap_overlay(painter)
        self._draw_creation_preview(painter)
        painter.restore()
        painter.setTransform(QTransform())
        painter.setPen(QPen(QColor("#44444d"), 1))
        chapter_poly = self.camera_transform().map(QPolygonF(QRectF(
            0, 0, self.chapter.width, self.chapter.height
        )))
        painter.drawPolygon(chapter_poly)
        self._draw_tablet_hover(painter)
        self._draw_simplify_hover(painter)

    def _paint_navigation_snapshot(self, painter: QPainter) -> None:
        """Paint the captured artwork under the current camera cheaply."""
        current = self.camera_transform()
        inverse, invertible = self._navigation_snapshot_transform.inverted()
        if not invertible:
            inverse = QTransform()
        painter.save()
        painter.setTransform(current * inverse)
        painter.drawImage(QPointF(0, 0), self._navigation_snapshot)
        painter.restore()
        painter.setTransform(current)
        painter.save()
        painter.setClipRect(QRectF(0, 0, self.chapter.width, self.chapter.height))
        visible = self.visible_document_rect()
        self._draw_grid(painter, visible)
        self._draw_selection(painter)
        self._draw_page_gap_overlay(painter)
        painter.restore()
        painter.setTransform(QTransform())
        self._draw_tablet_hover(painter)
        self._draw_simplify_hover(painter)

    def _capture_navigation_snapshot(self) -> None:
        if self.chapter is None:
            return
        # ``grab`` is performed once at gesture/rebase boundaries.  All
        # intermediate frames only transform this image, avoiding a complete
        # layer/vector/gradient render for every touch packet.
        self._navigation_snapshot = self.grab().toImage()
        self._navigation_snapshot_transform = self.camera_transform()
        self._navigation_snapshot_active = not self._navigation_snapshot.isNull()

    def _clear_navigation_snapshot(self) -> None:
        self._navigation_snapshot_active = False
        self._navigation_snapshot = QImage()

    def _set_live_underlay_context(self) -> None:
        self._live_underlay_object_id = ""
        self._live_underlay_amount = 0.0
        if self.chapter is None or self.selected_kind != "object":
            return
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, (RasterObject, VectorDrawingObject)):
            return
        amount = max(0.0, min(1.0, float(obj.underlay_opacity)))
        if amount > 0:
            self._live_underlay_object_id = obj.object_id
            self._live_underlay_amount = amount

    def _clear_live_underlay_context(self) -> None:
        self._live_underlay_object_id = ""
        self._live_underlay_amount = 0.0

    def _render_selected_drawing_underlay(
        self, painter: QPainter, visible: QRectF,
    ) -> None:
        if (
            self.chapter is None
            or not self._live_underlay_object_id
            or self._live_underlay_amount <= 0
        ):
            return
        obj = self.chapter.objects.get(self._live_underlay_object_id)
        if (
            not isinstance(obj, (RasterObject, VectorDrawingObject))
            or not obj.visible
        ):
            return
        ancestors = self.chapter.ancestor_layers(obj.parent_layer_id)
        if any(not layer.visible or layer.opacity <= 0 for layer in ancestors):
            return
        world_x, world_y = self.chapter.layer_world_translation(
            obj.parent_layer_id
        )
        painter.save()
        painter.setOpacity(
            self.chapter.effective_object_opacity(obj.object_id)
            * self._live_underlay_amount
        )
        painter.translate(world_x, world_y)
        if isinstance(obj, VectorDrawingObject):
            self._render_vector_drawing(painter, obj)
        else:
            self._render_raster_content(
                painter, obj, visible.translated(-world_x, -world_y),
                use_transform_preview=True,
            )
        painter.restore()

    def _draw_page_gap_overlay(self, painter: QPainter) -> None:
        if self.chapter is None:
            return
        scale = max(self.scale, 0.05)
        painter.save()
        painter.setBrush(Qt.NoBrush)
        pen = QPen(
            QColor("#ff9f22"), 2 / scale, Qt.DotLine,
            Qt.RoundCap, Qt.RoundJoin,
        )
        painter.setPen(pen)
        if self._page_gap_prompt_y is not None:
            y = self._page_gap_prompt_y
            painter.drawLine(QPointF(0, y), QPointF(self.chapter.width, y))
        if self.tool == ToolKind.INSERT_PAGE_GAP and self._page_gap_hover:
            y = float(self._page_gap_hover["y"])
            painter.drawLine(QPointF(0, y), QPointF(self.chapter.width, y))
        if self._page_gap_editor_visible():
            top = float(self._page_gap_state["top_y"])
            bottom = float(self._page_gap_state["bottom_y"])
            painter.fillRect(
                QRectF(0, top, self.chapter.width, bottom - top),
                QColor(255, 159, 34, 38),
            )
            painter.drawLine(
                QPointF(0, top), QPointF(self.chapter.width, top)
            )
            painter.drawLine(
                QPointF(0, bottom), QPointF(self.chapter.width, bottom)
            )
        painter.restore()

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
                self.layer_effective_path(parent.layer_id),
                QColor(layer.fill_color or "#111111"),
            )
            painter.restore()
            return
        painter.save()
        painter.translate(layer.translate_x, layer.translate_y)
        world_x, world_y = self.chapter.layer_world_translation(
            layer.layer_id
        )
        local_visible = visible_world.translated(-world_x, -world_y)
        self._render_outward_gradient_children(
            painter, layer, parent_opacity * layer.opacity, local_visible
        )
        if layer.compound_enabled:
            self._render_compound_layer_contents(
                painter, layer, parent_opacity, visible_world
            )
            painter.restore()
            return
        if layer.layer_kind == "open_shape":
            style = layer.shape_style
            opacity = parent_opacity * layer.opacity
            painter.setOpacity(opacity)
            core = self.open_shape_mesh(
                layer.bound, style.base_thickness, 0,
                style.start_cap, style.end_cap,
            )
            painter.fillPath(
                core, QColor(style.primary_color or "#111111"),
            )
            clip_path = self.open_shape_mesh(
                layer.bound, style.base_thickness,
                style.outline_thickness * 2,
                style.start_cap, style.end_cap,
            )
            painter.save()
            painter.setClipPath(clip_path, Qt.IntersectClip)
            world_x, world_y = self.chapter.layer_world_translation(
                layer.layer_id
            )
            local_visible = visible_world.translated(-world_x, -world_y)
            for child in reversed(layer.children):
                if self._child_ignores_parent_mask(child):
                    continue
                if child.kind == "layer":
                    self._render_layer(
                        painter, self.chapter.layers[child.entity_id],
                        opacity, visible_world,
                    )
                else:
                    self._render_object(
                        painter, self.chapter.objects[child.entity_id],
                        opacity, local_visible,
                    )
            painter.restore()
            if style.outline_thickness > 0:
                ring = clip_path.subtracted(core)
                painter.fillPath(ring, QColor(style.outline_color))
            for child in reversed(layer.children):
                if not self._child_ignores_parent_mask(child):
                    continue
                if child.kind == "layer":
                    self._render_layer(
                        painter, self.chapter.layers[child.entity_id],
                        opacity, visible_world,
                    )
                else:
                    self._render_object(
                        painter, self.chapter.objects[child.entity_id],
                        opacity, local_visible,
                    )
            painter.restore()
            return
        layer_path = self.bound_path(layer.bound, layer.vertex_radius)
        opacity = parent_opacity * layer.opacity
        if layer.fill_color:
            painter.save()
            painter.setOpacity(opacity)
            painter.setClipPath(layer_path, Qt.IntersectClip)
            painter.fillPath(layer_path, QColor(layer.fill_color))
            painter.restore()
        world_x, world_y = self.chapter.layer_world_translation(layer.layer_id)
        local_visible = visible_world.translated(-world_x, -world_y)
        painter.save()
        painter.setClipPath(layer_path, Qt.IntersectClip)
        for child in reversed(layer.children):
            if self._child_ignores_parent_mask(child):
                continue
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
        for child in reversed(layer.children):
            if not self._child_ignores_parent_mask(child):
                continue
            if child.kind == "layer":
                self._render_layer(
                    painter, self.chapter.layers[child.entity_id],
                    opacity, visible_world,
                )
            else:
                self._render_object(
                    painter, self.chapter.objects[child.entity_id],
                    opacity, local_visible,
                )
        painter.restore()
        return

    def _render_compound_layer_contents(
        self, painter: QPainter, layer: LayerNode, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        layer_path = self.layer_effective_path(layer.layer_id)
        opacity = parent_opacity * layer.opacity
        world_x, world_y = self.chapter.layer_world_translation(layer.layer_id)
        local_visible = visible_world.translated(-world_x, -world_y)
        painter.save()
        painter.setClipPath(layer_path, Qt.IntersectClip)
        if layer.fill_color:
            painter.save()
            painter.setOpacity(opacity)
            painter.fillPath(layer_path, QColor(layer.fill_color))
            painter.restore()
        for child in reversed(layer.children):
            if self._child_ignores_parent_mask(child):
                continue
            if child.kind == "object":
                self._render_object(
                    painter, self.chapter.objects[child.entity_id], opacity,
                    local_visible,
                )
                continue
            candidate = self.chapter.layers[child.entity_id]
            if candidate.compound_operation == "ignore":
                self._render_layer(
                    painter, candidate, opacity, visible_world
                )
            elif candidate.visible:
                self._render_compound_contributor(
                    painter, candidate, opacity, visible_world
                )
        self._render_compound_reference_objects(
            painter, layer.layer_id, opacity, visible_world
        )
        painter.restore()
        if layer.border_width > 0:
            painter.save()
            painter.setOpacity(opacity)
            pen = QPen(
                QColor(layer.border_color), layer.border_width * 2,
                Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin,
            )
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(layer_path)
            painter.restore()
        for child in reversed(layer.children):
            if not self._child_ignores_parent_mask(child):
                continue
            if child.kind == "object":
                self._render_object(
                    painter, self.chapter.objects[child.entity_id], opacity,
                    local_visible,
                )
            else:
                self._render_layer(
                    painter, self.chapter.layers[child.entity_id],
                    opacity, visible_world,
                )

    def _render_compound_contributor(
        self, painter: QPainter, layer: LayerNode, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        if not layer.visible:
            return
        painter.save()
        painter.translate(layer.translate_x, layer.translate_y)
        path = (
            self.layer_effective_path(layer.layer_id)
            if layer.compound_enabled else self._layer_operand_path(layer)
        )
        painter.save()
        painter.setClipPath(path, Qt.IntersectClip)
        opacity = parent_opacity * layer.opacity
        world_x, world_y = self.chapter.layer_world_translation(layer.layer_id)
        local_visible = visible_world.translated(-world_x, -world_y)
        for child in reversed(layer.children):
            if self._child_ignores_parent_mask(child):
                continue
            if child.kind == "object":
                self._render_object(
                    painter, self.chapter.objects[child.entity_id], opacity,
                    local_visible,
                )
                continue
            candidate = self.chapter.layers[child.entity_id]
            if candidate.compound_operation == "ignore":
                self._render_layer(
                    painter, candidate, opacity, visible_world
                )
            elif candidate.visible:
                self._render_compound_contributor(
                    painter, candidate, opacity, visible_world
                )
        if layer.compound_enabled:
            self._render_compound_reference_objects(
                painter, layer.layer_id, opacity, visible_world
            )
        painter.restore()
        for child in reversed(layer.children):
            if not self._child_ignores_parent_mask(child):
                continue
            if child.kind == "object":
                self._render_object(
                    painter, self.chapter.objects[child.entity_id],
                    opacity, local_visible,
                )
            else:
                self._render_layer(
                    painter, self.chapter.layers[child.entity_id],
                    opacity, visible_world,
                )
        painter.restore()

    def _child_ignores_parent_mask(self, child: ChildRef) -> bool:
        entity = (
            self.chapter.layers.get(child.entity_id)
            if child.kind == "layer"
            else self.chapter.objects.get(child.entity_id)
        )
        return bool(entity and entity.ignore_parent_mask)

    @staticmethod
    def _is_outward_gradient(obj: GradientObject) -> bool:
        return bool(
            (
                obj.field_type == "radial"
                and obj.radial_field.reverse_direction
            )
            or (
                obj.field_type == "parent_shape"
                and obj.shape_field.reverse_direction
            )
        )

    def _render_outward_gradient_children(
        self, painter: QPainter, layer: LayerNode, opacity: float,
        local_visible: QRectF,
    ) -> None:
        """Render outward gradients before their direct parent artwork."""
        previous = self._rendering_outward_gradient
        self._rendering_outward_gradient = True
        try:
            for child in reversed(layer.children):
                if child.kind != "object":
                    continue
                obj = self.chapter.objects.get(child.entity_id)
                if (
                    isinstance(obj, GradientObject)
                    and self._is_outward_gradient(obj)
                ):
                    self._render_object(
                        painter, obj, opacity, local_visible
                    )
        finally:
            self._rendering_outward_gradient = previous

    def _render_compound_reference_objects(
        self, painter: QPainter, compound_id: str, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        compound_x, compound_y = self.chapter.layer_world_translation(
            compound_id
        )
        references: list[DocumentObject] = []

        def collect(layer: LayerNode) -> None:
            if layer.layer_id != compound_id and not layer.visible:
                return
            for child in reversed(layer.children):
                if child.kind == "object":
                    obj = self.chapter.objects[child.entity_id]
                    closest = self.chapter.closest_compound_ancestor(
                        obj.parent_layer_id, include_self=True
                    )
                    if (
                        obj.geometry_reference == "compound"
                        and closest is not None
                        and closest.layer_id == compound_id
                    ):
                        references.append(obj)
                else:
                    collect(self.chapter.layers[child.entity_id])

        collect(self.chapter.layers[compound_id])
        self._rendering_compound_references = True
        try:
            for obj in references:
                parent_x, parent_y = self.chapter.layer_world_translation(
                    obj.parent_layer_id
                )
                branch_opacity = parent_opacity
                cursor = self.chapter.layers[obj.parent_layer_id]
                while cursor.layer_id != compound_id:
                    branch_opacity *= cursor.opacity
                    if cursor.parent_id is None:
                        break
                    cursor = self.chapter.layers[cursor.parent_id]
                painter.save()
                painter.translate(
                    parent_x - compound_x, parent_y - compound_y
                )
                local_visible = visible_world.translated(
                    -parent_x, -parent_y
                )
                self._render_object(
                    painter, obj, branch_opacity, local_visible
                )
                painter.restore()
        finally:
            self._rendering_compound_references = False

    @staticmethod
    def _vector_centerline_path(stroke: VectorStroke) -> QPainterPath:
        path = QPainterPath()
        if not stroke.points:
            return path
        path.moveTo(QPointF(*stroke.points[0].position))
        for cubic in stroke_cubics(stroke.points, stroke.closed):
            path.cubicTo(
                QPointF(*cubic[1]),
                QPointF(*cubic[2]),
                QPointF(*cubic[3]),
            )
        return path

    def _vector_stroke_image(
        self, drawing: VectorDrawingObject, stroke: VectorStroke,
        *, cache_token: object | None = None,
    ) -> tuple[QImage, QRectF] | None:
        """Rasterize one stroke opacity mask, then colorize it exactly once."""
        if not stroke.points:
            return None
        device_ratio = max(1.0, float(self.devicePixelRatioF()))
        requested_scale = max(0.1, min(8.0, self.scale * device_ratio))
        key = (
            drawing.object_id,
            stroke.stroke_id,
            stroke.render_revision if cache_token is None else cache_token,
            stroke.color,
            stroke.closed,
            stroke.start_cap,
            stroke.end_cap,
            round(requested_scale, 3),
            device_ratio,
        )
        cached = self._vector_render_cache.get(key)
        if cached is not None:
            self._vector_render_cache.pop(key, None)
            self._vector_render_cache[key] = cached
            return cached
        left, top, width, height = stroke.derived_bounds()
        padding = 3.0 / requested_scale
        target = QRectF(
            left - padding, top - padding,
            max(1.0, width + padding * 2),
            max(1.0, height + padding * 2),
        )
        render_scale = requested_scale
        maximum_dimension = max(target.width(), target.height()) * render_scale
        if maximum_dimension > 8192:
            render_scale *= 8192 / maximum_dimension
        pixel_width = max(1, math.ceil(target.width() * render_scale))
        pixel_height = max(1, math.ceil(target.height() * render_scale))
        mask = QImage(pixel_width, pixel_height, QImage.Format_Alpha8)
        mask.fill(0)
        mask_painter = QPainter(mask)
        mask_painter.setRenderHint(QPainter.Antialiasing, True)
        mask_painter.setCompositionMode(QPainter.CompositionMode_Lighten)
        mask_painter.scale(render_scale, render_scale)
        mask_painter.translate(-target.left(), -target.top())
        if len(stroke.points) == 1:
            point = stroke.points[0]
            mask_painter.setPen(Qt.NoPen)
            mask_painter.setBrush(QColor(
                255, 255, 255,
                round(max(0.0, min(1.0, point.opacity)) * 255),
            ))
            mask_painter.drawEllipse(
                QPointF(point.x, point.y), point.width / 2, point.width / 2
            )
        else:
            samples = flatten_stroke(
                stroke.points, closed=stroke.closed, tolerance=0.3
            )
            raster_samples: list[
                tuple[tuple[float, float], float, float]
            ] = []
            for first, second in zip(samples, samples[1:]):
                length = math.dist(first.point, second.point)
                steps = max(1, math.ceil(length * render_scale / 3))
                if not raster_samples:
                    raster_samples.append(
                        (first.point, first.width, first.opacity)
                    )
                for step in range(1, steps + 1):
                    amount = step / steps
                    current_point = (
                        first.point[0]
                        + (second.point[0] - first.point[0]) * amount,
                        first.point[1]
                        + (second.point[1] - first.point[1]) * amount,
                    )
                    current_width = (
                        first.width + (second.width - first.width) * amount
                    )
                    current_opacity = (
                        first.opacity
                        + (second.opacity - first.opacity) * amount
                    )
                    raster_samples.append(
                        (current_point, current_width, current_opacity)
                    )
            for first, second in zip(raster_samples, raster_samples[1:]):
                opacity = max(
                    0.0, min(1.0, (first[2] + second[2]) / 2)
                )
                pen = QPen(
                    QColor(255, 255, 255, round(opacity * 255)),
                    max(1.0, (first[1] + second[1]) / 2),
                    Qt.SolidLine,
                    Qt.FlatCap,
                    Qt.RoundJoin,
                )
                mask_painter.setPen(pen)
                mask_painter.drawLine(
                    QPointF(*first[0]), QPointF(*second[0])
                )
            mask_painter.setPen(Qt.NoPen)
            for point, width, opacity in raster_samples[1:-1]:
                mask_painter.setBrush(QColor(
                    255, 255, 255,
                    round(max(0.0, min(1.0, opacity)) * 255),
                ))
                mask_painter.drawEllipse(
                    QPointF(*point), width / 2, width / 2
                )

            def draw_cap(
                endpoint, neighbor, cap: str, outward: bool,
            ) -> None:
                point, width, opacity = endpoint
                direction = QPointF(
                    point[0] - neighbor[0][0],
                    point[1] - neighbor[0][1],
                )
                magnitude = math.hypot(direction.x(), direction.y())
                if magnitude <= 1.0e-8:
                    return
                direction /= magnitude
                if not outward:
                    direction = -direction
                normal = QPointF(-direction.y(), direction.x())
                radius = width / 2
                mask_painter.setBrush(QColor(
                    255, 255, 255,
                    round(max(0.0, min(1.0, opacity)) * 255),
                ))
                if cap == "round":
                    mask_painter.drawEllipse(
                        QPointF(*point), radius, radius
                    )
                elif cap == "point":
                    center = QPointF(*point)
                    mask_painter.drawPolygon(QPolygonF([
                        center + normal * radius,
                        center + direction * radius,
                        center - normal * radius,
                    ]))
                elif cap == "square":
                    center = QPointF(*point) + direction * (radius / 2)
                    mask_painter.drawPolygon(QPolygonF([
                        center + normal * radius - direction * (radius / 2),
                        center - normal * radius - direction * (radius / 2),
                        center - normal * radius + direction * (radius / 2),
                        center + normal * radius + direction * (radius / 2),
                    ]))

            if raster_samples and not stroke.closed:
                draw_cap(
                    raster_samples[0], raster_samples[1],
                    stroke.start_cap, True,
                )
                draw_cap(
                    raster_samples[-1], raster_samples[-2],
                    stroke.end_cap, True,
                )
            elif raster_samples and stroke.closed:
                point, width, opacity = raster_samples[0]
                mask_painter.setBrush(QColor(
                    255, 255, 255,
                    round(max(0.0, min(1.0, opacity)) * 255),
                ))
                mask_painter.drawEllipse(
                    QPointF(*point), width / 2, width / 2
                )
        mask_painter.end()
        image = QImage(
            pixel_width, pixel_height, QImage.Format_ARGB32_Premultiplied
        )
        image.fill(Qt.transparent)
        image_painter = QPainter(image)
        image_painter.fillRect(image.rect(), QColor(stroke.color))
        image_painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        image_painter.drawImage(0, 0, mask)
        image_painter.end()
        result = image, target
        self._vector_render_cache[key] = result
        # Keep a bounded insertion-ordered cache without pulling a dependency
        # into the hot rendering path.  Re-inserting a hit makes it recent.
        while len(self._vector_render_cache) > 384:
            self._vector_render_cache.pop(next(iter(self._vector_render_cache)))
        return result

    def _render_vector_fill(
        self, painter: QPainter, fill: VectorFillObject,
    ) -> None:
        if not fill.visible:
            return
        painter.save()
        painter.setOpacity(
            painter.opacity() * (1.0 if fill.opacity_locked else fill.opacity)
        )
        painter.fillPath(self.bound_path(fill.geometry), QColor(fill.fill_color))
        painter.restore()

    def _render_vector_drawing(
        self, painter: QPainter, drawing: VectorDrawingObject,
    ) -> None:
        painter.save()
        painter.translate(drawing.x, drawing.y)
        # Fill IDs are frontmost-first, so paint them back-to-front.
        for fill_id in reversed(drawing.fill_child_ids):
            fill = self.chapter.objects.get(fill_id)
            if isinstance(fill, VectorFillObject):
                self._render_vector_fill(painter, fill)
        for stroke in drawing.strokes:
            preview_points = {
                point.point_id: self._selection_vector_preview[point.point_id]
                for point in stroke.points
                if point.point_id in self._selection_vector_preview
            }
            render_stroke = stroke
            cache_token = None
            if (
                preview_points
                and drawing.object_id == self.selected_object_id
            ):
                render_stroke = VectorStroke(
                    stroke_id=stroke.stroke_id,
                    color=stroke.color,
                    closed=stroke.closed,
                    start_cap=stroke.start_cap,
                    end_cap=stroke.end_cap,
                    points=[
                        VectorStrokePoint(
                            point_id=point.point_id,
                            x=preview_points.get(point.point_id, {}).get(
                                "position", point.position
                            )[0],
                            y=preview_points.get(point.point_id, {}).get(
                                "position", point.position
                            )[1],
                            incoming=preview_points.get(
                                point.point_id, {}
                            ).get("incoming", point.incoming),
                            outgoing=preview_points.get(
                                point.point_id, {}
                            ).get("outgoing", point.outgoing),
                            width=preview_points.get(
                                point.point_id, {}
                            ).get("width", point.width),
                            opacity=point.opacity,
                        )
                        for point in stroke.points
                    ],
                    render_revision=stroke.render_revision,
                )
                cache_token = (
                    "selection-preview", self._selection_vector_preview_revision
                )
            rendered = self._vector_stroke_image(
                drawing, render_stroke, cache_token=cache_token
            )
            if rendered is not None:
                image, target = rendered
                painter.drawImage(target, image)
        painter.restore()

    @staticmethod
    def _apply_ramp_stops(
        gradient: QLinearGradient | QRadialGradient,
        ramp: ColorGradientRamp, *, reverse: bool = False,
    ) -> None:
        ramp.validate()
        for stop in ramp.stops:
            position = 1.0 - stop.position if reverse else stop.position
            gradient.setColorAt(position, QColor(stop.color))

    @staticmethod
    def _sample_color_ramp(
        ramp: ColorGradientRamp, value: float,
    ) -> QColor:
        ramp.validate()
        value = max(0.0, min(1.0, float(value)))
        stops = ramp.stops
        if value <= stops[0].position:
            return QColor(stops[0].color)
        for left, right in zip(stops, stops[1:]):
            if value > right.position:
                continue
            span = right.position - left.position
            if span <= 1e-9:
                return QColor(right.color)
            amount = (value - left.position) / span
            first, second = QColor(left.color), QColor(right.color)
            return QColor.fromRgbF(
                first.redF() + (second.redF() - first.redF()) * amount,
                first.greenF()
                + (second.greenF() - first.greenF()) * amount,
                first.blueF() + (second.blueF() - first.blueF()) * amount,
                first.alphaF()
                + (second.alphaF() - first.alphaF()) * amount,
            )
        return QColor(stops[-1].color)

    @staticmethod
    def _path_line_segments(
        path: QPainterPath,
    ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        segments: list[
            tuple[tuple[float, float], tuple[float, float]]
        ] = []
        for polygon in path.toSubpathPolygons():
            points = [(point.x(), point.y()) for point in polygon]
            if len(points) < 2:
                continue
            for start, end in zip(points, points[1:]):
                segments.append((start, end))
            if points[0] != points[-1]:
                segments.append((points[-1], points[0]))
        return segments

    @staticmethod
    def _gradient_path_signature(path: QPainterPath) -> tuple:
        return tuple(
            (
                round(path.elementAt(index).x, 3),
                round(path.elementAt(index).y, 3),
                path.elementAt(index).type.value,
            )
            for index in range(path.elementCount())
        )

    @staticmethod
    def _gradient_ramp_signature(ramp: ColorGradientRamp) -> tuple:
        ramp.validate()
        return tuple(
            (stop.stop_id, round(stop.position, 6), stop.color)
            for stop in ramp.stops
        )

    def _cache_gradient_value(
        self, cache: dict, key: tuple, value: object, limit: int = 32,
    ) -> object:
        cache[key] = value
        while len(cache) > limit:
            cache.pop(next(iter(cache)))
        return value

    @staticmethod
    def _gradient_grid(bounds: QRectF, maximum: int = 768) -> tuple[int, int]:
        width = max(2.0, bounds.width())
        height = max(2.0, bounds.height())
        ratio = width / height
        if ratio >= 1:
            return maximum, max(2, round(maximum / ratio))
        return max(2, round(maximum * ratio)), maximum

    def _gradient_grid_for_preview(
        self, bounds: QRectF,
    ) -> tuple[int, int]:
        # Geometry drags should remain interactive.  A final full-resolution
        # image is rebuilt when the gesture is released.
        return self._gradient_grid(
            bounds, 256 if self._gradient_preview_active else 768
        )

    @staticmethod
    def _gradient_coordinates(
        bounds: QRectF, width: int, height: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        xs = np.linspace(
            bounds.left() + bounds.width() / (2 * width),
            bounds.right() - bounds.width() / (2 * width),
            width,
            dtype=np.float32,
        )
        ys = np.linspace(
            bounds.top() + bounds.height() / (2 * height),
            bounds.bottom() - bounds.height() / (2 * height),
            height,
            dtype=np.float32,
        )
        return np.meshgrid(xs, ys)

    def _path_projection_arrays(
        self, path: QPainterPath, bounds: QRectF, width: int, height: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        signature = self._gradient_path_signature(path)
        key = (
            "projection", signature,
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
            width, height,
        )
        cached = self._gradient_geometry_cache.get(key)
        if cached is not None:
            return cached
        polygons = path.toSubpathPolygons()
        segment_pairs: list[
            tuple[tuple[float, float], tuple[float, float]]
        ] = []
        for polygon in polygons:
            points = [(point.x(), point.y()) for point in polygon]
            segment_pairs.extend(zip(points, points[1:]))
        if not segment_pairs:
            empty = np.zeros((height, width), dtype=np.float32)
            return empty, empty, empty
        starts = np.asarray(
            [pair[0] for pair in segment_pairs], dtype=np.float32
        )
        ends = np.asarray(
            [pair[1] for pair in segment_pairs], dtype=np.float32
        )
        vectors = ends - starts
        lengths = np.sqrt(np.sum(vectors * vectors, axis=1))
        usable = lengths > 1e-5
        starts, vectors, lengths = (
            starts[usable], vectors[usable], lengths[usable]
        )
        if not len(lengths):
            empty = np.zeros((height, width), dtype=np.float32)
            return empty, empty, empty
        cumulative = np.concatenate((
            np.zeros(1, dtype=np.float32), np.cumsum(lengths)
        ))
        total = max(float(cumulative[-1]), 1e-6)
        grid_x, grid_y = self._gradient_coordinates(
            bounds, width, height
        )
        best_distance = np.full(
            (height, width), np.inf, dtype=np.float32
        )
        best_amount = np.zeros((height, width), dtype=np.float32)
        best_signed = np.zeros((height, width), dtype=np.float32)
        for index, (start, vector, length) in enumerate(
            zip(starts, vectors, lengths)
        ):
            relative_x = grid_x - start[0]
            relative_y = grid_y - start[1]
            length_squared = float(length * length)
            along = np.clip(
                (relative_x * vector[0] + relative_y * vector[1])
                / length_squared,
                0.0, 1.0,
            )
            dx = relative_x - along * vector[0]
            dy = relative_y - along * vector[1]
            distance = dx * dx + dy * dy
            replace = distance < best_distance
            best_distance[replace] = distance[replace]
            amount = (cumulative[index] + along * length) / total
            best_amount[replace] = amount[replace]
            signed = (
                vector[0] * relative_y - vector[1] * relative_x
            ) / length
            best_signed[replace] = signed[replace]
        result = best_amount, best_signed, np.sqrt(best_distance)
        return self._cache_gradient_value(
            self._gradient_geometry_cache, key, result
        )

    def _path_coverage(
        self, path: QPainterPath, bounds: QRectF, width: int, height: int,
    ) -> np.ndarray:
        signature = self._gradient_path_signature(path)
        key = (
            "coverage", signature,
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
            width, height,
        )
        cached = self._gradient_geometry_cache.get(key)
        if cached is not None:
            return cached
        mask = QImage(width, height, QImage.Format.Format_Alpha8)
        mask.fill(0)
        mask_painter = QPainter(mask)
        transform = QTransform(
            width / max(bounds.width(), 1e-6), 0, 0,
            0, height / max(bounds.height(), 1e-6), 0,
            -bounds.left() * width / max(bounds.width(), 1e-6),
            -bounds.top() * height / max(bounds.height(), 1e-6), 1,
        )
        mask_painter.setTransform(transform)
        mask_painter.fillPath(path, Qt.GlobalColor.white)
        mask_painter.end()
        stride = mask.bytesPerLine()
        raw = np.frombuffer(mask.bits(), dtype=np.uint8).reshape(
            height, stride
        )
        coverage = raw[:, :width].copy() > 0
        return self._cache_gradient_value(
            self._gradient_geometry_cache, key, coverage
        )

    @staticmethod
    def _gradient_ramp_lut(
        ramp: ColorGradientRamp, size: int = 1024,
    ) -> np.ndarray:
        ramp.validate()
        positions = np.asarray(
            [stop.position for stop in ramp.stops], dtype=np.float32
        )
        colors = np.asarray([
            [
                QColor(stop.color).red(),
                QColor(stop.color).green(),
                QColor(stop.color).blue(),
                QColor(stop.color).alpha(),
            ]
            for stop in ramp.stops
        ], dtype=np.float32)
        values = np.linspace(0.0, 1.0, size, dtype=np.float32)
        right = np.searchsorted(positions, values, side="right")
        right = np.clip(right, 1, len(positions) - 1)
        left = right - 1
        spans = positions[right] - positions[left]
        amounts = np.divide(
            values - positions[left], spans,
            out=np.ones_like(values), where=spans > 1e-8,
        )
        result = (
            colors[left] * (1.0 - amounts[:, None])
            + colors[right] * amounts[:, None]
        )
        result[values <= positions[0]] = colors[0]
        result[values >= positions[-1]] = colors[-1]
        return np.clip(np.rint(result), 0, 255).astype(np.uint8)

    def _gradient_image_from_scalar(
        self, scalar: np.ndarray, coverage: np.ndarray,
        ramp: ColorGradientRamp, bounds: QRectF, scalar_key: tuple,
    ) -> tuple[QImage, QRectF]:
        ramp_key = self._gradient_ramp_signature(ramp)
        key = ("colored", scalar_key, ramp_key)
        cached = self._gradient_render_cache.get(key)
        if cached is not None:
            return cached
        lut = self._gradient_ramp_lut(ramp)
        indices = np.clip(
            np.rint(np.clip(scalar, 0.0, 1.0) * (len(lut) - 1)),
            0, len(lut) - 1,
        ).astype(np.int32)
        rgba = lut[indices].copy()
        rgba[~coverage] = 0
        alpha = rgba[..., 3:4].astype(np.uint16)
        rgba[..., :3] = (
            rgba[..., :3].astype(np.uint16) * alpha + 127
        ) // 255
        rgba = np.ascontiguousarray(rgba)
        height, width = scalar.shape
        image = QImage(
            rgba.data, width, height, rgba.strides[0],
            QImage.Format.Format_RGBA8888_Premultiplied,
        ).copy()
        result = image, QRectF(bounds)
        return self._cache_gradient_value(
            self._gradient_render_cache, key, result
        )

    def _shape_gradient_center(
        self, obj: GradientObject, path: QPainterPath,
    ) -> QPointF:
        field = obj.shape_field
        if not field.center_auto and field.manual_center is not None:
            return QPointF(*field.manual_center)
        bounds = path.boundingRect()
        center = bounds.center()
        if path.contains(center):
            return center
        # A stable interior fallback for concave and multi-contour shapes.
        for polygon in path.toSubpathPolygons():
            candidate = polygon.boundingRect().center()
            if path.contains(candidate):
                return candidate
            for point in polygon:
                toward = QPointF(
                    point.x() * 0.9 + bounds.center().x() * 0.1,
                    point.y() * 0.9 + bounds.center().y() * 0.1,
                )
                if path.contains(toward):
                    return toward
        return center

    def _shape_gradient_image(
        self, obj: ColorFillGradientObject, path: QPainterPath,
    ) -> tuple[QImage, QRectF] | None:
        bounds = path.boundingRect()
        if bounds.isEmpty():
            return None
        field = obj.shape_field
        if field.reverse_direction:
            bounds = bounds.adjusted(
                -field.distance, -field.distance,
                field.distance, field.distance,
            )
        width, height = self._gradient_grid_for_preview(bounds)
        path_signature = self._gradient_path_signature(path)
        scalar_key = (
            "shape", path_signature, width, height,
            field.reverse_direction,
            field.uniform, round(field.distance, 4),
            () if field.reverse_direction else (
                field.center_auto, field.manual_center,
            ),
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.ramp, cached_bounds, scalar_key
            )
        if field.reverse_direction:
            # Outward fields change their visible rectangle as Distance is
            # edited.  Build one canonical padded boundary field and sample
            # it for the current viewport so distance drags only redo the
            # scalar normalization and ramp lookup.
            canonical_bounds = path.boundingRect().adjusted(
                -1000.0, -1000.0, 1000.0, 1000.0
            )
            canonical_width, canonical_height = (
                self._gradient_grid_for_preview(canonical_bounds)
            )
            boundary_key = (
                "shape-boundary", path_signature,
                canonical_width, canonical_height,
            )
            boundary_data = self._gradient_geometry_cache.get(boundary_key)
            if boundary_data is None:
                _amount, _signed, canonical_boundary = (
                    self._path_projection_arrays(
                        path, canonical_bounds,
                        canonical_width, canonical_height,
                    )
                )
                canonical_inside = self._path_coverage(
                    path, canonical_bounds,
                    canonical_width, canonical_height,
                )
                boundary_data = (
                    canonical_boundary, canonical_inside, canonical_bounds,
                )
                self._cache_gradient_value(
                    self._gradient_geometry_cache,
                    boundary_key, boundary_data,
                )
            canonical_boundary, canonical_inside, canonical_bounds = boundary_data
            target_x, target_y = self._gradient_coordinates(
                bounds, width, height
            )
            x_index = np.clip(
                ((target_x - canonical_bounds.left())
                 / max(canonical_bounds.width(), 1e-6)
                 * (canonical_width - 1)).astype(np.int32),
                0, canonical_width - 1,
            )
            y_index = np.clip(
                ((target_y - canonical_bounds.top())
                 / max(canonical_bounds.height(), 1e-6)
                 * (canonical_height - 1)).astype(np.int32),
                0, canonical_height - 1,
            )
            boundary = canonical_boundary[y_index, x_index]
            inside = canonical_inside[y_index, x_index]
        else:
            _amount, _signed, boundary = self._path_projection_arrays(
                path, bounds, width, height
            )
            inside = self._path_coverage(path, bounds, width, height)
        if field.reverse_direction:
            scalar = np.clip(
                boundary / max(field.distance, 0.001), 0.0, 1.0
            )
            coverage = ~inside
        elif field.uniform:
            scalar = np.clip(
                boundary / max(field.distance, 0.001), 0.0, 1.0
            )
            coverage = inside
        else:
            grid_x, grid_y = self._gradient_coordinates(
                bounds, width, height
            )
            center = self._shape_gradient_center(obj, path)
            center_distance = np.hypot(
                grid_x - center.x(), grid_y - center.y()
            )
            denominator = boundary + center_distance
            scalar = np.divide(
                boundary, denominator,
                out=np.ones_like(boundary),
                where=denominator > 1e-6,
            )
            coverage = inside
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.ramp, bounds, scalar_key
        )

    @staticmethod
    def _radial_boundary_path(field: RadialGradientField) -> QPainterPath:
        radius_y = field.radius_y if field.ellipse_enabled else field.radius_x
        path = QPainterPath()
        path.addEllipse(QRectF(
            -field.radius_x, -radius_y,
            field.radius_x * 2, radius_y * 2,
        ))
        transform = QTransform()
        transform.translate(field.origin_x, field.origin_y)
        transform.rotate(field.rotation)
        return transform.map(path)

    def _radial_uniform_image(
        self, obj: ColorFillGradientObject,
    ) -> tuple[QImage, QRectF] | None:
        field = obj.radial_field
        path = self._radial_boundary_path(field)
        bounds = path.boundingRect()
        if bounds.isEmpty():
            return None
        width, height = self._gradient_grid_for_preview(bounds)
        path_signature = self._gradient_path_signature(path)
        scalar_key = (
            "radial-uniform", path_signature, width, height,
            round(field.distance, 4),
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.ramp, cached_bounds, scalar_key
            )
        _amount, _signed, boundary = self._path_projection_arrays(
            path, bounds, width, height
        )
        coverage = self._path_coverage(path, bounds, width, height)
        scalar = np.clip(
            boundary / max(field.distance, 0.001), 0.0, 1.0
        )
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.ramp, bounds, scalar_key
        )

    def _line_gradient_image(
        self, obj: ColorFillGradientObject, path: QPainterPath,
        bounds: QRectF,
    ) -> tuple[QImage, QRectF] | None:
        if bounds.isEmpty():
            return None
        width, height = self._gradient_grid_for_preview(bounds)
        field = obj.line_field
        signature = self._gradient_path_signature(path)
        scalar_key = (
            "line", signature, width, height, field.direction_mode,
            field.reverse_direction,
            round(field.perpendicular_distance, 4),
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.ramp, cached_bounds, scalar_key
            )
        amount, signed, _distance = self._path_projection_arrays(
            path, bounds, width, height
        )
        if field.direction_mode == "perpendicular":
            direction = 1.0 if field.perpendicular_distance > 0 else -1.0
            scalar = np.clip(
                signed * direction / abs(field.perpendicular_distance),
                0.0, 1.0,
            )
        else:
            scalar = amount
        if field.reverse_direction:
            scalar = 1.0 - scalar
        coverage = np.ones_like(scalar, dtype=bool)
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.ramp, bounds, scalar_key
        )

    def _radial_outward_image(
        self, obj: ColorFillGradientObject,
    ) -> tuple[QImage, QRectF] | None:
        field = obj.radial_field
        radius_y = field.radius_y if field.ellipse_enabled else field.radius_x
        extent_x = field.radius_x + field.distance
        extent_y = radius_y + field.distance
        radius = math.hypot(extent_x, extent_y)
        bounds = QRectF(
            field.origin_x - radius, field.origin_y - radius,
            radius * 2, radius * 2,
        )
        width, height = self._gradient_grid_for_preview(bounds)
        scalar_key = (
            "radial-out", width, height,
            round(field.origin_x, 4), round(field.origin_y, 4),
            round(field.radius_x, 4), round(radius_y, 4),
            round(field.rotation, 4), round(field.distance, 4),
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.ramp, cached_bounds, scalar_key
            )
        grid_x, grid_y = self._gradient_coordinates(bounds, width, height)
        angle = math.radians(-field.rotation)
        dx, dy = grid_x - field.origin_x, grid_y - field.origin_y
        local_x = dx * math.cos(angle) - dy * math.sin(angle)
        local_y = dx * math.sin(angle) + dy * math.cos(angle)
        normalized = np.sqrt(
            (local_x / field.radius_x) ** 2
            + (local_y / radius_y) ** 2
        )
        ray_length = np.hypot(local_x, local_y)
        boundary_length = np.divide(
            ray_length, normalized,
            out=np.zeros_like(ray_length), where=normalized > 1e-6,
        )
        outside_distance = np.maximum(0.0, ray_length - boundary_length)
        scalar = np.clip(
            outside_distance / max(field.distance, 0.001),
            0.0, 1.0,
        )
        coverage = normalized >= 1.0
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.ramp, bounds, scalar_key
        )

    def _render_color_gradient(
        self, painter: QPainter, obj: ColorFillGradientObject,
        local_visible: QRectF,
    ) -> None:
        parent_path = self.layer_effective_path(obj.parent_layer_id)
        if parent_path.isEmpty():
            return
        if obj.field_type == "line":
            rendered = self._line_gradient_image(
                obj, self.bound_path(obj.line_field.geometry),
                parent_path.boundingRect(),
            )
            if rendered is not None:
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver
                )
                painter.drawImage(rendered[1], rendered[0])
            return
        if obj.field_type == "radial":
            field = obj.radial_field
            if field.reverse_direction:
                rendered = self._radial_outward_image(obj)
                if rendered is not None:
                    painter.setCompositionMode(
                        QPainter.CompositionMode.CompositionMode_SourceOver
                    )
                    painter.drawImage(rendered[1], rendered[0])
                return
            if field.uniform:
                rendered = self._radial_uniform_image(obj)
                if rendered is not None:
                    painter.setCompositionMode(
                        QPainter.CompositionMode.CompositionMode_SourceOver
                    )
                    painter.drawImage(rendered[1], rendered[0])
                return
            center_x, center_y = field.center()
            angle = math.radians(-field.rotation)
            dx, dy = center_x - field.origin_x, center_y - field.origin_y
            radius_y = (
                field.radius_y
                if field.ellipse_enabled else field.radius_x
            )
            focal = QPointF(
                (dx * math.cos(angle) - dy * math.sin(angle))
                / field.radius_x,
                (dx * math.sin(angle) + dy * math.cos(angle))
                / radius_y,
            )
            gradient = QRadialGradient(QPointF(0, 0), 1.0, focal)
            gradient.setSpread(QRadialGradient.Spread.PadSpread)
            self._apply_ramp_stops(gradient, obj.ramp, reverse=True)
            brush = QBrush(gradient)
            transform = QTransform()
            transform.translate(field.origin_x, field.origin_y)
            transform.rotate(field.rotation)
            transform.scale(field.radius_x, radius_y)
            brush.setTransform(transform)
            painter.fillRect(local_visible, brush)
            return
        rendered = self._shape_gradient_image(obj, parent_path)
        if rendered is not None:
            image, target = rendered
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver
            )
            painter.drawImage(target, image)

    def _render_gradient(
        self, painter: QPainter, obj: GradientObject,
        local_visible: QRectF,
    ) -> None:
        if isinstance(obj, ColorFillGradientObject):
            self._render_color_gradient(painter, obj, local_visible)

    def _render_object(
        self, painter: QPainter, obj: DocumentObject, parent_opacity: float,
        local_visible: QRectF,
    ) -> None:
        if obj.object_id == self._render_excluded_object_id:
            return
        if (
            not self._rendering_compound_references
            and obj.geometry_reference == "compound"
            and self.chapter.closest_compound_ancestor(
                obj.parent_layer_id, include_self=True
            ) is not None
        ):
            return
        if not obj.visible:
            return
        if (
            isinstance(obj, GradientObject)
            and self._is_outward_gradient(obj)
            and not self._rendering_outward_gradient
        ):
            return
        painter.save()
        opacity = (
            parent_opacity
            if obj.opacity_locked else parent_opacity * obj.opacity
        )
        if obj.object_id == self._live_underlay_object_id:
            opacity *= 1.0 - self._live_underlay_amount
        painter.setOpacity(opacity)
        if isinstance(obj, VectorDrawingObject):
            self._render_vector_drawing(painter, obj)
        elif isinstance(obj, GradientObject):
            self._render_gradient(painter, obj, local_visible)
        elif isinstance(obj, RasterObject):
            self._render_raster_content(
                painter, obj, local_visible, use_transform_preview=True
            )
        elif isinstance(obj, TextObject):
            self._draw_text_object(painter, obj)
        painter.restore()

    def _render_raster_content(
        self, painter: QPainter, obj: RasterObject, local_visible: QRectF,
        *, use_transform_preview: bool,
    ) -> None:
        preview = (
            use_transform_preview
            and obj.object_id == self.selected_object_id
            and self._transform_preview_quad is not None
        )
        if preview and (
            self._transform_drag_mode == "translate"
            and self._transform_start_quad is not None
        ):
            dx = (
                self._transform_preview_quad[0][0]
                - self._transform_start_quad[0][0]
            )
            dy = (
                self._transform_preview_quad[0][1]
                - self._transform_start_quad[0][1]
            )
            painter.translate(obj.x + dx, obj.y + dy)
            object_visible = local_visible.translated(
                -obj.x - dx, -obj.y - dy
            )
            for (tile_x, tile_y), image in self.tiles.iter_tiles(
                obj.object_id, object_visible
            ):
                painter.drawImage(
                    tile_x * obj.tile_size, tile_y * obj.tile_size, image
                )
            return
        if preview:
            source = QRectF(*obj.interaction_rect).translated(obj.x, obj.y)
            transform = self._quad_transform(
                source, self._transform_preview_quad
            )
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.setTransform(transform, True)
            inverse, invertible = transform.inverted()
            object_visible = (
                inverse.mapRect(local_visible).translated(-obj.x, -obj.y)
                if invertible else None
            )
            for (tile_x, tile_y), image in self.tiles.iter_tiles(
                obj.object_id, object_visible
            ):
                painter.drawImage(
                    obj.x + tile_x * obj.tile_size,
                    obj.y + tile_y * obj.tile_size,
                    image,
                )
            return
        painter.translate(obj.x, obj.y)
        object_visible = local_visible.translated(-obj.x, -obj.y)
        for (tile_x, tile_y), image in self.tiles.iter_tiles(
            obj.object_id, object_visible
        ):
            painter.drawImage(
                tile_x * obj.tile_size, tile_y * obj.tile_size, image
            )

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
        reference = parent
        if obj.geometry_reference == "compound":
            reference = (
                self.chapter.closest_compound_ancestor(
                    obj.parent_layer_id, include_self=True
                ) or parent
            )
        path = (
            self._layer_operand_path(reference)
            if (
                obj.geometry_reference == "direct"
                and reference.compound_enabled
            )
            else self.layer_effective_path(reference.layer_id)
        )
        bounds = path.boundingRect()
        parent_x, parent_y = self.chapter.layer_world_translation(
            parent.layer_id
        )
        reference_x, reference_y = self.chapter.layer_world_translation(
            reference.layer_id
        )
        left = bounds.left() + reference_x - parent_x
        top = bounds.top() + reference_y - parent_y
        width, height = bounds.width(), bounds.height()
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

    @staticmethod
    def _quad_to_quad_transform(
        source: list[tuple[float, float]],
        destination: list[tuple[float, float]],
    ) -> QTransform:
        source_polygon = QPolygonF([QPointF(*point) for point in source])
        destination_polygon = QPolygonF([
            QPointF(*point) for point in destination
        ])
        transform = QTransform.quadToQuad(
            source_polygon, destination_polygon
        )
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
        offset = self._text_vertical_offset(obj, document, source.height())
        transform = self._quad_transform(source, self._text_quad(obj))
        painter.save()
        painter.setTransform(transform, True)
        painter.setClipRect(source)
        painter.translate(0, offset)
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
                    transform.map(self.layer_effective_path(layer.layer_id)),
                    Qt.IntersectClip,
                )
        start, end, size, color = self._predictive
        preview = QColor(color)
        preview.setAlpha(round(110 * color.alphaF()))
        pen = QPen(preview, size, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(start, end)
        painter.restore()

    def _draw_live_vector_gesture(self, painter: QPainter) -> None:
        if (
            self._vector_gesture_mode == "simplify"
            and self._vector_sweep
            and self._selected_vector_drawing() is not None
        ):
            drawing = self._selected_vector_drawing()
            layer_x, layer_y = self.chapter.layer_world_translation(
                drawing.parent_layer_id
            )
            radius = 12.0 / max(self.scale, 0.05)
            painter.save()
            painter.translate(layer_x + drawing.x, layer_y + drawing.y)
            overlay = QColor(255, 139, 30, 72)
            sweep = self._vector_simplify_overlay or self._vector_sweep
            if len(sweep) == 1:
                painter.setPen(Qt.NoPen)
                painter.setBrush(overlay)
                painter.drawEllipse(
                    QPointF(*sweep[0].point), radius, radius
                )
            else:
                path = QPainterPath(QPointF(*sweep[0].point))
                for sample in sweep[1:]:
                    path.lineTo(QPointF(*sample.point))
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(
                    overlay, radius * 2, Qt.SolidLine,
                    Qt.RoundCap, Qt.RoundJoin,
                ))
                painter.drawPath(path)
            painter.restore()
            return
        if (
            self._vector_gesture_mode != "pencil"
            or not self._vector_samples
            or self._active_vector_drawing() is None
        ):
            return
        drawing = self._active_vector_drawing()
        layer_x, layer_y = self.chapter.layer_world_translation(
            drawing.parent_layer_id
        )
        painter.save()
        painter.translate(layer_x + drawing.x, layer_y + drawing.y)
        color = QColor(self.primary_color)
        if len(self._vector_samples) == 1:
            sample = self._vector_samples[0]
            width, opacity = self._vector_pressure_values(sample.pressure)
            color.setAlphaF(color.alphaF() * opacity)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(
                QPointF(*sample.point), width / 2, width / 2
            )
        else:
            for first, second in zip(
                self._vector_samples, self._vector_samples[1:]
            ):
                width_a, opacity_a = self._vector_pressure_values(
                    first.pressure
                )
                width_b, opacity_b = self._vector_pressure_values(
                    second.pressure
                )
                segment_color = QColor(color)
                segment_color.setAlphaF(
                    color.alphaF() * (opacity_a + opacity_b) / 2
                )
                painter.setPen(QPen(
                    segment_color,
                    (width_a + width_b) / 2,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                ))
                painter.drawLine(
                    QPointF(*first.point), QPointF(*second.point)
                )
        painter.restore()

    def _draw_simplify_hover(self, painter: QPainter) -> None:
        if self.tool != ToolKind.VECTOR_SIMPLIFY:
            return
        center = self._tablet_hover_widget or self._pointer_hover_widget
        if center is None:
            return
        painter.save()
        painter.setTransform(QTransform())
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#ff8b1e"), 1.5))
        painter.drawEllipse(center, 12.0, 12.0)
        painter.restore()

    def _draw_tablet_hover(self, painter: QPainter) -> None:
        if (
            self._tablet_hover_widget is None
            or self._tablet_tool_active
            or self._nav_mode is not None
            or self.tool not in {
                ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER
            }
        ):
            return
        size = (
            self.settings.active_eraser_pixels()
            if self.tool == ToolKind.RASTER_ERASER
            else self.settings.pencil_size()
        )
        radius = max(2.0, size * self.scale / 2)
        center = self._tablet_hover_widget
        painter.save()
        painter.setTransform(QTransform())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#E8F5FF"), 1.25))
        painter.drawEllipse(center, radius, radius)
        cross = min(6.0, max(3.0, radius * 0.35))
        painter.drawLine(
            QPointF(center.x() - cross, center.y()),
            QPointF(center.x() + cross, center.y()),
        )
        painter.drawLine(
            QPointF(center.x(), center.y() - cross),
            QPointF(center.x(), center.y() + cross),
        )
        painter.restore()

    def _draw_selection(self, painter: QPainter) -> None:
        if self.tool in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
        }:
            self._draw_drawing_selection(painter)
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
                painter.drawPath(self.layer_effective_path(active.layer_id))
            painter.restore()
        pen = QPen(QColor("#36b7ff"), 2 / max(self.scale, 0.05), Qt.DashLine)
        painter.setPen(pen)
        selected_object = self.chapter.objects.get(self.selected_id)
        if self.selected_kind == "layer":
            layer = self.chapter.layers.get(self.selected_id)
            if layer:
                world_x, world_y = self.chapter.layer_world_translation(layer.layer_id)
                painter.translate(world_x, world_y)
                if layer.bound is not None:
                    painter.drawPath(self.layer_effective_path(layer.layer_id))
                if (
                    self.tool == ToolKind.SHAPE_EDIT
                    and layer.bound is not None
                ):
                    if (
                        self._geometry_transform_target
                        == ("layer", layer.layer_id)
                        and self._transform_preview_quad is not None
                    ):
                        control_quad = self._transform_preview_quad
                    else:
                        left, top, width, height = layer.bound.bbox()
                        control_quad = self._rect_quad(QRectF(
                            left, top, max(1.0, width),
                            max(1.0, height)
                        ))
                    self._draw_transform_controls(
                        painter, control_quad,
                    )
                if self.tool == ToolKind.BOUND_EDIT and layer.bound is not None:
                    self._draw_shape_edit_handles(painter, layer)
        else:
            quad = self._selected_world_quad()
            if quad:
                polygon = QPolygonF([QPointF(*point) for point in quad])
                painter.drawPolygon(polygon)
                if (
                    isinstance(selected_object, RasterObject)
                    or self.tool == ToolKind.TRANSFORM
                    or (
                        self.tool == ToolKind.SHAPE_EDIT
                        and isinstance(selected_object, GradientObject)
                        and selected_object.field_type
                        in {"line", "radial"}
                    )
                    or (
                        self.tool == ToolKind.TEXT_EDIT
                        and isinstance(selected_object, TextObject)
                        and selected_object.layout_mode == "free"
                    )
                ):
                    self._draw_transform_controls(painter, quad)
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
                if (
                    isinstance(selected_object, VectorDrawingObject)
                    and self.tool in {
                        ToolKind.VECTOR_EDIT, ToolKind.VECTOR_REDRAW,
                        ToolKind.VECTOR_SIMPLIFY,
                        ToolKind.DRAW_SELECT_RECT,
                        ToolKind.DRAW_SELECT_LASSO,
                        ToolKind.DRAW_SELECT_STROKE,
                    }
                ):
                    self._draw_vector_edit_handles(
                        painter, selected_object
                    )
                if (
                    self.tool == ToolKind.SHAPE_EDIT
                    and isinstance(selected_object, GradientObject)
                ):
                    self._draw_gradient_edit_handles(
                        painter, selected_object
                    )
        painter.restore()

    def _gradient_local_to_world(
        self, obj: GradientObject, point: tuple[float, float],
    ) -> QPointF:
        x, y = self.chapter.layer_world_translation(obj.parent_layer_id)
        return QPointF(point[0] + x, point[1] + y)

    def _gradient_world_to_local(
        self, obj: GradientObject, point: QPointF,
    ) -> QPointF:
        x, y = self.chapter.layer_world_translation(obj.parent_layer_id)
        return QPointF(point.x() - x, point.y() - y)

    @staticmethod
    def _rotated_gradient_point(
        origin: tuple[float, float], vector: tuple[float, float],
        rotation: float,
    ) -> tuple[float, float]:
        angle = math.radians(rotation)
        cosine, sine = math.cos(angle), math.sin(angle)
        return (
            origin[0] + vector[0] * cosine - vector[1] * sine,
            origin[1] + vector[0] * sine + vector[1] * cosine,
        )

    def _gradient_control_points(
        self, obj: GradientObject,
    ) -> dict[str, QPointF]:
        result: dict[str, QPointF] = {}
        if obj.field_type == "line":
            geometry = obj.line_field.geometry
            for node in geometry.nodes:
                result[f"node:{node.node_id}"] = (
                    self._gradient_local_to_world(obj, node.position)
                )
                if node.incoming is not None:
                    result[f"incoming:{node.node_id}"] = (
                        self._gradient_local_to_world(obj, node.incoming)
                    )
                if node.outgoing is not None:
                    result[f"outgoing:{node.node_id}"] = (
                        self._gradient_local_to_world(obj, node.outgoing)
                    )
            selected = next((
                node for node in geometry.nodes
                if node.node_id == self._selected_shape_node_id
            ), None)
            if selected is not None:
                for name, point in self._shape_gizmo_positions(
                    geometry, selected, geometry_only=True
                ).items():
                    result[f"{name}:{selected.node_id}"] = (
                        self._gradient_local_to_world(obj, point.toTuple())
                    )
            for index in range(max(0, len(geometry.nodes) - 1)):
                point = self._shape_segment_point(geometry, index, 0.5)
                result[f"insert:{index}"] = (
                    self._gradient_local_to_world(obj, point.toTuple())
                )
            if (
                obj.line_field.direction_mode == "perpendicular"
                and len(geometry.nodes) >= 2
            ):
                path = self.bound_path(geometry)
                percent = path.percentAtLength(path.length() / 2)
                midpoint = path.pointAtPercent(percent)
                angle = math.radians(
                    -path.angleAtPercent(percent) + 90
                )
                distance = obj.line_field.perpendicular_distance
                result["distance:"] = self._gradient_local_to_world(
                    obj,
                    (
                        midpoint.x() + math.cos(angle) * distance,
                        midpoint.y() + math.sin(angle) * distance,
                    ),
                )
            return result
        if obj.field_type == "radial":
            field = obj.radial_field
            origin = (field.origin_x, field.origin_y)
            result["origin:"] = self._gradient_local_to_world(obj, origin)
            result["radius_x:"] = self._gradient_local_to_world(
                obj, self._rotated_gradient_point(
                    origin, (field.radius_x, 0), field.rotation
                )
            )
            if field.ellipse_enabled:
                result["radius_y:"] = self._gradient_local_to_world(
                    obj, self._rotated_gradient_point(
                        origin, (0, field.radius_y), field.rotation
                    )
                )
                result["rotate:"] = self._gradient_local_to_world(
                    obj, self._rotated_gradient_point(
                        origin, (0, -field.radius_y - 35 / self.scale),
                        field.rotation,
                    )
                )
            if field.reverse_direction or field.uniform:
                signed_distance = (
                    field.distance
                    if field.reverse_direction else -field.distance
                )
                result["distance:"] = self._gradient_local_to_world(
                    obj, self._rotated_gradient_point(
                        origin,
                        (field.radius_x + signed_distance, 0),
                        field.rotation,
                    )
                )
            else:
                result["center:"] = self._gradient_local_to_world(
                    obj, field.center()
                )
            result["toggle:"] = self._gradient_local_to_world(
                obj, self._rotated_gradient_point(
                    origin,
                    (-field.radius_x - 45 / self.scale, 0),
                    field.rotation,
                )
            )
            return result
        path = self.layer_effective_path(obj.parent_layer_id)
        if obj.shape_field.reverse_direction or obj.shape_field.uniform:
            bounds = path.boundingRect()
            signed_distance = (
                obj.shape_field.distance
                if obj.shape_field.reverse_direction
                else -obj.shape_field.distance
            )
            result["distance:"] = self._gradient_local_to_world(
                obj, (
                    bounds.right() + signed_distance,
                    bounds.center().y(),
                )
            )
        else:
            center = self._shape_gradient_center(obj, path)
            result["center:"] = self._gradient_local_to_world(
                obj, center.toTuple()
            )
        return result

    def _draw_gradient_edit_handles(
        self, painter: QPainter, obj: GradientObject,
    ) -> None:
        scale = max(self.scale, 0.05)
        controls = self._gradient_control_points(obj)
        painter.save()
        painter.setPen(QPen(QColor("#ff9f22"), 2 / scale))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if obj.field_type == "line":
            layer_x, layer_y = self.chapter.layer_world_translation(
                obj.parent_layer_id
            )
            painter.save()
            painter.translate(layer_x, layer_y)
            painter.drawPath(self.bound_path(obj.line_field.geometry))
            selected_id = self._selected_shape_node_id
            for node in obj.line_field.geometry.nodes:
                self._draw_path_node_handle(
                    painter, node, node.node_id == selected_id
                )
            action_radius = 8 * SHAPE_CONTROL_SCALE / scale
            for index in range(
                max(0, len(obj.line_field.geometry.nodes) - 1)
            ):
                insert = self._shape_segment_point(
                    obj.line_field.geometry, index, 0.5
                )
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(
                    insert, action_radius * 0.65, action_radius * 0.65
                )
            selected = next((
                node for node in obj.line_field.geometry.nodes
                if node.node_id == selected_id
            ), None)
            if selected is not None and False:
                offset = 42 / scale
                type_point = QPointF(
                    selected.x + offset, selected.y - offset
                )
                delete_point = QPointF(
                    selected.x + offset, selected.y + offset
                )
                type_rect = QRectF(
                    type_point.x() - action_radius * 2.2,
                    type_point.y() - action_radius,
                    action_radius * 4.4, action_radius * 2,
                )
                painter.drawRoundedRect(
                    type_rect, action_radius / 2, action_radius / 2
                )
                painter.drawText(
                    type_rect, Qt.AlignmentFlag.AlignCenter,
                    "Bézier"
                    if selected.point_type == "bezier" else "Vector",
                )
                painter.drawEllipse(
                    delete_point, action_radius, action_radius
                )
                cross = action_radius * 0.55
                painter.drawLine(
                    delete_point + QPointF(-cross, -cross),
                    delete_point + QPointF(cross, cross),
                )
                painter.drawLine(
                    delete_point + QPointF(-cross, cross),
                    delete_point + QPointF(cross, -cross),
                )
            if selected is not None:
                self._draw_selected_shape_gizmos(
                    painter, obj.line_field.geometry, selected,
                    geometry_only=True,
                )
            painter.restore()
            painter.restore()
            return
        if obj.field_type == "radial":
            field = obj.radial_field
            radius_y = (
                field.radius_y
                if field.ellipse_enabled else field.radius_x
            )
            painter.save()
            origin = controls["origin:"]
            painter.translate(origin)
            painter.rotate(field.rotation)
            painter.drawEllipse(QRectF(
                -field.radius_x, -radius_y,
                field.radius_x * 2, radius_y * 2,
            ))
            painter.restore()
        radius = 8 * SHAPE_CONTROL_SCALE / scale
        for key, point in controls.items():
            if key == "toggle:":
                painter.drawRoundedRect(QRectF(
                    point.x() - radius * 1.8, point.y() - radius,
                    radius * 3.6, radius * 2,
                ), radius / 2, radius / 2)
                painter.drawText(
                    QRectF(
                        point.x() - radius * 1.8, point.y() - radius,
                        radius * 3.6, radius * 2,
                    ),
                    Qt.AlignmentFlag.AlignCenter,
                    "Ellipse" if obj.radial_field.ellipse_enabled else "Circle",
                )
            elif key == "center:":
                painter.drawEllipse(point, radius, radius)
                cross = radius * 0.6
                painter.drawLine(
                    point + QPointF(-cross, 0),
                    point + QPointF(cross, 0),
                )
                painter.drawLine(
                    point + QPointF(0, -cross),
                    point + QPointF(0, cross),
                )
            else:
                painter.drawEllipse(point, radius, radius)
        painter.restore()

    def _gradient_control_hit(
        self, obj: GradientObject, point: QPointF,
    ) -> tuple[str, str] | None:
        tolerance = 15 * SHAPE_CONTROL_SCALE / max(self.scale, 0.05)
        controls = self._gradient_control_points(obj)
        # Action and Bézier controls win over anchors.
        priority = {
            "toggle": 0, "incoming": 1, "outgoing": 1,
            "center": 2, "rotate": 2, "radius_x": 2,
            "radius_y": 2, "origin": 3, "node": 4,
            "type": 0, "delete": 0, "lock": 0, "roundness": 0,
            "distance": 2, "insert": 5,
        }
        hits: list[tuple[int, float, str, str]] = []
        for key, candidate in controls.items():
            kind, node_id = key.split(":", 1)
            distance = math.dist(
                point.toTuple(), candidate.toTuple()
            )
            if kind == "type":
                type_hit = QRectF(
                    candidate.x() - 32 / max(self.scale, 0.05),
                    candidate.y() - 14 / max(self.scale, 0.05),
                    64 / max(self.scale, 0.05),
                    28 / max(self.scale, 0.05),
                )
                if type_hit.contains(point):
                    hits.append((
                        priority[kind], distance, kind, node_id
                    ))
                continue
            if (
                obj.field_type == "radial"
                and obj.radial_field.center_auto
                and kind == "center"
                and distance > 7 * SHAPE_CONTROL_SCALE / max(
                    self.scale, 0.05
                )
            ):
                continue
            if (
                obj.field_type == "radial"
                and obj.radial_field.center_auto
                and kind == "origin"
                and distance <= 7 * SHAPE_CONTROL_SCALE / max(
                    self.scale, 0.05
                )
            ):
                continue
            if distance <= tolerance:
                hits.append((
                    priority.get(kind, 9), distance, kind, node_id
                ))
        if not hits:
            return None
        _priority, _distance, kind, node_id = min(hits)
        return kind, node_id

    def _begin_gradient_edit(
        self, obj: GradientObject, point: QPointF,
    ) -> bool:
        hit = self._gradient_control_hit(obj, point)
        if hit is None:
            return False
        kind, node_id = hit
        if kind in {"type", "delete", "insert", "lock"}:
            before = self.chapter.to_dict()
            geometry = obj.line_field.geometry
            if kind == "insert":
                index = int(node_id)
                node = self._split_shape_segment(geometry, index, 0.5)
                geometry.nodes.insert(index + 1, node)
                self._selected_shape_node_id = node.node_id
                label = "Insert gradient path point"
            else:
                node = next((
                    candidate for candidate in geometry.nodes
                    if candidate.node_id == node_id
                ), None)
                if node is None:
                    return False
                if kind == "type":
                    self._toggle_shape_node_type(geometry, node)
                    label = "Change gradient path point type"
                elif kind == "lock":
                    self._toggle_shape_node_lock(geometry, node)
                    label = "Lock gradient Bézier handles"
                else:
                    if len(geometry.nodes) <= 2:
                        return True
                    geometry.nodes.remove(node)
                    self._selected_shape_node_id = ""
                    label = "Delete gradient path point"
            geometry.normalize_bezier_handles()
            obj.touch_revision()
            self._push_immediate_shape_change(before, label)
            return True
        if kind == "toggle":
            before = self.chapter.to_dict()
            obj.radial_field.ellipse_enabled = (
                not obj.radial_field.ellipse_enabled
            )
            obj.radial_field.validate()
            obj.touch_revision()
            self._push_immediate_shape_change(
                before, "Toggle circle / ellipse gradient"
            )
            return True
        self._model_before = self.chapter.to_dict()
        self._active_gradient_control = hit
        self._gradient_preview_active = True
        self._shape_control_dragged = False
        self._drag_start_doc = QPointF(point)
        if kind == "node":
            self._selected_shape_node_id = node_id
        self.setToolTip({
            "node": "Move gradient path point",
            "incoming": "Move incoming Bézier control",
            "outgoing": "Move outgoing Bézier control",
            "origin": "Move radial gradient origin",
            "radius_x": "Change horizontal gradient radius",
            "radius_y": "Change vertical gradient radius",
            "rotate": "Rotate ellipse gradient",
            "center": (
                "Move gradient center; double-click resets automatic centering"
            ),
            "roundness": "Drag to adjust roundness; click to toggle",
            "lock": "Lock or unlock Bézier handles",
            "distance": "Set gradient distance",
        }.get(kind, "Edit gradient"))
        return True

    def _update_gradient_edit(
        self, obj: GradientObject, world_point: QPointF,
    ) -> None:
        if self._active_gradient_control is None:
            return
        kind, node_id = self._active_gradient_control
        snapped_world = (
            self._snap(world_point, obj.parent_layer_id)
            if self.settings.snap_to_grid else world_point
        )
        local = self._gradient_world_to_local(obj, snapped_world)
        if obj.field_type == "line":
            if kind == "distance":
                path = self.bound_path(obj.line_field.geometry)
                percent = path.percentAtLength(path.length() / 2)
                midpoint = path.pointAtPercent(percent)
                angle = math.radians(
                    -path.angleAtPercent(percent) + 90
                )
                normal = QPointF(math.cos(angle), math.sin(angle))
                value = QPointF.dotProduct(local - midpoint, normal)
                obj.line_field.perpendicular_distance = (
                    value if abs(value) >= 1.0
                    else 1.0 if value >= 0 else -1.0
                )
                obj.line_field.validate()
                obj.touch_revision()
                self.documentChanged.emit(QRectF())
                self.update()
                return
            node = next((
                candidate for candidate in obj.line_field.geometry.nodes
                if candidate.node_id == node_id
            ), None)
            if node is None:
                return
            if kind == "node":
                dx, dy = local.x() - node.x, local.y() - node.y
                node.x, node.y = local.x(), local.y()
                if node.incoming is not None:
                    node.incoming = (
                        node.incoming[0] + dx, node.incoming[1] + dy
                    )
                if node.outgoing is not None:
                    node.outgoing = (
                        node.outgoing[0] + dx, node.outgoing[1] + dy
                    )
            elif kind in {"incoming", "outgoing"}:
                self._move_shape_bezier_handle(
                    obj.line_field.geometry, node, kind,
                    (local.x(), local.y()),
                )
            elif kind == "roundness":
                if math.dist(
                    world_point.toTuple(), self._drag_start_doc.toTuple()
                ) > 3 / max(self.scale, 0.05):
                    self._shape_control_dragged = True
                if self._shape_control_dragged:
                    position = QPointF(node.x, node.y)
                    node.roundness = min(
                        self._maximum_shape_roundness(
                            obj.line_field.geometry, node
                        ),
                        math.dist(
                            position.toTuple(), local.toTuple()
                        ),
                    )
                    if node.roundness < 1e-6:
                        node.roundness = 0.0
                    node.roundness_enabled = True
            obj.line_field.geometry.normalize_bezier_handles()
        elif obj.field_type == "radial":
            field = obj.radial_field
            if kind == "origin":
                dx = local.x() - field.origin_x
                dy = local.y() - field.origin_y
                field.origin_x, field.origin_y = local.x(), local.y()
                if (
                    not field.center_auto
                    and field.manual_center is not None
                ):
                    field.manual_center = (
                        field.manual_center[0] + dx,
                        field.manual_center[1] + dy,
                    )
            elif kind == "center":
                field.center_auto = False
                field.manual_center = (local.x(), local.y())
            elif kind == "distance":
                dx = local.x() - field.origin_x
                dy = local.y() - field.origin_y
                angle = math.radians(-field.rotation)
                rotated_x = dx * math.cos(angle) - dy * math.sin(angle)
                field.distance = max(1.0, (
                    rotated_x - field.radius_x
                    if field.reverse_direction
                    else field.radius_x - rotated_x
                ))
            else:
                dx = local.x() - field.origin_x
                dy = local.y() - field.origin_y
                angle = math.radians(-field.rotation)
                rotated_x = dx * math.cos(angle) - dy * math.sin(angle)
                rotated_y = dx * math.sin(angle) + dy * math.cos(angle)
                if kind == "radius_x":
                    field.radius_x = max(0.001, abs(rotated_x))
                elif kind == "radius_y":
                    field.radius_y = max(0.001, abs(rotated_y))
                elif kind == "rotate":
                    field.rotation = math.degrees(
                        math.atan2(dy, dx)
                    ) + 90
            field.validate()
        elif kind == "center":
            obj.shape_field.center_auto = False
            obj.shape_field.manual_center = (local.x(), local.y())
            obj.shape_field.validate()
        elif kind == "distance":
            bounds = self.layer_effective_path(
                obj.parent_layer_id
            ).boundingRect()
            obj.shape_field.distance = max(1.0, (
                local.x() - bounds.right()
                if obj.shape_field.reverse_direction
                else bounds.right() - local.x()
            ))
            obj.shape_field.validate()
        obj.touch_revision()
        self.documentChanged.emit(QRectF())
        self.update()

    def _reset_gradient_center(
        self, obj: GradientObject, point: QPointF,
    ) -> bool:
        hit = self._gradient_control_hit(obj, point)
        if hit is None or hit[0] != "center":
            return False
        before = self.chapter.to_dict()
        if obj.field_type == "radial":
            obj.radial_field.center_auto = True
            obj.radial_field.manual_center = None
        elif obj.field_type == "parent_shape":
            obj.shape_field.center_auto = True
            obj.shape_field.manual_center = None
        else:
            return False
        obj.touch_revision()
        self._push_immediate_shape_change(
            before, "Reset gradient center"
        )
        return True

    def _transform_control_points(
        self, quad: list[tuple[float, float]],
        pivot: QPointF | None = None,
    ) -> tuple[list[tuple[float, float]], QPointF, QPointF]:
        handles = self._quad_handles(quad)
        center = QPointF(
            sum(point[0] for point in quad) / 4,
            sum(point[1] for point in quad) / 4,
        )
        top = QPointF(*self._edge_midpoints(quad)[0])
        direction = top - center
        length = max(1e-6, math.hypot(direction.x(), direction.y()))
        rotate = top + direction / length * (
            30 / max(self.scale, 0.05)
        )
        return handles, rotate, pivot or center

    def _draw_transform_controls(
        self, painter: QPainter, quad: list[tuple[float, float]],
    ) -> None:
        handles, rotate, pivot = self._transform_control_points(
            quad, self._transform_pivot
        )
        radius = 7 / max(self.scale, 0.05)
        painter.setBrush(QColor("#f5f5f5"))
        for point in handles:
            painter.drawEllipse(QPointF(*point), radius, radius)
        top = QPointF(*self._edge_midpoints(quad)[0])
        painter.drawLine(top, rotate)
        painter.drawEllipse(rotate, radius, radius)
        cross = 8 / max(self.scale, 0.05)
        painter.drawLine(
            QPointF(pivot.x() - cross, pivot.y()),
            QPointF(pivot.x() + cross, pivot.y()),
        )
        painter.drawLine(
            QPointF(pivot.x(), pivot.y() - cross),
            QPointF(pivot.x(), pivot.y() + cross),
        )

    def _draw_vector_edit_handles(
        self, painter: QPainter, drawing: VectorDrawingObject,
    ) -> None:
        layer_x, layer_y = self.chapter.layer_world_translation(
            drawing.parent_layer_id
        )
        scale = max(self.scale, 0.05)
        painter.save()
        painter.translate(layer_x + drawing.x, layer_y + drawing.y)
        show_all = (
            (
                self.tool == ToolKind.VECTOR_REDRAW
                and self.settings.vector_redraw_interaction == "manual"
            )
            or self.tool == ToolKind.VECTOR_SIMPLIFY
            or self.tool in {
                ToolKind.DRAW_SELECT_RECT,
                ToolKind.DRAW_SELECT_LASSO,
                ToolKind.DRAW_SELECT_STROKE,
            }
        )
        for stroke in drawing.strokes:
            if (
                not show_all
                and stroke.stroke_id not in self._selected_vector_stroke_ids
            ):
                continue
            if (
                stroke.stroke_id in self._selected_vector_stroke_ids
                or self.tool in {
                    ToolKind.VECTOR_REDRAW, ToolKind.VECTOR_SIMPLIFY,
                }
            ):
                painter.setPen(QPen(
                    QColor("#ff9f2f"), 2.5 / scale, Qt.SolidLine,
                    Qt.RoundCap, Qt.RoundJoin,
                ))
                painter.setBrush(Qt.NoBrush)
                painter.drawPath(self._vector_centerline_path(stroke))
            for point in stroke.points:
                preview = self._selection_vector_preview.get(point.point_id)
                position = (
                    preview.get("position", point.position)
                    if preview is not None else point.position
                )
                selected = (
                    point.point_id in self._selected_vector_point_ids
                    or point.point_id in self._vector_simplify_point_ids
                )
                painter.setPen(QPen(
                    QColor("#ff7417" if selected else "#079bd3"),
                    (3 if selected else 2) / scale,
                ))
                painter.setBrush(QColor("#aeeaff"))
                radius = (7 if selected else 5.5) / scale
                painter.drawEllipse(
                    QPointF(*position), radius, radius
                )
        painter.restore()

    def _draw_path_node_handle(
        self, painter: QPainter, node: PathNode, selected: bool,
        hovered: bool = False,
    ) -> None:
        scale = max(self.scale, 0.05)
        point_color = QColor("#FF7417" if selected else "#0097D7")
        gizmo_color = QColor("#FFBE00" if selected else "#9BDDF0")
        point_radius = 6 * SHAPE_CONTROL_SCALE / scale
        control_radius = 8 * SHAPE_CONTROL_SCALE / scale
        painter.save()
        painter.setBrush(QColor("#ffffff"))
        if hovered and not selected:
            painter.setPen(QPen(
                QColor("#9BDDF0"), 2 * SHAPE_CONTROL_SCALE / scale
            ))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(
                QPointF(node.x, node.y),
                point_radius + 3 * SHAPE_CONTROL_SCALE / scale,
                point_radius + 3 * SHAPE_CONTROL_SCALE / scale,
            )
            painter.setBrush(QColor("#ffffff"))
        if node.point_type == "bezier":
            painter.setPen(QPen(
                gizmo_color, 2 * SHAPE_CONTROL_SCALE / scale
            ))
            for control in (node.incoming, node.outgoing):
                if control is None:
                    continue
                painter.drawLine(QPointF(node.x, node.y), QPointF(*control))
                painter.drawEllipse(
                    QPointF(*control), control_radius, control_radius
                )
            painter.setPen(QPen(
                point_color, 3 * SHAPE_CONTROL_SCALE / scale
            ))
            painter.drawEllipse(
                QPointF(node.x, node.y), point_radius, point_radius
            )
        else:
            painter.setPen(QPen(
                point_color, 3 * SHAPE_CONTROL_SCALE / scale
            ))
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
        *, geometry_only: bool = False,
    ) -> dict[str, QPointF]:
        bound = self._contour_bound_for_node(bound, node)
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
        opposite = normal * (-max(
            24 * SHAPE_CONTROL_SCALE / scale, node.roundness
        ))
        result = {"type": position + QPointF(
            44 * SHAPE_CONTROL_SCALE / scale,
            -34 * SHAPE_CONTROL_SCALE / scale,
        )}
        if not bound.closed and not geometry_only:
            result["thickness"] = position + side
        if self._can_delete_shape_node(bound, node):
            result["delete"] = position + QPointF(
                -44 * SHAPE_CONTROL_SCALE / scale,
                -34 * SHAPE_CONTROL_SCALE / scale,
            )
        may_round = (
            0 < index < len(bound.nodes) - 1 or bound.closed
        ) and (
            node.point_type == "vector"
            or (
                node.point_type == "bezier"
                and not node.handles_locked
                and node.incoming is not None
                and node.outgoing is not None
            )
        )
        if may_round:
            result["roundness"] = position + opposite
        if (
            node.point_type == "bezier"
            and node.incoming is not None
            and node.outgoing is not None
            and (bound.closed or 0 < index < len(bound.nodes) - 1)
        ):
            result["lock"] = position + QPointF(
                58 * SHAPE_CONTROL_SCALE / scale,
                18 * SHAPE_CONTROL_SCALE / scale,
            )
        if (
            not geometry_only
            and not bound.closed
            and index in {0, len(bound.nodes) - 1}
        ):
            result["cap"] = position + QPointF(
                44 * SHAPE_CONTROL_SCALE / scale,
                38 * SHAPE_CONTROL_SCALE / scale,
            )
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
            distance = max(
                18 * SHAPE_CONTROL_SCALE * math.sqrt(2) / scale,
                radius * math.sqrt(2),
            )
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
        if best is None or best[0] > (
            10 * SHAPE_CONTROL_SCALE / max(self.scale, 0.05)
        ):
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
        *, geometry_only: bool = False,
    ) -> dict | None:
        """Resolve one shape target using the same priority as rendering."""
        if bound.additional_contours:
            candidates = [
                PathContour(bound.nodes, bound.closed),
                *bound.additional_contours,
            ]
            interior: dict | None = None
            for contour_index, contour in enumerate(candidates):
                working = BoundGeometry(
                    nodes=contour.nodes, closed=contour.closed,
                    primitive="custom",
                )
                hit = self._shape_hit_test(
                    working, local, include_insert=include_insert,
                    geometry_only=geometry_only,
                )
                if hit is None:
                    continue
                hit["contour_index"] = contour_index
                if hit["kind"] != "interior":
                    return hit
                interior = interior or hit
            return interior
        scale = max(self.scale, 0.05)
        tolerance = 14 * SHAPE_CONTROL_SCALE / scale
        selected = self._selected_shape_node(bound)
        if bound.primitive == "rectangle":
            for index, position in enumerate(
                self._rectangle_radius_positions(bound)
            ):
                if math.dist(
                    (local.x(), local.y()), (position.x(), position.y())
                ) <= 11 * SHAPE_CONTROL_SCALE / scale:
                    return {
                        "kind": "radius", "index": index,
                        "node_id": bound.nodes[index].node_id,
                        "position": position,
                    }
        if selected is not None and self._can_delete_shape_node(bound, selected):
            position = self._shape_gizmo_positions(
                bound, selected, geometry_only=geometry_only
            )["delete"]
            if math.dist(
                (local.x(), local.y()), (position.x(), position.y())
            ) <= 12 * SHAPE_CONTROL_SCALE / scale:
                return {
                    "kind": "gizmo", "name": "delete",
                    "node_id": selected.node_id, "position": position,
                }
        if selected is not None and bound.primitive == "custom":
            for name, position in self._shape_gizmo_positions(
                bound, selected, geometry_only=geometry_only
            ).items():
                if name == "delete":
                    continue
                if name == "type":
                    hit = QRectF(
                        position.x() - 30 / scale,
                        position.y() - 12 / scale,
                        60 / scale, 24 / scale,
                    ).contains(local)
                else:
                    hit_tolerance = (
                        (16 if name == "lock" else 12)
                        * SHAPE_CONTROL_SCALE / scale
                    )
                    hit = math.dist(
                        (local.x(), local.y()),
                        (position.x(), position.y()),
                    ) <= hit_tolerance
                if hit:
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
                radius = (
                    (6 if index < 4 else 4.5)
                    * SHAPE_CONTROL_SCALE / scale
                )
                painter.setPen(QPen(
                    QColor("#9BDDF0") if hovered and not selected else color,
                    (4 if hovered else 3) * SHAPE_CONTROL_SCALE / scale,
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
                        color,
                        (3 if hovered or selected else 2)
                        * SHAPE_CONTROL_SCALE / scale,
                    ))
                    painter.drawLine(
                        QPointF(*bound.nodes[index].position), point
                    )
                    painter.setBrush(QColor("#ffffff"))
                    painter.drawEllipse(
                        point,
                        4.5 * SHAPE_CONTROL_SCALE / scale,
                        4.5 * SHAPE_CONTROL_SCALE / scale,
                    )
            if selected_index >= 0 and self._can_delete_shape_node(bound):
                self._draw_delete_point_gizmo(
                    painter, bound, bound.nodes[selected_index]
                )
            if self._shape_hover_insert is not None:
                point = self._shape_hover_insert[2]
                radius = (
                    3 * SHAPE_CONTROL_SCALE
                    / max(self.scale, 0.05)
                )
                painter.setPen(QPen(
                    QColor("#9BDDF0"),
                    2 * SHAPE_CONTROL_SCALE / max(self.scale, 0.05),
                ))
                painter.setBrush(QColor("#ffffff"))
                painter.drawEllipse(point, radius, radius)
            return
        for contour in bound.iter_contours():
            for node in contour.nodes:
                selected = (
                    node.node_id == self._selected_shape_node_id
                    or node.node_id in self._selected_shape_node_ids
                )
                hovered = (
                    hover.get("kind") == "node"
                    and hover.get("node_id") == node.node_id
                )
                self._draw_path_node_handle(
                    painter, node, selected, hovered
                )
                if node.node_id == self._selected_shape_node_id:
                    self._draw_selected_shape_gizmos(
                        painter, bound, node, layer.shape_style
                    )
        if self._shape_hover_insert is not None:
            point = self._shape_hover_insert[2]
            radius = (
                3 * SHAPE_CONTROL_SCALE / max(self.scale, 0.05)
            )
            painter.setPen(QPen(
                QColor("#9BDDF0"),
                2 * SHAPE_CONTROL_SCALE / max(self.scale, 0.05),
            ))
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(point, radius, radius)

    def _draw_selected_shape_gizmos(
        self, painter: QPainter, bound: BoundGeometry, node: PathNode,
        style: ShapeStyle | None = None,
        *, geometry_only: bool = False,
    ) -> None:
        scale = max(self.scale, 0.05)
        positions = self._shape_gizmo_positions(
            bound, node, geometry_only=geometry_only
        )
        painter.save()
        painter.setPen(QPen(
            QColor("#FFBE00"), 2 * SHAPE_CONTROL_SCALE / scale
        ))
        painter.setBrush(QColor("#ffffff"))
        for name, point in positions.items():
            painter.drawLine(QPointF(node.x, node.y), point)
            radius = 4.5 * SHAPE_CONTROL_SCALE / scale
            if name == "delete":
                painter.drawEllipse(
                    point,
                    radius + SHAPE_CONTROL_SCALE / scale,
                    radius + SHAPE_CONTROL_SCALE / scale,
                )
                arm = 2.5 * SHAPE_CONTROL_SCALE / scale
                painter.drawLine(
                    point + QPointF(-arm, -arm),
                    point + QPointF(arm, arm),
                )
                painter.drawLine(
                    point + QPointF(-arm, arm),
                    point + QPointF(arm, -arm),
                )
            elif name == "lock":
                self._draw_bezier_lock_gizmo(
                    painter, point, node.handles_locked, scale
                )
            elif name == "type":
                badge = QRectF(
                    point.x() - 30 / scale,
                    point.y() - 12 / scale,
                    60 / scale, 24 / scale,
                )
                painter.drawRoundedRect(
                    badge, 6 / scale, 6 / scale
                )
                font = painter.font()
                font.setPixelSize(max(1, round(11 / scale)))
                font.setBold(True)
                painter.setFont(font)
                painter.drawText(
                    badge, Qt.AlignCenter,
                    "Bézier" if node.point_type == "bezier" else "Vector",
                )
            elif name == "cap":
                painter.drawRect(QRectF(
                    point.x() - radius, point.y() - radius,
                    radius * 2, radius * 2,
                ))
                cap = (
                    style.start_cap
                    if style and node is bound.nodes[0]
                    else style.end_cap if style else "round"
                )
                cap_size = 2.5 * SHAPE_CONTROL_SCALE / scale
                painter.drawLine(
                    point + QPointF(0, -cap_size),
                    point + QPointF(0, cap_size),
                )
                if cap == "round":
                    painter.drawArc(QRectF(
                        point.x() - 2 * SHAPE_CONTROL_SCALE / scale,
                        point.y() - cap_size,
                        4 * SHAPE_CONTROL_SCALE / scale,
                        5 * SHAPE_CONTROL_SCALE / scale,
                    ), 90 * 16, 180 * 16)
                elif cap == "point":
                    painter.drawLine(
                        point + QPointF(0, -cap_size),
                        point + QPointF(cap_size, 0),
                    )
                    painter.drawLine(
                        point + QPointF(cap_size, 0),
                        point + QPointF(0, cap_size),
                    )
            else:
                if name == "roundness" and node.roundness_enabled:
                    painter.setBrush(QColor("#FFBE00"))
                painter.drawEllipse(point, radius, radius)
                painter.setBrush(QColor("#ffffff"))
        painter.restore()

    @staticmethod
    def _draw_bezier_lock_gizmo(
        painter: QPainter, point: QPointF, locked: bool, scale: float,
    ) -> None:
        """Draw an unmistakable screen-stable closed/open padlock."""
        unit = SHAPE_CONTROL_SCALE / scale
        painter.save()
        painter.setPen(QPen(QColor("#FFBE00"), 2 * unit))
        painter.setBrush(QColor("#ffffff"))
        body = QRectF(
            point.x() - 7 * unit, point.y() - unit,
            14 * unit, 10 * unit,
        )
        painter.drawRoundedRect(body, 2 * unit, 2 * unit)
        shackle = QPainterPath()
        if locked:
            shackle.moveTo(point + QPointF(-5 * unit, -unit))
            shackle.lineTo(point + QPointF(-5 * unit, -5 * unit))
            shackle.cubicTo(
                point + QPointF(-5 * unit, -11 * unit),
                point + QPointF(5 * unit, -11 * unit),
                point + QPointF(5 * unit, -5 * unit),
            )
            shackle.lineTo(point + QPointF(5 * unit, -unit))
        else:
            shackle.moveTo(point + QPointF(-5 * unit, -unit))
            shackle.lineTo(point + QPointF(-5 * unit, -5 * unit))
            shackle.cubicTo(
                point + QPointF(-5 * unit, -11 * unit),
                point + QPointF(5 * unit, -11 * unit),
                point + QPointF(5 * unit, -5 * unit),
            )
            shackle.lineTo(point + QPointF(7 * unit, -7 * unit))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(shackle)
        painter.setBrush(QColor("#FFBE00"))
        painter.drawEllipse(
            point + QPointF(0, 3 * unit), 1.25 * unit, 1.25 * unit
        )
        painter.drawLine(
            point + QPointF(0, 4 * unit),
            point + QPointF(0, 6 * unit),
        )
        painter.restore()

    def _draw_delete_point_gizmo(
        self, painter: QPainter, bound: BoundGeometry, node: PathNode,
    ) -> None:
        scale = max(self.scale, 0.05)
        point = self._shape_gizmo_positions(bound, node)["delete"]
        painter.save()
        painter.setPen(QPen(
            QColor("#FFBE00"), 2 * SHAPE_CONTROL_SCALE / scale
        ))
        painter.setBrush(QColor("#ffffff"))
        painter.drawLine(QPointF(node.x, node.y), point)
        radius = 5.5 * SHAPE_CONTROL_SCALE / scale
        painter.drawEllipse(point, radius, radius)
        arm = 2.5 * SHAPE_CONTROL_SCALE / scale
        painter.drawLine(
            point + QPointF(-arm, -arm), point + QPointF(arm, arm)
        )
        painter.drawLine(
            point + QPointF(-arm, arm), point + QPointF(arm, -arm)
        )
        painter.restore()

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
                        geometry_only=(
                            self._gradient_creation_type == "line"
                        ),
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
        if isinstance(obj, VectorDrawingObject):
            left, top, width, height = obj.derived_bounds()
            local = QRectF(
                obj.x + left, obj.y + top, max(1.0, width), max(1.0, height)
            )
            return [
                (x + layer_x, y + layer_y)
                for x, y in self._rect_quad(local)
            ]
        if isinstance(obj, VectorFillObject):
            owner = self.chapter.objects.get(obj.owner_drawing_id)
            owner_x = owner.x if isinstance(owner, VectorDrawingObject) else 0.0
            owner_y = owner.y if isinstance(owner, VectorDrawingObject) else 0.0
            left, top, width, height = obj.derived_bounds()
            local = QRectF(
                owner_x + left, owner_y + top,
                max(1.0, width), max(1.0, height),
            )
            return [
                (x + layer_x, y + layer_y)
                for x, y in self._rect_quad(local)
            ]
        if isinstance(obj, GradientObject):
            if obj.field_type == "line":
                bounds = self.bound_path(
                    obj.line_field.geometry
                ).boundingRect()
            elif obj.field_type == "radial":
                field = obj.radial_field
                radius_y = (
                    field.radius_y
                    if field.ellipse_enabled else field.radius_x
                )
                corners = [
                    self._rotated_gradient_point(
                        (field.origin_x, field.origin_y), vector,
                        field.rotation,
                    )
                    for vector in (
                        (-field.radius_x, -radius_y),
                        (field.radius_x, -radius_y),
                        (field.radius_x, radius_y),
                        (-field.radius_x, radius_y),
                    )
                ]
                return [
                    (x + layer_x, y + layer_y) for x, y in corners
                ]
            else:
                bounds = self.layer_effective_path(
                    obj.parent_layer_id
                ).boundingRect()
            bounds = QRectF(
                bounds.left(), bounds.top(),
                max(1.0, bounds.width()), max(1.0, bounds.height()),
            )
            return [
                (x + layer_x, y + layer_y)
                for x, y in self._rect_quad(bounds)
            ]
        return [
            (layer_x + obj.x, layer_y + obj.y),
            (layer_x + obj.x + 80, layer_y + obj.y),
            (layer_x + obj.x + 80, layer_y + obj.y + 80),
            (layer_x + obj.x, layer_y + obj.y + 80),
        ]

    def _selected_world_quad(self) -> list[tuple[float, float]] | None:
        if (
            self._geometry_transform_target is not None
            and self._geometry_transform_target[0] == "object"
            and self._transform_preview_quad is not None
        ):
            obj = self.chapter.objects.get(
                self._geometry_transform_target[1]
            )
            if obj is not None:
                layer_x, layer_y = self.chapter.layer_world_translation(
                    obj.parent_layer_id
                )
                return [
                    (x + layer_x, y + layer_y)
                    for x, y in self._transform_preview_quad
                ]
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

    def _point_inside_layer_masks(
        self, layer_id: str, point: QPointF,
        obj: DocumentObject | None = None,
    ) -> bool:
        layers = self.chapter.ancestor_layers(layer_id)
        if obj is not None and obj.geometry_reference == "compound":
            compound = self.chapter.closest_compound_ancestor(
                layer_id, include_self=True
            )
            if compound is not None:
                index = next(
                    i for i, layer in enumerate(layers)
                    if layer.layer_id == compound.layer_id
                )
                layers = layers[:index + 1]
        direct_mask_id = (
            layer_id
            if (
                obj is not None
                and (
                    obj.ignore_parent_mask
                    or (
                        isinstance(obj, GradientObject)
                        and self._is_outward_gradient(obj)
                    )
                )
            ) else ""
        )
        skipped_masks = {direct_mask_id} if direct_mask_id else set()
        for parent, child in zip(layers, layers[1:]):
            if child.ignore_parent_mask:
                skipped_masks.add(parent.layer_id)
        for layer in layers:
            wx, wy = self.chapter.layer_world_translation(layer.layer_id)
            if not layer.visible:
                return False
            if (
                layer.bound is not None
                and layer.layer_id not in skipped_masks
            ):
                path = self.layer_effective_path(layer.layer_id)
                if not path.contains(QPointF(point.x() - wx, point.y() - wy)):
                    return False
        return True

    def _entities_front_to_back(
        self, page_id: str,
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []

        def walk(layer_id: str) -> None:
            layer = self.chapter.layers[layer_id]
            for child in layer.children:
                if child.kind == "object":
                    result.append(("object", child.entity_id))
                    obj = self.chapter.objects.get(child.entity_id)
                    if isinstance(obj, VectorDrawingObject):
                        result.extend(
                            ("object", fill_id)
                            for fill_id in obj.fill_child_ids
                            if fill_id in self.chapter.objects
                        )
                else:
                    candidate = self.chapter.layers[child.entity_id]
                    if (
                        not candidate.is_page
                        and candidate.layer_kind != "fill"
                        and candidate.bound is not None
                    ):
                        result.append(("layer", child.entity_id))
                    walk(child.entity_id)

        walk(page_id)
        return result

    def _object_hit_contains(
        self, obj: DocumentObject, point: QPointF,
    ) -> bool:
        if not obj.visible:
            return False
        if isinstance(obj, VectorDrawingObject):
            if not obj.opacity_locked and obj.opacity <= 0:
                return False
            local = self._vector_local_point(obj, point)
            tolerance = 6.0 / max(self.scale, 0.05)
            for stroke in obj.strokes:
                if QColor(stroke.color).alpha() <= 0:
                    continue
                location = nearest_on_stroke(
                    stroke.points,
                    (local.x(), local.y()),
                    closed=stroke.closed,
                )
                if (
                    location is not None
                    and location.opacity > 0
                    and location.distance
                    <= location.width / 2 + tolerance
                ):
                    return True
            return False
        if isinstance(obj, VectorFillObject):
            owner = self.chapter.objects.get(obj.owner_drawing_id)
            if (
                not isinstance(owner, VectorDrawingObject)
                or not owner.visible
                or (not owner.opacity_locked and owner.opacity <= 0)
                or (not obj.opacity_locked and obj.opacity <= 0)
                or QColor(obj.fill_color).alpha() <= 0
            ):
                return False
            local = self._vector_local_point(owner, point)
            return self.bound_path(obj.geometry).contains(local)
        if isinstance(obj, GradientObject):
            if not obj.opacity_locked and obj.opacity <= 0:
                return False
            layer_x, layer_y = self.chapter.layer_world_translation(
                obj.parent_layer_id
            )
            local = QPointF(point.x() - layer_x, point.y() - layer_y)
            if self._is_outward_gradient(obj):
                if obj.field_type == "radial":
                    field = obj.radial_field
                    angle = math.radians(-field.rotation)
                    dx, dy = (
                        local.x() - field.origin_x,
                        local.y() - field.origin_y,
                    )
                    x = dx * math.cos(angle) - dy * math.sin(angle)
                    y = dx * math.sin(angle) + dy * math.cos(angle)
                    radius_y = (
                        field.radius_y
                        if field.ellipse_enabled else field.radius_x
                    )
                    normalized = math.hypot(
                        x / field.radius_x, y / radius_y
                    )
                    if normalized < 1:
                        return False
                    ray = math.hypot(x, y)
                    boundary = ray / max(normalized, 1e-6)
                    return ray - boundary <= field.distance
                path = self.layer_effective_path(obj.parent_layer_id)
                stroker = QPainterPathStroker()
                stroker.setWidth(obj.shape_field.distance * 2)
                return stroker.createStroke(path).subtracted(path).contains(
                    local
                )
            return self.layer_effective_path(
                obj.parent_layer_id
            ).contains(local)
        quad = self.object_world_quad(obj.object_id)
        path = QPainterPath()
        if quad:
            path.addPolygon(QPolygonF([
                QPointF(*candidate) for candidate in quad
            ]))
        return bool(quad and path.contains(point))

    def _objects_front_to_back(self, page_id: str) -> list[str]:
        return [
            entity_id for kind, entity_id
            in self._entities_front_to_back(page_id)
            if kind == "object"
        ]

    def _point_inside_parent_masks(
        self, layer_id: str, point: QPointF,
    ) -> bool:
        layer = self.chapter.layers[layer_id]
        current = layer
        while current.parent_id:
            parent = self.chapter.layers[current.parent_id]
            if not parent.visible:
                return False
            if parent.bound is not None and not current.ignore_parent_mask:
                wx, wy = self.chapter.layer_world_translation(parent.layer_id)
                path = self.layer_effective_path(parent.layer_id)
                if not path.contains(QPointF(point.x() - wx, point.y() - wy)):
                    return False
            current = parent
        return True

    def _shape_border_contains(
        self, layer_id: str, point: QPointF, raw: bool = False,
        include_pages: bool = False,
    ) -> bool:
        layer = self.chapter.layers[layer_id]
        if (
            not layer.visible or layer.bound is None
            or (layer.is_page and not include_pages)
            or layer.layer_kind == "fill"
            or (
                not raw
                and not self._point_inside_parent_masks(layer_id, point)
            )
        ):
            return False
        if raw and any(
            not ancestor.visible
            for ancestor in self.chapter.ancestor_layers(layer_id)
        ):
            return False
        if (
            not raw
            and self.chapter.contributing_compound_ancestor(layer_id)
            is not None
        ):
            return False
        wx, wy = self.chapter.layer_world_translation(layer_id)
        local = QPointF(point.x() - wx, point.y() - wy)
        path = (
            self.layer_shape_path(layer)
            if raw else self.layer_effective_path(layer_id)
        )
        stroker = QPainterPathStroker()
        visible_width = max(
            24.0 / max(self.scale, 0.05),
            layer.shape_style.outline_thickness * 2,
        )
        stroker.setWidth(visible_width)
        border = stroker.createStroke(path)
        return border.contains(local) or (
            layer.layer_kind == "open_shape" and path.contains(local)
        )

    def hit_test_shape_edit_layers(
        self, point: QPointF,
    ) -> list[dict[str, str]]:
        if self.chapter is None:
            return []
        result: list[dict[str, str]] = []
        for page_id in self.chapter.root_page_ids:
            if self._shape_border_contains(
                page_id, point, raw=True, include_pages=True
            ):
                result.append({"kind": "layer", "id": page_id})
            for kind, entity_id in self._entities_front_to_back(page_id):
                if (
                    kind == "layer"
                    and self._shape_border_contains(
                        entity_id, point, raw=True
                    )
                ):
                    result.append({"kind": "layer", "id": entity_id})
        return result

    def hit_test_entities(self, point: QPointF) -> list[dict[str, str]]:
        if self.chapter is None:
            return []
        candidates: list[tuple[str, str]] = []
        for page_id in self.chapter.root_page_ids:
            if self._shape_border_contains(
                page_id, point, include_pages=True
            ):
                candidates.append(("layer", page_id))
            candidates.extend(self._entities_front_to_back(page_id))
        hits: list[dict[str, str]] = []
        for kind, entity_id in candidates:
            if kind == "layer":
                layer = self.chapter.layers[entity_id]
                if (
                    layer.is_page
                    or self._shape_border_contains(entity_id, point)
                ):
                    hits.append({"kind": kind, "id": entity_id})
                continue
            obj = self.chapter.objects[entity_id]
            if (
                self._object_hit_contains(obj, point)
                and self._point_inside_layer_masks(
                    obj.parent_layer_id, point, obj
                )
            ):
                hits.append({"kind": kind, "id": entity_id})
        return hits

    def hit_test_objects(
        self, point: QPointF, text_only: bool = False,
    ) -> list[str]:
        if self.chapter is None:
            return []
        candidates = [
            object_id
            for page_id in self.chapter.root_page_ids
            for object_id in self._objects_front_to_back(page_id)
        ]
        hits: list[str] = []
        for object_id in candidates:
            obj = self.chapter.objects[object_id]
            if text_only and not isinstance(obj, TextObject):
                continue
            if (
                self._object_hit_contains(obj, point)
                and self._point_inside_layer_masks(
                    obj.parent_layer_id, point, obj
                )
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
            if selected.compound_enabled:
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

    # ---- page creation and editable gutters ---------------------------
    def page_world_bounds(self, page_id: str) -> QRectF:
        if (
            self.chapter is None or page_id not in self.chapter.layers
            or self.chapter.layers[page_id].bound is None
        ):
            return QRectF()
        page = self.chapter.layers[page_id]
        left, top, width, height = page.bound.bbox()
        return QRectF(
            page.translate_x + left, page.translate_y + top,
            width, height,
        )

    def physically_ordered_pages(self) -> list[str]:
        if self.chapter is None:
            return []
        root_order = {
            page_id: index
            for index, page_id in enumerate(self.chapter.root_page_ids)
        }
        return sorted(
            self.chapter.root_page_ids,
            key=lambda page_id: (
                self.page_world_bounds(page_id).top(),
                self.page_world_bounds(page_id).bottom(),
                root_order[page_id],
            ),
        )

    def begin_page_creation(
        self, anchor_page_id: str, kind: str, *,
        before: dict | None = None,
        gap_bounds: tuple[float, float] | None = None,
    ) -> bool:
        if (
            self.chapter is None
            or anchor_page_id not in self.chapter.root_page_ids
            or kind not in {"rectangle", "circle", "custom"}
        ):
            return False
        if gap_bounds is None:
            self._clear_page_gap_editor()
        self._page_creation_anchor_id = anchor_page_id
        self._page_creation_before = before or self.chapter.to_dict()
        self._page_creation_kind = kind
        self._page_creation_draft = None
        self._page_creation_committing = False
        self._page_creation_gap_bounds = gap_bounds
        self._page_creation_base_height = self.chapter.height
        anchor = self.page_world_bounds(anchor_page_id)
        self.chapter.height = max(
            self.chapter.height,
            math.ceil(anchor.bottom() + 120 + 1080),
        )
        self._creation_points.clear()
        self._creation_nodes.clear()
        self._creation_selected_node_id = ""
        self._creation_active_control = None
        target = {
            "rectangle": ToolKind.BOX_BOUND,
            "circle": ToolKind.CIRCLE_BOUND,
            "custom": ToolKind.SHAPE_CREATE,
        }[kind]
        self.tool = target
        self.toolChanged.emit(target)
        self.documentChanged.emit(QRectF())
        self.update()
        return True

    def _cancel_page_creation(self) -> None:
        before = self._page_creation_before
        anchor_id = self._page_creation_anchor_id
        self._page_creation_anchor_id = ""
        self._page_creation_before = None
        self._page_creation_kind = ""
        self._page_creation_draft = None
        self._page_creation_committing = False
        self._page_creation_gap_bounds = None
        self._page_creation_base_height = 0
        self._creation_points.clear()
        self._creation_nodes.clear()
        self._creation_selected_node_id = ""
        self._creation_active_control = None
        self._creation_style = None
        self._page_gap_transaction = None
        self._clear_page_gap_editor()
        self.pageGapConfirmationChanged.emit(False)
        if before is not None:
            self.replace_chapter(before)
        if (
            self.chapter is not None
            and anchor_id in self.chapter.layers
        ):
            self.set_selection(
                "layer", anchor_id, activate_default_tool=False
            )
        self.tool = ToolKind.SHAPE_EDIT
        self.toolChanged.emit(self.tool)
        self.interactionFinished.emit()

    def _finish_pending_page_bound(self, bound: BoundGeometry) -> bool:
        anchor_id = self._page_creation_anchor_id
        if (
            self.chapter is None or not anchor_id
            or anchor_id not in self.chapter.layers
            or self._page_creation_committing
        ):
            return False
        if not bound.closed:
            return False
        anchor = self.page_world_bounds(anchor_id)
        _left, top, _width, height = bound.bbox()
        if top < anchor.bottom() - 1e-6:
            self.pageCreationInvalid.emit(
                "Draw the new page completely below the active page."
            )
            self.update()
            return False
        if self._page_creation_gap_bounds is not None:
            gap_top, gap_bottom = self._page_creation_gap_bounds
            if top < gap_top - 1e-6 or top + height > gap_bottom + 1e-6:
                self.pageCreationInvalid.emit(
                    "Keep the complete page inside the confirmed page gap."
                )
                self.update()
                return False
        before = self._page_creation_before or self.chapter.to_dict()
        self._page_creation_draft = BoundGeometry.from_dict(bound.to_dict())
        self._page_creation_committing = True
        self.pageCreationFinished.emit(
            BoundGeometry.from_dict(self._page_creation_draft.to_dict()),
            before, anchor_id
        )
        return True

    def resolve_page_creation(
        self, success: bool, message: str = "",
    ) -> None:
        """Acknowledge the synchronous MainWindow insertion request.

        A failed insertion deliberately leaves the draft and anchor intact so
        the user can adjust/redraw it or cancel without losing the workflow.
        """
        self._page_creation_committing = False
        if not success:
            if message:
                self.pageCreationInvalid.emit(message)
            self.update()
            return
        self._page_creation_anchor_id = ""
        self._page_creation_before = None
        self._page_creation_kind = ""
        self._page_creation_draft = None
        self._page_creation_gap_bounds = None
        self._page_creation_base_height = 0
        self._creation_points.clear()
        self._creation_nodes.clear()
        self._creation_selected_node_id = ""
        self._creation_active_control = None
        self._creation_close_candidate = False
        self._creation_style = None
        self._shape_hover_target = None
        self._shape_hover_insert = None
        self.update()

    def page_creation_base_height(self) -> int:
        return int(self._page_creation_base_height or (
            self.chapter.height if self.chapter is not None else 0
        ))

    def set_page_gap_prompt_line(self, y: float | None) -> None:
        self._page_gap_prompt_y = None if y is None else float(y)
        self.update()

    def begin_page_gap_editor(
        self, owner_id: str, top_ids: list[str], bottom_ids: list[str],
        top_y: float, bottom_y: float,
    ) -> None:
        self._page_gap_prompt_y = None
        self._page_gap_state = {
            "owner_id": owner_id,
            "top_ids": list(top_ids),
            "bottom_ids": list(bottom_ids),
            "top_y": float(top_y),
            "bottom_y": max(float(top_y), float(bottom_y)),
        }
        self._page_gap_hover = None
        self.update()

    def begin_page_gap_transaction(
        self, origin: str, anchor_id: str, top_ids: list[str],
        bottom_ids: list[str], top_y: float,
    ) -> bool:
        if (
            self.chapter is None
            or origin not in {"add_page", "standalone"}
            or anchor_id not in self.chapter.root_page_ids
            or not bottom_ids
        ):
            return False
        before = self.chapter.to_dict()
        for page_id in bottom_ids:
            if page_id in self.chapter.layers:
                self.chapter.layers[page_id].translate_y += 120
        self._page_gap_transaction = {
            "origin": origin,
            "anchor_id": anchor_id,
            "before": before,
            "confirmed": False,
        }
        self.begin_page_gap_editor(
            anchor_id, top_ids, bottom_ids, top_y, top_y + 120
        )
        self._ensure_page_height_safety()
        self.pageGapConfirmationChanged.emit(True)
        self.hierarchyChanged.emit()
        self.documentChanged.emit(QRectF())
        self.update()
        return True

    def page_gap_transaction(self) -> dict | None:
        if self._page_gap_transaction is None or self._page_gap_state is None:
            return None
        return {
            **self._page_gap_transaction,
            "top_ids": list(self._page_gap_state["top_ids"]),
            "bottom_ids": list(self._page_gap_state["bottom_ids"]),
            "top_y": float(self._page_gap_state["top_y"]),
            "bottom_y": float(self._page_gap_state["bottom_y"]),
        }

    def confirm_page_gap_transaction(self) -> dict | None:
        if self.chapter is None or self._page_gap_transaction is None:
            return None
        self._ensure_page_height_safety()
        transaction = self.page_gap_transaction()
        if transaction is None:
            return None
        origin = transaction["origin"]
        if origin == "standalone":
            before = transaction["before"]
            owner_id = transaction["anchor_id"]
            after = self.chapter.to_dict()
            self._page_gap_transaction = None
            self._clear_page_gap_editor()
            self.pageGapConfirmationChanged.emit(False)
            if before != after:
                self.push_model_change(
                    before, after, "Insert page gap"
                )
            if owner_id in self.chapter.layers:
                self.set_selection(
                    "layer", owner_id, activate_default_tool=False
                )
            self.tool = ToolKind.SHAPE_EDIT
            self.toolChanged.emit(self.tool)
            self.hierarchyChanged.emit()
            self.documentChanged.emit(QRectF())
            self.interactionFinished.emit()
            self.update()
        else:
            self._page_gap_transaction["confirmed"] = True
        return transaction

    def cancel_page_gap_transaction(self) -> str:
        transaction = self._page_gap_transaction
        if transaction is None:
            self._clear_page_gap_editor()
            self.pageGapConfirmationChanged.emit(False)
            return ""
        origin = str(transaction["origin"])
        before = transaction["before"]
        anchor_id = str(transaction["anchor_id"])
        self._page_gap_transaction = None
        self._clear_page_gap_editor()
        self.pageGapConfirmationChanged.emit(False)
        self.replace_chapter(before)
        if anchor_id in self.chapter.layers:
            self.set_selection(
                "layer", anchor_id, activate_default_tool=False
            )
        if origin == "standalone":
            self.tool = ToolKind.INSERT_PAGE_GAP
            self.toolChanged.emit(self.tool)
        else:
            self.tool = ToolKind.SHAPE_EDIT
            self.toolChanged.emit(self.tool)
        self.interactionFinished.emit()
        self.update()
        return origin

    def finish_page_gap_workflow(self) -> None:
        self._page_gap_transaction = None
        self._clear_page_gap_editor()
        self.pageGapConfirmationChanged.emit(False)

    def _clear_page_gap_editor(self) -> None:
        self._page_gap_prompt_y = None
        self._page_gap_state = None
        self._page_gap_hover = None
        self._page_gap_drag_mode = None
        self._page_gap_drag_before = None
        self._page_gap_drag_translations.clear()
        self.unsetCursor()
        self.update()

    def _page_gap_editor_visible(self) -> bool:
        return bool(
            self._page_gap_state
            and (
                self._page_gap_transaction is not None
                or (
                    self.selected_kind == "layer"
                    and self.selected_id == self._page_gap_state.get("owner_id")
                )
            )
        )

    def _page_gap_hit(self, world: QPointF) -> str | None:
        if not self._page_gap_editor_visible():
            return None
        top = float(self._page_gap_state["top_y"])
        bottom = float(self._page_gap_state["bottom_y"])
        tolerance = 12 / max(self.scale, 0.05)
        if abs(world.y() - top) <= tolerance:
            return "top"
        if abs(world.y() - bottom) <= tolerance:
            return "bottom"
        if top < world.y() < bottom:
            return "band"
        return None

    def _update_page_gap_hover(self, world: QPointF) -> None:
        if self.tool != ToolKind.INSERT_PAGE_GAP or self.chapter is None:
            self._page_gap_hover = None
            return
        ordered = self.physically_ordered_pages()
        hover = None
        for index in range(len(ordered) - 1):
            upper = self.page_world_bounds(ordered[index])
            lower = self.page_world_bounds(ordered[index + 1])
            if upper.bottom() <= world.y() <= lower.top():
                hover = {
                    "y": world.y(),
                    "top_ids": ordered[:index + 1],
                    "bottom_ids": ordered[index + 1:],
                    "owner_id": ordered[index + 1],
                }
                break
        self._page_gap_hover = hover
        self.setCursor(
            Qt.PointingHandCursor if hover else Qt.ForbiddenCursor
        )
        self.update()

    def _begin_page_gap_interaction(self, world: QPointF) -> bool:
        mode = self._page_gap_hit(world)
        if mode is None:
            return False
        state = self._page_gap_state
        self._page_gap_drag_mode = mode
        self._page_gap_drag_before = self.chapter.to_dict()
        self._page_gap_drag_start_y = world.y()
        self._page_gap_drag_start_top = float(state["top_y"])
        self._page_gap_drag_start_bottom = float(state["bottom_y"])
        ids = set(state["top_ids"]) | set(state["bottom_ids"])
        self._page_gap_drag_translations = {
            page_id: self.chapter.layers[page_id].translate_y
            for page_id in ids if page_id in self.chapter.layers
        }
        self.setCursor(
            Qt.ClosedHandCursor
            if mode == "band" else Qt.PointingHandCursor
        )
        return True

    def _move_page_gap_interaction(self, world: QPointF) -> None:
        if not self._page_gap_drag_mode or not self._page_gap_state:
            return
        delta = world.y() - self._page_gap_drag_start_y
        mode = self._page_gap_drag_mode
        if mode == "top":
            delta = min(
                delta,
                self._page_gap_drag_start_bottom
                - self._page_gap_drag_start_top,
            )
            moving = self._page_gap_state["top_ids"]
            self._page_gap_state["top_y"] = (
                self._page_gap_drag_start_top + delta
            )
        elif mode == "bottom":
            delta = max(
                delta,
                self._page_gap_drag_start_top
                - self._page_gap_drag_start_bottom,
            )
            moving = self._page_gap_state["bottom_ids"]
            self._page_gap_state["bottom_y"] = (
                self._page_gap_drag_start_bottom + delta
            )
        else:
            moving = (
                self._page_gap_state["top_ids"]
                + self._page_gap_state["bottom_ids"]
            )
            self._page_gap_state["top_y"] = (
                self._page_gap_drag_start_top + delta
            )
            self._page_gap_state["bottom_y"] = (
                self._page_gap_drag_start_bottom + delta
            )
        for page_id in moving:
            if (
                page_id in self.chapter.layers
                and page_id in self._page_gap_drag_translations
            ):
                self.chapter.layers[page_id].translate_y = (
                    self._page_gap_drag_translations[page_id] + delta
                )
        self.documentChanged.emit(QRectF())
        self.update()

    def _ensure_page_height_safety(self) -> None:
        if self.chapter is None or not self.chapter.root_page_ids:
            return
        bounds = [
            self.page_world_bounds(page_id)
            for page_id in self.chapter.root_page_ids
        ]
        minimum_top = min(rect.top() for rect in bounds)
        if minimum_top < 0:
            correction = 120 - minimum_top
            for page_id in self.chapter.root_page_ids:
                self.chapter.layers[page_id].translate_y += correction
            if self._page_gap_state:
                self._page_gap_state["top_y"] += correction
                self._page_gap_state["bottom_y"] += correction
            self.chapter.height += math.ceil(correction)
            bounds = [
                self.page_world_bounds(page_id)
                for page_id in self.chapter.root_page_ids
            ]
        maximum_bottom = max(rect.bottom() for rect in bounds)
        if maximum_bottom > self.chapter.height:
            self.chapter.height = math.ceil(maximum_bottom + 120)

    def _finish_page_gap_interaction(self) -> bool:
        if not self._page_gap_drag_mode:
            return False
        before = self._page_gap_drag_before
        self._page_gap_drag_mode = None
        self._page_gap_drag_before = None
        self._page_gap_drag_translations.clear()
        self._ensure_page_height_safety()
        after = self.chapter.to_dict()
        if (
            self._page_gap_transaction is None
            and before is not None and before != after
        ):
            self.push_model_change(before, after, "Adjust page gap")
            self.hierarchyChanged.emit()
            self.documentChanged.emit(QRectF())
        self.setCursor(Qt.OpenHandCursor)
        self.interactionFinished.emit()
        self.update()
        return True

    def _insert_hovered_page_gap(self) -> bool:
        hover = self._page_gap_hover
        if self.chapter is None or hover is None:
            return False
        return self.begin_page_gap_transaction(
            "standalone", hover["owner_id"],
            hover["top_ids"], hover["bottom_ids"], hover["y"],
        )

    # ---- input ---------------------------------------------------------
    def _navigation_mode(self) -> str | None:
        modifiers = QGuiApplication.keyboardModifiers()
        if modifiers == (Qt.AltModifier | Qt.ShiftModifier):
            return "zoom"
        if modifiers == Qt.AltModifier:
            return "pan"
        if modifiers == Qt.ShiftModifier:
            if (
                self.tool in {
                    ToolKind.SHAPE_EDIT,
                    ToolKind.VECTOR_EDIT,
                    ToolKind.DRAW_SELECT_RECT,
                    ToolKind.DRAW_SELECT_LASSO,
                    ToolKind.DRAW_SELECT_STROKE,
                }
                or (
                    self.tool == ToolKind.VECTOR_REDRAW
                    and self.settings.vector_redraw_interaction == "point"
                )
            ):
                return None
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
        self._pointer_hover_widget = QPointF(event.position())
        world = self.widget_to_document(event.position())
        transform_quad = (
            self._selection_transform_quad
            if self.tool in {
                ToolKind.DRAW_SELECT_RECT,
                ToolKind.DRAW_SELECT_LASSO,
                ToolKind.DRAW_SELECT_STROKE,
            }
            else (
                self._selected_world_quad()
                if (
                    self.tool == ToolKind.TRANSFORM
                    or (
                        self.tool == ToolKind.TEXT_EDIT
                        and isinstance(
                            self.chapter.objects.get(
                                self.selected_object_id
                            ), TextObject
                        )
                        and self.chapter.objects[
                            self.selected_object_id
                        ].layout_mode == "free"
                    )
                )
                else None
            )
        )
        over_transform_handle = False
        over_transform_edge = False
        if transform_quad:
            tolerance = 12 / max(self.scale, 0.05)
            pivot = (
                self._selection_pivot
                if self.tool in {
                    ToolKind.DRAW_SELECT_RECT,
                    ToolKind.DRAW_SELECT_LASSO,
                    ToolKind.DRAW_SELECT_STROKE,
                }
                else self._transform_pivot
            )
            handles, rotate, pivot = self._transform_control_points(
                transform_quad, pivot
            )
            over_transform_handle = any(
                math.dist(world.toTuple(), candidate) <= tolerance
                for candidate in handles
            ) or math.dist(
                world.toTuple(), rotate.toTuple()
            ) <= tolerance or math.dist(
                world.toTuple(), pivot.toTuple()
            ) <= tolerance
            outline = QPainterPath()
            outline.addPolygon(QPolygonF([
                QPointF(*point) for point in transform_quad
            ]))
            stroker = QPainterPathStroker()
            stroker.setWidth(16 / max(self.scale, 0.05))
            over_transform_edge = stroker.createStroke(
                outline
            ).contains(world)
        if over_transform_handle:
            self.setCursor(Qt.PointingHandCursor)
        elif over_transform_edge:
            self.setCursor(Qt.SizeAllCursor)
        elif self.tool == ToolKind.DRAW_SELECT_STROKE:
            self.setCursor(Qt.PointingHandCursor)
        elif self.tool in {
            ToolKind.DRAW_SELECT_RECT, ToolKind.DRAW_SELECT_LASSO,
        }:
            self.setCursor(Qt.CrossCursor)
        elif self.tool == ToolKind.TRANSFORM:
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.unsetCursor()
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
        if (
            self.tool == ToolKind.SHAPE_EDIT
            and self.chapter is not None
        ):
            obj = self.chapter.objects.get(self.selected_object_id)
            if (
                isinstance(obj, GradientObject)
                and self._reset_gradient_center(
                    obj, self.widget_to_document(event.position())
                )
            ):
                event.accept()
                return
        if (
            self.tool == ToolKind.TRANSFORM
            and self.chapter is not None
            and self.selected_kind == "object"
        ):
            obj = self.chapter.objects.get(self.selected_id)
            if isinstance(obj, TextObject) and obj.layout_mode == "free":
                world = self.widget_to_document(event.position())
                path = QPainterPath()
                path.addPolygon(QPolygonF([
                    QPointF(*point)
                    for point in self.object_world_quad(obj.object_id)
                ]))
                if path.contains(world):
                    self.set_tool(ToolKind.TEXT_EDIT)
                    self._begin_text_pointer(world)
                    self._text_dragging = False
                    event.accept()
                    return
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
        if event.key() in {Qt.Key_Shift, Qt.Key_Control}:
            self.update()
        if event.key() == Qt.Key_Escape and self._page_gap_state is not None:
            if self._page_gap_transaction is not None:
                self.cancel_page_gap_transaction()
            else:
                self._clear_page_gap_editor()
            event.accept()
            return
        if event.key() == Qt.Key_Escape and self._page_creation_anchor_id:
            self._cancel_page_creation()
            event.accept()
            return
        if (
            self.tool == ToolKind.SHAPE_EDIT
            and self._geometry_transform_target is not None
            and self._transform_start_quad is not None
        ):
            self._update_geometry_transform_preview(point)
            self.update()
            return
        if (
            event.key() == Qt.Key_Escape
            and self._gradient_creation_parent_id
        ):
            self._cancel_gradient_creation()
            self.set_tool(ToolKind.SHAPE_EDIT)
            event.accept()
            return
        if self._handle_text_key(event):
            return
        if (
            event.key() == Qt.Key_Escape
            and self._vector_gesture_mode is not None
        ):
            self._cancel_vector_gesture(restore=True)
            self.interactionFinished.emit()
            return
        if self.tool == ToolKind.SHAPE_CREATE:
            if (
                event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and len(self._creation_nodes) >= 2
            ):
                self._finish_shape(False)
                return
            if event.key() == Qt.Key_Escape:
                if self._gradient_creation_parent_id:
                    self._cancel_gradient_creation()
                    self.set_tool(ToolKind.SHAPE_EDIT)
                    return
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
            self._raster_creation_index = None
            self.set_tool(ToolKind.OBJECT_SELECT)
            return
        if event.key() == Qt.Key_Escape:
            self.set_tool(ToolKind.OBJECT_SELECT)
        if (
            event.key() == Qt.Key_Delete
            and self.tool == ToolKind.SHAPE_EDIT
            and self.chapter is not None
            and self._selected_shape_node_id
        ):
            gradient = self.chapter.objects.get(self.selected_object_id)
            if (
                isinstance(gradient, GradientObject)
                and gradient.field_type == "line"
                and len(gradient.line_field.geometry.nodes) > 2
            ):
                before = self.chapter.to_dict()
                gradient.line_field.geometry.nodes = [
                    node for node in gradient.line_field.geometry.nodes
                    if node.node_id != self._selected_shape_node_id
                ]
                gradient.line_field.geometry.normalize_bezier_handles()
                gradient.touch_revision()
                self._selected_shape_node_id = ""
                self._push_immediate_shape_change(
                    before, "Delete gradient path point"
                )
                return
        if (
            self.tool == ToolKind.BOUND_EDIT and self.selected_kind == "layer"
            and self._selected_shape_node_id and self.chapter is not None
        ):
            layer = self.chapter.layers[self.selected_id]
            if (
                event.key() == Qt.Key_Delete
                and self._delete_selected_shape_node(layer)
            ):
                return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if event.key() in {Qt.Key_Shift, Qt.Key_Control}:
            self.update()
        super().keyReleaseEvent(event)

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
        self._tablet_hover_widget = QPointF(event.position())
        nav = self._navigation_mode()
        if event.type() == QEvent.TabletPress:
            if nav:
                self._begin_navigation(nav, event.position())
            elif event.button() == Qt.LeftButton or event.pressure() > 0:
                gradient = self.chapter.objects.get(
                    self.selected_object_id
                )
                world = self.widget_to_document(event.position())
                hit = (
                    self._gradient_control_hit(gradient, world)
                    if (
                        self.tool == ToolKind.SHAPE_EDIT
                        and isinstance(gradient, GradientObject)
                    ) else None
                )
                now = time.monotonic()
                if hit is not None and hit[0] == "center":
                    previous = self._last_gradient_tablet_tap
                    self._last_gradient_tablet_tap = (
                        now, QPointF(event.position())
                    )
                    if (
                        previous is not None
                        and now - previous[0] <= 0.45
                        and math.dist(
                            event.position().toTuple(),
                            previous[1].toTuple(),
                        ) <= 18
                        and self._reset_gradient_center(gradient, world)
                    ):
                        self._last_gradient_tablet_tap = None
                        self.update()
                        event.accept()
                        return
                else:
                    self._last_gradient_tablet_tap = None
                self._tablet_tool_active = True
                self._tool_press(event.position(), event.pressure())
        elif event.type() == QEvent.TabletMove:
            if self._nav_mode:
                self._update_navigation(event.position())
            elif self._tablet_tool_active:
                self._tool_move(event.position(), event.pressure())
            elif self.tool == ToolKind.DRAW_SELECT_STROKE:
                self._continue_drawing_selection(
                    self.widget_to_document(event.position()),
                    event.position(),
                )
        elif event.type() == QEvent.TabletRelease:
            if self._nav_mode:
                self._end_navigation()
            elif self._tablet_tool_active:
                self._tablet_tool_active = False
                self._tool_release()
        self.update()
        event.accept()

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.Leave:
            self._tablet_hover_widget = None
            self.update()
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
            self._touch_frame_timer.stop()
            self._touch_pending_points = None
            self._rebase_touch_navigation(points)
            self._capture_navigation_snapshot()
            event.accept()
            return True
        if event.type() == QEvent.TouchUpdate and points:
            if len(points) != len(self._touch_anchor_points):
                self._touch_frame_timer.stop()
                self._touch_pending_points = None
                self._rebase_touch_navigation(points)
                self._capture_navigation_snapshot()
            else:
                # Touch hardware can deliver well over a hundred updates per
                # second.  Keep only the newest frame and apply it once from
                # the stable gesture anchor; this removes accumulated camera
                # work without changing the deterministic transform.
                self._touch_pending_points = [QPointF(point) for point in points]
                if not self._touch_frame_timer.isActive():
                    self._touch_frame_timer.start(0)
            event.accept()
            return True
        self._touch_frame_timer.stop()
        self._touch_pending_points = None
        self._clear_navigation_snapshot()
        self._touch_points.clear()
        self._touch_anchor_points.clear()
        self.interactionFinished.emit()
        event.accept()
        return True

    def _flush_touch_navigation(self) -> None:
        points = self._touch_pending_points
        self._touch_pending_points = None
        if points:
            self._apply_touch_navigation(points)

    @staticmethod
    def _touch_centroid(points: list[QPointF]) -> QPointF:
        return QPointF(
            sum(point.x() for point in points) / len(points),
            sum(point.y() for point in points) / len(points),
        )

    def _rebase_touch_navigation(self, points: list[QPointF]) -> None:
        self._touch_points = [QPointF(point) for point in points]
        self._touch_anchor_points = [QPointF(point) for point in points]
        self._touch_anchor_center = QPointF(self.center_x, self.center_y)
        self._touch_anchor_scale = self.scale
        self._touch_anchor_rotation = self.rotation
        if not points:
            self._touch_anchor_document = QPointF()
            return
        center = self._touch_centroid(points)
        self._touch_anchor_document = self.widget_to_document(center)
        if len(points) >= 2:
            vector = points[1] - points[0]
            self._touch_anchor_distance = max(
                0.001, math.hypot(vector.x(), vector.y())
            )
            self._touch_anchor_angle = math.atan2(vector.y(), vector.x())

    def _apply_touch_navigation(
        self, points: list[QPointF],
    ) -> tuple[float, float, float, float]:
        """Apply a deterministic pan/pinch/twist frame from stable anchors."""
        if (
            not self._touch_anchor_points
            or len(points) != len(self._touch_anchor_points)
        ):
            self._rebase_touch_navigation(points)
            return self.center_x, self.center_y, self.rotation, self.scale
        current_center = self._touch_centroid(points)
        new_scale = self._touch_anchor_scale
        new_rotation = self._touch_anchor_rotation
        if len(points) >= 2:
            vector = points[1] - points[0]
            distance = max(0.001, math.hypot(vector.x(), vector.y()))
            angle = math.atan2(vector.y(), vector.x())
            angle_delta = math.atan2(
                math.sin(angle - self._touch_anchor_angle),
                math.cos(angle - self._touch_anchor_angle),
            )
            new_scale = max(
                0.05,
                min(
                    8.0,
                    self._touch_anchor_scale
                    * distance / self._touch_anchor_distance,
                ),
            )
            new_rotation = (
                self._touch_anchor_rotation + math.degrees(angle_delta)
            )

        viewport_delta = current_center - QPointF(
            self.width() / 2, self.height() / 2
        )
        angle = math.radians(new_rotation)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        document_delta = QPointF(
            (
                viewport_delta.x() * cos_a
                + viewport_delta.y() * sin_a
            ) / new_scale,
            (
                -viewport_delta.x() * sin_a
                + viewport_delta.y() * cos_a
            ) / new_scale,
        )
        self.center_x = self._touch_anchor_document.x() - document_delta.x()
        self.center_y = self._touch_anchor_document.y() - document_delta.y()
        self.scale = new_scale
        self.rotation = new_rotation
        self._touch_points = [QPointF(point) for point in points]
        self.update()
        self.cameraChanged.emit()
        return self.center_x, self.center_y, self.rotation, self.scale

    def _apply_touch_pan_delta(self, delta_x: float, delta_y: float) -> None:
        """Apply tablet navigation deltas without changing desktop grab-pan."""
        self.center_x -= delta_x / self.scale
        self.center_y -= delta_y / self.scale

    def _creation_selected_node(self) -> PathNode | None:
        return next((
            node for node in self._creation_nodes
            if node.node_id == self._creation_selected_node_id
        ), None)

    def _normalize_creation_handles(self) -> None:
        if len(self._creation_nodes) >= 2:
            BoundGeometry._normalize_contour_handles(PathContour(
                self._creation_nodes, False
            ))

    def _creation_hit_test(self, point: QPointF) -> dict | None:
        if not self._creation_nodes:
            return None
        self._normalize_creation_handles()
        if (
            self._page_creation_anchor_id
            and len(self._creation_nodes) >= 3
        ):
            first = self._creation_nodes[0]
            tolerance = 14 * SHAPE_CONTROL_SCALE / max(
                self.scale, 0.05
            )
            if math.dist(
                (point.x(), point.y()), first.position
            ) <= tolerance:
                return {
                    "kind": "node", "index": 0,
                    "node_id": first.node_id,
                    "position": QPointF(first.x, first.y),
                }
        if len(self._creation_nodes) == 1:
            node = self._creation_nodes[0]
            tolerance = (
                14 * SHAPE_CONTROL_SCALE / max(self.scale, 0.05)
            )
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
            return self._shape_hit_test(
                geometry, point,
                geometry_only=(self._gradient_creation_type == "line"),
            )
        finally:
            self._selected_shape_node_id = previous

    @staticmethod
    def _shape_hit_tooltip(
        bound: BoundGeometry | None, hit: dict | None,
        style: ShapeStyle | None = None,
    ) -> str:
        if not hit:
            return ""
        kind = hit["kind"]
        labels = {
            "radius": "Drag to adjust this corner's roundness",
            "node": "Click to select; drag to move this point",
            "rectangle_point": "Drag to move this rectangle point",
            "rectangle_edge": "Drag to move both points on this edge",
            "primitive_handle": "Drag to resize this shape",
            "insert": "Click to insert a Vector point; drag to insert a Bézier point",
            "interior": "Drag to move this shape",
        }
        if kind == "control":
            direction = hit.get("name", "")
            return (
                f"Drag the {direction} Bézier handle"
                if direction in {"incoming", "outgoing"}
                else "Drag this Bézier handle"
            )
        if kind != "gizmo":
            return labels.get(kind, "")
        name = hit.get("name")
        node = next((
            candidate
            for contour in bound.iter_contours()
            for candidate in contour.nodes
            if candidate.node_id == hit.get("node_id")
        ), None) if bound is not None else None
        if name == "delete":
            return "Delete this point"
        if name == "type" and node is not None:
            target = "Bézier" if node.point_type == "vector" else "Vector"
            return f"Convert this point to {target}"
        if name == "lock" and node is not None:
            return (
                "Unlock Bézier handles"
                if node.handles_locked else "Lock Bézier handles"
            )
        if name == "thickness":
            return "Drag to adjust stroke thickness at this point"
        if name == "roundness":
            return "Click to toggle smoothness; drag to adjust it"
        if name == "cap" and node is not None:
            is_start = bool(bound and node is bound.nodes[0])
            cap = (
                style.start_cap if style and is_start
                else style.end_cap if style else "round"
            )
            end = "start" if is_start else "end"
            return f"Change the {end} cap (currently {cap.title()})"
        return ""

    def _update_creation_hover(self, point: QPointF) -> None:
        hit = self._creation_hit_test(point)
        self._shape_hover_target = hit
        self._shape_hover_insert = (
            hit["insert"] if hit and hit["kind"] == "insert" else None
        )
        geometry = (
            BoundGeometry.path(self._creation_nodes, False)
            if len(self._creation_nodes) >= 2 else None
        )
        self.setToolTip(self._shape_hit_tooltip(
            geometry, hit, self._creation_style
        ))
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
            elif name == "delete":
                if self._can_delete_shape_node(geometry):
                    self._creation_nodes.remove(node)
                    self._creation_selected_node_id = ""
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
                self._creation_node_dragged = False
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
                not self._gradient_creation_parent_id
                and
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
                node.handles_locked = False
                snapped = self._snap(
                    point, self._target_parent_for_new_layer()
                )
                target = (snapped.x(), snapped.y())
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
                snapped = self._snap(
                    point, self._target_parent_for_new_layer()
                )
                node.incoming = (snapped.x(), snapped.y())
                node.outgoing = (
                    node.x * 2 - snapped.x(), node.y * 2 - snapped.y()
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
            snapped = self._snap(
                point, self._target_parent_for_new_layer()
            )
            geometry = BoundGeometry.path(self._creation_nodes, False)
            self._move_shape_bezier_handle(
                geometry, node, control, (snapped.x(), snapped.y())
            )
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
            if moved:
                geometry = BoundGeometry.path(self._creation_nodes, False)
                node.roundness = min(
                    self._maximum_shape_roundness(geometry, node),
                    max(
                        0.0,
                        math.dist(node.position, (point.x(), point.y())),
                    ),
                )
                node.roundness_enabled = True
        self._normalize_creation_handles()
        self.update()
        return True

    # ---- drawing selections -------------------------------------------
    def _clear_drawing_selection(self, *, reset_pivot: bool = True) -> None:
        self._drawing_selection_path = QPainterPath()
        self._drawing_selection_gesture.clear()
        self._selection_transform_quad = None
        self._selection_transform_start_quad = None
        self._selection_transform_mode = None
        self._selection_transform_handle = None
        self._hover_vector_stroke_id = ""
        self._selection_vector_preview.clear()
        self._selection_vector_points.clear()
        self._selection_vector_preview_revision += 1
        if reset_pivot:
            self._selection_pivot = None
            self._selection_pivot_custom = False

    def _drawing_selection_object(
        self,
    ) -> RasterObject | VectorDrawingObject | None:
        if self.chapter is None:
            return None
        candidate = self.chapter.objects.get(self.selected_object_id)
        return (
            candidate
            if isinstance(candidate, (RasterObject, VectorDrawingObject))
            else None
        )

    def _drawing_local_point(
        self, obj: RasterObject | VectorDrawingObject, world: QPointF,
    ) -> QPointF:
        layer_x, layer_y = self.chapter.layer_world_translation(
            obj.parent_layer_id
        )
        return QPointF(
            world.x() - layer_x - obj.x,
            world.y() - layer_y - obj.y,
        )

    def _point_inside_drawing_bounds(
        self, obj: RasterObject | VectorDrawingObject, world: QPointF,
    ) -> bool:
        if isinstance(obj, RasterObject):
            quad = self.object_world_quad(obj.object_id)
            if not quad:
                return False
            path = QPainterPath()
            path.addPolygon(QPolygonF([QPointF(*point) for point in quad]))
            path.closeSubpath()
            return path.contains(world)
        if not obj.strokes:
            return False
        local = self._drawing_local_point(obj, world)
        bounds = QRectF(*obj.derived_bounds())
        return not bounds.isEmpty() and bounds.contains(local)

    @staticmethod
    def _selection_operation() -> str:
        modifiers = QGuiApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            return "remove"
        if modifiers & Qt.ShiftModifier:
            return "add"
        return "replace"

    @staticmethod
    def _stroke_ids_containing_points(
        drawing: VectorDrawingObject, point_ids: set[str],
    ) -> set[str]:
        return {
            stroke.stroke_id
            for stroke in drawing.strokes
            if any(point.point_id in point_ids for point in stroke.points)
        }

    @staticmethod
    def _point_ids_for_strokes(
        drawing: VectorDrawingObject, stroke_ids: set[str],
    ) -> set[str]:
        return {
            point.point_id
            for stroke in drawing.strokes
            if stroke.stroke_id in stroke_ids
            for point in stroke.points
        }

    def _reset_drawing_selection_frame(self) -> None:
        self._selection_pivot = None
        self._selection_pivot_custom = False
        self._refresh_drawing_selection_transform()

    def _strokes_intersecting_selection_region(
        self, drawing: VectorDrawingObject, region: QPainterPath,
    ) -> set[str]:
        result: set[str] = set()
        stroker = QPainterPathStroker()
        stroker.setWidth(max(0.05, 1.0 / max(self.scale, 0.05)))
        for stroke in drawing.strokes:
            centerline = self._vector_centerline_path(stroke)
            if (
                region.intersects(stroker.createStroke(centerline))
                or any(
                    region.contains(QPointF(point.x, point.y))
                    for point in stroke.points
                )
            ):
                result.add(stroke.stroke_id)
        return result

    def _begin_drawing_selection_transform(
        self, obj: RasterObject | VectorDrawingObject, world: QPointF,
    ) -> bool:
        quad = self._selection_transform_quad
        if not quad:
            return False
        tolerance = 14 / max(self.scale, 0.05)
        handles, rotate, pivot = self._transform_control_points(
            quad, self._selection_pivot
        )
        pivot_distance = math.dist(world.toTuple(), pivot.toTuple())
        if (
            4 / max(self.scale, 0.05)
            <= pivot_distance <= tolerance
        ):
            mode, handle = "pivot", None
        elif math.dist(world.toTuple(), rotate.toTuple()) <= tolerance:
            mode, handle = "rotate", None
        else:
            distances = [
                math.dist(world.toTuple(), candidate)
                for candidate in handles
            ]
            selection_path = QPainterPath()
            selection_path.addPolygon(QPolygonF([
                QPointF(*candidate) for candidate in quad
            ]))
            center = QPointF(
                sum(x for x, _ in quad) / 4,
                sum(y for _, y in quad) / 4,
            )
            center_distance = math.dist(
                world.toTuple(), center.toTuple()
            )
            if (
                selection_path.contains(world)
                and distances
                and center_distance < min(distances)
            ):
                mode, handle = "translate", None
            elif distances and min(distances) <= tolerance:
                mode, handle = "handle", distances.index(min(distances))
            else:
                stroker = QPainterPathStroker()
                stroker.setWidth(18 / max(self.scale, 0.05))
                if (
                    selection_path.contains(world)
                    or stroker.createStroke(selection_path).contains(world)
                ):
                    mode, handle = "translate", None
                else:
                    return False
        self._selection_transform_mode = mode
        self._selection_transform_handle = handle
        self._selection_transform_start = QPointF(world)
        self._selection_transform_start_quad = list(quad)
        self._selection_rotate_quad = list(quad)
        self._selection_before_model = self.chapter.to_dict()
        self._selection_vector_points = {}
        self._selection_vector_preview.clear()
        self._selection_vector_preview_revision += 1
        if isinstance(obj, VectorDrawingObject):
            self._selection_vector_points = {
                point.point_id: {
                    "position": point.position,
                    "incoming": point.incoming,
                    "outgoing": point.outgoing,
                    "width": point.width,
                }
                for stroke in obj.strokes
                for point in stroke.points
                if point.point_id in self._selected_vector_point_ids
            }
        else:
            self._selection_before_tiles = self.tiles.object_tiles(
                obj.object_id
            )
        self._selection_rotate_start = math.atan2(
            world.y() - pivot.y(), world.x() - pivot.x()
        )
        return True

    def _update_drawing_selection_transform(
        self, obj: RasterObject | VectorDrawingObject, world: QPointF,
    ) -> None:
        start = self._selection_transform_start_quad
        if not start:
            return
        mode = self._selection_transform_mode
        pivot = self._selection_pivot or QPointF(
            sum(x for x, _ in start) / 4,
            sum(y for _, y in start) / 4,
        )
        if mode == "pivot":
            self._selection_pivot = QPointF(world)
            self._selection_pivot_custom = True
            self.update()
            return
        if mode == "translate":
            delta = world - self._selection_transform_start
            target = [
                (x + delta.x(), y + delta.y()) for x, y in start
            ]
        elif mode == "rotate":
            angle = math.atan2(
                world.y() - pivot.y(), world.x() - pivot.x()
            ) - self._selection_rotate_start
            cosine, sine = math.cos(angle), math.sin(angle)
            target = [
                (
                    pivot.x() + (x - pivot.x()) * cosine
                    - (y - pivot.y()) * sine,
                    pivot.y() + (x - pivot.x()) * sine
                    + (y - pivot.y()) * cosine,
                )
                for x, y in start
            ]
        else:
            handle = self._selection_transform_handle
            if handle is None:
                return
            anchors = start + self._edge_midpoints(start)
            opposite = [2, 3, 0, 1, 6, 7, 4, 5][handle]
            origin = QPointF(*anchors[opposite])
            initial = QPointF(*anchors[handle])
            if self.settings.transform_mode == "uniform":
                factor = math.dist(
                    origin.toTuple(), world.toTuple()
                ) / max(
                    1e-6, math.dist(origin.toTuple(), initial.toTuple())
                )
                target = [
                    (
                        origin.x() + (x - origin.x()) * factor,
                        origin.y() + (y - origin.y()) * factor,
                    )
                    for x, y in start
                ]
            else:
                target = list(start)
                if handle < 4:
                    target[handle] = (world.x(), world.y())
                else:
                    edge = handle - 4
                    midpoint = QPointF(*self._edge_midpoints(start)[edge])
                    delta = world - midpoint
                    for index in (edge, (edge + 1) % 4):
                        target[index] = (
                            start[index][0] + delta.x(),
                            start[index][1] + delta.y(),
                        )
        if not self._quad_is_valid(target):
            return
        self._selection_transform_quad = target
        if isinstance(obj, VectorDrawingObject):
            layer_x, layer_y = self.chapter.layer_world_translation(
                obj.parent_layer_id
            )
            offset = QPointF(layer_x + obj.x, layer_y + obj.y)
            transform = self._quad_to_quad_transform(start, target)
            width_scale = math.sqrt(abs(transform.determinant()))
            self._selection_vector_preview = {}
            for point_id, source in self._selection_vector_points.items():
                mapped = transform.map(
                    QPointF(*source["position"]) + offset
                ) - offset
                incoming = source["incoming"]
                if incoming is not None:
                    incoming = (
                        transform.map(QPointF(*incoming) + offset) - offset
                    ).toTuple()
                outgoing = source["outgoing"]
                if outgoing is not None:
                    outgoing = (
                        transform.map(QPointF(*outgoing) + offset) - offset
                    ).toTuple()
                self._selection_vector_preview[point_id] = {
                    "position": mapped.toTuple(),
                    "incoming": incoming,
                    "outgoing": outgoing,
                    "width": max(
                        1.0, min(1000.0, float(source["width"]) * width_scale)
                    ),
                }
            self._selection_vector_preview_revision += 1
        self.update()

    def _restore_drawing_transform_model(
        self, model: dict, quad: list[tuple[float, float]] | None,
        pivot: QPointF | None, pivot_custom: bool,
    ) -> None:
        self.replace_chapter(model)
        self._selection_vector_preview.clear()
        self._selection_vector_preview_revision += 1
        self._selection_transform_quad = (
            list(quad) if quad is not None else None
        )
        self._selection_pivot = (
            QPointF(pivot) if pivot is not None else None
        )
        self._selection_pivot_custom = bool(pivot_custom)
        self.update()

    def _finish_drawing_selection_transform(
        self, obj: RasterObject | VectorDrawingObject,
    ) -> bool:
        mode = self._selection_transform_mode
        self._selection_transform_mode = None
        self._selection_transform_handle = None
        if mode == "pivot":
            self._selection_before_model = None
            self._selection_before_tiles = None
            self.update()
            return True
        before = self._selection_before_model
        self._selection_before_model = None
        if isinstance(obj, VectorDrawingObject):
            preview = dict(self._selection_vector_preview)
            changed_strokes: set[str] = set()
            for stroke in obj.strokes:
                for point in stroke.points:
                    mapped = preview.get(point.point_id)
                    if mapped is None:
                        continue
                    point.position = mapped["position"]
                    point.incoming = mapped["incoming"]
                    point.outgoing = mapped["outgoing"]
                    point.width = mapped["width"]
                    changed_strokes.add(stroke.stroke_id)
                if stroke.stroke_id in changed_strokes:
                    stroke.touch_render_revision()
            if changed_strokes:
                obj.touch_revision()
            self._selection_vector_preview.clear()
            self._selection_vector_preview_revision += 1
            self._selection_vector_points.clear()
            if before is not None:
                after = self.chapter.to_dict()
                if before != after:
                    before_quad = list(
                        self._selection_transform_start_quad or []
                    ) or None
                    after_quad = list(
                        self._selection_transform_quad or []
                    ) or None
                    pivot = (
                        QPointF(self._selection_pivot)
                        if self._selection_pivot is not None else None
                    )
                    pivot_custom = self._selection_pivot_custom
                    self.command_stack.push(
                        CallbackCommand(
                            "Transform vector selection",
                            lambda: self._restore_drawing_transform_model(
                                after, after_quad, pivot, pivot_custom
                            ),
                            lambda: self._restore_drawing_transform_model(
                                before, before_quad, pivot, pivot_custom
                            ),
                        ),
                        already_done=True,
                    )
            self._vector_changed(changed_stroke_ids=changed_strokes)
        elif self._selection_before_tiles is not None:
            self._commit_raster_selection_transform(
                obj, self._selection_before_tiles
            )
            self._selection_before_tiles = None
        self.interactionFinished.emit()
        self.update()
        return True

    def _commit_raster_selection_transform(
        self, obj: RasterObject,
        before_tiles: dict[tuple[int, int], QImage],
    ) -> None:
        start = self._selection_transform_start_quad
        destination = self._selection_transform_quad
        if (
            not start or not destination
            or self._drawing_selection_path.isEmpty()
        ):
            return
        layer_x, layer_y = self.chapter.layer_world_translation(
            obj.parent_layer_id
        )
        offset = QPointF(layer_x + obj.x, layer_y + obj.y)
        source_quad = [
            (x - offset.x(), y - offset.y()) for x, y in start
        ]
        destination_local = [
            (x - offset.x(), y - offset.y())
            for x, y in destination
        ]
        transform = self._quad_to_quad_transform(
            source_quad, destination_local
        )
        if not transform.isInvertible():
            return
        source_path = QPainterPath(self._drawing_selection_path)
        target_path = transform.map(source_path)
        result = {
            key: QImage(image) for key, image in before_tiles.items()
        }
        source_keys = self.tiles.keys_for_rect(source_path.boundingRect())
        for key in source_keys:
            image = result.get(key)
            if image is None:
                continue
            painter = QPainter(image)
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.translate(
                -key[0] * obj.tile_size, -key[1] * obj.tile_size
            )
            painter.fillPath(source_path, Qt.black)
            painter.end()
            if self.tiles.is_empty(image):
                result.pop(key, None)
        target_keys = self.tiles.keys_for_rect(
            target_path.boundingRect().adjusted(-2, -2, 2, 2)
        )
        for key in target_keys:
            image = result.get(key)
            if image is None:
                image = self.tiles._empty(obj.tile_size)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.translate(
                -key[0] * obj.tile_size, -key[1] * obj.tile_size
            )
            painter.setClipPath(target_path, Qt.IntersectClip)
            painter.setTransform(transform, True)
            for (source_x, source_y), source_image in before_tiles.items():
                painter.drawImage(
                    source_x * obj.tile_size,
                    source_y * obj.tile_size,
                    source_image,
                )
            painter.end()
            if self.tiles.is_empty(image):
                result.pop(key, None)
            else:
                result[key] = image
        before_frame = tuple(obj.interaction_rect)
        before_state = {
            "frame": before_frame,
            "path": QPainterPath(source_path),
            "quad": list(start),
            "pivot": (
                QPointF(self._selection_pivot)
                if self._selection_pivot is not None else None
            ),
            "pivot_custom": self._selection_pivot_custom,
        }
        self.tiles.replace_object_tiles(obj.object_id, result)
        content = self.tiles.content_bounds(obj.object_id)
        if content is not None:
            frame = content.adjusted(
                -RASTER_FRAME_MARGIN, -RASTER_FRAME_MARGIN,
                RASTER_FRAME_MARGIN, RASTER_FRAME_MARGIN,
            )
            obj.interaction_rect = (
                frame.left(), frame.top(), frame.width(), frame.height()
            )
        after_frame = tuple(obj.interaction_rect)
        after_state = {
            "frame": after_frame,
            "path": QPainterPath(target_path),
            "quad": list(destination),
            "pivot": (
                QPointF(self._selection_pivot)
                if self._selection_pivot is not None else None
            ),
            "pivot_custom": self._selection_pivot_custom,
        }
        all_keys = set(before_tiles) | set(result)
        before_patch = {
            key: before_tiles.get(key) for key in all_keys
        }
        after_patch = {key: result.get(key) for key in all_keys}
        self.command_stack.push(
            TilePatchCommand(
                "Transform raster selection", self.tiles, obj.object_id,
                before_patch, after_patch,
                lambda: (
                    self.documentChanged.emit(QRectF()), self.update()
                ),
                before_state, after_state,
                lambda state, object_id=obj.object_id:
                self._restore_raster_selection_transform_state(
                    object_id, state
                ),
            ),
            already_done=True,
        )
        self._drawing_selection_path = target_path
        self.documentChanged.emit(QRectF())

    def _restore_raster_selection_transform_state(
        self, object_id: str, state: dict,
    ) -> None:
        self._restore_raster_frame(object_id, state["frame"])
        self._drawing_selection_path = QPainterPath(state["path"])
        self._selection_transform_quad = list(state["quad"])
        pivot = state.get("pivot")
        self._selection_pivot = (
            QPointF(pivot) if pivot is not None else None
        )
        self._selection_pivot_custom = bool(
            state.get("pivot_custom", False)
        )

    def select_all_drawing(self) -> bool:
        """Select every pixel/point in the active drawing."""
        obj = self._drawing_selection_object()
        if obj is None:
            return False
        if isinstance(obj, VectorDrawingObject):
            self._set_vector_selection(
                obj,
                {stroke.stroke_id for stroke in obj.strokes},
                {
                    point.point_id
                    for stroke in obj.strokes
                    for point in stroke.points
                },
            )
        else:
            bounds = self.tiles.content_bounds(obj.object_id)
            if bounds is None:
                bounds = QRectF(*obj.interaction_rect)
            path = QPainterPath()
            path.addRect(bounds)
            self._drawing_selection_path = path
        self._refresh_drawing_selection_transform()
        self.update()
        return True

    def _refresh_drawing_selection_transform(self) -> None:
        obj = self._drawing_selection_object()
        if obj is None:
            self._selection_transform_quad = None
            return
        bounds = QRectF()
        if isinstance(obj, RasterObject):
            bounds = self._drawing_selection_path.boundingRect()
        else:
            points = [
                point
                for stroke in obj.strokes
                for point in stroke.points
                if point.point_id in self._selected_vector_point_ids
            ]
            if points:
                left = min(point.x for point in points)
                right = max(point.x for point in points)
                top = min(point.y for point in points)
                bottom = max(point.y for point in points)
                bounds = QRectF(left, top, right - left, bottom - top)
                if bounds.width() < 1:
                    bounds.adjust(-0.5, 0, 0.5, 0)
                if bounds.height() < 1:
                    bounds.adjust(0, -0.5, 0, 0.5)
        if bounds.isNull() or bounds.isEmpty():
            self._selection_transform_quad = None
            return
        layer_x, layer_y = self.chapter.layer_world_translation(
            obj.parent_layer_id
        )
        offset_x, offset_y = layer_x + obj.x, layer_y + obj.y
        self._selection_transform_quad = [
            (bounds.left() + offset_x, bounds.top() + offset_y),
            (bounds.right() + offset_x, bounds.top() + offset_y),
            (bounds.right() + offset_x, bounds.bottom() + offset_y),
            (bounds.left() + offset_x, bounds.bottom() + offset_y),
        ]
        if self._selection_pivot is None:
            center = bounds.center()
            self._selection_pivot = QPointF(
                center.x() + offset_x, center.y() + offset_y
            )

    def _begin_drawing_selection(
        self, world: QPointF, widget: QPointF, *,
        test_transform: bool = True,
    ) -> bool:
        obj = self._drawing_selection_object()
        if obj is None:
            return False
        self._drawing_selection_operation = self._selection_operation()
        if (
            test_transform
            and self._drawing_selection_operation == "replace"
            and self._begin_drawing_selection_transform(obj, world)
        ):
            return True
        local = self._drawing_local_point(obj, world)
        if self.tool == ToolKind.DRAW_SELECT_STROKE:
            if not isinstance(obj, VectorDrawingObject):
                return False
            hits = self._hit_vector_strokes(obj, local)
            if not hits:
                self._drawing_selection_gesture = [QPointF(local)]
                return True
            stroke_id = hits[0]
            stroke = self._vector_stroke_by_id(obj, stroke_id)
            strokes = set(self._selected_vector_stroke_ids)
            if self._drawing_selection_operation == "replace":
                strokes = {stroke_id}
            elif self._drawing_selection_operation == "add":
                if stroke_id in strokes:
                    strokes.remove(stroke_id)
                else:
                    strokes.add(stroke_id)
            else:
                strokes.discard(stroke_id)
            self._set_vector_selection(
                obj, strokes, self._point_ids_for_strokes(obj, strokes)
            )
            self._reset_drawing_selection_frame()
            return True
        self._drawing_selection_gesture = [QPointF(local)]
        return True

    def _continue_drawing_selection(
        self, world: QPointF, widget: QPointF,
    ) -> bool:
        obj = self._drawing_selection_object()
        if obj is None:
            return False
        if self._selection_transform_mode is not None:
            self._update_drawing_selection_transform(obj, world)
            return True
        local = self._drawing_local_point(obj, world)
        if self.tool == ToolKind.DRAW_SELECT_STROKE:
            if isinstance(obj, VectorDrawingObject):
                if self._drawing_selection_gesture:
                    if math.dist(
                        self._drawing_selection_gesture[-1].toTuple(),
                        local.toTuple(),
                    ) >= 1.5 / max(self.scale, 0.05):
                        self._drawing_selection_gesture.append(QPointF(local))
                        self.update()
                else:
                    hits = self._hit_vector_strokes(obj, local)
                    hovered = hits[0] if hits else ""
                    if hovered != self._hover_vector_stroke_id:
                        self._hover_vector_stroke_id = hovered
                        self.update()
            return True
        if not self._drawing_selection_gesture:
            return False
        if self.tool == ToolKind.DRAW_SELECT_RECT:
            if len(self._drawing_selection_gesture) == 1:
                self._drawing_selection_gesture.append(QPointF(local))
            else:
                self._drawing_selection_gesture[-1] = QPointF(local)
        elif math.dist(
            self._drawing_selection_gesture[-1].toTuple(), local.toTuple()
        ) >= 1.5 / max(self.scale, 0.05):
            self._drawing_selection_gesture.append(QPointF(local))
        self.update()
        return True

    def _finish_drawing_selection(self) -> bool:
        obj = self._drawing_selection_object()
        if obj is not None and self._selection_transform_mode is not None:
            return self._finish_drawing_selection_transform(obj)
        if obj is None or not self._drawing_selection_gesture:
            return False
        gesture = self._drawing_selection_gesture
        self._drawing_selection_gesture = []
        region = QPainterPath()
        if self.tool == ToolKind.DRAW_SELECT_RECT:
            end = gesture[-1]
            region.addRect(QRectF(gesture[0], end).normalized())
        else:
            if len(gesture) < 3:
                if self._drawing_selection_operation == "replace":
                    self._clear_drawing_selection()
                    if isinstance(obj, VectorDrawingObject):
                        self._set_vector_selection(obj, set(), set())
                self.update()
                return True
            polygon = QPolygonF(gesture)
            polygon.append(gesture[0])
            region.addPolygon(polygon)
            region.closeSubpath()
        if isinstance(obj, VectorDrawingObject):
            if self.tool == ToolKind.DRAW_SELECT_STROKE:
                selected_strokes = self._strokes_intersecting_selection_region(
                    obj, region
                )
                if self._drawing_selection_operation == "replace":
                    strokes = selected_strokes
                elif self._drawing_selection_operation == "add":
                    strokes = (
                        self._selected_vector_stroke_ids | selected_strokes
                    )
                else:
                    strokes = (
                        self._selected_vector_stroke_ids - selected_strokes
                    )
                self._set_vector_selection(
                    obj, strokes,
                    self._point_ids_for_strokes(obj, strokes),
                )
                self._reset_drawing_selection_frame()
                self.update()
                return True
            selected_points = {
                point.point_id
                for stroke in obj.strokes
                for point in stroke.points
                if region.contains(QPointF(point.x, point.y))
            }
            selected_strokes = {
                stroke.stroke_id
                for stroke in obj.strokes
                if any(
                    point.point_id in selected_points
                    for point in stroke.points
                )
            }
            if self._drawing_selection_operation == "replace":
                points, strokes = selected_points, selected_strokes
            elif self._drawing_selection_operation == "add":
                points = self._selected_vector_point_ids | selected_points
                strokes = self._stroke_ids_containing_points(obj, points)
            else:
                points = self._selected_vector_point_ids - selected_points
                strokes = self._stroke_ids_containing_points(obj, points)
            self._set_vector_selection(obj, strokes, points)
        else:
            current = self._drawing_selection_path
            if self._drawing_selection_operation == "replace":
                current = region
            elif self._drawing_selection_operation == "add":
                current = current.united(region)
            else:
                current = current.subtracted(region)
            self._drawing_selection_path = current.simplified()
        self._reset_drawing_selection_frame()
        self.update()
        return True

    def _draw_drawing_selection(self, painter: QPainter) -> None:
        obj = self._drawing_selection_object()
        if obj is None:
            return
        layer_x, layer_y = self.chapter.layer_world_translation(
            obj.parent_layer_id
        )
        offset = QPointF(layer_x + obj.x, layer_y + obj.y)
        painter.save()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(
            QColor("#59c9ff"), 1.5 / max(self.scale, 0.05),
            Qt.DashLine,
        ))
        if isinstance(obj, RasterObject) and not self._drawing_selection_path.isEmpty():
            painter.translate(offset)
            painter.drawPath(self._drawing_selection_path)
            painter.translate(-offset)
        if self._drawing_selection_gesture:
            preview = QPainterPath()
            if self.tool == ToolKind.DRAW_SELECT_RECT:
                preview.addRect(QRectF(
                    self._drawing_selection_gesture[0],
                    self._drawing_selection_gesture[-1],
                ).normalized())
            else:
                preview.addPolygon(QPolygonF(self._drawing_selection_gesture))
            painter.translate(offset)
            painter.drawPath(preview)
            painter.translate(-offset)
        if self._hover_vector_stroke_id and isinstance(
            obj, VectorDrawingObject
        ):
            stroke = self._vector_stroke_by_id(
                obj, self._hover_vector_stroke_id
            )
            if stroke is not None:
                painter.setPen(QPen(
                    QColor("#239cff"), 5 / max(self.scale, 0.05),
                    Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin,
                ))
                painter.translate(offset)
                painter.drawPath(self._vector_centerline_path(stroke))
                painter.translate(-offset)
        quad = self._selection_transform_quad
        if quad:
            painter.setPen(QPen(
                QColor("#249eff"), 1.5 / max(self.scale, 0.05)
            ))
            painter.drawPolygon(QPolygonF([QPointF(*point) for point in quad]))
            radius = 6 / max(self.scale, 0.05)
            painter.setBrush(QColor("#ffffff"))
            for point in self._quad_handles(quad):
                painter.drawRect(QRectF(
                    point[0] - radius, point[1] - radius,
                    radius * 2, radius * 2,
                ))
            top = self._edge_midpoints(quad)[0]
            center = QPointF(
                sum(point[0] for point in quad) / 4,
                sum(point[1] for point in quad) / 4,
            )
            direction = QPointF(top[0], top[1]) - center
            length = max(1e-6, math.hypot(direction.x(), direction.y()))
            rotate = QPointF(top[0], top[1]) + direction / length * (
                28 / max(self.scale, 0.05)
            )
            painter.drawLine(QPointF(*top), rotate)
            painter.drawEllipse(rotate, radius, radius)
            pivot = self._selection_pivot or center
            cross = 8 / max(self.scale, 0.05)
            painter.drawLine(
                QPointF(pivot.x() - cross, pivot.y()),
                QPointF(pivot.x() + cross, pivot.y()),
            )
            painter.drawLine(
                QPointF(pivot.x(), pivot.y() - cross),
                QPointF(pivot.x(), pivot.y() + cross),
            )
        hover = self._pointer_hover_widget or self._tablet_hover_widget
        operation = self._selection_operation()
        if hover is not None and operation in {"add", "remove"}:
            marker = self.widget_to_document(hover) + QPointF(
                14 / max(self.scale, 0.05),
                -10 / max(self.scale, 0.05),
            )
            font = painter.font()
            font.setPixelSize(max(1, round(15 / max(self.scale, 0.05))))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QPen(QColor("#239cff")))
            painter.drawText(marker, "+" if operation == "add" else "−")
        painter.restore()

    # ---- vector drawing tools -----------------------------------------
    def _selected_vector_drawing(self) -> VectorDrawingObject | None:
        if self.chapter is None or self.selected_kind != "object":
            return None
        candidate = self.chapter.objects.get(self.selected_id)
        return (
            candidate if isinstance(candidate, VectorDrawingObject) else None
        )

    @property
    def selected_vector_stroke_ids(self) -> set[str]:
        return set(self._selected_vector_stroke_ids)

    @property
    def selected_vector_point_ids(self) -> set[str]:
        return set(self._selected_vector_point_ids)

    def _set_vector_selection(
        self,
        drawing: VectorDrawingObject,
        stroke_ids: set[str] | None = None,
        point_ids: set[str] | None = None,
    ) -> None:
        live_strokes = {stroke.stroke_id for stroke in drawing.strokes}
        live_points = {
            point.point_id
            for stroke in drawing.strokes
            for point in stroke.points
        }
        if stroke_ids is not None:
            self._selected_vector_stroke_ids = stroke_ids & live_strokes
        else:
            self._selected_vector_stroke_ids &= live_strokes
        if point_ids is not None:
            self._selected_vector_point_ids = point_ids & live_points
        else:
            self._selected_vector_point_ids &= live_points
        self.vectorSelectionChanged.emit(
            set(self._selected_vector_stroke_ids),
            set(self._selected_vector_point_ids),
        )
        self.update()

    @staticmethod
    def _vector_point_by_id(
        drawing: VectorDrawingObject, point_id: str,
    ) -> VectorStrokePoint | None:
        return next((
            point
            for stroke in drawing.strokes
            for point in stroke.points
            if point.point_id == point_id
        ), None)

    @staticmethod
    def _vector_stroke_by_id(
        drawing: VectorDrawingObject, stroke_id: str,
    ) -> VectorStroke | None:
        return next((
            stroke for stroke in drawing.strokes
            if stroke.stroke_id == stroke_id
        ), None)

    def _hit_vector_strokes(
        self, drawing: VectorDrawingObject, local: QPointF,
        tolerance: float | None = None,
    ) -> list[str]:
        tolerance = (
            8.0 / max(self.scale, 0.05)
            if tolerance is None else tolerance
        )
        return [
            stroke.stroke_id
            for stroke in reversed(drawing.strokes)
            if centerline_hit(
                stroke.points,
                (local.x(), local.y()),
                closed=stroke.closed,
                extra_tolerance=tolerance,
            ) is not None
        ]

    def _hit_vector_point(
        self, drawing: VectorDrawingObject, local: QPointF,
    ) -> tuple[str, str] | None:
        tolerance = 11.0 / max(self.scale, 0.05)
        candidates: list[tuple[float, str, str]] = []
        for stroke in reversed(drawing.strokes):
            if stroke.stroke_id not in self._selected_vector_stroke_ids:
                continue
            for point in stroke.points:
                separation = math.dist(
                    (local.x(), local.y()), point.position
                )
                if separation <= tolerance:
                    candidates.append(
                        (separation, stroke.stroke_id, point.point_id)
                    )
        if not candidates:
            return None
        _, stroke_id, point_id = min(candidates)
        return stroke_id, point_id

    def _begin_vector_edit_pointer(
        self, drawing: VectorDrawingObject, local: QPointF,
    ) -> bool:
        operation = self._selection_operation()
        point_hit = self._hit_vector_point(drawing, local)
        if point_hit is not None:
            stroke_id, point_id = point_hit
            strokes = set(self._selected_vector_stroke_ids)
            strokes.add(stroke_id)
            points = set(self._selected_vector_point_ids)
            if operation == "remove":
                points.discard(point_id)
                self._set_vector_selection(drawing, strokes, points)
                return True
            if operation == "add":
                points.add(point_id)
            elif point_id not in points:
                points = {point_id}
            self._set_vector_selection(drawing, strokes, points)
            self._vector_before = {
                drawing.object_id: drawing.to_dict()
            }
            self._vector_drag_origin = QPointF(local)
            self._vector_drag_points = {}
            for selected_id in self._selected_vector_point_ids:
                point = self._vector_point_by_id(drawing, selected_id)
                if point is not None:
                    self._vector_drag_points[selected_id] = (
                        point.position, point.incoming, point.outgoing
                    )
            self._vector_gesture_mode = "edit_drag"
            return True
        stroke_hits = self._hit_vector_strokes(drawing, local)
        if stroke_hits:
            stroke_id = stroke_hits[0]
            strokes = set(self._selected_vector_stroke_ids)
            points = set(self._selected_vector_point_ids)
            if operation == "remove":
                strokes.discard(stroke_id)
                stroke = self._vector_stroke_by_id(drawing, stroke_id)
                if stroke is not None:
                    points -= {
                        point.point_id for point in stroke.points
                    }
            elif operation == "add":
                strokes.add(stroke_id)
            else:
                strokes = {stroke_id}
                points = set()
            if operation == "remove":
                point_ids = points
            elif operation == "add":
                point_ids = {
                    point_id for point_id in points
                    if any(
                        stroke.stroke_id in strokes
                        and any(
                            point.point_id == point_id
                            for point in stroke.points
                        )
                        for stroke in drawing.strokes
                    )
                }
            else:
                point_ids = set()
            self._set_vector_selection(drawing, strokes, point_ids)
            return True
        self._set_vector_selection(drawing, set(), set())
        return False

    def _begin_vector_point_select(
        self, drawing: VectorDrawingObject, local: QPointF,
    ) -> None:
        operation = self._selection_operation()
        self._drawing_selection_operation = operation
        point_hit = self._hit_vector_point(drawing, local)
        strokes = set(self._selected_vector_stroke_ids)
        points = set(self._selected_vector_point_ids)
        if point_hit is not None:
            stroke_id, point_id = point_hit
            if operation == "remove":
                points.discard(point_id)
                strokes = self._stroke_ids_containing_points(
                    drawing, points
                )
            elif operation == "add":
                points.add(point_id)
                strokes.add(stroke_id)
            else:
                points = {point_id}
                strokes = {stroke_id}
        else:
            hits = self._hit_vector_strokes(drawing, local)
            if hits:
                stroke_id = hits[0]
                stroke = self._vector_stroke_by_id(drawing, stroke_id)
                stroke_points = {
                    point.point_id for point in stroke.points
                } if stroke else set()
                if operation == "remove":
                    points -= stroke_points
                    strokes.discard(stroke_id)
                elif operation == "add":
                    strokes.add(stroke_id)
                    points |= stroke_points
                else:
                    strokes = {stroke_id}
                    points = stroke_points
            elif operation == "replace":
                strokes.clear()
                points.clear()
        self._set_vector_selection(drawing, strokes, points)
        self._vector_gesture_mode = "point_select"
        self._vector_sweep = [
            FreehandSample(local.x(), local.y(), 1.0)
        ]

    def _continue_vector_point_select(
        self, drawing: VectorDrawingObject, local: QPointF,
    ) -> None:
        radius = 11.0 / max(self.scale, 0.05)
        points = set(self._selected_vector_point_ids)
        for stroke in drawing.strokes:
            for point in stroke.points:
                if math.dist(point.position, (local.x(), local.y())) <= radius:
                    if self._drawing_selection_operation == "remove":
                        points.discard(point.point_id)
                    else:
                        points.add(point.point_id)
        self._set_vector_selection(
            drawing, self._stroke_ids_containing_points(drawing, points),
            points,
        )

    def _update_vector_anchor_drag(
        self, drawing: VectorDrawingObject, local: QPointF,
    ) -> None:
        delta = local - self._vector_drag_origin
        point_strokes = {
            point.point_id: stroke.stroke_id
            for stroke in drawing.strokes for point in stroke.points
        }
        changed_strokes: set[str] = set()
        for point_id, (position, incoming, outgoing) in (
            self._vector_drag_points.items()
        ):
            point = self._vector_point_by_id(drawing, point_id)
            if point is None:
                continue
            point.x = position[0] + delta.x()
            point.y = position[1] + delta.y()
            if incoming is not None:
                point.incoming = (
                    incoming[0] + delta.x(), incoming[1] + delta.y()
                )
            if outgoing is not None:
                point.outgoing = (
                    outgoing[0] + delta.x(), outgoing[1] + delta.y()
                )
            stroke_id = point_strokes.get(point_id)
            if stroke_id:
                changed_strokes.add(stroke_id)
        for stroke in drawing.strokes:
            if stroke.stroke_id in changed_strokes:
                stroke.touch_render_revision()
        if changed_strokes:
            drawing.touch_revision()
        self._vector_changed(changed_stroke_ids=changed_strokes)

    def _vector_pressure_values(self, pressure: float) -> tuple[float, float]:
        pressure = pressure if pressure > 0.001 else 1.0
        width = float(self.settings.pencil_size())
        opacity = 1.0
        if self._preset.pressure_size:
            width *= self._preset.size_curve.evaluate_fast(pressure)
        if self._preset.pressure_opacity:
            opacity = self._preset.opacity_curve.evaluate_fast(pressure)
        return (
            max(1.0, min(1000.0, width)),
            max(0.0, min(1.0, opacity)),
        )

    def _append_vector_sample(
        self, local: QPointF, pressure: float,
    ) -> None:
        sample = FreehandSample(
            local.x(), local.y(),
            pressure if pressure > 0.001 else 1.0,
        )
        if (
            self._vector_samples
            and math.dist(
                self._vector_samples[-1].point, sample.point
            ) < 0.2
        ):
            self._vector_samples[-1] = sample
        else:
            self._vector_samples.append(sample)

    def _begin_vector_pencil(
        self, drawing: VectorDrawingObject, local: QPointF, pressure: float,
    ) -> None:
        self._vector_before = {drawing.object_id: drawing.to_dict()}
        self._vector_gesture_mode = "pencil"
        self._vector_samples = []
        self._append_vector_sample(local, pressure)

    def _finish_vector_pencil(
        self, drawing: VectorDrawingObject,
    ) -> None:
        fitted = fit_freehand(
            self._vector_samples,
            error=self.settings.vector_fit_error,
            resample_spacing=max(
                0.5, min(2.0, self.settings.vector_fit_error / 2)
            ),
        )
        if fitted:
            points: list[VectorStrokePoint] = []
            for fitted_point in fitted:
                width, opacity = self._vector_pressure_values(
                    fitted_point.pressure
                )
                points.append(VectorStrokePoint(
                    x=fitted_point.x,
                    y=fitted_point.y,
                    incoming=fitted_point.incoming,
                    outgoing=fitted_point.outgoing,
                    width=width,
                    opacity=opacity,
                ))
            stroke = VectorStroke(
                color=self.primary_color,
                points=points,
                start_cap="round",
                end_cap="round",
            )
            drawing.strokes.append(stroke)
            drawing.touch_revision()
        before = self._vector_before or {}
        self._vector_gesture_mode = None
        self._vector_samples = []
        self._vector_before = None
        self._push_vector_change(before, "Vector pencil stroke")
        self.interactionFinished.emit()

    def _stroke_from_spans(
        self, source: VectorStroke, spans: list[CubicSpan],
        *, preserve_id: bool = False,
    ) -> VectorStroke | None:
        if not spans:
            return None

        def attributes(segment: int, amount: float) -> tuple[float, float]:
            return (
                interpolate_stroke_attribute(
                    source.points, segment, amount, "width", 1.0,
                    closed=source.closed,
                ),
                interpolate_stroke_attribute(
                    source.points, segment, amount, "opacity", 1.0,
                    closed=source.closed,
                ),
            )

        first = spans[0]
        width, opacity = attributes(first.source_segment, first.t0)
        points = [VectorStrokePoint(
            x=first.cubic[0][0],
            y=first.cubic[0][1],
            width=width,
            opacity=opacity,
        )]
        for span in spans:
            current = points[-1]
            current.outgoing = span.cubic[1]
            width, opacity = attributes(span.source_segment, span.t1)
            points.append(VectorStrokePoint(
                x=span.cubic[3][0],
                y=span.cubic[3][1],
                incoming=span.cubic[2],
                width=width,
                opacity=opacity,
            ))
        starts_original = (
            first.source_segment == 0 and first.t0 <= 1.0e-6
        )
        last = spans[-1]
        ends_original = (
            last.source_segment == len(stroke_cubics(
                source.points, source.closed
            )) - 1
            and last.t1 >= 1 - 1.0e-6
        )
        result = VectorStroke(
            color=source.color,
            points=points,
            start_cap=source.start_cap if starts_original else "round",
            end_cap=source.end_cap if ends_original else "round",
            render_revision=source.render_revision + 1,
        )
        if preserve_id:
            result.stroke_id = source.stroke_id
        return result

    def _stroke_from_cubics(
        self, source: VectorStroke, cubics: list[Cubic],
        *, preserve_id: bool = True, closed: bool = False,
    ) -> VectorStroke | None:
        if not cubics:
            return None
        original = stroke_cubics(source.points, source.closed)

        def attributes(point: tuple[float, float]) -> tuple[float, float]:
            projection = nearest_on_path(original, point)
            if projection is None:
                return source.points[0].width, source.points[0].opacity
            return (
                interpolate_stroke_attribute(
                    source.points,
                    projection.segment_index,
                    projection.t,
                    "width",
                    1.0,
                    closed=source.closed,
                ),
                interpolate_stroke_attribute(
                    source.points,
                    projection.segment_index,
                    projection.t,
                    "opacity",
                    1.0,
                    closed=source.closed,
                ),
            )

        width, opacity = attributes(cubics[0][0])
        points = [VectorStrokePoint(
            x=cubics[0][0][0],
            y=cubics[0][0][1],
            width=width,
            opacity=opacity,
        )]
        for cubic in cubics:
            points[-1].outgoing = cubic[1]
            width, opacity = attributes(cubic[3])
            points.append(VectorStrokePoint(
                x=cubic[3][0], y=cubic[3][1],
                incoming=cubic[2], width=width, opacity=opacity,
            ))
        if closed and math.dist(points[0].position, points[-1].position) < 1e-6:
            closing = points.pop()
            points[0].incoming = closing.incoming
        result = VectorStroke(
            color=source.color,
            closed=closed,
            start_cap=source.start_cap,
            end_cap=source.end_cap,
            points=points,
            render_revision=source.render_revision + 1,
        )
        if preserve_id:
            result.stroke_id = source.stroke_id
        return result

    def _vector_stroke_touched(
        self, stroke: VectorStroke, sweep: list[tuple[float, float]],
        radius: float,
    ) -> bool:
        shape = "square" if self.settings.eraser_square else "round"
        if len(stroke.points) == 1:
            point = stroke.points[0]
            return corridor_contains(
                point.position,
                sweep,
                radius + point.width / 2,
                shape=shape,
            )
        cubics = stroke_cubics(stroke.points, stroke.closed)
        for segment_index, cubic in enumerate(cubics):
            following = (segment_index + 1) % len(stroke.points)
            local_width = max(
                stroke.points[segment_index].width,
                stroke.points[following].width,
            )
            if corridor_hits_path(
                [cubic],
                sweep,
                radius + local_width / 2,
                shape=shape,
            ):
                return True
        return False

    def _erase_vector_intersection_groups(
        self,
        drawing: VectorDrawingObject,
        stroke: VectorStroke,
        cubics: list[Cubic],
        sweep: list[tuple[float, float]],
        radius: float,
    ) -> list[list[CubicSpan]]:
        shape = "square" if self.settings.eraser_square else "round"
        touched: list[tuple[float, float]] = []
        for segment_index, cubic in enumerate(cubics):
            following = (segment_index + 1) % len(stroke.points)
            local_width = max(
                stroke.points[segment_index].width,
                stroke.points[following].width,
            )
            for _, start, end in corridor_path_intervals(
                [cubic],
                sweep,
                radius + local_width / 2,
                shape=shape,
            ):
                parameter = (start + end) / 2
                touched.append((
                    distance_to_polyline(
                        cubic_eval(cubic, parameter), sweep
                    ),
                    segment_index + parameter,
                ))
        if not touched:
            return [[
                CubicSpan(cubic, index, 0.0, 1.0)
                for index, cubic in enumerate(cubics)
            ]]
        scalar = min(touched)[1]
        cuts: list[float] = []
        for intersection in path_self_intersections(
            cubics, closed=stroke.closed
        ):
            cuts.extend((
                intersection.first_segment + intersection.first_t,
                intersection.second_segment + intersection.second_t,
            ))
        for other in drawing.strokes:
            if other is stroke or len(other.points) < 2:
                continue
            for intersection in path_intersections(
                cubics, stroke_cubics(other.points, other.closed)
            ):
                cuts.append(
                    intersection.first_segment + intersection.first_t
                )
        cuts = sorted({
            round(value, 9) for value in cuts
            if abs(value - scalar) > 1.0e-6
        })
        length = float(len(cubics))
        if not stroke.closed:
            cuts = [0.0, *cuts, length]
        if not cuts:
            return []
        before = max(
            (value for value in cuts if value < scalar), default=None
        )
        after = min(
            (value for value in cuts if value > scalar), default=None
        )
        if stroke.closed:
            if before is None:
                before = max(cuts) - length
            if after is None:
                after = min(cuts) + length
        if before is None or after is None:
            return []
        erased: dict[int, list[tuple[float, float]]] = {}
        cursor = before
        while cursor < after - 1.0e-8:
            wrapped = cursor % len(cubics)
            index = int(math.floor(wrapped)) % len(cubics)
            start = wrapped - math.floor(wrapped)
            amount = min(1.0 - start, after - cursor)
            erased.setdefault(index, []).append(
                (start, start + amount)
            )
            cursor += amount

        def complement(
            values: list[tuple[float, float]],
        ) -> list[tuple[float, float]]:
            if not values:
                return [(0.0, 1.0)]
            merged: list[list[float]] = []
            for start, end in sorted(values):
                if not merged or start > merged[-1][1] + 1.0e-7:
                    merged.append([start, end])
                else:
                    merged[-1][1] = max(merged[-1][1], end)
            result = []
            position = 0.0
            for start, end in merged:
                if start > position + 1.0e-7:
                    result.append((position, start))
                position = max(position, end)
            if position < 1.0 - 1.0e-7:
                result.append((position, 1.0))
            return result

        groups: list[list[CubicSpan]] = []
        current: list[CubicSpan] = []
        for index, cubic in enumerate(cubics):
            kept = complement(erased.get(index, []))
            for kept_index, (start, end) in enumerate(kept):
                if (
                    current
                    and not (
                        current[-1].source_segment == index - 1
                        and current[-1].t1 >= 1 - 1.0e-7
                        and start <= 1.0e-7
                        and kept_index == 0
                    )
                ):
                    groups.append(current)
                    current = []
                current.append(CubicSpan(
                    cubic_subsegment(cubic, start, end),
                    index,
                    start,
                    end,
                ))
                if end < 1 - 1.0e-7:
                    groups.append(current)
                    current = []
            if not kept and current:
                groups.append(current)
                current = []
        if current:
            groups.append(current)
        return groups

    def _finish_vector_eraser(
        self, drawing: VectorDrawingObject,
    ) -> None:
        before = self._vector_before or {}
        if before:
            self._restore_vector_payloads(before)
            drawing = self._selected_vector_drawing() or drawing
        self._apply_vector_eraser_sweep(drawing)
        self._set_vector_selection(drawing)
        self._vector_gesture_mode = None
        self._vector_sweep = []
        self._vector_before = None
        self._push_vector_change(before, "Vector eraser")
        self.interactionFinished.emit()

    def _apply_vector_eraser_sweep(
        self, drawing: VectorDrawingObject,
    ) -> bool:
        sweep = [sample.point for sample in self._vector_sweep]
        if not sweep:
            return False
        radius = self.settings.active_eraser_pixels() / 2
        mode = self.settings.vector_eraser_mode
        rebuilt: list[VectorStroke] = []
        changed = False
        changed_strokes: set[str] = set()
        for stroke in drawing.strokes:
            if not self._vector_stroke_touched(stroke, sweep, radius):
                rebuilt.append(stroke)
                continue
            changed = True
            changed_strokes.add(stroke.stroke_id)
            if mode == "stroke" or len(stroke.points) == 1:
                continue
            cubics = stroke_cubics(stroke.points, stroke.closed)
            groups = (
                self._erase_vector_intersection_groups(
                    drawing, stroke, cubics, sweep, radius
                )
                if mode == "intersection"
                else erase_stroke_by_corridor(
                    stroke.points,
                    sweep,
                    radius,
                    shape=(
                        "square" if self.settings.eraser_square else "round"
                    ),
                    closed=stroke.closed,
                )
            )
            for group_index, group in enumerate(groups):
                replacement = self._stroke_from_spans(
                    stroke, group, preserve_id=group_index == 0
                )
                if replacement is not None:
                    rebuilt.append(replacement)
        if changed:
            drawing.strokes = rebuilt
            drawing.touch_revision()
            self._vector_changed(changed_stroke_ids=changed_strokes)
        return changed

    def _manual_redraw_at(
        self, drawing: VectorDrawingObject,
        local: QPointF, pressure: float,
    ) -> None:
        radius = 12.0 / max(self.scale, 0.05)
        curve = (
            self._preset.size_curve
            if self.settings.vector_redraw_parameter == "thickness"
            else self._preset.opacity_curve
        )
        mapped = curve.evaluate_fast(
            pressure if pressure > 0.001 else 1.0
        )
        changed = False
        changed_strokes: set[str] = set()
        for stroke in drawing.strokes:
            for point in stroke.points:
                if math.dist(point.position, (local.x(), local.y())) > radius:
                    continue
                if self.settings.vector_redraw_parameter == "thickness":
                    point.width = max(
                        1.0,
                        min(
                            1000.0,
                            1.0
                            + (
                                self.settings.vector_redraw_thickness_max - 1
                            ) * mapped,
                        ),
                    )
                else:
                    point.opacity = max(
                        0.0,
                        min(
                            1.0,
                            self.settings.vector_redraw_opacity_max
                            / 100 * mapped,
                        ),
                    )
                changed = True
                changed_strokes.add(stroke.stroke_id)
        if changed:
            drawing.touch_revision()
            for stroke in drawing.strokes:
                if stroke.stroke_id in changed_strokes:
                    stroke.touch_render_revision()
            self._vector_changed(changed_stroke_ids=changed_strokes)

    def _vector_target_points(
        self, drawing: VectorDrawingObject,
    ) -> list[VectorStrokePoint]:
        if self._selected_vector_point_ids:
            return [
                point
                for stroke in drawing.strokes
                for point in stroke.points
                if point.point_id in self._selected_vector_point_ids
            ]
        if self._selected_vector_stroke_ids:
            return [
                point
                for stroke in drawing.strokes
                if stroke.stroke_id in self._selected_vector_stroke_ids
                for point in stroke.points
            ]
        return [
            point for stroke in drawing.strokes for point in stroke.points
        ]

    def apply_vector_redraw(
        self,
        parameter: str | None = None,
        operation: str | None = None,
        value: float | None = None,
    ) -> bool:
        drawing = self._selected_vector_drawing()
        if drawing is None:
            return False
        targets = self._vector_target_points(drawing)
        if not targets:
            return False
        before = {drawing.object_id: drawing.to_dict()}
        parameter = parameter or self.settings.vector_redraw_parameter
        operation = operation or self.settings.vector_redraw_operation
        amount = (
            self.settings.vector_redraw_amount
            if value is None else float(value)
        )
        if parameter not in {"thickness", "opacity"}:
            return False
        if operation not in {"increase", "decrease", "uniform"}:
            return False
        changed_strokes: set[str] = set()
        point_strokes = {
            point.point_id: stroke.stroke_id
            for stroke in drawing.strokes for point in stroke.points
        }
        for point in targets:
            current = point.width if parameter == "thickness" else point.opacity
            value = amount if parameter == "thickness" else amount / 100
            if operation == "increase":
                current += value
            elif operation == "decrease":
                current -= value
            else:
                current = value
            if parameter == "thickness":
                point.width = max(1.0, min(1000.0, current))
            else:
                point.opacity = max(0.0, min(1.0, current))
            stroke_id = point_strokes.get(point.point_id)
            if stroke_id:
                changed_strokes.add(stroke_id)
        drawing.touch_revision()
        for stroke in drawing.strokes:
            if stroke.stroke_id in changed_strokes:
                stroke.touch_render_revision()
        return self._push_vector_change(before, "Redraw vector parameter")

    def _simplify_vector_stroke(
        self, stroke: VectorStroke,
    ) -> VectorStroke:
        cubics = stroke_cubics(stroke.points, stroke.closed)
        if not cubics:
            return stroke
        rebuilt, _mapping = self._simplify_vector_stroke_segments(
            stroke, set(range(len(cubics))), set()
        )
        return rebuilt

    @staticmethod
    def _segments_incident_to_points(
        stroke: VectorStroke, point_ids: set[str],
    ) -> set[int]:
        count = len(stroke.points)
        if count < 2:
            return set()
        result: set[int] = set()
        for index, point in enumerate(stroke.points):
            if point.point_id not in point_ids:
                continue
            if stroke.closed:
                result.update({(index - 1) % count, index % count})
            else:
                if index > 0:
                    result.add(index - 1)
                if index < count - 1:
                    result.add(index)
        return result

    def _simplify_vector_stroke_segments(
        self, stroke: VectorStroke, segment_indexes: set[int],
        remap_point_ids: set[str],
    ) -> tuple[VectorStroke, dict[str, str]]:
        cubics = stroke_cubics(stroke.points, stroke.closed)
        if not cubics or not segment_indexes:
            return stroke, {
                point_id: point_id for point_id in remap_point_ids
            }
        rebuilt_cubics = simplify_cubic_segments(
            cubics, segment_indexes,
            self.settings.vector_simplify_amount,
            closed=stroke.closed,
        )
        rebuilt = self._stroke_from_cubics(
            stroke, rebuilt_cubics,
            preserve_id=True, closed=stroke.closed,
        ) or stroke
        # Keep stable IDs for every anchor that survives at the same position,
        # including every boundary of an untouched cubic run.
        used: set[int] = set()
        preserved: dict[str, str] = {}
        for original in stroke.points:
            candidates = [
                (math.dist(original.position, candidate.position), index)
                for index, candidate in enumerate(rebuilt.points)
                if index not in used
            ]
            if not candidates:
                continue
            distance, index = min(candidates)
            if distance <= 1.0e-6:
                rebuilt.points[index].point_id = original.point_id
                used.add(index)
                preserved[original.point_id] = original.point_id
        mapping: dict[str, str] = {}
        for point_id in remap_point_ids:
            if point_id in preserved:
                mapping[point_id] = point_id
                continue
            original = next((
                point for point in stroke.points
                if point.point_id == point_id
            ), None)
            if original is None or not rebuilt.points:
                continue
            nearest = min(
                rebuilt.points,
                key=lambda point: math.dist(
                    original.position, point.position
                ),
            )
            mapping[point_id] = nearest.point_id
        return rebuilt, mapping

    def apply_vector_simplify(self, amount: int | None = None) -> bool:
        drawing = self._selected_vector_drawing()
        if drawing is None or not drawing.strokes:
            return False
        previous_amount = self.settings.vector_simplify_amount
        if amount is not None:
            self.settings.vector_simplify_amount = max(
                0, min(100, int(amount))
            )
        selected_points = set(self._selected_vector_point_ids)
        selected_strokes = set(self._selected_vector_stroke_ids)
        before = {drawing.object_id: drawing.to_dict()}
        rebuilt_strokes: list[VectorStroke] = []
        remapped_points: set[str] = set()
        affected_strokes: set[str] = set()
        for stroke in drawing.strokes:
            if selected_points:
                local_points = {
                    point.point_id for point in stroke.points
                    if point.point_id in selected_points
                }
                segments = self._segments_incident_to_points(
                    stroke, local_points
                )
            elif not selected_strokes or stroke.stroke_id in selected_strokes:
                local_points = set()
                segments = set(range(len(stroke_cubics(
                    stroke.points, stroke.closed
                ))))
            else:
                local_points, segments = set(), set()
            if not segments:
                rebuilt_strokes.append(stroke)
                continue
            rebuilt, mapping = self._simplify_vector_stroke_segments(
                stroke, segments, local_points
            )
            rebuilt_strokes.append(rebuilt)
            affected_strokes.add(stroke.stroke_id)
            remapped_points.update(mapping.values())
        drawing.strokes = rebuilt_strokes
        if not affected_strokes:
            if amount is not None:
                self.settings.vector_simplify_amount = previous_amount
            return False
        drawing.touch_revision()
        for stroke in drawing.strokes:
            if stroke.stroke_id in affected_strokes:
                stroke.render_revision = max(stroke.render_revision, 1)
        if selected_points:
            self._set_vector_selection(
                drawing, affected_strokes, remapped_points
            )
        else:
            self._set_vector_selection(
                drawing,
                selected_strokes if selected_strokes else affected_strokes,
                set(),
            )
        changed = self._push_vector_change(before, "Simplify vector line")
        if amount is not None:
            self.settings.vector_simplify_amount = previous_amount
        return changed

    def _collect_vector_endpoint(
        self, drawing: VectorDrawingObject, local: QPointF,
    ) -> None:
        candidates: list[tuple[float, str, str]] = []
        for stroke in reversed(drawing.strokes):
            if not stroke.points:
                continue
            endpoints = [("start", stroke.points[0])]
            if len(stroke.points) > 1 and not stroke.closed:
                endpoints.append(("end", stroke.points[-1]))
            for end, point in endpoints:
                separation = math.dist(
                    point.position, (local.x(), local.y())
                )
                tolerance = max(
                    12.0 / max(self.scale, 0.05), point.width / 2
                )
                if separation <= tolerance:
                    candidates.append(
                        (separation, stroke.stroke_id, end)
                    )
        if candidates:
            _, stroke_id, endpoint = min(candidates)
            candidate = stroke_id, endpoint
            if candidate not in self._vector_connect_endpoints:
                self._vector_connect_endpoints.append(candidate)

    @staticmethod
    def _reverse_vector_points(
        points: list[VectorStrokePoint],
    ) -> list[VectorStrokePoint]:
        result: list[VectorStrokePoint] = []
        for point in reversed(points):
            result.append(VectorStrokePoint(
                point_id=point.point_id,
                x=point.x,
                y=point.y,
                incoming=point.outgoing,
                outgoing=point.incoming,
                width=point.width,
                opacity=point.opacity,
            ))
        return result

    def _finish_vector_connect(
        self, drawing: VectorDrawingObject,
    ) -> None:
        before = self._vector_before or {}
        endpoints = self._vector_connect_endpoints[:2]
        changed = False
        if len(endpoints) == 2:
            first_id, first_end = endpoints[0]
            second_id, second_end = endpoints[1]
            first = self._vector_stroke_by_id(drawing, first_id)
            second = self._vector_stroke_by_id(drawing, second_id)
            if first is not None and second is not None:
                if first is second and first_end != second_end:
                    oriented = (
                        self._reverse_vector_points(first.points)
                        if first_end == "start" else [
                            VectorStrokePoint.from_dict(point.to_dict())
                            for point in first.points
                        ]
                    )
                    cubics = stroke_cubics(oriented, False)
                    departure = (
                        cubic_derivative(cubics[-1], 1)
                        if cubics else (1.0, 0.0)
                    )
                    arrival = (
                        cubic_derivative(cubics[0], 0)
                        if cubics else (1.0, 0.0)
                    )
                    bridge = tangent_bridge(
                        oriented[-1].position,
                        oriented[0].position,
                        departure,
                        arrival,
                    )
                    oriented[-1].outgoing = bridge[1]
                    oriented[0].incoming = bridge[2]
                    first.points = oriented
                    first.closed = True
                    changed = True
                elif first is not second:
                    first_points = (
                        self._reverse_vector_points(first.points)
                        if first_end == "start" else [
                            VectorStrokePoint.from_dict(point.to_dict())
                            for point in first.points
                        ]
                    )
                    second_points = (
                        self._reverse_vector_points(second.points)
                        if second_end == "end" else [
                            VectorStrokePoint.from_dict(point.to_dict())
                            for point in second.points
                        ]
                    )
                    first_cubics = stroke_cubics(first_points, False)
                    second_cubics = stroke_cubics(second_points, False)
                    departure = (
                        cubic_derivative(first_cubics[-1], 1)
                        if first_cubics else (
                            second_points[0].x - first_points[-1].x,
                            second_points[0].y - first_points[-1].y,
                        )
                    )
                    arrival = (
                        cubic_derivative(second_cubics[0], 0)
                        if second_cubics else departure
                    )
                    bridge = tangent_bridge(
                        first_points[-1].position,
                        second_points[0].position,
                        departure,
                        arrival,
                    )
                    first_points[-1].outgoing = bridge[1]
                    second_points[0].incoming = bridge[2]
                    first.points = [*first_points, *second_points]
                    first.closed = False
                    drawing.strokes = [
                        stroke for stroke in drawing.strokes
                        if stroke is not second
                    ]
                    changed = True
        if changed:
            drawing.touch_revision()
        self._vector_gesture_mode = None
        self._vector_sweep = []
        self._vector_connect_endpoints = []
        self._vector_before = None
        self._set_vector_selection(drawing)
        self._push_vector_change(before, "Connect vector lines")
        self.interactionFinished.emit()

    def _finish_vector_simplify_gesture(
        self, drawing: VectorDrawingObject,
    ) -> None:
        before = self._vector_before or {}
        changed = False
        rebuilt: list[VectorStroke] = []
        affected_strokes: set[str] = set()
        remapped_points: set[str] = set()
        selected_points = set(self._vector_simplify_point_ids)
        for stroke in drawing.strokes:
            local_points = {
                point.point_id for point in stroke.points
                if point.point_id in selected_points
            }
            segments = self._segments_incident_to_points(
                stroke, local_points
            )
            if not segments:
                rebuilt.append(stroke)
                continue
            simplified, mapping = self._simplify_vector_stroke_segments(
                stroke, segments, local_points
            )
            rebuilt.append(simplified)
            affected_strokes.add(stroke.stroke_id)
            remapped_points.update(mapping.values())
            changed = True
        if changed:
            drawing.strokes = rebuilt
            drawing.touch_revision()
            for stroke in drawing.strokes:
                if stroke.stroke_id in affected_strokes:
                    stroke.render_revision = max(
                        stroke.render_revision, 1
                    )
        self._vector_gesture_mode = None
        self._vector_sweep = []
        self._vector_simplify_point_ids.clear()
        self._vector_simplify_anchor_grid.clear()
        self._vector_simplify_last_sample = None
        self._vector_simplify_overlay.clear()
        self._vector_before = None
        self._set_vector_selection(
            drawing, affected_strokes, remapped_points
        )
        self._push_vector_change(before, "Simplify vector line")
        self.tool = ToolKind.VECTOR_EDIT
        self.toolChanged.emit(self.tool)
        self.interactionFinished.emit()

    def _build_simplify_anchor_index(
        self, drawing: VectorDrawingObject,
    ) -> None:
        """Build a small document-space grid once per simplify gesture."""
        cell = max(1.0, 12.0 / max(self.scale, 0.05))
        self._vector_simplify_grid_size = cell
        grid: dict[
            tuple[int, int], list[tuple[str, str, tuple[float, float]]]
        ] = {}
        for stroke in drawing.strokes:
            for point in stroke.points:
                key = (
                    math.floor(point.x / cell),
                    math.floor(point.y / cell),
                )
                grid.setdefault(key, []).append((
                    stroke.stroke_id, point.point_id, point.position
                ))
        self._vector_simplify_anchor_grid = grid
        self._vector_simplify_last_sample = None
        self._vector_simplify_overlay = []

    def _update_simplify_point_sweep(
        self, drawing: VectorDrawingObject,
    ) -> None:
        if not self._vector_sweep:
            return
        if not self._vector_simplify_anchor_grid:
            self._build_simplify_anchor_index(drawing)
        radius = self._vector_simplify_grid_size
        sample = self._vector_sweep[-1]
        current = sample.point
        previous = self._vector_simplify_last_sample
        if previous is None:
            previous = current
        # Only query cells touched by the newest segment.  This avoids the
        # previous O(strokes * points * sweep_samples) rescans.
        min_x = math.floor((min(previous[0], current[0]) - radius) / radius)
        max_x = math.floor((max(previous[0], current[0]) + radius) / radius)
        min_y = math.floor((min(previous[1], current[1]) - radius) / radius)
        max_y = math.floor((max(previous[1], current[1]) + radius) / radius)
        candidates: dict[str, tuple[float, float]] = {}
        for gx in range(min_x, max_x + 1):
            for gy in range(min_y, max_y + 1):
                for _stroke_id, point_id, position in self._vector_simplify_anchor_grid.get((gx, gy), ()):
                    candidates[point_id] = position
        for point_id, position in candidates.items():
            if self._point_segment_distance(
                QPointF(*position), previous, current
            ) <= radius:
                self._vector_simplify_point_ids.add(point_id)
        self._vector_simplify_last_sample = current
        # Keep the visual brush path compact; the model target set remains
        # exact and is not affected by this decimation.
        if (
            not self._vector_simplify_overlay
            or math.dist(
                self._vector_simplify_overlay[-1].point, current
            ) >= max(0.5, radius * 0.35)
        ):
            self._vector_simplify_overlay.append(sample)

    def _vector_fill_settings(self) -> dict:
        return {
            "close_gaps": self.settings.fill_close_gaps,
            "gap_threshold": self.settings.fill_gap_threshold,
            "fill_narrow_areas": self.settings.fill_narrow_areas,
            "area_scaling": self.settings.fill_area_scaling,
            "area_amount": self.settings.fill_area_amount,
            "area_mode": self.settings.fill_area_mode,
            "mode": self.settings.fill_mode,
        }

    def _face_path(
        self, face, drawing: VectorDrawingObject | None = None,
    ) -> QPainterPath:
        path = QPainterPath()
        if not face.vertices:
            return path
        path.moveTo(QPointF(*face.vertices[0]))
        source_paths = (
            [
                stroke_cubics(stroke.points, stroke.closed)
                for stroke in drawing.strokes
                if stroke_cubics(stroke.points, stroke.closed)
            ]
            if drawing is not None else []
        )
        for edge in face.edges:
            provenance = edge.provenance
            if (
                provenance.path_index >= 0
                and provenance.path_index < len(source_paths)
                and provenance.segment_index >= 0
                and provenance.segment_index
                < len(source_paths[provenance.path_index])
                and not provenance.virtual
            ):
                cubic = source_paths[
                    provenance.path_index
                ][provenance.segment_index]
                subspan = cubic_subsegment(
                    cubic, provenance.t0, provenance.t1
                )
                path.cubicTo(
                    QPointF(*subspan[1]),
                    QPointF(*subspan[2]),
                    QPointF(*subspan[3]),
                )
            else:
                path.lineTo(QPointF(*edge.end))
        path.closeSubpath()
        return path

    def _scale_vector_fill_path(self, path: QPainterPath) -> QPainterPath:
        if (
            not self.settings.fill_area_scaling
            or abs(self.settings.fill_area_amount) <= 1.0e-6
        ):
            return path
        amount = self.settings.fill_area_amount
        stroker = QPainterPathStroker()
        stroker.setWidth(abs(amount) * 2)
        stroker.setJoinStyle(
            Qt.RoundJoin
            if self.settings.fill_area_mode == "round"
            else Qt.MiterJoin
        )
        stroker.setCapStyle(
            Qt.RoundCap
            if self.settings.fill_area_mode == "round"
            else Qt.SquareCap
        )
        border = stroker.createStroke(path)
        return (
            path.united(border)
            if amount > 0 else path.subtracted(border)
        )

    def _remove_narrow_vector_fill_areas(
        self, path: QPainterPath,
    ) -> QPainterPath:
        """Remove thin corridors without rejecting an otherwise large face."""
        if self.settings.fill_narrow_areas or path.isEmpty():
            return path
        width = max(
            2.0,
            self.settings.fill_gap_threshold
            if self.settings.fill_close_gaps else 2.0,
        )
        stroker = QPainterPathStroker()
        stroker.setWidth(width)
        stroker.setJoinStyle(Qt.RoundJoin)
        stroker.setCapStyle(Qt.RoundCap)
        core = path.subtracted(stroker.createStroke(path))
        if core.isEmpty():
            return QPainterPath()
        restored = core.united(stroker.createStroke(core))
        return restored.intersected(path)

    def _prepare_vector_fill_path(
        self, path: QPainterPath,
    ) -> QPainterPath:
        return self._scale_vector_fill_path(
            self._remove_narrow_vector_fill_areas(path)
        )

    def _geometry_for_face(
        self, face, drawing: VectorDrawingObject,
    ) -> BoundGeometry | None:
        path = self._prepare_vector_fill_path(
            self._face_path(face, drawing)
        )
        if path.isEmpty():
            return None
        return self._geometry_from_painter_path(path)

    def _vector_fill_faces(self, drawing: VectorDrawingObject):
        paths: list[list[Cubic]] = []
        closed: list[bool] = []
        for stroke in drawing.strokes:
            cubics = stroke_cubics(stroke.points, stroke.closed)
            if not cubics:
                continue
            paths.append(cubics)
            closed.append(stroke.closed)
        if not paths:
            return []
        faces = trace_cubic_faces(
            paths,
            closed=closed,
            gap_threshold=(
                self.settings.fill_gap_threshold
                if self.settings.fill_close_gaps else 0.0
            ),
            flatten_tolerance=0.2,
        )
        return faces

    def _point_hits_vector_stroke(
        self, drawing: VectorDrawingObject, local: tuple[float, float],
    ) -> bool:
        return any(
            centerline_hit(
                stroke.points,
                local,
                closed=stroke.closed,
                extra_tolerance=1.0 / max(self.scale, 0.05),
            ) is not None
            for stroke in drawing.strokes
        )

    def _existing_fill_at(
        self, drawing: VectorDrawingObject, local: tuple[float, float],
    ) -> VectorFillObject | None:
        return next((
            fill
            for fill in self.chapter.vector_fill_children(drawing.object_id)
            if self.bound_path(fill.geometry).contains(QPointF(*local))
        ), None)

    def _finish_vector_fill(
        self, drawing: VectorDrawingObject,
    ) -> None:
        before = self._vector_before or self._capture_vector_graph(drawing)
        samples = [sample.point for sample in self._vector_sweep]
        faces = self._vector_fill_faces(drawing)
        created = False
        changed = False
        settings = self._vector_fill_settings()
        if self.settings.fill_mode == "enclose":
            if len(samples) >= 3:
                lasso = QPainterPath()
                lasso.addPolygon(QPolygonF([
                    QPointF(*point) for point in samples
                ]))
                lasso.closeSubpath()
                chosen = []
                for face in faces:
                    centroid = (
                        sum(point[0] for point in face.vertices)
                        / len(face.vertices),
                        sum(point[1] for point in face.vertices)
                        / len(face.vertices),
                    )
                    if lasso.contains(QPointF(*centroid)):
                        chosen.append(face)
                compiled = QPainterPath()
                for face in chosen:
                    compiled = compiled.united(
                        self._face_path(face, drawing)
                    )
                compiled = self._prepare_vector_fill_path(compiled)
                geometry = self._geometry_from_painter_path(compiled)
                if geometry is not None:
                    probe = next(iter(chosen), None)
                    centroid = (
                        (
                            sum(point[0] for point in probe.vertices)
                            / len(probe.vertices),
                            sum(point[1] for point in probe.vertices)
                            / len(probe.vertices),
                        )
                        if probe is not None else samples[0]
                    )
                    fill = self._existing_fill_at(drawing, centroid)
                    if fill is None:
                        fill = VectorFillObject(
                            geometry=geometry,
                            fill_color=self.primary_color,
                            source_lasso=list(samples),
                            fill_settings=settings,
                        )
                        self.chapter.add_vector_fill(
                            drawing.object_id, fill, index=0
                        )
                        created = True
                    else:
                        fill.geometry = geometry
                        fill.fill_color = self.primary_color
                        fill.source_lasso = list(samples)
                        fill.source_seed = None
                        fill.fill_settings = settings
                        drawing.touch_revision()
                    changed = True
        else:
            used: set[tuple[tuple[float, float], ...]] = set()
            for seed in samples:
                if self._point_hits_vector_stroke(drawing, seed):
                    continue
                face = find_face_containing(faces, seed)
                if face is None:
                    continue
                signature = tuple(sorted(
                    (round(point[0], 3), round(point[1], 3))
                    for point in face.vertices
                ))
                if signature in used:
                    continue
                used.add(signature)
                geometry = self._geometry_for_face(face, drawing)
                if geometry is None:
                    continue
                fill = self._existing_fill_at(drawing, seed)
                if fill is None:
                    fill = VectorFillObject(
                        geometry=geometry,
                        fill_color=self.primary_color,
                        source_seed=seed,
                        fill_settings=settings,
                    )
                    self.chapter.add_vector_fill(
                        drawing.object_id, fill, index=0
                    )
                    created = True
                else:
                    fill.geometry = geometry
                    fill.fill_color = self.primary_color
                    fill.source_seed = seed
                    fill.source_lasso = []
                    fill.fill_settings = settings
                    drawing.touch_revision()
                changed = True
        self._vector_gesture_mode = None
        self._vector_sweep = []
        self._vector_before = None
        if changed:
            self._push_vector_change(
                before, "Vector fill", hierarchy=created
            )
        self.set_selection(
            "object", drawing.object_id, activate_default_tool=False
        )
        self.tool = ToolKind.FILL
        self.toolChanged.emit(self.tool)
        self.interactionFinished.emit()
        self.update()

    def _apply_shape_fill(self, world: QPointF) -> bool:
        if (
            self.chapter is None
            or self.selected_kind != "layer"
            or self.selected_id not in self.chapter.layers
        ):
            return False
        layer = self.chapter.layers[self.selected_id]
        if (
            layer.bound is None or layer.is_page
            or layer.layer_kind == "fill"
        ):
            return False
        before = self.chapter.to_dict()
        if self._shape_border_contains(layer.layer_id, world, raw=True):
            layer.shape_style.outline_color = self.primary_color
            if layer.shape_style.outline_thickness <= 0:
                layer.shape_style.outline_thickness = 4.0
        elif layer.bound.closed:
            world_x, world_y = self.chapter.layer_world_translation(
                layer.layer_id
            )
            local = QPointF(world.x() - world_x, world.y() - world_y)
            if not self.layer_effective_path(layer.layer_id).contains(local):
                return False
            layer.shape_style.primary_color = self.primary_color
        else:
            return False
        after = self.chapter.to_dict()
        self.push_model_change(before, after, "Fill shape")
        self.documentChanged.emit(QRectF())
        self.interactionFinished.emit()
        self.update()
        return True

    def _begin_vector_gesture(
        self, drawing: VectorDrawingObject,
        world: QPointF, pressure: float,
    ) -> None:
        local = self._vector_local_point(drawing, world)
        if self.tool == ToolKind.RASTER_PENCIL:
            self._begin_vector_pencil(drawing, local, pressure)
            return
        if self.tool == ToolKind.RASTER_ERASER:
            self._vector_before = {drawing.object_id: drawing.to_dict()}
            self._vector_gesture_mode = "eraser"
        elif self.tool == ToolKind.VECTOR_REDRAW:
            if self.settings.vector_redraw_interaction == "point":
                self._begin_vector_point_select(drawing, local)
                return
            self._vector_before = {drawing.object_id: drawing.to_dict()}
            self._vector_gesture_mode = "redraw"
        elif self.tool == ToolKind.VECTOR_CONNECT:
            self._vector_before = {drawing.object_id: drawing.to_dict()}
            self._vector_gesture_mode = "connect"
            self._vector_connect_endpoints = []
        elif self.tool == ToolKind.VECTOR_SIMPLIFY:
            self._vector_before = {drawing.object_id: drawing.to_dict()}
            self._vector_gesture_mode = "simplify"
        elif self.tool == ToolKind.FILL:
            self._vector_before = self._capture_vector_graph(drawing)
            self._vector_gesture_mode = "fill"
        self._vector_sweep = [
            FreehandSample(local.x(), local.y(), pressure)
        ]
        if self._vector_gesture_mode == "simplify":
            self._build_simplify_anchor_index(drawing)
            self._update_simplify_point_sweep(drawing)
        if self._vector_gesture_mode == "redraw":
            self._manual_redraw_at(drawing, local, pressure)
        elif self._vector_gesture_mode == "connect":
            self._collect_vector_endpoint(drawing, local)

    def _continue_vector_gesture(
        self, world: QPointF, pressure: float,
    ) -> None:
        drawing = (
            self._active_vector_drawing()
            if self._vector_gesture_mode == "fill"
            else self._selected_vector_drawing()
        )
        if drawing is None:
            return
        local = self._vector_local_point(drawing, world)
        if self._vector_gesture_mode == "edit_drag":
            self._update_vector_anchor_drag(drawing, local)
            return
        if self._vector_gesture_mode == "point_select":
            self._continue_vector_point_select(drawing, local)
            return
        if self._vector_gesture_mode == "pencil":
            self._append_vector_sample(local, pressure)
            self.update()
            return
        sample = FreehandSample(local.x(), local.y(), pressure)
        if not self._vector_sweep:
            self._vector_sweep.append(sample)
        elif self._vector_gesture_mode == "fill":
            previous = self._vector_sweep[-1]
            separation = math.dist(previous.point, sample.point)
            spacing = max(1.0, 4.0 / max(self.scale, 0.05))
            steps = min(4096, max(1, math.ceil(separation / spacing)))
            for step in range(1, steps + 1):
                amount = step / steps
                self._vector_sweep.append(FreehandSample(
                    previous.x + (sample.x - previous.x) * amount,
                    previous.y + (sample.y - previous.y) * amount,
                    previous.pressure
                    + (sample.pressure - previous.pressure) * amount,
                ))
        elif math.dist(self._vector_sweep[-1].point, sample.point) >= 0.2:
            self._vector_sweep.append(sample)
        if self._vector_gesture_mode == "simplify":
            self._update_simplify_point_sweep(drawing)
        if self._vector_gesture_mode == "redraw":
            self._manual_redraw_at(drawing, local, pressure)
        elif self._vector_gesture_mode == "connect":
            self._collect_vector_endpoint(drawing, local)
        elif (
            self._vector_gesture_mode == "eraser"
            and len(self._vector_sweep) % 2 == 0
            and self._vector_before
        ):
            self._restore_vector_payloads(self._vector_before)
            drawing = self._selected_vector_drawing() or drawing
            self._apply_vector_eraser_sweep(drawing)
        self.update()

    def _end_vector_gesture(self) -> None:
        drawing = (
            self._active_vector_drawing()
            if self._vector_gesture_mode == "fill"
            else self._selected_vector_drawing()
        )
        if drawing is None:
            self._cancel_vector_gesture()
            return
        mode = self._vector_gesture_mode
        if mode == "edit_drag":
            before = self._vector_before or {}
            self._vector_gesture_mode = None
            self._vector_before = None
            self._vector_drag_points.clear()
            self._push_vector_change(before, "Move vector points")
            self.interactionFinished.emit()
        elif mode == "point_select":
            self._vector_gesture_mode = None
            self._vector_sweep = []
            self.interactionFinished.emit()
        elif mode == "pencil":
            self._finish_vector_pencil(drawing)
        elif mode == "eraser":
            self._finish_vector_eraser(drawing)
        elif mode == "redraw":
            before = self._vector_before or {}
            self._vector_gesture_mode = None
            self._vector_before = None
            self._vector_sweep = []
            self._push_vector_change(before, "Redraw vector parameter")
            self.interactionFinished.emit()
        elif mode == "connect":
            self._finish_vector_connect(drawing)
        elif mode == "simplify":
            self._finish_vector_simplify_gesture(drawing)
        elif mode == "fill":
            self._finish_vector_fill(drawing)

    # ---- tool actions --------------------------------------------------
    def _tool_press(self, widget_point: QPointF, pressure: float) -> None:
        point = self.widget_to_document(widget_point)
        self._press_widget_point = QPointF(widget_point)
        self._press_document_point = QPointF(point)
        if self._begin_page_gap_interaction(point):
            return
        if self.tool == ToolKind.INSERT_PAGE_GAP:
            self._update_page_gap_hover(point)
            self._insert_hovered_page_gap()
            return
        if self.tool in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
        }:
            drawing = self._drawing_selection_object()
            if (
                drawing is not None
                and self._selection_operation() == "replace"
                and self._begin_drawing_selection_transform(drawing, point)
            ):
                return
            # Drawing-selection transforms own their quad.  Raster's outer
            # translation affordance must not swallow an outside tap here;
            # only its explicit rotate/pivot/resize handles retain priority.
            selected_raster = (
                self.chapter.objects.get(self.selected_object_id)
                if self.chapter is not None else None
            )
            if isinstance(selected_raster, RasterObject):
                quad = self.object_world_quad(selected_raster.object_id)
                if quad:
                    raster_mode, _ = self._raster_transform_control_hit(
                        quad, point
                    )
                    if raster_mode in {"handle", "rotate", "pivot"} \
                            and self._begin_selected_raster_transform(point):
                        return
            if (
                drawing is not None
                and not self._point_inside_drawing_bounds(drawing, point)
            ):
                self._pending_drawing_selection_press = (
                    QPointF(widget_point), QPointF(point), pressure
                )
                return
            self._begin_drawing_selection(
                point, widget_point, test_transform=False
            )
            return
        if self._begin_or_defer_selected_raster_transform(
            widget_point, point
        ):
            return
        if self.tool in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}:
            obj = self.chapter.objects.get(self.selected_object_id)
            if isinstance(obj, VectorDrawingObject):
                path = QPainterPath()
                bounds = QRectF(*obj.derived_bounds())
                if obj.strokes and not bounds.isEmpty():
                    radius = (
                        self.settings.active_eraser_pixels() / 2
                        + 4 / max(self.scale, 0.05)
                        if self.tool == ToolKind.RASTER_ERASER else 0.0
                    )
                    bounds.adjust(-radius, -radius, radius, radius)
                    layer_x, layer_y = self.chapter.layer_world_translation(
                        obj.parent_layer_id
                    )
                    bounds.translate(layer_x + obj.x, layer_y + obj.y)
                    path.addRect(bounds)
                if not obj.strokes or path.contains(point):
                    self._begin_vector_gesture(obj, point, pressure)
                else:
                    self._pending_vector_press = (
                        QPointF(widget_point), QPointF(point), pressure
                    )
                return
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
                self._pending_raster_press = (
                    QPointF(widget_point), QPointF(point), pressure
                )
            return
        if self.tool == ToolKind.VECTOR_EDIT:
            drawing = self._selected_vector_drawing()
            if drawing is not None:
                self._begin_vector_edit_pointer(
                    drawing, self._vector_local_point(drawing, point)
                )
            return
        if self.tool in {
            ToolKind.VECTOR_REDRAW,
            ToolKind.VECTOR_CONNECT,
            ToolKind.VECTOR_SIMPLIFY,
        }:
            drawing = self._selected_vector_drawing()
            if drawing is not None:
                self._begin_vector_gesture(drawing, point, pressure)
            return
        if self.tool == ToolKind.FILL:
            drawing = self._active_vector_drawing()
            if drawing is not None:
                self._begin_vector_gesture(drawing, point, pressure)
            elif self.selected_kind == "layer":
                self._apply_shape_fill(point)
            return
        if self.tool == ToolKind.TEXT_EDIT and not isinstance(
            self.chapter.objects.get(self.selected_object_id), TextObject
        ):
            hits = self.hit_test_objects(point, text_only=True)
            if hits:
                self.set_selection("object", hits[0])
            return
        if self.tool == ToolKind.SHAPE_EDIT:
            selected_gradient = self.chapter.objects.get(
                self.selected_object_id
            )
            if (
                isinstance(selected_gradient, GradientObject)
                and self._begin_gradient_edit(selected_gradient, point)
            ):
                return
            if (
                self.selected_kind == "layer"
                and self._begin_shape_edit(point, allow_interior=False)
            ):
                return
            if self._begin_geometry_transform(point):
                return
            hits = [
                hit for hit in self.hit_test_shape_edit_layers(point)
                if hit["id"] != self.selected_id
            ]
            if (
                len(hits) > 1
                and QGuiApplication.keyboardModifiers() & Qt.ControlModifier
            ):
                self.selectionCandidatesRequested.emit(
                    hits, self.mapToGlobal(widget_point.toPoint())
                )
                return
            if hits:
                self.set_selection("layer", hits[0]["id"])
                return
            if (
                self.selected_kind == "layer"
                and self._begin_shape_edit(point)
            ):
                return
        if self.tool == ToolKind.OBJECT_SELECT:
            self._request_object_selection(point, widget_point)
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
        if self.tool == ToolKind.RASTER_CREATE:
            target = self._raster_creation_parent_id or self._target_parent_for_new_layer()
            snapped = self._snap(point, target)
            self._creation_points = [
                (snapped.x(), snapped.y()), (snapped.x(), snapped.y())
            ]
            return
        if (
            self.tool == ToolKind.TEXT_EDIT
            and self.selected_object_id
            and isinstance(
                self.chapter.objects.get(self.selected_object_id), TextObject
            )
            and self.chapter.objects[
                self.selected_object_id
            ].layout_mode == "free"
        ):
            obj = self.chapter.objects[self.selected_object_id]
            world_quad = self.object_world_quad(obj.object_id)
            mode, handle = self._transform_control_hit(world_quad, point)
            if mode in {"handle", "rotate", "pivot"}:
                layer_x, layer_y = self.chapter.layer_world_translation(
                    obj.parent_layer_id
                )
                self._transform_handle_index = handle
                self._transform_drag_mode = mode
                self._model_before = self.chapter.to_dict()
                self._drag_start_doc = point
                self._transform_start_quad = [
                    (x - layer_x, y - layer_y) for x, y in world_quad
                ]
                self._transform_preview_quad = list(
                    self._transform_start_quad
                )
                pivot = self._transform_pivot or QPointF(
                    sum(x for x, _ in world_quad) / 4,
                    sum(y for _, y in world_quad) / 4,
                )
                self._transform_rotate_start = math.atan2(
                    point.y() - pivot.y(), point.x() - pivot.x()
                )
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
            mode, handle = self._transform_control_hit(world_quad, point)
            if not mode:
                return
            self._transform_handle_index = handle
            self._transform_drag_mode = mode
            self._model_before = self.chapter.to_dict()
            self._drag_start_doc = point
            self._transform_start_quad = local_quad
            self._transform_preview_quad = list(local_quad)
            pivot = self._transform_pivot or QPointF(
                sum(x for x, _ in world_quad) / 4,
                sum(y for _, y in world_quad) / 4,
            )
            self._transform_rotate_start = math.atan2(
                point.y() - pivot.y(), point.x() - pivot.x()
            )
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
                    primary_color=self.secondary_color,
                    base_thickness=float(self.settings.pencil_size()),
                    outline_color=self.primary_color,
                    outline_thickness=4.0,
                )
            self._creation_active_control = "new_point"
            self._creation_press_widget = QPointF(widget_point)
            self._creation_node_dragged = False
            self._creation_close_candidate = False
            self.update()

    def _tool_move(self, widget_point: QPointF, pressure: float) -> None:
        point = self.widget_to_document(widget_point)
        if self._page_gap_drag_mode is not None:
            self._move_page_gap_interaction(point)
            return
        if self.tool == ToolKind.INSERT_PAGE_GAP:
            self._update_page_gap_hover(point)
            return
        if (
            self._model_before is not None
            and self._transform_start_quad is not None
            and isinstance(
                self.chapter.objects.get(self.selected_object_id),
                RasterObject,
            )
        ):
            self._update_transform_preview(point)
            self.update()
            return
        gap_hit = self._page_gap_hit(point)
        if gap_hit == "band":
            self.setCursor(Qt.OpenHandCursor)
        elif gap_hit in {"top", "bottom"}:
            self.setCursor(Qt.PointingHandCursor)
        elif self._model_before is None:
            selected_raster = self.chapter.objects.get(
                self.selected_object_id
            )
            if isinstance(selected_raster, RasterObject):
                world_quad = self.object_world_quad(
                    selected_raster.object_id
                )
                raster_hit, _handle = (
                    self._raster_transform_control_hit(world_quad, point)
                    if world_quad else ("", None)
                )
                if raster_hit == "translate":
                    self.setCursor(Qt.SizeAllCursor)
                elif raster_hit is not None:
                    self.setCursor(Qt.PointingHandCursor)
                else:
                    self.unsetCursor()
            elif (
                self.tool == ToolKind.SHAPE_EDIT
                and isinstance(selected_raster, GradientObject)
            ):
                hit = self._gradient_control_hit(
                    selected_raster, point
                )
                if hit is not None:
                    self.setCursor(Qt.PointingHandCursor)
                    self.setToolTip({
                        "toggle": "Switch between circle and ellipse",
                        "node": "Move gradient path point",
                        "incoming": "Move incoming Bézier control",
                        "outgoing": "Move outgoing Bézier control",
                        "origin": "Move radial gradient origin",
                        "radius_x": "Change horizontal radius",
                        "radius_y": "Change vertical radius",
                        "rotate": "Rotate ellipse gradient",
                        "center": (
                            "Move gradient center; double-click to reset"
                        ),
                        "type": "Switch this point between Vector and Bézier",
                        "delete": "Delete this gradient path point",
                        "insert": "Insert a point on the gradient path",
                        "lock": "Lock or unlock Bézier handles",
                        "roundness": (
                            "Click to toggle roundness; drag to adjust it"
                        ),
                        "distance": "Adjust the gradient distance",
                    }.get(hit[0], "Edit gradient"))
                else:
                    self.unsetCursor()
                    self.setToolTip("")
        if self.tool in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
        }:
            if self._pending_drawing_selection_press is not None:
                press_widget, press_document, _press_pressure = (
                    self._pending_drawing_selection_press
                )
                if math.dist(
                    (widget_point.x(), widget_point.y()),
                    (press_widget.x(), press_widget.y()),
                ) > 4:
                    self._pending_drawing_selection_press = None
                    self._begin_drawing_selection(
                        press_document, press_widget, test_transform=False
                    )
                    self._continue_drawing_selection(point, widget_point)
                return
            self._continue_drawing_selection(point, widget_point)
            return
        if self._vector_gesture_mode is not None:
            self._continue_vector_gesture(point, pressure)
            return
        if self._pending_raster_transform_press is not None:
            press_widget, press_document = self._pending_raster_transform_press
            if math.dist(
                (widget_point.x(), widget_point.y()),
                (press_widget.x(), press_widget.y()),
            ) > 4:
                self._pending_raster_transform_press = None
                if self._begin_selected_raster_transform(press_document):
                    self._update_transform_preview(point)
            return
        if self._pending_vector_press is not None:
            press_widget, press_document, press_pressure = (
                self._pending_vector_press
            )
            if math.dist(
                (widget_point.x(), widget_point.y()),
                (press_widget.x(), press_widget.y()),
            ) > 4:
                self._pending_vector_press = None
                drawing = self._selected_vector_drawing()
                if drawing is not None:
                    self._begin_vector_gesture(
                        drawing, press_document, press_pressure
                    )
                    self._continue_vector_gesture(point, pressure)
            return
        if self._pending_raster_press is not None:
            press_widget, press_document, press_pressure = (
                self._pending_raster_press
            )
            if math.dist(
                (widget_point.x(), widget_point.y()),
                (press_widget.x(), press_widget.y()),
            ) > 4:
                self._pending_raster_press = None
                self._begin_stroke(press_document, press_pressure)
                if self._drawing:
                    self._continue_stroke(point, pressure)
            return
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
            self.tool in {ToolKind.TRANSFORM, ToolKind.TEXT_EDIT}
            and self._model_before is not None
            and self._transform_start_quad is not None
        ):
            self._update_transform_preview(point)
            self.update()
            return
        selected_gradient = self.chapter.objects.get(
            self.selected_object_id
        )
        if (
            self.tool == ToolKind.SHAPE_EDIT
            and isinstance(selected_gradient, GradientObject)
            and self._active_gradient_control is not None
        ):
            self._update_gradient_edit(selected_gradient, point)
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
        if self._finish_page_gap_interaction():
            return
        if self.tool == ToolKind.INSERT_PAGE_GAP:
            return
        if (
            self._model_before is not None
            and self._transform_preview_quad is not None
            and isinstance(
                self.chapter.objects.get(self.selected_object_id),
                RasterObject,
            )
        ):
            self._commit_object_transform()
            self.interactionFinished.emit()
            return
        if self.tool in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
        }:
            if self._pending_drawing_selection_press is not None:
                widget_point, point, _pressure = (
                    self._pending_drawing_selection_press
                )
                self._pending_drawing_selection_press = None
                self._request_object_selection(point, widget_point)
                self.interactionFinished.emit()
                return
            self._finish_drawing_selection()
            self.interactionFinished.emit()
            return
        if self._vector_gesture_mode is not None:
            self._end_vector_gesture()
            return
        if self._pending_raster_transform_press is not None:
            widget_point, point = self._pending_raster_transform_press
            self._pending_raster_transform_press = None
            self._request_object_selection(point, widget_point)
            self.interactionFinished.emit()
            return
        if self._pending_vector_press is not None:
            widget_point, point, _pressure = self._pending_vector_press
            self._pending_vector_press = None
            self._request_object_selection(point, widget_point)
            self.interactionFinished.emit()
            return
        if self._pending_raster_press is not None:
            widget_point, point, _pressure = self._pending_raster_press
            self._pending_raster_press = None
            self._request_object_selection(point, widget_point)
            self.interactionFinished.emit()
            return
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
        if (
            self._page_creation_anchor_id
            and self._page_creation_kind in {"rectangle", "circle"}
            and len(self._creation_points) >= 2
        ):
            first, second = (
                self._creation_points[0], self._creation_points[-1]
            )
            if math.dist(first, second) < 2:
                self.update()
                return
            if self._page_creation_kind == "rectangle":
                left, right = sorted((first[0], second[0]))
                top, bottom = sorted((first[1], second[1]))
                bound = BoundGeometry.rectangle(
                    left, top, right - left, bottom - top
                )
            else:
                bound = BoundGeometry.circle(
                    first[0], first[1], math.dist(first, second)
                )
            self._finish_pending_page_bound(bound)
            self.interactionFinished.emit()
            return
        if self.tool == ToolKind.SHAPE_CREATE:
            close = (
                self._creation_close_candidate
                and not self._creation_node_dragged
            )
            if (
                self._creation_active_control == "roundness"
                and not self._creation_node_dragged
            ):
                node = self._creation_selected_node()
                if node is not None:
                    geometry = BoundGeometry.path(
                        self._creation_nodes, False
                    )
                    self._toggle_shape_node_roundness(geometry, node)
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
            self.tool in {ToolKind.TRANSFORM, ToolKind.TEXT_EDIT}
            and self._model_before is not None
            and self._transform_preview_quad is not None
            and self.selected_object_id
        ):
            self._commit_object_transform()
            self.interactionFinished.emit()
            return
        if (
            self._geometry_transform_target is not None
            and self._model_before is not None
            and self._transform_preview_quad is not None
        ):
            self._commit_geometry_transform()
            self.interactionFinished.emit()
            return
        if (
            self._model_before is not None
            and self._active_gradient_control is not None
        ):
            before, self._model_before = self._model_before, None
            gradient_control = self._active_gradient_control
            self._active_gradient_control = None
            self._gradient_preview_active = False
            # Preview tiles are intentionally low resolution; discard them so
            # the next paint builds final-resolution visible tiles.
            self._gradient_render_cache.clear()
            selected = self.chapter.objects.get(self.selected_object_id)
            if isinstance(selected, GradientObject):
                if (
                    selected.field_type == "line"
                    and gradient_control[0] == "roundness"
                    and not self._shape_control_dragged
                ):
                    node = next((
                        candidate
                        for candidate in selected.line_field.geometry.nodes
                        if candidate.node_id == gradient_control[1]
                    ), None)
                    if node is not None:
                        self._toggle_shape_node_roundness(
                            selected.line_field.geometry, node
                        )
                selected.validate_gradient()
                selected.touch_revision()
            self._shape_control_dragged = False
            after = self.chapter.to_dict()
            if before != after:
                self.push_model_change(before, after, "Edit gradient geometry")
                self.hierarchyChanged.emit()
            self.interactionFinished.emit()
            self.update()
            return
        if self._model_before is not None:
            before, self._model_before = self._model_before, None
            if (
                self._active_shape_control == "roundness"
                and not self._shape_control_dragged
                and self.selected_kind == "layer"
            ):
                layer = self.chapter.layers[self.selected_id]
                selected = self._selected_shape_node(layer.bound)
                if selected is not None:
                    self._toggle_shape_node_roundness(
                        layer.bound, selected
                    )
            self._active_handle = None
            self._active_shape_control = None
            self._shape_control_dragged = False
            self._bound_drag_mode = None
            self._bound_start_points = []
            if (
                self.selected_kind == "layer"
                and self.selected_id in self.chapter.layers
                and self.chapter.layers[self.selected_id].bound is not None
            ):
                self.chapter.layers[
                    self.selected_id
                ].bound.normalize_bezier_handles()
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
            if (
                self._gradient_creation_type == "radial"
                and self._gradient_creation_parent_id
            ):
                self.create_gradient(
                    self._gradient_creation_parent_id,
                    "radial",
                    radial=(first, math.dist(first, second)),
                    before=self._gradient_creation_before,
                )
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
            self._create_layer_from_world_bound(
                bound,
                style=ShapeStyle(
                    primary_color=self.secondary_color,
                    outline_color=self.primary_color,
                    outline_thickness=4.0,
                ),
            )

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

    def begin_raster_creation(
        self, parent_id: str, insertion_index: int | None = None,
    ) -> bool:
        if (
            self.chapter is None or parent_id not in self.chapter.layers
            or self.chapter.layers[parent_id].layer_kind
            in {"fill", "open_shape"}
        ):
            return False
        self._raster_creation_parent_id = parent_id
        self._raster_creation_index = insertion_index
        self._creation_points.clear()
        return self.set_tool(ToolKind.RASTER_CREATE)

    def begin_gradient_creation(
        self, parent_id: str, field_type: str,
    ) -> bool:
        if (
            self.chapter is None
            or parent_id not in self.chapter.layers
            or self.chapter.layers[parent_id].layer_kind == "fill"
            or field_type not in {"line", "radial", "parent_shape"}
        ):
            return False
        if self.chapter.gradient_children(parent_id, field_type):
            return False
        if field_type == "parent_shape":
            return self.create_gradient(parent_id, field_type) is not None
        self._gradient_creation_parent_id = parent_id
        self._gradient_creation_type = field_type
        self._gradient_creation_before = self.chapter.to_dict()
        self._creation_points.clear()
        self._creation_nodes.clear()
        self._creation_selected_node_id = ""
        self._creation_active_control = None
        self.set_selection(
            "layer", parent_id, activate_default_tool=False
        )
        return self.set_tool(
            ToolKind.SHAPE_CREATE
            if field_type == "line" else ToolKind.CIRCLE_BOUND
        )

    def _cancel_gradient_creation(self) -> None:
        self._gradient_creation_parent_id = ""
        self._gradient_creation_type = ""
        self._gradient_creation_before = None
        self._creation_points.clear()
        self._creation_nodes.clear()
        self._creation_selected_node_id = ""
        self._creation_active_control = None
        self._shape_hover_target = None
        self._shape_hover_insert = None
        self.update()

    def create_gradient(
        self, parent_id: str, field_type: str,
        *, world_geometry: BoundGeometry | None = None,
        radial: tuple[tuple[float, float], float] | None = None,
        before: dict | None = None,
    ) -> ColorFillGradientObject | None:
        if (
            self.chapter is None
            or parent_id not in self.chapter.layers
            or self.chapter.layers[parent_id].layer_kind == "fill"
            or field_type not in {"line", "radial", "parent_shape"}
        ):
            return None
        before = before or self.chapter.to_dict()
        parent_x, parent_y = self.chapter.layer_world_translation(parent_id)
        parent_bounds = self.layer_effective_path(parent_id).boundingRect()
        count = sum(
            isinstance(item, GradientObject)
            for item in self.chapter.objects.values()
        ) + 1
        obj = ColorFillGradientObject(
            name=f"Gradient {count}",
            field_type=field_type,
            ramp=ColorGradientRamp(stops=[
                ColorGradientStop(
                    position=0.0, color=self.primary_color
                ),
                ColorGradientStop(
                    position=1.0, color=self.secondary_color
                ),
            ]),
        )
        if world_geometry is not None:
            local = BoundGeometry.from_dict(world_geometry.to_dict())
            for contour in local.iter_contours():
                for node in contour.nodes:
                    node.x -= parent_x
                    node.y -= parent_y
                    if node.incoming is not None:
                        node.incoming = (
                            node.incoming[0] - parent_x,
                            node.incoming[1] - parent_y,
                        )
                    if node.outgoing is not None:
                        node.outgoing = (
                            node.outgoing[0] - parent_x,
                            node.outgoing[1] - parent_y,
                        )
            local.closed = False
            local.normalize_bezier_handles()
            obj.line_field = LineGradientField(local)
        elif radial is not None:
            (world_x, world_y), radius = radial
            obj.radial_field = RadialGradientField(
                origin_x=world_x - parent_x,
                origin_y=world_y - parent_y,
                radius_x=radius,
                radius_y=radius,
            )
        else:
            center = parent_bounds.center()
            obj.line_field = LineGradientField(BoundGeometry.path([
                PathNode(x=parent_bounds.left(), y=center.y()),
                PathNode(x=parent_bounds.right(), y=center.y()),
            ]))
            obj.radial_field = RadialGradientField(
                origin_x=center.x(), origin_y=center.y(),
                radius_x=max(1.0, parent_bounds.width() / 2),
                radius_y=max(1.0, parent_bounds.height() / 2),
            )
        obj.validate_gradient()
        self.chapter.add_object(parent_id, obj)
        self.set_selection("object", obj.object_id)
        self._cancel_gradient_creation()
        after = self.chapter.to_dict()
        self.push_model_change(before, after, "Add gradient")
        self.hierarchyChanged.emit()
        self.documentChanged.emit(QRectF())
        self.interactionFinished.emit()
        return obj

    def create_vector_drawing(
        self, parent_id: str, insertion_index: int | None = None,
    ) -> VectorDrawingObject | None:
        if (
            self.chapter is None
            or parent_id not in self.chapter.layers
            or self.chapter.layers[parent_id].layer_kind == "fill"
        ):
            return None
        before = self.chapter.to_dict()
        count = sum(
            isinstance(item, VectorDrawingObject)
            for item in self.chapter.objects.values()
        ) + 1
        drawing = VectorDrawingObject(name=f"Vector Drawing {count}")
        self.chapter.add_object(
            parent_id, drawing, index=insertion_index
        )
        after = self.chapter.to_dict()
        self.push_model_change(
            before, after, "Add vector drawing"
        )
        self.set_selection("object", drawing.object_id)
        self.hierarchyChanged.emit()
        self.documentChanged.emit(QRectF())
        self.interactionFinished.emit()
        return drawing

    def _create_raster_from_world_rect(
        self, first: tuple[float, float], second: tuple[float, float],
    ) -> None:
        parent_id = self._raster_creation_parent_id
        insertion_index = self._raster_creation_index
        self._raster_creation_parent_id = ""
        self._raster_creation_index = None
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
        self.chapter.add_object(parent_id, obj, index=insertion_index)
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
        path = self.layer_effective_path(layer.layer_id)
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
        hits = self.hit_test_entities(point)
        if (
            len(hits) > 1
            and QGuiApplication.keyboardModifiers() & Qt.ControlModifier
        ):
            self.selectionCandidatesRequested.emit(
                hits, self.mapToGlobal(widget_point.toPoint())
            )
            return
        if hits:
            hit = hits[0]
            self.set_selection(
                hit["kind"], hit["id"], activate_default_tool=True
            )
            return
        page_id = self.active_page_id
        if page_id and page_id in self.chapter.layers:
            self.set_selection("layer", page_id, activate_default_tool=False)
            self.set_tool(ToolKind.OBJECT_SELECT)

    def _selected_shape_node(self, bound: BoundGeometry) -> PathNode | None:
        return next((
            node
            for contour in bound.iter_contours()
            for node in contour.nodes
            if node.node_id == self._selected_shape_node_id
        ), None)

    @staticmethod
    def _contour_bound_for_node(
        bound: BoundGeometry, node: PathNode,
    ) -> BoundGeometry:
        contour = bound.contour_for_node(node.node_id)
        if contour is None or contour.nodes is bound.nodes:
            return bound
        return BoundGeometry(
            nodes=contour.nodes, closed=contour.closed, primitive="custom"
        )

    @classmethod
    def _can_delete_shape_node(
        cls, bound: BoundGeometry, node: PathNode | None = None,
    ) -> bool:
        if node is not None:
            bound = cls._contour_bound_for_node(bound, node)
        minimum = 3 if bound.closed else 2
        return len(bound.nodes) > minimum

    def _delete_selected_shape_node(self, layer: LayerNode) -> bool:
        bound = layer.bound
        node = self._selected_shape_node(bound)
        if node is None or not self._can_delete_shape_node(bound, node):
            return False
        bound.normalize_bezier_handles()
        before = self.chapter.to_dict()
        bound.primitive = "custom"
        contour = bound.contour_for_node(node.node_id)
        contour.nodes.remove(node)
        bound.normalize_bezier_handles()
        self._selected_shape_node_id = ""
        self._push_immediate_shape_change(before, "Delete shape point")
        return True

    def _maximum_shape_roundness(
        self, bound: BoundGeometry, node: PathNode,
    ) -> float:
        bound = self._contour_bound_for_node(bound, node)
        index = bound.nodes.index(node)
        if not bound.closed and index in {0, len(bound.nodes) - 1}:
            return 0.0

        def segment_length(segment: int) -> float:
            previous = self._shape_segment_point(bound, segment, 0.0)
            total = 0.0
            for step in range(1, 49):
                current = self._shape_segment_point(
                    bound, segment, step / 48
                )
                total += math.dist(
                    (previous.x(), previous.y()),
                    (current.x(), current.y()),
                )
                previous = current
            return total

        incoming = index - 1 if index else len(bound.nodes) - 1
        outgoing = index
        return min(
            segment_length(incoming), segment_length(outgoing)
        ) / 2

    def _toggle_shape_node_roundness(
        self, bound: BoundGeometry, node: PathNode,
    ) -> None:
        if node.roundness_enabled:
            node.roundness_enabled = False
            return
        maximum = self._maximum_shape_roundness(bound, node)
        if maximum <= 0:
            return
        node.roundness = min(node.roundness, maximum)
        node.roundness_enabled = True

    def _begin_shape_edit(
        self, world_point: QPointF, allow_interior: bool = True,
    ) -> bool:
        layer = self.chapter.layers[self.selected_id]
        if layer.layer_kind == "fill" or layer.bound is None:
            return False
        wx, wy = self.chapter.layer_world_translation(layer.layer_id)
        local = QPointF(world_point.x() - wx, world_point.y() - wy)
        bound = layer.bound
        bound.normalize_bezier_handles()
        hit = self._shape_hit_test(bound, local)
        self._shape_hover_target = hit
        self._shape_hover_insert = (
            hit["insert"] if hit and hit["kind"] == "insert" else None
        )
        if hit is None:
            return False
        kind = hit["kind"]
        if kind == "interior" and not allow_interior:
            return False
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
            elif name == "delete":
                self._delete_selected_shape_node(layer)
            elif name == "cap":
                before = self.chapter.to_dict()
                self._cycle_shape_cap(layer, selected)
                self._push_immediate_shape_change(before, "Change line cap")
            else:
                self._model_before = self.chapter.to_dict()
                self._active_shape_control = name
                self._drag_start_doc = QPointF(world_point)
                self._shape_control_dragged = False
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
            node_id = hit["node_id"]
            shift = bool(
                QGuiApplication.keyboardModifiers() & Qt.ShiftModifier
            )
            if shift:
                if node_id in self._selected_shape_node_ids:
                    self._selected_shape_node_ids.remove(node_id)
                    if self._selected_shape_node_id == node_id:
                        self._selected_shape_node_id = next(
                            iter(self._selected_shape_node_ids), ""
                        )
                    self.update()
                    return True
                self._selected_shape_node_ids.add(node_id)
            else:
                if node_id not in self._selected_shape_node_ids:
                    self._selected_shape_node_ids = {node_id}
            self._selected_shape_node_id = node_id
            selected = self._selected_shape_node(bound)
            self._model_before = self.chapter.to_dict()
            self._active_shape_control = "node"
            self._drag_start_value = selected.to_dict()
            self._drag_start_doc = QPointF(world_point)
            self._shape_drag_nodes = {
                node.node_id: node.to_dict()
                for contour in bound.iter_contours()
                for node in contour.nodes
                if node.node_id in self._selected_shape_node_ids
            }
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
                index, percent, insert_point, world_point,
                contour_index=int(hit.get("contour_index", 0)),
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
        if start.point_type == "bezier":
            start.outgoing = q0
        if end.point_type == "bezier":
            end.incoming = q2
        if start.point_type == "bezier" and start.incoming is not None:
            start.handles_locked = False
        if end.point_type == "bezier" and end.outgoing is not None:
            end.handles_locked = False
        return PathNode(
            x=point[0], y=point[1], point_type="bezier",
            incoming=r0, outgoing=r1, handles_locked=False,
        )

    def _insert_shape_node(
        self, index: int, percent: float, insert_point: QPointF,
        world_point: QPointF, contour_index: int = 0,
    ) -> None:
        layer = self.chapter.layers[self.selected_id]
        bound = layer.bound
        before = self.chapter.to_dict()
        bound.primitive = "custom"
        contour = (
            PathContour(bound.nodes, bound.closed)
            if contour_index == 0
            else bound.additional_contours[contour_index - 1]
        )
        working = BoundGeometry(
            nodes=contour.nodes, closed=contour.closed,
            primitive="custom",
        )
        node = self._split_shape_segment(working, index, percent)
        contour.nodes.insert(index + 1, node)
        bound.normalize_bezier_handles()
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
        if (
            control == "roundness"
            and math.dist(
                (world_point.x(), world_point.y()),
                (self._drag_start_doc.x(), self._drag_start_doc.y()),
            ) > 3 / max(self.scale, 0.05)
        ):
            self._shape_control_dragged = True
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
            bound.nodes[index].roundness_enabled = (
                bound.nodes[index].roundness > 0
            )
        elif control == "node" and selected is not None:
            snapped = self._snap(world_point, layer.layer_id)
            target = QPointF(snapped.x() - wx, snapped.y() - wy)
            primary_start = self._shape_drag_nodes.get(
                selected.node_id, self._drag_start_value
            )
            dx = target.x() - float(primary_start["x"])
            dy = target.y() - float(primary_start["y"])
            for contour in bound.iter_contours():
                for node in contour.nodes:
                    source = self._shape_drag_nodes.get(node.node_id)
                    if source is None:
                        continue
                    node.position = (
                        float(source["x"]) + dx,
                        float(source["y"]) + dy,
                    )
                    incoming = source.get("incoming")
                    outgoing = source.get("outgoing")
                    node.incoming = (
                        (float(incoming[0]) + dx, float(incoming[1]) + dy)
                        if incoming is not None else None
                    )
                    node.outgoing = (
                        (float(outgoing[0]) + dx, float(outgoing[1]) + dy)
                        if outgoing is not None else None
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
            snapped = self._snap(world_point, layer.layer_id)
            target = (snapped.x() - wx, snapped.y() - wy)
            self._move_shape_bezier_handle(
                bound, selected, control, target
            )
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
            if self._shape_control_dragged:
                selected.roundness = min(
                    self._maximum_shape_roundness(bound, selected),
                    max(
                        0.0,
                        math.dist(
                            selected.position, (local.x(), local.y())
                        ),
                    ),
                )
                selected.roundness_enabled = True
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
            bound.additional_contours = [
                PathContour.from_dict(contour.to_dict())
                for contour in original.additional_contours
            ]
            for contour in bound.iter_contours():
                for node in contour.nodes:
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
        bound.normalize_bezier_handles()
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
        self.setToolTip(self._shape_hit_tooltip(
            layer.bound, hit, layer.shape_style
        ))
        self.update()

    def _toggle_shape_node_type(
        self, bound: BoundGeometry, node: PathNode,
    ) -> None:
        bound.primitive = "custom"
        working = self._contour_bound_for_node(bound, node)
        index = working.nodes.index(node)
        if node.point_type == "bezier":
            node.point_type = "vector"
            node.incoming = node.outgoing = None
            working.normalize_bezier_handles()
            return
        previous = (
            working.nodes[index - 1]
            if index or working.closed else None
        )
        following = (
            working.nodes[(index + 1) % len(working.nodes)]
            if working.closed or index + 1 < len(working.nodes) else None
        )
        node.point_type = "bezier"
        node.handles_locked = previous is not None and following is not None
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
        working.normalize_bezier_handles()

    @staticmethod
    def _move_shape_bezier_handle(
        bound: BoundGeometry, node: PathNode, control: str,
        target: tuple[float, float],
    ) -> None:
        needs_incoming, needs_outgoing = bound.handle_requirements(node)
        permitted = {
            "incoming": needs_incoming,
            "outgoing": needs_outgoing,
        }
        if not permitted.get(control, False):
            return
        setattr(node, control, target)
        other = "outgoing" if control == "incoming" else "incoming"
        if node.handles_locked and permitted[other]:
            setattr(node, other, (
                node.x * 2 - target[0], node.y * 2 - target[1]
            ))
        bound.normalize_bezier_handles()

    @staticmethod
    def _toggle_shape_node_lock(
        bound: BoundGeometry, node: PathNode,
    ) -> None:
        needs_incoming, needs_outgoing = bound.handle_requirements(node)
        if not (needs_incoming and needs_outgoing):
            node.handles_locked = False
            bound.normalize_bezier_handles()
            return
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
        bound.normalize_bezier_handles()

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
        if (
            self._gradient_creation_type == "line"
            and self._gradient_creation_parent_id
        ):
            if len(self._creation_nodes) < 2:
                return
            nodes = [
                PathNode.from_dict(node.to_dict())
                for node in self._creation_nodes
            ]
            bound = BoundGeometry.path(nodes, False)
            bound.normalize_bezier_handles()
            self.create_gradient(
                self._gradient_creation_parent_id,
                "line",
                world_geometry=bound,
                before=self._gradient_creation_before,
            )
            return
        if self._page_creation_anchor_id:
            if len(self._creation_nodes) < 3:
                self.pageCreationInvalid.emit(
                    "A page shape requires at least three points."
                )
                return
            nodes = [
                PathNode.from_dict(node.to_dict())
                for node in self._creation_nodes
            ]
            bound = BoundGeometry.path(nodes, True)
            bound.normalize_bezier_handles()
            if not self._finish_pending_page_bound(bound):
                return
            return
        minimum = 3 if closed else 2
        if len(self._creation_nodes) < minimum:
            return
        nodes = [PathNode.from_dict(node.to_dict()) for node in self._creation_nodes]
        bound = BoundGeometry.path(nodes, closed)
        bound.normalize_bezier_handles()
        self._creation_nodes = []
        self._creation_points = []
        self._creation_selected_node_id = ""
        self._creation_active_control = None
        self._shape_hover_target = None
        self._shape_hover_insert = None
        style = self._creation_style or ShapeStyle(
            primary_color=self.secondary_color,
            base_thickness=float(self.settings.pencil_size()),
            outline_color=self.primary_color,
            outline_thickness=4.0,
        )
        if closed:
            style = ShapeStyle.from_dict(style.to_dict())
            style.primary_color = self.secondary_color
        self._creation_style = None
        created = self._create_layer_from_world_bound(
            bound, style=style,
        )
        if created is not None:
            self.set_tool(ToolKind.SHAPE_EDIT)

    def _create_layer_from_world_bound(
        self, bound: BoundGeometry, style: ShapeStyle | None = None,
    ) -> LayerNode | None:
        placement = self._target_placement_for_new_bound()
        if placement is None:
            return None
        parent_id, insertion_index = placement
        before = self.chapter.to_dict()
        parent_x, parent_y = self.chapter.layer_world_translation(parent_id)
        local = BoundGeometry.from_dict(bound.to_dict())
        for contour in local.iter_contours():
            for node in contour.nodes:
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
        return layer

    @staticmethod
    def _geometry_from_painter_path(
        path: QPainterPath,
    ) -> BoundGeometry | None:
        contours: list[PathContour] = []
        nodes: list[PathNode] = []

        def finish() -> None:
            nonlocal nodes
            if len(nodes) > 1 and math.dist(
                nodes[0].position, nodes[-1].position
            ) <= 1e-5:
                closing = nodes.pop()
                if closing.incoming is not None:
                    nodes[0].point_type = "bezier"
                    nodes[0].incoming = closing.incoming
            if len(nodes) >= 3:
                for node in nodes:
                    if node.point_type == "bezier":
                        node.incoming = node.incoming or node.position
                        node.outgoing = node.outgoing or node.position
                        node.handles_locked = False
                contours.append(PathContour(nodes=nodes, closed=True))
            nodes = []

        index = 0
        while index < path.elementCount():
            element = path.elementAt(index)
            if element.isMoveTo():
                finish()
                nodes = [PathNode(x=element.x, y=element.y)]
                index += 1
                continue
            if not nodes:
                index += 1
                continue
            if element.isLineTo():
                nodes.append(PathNode(x=element.x, y=element.y))
                index += 1
                continue
            if element.isCurveTo() and index + 2 < path.elementCount():
                control_two = path.elementAt(index + 1)
                endpoint = path.elementAt(index + 2)
                nodes[-1].point_type = "bezier"
                nodes[-1].outgoing = (element.x, element.y)
                nodes.append(PathNode(
                    x=endpoint.x, y=endpoint.y, point_type="bezier",
                    incoming=(control_two.x, control_two.y),
                    handles_locked=False,
                ))
                index += 3
                continue
            index += 1
        finish()
        if not contours:
            return None
        primary, *additional = contours
        geometry = BoundGeometry(
            nodes=primary.nodes, closed=True, primitive="custom",
            additional_contours=additional,
        )
        geometry.normalize_bezier_handles()
        return geometry

    def flatten_compound_layer(self, layer_id: str) -> bool:
        if (
            self.chapter is None or layer_id not in self.chapter.layers
            or not self.chapter.layers[layer_id].compound_enabled
        ):
            return False
        layer = self.chapter.layers[layer_id]
        calculated = self.layer_effective_path(layer_id)
        geometry = self._geometry_from_painter_path(calculated)
        if geometry is None:
            return False
        before = self.chapter.to_dict()
        root_x, root_y = self.chapter.layer_world_translation(layer_id)
        removed_layers: set[str] = set()

        def removed_opacity(parent_id: str) -> float:
            factor = 1.0
            cursor = parent_id
            while cursor and cursor != layer_id:
                current = self.chapter.layers[cursor]
                factor *= current.opacity
                cursor = current.parent_id
            return factor

        def reparent_object(obj: DocumentObject) -> ChildRef:
            old_x, old_y = self.chapter.layer_world_translation(
                obj.parent_layer_id
            )
            dx, dy = old_x - root_x, old_y - root_y
            if isinstance(obj, TextObject):
                if (
                    obj.layout_mode == "strict"
                    and obj.geometry_reference == "direct"
                ):
                    rect = self._strict_text_rect(obj).translated(dx, dy)
                    obj.transform_quad = self._rect_quad(rect)
                    obj.layout_mode = "free"
                elif obj.layout_mode == "free" and obj.transform_quad:
                    obj.transform_quad = [
                        (x + dx, y + dy) for x, y in obj.transform_quad
                    ]
            elif isinstance(obj, GradientObject):
                for contour in obj.line_field.geometry.iter_contours():
                    for node in contour.nodes:
                        node.x += dx
                        node.y += dy
                        if node.incoming is not None:
                            node.incoming = (
                                node.incoming[0] + dx,
                                node.incoming[1] + dy,
                            )
                        if node.outgoing is not None:
                            node.outgoing = (
                                node.outgoing[0] + dx,
                                node.outgoing[1] + dy,
                            )
                radial = obj.radial_field
                radial.origin_x += dx
                radial.origin_y += dy
                if radial.manual_center is not None:
                    radial.manual_center = (
                        radial.manual_center[0] + dx,
                        radial.manual_center[1] + dy,
                    )
                if obj.shape_field.manual_center is not None:
                    obj.shape_field.manual_center = (
                        obj.shape_field.manual_center[0] + dx,
                        obj.shape_field.manual_center[1] + dy,
                    )
                obj.touch_revision()
            obj.x += dx
            obj.y += dy
            opacity_factor = removed_opacity(obj.parent_layer_id)
            if opacity_factor != 1.0:
                if obj.opacity_locked:
                    obj.opacity_locked = False
                    obj.opacity = opacity_factor
                else:
                    obj.opacity *= opacity_factor
            obj.parent_layer_id = layer_id
            obj.geometry_reference = "direct"
            if isinstance(obj, RasterObject):
                layer.last_raster_id = obj.object_id
            return ChildRef("object", obj.object_id)

        def preserve_ignored(candidate: LayerNode) -> ChildRef:
            opacity_factor = removed_opacity(candidate.parent_id)
            world_x, world_y = self.chapter.layer_world_translation(
                candidate.layer_id
            )
            candidate.parent_id = layer_id
            candidate.translate_x = world_x - root_x
            candidate.translate_y = world_y - root_y
            candidate.opacity *= opacity_factor
            return ChildRef("layer", candidate.layer_id)

        def flatten_branch(candidate: LayerNode) -> list[ChildRef]:
            result: list[ChildRef] = []
            for reference in list(candidate.children):
                if reference.kind == "object":
                    result.append(reparent_object(
                        self.chapter.objects[reference.entity_id]
                    ))
                    continue
                child = self.chapter.layers[reference.entity_id]
                if child.compound_operation == "ignore" or not child.visible:
                    result.append(preserve_ignored(child))
                elif child.layer_kind == "fill":
                    removed_layers.add(child.layer_id)
                else:
                    result.extend(flatten_branch(child))
                    removed_layers.add(child.layer_id)
            candidate.children = []
            return result

        rebuilt: list[ChildRef] = []
        for reference in list(layer.children):
            if reference.kind == "object":
                rebuilt.append(reference)
                obj = self.chapter.objects[reference.entity_id]
                obj.geometry_reference = "direct"
                continue
            child = self.chapter.layers[reference.entity_id]
            if child.compound_operation == "ignore" or not child.visible:
                rebuilt.append(reference)
            elif child.layer_kind == "fill":
                removed_layers.add(child.layer_id)
            else:
                rebuilt.extend(flatten_branch(child))
                removed_layers.add(child.layer_id)
        for removed_id in removed_layers:
            self.chapter.layers.pop(removed_id, None)
        layer.children = rebuilt
        layer.bound = geometry
        layer.layer_kind = "bounded"
        layer.compound_enabled = False
        layer.compound_operation = "add"
        self._compound_path_cache.clear()
        after = self.chapter.to_dict()
        self.push_model_change(before, after, "Flatten compound shape")
        self.documentChanged.emit(QRectF())
        self.hierarchyChanged.emit()
        self.set_selection("layer", layer_id)
        self.update()
        return True

    def _begin_stroke(self, point: QPointF, pressure: float) -> None:
        if self.chapter is None or self.selected_kind != "object":
            return
        obj = self.chapter.objects.get(self.selected_id)
        if not isinstance(obj, RasterObject):
            return
        layer_x, layer_y = self.chapter.layer_world_translation(obj.parent_layer_id)
        local = QPointF(round(point.x() - layer_x - obj.x), round(point.y() - layer_y - obj.y))
        self._drawing = True
        self._last_draw_point = local
        self._last_pressure = pressure if pressure > 0.001 else 1.0
        self._stroke_before = {}
        self._stroke_frame_before = tuple(obj.interaction_rect)
        self._stroke_erasing = self.tool == ToolKind.RASTER_ERASER
        self._predictive = None
        size, opacity = self._brush_values(self._last_pressure)
        if self.tool == ToolKind.RASTER_PENCIL:
            size *= self._preset.stroke_start_ratio
        dirty = self.tiles.paint_dab(
            obj.object_id, local, size, QColor(self.primary_color), opacity,
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
            QColor(self.primary_color), opacity_start, opacity_end,
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
                QColor(self.primary_color),
            )
        self._emit_raster_dirty(obj, dirty)

    def _end_stroke(self) -> None:
        obj = self.chapter.objects[self.selected_id]
        if (
            not self._stroke_erasing
            and self._preset.stroke_end_ratio < 0.999
        ):
            size, opacity = self._brush_values(self._last_pressure)
            dirty = self.tiles.paint_dab(
                obj.object_id, self._last_draw_point,
                size * self._preset.stroke_end_ratio,
                QColor(self.primary_color), opacity,
                antialias=self._preset.antialiasing,
                before=self._stroke_before,
            )
            self._emit_raster_dirty(obj, dirty)
        keys = set(self._stroke_before)
        self.tiles.prune_empty(obj.object_id, keys)
        frame_before = (
            self._stroke_frame_before
            if self._stroke_frame_before is not None
            else tuple(obj.interaction_rect)
        )
        content = self.tiles.content_bounds(obj.object_id)
        if content is None:
            obj.interaction_rect = frame_before
        else:
            padded = content.adjusted(
                -RASTER_FRAME_MARGIN, -RASTER_FRAME_MARGIN,
                RASTER_FRAME_MARGIN, RASTER_FRAME_MARGIN,
            )
            frame = (
                padded
                if self._stroke_erasing
                else QRectF(*frame_before).united(padded)
            )
            obj.interaction_rect = (
                frame.left(), frame.top(),
                max(1.0, frame.width()), max(1.0, frame.height()),
            )
        frame_after = tuple(obj.interaction_rect)
        after = self.tiles.snapshot(obj.object_id, keys)
        command = TilePatchCommand(
            "Raster stroke", self.tiles, obj.object_id,
            self._stroke_before, after,
            lambda: (self.update(), self.documentChanged.emit(QRectF())),
            frame_before, frame_after,
            lambda frame, object_id=obj.object_id:
            self._restore_raster_frame(object_id, frame),
        )
        self.command_stack.push(command, already_done=True)
        self._stroke_before = {}
        self._stroke_frame_before = None
        self._stroke_erasing = False
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
        opacity *= QColor(self.primary_color).alphaF()
        return max(0.5, size), opacity

    def refresh_brush_settings(self) -> None:
        self._preset = self.settings.active_brush_preset()
        self.update()

    def _emit_raster_dirty(self, obj: RasterObject, local: QRectF) -> None:
        frame = QRectF(*obj.interaction_rect)
        if not self._stroke_erasing:
            frame = frame.united(local.adjusted(
                -RASTER_FRAME_MARGIN, -RASTER_FRAME_MARGIN,
                RASTER_FRAME_MARGIN, RASTER_FRAME_MARGIN,
            ))
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

    def _restore_raster_frame(
        self, object_id: str, frame: object,
    ) -> None:
        if (
            self.chapter is None or frame is None
            or object_id not in self.chapter.objects
        ):
            return
        obj = self.chapter.objects[object_id]
        if isinstance(obj, RasterObject):
            obj.interaction_rect = tuple(frame)

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

    def _transform_control_hit(
        self, quad: list[tuple[float, float]], point: QPointF,
    ) -> tuple[str, int | None]:
        tolerance = 14 / max(self.scale, 0.05)
        handles, rotate, pivot = self._transform_control_points(
            quad, self._transform_pivot
        )
        pivot_distance = math.dist(point.toTuple(), pivot.toTuple())
        if (
            4 / max(self.scale, 0.05)
            <= pivot_distance <= tolerance
        ):
            return "pivot", None
        if math.dist(point.toTuple(), rotate.toTuple()) <= tolerance:
            return "rotate", None
        distances = [
            math.dist(point.toTuple(), candidate) for candidate in handles
        ]
        if distances and min(distances) <= tolerance:
            return "handle", distances.index(min(distances))
        path = QPainterPath()
        path.addPolygon(QPolygonF([QPointF(*candidate) for candidate in quad]))
        return ("translate", None) if path.contains(point) else ("", None)

    @staticmethod
    def _point_segment_distance(
        point: QPointF, start: tuple[float, float],
        end: tuple[float, float],
    ) -> float:
        dx, dy = end[0] - start[0], end[1] - start[1]
        length_squared = dx * dx + dy * dy
        if length_squared <= 1e-9:
            return math.dist(point.toTuple(), start)
        percent = max(0.0, min(
            1.0,
            ((point.x() - start[0]) * dx
             + (point.y() - start[1]) * dy) / length_squared,
        ))
        nearest = start[0] + percent * dx, start[1] + percent * dy
        return math.dist(point.toTuple(), nearest)

    def _raster_transform_control_hit(
        self, quad: list[tuple[float, float]], point: QPointF,
    ) -> tuple[str, int | None]:
        tolerance = 14 / max(self.scale, 0.05)
        handles, rotate, pivot = self._transform_control_points(
            quad, self._transform_pivot
        )
        pivot_distance = math.dist(point.toTuple(), pivot.toTuple())
        if 4 / max(self.scale, 0.05) <= pivot_distance <= tolerance:
            return "pivot", None
        if math.dist(point.toTuple(), rotate.toTuple()) <= tolerance:
            return "rotate", None
        distances = [
            math.dist(point.toTuple(), candidate) for candidate in handles
        ]
        if distances and min(distances) <= tolerance:
            return "handle", distances.index(min(distances))
        edge_distance = min(
            self._point_segment_distance(
                point, quad[index], quad[(index + 1) % 4]
            )
            for index in range(4)
        )
        # Translation is deliberately an outside-frame affordance.  A
        # pencil stroke on the raster edge must not be mistaken for a move.
        frame_path = QPainterPath()
        frame_path.addPolygon(QPolygonF([
            QPointF(*candidate) for candidate in quad
        ]))
        frame_path.closeSubpath()
        outside_margin = 20.0 / max(self.scale, 0.05)
        if (
            edge_distance <= outside_margin
            and (
                edge_distance <= 1.0e-6
                or not frame_path.contains(point)
            )
        ):
            return "translate", None
        return "", None

    def _begin_geometry_transform(self, point: QPointF) -> bool:
        if self.chapter is None:
            return False
        if self.selected_kind == "layer":
            layer = self.chapter.layers.get(self.selected_id)
            if layer is None or layer.bound is None:
                return False
            parent_id = layer.layer_id
            wx, wy = self.chapter.layer_world_translation(parent_id)
            left, top, width, height = layer.bound.bbox()
            local_quad = self._rect_quad(QRectF(
                left, top, max(1.0, width), max(1.0, height)
            ))
            world_quad = [(x + wx, y + wy) for x, y in local_quad]
            target = ("layer", layer.layer_id)
        else:
            obj = self.chapter.objects.get(self.selected_id)
            if (
                not isinstance(obj, GradientObject)
                or obj.field_type == "parent_shape"
            ):
                return False
            parent_id = obj.parent_layer_id
            wx, wy = self.chapter.layer_world_translation(parent_id)
            world_quad = self.object_world_quad(obj.object_id)
            if not world_quad:
                return False
            local_quad = [(x - wx, y - wy) for x, y in world_quad]
            target = ("object", obj.object_id)
        mode, handle = self._raster_transform_control_hit(
            world_quad, point
        )
        if not mode:
            return False
        self._geometry_transform_target = target
        self._transform_handle_index = handle
        self._transform_drag_mode = mode
        self._model_before = self.chapter.to_dict()
        self._drag_start_doc = QPointF(point)
        self._transform_start_quad = list(local_quad)
        self._transform_preview_quad = list(local_quad)
        pivot = self._transform_pivot or QPointF(
            sum(x for x, _ in world_quad) / 4,
            sum(y for _, y in world_quad) / 4,
        )
        self._transform_rotate_start = math.atan2(
            point.y() - pivot.y(), point.x() - pivot.x()
        )
        return True

    def _geometry_transform_parent_id(self) -> str:
        if self._geometry_transform_target is None:
            return ""
        kind, entity_id = self._geometry_transform_target
        if kind == "layer":
            return entity_id
        obj = self.chapter.objects.get(entity_id)
        return obj.parent_layer_id if obj is not None else ""

    def _update_geometry_transform_preview(self, point: QPointF) -> None:
        parent_id = self._geometry_transform_parent_id()
        if not parent_id or self._transform_start_quad is None:
            return
        layer_x, layer_y = self.chapter.layer_world_translation(parent_id)
        local_point = (point.x() - layer_x, point.y() - layer_y)
        start = list(self._transform_start_quad)
        dx = point.x() - self._drag_start_doc.x()
        dy = point.y() - self._drag_start_doc.y()
        if self._transform_drag_mode == "pivot":
            self._transform_pivot = QPointF(point)
            self._transform_pivot_custom = True
            return
        if self._transform_drag_mode == "rotate":
            world_pivot = self._transform_pivot or QPointF(
                sum(x for x, _ in start) / 4 + layer_x,
                sum(y for _, y in start) / 4 + layer_y,
            )
            angle = (
                math.atan2(
                    point.y() - world_pivot.y(),
                    point.x() - world_pivot.x(),
                )
                - self._transform_rotate_start
            )
            cosine, sine = math.cos(angle), math.sin(angle)
            pivot = QPointF(
                world_pivot.x() - layer_x, world_pivot.y() - layer_y
            )
            candidate = [
                (
                    pivot.x() + (x - pivot.x()) * cosine
                    - (y - pivot.y()) * sine,
                    pivot.y() + (x - pivot.x()) * sine
                    + (y - pivot.y()) * cosine,
                )
                for x, y in start
            ]
        elif self._transform_drag_mode == "translate":
            candidate = [(x + dx, y + dy) for x, y in start]
        elif self.settings.transform_mode == "uniform":
            handle = self._transform_handle_index
            anchors = start + self._edge_midpoints(start)
            opposite = [2, 3, 0, 1, 6, 7, 4, 5][handle]
            origin, initial = anchors[opposite], anchors[handle]
            target = self._snap_transform_point(local_point, parent_id)
            factor = math.dist(origin, target) / max(
                math.dist(origin, initial), 1e-6
            )
            candidate = [
                (
                    origin[0] + (x - origin[0]) * factor,
                    origin[1] + (y - origin[1]) * factor,
                )
                for x, y in start
            ]
        else:
            handle = self._transform_handle_index
            target = self._snap_transform_point(local_point, parent_id)
            candidate = list(start)
            if handle < 4:
                candidate[handle] = target
            else:
                edge = handle - 4
                midpoint = self._edge_midpoints(start)[edge]
                change = target[0] - midpoint[0], target[1] - midpoint[1]
                for index in (edge, (edge + 1) % 4):
                    candidate[index] = (
                        start[index][0] + change[0],
                        start[index][1] + change[1],
                    )
        if self._quad_is_valid(candidate):
            self._transform_preview_quad = candidate

    def _commit_geometry_transform(self) -> None:
        target = self._geometry_transform_target
        before = self._model_before
        source = self._transform_start_quad
        destination = self._transform_preview_quad
        drag_mode = self._transform_drag_mode
        self._geometry_transform_target = None
        self._model_before = None
        self._transform_start_quad = None
        self._transform_preview_quad = None
        self._transform_handle_index = None
        self._transform_drag_mode = None
        if (
            target is None or before is None or source is None
            or destination is None or drag_mode == "pivot"
        ):
            self.update()
            return
        transform = QTransform.quadToQuad(
            QPolygonF([QPointF(*value) for value in source]),
            QPolygonF([QPointF(*value) for value in destination]),
        )

        def map_point(value: tuple[float, float]) -> tuple[float, float]:
            mapped = transform.map(QPointF(*value))
            return mapped.x(), mapped.y()

        kind, entity_id = target
        if kind == "layer":
            layer = self.chapter.layers[entity_id]
            for contour in layer.bound.iter_contours():
                for node in contour.nodes:
                    node.position = map_point(node.position)
                    if node.incoming is not None:
                        node.incoming = map_point(node.incoming)
                    if node.outgoing is not None:
                        node.outgoing = map_point(node.outgoing)
            if drag_mode not in {"translate"}:
                layer.bound.primitive = "custom"
            layer.bound.normalize_bezier_handles()
            label = "Transform shape"
        else:
            obj = self.chapter.objects[entity_id]
            if obj.field_type == "line":
                for contour in obj.line_field.geometry.iter_contours():
                    for node in contour.nodes:
                        node.position = map_point(node.position)
                        if node.incoming is not None:
                            node.incoming = map_point(node.incoming)
                        if node.outgoing is not None:
                            node.outgoing = map_point(node.outgoing)
            else:
                field = obj.radial_field
                origin = map_point((field.origin_x, field.origin_y))
                radius_y = (
                    field.radius_y if field.ellipse_enabled
                    else field.radius_x
                )
                x_point = map_point(self._rotated_gradient_point(
                    (field.origin_x, field.origin_y),
                    (field.radius_x, 0), field.rotation,
                ))
                y_point = map_point(self._rotated_gradient_point(
                    (field.origin_x, field.origin_y),
                    (0, radius_y), field.rotation,
                ))
                field.origin_x, field.origin_y = origin
                field.radius_x = max(0.001, math.dist(origin, x_point))
                field.radius_y = max(0.001, math.dist(origin, y_point))
                field.rotation = math.degrees(math.atan2(
                    x_point[1] - origin[1], x_point[0] - origin[0]
                ))
                field.ellipse_enabled = True
                if field.manual_center is not None:
                    field.manual_center = map_point(field.manual_center)
            obj.touch_revision()
            label = "Transform gradient"
        after = self.chapter.to_dict()
        if before != after:
            self.push_model_change(before, after, label)
            self.hierarchyChanged.emit()
        self.documentChanged.emit(QRectF())
        self.update()

    def _begin_selected_raster_transform(self, point: QPointF) -> bool:
        if self.chapter is None or not self.selected_object_id:
            return False
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, RasterObject):
            return False
        world_quad = self.object_world_quad(obj.object_id)
        if not world_quad:
            return False
        mode, handle = self._raster_transform_control_hit(
            world_quad, point
        )
        if not mode:
            return False
        layer_x, layer_y = self.chapter.layer_world_translation(
            obj.parent_layer_id
        )
        self._transform_handle_index = handle
        self._transform_drag_mode = mode
        self._model_before = self.chapter.to_dict()
        self._drag_start_doc = QPointF(point)
        self._transform_start_quad = [
            (x - layer_x, y - layer_y) for x, y in world_quad
        ]
        self._transform_preview_quad = list(self._transform_start_quad)
        pivot = self._transform_pivot or QPointF(
            sum(x for x, _ in world_quad) / 4,
            sum(y for _, y in world_quad) / 4,
        )
        self._transform_rotate_start = math.atan2(
            point.y() - pivot.y(), point.x() - pivot.x()
        )
        self._build_raster_transform_cache()
        return True

    def _begin_or_defer_selected_raster_transform(
        self, widget_point: QPointF, point: QPointF,
    ) -> bool:
        """Reserve an outside-frame translation until a drag is confirmed."""
        if self.chapter is None or not self.selected_object_id:
            return False
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, RasterObject):
            return False
        quad = self.object_world_quad(obj.object_id)
        if not quad:
            return False
        mode, _handle = self._raster_transform_control_hit(quad, point)
        if mode == "translate":
            self._pending_raster_transform_press = (
                QPointF(widget_point), QPointF(point)
            )
            return True
        return self._begin_selected_raster_transform(point)

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
        if not self.settings.snap_to_grid:
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
        if self._transform_drag_mode == "pivot":
            self._transform_pivot = QPointF(point)
            self._transform_pivot_custom = True
            return
        if self._transform_drag_mode == "rotate":
            world_pivot = self._transform_pivot or QPointF(
                sum(x for x, _ in start) / 4 + layer_x,
                sum(y for _, y in start) / 4 + layer_y,
            )
            current_angle = math.atan2(
                point.y() - world_pivot.y(),
                point.x() - world_pivot.x(),
            )
            angle = current_angle - self._transform_rotate_start
            cosine, sine = math.cos(angle), math.sin(angle)
            pivot = QPointF(
                world_pivot.x() - layer_x, world_pivot.y() - layer_y
            )
            candidate = [
                (
                    pivot.x()
                    + (x - pivot.x()) * cosine
                    - (y - pivot.y()) * sine,
                    pivot.y()
                    + (x - pivot.x()) * sine
                    + (y - pivot.y()) * cosine,
                )
                for x, y in start
            ]
        elif self._transform_drag_mode == "translate":
            candidate = [(x + dx, y + dy) for x, y in start]
            if self.settings.snap_to_grid:
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
        source = list(self._transform_start_quad)
        drag_mode = self._transform_drag_mode
        self._model_before = None
        self._transform_start_quad = None
        self._transform_preview_quad = None
        self._transform_handle_index = None
        self._transform_drag_mode = None
        self._transform_static_cache = QImage()
        if drag_mode == "pivot":
            self.update()
            return
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
        if drag_mode == "translate":
            obj.x += destination[0][0] - source[0][0]
            obj.y += destination[0][1] - source[0][1]
            after_model = self.chapter.to_dict()
            if before_model != after_model:
                self.push_model_change(
                    before_model, after_model, "Translate raster"
                )
                self.hierarchyChanged.emit()
            self.documentChanged.emit(QRectF())
            self.update()
            return
        before_tiles = self.tiles.object_tiles(object_id)
        try:
            after_tiles = self.tiles.projective_transform(
                object_id, obj.x, obj.y, destination,
                QRectF(*obj.interaction_rect),
            )
        except ValueError:
            self.update()
            return
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
            apply(after_model, after_tiles)

        def undo_transform() -> None:
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
                self.layer_effective_path(layer.layer_id),
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
        document = self._text_document(obj, source.width())
        offset = self._text_vertical_offset(obj, document, source.height())
        transform = self._quad_transform(source, self._text_quad(obj))
        transform.translate(0, offset)
        return (
            document,
            QPointF(layer_x, layer_y),
            transform,
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
