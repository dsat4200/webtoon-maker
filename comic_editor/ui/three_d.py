"""3D-mode controller kept independent from the 2D canvas implementation.

The controller owns transient navigation/selection state and writes only the
sparse presentation fields that Webtoon Maker is authoritative for.  Blender
catalogs and geometry are treated as read-only inputs.
"""
from __future__ import annotations

import copy
import math
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from PySide6.QtCore import QObject, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage


class ThreeDToolKind(Enum):
    TRANSFORM = "transform_object"
    ADD_LIGHT = "add_light"
    DRAW_CUBE = "draw_cube"
    DRAW_CYLINDER = "draw_cylinder"
    SELECT_RECT = "select_rect"
    SELECT_LASSO = "select_lasso"


@dataclass
class ThreeDNavigationState:
    target: tuple[float, float, float] = (0.0, 0.0, 0.0)
    yaw: float = 35.0
    pitch: float = 20.0
    distance: float = 8.0
    pan: tuple[float, float] = (0.0, 0.0)
    orientation: tuple[float, float, float, float] | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> "ThreeDNavigationState":
        if not isinstance(value, dict):
            return cls()
        target = value.get("target", cls.target)
        pan = value.get("pan", cls.pan)
        raw_orientation = value.get("orientation")
        orientation = None
        if (
            isinstance(raw_orientation, (list, tuple))
            and len(raw_orientation) == 4
        ):
            candidate = tuple(float(item) for item in raw_orientation)
            if all(math.isfinite(item) for item in candidate):
                orientation = candidate
        return cls(
            target=tuple(float(item) for item in target[:3]),
            yaw=float(value.get("yaw", 35.0)),
            pitch=float(value.get("pitch", 20.0)),
            distance=max(0.01, float(value.get("distance", 8.0))),
            pan=tuple(float(item) for item in pan[:2]),
            orientation=orientation,
        )

    def to_dict(self) -> dict:
        result = {
            "target": list(self.target), "yaw": self.yaw,
            "pitch": self.pitch, "distance": self.distance,
            "pan": list(self.pan),
        }
        if self.orientation is not None:
            result["orientation"] = list(self.orientation)
        return result

    @classmethod
    def from_camera(cls, camera) -> "ThreeDNavigationState":
        """Seed navigation from an exact Blender camera without a first-drag jump."""
        import numpy as np

        orientation = np.asarray(camera.orientation, dtype=np.float64)
        forward = np.asarray(camera.forward, dtype=np.float64)
        pitch = math.degrees(math.asin(float(np.clip(forward[1], -1.0, 1.0))))
        yaw = math.degrees(math.atan2(-float(forward[0]), -float(forward[2])))
        return cls(
            target=tuple(float(item) for item in camera.target),
            yaw=yaw, pitch=pitch, distance=float(camera.distance),
            pan=(0.0, 0.0),
            orientation=tuple(float(item) for item in orientation),
        )


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return result if isinstance(result, dict) else {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


class ThreeDViewportController(QObject):
    """Routes 3D input, frame overrides, and latest-only render requests."""

    modeChanged = Signal(bool, str)
    toolChanged = Signal(object)
    selectionChanged = Signal(object)
    frameChanged = Signal(str)
    imageChanged = Signal(str)
    hierarchyChanged = Signal()
    statusMessage = Signal(str)
    interactionCommitted = Signal(str)
    frameEditCommitted = Signal(str, object, object, str)
    editingAvailabilityChanged = Signal(bool, str)

    def __init__(
        self, parent: QObject | None = None, *, render_service=None,
        scene_provider: Callable[[str, Any, Any], Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.chapter = None
        self.sidecar = None
        self.active_layer_id = ""
        self.active_frame_id = ""
        self.tool = ThreeDToolKind.TRANSFORM
        self.pending_light_type = "point"
        self.multi_select = False
        self.selected_entity_ids: set[str] = set()
        self.navigation = ThreeDNavigationState()
        self._render_service = render_service
        self._scene_provider = scene_provider
        self._service_connected = False
        self._generation = 0
        self._expected_generation: dict[str, int] = {}
        self._images: dict[str, QImage] = {}
        self._errors: dict[str, str] = {}
        self._target_size: tuple[int, int] = (640, 480)
        self._active_scene = None
        self._drag_mode = ""
        self._gizmo_handle = ""
        self._drag_origin = QPointF()
        self._drag_last = QPointF()
        self._selection_path: list[QPointF] = []
        self._before_navigation: dict | None = None
        self._refine_timer = QTimer(self)
        self._refine_timer.setSingleShot(True)
        self._refine_timer.timeout.connect(
            lambda: self.request_render(interactive=False)
        )

    @property
    def active(self) -> bool:
        return bool(self.active_layer_id and self.active_frame_id)

    @property
    def render_available(self) -> bool:
        service = self._render_service
        return bool(service is not None and getattr(service, "available", False))

    @property
    def render_unavailable_reason(self) -> str:
        service = self._render_service
        if service is None:
            return self._errors.get(self.active_layer_id, "3D renderer not started")
        return str(
            getattr(service, "reason", "")
            or self._errors.get(self.active_layer_id, "")
        )

    @property
    def editing_available(self) -> bool:
        """3D mutation is disabled when no native renderer/scene is usable."""
        return self.render_available

    @property
    def target_size(self) -> tuple[int, int]:
        return self._target_size

    def set_documents(self, chapter, sidecar) -> None:
        if self.active:
            self.deactivate()
        self.chapter = chapter
        self.sidecar = sidecar
        self.selected_entity_ids.clear()
        self._images.clear()
        self._errors.clear()
        self.hierarchyChanged.emit()

    def _frame(self, frame_id: str | None = None):
        frames = getattr(self.sidecar, "frames", {}) if self.sidecar else {}
        return frames.get(frame_id or self.active_frame_id)

    def activate(
        self, layer_id: str, target_size: tuple[int, int] | None = None,
    ) -> bool:
        if self.chapter is None or layer_id not in self.chapter.layers:
            return False
        layer = self.chapter.layers[layer_id]
        if layer.layer_kind != "blender" or not layer.comic_frame_id:
            return False
        frame = self._frame(layer.comic_frame_id)
        if frame is None:
            self.statusMessage.emit("The 3D layer's comic-frame sidecar is missing")
            return False
        self.active_layer_id = layer_id
        self.active_frame_id = layer.comic_frame_id
        if target_size:
            self._target_size = (
                max(1, int(target_size[0])), max(1, int(target_size[1]))
            )
        overrides = getattr(frame, "presentation_overrides", {})
        self.navigation = ThreeDNavigationState.from_mapping(
            overrides.get("camera_navigation", {})
        )
        self.multi_select = bool(
            overrides.get("tool_settings", {}).get("multi_select", False)
        )
        self.selected_entity_ids.clear()
        self.modeChanged.emit(True, layer_id)
        self.frameChanged.emit(self.active_frame_id)
        self.request_render()
        return True

    def deactivate(self) -> None:
        if not self.active_layer_id:
            return
        previous = self.active_layer_id
        self._refine_timer.stop()
        self._drag_mode = ""
        self._gizmo_handle = ""
        self._selection_path.clear()
        self.active_layer_id = ""
        self.active_frame_id = ""
        self.selected_entity_ids.clear()
        self.modeChanged.emit(False, previous)

    def shutdown(self) -> None:
        service = self._render_service
        if service is not None:
            try:
                service.shutdown()
            except (AttributeError, RuntimeError):
                pass

    def set_tool(self, tool: ThreeDToolKind) -> None:
        if tool == self.tool:
            return
        self.tool = tool
        self._drag_mode = ""
        self._gizmo_handle = ""
        self._selection_path.clear()
        self.toolChanged.emit(tool)

    def set_pending_light_type(self, light_type: str) -> None:
        """Choose the frame-local light created by the Add Light tool."""
        normalized = str(light_type).lower()
        if normalized not in {"sun", "point", "rectangle", "spot"}:
            raise ValueError("Unsupported local light type")
        self.pending_light_type = normalized
        self.set_tool(ThreeDToolKind.ADD_LIGHT)

    @staticmethod
    def _matrix_components(matrix):
        """Return TRS plus residual shear without changing the source matrix."""
        import numpy as np

        value = np.asarray(matrix, dtype=np.float64).reshape((4, 4))
        rotation, upper = np.linalg.qr(value[:3, :3])
        if np.linalg.det(rotation) < 0.0:
            axis = int(np.argmax(np.abs(np.diag(upper))))
            rotation[:, axis] *= -1.0
            upper[axis, :] *= -1.0
        scales = np.diag(upper).copy()
        residual = np.identity(3, dtype=np.float64)
        for axis in range(3):
            if abs(scales[axis]) > 1.0e-12:
                residual[axis, :] = upper[axis, :] / scales[axis]
            else:
                residual[axis, :] = upper[axis, :]
                residual[axis, axis] = 1.0
        sy = max(-1.0, min(1.0, -float(rotation[2, 0])))
        y = math.asin(sy)
        if abs(math.cos(y)) > 1.0e-7:
            x = math.atan2(rotation[2, 1], rotation[2, 2])
            z = math.atan2(rotation[1, 0], rotation[0, 0])
        else:
            x = math.atan2(-rotation[1, 2], rotation[1, 1])
            z = 0.0
        return (
            value[:3, 3].copy(),
            np.degrees(np.array([x, y, z], dtype=np.float64)),
            scales,
            residual,
        )

    @staticmethod
    def _compose_matrix(translation, rotation_degrees, scales, residual):
        import numpy as np

        x, y, z = np.radians(np.asarray(rotation_degrees, dtype=np.float64))
        cx, sx = math.cos(x), math.sin(x)
        cy, sy = math.cos(y), math.sin(y)
        cz, sz = math.cos(z), math.sin(z)
        rotation = np.array([
            [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
            [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
            [-sy, cy * sx, cy * cx],
        ], dtype=np.float64)
        result = np.identity(4, dtype=np.float64)
        result[:3, :3] = (
            rotation
            @ np.diag(np.asarray(scales, dtype=np.float64))
            @ np.asarray(residual, dtype=np.float64)
        )
        result[:3, 3] = np.asarray(translation, dtype=np.float64)
        return result

    def _selected_matrix_target(self, *, create: bool = False):
        frame = self._frame()
        if frame is None or len(self.selected_entity_ids) != 1:
            return None
        entity_id = next(iter(self.selected_entity_ids))
        local = next((
            record for record in frame.local_entities
            if isinstance(record, dict) and str(record.get("id")) == entity_id
        ), None)
        if local is not None:
            transform = (
                local.setdefault("transform", {})
                if create else local.get("transform", {})
            )
            if not isinstance(transform, dict):
                return None
            raw = transform.get("matrix")
            target = transform
            key = "matrix"
        else:
            overrides = frame.presentation_overrides.get("transforms", {})
            if create:
                overrides = frame.presentation_overrides.setdefault(
                    "transforms", {}
                )
                target = overrides.setdefault(entity_id, {})
            else:
                target = (
                    overrides.get(entity_id, {})
                    if isinstance(overrides, dict) else {}
                )
            if not isinstance(target, dict):
                return None
            source = frame.source_state.get("transforms", {})
            raw = target.get("matrix_local")
            if raw is None and isinstance(source, dict):
                raw = source.get(entity_id, {}).get("matrix_local")
            key = "matrix_local"
        if not isinstance(raw, (list, tuple)) or len(raw) != 16:
            import numpy as np
            raw = np.identity(4, dtype=np.float64).reshape(
                16, order="F"
            ).tolist()
        import numpy as np
        matrix = np.asarray(raw, dtype=np.float64).reshape((4, 4), order="F")
        return frame, target, key, matrix

    def selected_transform_components(self):
        """Return position, XYZ degrees, and signed scale for one selection."""
        target = self._selected_matrix_target()
        if target is None:
            return None
        translation, rotation, scales, _residual = self._matrix_components(
            target[3]
        )
        return tuple(float(item) for item in (
            *translation, *rotation, *scales,
        ))

    def set_selected_transform_components(self, values) -> bool:
        """Write a sparse exact matrix while preserving residual source shear."""
        if not self.editing_available:
            return False
        target = self._selected_matrix_target(create=True)
        if target is None:
            return False
        numeric = tuple(float(item) for item in values)
        if len(numeric) != 9 or not all(math.isfinite(item) for item in numeric):
            raise ValueError("3D transforms require nine finite values")
        frame, record, key, matrix = target
        before = self._frame_payload(frame)
        # ComicFrameDocument.to_dict() validates and normalizes nested records,
        # so reacquire the target rather than retaining a stale nested mapping.
        target = self._selected_matrix_target(create=True)
        if target is None:
            return False
        frame, record, key, matrix = target
        _old_translation, _old_rotation, _old_scales, residual = (
            self._matrix_components(matrix)
        )
        replacement = self._compose_matrix(
            numeric[:3], numeric[3:6], numeric[6:9], residual
        )
        record[key] = replacement.reshape(16, order="F").tolist()
        self._touch_frame(frame)
        self._commit_frame_payload(frame, before, "Set 3D object transform")
        self.request_render()
        self.interactionCommitted.emit("Set 3D object transform")
        return True

    @staticmethod
    def _property_color(value: Any) -> str:
        """Return the neutral ARGB spelling used by the object-properties UI."""
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            channels = [max(0.0, min(1.0, float(item))) for item in value[:4]]
            while len(channels) < 4:
                channels.append(1.0)
            color = QColor.fromRgbF(*channels)
        else:
            color = QColor(str(value or "#FFFFFFFF"))
        if not color.isValid():
            color = QColor("#FFFFFFFF")
        return color.name(QColor.NameFormat.HexArgb).upper()

    @staticmethod
    def _property_color_vector(value: Any) -> list[float]:
        color = QColor(ThreeDViewportController._property_color(value))
        return [color.redF(), color.greenF(), color.blueF()]

    def _selected_property_target(self):
        """Resolve one selection to a local record or Blender property state."""
        frame = self._frame()
        if frame is None or len(self.selected_entity_ids) != 1:
            return None
        entity_id = next(iter(self.selected_entity_ids))
        local = next((
            record for record in frame.local_entities
            if isinstance(record, dict) and str(record.get("id")) == entity_id
        ), None)
        if local is not None:
            return frame, entity_id, "local", str(local.get("type", "")), local
        catalog = getattr(getattr(self.sidecar, "document", None),
                          "object_catalog", {})
        catalog_record = (
            catalog.get(entity_id, {}) if isinstance(catalog, dict) else {}
        )
        object_type = str(
            catalog_record.get("object_type", catalog_record.get("type", ""))
            if isinstance(catalog_record, dict) else ""
        ).lower()
        if entity_id in frame.source_state.get("lights", {}):
            object_type = "light"
        elif entity_id in frame.source_state.get("cameras", {}):
            object_type = "camera"
        if object_type not in {"light", "camera"}:
            return None
        return frame, entity_id, "blender", object_type, catalog_record

    def selected_entity_properties(self) -> dict[str, Any] | None:
        """Return editable neutral properties for exactly one selected entity.

        Blender values are merged with sparse Webtoon overrides.  The neutral
        field spellings deliberately hide Blender's radians, AREA spelling,
        and custom-distance details from the UI.
        """
        target = self._selected_property_target()
        if target is None:
            return None
        frame, entity_id, ownership, kind, record = target
        result: dict[str, Any] = {
            "entity_id": entity_id, "ownership": ownership, "kind": kind,
            "name": str(record.get("name", entity_id)), "properties": {},
        }
        if ownership == "local":
            parameters = record.get("parameters", {})
            if not isinstance(parameters, dict):
                parameters = {}
            if kind == "cube":
                size = parameters.get("size", (1.0, 1.0, 1.0))
                if not isinstance(size, (list, tuple)):
                    size = (size, size, size)
                values = [float(item) for item in size[:3]]
                values.extend([1.0] * (3 - len(values)))
                result["properties"] = {
                    "size_x": values[0], "size_y": values[1],
                    "size_z": values[2],
                }
            elif kind == "cylinder":
                result["properties"] = {
                    "radius": float(parameters.get("radius", 0.5)),
                    "depth": float(parameters.get("depth", 1.0)),
                    "segments": int(parameters.get(
                        "segments", parameters.get("vertices", 32)
                    )),
                }
            elif kind in {"sun", "point", "rectangle", "spot"}:
                size = parameters.get("size", (1.0, 1.0))
                if not isinstance(size, (list, tuple)):
                    size = (size, size)
                result["kind"] = "light"
                result["properties"] = {
                    "light_type": kind,
                    "color": self._property_color(parameters.get("color")),
                    "energy": float(parameters.get("energy", 1.0)),
                    "range": float(parameters.get("range", 0.0)),
                    "area_width": float(size[0]),
                    "area_height": float(size[1] if len(size) > 1 else size[0]),
                    "spot_angle": float(parameters.get("spot_size", 45.0)),
                    "casts_shadow": bool(parameters.get("casts_shadow", True)),
                }
            else:
                return None
            return result

        source = frame.source_state.get(f"{kind}s", {})
        overrides = frame.presentation_overrides.get(f"{kind}s", {})
        base = source.get(entity_id, {}) if isinstance(source, dict) else {}
        changed = overrides.get(entity_id, {}) if isinstance(overrides, dict) else {}
        values = {
            **(base if isinstance(base, dict) else {}),
            **(changed if isinstance(changed, dict) else {}),
        }
        if kind == "light":
            raw_type = str(values.get("type", "POINT")).lower()
            light_type = {
                "area": "rectangle", "rect": "rectangle",
            }.get(raw_type, raw_type)
            if light_type not in {"sun", "point", "rectangle", "spot"}:
                light_type = "point"
            use_custom_distance = bool(values.get("use_custom_distance", False))
            light_range = values.get("range")
            if light_range is None:
                light_range = (
                    values.get("cutoff_distance", 0.0)
                    if use_custom_distance else 0.0
                )
            size = values.get("size", 1.0)
            size_y = values.get("size_y", size)
            spot_size = float(values.get("spot_size", math.radians(45.0)))
            result["properties"] = {
                "light_type": light_type,
                "color": self._property_color(values.get("color")),
                "energy": float(values.get("energy", 1.0)),
                "range": float(light_range),
                "area_width": float(size), "area_height": float(size_y),
                "spot_angle": math.degrees(spot_size),
                "casts_shadow": bool(values.get(
                    "casts_shadow", values.get("use_shadow", True)
                )),
            }
        else:
            raw_type = str(values.get("type", "PERSP")).upper()
            fov = values.get(
                "fov_y_radians", values.get("fov_radians", math.radians(50.0))
            )
            result["properties"] = {
                "camera_type": (
                    "orthographic" if raw_type == "ORTHO" else "perspective"
                ),
                "fov": math.degrees(float(fov)),
                "ortho_scale": float(values.get("ortho_scale", 10.0)),
                "clip_start": float(values.get("clip_start", 0.01)),
                "clip_end": float(values.get("clip_end", 1000.0)),
            }
        return result

    def selected_entity_metadata(
        self, entity_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return source-owned metadata for read-only Object Properties display."""
        frame = self._frame()
        if frame is None:
            return None
        if entity_id is None:
            if len(self.selected_entity_ids) != 1:
                return None
            entity_id = next(iter(self.selected_entity_ids))
        entity_id = str(entity_id)
        local = next((
            item for item in frame.local_entities
            if isinstance(item, dict) and str(item.get("id")) == entity_id
        ), None)
        if local is not None:
            return {"ownership": "Webtoon", **copy.deepcopy(local)}
        document = getattr(self.sidecar, "document", None)
        catalog = getattr(document, "object_catalog", {})
        result: dict[str, Any] = {
            "ownership": "Blender", "source_id": entity_id,
            "catalog": copy.deepcopy(
                catalog.get(entity_id, {}) if isinstance(catalog, dict) else {}
            ),
        }
        captured: dict[str, Any] = {}
        for category in (
            "transforms", "visibility", "lights", "cameras",
            "shape_keys", "poses", "opaque_keyframed",
        ):
            records = frame.source_state.get(category, {})
            if isinstance(records, dict) and entity_id in records:
                captured[category] = copy.deepcopy(records[entity_id])
        if captured:
            result["captured"] = captured
        return result

    @staticmethod
    def _sparse_property(overrides: dict, key: str, value, base) -> None:
        """Write one override, removing it when it equals the Blender value."""
        equal = False
        if isinstance(value, float) or isinstance(base, float):
            try:
                equal = math.isclose(
                    float(value), float(base), rel_tol=1.0e-9, abs_tol=1.0e-9
                )
            except (TypeError, ValueError):
                equal = False
        else:
            equal = value == base
        if equal:
            overrides.pop(key, None)
        else:
            overrides[key] = value

    def set_selected_entity_properties(self, values: dict[str, Any]) -> bool:
        """Apply one validated entity-property edit as one undoable frame edit."""
        if not self.editing_available:
            return False
        target = self._selected_property_target()
        if target is None or not isinstance(values, dict):
            return False
        frame, entity_id, ownership, kind, record = target
        current_descriptor = self.selected_entity_properties()
        current_properties = (
            current_descriptor.get("properties", {})
            if current_descriptor is not None else {}
        )
        before = self._frame_payload(frame)
        # Serializing the before-payload normalizes the dataclass's nested
        # dictionaries in place.  Resolve the selected record again afterward.
        target = self._selected_property_target()
        if target is None:
            return False
        frame, entity_id, ownership, kind, record = target

        def finite(name: str, default: float, minimum: float = 0.0) -> float:
            value = float(values.get(name, default))
            if not math.isfinite(value) or value < minimum:
                raise ValueError(f"{name} must be a finite value of at least {minimum}")
            return value

        if ownership == "local":
            parameters = record.setdefault("parameters", {})
            if not isinstance(parameters, dict):
                raise ValueError("Local 3D entity parameters must be an object")
            if kind == "cube":
                parameters["size"] = [
                    finite("size_x", 1.0, 0.001),
                    finite("size_y", 1.0, 0.001),
                    finite("size_z", 1.0, 0.001),
                ]
            elif kind == "cylinder":
                parameters["radius"] = finite("radius", 0.5, 0.001)
                parameters["depth"] = finite("depth", 1.0, 0.001)
                segments = int(values.get("segments", 32))
                if not 3 <= segments <= 512:
                    raise ValueError("Cylinder segments must be between 3 and 512")
                parameters["vertices"] = segments
                parameters.pop("segments", None)
            elif kind in {"sun", "point", "rectangle", "spot"}:
                new_type = str(values.get("light_type", kind)).lower()
                if new_type not in {"sun", "point", "rectangle", "spot"}:
                    raise ValueError("Unsupported local light type")
                record["type"] = new_type
                parameters["color"] = self._property_color(values.get("color"))
                parameters["energy"] = finite("energy", 1.0)
                parameters["range"] = finite("range", 0.0)
                parameters["size"] = [
                    finite("area_width", 1.0, 0.001),
                    finite("area_height", 1.0, 0.001),
                ]
                parameters["spot_size"] = finite("spot_angle", 45.0, 0.001)
                parameters["casts_shadow"] = bool(
                    values.get("casts_shadow", True)
                )
            else:
                return False
        else:
            category = f"{kind}s"
            sources = frame.source_state.get(category, {})
            base = sources.get(entity_id, {}) if isinstance(sources, dict) else {}
            if not isinstance(base, dict):
                base = {}
            category_overrides = frame.presentation_overrides.setdefault(
                category, {}
            )
            override = category_overrides.setdefault(entity_id, {})
            if not isinstance(override, dict):
                override = {}
                category_overrides[entity_id] = override

            def property_changed(name: str) -> bool:
                if name not in values:
                    return False
                current = current_properties.get(name)
                proposed = values[name]
                if name == "color":
                    return self._property_color(proposed) != self._property_color(
                        current
                    )
                if isinstance(current, (int, float)) and not isinstance(
                    current, bool
                ):
                    try:
                        return not math.isclose(
                            float(current), float(proposed),
                            rel_tol=1.0e-9, abs_tol=1.0e-9,
                        )
                    except (TypeError, ValueError):
                        return True
                return current != proposed

            if kind == "light":
                new_type = str(values.get("light_type", "point")).lower()
                blender_type = {
                    "sun": "SUN", "point": "POINT",
                    "rectangle": "AREA", "spot": "SPOT",
                }.get(new_type)
                if blender_type is None:
                    raise ValueError("Unsupported Blender light type")
                fields: dict[str, Any] = {}
                if property_changed("light_type"):
                    fields["type"] = blender_type
                if property_changed("color"):
                    fields["color"] = self._property_color_vector(
                        values.get("color")
                    )
                if property_changed("energy"):
                    fields["energy"] = finite("energy", 1.0)
                if property_changed("range"):
                    light_range = finite("range", 0.0)
                    fields["use_custom_distance"] = light_range > 0.0
                    fields["cutoff_distance"] = light_range
                if property_changed("area_width"):
                    fields["size"] = finite("area_width", 1.0, 0.001)
                if property_changed("area_height"):
                    fields["size_y"] = finite("area_height", 1.0, 0.001)
                if property_changed("spot_angle"):
                    fields["spot_size"] = math.radians(finite(
                        "spot_angle", 45.0, 0.001
                    ))
                if property_changed("casts_shadow"):
                    fields["use_shadow"] = bool(values.get(
                        "casts_shadow", True
                    ))
                for key, value in fields.items():
                    self._sparse_property(override, key, value, base.get(key))
            else:
                camera_type = str(
                    values.get("camera_type", "perspective")
                ).lower()
                if camera_type not in {"perspective", "orthographic"}:
                    raise ValueError("Unsupported Blender camera type")
                fields: dict[str, Any] = {}
                if property_changed("camera_type"):
                    fields["type"] = (
                        "ORTHO" if camera_type == "orthographic" else "PERSP"
                    )
                if property_changed("fov"):
                    fields["fov_y_radians"] = math.radians(finite(
                        "fov", 50.0, 0.001
                    ))
                if property_changed("ortho_scale"):
                    fields["ortho_scale"] = finite(
                        "ortho_scale", 10.0, 0.001
                    )
                if property_changed("clip_start"):
                    fields["clip_start"] = finite(
                        "clip_start", 0.01, 0.000001
                    )
                if property_changed("clip_end"):
                    clip_start = float(values.get(
                        "clip_start", current_properties.get("clip_start", 0.01)
                    ))
                    fields["clip_end"] = finite(
                        "clip_end", 1000.0, clip_start + 0.000001
                    )
                for key, value in fields.items():
                    base_value = base.get(key)
                    if key == "fov_y_radians" and base_value is None:
                        base_value = base.get("fov_radians")
                    self._sparse_property(override, key, value, base_value)
            if not override:
                category_overrides.pop(entity_id, None)
            if not category_overrides:
                frame.presentation_overrides.pop(category, None)

        after_without_revision = self._frame_payload(frame)
        if before == after_without_revision:
            return False
        self._touch_frame(frame)
        self._commit_frame_payload(frame, before, "Set 3D object properties")
        self.request_render()
        self.hierarchyChanged.emit()
        self.interactionCommitted.emit("Set 3D object properties")
        return True

    def set_multi_select(self, enabled: bool) -> None:
        if not self.editing_available:
            return
        enabled = bool(enabled)
        self.multi_select = enabled
        frame = self._frame()
        if frame is not None:
            before = self._frame_payload(frame)
            overrides = frame.presentation_overrides
            overrides.setdefault("tool_settings", {})["multi_select"] = enabled
            self._touch_frame(frame)
            self._commit_frame_payload(frame, before, "Change 3D tool settings")
        if not enabled and len(self.selected_entity_ids) > 1:
            self.selected_entity_ids = set(sorted(self.selected_entity_ids)[:1])
            self.selectionChanged.emit(set(self.selected_entity_ids))
        self.interactionCommitted.emit("Change 3D tool settings")

    def set_transform_settings(
        self, *, space: str | None = None, mode: str | None = None,
    ) -> None:
        if not self.editing_available:
            return
        frame = self._frame()
        if frame is None:
            return
        before = self._frame_payload(frame)
        settings = frame.presentation_overrides.setdefault("tool_settings", {})
        if space is not None:
            settings["transform_space"] = (
                space if space in {"global", "local"} else "global"
            )
        if mode is not None:
            settings["gizmo_mode"] = (
                mode if mode in {"move", "rotate", "scale", "trackball"}
                else "move"
            )
        self._touch_frame(frame)
        self._commit_frame_payload(frame, before, "Change 3D transform tool")
        self.interactionCommitted.emit("Change 3D transform tool")

    def renderer_setting(self, name: str, default=None):
        frame = self._frame()
        if frame is None:
            return default
        return frame.presentation_overrides.get(
            "renderer_settings", {}
        ).get(name, default)

    def set_renderer_setting(self, name: str, value) -> None:
        if not self.editing_available:
            return
        frame = self._frame()
        if frame is None:
            return
        before = self._frame_payload(frame)
        frame.presentation_overrides.setdefault(
            "renderer_settings", {}
        )[name] = value
        self._touch_frame(frame)
        self._commit_frame_payload(frame, before, f"Change 3D {name}")
        self.request_render()
        self.interactionCommitted.emit(f"Change 3D {name}")

    def set_virtual_visibility(
        self, layer_id: str, source_id: str, visible: bool,
    ) -> None:
        if (
            not self.editing_available or self.chapter is None
            or layer_id not in self.chapter.layers
        ):
            return
        frame = self._frame(self.chapter.layers[layer_id].comic_frame_id)
        if frame is None:
            return
        before = self._frame_payload(frame)
        document = getattr(self.sidecar, "document", None)
        collection_catalog = getattr(document, "collection_catalog", {})
        if source_id in collection_catalog:
            frame.presentation_overrides.setdefault(
                "collection_visibility", {}
            )[source_id] = bool(visible)
        else:
            local = next((
                item for item in frame.local_entities
                if isinstance(item, dict) and str(item.get("id")) == source_id
            ), None)
            if local is not None:
                local["visible"] = bool(visible)
            else:
                frame.presentation_overrides.setdefault(
                    "visibility", {}
                )[source_id] = bool(visible)
        self._touch_frame(frame)
        self._commit_frame_payload(
            frame, before, "Toggle Blender object visibility"
        )
        self.hierarchyChanged.emit()
        if layer_id == self.active_layer_id:
            self.request_render()
        self.interactionCommitted.emit("Toggle Blender object visibility")

    @staticmethod
    def _touch_frame(frame) -> None:
        frame.revision = max(int(getattr(frame, "revision", 0)) + 1, 1)

    def add_local_entity(self, entity_type: str, **parameters) -> str:
        if not self.editing_available:
            return ""
        frame = self._frame()
        if frame is None or entity_type not in {
            "cube", "cylinder", "sun", "point", "rectangle", "spot",
        }:
            return ""
        before = self._frame_payload(frame)
        entity_id = str(uuid.uuid4())
        entity = {
            "id": entity_id, "name": parameters.pop(
                "name", entity_type.replace("_", " ").title()
            ),
            "type": entity_type, "transform": parameters.pop(
                "transform", {
                    "translation": [0.0, 0.0, 0.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                }
            ),
            "parameters": parameters,
        }
        frame.local_entities.append(entity)
        self._touch_frame(frame)
        self._commit_frame_payload(
            frame, before, f"Add local 3D {entity_type}"
        )
        self.selected_entity_ids = {entity_id}
        self.selectionChanged.emit(set(self.selected_entity_ids))
        self.hierarchyChanged.emit()
        self.request_render()
        self.interactionCommitted.emit(f"Add local 3D {entity_type}")
        return entity_id

    def reset_selected_to_blender(self) -> bool:
        if not self.editing_available:
            return False
        frame = self._frame()
        if frame is None or not self.selected_entity_ids:
            return False
        before = self._frame_payload(frame)
        local_by_id = {
            str(record.get("id")): record
            for record in frame.local_entities if isinstance(record, dict)
        }
        for entity_id in self.selected_entity_ids:
            if entity_id in local_by_id:
                local_by_id[entity_id]["transform"] = {
                    "translation": [0.0, 0.0, 0.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                }
                continue
            for category in (
                "transforms", "visibility", "shape_keys", "poses",
                "cameras", "lights",
            ):
                values = frame.presentation_overrides.get(category)
                if isinstance(values, dict):
                    values.pop(entity_id, None)
        self._touch_frame(frame)
        self._commit_frame_payload(frame, before, "Reset 3D object to Blender")
        self.request_render()
        self.interactionCommitted.emit("Reset 3D object to Blender")
        return True

    @staticmethod
    def _frame_payload(frame) -> dict:
        return copy.deepcopy(_mapping(frame))

    def _commit_frame_payload(
        self, frame, before: dict | None, label: str,
    ) -> None:
        if before is None:
            return
        after = self._frame_payload(frame)
        if before != after:
            self.frameEditCommitted.emit(
                frame.frame_id, before, after, label
            )

    def apply_frame_payload(
        self, frame_id: str, payload: dict, *, monotonic: bool = True,
    ) -> None:
        """Apply an undo/redo payload without rewinding sync revisions."""
        if self.sidecar is None or frame_id not in self.sidecar.frames:
            return
        current = self.sidecar.frames[frame_id]
        current_revision = int(getattr(current, "revision", 0))
        try:
            from comic_editor.three_d.documents import ComicFrameDocument
            replacement = ComicFrameDocument.from_dict(copy.deepcopy(payload))
        except (ImportError, TypeError, ValueError, AttributeError):
            replacement = copy.deepcopy(current)
            replacement.__dict__.clear()
            replacement.__dict__.update(copy.deepcopy(payload))
        if monotonic:
            replacement.revision = max(
                current_revision + 1, int(getattr(replacement, "revision", 0)) + 1
            )
        self.sidecar.frames[frame_id] = replacement
        if frame_id == self.active_frame_id:
            self.navigation = ThreeDNavigationState.from_mapping(
                replacement.presentation_overrides.get(
                    "camera_navigation", {}
                )
            )
            self.multi_select = bool(
                replacement.presentation_overrides.get(
                    "tool_settings", {}
                ).get("multi_select", False)
            )
            self.request_render()
        self.hierarchyChanged.emit()

    def replace_sidecar(self, snapshot, *, monotonic: bool = True) -> None:
        """Restore a sidecar transaction while keeping revision counters rising."""
        if self.sidecar is None or snapshot is None:
            return
        current_document = getattr(self.sidecar, "document", None)
        current_revision = int(getattr(current_document, "revision", 0))
        current_source_revision = int(
            getattr(current_document, "source_revision", 0)
        )
        current_extensions = getattr(current_document, "extensions", {})
        current_source_digest = (
            str(current_extensions.get("accepted_source_digest", ""))
            if isinstance(current_extensions, dict) else ""
        )
        restored = copy.deepcopy(snapshot)
        if monotonic and getattr(restored, "document", None) is not None:
            restored.document.revision = max(
                current_revision + 1,
                int(getattr(restored.document, "revision", 0)) + 1,
            )
            restored_source_revision = int(
                getattr(restored.document, "source_revision", 0)
            )
            if restored_source_revision <= current_source_revision:
                restored.document.source_revision = current_source_revision
                if current_source_digest:
                    restored.document.extensions[
                        "accepted_source_digest"
                    ] = current_source_digest
        self.sidecar.__dict__.clear()
        self.sidecar.__dict__.update(restored.__dict__)
        self.hierarchyChanged.emit()
        if self.active_frame_id in self.sidecar.frames:
            self.request_render()

    def image_for_layer(self, layer_id: str) -> QImage | None:
        image = self._images.get(layer_id)
        return QImage(image) if image is not None and not image.isNull() else None

    def set_cached_image(self, layer_id: str, image: QImage) -> None:
        if not image.isNull():
            self._images[layer_id] = QImage(image)
            self.imageChanged.emit(layer_id)

    def _ensure_render_service(self) -> bool:
        if self._render_service is None:
            try:
                from comic_editor.three_d.render_service import RenderService
                self._render_service = RenderService(self)
            except Exception as error:  # optional GL dependency / platform
                self._errors[self.active_layer_id] = str(error)
                return False
        if not self._service_connected:
            self._render_service.result_ready.connect(self._accept_result)
            self._render_service.render_failed.connect(self._render_failed)
            availability = getattr(
                self._render_service, "availability_changed", None
            )
            if availability is not None:
                availability.connect(self._renderer_availability_changed)
            self._service_connected = True
        return True

    def _renderer_availability_changed(
        self, available: bool, reason: str,
    ) -> None:
        self.editingAvailabilityChanged.emit(
            bool(available), str(reason)
        )

    def request_render(
        self, target_size: tuple[int, int] | None = None, *,
        interactive: bool = False,
    ) -> int:
        if not self.active:
            return 0
        if target_size:
            self._target_size = (
                max(1, int(target_size[0])), max(1, int(target_size[1]))
            )
        if not self._ensure_render_service():
            self.imageChanged.emit(self.active_layer_id)
            return 0
        try:
            from comic_editor.three_d.render_service import (
                RenderQuality, RenderRequest,
            )
        except ImportError as error:
            self._errors[self.active_layer_id] = str(error)
            self.imageChanged.emit(self.active_layer_id)
            return 0
        frame = self._frame()
        document = getattr(self.sidecar, "document", None)
        if frame is None or document is None:
            return 0
        scene = None
        if self._scene_provider is not None:
            try:
                scene = self._scene_provider(
                    self.active_layer_id, frame, self.sidecar
                )
                warnings = getattr(scene, "warnings", ()) if scene is not None else ()
                if warnings:
                    self.statusMessage.emit(str(warnings[-1]))
            except (OSError, ValueError, RuntimeError) as error:
                self._errors[self.active_layer_id] = str(error)
        self._active_scene = scene
        # A frame without an app-owned camera override initially renders from
        # Blender's exact camera.  Seed the controller from that result so the
        # first orbit, pan, or wheel gesture continues from the visible view.
        if (
            scene is not None
            and not frame.presentation_overrides.get("camera_navigation")
        ):
            self.navigation = ThreeDNavigationState.from_camera(
                scene.active_camera
            )
        self.editingAvailabilityChanged.emit(
            self.editing_available, self.render_unavailable_reason
        )
        width, height = self._target_size
        if interactive:
            width, height = max(64, width // 2), max(64, height // 2)
        quality_name = str(
            self.renderer_setting("quality", "interactive" if interactive else "full")
        ).lower()
        quality = (
            RenderQuality.INTERACTIVE if interactive
            else {
                "draft": RenderQuality.DRAFT,
                "interactive": RenderQuality.INTERACTIVE,
                "full": RenderQuality.FINAL,
                "final": RenderQuality.FINAL,
            }.get(quality_name, RenderQuality.FINAL)
        )
        self._generation += 1
        request = RenderRequest(
            chapter_id=self.chapter.chapter_id,
            frame_id=frame.frame_id,
            scene_revision=int(getattr(frame, "revision", 0)),
            material_revision=int(getattr(document, "revision", 0)),
            cache_revision=str(
                getattr(document, "current_cache_revision", "") or ""
            ),
            target_size=(width, height), quality=quality,
            generation_id=self._generation, scene=scene, transparent=True,
            antialiasing=bool(self.renderer_setting("antialiasing", False)),
            selected_node_ids=frozenset(self.selected_entity_ids),
        )
        generation = int(self._render_service.submit(request))
        self._expected_generation[self.active_layer_id] = generation
        return generation

    def _accept_result(self, result) -> None:
        request = getattr(result, "request", None)
        if request is None or self.chapter is None:
            return
        layer_id = next((
            candidate.layer_id for candidate in self.chapter.layers.values()
            if candidate.layer_kind == "blender"
            and candidate.comic_frame_id == request.frame_id
        ), "")
        if not layer_id or self._expected_generation.get(layer_id) != result.generation_id:
            return
        if request.chapter_id != self.chapter.chapter_id:
            return
        image = getattr(result, "image", QImage())
        available = bool(getattr(result, "available", not getattr(result, "error", None)))
        if not image.isNull() and (
            available or layer_id not in self._images
        ):
            self._images[layer_id] = QImage(image)
        error = getattr(result, "error", None)
        if error:
            self._errors[layer_id] = str(error)
        else:
            self._errors.pop(layer_id, None)
        self.imageChanged.emit(layer_id)

    def _render_failed(self, generation: int, message: str) -> None:
        if self._expected_generation.get(self.active_layer_id) != generation:
            return
        self._errors[self.active_layer_id] = str(message)
        self.imageChanged.emit(self.active_layer_id)

    def virtual_hierarchy(self) -> dict[str, list[dict]]:
        if self.chapter is None or self.sidecar is None:
            return {}
        document = getattr(self.sidecar, "document", None)
        if document is None:
            return {}
        collections = {
            str(key): _mapping(value)
            for key, value in getattr(document, "collection_catalog", {}).items()
        }
        objects = {
            str(key): _mapping(value)
            for key, value in getattr(document, "object_catalog", {}).items()
        }
        result: dict[str, list[dict]] = {}
        for layer in self.chapter.layers.values():
            if layer.layer_kind != "blender" or not layer.comic_frame_id:
                continue
            frame = self._frame(layer.comic_frame_id)
            if frame is None:
                result[layer.layer_id] = []
                continue
            source_visibility = frame.source_state.get("visibility", {})
            visibility = frame.presentation_overrides.get("visibility", {})
            included = set(getattr(frame, "included_collection_ids", ()))
            collection_entries: dict[str, dict] = {}
            roots: list[dict] = []
            for collection_id, record in collections.items():
                entry = {
                    "id": collection_id,
                    "name": record.get("name", collection_id),
                    "kind": "collection", "type_label": "Collection",
                    "visible": frame.collection_visible(collection_id),
                    "children": [],
                }
                collection_entries[collection_id] = entry
            for collection_id, record in collections.items():
                parent_id = str(record.get(
                    "parent_id", record.get("parent_collection_id", "")
                ) or "")
                entry = collection_entries[collection_id]
                if parent_id in collection_entries:
                    collection_entries[parent_id]["children"].append(entry)
                else:
                    roots.append(entry)
            for object_id, record in objects.items():
                object_type = str(
                    record.get("object_type", record.get("type", "object"))
                ).lower()
                entry = {
                    "id": object_id,
                    "name": record.get("name", object_id),
                    "kind": object_type,
                    "type_label": object_type.replace("_", " ").title(),
                    "visible": bool(visibility.get(
                        object_id,
                        _mapping(source_visibility.get(object_id, {})).get(
                            "visible",
                            not _mapping(source_visibility.get(object_id, {})).get(
                                "hide_render", False
                            ),
                        ) if isinstance(
                            source_visibility.get(object_id), dict
                        ) else source_visibility.get(object_id, True),
                    )),
                    "children": [],
                }
                collection_ids = record.get("collection_ids", ())
                if not collection_ids:
                    single = record.get("collection_id", "")
                    collection_ids = [single] if single else []
                owner = next((
                    collection_entries.get(str(candidate))
                    for candidate in collection_ids
                    if str(candidate) in collection_entries
                ), None)
                (owner["children"] if owner is not None else roots).append(entry)
            for local in getattr(frame, "local_entities", ()):
                record = _mapping(local)
                roots.append({
                    "id": str(record.get("id", "")),
                    "name": str(record.get("name", "Local Entity")),
                    "kind": str(record.get("type", "local")),
                    "type_label": "Local " + str(
                        record.get("type", "Entity")
                    ).title(),
                    "visible": bool(record.get("visible", True)),
                    "children": [],
                })
            result[layer.layer_id] = roots
        return result

    @staticmethod
    def _selection_operation(modifiers) -> str:
        if modifiers & Qt.ControlModifier:
            return "remove"
        if modifiers & Qt.ShiftModifier:
            return "add"
        return "replace"

    def cursor_mode(self, modifiers) -> str:
        if not self.multi_select:
            return "select"
        return self._selection_operation(modifiers)

    def gizmo_geometry(self) -> dict[str, Any] | None:
        """Return projected handles for the primary selected scene entity."""
        scene = self._active_scene
        selected = next((
            scene.nodes[item] for item in sorted(self.selected_entity_ids)
            if scene is not None and item in scene.nodes
        ), None)
        if scene is None or selected is None:
            return None
        import numpy as np
        from comic_editor.three_d.renderer.projection import clip_planes

        low, high = scene.bounds()
        radius = max(0.001, float(np.linalg.norm(high - low)) * 0.5)
        near, far = clip_planes(scene.active_camera.distance, radius)
        context = scene.projection.context(self._target_size, near, far)
        camera_point = (
            scene.active_camera.view_matrix()
            @ np.append(selected.world_origin, 1.0)
        )[:3]
        projected = scene.projection.project_cpu(camera_point, context)
        if not projected.lens_valid:
            return None
        center = QPointF(
            float(projected.screen_px[0]), float(projected.screen_px[1])
        )
        frame = self._frame()
        settings = (
            frame.presentation_overrides.get("tool_settings", {})
            if frame is not None else {}
        )
        space = str(settings.get("transform_space", "global"))
        mode = str(settings.get("gizmo_mode", "move"))
        if space == "local":
            linear = selected.world_matrix[:3, :3]
            directions = []
            for index in range(3):
                value = linear[:, index].astype(np.float64)
                length = float(np.linalg.norm(value))
                directions.append(
                    value / length if length > 1.0e-12
                    else np.identity(3, dtype=np.float64)[:, index]
                )
        else:
            directions = [
                np.array([1.0, 0.0, 0.0]),
                np.array([0.0, 1.0, 0.0]),
                np.array([0.0, 0.0, 1.0]),
            ]
        axes: dict[str, QPointF] = {}
        world_axes: dict[str, Any] = {}
        length = 64.0
        for name, direction in zip(("x", "y", "z"), directions):
            screen = np.array([
                float(np.dot(direction, scene.active_camera.right)),
                -float(np.dot(direction, scene.active_camera.up)),
            ])
            screen_length = float(np.linalg.norm(screen))
            if screen_length <= 1.0e-6:
                # An axis aimed straight through the lens still gets a small,
                # deterministic handle instead of becoming unselectable.
                screen = np.array([0.35, -0.35])
                screen_length = float(np.linalg.norm(screen))
            screen /= screen_length
            axes[name] = QPointF(
                center.x() + float(screen[0]) * length,
                center.y() + float(screen[1]) * length,
            )
            world_axes[name] = direction
        return {
            "center": center, "axes": axes, "world_axes": world_axes,
            "mode": mode, "space": space, "radius": length * 0.72,
            "node_id": selected.node_id,
        }

    @staticmethod
    def _distance_to_segment(
        point: QPointF, start: QPointF, end: QPointF,
    ) -> float:
        dx, dy = end.x() - start.x(), end.y() - start.y()
        length_squared = dx * dx + dy * dy
        if length_squared <= 1.0e-12:
            return math.dist(point.toTuple(), start.toTuple())
        amount = max(0.0, min(1.0, (
            (point.x() - start.x()) * dx
            + (point.y() - start.y()) * dy
        ) / length_squared))
        closest = QPointF(start.x() + dx * amount, start.y() + dy * amount)
        return math.dist(point.toTuple(), closest.toTuple())

    def _gizmo_hit(self, position: QPointF) -> str:
        geometry = self.gizmo_geometry()
        if geometry is None:
            return ""
        center = geometry["center"]
        distance = math.dist(position.toTuple(), center.toTuple())
        mode = geometry["mode"]
        if mode == "trackball":
            return "trackball" if distance <= geometry["radius"] + 10.0 else ""
        ranked = sorted(
            (
                self._distance_to_segment(position, center, endpoint), name
            )
            for name, endpoint in geometry["axes"].items()
        )
        if ranked and ranked[0][0] <= 9.0:
            return ranked[0][1]
        if distance <= 11.0 and mode in {"move", "scale"}:
            return "free"
        return ""

    def pointer_press(self, position: QPointF, button, modifiers) -> bool:
        if not self.active:
            return False
        if not self.editing_available:
            self.statusMessage.emit(
                self.render_unavailable_reason or "3D editing requires OpenGL"
            )
            return False
        if button == Qt.MiddleButton:
            self._drag_mode = "pan"
            self._drag_origin = self._drag_last = QPointF(position)
            frame = self._frame()
            self._before_navigation = (
                self._frame_payload(frame) if frame is not None else None
            )
            return True
        if button != Qt.LeftButton:
            return False
        self._drag_origin = self._drag_last = QPointF(position)
        frame = self._frame()
        self._before_navigation = (
            self._frame_payload(frame) if frame is not None else None
        )
        if self.tool in {ThreeDToolKind.SELECT_RECT, ThreeDToolKind.SELECT_LASSO}:
            self._drag_mode = "select"
            self._selection_path = [QPointF(position)]
        elif self.tool == ThreeDToolKind.DRAW_CUBE:
            self._drag_mode = "draw_cube"
        elif self.tool == ThreeDToolKind.DRAW_CYLINDER:
            self._drag_mode = "draw_cylinder"
        elif self.tool == ThreeDToolKind.ADD_LIGHT:
            self._drag_mode = "add_light"
        elif (
            self.tool == ThreeDToolKind.TRANSFORM
            and self.selected_entity_ids
        ):
            self._gizmo_handle = self._gizmo_hit(position)
            self._drag_mode = "transform" if self._gizmo_handle else "orbit"
        else:
            self._drag_mode = "orbit"
        return True

    def _apply_transform_drag(self, delta: QPointF) -> None:
        frame = self._frame()
        if frame is None:
            return
        import numpy as np

        settings = frame.presentation_overrides.get("tool_settings", {})
        mode = str(settings.get("gizmo_mode", "move"))
        space = str(settings.get("transform_space", "global"))
        geometry = self.gizmo_geometry()
        if geometry is None or not self._gizmo_handle:
            return
        overrides = frame.presentation_overrides.setdefault("transforms", {})
        source = frame.source_state.get("transforms", {})
        local_by_id = {
            str(record.get("id")): record
            for record in frame.local_entities if isinstance(record, dict)
        }
        for entity_id in self.selected_entity_ids:
            local_record = local_by_id.get(entity_id)
            if local_record is not None:
                transform = local_record.setdefault("transform", {})
                raw = transform.get("matrix")
            else:
                record = overrides.setdefault(entity_id, {})
                raw = record.get("matrix_local")
                if raw is None:
                    raw = source.get(entity_id, {}).get("matrix_local")
            matrix = (
                np.asarray(raw, dtype=np.float64).reshape((4, 4), order="F")
                if isinstance(raw, (list, tuple)) and len(raw) == 16
                else np.identity(4, dtype=np.float64)
            )
            scene_node = self._active_scene.nodes.get(entity_id)
            parent_world = np.identity(4, dtype=np.float64)
            if (
                scene_node is not None and scene_node.parent_id
                and scene_node.parent_id in self._active_scene.nodes
            ):
                parent_world = self._active_scene.nodes[
                    scene_node.parent_id
                ].world_matrix
            world = parent_world @ matrix
            pivot = world[:3, 3].copy()
            handle = self._gizmo_handle
            endpoint = geometry["axes"].get(handle)
            screen_direction = None
            if endpoint is not None:
                screen_direction = np.array([
                    endpoint.x() - geometry["center"].x(),
                    endpoint.y() - geometry["center"].y(),
                ], dtype=np.float64)
                screen_direction /= max(
                    float(np.linalg.norm(screen_direction)), 1.0e-12
                )
            axis_world = geometry["world_axes"].get(handle)
            camera = self._active_scene.active_camera
            if str(getattr(self._active_scene.projection, "mode", "")).lower().endswith("orthographic"):
                world_per_pixel = (
                    float(self._active_scene.projection.ortho_height)
                    / max(1, self._target_size[1])
                )
            else:
                depth = max(
                    0.01,
                    float(np.linalg.norm(pivot - camera.position)),
                )
                world_per_pixel = (
                    2.0 * depth
                    * math.tan(math.radians(
                        float(self._active_scene.projection.vertical_fov_deg)
                    ) * 0.5)
                    / max(1, self._target_size[1])
                )
            if mode == "move":
                if axis_world is None:
                    motion = (
                        camera.right * float(delta.x())
                        - camera.up * float(delta.y())
                    ) * world_per_pixel
                else:
                    scalar = float(np.dot(
                        np.array([delta.x(), delta.y()]), screen_direction
                    )) * world_per_pixel
                    motion = np.asarray(axis_world) * scalar
                world[:3, 3] += motion
                matrix = np.linalg.pinv(parent_world) @ world
            elif mode == "scale":
                amount = (
                    float(np.dot(
                        np.array([delta.x(), delta.y()]), screen_direction
                    )) if screen_direction is not None
                    else float(delta.x() - delta.y())
                )
                factor = max(0.01, 1.0 + amount * 0.01)
                if space == "local":
                    scaling = np.identity(4, dtype=np.float64)
                    if handle in {"x", "y", "z"}:
                        scaling[("x", "y", "z").index(handle), (
                            "x", "y", "z"
                        ).index(handle)] = factor
                    else:
                        scaling[:3, :3] *= factor
                    matrix = matrix @ scaling
                else:
                    if axis_world is None:
                        linear = np.identity(3) * factor
                    else:
                        axis = np.asarray(axis_world, dtype=np.float64)
                        linear = np.identity(3) + (factor - 1.0) * np.outer(
                            axis, axis
                        )
                    scaling = np.identity(4)
                    scaling[:3, :3] = linear
                    scaling[:3, 3] = pivot - linear @ pivot
                    matrix = np.linalg.pinv(parent_world) @ scaling @ world
            else:
                if mode == "trackball" or handle == "trackball":
                    axis_world = (
                        camera.up * float(delta.x())
                        + camera.right * float(delta.y())
                    )
                    magnitude = float(np.linalg.norm(axis_world))
                    if magnitude <= 1.0e-12:
                        continue
                    axis_world /= magnitude
                    angle = math.hypot(delta.x(), delta.y()) * 0.012
                else:
                    if axis_world is None or screen_direction is None:
                        continue
                    tangent = np.array([
                        -screen_direction[1], screen_direction[0]
                    ])
                    angle = float(np.dot(
                        np.array([delta.x(), delta.y()]), tangent
                    )) * 0.015
                cosine, sine = math.cos(angle), math.sin(angle)
                axis = np.asarray(axis_world, dtype=np.float64)
                axis /= max(float(np.linalg.norm(axis)), 1.0e-12)
                cross = np.array([
                    [0.0, -axis[2], axis[1]],
                    [axis[2], 0.0, -axis[0]],
                    [-axis[1], axis[0], 0.0],
                ])
                linear = (
                    np.identity(3) * cosine
                    + (1.0 - cosine) * np.outer(axis, axis)
                    + sine * cross
                )
                rotation = np.identity(4)
                rotation[:3, :3] = linear
                if space == "local" and mode != "trackball":
                    local_axis = np.zeros(3)
                    local_axis[("x", "y", "z").index(handle)] = 1.0
                    lx, ly, lz = local_axis
                    local_cross = np.array([
                        [0.0, -lz, ly], [lz, 0.0, -lx], [-ly, lx, 0.0]
                    ])
                    rotation[:3, :3] = (
                        np.identity(3) * cosine
                        + (1.0 - cosine) * np.outer(local_axis, local_axis)
                        + sine * local_cross
                    )
                    matrix = matrix @ rotation
                else:
                    rotation[:3, 3] = pivot - linear @ pivot
                    matrix = np.linalg.pinv(parent_world) @ rotation @ world
            flattened = matrix.reshape(16, order="F").tolist()
            if local_record is not None:
                local_record.setdefault("transform", {})["matrix"] = flattened
            else:
                overrides[entity_id]["matrix_local"] = flattened
        self._touch_frame(frame)

    def pointer_move(self, position: QPointF, modifiers) -> bool:
        del modifiers
        if not self.active or not self._drag_mode:
            return False
        delta = position - self._drag_last
        self._drag_last = QPointF(position)
        if self._drag_mode == "pan":
            self.navigation.pan = (
                self.navigation.pan[0] - delta.x() * 0.01,
                self.navigation.pan[1] + delta.y() * 0.01,
            )
        elif self._drag_mode == "orbit":
            self.navigation.yaw = (self.navigation.yaw + delta.x() * 0.35) % 360
            self.navigation.pitch = max(
                -89.0, min(89.0, self.navigation.pitch + delta.y() * 0.35)
            )
            if self.navigation.orientation is not None:
                import numpy as np
                from comic_editor.three_d.renderer.camera import CameraState

                camera = CameraState(
                    target=np.asarray(self.navigation.target, dtype=np.float64),
                    distance=self.navigation.distance,
                    orientation=np.asarray(
                        self.navigation.orientation, dtype=np.float64
                    ),
                )
                camera.orbit(float(delta.x()), float(delta.y()))
                self.navigation.orientation = tuple(
                    float(item) for item in camera.orientation
                )
        elif self._drag_mode == "select":
            if self.tool == ThreeDToolKind.SELECT_LASSO:
                if not self._selection_path or math.dist(
                    self._selection_path[-1].toTuple(), position.toTuple()
                ) >= 3:
                    self._selection_path.append(QPointF(position))
            elif len(self._selection_path) == 1:
                self._selection_path.append(QPointF(position))
            else:
                self._selection_path[-1] = QPointF(position)
        elif self._drag_mode == "transform":
            self._apply_transform_drag(delta)
        if self._drag_mode in {"pan", "orbit"}:
            self._store_navigation()
            self.request_render(interactive=True)
            self._refine_timer.start(140)
        elif self._drag_mode == "transform":
            self.request_render(interactive=True)
            self._refine_timer.start(140)
        return True

    @staticmethod
    def _point_in_polygon(point: QPointF, polygon: list[QPointF]) -> bool:
        inside = False
        previous = polygon[-1]
        for current in polygon:
            crosses = (current.y() > point.y()) != (previous.y() > point.y())
            if crosses:
                x = (
                    (previous.x() - current.x())
                    * (point.y() - current.y())
                    / (previous.y() - current.y())
                    + current.x()
                )
                if point.x() < x:
                    inside = not inside
            previous = current
        return inside

    def _selection_hits(self, release_position: QPointF) -> list[str]:
        scene = self._active_scene
        if scene is None:
            return []
        import numpy as np
        from comic_editor.three_d.renderer.projection import clip_planes
        from comic_editor.three_d.renderer.picking import (
            pick_scene, screen_to_world_ray,
        )

        low, high = scene.bounds()
        radius = max(0.001, float(np.linalg.norm(high - low)) * 0.5)
        near, far = clip_planes(scene.active_camera.distance, radius)
        context = scene.projection.context(self._target_size, near, far)
        path = [QPointF(item) for item in self._selection_path]
        drag_distance = math.dist(
            self._drag_origin.toTuple(), release_position.toTuple()
        )
        if drag_distance < 4.0 or len(path) < 2:
            ray = screen_to_world_ray(
                np.array(release_position.toTuple(), dtype=np.float64),
                scene.active_camera, scene.projection, context,
            )
            view = scene.active_camera.view_matrix()
            hits: list[tuple[float, float, str]] = []
            hit = pick_scene(scene, ray)
            if hit is not None:
                hits.append((hit.distance, 0.0, hit.node_id))
            for node in scene.nodes.values():
                if not node.visible or (
                    node.light_index is None and node.camera_index is None
                ):
                    continue
                camera_point = (view @ np.append(node.world_origin, 1.0))[:3]
                projected = scene.projection.project_cpu(camera_point, context)
                distance = math.dist(
                    projected.screen_px, release_position.toTuple()
                )
                if projected.lens_valid and distance <= 16.0:
                    hits.append((
                        projected.camera_depth, distance, node.node_id
                    ))
            hits.sort()
            return [hits[0][2]] if hits else []

        view = scene.active_camera.view_matrix()
        candidates: list[tuple[float, str]] = []
        if self.tool == ThreeDToolKind.SELECT_RECT:
            left, right = sorted((path[0].x(), release_position.x()))
            top, bottom = sorted((path[0].y(), release_position.y()))

            def contains(value: QPointF) -> bool:
                return left <= value.x() <= right and top <= value.y() <= bottom
        else:
            polygon = path + [QPointF(release_position)]

            def contains(value: QPointF) -> bool:
                return self._point_in_polygon(value, polygon)

        # Meshes are selected by visible pixels, not by their projected origin.
        # Keep this deterministic and bounded by rasterizing at no more than
        # roughly one megapixel, then scale the selection polygon accordingly.
        from comic_editor.three_d.renderer.id_buffer import (
            rasterize_scene_ids, select_region_ids,
        )

        target_width, target_height = self._target_size
        raster_scale = min(
            1.0,
            1024.0 / max(target_width, target_height, 1),
            math.sqrt(1_048_576.0 / max(target_width * target_height, 1)),
        )
        raster_size = (
            max(1, round(target_width * raster_scale)),
            max(1, round(target_height * raster_scale)),
        )
        if self.tool == ThreeDToolKind.SELECT_RECT:
            raster_polygon = np.asarray([
                [left, top], [right, top], [right, bottom], [left, bottom],
            ], dtype=np.float64)
        else:
            raster_polygon = np.asarray(
                [[item.x(), item.y()] for item in polygon], dtype=np.float64
            )
        raster_polygon *= raster_scale
        id_buffer = rasterize_scene_ids(scene, raster_size)
        mesh_ids = select_region_ids(
            id_buffer, raster_polygon, multi_select=True
        )
        id_by_node = {
            node_id: integer_id
            for integer_id, node_id in enumerate(id_buffer.id_to_node)
            if node_id
        }
        for node_id in mesh_ids:
            integer_id = id_by_node[node_id]
            depths = id_buffer.depth[id_buffer.ids == integer_id]
            candidates.append((
                float(np.min(depths, initial=np.inf)), node_id
            ))

        # Lights and cameras use selectable screen icons and therefore do not
        # participate in the triangle ID pass.
        for node in scene.nodes.values():
            if not node.visible or (
                node.light_index is None and node.camera_index is None
            ):
                continue
            camera_point = (view @ np.append(node.world_origin, 1.0))[:3]
            projected = scene.projection.project_cpu(camera_point, context)
            point = QPointF(
                float(projected.screen_px[0]), float(projected.screen_px[1])
            )
            if projected.lens_valid and contains(point):
                candidates.append((projected.camera_depth, node.node_id))
        candidates.sort()
        return list(dict.fromkeys(
            node_id for _distance, node_id in candidates
        ))

    def _surface_placement_matrix(self, position: QPointF, height: float = 1.0):
        """Return a surface-aligned local-entity matrix for a viewport point."""
        scene = self._active_scene
        if scene is None:
            return None
        import numpy as np
        from comic_editor.three_d.renderer.picking import (
            pick_scene, screen_to_world_ray,
        )
        from comic_editor.three_d.renderer.primitives import (
            surface_alignment_matrix,
        )
        from comic_editor.three_d.renderer.projection import clip_planes

        low, high = scene.bounds()
        radius = max(0.001, float(np.linalg.norm(high - low)) * 0.5)
        near, far = clip_planes(scene.active_camera.distance, radius)
        context = scene.projection.context(self._target_size, near, far)
        ray = screen_to_world_ray(
            np.asarray(position.toTuple(), dtype=np.float64),
            scene.active_camera, scene.projection, context,
        )
        hit = pick_scene(scene, ray)
        if hit is not None:
            normal = hit.normal
            point = hit.point + normal * max(0.0, float(height)) * 0.5
        elif ray.lens_valid and abs(float(ray.direction[1])) > 1.0e-9:
            distance = -float(ray.origin[1]) / float(ray.direction[1])
            point = ray.origin + ray.direction * max(0.0, distance)
            normal = np.array([0.0, 1.0, 0.0], dtype=np.float64)
            point += normal * max(0.0, float(height)) * 0.5
        else:
            point = scene.active_camera.target.copy()
            normal = np.array([0.0, 1.0, 0.0], dtype=np.float64)
            point[1] += max(0.0, float(height)) * 0.5
        return surface_alignment_matrix(
            point, normal, scene.active_camera.forward,
        )

    def pointer_release(
        self, position: QPointF, button, modifiers,
        hit_ids: list[str] | None = None,
    ) -> bool:
        if not self.active or button not in {Qt.LeftButton, Qt.MiddleButton}:
            return False
        mode, self._drag_mode = self._drag_mode, ""
        self._gizmo_handle = ""
        if not mode:
            return False
        if mode in {"pan", "orbit", "transform"}:
            if mode != "transform":
                self._store_navigation()
            frame = self._frame()
            if frame is not None:
                self._commit_frame_payload(
                    frame, self._before_navigation,
                    "Transform 3D object" if mode == "transform"
                    else "Navigate 3D camera",
                )
            self.request_render()
            self.interactionCommitted.emit(
                "Transform 3D object" if mode == "transform"
                else "Navigate 3D camera"
            )
        elif mode == "select":
            ids = list(
                self._selection_hits(position)
                if hit_ids is None else hit_ids
            )
            if not self.multi_select and ids:
                ids = ids[:1]
            operation = self._selection_operation(modifiers)
            if not self.multi_select or operation == "replace":
                self.selected_entity_ids = set(ids)
            elif operation == "add":
                self.selected_entity_ids.update(ids)
            else:
                self.selected_entity_ids.difference_update(ids)
            self.selectionChanged.emit(set(self.selected_entity_ids))
            self.request_render()
        elif mode == "draw_cube":
            matrix = self._surface_placement_matrix(position, 1.0)
            self.add_local_entity(
                "cube", size=[1.0, 1.0, 1.0],
                transform={"matrix": matrix.reshape(
                    16, order="F"
                ).tolist()} if matrix is not None else {
                    "translation": [0.0, 0.5, 0.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                },
            )
        elif mode == "draw_cylinder":
            matrix = self._surface_placement_matrix(position, 1.0)
            self.add_local_entity(
                "cylinder", radius=0.5, depth=1.0, vertices=32,
                transform={"matrix": matrix.reshape(
                    16, order="F"
                ).tolist()} if matrix is not None else {
                    "translation": [0.0, 0.5, 0.0],
                    "rotation": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                },
            )
        elif mode == "add_light":
            defaults = {
                "sun": {"color": "#FFFFFFFF", "energy": 3.0},
                "point": {"color": "#FFFFFFFF", "energy": 1000.0,
                          "range": 10.0},
                "rectangle": {"color": "#FFFFFFFF", "energy": 1000.0,
                              "size": [1.0, 1.0]},
                "spot": {"color": "#FFFFFFFF", "energy": 1000.0,
                         "range": 10.0, "spot_size": 45.0},
            }
            self.add_local_entity(
                self.pending_light_type, **defaults[self.pending_light_type]
            )
        self._selection_path.clear()
        return True

    def wheel(self, delta_y: int, modifiers, *, commit: bool = True) -> bool:
        del modifiers
        if not self.active or not self.editing_available:
            return False
        frame = self._frame()
        before = self._frame_payload(frame) if frame is not None else None
        factor = math.pow(1.0015, -int(delta_y))
        projection_mode = str(
            getattr(
                getattr(self._active_scene, "projection", None),
                "mode", "",
            )
        ).lower()
        if frame is not None and "orthographic" in projection_mode:
            settings = frame.presentation_overrides.setdefault(
                "renderer_settings", {}
            )
            current_height = float(settings.get(
                "ortho_height",
                getattr(
                    getattr(self._active_scene, "projection", None),
                    "ortho_height", 10.0,
                ),
            ))
            settings["ortho_height"] = max(
                0.001, min(1_000_000.0, current_height * factor)
            )
            self._touch_frame(frame)
        else:
            self.navigation.distance = max(
                0.02, min(100000.0, self.navigation.distance * factor)
            )
            self._store_navigation()
        frame = self._frame()
        if commit and frame is not None:
            self._commit_frame_payload(frame, before, "Dolly 3D camera")
        self.request_render(interactive=True)
        self._refine_timer.start(140)
        if commit:
            self.interactionCommitted.emit("Dolly 3D camera")
        return True

    def _store_navigation(self) -> None:
        frame = self._frame()
        if frame is None:
            return
        frame.presentation_overrides["camera_navigation"] = (
            self.navigation.to_dict()
        )
        self._touch_frame(frame)
