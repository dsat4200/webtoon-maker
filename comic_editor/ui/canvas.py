"""Tiled vertical document viewport, renderer, and drawing tools."""
from __future__ import annotations

import gc
import base64
import html as html_lib
import math
import time
import zlib
import json
import mimetypes
import re
import threading
import urllib.parse
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

import numpy as np

from PySide6.QtCore import (
    QBuffer, QByteArray, QEvent, QIODevice, QPoint, QPointF, QRect, QRectF, Qt,
    QObject, QRunnable, QThreadPool, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QAbstractTextDocumentLayout, QBrush, QColor, QFont, QFontMetricsF,
    QGuiApplication,
    QImage, QInputDevice, QInputMethodEvent,
    QLinearGradient,
    QMouseEvent, QOffscreenSurface, QOpenGLContext, QPainter, QPainterPath,
    QPainterPathStroker, QPalette,
    QPen, QPolygonF, QRadialGradient, QSurfaceFormat, QTextBlockFormat,
    QTextCursor, QTextDocument, QTransform, QValidator,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QHBoxLayout, QSpinBox, QToolButton, QWidget,
)

from comic_editor.core.commands import (
    CallbackCommand, CommandStack, ObjectPatchCommand, TilePatchCommand,
)
from comic_editor.core.assets import (
    AssetManifest, AssetRepository, entity_visual_bounds, instantiate_asset,
)
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ChildRef, ColorFillGradientObject,
    ColorGradientRamp, ColorGradientStop, DocumentObject, GradientObject,
    LineGradientField, LayerNode, RadialGradientField,
    ImageObject, PathContour, PathNode, RasterObject, ShapeStyle, TextObject,
    BlurModifier, OutlineModifier, ToneMask,
    SpeedLineCenterObject, SpeedLinesGradientObject, VectorDrawingObject,
    VectorStroke, VectorStrokePoint,
    ImageSourceDescriptor, canonical_argb, image_source_from_dict,
    object_from_dict,
)
from comic_editor.core.pressure import BrushPreset
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.images import ImageStore
from comic_editor.core.vector_geometry import (
    Cubic, CubicSpan, FreehandSample, centerline_hit, connect_cubic_paths,
    corridor_contains, corridor_hits_path, corridor_path_intervals,
    cubic_derivative, cubic_eval, cubic_subsegment,
    distance_to_polyline, erase_stroke_by_corridor,
    fit_freehand, flatten_stroke, interpolate_stroke_attribute,
    nearest_on_path,
    nearest_on_stroke, path_self_intersections, point_in_polygon,
    path_intersections, simplify_cubic_segments,
    stroke_cubics, tangent_bridge,
)
from comic_editor.ui.windows_input import configure_simultaneous_pen_touch
from comic_editor.ui.modifier_rendering import (
    BlurPyramidCache, OutlineDistanceCache, apply_modifier_stack,
    apply_opacity_mask,
)


class ToolKind(Enum):
    OBJECT_SELECT = "object_select"
    RASTER_PENCIL = "raster_pencil"
    RASTER_ERASER = "raster_eraser"
    EYEDROPPER = "eyedropper"
    FILL = "fill"
    GRADIENT = "gradient"
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
    DRAW_SHAPE = "draw_shape"
    INSERT_PAGE_GAP = "insert_page_gap"
    BOX_BOUND = "box_bound"
    CIRCLE_BOUND = "circle_bound"
    SHAPE_CREATE = "shape_create"
    POLYGON_BOUND = "shape_create"
    RASTER_CREATE = "raster_create"


RASTER_FRAME_MARGIN = 24.0
SHAPE_CONTROL_SCALE = 1.5
ASSET_MIME = "application/x-webtoon-asset"
VECTOR_RENDER_CACHE_BUDGET = 64 * 1024 * 1024


class _FillWorkerSignals(QObject):
    finished = Signal(object, object)


class _FillWorker(QRunnable):
    """Run a fill against detached tiles and return a patch, never live data."""

    def __init__(
        self, store: TileStore, object_id: str, point: QPointF | None,
        frame: QRectF, color: QColor, profile: dict[str, object],
        region_policy: str,
        selection_tile, reference_tiles: dict[tuple[int, int], QImage] | None,
        cancel_event: threading.Event, context: dict,
    ) -> None:
        super().__init__()
        self.store = store
        self.object_id = object_id
        self.point = QPointF(point) if point is not None else None
        self.frame = QRectF(frame)
        self.color = QColor(color)
        self.profile = dict(profile)
        self.region_policy = str(region_policy)
        self.selection_tile = selection_tile
        self.reference_tiles = reference_tiles
        self.cancel_event = cancel_event
        self.context = context
        self.signals = _FillWorkerSignals()

    def run(self) -> None:
        before: dict[tuple[int, int], QImage | None] = {}
        try:
            dirty = self.store.advanced_fill(
                self.object_id, self.point, self.frame, self.color,
                self.profile, before, region_policy=self.region_policy,
                selection_tile=self.selection_tile,
                reference_tile=(
                    None if self.reference_tiles is None
                    else self.reference_tiles.get
                ),
                cancel_check=self.cancel_event.is_set,
            )
            after = (
                self.store.snapshot(self.object_id, set(before))
                if before and not self.cancel_event.is_set() else {}
            )
            result = {
                **self.context, "before": before, "after": after,
                "dirty": QRectF(dirty), "cancelled": self.cancel_event.is_set(),
                "error": None,
            }
        except Exception as error:  # pragma: no cover - defensive worker gate
            result = {
                **self.context, "before": {}, "after": {},
                "dirty": QRectF(), "cancelled": self.cancel_event.is_set(),
                "error": error,
            }
        self.signals.finished.emit(self, result)


class _FillReplayWorker(QRunnable):
    """Rebuild a recorded fill gesture against detached pre-fill tiles."""

    def __init__(
        self, store: TileStore, object_id: str, frame: QRectF,
        steps: list[tuple[QPointF | None, QPainterPath | None, str]],
        selection_path: QPainterPath, color: QColor,
        profile: dict[str, object],
        reference_tiles: dict[tuple[int, int], QImage] | None,
        cancel_event: threading.Event, context: dict,
    ) -> None:
        super().__init__()
        self.store = store
        self.object_id = object_id
        self.frame = QRectF(frame)
        self.steps = steps
        self.selection_path = QPainterPath(selection_path)
        self.color = QColor(color)
        self.profile = dict(profile)
        self.reference_tiles = reference_tiles
        self.cancel_event = cancel_event
        self.context = context
        self.signals = _FillWorkerSignals()

    def run(self) -> None:
        before: dict[tuple[int, int], QImage | None] = {}
        dirty = QRectF()
        size = self.store.tile_size

        def mask_for(path: QPainterPath, key: tuple[int, int]):
            if path.isEmpty():
                return None
            image = QImage(
                size, size, QImage.Format.Format_ARGB32_Premultiplied
            )
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.translate(-key[0] * size, -key[1] * size)
            painter.fillPath(path, QColor("white"))
            painter.end()
            rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
            values = np.frombuffer(bytes(rgba.constBits()), dtype=np.uint8)
            return values.reshape((size, size, 4))[..., 3] > 0

        try:
            for point, extra_path, policy in self.steps:
                if self.cancel_event.is_set():
                    break
                selection = QPainterPath(self.selection_path)
                if extra_path is not None:
                    selection = (
                        QPainterPath(extra_path) if selection.isEmpty()
                        else selection.intersected(extra_path)
                    )
                changed = self.store.advanced_fill(
                    self.object_id, point, self.frame, self.color,
                    self.profile, before, region_policy=policy,
                    reference_tile=(
                        None if self.reference_tiles is None
                        else self.reference_tiles.get
                    ),
                    selection_tile=lambda key, path=selection: mask_for(
                        path, key
                    ),
                    cancel_check=self.cancel_event.is_set,
                )
                if not changed.isEmpty():
                    dirty = (
                        QRectF(changed) if dirty.isEmpty()
                        else dirty.united(changed)
                    )
            after = (
                self.store.snapshot(self.object_id, set(before))
                if before and not self.cancel_event.is_set() else {}
            )
            result = {
                **self.context, "before": before, "after": after,
                "dirty": dirty, "cancelled": self.cancel_event.is_set(),
                "error": None,
            }
        except Exception as error:  # pragma: no cover - defensive worker gate
            result = {
                **self.context, "before": {}, "after": {},
                "dirty": QRectF(), "cancelled": self.cancel_event.is_set(),
                "error": error,
            }
        self.signals.finished.emit(self, result)


@dataclass
class _FillReplayState:
    chapter: ChapterDocument
    object_id: str
    object_model: str
    command: TilePatchCommand
    history_revision: int
    base_tiles: dict[tuple[int, int], QImage]
    steps: list[tuple[QPointF | None, QPainterPath | None, str]]
    selection_path: QPainterPath
    color: QColor
    profile: dict[str, object]
    reference_entities: list[tuple[str, str]]
    reference_signature: tuple
    reference_settings: tuple
    reference_tiles: dict[tuple[int, int], QImage]
    current_signature: tuple
    dirty_world: QRectF
VECTOR_RENDER_INDEX_CELL = 256.0
WHEEL_ZOOM_SETTLE_MS = 120


class _TextSizeSpinBox(QSpinBox):
    editStarted = Signal(int)
    editCanceled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._focus_value = 32
        self.setRange(6, 250)
        self.setSingleStep(1)
        self.setKeyboardTracking(False)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self._focus_value = self.value()
        super().focusInEvent(event)
        self.editStarted.emit(self._focus_value)
        QTimer.singleShot(0, self.selectAll)

    def validate(self, text: str, position: int):
        raw = text.strip()
        if raw in {"", "+", "-"}:
            return QValidator.State.Intermediate, text, position
        try:
            int(raw)
        except ValueError:
            return QValidator.State.Invalid, text, position
        return QValidator.State.Acceptable, text, position

    def valueFromText(self, text: str) -> int:  # noqa: N802
        try:
            value = int(text.strip())
        except ValueError:
            value = self.minimum()
        return max(self.minimum(), min(self.maximum(), value))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        QTimer.singleShot(0, self.selectAll)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.setValue(self._focus_value)
            self.editCanceled.emit()
            self.clearFocus()
            event.accept()
            return
        super().keyPressEvent(event)


class _TextGizmoOverlay(QWidget):
    sizeDecreaseRequested = Signal()
    sizeIncreaseRequested = Signal()
    boldRequested = Signal()
    italicRequested = Signal()
    sizeEditStarted = Signal(int)
    sizeEditCanceled = Signal()
    sizeCommitted = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("textGizmoOverlay")
        self.setStyleSheet(
            "#textGizmoOverlay { background: rgba(32,32,36,235); "
            "border: 1px solid #f2a23a; border-radius: 8px; }"
            "#textGizmoOverlay QToolButton { color: #f2a23a; "
            "font-weight: bold; border: 1px solid #8f6626; "
            "border-radius: 6px; padding: 1px 7px; }"
            "#textGizmoOverlay QToolButton:checked { color: #202024; "
            "background: #f2a23a; }"
            "#textGizmoOverlay QSpinBox { color: #f6f6f6; "
            "background: #1f1f23; border: 1px solid #8f6626; "
            "border-radius: 6px; padding: 1px 4px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        self.decrease = self._button("−", "Decrease font size by 1")
        self.size = _TextSizeSpinBox(self)
        self.size.setFixedSize(73, 34)
        self.size.setToolTip("Font size (6–250)")
        self.increase = self._button("+", "Increase font size by 1")
        self.bold = self._button("B", "Toggle bold", checkable=True)
        self.italic = self._button("I", "Toggle italic", checkable=True)
        italic_font = self.italic.font()
        italic_font.setItalic(True)
        self.italic.setFont(italic_font)
        for control in (
            self.decrease, self.size, self.increase, self.bold, self.italic,
        ):
            layout.addWidget(control)
            font = control.font()
            font.setPointSizeF(max(1.0, font.pointSizeF() * 1.4))
            control.setFont(font)
        self.decrease.clicked.connect(self.sizeDecreaseRequested)
        self.increase.clicked.connect(self.sizeIncreaseRequested)
        self.bold.clicked.connect(self.boldRequested)
        self.italic.clicked.connect(self.italicRequested)
        self.size.editStarted.connect(self.sizeEditStarted)
        self.size.editCanceled.connect(self.sizeEditCanceled)
        self.size.editingFinished.connect(
            lambda: self.sizeCommitted.emit(self.size.value())
        )
        self.hide()

    def _button(
        self, text: str, tooltip: str, *, checkable: bool = False,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setCheckable(checkable)
        button.setFixedSize(38, 34)
        return button

    def set_state(self, size: int, bold: bool, italic: bool) -> None:
        for control in (self.size, self.bold, self.italic):
            control.blockSignals(True)
        self.size.setValue(max(6, min(250, int(size))))
        self.bold.setChecked(bool(bold))
        self.italic.setChecked(bool(italic))
        for control in (self.size, self.bold, self.italic):
            control.blockSignals(False)


@dataclass
class CanvasSessionState:
    chapter: ChapterDocument
    tiles: TileStore
    images: ImageStore
    command_stack: CommandStack
    tool: ToolKind
    selected_kind: str
    selected_id: str
    active_page_id: str
    active_layer_id: str
    selected_object_id: str
    selected_entities: list[tuple[str, str]]
    center_x: float
    center_y: float
    scale: float
    rotation: float
    compound_cache: dict
    vector_cache: dict
    vector_cache_bytes: int
    vector_spatial_indexes: dict
    gradient_geometry_cache: dict
    gradient_scalar_cache: dict
    gradient_render_cache: dict
    modifier_render_cache: OrderedDict
    modifier_render_cache_bytes: int
    modifier_source_cache: OrderedDict
    modifier_source_cache_bytes: int
    outline_distance_cache: OutlineDistanceCache
    blur_pyramid_cache: BlurPyramidCache


class CanvasPerformanceMonitor:
    """Small bounded recorder for drawing-handler and frame timings."""

    def __init__(self, capacity: int = 512) -> None:
        self.input_ms: deque[float] = deque(maxlen=capacity)
        self.submit_ms: deque[float] = deque(maxlen=capacity)
        self.frame_ms: deque[float] = deque(maxlen=capacity)

    @staticmethod
    def _percentile(values: deque[float], amount: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(
            0, min(len(ordered) - 1, round((len(ordered) - 1) * amount))
        )
        return ordered[index]

    def snapshot(self, renderer: str) -> dict:
        return {
            "renderer": renderer,
            "input_p50_ms": self._percentile(self.input_ms, 0.50),
            "input_p95_ms": self._percentile(self.input_ms, 0.95),
            "input_p99_ms": self._percentile(self.input_ms, 0.99),
            "submit_p95_ms": self._percentile(self.submit_ms, 0.95),
            "frame_p95_ms": self._percentile(self.frame_ms, 0.95),
            "samples": len(self.input_ms),
        }


class _CanvasLogic:
    documentChanged = Signal(object)
    visualChanged = Signal(object)
    selectionChanged = Signal(str, str)
    selectionSetChanged = Signal(object)
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
    transformModeChanged = Signal(str)
    importStatusMessage = Signal(str)
    colorSampled = Signal(str)
    colorSampleCommitted = Signal(str)
    eyedropperGestureChanged = Signal(bool)

    def __init__(self, settings: EditorSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.chapter: ChapterDocument | None = None
        self.tiles = TileStore()
        self.images = ImageStore()
        # Runtime-only Blender preview dimensions/quads. These never enter the
        # chapter model or Undo history.
        self._image_runtime_geometry: dict[str, dict] = {}
        self.command_stack = CommandStack()
        self.tool = ToolKind.OBJECT_SELECT
        self.selected_kind = ""
        self.selected_id = ""
        self.active_page_id = ""
        self.active_layer_id = ""
        self.selected_object_id = ""
        self.selected_entities: list[tuple[str, str]] = []
        self.active_modifier_id = ""
        self.active_tone_mask_id = ""
        self.preview_tone_mask_id = ""
        self._mask_stroke_dirty = QRectF()
        self._mask_stroke_revision_before = 0
        self._mask_sample_queue: deque[tuple[QPointF, float, float]] = deque()
        self._mask_sample_timer = QTimer(self)
        self._mask_sample_timer.setSingleShot(True)
        self._mask_sample_timer.setInterval(0)
        self._mask_sample_timer.timeout.connect(self._flush_mask_samples)
        self._mask_runtime_revision = 0
        self._mask_has_painted_sample = False
        self._tone_mask_overlay_cache = QImage()
        self._tone_mask_overlay_key: tuple | None = None
        self._tone_mask_tile_cache: OrderedDict[tuple, QImage] = OrderedDict()
        self._tone_mask_contributor_cache: OrderedDict[
            tuple, np.ndarray
        ] = OrderedDict()
        self._tone_mask_contributor_cache_bytes = 0
        self._tone_mask_contributor_cache_budget = 64 * 1024 * 1024
        self._preserve_tone_mask_contributors_once = False
        self._modifier_handle_drag: dict | None = None
        self.center_x = 540.0
        self.center_y = 540.0
        self.scale = 0.6
        self.rotation = 0.0
        self._nav_mode: str | None = None
        self._nav_anchor = QPointF()
        self._nav_anchor_center = QPointF()
        self._nav_anchor_scale = 1.0
        self._nav_anchor_rotation = 0.0
        self._nav_anchor_document = QPointF()
        self._nav_pending_point: QPointF | None = None
        self._nav_frame_timer = QTimer(self)
        self._nav_frame_timer.setSingleShot(True)
        self._nav_frame_timer.timeout.connect(
            self._flush_navigation_update
        )
        self._wheel_zoom_timer = QTimer(self)
        self._wheel_zoom_timer.setSingleShot(True)
        self._wheel_zoom_timer.setInterval(WHEEL_ZOOM_SETTLE_MS)
        self._wheel_zoom_timer.timeout.connect(self._settle_wheel_zoom)
        self._drawing = False
        self._eyedropper_sampling = False
        self._eyedropper_last_color = ""
        self._eyedropper_widget_point: QPointF | None = None
        self._last_draw_point = QPointF()
        self._last_pressure = 1.0
        self._stroke_before: dict[tuple[int, int], QImage | None] = {}
        self._stroke_frame_before: tuple[float, float, float, float] | None = None
        self._stroke_erasing = False
        self._stroke_preset: BrushPreset | None = None
        self._stroke_base_size = float(settings.pencil_size())
        self._stroke_dirty_world = QRectF()
        self._gc_was_enabled = False
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
        self._text_transform_cache = QImage()
        self._transform_pivot: QPointF | None = None
        self._transform_pivot_custom = False
        self._transform_rotate_start = 0.0
        self._transform_gizmo_key: tuple | None = None
        self._transform_gizmo_slot: int | None = None
        self._render_excluded_object_id = ""
        self._render_modifier_sources: set[tuple[str, str]] = set()
        self._render_base_alpha = False
        self._interactive_render = False
        self._render_exclude_text = False
        self._rendering_mask_contributor = 0
        self._suppress_outline_for_mask = False
        self._suppress_selection_undo = False
        self._modifier_render_cache: OrderedDict[tuple, QImage] = OrderedDict()
        self._modifier_render_cache_bytes = 0
        self._modifier_render_cache_budget = 64 * 1024 * 1024
        self._modifier_source_cache: OrderedDict[tuple, QImage] = OrderedDict()
        self._modifier_source_cache_bytes = 0
        self._modifier_source_cache_budget = 64 * 1024 * 1024
        self._outline_distance_cache = OutlineDistanceCache()
        self._blur_pyramid_cache = BlurPyramidCache()
        self._rendering_compound_references = False
        self._rendering_outward_gradient = False
        self._live_underlay_object_id = ""
        self._live_underlay_amount = 0.0
        self._text_editing = False
        self._text_caret_visible = True
        self._text_caret_timer = QTimer(self)
        self._text_caret_timer.setInterval(500)
        self._text_caret_timer.timeout.connect(self._blink_text_caret)
        self._text_cursor_position = 0
        self._text_selection_anchor = 0
        self._text_property_drag: dict | None = None
        self._text_size_edit_before: dict | None = None
        self._text_size_edit_object_id = ""
        self._text_size_edit_canceled = False
        self._text_gizmo_overlay = _TextGizmoOverlay(self)
        self._text_gizmo_overlay.sizeDecreaseRequested.connect(
            lambda: self._change_selected_text_property(
                "font_size", -1, relative=True, label="Decrease text size"
            )
        )
        self._text_gizmo_overlay.sizeIncreaseRequested.connect(
            lambda: self._change_selected_text_property(
                "font_size", 1, relative=True, label="Increase text size"
            )
        )
        self._text_gizmo_overlay.boldRequested.connect(
            lambda: self._toggle_selected_text_property("bold", "Toggle bold")
        )
        self._text_gizmo_overlay.italicRequested.connect(
            lambda: self._toggle_selected_text_property("italic", "Toggle italic")
        )
        self._text_gizmo_overlay.sizeEditStarted.connect(
            self._begin_text_size_edit
        )
        self._text_gizmo_overlay.sizeEditCanceled.connect(
            self._cancel_text_size_edit
        )
        self._text_gizmo_overlay.sizeCommitted.connect(
            self._commit_text_size_edit
        )
        self._text_dragging = False
        self._last_text_double_click: tuple[float, QPointF, str] | None = None
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
        self._creation_parent_id = ""
        self._creation_insertion_index: int | None = None
        self._creation_compound_operation = "add"
        self._shape_property_drag: dict | None = None
        self._raster_creation_parent_id = ""
        self._raster_creation_index: int | None = None
        self._gradient_creation_parent_id = ""
        self._gradient_creation_type = ""
        self._gradient_creation_family = "color_fill"
        self._gradient_creation_before: dict | None = None
        self._gradient_tool_field_type = "line"
        self._selected_shape_node_id = ""
        self._selected_shape_node_ids: set[str] = set()
        self._shape_drag_nodes: dict[str, dict] = {}
        self._active_shape_control: str | None = None
        self._rectangle_roundness_linked = False
        self._input_press_modifiers = None
        self._active_gradient_control: tuple[str, str] | None = None
        self._geometry_transform_target: tuple[str, str] | None = None
        self._multi_transform_start_world_quads: dict[
            str, list[tuple[float, float]]
        ] = {}
        self._multi_transform_preview_quads: dict[
            str, list[tuple[float, float]]
        ] = {}
        self._shape_control_dragged = False
        self._shape_hover_insert: tuple[int, float, QPointF] | None = None
        self._shape_hover_target: dict | None = None
        self._pending_primitive_insert: (
            tuple[str, int, float, QPointF, QPointF] | None
        ) = None
        self._tablet_tool_active = False
        self._pen_contact_active = False
        self._device_supports_pressure = False
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
        self._windows_touch_configuration: tuple[int, bool] | None = None
        self._pending_raster_press: tuple[QPointF, QPointF, float] | None = None
        self._pending_vector_press: tuple[QPointF, QPointF, float] | None = None
        self._pending_drawing_selection_press: (
            tuple[QPointF, QPointF, float] | None
        ) = None
        self._preset = settings.active_brush_preset()
        self.primary_color = canonical_argb(settings.brush_color)
        self.secondary_color = "#FFFFFFFF"
        self.active_color_slot = "primary"
        self._predictive: tuple[QPointF, QPointF, float, QColor] | None = None
        self._compound_path_cache: dict[str, QPainterPath] = {}
        # Stroke images are deliberately cached independently.  A drawing can
        # contain thousands of strokes, so editing one must not evict every
        # unrelated image.  The tuple key is intentionally permissive because
        # preview revisions use a transient token.
        self._vector_render_cache: dict[
            tuple, tuple[QImage, QRectF]
        ] = {}
        self._vector_render_cache_bytes = 0
        self._vector_spatial_indexes: dict[str, dict] = {}
        self._vector_render_scale_override: float | None = None
        self._vector_render_scale_owner: str | None = None
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
        self._vector_preview_tiles = TileStore()
        self._vector_preview_id = "live-vector-preview"
        self._vector_preview_dirty = QRectF()
        self._promoted_vector_preview: dict | None = None
        self._vector_sweep: list[FreehandSample] = []
        self._vector_eraser_grid: dict[tuple[int, int], set[str]] = {}
        self._vector_eraser_bounds: dict[str, QRectF] = {}
        self._vector_eraser_grid_size = 64.0
        self._vector_eraser_grid_revision: tuple[str, int] | None = None
        self._vector_eraser_preview: dict[str, list[VectorStroke]] = {}
        self._vector_eraser_preview_revision = 0
        self._vector_eraser_preview_versions: dict[str, int] = {}
        self._vector_eraser_background_cache = QImage()
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
        self._hover_vector_point_id = ""
        self._drawing_selection_path = QPainterPath()
        self._drawing_selection_gesture: list[QPointF] = []
        self._drawing_selection_operation = "replace"
        self._drawing_selection_shift_anchor: QPointF | None = None
        self._drawing_selection_shift_active: bool = False
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
        self._fill_before: dict[tuple[int, int], QImage | None] = {}
        self._fill_dirty_world = QRectF()
        self._fill_gesture_active = False
        self._fill_gesture_points: list[QPointF] = []
        self._fill_last_world = QPointF()
        self._fill_reference_tile_cache: OrderedDict[tuple, QImage] = OrderedDict()
        self._fill_reference_tile_cache_bytes = 0
        self._fill_reference_tile_cache_budget = 64 * 1024 * 1024
        self._preserve_fill_reference_cache = False
        self._fill_job_generation = 0
        self._fill_job_cancel: threading.Event | None = None
        self._fill_workers: set[QRunnable] = set()
        self._fill_job_error: Exception | None = None
        self._fill_replay_state: _FillReplayState | None = None
        self._fill_replay_generation = 0
        self._fill_replay_cancel: threading.Event | None = None
        self._fill_replay_pending_tolerance: int | None = None
        self._fill_replay_timer = QTimer(self)
        self._fill_replay_timer.setSingleShot(True)
        self._fill_replay_timer.timeout.connect(
            self._recalculate_last_fill_tolerance
        )
        self._fill_operation_base_tiles: dict[
            tuple[int, int], QImage
        ] = {}
        self._fill_operation_profile: dict[str, object] = {}
        self._fill_operation_color = QColor()
        self._fill_operation_selection = QPainterPath()
        self._fill_operation_reference_tiles: dict[
            tuple[int, int], QImage
        ] = {}
        self._page_creation_anchor_id = ""
        self._page_creation_before: dict | None = None
        self._page_creation_kind = ""
        self._page_creation_draft: BoundGeometry | None = None
        self._page_creation_committing = False
        self._page_creation_gap_bounds: tuple[float, float] | None = None
        self._page_creation_base_height = 0
        self._gradient_creation_parent_id = ""
        self._gradient_creation_type = ""
        self._gradient_creation_family = "color_fill"
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
        self.asset_repository: AssetRepository | None = None
        self._asset_drag_manifest: AssetManifest | None = None
        self._asset_drag_tiles: TileStore | None = None
        self._asset_drag_images: ImageStore | None = None
        self._external_drag_sources: list[tuple[str, str, bytes]] = []
        self._external_drag_entries: list[dict] = []
        self._external_drag_generation = 0
        self._external_drag_replies: dict[QNetworkReply, tuple[int, dict]] = {}
        self._pending_external_drop: dict | None = None
        self._external_drag_widget = QPointF()
        self._network_manager = QNetworkAccessManager(self)
        self._asset_drag_image = QImage()
        self._asset_drag_world = QPointF()
        self._asset_drag_parent_id = ""
        self._asset_drag_valid = False
        self._asset_drag_clip_cache: dict[str, QPainterPath | None] = {}
        self._scene_cache = QImage()
        self._scene_cache_key: tuple | None = None
        self._scene_dirty_full = True
        self._scene_dirty_widget = QRect()
        self._preserve_scene_cache_once = False
        self._visual_pending_world = QRectF()
        self._visual_pending_widget = QRect()
        self._visual_frame_timer = QTimer(self)
        self._visual_frame_timer.setSingleShot(True)
        self._visual_frame_timer.timeout.connect(self._flush_visual_dirty)
        self._performance = CanvasPerformanceMonitor()
        self.documentChanged.connect(self._clear_compound_path_cache)
        self.documentChanged.connect(self._document_visual_changed)
        self.documentChanged.connect(self._invalidate_fill_reference_cache)
        self.hierarchyChanged.connect(self._clear_compound_path_cache)
        self.hierarchyChanged.connect(self._invalidate_scene_cache)
        self.setMinimumSize(480, 480)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_AcceptTouchEvents, True)
        self.setAttribute(Qt.WA_TabletTracking, True)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.configure_tablet_navigation()

    def configure_tablet_navigation(self) -> bool:
        """Apply native touch delivery policy for the current tablet mode."""
        top_level = self.window()
        hwnd = int(top_level.winId())
        configuration = (hwnd, bool(self.settings.tablet_mode))
        if configuration == self._windows_touch_configuration:
            return True
        configured = configure_simultaneous_pen_touch(*configuration)
        if configured:
            self._windows_touch_configuration = configuration
        return configured

    # ---- document and commands -----------------------------------------
    def _clear_creation_gesture(self) -> None:
        """Discard an incomplete creation gesture without committing it."""
        self._creation_points.clear()
        self._creation_nodes.clear()
        self._creation_selected_node_id = ""
        self._creation_active_control = None
        self._creation_close_candidate = False
        self._creation_node_dragged = False
        self._creation_style = None
        self._creation_parent_id = ""
        self._creation_insertion_index = None
        self._creation_compound_operation = "add"
        self._shape_property_drag = None
        self._raster_creation_parent_id = ""
        self._raster_creation_index = None
        self._pending_primitive_insert = None
        self._shape_hover_target = None
        self._shape_hover_insert = None
        self.setToolTip("")

    def performance_snapshot(self) -> dict:
        renderer = "gpu" if isinstance(self, QOpenGLWidget) else "raster"
        return self._performance.snapshot(renderer)

    def _suspend_gc_for_stroke(self) -> None:
        if self._gc_was_enabled:
            return
        self._gc_was_enabled = gc.isenabled()
        if self._gc_was_enabled:
            gc.disable()

    def _restore_gc_after_stroke(self) -> None:
        if self._gc_was_enabled and not gc.isenabled():
            gc.enable()
        self._gc_was_enabled = False

    def _abort_raster_stroke_after_error(self) -> None:
        """Restore the pre-stroke tiles and leave no drawing state behind."""
        object_id = self.selected_id
        for key, image in self._stroke_before.items():
            self.tiles.set_tile(object_id, key, image)
        self._stroke_before = {}
        self._stroke_frame_before = None
        self._stroke_erasing = False
        self._stroke_dirty_world = QRectF()
        self._drawing = False
        self._stroke_preset = None
        self._predictive = None
        self._restore_gc_after_stroke()

    def _invalidate_scene_cache(self, *args) -> None:
        self._scene_dirty_full = True
        self._scene_dirty_widget = QRect()
        self._scene_cache_key = None

    def _invalidate_tone_mask_overlay(self, *, contributors: bool = True) -> None:
        if contributors:
            self._tone_mask_overlay_cache = QImage()
            self._tone_mask_overlay_key = None
            self._tone_mask_contributor_cache.clear()
            self._tone_mask_contributor_cache_bytes = 0
        self._tone_mask_tile_cache.clear()

    def _invalidate_fill_reference_cache(self, *args) -> None:
        del args
        if self._preserve_fill_reference_cache:
            return
        self._fill_reference_tile_cache.clear()
        self._fill_reference_tile_cache_bytes = 0

    def _world_dirty_to_widget(self, world: QRectF) -> QRect:
        if world.isEmpty():
            return QRect()
        mapped = self.camera_transform().mapRect(world).adjusted(-4, -4, 4, 4)
        return mapped.toAlignedRect().intersected(self.rect())

    def _mark_scene_dirty_world(self, world: QRectF) -> QRect:
        widget = self._world_dirty_to_widget(world)
        if widget.isEmpty():
            return widget
        self._scene_dirty_widget = (
            QRect(widget) if self._scene_dirty_widget.isEmpty()
            else self._scene_dirty_widget.united(widget)
        )
        return widget

    def _document_visual_changed(self, world_rect) -> None:
        if self._preserve_tone_mask_contributors_once:
            self._preserve_tone_mask_contributors_once = False
            self._invalidate_tone_mask_overlay(contributors=False)
        elif not self._drawing or not self.active_tone_mask_id:
            self._invalidate_tone_mask_overlay()
        if self._preserve_scene_cache_once:
            self._preserve_scene_cache_once = False
            return
        if (
            world_rect is None
            or not hasattr(world_rect, "isEmpty")
            or world_rect.isEmpty()
        ):
            self._invalidate_scene_cache()
            self.update()
            return
        widget = self._mark_scene_dirty_world(QRectF(world_rect))
        if not widget.isEmpty():
            self.update(widget)

    def _queue_visual_dirty(
        self, world: QRectF, *, scene: bool = True,
        notify_preview: bool = True,
    ) -> None:
        if world.isEmpty():
            return
        widget = (
            self._mark_scene_dirty_world(world)
            if scene else self._world_dirty_to_widget(world)
        )
        if widget.isEmpty():
            return
        if notify_preview:
            self._visual_pending_world = (
                QRectF(world) if self._visual_pending_world.isEmpty()
                else self._visual_pending_world.united(world)
            )
        self._visual_pending_widget = (
            QRect(widget) if self._visual_pending_widget.isEmpty()
            else self._visual_pending_widget.united(widget)
        )
        if not self._visual_frame_timer.isActive():
            self._visual_frame_timer.start(0)

    def _object_has_effect_modifiers(self, object_id: str) -> bool:
        if self.chapter is None:
            return False
        obj = self.chapter.objects.get(object_id)
        return bool(
            obj is not None and (
                self._object_is_mask_contributor(object_id)
                or obj.modifier_ids or obj.opacity_mask is not None
                or any(
                    layer.modifier_ids or layer.opacity_mask is not None
                    for layer in self.chapter.ancestor_layers(
                        obj.parent_layer_id
                    )
                )
            )
        )

    def _object_is_mask_contributor(self, object_id: str) -> bool:
        obj = self.chapter.objects.get(object_id) if self.chapter else None
        ancestors = {
            ("layer", layer.layer_id)
            for layer in self.chapter.ancestor_layers(obj.parent_layer_id)
        } if obj is not None else set()
        return bool(
            self.chapter is not None
            and any(
                ("object", object_id) in mask.contributors
                or bool(ancestors.intersection(mask.contributors))
                for mask in self.chapter.masks.values()
            )
        )

    def modifier_expanded_dirty(
        self, object_id: str, world: QRectF,
    ) -> QRectF:
        """Expand source dirtiness by every applicable blur footprint."""
        if self.chapter is None or world.isEmpty():
            return QRectF(world)
        obj = self.chapter.objects.get(object_id)
        if obj is None:
            return QRectF(world)
        if self._object_is_mask_contributor(object_id):
            return QRectF(
                0, 0, self.chapter.width, self.chapter.height
            )
        modifier_ids = list(obj.modifier_ids)
        for layer in self.chapter.ancestor_layers(obj.parent_layer_id):
            modifier_ids.extend(layer.modifier_ids)
        padding = max((
            self._modifier_maximum(
                modifier, "strength", modifier.strength
            ) * 3.0
            if isinstance(modifier, BlurModifier)
            else self._modifier_maximum(
                modifier, "thickness", modifier.thickness
            )
            if isinstance(modifier, OutlineModifier)
            else 0.0
            for modifier_id in modifier_ids
            if (modifier := self.chapter.modifiers.get(modifier_id)) is not None
            and modifier.intensity > 0
        ), default=0.0)
        return QRectF(world).adjusted(-padding, -padding, padding, padding)

    def _entity_expanded_dirty(
        self, kind: str, entity_id: str, world: QRectF,
    ) -> QRectF:
        """Conservatively include effects and mask dependants for a subtree."""
        if self.chapter is None or world.isEmpty():
            return QRectF(world)
        targets: list[LayerNode | DocumentObject] = []

        def collect_layer(layer_id: str) -> None:
            layer = self.chapter.layers.get(layer_id)
            if layer is None:
                return
            targets.append(layer)
            for child in layer.children:
                if child.kind == "layer":
                    collect_layer(child.entity_id)
                else:
                    obj = self.chapter.objects.get(child.entity_id)
                    if obj is not None:
                        targets.append(obj)

        if kind == "layer":
            collect_layer(entity_id)
        else:
            obj = self.chapter.objects.get(entity_id)
            if obj is not None:
                targets.append(obj)
        target_keys = {
            (
                "layer" if isinstance(target, LayerNode) else "object",
                target.layer_id if isinstance(target, LayerNode)
                else target.object_id,
            )
            for target in targets
        }
        if any(
            target_keys.intersection(mask.contributors)
            for mask in self.chapter.masks.values()
        ):
            return QRectF(0, 0, self.chapter.width, self.chapter.height)
        modifier_ids: list[str] = []
        for target in targets:
            modifier_ids.extend(target.modifier_ids)
        parent_id = (
            self.chapter.layers[entity_id].parent_id
            if kind == "layer" and entity_id in self.chapter.layers
            else self.chapter.objects[entity_id].parent_layer_id
            if kind == "object" and entity_id in self.chapter.objects
            else None
        )
        while parent_id:
            parent = self.chapter.layers[parent_id]
            modifier_ids.extend(parent.modifier_ids)
            parent_id = parent.parent_id
        padding = max((
            self._modifier_maximum(
                modifier, "strength", modifier.strength
            ) * 3.0
            if isinstance(modifier, BlurModifier)
            else self._modifier_maximum(
                modifier, "thickness", modifier.thickness
            )
            if isinstance(modifier, OutlineModifier)
            else 0.0
            for modifier_id in modifier_ids
            if (modifier := self.chapter.modifiers.get(modifier_id)) is not None
            and modifier.intensity > 0
        ), default=0.0)
        return QRectF(world).adjusted(-padding, -padding, padding, padding)

    def _flush_visual_dirty(self) -> None:
        world = QRectF(self._visual_pending_world)
        widget = QRect(self._visual_pending_widget)
        self._visual_pending_world = QRectF()
        self._visual_pending_widget = QRect()
        if not world.isEmpty():
            self.visualChanged.emit(world)
        if not widget.isEmpty():
            self.update(widget)

    def _scene_key(self) -> tuple:
        return (
            id(self.chapter), self.width(), self.height(),
            round(float(self.devicePixelRatioF()), 4),
            round(self.center_x, 6), round(self.center_y, 6),
            round(self.scale, 8), round(self.rotation, 6),
        )

    def _ensure_scene_cache(self) -> None:
        if self.chapter is None or self.width() <= 0 or self.height() <= 0:
            return
        key = self._scene_key()
        ratio = max(1.0, float(self.devicePixelRatioF()))
        pixel_width = max(1, round(self.width() * ratio))
        pixel_height = max(1, round(self.height() * ratio))
        size_changed = (
            self._scene_cache.isNull()
            or self._scene_cache.width() != pixel_width
            or self._scene_cache.height() != pixel_height
        )
        if size_changed:
            self._scene_cache = QImage(
                pixel_width, pixel_height,
                QImage.Format_ARGB32_Premultiplied,
            )
            self._scene_cache.setDevicePixelRatio(ratio)
        if size_changed or self._scene_cache_key != key:
            self._scene_cache_key = key
            self._scene_dirty_full = True
            self._scene_dirty_widget = QRect()
        dirty = (
            QRect(self.rect()) if self._scene_dirty_full
            else QRect(self._scene_dirty_widget)
        )
        if dirty.isEmpty():
            return
        self._render_scene_cache_rect(dirty)
        self._scene_dirty_full = False
        self._scene_dirty_widget = QRect()

    def _render_scene_cache_rect(self, dirty: QRect) -> None:
        painter = QPainter(self._scene_cache)
        painter.setClipRect(dirty)
        painter.fillRect(dirty, QColor("#242428"))
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setTransform(self.camera_transform())
        painter.fillRect(
            QRectF(0, 0, self.chapter.width, self.chapter.height),
            QColor(self.chapter.background),
        )
        painter.save()
        painter.setClipRect(
            QRectF(0, 0, self.chapter.width, self.chapter.height),
            Qt.IntersectClip,
        )
        inverse, valid = self.camera_transform().inverted()
        visible = (
            inverse.map(QPolygonF(QRectF(dirty))).boundingRect()
            if valid else self.visible_document_rect()
        )
        self._set_live_underlay_context()
        previous_excluded = self._render_excluded_object_id
        previous_interactive = self._interactive_render
        self._interactive_render = True
        if self._text_editing:
            selected = self.chapter.objects.get(self.selected_object_id)
            if isinstance(selected, TextObject):
                self._render_excluded_object_id = selected.object_id
        try:
            for page_id in reversed(self.chapter.root_page_ids):
                self._render_layer(
                    painter, self.chapter.layers[page_id], 1.0, visible
                )
        finally:
            self._render_excluded_object_id = previous_excluded
            self._interactive_render = previous_interactive
        self._render_selected_drawing_underlay(painter, visible)
        self._clear_live_underlay_context()
        self._draw_grid(painter, visible)
        painter.restore()
        painter.setTransform(QTransform())
        painter.setPen(QPen(QColor("#44444d"), 1))
        painter.drawPolygon(self.camera_transform().map(QPolygonF(QRectF(
            0, 0, self.chapter.width, self.chapter.height
        ))))
        painter.end()

    def _clear_detached_input_state(self) -> None:
        """Reset transient pointer state that cannot survive without a document."""
        self._clear_creation_gesture()
        self._pending_raster_press = None
        self._pending_vector_press = None
        self._pending_raster_transform_press = None
        self._pending_drawing_selection_press = None
        self._outside_click_candidate = False
        self._tablet_tool_active = False
        self._pen_contact_active = False
        self._nav_mode = None
        self._nav_frame_timer.stop()
        self._nav_pending_point = None
        self._wheel_zoom_timer.stop()
        self._vector_render_scale_override = None
        self._vector_render_scale_owner = None
        self._transform_gizmo_key = None
        self._transform_gizmo_slot = None
        self._pointer_hover_widget = None
        self._tablet_hover_widget = None
        self._touch_frame_timer.stop()
        self._touch_pending_points = None
        self._touch_points.clear()
        self._touch_anchor_points.clear()
        self._drawing = False
        self._stroke_preset = None
        self._stroke_dirty_world = QRectF()
        self._stroke_before.clear()
        self._vector_preview_tiles = TileStore()
        self._vector_preview_dirty = QRectF()
        self._vector_eraser_grid.clear()
        self._vector_eraser_bounds.clear()
        self._vector_eraser_preview.clear()
        self._vector_eraser_preview_versions.clear()
        self._vector_eraser_background_cache = QImage()
        self._restore_gc_after_stroke()
        self._page_creation_anchor_id = ""
        self._page_creation_before = None
        self._page_creation_kind = ""
        self._page_creation_draft = None
        self._page_creation_committing = False
        self._page_creation_gap_bounds = None
        self._page_creation_base_height = 0
        self._gradient_creation_parent_id = ""
        self._gradient_creation_type = ""
        self._gradient_creation_family = "color_fill"
        self._gradient_creation_before = None
        self.unsetCursor()

    def set_document(
        self, chapter: ChapterDocument, tiles: TileStore,
        images: ImageStore | None = None, reset_view: bool = True,
    ) -> None:
        self._cancel_fill_job()
        self._clear_fill_replay()
        self._clear_detached_input_state()
        self.chapter = chapter
        self.tiles = tiles
        self.images = images or ImageStore()
        self._image_runtime_geometry.clear()
        self._compound_path_cache.clear()
        self._gradient_geometry_cache.clear()
        self._gradient_scalar_cache.clear()
        self._gradient_render_cache.clear()
        self._modifier_render_cache.clear()
        self._modifier_render_cache_bytes = 0
        self._modifier_source_cache.clear()
        self._modifier_source_cache_bytes = 0
        self._outline_distance_cache.clear()
        self._blur_pyramid_cache.clear()
        self._invalidate_tone_mask_overlay()
        self._promoted_vector_preview = None
        self._invalidate_scene_cache()
        self._ensure_raster_frames()
        self.command_stack.clear()
        self.selected_kind = ""
        self.selected_id = ""
        self.active_page_id = ""
        self.active_layer_id = ""
        self.selected_object_id = ""
        self.selected_entities = []
        self._selected_vector_stroke_ids.clear()
        self._selected_vector_point_ids.clear()
        self._clear_vector_render_cache()
        self._vector_spatial_indexes.clear()
        self._pending_raster_transform_press = None
        self._gradient_render_cache.clear()
        self._gradient_preview_active = False
        self._touch_frame_timer.stop()
        self._touch_pending_points = None
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

    def capture_session_state(self) -> CanvasSessionState | None:
        """Detach the committed document state without discarding warm caches."""
        if self.chapter is None:
            return None
        self._commit_text_edit()
        if self._vector_gesture_mode is not None or self._vector_before:
            self._cancel_vector_gesture(restore=True)
        if self._page_creation_anchor_id:
            self._cancel_page_creation()
        if self._gradient_creation_parent_id:
            self._cancel_gradient_creation()
        self._clear_creation_gesture()
        self._clear_transform_preview()
        self._clear_asset_drag_preview()
        return CanvasSessionState(
            chapter=self.chapter, tiles=self.tiles, images=self.images,
            command_stack=self.command_stack, tool=self.tool,
            selected_kind=self.selected_kind, selected_id=self.selected_id,
            active_page_id=self.active_page_id,
            active_layer_id=self.active_layer_id,
            selected_object_id=self.selected_object_id,
            selected_entities=list(self.selected_entities),
            center_x=self.center_x, center_y=self.center_y,
            scale=self.scale, rotation=self.rotation,
            compound_cache=self._compound_path_cache,
            vector_cache=self._vector_render_cache,
            vector_cache_bytes=self._vector_render_cache_bytes,
            vector_spatial_indexes=self._vector_spatial_indexes,
            gradient_geometry_cache=self._gradient_geometry_cache,
            gradient_scalar_cache=self._gradient_scalar_cache,
            gradient_render_cache=self._gradient_render_cache,
            modifier_render_cache=self._modifier_render_cache,
            modifier_render_cache_bytes=self._modifier_render_cache_bytes,
            modifier_source_cache=self._modifier_source_cache,
            modifier_source_cache_bytes=self._modifier_source_cache_bytes,
            outline_distance_cache=self._outline_distance_cache,
            blur_pyramid_cache=self._blur_pyramid_cache,
        )

    def restore_session_state(self, state: CanvasSessionState) -> None:
        """Activate a previously captured tab without loading it again."""
        self._clear_detached_input_state()
        self.chapter, self.tiles, self.images = (
            state.chapter, state.tiles, state.images
        )
        self._image_runtime_geometry.clear()
        self.command_stack = state.command_stack
        self.tool = state.tool
        self.selected_kind, self.selected_id = state.selected_kind, state.selected_id
        self.active_page_id, self.active_layer_id = (
            state.active_page_id, state.active_layer_id
        )
        self.selected_object_id = state.selected_object_id
        self.selected_entities = list(state.selected_entities)
        self.center_x, self.center_y = state.center_x, state.center_y
        self.scale, self.rotation = state.scale, state.rotation
        self._compound_path_cache = state.compound_cache
        self._vector_render_cache = state.vector_cache
        self._vector_render_cache_bytes = state.vector_cache_bytes
        self._vector_spatial_indexes = state.vector_spatial_indexes
        self._gradient_geometry_cache = state.gradient_geometry_cache
        self._gradient_scalar_cache = state.gradient_scalar_cache
        self._gradient_render_cache = state.gradient_render_cache
        self._modifier_render_cache = state.modifier_render_cache
        self._modifier_render_cache_bytes = state.modifier_render_cache_bytes
        self._modifier_source_cache = state.modifier_source_cache
        self._modifier_source_cache_bytes = state.modifier_source_cache_bytes
        self._outline_distance_cache = state.outline_distance_cache
        self._blur_pyramid_cache = state.blur_pyramid_cache
        self._invalidate_tone_mask_overlay()
        self._promoted_vector_preview = None
        self._invalidate_scene_cache()
        self._touch_frame_timer.stop()
        self._touch_pending_points = None
        self._clear_asset_drag_preview()
        self.update()
        self.hierarchyChanged.emit()
        self.selectionChanged.emit(self.selected_kind, self.selected_id)
        self.selectionSetChanged.emit(list(self.selected_entities))
        self.toolChanged.emit(self.tool)

    def clear_document(self) -> None:
        if self.chapter is not None and self._page_creation_anchor_id:
            self._cancel_page_creation()
        if self._gradient_creation_parent_id:
            self._cancel_gradient_creation()
        self._clear_detached_input_state()
        self.chapter = None
        self.tiles = TileStore()
        self.images = ImageStore()
        self._image_runtime_geometry.clear()
        self.command_stack = CommandStack()
        self.selected_kind = self.selected_id = ""
        self.active_page_id = self.active_layer_id = ""
        self.selected_object_id = ""
        self.selected_entities = []
        self._compound_path_cache.clear()
        self._clear_vector_render_cache()
        self._vector_spatial_indexes.clear()
        self._gradient_geometry_cache.clear()
        self._gradient_scalar_cache.clear()
        self._gradient_render_cache.clear()
        self._modifier_render_cache.clear()
        self._modifier_render_cache_bytes = 0
        self._modifier_source_cache.clear()
        self._modifier_source_cache_bytes = 0
        self._outline_distance_cache.clear()
        self._blur_pyramid_cache.clear()
        self._invalidate_tone_mask_overlay()
        self._promoted_vector_preview = None
        self._invalidate_scene_cache()
        self._clear_asset_drag_preview()
        self.update()

    def set_active_colors(self, primary: str, secondary: str) -> None:
        """Set the per-series colors used by contextual drawing tools."""
        self.primary_color = canonical_argb(primary)
        self.secondary_color = canonical_argb(secondary, "#FFFFFFFF")
        self.settings.brush_color = self.primary_color
        self.update()

    def set_active_color_slot(self, slot: str) -> None:
        if slot not in {"primary", "secondary"}:
            raise ValueError("Color slot must be 'primary' or 'secondary'")
        self.active_color_slot = slot

    def replace_chapter(self, state: dict) -> None:
        self._commit_text_edit()
        self._clear_transform_preview()
        self._page_gap_transaction = None
        self._clear_page_gap_editor()
        self.pageGapConfirmationChanged.emit(False)
        self.chapter = ChapterDocument.from_dict(state)
        self._image_runtime_geometry.clear()
        self._compound_path_cache.clear()
        self._gradient_geometry_cache.clear()
        self._gradient_scalar_cache.clear()
        self._gradient_render_cache.clear()
        self._modifier_render_cache.clear()
        self._modifier_render_cache_bytes = 0
        self._modifier_source_cache.clear()
        self._modifier_source_cache_bytes = 0
        self._outline_distance_cache.clear()
        self._blur_pyramid_cache.clear()
        self._invalidate_tone_mask_overlay()
        self._promoted_vector_preview = None
        self._invalidate_scene_cache()
        valid = (
            self.selected_id in self.chapter.layers
            if self.selected_kind == "layer"
            else self.selected_id in self.chapter.objects
        )
        if not valid:
            self.selected_kind = ""
            self.selected_id = ""
            self.selected_object_id = ""
            self.selected_entities = []
        else:
            restored: list[tuple[str, str]] = []
            for kind, entity_id in self.selected_entities:
                if kind != "object":
                    continue
                obj = self.chapter.objects.get(entity_id)
                if isinstance(obj, (RasterObject, VectorDrawingObject)):
                    restored.append((kind, entity_id))
            if len(restored) < 2:
                restored = [(self.selected_kind, self.selected_id)]
            self.selected_entities = restored
        self._sync_selection_levels()
        self.chapterReplaced.emit(self.chapter)
        self.hierarchyChanged.emit()
        self.selectionChanged.emit(self.selected_kind, self.selected_id)
        self.selectionSetChanged.emit(list(self.selected_entities))
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
        return None

    def _drawing_object_transform(
        self,
        obj: RasterObject | VectorDrawingObject,
        destination: list[tuple[float, float]] | None = None,
    ) -> QTransform:
        target = obj.transform_quad if destination is None else destination
        if target is None:
            return QTransform()
        return self._quad_transform(
            QRectF(*self._object_transform_frame(obj)), list(target)
        )

    def _drawing_local_visible_rect(
        self,
        obj: RasterObject | VectorDrawingObject,
        parent_visible: QRectF,
        destination: list[tuple[float, float]] | None = None,
    ) -> QRectF | None:
        target = obj.transform_quad if destination is None else destination
        visible = QRectF(parent_visible)
        if target is not None:
            inverse, valid = self._drawing_object_transform(
                obj, target
            ).inverted()
            if not valid:
                return None
            visible = inverse.mapRect(visible)
        return visible.translated(-obj.x, -obj.y)

    def _layer_parent_transform(self, layer: LayerNode) -> QTransform:
        if (
            self._geometry_transform_target == ("layer_group", layer.layer_id)
            and self._transform_preview_quad is not None
            and layer.bound is not None
        ):
            left, top, width, height = layer.bound.bbox()
            return self._quad_transform(
                QRectF(left, top, max(1.0, width), max(1.0, height)),
                list(self._transform_preview_quad),
            )
        if layer.transform_frame is not None and layer.transform_quad is not None:
            return self._quad_transform(
                QRectF(*layer.transform_frame), list(layer.transform_quad)
            )
        transform = QTransform()
        transform.translate(layer.translate_x, layer.translate_y)
        return transform

    def layer_world_transform(self, layer_id: str) -> QTransform:
        transform = QTransform()
        for layer in self.chapter.ancestor_layers(layer_id):
            transform = self._layer_parent_transform(layer) * transform
        return transform

    def _document_layer_world_transform(
        self, document: ChapterDocument, layer_id: str,
    ) -> QTransform:
        """Resolve a layer's complete local-to-document mapping."""
        transform = QTransform()
        for layer in document.ancestor_layers(layer_id):
            if document is self.chapter:
                local = self._layer_parent_transform(layer)
            elif (
                layer.transform_frame is not None
                and layer.transform_quad is not None
            ):
                local = self._quad_transform(
                    QRectF(*layer.transform_frame),
                    list(layer.transform_quad),
                )
            else:
                local = QTransform()
                local.translate(layer.translate_x, layer.translate_y)
            transform = local * transform
        return transform

    def _layer_world_to_local(
        self, layer_id: str, point: QPointF,
    ) -> QPointF:
        inverse, valid = self.layer_world_transform(layer_id).inverted()
        return inverse.map(point) if valid else QPointF(point)

    def _drawing_local_rect_to_world(
        self,
        obj: RasterObject | VectorDrawingObject,
        local_rect: QRectF,
    ) -> QRectF:
        if local_rect.isEmpty():
            return QRectF()
        parent_polygon = QPolygonF([
            local_rect.topLeft() + QPointF(obj.x, obj.y),
            local_rect.topRight() + QPointF(obj.x, obj.y),
            local_rect.bottomRight() + QPointF(obj.x, obj.y),
            local_rect.bottomLeft() + QPointF(obj.x, obj.y),
        ])
        if obj.transform_quad is not None:
            parent_polygon = self._drawing_object_transform(obj).map(
                parent_polygon
            )
        return self.layer_world_transform(obj.parent_layer_id).map(
            parent_polygon
        ).boundingRect()

    def _vector_local_point(
        self, drawing: VectorDrawingObject, world: QPointF,
    ) -> QPointF:
        parent_local = self._layer_world_to_local(
            drawing.parent_layer_id, world
        )
        if drawing.transform_quad is not None:
            inverse, valid = self._drawing_object_transform(
                drawing
            ).inverted()
            if valid:
                parent_local = inverse.map(parent_local)
        return QPointF(
            parent_local.x() - drawing.x,
            parent_local.y() - drawing.y,
        )

    def _raster_local_point(
        self, obj: RasterObject, world: QPointF,
    ) -> QPointF:
        parent_local = self._layer_world_to_local(obj.parent_layer_id, world)
        if obj.transform_quad is not None:
            inverse, valid = self._drawing_object_transform(obj).inverted()
            if valid:
                parent_local = inverse.map(parent_local)
        return QPointF(parent_local.x() - obj.x, parent_local.y() - obj.y)

    def _raster_world_point(
        self, obj: RasterObject, local: QPointF,
    ) -> QPointF:
        parent_local = QPointF(local.x() + obj.x, local.y() + obj.y)
        if obj.transform_quad is not None:
            parent_local = self._drawing_object_transform(obj).map(
                parent_local
            )
        return self.layer_world_transform(obj.parent_layer_id).map(parent_local)

    def _vector_world_point(
        self, obj: VectorDrawingObject, local: QPointF,
    ) -> QPointF:
        parent_local = QPointF(local.x() + obj.x, local.y() + obj.y)
        if obj.transform_quad is not None:
            parent_local = self._drawing_object_transform(obj).map(parent_local)
        return self.layer_world_transform(obj.parent_layer_id).map(parent_local)

    def _drawing_local_to_world_transform(
        self, obj: RasterObject | VectorDrawingObject,
    ) -> QTransform:
        """Return the projective drawing-local to document transform."""
        frame = QRectF(*self._object_transform_frame(obj)).translated(
            -obj.x, -obj.y
        )
        if frame.width() <= 0 or frame.height() <= 0:
            return QTransform()
        destination: list[tuple[float, float]] = []
        for point in self._rect_quad(frame):
            mapped = (
                self._raster_world_point(obj, QPointF(*point))
                if isinstance(obj, RasterObject)
                else self._vector_world_point(obj, QPointF(*point))
            )
            destination.append(mapped.toTuple())
        return self._quad_transform(frame, destination)

    def _capture_vector_graph(
        self, drawing: VectorDrawingObject,
    ) -> dict[str, dict | None]:
        identifiers = [drawing.object_id]
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
            self._clear_vector_render_cache()
            self._vector_spatial_indexes.clear()
        elif changed_stroke_ids:
            self._invalidate_vector_cache_strokes(changed_stroke_ids)
            drawing = self._active_vector_drawing()
            if drawing is not None:
                self._vector_spatial_indexes.pop(drawing.object_id, None)
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
                identifiers.add(drawing_id)
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
        self._vector_preview_tiles = TileStore()
        self._vector_preview_dirty = QRectF()
        self._vector_sweep.clear()
        self._vector_eraser_grid.clear()
        self._vector_eraser_bounds.clear()
        self._vector_eraser_grid_revision = None
        self._vector_eraser_preview.clear()
        self._clear_vector_eraser_live_cache()
        self._vector_simplify_point_ids.clear()
        self._vector_simplify_anchor_grid.clear()
        self._vector_simplify_last_sample = None
        self._vector_simplify_overlay.clear()
        self._vector_before = None
        self._vector_drag_points.clear()
        self._vector_connect_endpoints.clear()
        self._drawing = False
        self._stroke_preset = None
        self._restore_gc_after_stroke()
        self._vector_changed()

    def set_selection(
        self, kind: str, entity_id: str, activate_default_tool: bool = True,
    ) -> None:
        if self.chapter is None:
            return
        before = None if self._suppress_selection_undo else self._selection_snapshot()
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
            self._clear_fill_replay()
            if self._vector_gesture_mode is not None:
                self._cancel_vector_gesture(restore=True)
            self._cancel_text_property_drag()
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
        self.selected_entities = [(kind, entity_id)]
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
            elif activate_default_tool and isinstance(obj, GradientObject):
                self.tool = ToolKind.GRADIENT
            elif activate_default_tool and isinstance(obj, TextObject):
                self.tool = ToolKind.TEXT_EDIT
            elif activate_default_tool and isinstance(obj, ImageObject):
                self.tool = ToolKind.OBJECT_SELECT
        else:
            self.selected_object_id = ""
            self.active_layer_id = entity_id
            self.active_page_id = self.chapter.page_for_layer(entity_id).layer_id
            layer = self.chapter.layers[entity_id]
            if (
                activate_default_tool
                and layer.bound is not None
            ):
                self.tool = ToolKind.SHAPE_EDIT
        if self.tool != previous_tool:
            self.toolChanged.emit(self.tool)
        self.selectionChanged.emit(kind, entity_id)
        self.selectionSetChanged.emit(list(self.selected_entities))
        self._invalidate_scene_cache()
        self.update()
        if before is not None:
            after = self._selection_snapshot()
            if before["kind"] != after["kind"] or before["id"] != after["id"] or before["entities"] != after["entities"] or before["path"] != after["path"]:
                self._push_selection_undo(before, after)

    def set_selection_set(
        self, entities: Iterable[tuple[str, str]],
        primary: tuple[str, str] | None = None,
    ) -> bool:
        """Select an outliner-authored raster/vector object set."""
        if self.chapter is None:
            return False
        before = None if self._suppress_selection_undo else self._selection_snapshot()
        ordered: list[tuple[str, str]] = []
        for kind, entity_id in entities:
            key = (str(kind), str(entity_id))
            if key not in ordered:
                ordered.append(key)
        if not ordered:
            self.clear_selection()
            return True
        if len(ordered) == 1:
            self.set_selection(*ordered[0], activate_default_tool=True)
            return True
        filtered: list[tuple[str, str]] = []
        for kind, entity_id in ordered:
            if kind == "layer":
                layer = self.chapter.layers.get(entity_id)
                if layer is None or layer.is_page:
                    return False
                filtered.append((kind, entity_id))
            elif kind == "object":
                if entity_id not in self.chapter.objects:
                    return False
                filtered.append((kind, entity_id))
            else:
                return False
        ordered = filtered
        primary = primary if primary in ordered else ordered[-1]
        primary_kind, primary_id = primary
        if primary_id != self.selected_object_id:
            if self._vector_gesture_mode is not None:
                self._cancel_vector_gesture(restore=True)
            self._cancel_text_property_drag()
            self._clear_transform_preview()
            self._transform_pivot = None
            self._transform_pivot_custom = False
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
        if self._text_editing:
            self._commit_text_edit()
        self.selected_kind, self.selected_id = primary
        if primary_kind == "object":
            self.selected_object_id = primary_id
            obj = self.chapter.objects.get(primary_id)
            self.active_layer_id = obj.parent_layer_id if obj else ""
            try:
                self.active_page_id = self.chapter.page_for_layer(
                    self.active_layer_id
                ).layer_id if self.active_layer_id else ""
            except Exception:
                self.active_page_id = ""
        else:
            self.selected_object_id = ""
            self.active_layer_id = primary_id
            try:
                self.active_page_id = self.chapter.page_for_layer(primary_id).layer_id
            except Exception:
                self.active_page_id = ""
        self.selected_entities = ordered
        if self.tool != ToolKind.TRANSFORM:
            self.tool = ToolKind.TRANSFORM
            self.toolChanged.emit(self.tool)
        self.selectionSetChanged.emit(list(self.selected_entities))
        # Compatibility consumers still key off the primary-selection signal.
        # Re-emit after installing the complete set so tree synchronization
        # cannot collapse the selection to the primary row.
        self.selectionChanged.emit(*primary)
        self._invalidate_scene_cache()
        self.update()
        if before is not None:
            after = self._selection_snapshot()
            self._push_selection_undo(before, after)
        return True

    def clear_selection(self) -> None:
        """Clear the current entity and notify every selection consumer."""
        if self.chapter is None:
            return
        before = None if self._suppress_selection_undo else self._selection_snapshot()
        if self._vector_gesture_mode is not None:
            self._cancel_vector_gesture(restore=True)
        if (
            self._page_gap_state is not None
            and self._page_gap_transaction is None
        ):
            self._clear_page_gap_editor()
        self._commit_text_edit()
        self._cancel_text_property_drag()
        self._clear_transform_preview()
        self.selected_kind = ""
        self.selected_id = ""
        self.selected_object_id = ""
        self.selected_entities = []
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
        self.selectionSetChanged.emit([])
        self._invalidate_scene_cache()
        self.update()
        if before is not None:
            after = self._selection_snapshot()
            self._push_selection_undo(before, after)

    def _selection_snapshot(self):
        return {
            "kind": self.selected_kind,
            "id": self.selected_id,
            "object_id": self.selected_object_id,
            "layer_id": self.active_layer_id,
            "page_id": self.active_page_id,
            "entities": list(self.selected_entities),
            "path": QPainterPath(self._drawing_selection_path),
        }

    def _restore_selection_snapshot(self, snap) -> None:
        self._suppress_selection_undo = True
        try:
            self.selected_kind = snap["kind"]
            self.selected_id = snap["id"]
            self.selected_object_id = snap["object_id"]
            self.active_layer_id = snap["layer_id"]
            self.active_page_id = snap["page_id"]
            self.selected_entities = list(snap["entities"])
            self._drawing_selection_path = QPainterPath(snap["path"])
            self._invalidate_scene_cache()
            self.selectionChanged.emit(self.selected_kind, self.selected_id)
            self.selectionSetChanged.emit(list(self.selected_entities))
            self.update()
        finally:
            self._suppress_selection_undo = False

    def _push_selection_undo(self, before, after) -> None:
        if self._suppress_selection_undo:
            return
        if before == after:
            return
        if not before["kind"] and not before["id"] and not before["entities"]:
            return
        self.command_stack.push(CallbackCommand(
            "Change selection",
            lambda b=before: self._restore_selection_snapshot(b),
            lambda a=after: self._restore_selection_snapshot(a),
        ), already_done=True)

    def set_tool(self, tool: ToolKind) -> bool:
        selected_object = (
            self.chapter.objects.get(self.selected_object_id)
            if self.chapter is not None else None
        )
        if tool != self.tool and self._vector_gesture_mode is not None:
            self._cancel_vector_gesture(restore=True)
        if tool != self.tool and self._fill_gesture_active:
            self._cancel_fill_gesture(restore=True)
        if tool != self.tool:
            self._cancel_fill_job()
        if tool != ToolKind.TEXT_EDIT:
            self._cancel_text_property_drag()
        if (
            tool not in {ToolKind.SHAPE_CREATE, ToolKind.SHAPE_EDIT}
            or tool != self.tool
        ):
            self._cancel_shape_property_drag()
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
        if tool != ToolKind.EYEDROPPER and self._eyedropper_sampling:
            self._eyedropper_sampling = False
            self._eyedropper_last_color = ""
            self._eyedropper_widget_point = None
            self.eyedropperGestureChanged.emit(False)
        if tool != ToolKind.VECTOR_EDIT:
            self._hover_vector_point_id = ""
        if self.tool == ToolKind.TRANSFORM and tool != ToolKind.TRANSFORM:
            self._clear_transform_preview()
        if self.tool == ToolKind.SHAPE_CREATE and tool != ToolKind.SHAPE_CREATE:
            self._clear_creation_gesture()
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
                (
                    RasterObject, VectorDrawingObject, GradientObject,
                    SpeedLineCenterObject,
                ),
            ):
                self.set_selection(
                    "layer", selected.parent_layer_id,
                    activate_default_tool=False,
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
                not (
                    self.selected_kind == "layer"
                    and (
                        layer := self.chapter.layers.get(self.selected_id)
                    ) is not None
                    and not layer.is_page
                    and layer.bound is not None
                    and layer.bound.closed
                )
                and not isinstance(
                    self.chapter.objects.get(self.selected_object_id),
                    RasterObject,
                )
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
        self._invalidate_scene_cache()
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

    def _set_centered_scale(self, scale: float) -> None:
        """Scale around the visible viewport center without moving the camera."""
        self.scale = max(0.05, min(8.0, float(scale)))

    def _center_camera_on_widget_anchor(
        self,
        document_anchor: QPointF,
        widget_anchor: QPointF,
        *,
        scale: float | None = None,
        rotation: float | None = None,
    ) -> None:
        target_scale = self.scale if scale is None else float(scale)
        target_rotation = self.rotation if rotation is None else float(rotation)
        viewport_delta = widget_anchor - QPointF(
            self.width() / 2, self.height() / 2
        )
        angle = math.radians(target_rotation)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        document_delta = QPointF(
            (
                viewport_delta.x() * cos_a
                + viewport_delta.y() * sin_a
            ) / target_scale,
            (
                -viewport_delta.x() * sin_a
                + viewport_delta.y() * cos_a
            ) / target_scale,
        )
        self.center_x = document_anchor.x() - document_delta.x()
        self.center_y = document_anchor.y() - document_delta.y()

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

    def reset_rotation(self) -> None:
        """Reset only canvas rotation, preserving scale and document center."""
        if abs(self.rotation) <= 1e-9:
            return
        self.rotation = 0.0
        self._invalidate_scene_cache()
        self.update()
        self.cameraChanged.emit()
        self.interactionFinished.emit()

    def sample_composited_color(self, world: QPointF) -> str | None:
        """Sample one full-resolution chapter pixel without UI overlays."""
        if self.chapter is None:
            return None
        sample_x, sample_y = math.floor(world.x()), math.floor(world.y())
        if not (
            0 <= sample_x < self.chapter.width
            and 0 <= sample_y < self.chapter.height
        ):
            return None
        image = QImage(3, 3, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(self.chapter.background))
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            transform = QTransform()
            transform.translate(1 - sample_x, 1 - sample_y)
            painter.setTransform(transform)
            visible = QRectF(sample_x - 1, sample_y - 1, 3, 3)
            for page_id in reversed(self.chapter.root_page_ids):
                self._render_layer(
                    painter, self.chapter.layers[page_id], 1.0, visible
                )
        finally:
            painter.end()
        return image.pixelColor(1, 1).name(
            QColor.NameFormat.HexArgb
        ).upper()

    def entity_world_rect(
        self, kind: str, entity_id: str,
    ) -> QRectF | None:
        if self.chapter is None:
            return None
        if kind == "object":
            return self.object_world_rect(entity_id)
        layer = self.chapter.layers.get(entity_id)
        if layer is None or layer.bound is None:
            return None
        return self.layer_world_transform(entity_id).map(
            self.layer_effective_path(entity_id)
        ).boundingRect()

    def _sample_eyedropper(self, world: QPointF) -> bool:
        color = self.sample_composited_color(world)
        if color is None:
            return False
        self._eyedropper_last_color = color
        self.colorSampled.emit(color)
        self.update()
        return True

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
        if base_width <= 0 and extra_width <= 0:
            return QPainterPath()
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
        # Shared by both endpoint branches.  A round start must not depend on
        # the end cap also being round.
        round_cap_kappa = 0.5522847498307936
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
            outward = center + tangent * radius
            mesh.cubicTo(
                left[-1] + tangent * (round_cap_kappa * radius),
                outward + normal * (round_cap_kappa * radius),
                outward,
            )
            mesh.cubicTo(
                outward - normal * (round_cap_kappa * radius),
                right[-1] + tangent * (round_cap_kappa * radius),
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
                right[0] + outward_tangent * (round_cap_kappa * radius),
                outward - normal * (round_cap_kappa * radius),
                outward,
            )
            mesh.cubicTo(
                outward + normal * (round_cap_kappa * radius),
                left[0] + outward_tangent * (round_cap_kappa * radius),
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
        self._asset_drag_clip_cache.clear()

    def _layer_operand_path(self, layer: LayerNode) -> QPainterPath:
        if layer.bound is None:
            return QPainterPath()
        if layer.layer_kind == "open_shape":
            return self.open_shape_mesh(
                layer.bound, layer.shape_style.base_thickness, 0,
                layer.shape_style.start_cap, layer.shape_style.end_cap,
            )
        return self.bound_path(layer.bound, layer.vertex_radius)

    def _document_layer_effective_path(
        self, document: ChapterDocument, layer_id: str,
        cache: dict[str, QPainterPath], *,
        virtual_parent_id: str = "",
        virtual_path_world: QPainterPath | None = None,
        virtual_operation: str = "add",
    ) -> QPainterPath:
        """Build one effective shape, optionally including a virtual child."""
        layer = document.layers[layer_id]
        if not layer.compound_enabled:
            return self.layer_shape_path(layer)
        cached = cache.get(layer_id)
        if cached is not None:
            return QPainterPath(cached)
        root_inverse, invertible = self._document_layer_world_transform(
            document, layer_id
        ).inverted()
        if not invertible:
            return QPainterPath()
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
                child = document.layers[reference.entity_id]
                if not child.visible or child.compound_operation == "ignore":
                    continue
                operand = (
                    self._document_layer_effective_path(
                        document, child.layer_id, cache,
                        virtual_parent_id=virtual_parent_id,
                        virtual_path_world=virtual_path_world,
                        virtual_operation=virtual_operation,
                    )
                    if child.compound_enabled
                    else self._layer_operand_path(child)
                )
                operand = root_inverse.map(
                    self._document_layer_world_transform(
                        document, child.layer_id
                    ).map(operand)
                )
                if child.compound_operation == "subtract":
                    subtractions = combine(subtractions, operand)
                else:
                    additions = combine(additions, operand)
                if not child.compound_enabled:
                    collect(child)

            if (
                parent.layer_id == virtual_parent_id
                and virtual_path_world is not None
                and not virtual_path_world.isEmpty()
            ):
                operand = root_inverse.map(virtual_path_world)
                if virtual_operation == "subtract":
                    subtractions = combine(subtractions, operand)
                elif virtual_operation != "ignore":
                    additions = combine(additions, operand)

        collect(layer)
        result = (
            additions.subtracted(subtractions)
            if not subtractions.isEmpty() else additions
        )
        result.setFillRule(Qt.OddEvenFill)
        cache[layer_id] = QPainterPath(result)
        return result

    def layer_effective_path(self, layer_id: str) -> QPainterPath:
        return self._document_layer_effective_path(
            self.chapter, layer_id, self._compound_path_cache
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        frame_started = time.perf_counter_ns()
        self._update_text_gizmo_overlay()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#242428"))
        painter.setRenderHint(QPainter.Antialiasing, True)
        if self.chapter is None:
            painter.setPen(QColor("#8e8e96"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Create or open a series to begin")
            self._performance.frame_ms.append(
                (time.perf_counter_ns() - frame_started) / 1_000_000
            )
            return
        if (
            not self._transform_static_cache.isNull()
            and self._transform_preview_quad is not None
            and self._geometry_transform_target is None
            and not (self.active_tone_mask_id or self.preview_tone_mask_id)
            and (
                isinstance(
                    self.chapter.objects.get(self.selected_object_id),
                    (RasterObject, VectorDrawingObject, ImageObject),
                )
                or (
                    isinstance(
                        self.chapter.objects.get(self.selected_object_id), TextObject
                    )
                    and not self._text_transform_cache.isNull()
                )
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
            selected = self.chapter.objects.get(self.selected_object_id)
            if self._is_transformable_object(selected):
                self._render_selected_raster_preview(painter, visible)
            else:
                self._render_selected_text_preview(painter)
            self._render_selected_drawing_underlay(painter, visible)
            self._clear_live_underlay_context()
            painter.restore()
            self._draw_selection(painter)
            self._draw_focal_modifier_handles(painter)
            self._performance.frame_ms.append(
                (time.perf_counter_ns() - frame_started) / 1_000_000
            )
            return
        live_vector_eraser = (
            self._vector_gesture_mode == "eraser"
            and isinstance(
                self.chapter.objects.get(self.selected_object_id),
                VectorDrawingObject,
            )
            and not self._vector_eraser_background_cache.isNull()
            and not self._object_has_effect_modifiers(
                self.selected_object_id
            )
        )
        if live_vector_eraser:
            painter.setTransform(QTransform())
            painter.drawImage(0, 0, self._vector_eraser_background_cache)
            painter.setTransform(self.camera_transform())
            painter.save()
            painter.setClipRect(
                QRectF(0, 0, self.chapter.width, self.chapter.height)
            )
            visible = self.visible_document_rect()
            self._set_live_underlay_context()
            self._render_selected_raster_preview(painter, visible)
            self._render_selected_drawing_underlay(painter, visible)
            self._clear_live_underlay_context()
            self._draw_page_gap_overlay(painter)
            painter.restore()
        else:
            self._ensure_scene_cache()
            painter.setTransform(QTransform())
            painter.drawImage(0, 0, self._scene_cache)
            painter.setTransform(self.camera_transform())
            painter.save()
            painter.setClipRect(
                QRectF(0, 0, self.chapter.width, self.chapter.height)
            )
            self._draw_predictive_ink(painter)
            self._draw_live_vector_gesture(painter)
            selected_live = self.chapter.objects.get(
                self.selected_object_id
            )
            if self._text_editing and isinstance(selected_live, TextObject):
                self._render_selected_raster_preview(
                    painter, self.visible_document_rect()
                )
            self._draw_page_gap_overlay(painter)
            painter.restore()
        painter.save()
        self._draw_tone_mask_preview(painter)
        if not (self.active_tone_mask_id or self.preview_tone_mask_id):
            self._draw_selection(painter)
            self._draw_focal_modifier_handles(painter)
            self._draw_creation_preview(painter)
            self._draw_asset_drag_preview(painter)
        painter.restore()
        painter.setTransform(QTransform())
        self._draw_tablet_hover(painter)
        self._draw_simplify_hover(painter)
        self._draw_eyedropper_swatch(painter)
        self._performance.frame_ms.append(
            (time.perf_counter_ns() - frame_started) / 1_000_000
        )

    def _draw_asset_drag_preview(self, painter: QPainter) -> None:
        if (
            not self._asset_drag_valid
            or self._asset_drag_manifest is None
            or self._asset_drag_image.isNull()
        ):
            return
        _x, _y, width, height = self._asset_drag_manifest.visual_bounds
        destination = QRectF(
            self._asset_drag_world.x() - width / 2,
            self._asset_drag_world.y() - height / 2,
            width, height,
        )
        root = (
            self._asset_drag_manifest.document.objects.get(
                self._asset_drag_manifest.root_id
            ) if self._asset_drag_manifest.root_kind == "object" else None
        )

        target = self.chapter.layers.get(self._asset_drag_parent_id)
        if isinstance(root, ImageObject) and target is not None and not target.is_page:
            bounds = self.layer_world_transform(target.layer_id).map(
                self.layer_effective_path(target.layer_id)
            ).boundingRect()
            aspect = root.pixel_width / max(1.0, root.pixel_height)
            height = max(1.0, bounds.height())
            width = height * aspect
            destination = QRectF(
                bounds.center().x() - width / 2,
                bounds.center().y() - height / 2,
                width, height,
            )
        painter.save()
        clip_path = self._asset_drag_clip_path(self._asset_drag_parent_id)
        if clip_path is not None:
            painter.setClipPath(clip_path, Qt.IntersectClip)
        painter.setOpacity(0.70)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(destination, self._asset_drag_image)
        painter.restore()

        # Keep the destination affordance readable even where the asset ghost
        # is clipped away by the prospective parent hierarchy.
        painter.save()
        layer = self.chapter.layers.get(self._asset_drag_parent_id)
        if layer is not None and layer.bound is not None:
            painter.setTransform(
                self.layer_world_transform(layer.layer_id), True
            )
            pen = QPen(QColor("#56a8ff"), 2.0 / max(self.scale, 0.05), Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(self.layer_effective_path(layer.layer_id))
        painter.restore()

    def set_tone_mask_mode(self, mask_id: str) -> None:
        if self._drawing and self.active_tone_mask_id:
            self._end_mask_stroke()
        self.active_tone_mask_id = str(mask_id)
        self.preview_tone_mask_id = ""
        self._mask_sample_timer.stop()
        self._mask_sample_queue.clear()
        self._invalidate_tone_mask_overlay()
        self.update()

    @staticmethod
    def _blue_mask_image(values: np.ndarray) -> QImage:
        alpha = np.ascontiguousarray(
            np.clip(values, 0.0, 1.0) * (0.35 * 255.0),
            dtype=np.uint8,
        )
        height, width = alpha.shape
        rgba = np.empty((height, width, 4), dtype=np.uint8)
        rgba[..., 0] = 0x64
        rgba[..., 1] = 0xB5
        rgba[..., 2] = 0xF6
        rgba[..., 3] = alpha
        return QImage(
            rgba.data, width, height, width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    def _blue_mask_tile(self, key: tuple[int, int], tile: QImage) -> QImage:
        cache_key = (key, int(tile.cacheKey()))
        cached = self._tone_mask_tile_cache.get(cache_key)
        if cached is not None:
            self._tone_mask_tile_cache.move_to_end(cache_key)
            return cached
        image = self._blue_mask_image(self._image_alpha_array(tile))
        self._tone_mask_tile_cache[cache_key] = image
        while len(self._tone_mask_tile_cache) > 128:
            self._tone_mask_tile_cache.popitem(last=False)
        return image

    def _draw_tone_mask_preview(self, painter: QPainter) -> None:
        mask_id = self.active_tone_mask_id or self.preview_tone_mask_id
        if not mask_id or self.chapter is None:
            return
        width, height = max(1, self.width()), max(1, self.height())
        mask = self.chapter.masks.get(mask_id)
        if mask is None:
            return
        transform = self.camera_transform()
        transform_key = tuple(round(value, 6) for value in (
            transform.m11(), transform.m12(), transform.m13(),
            transform.m21(), transform.m22(), transform.m23(),
            transform.m31(), transform.m32(), transform.m33(),
        ))
        cache_key = (
            mask_id, width, height, transform_key,
            tuple(mask.contributors), int(mask.revision),
        )
        if self._tone_mask_overlay_key != cache_key:
            field = self.render_tone_mask_field(
                mask_id, width, height, transform,
                self.visible_document_rect(), include_paint=False,
            )
            self._tone_mask_overlay_cache = self._blue_mask_image(field)
            self._tone_mask_overlay_key = cache_key
        painter.save()
        painter.setTransform(QTransform())
        painter.drawImage(0, 0, self._tone_mask_overlay_cache)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.setTransform(transform)
        painter.setClipRect(QRectF(0, 0, self.chapter.width, self.chapter.height))
        visible = self.visible_document_rect()
        for key, tile in self.tiles.iter_tiles(mask_id, visible):
            painter.drawImage(
                key[0] * self.tiles.tile_size,
                key[1] * self.tiles.tile_size,
                self._blue_mask_tile(key, tile),
            )
        painter.restore()

    def _cancel_external_drag_downloads(self) -> None:
        self._external_drag_generation += 1
        for reply in list(self._external_drag_replies):
            reply.abort()
            reply.deleteLater()
        self._external_drag_replies.clear()
        self._external_drag_entries.clear()
        self._pending_external_drop = None

    def _clear_asset_drag_preview(self, *, cancel_downloads: bool = True) -> None:
        changed = self._asset_drag_manifest is not None
        if cancel_downloads:
            self._cancel_external_drag_downloads()
        self._asset_drag_manifest = None
        self._asset_drag_tiles = None
        self._asset_drag_images = None
        self._external_drag_sources.clear()
        self._asset_drag_image = QImage()
        self._asset_drag_parent_id = ""
        self._asset_drag_valid = False
        self._asset_drag_clip_cache.clear()
        if changed:
            self.update()

    def place_image_sources(
        self,
        sources: list[tuple[str, str, bytes]],
        parent_id: str,
        world_center: QPointF,
        *,
        insertion_index: int | None = None,
        fit_parent: bool = False,
        label: str = "Import images",
        source_descriptors: list[ImageSourceDescriptor] | None = None,
        logical_sizes: list[tuple[int, int]] | None = None,
    ) -> list[str]:
        """Embed decoded originals and add image objects as one command."""
        if (
            self.chapter is None or parent_id not in self.chapter.layers
        ):
            return []
        valid: list[tuple[int, str, str, bytes, QImage]] = []
        for source_index, (filename, mime_type, data) in enumerate(sources):
            temporary = ImageStore()
            try:
                temporary.put("preview", filename, data, mime_type)
                image = temporary.image("preview")
            except ValueError:
                continue
            valid.append((source_index, filename, mime_type, bytes(data), image))
        if not valid:
            return []
        before_model = self.chapter.to_dict()
        before_images = self.images.snapshot()
        local_center = self._layer_world_to_local(parent_id, world_center)
        created: list[str] = []
        for offset, (
            source_index, filename, mime_type, data, image,
        ) in enumerate(valid):
            source = (
                image_source_from_dict(
                    source_descriptors[source_index].to_dict()
                )
                if source_descriptors is not None
                and source_index < len(source_descriptors)
                else None
            )
            logical_width, logical_height = (
                logical_sizes[source_index]
                if logical_sizes is not None and source_index < len(logical_sizes)
                else (image.width(), image.height())
            )
            logical_width = max(1, int(logical_width))
            logical_height = max(1, int(logical_height))
            obj = ImageObject(
                name=ImageStore.safe_filename(filename),
                source_filename=ImageStore.safe_filename(filename),
                source_mime_type=mime_type or "application/octet-stream",
                pixel_width=logical_width, pixel_height=logical_height,
                placement_mode="fit_parent" if fit_parent else "free",
                fit_mode="auto_height",
                source=source,
            )
            obj.transform_frame = (
                0.0, 0.0, float(logical_width), float(logical_height)
            )
            if not fit_parent:
                rect = QRectF(
                    local_center.x() - logical_width / 2,
                    local_center.y() - logical_height / 2,
                    logical_width, logical_height,
                )
                obj.transform_quad = self._rect_quad(rect)
            index = (
                insertion_index + offset
                if insertion_index is not None else None
            )
            self.chapter.add_object(parent_id, obj, index=index)
            self.images.put(
                obj.object_id, obj.source_filename, data,
                obj.source_mime_type,
            )
            created.append(obj.object_id)
        after_model = self.chapter.to_dict()
        after_images = self.images.snapshot()

        def restore(model: dict, resources: dict) -> None:
            self.replace_chapter(model)
            self.images.restore(resources)
            self.hierarchyChanged.emit()
            self.documentChanged.emit(QRectF())
            self.update()

        self.command_stack.push(CallbackCommand(
            label,
            lambda: restore(after_model, after_images),
            lambda: restore(before_model, before_images),
        ), already_done=True)
        self.set_selection("object", created[-1])
        self.hierarchyChanged.emit()
        self.documentChanged.emit(QRectF())
        self.interactionFinished.emit()
        self.update()
        return created

    def _asset_root_bypasses_parent_mask(self) -> bool:
        manifest = self._asset_drag_manifest
        if manifest is None:
            return False
        if manifest.root_kind == "layer":
            root = manifest.document.layers.get(manifest.root_id)
        else:
            root = manifest.document.objects.get(manifest.root_id)
        return bool(
            root
            and (
                root.ignore_parent_mask
                or (
                    isinstance(root, GradientObject)
                    and self._is_outward_gradient(root)
                )
            )
        )

    def _asset_virtual_compound_operand(
        self, layers: list[LayerNode],
    ) -> tuple[QPainterPath | None, str]:
        """Map the prospective root layer shape into target world space."""
        manifest = self._asset_drag_manifest
        if (
            manifest is None or manifest.root_kind != "layer"
            or not any(layer.compound_enabled for layer in layers)
        ):
            return None, "ignore"
        source = manifest.document
        root = source.layers.get(manifest.root_id)
        if (
            root is None or not root.visible
            or root.compound_operation == "ignore"
        ):
            return None, "ignore"
        source_path = self._document_layer_effective_path(
            source, root.layer_id, {}
        )
        source_path = self._document_layer_world_transform(
            source, root.layer_id
        ).map(source_path)
        bounds_x, bounds_y, bounds_width, bounds_height = manifest.visual_bounds
        transform = QTransform()
        transform.translate(
            self._asset_drag_world.x() - (bounds_x + bounds_width / 2),
            self._asset_drag_world.y() - (bounds_y + bounds_height / 2),
        )
        return transform.map(source_path), root.compound_operation

    def _asset_drag_changes_compound_path(self, parent_id: str) -> bool:
        manifest = self._asset_drag_manifest
        if (
            self.chapter is None or manifest is None
            or manifest.root_kind != "layer" or not parent_id
        ):
            return False
        root = manifest.document.layers.get(manifest.root_id)
        return bool(
            root and root.visible
            and root.compound_operation != "ignore"
            and any(
                layer.compound_enabled
                for layer in self.chapter.ancestor_layers(parent_id)
            )
        )

    def _asset_drag_clip_path(self, parent_id: str) -> QPainterPath | None:
        """Return the non-mutating world-space mask for a prospective drop."""
        if parent_id in self._asset_drag_clip_cache:
            cached = self._asset_drag_clip_cache[parent_id]
            return QPainterPath(cached) if cached is not None else None
        if self.chapter is None or self._asset_drag_manifest is None:
            return None

        layers = self.chapter.ancestor_layers(parent_id)
        manifest = self._asset_drag_manifest
        root_object = (
            manifest.document.objects.get(manifest.root_id)
            if manifest.root_kind == "object" else None
        )
        if root_object is not None and root_object.geometry_reference == "compound":
            compound = self.chapter.closest_compound_ancestor(
                parent_id, include_self=True
            )
            if compound is not None:
                compound_index = next(
                    index for index, layer in enumerate(layers)
                    if layer.layer_id == compound.layer_id
                )
                layers = layers[:compound_index + 1]

        virtual_path, virtual_operation = self._asset_virtual_compound_operand(
            layers
        )
        prospective_cache: dict[str, QPainterPath] = {}

        skipped_masks: set[str] = set()
        if self._asset_root_bypasses_parent_mask():
            skipped_masks.add(parent_id)
        for parent, child in zip(layers, layers[1:]):
            if child.ignore_parent_mask:
                skipped_masks.add(parent.layer_id)

        result: QPainterPath | None = None
        for layer in layers:
            if not layer.visible:
                result = QPainterPath()
                break
            if layer.bound is None or layer.layer_id in skipped_masks:
                continue
            path = self._document_layer_effective_path(
                self.chapter, layer.layer_id, prospective_cache,
                virtual_parent_id=parent_id,
                virtual_path_world=virtual_path,
                virtual_operation=virtual_operation,
            )
            path = self.layer_world_transform(layer.layer_id).map(path)
            result = path if result is None else result.intersected(path)

        stored = QPainterPath(result) if result is not None else None
        self._asset_drag_clip_cache[parent_id] = stored
        return QPainterPath(stored) if stored is not None else None

    def _asset_parent_accepts(self, layer_id: str, manifest: AssetManifest) -> bool:
        layer = self.chapter.layers.get(layer_id)
        if (
            layer is None
            or any(
                not ancestor.visible
                for ancestor in self.chapter.ancestor_layers(layer_id)
            )
        ):
            return False
        if manifest.root_kind != "object":
            return True
        root = manifest.document.objects.get(manifest.root_id)
        if not isinstance(root, GradientObject):
            return not isinstance(root, SpeedLineCenterObject)
        family = "speed_lines" if isinstance(root, SpeedLinesGradientObject) else "color_fill"
        return not self.chapter.gradient_children(
            layer_id, root.field_type, family=family
        )

    def _asset_target_parent(self, world: QPointF, manifest: AssetManifest) -> str:
        candidates: list[tuple[int, int, str]] = []

        def walk(layer_id: str, depth: int, order: int) -> int:
            layer = self.chapter.layers[layer_id]
            next_order = order + 1
            if not layer.visible:
                return next_order
            if layer.bound is not None:
                if self.layer_effective_path(layer_id).contains(
                    self._layer_world_to_local(layer_id, world)
                ):
                    candidates.append((depth, -order, layer_id))
            for child in layer.children:
                if child.kind == "layer":
                    next_order = walk(child.entity_id, depth + 1, next_order)
            return next_order

        order = 0
        for page_id in self.chapter.root_page_ids:
            order = walk(page_id, 0, order)
        for _depth, _order, layer_id in sorted(candidates, reverse=True):
            if self._asset_parent_accepts(layer_id, manifest):
                return layer_id
        fallback = self.active_page_id
        if not fallback and self.chapter.root_page_ids:
            fallback = self.chapter.root_page_ids[0]
        return fallback if fallback and self._asset_parent_accepts(fallback, manifest) else ""

    @staticmethod
    def _mime_image_bytes(mime) -> tuple[str, str, bytes] | None:
        if not mime.hasImage():
            return None
        value = mime.imageData()
        image = value.toImage() if hasattr(value, "toImage") else QImage(value)
        if image.isNull():
            return None
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        saved = image.save(buffer, "PNG")
        buffer.close()
        return (
            "Dragged Image.png", "image/png", bytes(payload)
        ) if saved else None

    @staticmethod
    def _data_uri_source(value: str) -> tuple[str, str, bytes] | None:
        match = re.fullmatch(
            r"data:(image/[-+.\w]+)(?:;charset=[^;,]+)?(;base64)?,(.*)",
            value.strip(), re.I | re.S,
        )
        if not match:
            return None
        mime_type = match.group(1).lower()
        try:
            data = (
                base64.b64decode(match.group(3), validate=True)
                if match.group(2)
                else urllib.parse.unquote_to_bytes(match.group(3))
            )
        except (ValueError, TypeError):
            return None
        suffix = mimetypes.guess_extension(mime_type) or ".img"
        return f"Dragged Image{suffix}", mime_type, data

    @staticmethod
    def _validated_image_source(
        filename: str, mime_type: str, data: bytes,
    ) -> tuple[str, str, bytes] | None:
        if not data or len(data) > 256 * 1024 * 1024:
            return None
        probe = ImageStore()
        try:
            source = probe.put("probe", filename, data, mime_type)
        except ValueError:
            return None
        return source.filename, source.mime_type, bytes(source.data)

    def _external_image_entries(self, mime) -> list[dict]:
        """Normalize Explorer, browser, and direct-image drag payloads."""
        entries: list[dict] = []
        candidates: list[tuple[str, str]] = []
        download_format = next((
            name for name in mime.formats()
            if str(name).casefold() == "downloadurl"
        ), "")
        if download_format:
            raw = bytes(mime.data(download_format)).decode(
                "utf-8", "replace"
            ).strip().strip("\x00")
            parts = raw.split(":", 2)
            if len(parts) == 3:
                candidates.append((parts[2], parts[1]))
        if mime.hasHtml():
            for source in re.findall(
                r'<img[^>]+src=["\']([^"\']+)', mime.html(), re.I
            ):
                candidates.append((html_lib.unescape(source), ""))
        if mime.hasUrls():
            candidates.extend((url.toString(), "") for url in mime.urls())
        if mime.hasText():
            text_value = mime.text().strip()
            if re.match(r"^(?:https?://|data:image/|file:)", text_value, re.I):
                candidates.append((text_value, ""))

        seen: set[str] = set()
        for raw_value, suggested_name in candidates:
            value = raw_value.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            data_source = self._data_uri_source(value)
            if data_source is not None:
                validated = self._validated_image_source(*data_source)
                if validated is not None:
                    entries.append({
                        "filename": validated[0], "mime_type": validated[1],
                        "data": validated[2], "url": "", "pending": False,
                        "failed": False,
                    })
                continue
            url = QUrl(value)
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                try:
                    validated = self._validated_image_source(
                        path.name,
                        mimetypes.guess_type(path.name)[0] or "",
                        path.read_bytes(),
                    )
                except OSError:
                    validated = None
                if validated is not None:
                    entries.append({
                        "filename": validated[0], "mime_type": validated[1],
                        "data": validated[2], "url": "", "pending": False,
                        "failed": False,
                    })
            elif url.scheme().lower() in {"http", "https"}:
                filename = ImageStore.safe_filename(
                    suggested_name or Path(
                        urllib.parse.unquote(url.path())
                    ).name or "Web Image"
                )
                entries.append({
                    "filename": filename,
                    "mime_type": mimetypes.guess_type(filename)[0] or "",
                    "data": b"", "url": url.toString(), "pending": True,
                    "failed": False,
                })

        fallback = self._mime_image_bytes(mime)
        if fallback is not None:
            validated = self._validated_image_source(*fallback)
            if validated is not None:
                remote = next((entry for entry in entries if entry["url"]), None)
                if remote is not None and not remote["data"]:
                    remote["data"] = validated[2]
                    remote["mime_type"] = validated[1]
                    if remote["filename"] == "Web Image":
                        remote["filename"] = validated[0]
                elif not entries:
                    entries.append({
                        "filename": validated[0], "mime_type": validated[1],
                        "data": validated[2], "url": "", "pending": False,
                        "failed": False,
                    })
        return entries

    def _external_image_sources(self, mime) -> list[tuple[str, str, bytes]]:
        """Compatibility helper returning sources immediately present in MIME data."""
        return [
            (entry["filename"], entry["mime_type"], entry["data"])
            for entry in self._external_image_entries(mime)
            if entry["data"] and not entry["failed"]
        ]

    def _start_external_drag_download(self, generation: int, entry: dict) -> None:
        request = QNetworkRequest(QUrl(entry["url"]))
        request.setRawHeader(b"User-Agent", b"WebtoonMaker/1.0")
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        reply = self._network_manager.get(request)
        self._external_drag_replies[reply] = (generation, entry)
        reply.finished.connect(lambda current=reply: self._finish_external_drag_download(current))
        QTimer.singleShot(
            15000,
            lambda current=reply: (
                current.abort()
                if current in self._external_drag_replies else None
            ),
        )

    def _finish_external_drag_download(self, reply: QNetworkReply) -> None:
        payload = self._external_drag_replies.pop(reply, None)
        if payload is None:
            reply.deleteLater()
            return
        generation, entry = payload
        entry["pending"] = False
        if generation != self._external_drag_generation:
            reply.deleteLater()
            return
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        data = bytes(reply.readAll())
        final_url = reply.url()
        filename = Path(urllib.parse.unquote(final_url.path())).name or entry["filename"]
        validated = None
        if (
            reply.error() == QNetworkReply.NetworkError.NoError
            and (status is None or 200 <= int(status) < 300)
        ):
            validated = self._validated_image_source(
                filename, entry["mime_type"], data
            )
        if validated is not None:
            entry.update({
                "filename": validated[0], "mime_type": validated[1],
                "data": validated[2], "failed": False,
            })
            if self._pending_external_drop is None:
                if self._install_external_drag_preview(entry):
                    self._update_asset_drag(self._external_drag_widget)
        elif not entry["data"]:
            entry["failed"] = True
        reply.deleteLater()
        self._finish_pending_external_drop_if_ready()

    def _install_external_drag_preview(self, entry: dict) -> bool:
        if not entry.get("data"):
            return False
        preview_store = ImageStore()
        try:
            preview_store.put(
                "preview", entry["filename"], entry["data"], entry["mime_type"]
            )
        except ValueError:
            return False
        image = preview_store.image("preview")
        document = ChapterDocument(
            name="Image Drag", width=max(1, image.width()),
            height=max(1, image.height()), background="#00000000",
            document_kind="asset",
        )
        page = document.add_page(
            "Image", BoundGeometry.rectangle(
                0, 0, max(1, image.width()), max(1, image.height())
            )
        )
        obj = ImageObject(
            name=entry["filename"], source_filename=entry["filename"],
            source_mime_type=entry["mime_type"], pixel_width=image.width(),
            pixel_height=image.height(),
            transform_frame=(0, 0, image.width(), image.height()),
            transform_quad=self._rect_quad(QRectF(0, 0, image.width(), image.height())),
        )
        document.add_object(page.layer_id, obj)
        self._asset_drag_manifest = AssetManifest(
            name=entry["filename"], root_kind="object", root_id=obj.object_id,
            document=document, visual_bounds=(0, 0, image.width(), image.height()),
        )
        self._asset_drag_tiles = TileStore()
        self._asset_drag_images = preview_store
        self._asset_drag_image = image
        self._asset_drag_clip_cache.clear()
        self.update()
        return True

    @staticmethod
    def _external_drag_placeholder(filename: str) -> dict:
        image = QImage(96, 96, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor(53, 45, 37, 230))
        painter = QPainter(image)
        painter.setPen(QPen(QColor("#f2a23a"), 5))
        painter.drawRect(QRectF(12, 12, 72, 72))
        painter.drawLine(QPointF(25, 68), QPointF(47, 43))
        painter.drawLine(QPointF(47, 43), QPointF(73, 70))
        painter.end()
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        buffer.close()
        return {
            "filename": filename or "Web Image", "mime_type": "image/png",
            "data": bytes(payload), "url": "", "pending": False,
            "failed": False,
        }

    def _finish_pending_external_drop_if_ready(self) -> None:
        pending = self._pending_external_drop
        if pending is None or any(
            entry["pending"] for entry in pending["entries"]
        ):
            return
        self._pending_external_drop = None
        sources = [
            (entry["filename"], entry["mime_type"], entry["data"])
            for entry in pending["entries"]
            if entry["data"] and not entry["failed"]
        ]
        if not sources:
            self.importStatusMessage.emit("No dropped images could be decoded")
            self._clear_asset_drag_preview()
            return
        created = self.place_image_sources(
            sources, pending["parent_id"], pending["world"],
            insertion_index=pending["insertion_index"],
            fit_parent=pending["fit_parent"], label="Drop images",
        )
        skipped = len(pending["entries"]) - len(sources)
        if skipped:
            self.importStatusMessage.emit(
                f"Imported {len(created)} image(s); skipped {skipped} invalid item(s)"
            )
        self._clear_asset_drag_preview()

    def _begin_external_image_drag(self, event) -> bool:
        self._cancel_external_drag_downloads()
        generation = self._external_drag_generation
        entries = self._external_image_entries(event.mimeData())
        if not entries:
            return False
        self._external_drag_entries = entries
        self._external_drag_sources = [
            (entry["filename"], entry["mime_type"], entry["data"])
            for entry in entries if entry["data"]
        ]
        preview = next((entry for entry in entries if entry["data"]), None)
        self._install_external_drag_preview(
            preview or self._external_drag_placeholder(entries[0]["filename"])
        )
        for entry in entries:
            if entry["pending"]:
                self._start_external_drag_download(generation, entry)
        self._external_drag_widget = QPointF(event.position())
        self._update_asset_drag(event.position())
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        return True

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self.chapter is None:
            event.ignore()
            return
        if not event.mimeData().hasFormat(ASSET_MIME):
            if not self._begin_external_image_drag(event):
                event.ignore()
            return
        if self.asset_repository is None:
            event.ignore()
            return
        try:
            payload = json.loads(bytes(
                event.mimeData().data(ASSET_MIME)
            ).decode("utf-8"))
            manifest, tiles, images = self.asset_repository.load(
                str(payload["asset_id"]), include_images=True
            )
            image = self._render_entity_crop(
                manifest.document, tiles, manifest.root_kind, manifest.root_id,
                images=images,
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            event.ignore()
            return
        self._asset_drag_manifest = manifest
        self._asset_drag_tiles = tiles
        self._asset_drag_images = images
        self._asset_drag_image = image
        self._asset_drag_clip_cache.clear()
        self._update_asset_drag(event.position())
        event.acceptProposedAction()

    def _update_asset_drag(self, widget_point: QPointF) -> None:
        if self._asset_drag_manifest is None:
            return
        self._asset_drag_world = self.widget_to_document(widget_point)
        self._asset_drag_parent_id = self._asset_target_parent(
            self._asset_drag_world, self._asset_drag_manifest
        )
        if self._asset_drag_changes_compound_path(self._asset_drag_parent_id):
            self._asset_drag_clip_cache.clear()
        self._asset_drag_valid = bool(self._asset_drag_parent_id)
        self.update()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._asset_drag_manifest is None:
            event.ignore()
            return
        self._external_drag_widget = QPointF(event.position())
        self._update_asset_drag(event.position())
        if self._asset_drag_valid:
            if self._external_drag_entries:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
            else:
                event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._clear_asset_drag_preview()
        event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802
        if (
            not self._asset_drag_valid
            or self._asset_drag_manifest is None
            or self._asset_drag_tiles is None
        ):
            self._clear_asset_drag_preview()
            event.ignore()
            return
        if self._external_drag_entries:
            parent_id = self._asset_drag_parent_id
            parent = self.chapter.layers.get(parent_id)
            insertion_index = None
            selected = self.chapter.objects.get(self.selected_object_id)
            if selected is not None and selected.parent_layer_id == parent_id:
                insertion_index = next((
                    index for index, child in enumerate(parent.children)
                    if child.kind == "object"
                    and child.entity_id == selected.object_id
                ), None)
            if any(entry["pending"] for entry in self._external_drag_entries):
                self._pending_external_drop = {
                    "entries": list(self._external_drag_entries),
                    "parent_id": parent_id,
                    "world": QPointF(self._asset_drag_world),
                    "insertion_index": insertion_index,
                    "fit_parent": bool(parent is not None and not parent.is_page),
                }
                self._asset_drag_manifest = None
                self._asset_drag_tiles = None
                self._asset_drag_images = None
                self._asset_drag_image = QImage()
                self._asset_drag_parent_id = ""
                self._asset_drag_valid = False
                self.update()
            else:
                sources = [
                    (entry["filename"], entry["mime_type"], entry["data"])
                    for entry in self._external_drag_entries
                    if entry["data"] and not entry["failed"]
                ]
                created = self.place_image_sources(
                    sources, parent_id, QPointF(self._asset_drag_world),
                    insertion_index=insertion_index,
                    fit_parent=bool(parent is not None and not parent.is_page),
                    label="Drop images",
                )
                self._clear_asset_drag_preview()
                if not created:
                    event.ignore()
                    return
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        manifest = self._asset_drag_manifest
        source_tiles = self._asset_drag_tiles
        parent_id = self._asset_drag_parent_id
        world = QPointF(self._asset_drag_world)
        before = self.chapter.to_dict()
        before_images = self.images.snapshot()
        try:
            kind, root_id, created_objects = instantiate_asset(
                manifest, source_tiles, self.chapter, self.tiles,
                parent_id, world.x(), world.y(),
                source_images=self._asset_drag_images,
                target_images=self.images,
            )
        except (KeyError, ValueError):
            self._clear_asset_drag_preview()
            event.ignore()
            return
        root_object = (
            self.chapter.objects.get(root_id) if kind == "object" else None
        )
        parent_layer = self.chapter.layers.get(parent_id)
        if (
            isinstance(root_object, ImageObject)
            and parent_layer is not None and not parent_layer.is_page
        ):
            root_object.placement_mode = "fit_parent"
            root_object.fit_mode = "auto_height"
            root_object.transform_quad = None
        after = self.chapter.to_dict()
        after_images = self.images.snapshot()
        before_mask_ids = {
            str(item.get("id")) for item in before.get("masks", [])
        }
        created_masks = set(self.chapter.masks) - before_mask_ids
        resource_ids = set(created_objects) | created_masks
        tile_payload = {
            object_id: self.tiles.object_tiles(object_id)
            for object_id in resource_ids
        }

        def restore(state: dict, resources: dict, with_tiles: bool) -> None:
            self.replace_chapter(state)
            self.images.restore(resources)
            for object_id in resource_ids:
                if with_tiles:
                    self.tiles.replace_object_tiles(
                        object_id, tile_payload.get(object_id, {})
                    )
                else:
                    self.tiles.remove_object(object_id)
            self.documentChanged.emit(QRectF())
            self.hierarchyChanged.emit()

        self.command_stack.push(CallbackCommand(
            f"Place asset {manifest.name}",
            lambda: restore(after, after_images, True),
            lambda: restore(before, before_images, False),
        ), already_done=True)
        self.set_selection(kind, root_id, activate_default_tool=True)
        self.documentChanged.emit(QRectF())
        self.hierarchyChanged.emit()
        self._clear_asset_drag_preview()
        event.acceptProposedAction()

    def _set_live_underlay_context(self) -> None:
        self._live_underlay_object_id = ""
        self._live_underlay_amount = 0.0
        if self.chapter is None or self.selected_kind != "object":
            return
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, (RasterObject, VectorDrawingObject, ImageObject)):
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
            not isinstance(obj, (RasterObject, VectorDrawingObject, ImageObject))
            or not obj.visible
        ):
            return
        ancestors = self.chapter.ancestor_layers(obj.parent_layer_id)
        if any(not layer.visible or layer.opacity <= 0 for layer in ancestors):
            return
        painter.save()
        painter.setOpacity(
            self.chapter.effective_object_opacity(obj.object_id)
            * self._live_underlay_amount
        )
        parent_transform = self.layer_world_transform(obj.parent_layer_id)
        painter.setTransform(parent_transform, True)
        inverse, valid = parent_transform.inverted()
        local_visible = inverse.mapRect(visible) if valid else visible
        if isinstance(obj, VectorDrawingObject):
            self._render_vector_drawing(
                painter, obj, local_visible
            )
        elif isinstance(obj, RasterObject):
            self._render_raster_content(
                painter, obj, local_visible,
                use_transform_preview=True,
            )
        else:
            self._render_image_object(painter, obj)
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

    def _render_entity_crop(
        self, document: ChapterDocument, tiles: TileStore,
        kind: str, entity_id: str, maximum: int = 1024,
        images: ImageStore | None = None,
    ) -> QImage:
        bounds = entity_visual_bounds(document, tiles, kind, entity_id)
        if bounds.isEmpty():
            return QImage()
        scale = min(
            1.0,
            maximum / max(1.0, bounds.width()),
            maximum / max(1.0, bounds.height()),
        )
        width = max(1, math.ceil(bounds.width() * scale))
        height = max(1, math.ceil(bounds.height() * scale))
        image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        previous = (
            self.chapter, self.tiles, self.images,
            self._compound_path_cache, self._vector_render_cache,
            self._vector_render_cache_bytes, self._vector_spatial_indexes,
            self._vector_render_scale_override,
            self._vector_render_scale_owner,
            self._gradient_geometry_cache, self._gradient_scalar_cache,
            self._gradient_render_cache,
        )
        try:
            self.chapter, self.tiles, self.images = (
                document, tiles, images or ImageStore()
            )
            self._compound_path_cache = {}
            self._vector_render_cache = {}
            self._vector_render_cache_bytes = 0
            self._vector_spatial_indexes = {}
            self._vector_render_scale_override = None
            self._vector_render_scale_owner = None
            self._gradient_geometry_cache = {}
            self._gradient_scalar_cache = {}
            self._gradient_render_cache = {}
            painter = QPainter(image)
            try:
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                painter.setTransform(QTransform(
                    scale, 0.0, 0.0, scale,
                    -bounds.left() * scale, -bounds.top() * scale,
                ))
                if kind == "layer":
                    self._render_layer(
                        painter, document.layers[entity_id], 1.0, bounds
                    )
                else:
                    obj = document.objects[entity_id]
                    parent_transform = self.layer_world_transform(
                        obj.parent_layer_id
                    )
                    inverse, valid = parent_transform.inverted()
                    painter.setTransform(parent_transform, True)
                    self._render_object(
                        painter, obj, 1.0,
                        inverse.mapRect(bounds) if valid else bounds,
                    )
            finally:
                if painter.isActive():
                    painter.end()
        finally:
            (
                self.chapter, self.tiles, self.images,
                self._compound_path_cache, self._vector_render_cache,
                self._vector_render_cache_bytes,
                self._vector_spatial_indexes,
                self._vector_render_scale_override,
                self._vector_render_scale_owner,
                self._gradient_geometry_cache, self._gradient_scalar_cache,
                self._gradient_render_cache,
            ) = previous
        return image

    def render_asset_thumbnail(
        self, manifest: AssetManifest, tiles: TileStore,
        size: int = 256, padding: int = 12,
        images: ImageStore | None = None,
    ) -> QImage:
        crop = self._render_entity_crop(
            manifest.document, tiles, manifest.root_kind, manifest.root_id,
            images=images,
        )
        result = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        result.fill(Qt.transparent)
        if crop.isNull():
            return result
        alpha = TileStore._alpha_bbox(crop)
        source = QRect(*alpha) if alpha is not None else crop.rect()
        available = max(1, size - padding * 2)
        factor = min(available / source.width(), available / source.height())
        width = max(1, round(source.width() * factor))
        height = max(1, round(source.height() * factor))
        destination = QRect(
            (size - width) // 2, (size - height) // 2, width, height
        )
        painter = QPainter(result)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(destination, crop, source)
        painter.end()
        return result

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
        if (
            layer.mask_only
            and self._rendering_mask_contributor <= 0
            and not (
                self._interactive_render
                and self.selected_kind == "layer"
                and self.selected_id == layer.layer_id
            )
        ):
            return
        if not layer.visible or (
            layer.opacity <= 0 and layer.opacity_mask is None
            and not self._render_base_alpha
        ):
            return
        if (
            (layer.modifier_ids or layer.opacity_mask is not None)
            and not self._render_base_alpha
            and ("layer", layer.layer_id) not in self._render_modifier_sources
        ):
            self._render_modified_layer(
                painter, layer, parent_opacity, visible_world
            )
            return
        painter.save()
        painter.setTransform(self._layer_parent_transform(layer), True)
        inverse, valid = self.layer_world_transform(layer.layer_id).inverted()
        local_visible = (
            inverse.mapRect(visible_world) if valid else QRectF(visible_world)
        )
        layer_opacity = (
            1.0
            if self._render_base_alpha
            or ("layer", layer.layer_id) in self._render_modifier_sources
            else layer.opacity
        )
        self._render_outward_gradient_children(
            painter, layer, parent_opacity * layer_opacity, local_visible
        )
        if layer.compound_enabled:
            self._render_compound_layer_contents(
                painter, layer, parent_opacity, visible_world
            )
            painter.restore()
            return
        if layer.layer_kind == "open_shape":
            style = layer.shape_style
            opacity = parent_opacity * layer_opacity
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
        opacity = parent_opacity * layer_opacity
        if layer.fill_color:
            painter.save()
            painter.setOpacity(opacity)
            painter.setClipPath(layer_path, Qt.IntersectClip)
            painter.fillPath(layer_path, QColor(layer.fill_color))
            painter.restore()
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

    def _render_modified_layer(
        self, painter: QPainter, layer: LayerNode, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        world_bounds = self.entity_world_rect("layer", layer.layer_id)
        modifiers = [
            self.chapter.modifiers[item] for item in layer.modifier_ids
            if item in self.chapter.modifiers
        ]
        if getattr(self, "_suppress_outline_for_mask", False):
            modifiers = [m for m in modifiers if not isinstance(m, OutlineModifier)]
        if (
            world_bounds is None or world_bounds.isEmpty()
            or (not modifiers and layer.opacity_mask is None)
        ):
            self._render_modifier_sources.add(("layer", layer.layer_id))
            try:
                self._render_layer(painter, layer, parent_opacity, visible_world)
            finally:
                self._render_modifier_sources.discard(("layer", layer.layer_id))
            return
        parent_transform = (
            self.layer_world_transform(layer.parent_id)
            if layer.parent_id else QTransform()
        )
        parent_inverse, valid = parent_transform.inverted()
        if not valid:
            return
        local = parent_inverse.mapRect(world_bounds)
        if layer.layer_kind == "open_shape":
            padding = (
                layer.shape_style.base_thickness / 2
                + layer.shape_style.outline_thickness + 2
            )
            local.adjust(-padding, -padding, padding, padding)
        expansion = max((
            self._modifier_maximum(
                modifier, "strength", modifier.strength
            ) * 3.0
            if isinstance(modifier, BlurModifier)
            else 100.0
            if isinstance(modifier, OutlineModifier)
            else 0.0
            for modifier in modifiers
        ), default=0.0)
        local.adjust(-expansion, -expansion, expansion, expansion)
        bounds = QRectF(
            math.floor(local.left()), math.floor(local.top()),
            max(1, math.ceil(local.right()) - math.floor(local.left())),
            max(1, math.ceil(local.bottom()) - math.floor(local.top())),
        )
        world_origin = parent_transform.map(bounds.topLeft())
        layer_signature = self._modifier_layer_signature(layer.layer_id)
        cache_key = (
            "layer", layer.layer_id,
            layer_signature,
            self._render_exclude_text,
            self._rect_signature(bounds), world_origin.toTuple(),
        )
        processed = self._modifier_cache_get(cache_key)
        if processed is None:
            source_key = (
                "layer-source", layer.layer_id,
                layer_signature[0], layer_signature[3], layer_signature[4],
                self._render_exclude_text,
                self._rect_signature(bounds), world_origin.toTuple(),
            )
            image = self._modifier_source_cache_get(source_key)
            if image is None:
                image = QImage(
                    max(1, math.ceil(bounds.width())),
                    max(1, math.ceil(bounds.height())),
                    QImage.Format.Format_ARGB32_Premultiplied,
                )
                image.fill(Qt.GlobalColor.transparent)
                source = QPainter(image)
                source.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                source.translate(-bounds.left(), -bounds.top())
                self._render_modifier_sources.add(("layer", layer.layer_id))
                try:
                    self._render_layer(source, layer, 1.0, visible_world)
                    drawing = self._active_vector_drawing()
                    modified_ancestors = (
                        [
                            candidate.layer_id
                            for candidate in self.chapter.ancestor_layers(
                                drawing.parent_layer_id
                            )
                            if candidate.modifier_ids
                        ]
                        if drawing is not None else []
                    )
                    if (
                        drawing is not None and not drawing.modifier_ids
                        and modified_ancestors
                        and modified_ancestors[-1] == layer.layer_id
                    ):
                        self._render_modified_vector_pencil_preview(
                            source, layer.parent_id or ""
                        )
                finally:
                    self._render_modifier_sources.discard(
                        ("layer", layer.layer_id)
                    )
                    source.end()
                self._modifier_source_cache_put(source_key, image)
            width, height = image.width(), image.height()
            world_to_image = self._world_to_image_transform(
                parent_transform, bounds, width, height
            )
            processed = apply_modifier_stack(
                image, modifiers, world_origin.toTuple(),
                self._modifier_mask_fields(
                    modifiers, width, height,
                    world_to_image, visible_world,
                ),
                outline_distance_cache=self._outline_distance_cache,
                blur_pyramid_cache=self._blur_pyramid_cache,
            )
            if layer.opacity_mask is not None:
                binding = layer.opacity_mask
                processed = apply_opacity_mask(
                    processed,
                    self.render_tone_mask_field(
                        binding.mask_id, width, height,
                        world_to_image, visible_world,
                    ),
                    binding.black_value, binding.white_value,
                )
            self._modifier_cache_put(cache_key, processed)
        painter.save()
        painter.setOpacity(parent_opacity * layer.opacity)
        transform = self._layer_parent_transform(layer)
        painter.setClipPath(
            transform.map(self.layer_effective_path(layer.layer_id)),
            Qt.ClipOperation.IntersectClip,
        )
        painter.drawImage(bounds.topLeft(), processed)
        painter.restore()

    def _render_compound_layer_contents(
        self, painter: QPainter, layer: LayerNode, parent_opacity: float,
        visible_world: QRectF,
    ) -> None:
        layer_path = self.layer_effective_path(layer.layer_id)
        opacity = parent_opacity * (
            1.0 if self._render_base_alpha
            or ("layer", layer.layer_id) in self._render_modifier_sources
            else layer.opacity
        )
        inverse, valid = self.layer_world_transform(layer.layer_id).inverted()
        local_visible = inverse.mapRect(visible_world) if valid else visible_world
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
        painter.setTransform(self._layer_parent_transform(layer), True)
        path = (
            self.layer_effective_path(layer.layer_id)
            if layer.compound_enabled else self._layer_operand_path(layer)
        )
        painter.save()
        painter.setClipPath(path, Qt.IntersectClip)
        opacity = parent_opacity * (
            1.0 if self._render_base_alpha
            or ("layer", layer.layer_id) in self._render_modifier_sources
            else layer.opacity
        )
        inverse, valid = self.layer_world_transform(layer.layer_id).inverted()
        local_visible = inverse.mapRect(visible_world) if valid else visible_world
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
        compound_inverse, valid = self.layer_world_transform(
            compound_id
        ).inverted()
        if not valid:
            return
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
                parent_transform = self.layer_world_transform(
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
                painter.setTransform(
                    parent_transform * compound_inverse, True
                )
                parent_inverse, invertible = parent_transform.inverted()
                local_visible = (
                    parent_inverse.mapRect(visible_world)
                    if invertible else visible_world
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

    @staticmethod
    def _vector_cache_entry_bytes(
        value: tuple[QImage, QRectF],
    ) -> int:
        image = value[0]
        return max(0, int(image.sizeInBytes()))

    def _clear_vector_render_cache(self) -> None:
        self._vector_render_cache.clear()
        self._vector_render_cache_bytes = 0

    def _recount_vector_render_cache_bytes(self) -> None:
        self._vector_render_cache_bytes = sum(
            self._vector_cache_entry_bytes(value)
            for value in self._vector_render_cache.values()
        )

    def _invalidate_vector_cache_strokes(
        self, stroke_ids: set[str],
    ) -> None:
        if not stroke_ids:
            return
        for key in list(self._vector_render_cache):
            if len(key) >= 2 and key[1] in stroke_ids:
                value = self._vector_render_cache.pop(key)
                self._vector_render_cache_bytes -= (
                    self._vector_cache_entry_bytes(value)
                )
        self._vector_render_cache_bytes = max(
            0, self._vector_render_cache_bytes
        )

    def _clear_vector_eraser_live_cache(self) -> None:
        for key in list(self._vector_render_cache):
            token = key[2] if len(key) >= 3 else None
            if (
                isinstance(token, tuple)
                and token
                and token[0] == "eraser-preview"
            ):
                value = self._vector_render_cache.pop(key)
                self._vector_render_cache_bytes -= (
                    self._vector_cache_entry_bytes(value)
                )
        self._vector_render_cache_bytes = max(
            0, self._vector_render_cache_bytes
        )
        self._vector_eraser_preview_versions.clear()
        self._vector_eraser_background_cache = QImage()

    def _store_vector_render_cache(
        self, key: tuple, value: tuple[QImage, QRectF],
    ) -> None:
        entry_bytes = self._vector_cache_entry_bytes(value)
        previous = self._vector_render_cache.pop(key, None)
        if previous is not None:
            self._vector_render_cache_bytes -= (
                self._vector_cache_entry_bytes(previous)
            )
        if entry_bytes > VECTOR_RENDER_CACHE_BUDGET:
            self._vector_render_cache_bytes = max(
                0, self._vector_render_cache_bytes
            )
            return
        self._vector_render_cache[key] = value
        self._vector_render_cache_bytes += entry_bytes
        while (
            self._vector_render_cache
            and self._vector_render_cache_bytes
            > VECTOR_RENDER_CACHE_BUDGET
        ):
            oldest = next(iter(self._vector_render_cache))
            removed = self._vector_render_cache.pop(oldest)
            self._vector_render_cache_bytes -= (
                self._vector_cache_entry_bytes(removed)
            )
        self._vector_render_cache_bytes = max(
            0, self._vector_render_cache_bytes
        )

    def _requested_vector_render_scale(self) -> float:
        if self._vector_render_scale_override is not None:
            return self._vector_render_scale_override
        return max(
            0.1,
            min(8.0, self.scale * max(1.0, self.devicePixelRatioF())),
        )

    def _vector_stroke_indexes(
        self, drawing: VectorDrawingObject, visible: QRectF | None,
    ) -> list[int]:
        """Return visible stroke indexes in their original paint order."""
        if visible is None:
            return list(range(len(drawing.strokes)))
        # Point/handle previews can move geometry without touching the model
        # revision.  Do not consult stale cells while such an edit is live.
        if (
            drawing.object_id == self.selected_object_id
            and (
                self._selection_vector_preview
                or self._vector_gesture_mode in {
                    "edit_drag", "redraw", "simplify", "connect",
                }
            )
        ):
            return list(range(len(drawing.strokes)))
        revision = (drawing.drawing_revision, len(drawing.strokes))
        index = self._vector_spatial_indexes.get(drawing.object_id)
        if index is None or index["revision"] != revision:
            cell = VECTOR_RENDER_INDEX_CELL
            cells: dict[tuple[int, int], list[int]] = {}
            global_strokes: list[int] = []
            for stroke_index, stroke in enumerate(drawing.strokes):
                if not stroke.points:
                    continue
                bounds = QRectF(*stroke.derived_bounds())
                if not all(math.isfinite(value) for value in (
                    bounds.left(), bounds.right(),
                    bounds.top(), bounds.bottom(),
                )):
                    global_strokes.append(stroke_index)
                    continue
                left = math.floor(bounds.left() / cell)
                right = math.floor(bounds.right() / cell)
                top = math.floor(bounds.top() / cell)
                bottom = math.floor(bounds.bottom() / cell)
                cell_count = (right - left + 1) * (bottom - top + 1)
                if cell_count > 4096:
                    global_strokes.append(stroke_index)
                    continue
                for y in range(top, bottom + 1):
                    for x in range(left, right + 1):
                        cells.setdefault((x, y), []).append(stroke_index)
            index = {
                "revision": revision,
                "cells": cells,
                "global": global_strokes,
            }
            self._vector_spatial_indexes[drawing.object_id] = index
        cell = VECTOR_RENDER_INDEX_CELL
        if not all(math.isfinite(value) for value in (
            visible.left(), visible.right(),
            visible.top(), visible.bottom(),
        )):
            return list(range(len(drawing.strokes)))
        left = math.floor(visible.left() / cell)
        right = math.floor(visible.right() / cell)
        top = math.floor(visible.top() / cell)
        bottom = math.floor(visible.bottom() / cell)
        candidates = set(index["global"])
        query_cells = (right - left + 1) * (bottom - top + 1)
        if query_cells > max(4096, len(index["cells"]) * 4):
            for (x, y), stroke_indexes in index["cells"].items():
                if left <= x <= right and top <= y <= bottom:
                    candidates.update(stroke_indexes)
        else:
            for y in range(top, bottom + 1):
                for x in range(left, right + 1):
                    candidates.update(index["cells"].get((x, y), ()))
        return sorted(candidates)

    def _vector_stroke_image(
        self, drawing: VectorDrawingObject, stroke: VectorStroke,
        *, cache_token: object | None = None,
    ) -> tuple[QImage, QRectF] | None:
        """Rasterize one stroke opacity mask, then colorize it exactly once."""
        if not stroke.points:
            return None
        device_ratio = max(1.0, float(self.devicePixelRatioF()))
        requested_scale = self._requested_vector_render_scale()
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
        self._store_vector_render_cache(key, result)
        return result

    def _vector_stroke_with_selection_preview(
        self, stroke: VectorStroke,
    ) -> VectorStroke:
        if not self._selection_vector_preview:
            return stroke
        preview_points = {
            point.point_id: self._selection_vector_preview[point.point_id]
            for point in stroke.points
            if point.point_id in self._selection_vector_preview
        }
        if not preview_points:
            return stroke
        return VectorStroke(
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

    def _render_vector_drawing(
        self, painter: QPainter, drawing: VectorDrawingObject,
        local_visible: QRectF | None = None,
    ) -> None:
        painter.save()
        destination = (
            list(self._multi_transform_preview_quads[drawing.object_id])
            if drawing.object_id in self._multi_transform_preview_quads
            else
            list(self._transform_preview_quad)
            if (
                drawing.object_id == self.selected_object_id
                and self._transform_preview_quad is not None
            )
            else list(drawing.transform_quad)
            if drawing.transform_quad is not None else None
        )
        if destination is not None:
            painter.setTransform(
                self._drawing_object_transform(drawing, destination), True
            )
        painter.translate(drawing.x, drawing.y)
        drawing_visible = (
            self._drawing_local_visible_rect(
                drawing, local_visible, destination
            )
            if local_visible is not None else None
        )
        for stroke_index in self._vector_stroke_indexes(
            drawing, drawing_visible
        ):
            stroke = drawing.strokes[stroke_index]
            if (
                drawing_visible is not None
                and not QRectF(*stroke.derived_bounds()).intersects(
                    drawing_visible
                )
            ):
                continue
            if (
                self._vector_gesture_mode == "eraser"
                and drawing.object_id == self.selected_object_id
                and stroke.stroke_id in self._vector_eraser_preview
            ):
                for replacement in self._vector_eraser_preview[
                    stroke.stroke_id
                ]:
                    rendered = self._vector_stroke_image(
                        drawing, replacement,
                        cache_token=(
                            "eraser-preview",
                            self._vector_eraser_preview_versions.get(
                                stroke.stroke_id, 0
                            ),
                            replacement.stroke_id,
                        ),
                    )
                    if rendered is not None:
                        image, target = rendered
                        painter.drawImage(target, image)
                continue
            promoted = self._promoted_vector_preview
            requested_scale = self._requested_vector_render_scale()
            if (
                promoted is not None
                and promoted["drawing_id"] == drawing.object_id
                and promoted["stroke_id"] == stroke.stroke_id
                and promoted["render_revision"] == stroke.render_revision
                and requested_scale <= 1.25
            ):
                tile_size = promoted["tile_size"]
                for (tile_x, tile_y), image in promoted["tiles"].items():
                    painter.drawImage(
                        tile_x * tile_size, tile_y * tile_size, image
                    )
                continue
            render_stroke = (
                self._vector_stroke_with_selection_preview(stroke)
                if (
                    drawing.object_id == self.selected_object_id
                    and self._selection_vector_preview
                ) else stroke
            )
            cache_token = None
            if (
                render_stroke is not stroke
                and drawing.object_id == self.selected_object_id
            ):
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
        if coverage.dtype == np.bool_:
            rgba[~coverage] = 0
        else:
            coverage_alpha = np.clip(
                coverage.astype(np.float32), 0.0, 1.0
            )
            rgba[..., 3] = np.clip(np.rint(
                rgba[..., 3].astype(np.float32) * coverage_alpha
            ), 0, 255).astype(np.uint8)
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
        if isinstance(obj, SpeedLinesGradientObject):
            # Legacy Speed Lines records are omitted during load.  Keep this
            # guard for in-memory documents created by older integrations so
            # the removed feature can never re-enter the renderer.
            return
        if isinstance(obj, ColorFillGradientObject):
            self._render_color_gradient(painter, obj, local_visible)

    # ---- Speed lines gradient ----

    @staticmethod
    def _gradient_thickness_lut(ramp: ColorGradientRamp) -> np.ndarray:
        """0..1 float LUT of a ramp's greyscale (alpha ignored)."""
        ramp.validate()
        colors = np.asarray([
            [
                QColor(stop.color).red(),
                QColor(stop.color).green(),
                QColor(stop.color).blue(),
            ]
            for stop in ramp.stops
        ], dtype=np.float32)
        positions = np.asarray(
            [stop.position for stop in ramp.stops], dtype=np.float32
        )
        values = np.linspace(0.0, 1.0, 1024, dtype=np.float32)
        right = np.clip(
            np.searchsorted(positions, values, side="right"),
            1, len(positions) - 1,
        )
        left = right - 1
        spans = positions[right] - positions[left]
        amounts = np.divide(
            values - positions[left], spans,
            out=np.ones_like(values), where=spans > 1e-8,
        )
        mixed = (
            colors[left] * (1.0 - amounts[:, None])
            + colors[right] * amounts[:, None]
        )
        grey = (
            mixed[:, 0] * 0.2126 + mixed[:, 1] * 0.7152
            + mixed[:, 2] * 0.0722
        )
        grey[values <= positions[0]] = (
            mixed[0, 0] * 0.2126 + mixed[0, 1] * 0.7152
            + mixed[0, 2] * 0.0722
        )
        grey[values >= positions[-1]] = (
            mixed[-1, 0] * 0.2126 + mixed[-1, 1] * 0.7152
            + mixed[-1, 2] * 0.0722
        )
        return np.clip(grey / 255.0, 0.0, 1.0).astype(np.float32)

    def _speed_noise(
        self, count: int, seed: int, distance: float, scale: float,
        *, closed: bool = False,
    ) -> np.ndarray:
        """Deterministic per-line endpoint noise with neighbor smoothing."""
        if count <= 0 or distance <= 0:
            return np.zeros(max(count, 0), dtype=np.float32)
        rng = np.random.RandomState(seed)
        raw = rng.uniform(0.0, 1.0, count).astype(np.float32)
        window = max(1, min(count, round(scale)))
        if window > 1:
            kernel = np.ones(window, dtype=np.float32) / window
            before = window // 2
            after = window - before - 1
            padded = np.pad(
                raw, (before, after), mode="wrap" if closed else "edge"
            )
            raw = np.convolve(padded, kernel, mode="valid")
        return raw * float(distance)

    @staticmethod
    def _speed_closest_points(
        points: np.ndarray, path: QPainterPath,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Closest flattened-path boundary point for each input point."""
        segment_starts: list[tuple[float, float]] = []
        segment_ends: list[tuple[float, float]] = []
        for polygon in path.toSubpathPolygons():
            polygon_points = [(point.x(), point.y()) for point in polygon]
            for start, end in zip(polygon_points, polygon_points[1:]):
                if math.dist(start, end) > 1e-6:
                    segment_starts.append(start)
                    segment_ends.append(end)
        if not segment_starts:
            return points.copy(), np.zeros(len(points), dtype=np.float32)
        starts = np.asarray(segment_starts, dtype=np.float32)
        vectors = np.asarray(segment_ends, dtype=np.float32) - starts
        lengths_squared = np.sum(vectors * vectors, axis=1)
        nearest = np.empty_like(points, dtype=np.float32)
        distances = np.empty(len(points), dtype=np.float32)
        for index, point in enumerate(points):
            relative = point[None, :] - starts
            along = np.clip(
                np.sum(relative * vectors, axis=1) / lengths_squared,
                0.0, 1.0,
            )
            projected = starts + vectors * along[:, None]
            squared = np.sum((projected - point[None, :]) ** 2, axis=1)
            winner = int(np.argmin(squared))
            nearest[index] = projected[winner]
            distances[index] = math.sqrt(float(squared[winner]))
        return nearest, distances

    @staticmethod
    def _speed_boundary_contours(
        path: QPainterPath, spacing: float,
    ) -> list[np.ndarray]:
        """Sample every closed painter-path contour at equal arc intervals."""
        contours: list[np.ndarray] = []
        for polygon in path.toSubpathPolygons():
            points = np.asarray(
                [(point.x(), point.y()) for point in polygon],
                dtype=np.float32,
            )
            if len(points) < 3:
                continue
            if np.linalg.norm(points[0] - points[-1]) > 1e-4:
                points = np.vstack((points, points[0]))
            vectors = points[1:] - points[:-1]
            lengths = np.sqrt(np.sum(vectors * vectors, axis=1))
            usable = lengths > 1e-5
            starts = points[:-1][usable]
            vectors = vectors[usable]
            lengths = lengths[usable]
            if not len(lengths):
                continue
            cumulative = np.concatenate((
                np.zeros(1, dtype=np.float32), np.cumsum(lengths)
            ))
            total = float(cumulative[-1])
            count = max(3, int(round(total / max(spacing, 1e-3))))
            distances = (
                np.arange(count, dtype=np.float32) + 0.5
            ) * total / count
            segments = np.clip(
                np.searchsorted(cumulative, distances, side="right") - 1,
                0, len(lengths) - 1,
            )
            local = (
                distances - cumulative[segments]
            ) / lengths[segments]
            contours.append(
                starts[segments] + vectors[segments] * local[:, None]
            )
        return contours

    @staticmethod
    def _speed_open_path_samples(
        path: QPainterPath, count: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return arc-length path samples and unit normals."""
        total = max(float(path.length()), 1e-6)
        count = max(2, int(count))
        distances = np.linspace(0.0, total, count, dtype=np.float32)
        points: list[tuple[float, float]] = []
        normals: list[tuple[float, float]] = []
        for distance in distances:
            percent = path.percentAtLength(float(distance))
            point = path.pointAtPercent(percent)
            angle = math.radians(-path.angleAtPercent(percent) + 90.0)
            points.append((point.x(), point.y()))
            normals.append((math.cos(angle), math.sin(angle)))
        return (
            np.asarray(points, dtype=np.float32),
            np.asarray(normals, dtype=np.float32),
        )

    @staticmethod
    def _speed_neighbor_widths(
        starts: np.ndarray, targets: np.ndarray, spacing: float,
        gap: float, *, closed: bool,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Maximum safe line widths at source and target without overlap."""
        count = len(starts)
        maximum = max(0.0, spacing - gap)
        source = np.full(count, maximum, dtype=np.float32)
        target = np.full(count, maximum, dtype=np.float32)
        if count <= 1:
            return source, target
        for index in range(count):
            neighbors: list[int] = []
            if closed or index > 0:
                neighbors.append((index - 1) % count)
            if closed or index + 1 < count:
                neighbors.append((index + 1) % count)
            source_distances = [
                float(np.linalg.norm(starts[index] - starts[neighbor]))
                for neighbor in neighbors
            ]
            target_distances = [
                float(np.linalg.norm(targets[index] - targets[neighbor]))
                for neighbor in neighbors
            ]
            if source_distances:
                source[index] = max(
                    0.0, min(maximum, min(source_distances) - gap)
                )
            if target_distances:
                target[index] = max(
                    0.0, min(maximum, min(target_distances) - gap)
                )
        return source, target

    @staticmethod
    def _speed_monotone_lane(
        points: np.ndarray, normals: np.ndarray,
    ) -> np.ndarray | None:
        """Drop offset-curve cusp loops and retain the longest forward run."""
        if len(points) < 2:
            return None
        tangents = np.column_stack((normals[:, 1], -normals[:, 0]))
        segments = points[1:] - points[:-1]
        midpoint_tangents = tangents[1:] + tangents[:-1]
        tangent_lengths = np.sqrt(
            np.sum(midpoint_tangents * midpoint_tangents, axis=1)
        )
        segment_lengths = np.sqrt(np.sum(segments * segments, axis=1))
        forward = np.sum(segments * midpoint_tangents, axis=1)
        valid = forward > (
            segment_lengths * np.maximum(tangent_lengths, 1e-6) * 0.02
        )
        runs: list[tuple[float, int, int]] = []
        start: int | None = None
        for index, usable in enumerate(valid):
            if usable and start is None:
                start = index
            if start is not None and (not usable or index == len(valid) - 1):
                end = index + 1 if usable and index == len(valid) - 1 else index
                if end > start:
                    runs.append((
                        float(np.sum(segment_lengths[start:end])),
                        start, end + 1,
                    ))
                start = None
        if not runs:
            return None
        _length, first, last = max(runs, key=lambda item: item[0])
        return points[first:last].copy()

    @staticmethod
    def _speed_trim_stroke(
        points: np.ndarray, available: np.ndarray, cut: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
        vectors = points[1:] - points[:-1]
        lengths = np.sqrt(np.sum(vectors * vectors, axis=1))
        usable = lengths > 1e-5
        if not np.any(usable):
            return None
        cumulative = np.concatenate((
            np.zeros(1, dtype=np.float32), np.cumsum(lengths)
        ))
        total = float(cumulative[-1])
        terminal = max(0.75, total - max(0.0, float(cut)))
        terminal = min(total, terminal)
        segment = min(
            len(lengths) - 1,
            max(0, int(np.searchsorted(cumulative, terminal, side="right") - 1)),
        )
        fraction = (
            (terminal - float(cumulative[segment]))
            / max(float(lengths[segment]), 1e-6)
        )
        kept_points = [point.copy() for point in points[:segment + 1]]
        kept_available = [float(value) for value in available[:segment + 1]]
        endpoint = points[segment] + vectors[segment] * fraction
        endpoint_available = (
            float(available[segment]) * (1.0 - fraction)
            + float(available[segment + 1]) * fraction
        )
        if not kept_points or np.linalg.norm(endpoint - kept_points[-1]) > 1e-5:
            kept_points.append(endpoint)
            kept_available.append(endpoint_available)
        if len(kept_points) < 2:
            return None
        kept = np.asarray(kept_points, dtype=np.float32)
        avail = np.asarray(kept_available, dtype=np.float32)
        effective_vectors = kept[1:] - kept[:-1]
        effective_lengths = np.sqrt(
            np.sum(effective_vectors * effective_vectors, axis=1)
        )
        effective_cumulative = np.concatenate((
            np.zeros(1, dtype=np.float32), np.cumsum(effective_lengths)
        ))
        color_t = np.clip(effective_cumulative / max(total, 1e-6), 0.0, 1.0)
        thickness_t = np.clip(
            effective_cumulative / max(float(effective_cumulative[-1]), 1e-6),
            0.0, 1.0,
        )
        return kept, avail, color_t, thickness_t

    def _speed_strokes_image(
        self, obj: SpeedLinesGradientObject,
        strokes: list[tuple[np.ndarray, np.ndarray]],
        cuts: np.ndarray, bounds: QRectF, scalar_key: tuple,
    ) -> tuple[QImage, QRectF] | None:
        """Rasterize variable-width stroke centerlines into scalar/coverage."""
        if bounds.isEmpty() or not strokes:
            return None
        width, height = self._gradient_grid_for_preview(bounds)
        cache_key = scalar_key + (width, height)
        cached = self._gradient_scalar_cache.get(cache_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.color_ramp, cached_bounds, cache_key
            )
        scalar = np.zeros((height, width), dtype=np.float32)
        coverage = np.zeros((height, width), dtype=np.float32)
        thickness_lut = self._gradient_thickness_lut(obj.thickness_ramp)
        pixel_width = bounds.width() / width
        pixel_height = bounds.height() / height
        antialias = max(pixel_width, pixel_height, 1e-4)
        for stroke_index, (raw_points, raw_available) in enumerate(strokes):
            trimmed = self._speed_trim_stroke(
                raw_points, raw_available,
                float(cuts[stroke_index]) if stroke_index < len(cuts) else 0.0,
            )
            if trimmed is None:
                continue
            points, available, color_t, thickness_t = trimmed
            for index in range(len(points) - 1):
                start, end = points[index], points[index + 1]
                vector = end - start
                length_squared = float(np.dot(vector, vector))
                if length_squared <= 1e-8:
                    continue
                maximum_width = max(
                    float(available[index]), float(available[index + 1])
                )
                padding = maximum_width * 0.5 + antialias
                left = min(float(start[0]), float(end[0])) - padding
                right = max(float(start[0]), float(end[0])) + padding
                top = min(float(start[1]), float(end[1])) - padding
                bottom = max(float(start[1]), float(end[1])) + padding
                x0 = max(0, int(math.floor(
                    (left - bounds.left()) / max(pixel_width, 1e-6)
                )))
                x1 = min(width, int(math.ceil(
                    (right - bounds.left()) / max(pixel_width, 1e-6)
                )))
                y0 = max(0, int(math.floor(
                    (top - bounds.top()) / max(pixel_height, 1e-6)
                )))
                y1 = min(height, int(math.ceil(
                    (bottom - bounds.top()) / max(pixel_height, 1e-6)
                )))
                if x0 >= x1 or y0 >= y1:
                    continue
                xs = bounds.left() + (
                    np.arange(x0, x1, dtype=np.float32) + 0.5
                ) * pixel_width
                ys = bounds.top() + (
                    np.arange(y0, y1, dtype=np.float32) + 0.5
                ) * pixel_height
                grid_x, grid_y = np.meshgrid(xs, ys)
                relative_x = grid_x - float(start[0])
                relative_y = grid_y - float(start[1])
                raw_along = (
                    (relative_x * float(vector[0])
                     + relative_y * float(vector[1])) / length_squared
                )
                along = np.clip(raw_along, 0.0, 1.0)
                nearest_x = float(start[0]) + along * float(vector[0])
                nearest_y = float(start[1]) + along * float(vector[1])
                distance = np.hypot(grid_x - nearest_x, grid_y - nearest_y)
                segment_thickness_t = (
                    float(thickness_t[index]) * (1.0 - along)
                    + float(thickness_t[index + 1]) * along
                )
                lut_indices = np.clip(
                    np.rint(segment_thickness_t * 1023.0), 0, 1023
                ).astype(np.int32)
                safe_width = (
                    float(available[index]) * (1.0 - along)
                    + float(available[index + 1]) * along
                )
                half_width = thickness_lut[lut_indices] * safe_width * 0.5
                segment_coverage = np.clip(
                    (half_width - distance) / antialias + 0.5,
                    0.0, 1.0,
                ).astype(np.float32)
                segment_coverage *= (
                    (raw_along >= 0.0) & (raw_along <= 1.0)
                )
                target_coverage = coverage[y0:y1, x0:x1]
                replace = segment_coverage > target_coverage
                if not np.any(replace):
                    continue
                segment_color_t = (
                    float(color_t[index]) * (1.0 - along)
                    + float(color_t[index + 1]) * along
                )
                target_scalar = scalar[y0:y1, x0:x1]
                target_scalar[replace] = segment_color_t[replace]
                target_coverage[replace] = segment_coverage[replace]
        self._cache_gradient_value(
            self._gradient_scalar_cache, cache_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.color_ramp, bounds, cache_key
        )

    def _speed_thickness_pixels(
        self, lut: np.ndarray, t: np.ndarray, available: float,
    ) -> np.ndarray:
        indices = np.clip(
            (np.clip(t, 0.0, 1.0) * 1023.0).astype(np.int32), 0, 1023
        )
        return lut[indices] * available

    def _render_speed_lines_gradient(
        self, painter: QPainter, obj: SpeedLinesGradientObject,
        local_visible: QRectF,
    ) -> None:
        parent_path = self.layer_effective_path(obj.parent_layer_id)
        if parent_path.isEmpty():
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )
        if obj.field_type == "line":
            rendered = self._speed_lines_line_image(
                obj, self.bound_path(obj.line_field.geometry),
                parent_path.boundingRect(),
            )
            if rendered is not None:
                painter.drawImage(rendered[1], rendered[0])
            return
        if obj.field_type == "radial":
            field = obj.radial_field
            boundary = self._radial_boundary_path(field)
            if field.reverse_direction:
                bounds = boundary.boundingRect().adjusted(
                    -field.distance, -field.distance,
                    field.distance, field.distance,
                )
            else:
                bounds = boundary.boundingRect()
            rendered = self._speed_lines_ray_image(
                obj, boundary, bounds,
                outward=field.reverse_direction,
                center_point=field.center(),
            )
            if rendered is not None:
                painter.drawImage(rendered[1], rendered[0])
            return
        field = obj.shape_field
        bounds = parent_path.boundingRect()
        if field.reverse_direction:
            bounds = bounds.adjusted(
                -field.distance, -field.distance,
                field.distance, field.distance,
            )
        rendered = self._speed_lines_ray_image(
            obj, parent_path, bounds, outward=field.reverse_direction,
            center_point=(
                self._shape_gradient_center(obj, parent_path).toTuple()
            ),
        )
        if rendered is not None:
            painter.drawImage(rendered[1], rendered[0])

    def _speed_lines_center_geometry(
        self, obj: SpeedLinesGradientObject, closed_parent: bool,
    ) -> BoundGeometry | None:
        if not obj.center_shape_id:
            return None
        center = self.chapter.speed_center_for(obj.object_id)
        if center is not None and center.geometry.closed == closed_parent:
            return center.geometry
        return None

    def _speed_parallel_field_image(
        self, obj: SpeedLinesGradientObject, path: QPainterPath,
        bounds: QRectF, scalar_key: tuple, seed: int,
    ) -> tuple[QImage, QRectF] | None:
        """Render robust mathematical offset curves without cusp loops."""
        width, height = self._gradient_grid_for_preview(bounds)
        cache_key = scalar_key + ("parallel-field", width, height)
        cached = self._gradient_scalar_cache.get(cache_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.color_ramp, cached_bounds, cache_key
            )
        amount, signed, _distance = self._path_projection_arrays(
            path, bounds, width, height
        )
        speed = obj.speed_field
        spacing = 1.0 / max(speed.density, 0.005)
        available = max(0.0, spacing - speed.gap)
        lane = np.rint(signed / spacing).astype(np.int32)
        minimum_lane = int(np.min(lane))
        maximum_lane = int(np.max(lane))
        lane_count = maximum_lane - minimum_lane + 1
        noise = self._speed_noise(
            lane_count, seed, speed.randomness_distance,
            speed.randomness_scale, closed=False,
        )
        lane_index = lane - minimum_lane
        arc = max(float(path.length()), 1e-6)
        terminal = np.clip(
            1.0 - (speed.close_range + noise[lane_index]) / arc,
            1e-3, 1.0,
        )
        travel = 1.0 - amount if obj.line_field.reverse_direction else amount
        thickness_t = np.clip(
            np.divide(
                travel, terminal, out=np.ones_like(travel),
                where=terminal > 1e-6,
            ),
            0.0, 1.0,
        )
        thickness_lut = self._gradient_thickness_lut(obj.thickness_ramp)
        thickness_indices = np.clip(
            np.rint(thickness_t * 1023.0), 0, 1023
        ).astype(np.int32)
        half_width = thickness_lut[thickness_indices] * available * 0.5
        distance_to_lane = np.abs(
            signed - lane.astype(np.float32) * spacing
        )
        antialias = max(
            bounds.width() / width, bounds.height() / height, 1e-4
        )
        coverage = np.clip(
            (half_width - distance_to_lane) / antialias + 0.5,
            0.0, 1.0,
        ).astype(np.float32)
        coverage *= travel <= terminal
        scalar = np.clip(travel, 0.0, 1.0).astype(np.float32)
        self._cache_gradient_value(
            self._gradient_scalar_cache, cache_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.color_ramp, bounds, cache_key
        )

    def _speed_lines_line_image(
        self, obj: SpeedLinesGradientObject, path: QPainterPath,
        bounds: QRectF,
    ) -> tuple[QImage, QRectF] | None:
        if bounds.isEmpty() or path.isEmpty() or path.length() <= 1e-5:
            return None
        field = obj.line_field
        speed = obj.speed_field
        speed.validate()
        spacing = 1.0 / max(speed.density, 0.005)
        guide_signature = self._gradient_path_signature(path)
        parent_signature = self._gradient_path_signature(
            self.layer_effective_path(obj.parent_layer_id)
        )
        center_geometry = self._speed_lines_center_geometry(
            obj, closed_parent=False
        )
        center_path = (
            self.bound_path(center_geometry)
            if center_geometry is not None else None
        )
        center_signature = (
            self._gradient_path_signature(center_path)
            if center_path is not None else ()
        )
        scalar_key = (
            "manga-speed-line", obj.object_id, guide_signature,
            parent_signature, center_signature,
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
            field.direction_mode, field.reverse_direction,
            round(field.perpendicular_distance, 4),
            round(speed.density, 5), round(speed.gap, 4),
            round(speed.close_range, 4),
            round(speed.randomness_distance, 4),
            round(speed.randomness_scale, 4),
            self._gradient_ramp_signature(obj.thickness_ramp),
        )
        seed = zlib.crc32(
            f"{obj.object_id}|{field.direction_mode}".encode()
        )
        if field.direction_mode == "parallel" and center_path is None:
            return self._speed_parallel_field_image(
                obj, path, bounds, scalar_key, seed
            )
        strokes: list[tuple[np.ndarray, np.ndarray]] = []
        if field.direction_mode == "perpendicular":
            count = max(2, int(round(float(path.length()) / spacing)) + 1)
            starts, normals = self._speed_open_path_samples(path, count)
            if center_path is not None:
                targets, _distances = self._speed_closest_points(
                    starts, center_path
                )
            else:
                distance = float(field.perpendicular_distance)
                targets = starts + normals * distance
            source_width, target_width = self._speed_neighbor_widths(
                starts, targets, spacing, speed.gap, closed=False
            )
            for index in range(len(starts)):
                strokes.append((
                    np.asarray((starts[index], targets[index]), dtype=np.float32),
                    np.asarray(
                        (source_width[index], target_width[index]),
                        dtype=np.float32,
                    ),
                ))
        else:
            sample_step = max(2.0, min(6.0, spacing * 0.4))
            sample_count = max(
                16, int(math.ceil(float(path.length()) / sample_step)) + 1
            )
            base_points, normals = self._speed_open_path_samples(
                path, sample_count
            )
            diagonal = max(
                math.hypot(bounds.width(), bounds.height()), spacing
            )
            lane_count = max(1, int(math.ceil(diagonal / spacing)))
            offsets = np.arange(
                -lane_count, lane_count + 1, dtype=np.float32
            ) * spacing
            for offset in offsets:
                lane = self._speed_monotone_lane(
                    base_points + normals * float(offset), normals
                )
                if lane is None:
                    continue
                if (
                    float(np.max(lane[:, 0])) < bounds.left() - spacing
                    or float(np.min(lane[:, 0])) > bounds.right() + spacing
                    or float(np.max(lane[:, 1])) < bounds.top() - spacing
                    or float(np.min(lane[:, 1])) > bounds.bottom() + spacing
                ):
                    continue
                if field.reverse_direction:
                    lane = lane[::-1].copy()
                if center_path is not None:
                    closest, distances = self._speed_closest_points(
                        lane, center_path
                    )
                    terminal_index = int(np.argmin(distances))
                    terminal_index = max(1, terminal_index)
                    lane = lane[:terminal_index + 1].copy()
                    terminal = closest[min(terminal_index, len(closest) - 1)]
                    if np.linalg.norm(lane[-1] - terminal) > 1e-4:
                        lane = np.vstack((lane, terminal))
                available = np.full(
                    len(lane), max(0.0, spacing - speed.gap),
                    dtype=np.float32,
                )
                strokes.append((lane, available))
        cuts = speed.close_range + self._speed_noise(
            len(strokes), seed, speed.randomness_distance,
            speed.randomness_scale, closed=False,
        )
        return self._speed_strokes_image(
            obj, strokes, cuts, bounds, scalar_key
        )

    def _speed_lines_line_image_legacy(
        self, obj: SpeedLinesGradientObject, path: QPainterPath,
        bounds: QRectF,
    ) -> tuple[QImage, QRectF] | None:
        if bounds.isEmpty():
            return None
        width, height = self._gradient_grid_for_preview(bounds)
        field = obj.line_field
        speed = obj.speed_field
        signature = self._gradient_path_signature(path)
        thickness_lut = self._gradient_thickness_lut(obj.thickness_ramp)
        lut_key = self._gradient_ramp_signature(obj.thickness_ramp)
        scalar_key = (
            "speed-line", signature, width, height,
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
            field.direction_mode,
            round(field.perpendicular_distance, 4),
            round(speed.density, 5), round(speed.gap, 4),
            round(speed.close_range, 4),
            round(speed.randomness_distance, 4),
            round(speed.randomness_scale, 4), lut_key,
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.color_ramp, cached_bounds, scalar_key
            )
        amount, signed, _distance = self._path_projection_arrays(
            path, bounds, width, height
        )
        spacing = 1.0 / max(speed.density, 0.005)
        available = max(0.0, spacing - speed.gap)
        arc = max(float(path.length()), 1e-6)
        seed = zlib.crc32(f"{signature}|{field.direction_mode}|"
                          f"{field.perpendicular_distance:.4f}|"
                          f"{speed.density:.5f}|{speed.gap:.4f}|"
                          f"{speed.close_range:.4f}|"
                          f"{speed.randomness_distance:.4f}|"
                          f"{speed.randomness_scale:.4f}".encode())
        if field.direction_mode == "parallel":
            side = float(field.perpendicular_distance)
            k = np.floor((signed - side) / spacing).astype(np.int32)
            centerline = side + k.astype(np.float32) * spacing
            t = amount
            thickness = self._speed_thickness_pixels(
                thickness_lut, t, available
            )
            count = int(np.max(np.abs(k))) + 4 if len(k) else 4
            noise = self._speed_noise(
                count, seed, speed.randomness_distance,
                speed.randomness_scale,
            )
            index = np.clip(k, 0, count - 1)
            end = (speed.close_range + noise[index]) / arc
            scalar = np.clip(t, 0.0, 1.0)
            if side > 0:
                on_side = k >= 0
            elif side < 0:
                on_side = k <= 0
            else:
                on_side = np.ones_like(k, dtype=bool)
            coverage = (
                on_side
                & (np.abs(signed - centerline) <= thickness * 0.5)
                & (t >= end) & (t <= 1.0 - end)
            )
        else:
            side = 1.0 if field.perpendicular_distance >= 0 else -1.0
            length = abs(float(field.perpendicular_distance))
            count = max(1, int(round(arc * speed.density)))
            noise = self._speed_noise(
                count, seed, speed.randomness_distance,
                speed.randomness_scale,
            )
            cut = np.clip(
                (speed.close_range + noise).astype(np.float32),
                0.0, max(0.0, length),
            )
            n = np.clip(
                (amount * count).astype(np.int32), 0, count - 1
            )
            center = (n.astype(np.float32) + 0.5) / count
            u = signed * side
            length_n = length - cut[n]
            u_norm = np.divide(
                u, np.maximum(length_n, 1e-6),
                out=np.zeros_like(u), where=length_n > 1e-6,
            )
            u_norm = np.clip(u_norm, 0.0, 1.0)
            thickness = self._speed_thickness_pixels(
                thickness_lut, u_norm, available
            )
            band = thickness * 0.5 / arc
            scalar = u_norm
            coverage = (
                (u >= 0.0) & (u <= length_n)
                & (np.abs(amount - center) <= band) & (length > 0.0)
            )
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.color_ramp, bounds, scalar_key
        )

    def _speed_lines_ray_image(
        self, obj: SpeedLinesGradientObject, boundary: QPainterPath,
        bounds: QRectF, *, outward: bool,
        center_point: tuple[float, float],
    ) -> tuple[QImage, QRectF] | None:
        if bounds.isEmpty() or boundary.isEmpty():
            return None
        speed = obj.speed_field
        speed.validate()
        spacing = 1.0 / max(speed.density, 0.005)
        center_geometry = None if outward else self._speed_lines_center_geometry(
            obj, closed_parent=True
        )
        center_path = (
            self.bound_path(center_geometry)
            if center_geometry is not None else None
        )
        if not outward:
            target_bounds = (
                center_path.boundingRect()
                if center_path is not None
                else QRectF(center_point[0], center_point[1], 0.001, 0.001)
            )
            bounds = bounds.united(target_bounds)
        boundary_signature = self._gradient_path_signature(boundary)
        center_signature: tuple = (
            ("path", self._gradient_path_signature(center_path))
            if center_path is not None
            else ("point", round(center_point[0], 4), round(center_point[1], 4))
        )
        span_distance = (
            float(obj.radial_field.distance)
            if obj.field_type == "radial" else float(obj.shape_field.distance)
        )
        scalar_key = (
            "manga-speed-rays", obj.object_id, boundary_signature,
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
            outward, round(span_distance, 4), center_signature,
            round(speed.density, 5), round(speed.gap, 4),
            round(speed.close_range, 4),
            round(speed.randomness_distance, 4),
            round(speed.randomness_scale, 4),
            self._gradient_ramp_signature(obj.thickness_ramp),
        )
        contours = self._speed_boundary_contours(boundary, spacing)
        strokes: list[tuple[np.ndarray, np.ndarray]] = []
        cuts: list[float] = []
        center = np.asarray(center_point, dtype=np.float32)
        for contour_index, starts in enumerate(contours):
            if outward:
                directions = starts - center[None, :]
                lengths = np.sqrt(np.sum(directions * directions, axis=1))
                fallback = starts - np.mean(starts, axis=0, keepdims=True)
                missing = lengths <= 1e-5
                if np.any(missing):
                    directions[missing] = fallback[missing]
                    lengths = np.sqrt(np.sum(directions * directions, axis=1))
                directions = np.divide(
                    directions, np.maximum(lengths[:, None], 1e-6)
                )
                targets = starts + directions * max(span_distance, 0.001)
            elif center_path is not None:
                targets, _distances = self._speed_closest_points(
                    starts, center_path
                )
            else:
                targets = np.repeat(center[None, :], len(starts), axis=0)
            source_width, target_width = self._speed_neighbor_widths(
                starts, targets, spacing, speed.gap, closed=True
            )
            for index in range(len(starts)):
                strokes.append((
                    np.asarray((starts[index], targets[index]), dtype=np.float32),
                    np.asarray(
                        (source_width[index], target_width[index]),
                        dtype=np.float32,
                    ),
                ))
            contour_seed = zlib.crc32(
                f"{obj.object_id}|{contour_index}|{outward}".encode()
            )
            contour_noise = self._speed_noise(
                len(starts), contour_seed, speed.randomness_distance,
                speed.randomness_scale, closed=True,
            )
            cuts.extend((speed.close_range + contour_noise).tolist())
        return self._speed_strokes_image(
            obj, strokes, np.asarray(cuts, dtype=np.float32),
            bounds, scalar_key,
        )

    def _speed_lines_ring_image_legacy(
        self, obj: SpeedLinesGradientObject, boundary: QPainterPath,
        bounds: QRectF, *, outward: bool,
        center_point: tuple[float, float],
    ) -> tuple[QImage, QRectF] | None:
        if bounds.isEmpty():
            return None
        width, height = self._gradient_grid_for_preview(bounds)
        speed = obj.speed_field
        boundary_sig = self._gradient_path_signature(boundary)
        thickness_lut = self._gradient_thickness_lut(obj.thickness_ramp)
        lut_key = self._gradient_ramp_signature(obj.thickness_ramp)
        span_distance = (
            round(obj.radial_field.distance, 4)
            if obj.field_type == "radial" else round(obj.shape_field.distance, 4)
        )
        center_geometry = None if outward else self._speed_lines_center_geometry(
            obj, closed_parent=True
        )
        center_sig: tuple = ()
        if center_geometry is not None:
            center_sig = (
                "path",
                self._gradient_path_signature(self.bound_path(center_geometry)),
            )
        else:
            center_sig = ("point", round(center_point[0], 4),
                          round(center_point[1], 4))
        scalar_key = (
            "speed-ring", boundary_sig, width, height,
            round(bounds.x(), 3), round(bounds.y(), 3),
            round(bounds.width(), 3), round(bounds.height(), 3),
            outward, round(span_distance, 4), center_sig,
            round(speed.density, 5), round(speed.gap, 4),
            round(speed.close_range, 4),
            round(speed.randomness_distance, 4),
            round(speed.randomness_scale, 4), lut_key,
        )
        cached = self._gradient_scalar_cache.get(scalar_key)
        if cached is not None:
            scalar, coverage, cached_bounds = cached
            return self._gradient_image_from_scalar(
                scalar, coverage, obj.color_ramp, cached_bounds, scalar_key
            )
        _amount, _signed, boundary_distance = self._path_projection_arrays(
            boundary, bounds, width, height
        )
        grid_x, grid_y = self._gradient_coordinates(bounds, width, height)
        center_inside: np.ndarray | None = None
        if center_geometry is not None:
            _c_amount, _c_signed, center_distance = self._path_projection_arrays(
                self.bound_path(center_geometry), bounds, width, height
            )
            center_inside = self._path_coverage(
                self.bound_path(center_geometry), bounds, width, height
            )
        else:
            center_distance = np.hypot(
                grid_x - center_point[0], grid_y - center_point[1]
            )
        spacing = 1.0 / max(speed.density, 0.005)
        available = max(0.0, spacing - speed.gap)
        if outward:
            span = max(float(span_distance), 0.001)
            t = np.clip(boundary_distance / span, 0.0, 1.0)
            max_k = int(np.ceil(float(np.max(boundary_distance)) / spacing)) + 4
        else:
            t = np.divide(
                boundary_distance,
                boundary_distance + center_distance,
                out=np.ones_like(boundary_distance),
                where=(boundary_distance + center_distance) > 1e-6,
            )
            t = np.clip(t, 0.0, 1.0)
            max_k = int(np.ceil(
                float(np.max(boundary_distance)) / spacing
            )) + 4
        k = np.round(boundary_distance / spacing).astype(np.int32)
        centerline = k.astype(np.float32) * spacing
        thickness = self._speed_thickness_pixels(
            thickness_lut, t, available
        )
        seed = zlib.crc32(f"{boundary_sig}|{outward}|{span_distance:.4f}|"
                          f"{center_sig}|{speed.density:.5f}|{speed.gap:.4f}|"
                          f"{speed.close_range:.4f}|"
                          f"{speed.randomness_distance:.4f}|"
                          f"{speed.randomness_scale:.4f}".encode())
        count = max(max_k, 4)
        noise = self._speed_noise(
            count, seed, speed.randomness_distance, speed.randomness_scale,
        )
        index = np.clip(k, 0, count - 1)
        cut = speed.close_range + noise[index]
        scalar = t
        band = np.abs(boundary_distance - centerline) <= thickness * 0.5
        if outward:
            coverage = band & (boundary_distance >= 0.0) & (
                boundary_distance <= span
            )
        elif center_inside is not None:
            coverage = band & (center_distance >= cut) & ~center_inside
        else:
            coverage = band & (center_distance >= cut)
        self._cache_gradient_value(
            self._gradient_scalar_cache, scalar_key,
            (scalar, coverage, QRectF(bounds)),
        )
        return self._gradient_image_from_scalar(
            scalar, coverage, obj.color_ramp, bounds, scalar_key
        )

    def _render_object(
        self, painter: QPainter, obj: DocumentObject, parent_opacity: float,
        local_visible: QRectF,
    ) -> None:
        if self._render_exclude_text and isinstance(obj, TextObject):
            return
        if obj.object_id == self._render_excluded_object_id:
            return
        if (
            obj.mask_only
            and self._rendering_mask_contributor <= 0
            and not (
                self._interactive_render
                and self.selected_kind == "object"
                and self.selected_id == obj.object_id
            )
        ):
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
        if (
            (obj.modifier_ids or obj.opacity_mask is not None)
            and not self._render_base_alpha
            and ("object", obj.object_id) not in self._render_modifier_sources
        ):
            self._render_modified_object(
                painter, obj, parent_opacity, local_visible
            )
            return
        painter.save()
        opacity = (
            parent_opacity
            if obj.opacity_locked else parent_opacity * obj.opacity
        )
        if obj.object_id == self._live_underlay_object_id:
            opacity *= 1.0 - self._live_underlay_amount
        painter.setOpacity(opacity)
        self._render_object_content(painter, obj, local_visible)
        painter.restore()

    def _render_object_content(
        self, painter: QPainter, obj: DocumentObject,
        local_visible: QRectF,
    ) -> None:
        if isinstance(obj, VectorDrawingObject):
            self._render_vector_drawing(painter, obj, local_visible)
        elif isinstance(obj, GradientObject):
            self._render_gradient(painter, obj, local_visible)
        elif isinstance(obj, RasterObject):
            self._render_raster_content(
                painter, obj, local_visible, use_transform_preview=True
            )
        elif isinstance(obj, ImageObject):
            self._render_image_object(painter, obj)
        elif isinstance(obj, TextObject):
            self._draw_text_object(painter, obj)

    @staticmethod
    def _rect_signature(rect: QRectF) -> tuple[float, float, float, float]:
        return (
            round(rect.x(), 5), round(rect.y(), 5),
            round(rect.width(), 5), round(rect.height(), 5),
        )

    def _modifier_parameter_signature(self, ids: Iterable[str]) -> tuple[str, ...]:
        result: list[str] = []
        mask_ids: set[str] = set()
        for item in ids:
            modifier = self.chapter.modifiers.get(item)
            if modifier is None:
                continue
            result.append(json.dumps(
                modifier.to_dict(), sort_keys=True, separators=(",", ":"),
            ))
            mask_ids.update(
                binding.mask_id
                for binding in modifier.parameter_masks.values()
            )
        result.extend(
            repr(self._tone_mask_signature(mask_id))
            for mask_id in sorted(mask_ids)
        )
        return tuple(result)

    def _tone_mask_signature(
        self, mask_id: str, _stack: frozenset[str] = frozenset(),
        *, include_paint: bool = True,
    ) -> tuple:
        mask = self.chapter.masks.get(mask_id)
        if mask is None:
            return (mask_id, "missing")
        if mask_id in _stack:
            return (mask_id, "cycle")
        stack = _stack | {mask_id}

        def entity_signature(kind: str, entity_id: str) -> tuple:
            entity = self.chapter.mask_contributor(kind, entity_id)
            if entity is None:
                return kind, entity_id, "missing"
            pixels: tuple = ()
            if isinstance(entity, RasterObject):
                pixels = tuple(sorted(
                    (key, int(image.cacheKey()))
                    for key, image in self.tiles.object_tiles(
                        entity.object_id
                    ).items()
                ))
            elif isinstance(entity, ImageObject):
                pixels = (int(self.images.image(entity.object_id).cacheKey()),)
            elif (
                isinstance(entity, VectorDrawingObject)
                and entity.object_id == self.selected_object_id
            ):
                pixels = (
                    self._vector_eraser_preview_revision,
                    tuple(sorted(
                        (key, int(image.cacheKey()))
                        for key, image in self._vector_preview_tiles.object_tiles(
                            self._vector_preview_id
                        ).items()
                    )),
                )
            children: tuple = ()
            if isinstance(entity, LayerNode):
                children = tuple(
                    entity_signature(child.kind, child.entity_id)
                    for child in entity.children
                )
                ancestor_layers = self.chapter.ancestor_layers(
                    entity.layer_id
                )[:-1]
            else:
                ancestor_layers = self.chapter.ancestor_layers(
                    entity.parent_layer_id
                )
            ancestors = tuple(
                json.dumps(layer.to_dict(), sort_keys=True)
                for layer in ancestor_layers
            )
            dependent_mask_ids: set[str] = set()
            if entity.opacity_mask is not None:
                dependent_mask_ids.add(entity.opacity_mask.mask_id)
            for modifier_id in entity.modifier_ids:
                modifier = self.chapter.modifiers.get(modifier_id)
                if modifier is not None:
                    dependent_mask_ids.update(
                        binding.mask_id
                        for binding in modifier.parameter_masks.values()
                    )
            return (
                kind, entity_id,
                json.dumps(entity.to_dict(), sort_keys=True),
                pixels, children, ancestors,
                tuple(
                    self._tone_mask_signature(dependent, stack)
                    for dependent in sorted(dependent_mask_ids)
                ),
            )

        paint = (
            tuple(sorted(
                (key, int(image.cacheKey()))
                for key, image in self.tiles.object_tiles(mask_id).items()
            ))
            if include_paint else ()
        )
        return (
            json.dumps(mask.to_dict(), sort_keys=True), paint,
            tuple(entity_signature(*item) for item in mask.contributors),
        )

    def _ancestor_mask_path(
        self, kind: str, entity_id: str,
    ) -> QPainterPath | None:
        if kind == "object":
            entity = self.chapter.objects.get(entity_id)
            if entity is None:
                return QPainterPath()
            layer_id = entity.parent_layer_id
            layers = self.chapter.ancestor_layers(layer_id)
            direct_ignore = bool(entity.ignore_parent_mask)
        else:
            entity = self.chapter.layers.get(entity_id)
            if entity is None:
                return QPainterPath()
            layers = self.chapter.ancestor_layers(entity_id)
            layers = layers[:-1]
            direct_ignore = bool(entity.ignore_parent_mask)
        skipped: set[str] = set()
        if direct_ignore and layers:
            skipped.add(layers[-1].layer_id)
        chain_id = (
            entity.parent_id if kind == "layer" else layer_id
        )
        full_chain = (
            self.chapter.ancestor_layers(chain_id) if chain_id else []
        )
        for parent, child in zip(full_chain, full_chain[1:]):
            if child.ignore_parent_mask:
                skipped.add(parent.layer_id)
        result: QPainterPath | None = None
        for layer in layers:
            if not layer.visible:
                return QPainterPath()
            if layer.bound is None or layer.layer_id in skipped:
                continue
            path = self.layer_world_transform(layer.layer_id).map(
                self.layer_effective_path(layer.layer_id)
            )
            result = path if result is None else result.intersected(path)
        return result

    def _render_base_mask_contributor(
        self, painter: QPainter, kind: str, entity_id: str,
        visible_world: QRectF,
    ) -> None:
        entity = self.chapter.mask_contributor(kind, entity_id)
        if entity is None or not entity.visible:
            return
        ancestors = (
            self.chapter.ancestor_layers(entity.parent_layer_id)
            if kind == "object"
            else self.chapter.ancestor_layers(entity_id)[:-1]
        )
        if any(not layer.visible for layer in ancestors):
            return
        painter.save()
        clip = self._ancestor_mask_path(kind, entity_id)
        if clip is not None:
            if clip.isEmpty():
                painter.restore()
                return
            painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
        self._rendering_mask_contributor += 1
        self._suppress_outline_for_mask = True
        try:
            if kind == "object":
                parent_transform = self.layer_world_transform(
                    entity.parent_layer_id
                )
                inverse, valid = parent_transform.inverted()
                painter.setTransform(parent_transform, True)
                ancestor_opacity = 1.0
                for ancestor in self.chapter.ancestor_layers(
                    entity.parent_layer_id
                ):
                    ancestor_opacity *= ancestor.opacity
                self._render_object(
                    painter, entity, ancestor_opacity,
                    inverse.mapRect(visible_world) if valid else visible_world,
                )
                if isinstance(entity, VectorDrawingObject):
                    self._render_modified_vector_pencil_preview(
                        painter, entity.parent_layer_id
                    )
                return
            layer = entity
            parent_transform = (
                self.layer_world_transform(layer.parent_id)
                if layer.parent_id else QTransform()
            )
            inverse, valid = parent_transform.inverted()
            painter.setTransform(parent_transform, True)
            ancestor_opacity = 1.0
            if layer.parent_id:
                for ancestor in self.chapter.ancestor_layers(layer.parent_id):
                    ancestor_opacity *= ancestor.opacity
            self._render_layer(
                painter, layer, ancestor_opacity,
                inverse.mapRect(visible_world) if valid else visible_world,
            )
        finally:
            self._rendering_mask_contributor -= 1
            self._suppress_outline_for_mask = False
            painter.restore()

    @staticmethod
    def _image_alpha_array(image: QImage) -> np.ndarray:
        converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
        view = np.frombuffer(
            converted.constBits(), dtype=np.uint8,
            count=converted.sizeInBytes(),
        ).reshape(converted.height(), converted.bytesPerLine())
        return (
            view[:, :converted.width() * 4]
            .reshape(converted.height(), converted.width(), 4)[..., 3]
            .astype(np.float32) / 255.0
        )

    def render_tone_mask_field(
        self, mask_id: str, width: int, height: int,
        world_to_image: QTransform, visible_world: QRectF,
        *, include_paint: bool = True,
    ) -> np.ndarray:
        mask = self.chapter.masks.get(mask_id) if self.chapter else None
        width, height = max(1, int(width)), max(1, int(height))
        if mask is None:
            return np.zeros((height, width), dtype=np.float32)
        transform_signature = tuple(round(value, 6) for value in (
            world_to_image.m11(), world_to_image.m12(), world_to_image.m13(),
            world_to_image.m21(), world_to_image.m22(), world_to_image.m23(),
            world_to_image.m31(), world_to_image.m32(), world_to_image.m33(),
        ))
        contributor_key = (
            mask_id, width, height, transform_signature,
            self._rect_signature(visible_world),
            self._tone_mask_signature(mask_id, include_paint=False),
        )
        cached = self._tone_mask_contributor_cache.pop(
            contributor_key, None
        )
        if cached is None:
            result = np.zeros((height, width), dtype=np.float32)
            for kind, entity_id in mask.contributors:
                image = QImage(
                    width, height, QImage.Format.Format_ARGB32_Premultiplied
                )
                image.fill(Qt.GlobalColor.transparent)
                painter = QPainter(image)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setRenderHint(
                    QPainter.RenderHint.SmoothPixmapTransform, True
                )
                painter.setTransform(world_to_image)
                self._render_base_mask_contributor(
                    painter, kind, entity_id, visible_world
                )
                painter.end()
                result += self._image_alpha_array(image)
            result = np.clip(result, 0.0, 1.0)
            size = int(result.nbytes)
            if 0 < size <= self._tone_mask_contributor_cache_budget:
                self._tone_mask_contributor_cache[
                    contributor_key
                ] = result.copy()
                self._tone_mask_contributor_cache_bytes += size
                while (
                    self._tone_mask_contributor_cache
                    and self._tone_mask_contributor_cache_bytes
                    > self._tone_mask_contributor_cache_budget
                ):
                    _old_key, old = (
                        self._tone_mask_contributor_cache.popitem(last=False)
                    )
                    self._tone_mask_contributor_cache_bytes -= int(old.nbytes)
        else:
            self._tone_mask_contributor_cache[contributor_key] = cached
            result = cached.copy()
        if include_paint:
            paint = QImage(
                width, height, QImage.Format.Format_ARGB32_Premultiplied
            )
            paint.fill(Qt.GlobalColor.transparent)
            painter = QPainter(paint)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setTransform(world_to_image)
            for (tile_x, tile_y), tile in self.tiles.iter_tiles(
                mask_id, visible_world
            ):
                painter.drawImage(
                    tile_x * self.tiles.tile_size,
                    tile_y * self.tiles.tile_size,
                    tile,
                )
            painter.end()
            result += self._image_alpha_array(paint)
        return np.clip(result, 0.0, 1.0)

    @staticmethod
    def _world_to_image_transform(
        parent_transform: QTransform, bounds: QRectF,
        width: int, height: int,
    ) -> QTransform:
        world = parent_transform.map(QPolygonF([
            bounds.topLeft(), bounds.topRight(),
            bounds.bottomRight(), bounds.bottomLeft(),
        ]))
        destination = QPolygonF([
            QPointF(0, 0), QPointF(width, 0),
            QPointF(width, height), QPointF(0, height),
        ])
        result = QTransform.quadToQuad(world, destination)
        return result if isinstance(result, QTransform) else QTransform()

    def _modifier_mask_fields(
        self, modifiers, width: int, height: int,
        world_to_image: QTransform, visible_world: QRectF,
    ) -> dict[tuple[str, str], np.ndarray]:
        result: dict[tuple[str, str], np.ndarray] = {}
        rendered: dict[str, np.ndarray] = {}
        for modifier in modifiers:
            for attribute, binding in modifier.parameter_masks.items():
                field = rendered.get(binding.mask_id)
                if field is None:
                    field = self.render_tone_mask_field(
                        binding.mask_id, width, height,
                        world_to_image, visible_world,
                    )
                    rendered[binding.mask_id] = field
                result[(modifier.modifier_id, attribute)] = field
        return result

    @staticmethod
    def _modifier_maximum(modifier, attribute: str, fallback: float) -> float:
        binding = modifier.parameter_masks.get(attribute)
        return max(
            float(fallback),
            float(binding.black_value) if binding is not None else fallback,
            float(binding.white_value) if binding is not None else fallback,
        )

    def _modifier_object_signature(self, obj: DocumentObject) -> tuple:
        pixels: tuple = ()
        if isinstance(obj, RasterObject):
            pixels = tuple(sorted(
                (key, int(image.cacheKey()))
                for key, image in self.tiles.object_tiles(
                    obj.object_id
                ).items()
            ))
        elif isinstance(obj, ImageObject):
            pixels = (int(self.images.image(obj.object_id).cacheKey()),)
        live = ()
        if obj.object_id == self.selected_object_id:
            selection_preview = ()
            if (
                isinstance(obj, RasterObject)
                and self._selection_before_tiles is not None
                and self._selection_transform_start_quad
                and self._selection_transform_quad
                and not self._drawing_selection_path.isEmpty()
            ):
                selection_preview = (
                    self._gradient_path_signature(
                        self._drawing_selection_path
                    ),
                    tuple(self._selection_transform_start_quad),
                    tuple(self._selection_transform_quad),
                )
            live = (
                self._vector_eraser_preview_revision,
                tuple(sorted(
                    (key, int(image.cacheKey()))
                    for key, image in self._vector_preview_tiles.object_tiles(
                        self._vector_preview_id
                    ).items()
                )),
                tuple(self._multi_transform_preview_quads.get(
                    obj.object_id, ()
                )),
                selection_preview,
            )
        return (
            json.dumps(
                obj.to_dict(), sort_keys=True, separators=(",", ":")
            ),
            self._modifier_parameter_signature(obj.modifier_ids),
            self._tone_mask_signature(obj.opacity_mask.mask_id)
            if obj.opacity_mask is not None else (),
            pixels, live,
        )

    def _modifier_layer_signature(self, layer_id: str) -> tuple:
        layer = self.chapter.layers[layer_id]
        children = []
        for reference in layer.children:
            if reference.kind == "layer":
                children.append(self._modifier_layer_signature(
                    reference.entity_id
                ))
            else:
                children.append(self._modifier_object_signature(
                    self.chapter.objects[reference.entity_id]
                ))
        preview = (
            tuple(self._transform_preview_quad or ())
            if self._geometry_transform_target == ("layer_group", layer_id)
            else ()
        )
        return (
            json.dumps(
                layer.to_dict(), sort_keys=True, separators=(",", ":")
            ),
            self._modifier_parameter_signature(layer.modifier_ids),
            self._tone_mask_signature(layer.opacity_mask.mask_id)
            if layer.opacity_mask is not None else (),
            tuple(children), preview,
        )

    def _modifier_cache_get(self, key: tuple) -> QImage | None:
        image = self._modifier_render_cache.pop(key, None)
        if image is None:
            return None
        self._modifier_render_cache[key] = image
        return QImage(image)

    def _modifier_cache_put(self, key: tuple, image: QImage) -> None:
        size = int(image.sizeInBytes())
        if size <= 0 or size > self._modifier_render_cache_budget:
            return
        previous = self._modifier_render_cache.pop(key, None)
        if previous is not None:
            self._modifier_render_cache_bytes -= int(previous.sizeInBytes())
        self._modifier_render_cache[key] = QImage(image)
        self._modifier_render_cache_bytes += size
        while (
            self._modifier_render_cache
            and self._modifier_render_cache_bytes
            > self._modifier_render_cache_budget
        ):
            _old_key, old_image = self._modifier_render_cache.popitem(
                last=False
            )
            self._modifier_render_cache_bytes -= int(old_image.sizeInBytes())

    def _modifier_source_cache_get(self, key: tuple) -> QImage | None:
        image = self._modifier_source_cache.pop(key, None)
        if image is None:
            return None
        self._modifier_source_cache[key] = image
        return QImage(image)

    def _modifier_source_cache_put(self, key: tuple, image: QImage) -> None:
        size = int(image.sizeInBytes())
        if size <= 0 or size > self._modifier_source_cache_budget:
            return
        previous = self._modifier_source_cache.pop(key, None)
        if previous is not None:
            self._modifier_source_cache_bytes -= int(previous.sizeInBytes())
        self._modifier_source_cache[key] = QImage(image)
        self._modifier_source_cache_bytes += size
        while (
            self._modifier_source_cache
            and self._modifier_source_cache_bytes
            > self._modifier_source_cache_budget
        ):
            _old_key, old = self._modifier_source_cache.popitem(last=False)
            self._modifier_source_cache_bytes -= int(old.sizeInBytes())

    def _render_modified_object(
        self, painter: QPainter, obj: DocumentObject,
        parent_opacity: float, local_visible: QRectF,
    ) -> None:
        modifiers = [
            self.chapter.modifiers[item] for item in obj.modifier_ids
            if item in self.chapter.modifiers
        ]
        if getattr(self, "_suppress_outline_for_mask", False):
            modifiers = [m for m in modifiers if not isinstance(m, OutlineModifier)]
        world_bounds = self.object_world_rect(obj.object_id)
        if isinstance(obj, RasterObject):
            preview_bounds = self._raster_selection_preview_world_bounds(obj)
            if preview_bounds is not None:
                world_bounds = (
                    preview_bounds
                    if world_bounds is None else world_bounds.united(
                        preview_bounds
                    )
                )
        if (
            world_bounds is None or world_bounds.isEmpty()
            or (not modifiers and obj.opacity_mask is None)
        ):
            self._render_modifier_sources.add(("object", obj.object_id))
            try:
                self._render_object(painter, obj, parent_opacity, local_visible)
            finally:
                self._render_modifier_sources.discard(("object", obj.object_id))
            return
        layer_transform = self.layer_world_transform(obj.parent_layer_id)
        layer_inverse, valid = layer_transform.inverted()
        if not valid:
            return
        local = layer_inverse.mapRect(world_bounds)
        expansion = max((
            self._modifier_maximum(
                modifier, "strength", modifier.strength
            ) * 3.0
            if isinstance(modifier, BlurModifier)
            else 100.0
            if isinstance(modifier, OutlineModifier)
            else 0.0
            for modifier in modifiers
        ), default=0.0)
        local.adjust(-expansion, -expansion, expansion, expansion)
        bounds = QRectF(
            math.floor(local.left()), math.floor(local.top()),
            max(1, math.ceil(local.right()) - math.floor(local.left())),
            max(1, math.ceil(local.bottom()) - math.floor(local.top())),
        )
        world_origin = layer_transform.map(bounds.topLeft())
        object_signature = self._modifier_object_signature(obj)
        cache_key = (
            "object", obj.object_id,
            object_signature,
            self._rect_signature(bounds), world_origin.toTuple(),
        )
        processed = self._modifier_cache_get(cache_key)
        if processed is None:
            source_key = (
                "object-source", obj.object_id,
                object_signature[0], object_signature[3], object_signature[4],
                self._rect_signature(bounds), world_origin.toTuple(),
            )
            image = self._modifier_source_cache_get(source_key)
            if image is None:
                image = QImage(
                    max(1, math.ceil(bounds.width())),
                    max(1, math.ceil(bounds.height())),
                    QImage.Format.Format_ARGB32_Premultiplied,
                )
                image.fill(Qt.GlobalColor.transparent)
                source = QPainter(image)
                source.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                source.translate(-bounds.left(), -bounds.top())
                self._render_modifier_sources.add(("object", obj.object_id))
                try:
                    self._render_object_content(source, obj, bounds)
                    if isinstance(obj, VectorDrawingObject):
                        self._render_modified_vector_pencil_preview(
                            source, obj.parent_layer_id
                        )
                finally:
                    self._render_modifier_sources.discard(
                        ("object", obj.object_id)
                    )
                    source.end()
                self._modifier_source_cache_put(source_key, image)
            width, height = image.width(), image.height()
            world_to_image = self._world_to_image_transform(
                layer_transform, bounds, width, height
            )
            processed = apply_modifier_stack(
                image, modifiers, world_origin.toTuple(),
                self._modifier_mask_fields(
                    modifiers, width, height,
                    world_to_image, world_bounds,
                ),
                outline_distance_cache=self._outline_distance_cache,
                blur_pyramid_cache=self._blur_pyramid_cache,
            )
            if obj.opacity_mask is not None:
                binding = obj.opacity_mask
                processed = apply_opacity_mask(
                    processed,
                    self.render_tone_mask_field(
                        binding.mask_id, width, height,
                        world_to_image, world_bounds,
                    ),
                    binding.black_value, binding.white_value,
                )
            self._modifier_cache_put(cache_key, processed)
        opacity = parent_opacity if self._render_base_alpha else (
            parent_opacity
            if obj.opacity_locked else parent_opacity * obj.opacity
        )
        if obj.object_id == self._live_underlay_object_id:
            opacity *= 1.0 - self._live_underlay_amount
        painter.save()
        painter.setOpacity(opacity)
        painter.drawImage(bounds.topLeft(), processed)
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
        destination = (
            list(self._multi_transform_preview_quads[obj.object_id])
            if obj.object_id in self._multi_transform_preview_quads
            else list(self._transform_preview_quad) if preview
            else list(obj.transform_quad) if obj.transform_quad is not None
            else None
        )
        if destination is not None:
            transform = self._drawing_object_transform(obj, destination)
            painter.save()
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.setTransform(transform, True)
            object_visible = self._drawing_local_visible_rect(
                obj, local_visible, destination
            )
            painter.translate(obj.x, obj.y)
            if self._render_raster_selection_preview(
                painter, obj, object_visible
            ):
                painter.restore()
                return
            if object_visible is not None:
                object_visible = object_visible.intersected(
                    QRectF(*obj.interaction_rect)
                )
            for (tile_x, tile_y), image in self.tiles.iter_tiles(
                obj.object_id, object_visible
            ):
                painter.drawImage(
                    tile_x * obj.tile_size,
                    tile_y * obj.tile_size,
                    image,
                )
            painter.restore()
            return
        painter.translate(obj.x, obj.y)
        object_visible = local_visible.translated(-obj.x, -obj.y)
        if self._render_raster_selection_preview(
            painter, obj, object_visible
        ):
            return
        for (tile_x, tile_y), image in self.tiles.iter_tiles(
            obj.object_id, object_visible
        ):
            painter.drawImage(
                tile_x * obj.tile_size, tile_y * obj.tile_size, image
            )

    def _raster_selection_preview_state(
        self, obj: RasterObject,
    ) -> tuple[
        dict[tuple[int, int], QImage], QPainterPath, QTransform,
    ] | None:
        before_tiles = self._selection_before_tiles
        source_quad = self._selection_transform_start_quad
        destination_quad = self._selection_transform_quad
        if (
            obj.object_id != self.selected_object_id
            or before_tiles is None
            or not source_quad
            or not destination_quad
            or self._drawing_selection_path.isEmpty()
        ):
            return None
        local_to_world = self._drawing_local_to_world_transform(obj)
        world_to_local, valid = local_to_world.inverted()
        if not valid:
            return None
        source_local = [
            world_to_local.map(QPointF(x, y)).toTuple()
            for x, y in source_quad
        ]
        destination_local = [
            world_to_local.map(QPointF(x, y)).toTuple()
            for x, y in destination_quad
        ]
        transform = self._quad_to_quad_transform(
            source_local, destination_local
        )
        if not transform.isInvertible():
            return None
        return before_tiles, QPainterPath(self._drawing_selection_path), transform

    @staticmethod
    def _tile_mapping_bounds(
        tiles: dict[tuple[int, int], QImage], tile_size: int,
    ) -> QRectF:
        bounds = QRectF()
        first = True
        for tile_x, tile_y in tiles:
            tile = QRectF(
                tile_x * tile_size, tile_y * tile_size,
                tile_size, tile_size,
            )
            bounds = tile if first else bounds.united(tile)
            first = False
        return bounds

    @staticmethod
    def _draw_tile_mapping(
        painter: QPainter, tiles: dict[tuple[int, int], QImage],
        tile_size: int, visible: QRectF | None,
    ) -> None:
        for (tile_x, tile_y), image in tiles.items():
            target = QRectF(
                tile_x * tile_size, tile_y * tile_size,
                tile_size, tile_size,
            )
            if visible is not None and not target.intersects(visible):
                continue
            painter.drawImage(target.topLeft(), image)

    def _render_raster_selection_preview(
        self, painter: QPainter, obj: RasterObject,
        local_visible: QRectF | None,
    ) -> bool:
        state = self._raster_selection_preview_state(obj)
        if state is None:
            return False
        before_tiles, source_path, transform = state
        tile_bounds = self._tile_mapping_bounds(before_tiles, obj.tile_size)
        if tile_bounds.isEmpty():
            return True

        unselected = QPainterPath()
        unselected.addRect(tile_bounds)
        unselected = unselected.subtracted(source_path)
        painter.save()
        painter.setClipPath(unselected, Qt.ClipOperation.IntersectClip)
        self._draw_tile_mapping(
            painter, before_tiles, obj.tile_size, local_visible
        )
        painter.restore()

        source_visible = None
        if local_visible is not None:
            inverse, valid = transform.inverted()
            if valid:
                source_visible = inverse.mapRect(local_visible)
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setTransform(transform, True)
        painter.setClipPath(source_path, Qt.ClipOperation.IntersectClip)
        self._draw_tile_mapping(
            painter, before_tiles, obj.tile_size, source_visible
        )
        painter.restore()
        return True

    def _raster_selection_preview_world_bounds(
        self, obj: RasterObject,
    ) -> QRectF | None:
        state = self._raster_selection_preview_state(obj)
        if state is None:
            return None
        _tiles, source_path, transform = state
        target_path = transform.map(source_path)
        return self._drawing_local_to_world_transform(obj).map(
            target_path
        ).boundingRect()

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
        reference_world = self.layer_world_transform(
            reference.layer_id
        ).map(path)
        parent_inverse, valid = self.layer_world_transform(
            parent.layer_id
        ).inverted()
        local_bounds = (
            parent_inverse.map(reference_world).boundingRect()
            if valid else bounds
        )
        left, top = local_bounds.left(), local_bounds.top()
        width, height = local_bounds.width(), local_bounds.height()
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
            highlight = QColor("#F2A23A")
            highlight.setAlphaF(0.4)
            selection.format.setBackground(highlight)
            context.selections = [selection]
        document.documentLayout().draw(painter, context)
        if (
            editing and self.hasFocus() and self._text_caret_visible
            and self._text_cursor_position == self._text_selection_anchor
        ):
            caret = self._text_caret_rect(document, self._text_cursor_position)
            pen = QPen(QColor("#111111"), 1)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(caret.topLeft(), caret.bottomLeft())

    def _blink_text_caret(self) -> None:
        if not self._text_editing:
            self._text_caret_timer.stop()
            return
        self._text_caret_visible = not self._text_caret_visible
        self.update()

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
            transform = self.layer_world_transform(layer.layer_id)
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
            radius = 12.0 / max(self.scale, 0.05)
            painter.save()
            painter.setTransform(
                self.layer_world_transform(drawing.parent_layer_id), True
            )
            if drawing.transform_quad is not None:
                painter.setTransform(
                    self._drawing_object_transform(drawing), True
                )
            painter.translate(drawing.x, drawing.y)
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
        if drawing.modifier_ids or any(
            layer.modifier_ids for layer in self.chapter.ancestor_layers(
                drawing.parent_layer_id
            )
        ):
            # The isolated modifier source pass owns this live overlay.
            return
        painter.save()
        parent_transform = self.layer_world_transform(drawing.parent_layer_id)
        painter.setTransform(parent_transform, True)
        parent_inverse, valid = parent_transform.inverted()
        if drawing.transform_quad is not None:
            painter.setTransform(
                self._drawing_object_transform(drawing), True
            )
        painter.translate(drawing.x, drawing.y)
        local_visible = self._drawing_local_visible_rect(
            drawing,
            parent_inverse.mapRect(self.visible_document_rect())
            if valid else self.visible_document_rect(),
        )
        tile_size = self._vector_preview_tiles.tile_size
        for (tile_x, tile_y), image in self._vector_preview_tiles.iter_tiles(
            self._vector_preview_id, local_visible
        ):
            painter.drawImage(tile_x * tile_size, tile_y * tile_size, image)
        painter.restore()

    @staticmethod
    def _rect_from_quad(quad: list[tuple[float, float]]) -> QRectF:
        return QPolygonF([QPointF(*point) for point in quad]).boundingRect()

    def _image_fit_quad(self, obj: ImageObject) -> list[tuple[float, float]]:
        parent = self.chapter.layers.get(obj.parent_layer_id)
        if parent is None or parent.bound is None:
            return self._rect_quad(QRectF(
                obj.x, obj.y, obj.pixel_width, obj.pixel_height
            ))
        bounds = self.layer_effective_path(parent.layer_id).boundingRect()
        width = max(1.0, bounds.width())
        height = max(1.0, bounds.height())
        aspect = obj.pixel_width / max(1.0, obj.pixel_height)
        if obj.fit_mode == "auto_width":
            target_width, target_height = width, width / aspect
        elif obj.fit_mode == "auto_height":
            target_width, target_height = height * aspect, height
        elif obj.fit_mode == "fit_inside":
            scale = min(width / obj.pixel_width, height / obj.pixel_height)
            target_width = obj.pixel_width * scale
            target_height = obj.pixel_height * scale
        else:
            target_width, target_height = width, height
        rect = QRectF(
            bounds.center().x() - target_width / 2,
            bounds.center().y() - target_height / 2,
            target_width, target_height,
        )
        return self._rect_quad(rect)

    def _image_model_local_quad(self, obj: ImageObject) -> list[tuple[float, float]]:
        if obj.placement_mode == "fit_parent":
            return self._image_fit_quad(obj)
        if obj.transform_quad is not None:
            return list(obj.transform_quad)
        return self._rect_quad(QRectF(
            obj.x, obj.y, obj.pixel_width, obj.pixel_height
        ))

    def _image_local_quad(self, obj: ImageObject) -> list[tuple[float, float]]:
        override = self._image_runtime_geometry.get(obj.object_id)
        if override is not None:
            return list(override["quad"])
        return self._image_model_local_quad(obj)

    def set_image_runtime_geometry(
        self, object_id: str, width: int, height: int,
        quad: list[tuple[float, float]],
    ) -> None:
        self._image_runtime_geometry[object_id] = {
            "width": max(1, int(width)), "height": max(1, int(height)),
            "quad": [tuple(point) for point in quad],
        }

    def clear_image_runtime_geometry(self, object_id: str) -> None:
        self._image_runtime_geometry.pop(object_id, None)

    def _render_image_object(self, painter: QPainter, obj: ImageObject) -> None:
        image = self.images.image(obj.object_id)
        if image.isNull() and not obj.is_blender_linked:
            return
        geometry = self._image_runtime_geometry.get(obj.object_id)
        source_width = geometry["width"] if geometry else obj.pixel_width
        source_height = geometry["height"] if geometry else obj.pixel_height
        source = QRectF(0, 0, source_width, source_height)
        destination = self._image_local_quad(obj)
        if obj.object_id in self._multi_transform_preview_quads:
            destination = list(self._multi_transform_preview_quads[obj.object_id])
        if (
            obj.object_id == self.selected_object_id
            and self._transform_preview_quad is not None
            and self._transform_start_quad is not None
        ):
            destination = list(self._transform_preview_quad)
        transform = self._quad_transform(source, destination)
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.setTransform(transform, True)
        if image.isNull():
            painter.fillRect(source, QColor("#322f39"))
            step = max(8.0, min(source.width(), source.height()) / 12.0)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#3e3a47"))
            rows = max(1, math.ceil(source.height() / step))
            columns = max(1, math.ceil(source.width() / step))
            for row in range(rows):
                for column in range(columns):
                    if (row + column) % 2:
                        painter.drawRect(QRectF(
                            column * step, row * step, step, step
                        ))
            painter.setPen(QColor("#d9d4e5"))
            painter.drawText(
                source, Qt.AlignCenter,
                "Waiting for Blender\nComic View",
            )
        else:
            painter.drawImage(source, image)
        painter.restore()

    def _render_modified_vector_pencil_preview(
        self, painter: QPainter, coordinate_parent_id: str,
    ) -> None:
        drawing = self._active_vector_drawing()
        if (
            drawing is None or self._vector_gesture_mode != "pencil"
            or not self._vector_samples
        ):
            return
        if coordinate_parent_id != drawing.parent_layer_id:
            ancestors = {
                layer.layer_id for layer in self.chapter.ancestor_layers(
                    drawing.parent_layer_id
                )
            }
            if coordinate_parent_id not in ancestors:
                return
        coordinate_transform = (
            self.layer_world_transform(coordinate_parent_id)
            if coordinate_parent_id else QTransform()
        )
        coordinate_inverse, valid = coordinate_transform.inverted()
        if not valid:
            return
        relative = self.layer_world_transform(
            drawing.parent_layer_id
        ) * coordinate_inverse
        painter.save()
        painter.setTransform(relative, True)
        if drawing.transform_quad is not None:
            painter.setTransform(
                self._drawing_object_transform(drawing), True
            )
        painter.translate(drawing.x, drawing.y)
        tile_size = self._vector_preview_tiles.tile_size
        for (tile_x, tile_y), image in self._vector_preview_tiles.iter_tiles(
            self._vector_preview_id, None
        ):
            painter.drawImage(tile_x * tile_size, tile_y * tile_size, image)
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
            ToolKind.DRAW_SHAPE,
            ToolKind.FILL,
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
            painter.save()
            painter.setTransform(
                self.layer_world_transform(active.layer_id), True
            )
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
                if self.tool == ToolKind.SHAPE_EDIT and layer.bound is not None:
                    self._draw_shape_overlay(painter)
                if layer.bound is not None:
                    painter.save()
                    painter.setTransform(
                        self.layer_world_transform(layer.layer_id), True
                    )
                    painter.drawPath(self.layer_effective_path(layer.layer_id))
                    painter.restore()
                if (
                    self.tool in {ToolKind.SHAPE_EDIT, ToolKind.TRANSFORM}
                    and layer.bound is not None
                ):
                    cage = self._active_transform_cage()
                    if cage is not None:
                        self._draw_transform_controls(
                            painter, cage[0], self._transform_pivot,
                            use_global_pivot=True,
                        )
                if self.tool == ToolKind.BOUND_EDIT and layer.bound is not None:
                    painter.save()
                    painter.setTransform(
                        self.layer_world_transform(layer.layer_id), True
                    )
                    self._draw_shape_edit_handles(
                        painter, layer.bound, layer.shape_style
                    )
                    painter.restore()
        else:
            if len(self.selected_entities) > 1:
                painter.save()
                painter.setPen(QPen(
                    QColor("#7fd2ff"),
                    1.3 / max(self.scale, 0.05), Qt.PenStyle.DotLine,
                ))
                for kind, entity_id in self.selected_entities:
                    if kind != "object":
                        continue
                    local_preview = self._multi_transform_preview_quads.get(
                        entity_id
                    )
                    if local_preview is not None:
                        obj = self.chapter.objects.get(entity_id)
                        transform = self.layer_world_transform(
                            obj.parent_layer_id
                        )
                        outline = [
                            transform.map(QPointF(*value))
                            for value in local_preview
                        ]
                    else:
                        outline = [
                            QPointF(*value) for value in (
                                self.object_world_quad(entity_id) or []
                            )
                        ]
                    if outline:
                        painter.drawPolygon(QPolygonF(outline))
                painter.restore()
            quad = self._selected_world_quad()
            if quad:
                polygon = QPolygonF([QPointF(*point) for point in quad])
                live_vector_eraser = (
                    self._vector_gesture_mode == "eraser"
                    and isinstance(selected_object, VectorDrawingObject)
                )
                # A zero-height vector bounds cage can lie directly over the
                # ink.  During erasing it would redraw the committed stroke in
                # blue and conceal the otherwise-correct live cutout.
                if not live_vector_eraser:
                    painter.drawPolygon(polygon)
                if (
                    not live_vector_eraser
                    and not self._is_two_point_line_gradient(selected_object)
                    and not (
                        isinstance(selected_object, ImageObject)
                        and selected_object.placement_mode == "fit_parent"
                    )
                    and (
                        self._object_transform_cage_visible(selected_object)
                        or self.tool == ToolKind.TRANSFORM
                        or (
                            self.tool in {ToolKind.SHAPE_EDIT, ToolKind.GRADIENT}
                            and isinstance(selected_object, GradientObject)
                            and selected_object.field_type in {"line", "radial"}
                        )
                        or (
                            self.tool == ToolKind.TEXT_EDIT
                            and isinstance(selected_object, TextObject)
                            and selected_object.layout_mode == "free"
                        )
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
                    self.tool in {ToolKind.SHAPE_EDIT, ToolKind.GRADIENT}
                    and isinstance(selected_object, GradientObject)
                ):
                    self._draw_gradient_edit_handles(
                        painter, selected_object
                    )
                if (
                    self.tool == ToolKind.SHAPE_EDIT
                    and isinstance(selected_object, SpeedLineCenterObject)
                ):
                    painter.save()
                    painter.setTransform(
                        self.layer_world_transform(
                            selected_object.parent_layer_id
                        ), True
                    )
                    self._draw_shape_edit_handles(
                        painter, selected_object.geometry
                    )
                    painter.restore()
                if isinstance(selected_object, TextObject):
                    self._draw_text_property_handles(painter)
        painter.restore()
        self._draw_transform_mode_gizmo(painter)

    def _draw_text_property_handles(self, painter: QPainter) -> None:
        positions = self._text_property_handle_positions()
        if not positions:
            return
        scale = max(self.scale, 0.05)
        radius = 14 / scale
        drag = self._text_property_drag
        painter.save()
        painter.setPen(QPen(QColor("#f2a23a"), 4 / scale))
        painter.setBrush(QColor("#f2a23a"))
        font = painter.font()
        font.setPixelSize(max(1, round(20 / scale)))
        font.setBold(True)
        painter.setFont(font)
        for key, anchor in positions.items():
            center = QPointF(anchor)
            if drag is not None and drag["key"] == key:
                anchor_widget = self.document_to_widget(anchor)
                center = self.widget_to_document(QPointF(
                    drag["current_x"], anchor_widget.y()
                ))
                painter.drawLine(anchor, center)
            painter.drawEllipse(center, radius, radius)
            painter.setPen(QPen(QColor("#4a3212"), 2 / scale))
            painter.drawText(
                QRectF(
                    center.x() - radius, center.y() - radius,
                    radius * 2, radius * 2,
                ),
                Qt.AlignmentFlag.AlignCenter,
                "S" if key == "font_size" else "K",
            )
            painter.setPen(QPen(QColor("#f2a23a"), 4 / scale))
        painter.restore()

    def _gradient_local_to_world(
        self, obj: GradientObject, point: tuple[float, float],
    ) -> QPointF:
        return self.layer_world_transform(obj.parent_layer_id).map(
            QPointF(*point)
        )

    def _gradient_world_to_local(
        self, obj: GradientObject, point: QPointF,
    ) -> QPointF:
        return self._layer_world_to_local(obj.parent_layer_id, point)

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
            is_speed = isinstance(obj, SpeedLinesGradientObject)
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
            if is_speed and len(geometry.nodes) >= 2:
                path = self.bound_path(geometry)
                percent = path.percentAtLength(path.length() / 2)
                midpoint = path.pointAtPercent(percent)
                angle = math.radians(-path.angleAtPercent(percent) + 90)
                result["direction:"] = self._gradient_local_to_world(
                    obj,
                    (
                        midpoint.x() - math.cos(angle) * 60 / self.scale,
                        midpoint.y() - math.sin(angle) * 60 / self.scale,
                    ),
                )
                if obj.line_field.direction_mode == "parallel":
                    target_percent = (
                        0.0 if obj.line_field.reverse_direction else 1.0
                    )
                    target = path.pointAtPercent(target_percent)
                    tangent_angle = math.radians(
                        -path.angleAtPercent(target_percent)
                    )
                    sign = -1.0 if obj.line_field.reverse_direction else 1.0
                    result["flow:"] = self._gradient_local_to_world(
                        obj,
                        (
                            target.x() + math.cos(tangent_angle) * sign * 28 / self.scale,
                            target.y() + math.sin(tangent_angle) * sign * 28 / self.scale,
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
        if isinstance(obj, SpeedLinesGradientObject):
            center = self.chapter.speed_center_for(obj.object_id)
            if center is not None:
                painter.save()
                painter.setTransform(
                    self.layer_world_transform(center.parent_layer_id), True
                )
                painter.setPen(QPen(
                    QColor("#9BDDF0"), 1.5 / scale, Qt.DashLine
                ))
                painter.drawPath(self.bound_path(center.geometry))
                painter.restore()
                painter.setPen(QPen(QColor("#ff9f22"), 2 / scale))
        if obj.field_type == "line":
            painter.save()
            painter.setTransform(
                self.layer_world_transform(obj.parent_layer_id), True
            )
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
            for key in ("distance:", "direction:", "flow:"):
                point = controls.get(key)
                if point is None:
                    continue
                if key == "direction:":
                    badge = QRectF(
                        point.x() - action_radius * 3.0,
                        point.y() - action_radius,
                        action_radius * 6.0, action_radius * 2.0,
                    )
                    painter.drawRoundedRect(
                        badge, action_radius / 2, action_radius / 2
                    )
                    painter.drawText(
                        badge, Qt.AlignmentFlag.AlignCenter,
                        "Perpendicular"
                        if obj.line_field.direction_mode == "perpendicular"
                        else "Parallel",
                    )
                elif key == "flow:":
                    diamond = QPolygonF([
                        point + QPointF(0, -action_radius),
                        point + QPointF(action_radius, 0),
                        point + QPointF(0, action_radius),
                        point + QPointF(-action_radius, 0),
                    ])
                    painter.drawPolygon(diamond)
                else:
                    painter.drawEllipse(point, action_radius, action_radius)
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
            elif key == "direction:":
                badge = QRectF(
                    point.x() - radius * 2.6, point.y() - radius,
                    radius * 5.2, radius * 2,
                )
                painter.drawRoundedRect(
                    badge, radius / 2, radius / 2
                )
                painter.drawText(
                    badge, Qt.AlignmentFlag.AlignCenter,
                    "Perpendicular"
                    if obj.line_field.direction_mode == "perpendicular"
                    else "Parallel",
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
            "direction": 0, "flow": 0, "distance": 2, "insert": 5,
        }
        hits: list[tuple[int, float, str, str]] = []
        for key, candidate in controls.items():
            kind, node_id = key.split(":", 1)
            distance = math.dist(
                point.toTuple(), candidate.toTuple()
            )
            if kind in {"type", "direction"}:
                width = 64 if kind == "type" else 92
                badge_hit = QRectF(
                    candidate.x() - width / 2 / max(self.scale, 0.05),
                    candidate.y() - 14 / max(self.scale, 0.05),
                    width / max(self.scale, 0.05),
                    28 / max(self.scale, 0.05),
                )
                if badge_hit.contains(point):
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
        if kind == "direction":
            before = self.chapter.to_dict()
            obj.line_field.direction_mode = (
                "perpendicular"
                if obj.line_field.direction_mode == "parallel"
                else "parallel"
            )
            obj.line_field.validate()
            obj.touch_revision()
            self._push_immediate_shape_change(
                before, "Toggle speed lines direction"
            )
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
            "flow": "Drag to the other endpoint to reverse the speed lines",
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
            if kind == "flow":
                path = self.bound_path(obj.line_field.geometry)
                start = path.pointAtPercent(0.0)
                end = path.pointAtPercent(1.0)
                obj.line_field.reverse_direction = (
                    math.dist(local.toTuple(), start.toTuple())
                    < math.dist(local.toTuple(), end.toTuple())
                )
                obj.line_field.validate()
                obj.touch_revision()
                self.documentChanged.emit(QRectF())
                self.update()
                return
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

    def _active_transform_cage(
        self,
    ) -> tuple[list[tuple[float, float]], str] | None:
        """Return the visible eight-handle cage in world coordinates."""
        if self.chapter is None:
            return None
        if self.tool in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
        } and self._selection_transform_quad:
            return list(self._selection_transform_quad), "selection"
        if self.selected_kind == "layer" and self.tool in {
            ToolKind.SHAPE_EDIT, ToolKind.TRANSFORM,
        }:
            layer = self.chapter.layers.get(self.selected_id)
            if layer is None or layer.bound is None:
                return None
            if (
                self._geometry_transform_target in {
                    ("layer", layer.layer_id),
                    ("layer_group", layer.layer_id),
                }
                and self._transform_preview_quad is not None
            ):
                local_quad = self._transform_preview_quad
                transform = (
                    self.layer_world_transform(layer.parent_id)
                    if self._geometry_transform_target[0] == "layer_group"
                    and layer.parent_id else
                    QTransform()
                    if self._geometry_transform_target[0] == "layer_group"
                    else self.layer_world_transform(layer.layer_id)
                )
            else:
                left, top, width, height = layer.bound.bbox()
                local_quad = self._rect_quad(QRectF(
                    left, top, max(1.0, width), max(1.0, height)
                ))
                transform = self.layer_world_transform(layer.layer_id)
            return [
                transform.map(QPointF(x, y)).toTuple()
                for x, y in local_quad
            ], "object"
        if len(self.selected_entities) > 1 and self.tool == ToolKind.TRANSFORM:
            quad = (
                self._transform_preview_quad
                if self._geometry_transform_target == ("multi", "")
                and self._transform_preview_quad is not None
                else self._multi_selection_cage()
            )
            return (list(quad), "object") if quad else None
        if self.selected_kind != "object" or not self.selected_object_id:
            return None
        obj = self.chapter.objects.get(self.selected_object_id)
        if isinstance(obj, ImageObject) and obj.placement_mode == "fit_parent":
            return None
        if self._is_two_point_line_gradient(obj):
            return None
        visible = (
            self._object_transform_cage_visible(obj)
            or self.tool == ToolKind.TRANSFORM
            or (
                self.tool == ToolKind.TEXT_EDIT
                and isinstance(obj, TextObject)
                and obj.layout_mode == "free"
            )
            or (
                self.tool in {ToolKind.SHAPE_EDIT, ToolKind.GRADIENT}
                and isinstance(obj, GradientObject)
                and obj.field_type in {"line", "radial"}
            )
        )
        quad = self._selected_world_quad() if visible else None
        return (list(quad), "object") if quad else None

    def _transform_mode_gizmo_rect(self) -> QRectF:
        cage = self._active_transform_cage()
        if cage is None:
            self._transform_gizmo_key = None
            self._transform_gizmo_slot = None
            return QRectF()
        quad, pivot_kind = cage
        context_key = (
            id(self.chapter), self.selected_kind, self.selected_id,
            self.tool.value, pivot_kind,
        )
        if context_key != self._transform_gizmo_key:
            self._transform_gizmo_key = context_key
            self._transform_gizmo_slot = None
        widget_quad = self.camera_transform().map(QPolygonF([
            QPointF(*point) for point in quad
        ]))
        bounds = widget_quad.boundingRect()
        width, height, gap = 86.0, 30.0, 8.0
        candidates = [
            QRectF(bounds.left(), bounds.top() - height - gap, width, height),
            QRectF(bounds.right() - width, bounds.top() - height - gap, width, height),
            QRectF(bounds.left(), bounds.bottom() + gap, width, height),
            QRectF(bounds.right() - width, bounds.bottom() + gap, width, height),
        ]
        pivot = (
            self._selection_pivot if pivot_kind == "selection"
            else self._transform_pivot
        )
        handles, rotate, pivot = self._transform_control_points(quad, pivot)
        control_points = [
            self.camera_transform().map(QPointF(*point)) for point in handles
        ] + [
            self.camera_transform().map(rotate),
            self.camera_transform().map(pivot),
        ]
        # Typography handles live on the selected text's right edge.  Keep
        # the mode button out of their screen-space hit targets so the two
        # controls remain visually and interactively independent.
        typography_points = [
            self.document_to_widget(point)
            for point in self._text_property_handle_positions().values()
        ]
        control_points.extend(typography_points)
        overlay_rect = QRectF()
        text_overlay = getattr(self, "_text_gizmo_overlay", None)
        if text_overlay is not None and text_overlay.isVisible():
            overlay_rect = QRectF(text_overlay.geometry())
        if typography_points or not overlay_rect.isEmpty():
            candidates.extend([
                QRectF(
                    bounds.left() - width - gap,
                    bounds.center().y() - height / 2,
                    width,
                    height,
                ),
                QRectF(
                    bounds.right() + gap,
                    bounds.center().y() - height / 2,
                    width,
                    height,
                ),
                QRectF(
                    bounds.center().x() - width / 2,
                    bounds.top() - height - gap,
                    width,
                    height,
                ),
                QRectF(
                    bounds.center().x() - width / 2,
                    bounds.bottom() + gap,
                    width,
                    height,
                ),
            ])
        viewport = QRectF(self.rect()).adjusted(6, 6, -6, -6)

        def clamped(rect: QRectF) -> QRectF:
            x = min(max(rect.x(), viewport.left()), viewport.right() - width)
            y = min(max(rect.y(), viewport.top()), viewport.bottom() - height)
            return QRectF(x, y, width, height)

        candidates = [clamped(candidate) for candidate in candidates]

        def overlap_area(first: QRectF, second: QRectF) -> float:
            if not first.intersects(second):
                return 0.0
            overlap = first.intersected(second)
            return max(0.0, overlap.width()) * max(0.0, overlap.height())

        def score(rect: QRectF) -> tuple[int, float, float]:
            collisions = 0
            area = 0.0
            for index, point in enumerate(control_points):
                padding = 30.0 if index >= len(handles) + 2 else 8.0
                expanded = rect.adjusted(
                    -padding, -padding, padding, padding
                )
                if expanded.contains(point):
                    collisions += 1
                    area += overlap_area(rect, expanded)
            if not overlay_rect.isEmpty():
                area += overlap_area(rect, overlay_rect.adjusted(-4, -4, 4, 4))
                if rect.intersects(overlay_rect.adjusted(-4, -4, 4, 4)):
                    collisions += 1
            distance = abs(rect.center().x() - bounds.center().x()) + abs(
                rect.center().y() - bounds.center().y()
            )
            return collisions, area, distance

        if (
            self._transform_gizmo_slot is None
            or self._transform_gizmo_slot >= len(candidates)
        ):
            self._transform_gizmo_slot = min(
                range(len(candidates)), key=lambda index: score(candidates[index])
            )
        return candidates[self._transform_gizmo_slot]

    def _draw_transform_mode_gizmo(self, painter: QPainter) -> None:
        rect = self._transform_mode_gizmo_rect()
        if rect.isEmpty():
            return
        painter.save()
        painter.setTransform(QTransform())
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor("#ffb04a"), 1.5))
        painter.setBrush(QColor(45, 34, 25, 238))
        painter.drawRoundedRect(rect, 7, 7)
        font = painter.font()
        font.setPixelSize(13)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffb04a"))
        painter.drawText(
            rect, Qt.AlignCenter,
            "Uniform" if self.settings.transform_mode == "uniform" else "Free",
        )
        painter.restore()

    def _transform_mode_gizmo_hit(self, widget_point: QPointF) -> bool:
        rect = self._transform_mode_gizmo_rect()
        if rect.isEmpty() or not rect.contains(widget_point):
            return False
        cage = self._active_transform_cage()
        if cage is None:
            return False
        quad, pivot_kind = cage
        world = self.widget_to_document(widget_point)
        pivot = (
            self._selection_pivot if pivot_kind == "selection"
            else self._transform_pivot
        )
        handles, rotate, pivot = self._transform_control_points(quad, pivot)
        tolerance = 14 / max(self.scale, 0.05)
        if any(
            math.dist(world.toTuple(), point) <= tolerance
            for point in handles
        ) or math.dist(world.toTuple(), rotate.toTuple()) <= tolerance \
                or math.dist(world.toTuple(), pivot.toTuple()) <= tolerance:
            return False
        self.settings.transform_mode = (
            "uniform" if self.settings.transform_mode == "free" else "free"
        )
        self.settings.clamp()
        self.transformModeChanged.emit(self.settings.transform_mode)
        self.update()
        return True

    def _reset_transform_pivot_at(self, world: QPointF) -> bool:
        cage = self._active_transform_cage()
        if cage is None:
            return False
        quad, kind = cage
        current = self._selection_pivot if kind == "selection" else self._transform_pivot
        _handles, _rotate, pivot = self._transform_control_points(quad, current)
        if math.dist(world.toTuple(), pivot.toTuple()) > 14 / max(self.scale, 0.05):
            return False
        if kind == "selection":
            self._selection_pivot = None
            self._selection_pivot_custom = False
        else:
            self._transform_pivot = None
            self._transform_pivot_custom = False
        self.update()
        return True

    def _active_transform_hover_kind(self, world: QPointF) -> str:
        cage = self._active_transform_cage()
        if cage is None:
            return ""
        quad, kind = cage
        pivot = self._selection_pivot if kind == "selection" else self._transform_pivot
        handles, rotate, pivot_point = self._transform_control_points(quad, pivot)
        tolerance = 14 / max(self.scale, 0.05)
        if (
            any(math.dist(world.toTuple(), point) <= tolerance for point in handles)
            or math.dist(world.toTuple(), rotate.toTuple()) <= tolerance
            or math.dist(world.toTuple(), pivot_point.toTuple()) <= tolerance
        ):
            return "handle"
        if kind == "selection":
            path = QPainterPath()
            path.addPolygon(QPolygonF([QPointF(*point) for point in quad]))
            stroker = QPainterPathStroker()
            stroker.setWidth(18 / max(self.scale, 0.05))
            return (
                "translate"
                if path.contains(world) or stroker.createStroke(path).contains(world)
                else ""
            )
        if (
            self.tool == ToolKind.TRANSFORM
            and (
                self.selected_kind == "layer"
                or len(self.selected_entities) > 1
            )
        ):
            return self._transform_control_hit(quad, world)[0]
        obj = self.chapter.objects.get(self.selected_object_id)
        if isinstance(obj, (RasterObject, VectorDrawingObject, ImageObject)):
            return self._selected_object_transform_hit(obj, quad, world)[0]
        if isinstance(obj, TextObject):
            return self._text_transform_control_hit(quad, world)[0]
        return self._raster_transform_control_hit(quad, world)[0]

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
        pivot_override: QPointF | None = None,
        *, use_global_pivot: bool = True,
    ) -> None:
        handles, rotate, pivot = self._transform_control_points(
            quad,
            (
                self._transform_pivot
                if use_global_pivot and pivot_override is None
                else pivot_override
            ),
        )
        radius = 7 / max(self.scale, 0.05)
        painter.setBrush(QColor("#f5f5f5"))
        for point in handles:
            painter.drawEllipse(QPointF(*point), radius, radius)
        top = QPointF(*self._edge_midpoints(quad)[0])
        painter.drawLine(top, rotate)
        painter.drawEllipse(rotate, radius, radius)
        cross = 8 / max(self.scale, 0.05)
        painter.setBrush(QColor("#f5f5f5"))
        painter.drawEllipse(pivot, radius, radius)
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
        scale = max(self.scale, 0.05)
        icon_scale = self.settings.vector_point_icon_size / 100.0
        icon_opacity = self.settings.vector_point_icon_opacity / 100.0
        painter.save()
        show_all = (
            self.settings.vector_point_icons_visible
            or (
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
                and not any(
                    point.point_id == self._hover_vector_point_id
                    for point in stroke.points
                )
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
                overlay_stroke = (
                    self._vector_stroke_with_selection_preview(stroke)
                    if self._selection_vector_preview else stroke
                )
                local_path = self._vector_centerline_path(overlay_stroke)
                mapped_path = QPainterPath()
                for polygon in local_path.toSubpathPolygons():
                    mapped_path.addPolygon(QPolygonF([
                        self._vector_world_point(drawing, point)
                        for point in polygon
                    ]))
                painter.drawPath(mapped_path)
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
                hovered = point.point_id == self._hover_vector_point_id
                if not show_all and not selected and not hovered:
                    continue
                edge = QColor("#ff7417" if selected else "#079bd3")
                fill = QColor("#aeeaff")
                edge.setAlphaF(icon_opacity)
                fill.setAlphaF(icon_opacity)
                painter.setPen(QPen(
                    edge, (3 if selected else 2) / scale,
                ))
                painter.setBrush(fill)
                radius = (7 if selected else 5.5) * icon_scale / scale
                painter.drawEllipse(
                    self._vector_world_point(drawing, QPointF(*position)),
                    radius, radius
                )
        painter.restore()

    def _draw_eyedropper_swatch(self, painter: QPainter) -> None:
        if (
            not self._eyedropper_sampling
            or not self._eyedropper_last_color
            or self._eyedropper_widget_point is None
        ):
            return
        radius = 15.0
        center = QPointF(
            self._eyedropper_widget_point.x(),
            self._eyedropper_widget_point.y() - 40.0,
        )
        center.setX(max(radius + 2, min(self.width() - radius - 2, center.x())))
        center.setY(max(radius + 2, min(self.height() - radius - 2, center.y())))
        circle = QPainterPath()
        circle.addEllipse(center, radius, radius)
        painter.save()
        painter.setTransform(QTransform())
        painter.setClipPath(circle)
        cell = 5
        bounds = circle.boundingRect().toAlignedRect()
        for row, y in enumerate(range(bounds.top(), bounds.bottom() + 1, cell)):
            for column, x in enumerate(
                range(bounds.left(), bounds.right() + 1, cell)
            ):
                painter.fillRect(
                    QRect(x, y, cell, cell),
                    QColor("#d6d6d6" if (row + column) % 2 else "#8f8f8f"),
                )
        painter.fillPath(circle, QColor(self._eyedropper_last_color))
        painter.setClipping(False)
        outline = QColor("#ffffff")
        if QColor(self._eyedropper_last_color).lightnessF() > 0.72:
            outline = QColor("#171717")
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(outline, 2.0))
        painter.drawPath(circle)
        painter.restore()

    def _active_focal_modifier(self) -> BlurModifier | None:
        if self.chapter is None or not self.active_modifier_id:
            return None
        modifier = self.chapter.modifiers.get(self.active_modifier_id)
        if not isinstance(modifier, BlurModifier) or modifier.mode != "focal":
            return None
        if not any(
            target in self.chapter.modifier_target_ids(modifier.modifier_id)
            for target in self.selected_entities
        ):
            return None
        return modifier

    @staticmethod
    def _focal_points(
        modifier: BlurModifier,
    ) -> tuple[QPointF, QPointF, QPointF]:
        center = QPointF(*modifier.focal_center)
        direction = QPointF(
            math.cos(modifier.focal_angle),
            math.sin(modifier.focal_angle),
        )
        end = center + direction * modifier.focal_radius
        ramp = center + direction * (
            modifier.focal_radius * modifier.focal_ramp
        )
        return center, ramp, end

    def _transform_single_target_focal_modifiers(
        self, kind: str, entity_id: str, transform: QTransform,
    ) -> None:
        """Keep a sole target's document-space focal rig attached to it."""
        target = self.chapter.modifier_target(kind, entity_id)
        if target is None:
            return
        for modifier_id in target.modifier_ids:
            modifier = self.chapter.modifiers.get(modifier_id)
            if not isinstance(modifier, BlurModifier) or len(
                self.chapter.modifier_target_ids(modifier_id)
            ) != 1:
                continue
            center, _ramp, end = self._focal_points(modifier)
            mapped_center = transform.map(center)
            mapped_end = transform.map(end)
            delta = mapped_end - mapped_center
            modifier.focal_center = mapped_center.toTuple()
            modifier.focal_radius = max(
                1.0, math.hypot(delta.x(), delta.y())
            )
            modifier.focal_angle = math.atan2(delta.y(), delta.x())

    def _draw_focal_modifier_handles(self, painter: QPainter) -> None:
        modifier = self._active_focal_modifier()
        if modifier is None:
            return
        center, ramp, end = self._focal_points(modifier)
        scale = max(0.05, self.scale)
        painter.save()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(
            QColor("#ff7417"), 1.7 / scale, Qt.PenStyle.SolidLine
        ))
        painter.drawLine(center, end)
        painter.setPen(QPen(
            QColor("#ff8b26"), 1.4 / scale, Qt.PenStyle.DotLine
        ))
        painter.drawEllipse(
            center, modifier.focal_radius, modifier.focal_radius
        )
        for point, radius in ((center, 7.0), (ramp, 5.5), (end, 6.5)):
            painter.setPen(QPen(QColor("#452005"), 1.5 / scale))
            painter.setBrush(QColor("#ff8b26"))
            painter.drawEllipse(point, radius / scale, radius / scale)
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
        self, painter: QPainter, bound: BoundGeometry,
        style: ShapeStyle | None = None,
    ) -> None:
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
                        painter, bound, node, style
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

    def _compound_parent_for_child_parent(
        self, parent_id: str,
    ) -> LayerNode | None:
        """Return the compound that an immediate child would contribute to."""
        cursor_id = parent_id
        while cursor_id and cursor_id in self.chapter.layers:
            layer = self.chapter.layers[cursor_id]
            if layer.compound_enabled:
                return layer
            if layer.compound_operation == "ignore":
                return None
            cursor_id = layer.parent_id
        return None

    def _shape_overlay_context(self) -> dict | None:
        if self.chapter is None:
            return None
        if (
            self.tool == ToolKind.SHAPE_CREATE
            and len(self._creation_nodes) >= 2
            and not self._gradient_creation_parent_id
            and not self._page_creation_anchor_id
        ):
            return {
                "mode": "creation",
                "bound": BoundGeometry.path(self._creation_nodes, False),
                "style": self._creation_style or ShapeStyle(),
                "offset": QPointF(),
                "transform": QTransform(),
                "layer": None,
                "compound_parent": self._compound_parent_for_child_parent(
                    self._creation_parent_id
                ) if self._creation_parent_id else None,
                "operation": self._creation_compound_operation,
            }
        if self.tool == ToolKind.SHAPE_EDIT and self.selected_kind == "layer":
            layer = self.chapter.layers.get(self.selected_id)
            if layer is None or layer.bound is None:
                return None
            return {
                "mode": "edit",
                "bound": layer.bound,
                "style": layer.shape_style,
                "offset": QPointF(),
                "transform": self.layer_world_transform(layer.layer_id),
                "layer": layer,
                "compound_parent": self._compound_parent_for_child_parent(
                    layer.parent_id
                ) if layer.parent_id else None,
                "operation": layer.compound_operation,
            }
        return None

    def _shape_overlay_geometry(self) -> dict | None:
        context = self._shape_overlay_context()
        if context is None:
            return None
        left, top, width, height = context["bound"].bbox()
        offset = context["offset"]
        transform = context["transform"]
        top_right = transform.map(QPointF(left + width, top) + offset)
        bottom_right = transform.map(
            QPointF(left + width, top + height) + offset
        )
        edge = bottom_right - top_right
        handles: dict[str, QPointF] = {}
        if not context["bound"].closed:
            handles["base_thickness"] = self.document_to_widget(
                top_right + edge / 3.0
            )
        handles["outline_thickness"] = self.document_to_widget(
            top_right + edge * (2.0 / 3.0)
        )
        occupied = [
            self.document_to_widget(
                transform.map(QPointF(node.x, node.y) + offset)
            )
            for contour in context["bound"].iter_contours()
            for node in contour.nodes
        ]
        for key, original in list(handles.items()):
            candidates = [
                QPointF(original), original + QPointF(52, 0),
                original + QPointF(-52, 0), original + QPointF(0, -52),
                original + QPointF(0, 52), original + QPointF(76, -38),
                original + QPointF(76, 38),
            ]
            candidates = [QPointF(
                max(18.0, min(self.width() - 18.0, candidate.x())),
                max(18.0, min(self.height() - 18.0, candidate.y())),
            ) for candidate in candidates]
            clearance = lambda candidate: min(
                    (math.dist(candidate.toTuple(), point.toTuple())
                     for point in occupied),
                    default=9999.0,
                )
            handles[key] = (
                original if clearance(original) >= 44.0
                else max(candidates, key=clearance)
            )
        if "base_thickness" in handles:
            first = handles["base_thickness"]
            second = handles["outline_thickness"]
            delta = second - first
            distance = math.hypot(delta.x(), delta.y())
            if distance < 48:
                midpoint = (first + second) / 2
                direction = (
                    delta / distance if distance > 1e-6 else QPointF(0, 1)
                )
                handles["base_thickness"] = midpoint - direction * 24
                handles["outline_thickness"] = midpoint + direction * 24

        world_corners = [
            transform.map(QPointF(left, top) + offset),
            transform.map(QPointF(left + width, top) + offset),
            transform.map(QPointF(left + width, top + height) + offset),
            transform.map(QPointF(left, top + height) + offset),
        ]
        bounds = self.camera_transform().map(
            QPolygonF(world_corners)
        ).boundingRect()
        button_specs: list[tuple[str, str, float]] = []
        if context["mode"] == "creation":
            button_specs.append(("finish", "Finish", 76.0))
        if context["compound_parent"] is not None:
            button_specs.append((
                "compound", str(context["operation"]).title(), 92.0
            ))
        buttons: dict[str, tuple[QRectF, str]] = {}
        if button_specs:
            spacing = 6.0
            total = sum(item[2] for item in button_specs) + spacing * (
                len(button_specs) - 1
            )
            x = bounds.center().x() - total / 2
            x = max(8.0, min(max(8.0, self.width() - total - 8.0), x))
            candidate_rows = [
                bounds.top() - 38.0, bounds.bottom() + 8.0,
                bounds.center().y() - 15.0,
            ]
            candidate_rows = [
                max(8.0, min(max(8.0, self.height() - 38.0), value))
                for value in candidate_rows
            ]
            def row_clearance(value: float) -> float:
                rect = QRectF(x, value, total, 30.0).adjusted(-8, -8, 8, 8)
                return min((
                    math.hypot(
                        max(rect.left() - point.x(), 0, point.x() - rect.right()),
                        max(rect.top() - point.y(), 0, point.y() - rect.bottom()),
                    ) for point in occupied
                ), default=9999.0)
            y = max(candidate_rows, key=row_clearance)
            for name, label, width_px in button_specs:
                buttons[name] = (QRectF(x, y, width_px, 30.0), label)
                x += width_px + spacing
        return {"context": context, "handles": handles, "buttons": buttons}

    def _draw_shape_overlay(self, painter: QPainter) -> None:
        geometry = self._shape_overlay_geometry()
        if geometry is None:
            return
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = painter.font()
        font.setPixelSize(14)
        font.setBold(True)
        painter.setFont(font)
        for rect, label in geometry["buttons"].values():
            painter.setPen(QPen(QColor("#8f6626"), 2))
            painter.setBrush(QColor("#f2a23a"))
            painter.drawRoundedRect(rect, 7, 7)
            painter.setPen(QPen(QColor("#342309"), 1))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        drag = self._shape_property_drag
        font.setPixelSize(20)
        painter.setFont(font)
        for key, anchor in geometry["handles"].items():
            center = QPointF(anchor)
            if drag is not None and drag["key"] == key:
                center.setX(drag["current_x"])
                painter.setPen(QPen(QColor("#f2a23a"), 4))
                painter.drawLine(anchor, center)
            painter.setPen(QPen(QColor("#f2a23a"), 4))
            painter.setBrush(QColor("#f2a23a"))
            painter.drawEllipse(center, 14, 14)
            painter.setPen(QPen(QColor("#4a3212"), 2))
            painter.drawText(
                QRectF(center.x() - 14, center.y() - 14, 28, 28),
                Qt.AlignmentFlag.AlignCenter,
                "S" if key == "base_thickness" else "O",
            )
        painter.restore()

    def _shape_overlay_hit(self, widget_point: QPointF) -> str:
        geometry = self._shape_overlay_geometry()
        if geometry is None:
            return ""
        for name, (rect, _label) in geometry["buttons"].items():
            if rect.adjusted(-3, -3, 3, 3).contains(widget_point):
                return name
        for key, center in geometry["handles"].items():
            if math.dist(center.toTuple(), widget_point.toTuple()) <= 28:
                return key
        return ""

    def _cycle_shape_compound_operation(self, context: dict) -> None:
        values = ("add", "subtract", "ignore")
        current = context["operation"]
        target = values[(values.index(current) + 1) % len(values)]
        if context["mode"] == "creation":
            self._creation_compound_operation = target
            self.update()
            return
        layer = context["layer"]
        before = self.chapter.to_dict()
        layer.compound_operation = target
        self._push_immediate_shape_change(before, "Change compound operation")

    def _begin_shape_overlay_interaction(self, widget_point: QPointF) -> bool:
        hit = self._shape_overlay_hit(widget_point)
        geometry = self._shape_overlay_geometry()
        if not hit or geometry is None:
            return False
        context = geometry["context"]
        if hit == "finish":
            self._finish_shape(False)
            self.interactionFinished.emit()
            return True
        if hit == "compound":
            self._cycle_shape_compound_operation(context)
            self.interactionFinished.emit()
            return True
        if hit not in {"base_thickness", "outline_thickness"}:
            return False
        if context["mode"] == "creation" and self._creation_style is None:
            self._creation_style = ShapeStyle()
            context["style"] = self._creation_style
        style = context["style"]
        self._shape_property_drag = {
            "mode": context["mode"],
            "layer_id": (
                context["layer"].layer_id if context["layer"] is not None else ""
            ),
            "key": hit,
            "before": (
                self.chapter.to_dict() if context["mode"] == "edit" else None
            ),
            "start_x": widget_point.x(),
            "current_x": widget_point.x(),
            "start_value": float(getattr(style, hit)),
            "steps": 0,
        }
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        return True

    def _shape_property_drag_style(self, state: dict) -> ShapeStyle | None:
        if state["mode"] == "creation":
            return self._creation_style
        layer = self.chapter.layers.get(state["layer_id"])
        return layer.shape_style if layer is not None else None

    def _update_shape_property_drag(self, widget_point: QPointF) -> None:
        state = self._shape_property_drag
        if state is None:
            return
        style = self._shape_property_drag_style(state)
        if style is None:
            return
        state["current_x"] = widget_point.x()
        steps = math.trunc((widget_point.x() - state["start_x"]) / 4.0)
        if steps == state["steps"]:
            self.update()
            return
        state["steps"] = steps
        if steps == 0:
            value = state["start_value"]
        else:
            maximum = 150 if state["key"] == "base_thickness" else 100
            value = max(0, min(maximum, round(state["start_value"]) + steps))
        setattr(style, state["key"], value)
        if state["mode"] == "edit":
            self.documentChanged.emit(QRectF())
        self.update()

    def _finish_shape_property_drag(self) -> bool:
        state, self._shape_property_drag = self._shape_property_drag, None
        if state is None:
            return False
        self.unsetCursor()
        if state["mode"] == "edit":
            label = (
                "Drag stroke thickness"
                if state["key"] == "base_thickness"
                else "Drag outline thickness"
            )
            self._push_immediate_shape_change(state["before"], label)
        self.update()
        self.interactionFinished.emit()
        return True

    def _cancel_shape_property_drag(self) -> bool:
        state, self._shape_property_drag = self._shape_property_drag, None
        if state is None:
            return False
        style = self._shape_property_drag_style(state)
        if style is not None:
            setattr(style, state["key"], state["start_value"])
        self.unsetCursor()
        if state["mode"] == "edit":
            self.documentChanged.emit(QRectF())
        self.update()
        self.interactionFinished.emit()
        return True

    def _creation_compound_preview_paths(
        self, geometry: BoundGeometry, style: ShapeStyle,
    ) -> tuple[QPainterPath, QPainterPath] | None:
        if not self._creation_parent_id:
            return None
        parent = self._compound_parent_for_child_parent(
            self._creation_parent_id
        )
        if parent is None or self._creation_compound_operation == "ignore":
            return None
        operand = self.open_shape_mesh(
            geometry, style.base_thickness, 0,
            style.start_cap, style.end_cap,
        )
        prospective = self._document_layer_effective_path(
            self.chapter, parent.layer_id, {},
            virtual_parent_id=self._creation_parent_id,
            virtual_path_world=operand,
            virtual_operation=self._creation_compound_operation,
        )
        original = self.layer_effective_path(parent.layer_id)
        transform = self.layer_world_transform(parent.layer_id)
        return transform.map(original), transform.map(prospective)

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
            if geometry is not None:
                style = self._creation_style or ShapeStyle()
                compound_paths = self._creation_compound_preview_paths(
                    geometry, style
                )
                if compound_paths is not None:
                    original, prospective = compound_paths
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(24, 24, 28, 125))
                    painter.drawPath(original)
                    painter.setBrush(QColor(242, 162, 58, 65))
                    painter.drawPath(prospective)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(QPen(
                        QColor("#f2a23a"), 2 / max(self.scale, 0.05),
                        Qt.DashLine,
                    ))
                    painter.drawPath(prospective)
                core = self.open_shape_mesh(
                    geometry, style.base_thickness, 0,
                    style.start_cap, style.end_cap,
                )
                expanded = self.open_shape_mesh(
                    geometry, style.base_thickness,
                    style.outline_thickness * 2,
                    style.start_cap, style.end_cap,
                )
                primary = QColor(style.primary_color or "#111111")
                primary.setAlpha(145)
                painter.fillPath(core, primary)
                if style.outline_thickness > 0:
                    outline = QColor(style.outline_color)
                    outline.setAlpha(165)
                    painter.fillPath(expanded.subtracted(core), outline)
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(
                    QColor("#ffb347"), 2 / max(self.scale, 0.05),
                    Qt.DashLine,
                ))
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
            self._draw_shape_overlay(painter)
        elif len(self._creation_points) >= 2:
            first, second = self._creation_points[0], self._creation_points[-1]
            if (
                self.tool == ToolKind.GRADIENT
                and self._gradient_creation_type == "line"
            ):
                painter.drawLine(QPointF(*first), QPointF(*second))
            elif self.tool in {ToolKind.BOX_BOUND, ToolKind.RASTER_CREATE}:
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
        layer_transform = self.layer_world_transform(obj.parent_layer_id)
        def world_quad(local_quad):
            return [
                layer_transform.map(QPointF(x, y)).toTuple()
                for x, y in local_quad
            ]
        if isinstance(obj, TextObject):
            local_quad = (
                self._rect_quad(self._strict_text_rect(obj))
                if obj.layout_mode == "strict" else self._text_quad(obj)
            )
            return world_quad(local_quad)
        if isinstance(obj, RasterObject):
            if obj.transform_quad is not None:
                return world_quad(obj.transform_quad)
            bounds = QRectF(*obj.interaction_rect)
            local = QRectF(
                obj.x + bounds.x(), obj.y + bounds.y(),
                bounds.width(), bounds.height(),
            )
            return world_quad(self._rect_quad(local))
        if isinstance(obj, VectorDrawingObject):
            if obj.transform_quad is not None:
                return world_quad(obj.transform_quad)
            left, top, width, height = obj.derived_bounds()
            local = QRectF(
                obj.x + left, obj.y + top, max(1.0, width), max(1.0, height)
            )
            return world_quad(self._rect_quad(local))
        if isinstance(obj, ImageObject):
            return world_quad(self._image_local_quad(obj))
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
                return world_quad(corners)
            else:
                bounds = self.layer_effective_path(
                    obj.parent_layer_id
                ).boundingRect()
            bounds = QRectF(
                bounds.left(), bounds.top(),
                max(1.0, bounds.width()), max(1.0, bounds.height()),
            )
            return world_quad(self._rect_quad(bounds))
        return world_quad(self._rect_quad(QRectF(obj.x, obj.y, 80, 80)))

    def _selected_world_quad(self) -> list[tuple[float, float]] | None:
        if len(self.selected_entities) > 1:
            if (
                self._geometry_transform_target == ("multi", "")
                and self._transform_preview_quad is not None
            ):
                return list(self._transform_preview_quad)
            return self._multi_selection_cage()
        if (
            self._geometry_transform_target is not None
            and self._geometry_transform_target[0] == "object"
            and self._transform_preview_quad is not None
        ):
            obj = self.chapter.objects.get(
                self._geometry_transform_target[1]
            )
            if obj is not None:
                transform = self.layer_world_transform(obj.parent_layer_id)
                return [
                    transform.map(QPointF(x, y)).toTuple()
                    for x, y in self._transform_preview_quad
                ]
        if (
            self._transform_preview_quad is not None
            and self._transform_start_quad is not None
            and self.selected_object_id
        ):
            obj = self.chapter.objects[self.selected_object_id]
            transform = self.layer_world_transform(obj.parent_layer_id)
            return [
                transform.map(QPointF(x, y)).toTuple()
                for x, y in self._transform_preview_quad
            ]
        return self.object_world_quad(self.selected_id)

    def selected_widget_rect(self) -> QRect:
        if self.chapter is None or not self.selected_id:
            return QRect()
        if self.selected_kind == "object":
            quad = self._selected_world_quad()
            rect = (
                QPolygonF([QPointF(*point) for point in quad]).boundingRect()
                if quad else self.object_world_rect(self.selected_id)
            )
        else:
            layer = self.chapter.layers.get(self.selected_id)
            if not layer:
                return QRect()
            if (
                self._geometry_transform_target == ("layer", layer.layer_id)
                and self._transform_preview_quad is not None
            ):
                transform = self.layer_world_transform(layer.layer_id)
                rect = transform.map(QPolygonF([
                    QPointF(x, y) for x, y in self._transform_preview_quad
                ])).boundingRect()
                polygon = self.camera_transform().map(QPolygonF(rect))
                return polygon.boundingRect().toAlignedRect()
            if layer.bound is None and layer.parent_id:
                layer = self.chapter.layers[layer.parent_id]
            if layer.bound is None:
                return QRect()
            x, y, width, height = layer.bound.bbox()
            rect = self.layer_world_transform(layer.layer_id).mapRect(
                QRectF(x, y, width, height)
            )
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
            if not layer.visible:
                return False
            if (
                layer.bound is not None
                and layer.layer_id not in skipped_masks
            ):
                path = self.layer_effective_path(layer.layer_id)
                local = self._layer_world_to_local(layer.layer_id, point)
                if not path.contains(local):
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
                else:
                    candidate = self.chapter.layers[child.entity_id]
                    if not candidate.is_page and candidate.bound is not None:
                        result.append(("layer", child.entity_id))
                    walk(child.entity_id)

        walk(page_id)
        return result

    def _object_hit_contains(
        self, obj: DocumentObject, point: QPointF,
    ) -> bool:
        if not obj.visible:
            return False
        if isinstance(obj, SpeedLineCenterObject):
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
        if isinstance(obj, GradientObject):
            if not obj.opacity_locked and obj.opacity <= 0:
                return False
            local = self._layer_world_to_local(obj.parent_layer_id, point)
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
                path = self.layer_effective_path(parent.layer_id)
                local = self._layer_world_to_local(parent.layer_id, point)
                if not path.contains(local):
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
        path = (
            self.layer_shape_path(layer)
            if raw else self.layer_effective_path(layer_id)
        )
        world_path = self.layer_world_transform(layer_id).map(path)
        stroker = QPainterPathStroker()
        stroker.setWidth(24.0 / max(self.scale, 0.05))
        border = stroker.createStroke(world_path)
        return border.contains(point) or (
            layer.layer_kind == "open_shape" and world_path.contains(point)
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
        self._clear_creation_gesture()
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
        self._clear_creation_gesture()
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
        self._clear_creation_gesture()
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

    def _update_interaction_cursor(self, widget_point: QPointF) -> None:
        """Resolve the canvas cursor identically for mouse and pen input."""
        if self.chapter is None:
            self.unsetCursor()
            return
        point = QPointF(widget_point)
        world = self.widget_to_document(point)
        shape_overlay_hit = self._shape_overlay_hit(point)
        shape_hover_kind = (
            self._shape_hover_target.get("kind")
            if (
                self.tool in {ToolKind.SHAPE_CREATE, ToolKind.SHAPE_EDIT}
                and self._shape_hover_target
            ) else None
        )
        if shape_hover_kind not in {None, "interior"}:
            shape_overlay_hit = ""
        over_text_property = bool(self._text_property_handle_hit(point))
        selected_object = self.chapter.objects.get(self.selected_object_id)
        selected_gradient = (
            selected_object
            if isinstance(selected_object, GradientObject) else None
        )
        gradient_hit = (
            self._gradient_control_hit(selected_gradient, world)
            if self.tool in {ToolKind.SHAPE_EDIT, ToolKind.GRADIENT}
            and selected_gradient is not None else None
        )
        page_gap_hit = self._page_gap_hit(world)
        over_selected_text = False
        if self.tool == ToolKind.TEXT_EDIT and isinstance(
            selected_object, TextObject
        ):
            text_path = QPainterPath()
            text_path.addPolygon(QPolygonF([
                QPointF(*candidate)
                for candidate in self.object_world_quad(
                    selected_object.object_id
                )
            ]))
            over_selected_text = text_path.contains(world)

        transform_hover = self._active_transform_hover_kind(world)
        translation_active = bool(
            self._transform_drag_mode == "translate"
            or self._selection_transform_mode == "translate"
            or self._active_shape_control == "translate"
            or self._pending_raster_transform_press is not None
            or self._page_gap_drag_mode == "band"
        )
        transform_precision_active = bool(
            (
                self._transform_drag_mode
                and self._transform_drag_mode != "translate"
            )
            or (
                self._selection_transform_mode
                and self._selection_transform_mode != "translate"
            )
        )
        shape_precision_active = bool(
            self._active_shape_control
            and self._active_shape_control != "translate"
        )

        if translation_active:
            self.setCursor(Qt.ClosedHandCursor)
        elif self._page_gap_drag_mode in {"top", "bottom"}:
            self.setCursor(Qt.PointingHandCursor)
        elif page_gap_hit == "band":
            self.setCursor(Qt.OpenHandCursor)
        elif page_gap_hit in {"top", "bottom"}:
            self.setCursor(Qt.PointingHandCursor)
        elif self.tool == ToolKind.INSERT_PAGE_GAP:
            self.setCursor(
                Qt.PointingHandCursor
                if self._page_gap_hover else Qt.ForbiddenCursor
            )
        elif self._shape_property_drag is not None:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif transform_precision_active or shape_precision_active:
            self.setCursor(Qt.CrossCursor)
        elif self._transform_mode_gizmo_rect().contains(point):
            self.setCursor(Qt.PointingHandCursor)
        elif shape_overlay_hit in {"base_thickness", "outline_thickness"}:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif gradient_hit is not None:
            self.setCursor(Qt.PointingHandCursor)
        elif shape_hover_kind not in {None, "interior"}:
            self.setCursor(Qt.CrossCursor)
        elif shape_overlay_hit:
            self.setCursor(Qt.PointingHandCursor)
        elif over_text_property:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif transform_hover in {"handle", "rotate", "pivot"}:
            self.setCursor(Qt.CrossCursor)
        elif transform_hover == "translate" or shape_hover_kind == "interior":
            self.setCursor(Qt.OpenHandCursor)
        elif self.tool == ToolKind.DRAW_SELECT_STROKE:
            self.setCursor(Qt.PointingHandCursor)
        elif self.tool in {
            ToolKind.DRAW_SELECT_RECT, ToolKind.DRAW_SELECT_LASSO,
        }:
            self.setCursor(Qt.CrossCursor)
        elif over_selected_text:
            self.setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.chapter is None:
            self._clear_detached_input_state()
            event.accept()
            return
        if self._is_touch_mouse(event) or self._tablet_tool_active:
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            return
        nav = self._navigation_mode()
        if nav:
            self._begin_navigation(nav, event.position())
            return
        if self._select_all_text_from_triple_click(QPointF(event.position())):
            event.accept()
            return
        self._dispatch_tool_press(
            event.position(), 1.0, event.modifiers()
        )
        self._update_interaction_cursor(event.position())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.chapter is None:
            self._clear_detached_input_state()
            event.accept()
            return
        if self._is_touch_mouse(event) or self._tablet_tool_active:
            event.accept()
            return
        if self._nav_mode:
            self._queue_navigation_update(event.position())
            return
        self._pointer_hover_widget = QPointF(event.position())
        world = self.widget_to_document(event.position())
        if self.tool == ToolKind.SHAPE_CREATE and self._creation_nodes:
            self._update_creation_hover(world)
        elif self.tool == ToolKind.SHAPE_EDIT:
            self._update_shape_hover(world)
        input_started = time.perf_counter_ns()
        self._tool_move(event.position(), 1.0)
        self._update_interaction_cursor(event.position())
        if self._drawing or self._vector_gesture_mode in {"pencil", "eraser"}:
            elapsed = (time.perf_counter_ns() - input_started) / 1_000_000
            self._performance.input_ms.append(elapsed)
            self._performance.submit_ms.append(elapsed)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.chapter is None:
            self._clear_detached_input_state()
            event.accept()
            return
        if self._is_touch_mouse(event) or self._tablet_tool_active:
            event.accept()
            return
        if self._nav_mode:
            self._end_navigation(event.position())
            return
        if event.button() == Qt.LeftButton:
            self._tool_release()
            self._update_interaction_cursor(event.position())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            self.chapter is not None
            and self._reset_transform_pivot_at(
                self.widget_to_document(event.position())
            )
        ):
            event.accept()
            return
        if self.tool == ToolKind.TEXT_EDIT and self.chapter is not None:
            world = self.widget_to_document(event.position())
            if self._select_text_word_at(world):
                obj = self._editing_text_object()
                if obj is not None:
                    self._last_text_double_click = (
                        time.monotonic(), QPointF(event.position()), obj.object_id
                    )
                event.accept()
                return
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
                    self._select_text_word_at(world)
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
        if event.key() == Qt.Key_Escape and self._cancel_shape_property_drag():
            event.accept()
            return
        if event.key() == Qt.Key_Escape and self._cancel_text_property_drag():
            event.accept()
            return
        if (
            event.key() == Qt.Key_Escape
            and self._model_before is not None
            and self._active_shape_control is not None
        ):
            before, self._model_before = self._model_before, None
            self._active_handle = None
            self._active_shape_control = None
            self._rectangle_roundness_linked = False
            self._shape_control_dragged = False
            self._bound_drag_mode = None
            self._bound_start_points = []
            self.replace_chapter(before)
            self.interactionFinished.emit()
            event.accept()
            return
        if event.key() == Qt.Key_Escape and self._asset_drag_manifest is not None:
            self._clear_asset_drag_preview()
            event.accept()
            return
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
            event.key() == Qt.Key_Escape
            and self._gradient_creation_parent_id
        ):
            self._cancel_gradient_creation()
            self.set_tool(ToolKind.GRADIENT)
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
        if event.key() == Qt.Key_Escape and self._cancel_fill_gesture(
            restore=True
        ):
            self.interactionFinished.emit()
            event.accept()
            return
        if event.key() == Qt.Key_Escape and self._cancel_fill_job():
            self.interactionFinished.emit()
            event.accept()
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
                    self.set_tool(ToolKind.GRADIENT)
                    return
                self._clear_creation_gesture()
                self.update()
                return
            if event.key() in {Qt.Key_Backspace, Qt.Key_Delete} \
                    and self._creation_nodes:
                self._delete_creation_node(self._creation_selected_node_id)
                return
        if self.tool == ToolKind.RASTER_CREATE and event.key() == Qt.Key_Escape:
            self._creation_points.clear()
            self._raster_creation_parent_id = ""
            self._raster_creation_index = None
            self.set_tool(ToolKind.OBJECT_SELECT)
            return
        if event.key() == Qt.Key_Delete and self._delete_selected_vector_points():
            event.accept()
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
            self.tool == ToolKind.BOUND_EDIT and self._selected_shape_node_id
            and self.chapter is not None
        ):
            target = self._shape_edit_target()
            if target is not None and event.key() == Qt.Key_Delete:
                if self._delete_selected_shape_node(target[0]):
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
            self._begin_vector_scale_reuse("wheel")
            factor = math.pow(1.0015, event.angleDelta().y())
            self._set_centered_scale(self.scale * factor)
            self._wheel_zoom_timer.start()
        else:
            self.center_y -= event.angleDelta().y() / max(0.05, self.scale)
            self._snap_camera()
        self.update()
        self.cameraChanged.emit()
        event.accept()

    def tabletEvent(self, event) -> None:  # noqa: N802
        device = event.pointingDevice()
        if device is not None:
            self._device_supports_pressure = bool(
                device.capabilities() & QInputDevice.Capability.Pressure
            )
        if self.chapter is None:
            self._clear_detached_input_state()
            event.accept()
            return
        for child in self.children():
            if child.objectName() in ("exitMaskModeButton", "removeMaskButton") and child.isVisible():
                if child.geometry().contains(event.position().toPoint()):
                    if event.type() == QEvent.TabletRelease:
                        try:
                            child.click()
                        except Exception:
                            pass
                    event.ignore()
                    return
        self._tablet_hover_widget = QPointF(event.position())
        hover_world = self.widget_to_document(event.position())
        if self.tool == ToolKind.SHAPE_CREATE and self._creation_nodes:
            self._update_creation_hover(hover_world)
        elif self.tool == ToolKind.SHAPE_EDIT:
            self._update_shape_hover(hover_world)
        nav = self._navigation_mode()
        if event.type() == QEvent.TabletPress:
            # A real pen-down wins over any finger gesture already in flight.
            # Hover-only TabletMove events deliberately leave touch navigation
            # intact so fingers can pan, pinch, and rotate with the pen nearby.
            self._cancel_touch_navigation(
                emit_finished=True, flush_pending=False
            )
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
                self._pen_contact_active = True
                self._tablet_tool_active = True
                self._dispatch_tool_press(
                    event.position(), event.pressure(), event.modifiers()
                )
        elif event.type() == QEvent.TabletMove:
            if self._nav_mode:
                self._queue_navigation_update(event.position())
            elif self._pen_contact_active:
                input_started = time.perf_counter_ns()
                self._tool_move(event.position(), event.pressure())
                if (
                    self._drawing
                    or self._vector_gesture_mode in {"pencil", "eraser"}
                ):
                    elapsed = (
                        time.perf_counter_ns() - input_started
                    ) / 1_000_000
                    self._performance.input_ms.append(elapsed)
                    self._performance.submit_ms.append(elapsed)
            elif self.tool == ToolKind.DRAW_SELECT_STROKE:
                self._continue_drawing_selection(
                    self.widget_to_document(event.position()),
                    event.position(),
                )
        elif event.type() == QEvent.TabletRelease:
            if self._nav_mode:
                self._end_navigation(event.position())
            elif self._pen_contact_active:
                self._pen_contact_active = False
                self._tablet_tool_active = False
                self._tool_release()
        if self._nav_mode is None:
            self._update_interaction_cursor(event.position())
        if not (
            self._drawing
            or self._vector_gesture_mode in {"pencil", "eraser"}
        ):
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
        if self._vector_render_scale_owner == "wheel":
            self._wheel_zoom_timer.stop()
            self._finish_vector_scale_reuse("wheel", redraw=False)
        if mode == "zoom":
            self._begin_vector_scale_reuse("drag")
        self._nav_frame_timer.stop()
        self._nav_pending_point = None
        self._nav_mode = mode
        self._nav_anchor = QPointF(point)
        self._nav_anchor_center = QPointF(self.center_x, self.center_y)
        self._nav_anchor_scale = self.scale
        self._nav_anchor_rotation = self.rotation
        self._nav_anchor_document = self.widget_to_document(point)

    def _queue_navigation_update(self, point: QPointF) -> None:
        if self._nav_mode is None:
            return
        self._nav_pending_point = QPointF(point)
        if not self._nav_frame_timer.isActive():
            self._nav_frame_timer.start(0)

    def _flush_navigation_update(self) -> None:
        point = self._nav_pending_point
        self._nav_pending_point = None
        if point is not None and self._nav_mode is not None:
            self._update_navigation(point)

    def _update_navigation(self, point: QPointF) -> None:
        delta = point - self._nav_anchor
        if self._nav_mode == "pan":
            angle = math.radians(-self.rotation)
            dx = (delta.x() * math.cos(angle) - delta.y() * math.sin(angle)) / self.scale
            dy = (delta.x() * math.sin(angle) + delta.y() * math.cos(angle)) / self.scale
            self.center_x = self._nav_anchor_center.x() - dx
            self.center_y = self._nav_anchor_center.y() - dy
        elif self._nav_mode == "zoom":
            self._set_centered_scale(
                self._nav_anchor_scale * (1 + delta.x() * 0.005)
            )
            self._center_camera_on_widget_anchor(
                self._nav_anchor_document, self._nav_anchor
            )
        elif self._nav_mode == "rotate":
            center = QPointF(self.rect().center())
            start_angle = math.atan2(self._nav_anchor.y() - center.y(), self._nav_anchor.x() - center.x())
            current = math.atan2(point.y() - center.y(), point.x() - center.x())
            self.rotation = self._nav_anchor_rotation + math.degrees(current - start_angle)
        if self._nav_mode != "zoom":
            self._snap_camera()
        self.update()
        self.cameraChanged.emit()

    def _end_navigation(self, final_point: QPointF | None = None) -> None:
        self._nav_frame_timer.stop()
        if final_point is not None:
            self._nav_pending_point = QPointF(final_point)
        self._flush_navigation_update()
        mode = self._nav_mode
        self._nav_mode = None
        if mode == "zoom":
            self._finish_vector_scale_reuse("drag")
        if mode != "zoom":
            self._snap_camera()
        self.interactionFinished.emit()

    def _begin_vector_scale_reuse(self, owner: str) -> None:
        if self._vector_render_scale_owner == owner:
            return
        if self._vector_render_scale_owner is not None:
            self._vector_render_scale_override = None
        self._vector_render_scale_override = max(
            0.1,
            min(8.0, self.scale * max(1.0, self.devicePixelRatioF())),
        )
        self._vector_render_scale_owner = owner

    def _finish_vector_scale_reuse(
        self, owner: str, *, redraw: bool = True,
    ) -> None:
        if self._vector_render_scale_owner != owner:
            return
        self._vector_render_scale_owner = None
        self._vector_render_scale_override = None
        if redraw and self.chapter is not None:
            self._invalidate_scene_cache()
            self.update()

    def _settle_wheel_zoom(self) -> None:
        self._finish_vector_scale_reuse("wheel")

    def _touch_event(self, event) -> bool:
        if self._pen_contact_active or self._nav_mode:
            self._cancel_touch_navigation(
                emit_finished=True, flush_pending=False
            )
            event.accept()
            return True
        points = [item.position() for item in event.points()]
        if event.type() == QEvent.TouchBegin:
            self._touch_frame_timer.stop()
            self._touch_pending_points = None
            self._rebase_touch_navigation(points)
            event.accept()
            return True
        if event.type() == QEvent.TouchUpdate and points:
            if len(points) != len(self._touch_anchor_points):
                self._touch_frame_timer.stop()
                self._touch_pending_points = None
                self._rebase_touch_navigation(points)
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
        self._cancel_touch_navigation()
        self.interactionFinished.emit()
        event.accept()
        return True

    def _cancel_touch_navigation(
        self, *, emit_finished: bool = False,
        flush_pending: bool = True,
    ) -> None:
        active = bool(
            self._touch_frame_timer.isActive()
            or self._touch_pending_points
            or self._touch_points
            or self._touch_anchor_points
        )
        self._touch_frame_timer.stop()
        if flush_pending and self._touch_pending_points:
            self._flush_touch_navigation()
        self._touch_pending_points = None
        self._touch_points.clear()
        self._touch_anchor_points.clear()
        self._finish_vector_scale_reuse("touch")
        if emit_finished and active:
            self.interactionFinished.emit()

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
        if points:
            self._begin_vector_scale_reuse("touch")
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

        self.scale = new_scale
        self.rotation = new_rotation
        self._center_camera_on_widget_anchor(
            self._touch_anchor_document, current_center,
            scale=new_scale, rotation=new_rotation,
        )
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

    def _delete_creation_node(self, node_id: str = "") -> bool:
        if not self._creation_nodes:
            return False
        node = next((
            candidate for candidate in self._creation_nodes
            if candidate.node_id == node_id
        ), self._creation_nodes[-1])
        self._creation_nodes.remove(node)
        self._creation_selected_node_id = (
            self._creation_nodes[-1].node_id if self._creation_nodes else ""
        )
        self._creation_active_control = None
        self._creation_press_widget = QPointF()
        self._creation_close_candidate = False
        self._creation_node_dragged = False
        self._shape_hover_target = None
        self._shape_hover_insert = None
        if len(self._creation_nodes) >= 2:
            self._normalize_creation_handles()
        self.update()
        return True

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
            not self._gradient_creation_parent_id
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
                    "close": True,
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
        if kind == "node" and hit.get("close"):
            return "Click to close this shape; drag to move the first point"
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
                self._delete_creation_node(node.node_id)
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
            self._creation_close_candidate = bool(hit.get("close"))
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
        movement = math.dist(
            (widget_point.x(), widget_point.y()),
            (
                self._creation_press_widget.x(),
                self._creation_press_widget.y(),
            ),
        )
        movement_threshold = (
            max(3, QApplication.startDragDistance())
            if self._creation_close_candidate else 3
        )
        moved = movement > movement_threshold
        if moved:
            self._creation_node_dragged = True
            self._creation_close_candidate = False
        if control == "new_point":
            if moved:
                node.point_type = "bezier"
                node.handles_locked = False
                snapped = self._snap(
                    point, self._creation_parent_id
                    or self._target_parent_for_new_layer()
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
                    point, self._creation_parent_id
                    or self._target_parent_for_new_layer()
                )
                node.incoming = (snapped.x(), snapped.y())
                node.outgoing = (
                    node.x * 2 - snapped.x(), node.y * 2 - snapped.y()
                )
            elif control == "draft_node" and moved:
                snapped = self._snap(
                    point, self._creation_parent_id
                    or self._target_parent_for_new_layer()
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
                point, self._creation_parent_id
                or self._target_parent_for_new_layer()
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
        self._drawing_selection_shift_anchor = None
        self._drawing_selection_shift_active = False
        self._transform_gizmo_key = None
        self._transform_gizmo_slot = None
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
        return (
            self._raster_local_point(obj, world)
            if isinstance(obj, RasterObject)
            else self._vector_local_point(obj, world)
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
        if pivot_distance <= tolerance:
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
        previous_quad = list(self._selection_transform_quad or start)
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
            world_transform = self._quad_to_quad_transform(start, target)
            local_to_world = self._drawing_local_to_world_transform(obj)
            world_to_local, valid = local_to_world.inverted()
            if not valid:
                return
            width_scale = math.sqrt(abs(world_transform.determinant()))
            self._selection_vector_preview = {}
            for point_id, source in self._selection_vector_points.items():
                mapped = world_to_local.map(world_transform.map(
                    local_to_world.map(QPointF(*source["position"]))
                ))
                incoming = source["incoming"]
                if incoming is not None:
                    incoming = world_to_local.map(world_transform.map(
                        local_to_world.map(QPointF(*incoming))
                    )).toTuple()
                outgoing = source["outgoing"]
                if outgoing is not None:
                    outgoing = world_to_local.map(world_transform.map(
                        local_to_world.map(QPointF(*outgoing))
                    )).toTuple()
                self._selection_vector_preview[point_id] = {
                    "position": mapped.toTuple(),
                    "incoming": incoming,
                    "outgoing": outgoing,
                    "width": max(
                        1.0, min(1000.0, float(source["width"]) * width_scale)
                    ),
                }
            self._selection_vector_preview_revision += 1
        dirty = QPolygonF([
            QPointF(*candidate) for candidate in previous_quad
        ]).boundingRect().united(QPolygonF([
            QPointF(*candidate) for candidate in target
        ]).boundingRect()).adjusted(-3, -3, 3, 3)
        if isinstance(obj, RasterObject):
            if self._object_is_mask_contributor(obj.object_id):
                self._invalidate_tone_mask_overlay()
            dirty = self.modifier_expanded_dirty(obj.object_id, dirty)
        self._mark_scene_dirty_world(dirty)
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
        local_to_world = self._drawing_local_to_world_transform(obj)
        world_to_local, valid = local_to_world.inverted()
        if not valid:
            return
        source_quad = [
            world_to_local.map(QPointF(x, y)).toTuple() for x, y in start
        ]
        destination_local = [
            world_to_local.map(QPointF(x, y)).toTuple()
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

    def has_active_text_edit(self) -> bool:
        """Return whether keyboard commands currently belong to canvas text."""
        return bool(self._text_editing and self._editing_text_object() is not None)

    def select_all(self) -> bool:
        """Select text in an active editor, otherwise the active drawing."""
        obj = self._editing_text_object()
        if self._text_editing and obj is not None:
            self._text_selection_anchor = 0
            self._text_cursor_position = len(obj.text)
            self.update()
            return True
        return self.select_all_drawing()

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
        transform = self._drawing_local_to_world_transform(obj)
        self._selection_transform_quad = [
            transform.map(QPointF(*point)).toTuple()
            for point in self._rect_quad(bounds)
        ]
        if self._selection_pivot is None:
            self._selection_pivot = transform.map(bounds.center())

    def _begin_drawing_selection(
        self, world: QPointF, widget: QPointF, *,
        test_transform: bool = True,
    ) -> bool:
        if self.tool == ToolKind.DRAW_SHAPE:
            self._drawing_selection_operation = self._selection_operation()
            if self._drawing_selection_operation == "replace" and not self._drawing_selection_path.isEmpty():
                mods = QApplication.keyboardModifiers()
                if not (mods & Qt.ShiftModifier or mods & Qt.AltModifier):
                    before = self._selection_snapshot()
                    self._drawing_selection_path = QPainterPath()
                    after = self._selection_snapshot()
                    self._push_selection_undo(before, after)
            self._drawing_selection_gesture = [QPointF(world)]
            self.update()
            return True
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
        if self.tool == ToolKind.DRAW_SHAPE:
            if not self._drawing_selection_gesture:
                return False
            shift = bool(QGuiApplication.keyboardModifiers() & Qt.ShiftModifier)
            if shift and not self._drawing_selection_shift_active and len(self._drawing_selection_gesture) >= 1:
                self._drawing_selection_shift_anchor = QPointF(self._drawing_selection_gesture[-1])
                self._drawing_selection_shift_active = True
            elif not shift and self._drawing_selection_shift_active:
                self._drawing_selection_shift_active = False
                self._drawing_selection_shift_anchor = None
            if self._drawing_selection_shift_active:
                if len(self._drawing_selection_gesture) == 1:
                    self._drawing_selection_gesture.append(QPointF(world))
                else:
                    anchor = self._drawing_selection_shift_anchor
                    if anchor is not None and len(self._drawing_selection_gesture) >= 2:
                        self._drawing_selection_gesture = self._drawing_selection_gesture[:-1]
                        if math.dist(anchor.toTuple(), world.toTuple()) >= 1.0 / max(self.scale, 0.05):
                            self._drawing_selection_gesture.append(QPointF(world))
                        else:
                            self._drawing_selection_gesture.append(QPointF(anchor))
                    else:
                        self._drawing_selection_gesture[-1] = QPointF(world)
                self.update()
                return True
            if math.dist(self._drawing_selection_gesture[-1].toTuple(), world.toTuple()) >= 1.0 / max(self.scale, 0.05):
                self._drawing_selection_gesture.append(QPointF(world))
                self.update()
            return True
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
        elif self.tool == ToolKind.DRAW_SELECT_LASSO:
            shift = bool(QGuiApplication.keyboardModifiers() & Qt.ShiftModifier)
            if shift and not self._drawing_selection_shift_active and len(self._drawing_selection_gesture) >= 1:
                self._drawing_selection_shift_anchor = QPointF(self._drawing_selection_gesture[-1])
                self._drawing_selection_shift_active = True
            elif not shift and self._drawing_selection_shift_active:
                self._drawing_selection_shift_active = False
                self._drawing_selection_shift_anchor = None
            if self._drawing_selection_shift_active:
                if len(self._drawing_selection_gesture) == 1:
                    self._drawing_selection_gesture.append(QPointF(local))
                else:
                    anchor = self._drawing_selection_shift_anchor
                    if anchor is not None and len(self._drawing_selection_gesture) >= 1:
                        if len(self._drawing_selection_gesture) >= 2:
                            self._drawing_selection_gesture = self._drawing_selection_gesture[:-1]
                        self._drawing_selection_gesture.append(QPointF(local))
                    else:
                        self._drawing_selection_gesture[-1] = QPointF(local)
                self.update()
                return True
            if math.dist(self._drawing_selection_gesture[-1].toTuple(), local.toTuple()) >= 1.5 / max(self.scale, 0.05):
                self._drawing_selection_gesture.append(QPointF(local))
        self.update()
        return True

    def _finish_drawing_selection(self) -> bool:
        if self.tool == ToolKind.DRAW_SHAPE:
            return self._finish_draw_shape()
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
        if self.tool == ToolKind.DRAW_SHAPE:
            painter.save()
            painter.setBrush(QColor(255, 138, 36, 40))
            painter.setPen(QPen(QColor("#ff8a24"), 1.5 / max(self.scale, 0.05), Qt.SolidLine))
            if not self._drawing_selection_path.isEmpty():
                painter.drawPath(self._drawing_selection_path)
            if self._drawing_selection_gesture:
                preview = QPainterPath()
                preview.addPolygon(QPolygonF(self._drawing_selection_gesture))
                preview.closeSubpath()
                painter.drawPath(preview)
            painter.restore()
            return
        obj = self._drawing_selection_object()
        if obj is None:
            return
        transform = self._drawing_local_to_world_transform(obj)
        painter.save()
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(
            QColor("#59c9ff"), 1.5 / max(self.scale, 0.05),
            Qt.DashLine,
        ))
        if isinstance(obj, RasterObject) and not self._drawing_selection_path.isEmpty():
            painter.save()
            painter.setTransform(transform, True)
            painter.drawPath(self._drawing_selection_path)
            painter.restore()
        if self._drawing_selection_gesture:
            preview = QPainterPath()
            if self.tool == ToolKind.DRAW_SELECT_RECT:
                preview.addRect(QRectF(
                    self._drawing_selection_gesture[0],
                    self._drawing_selection_gesture[-1],
                ).normalized())
            else:
                preview.addPolygon(QPolygonF(self._drawing_selection_gesture))
            painter.save()
            painter.setTransform(transform, True)
            painter.drawPath(preview)
            painter.restore()
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
                painter.save()
                painter.setTransform(transform, True)
                painter.drawPath(self._vector_centerline_path(stroke))
                painter.restore()
        quad = self._selection_transform_quad
        if quad:
            painter.setPen(QPen(
                QColor("#249eff"), 1.5 / max(self.scale, 0.05)
            ))
            painter.drawPolygon(QPolygonF([QPointF(*point) for point in quad]))
            self._draw_transform_controls(
                painter, quad, self._selection_pivot,
                use_global_pivot=False,
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

    def _finish_draw_shape(self) -> bool:
        gesture = self._drawing_selection_gesture
        self._drawing_selection_gesture = []
        if not gesture or len(gesture) < 3:
            self._clear_drawing_selection()
            self.update()
            return True
        polygon = QPolygonF(gesture)
        polygon.append(gesture[0])
        new_path = QPainterPath()
        new_path.addPolygon(polygon)
        new_path.closeSubpath()
        op = getattr(self, "_drawing_selection_operation", "replace")
        base = QPainterPath(self._drawing_selection_path)
        if base.isEmpty() or op == "replace":
            region = new_path
        elif op == "add":
            region = base.united(new_path)
        else:
            region = base.subtracted(new_path)
        region = region.simplified()
        if region.isEmpty():
            self._clear_drawing_selection()
            self.update()
            return True
        polys = region.toFillPolygons()
        if polys:
            outline = max(polys, key=lambda p: abs(QPolygonF(p).boundingRect().width() * QPolygonF(p).boundingRect().height()))
            outline_poly = QPolygonF(outline)
        else:
            outline_poly = polygon
        world_rect = region.boundingRect()
        if self.chapter is None:
            self._clear_drawing_selection()
            return False
        parent_id = self.active_layer_id or self.active_page_id
        if not parent_id:
            parent_id = self.chapter.root_page_ids[0] if self.chapter.root_page_ids else ""
        if not parent_id or parent_id not in self.chapter.layers:
            self._clear_drawing_selection()
            return False
        x, y, w, h = world_rect.x(), world_rect.y(), world_rect.width(), world_rect.height()
        if w < 5 or h < 5:
            self._clear_drawing_selection()
            return False
        try:
            simplify_tolerance = float(getattr(self.settings, "draw_shape_simplify", 1.0))
        except Exception:
            simplify_tolerance = 1.0
        pts = [QPointF(p) for p in outline_poly]
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        def _perp_dist(pt, a, b):
            if a == b:
                return math.hypot(pt.x() - a.x(), pt.y() - a.y())
            dx = b.x() - a.x()
            dy = b.y() - a.y()
            t = ((pt.x() - a.x()) * dx + (pt.y() - a.y()) * dy) / (dx * dx + dy * dy)
            t = max(0.0, min(1.0, t))
            proj = QPointF(a.x() + t * dx, a.y() + t * dy)
            return math.hypot(pt.x() - proj.x(), pt.y() - proj.y())
        def _dp(points, eps):
            if len(points) <= 2:
                return points[:]
            a, b = points[0], points[-1]
            max_d = -1.0
            idx = -1
            for i in range(1, len(points) - 1):
                d = _perp_dist(points[i], a, b)
                if d > max_d:
                    max_d = d
                    idx = i
            if max_d > eps:
                left = _dp(points[: idx + 1], eps)
                right = _dp(points[idx:], eps)
                return left[:-1] + right
            return [a, b]
        max_pts = 800
        if len(pts) > max_pts:
            step = max(1, len(pts) // max_pts)
            pts = pts[::step]
        simplified = _dp(pts, simplify_tolerance) if len(pts) > 2 else pts[:]
        if len(simplified) < 6 and len(pts) >= 6:
            simplified = pts[:: max(1, len(pts) // 60)]
        if len(simplified) > 300:
            simplified = simplified[:: max(1, len(simplified) // 300)]
        nodes = [PathNode(x=float(p.x()), y=float(p.y())) for p in simplified]
        if len(nodes) < 3:
            nodes = [PathNode(x=float(p.x()), y=float(p.y())) for p in [QPointF(x, y), QPointF(x + w, y), QPointF(x + w, y + h), QPointF(x, y + h)]]
        before = self.chapter.to_dict()
        bound = BoundGeometry.path(nodes, closed=True)
        bound.primitive = "custom"
        style = ShapeStyle(primary_color="#00000000", outline_thickness=2, outline_color="#FF000000")
        try:
            layer = self.chapter.add_layer(parent_id, "Panel", bound, style=style)
        except Exception:
            self._clear_drawing_selection()
            return False
        after = self.chapter.to_dict()
        self.push_model_change(before, after, "Create Draw Shape")
        self._clear_drawing_selection()
        self.set_selection("layer", layer.layer_id)
        self._invalidate_scene_cache()
        self.update()
        return True

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
        query = self.camera_transform().map(
            self._vector_world_point(drawing, local)
        )
        tolerance = 11.0
        candidates: list[tuple[float, str, str]] = []
        for stroke in reversed(drawing.strokes):
            if stroke.stroke_id not in self._selected_vector_stroke_ids:
                continue
            for point in stroke.points:
                candidate = self.camera_transform().map(
                    self._vector_world_point(drawing, QPointF(*point.position))
                )
                separation = math.dist(query.toTuple(), candidate.toTuple())
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
        pressure = self._effective_pressure(pressure)
        preset = self._stroke_preset or self._preset
        width = (
            self._stroke_base_size
            if self._stroke_preset is not None
            else float(self.settings.pencil_size())
        )
        opacity = 1.0
        if preset.pressure_size:
            width *= preset.size_curve.evaluate_fast(pressure)
        if preset.pressure_opacity:
            opacity = preset.opacity_curve.evaluate_fast(pressure)
        return (
            max(1.0, min(1000.0, width)),
            max(0.0, min(1.0, opacity)),
        )

    def _append_vector_sample(
        self, local: QPointF, pressure: float,
    ) -> None:
        sample = FreehandSample(
            local.x(), local.y(),
            self._effective_pressure(pressure),
        )
        if (
            self._vector_samples
            and math.dist(
                self._vector_samples[-1].point, sample.point
            ) < 0.2
        ):
            self._vector_samples[-1] = sample
            return
        previous = self._vector_samples[-1] if self._vector_samples else None
        self._vector_samples.append(sample)
        if self._vector_gesture_mode != "pencil":
            return
        width, opacity = self._vector_pressure_values(sample.pressure)
        color_alpha = QColor(self.primary_color).alphaF()
        opacity *= color_alpha
        try:
            if previous is None:
                dirty = self._vector_preview_tiles.paint_dab(
                    self._vector_preview_id, QPointF(*sample.point), width,
                    QColor(self.primary_color), opacity,
                    antialias=self._stroke_preset.antialiasing,
                )
            else:
                previous_width, previous_opacity = self._vector_pressure_values(
                    previous.pressure
                )
                previous_opacity *= color_alpha
                dirty = self._vector_preview_tiles.paint_segment(
                    self._vector_preview_id,
                    QPointF(*previous.point), QPointF(*sample.point),
                    previous_width, width, QColor(self.primary_color),
                    previous_opacity, opacity,
                    antialias=self._stroke_preset.antialiasing,
                    density=self._stroke_preset.density,
                )
        except Exception:
            self._cancel_vector_gesture(restore=True)
            raise
        if dirty.isEmpty():
            return
        self._vector_preview_dirty = (
            QRectF(dirty) if self._vector_preview_dirty.isEmpty()
            else self._vector_preview_dirty.united(dirty)
        )
        drawing = self._active_vector_drawing()
        if drawing is None:
            return
        modified = self._object_has_effect_modifiers(drawing.object_id)
        world_dirty = self.modifier_expanded_dirty(
            drawing.object_id,
            self._drawing_local_rect_to_world(drawing, dirty),
        )
        self._queue_visual_dirty(
            world_dirty, scene=modified, notify_preview=False,
        )

    def _begin_vector_pencil(
        self, drawing: VectorDrawingObject, local: QPointF, pressure: float,
    ) -> None:
        self._vector_before = {drawing.object_id: drawing.to_dict()}
        self._vector_gesture_mode = "pencil"
        self._vector_samples = []
        self._vector_preview_tiles = TileStore()
        self._vector_preview_dirty = QRectF()
        self._stroke_preset = BrushPreset.from_dict(self._preset.to_dict())
        self._stroke_base_size = float(self.settings.pencil_size())
        self._drawing = True
        self._suspend_gc_for_stroke()
        try:
            self._append_vector_sample(local, pressure)
        except Exception:
            self._restore_gc_after_stroke()
            raise

    def _finish_vector_pencil(
        self, drawing: VectorDrawingObject,
    ) -> None:
        try:
            self._finish_vector_pencil_impl(drawing)
        except Exception:
            try:
                self._cancel_vector_gesture(restore=True)
            finally:
                self._restore_gc_after_stroke()
            raise
        finally:
            self._restore_gc_after_stroke()

    def _finish_vector_pencil_impl(
        self, drawing: VectorDrawingObject,
    ) -> None:
        fitted = fit_freehand(
            self._vector_samples,
            error=self.settings.vector_fit_error,
            resample_spacing=None,
            attribute_error=0.025,
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
            self._promoted_vector_preview = {
                "drawing_id": drawing.object_id,
                "stroke_id": stroke.stroke_id,
                "render_revision": stroke.render_revision,
                "tiles": self._vector_preview_tiles.object_tiles(
                    self._vector_preview_id
                ),
                "tile_size": self._vector_preview_tiles.tile_size,
            }
        before = self._vector_before or {}
        self._vector_gesture_mode = None
        self._vector_samples = []
        self._vector_preview_tiles = TileStore()
        self._vector_preview_dirty = QRectF()
        self._vector_before = None
        self._stroke_preset = None
        self._drawing = False
        self._push_vector_change(before, "Vector pencil stroke")
        self._restore_gc_after_stroke()
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

    def _build_vector_eraser_index(
        self, drawing: VectorDrawingObject,
    ) -> None:
        revision = (drawing.object_id, drawing.drawing_revision)
        if self._vector_eraser_grid_revision == revision:
            return
        cell = self._vector_eraser_grid_size
        grid: dict[tuple[int, int], set[str]] = {}
        bounds_by_stroke: dict[str, QRectF] = {}
        for stroke in drawing.strokes:
            if not stroke.points:
                continue
            bounds = QRectF(*stroke.derived_bounds())
            bounds_by_stroke[stroke.stroke_id] = bounds
            left = math.floor(bounds.left() / cell)
            right = math.floor(bounds.right() / cell)
            top = math.floor(bounds.top() / cell)
            bottom = math.floor(bounds.bottom() / cell)
            for y in range(top, bottom + 1):
                for x in range(left, right + 1):
                    grid.setdefault((x, y), set()).add(stroke.stroke_id)
        self._vector_eraser_grid = grid
        self._vector_eraser_bounds = bounds_by_stroke
        self._vector_eraser_grid_revision = revision

    def _vector_eraser_candidates(
        self, sweep: list[tuple[float, float]], radius: float,
    ) -> set[str]:
        if not sweep:
            return set()
        points = sweep[-2:] if len(sweep) > 1 else sweep
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        bounds = QRectF(
            min(xs) - radius, min(ys) - radius,
            max(xs) - min(xs) + radius * 2,
            max(ys) - min(ys) + radius * 2,
        )
        cell = self._vector_eraser_grid_size
        result: set[str] = set()
        for y in range(
            math.floor(bounds.top() / cell),
            math.floor(bounds.bottom() / cell) + 1,
        ):
            for x in range(
                math.floor(bounds.left() / cell),
                math.floor(bounds.right() / cell) + 1,
            ):
                result.update(self._vector_eraser_grid.get((x, y), ()))
        return result

    def _update_vector_eraser_preview(
        self, drawing: VectorDrawingObject,
    ) -> None:
        sweep = [sample.point for sample in self._vector_sweep]
        if not sweep:
            return
        radius = self.settings.active_eraser_pixels() / 2
        segment = sweep[-2:] if len(sweep) > 1 else sweep
        segment_x = [point[0] for point in segment]
        segment_y = [point[1] for point in segment]
        segment_bounds = QRectF(
            min(segment_x) - radius, min(segment_y) - radius,
            max(segment_x) - min(segment_x) + radius * 2,
            max(segment_y) - min(segment_y) + radius * 2,
        ).adjusted(-1e-6, -1e-6, 1e-6, 1e-6)
        strokes = {stroke.stroke_id: stroke for stroke in drawing.strokes}
        changed_bounds = QRectF()
        changed = False
        for stroke_id in self._vector_eraser_candidates(sweep, radius):
            original = strokes.get(stroke_id)
            if (
                original is None
                or not self._vector_eraser_bounds.get(
                    stroke_id, QRectF(*original.derived_bounds())
                ).intersects(segment_bounds)
                or not self._vector_stroke_touched(original, segment, radius)
            ):
                continue
            mode = self.settings.vector_eraser_mode
            if mode == "intersection" and stroke_id in self._vector_eraser_preview:
                continue
            replacements: list[VectorStroke] = []
            if mode == "stroke" or len(original.points) == 1:
                replacements = []
            elif mode == "intersection":
                groups = self._erase_vector_intersection_groups(
                    drawing, original,
                    stroke_cubics(original.points, original.closed),
                    sweep, radius,
                )
                for index, group in enumerate(groups):
                    replacement = self._stroke_from_spans(
                        original, group, preserve_id=index == 0
                    )
                    if replacement is not None:
                        replacements.append(replacement)
            else:
                sources = self._vector_eraser_preview.get(
                    stroke_id, [original]
                )
                for source in sources:
                    groups = erase_stroke_by_corridor(
                        source.points, segment, radius,
                        shape=(
                            "square" if self.settings.eraser_square
                            else "round"
                        ),
                        closed=source.closed,
                    )
                    for index, group in enumerate(groups):
                        replacement = self._stroke_from_spans(
                            source, group, preserve_id=index == 0
                        )
                        if replacement is not None:
                            replacements.append(replacement)
            self._vector_eraser_preview[stroke_id] = replacements
            self._vector_eraser_preview_versions[stroke_id] = (
                self._vector_eraser_preview_versions.get(stroke_id, 0) + 1
            )
            if mode == "point":
                xs = [point[0] for point in segment]
                ys = [point[1] for point in segment]
                padding = radius + max(
                    (point.width for point in original.points), default=1.0
                ) / 2 + 3
                bounds = QRectF(
                    min(xs) - padding, min(ys) - padding,
                    max(xs) - min(xs) + padding * 2,
                    max(ys) - min(ys) + padding * 2,
                )
            else:
                bounds = QRectF(*original.derived_bounds())
            changed_bounds = (
                bounds if changed_bounds.isEmpty()
                else changed_bounds.united(bounds)
            )
            changed = True
        if not changed:
            return
        self._vector_eraser_preview_revision += 1
        world_dirty = self._drawing_local_rect_to_world(
            drawing, changed_bounds
        )
        if self._object_has_effect_modifiers(drawing.object_id):
            self._queue_visual_dirty(
                self.modifier_expanded_dirty(
                    drawing.object_id, world_dirty
                ),
                scene=True, notify_preview=False,
            )
            return
        widget_dirty = self._world_dirty_to_widget(world_dirty)
        if not widget_dirty.isEmpty():
            # The eraser owns a separate background, so repaint it directly;
            # marking the main scene dirty would make it show the committed
            # (pre-gesture) vector again until release.
            self.update(widget_dirty)

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
            if not QRectF(*stroke.derived_bounds()).intersects(
                QRectF(*other.derived_bounds())
            ):
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
        preview = dict(self._vector_eraser_preview)
        changed_strokes = set(preview)
        if changed_strokes:
            rebuilt: list[VectorStroke] = []
            for stroke in drawing.strokes:
                replacements = preview.get(stroke.stroke_id)
                if replacements is None:
                    rebuilt.append(stroke)
                else:
                    rebuilt.extend(replacements)
            drawing.strokes = rebuilt
            drawing.touch_revision()
        self._vector_eraser_preview.clear()
        self._clear_vector_eraser_live_cache()
        self._vector_eraser_grid.clear()
        self._vector_eraser_bounds.clear()
        self._vector_eraser_grid_revision = None
        self._set_vector_selection(drawing)
        self._vector_gesture_mode = None
        self._vector_sweep = []
        self._vector_before = None
        pushed = self._push_vector_change(before, "Vector eraser")
        if not pushed:
            self.update()
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
            self._effective_pressure(pressure)
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

    def _raster_fill_visual_changed(
        self, object_id: str, world: QRectF,
    ) -> None:
        if self.chapter is None or object_id not in self.chapter.objects:
            return
        self._modifier_source_cache.clear()
        self._modifier_source_cache_bytes = 0
        self._outline_distance_cache.clear()
        widget = self._mark_scene_dirty_world(world)
        self.documentChanged.emit(world)
        if not widget.isEmpty():
            self.update(widget)

    def _active_fill_color(self) -> QColor:
        if self.active_color_slot == "secondary":
            return QColor(self.secondary_color)
        return QColor(self.primary_color)

    def _snapshot_fill_object_tiles(
        self, object_id: str,
    ) -> dict[tuple[int, int], QImage]:
        return {
            key: QImage(image)
            for key, image in self.tiles.object_tiles(object_id).items()
        }

    @staticmethod
    def _fill_reference_settings_signature(
        profile: dict[str, object],
    ) -> tuple:
        return tuple(sorted(
            (name, repr(profile.get(name))) for name in (
                "reference_mode", "exclude_editing_target",
                "exclude_images", "exclude_gradients", "exclude_mask_only",
                "fill_up_to_vector_path", "include_vector_path",
            )
        ))

    def _clear_fill_replay(self) -> None:
        self._fill_replay_timer.stop()
        if self._fill_replay_cancel is not None:
            self._fill_replay_cancel.set()
            self._fill_replay_cancel = None
        self._fill_replay_pending_tolerance = None
        self._fill_replay_generation += 1
        self._fill_replay_state = None

    def validate_fill_replay_history(self) -> None:
        state = self._fill_replay_state
        if (
            state is not None
            and self.command_stack.top_undo_command is not state.command
        ):
            self._clear_fill_replay()

    def _install_fill_replay(
        self, obj: RasterObject, command: TilePatchCommand,
        steps: list[tuple[QPointF | None, QPainterPath | None, str]],
        dirty_world: QRectF,
    ) -> None:
        profile = dict(self._fill_operation_profile)
        entities = self._fill_reference_entities(obj, profile)
        self._fill_replay_generation += 1
        self._fill_replay_state = _FillReplayState(
            chapter=self.chapter,
            object_id=obj.object_id,
            object_model=json.dumps(obj.to_dict(), sort_keys=True),
            command=command,
            history_revision=self.command_stack.revision,
            base_tiles={
                key: QImage(image)
                for key, image in self._fill_operation_base_tiles.items()
            },
            steps=[
                (
                    QPointF(point) if point is not None else None,
                    QPainterPath(path) if path is not None else None,
                    str(policy),
                )
                for point, path, policy in steps
            ],
            selection_path=QPainterPath(self._fill_operation_selection),
            color=QColor(self._fill_operation_color),
            profile=profile,
            reference_entities=list(entities),
            reference_signature=self._fill_reference_signature(
                entities, skip_pixels_for=obj.object_id
            ),
            reference_settings=self._fill_reference_settings_signature(profile),
            reference_tiles={
                key: QImage(image)
                for key, image in self._fill_operation_reference_tiles.items()
            },
            current_signature=self._fill_object_signature(obj.object_id),
            dirty_world=QRectF(dirty_world),
        )

    def _fill_replay_is_eligible(self, state: _FillReplayState) -> bool:
        if (
            self.chapter is not state.chapter
            or self.selected_object_id != state.object_id
            or self.command_stack.top_undo_command is not state.command
            or self.command_stack.revision != state.history_revision
        ):
            return False
        obj = self.chapter.objects.get(state.object_id)
        if (
            not isinstance(obj, RasterObject)
            or json.dumps(obj.to_dict(), sort_keys=True) != state.object_model
            or self._fill_object_signature(state.object_id)
            != state.current_signature
        ):
            return False
        entities = self._fill_reference_entities(obj, state.profile)
        return (
            entities == state.reference_entities
            and self._fill_reference_signature(
                entities, skip_pixels_for=state.object_id
            ) == state.reference_signature
        )

    def request_fill_tolerance_replay(
        self, tolerance: int, immediate: bool = False,
    ) -> None:
        tolerance = max(0, min(255, int(tolerance)))
        state = self._fill_replay_state
        if state is None or not self._fill_replay_is_eligible(state):
            if state is not None:
                self._clear_fill_replay()
            return
        if self._fill_replay_cancel is not None:
            self._fill_replay_cancel.set()
            self._fill_replay_cancel = None
            self._fill_replay_generation += 1
        self._fill_replay_pending_tolerance = tolerance
        if immediate:
            self._fill_replay_timer.stop()
            self._recalculate_last_fill_tolerance()
        else:
            self._fill_replay_timer.start(75)

    def _start_fill_replay_worker(
        self, state: _FillReplayState, obj: RasterObject,
        profile: dict[str, object], tolerance: int,
    ) -> bool:
        frame = QRectF(*obj.interaction_rect)
        keys = self.tiles.keys_for_rect(frame)
        mode = str(profile.get("reference_mode", "editing"))
        reference_tiles: dict[tuple[int, int], QImage] | None = None
        if mode != "editing":
            required = len(keys) * self.tiles.tile_size ** 2 * 4
            if required > self._fill_reference_tile_cache_budget:
                return False
            signature = self._fill_reference_signature(
                state.reference_entities
            )
            reference_tiles = {}
            for key in keys:
                stored = state.reference_tiles.get(key)
                if stored is None:
                    image = self._fill_reference_tile(
                        obj, key, profile,
                        entities=state.reference_entities,
                        signature=signature,
                        settings_signature=state.reference_settings,
                    )
                    stored = QImage(image) if image is not None else QImage()
                    state.reference_tiles[key] = QImage(stored)
                reference_tiles[key] = QImage(stored)
        detached = TileStore(self.tiles.tile_size)
        detached.replace_object_tiles(state.object_id, state.base_tiles)
        self._fill_replay_generation += 1
        generation = self._fill_replay_generation
        cancel_event = threading.Event()
        self._fill_replay_cancel = cancel_event
        worker = _FillReplayWorker(
            detached, state.object_id, frame, state.steps,
            state.selection_path, state.color, profile,
            reference_tiles, cancel_event, {
                "generation": generation,
                "state": state,
                "tolerance": tolerance,
                "expected_signature": state.current_signature,
            },
        )
        worker.signals.finished.connect(self._finish_fill_replay_worker)
        self._fill_workers.add(worker)
        QThreadPool.globalInstance().start(worker)
        return True

    def _finish_fill_replay_worker(
        self, worker: _FillReplayWorker, result: dict,
    ) -> None:
        self._fill_workers.discard(worker)
        generation = int(result.get("generation", -1))
        if generation == self._fill_replay_generation:
            self._fill_replay_cancel = None
        state = result.get("state")
        if (
            result.get("cancelled")
            or result.get("error") is not None
            or not isinstance(state, _FillReplayState)
            or state is not self._fill_replay_state
            or generation != self._fill_replay_generation
            or state.current_signature != result.get("expected_signature")
            or not self._fill_replay_is_eligible(state)
        ):
            return
        obj = self.chapter.objects[state.object_id]
        replay_before = result.get("before") or {}
        replay_after = result.get("after") or {}
        keys = set(state.command.before) | set(replay_before)
        for key in keys:
            base = state.base_tiles.get(key)
            image = (
                replay_after.get(key)
                if key in replay_before else base
            )
            self.tiles.set_tile(
                state.object_id, key, QImage(image) if image is not None else None
            )
            state.command.before[key] = (
                QImage(base) if base is not None else None
            )
            state.command.after[key] = (
                QImage(image) if image is not None else None
            )
        tolerance = max(0, min(255, int(result.get("tolerance", 16))))
        state.profile["tolerance"] = tolerance
        state.current_signature = self._fill_object_signature(state.object_id)
        dirty_local = QRectF(result.get("dirty") or QRectF())
        new_world = (
            self.modifier_expanded_dirty(
                state.object_id,
                self._drawing_local_rect_to_world(obj, dirty_local),
            )
            if not dirty_local.isEmpty() else QRectF()
        )
        dirty_world = QRectF(state.dirty_world)
        if not new_world.isEmpty():
            dirty_world = (
                QRectF(new_world) if dirty_world.isEmpty()
                else dirty_world.united(new_world)
            )
        state.dirty_world = QRectF(dirty_world)
        self._preserve_fill_reference_cache = True
        try:
            self._raster_fill_visual_changed(state.object_id, dirty_world)
        finally:
            self._preserve_fill_reference_cache = False
        self.interactionFinished.emit()

    def _recalculate_last_fill_tolerance(self) -> None:
        state = self._fill_replay_state
        tolerance = self._fill_replay_pending_tolerance
        self._fill_replay_pending_tolerance = None
        if (
            state is None or tolerance is None
            or not self._fill_replay_is_eligible(state)
        ):
            if state is not None and not self._fill_replay_is_eligible(state):
                self._clear_fill_replay()
            return
        obj = self.chapter.objects[state.object_id]
        profile = dict(state.profile)
        profile["tolerance"] = tolerance
        if (
            len(self.tiles.keys_for_rect(QRectF(*obj.interaction_rect))) > 16
            and self._start_fill_replay_worker(
                state, obj, profile, tolerance
            )
        ):
            return
        detached = TileStore(self.tiles.tile_size)
        detached.replace_object_tiles(state.object_id, state.base_tiles)
        replay_before: dict[tuple[int, int], QImage | None] = {}
        dirty_local = QRectF()

        def reference_tile(key: tuple[int, int]) -> QImage | None:
            stored = state.reference_tiles.get(key)
            if stored is not None:
                return None if stored.isNull() else QImage(stored)
            image = self._fill_reference_tile(
                obj, key, profile, entities=state.reference_entities,
                signature=self._fill_reference_signature(
                    state.reference_entities
                ),
                settings_signature=state.reference_settings,
            )
            state.reference_tiles[key] = (
                QImage(image) if image is not None else QImage()
            )
            return image

        mode = str(profile.get("reference_mode", "editing"))
        for point, extra_path, policy in state.steps:
            selection = QPainterPath(state.selection_path)
            if extra_path is not None:
                selection = (
                    QPainterPath(extra_path) if selection.isEmpty()
                    else selection.intersected(extra_path)
                )
            changed = detached.advanced_fill(
                state.object_id, point, QRectF(*obj.interaction_rect),
                state.color, profile, replay_before,
                region_policy=policy,
                reference_tile=(None if mode == "editing" else reference_tile),
                selection_tile=lambda key, path=selection: (
                    self._fill_path_mask_tile(path, key)
                ),
            )
            if not changed.isEmpty():
                dirty_local = (
                    QRectF(changed) if dirty_local.isEmpty()
                    else dirty_local.united(changed)
                )

        keys = set(state.command.before) | set(replay_before)
        result_tiles = detached.object_tiles(state.object_id)
        for key in keys:
            image = result_tiles.get(key)
            self.tiles.set_tile(
                state.object_id, key, QImage(image) if image is not None else None
            )
            base = state.base_tiles.get(key)
            state.command.before[key] = (
                QImage(base) if base is not None else None
            )
            state.command.after[key] = (
                QImage(image) if image is not None else None
            )
        state.profile["tolerance"] = tolerance
        state.current_signature = self._fill_object_signature(state.object_id)
        new_world = (
            self.modifier_expanded_dirty(
                state.object_id,
                self._drawing_local_rect_to_world(obj, dirty_local),
            )
            if not dirty_local.isEmpty() else QRectF()
        )
        dirty_world = QRectF(state.dirty_world)
        if not new_world.isEmpty():
            dirty_world = (
                QRectF(new_world) if dirty_world.isEmpty()
                else dirty_world.united(new_world)
            )
        state.dirty_world = QRectF(dirty_world)
        self._preserve_fill_reference_cache = True
        try:
            self._raster_fill_visual_changed(state.object_id, dirty_world)
        finally:
            self._preserve_fill_reference_cache = False
        self.interactionFinished.emit()

    def _cancel_fill_job(self) -> bool:
        event = self._fill_job_cancel
        if event is None:
            return False
        event.set()
        self._fill_job_cancel = None
        self._fill_job_generation += 1
        return True

    def _fill_object_signature(self, object_id: str) -> tuple:
        return tuple(sorted(
            (key, int(image.cacheKey()))
            for key, image in self.tiles.object_tiles(object_id).items()
        ))

    def _start_async_fill(
        self, obj: RasterObject, local: QPointF | None, frame: QRectF,
        profile: dict[str, object], extra_path: QPainterPath | None,
        color: QColor, region_policy: str,
        reference_tiles: dict[tuple[int, int], QImage] | None = None,
    ) -> bool:
        """Run a large editing-layer fill off-thread on detached QImages."""
        self._cancel_fill_job()
        self._fill_job_generation += 1
        generation = self._fill_job_generation
        cancel_event = threading.Event()
        self._fill_job_cancel = cancel_event
        self._fill_job_error = None
        detached = TileStore(self.tiles.tile_size)
        detached.replace_object_tiles(
            obj.object_id, self.tiles.object_tiles(obj.object_id)
        )
        selection = QPainterPath(self._drawing_selection_path)
        if extra_path is not None:
            selection = (
                QPainterPath(extra_path) if selection.isEmpty()
                else selection.intersected(extra_path)
            )
        tile_size = self.tiles.tile_size

        def selection_tile(key: tuple[int, int]) -> np.ndarray | None:
            if selection.isEmpty():
                return None
            image = QImage(
                tile_size, tile_size,
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            painter.translate(-key[0] * tile_size, -key[1] * tile_size)
            painter.fillPath(selection, QColor("white"))
            painter.end()
            rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
            values = np.frombuffer(bytes(rgba.constBits()), dtype=np.uint8)
            return values.reshape((tile_size, tile_size, 4))[..., 3] > 0

        context = {
            "generation": generation,
            "chapter": self.chapter,
            "object_id": obj.object_id,
            "object_model": json.dumps(obj.to_dict(), sort_keys=True),
            "base_signature": self._fill_object_signature(obj.object_id),
        }
        worker = _FillWorker(
            detached, obj.object_id, local, frame,
            color, profile, region_policy, selection_tile,
            reference_tiles, cancel_event, context,
        )
        worker.signals.finished.connect(self._finish_async_fill)
        self._fill_workers.add(worker)
        QThreadPool.globalInstance().start(worker)
        return True

    def _finish_async_fill(
        self, worker: _FillWorker, result: dict,
    ) -> None:
        self._fill_workers.discard(worker)
        generation = int(result.get("generation", -1))
        if generation == self._fill_job_generation:
            self._fill_job_cancel = None
        error = result.get("error")
        if isinstance(error, Exception):
            self._fill_job_error = error
        object_id = str(result.get("object_id", ""))
        obj = (
            self.chapter.objects.get(object_id)
            if self.chapter is result.get("chapter") else None
        )
        if (
            result.get("cancelled") or error is not None
            or generation != self._fill_job_generation
            or not isinstance(obj, RasterObject)
            or json.dumps(obj.to_dict(), sort_keys=True)
            != result.get("object_model")
            or self._fill_object_signature(object_id)
            != result.get("base_signature")
        ):
            self.interactionFinished.emit()
            return
        before = result.get("before") or {}
        after = result.get("after") or {}
        dirty_local = QRectF(result.get("dirty") or QRectF())
        if not before or dirty_local.isEmpty():
            self.interactionFinished.emit()
            return
        for key, image in after.items():
            self.tiles.set_tile(object_id, key, image)
        dirty_world = self.modifier_expanded_dirty(
            object_id, self._drawing_local_rect_to_world(obj, dirty_local)
        )
        callback = lambda target=object_id, rect=QRectF(dirty_world): (
            self._raster_fill_visual_changed(target, rect)
        )
        command = TilePatchCommand(
            "Fill Selection", self.tiles, object_id,
            before, after, callback,
        )
        self.command_stack.push(command, already_done=True)
        self._install_fill_replay(
            obj, command, [(None, None, "area")], dirty_world
        )
        self._raster_fill_visual_changed(object_id, dirty_world)
        self.interactionFinished.emit()

    def _fill_selection_mask_tile(
        self, key: tuple[int, int], extra_path: QPainterPath | None = None,
    ) -> np.ndarray | None:
        path = QPainterPath(self._drawing_selection_path)
        if extra_path is not None:
            path = (
                QPainterPath(extra_path) if path.isEmpty()
                else path.intersected(extra_path)
            )
        if path.isEmpty():
            return None
        return self._fill_path_mask_tile(path, key)

    def _fill_path_mask_tile(
        self, path: QPainterPath, key: tuple[int, int],
    ) -> np.ndarray | None:
        if path.isEmpty():
            return None
        size = self.tiles.tile_size
        image = QImage(
            size, size, QImage.Format.Format_ARGB32_Premultiplied
        )
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.translate(-key[0] * size, -key[1] * size)
        painter.fillPath(path, QColor("white"))
        painter.end()
        return self._image_alpha_array(image) > 0.0

    def _fill_reference_entities(
        self, target: RasterObject, profile: dict[str, object],
    ) -> list[tuple[str, str]]:
        mode = str(profile.get("reference_mode", "editing"))
        if mode == "editing":
            return [("object", target.object_id)]
        candidates: list[tuple[str, str]] = []
        if mode == "reference":
            candidates = [
                ("layer", layer.layer_id)
                for layer in self.chapter.layers.values()
                if layer.fill_reference and not layer.is_page
            ] + [
                ("object", obj.object_id)
                for obj in self.chapter.objects.values()
                if obj.fill_reference
            ]
        elif mode == "selected":
            candidates = list(self.selected_entities)
        elif mode == "current_folder":
            parent = self.chapter.layers[target.parent_layer_id]
            candidates = [
                (child.kind, child.entity_id) for child in parent.children
            ]
        else:
            for page_id in self.chapter.root_page_ids:
                page = self.chapter.layers[page_id]
                candidates.extend(
                    (child.kind, child.entity_id) for child in page.children
                )

        def allowed(kind: str, entity_id: str) -> bool:
            entity = (
                self.chapter.layers.get(entity_id)
                if kind == "layer" else self.chapter.objects.get(entity_id)
            )
            if entity is None or not entity.visible:
                return False
            if (
                entity_id == target.object_id
                and bool(profile.get("exclude_editing_target", False))
            ):
                return False
            ancestors = (
                self.chapter.ancestor_layers(entity_id)
                if kind == "layer"
                else self.chapter.ancestor_layers(entity.parent_layer_id)
            )
            if any(
                not ancestor.visible or ancestor.mask_only
                for ancestor in ancestors
            ):
                return False
            if bool(getattr(entity, "mask_only", False)):
                return False
            if isinstance(entity, TextObject):
                return False
            if isinstance(entity, ImageObject) and bool(
                profile.get("exclude_images", False)
            ):
                return False
            if isinstance(entity, GradientObject) and bool(
                profile.get("exclude_gradients", False)
            ):
                return False
            if isinstance(entity, VectorDrawingObject) and not bool(
                profile.get("fill_up_to_vector_path", True)
            ):
                return False
            return True

        unique: list[tuple[str, str]] = []
        for candidate in candidates:
            if candidate not in unique and allowed(*candidate):
                unique.append(candidate)
        selected_layers = {
            entity_id for kind, entity_id in unique if kind == "layer"
        }

        def covered_by_selected_layer(kind: str, entity_id: str) -> bool:
            parent_id = (
                self.chapter.layers[entity_id].parent_id
                if kind == "layer" else self.chapter.objects[entity_id].parent_layer_id
            )
            while parent_id:
                if parent_id in selected_layers:
                    return True
                parent_id = self.chapter.layers[parent_id].parent_id
            return False

        unique = [
            candidate for candidate in unique
            if not covered_by_selected_layer(*candidate)
        ]
        order = {
            (
                "layer" if isinstance(entity, LayerNode) else "object",
                entity.layer_id if isinstance(entity, LayerNode)
                else entity.object_id,
            ): index
            for index, (_kind, entity) in enumerate(
                self.chapter.iter_render_order()
            )
        }
        return sorted(unique, key=lambda candidate: order.get(candidate, 0))

    def _fill_reference_signature(
        self, entities: list[tuple[str, str]], *, skip_pixels_for: str = "",
    ) -> tuple:
        result: list[tuple] = []
        for kind, entity_id in entities:
            entity = (
                self.chapter.layers[entity_id]
                if kind == "layer" else self.chapter.objects[entity_id]
            )
            pixels: tuple = ()
            if (
                isinstance(entity, RasterObject)
                and entity_id != skip_pixels_for
            ):
                pixels = tuple(sorted(
                    (key, int(image.cacheKey()))
                    for key, image in self.tiles.object_tiles(entity_id).items()
                ))
            elif isinstance(entity, ImageObject):
                pixels = (int(self.images.image(entity_id).cacheKey()),)
            result.append((
                kind, entity_id,
                json.dumps(entity.to_dict(), sort_keys=True), pixels,
            ))
        return tuple(result)

    def _render_fill_reference_entity(
        self, painter: QPainter, kind: str, entity_id: str,
        visible_world: QRectF,
    ) -> None:
        if kind == "layer":
            layer = self.chapter.layers[entity_id]
            parent_transform = (
                self.layer_world_transform(layer.parent_id)
                if layer.parent_id else QTransform()
            )
            inverse, valid = parent_transform.inverted()
            painter.save()
            painter.setTransform(parent_transform, True)
            self._render_layer(
                painter, layer, 1.0,
                inverse.mapRect(visible_world) if valid else visible_world,
            )
            painter.restore()
            return
        obj = self.chapter.objects[entity_id]
        parent_transform = self.layer_world_transform(obj.parent_layer_id)
        inverse, valid = parent_transform.inverted()
        parent_opacity = 1.0
        for ancestor in self.chapter.ancestor_layers(obj.parent_layer_id):
            parent_opacity *= ancestor.opacity
        painter.save()
        painter.setTransform(parent_transform, True)
        self._render_object(
            painter, obj, parent_opacity,
            inverse.mapRect(visible_world) if valid else visible_world,
        )
        painter.restore()

    def _fill_reference_tile(
        self, target: RasterObject, key: tuple[int, int],
        profile: dict[str, object],
        *, entities: list[tuple[str, str]] | None = None,
        signature: tuple | None = None,
        settings_signature: tuple | None = None,
    ) -> QImage | None:
        entities = (
            entities if entities is not None
            else self._fill_reference_entities(target, profile)
        )
        if entities == [("object", target.object_id)]:
            return self.tiles.tile(target.object_id, key)
        if signature is None:
            signature = self._fill_reference_signature(entities)
        if settings_signature is None:
            settings_signature = tuple(sorted(
                (name, repr(profile.get(name))) for name in (
                    "reference_mode", "exclude_editing_target",
                    "exclude_images", "exclude_gradients", "exclude_mask_only",
                    "fill_up_to_vector_path", "include_vector_path",
                )
            ))
        cache_key = (
            target.object_id, key, signature, settings_signature,
            tuple(target.transform_quad or ()), target.x, target.y,
        )
        cached = self._fill_reference_tile_cache.pop(cache_key, None)
        if cached is not None:
            self._fill_reference_tile_cache[cache_key] = cached
            return QImage(cached)
        size = self.tiles.tile_size
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        local_to_world = self._drawing_local_to_world_transform(target)
        world_to_local, valid = local_to_world.inverted()
        if not valid:
            return image
        shift = QTransform()
        shift.translate(-key[0] * size, -key[1] * size)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setTransform(world_to_local * shift)
        local_rect = QRectF(
            key[0] * size, key[1] * size, size, size
        )
        visible_world = local_to_world.mapRect(local_rect)
        previous_interactive = self._interactive_render
        previous_exclude_text = self._render_exclude_text
        self._interactive_render = False
        self._render_exclude_text = True
        try:
            for kind, entity_id in entities:
                self._render_fill_reference_entity(
                    painter, kind, entity_id, visible_world
                )
        finally:
            self._interactive_render = previous_interactive
            self._render_exclude_text = previous_exclude_text
            painter.end()
        stored = QImage(image)
        self._fill_reference_tile_cache[cache_key] = stored
        self._fill_reference_tile_cache_bytes += int(stored.sizeInBytes())
        while (
            self._fill_reference_tile_cache
            and self._fill_reference_tile_cache_bytes
            > self._fill_reference_tile_cache_budget
        ):
            _old_key, old = self._fill_reference_tile_cache.popitem(last=False)
            self._fill_reference_tile_cache_bytes -= int(old.sizeInBytes())
        return image

    def _apply_raster_fill(
        self, obj: RasterObject, world_point: QPointF | None,
        *, before: dict[tuple[int, int], QImage | None] | None = None,
        commit: bool = True, extra_path: QPainterPath | None = None,
        profile_overrides: dict[str, object] | None = None,
        profile_snapshot: dict[str, object] | None = None,
        color: QColor | None = None, region_policy: str = "seed",
        selection_path: QPainterPath | None = None,
        reference_capture: dict[tuple[int, int], QImage] | None = None,
    ) -> bool:
        local = (
            self._raster_local_point(obj, world_point)
            if world_point is not None else None
        )
        frame = QRectF(*obj.interaction_rect)
        if local is not None and not frame.contains(local):
            return False
        if local is None:
            clip = QPainterPath(self._drawing_selection_path)
            if extra_path is not None:
                clip = (
                    QPainterPath(extra_path) if clip.isEmpty()
                    else clip.intersected(extra_path)
                )
            if not clip.isEmpty():
                frame = frame.intersected(clip.boundingRect())
            if frame.isEmpty():
                return False
        patch_before = before if before is not None else {}
        profile = dict(
            profile_snapshot
            if profile_snapshot is not None
            else self.settings.active_fill_profile()
        )
        if profile_overrides:
            profile.update(profile_overrides)
        if extra_path is not None:
            profile["connected_pixels_only"] = False
        mode = str(profile.get("reference_mode", "editing"))
        reference_entities = (
            self._fill_reference_entities(obj, profile)
            if mode != "editing" else [("object", obj.object_id)]
        )
        reference_signature = (
            self._fill_reference_signature(reference_entities)
            if mode != "editing" else ()
        )
        reference_settings = tuple(sorted(
            (name, repr(profile.get(name))) for name in (
                "reference_mode", "exclude_editing_target",
                "exclude_images", "exclude_gradients", "exclude_mask_only",
                "fill_up_to_vector_path", "include_vector_path",
            )
        ))
        frozen_selection = QPainterPath(
            selection_path
            if selection_path is not None
            else self._drawing_selection_path
        )
        if extra_path is not None:
            frozen_selection = (
                QPainterPath(extra_path) if frozen_selection.isEmpty()
                else frozen_selection.intersected(extra_path)
            )

        def reference_for(key: tuple[int, int]) -> QImage | None:
            image = self._fill_reference_tile(
                obj, key, profile, entities=reference_entities,
                signature=reference_signature,
                settings_signature=reference_settings,
            )
            if reference_capture is not None and key not in reference_capture:
                reference_capture[key] = QImage(image) if image is not None else QImage()
            return image

        dirty_local = self.tiles.advanced_fill(
            obj.object_id, local, frame,
            QColor(color) if color is not None else self._active_fill_color(),
            profile, patch_before, region_policy=region_policy,
            reference_tile=(
                None if mode == "editing"
                else reference_for
            ),
            selection_tile=lambda key: self._fill_path_mask_tile(
                frozen_selection, key
            ),
        )
        if dirty_local.isEmpty() or not patch_before:
            return False
        dirty_world = self.modifier_expanded_dirty(
            obj.object_id,
            self._drawing_local_rect_to_world(obj, dirty_local),
        )
        self._fill_dirty_world = (
            QRectF(dirty_world) if self._fill_dirty_world.isEmpty()
            else self._fill_dirty_world.united(dirty_world)
        )
        preserve_references = (
            mode != "editing"
            and ("object", obj.object_id) not in reference_entities
        )
        if not commit:
            self._preserve_fill_reference_cache = preserve_references
            try:
                self._raster_fill_visual_changed(obj.object_id, dirty_world)
            finally:
                self._preserve_fill_reference_cache = False
            return True
        after = self.tiles.snapshot(obj.object_id, set(patch_before))
        callback = lambda object_id=obj.object_id, rect=QRectF(dirty_world): (
            self._raster_fill_visual_changed(object_id, rect)
        )
        command = TilePatchCommand(
            "Raster fill", self.tiles, obj.object_id,
            patch_before, after, callback,
        )
        self.command_stack.push(command, already_done=True)
        self._install_fill_replay(
            obj, command,
            [(
                QPointF(local) if local is not None else None,
                QPainterPath(extra_path) if extra_path is not None else None,
                region_policy,
            )],
            dirty_world,
        )
        self._preserve_fill_reference_cache = preserve_references
        try:
            self._raster_fill_visual_changed(obj.object_id, dirty_world)
        finally:
            self._preserve_fill_reference_cache = False
        self.interactionFinished.emit()
        return True

    def _begin_fill_gesture(
        self, obj: RasterObject, world_point: QPointF,
    ) -> None:
        self._clear_fill_replay()
        self._fill_before = {}
        self._fill_dirty_world = QRectF()
        self._fill_operation_base_tiles = self._snapshot_fill_object_tiles(
            obj.object_id
        )
        self._fill_operation_profile = dict(
            self.settings.active_fill_profile()
        )
        self._fill_operation_color = self._active_fill_color()
        self._fill_operation_selection = QPainterPath(
            self._drawing_selection_path
        )
        self._fill_operation_reference_tiles = {}
        self._fill_gesture_points = [
            self._raster_local_point(obj, world_point)
        ]
        self._fill_last_world = QPointF(world_point)
        self._fill_gesture_active = True
        subtool = self.settings.active_fill_subtool
        if subtool not in {"enclose_fill", "lasso_fill"}:
            self._apply_raster_fill(
                obj, world_point, before=self._fill_before, commit=False,
                profile_snapshot=self._fill_operation_profile,
                color=self._fill_operation_color,
                selection_path=self._fill_operation_selection,
                reference_capture=self._fill_operation_reference_tiles,
            )

    def _continue_fill_gesture(
        self, obj: RasterObject, world_point: QPointF,
    ) -> None:
        if not self._fill_gesture_active:
            return
        local = self._raster_local_point(obj, world_point)
        subtool = self.settings.active_fill_subtool
        spacing = (
            max(1.0, 8.0 / max(self.scale, 0.05))
            if subtool == "leftover_pen"
            else max(1.0, 4.0 / max(self.scale, 0.05))
        )
        if self._fill_gesture_points and math.dist(
            self._fill_gesture_points[-1].toTuple(), local.toTuple()
        ) < spacing:
            return
        self._fill_gesture_points.append(local)
        self._fill_last_world = QPointF(world_point)
        if subtool not in {"enclose_fill", "lasso_fill"}:
            self._apply_raster_fill(
                obj, world_point, before=self._fill_before, commit=False,
                profile_snapshot=self._fill_operation_profile,
                color=self._fill_operation_color,
                selection_path=self._fill_operation_selection,
                reference_capture=self._fill_operation_reference_tiles,
            )
        else:
            self.update()

    def _finish_fill_gesture(self, obj: RasterObject) -> None:
        if not self._fill_gesture_active:
            return
        subtool = self.settings.active_fill_subtool
        if (
            subtool in {"enclose_fill", "lasso_fill"}
            and len(self._fill_gesture_points) >= 3
        ):
            path = QPainterPath()
            path.addPolygon(QPolygonF(self._fill_gesture_points))
            path.closeSubpath()
            self._apply_raster_fill(
                obj, None, before=self._fill_before,
                commit=False, extra_path=path,
                profile_snapshot=self._fill_operation_profile,
                color=self._fill_operation_color,
                region_policy=(
                    "transparent" if subtool == "enclose_fill" else "area"
                ),
                selection_path=self._fill_operation_selection,
                reference_capture=self._fill_operation_reference_tiles,
            )
        before = self._fill_before
        dirty = QRectF(self._fill_dirty_world)
        gesture_points = [QPointF(point) for point in self._fill_gesture_points]
        self._fill_before = {}
        self._fill_dirty_world = QRectF()
        self._fill_gesture_points = []
        self._fill_gesture_active = False
        if not before:
            self.interactionFinished.emit()
            self.update()
            return
        after = self.tiles.snapshot(obj.object_id, set(before))
        callback = lambda object_id=obj.object_id, rect=QRectF(dirty): (
            self._raster_fill_visual_changed(object_id, rect)
        )
        label = {
            "enclose_fill": "Enclose and Fill",
            "lasso_fill": "Lasso Fill",
            "leftover_pen": "Leftover Pen",
        }.get(subtool, "Raster fill")
        command = TilePatchCommand(
            label, self.tiles, obj.object_id, before, after, callback,
        )
        self.command_stack.push(command, already_done=True)
        if subtool in {"enclose_fill", "lasso_fill"}:
            replay_path = QPainterPath()
            replay_path.addPolygon(QPolygonF(gesture_points))
            replay_path.closeSubpath()
            steps = [(
                None, replay_path,
                "transparent" if subtool == "enclose_fill" else "area",
            )]
        else:
            steps = [(point, None, "seed") for point in gesture_points]
        self._install_fill_replay(obj, command, steps, dirty)
        self._raster_fill_visual_changed(obj.object_id, dirty)
        self.interactionFinished.emit()

    def _cancel_fill_gesture(self, *, restore: bool = True) -> bool:
        if not self._fill_gesture_active:
            return False
        obj = (
            self.chapter.objects.get(self.selected_object_id)
            if self.chapter is not None else None
        )
        if restore and isinstance(obj, RasterObject):
            for key, image in self._fill_before.items():
                self.tiles.set_tile(obj.object_id, key, image)
            if not self._fill_dirty_world.isEmpty():
                self._raster_fill_visual_changed(
                    obj.object_id, self._fill_dirty_world
                )
        self._fill_before = {}
        self._fill_dirty_world = QRectF()
        self._fill_gesture_points = []
        self._fill_gesture_active = False
        self.update()
        return True

    def fill_active_selection(self) -> bool:
        if self.chapter is None or self._drawing_selection_path.isEmpty():
            return False
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, RasterObject):
            return False
        self._clear_fill_replay()
        profile = dict(self.settings.active_fill_profile())
        color = self._active_fill_color()
        self._fill_operation_base_tiles = self._snapshot_fill_object_tiles(
            obj.object_id
        )
        self._fill_operation_profile = dict(profile)
        self._fill_operation_color = QColor(color)
        self._fill_operation_selection = QPainterPath(
            self._drawing_selection_path
        )
        self._fill_operation_reference_tiles = {}
        frame = QRectF(*obj.interaction_rect).intersected(
            self._drawing_selection_path.boundingRect()
        )
        if (
            len(self.tiles.keys_for_rect(frame)) > 16
        ):
            profile["connected_pixels_only"] = False
            reference_tiles = None
            if str(profile.get("reference_mode", "editing")) != "editing":
                keys = self.tiles.keys_for_rect(frame)
                required = len(keys) * self.tiles.tile_size ** 2 * 4
                if required > self._fill_reference_tile_cache_budget:
                    return self._apply_raster_fill(
                        obj, None, profile_snapshot=profile, color=color,
                        region_policy="area",
                        selection_path=self._fill_operation_selection,
                        reference_capture=self._fill_operation_reference_tiles,
                    )
                entities = self._fill_reference_entities(obj, profile)
                signature = self._fill_reference_signature(entities)
                settings_signature = tuple(sorted(
                    (name, repr(profile.get(name))) for name in (
                        "reference_mode", "exclude_editing_target",
                        "exclude_images",
                        "exclude_gradients", "exclude_mask_only",
                        "fill_up_to_vector_path", "include_vector_path",
                    )
                ))
                reference_tiles = {
                    key: self._fill_reference_tile(
                        obj, key, profile, entities=entities,
                        signature=signature,
                        settings_signature=settings_signature,
                    )
                    for key in keys
                }
                self._fill_operation_reference_tiles = {
                    key: QImage(image) if image is not None else QImage()
                    for key, image in reference_tiles.items()
                }
            return self._start_async_fill(
                obj, None, frame, profile, None, color, "area",
                reference_tiles,
            )
        return self._apply_raster_fill(
            obj, None, profile_snapshot=profile, color=color,
            region_policy="area",
            selection_path=self._fill_operation_selection,
            reference_capture=self._fill_operation_reference_tiles,
        )

    def _apply_shape_fill(self, world: QPointF) -> bool:
        if (
            self.chapter is None
            or self.selected_kind != "layer"
            or self.selected_id not in self.chapter.layers
        ):
            return False
        layer = self.chapter.layers[self.selected_id]
        if (
            layer.bound is None or layer.is_page or not layer.bound.closed
        ):
            return False
        local = self._layer_world_to_local(layer.layer_id, world)
        if not self.layer_effective_path(layer.layer_id).contains(local):
            return False
        before = self.chapter.to_dict()
        layer.shape_style.primary_color = self._active_fill_color().name(
            QColor.NameFormat.HexArgb
        ).upper()
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
            self._clear_vector_eraser_live_cache()
            self._vector_eraser_preview.clear()
            self._vector_eraser_preview_revision += 1
            self._vector_eraser_grid_revision = None
            try:
                self._build_vector_eraser_index(drawing)
                self._build_vector_eraser_background_cache()
            except Exception:
                self._cancel_vector_gesture(restore=True)
                raise
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
        self._vector_sweep = [
            FreehandSample(local.x(), local.y(), pressure)
        ]
        if self._vector_gesture_mode == "eraser":
            try:
                self._update_vector_eraser_preview(drawing)
            except Exception:
                self._cancel_vector_gesture(restore=True)
                raise
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
        drawing = self._selected_vector_drawing()
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
            return
        sample = FreehandSample(local.x(), local.y(), pressure)
        sweep_count = len(self._vector_sweep)
        if not self._vector_sweep:
            self._vector_sweep.append(sample)
        elif math.dist(self._vector_sweep[-1].point, sample.point) >= 0.2:
            self._vector_sweep.append(sample)
        if self._vector_gesture_mode == "simplify":
            self._update_simplify_point_sweep(drawing)
        if self._vector_gesture_mode == "redraw":
            self._manual_redraw_at(drawing, local, pressure)
        elif self._vector_gesture_mode == "connect":
            self._collect_vector_endpoint(drawing, local)
        elif self._vector_gesture_mode == "eraser":
            if len(self._vector_sweep) != sweep_count:
                try:
                    self._update_vector_eraser_preview(drawing)
                except Exception:
                    self._cancel_vector_gesture(restore=True)
                    raise
            return
        self.update()

    def _end_vector_gesture(self) -> None:
        drawing = self._selected_vector_drawing()
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

    # ---- tool actions --------------------------------------------------
    def _begin_modifier_handle(self, widget_point: QPointF) -> bool:
        modifier = self._active_focal_modifier()
        if modifier is None:
            return False
        points = self._focal_points(modifier)
        widget_points = [self.camera_transform().map(point) for point in points]
        hit = next((
            index for index, candidate in enumerate(widget_points)
            if math.dist(widget_point.toTuple(), candidate.toTuple()) <= 12.0
        ), None)
        if hit is None:
            return False
        self._modifier_handle_drag = {
            "handle": ("center", "ramp", "end")[hit],
            "before": self.chapter.to_dict(),
            "press": self.widget_to_document(widget_point),
            "center": tuple(modifier.focal_center),
            "radius": modifier.focal_radius,
            "ramp": modifier.focal_ramp,
            "angle": modifier.focal_angle,
        }
        return True

    def _move_modifier_handle(self, widget_point: QPointF) -> bool:
        state = self._modifier_handle_drag
        modifier = self._active_focal_modifier()
        if state is None or modifier is None:
            return False
        point = self.widget_to_document(widget_point)
        if state["handle"] == "center":
            delta = point - state["press"]
            modifier.focal_center = (
                state["center"][0] + delta.x(),
                state["center"][1] + delta.y(),
            )
        elif state["handle"] == "end":
            center = QPointF(*modifier.focal_center)
            delta = point - center
            modifier.focal_radius = max(1.0, math.hypot(delta.x(), delta.y()))
            modifier.focal_angle = math.atan2(delta.y(), delta.x())
        else:
            center = QPointF(*modifier.focal_center)
            axis = QPointF(
                math.cos(modifier.focal_angle),
                math.sin(modifier.focal_angle),
            )
            projection = QPointF.dotProduct(point - center, axis)
            modifier.focal_ramp = max(
                0.0, min(1.0, projection / modifier.focal_radius)
            )
        modifier.validate()
        self._invalidate_scene_cache()
        self.documentChanged.emit(None)
        self.update()
        return True

    def _finish_modifier_handle(self) -> bool:
        state, self._modifier_handle_drag = self._modifier_handle_drag, None
        if state is None or self.chapter is None:
            return False
        after = self.chapter.to_dict()
        if state["before"] != after:
            self.push_model_change(
                state["before"], after, "Edit focal blur"
            )
        self.interactionFinished.emit()
        return True

    def _dispatch_tool_press(
        self, widget_point: QPointF, pressure: float, modifiers,
    ) -> None:
        """Preserve press modifiers without changing the tool-hook API."""
        self._input_press_modifiers = modifiers
        try:
            self._tool_press(widget_point, pressure)
        finally:
            self._input_press_modifiers = None

    def _tool_press(self, widget_point: QPointF, pressure: float) -> None:
        if self.chapter is None:
            self._clear_detached_input_state()
            return
        modifiers = self._input_press_modifiers
        if modifiers is None:
            modifiers = QGuiApplication.keyboardModifiers()
        point = self.widget_to_document(widget_point)
        self._press_widget_point = QPointF(widget_point)
        self._press_document_point = QPointF(point)
        if (
            self.active_tone_mask_id
            and self.tool in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}
        ):
            self._begin_mask_stroke(point, pressure)
            return
        if self.active_tone_mask_id and self.tool == ToolKind.FILL:
            return
        if self.tool == ToolKind.EYEDROPPER:
            self._eyedropper_widget_point = QPointF(widget_point)
            if self._sample_eyedropper(point):
                self._eyedropper_sampling = True
                self.eyedropperGestureChanged.emit(True)
            return
        if self._begin_modifier_handle(widget_point):
            return
        if self.tool == ToolKind.TRANSFORM and self._begin_multi_transform(point):
            return
        if (
            self.tool == ToolKind.TRANSFORM
            and self.selected_kind == "layer"
            and self._begin_geometry_transform(point)
        ):
            return
        if self._begin_page_gap_interaction(point):
            return
        if self.tool == ToolKind.INSERT_PAGE_GAP:
            self._update_page_gap_hover(point)
            self._insert_hovered_page_gap()
            return
        if (
            self.tool == ToolKind.SHAPE_CREATE and self._creation_nodes
            and self._begin_creation_shape_interaction(point, widget_point)
        ):
            return
        if self.tool == ToolKind.SHAPE_EDIT:
            target = self._shape_edit_target()
            if target is not None:
                bound, transform, style = target
                inverse, valid = transform.inverted()
                if not valid:
                    return
                local = inverse.map(point)
                hit = self._shape_hit_test(
                    bound, local, geometry_only=style is None
                )
                if hit is not None and hit.get("kind") != "interior" \
                        and self._begin_shape_edit(
                            point, allow_interior=False, modifiers=modifiers
                        ):
                    return
        if self._begin_shape_overlay_interaction(widget_point):
            return
        if self._begin_text_property_drag(widget_point):
            return
        if self._begin_selected_text_transform(point):
            return
        if self._transform_mode_gizmo_hit(widget_point):
            return
        if self.tool in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
            ToolKind.DRAW_SHAPE,
        }:
            if self.tool == ToolKind.DRAW_SHAPE:
                if self._selection_operation() == "replace" and not self._drawing_selection_path.isEmpty():
                    mods = QApplication.keyboardModifiers()
                    if not (mods & Qt.ShiftModifier or mods & Qt.AltModifier or mods & Qt.ControlModifier):
                        before = self._selection_snapshot()
                        self._drawing_selection_path = QPainterPath()
                        after = self._selection_snapshot()
                        self._push_selection_undo(before, after)
                self._begin_drawing_selection(point, widget_point, test_transform=False)
                return
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
            if self._is_transformable_object(selected_raster):
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
                bounds = QRectF(*obj.derived_bounds())
                if obj.strokes and not bounds.isEmpty():
                    radius = (
                        self.settings.active_eraser_pixels() / 2
                        + 4 / max(self.scale, 0.05)
                        if self.tool == ToolKind.RASTER_ERASER else 0.0
                    )
                    bounds.adjust(-radius, -radius, radius, radius)
                local = self._vector_local_point(obj, point)
                if not obj.strokes or bounds.contains(local):
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
            selected = self.chapter.objects.get(self.selected_object_id)
            if isinstance(selected, RasterObject):
                self._begin_fill_gesture(selected, point)
            elif self.selected_kind == "layer":
                self._apply_shape_fill(point)
            return
        if self.tool == ToolKind.GRADIENT:
            selected_gradient = self.chapter.objects.get(
                self.selected_object_id
            )
            if isinstance(selected_gradient, GradientObject):
                self._begin_gradient_edit(selected_gradient, point)
                return
            parent_id = self._gradient_tool_parent_id()
            field_type = (
                self._gradient_creation_type or self._gradient_tool_field_type
            )
            if not parent_id:
                return
            matches = self.chapter.gradient_children(
                parent_id, field_type, family="color_fill"
            )
            if matches:
                self.set_selection(
                    "object", matches[0].object_id,
                    activate_default_tool=False,
                )
                self._begin_gradient_edit(matches[0], point)
                return
            local = self._layer_world_to_local(parent_id, point)
            if not self.layer_effective_path(parent_id).contains(local):
                return
            before = self.chapter.to_dict()
            if field_type == "parent_shape":
                self.create_gradient(
                    parent_id, field_type, before=before,
                    gradient_type="color_fill",
                )
                return
            self._gradient_creation_parent_id = parent_id
            self._gradient_creation_type = field_type
            self._gradient_creation_family = "color_fill"
            self._gradient_creation_before = before
            snapped = self._snap(point, parent_id)
            self._creation_points = [
                snapped.toTuple(), snapped.toTuple()
            ]
            return
        if self.tool == ToolKind.TEXT_EDIT and not isinstance(
            self.chapter.objects.get(self.selected_object_id), TextObject
        ):
            if self._select_foreign_object_at(point, widget_point):
                return
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
                isinstance(selected_gradient, SpeedLineCenterObject)
                and self._begin_shape_edit(
                    point, allow_interior=False, modifiers=modifiers
                )
            ):
                return
            if (
                self.selected_kind == "layer"
                and self._begin_shape_edit(
                    point, allow_interior=False, modifiers=modifiers
                )
            ):
                return
            if (
                self.selected_kind == "layer"
                and self._begin_shape_edit(point, modifiers=modifiers)
            ):
                return
            if self._begin_geometry_transform(point):
                return
            if self._select_foreign_object_at(point, widget_point):
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
                isinstance(selected_gradient, SpeedLineCenterObject)
                and self._begin_shape_edit(point, modifiers=modifiers)
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
            mode, handle = self._text_transform_control_hit(world_quad, point)
            if mode:
                self.commit_active_text_edit()
                obj = self.chapter.objects[self.selected_object_id]
                world_quad = self.object_world_quad(obj.object_id)
                inverse, valid = self.layer_world_transform(
                    obj.parent_layer_id
                ).inverted()
                if not valid:
                    return
                self._transform_handle_index = handle
                self._transform_drag_mode = mode
                self._model_before = self.chapter.to_dict()
                self._drag_start_doc = point
                self._transform_start_quad = [
                    inverse.map(QPointF(x, y)).toTuple()
                    for x, y in world_quad
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
                self._build_raster_transform_cache()
                self._build_text_transform_cache(obj)
                return
        if self.tool == ToolKind.TEXT_EDIT and self.selected_object_id:
            if self._begin_text_pointer(point):
                return
            if self._select_foreign_object_at(point, widget_point):
                return
        if self.tool == ToolKind.TRANSFORM and self.selected_object_id:
            obj = self.chapter.objects[self.selected_object_id]
            if isinstance(obj, TextObject) and obj.layout_mode != "free":
                return
            world_quad = self.object_world_quad(self.selected_object_id)
            if not world_quad:
                return
            inverse, valid = self.layer_world_transform(
                obj.parent_layer_id
            ).inverted()
            if not valid:
                return
            local_quad = [
                inverse.map(QPointF(x, y)).toTuple()
                for x, y in world_quad
            ]
            mode, handle = self._transform_control_hit(world_quad, point)
            if not mode:
                self._select_foreign_object_at(point, widget_point)
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
            elif isinstance(obj, TextObject):
                self._build_raster_transform_cache()
                self._build_text_transform_cache(obj)
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
            if not self._creation_nodes:
                if self._page_creation_anchor_id:
                    self._creation_parent_id = self._page_creation_anchor_id
                    self._creation_insertion_index = 0
                elif self._gradient_creation_parent_id:
                    self._creation_parent_id = self._gradient_creation_parent_id
                    self._creation_insertion_index = 0
                else:
                    placement = self._target_placement_for_new_bound()
                    if placement is None:
                        return
                    self._creation_parent_id = placement[0]
                    self._creation_insertion_index = placement[1]
                self._creation_compound_operation = "add"
            target = self._creation_parent_id
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
        if self.chapter is None:
            self._clear_detached_input_state()
            return
        point = self.widget_to_document(widget_point)
        if self.active_tone_mask_id and self._drawing:
            self._continue_mask_stroke(point, pressure)
            return
        if self.tool == ToolKind.EYEDROPPER and self._eyedropper_sampling:
            self._eyedropper_widget_point = QPointF(widget_point)
            self._sample_eyedropper(point)
            self.update()
            return
        if self._move_modifier_handle(widget_point):
            return
        if self.tool == ToolKind.VECTOR_EDIT and self._vector_before is None:
            drawing = self._selected_vector_drawing()
            hovered = ""
            if drawing is not None:
                hit = self._hit_vector_point(
                    drawing, self._vector_local_point(drawing, point)
                )
                hovered = hit[1] if hit else ""
            if hovered != self._hover_vector_point_id:
                self._hover_vector_point_id = hovered
                self.update()
        if self._shape_property_drag is not None:
            self._update_shape_property_drag(widget_point)
            return
        if self._text_property_drag is not None:
            self._update_text_property_drag(widget_point)
            return
        if self._page_gap_drag_mode is not None:
            self._move_page_gap_interaction(point)
            return
        if self.tool == ToolKind.INSERT_PAGE_GAP:
            self._update_page_gap_hover(point)
            return
        if (
            self._geometry_transform_target is not None
            and self._transform_start_quad is not None
        ):
            self._update_geometry_transform_preview(point)
            self.update()
            return
        if (
            self._model_before is not None
            and self._transform_start_quad is not None
            and isinstance(
                self.chapter.objects.get(self.selected_object_id),
                (RasterObject, VectorDrawingObject, ImageObject),
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
            if self._is_transformable_object(selected_raster):
                world_quad = self.object_world_quad(
                    selected_raster.object_id
                )
                raster_hit, _handle = (
                    self._selected_object_transform_hit(
                        selected_raster, world_quad, point
                    )
                    if world_quad else ("", None)
                )
                if raster_hit == "translate":
                    self.setCursor(Qt.OpenHandCursor)
                elif raster_hit:
                    self.setCursor(Qt.CrossCursor)
                else:
                    self.unsetCursor()
            elif (
                self.tool in {ToolKind.SHAPE_EDIT, ToolKind.GRADIENT}
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
            ToolKind.DRAW_SHAPE,
        }:
            if self.tool != ToolKind.DRAW_SHAPE and self._pending_drawing_selection_press is not None:
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
            if self.tool == ToolKind.DRAW_SHAPE and self._pending_drawing_selection_press is not None:
                self._pending_drawing_selection_press = None
            self._continue_drawing_selection(point, widget_point)
            return
        if self._vector_gesture_mode is not None:
            self._continue_vector_gesture(point, pressure)
            return
        if self._fill_gesture_active:
            obj = self.chapter.objects.get(self.selected_object_id)
            if isinstance(obj, RasterObject):
                self._continue_fill_gesture(obj, point)
            return
        if (
            self.tool == ToolKind.GRADIENT
            and self._gradient_creation_parent_id
            and len(self._creation_points) >= 2
        ):
            snapped = self._snap(point, self._gradient_creation_parent_id)
            self._creation_points[-1] = snapped.toTuple()
            self.update()
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
            self._model_before is not None
            and self._transform_start_quad is not None
            and self._transform_preview_quad is not None
        ):
            self._update_transform_preview(point)
            self.update()
            return
        selected_gradient = self.chapter.objects.get(
            self.selected_object_id
        )
        if (
            self.tool in {ToolKind.SHAPE_EDIT, ToolKind.GRADIENT}
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
            local_point = self._raster_local_point(obj, point)
            if self.settings.snap_to_grid:
                snapped = self._snap(point, obj.parent_layer_id)
                local_point = self._raster_local_point(obj, snapped)
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
        if self.active_tone_mask_id and self._drawing:
            self._end_mask_stroke()
            return
        if self._eyedropper_sampling:
            color = self._eyedropper_last_color
            self._eyedropper_sampling = False
            self._eyedropper_last_color = ""
            self._eyedropper_widget_point = None
            if color:
                self.colorSampleCommitted.emit(color)
            self.eyedropperGestureChanged.emit(False)
            self.interactionFinished.emit()
            return
        if self._finish_modifier_handle():
            return
        if self._finish_shape_property_drag():
            return
        if self._finish_text_property_drag():
            return
        if self.chapter is None:
            self._clear_detached_input_state()
            return
        if self._finish_page_gap_interaction():
            return
        if self.tool == ToolKind.INSERT_PAGE_GAP:
            return
        if (
            self._model_before is not None
            and self._transform_preview_quad is not None
            and self._is_transformable_object(
                self.chapter.objects.get(self.selected_object_id)
            )
        ):
            self._commit_object_transform()
            self.interactionFinished.emit()
            return
        if self.tool in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
            ToolKind.DRAW_SHAPE,
        }:
            if self.tool != ToolKind.DRAW_SHAPE and self._pending_drawing_selection_press is not None:
                widget_point, point, _pressure = (
                    self._pending_drawing_selection_press
                )
                self._pending_drawing_selection_press = None
                self._request_object_selection(point, widget_point)
                self.interactionFinished.emit()
                return
            if self.tool == ToolKind.DRAW_SHAPE and self._pending_drawing_selection_press is not None:
                self._pending_drawing_selection_press = None
                self.interactionFinished.emit()
                return
            self._finish_drawing_selection()
            self.interactionFinished.emit()
            return
        if self._vector_gesture_mode is not None:
            self._end_vector_gesture()
            return
        if self._fill_gesture_active:
            obj = self.chapter.objects.get(self.selected_object_id)
            if isinstance(obj, RasterObject):
                self._finish_fill_gesture(obj)
            else:
                self._cancel_fill_gesture(restore=True)
            return
        if (
            self.tool == ToolKind.GRADIENT
            and self._gradient_creation_parent_id
            and len(self._creation_points) >= 2
        ):
            first, second = self._creation_points[0], self._creation_points[-1]
            field_type = self._gradient_creation_type
            parent_id = self._gradient_creation_parent_id
            before = self._gradient_creation_before
            if math.dist(first, second) < 2:
                self._cancel_gradient_creation()
                self.interactionFinished.emit()
                return
            if field_type == "radial":
                self.create_gradient(
                    parent_id, "radial",
                    radial=(first, math.dist(first, second)), before=before,
                    gradient_type="color_fill",
                )
            else:
                geometry = BoundGeometry.path([
                    PathNode(x=first[0], y=first[1]),
                    PathNode(x=second[0], y=second[1]),
                ], False)
                self.create_gradient(
                    parent_id, "line", world_geometry=geometry,
                    before=before, gradient_type="color_fill",
                )
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
            and self._geometry_transform_target is None
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
            self._rectangle_roundness_linked = False
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
                    gradient_type=self._gradient_creation_family,
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
            or self.chapter.layers[parent_id].layer_kind == "open_shape"
        ):
            return False
        self._raster_creation_parent_id = parent_id
        self._raster_creation_index = insertion_index
        self._creation_points.clear()
        return self.set_tool(ToolKind.RASTER_CREATE)

    def begin_gradient_creation(
        self, parent_id: str, field_type: str,
        gradient_type: str = "color_fill",
    ) -> bool:
        if (
            self.chapter is None
            or parent_id not in self.chapter.layers
            or field_type not in {"line", "radial", "parent_shape"}
            or gradient_type != "color_fill"
        ):
            return False
        family = "color_fill"
        self._gradient_tool_field_type = field_type
        existing = self.chapter.gradient_children(
            parent_id, field_type, family=family
        )
        if existing:
            self.set_selection("object", existing[0].object_id)
            return self.set_tool(ToolKind.GRADIENT)
        self._gradient_creation_parent_id = parent_id
        self._gradient_creation_type = field_type
        self._gradient_creation_family = gradient_type
        self._gradient_creation_before = self.chapter.to_dict()
        self.set_selection(
            "layer", parent_id, activate_default_tool=False
        )
        return self.set_tool(ToolKind.GRADIENT)

    def set_gradient_field_type(self, field_type: str) -> None:
        if field_type not in {"line", "radial", "parent_shape"}:
            return
        self._gradient_tool_field_type = field_type
        if not self._creation_points:
            self._gradient_creation_type = ""

    def _gradient_tool_parent_id(self) -> str:
        if self.chapter is None:
            return ""
        if self._gradient_creation_parent_id in self.chapter.layers:
            return self._gradient_creation_parent_id
        if self.selected_kind == "layer":
            layer = self.chapter.layers.get(self.selected_id)
            return (
                layer.layer_id
                if layer is not None and layer.bound is not None else ""
            )
        obj = self.chapter.objects.get(self.selected_object_id)
        return obj.parent_layer_id if obj is not None else ""

    def _cancel_gradient_creation(self) -> None:
        self._gradient_creation_parent_id = ""
        self._gradient_creation_type = ""
        self._gradient_creation_family = "color_fill"
        self._gradient_creation_before = None
        self._clear_creation_gesture()
        self.update()

    def create_gradient(
        self, parent_id: str, field_type: str,
        *, world_geometry: BoundGeometry | None = None,
        radial: tuple[tuple[float, float], float] | None = None,
        before: dict | None = None,
        gradient_type: str = "color_fill",
    ) -> ColorFillGradientObject | SpeedLinesGradientObject | None:
        if (
            self.chapter is None
            or parent_id not in self.chapter.layers
            or field_type not in {"line", "radial", "parent_shape"}
            or gradient_type != "color_fill"
        ):
            return None
        before = before or self.chapter.to_dict()
        parent_transform = self.layer_world_transform(parent_id)
        parent_inverse, valid = parent_transform.inverted()
        if not valid:
            return None
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
                    node.position = parent_inverse.map(
                        QPointF(*node.position)
                    ).toTuple()
                    if node.incoming is not None:
                        node.incoming = parent_inverse.map(
                            QPointF(*node.incoming)
                        ).toTuple()
                    if node.outgoing is not None:
                        node.outgoing = parent_inverse.map(
                            QPointF(*node.outgoing)
                        ).toTuple()
            local.closed = False
            local.normalize_bezier_handles()
            obj.line_field = LineGradientField(local)
        elif radial is not None:
            (world_x, world_y), radius = radial
            local_center = parent_inverse.map(QPointF(world_x, world_y))
            local_edge = parent_inverse.map(QPointF(
                world_x + radius, world_y
            ))
            local_radius = max(1.0, math.dist(
                local_center.toTuple(), local_edge.toTuple()
            ))
            obj.radial_field = RadialGradientField(
                origin_x=local_center.x(),
                origin_y=local_center.y(),
                radius_x=local_radius,
                radius_y=local_radius,
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
        parent_transform = self.layer_world_transform(parent_id)
        parent_inverse, valid = parent_transform.inverted()
        if not valid:
            return
        count = sum(
            isinstance(item, RasterObject)
            for item in self.chapter.objects.values()
        ) + 1
        obj = RasterObject(
            name=f"Raster {count}",
            interaction_rect=(0.0, 0.0, world.width(), world.height()),
        )
        local_quad = [
            parent_inverse.map(point).toTuple()
            for point in (
                world.topLeft(), world.topRight(),
                world.bottomRight(), world.bottomLeft(),
            )
        ]
        if parent_transform.type() in {
            QTransform.TransformationType.TxNone,
            QTransform.TransformationType.TxTranslate,
        }:
            obj.x, obj.y = local_quad[0]
        else:
            obj.transform_frame = (0.0, 0.0, world.width(), world.height())
            obj.transform_quad = local_quad
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
        local = self._layer_world_to_local(layer.layer_id, point)
        if layer.bound is None:
            return False
        path = self.layer_effective_path(layer.layer_id)
        if path.contains(local):
            return False
        if layer.layer_kind == "open_shape":
            return True
        stroker = QPainterPathStroker()
        stroker.setWidth(24.0 / max(self.scale, 0.05))
        world_path = self.layer_world_transform(layer.layer_id).map(path)
        return not stroker.createStroke(world_path).contains(point)

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

    def _select_foreign_object_at(
        self, point: QPointF, widget_point: QPointF,
    ) -> bool:
        """Select the frontmost other object before a tool falls back to a layer."""
        hits = [
            hit for hit in self.hit_test_entities(point)
            if hit["kind"] == "object" and hit["id"] != self.selected_object_id
        ]
        if not hits:
            return False
        if len(hits) > 1 and QGuiApplication.keyboardModifiers() & Qt.ControlModifier:
            self.selectionCandidatesRequested.emit(
                hits, self.mapToGlobal(widget_point.toPoint())
            )
            return True
        self.set_selection("object", hits[0]["id"], activate_default_tool=True)
        return True

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

    def _shape_edit_target(
        self,
    ) -> tuple[BoundGeometry, QTransform, ShapeStyle | None] | None:
        """Resolve the geometry edited by the shape-edit tool.

        Returns geometry, its local-to-document transform, and optional style.
        """
        if self.selected_kind == "layer":
            layer = self.chapter.layers.get(self.selected_id)
            if layer is None or layer.bound is None:
                return None
            return (
                layer.bound,
                self.layer_world_transform(layer.layer_id),
                layer.shape_style,
            )
        if self.selected_kind == "object":
            obj = self.chapter.objects.get(self.selected_id)
            if isinstance(obj, SpeedLineCenterObject):
                return (
                    obj.geometry,
                    self.layer_world_transform(obj.parent_layer_id),
                    None,
                )
        return None

    def _delete_selected_shape_node(
        self, bound: BoundGeometry,
    ) -> bool:
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
        modifiers=None,
    ) -> bool:
        target = self._shape_edit_target()
        if target is None:
            return False
        bound, transform, style = target
        inverse, valid = transform.inverted()
        if not valid:
            return False
        local = inverse.map(world_point)
        bound.normalize_bezier_handles()
        hit = self._shape_hit_test(
            bound, local, geometry_only=style is None
        )
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
                self._delete_selected_shape_node(bound)
            elif name == "cap" and style is not None:
                before = self.chapter.to_dict()
                self._cycle_shape_cap(
                    self.chapter.layers[self.selected_id], selected
                )
                self._push_immediate_shape_change(before, "Change line cap")
            else:
                self._model_before = self.chapter.to_dict()
                self._active_shape_control = name
                self._drag_start_doc = QPointF(world_point)
                self._shape_control_dragged = False
                if name == "translate" and self.selected_kind == "layer":
                    layer = self.chapter.layers[self.selected_id]
                    self._drag_start_value = {
                        "translate_x": layer.translate_x,
                        "translate_y": layer.translate_y,
                    }
            return True
        if kind == "radius":
            index = hit["index"]
            self._selected_shape_node_id = bound.nodes[index].node_id
            self._model_before = self.chapter.to_dict()
            self._active_shape_control = f"primitive_roundness:{index}"
            self._drag_start_value = bound.to_dict()
            self._rectangle_roundness_linked = bool(
                (
                    QGuiApplication.keyboardModifiers()
                    if modifiers is None else modifiers
                ) & Qt.ControlModifier
            )
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
                insert_layer_id = (
                    self.chapter.objects[
                        self.selected_id
                    ].parent_layer_id
                    if self.selected_kind == "object"
                    else self.selected_id
                )
                self._pending_primitive_insert = (
                    insert_layer_id, index, percent,
                    QPointF(insert_point),
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
            if self.selected_kind == "layer":
                layer = self.chapter.layers[self.selected_id]
                self._drag_start_value = {
                    "translate_x": layer.translate_x,
                    "translate_y": layer.translate_y,
                }
            else:
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
        target = self._shape_edit_target()
        if target is None:
            return
        dirty_before = entity_visual_bounds(
            self.chapter, self.tiles, self.selected_kind, self.selected_id
        )
        bound, transform, _style = target
        inverse, valid = transform.inverted()
        if not valid:
            return
        layer_id = (
            self.chapter.objects[self.selected_id].parent_layer_id
            if self.selected_kind == "object"
            else self.selected_id
        )
        local = inverse.map(world_point)
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
            snapped = self._snap(world_point, layer_id)
            original = BoundGeometry.from_dict(self._drag_start_value)
            effective_index = self._move_bound_handle(
                bound, index, inverse.map(snapped), original,
            )
            if effective_index < 4:
                self._selected_shape_node_id = (
                    bound.nodes[effective_index].node_id
                )
        elif control.startswith("rectangle_point:"):
            index = int(control.split(":", 1)[1])
            snapped = self._snap(world_point, layer_id)
            bound.nodes[index].position = inverse.map(snapped).toTuple()
        elif control.startswith("rectangle_edge:"):
            index = int(control.split(":", 1)[1])
            original = BoundGeometry.from_dict(self._drag_start_value)
            first_index, second_index = index, (index + 1) % 4
            original_midpoint_local = QPointF(
                (
                    original.nodes[first_index].x
                    + original.nodes[second_index].x
                ) / 2,
                (
                    original.nodes[first_index].y
                    + original.nodes[second_index].y
                ) / 2,
            )
            original_midpoint = transform.map(original_midpoint_local)
            destination = world_point + (
                original_midpoint - self._drag_start_doc
            )
            if self.settings.snap_to_grid:
                destination = self._snap(destination, layer_id)
            local_destination = inverse.map(destination)
            delta = local_destination - original_midpoint_local
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
            radius = min(
                maximum,
                projected / math.sqrt(2),
            )
            if self._rectangle_roundness_linked:
                shared_maximum = min(
                    min(
                        math.dist(
                            candidate.position,
                            original.nodes[i - 1].position,
                        ),
                        math.dist(
                            candidate.position,
                            original.nodes[
                                (i + 1) % len(original.nodes)
                            ].position,
                        ),
                    ) / 2
                    for i, candidate in enumerate(original.nodes)
                )
                radius = min(radius, shared_maximum)
                for candidate in bound.nodes:
                    candidate.roundness = radius
                    candidate.roundness_enabled = radius > 0
            else:
                bound.nodes[index].roundness = radius
                bound.nodes[index].roundness_enabled = radius > 0
        elif control == "node" and selected is not None:
            snapped = self._snap(world_point, layer_id)
            target = inverse.map(snapped)
            primary_start = self._shape_drag_nodes.get(
                selected.node_id, self._drag_start_value
            )
            primary_position = primary_start["position"]
            dx = target.x() - float(primary_position[0])
            dy = target.y() - float(primary_position[1])
            for contour in bound.iter_contours():
                for node in contour.nodes:
                    source = self._shape_drag_nodes.get(node.node_id)
                    if source is None:
                        continue
                    source_position = source["position"]
                    node.position = (
                        float(source_position[0]) + dx,
                        float(source_position[1]) + dy,
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
            snapped = self._snap(world_point, layer_id)
            target = inverse.map(snapped).toTuple()
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
            if self.selected_kind == "layer":
                layer = self.chapter.layers[self.selected_id]
                parent_transform = (
                    self.layer_world_transform(layer.parent_id)
                    if layer.parent_id else QTransform()
                )
                parent_inverse, parent_valid = parent_transform.inverted()
                if not parent_valid:
                    return
                start = parent_inverse.map(self._drag_start_doc)
                current = parent_inverse.map(world_point)
                start_x = float(self._drag_start_value["translate_x"])
                start_y = float(self._drag_start_value["translate_y"])
                destination = QPointF(
                    start_x + current.x() - start.x(),
                    start_y + current.y() - start.y(),
                )
                if self.settings.snap_to_grid:
                    world_destination = parent_transform.map(destination)
                    snapped = self._snap(world_destination, layer.layer_id)
                    destination = parent_inverse.map(snapped)
                layer.translate_x = destination.x()
                layer.translate_y = destination.y()
            else:
                original = BoundGeometry.from_dict(self._drag_start_value)
                local_start = inverse.map(self._drag_start_doc)
                local_current = inverse.map(world_point)
                dx = local_current.x() - local_start.x()
                dy = local_current.y() - local_start.y()
                bound.nodes = [
                    PathNode.from_dict(node.to_dict())
                    for node in original.nodes
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
                                node.incoming[0] + dx,
                                node.incoming[1] + dy,
                            )
                        if node.outgoing:
                            node.outgoing = (
                                node.outgoing[0] + dx,
                                node.outgoing[1] + dy,
                            )
        bound.normalize_bezier_handles()
        dirty_after = entity_visual_bounds(
            self.chapter, self.tiles, self.selected_kind, self.selected_id
        )
        dirty = self._entity_expanded_dirty(
            self.selected_kind, self.selected_id,
            dirty_before.united(dirty_after).adjusted(-3, -3, 3, 3),
        )
        self.documentChanged.emit(dirty)

    def _update_shape_hover(self, world_point: QPointF) -> None:
        target = self._shape_edit_target()
        if target is None:
            self._shape_hover_target = None
            self._shape_hover_insert = None
            self.setToolTip("")
            return
        bound, transform, style = target
        inverse, valid = transform.inverted()
        if not valid:
            return
        local = inverse.map(world_point)
        hit = self._shape_hit_test(
            bound, local, geometry_only=style is None
        )
        self._shape_hover_target = hit
        self._shape_hover_insert = (
            hit["insert"] if hit and hit["kind"] == "insert" else None
        )
        self.setToolTip(self._shape_hit_tooltip(bound, hit, style))
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
                gradient_type=self._gradient_creation_family,
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
        placement = (
            (self._creation_parent_id, self._creation_insertion_index)
            if self._creation_parent_id
            and self._creation_insertion_index is not None
            else None
        )
        compound_operation = self._creation_compound_operation
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
        self._creation_parent_id = ""
        self._creation_insertion_index = None
        self._creation_compound_operation = "add"
        created = self._create_layer_from_world_bound(
            bound, style=style, placement=placement,
            compound_operation=compound_operation,
        )
        if created is not None:
            self.set_tool(ToolKind.SHAPE_EDIT)

    def _create_layer_from_world_bound(
        self, bound: BoundGeometry, style: ShapeStyle | None = None,
        *, placement: tuple[str, int] | None = None,
        compound_operation: str = "add",
    ) -> LayerNode | None:
        placement = placement or self._target_placement_for_new_bound()
        if placement is None:
            return None
        parent_id, insertion_index = placement
        before = self.chapter.to_dict()
        local = BoundGeometry.from_dict(bound.to_dict())
        parent_inverse, valid = self.layer_world_transform(
            parent_id
        ).inverted()
        if not valid:
            return None
        for contour in local.iter_contours():
            for node in contour.nodes:
                node.position = parent_inverse.map(
                    QPointF(*node.position)
                ).toTuple()
                if node.incoming:
                    node.incoming = parent_inverse.map(
                        QPointF(*node.incoming)
                    ).toTuple()
                if node.outgoing:
                    node.outgoing = parent_inverse.map(
                        QPointF(*node.outgoing)
                    ).toTuple()
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
        layer.compound_operation = (
            compound_operation
            if compound_operation in {"add", "subtract", "ignore"}
            else "add"
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
        root_world = self.layer_world_transform(layer_id)
        root_inverse, valid = root_world.inverted()
        if not valid:
            return False
        removed_layers: set[str] = set()

        def old_parent_to_root(parent_id: str, point: QPointF) -> QPointF:
            return root_inverse.map(
                self.layer_world_transform(parent_id).map(point)
            )

        def translation_between_parents(
            parent_id: str,
        ) -> tuple[float, float] | None:
            origin = old_parent_to_root(parent_id, QPointF())
            axis_x = old_parent_to_root(parent_id, QPointF(1, 0)) - origin
            axis_y = old_parent_to_root(parent_id, QPointF(0, 1)) - origin
            if (
                abs(axis_x.x() - 1.0) < 1e-7
                and abs(axis_x.y()) < 1e-7
                and abs(axis_y.x()) < 1e-7
                and abs(axis_y.y() - 1.0) < 1e-7
            ):
                return origin.x(), origin.y()
            return None

        def removed_opacity(parent_id: str) -> float:
            factor = 1.0
            cursor = parent_id
            while cursor and cursor != layer_id:
                current = self.chapter.layers[cursor]
                factor *= current.opacity
                cursor = current.parent_id
            return factor

        def reparent_object(obj: DocumentObject) -> ChildRef:
            old_parent_id = obj.parent_layer_id
            moved_by_quad = False
            if isinstance(obj, TextObject):
                if (
                    obj.layout_mode == "strict"
                    and obj.geometry_reference == "direct"
                ):
                    rect = self._strict_text_rect(obj)
                    obj.transform_quad = [
                        old_parent_to_root(
                            old_parent_id, QPointF(*point)
                        ).toTuple()
                        for point in self._rect_quad(rect)
                    ]
                    obj.layout_mode = "free"
                elif obj.layout_mode == "free":
                    source_quad = self._text_quad(obj)
                    obj.transform_quad = [
                        old_parent_to_root(
                            old_parent_id, QPointF(x, y)
                        ).toTuple()
                        for x, y in source_quad
                    ]
                    moved_by_quad = True
            elif isinstance(
                obj, (RasterObject, VectorDrawingObject, ImageObject)
            ):
                simple_offset = translation_between_parents(old_parent_id)
                if simple_offset is not None and obj.transform_quad is None:
                    if obj.transform_frame is not None:
                        left, top, width, height = obj.transform_frame
                        obj.transform_frame = (
                            left + simple_offset[0],
                            top + simple_offset[1], width, height,
                        )
                else:
                    if obj.transform_frame is None:
                        obj.transform_frame = self._object_transform_frame(obj)
                    source_quad = (
                        list(obj.transform_quad)
                        if obj.transform_quad is not None
                        else self._rect_quad(QRectF(*obj.transform_frame))
                    )
                    obj.transform_quad = [
                        old_parent_to_root(
                            old_parent_id, QPointF(x, y)
                        ).toTuple()
                        for x, y in source_quad
                    ]
                    moved_by_quad = True
            elif isinstance(obj, GradientObject):
                for contour in obj.line_field.geometry.iter_contours():
                    for node in contour.nodes:
                        node.position = old_parent_to_root(
                            old_parent_id, QPointF(node.x, node.y)
                        ).toTuple()
                        if node.incoming is not None:
                            node.incoming = old_parent_to_root(
                                old_parent_id, QPointF(*node.incoming)
                            ).toTuple()
                        if node.outgoing is not None:
                            node.outgoing = old_parent_to_root(
                                old_parent_id, QPointF(*node.outgoing)
                            ).toTuple()
                radial = obj.radial_field
                radial.origin_x, radial.origin_y = old_parent_to_root(
                    old_parent_id,
                    QPointF(radial.origin_x, radial.origin_y),
                ).toTuple()
                if radial.manual_center is not None:
                    radial.manual_center = old_parent_to_root(
                        old_parent_id, QPointF(*radial.manual_center)
                    ).toTuple()
                if obj.shape_field.manual_center is not None:
                    obj.shape_field.manual_center = old_parent_to_root(
                        old_parent_id,
                        QPointF(*obj.shape_field.manual_center),
                    ).toTuple()
                obj.touch_revision()
            if not moved_by_quad:
                obj.x, obj.y = old_parent_to_root(
                    old_parent_id, QPointF(obj.x, obj.y)
                ).toTuple()
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
            candidate_world = self.layer_world_transform(candidate.layer_id)
            left, top, width, height = candidate.bound.bbox()
            frame = QRectF(
                left, top, max(1.0, width), max(1.0, height)
            )
            destination = [
                root_inverse.map(
                    candidate_world.map(QPointF(*point))
                ).toTuple()
                for point in self._rect_quad(frame)
            ]
            candidate.parent_id = layer_id
            candidate.transform_frame = (
                frame.x(), frame.y(), frame.width(), frame.height()
            )
            candidate.transform_quad = destination
            candidate.translate_x = 0.0
            candidate.translate_y = 0.0
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
            else:
                rebuilt.extend(flatten_branch(child))
                removed_layers.add(child.layer_id)
        removed_modifier_ids: list[str] = []
        for removed_id in removed_layers:
            removed = self.chapter.layers.pop(removed_id, None)
            if removed is not None:
                removed_modifier_ids.extend(removed.modifier_ids)
        layer.children = rebuilt
        layer.bound = geometry
        layer.layer_kind = "bounded"
        layer.compound_enabled = False
        layer.compound_operation = "add"
        self.chapter._garbage_collect_modifiers(removed_modifier_ids)
        self._compound_path_cache.clear()
        after = self.chapter.to_dict()
        self.push_model_change(before, after, "Flatten compound shape")
        self.documentChanged.emit(QRectF())
        self.hierarchyChanged.emit()
        self.set_selection("layer", layer_id)
        self.update()
        return True

    def _begin_mask_stroke(self, point: QPointF, pressure: float) -> None:
        mask = (
            self.chapter.masks.get(self.active_tone_mask_id)
            if self.chapter else None
        )
        if mask is None:
            return
        self._drawing = True
        self._last_draw_point = QPointF(point)
        self._last_pressure = self._effective_pressure(pressure)
        self._stroke_preset = BrushPreset.from_dict(self._preset.to_dict())
        self._stroke_base_size = float(
            self.settings.active_eraser_pixels()
            if self.tool == ToolKind.RASTER_ERASER
            else self.settings.pencil_size()
        )
        self._stroke_before = {}
        self._mask_stroke_dirty = QRectF()
        self._mask_stroke_revision_before = mask.revision
        self._mask_sample_queue.clear()
        self._mask_has_painted_sample = False
        self._mask_sample_queue.append(
            (QPointF(point), float(pressure), time.monotonic())
        )
        # The initial contact must appear immediately. Subsequent packets are
        # collected and painted together at the next event-loop opportunity.
        self._flush_mask_samples()

    def _mask_brush_values(self, pressure: float) -> tuple[float, float]:
        """Return the dedicated mask brush's constant size and alpha."""
        if self.tool == ToolKind.RASTER_ERASER:
            return self._brush_values(pressure)
        if self.settings.mask_pencil_pressure_sensitive:
            amount = (
                self.settings.mask_pencil_from_alpha
                + max(0.0, min(1.0, float(pressure)))
                * (
                    self.settings.mask_pencil_to_alpha
                    - self.settings.mask_pencil_from_alpha
                )
            )
        else:
            amount = self.settings.mask_pencil_to_alpha
        return self._stroke_base_size, max(0.0, min(1.0, amount))

    def _continue_mask_stroke(self, point: QPointF, pressure: float) -> None:
        if (
            self.chapter is None
            or self.active_tone_mask_id not in self.chapter.masks
            or not self._drawing
        ):
            return
        self._mask_sample_queue.append(
            (QPointF(point), float(pressure), time.monotonic())
        )
        if not self._mask_sample_timer.isActive():
            self._mask_sample_timer.start()

    def _flush_mask_samples(self) -> None:
        if (
            not self._drawing or self.chapter is None
            or not self.active_tone_mask_id
        ):
            self._mask_sample_queue.clear()
            return
        mask = self.chapter.masks.get(self.active_tone_mask_id)
        if mask is None:
            self._mask_sample_queue.clear()
            return
        dirty = QRectF()
        erasing = self.tool == ToolKind.RASTER_ERASER
        while self._mask_sample_queue:
            point, raw_pressure, _timestamp = self._mask_sample_queue.popleft()
            pressure = self._effective_pressure(raw_pressure)
            if not self._mask_has_painted_sample:
                self._last_draw_point = QPointF(point)
                self._last_pressure = pressure
                size, opacity = self._mask_brush_values(pressure)
                changed = self.tiles.paint_dab(
                    mask.mask_id, point, size, QColor("#ffffffff"), opacity,
                    erase=erasing,
                    square=self.settings.eraser_square and erasing,
                    antialias=not erasing,
                    before=self._stroke_before,
                    replace_alpha=not erasing,
                )
                self._mask_has_painted_sample = True
            else:
                size_start, opacity_start = self._mask_brush_values(
                    self._last_pressure
                )
                size_end, opacity_end = self._mask_brush_values(pressure)
                changed = self.tiles.paint_segment(
                    mask.mask_id, self._last_draw_point, point,
                    size_start, size_end, QColor("#ffffffff"),
                    opacity_start, opacity_end,
                    erase=erasing,
                    square=self.settings.eraser_square and erasing,
                    antialias=not erasing,
                    density=1.0,
                    before=self._stroke_before,
                    replace_alpha=not erasing,
                )
                self._last_draw_point = QPointF(point)
                self._last_pressure = pressure
            if not changed.isEmpty():
                dirty = changed if dirty.isEmpty() else dirty.united(changed)
        if dirty.isEmpty():
            return
        self._mask_runtime_revision += 1
        self._mask_stroke_dirty = (
            dirty if self._mask_stroke_dirty.isEmpty()
            else self._mask_stroke_dirty.united(dirty)
        )
        self._invalidate_tone_mask_overlay(contributors=False)
        # Parameter masks are document-anchored. A conservative effect halo
        # covers the maximum 100 px outline and 100 px Gaussian blur support
        # without throwing away the rest of the warmed scene cache.
        scene_dirty = dirty.adjusted(-300.0, -300.0, 300.0, 300.0)
        widget_dirty = self._mark_scene_dirty_world(scene_dirty)
        overlay_dirty = self._world_dirty_to_widget(dirty)
        repaint = widget_dirty.united(overlay_dirty)
        if not repaint.isEmpty():
            self.update(repaint)

    def _restore_mask_revision(self, mask_id: str, revision: object) -> None:
        mask = self.chapter.masks.get(mask_id) if self.chapter else None
        if mask is not None and revision is not None:
            mask.revision = int(revision)

    def _mask_tiles_changed(self) -> None:
        self._invalidate_scene_cache()
        self.documentChanged.emit(QRectF())
        self.update()

    def _end_mask_stroke(self) -> None:
        mask = self.chapter.masks.get(self.active_tone_mask_id) if self.chapter else None
        if mask is None or not self._drawing:
            self._drawing = False
            return
        self._mask_sample_timer.stop()
        self._flush_mask_samples()
        keys = set(self._stroke_before)
        self.tiles.prune_empty(mask.mask_id, keys)
        if keys:
            mask.touch()
        after = self.tiles.snapshot(mask.mask_id, keys)
        command = TilePatchCommand(
            "Paint mask" if self.tool == ToolKind.RASTER_PENCIL else "Erase mask",
            self.tiles, mask.mask_id, self._stroke_before, after,
            self._mask_tiles_changed,
            self._mask_stroke_revision_before, mask.revision,
            lambda revision, mask_id=mask.mask_id:
            self._restore_mask_revision(mask_id, revision),
        )
        self.command_stack.push(command, already_done=True)
        dirty = QRectF(self._mask_stroke_dirty)
        self._stroke_before = {}
        self._stroke_preset = None
        self._mask_stroke_dirty = QRectF()
        self._mask_sample_queue.clear()
        self._mask_has_painted_sample = False
        self._drawing = False
        self._invalidate_tone_mask_overlay(contributors=False)
        self._preserve_tone_mask_contributors_once = True
        self.documentChanged.emit(dirty)
        self.interactionFinished.emit()
        self.update()

    def _begin_stroke(self, point: QPointF, pressure: float) -> None:
        if self.chapter is None or self.selected_kind != "object":
            return
        obj = self.chapter.objects.get(self.selected_id)
        if not isinstance(obj, RasterObject):
            return
        local = self._raster_local_point(obj, point)
        self._suspend_gc_for_stroke()
        self._drawing = True
        self._last_draw_point = local
        self._last_pressure = self._effective_pressure(pressure)
        self._stroke_preset = BrushPreset.from_dict(self._preset.to_dict())
        self._stroke_base_size = float(
            self.settings.active_eraser_pixels()
            if self.tool == ToolKind.RASTER_ERASER
            else self.settings.pencil_size()
        )
        self._stroke_dirty_world = QRectF()
        self._stroke_before = {}
        self._stroke_frame_before = tuple(obj.interaction_rect)
        self._stroke_erasing = self.tool == ToolKind.RASTER_ERASER
        self._predictive = None
        size, opacity = self._brush_values(self._last_pressure)
        if self.tool == ToolKind.RASTER_PENCIL:
            size *= self._stroke_preset.stroke_start_ratio
        try:
            dirty = self.tiles.paint_dab(
                obj.object_id, local, size, QColor(self.primary_color), opacity,
                erase=self.tool == ToolKind.RASTER_ERASER,
                square=(
                    self.settings.eraser_square
                    and self.tool == ToolKind.RASTER_ERASER
                ),
                antialias=(
                    self._stroke_preset.antialiasing
                    if self.tool == ToolKind.RASTER_PENCIL else False
                ),
                before=self._stroke_before,
            )
        except Exception:
            self._abort_raster_stroke_after_error()
            raise
        self._emit_raster_dirty(obj, dirty)

    def _continue_stroke(self, point: QPointF, pressure: float) -> None:
        obj = self.chapter.objects[self.selected_id]
        local = self._raster_local_point(obj, point)
        actual_pressure = self._effective_pressure(pressure)
        size_start, opacity_start = self._brush_values(self._last_pressure)
        size_end, opacity_end = self._brush_values(actual_pressure)
        size = (size_start + size_end) / 2
        try:
            dirty = self.tiles.paint_segment(
                obj.object_id, self._last_draw_point, local,
                size_start, size_end,
                QColor(self.primary_color), opacity_start, opacity_end,
                erase=self.tool == ToolKind.RASTER_ERASER,
                square=(
                    self.settings.eraser_square
                    and self.tool == ToolKind.RASTER_ERASER
                ),
                antialias=(
                    self._stroke_preset.antialiasing
                    if self.tool == ToolKind.RASTER_PENCIL else False
                ),
                density=(
                    self._stroke_preset.density
                    if self.tool == ToolKind.RASTER_PENCIL else 1.0
                ),
                before=self._stroke_before,
            )
        except Exception:
            self._abort_raster_stroke_after_error()
            raise
        previous_local = QPointF(self._last_draw_point)
        self._last_draw_point = local
        self._last_pressure = actual_pressure
        if self.settings.predictive_ink:
            world_current = self._raster_world_point(obj, local)
            delta = local - previous_local
            # Prediction is intentionally short and transient; it never enters
            # the tile store or undo history.
            predicted_local = QPointF(
                local.x() + delta.x() * 0.5,
                local.y() + delta.y() * 0.5,
            )
            world_predicted = self._raster_world_point(obj, predicted_local)
            self._predictive = (
                world_current, world_predicted, size,
                QColor(self.primary_color),
            )
        self._emit_raster_dirty(obj, dirty)

    def _end_stroke(self) -> None:
        try:
            self._end_stroke_impl()
        except Exception:
            self._abort_raster_stroke_after_error()
            raise
        finally:
            self._restore_gc_after_stroke()

    def _end_stroke_impl(self) -> None:
        obj = self.chapter.objects[self.selected_id]
        if (
            not self._stroke_erasing
            and self._stroke_preset.stroke_end_ratio < 0.999
        ):
            size, opacity = self._brush_values(self._last_pressure)
            dirty = self.tiles.paint_dab(
                obj.object_id, self._last_draw_point,
                size * self._stroke_preset.stroke_end_ratio,
                QColor(self.primary_color), opacity,
                antialias=self._stroke_preset.antialiasing,
                before=self._stroke_before,
            )
            self._emit_raster_dirty(obj, dirty)
        keys = set(self._stroke_before)
        if self._stroke_erasing:
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
        committed_dirty = QRectF(self._stroke_dirty_world)
        self._stroke_dirty_world = QRectF()
        self._stroke_preset = None
        self._predictive = None
        if not committed_dirty.isEmpty():
            self.documentChanged.emit(committed_dirty)
        self._restore_gc_after_stroke()
        self.interactionFinished.emit()

    def _brush_values(self, pressure: float) -> tuple[float, float]:
        erasing = self.tool == ToolKind.RASTER_ERASER
        base = self._stroke_base_size if self._stroke_preset is not None else (
            self.settings.active_eraser_pixels()
            if erasing else self.settings.pencil_size()
        )
        if erasing:
            return float(base), 1.0
        size = float(base)
        opacity = 1.0
        preset = self._stroke_preset or self._preset
        if preset.pressure_size:
            size *= preset.size_curve.evaluate_fast(pressure)
        if preset.pressure_opacity:
            opacity = preset.opacity_curve.evaluate_fast(pressure)
        opacity *= QColor(self.primary_color).alphaF()
        return max(0.5, size), opacity

    def _effective_pressure(self, pressure: float) -> float:
        """Normalize pressure without destroying valid light pen samples."""
        pressure = max(0.0, min(1.0, float(pressure)))
        if self._device_supports_pressure:
            return pressure
        # Mouse input and tablet devices without a pressure axis conventionally
        # report zero. Preserve their historical full-pressure drawing behavior.
        return pressure if pressure > 0.001 else 1.0

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
        world = self.modifier_expanded_dirty(
            obj.object_id,
            self._drawing_local_rect_to_world(obj, local),
        )
        self._stroke_dirty_world = (
            QRectF(world) if self._stroke_dirty_world.isEmpty()
            else self._stroke_dirty_world.united(world)
        )
        bottom = math.ceil(world.bottom())
        if bottom > self.chapter.height:
            self.chapter.height = bottom + 1080
            self.hierarchyChanged.emit()
        self._queue_visual_dirty(world)

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
        if pivot_distance <= tolerance:
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

    def _text_transform_control_hit(
        self, quad: list[tuple[float, float]], point: QPointF,
    ) -> tuple[str, int | None]:
        """Reserve only the dotted boundary, not the text interior, for moving."""
        mode, handle = self._transform_control_hit(quad, point)
        if mode in {"handle", "rotate", "pivot"}:
            return mode, handle
        outline = QPainterPath()
        outline.addPolygon(QPolygonF([QPointF(*candidate) for candidate in quad]))
        stroker = QPainterPathStroker()
        stroker.setWidth(16 / max(self.scale, 0.05))
        if stroker.createStroke(outline).contains(point):
            return "translate", None
        return "", None

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
        if pivot_distance <= tolerance:
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

    def _selected_object_transform_hit(
        self,
        obj: RasterObject | VectorDrawingObject | ImageObject,
        quad: list[tuple[float, float]],
        point: QPointF,
    ) -> tuple[str, int | None]:
        """Return only the transform affordances active in the current tool."""
        mode, handle = self._raster_transform_control_hit(quad, point)
        if mode:
            return mode, handle
        if isinstance(obj, ImageObject) and obj.placement_mode == "free":
            path = QPainterPath()
            path.addPolygon(QPolygonF([QPointF(*candidate) for candidate in quad]))
            if path.contains(point):
                return "translate", None
        if self.tool == ToolKind.TRANSFORM:
            return self._transform_control_hit(quad, point)
        return "", None

    def _multi_selection_cage(self) -> list[tuple[float, float]] | None:
        if self.chapter is None or len(self.selected_entities) < 2:
            return None
        rect = QRectF()
        first = True
        for kind, entity_id in self.selected_entities:
            if kind != "object":
                return None
            candidate = self.object_world_rect(entity_id)
            if candidate is None:
                continue
            rect = QRectF(candidate) if first else rect.united(candidate)
            first = False
        return None if first else self._rect_quad(rect)

    def _begin_multi_transform(self, point: QPointF) -> bool:
        cage = self._multi_selection_cage()
        if cage is None:
            return False
        mode, handle = self._transform_control_hit(cage, point)
        if not mode:
            return False
        self._geometry_transform_target = ("multi", "")
        self._transform_handle_index = handle
        self._transform_drag_mode = mode
        self._model_before = self.chapter.to_dict()
        self._drag_start_doc = QPointF(point)
        self._transform_start_quad = list(cage)
        self._transform_preview_quad = list(cage)
        self._multi_transform_start_world_quads = {
            entity_id: list(self.object_world_quad(entity_id) or [])
            for kind, entity_id in self.selected_entities if kind == "object"
        }
        self._multi_transform_preview_quads.clear()
        pivot = self._transform_pivot or QRectF(
            QPointF(*cage[0]), QPointF(*cage[2])
        ).center()
        self._transform_rotate_start = math.atan2(
            point.y() - pivot.y(), point.x() - pivot.x()
        )
        return True

    def _begin_geometry_transform(self, point: QPointF) -> bool:
        if self.chapter is None:
            return False
        if self.selected_kind == "layer":
            layer = self.chapter.layers.get(self.selected_id)
            if layer is None or layer.bound is None:
                return False
            left, top, width, height = layer.bound.bbox()
            frame_quad = self._rect_quad(QRectF(
                left, top, max(1.0, width), max(1.0, height)
            ))
            world_quad = [
                self.layer_world_transform(layer.layer_id).map(
                    QPointF(x, y)
                ).toTuple()
                for x, y in frame_quad
            ]
            if self.tool == ToolKind.TRANSFORM:
                parent_transform = (
                    self.layer_world_transform(layer.parent_id)
                    if layer.parent_id else QTransform()
                )
                inverse, valid = parent_transform.inverted()
                if not valid:
                    return False
                local_quad = [
                    inverse.map(QPointF(x, y)).toTuple()
                    for x, y in world_quad
                ]
                target = ("layer_group", layer.layer_id)
            else:
                local_quad = frame_quad
                target = ("layer", layer.layer_id)
        else:
            obj = self.chapter.objects.get(self.selected_id)
            if (
                not isinstance(obj, GradientObject)
                or obj.field_type == "parent_shape"
                or self._is_two_point_line_gradient(obj)
            ):
                return False
            parent_id = obj.parent_layer_id
            world_quad = self.object_world_quad(obj.object_id)
            if not world_quad:
                return False
            inverse, valid = self.layer_world_transform(parent_id).inverted()
            if not valid:
                return False
            local_quad = [
                inverse.map(QPointF(x, y)).toTuple()
                for x, y in world_quad
            ]
            target = ("object", obj.object_id)
        mode, handle = self._raster_transform_control_hit(world_quad, point)
        if not mode and self.tool == ToolKind.TRANSFORM:
            mode, handle = self._transform_control_hit(world_quad, point)
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
        if kind == "layer_group":
            return self.chapter.layers[entity_id].parent_id or ""
        if kind == "multi":
            return ""
        obj = self.chapter.objects.get(entity_id)
        return obj.parent_layer_id if obj is not None else ""

    def _update_geometry_transform_preview(self, point: QPointF) -> None:
        if self._geometry_transform_target == ("multi", ""):
            self._update_multi_transform_preview(point)
            return
        parent_id = self._geometry_transform_parent_id()
        if self._transform_start_quad is None:
            return
        parent_transform = (
            self.layer_world_transform(parent_id)
            if parent_id else QTransform()
        )
        inverse, valid = parent_transform.inverted()
        if not valid:
            return
        mapped_point = inverse.map(point)
        mapped_start = inverse.map(self._drag_start_doc)
        local_point = mapped_point.toTuple()
        start = list(self._transform_start_quad)
        dx = mapped_point.x() - mapped_start.x()
        dy = mapped_point.y() - mapped_start.y()
        if self._transform_drag_mode == "pivot":
            self._transform_pivot = QPointF(point)
            self._transform_pivot_custom = True
            return
        if self._transform_drag_mode == "rotate":
            world_pivot = self._transform_pivot or parent_transform.map(QPointF(
                sum(x for x, _ in start) / 4,
                sum(y for _, y in start) / 4,
            ))
            pivot = inverse.map(world_pivot)
            local_angle = math.atan2(
                mapped_point.y() - pivot.y(), mapped_point.x() - pivot.x()
            )
            start_angle = math.atan2(
                mapped_start.y() - pivot.y(), mapped_start.x() - pivot.x()
            )
            angle = (
                local_angle - start_angle
            )
            cosine, sine = math.cos(angle), math.sin(angle)
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

        if self._geometry_transform_target and (
            self._geometry_transform_target[0] == "layer_group"
        ):
            self._compound_path_cache.clear()
            self._invalidate_scene_cache()

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
        if kind == "multi":
            for object_id, destination_quad in (
                self._multi_transform_preview_quads.items()
            ):
                obj = self.chapter.objects.get(object_id)
                if not isinstance(obj, (RasterObject, VectorDrawingObject)):
                    continue
                if obj.transform_frame is None:
                    obj.transform_frame = self._object_transform_frame(obj)
                obj.transform_quad = list(destination_quad)
                self._transform_single_target_focal_modifiers(
                    "object", object_id, transform
                )
            self._multi_transform_start_world_quads.clear()
            self._multi_transform_preview_quads.clear()
            label = "Transform objects"
        elif kind == "layer_group":
            layer = self.chapter.layers[entity_id]
            parent_transform = (
                self.layer_world_transform(layer.parent_id)
                if layer.parent_id else QTransform()
            )
            world_transform = QTransform.quadToQuad(
                parent_transform.map(QPolygonF([
                    QPointF(*value) for value in source
                ])),
                parent_transform.map(QPolygonF([
                    QPointF(*value) for value in destination
                ])),
            )
            self._transform_single_target_focal_modifiers(
                "layer", entity_id, world_transform
            )
            left, top, width, height = layer.bound.bbox()
            layer.transform_frame = (
                left, top, max(1.0, width), max(1.0, height)
            )
            layer.transform_quad = list(destination)
            layer.translate_x = 0.0
            layer.translate_y = 0.0
            label = "Transform shape group"
        elif kind == "layer":
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
        if kind in {"layer", "layer_group"}:
            self._compound_path_cache.clear()
        self._invalidate_scene_cache()
        self.update()

    @staticmethod
    def _is_transformable_object(obj: DocumentObject | None) -> bool:
        return isinstance(obj, (RasterObject, VectorDrawingObject, ImageObject))

    @staticmethod
    def _is_two_point_line_gradient(obj: DocumentObject | None) -> bool:
        return bool(
            isinstance(obj, GradientObject)
            and obj.field_type == "line"
            and len(obj.line_field.geometry.nodes) == 2
        )

    def _object_transform_cage_visible(
        self, obj: DocumentObject | None,
    ) -> bool:
        if not self._is_transformable_object(obj):
            return False
        if isinstance(obj, ImageObject) and obj.placement_mode == "fit_parent":
            return False
        if self.tool == ToolKind.RASTER_PENCIL:
            return bool(self.settings.pencil_transform_handles_visible)
        if self.tool == ToolKind.RASTER_ERASER:
            return bool(self.settings.eraser_transform_handles_visible)
        if self.tool in {
            ToolKind.DRAW_SELECT_RECT,
            ToolKind.DRAW_SELECT_LASSO,
            ToolKind.DRAW_SELECT_STROKE,
        }:
            return False
        if isinstance(obj, VectorDrawingObject) and self.tool in {
            ToolKind.VECTOR_EDIT,
            ToolKind.VECTOR_REDRAW,
            ToolKind.VECTOR_CONNECT,
            ToolKind.VECTOR_SIMPLIFY,
            ToolKind.FILL,
        }:
            return False
        return True

    def _object_transform_frame(
        self, obj: RasterObject | VectorDrawingObject | ImageObject,
    ) -> tuple[float, float, float, float]:
        if obj.transform_frame is not None:
            return tuple(obj.transform_frame)
        if isinstance(obj, RasterObject):
            rect = QRectF(*obj.interaction_rect).translated(obj.x, obj.y)
            return rect.x(), rect.y(), rect.width(), rect.height()
        if isinstance(obj, VectorDrawingObject):
            left, top, width, height = obj.derived_bounds()
            return (
                obj.x + left, obj.y + top,
                max(1.0, width), max(1.0, height),
            )
        return 0.0, 0.0, float(obj.pixel_width), float(obj.pixel_height)

    def _begin_selected_raster_transform(self, point: QPointF) -> bool:
        if self.chapter is None or not self.selected_object_id:
            return False
        obj = self.chapter.objects.get(self.selected_object_id)
        if not self._is_transformable_object(obj):
            return False
        if not self._object_transform_cage_visible(obj) \
                and self.tool != ToolKind.TRANSFORM:
            return False
        vector_drawing_tool = isinstance(obj, VectorDrawingObject) and self.tool in {
            ToolKind.RASTER_PENCIL,
            ToolKind.RASTER_ERASER,
            ToolKind.VECTOR_EDIT,
            ToolKind.VECTOR_REDRAW,
            ToolKind.VECTOR_CONNECT,
            ToolKind.VECTOR_SIMPLIFY,
            ToolKind.FILL,
        }
        pencil_or_eraser = self.tool in {
            ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER,
        }
        if vector_drawing_tool and not pencil_or_eraser:
            return False
        if isinstance(obj, ImageObject) and obj.placement_mode == "fit_parent":
            return False
        world_quad = self.object_world_quad(obj.object_id)
        if not world_quad:
            return False
        mode, handle = self._selected_object_transform_hit(
            obj, world_quad, point
        )
        if not mode:
            return False
        parent_inverse, valid = self.layer_world_transform(
            obj.parent_layer_id
        ).inverted()
        if not valid:
            return False
        self._transform_handle_index = handle
        self._transform_drag_mode = mode
        self._model_before = self.chapter.to_dict()
        self._drag_start_doc = QPointF(point)
        self._transform_start_quad = [
            parent_inverse.map(QPointF(x, y)).toTuple()
            for x, y in world_quad
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
        if not self._is_transformable_object(obj):
            return False
        if not self._object_transform_cage_visible(obj) \
                and self.tool != ToolKind.TRANSFORM:
            return False
        if isinstance(obj, ImageObject) and obj.placement_mode == "fit_parent":
            return False
        quad = self.object_world_quad(obj.object_id)
        if not quad:
            return False
        mode, _handle = self._selected_object_transform_hit(obj, quad, point)
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
        transform = self.layer_world_transform(layer_id)
        inverse, valid = transform.inverted()
        if not valid:
            return point
        world = transform.map(QPointF(*point))
        grid = self.chapter.effective_grid(layer_id)
        snapped_x, snapped_y = grid.snap(
            world.x(), world.y()
        )
        return inverse.map(QPointF(snapped_x, snapped_y)).toTuple()

    def _update_transform_preview(self, point: QPointF) -> None:
        obj = self.chapter.objects[self.selected_object_id]
        old_preview = (
            list(self._transform_preview_quad)
            if isinstance(obj, ImageObject) and self._transform_preview_quad
            else None
        )
        parent_transform = self.layer_world_transform(obj.parent_layer_id)
        inverse, valid = parent_transform.inverted()
        if not valid:
            return
        mapped_point = inverse.map(point)
        mapped_start = inverse.map(self._drag_start_doc)
        local_point = mapped_point.toTuple()
        start = list(self._transform_start_quad)
        dx = mapped_point.x() - mapped_start.x()
        dy = mapped_point.y() - mapped_start.y()
        if self._transform_drag_mode == "pivot":
            self._transform_pivot = QPointF(point)
            self._transform_pivot_custom = True
            return
        if self._transform_drag_mode == "rotate":
            world_pivot = self._transform_pivot or parent_transform.map(QPointF(
                sum(x for x, _ in start) / 4,
                sum(y for _, y in start) / 4,
            ))
            pivot = inverse.map(world_pivot)
            current_angle = math.atan2(
                mapped_point.y() - pivot.y(), mapped_point.x() - pivot.x()
            )
            start_angle = math.atan2(
                mapped_start.y() - pivot.y(), mapped_start.x() - pivot.x()
            )
            angle = current_angle - start_angle
            cosine, sine = math.cos(angle), math.sin(angle)
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
            if isinstance(obj, ImageObject) and old_preview is not None:
                polygons = []
                for quad in (old_preview, candidate):
                    polygons.append(parent_transform.map(QPolygonF([
                        QPointF(x, y) for x, y in quad
                    ])).boundingRect())
                self._queue_visual_dirty(
                    polygons[0].united(polygons[1]), notify_preview=False
                )

    def _update_multi_transform_preview(self, point: QPointF) -> None:
        start = list(self._transform_start_quad or [])
        if len(start) != 4:
            return
        if self._transform_drag_mode == "pivot":
            self._transform_pivot = QPointF(point)
            self._transform_pivot_custom = True
            return
        if self._transform_drag_mode == "translate":
            dx = point.x() - self._drag_start_doc.x()
            dy = point.y() - self._drag_start_doc.y()
            candidate = [(x + dx, y + dy) for x, y in start]
        elif self._transform_drag_mode == "rotate":
            pivot = self._transform_pivot or QPolygonF(
                [QPointF(*value) for value in start]
            ).boundingRect().center()
            angle = math.atan2(
                point.y() - pivot.y(), point.x() - pivot.x()
            ) - self._transform_rotate_start
            cosine, sine = math.cos(angle), math.sin(angle)
            candidate = [(
                pivot.x() + (x - pivot.x()) * cosine - (y - pivot.y()) * sine,
                pivot.y() + (x - pivot.x()) * sine + (y - pivot.y()) * cosine,
            ) for x, y in start]
        elif self.settings.transform_mode == "uniform":
            handle = self._transform_handle_index
            anchors = start + self._edge_midpoints(start)
            opposite = [2, 3, 0, 1, 6, 7, 4, 5][handle]
            origin, initial = anchors[opposite], anchors[handle]
            factor = math.dist(origin, point.toTuple()) / max(
                math.dist(origin, initial), 1e-6
            )
            candidate = [(
                origin[0] + (x - origin[0]) * factor,
                origin[1] + (y - origin[1]) * factor,
            ) for x, y in start]
        else:
            handle = self._transform_handle_index
            candidate = list(start)
            if handle < 4:
                candidate[handle] = point.toTuple()
            else:
                edge = handle - 4
                midpoint = self._edge_midpoints(start)[edge]
                change = point.x() - midpoint[0], point.y() - midpoint[1]
                for index in (edge, (edge + 1) % 4):
                    candidate[index] = (
                        start[index][0] + change[0],
                        start[index][1] + change[1],
                    )
        if not self._quad_is_valid(candidate):
            return
        self._transform_preview_quad = candidate
        transform = QTransform.quadToQuad(
            QPolygonF([QPointF(*value) for value in start]),
            QPolygonF([QPointF(*value) for value in candidate]),
        )
        self._multi_transform_preview_quads = {}
        for object_id, quad in self._multi_transform_start_world_quads.items():
            obj = self.chapter.objects.get(object_id)
            if obj is None:
                continue
            inverse, valid = self.layer_world_transform(
                obj.parent_layer_id
            ).inverted()
            if not valid:
                continue
            self._multi_transform_preview_quads[object_id] = [
                inverse.map(transform.map(QPointF(*value))).toTuple()
                for value in quad
            ]
        self._invalidate_scene_cache()

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
        self._text_transform_cache = QImage()
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
        if obj is None or not isinstance(
            obj, (RasterObject, VectorDrawingObject, ImageObject, TextObject)
        ):
            return
        parent_transform = self.layer_world_transform(obj.parent_layer_id)
        world_transform = QTransform.quadToQuad(
            parent_transform.map(QPolygonF([
                QPointF(*value) for value in source
            ])),
            parent_transform.map(QPolygonF([
                QPointF(*value) for value in destination
            ])),
        )
        if drag_mode == "translate" and obj.transform_quad is None:
            obj.x += destination[0][0] - source[0][0]
            obj.y += destination[0][1] - source[0][1]
        else:
            if isinstance(obj, RasterObject) and obj.transform_quad is None:
                obj.transform_frame = tuple(obj.interaction_rect)
                obj.x = obj.y = 0
                bounds = self._rect_from_quad(destination)
                obj.interaction_rect = (
                    bounds.x(), bounds.y(),
                    max(1.0, bounds.width()), max(1.0, bounds.height()),
                )
            else:
                obj.transform_frame = self._object_transform_frame(obj)
            obj.transform_quad = destination
        self._transform_single_target_focal_modifiers(
            "object", object_id, world_transform
        )
        after_model = self.chapter.to_dict()
        if before_model != after_model:
            label = {
                RasterObject: "Transform raster",
                VectorDrawingObject: "Transform vector drawing",
                ImageObject: "Transform image",
            }.get(type(obj), "Transform object")
            self.push_model_change(before_model, after_model, label)
            self.hierarchyChanged.emit()
        self.documentChanged.emit(QRectF())
        self._invalidate_scene_cache()
        self.update()

    def _clear_transform_preview(self) -> None:
        self._model_before = None
        self._transform_start_quad = None
        self._transform_preview_quad = None
        self._transform_handle_index = None
        self._transform_drag_mode = None
        self._geometry_transform_target = None
        self._multi_transform_start_world_quads.clear()
        self._multi_transform_preview_quads.clear()
        self._transform_static_cache = QImage()
        self._text_transform_cache = QImage()
        self._render_excluded_object_id = ""

    def _build_raster_transform_cache(self) -> None:
        selected = (
            self.chapter.objects.get(self.selected_object_id)
            if self.chapter is not None else None
        )
        if isinstance(selected, ImageObject):
            # Image transforms must remain in normal hierarchy traversal so
            # unchanged artwork above them stays above them during the drag.
            self._transform_static_cache = QImage()
            self._text_transform_cache = QImage()
            return
        image = self._build_excluded_object_scene_cache(
            self.selected_object_id
        )
        self._transform_static_cache = image
        if image.isNull():
            self._text_transform_cache = QImage()

    def _build_excluded_object_scene_cache(
        self, object_id: str,
    ) -> QImage:
        if (
            self.chapter is None or not object_id
            or self.width() <= 0 or self.height() <= 0
        ):
            return QImage()
        ratio = max(1.0, float(self.devicePixelRatioF()))
        image = QImage(
            max(1, round(self.width() * ratio)),
            max(1, round(self.height() * ratio)),
            QImage.Format_ARGB32_Premultiplied,
        )
        image.setDevicePixelRatio(ratio)
        image.fill(QColor("#242428"))
        painter = QPainter(image)
        succeeded = False
        previous_excluded = self._render_excluded_object_id
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setTransform(self.camera_transform())
            painter.fillRect(
                QRectF(0, 0, self.chapter.width, self.chapter.height),
                QColor(self.chapter.background),
            )
            painter.save()
            try:
                painter.setClipRect(
                    QRectF(0, 0, self.chapter.width, self.chapter.height)
                )
                self._render_excluded_object_id = object_id
                visible = self.visible_document_rect()
                for page_id in reversed(self.chapter.root_page_ids):
                    self._render_layer(
                        painter, self.chapter.layers[page_id], 1.0, visible
                    )
                self._draw_grid(painter, visible)
            finally:
                painter.restore()
            succeeded = True
        finally:
            self._render_excluded_object_id = previous_excluded
            if painter.isActive():
                painter.end()
        return image if succeeded else QImage()

    def _build_vector_eraser_background_cache(self) -> None:
        self._vector_eraser_background_cache = (
            self._build_excluded_object_scene_cache(
                self.selected_object_id
            )
        )

    def _render_selected_raster_preview(
        self, painter: QPainter, visible: QRectF,
    ) -> None:
        obj = self.chapter.objects.get(self.selected_object_id)
        if not (
            self._is_transformable_object(obj) or isinstance(obj, TextObject)
        ):
            return
        painter.save()
        previous_interactive = self._interactive_render
        self._interactive_render = True
        try:
            opacity = 1.0
            for layer in self.chapter.ancestor_layers(obj.parent_layer_id):
                if not layer.visible or layer.opacity <= 0 or layer.bound is None:
                    return
                painter.setTransform(self._layer_parent_transform(layer), True)
                painter.setClipPath(
                    self.layer_effective_path(layer.layer_id),
                    Qt.IntersectClip,
                )
                opacity *= layer.opacity
            inverse, valid = self.layer_world_transform(
                obj.parent_layer_id
            ).inverted()
            self._render_object(
                painter, obj, opacity,
                inverse.mapRect(visible) if valid else visible,
            )
        finally:
            self._interactive_render = previous_interactive
            painter.restore()

    def _build_text_transform_cache(self, obj: TextObject) -> None:
        source = QRectF(0, 0, max(1.0, obj.width), max(1.0, obj.height))
        ratio = max(1.0, float(self.devicePixelRatioF()) * self.scale)
        largest = max(source.width(), source.height())
        if largest * ratio > 8192:
            ratio = max(0.1, 8192 / largest)
        image = QImage(
            max(1, math.ceil(source.width() * ratio)),
            max(1, math.ceil(source.height() * ratio)),
            QImage.Format_ARGB32_Premultiplied,
        )
        image.fill(Qt.GlobalColor.transparent)
        document = self._text_document(obj, source.width())
        offset = self._text_vertical_offset(obj, document, source.height())
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(ratio, ratio)
        painter.setClipRect(source)
        painter.translate(0, offset)
        self._draw_text_document(painter, obj, document)
        painter.end()
        self._text_transform_cache = image

    def _render_selected_text_preview(self, painter: QPainter) -> None:
        obj = self.chapter.objects.get(self.selected_object_id)
        if (
            not isinstance(obj, TextObject)
            or not obj.visible
            or self._text_transform_cache.isNull()
            or self._transform_preview_quad is None
        ):
            return
        painter.save()
        try:
            opacity = 1.0
            for layer in self.chapter.ancestor_layers(obj.parent_layer_id):
                if not layer.visible or layer.opacity <= 0 or layer.bound is None:
                    return
                painter.translate(layer.translate_x, layer.translate_y)
                painter.setClipPath(
                    self.layer_effective_path(layer.layer_id),
                    Qt.ClipOperation.IntersectClip,
                )
                opacity *= layer.opacity
            painter.setOpacity(
                opacity if obj.opacity_locked else opacity * obj.opacity
            )
            source = QRectF(0, 0, max(1.0, obj.width), max(1.0, obj.height))
            transform = self._quad_transform(source, self._transform_preview_quad)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.setTransform(transform, True)
            painter.drawImage(source, self._text_transform_cache)
        finally:
            painter.restore()

    def commit_active_text_edit(self) -> None:
        """Finish the current typing transaction before another text edit."""
        self._commit_text_edit()

    def reserves_delete_key(self) -> bool:
        """Whether Delete currently belongs to an in-canvas sub-editor."""
        return bool(
            self._text_editing
            or (
                self._selected_vector_point_ids
                and self.tool in {ToolKind.VECTOR_EDIT, ToolKind.VECTOR_REDRAW}
            )
            or (
                self._selected_shape_node_id
                and self.tool in {ToolKind.SHAPE_EDIT, ToolKind.BOUND_EDIT}
            )
        )

    def _delete_selected_vector_points(self) -> bool:
        if (
            self.chapter is None
            or not self._selected_vector_point_ids
            or self.tool not in {ToolKind.VECTOR_EDIT, ToolKind.VECTOR_REDRAW}
        ):
            return False
        drawing = self._selected_vector_drawing()
        if drawing is None:
            return False
        before = self._capture_vector_graph(drawing)
        selected = set(self._selected_vector_point_ids)
        changed_strokes: set[str] = set()
        remaining: list[VectorStroke] = []
        for stroke in drawing.strokes:
            points = [
                point for point in stroke.points
                if point.point_id not in selected
            ]
            if len(points) != len(stroke.points):
                stroke.points = points
                stroke.closed = stroke.closed and len(points) > 1
                stroke.touch_render_revision()
                changed_strokes.add(stroke.stroke_id)
            if stroke.points:
                remaining.append(stroke)
        if not changed_strokes:
            return False
        drawing.strokes = remaining
        drawing.touch_revision()
        self._selected_vector_point_ids.clear()
        self._selected_vector_stroke_ids.clear()
        self._hover_vector_point_id = ""
        self._hover_vector_stroke_id = ""
        self._push_vector_change(before, "Delete vector points")
        return True

    def _selected_text_for_gizmos(self) -> TextObject | None:
        if (
            self.chapter is None
            or self.tool != ToolKind.TEXT_EDIT
            or self.selected_kind != "object"
        ):
            return None
        obj = self.chapter.objects.get(self.selected_object_id)
        return obj if isinstance(obj, TextObject) else None

    def _begin_selected_text_transform(self, point: QPointF) -> bool:
        if (
            self.chapter is None
            or self.tool not in {ToolKind.TEXT_EDIT, ToolKind.TRANSFORM}
            or not self.selected_object_id
        ):
            return False
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, TextObject) or obj.layout_mode != "free":
            return False
        world_quad = self.object_world_quad(obj.object_id)
        mode, handle = self._text_transform_control_hit(world_quad, point)
        if not mode:
            return False
        self.commit_active_text_edit()
        obj = self.chapter.objects[self.selected_object_id]
        world_quad = self.object_world_quad(obj.object_id)
        parent_inverse, valid = self.layer_world_transform(
            obj.parent_layer_id
        ).inverted()
        if not valid:
            return False
        self._transform_handle_index = handle
        self._transform_drag_mode = mode
        self._model_before = self.chapter.to_dict()
        self._drag_start_doc = QPointF(point)
        self._transform_start_quad = [
            parent_inverse.map(QPointF(x, y)).toTuple()
            for x, y in world_quad
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
        self._build_text_transform_cache(obj)
        return True

    def _update_text_gizmo_overlay(self) -> None:
        overlay = self._text_gizmo_overlay
        obj = self._selected_text_for_gizmos()
        quad = self._selected_world_quad() if obj is not None else None
        if obj is None or not quad:
            overlay.hide()
            return
        if not overlay.size.hasFocus():
            overlay.set_state(round(obj.font_size), obj.bold, obj.italic)
        overlay.adjustSize()
        bounds = self.camera_transform().map(
            QPolygonF([QPointF(*point) for point in quad])
        ).boundingRect()
        x = round(bounds.center().x() - overlay.width() / 2)
        y = round(bounds.top() - overlay.height() - 8)
        if y < 8:
            y = round(bounds.bottom() + 8)
        x = max(8, min(max(8, self.width() - overlay.width() - 8), x))
        y = max(8, min(max(8, self.height() - overlay.height() - 8), y))
        overlay.move(x, y)
        overlay.show()
        overlay.raise_()

    def _finish_text_property_change(self, before: dict, label: str) -> None:
        if self.chapter is None:
            return
        after = self.chapter.to_dict()
        if before != after:
            self.push_model_change(before, after, label)
            self.documentChanged.emit(QRectF())
        self.update()
        self.interactionFinished.emit()

    def _change_selected_text_property(
        self, key: str, value, *, relative: bool = False, label: str,
    ) -> None:
        obj = self._selected_text_for_gizmos()
        if obj is None:
            return
        self.commit_active_text_edit()
        obj = self._selected_text_for_gizmos()
        if obj is None:
            return
        before = self.chapter.to_dict()
        if key == "font_size":
            current = round(float(obj.font_size))
            target = current + int(value) if relative else int(value)
            obj.font_size = max(6, min(250, target))
        else:
            setattr(obj, key, value)
        self._finish_text_property_change(before, label)

    def _toggle_selected_text_property(self, key: str, label: str) -> None:
        obj = self._selected_text_for_gizmos()
        if obj is not None:
            self._change_selected_text_property(
                key, not bool(getattr(obj, key)), label=label
            )

    def _begin_text_size_edit(self, value: int) -> None:
        del value
        obj = self._selected_text_for_gizmos()
        if obj is None:
            return
        self.commit_active_text_edit()
        obj = self._selected_text_for_gizmos()
        if obj is None:
            return
        self._text_size_edit_before = self.chapter.to_dict()
        self._text_size_edit_object_id = obj.object_id
        self._text_size_edit_canceled = False

    def _cancel_text_size_edit(self) -> None:
        self._text_size_edit_canceled = True
        self._text_size_edit_before = None
        self._text_size_edit_object_id = ""
        self.update()

    def _commit_text_size_edit(self, value: int) -> None:
        if self._text_size_edit_canceled:
            self._text_size_edit_canceled = False
            self._update_text_gizmo_overlay()
            return
        before, self._text_size_edit_before = self._text_size_edit_before, None
        object_id, self._text_size_edit_object_id = (
            self._text_size_edit_object_id, ""
        )
        if before is None or self.chapter is None:
            return
        obj = self.chapter.objects.get(object_id)
        if not isinstance(obj, TextObject) or object_id != self.selected_object_id:
            return
        obj.font_size = max(6, min(250, int(value)))
        self._finish_text_property_change(before, "Set text size")

    def _text_property_handle_positions(self) -> dict[str, QPointF]:
        obj = self._selected_text_for_gizmos()
        quad = self._selected_world_quad() if obj is not None else None
        if not quad:
            return {}
        top_right = QPointF(*quad[1])
        bottom_right = QPointF(*quad[2])
        edge = bottom_right - top_right
        return {
            "font_size": top_right + edge / 3.0,
            "kerning": top_right + edge * (2.0 / 3.0),
        }

    def _text_property_handle_hit(self, widget_point: QPointF) -> str:
        for key, world in self._text_property_handle_positions().items():
            position = self.document_to_widget(world)
            if math.dist(position.toTuple(), widget_point.toTuple()) <= 28:
                return key
        return ""

    def _begin_text_property_drag(self, widget_point: QPointF) -> bool:
        key = self._text_property_handle_hit(widget_point)
        obj = self._selected_text_for_gizmos()
        if not key or obj is None:
            return False
        self.commit_active_text_edit()
        obj = self._selected_text_for_gizmos()
        if obj is None:
            return False
        self._text_property_drag = {
            "key": key,
            "object_id": obj.object_id,
            "before": self.chapter.to_dict(),
            "start_x": widget_point.x(),
            "current_x": widget_point.x(),
            "start_value": float(getattr(obj, key)),
            "steps": 0,
        }
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        return True

    def _update_text_property_drag(self, widget_point: QPointF) -> None:
        state = self._text_property_drag
        if state is None or self.chapter is None:
            return
        obj = self.chapter.objects.get(state["object_id"])
        if not isinstance(obj, TextObject):
            return
        state["current_x"] = widget_point.x()
        delta = widget_point.x() - state["start_x"]
        steps = math.trunc(delta / 4.0)
        if steps == state["steps"]:
            self.update()
            return
        state["steps"] = steps
        if steps == 0:
            value = state["start_value"]
        elif state["key"] == "font_size":
            value = max(
                10, min(100, round(state["start_value"]) + steps)
            )
        else:
            value = max(
                1.0,
                min(10.0, round((state["start_value"] + steps * 0.1) * 10) / 10),
            )
        setattr(obj, state["key"], value)
        self.documentChanged.emit(QRectF())
        self.update()

    def _finish_text_property_drag(self) -> bool:
        state, self._text_property_drag = self._text_property_drag, None
        if state is None or self.chapter is None:
            return False
        self.unsetCursor()
        label = (
            "Drag text size" if state["key"] == "font_size"
            else "Drag text kerning"
        )
        self._finish_text_property_change(state["before"], label)
        return True

    def _cancel_text_property_drag(self) -> bool:
        state, self._text_property_drag = self._text_property_drag, None
        if state is None or self.chapter is None:
            return False
        obj = self.chapter.objects.get(state["object_id"])
        if isinstance(obj, TextObject):
            setattr(obj, state["key"], state["start_value"])
        self.unsetCursor()
        self.documentChanged.emit(QRectF())
        self.update()
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
        layer_transform = self.layer_world_transform(obj.parent_layer_id)
        if obj.layout_mode == "strict":
            rect = self._strict_text_rect(obj)
            document = self._text_document(obj, rect.width())
            offset = self._text_vertical_offset(obj, document, rect.height())
            local = QTransform()
            local.translate(rect.left(), rect.top() + offset)
            return (
                document, QPointF(), local * layer_transform,
            )
        source = QRectF(0, 0, max(1.0, obj.width), max(1.0, obj.height))
        document = self._text_document(obj, source.width())
        offset = self._text_vertical_offset(obj, document, source.height())
        transform = self._quad_transform(source, self._text_quad(obj))
        transform.translate(0, offset)
        return document, QPointF(), transform * layer_transform

    def _text_local_point(
        self, obj: TextObject, world: QPointF,
    ) -> tuple[QPointF, QTextDocument] | None:
        document, origin, transform = self._text_edit_layout(obj)
        inverse, valid = transform.inverted()
        if not valid:
            return None
        return inverse.map(world - origin), document

    def _text_position_at(
        self, obj: TextObject, point: QPointF, *, require_inside: bool,
    ) -> tuple[QTextDocument, int] | None:
        mapped = self._text_local_point(obj, point)
        if mapped is None:
            return None
        local, document = mapped
        if require_inside:
            object_path = QPainterPath()
            object_path.addPolygon(QPolygonF([
                QPointF(*candidate)
                for candidate in self.object_world_quad(obj.object_id)
            ]))
            if not object_path.contains(point):
                return None
        local.setX(max(0.0, min(local.x(), max(0.0, document.textWidth()))))
        local.setY(max(0.0, min(local.y(), document.size().height())))
        position = document.documentLayout().hitTest(local, Qt.FuzzyHit)
        return document, max(0, min(len(obj.text), position))

    def _select_text_word_at(self, point: QPointF) -> bool:
        obj = self._editing_text_object()
        if obj is None:
            return False
        hit = self._text_position_at(obj, point, require_inside=True)
        if hit is None:
            return False
        document, position = hit
        self._begin_text_session(obj)
        cursor = QTextCursor(document)
        cursor.setPosition(position)
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        self._text_selection_anchor = max(0, cursor.selectionStart())
        self._text_cursor_position = min(len(obj.text), cursor.selectionEnd())
        self._text_dragging = False
        self.setFocus(Qt.MouseFocusReason)
        self.update()
        return True

    def _select_all_text_from_triple_click(self, widget_point: QPointF) -> bool:
        state = self._last_text_double_click
        obj = self._editing_text_object()
        if state is None or obj is None or state[2] != obj.object_id:
            return False
        elapsed_ms = (time.monotonic() - state[0]) * 1000
        if elapsed_ms > QApplication.doubleClickInterval():
            self._last_text_double_click = None
            return False
        if math.dist(state[1].toTuple(), widget_point.toTuple()) > max(
            3, QApplication.startDragDistance()
        ):
            return False
        world = self.widget_to_document(widget_point)
        if self._text_position_at(obj, world, require_inside=True) is None:
            return False
        self._begin_text_session(obj)
        self._text_selection_anchor = 0
        self._text_cursor_position = len(obj.text)
        self._text_dragging = True
        self._last_text_double_click = None
        self.setFocus(Qt.MouseFocusReason)
        self.update()
        return True

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
        hit = self._text_position_at(obj, point, require_inside=True)
        if hit is None:
            return False
        _document, position = hit
        self._begin_text_session(obj)
        self._text_cursor_position = position
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
            local = self._layer_world_to_local(obj.parent_layer_id, point)
            left, top, width, height = parent.bound.bbox()
            local_x, local_y = local.x(), local.y()
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
        hit = self._text_position_at(obj, point, require_inside=False)
        if hit is None:
            return
        _document, position = hit
        self._text_cursor_position = position
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
        self._text_caret_visible = True
        self._text_caret_timer.start()
        self._invalidate_scene_cache()
        self.update()

    def _commit_text_edit(self) -> None:
        if not self._text_editing or self.chapter is None:
            return
        before = self._text_before_state
        after = self.chapter.to_dict()
        self._text_editing = False
        self._text_caret_timer.stop()
        self._text_caret_visible = True
        self.textEditingChanged.emit(False)
        self._text_before_state = None
        self._text_dragging = False
        self._strict_margin_edge = None
        self._strict_margin_start = None
        self._text_local_history = []
        self._invalidate_scene_cache()
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
