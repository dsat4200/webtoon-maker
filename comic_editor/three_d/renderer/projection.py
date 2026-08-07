"""Perspective, orthographic, and four central fisheye projections."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
import math
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


Float64Array = NDArray[np.float64]


class ProjectionMode(str, Enum):
    PERSPECTIVE = "Perspective"
    ORTHOGRAPHIC = "Orthographic"
    FISHEYE = "Fisheye"
    EQUIDISTANT_FISHEYE = "Fisheye"


class FisheyeMapping(str, Enum):
    EQUIDISTANT = "Equidistant"
    EQUISOLID_ANGLE = "Equisolid-angle"
    STEREOGRAPHIC = "Stereographic"
    ORTHOGRAPHIC = "Orthographic fisheye"


class FisheyeCrop(str, Enum):
    CIRCULAR = "Circular"
    FULL_FRAME = "Full Frame"


class CubemapQuality(str, Enum):
    ADAPTIVE = "Adaptive"
    PERFORMANCE = "Performance"
    BALANCED = "Balanced"
    HIGH = "High"


class ShaderProjectionMode(IntEnum):
    PERSPECTIVE = 0
    ORTHOGRAPHIC = 1
    FISHEYE = 2
    EQUIDISTANT_FISHEYE = 2


@dataclass(frozen=True, slots=True)
class ProjectionContext:
    viewport_size: tuple[int, int]
    near: float
    far: float

    def __post_init__(self) -> None:
        width, height = self.viewport_size
        if width <= 0 or height <= 0:
            raise ValueError("viewport dimensions must be positive")
        if self.near <= 0.0 or self.far <= self.near:
            raise ValueError("projection near/far distances are invalid")

    @property
    def width(self) -> int:
        return self.viewport_size[0]

    @property
    def height(self) -> int:
        return self.viewport_size[1]

    @property
    def aspect(self) -> float:
        return self.width / self.height


@dataclass(frozen=True, slots=True)
class ProjectedPoint:
    ndc: Float64Array
    screen_px: Float64Array
    camera_depth: float
    lens_valid: bool
    inside_viewport: bool


@dataclass(frozen=True, slots=True)
class CameraRay:
    origin: Float64Array
    direction: Float64Array
    lens_valid: bool


class Projection(Protocol):
    mode: ProjectionMode

    def project_cpu(self, camera_point: Float64Array, context: ProjectionContext, settings: "ProjectionSettings") -> ProjectedPoint: ...
    def screen_to_ray_cpu(self, screen_px: Float64Array, context: ProjectionContext, settings: "ProjectionSettings") -> CameraRay: ...
    def matrix(self, context: ProjectionContext, settings: "ProjectionSettings") -> Float64Array: ...


def _to_screen(ndc: Float64Array, context: ProjectionContext) -> Float64Array:
    return np.array(
        [(ndc[0] + 1.0) * 0.5 * context.width, (1.0 - ndc[1]) * 0.5 * context.height],
        dtype=np.float64,
    )


def _screen_to_ndc(screen_px: Float64Array, context: ProjectionContext) -> Float64Array:
    x, y = np.asarray(screen_px, dtype=np.float64)
    return np.array([2.0 * x / context.width - 1.0, 1.0 - 2.0 * y / context.height])


def _inside_screen(screen_px: Float64Array, context: ProjectionContext) -> bool:
    x, y = np.asarray(screen_px, dtype=np.float64)
    return bool(0.0 <= x <= context.width and 0.0 <= y <= context.height)


def _normalized(value: Float64Array) -> Float64Array:
    length = float(np.linalg.norm(value))
    if length <= 1.0e-12:
        return np.zeros_like(value, dtype=np.float64)
    return np.asarray(value, dtype=np.float64) / length


def _invalid_ray() -> CameraRay:
    return CameraRay(np.zeros(3), np.array([0.0, 0.0, -1.0]), False)


def _blender_ndc_shift(
    shift_x: float,
    shift_y: float,
    sensor_fit: str,
    aspect: float,
) -> Float64Array:
    if (
        not math.isfinite(aspect) or aspect <= 0.0
        or not math.isfinite(shift_x) or not math.isfinite(shift_y)
    ):
        raise ValueError("projection aspect and camera shifts must be finite")
    fit = str(sensor_fit).upper()
    if fit not in ("AUTO", "HORIZONTAL", "VERTICAL"):
        raise ValueError("camera sensor fit must be AUTO, HORIZONTAL, or VERTICAL")
    horizontal = fit == "HORIZONTAL" or (fit == "AUTO" and aspect >= 1.0)
    if horizontal:
        return np.array([2.0 * shift_x, 2.0 * shift_y * aspect], dtype=np.float64)
    return np.array([2.0 * shift_x / aspect, 2.0 * shift_y], dtype=np.float64)


def fisheye_radius(theta: float, mapping: FisheyeMapping) -> float:
    if mapping is FisheyeMapping.EQUIDISTANT:
        return theta
    if mapping is FisheyeMapping.EQUISOLID_ANGLE:
        return 2.0 * math.sin(theta * 0.5)
    if mapping is FisheyeMapping.STEREOGRAPHIC:
        return 2.0 * math.tan(theta * 0.5)
    return math.sin(theta)


def fisheye_theta(radius: float, mapping: FisheyeMapping) -> float:
    if mapping is FisheyeMapping.EQUIDISTANT:
        return radius
    if mapping is FisheyeMapping.EQUISOLID_ANGLE:
        return 2.0 * math.asin(float(np.clip(radius * 0.5, -1.0, 1.0)))
    if mapping is FisheyeMapping.STEREOGRAPHIC:
        return 2.0 * math.atan(radius * 0.5)
    return math.asin(float(np.clip(radius, -1.0, 1.0)))


def _matrix_projection(point: Float64Array, matrix: Float64Array, context: ProjectionContext) -> ProjectedPoint:
    point = np.asarray(point, dtype=np.float64)
    depth = -float(point[2])
    clip = matrix @ np.append(point, 1.0)
    valid = context.near <= depth <= context.far and abs(float(clip[3])) > 1.0e-12
    ndc = clip[:3] / clip[3] if valid else np.zeros(3, dtype=np.float64)
    inside = valid and bool(np.all(ndc >= -1.0 - 1e-10) and np.all(ndc <= 1.0 + 1e-10))
    return ProjectedPoint(ndc, _to_screen(ndc, context), depth, valid, inside)


class PerspectiveProjection:
    mode = ProjectionMode.PERSPECTIVE
    shader_mode = ShaderProjectionMode.PERSPECTIVE
    ray_remapped = False

    def project_cpu(self, camera_point: Float64Array, context: ProjectionContext, settings: "ProjectionSettings") -> ProjectedPoint:
        return _matrix_projection(camera_point, self.matrix(context, settings), context)

    def screen_to_ray_cpu(self, screen_px: Float64Array, context: ProjectionContext, settings: "ProjectionSettings") -> CameraRay:
        if not _inside_screen(screen_px, context):
            return _invalid_ray()
        ndc = _screen_to_ndc(screen_px, context) + settings.ndc_shift(context.aspect)
        tangent = math.tan(math.radians(settings.vertical_fov_deg) * 0.5)
        direction = _normalized(np.array([ndc[0] * context.aspect * tangent, ndc[1] * tangent, -1.0]))
        return CameraRay(np.zeros(3), direction, True)

    def matrix(self, context: ProjectionContext, settings: "ProjectionSettings") -> Float64Array:
        return perspective_matrix(
            settings.vertical_fov_deg,
            context.aspect,
            context.near,
            context.far,
            shift_x=settings.shift_x,
            shift_y=settings.shift_y,
            sensor_fit=settings.sensor_fit,
        )


class OrthographicProjection:
    mode = ProjectionMode.ORTHOGRAPHIC
    shader_mode = ShaderProjectionMode.ORTHOGRAPHIC
    ray_remapped = False

    def project_cpu(self, camera_point: Float64Array, context: ProjectionContext, settings: "ProjectionSettings") -> ProjectedPoint:
        return _matrix_projection(camera_point, self.matrix(context, settings), context)

    def screen_to_ray_cpu(self, screen_px: Float64Array, context: ProjectionContext, settings: "ProjectionSettings") -> CameraRay:
        if not _inside_screen(screen_px, context):
            return _invalid_ray()
        ndc = _screen_to_ndc(screen_px, context) + settings.ndc_shift(context.aspect)
        half_height = settings.ortho_height * 0.5
        origin = np.array([ndc[0] * half_height * context.aspect, ndc[1] * half_height, 0.0])
        return CameraRay(origin, np.array([0.0, 0.0, -1.0]), True)

    def matrix(self, context: ProjectionContext, settings: "ProjectionSettings") -> Float64Array:
        return orthographic_matrix(
            settings.ortho_height,
            context.aspect,
            context.near,
            context.far,
            shift_x=settings.shift_x,
            shift_y=settings.shift_y,
            sensor_fit=settings.sensor_fit,
        )


class FisheyeProjection:
    mode = ProjectionMode.FISHEYE
    shader_mode = ShaderProjectionMode.FISHEYE
    ray_remapped = True

    def project_cpu(self, camera_point: Float64Array, context: ProjectionContext, settings: "ProjectionSettings") -> ProjectedPoint:
        point = np.asarray(camera_point, dtype=np.float64)
        radial_depth = float(np.linalg.norm(point))
        if radial_depth <= 1.0e-12:
            ndc = np.zeros(3)
            return ProjectedPoint(ndc, _to_screen(ndc, context), 0.0, False, False)
        direction = point / radial_depth
        theta = math.acos(float(np.clip(-direction[2], -1.0, 1.0)))
        axis_length = float(np.linalg.norm(direction[:2]))
        rear_singular = axis_length <= 1.0e-12 and theta > 1.0e-8
        screen_direction = np.array([1.0, 0.0]) if axis_length <= 1e-12 else direction[:2] / axis_length
        half_fov = math.radians(settings.effective_fisheye_fov_deg) * 0.5
        normalized_radius = fisheye_radius(theta, settings.fisheye_mapping) / max(
            fisheye_radius(half_fov, settings.fisheye_mapping), 1.0e-12
        )
        reference = min(context.width, context.height) if settings.fisheye_crop is FisheyeCrop.CIRCULAR else math.hypot(context.width, context.height)
        ndc_xy = screen_direction * normalized_radius * np.array([reference / context.width, reference / context.height])
        ndc = np.array([ndc_xy[0], ndc_xy[1], 2.0 * (radial_depth - context.near) / (context.far - context.near) - 1.0])
        valid = not rear_singular and context.near <= radial_depth <= context.far and theta <= half_fov + 1e-10
        inside = valid and bool(np.all(ndc >= -1.0 - 1e-10) and np.all(ndc <= 1.0 + 1e-10))
        return ProjectedPoint(ndc, _to_screen(ndc, context), radial_depth, valid, inside)

    def screen_to_ray_cpu(self, screen_px: Float64Array, context: ProjectionContext, settings: "ProjectionSettings") -> CameraRay:
        if not _inside_screen(screen_px, context):
            return _invalid_ray()
        point = np.asarray(screen_px, dtype=np.float64)
        centered = np.array([point[0] - context.width * 0.5, context.height * 0.5 - point[1]])
        reference = min(context.width, context.height) if settings.fisheye_crop is FisheyeCrop.CIRCULAR else math.hypot(context.width, context.height)
        normalized_radius = 2.0 * float(np.linalg.norm(centered)) / reference
        if normalized_radius > 1.0 + 1e-10:
            return _invalid_ray()
        half_fov = math.radians(settings.effective_fisheye_fov_deg) * 0.5
        theta = fisheye_theta(normalized_radius * fisheye_radius(half_fov, settings.fisheye_mapping), settings.fisheye_mapping)
        if theta >= math.pi - 1e-10:
            return _invalid_ray()
        axis = float(np.linalg.norm(centered))
        direction_2d = np.array([1.0, 0.0]) if axis <= 1e-12 else centered / axis
        direction = np.array([math.sin(theta) * direction_2d[0], math.sin(theta) * direction_2d[1], -math.cos(theta)])
        return CameraRay(np.zeros(3), direction, True)

    def matrix(self, context: ProjectionContext, settings: "ProjectionSettings") -> Float64Array:
        return np.identity(4, dtype=np.float64)


EquidistantFisheyeProjection = FisheyeProjection


_PROJECTIONS: dict[ProjectionMode, Projection] = {
    ProjectionMode.PERSPECTIVE: PerspectiveProjection(),
    ProjectionMode.ORTHOGRAPHIC: OrthographicProjection(),
    ProjectionMode.FISHEYE: FisheyeProjection(),
}


@dataclass(slots=True)
class ProjectionSettings:
    mode: ProjectionMode = ProjectionMode.PERSPECTIVE
    vertical_fov_deg: float = 50.0
    ortho_height: float = 10.0
    fisheye_fov_deg: float = 180.0
    fisheye_crop: FisheyeCrop = FisheyeCrop.CIRCULAR
    fisheye_mapping: FisheyeMapping = FisheyeMapping.EQUIDISTANT
    cubemap_quality: CubemapQuality = CubemapQuality.PERFORMANCE
    shift_x: float = 0.0
    shift_y: float = 0.0
    sensor_fit: str = "AUTO"

    def __post_init__(self) -> None:
        self.mode = ProjectionMode(self.mode)
        self.fisheye_crop = FisheyeCrop(self.fisheye_crop)
        self.fisheye_mapping = FisheyeMapping(self.fisheye_mapping)
        self.cubemap_quality = CubemapQuality(self.cubemap_quality)
        self.sensor_fit = str(self.sensor_fit).upper()
        if not 1.0 <= self.vertical_fov_deg < 179.0:
            raise ValueError("vertical field of view must be between 1 and 179 degrees")
        if self.ortho_height <= 0.0:
            raise ValueError("orthographic height must be positive")
        if not math.isfinite(self.shift_x) or not math.isfinite(self.shift_y):
            raise ValueError("camera shifts must be finite")
        if self.sensor_fit not in ("AUTO", "HORIZONTAL", "VERTICAL"):
            raise ValueError("camera sensor fit must be AUTO, HORIZONTAL, or VERTICAL")

    @property
    def active_projection(self) -> Projection:
        return _PROJECTIONS[self.mode]

    @property
    def shader_mode(self) -> ShaderProjectionMode:
        return self.active_projection.shader_mode

    @property
    def ray_remapped(self) -> bool:
        return self.active_projection.ray_remapped

    @property
    def effective_fisheye_fov_deg(self) -> float:
        maximum = 180.0 if self.fisheye_mapping is FisheyeMapping.ORTHOGRAPHIC else 220.0
        return float(np.clip(self.fisheye_fov_deg, 30.0, maximum))

    def switch_mode(self, mode: ProjectionMode, camera_distance: float) -> None:
        mode = ProjectionMode(mode)
        if mode is self.mode:
            return
        if mode is ProjectionMode.ORTHOGRAPHIC:
            self.ortho_height = max(2.0 * camera_distance * math.tan(math.radians(self.vertical_fov_deg) * 0.5), 1e-6)
        self.mode = mode

    def context(self, viewport_size: tuple[int, int], near: float, far: float) -> ProjectionContext:
        return ProjectionContext(viewport_size, near, far)

    def ndc_shift(self, aspect: float) -> Float64Array:
        """Return Blender lens shift in normalized-device coordinates.

        Blender defines both camera shifts against the fitted sensor
        dimension.  For a horizontal fit that dimension is the image width;
        for a vertical fit it is the image height.  AUTO selects the larger
        viewport dimension, matching Blender's square-pixel camera fit.
        """
        return _blender_ndc_shift(
            self.shift_x, self.shift_y, self.sensor_fit, aspect
        )

    def project_cpu(self, camera_point: Float64Array, context: ProjectionContext) -> ProjectedPoint:
        return self.active_projection.project_cpu(camera_point, context, self)

    def project_many_cpu(self, camera_points: Float64Array, context: ProjectionContext) -> tuple[ProjectedPoint, ...]:
        points = np.asarray(camera_points, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 3:
            raise ValueError("camera_points must have shape (N, 3)")
        return tuple(self.project_cpu(point, context) for point in points)

    def screen_to_ray_cpu(self, screen_px: Float64Array, context: ProjectionContext) -> CameraRay:
        return self.active_projection.screen_to_ray_cpu(screen_px, context, self)

    def matrix(self, aspect: float, near: float, far: float) -> Float64Array:
        context = ProjectionContext((max(1, int(round(aspect * 1000))), 1000), near, far)
        return self.active_projection.matrix(context, self)


def clip_planes(distance: float, scene_radius: float) -> tuple[float, float]:
    radius = max(scene_radius, 1.0e-5)
    near = max(radius * 0.001, distance - 1.5 * radius)
    far = max(distance + 1.5 * radius, near + radius * 0.01)
    return near, far


def perspective_matrix(
    vertical_fov_deg: float,
    aspect: float,
    near: float,
    far: float,
    *,
    shift_x: float = 0.0,
    shift_y: float = 0.0,
    sensor_fit: str = "AUTO",
) -> Float64Array:
    if aspect <= 0.0 or near <= 0.0 or far <= near:
        raise ValueError("invalid perspective projection parameters")
    f = 1.0 / math.tan(math.radians(vertical_fov_deg) * 0.5)
    matrix = np.zeros((4, 4), dtype=np.float64)
    matrix[0, 0], matrix[1, 1] = f / aspect, f
    shift = _blender_ndc_shift(shift_x, shift_y, sensor_fit, aspect)
    matrix[0, 2], matrix[1, 2] = shift
    matrix[2, 2] = (far + near) / (near - far)
    matrix[2, 3] = 2.0 * far * near / (near - far)
    matrix[3, 2] = -1.0
    return matrix


def orthographic_matrix(
    visible_height: float,
    aspect: float,
    near: float,
    far: float,
    *,
    shift_x: float = 0.0,
    shift_y: float = 0.0,
    sensor_fit: str = "AUTO",
) -> Float64Array:
    if visible_height <= 0.0 or aspect <= 0.0 or near <= 0.0 or far <= near:
        raise ValueError("invalid orthographic projection parameters")
    half_height = visible_height * 0.5
    matrix = np.identity(4, dtype=np.float64)
    matrix[0, 0] = 1.0 / (half_height * aspect)
    matrix[1, 1] = 1.0 / half_height
    shift = _blender_ndc_shift(shift_x, shift_y, sensor_fit, aspect)
    matrix[0, 3], matrix[1, 3] = -shift
    matrix[2, 2] = -2.0 / (far - near)
    matrix[2, 3] = -(far + near) / (far - near)
    return matrix


# Kept public so GPU backends can use the exact CPU mapping constants.
FISHEYE_GLSL = """
float fisheye_radius(float theta, int mapping) {
    if (mapping == 1) return 2.0 * sin(0.5 * theta);
    if (mapping == 2) return 2.0 * tan(0.5 * theta);
    if (mapping == 3) return sin(theta);
    return theta;
}
"""
