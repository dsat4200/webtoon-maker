"""Working viewport frame, projection crop, and diagnostic overlay."""
from __future__ import annotations

import math
from typing import Any

import bpy
from mathutils import Matrix, Vector


DEFAULT_FRAME = (0.0, 0.0, 1.0, 1.0)
MIN_FRAME_PIXELS = 32
MAX_AXIS = 4096
MAX_PIXELS = 16_777_216

_bound_window = 0
_bound_area = 0
_draw_handle: object | None = None
_frame_edit_active = False
_resolution_assignment_active = False


def bind(context: object) -> None:
    global _bound_window, _bound_area
    window = getattr(context, "window", None)
    area = getattr(context, "area", None)
    if window is not None and area is not None and area.type == "VIEW_3D":
        _bound_window = int(window.as_pointer())
        _bound_area = int(area.as_pointer())


def find_view3d() -> tuple[object | None, object | None, object | None, object | None]:
    global _bound_window, _bound_area
    fallback = (None, None, None, None)
    manager = getattr(bpy.context, "window_manager", None)
    for window in getattr(manager, "windows", ()):
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next(
                (item for item in area.regions if item.type == "WINDOW"), None
            )
            if region is None:
                continue
            candidate = (window, area, area.spaces.active, region)
            if fallback[0] is None:
                fallback = candidate
            if (
                int(window.as_pointer()) == _bound_window
                and int(area.as_pointer()) == _bound_area
            ):
                return candidate
    if fallback[0] is not None:
        _bound_window = int(fallback[0].as_pointer())
        _bound_area = int(fallback[1].as_pointer())
    return fallback


def is_bound_context(context: object) -> bool:
    window = getattr(context, "window", None)
    area = getattr(context, "area", None)
    if window is None or area is None:
        return False
    if not _bound_window or not _bound_area:
        bind(context)
    return (
        int(window.as_pointer()) == _bound_window
        and int(area.as_pointer()) == _bound_area
    )


def camera_view_active(scene: object | None = None) -> bool:
    _window, _area, space, region = find_view3d()
    scene = scene or getattr(bpy.context, "scene", None)
    return bool(
        scene is not None and getattr(scene, "camera", None) is not None
        and space is not None and region is not None
        and space.region_3d.view_perspective == "CAMERA"
    )


def set_frame_edit_active(active: bool) -> None:
    global _frame_edit_active
    _frame_edit_active = bool(active)
    tag_redraw()


def frame_bounds(view: object) -> tuple[float, float, float, float]:
    values = (
        float(getattr(view, "frame_min_x", DEFAULT_FRAME[0])),
        float(getattr(view, "frame_min_y", DEFAULT_FRAME[1])),
        float(getattr(view, "frame_max_x", DEFAULT_FRAME[2])),
        float(getattr(view, "frame_max_y", DEFAULT_FRAME[3])),
    )
    left, bottom, right, top = values
    if not all(math.isfinite(item) for item in values):
        return DEFAULT_FRAME
    left, right = sorted((left, right))
    bottom, top = sorted((bottom, top))
    if right - left <= 1e-6 or top - bottom <= 1e-6:
        return DEFAULT_FRAME
    return left, bottom, right, top


def set_frame_bounds(view: object, values: object) -> None:
    try:
        left, bottom, right, top = (float(item) for item in values)
    except (TypeError, ValueError):
        left, bottom, right, top = DEFAULT_FRAME
    left, right = sorted((left, right))
    bottom, top = sorted((bottom, top))
    if right - left <= 1e-6 or top - bottom <= 1e-6:
        left, bottom, right, top = DEFAULT_FRAME
    view.frame_min_x, view.frame_min_y = left, bottom
    view.frame_max_x, view.frame_max_y = right, top


def camera_gate_aspect(scene: object) -> float:
    render = scene.render
    width = max(1.0, float(render.resolution_x) * float(render.pixel_aspect_x))
    height = max(1.0, float(render.resolution_y) * float(render.pixel_aspect_y))
    return width / height


def derive_resolution(
    width: object, bounds: tuple[float, float, float, float], scene: object,
) -> tuple[int, int]:
    width = max(64, min(MAX_AXIS, int(width)))
    left, bottom, right, top = bounds
    aspect = (
        camera_gate_aspect(scene)
        * max(right - left, 1e-9) / max(top - bottom, 1e-9)
    )
    height = max(1, round(width / max(aspect, 1e-9)))
    if height < 64:
        height = 64
        width = max(64, round(height * aspect))
    scale = min(
        1.0,
        MAX_AXIS / max(width, 1),
        MAX_AXIS / max(height, 1),
        math.sqrt(MAX_PIXELS / max(width * height, 1)),
    )
    if scale < 1.0:
        width = max(64, int(width * scale))
        height = max(64, int(height * scale))
    return width, height


def resolution_assignment_active() -> bool:
    return _resolution_assignment_active


def set_working_resolution(view: object, width: int, height: int) -> None:
    global _resolution_assignment_active
    _resolution_assignment_active = True
    try:
        view.width = int(width)
        view.height = int(height)
    finally:
        _resolution_assignment_active = False


def update_working_resolution(
    view: object, scene: object | None = None,
) -> tuple[int, int]:
    scene = scene or getattr(bpy.context, "scene", None)
    if scene is None:
        return int(view.width), int(view.height)
    width, height = derive_resolution(
        view.width, frame_bounds(view), scene
    )
    set_working_resolution(view, width, height)
    return width, height


def capture_viewport() -> dict[str, Any]:
    _window, _area, space, _region = find_view3d()
    if space is None:
        return {}
    rv3d = space.region_3d
    return {
        "view_perspective": str(rv3d.view_perspective),
        "view_location": [float(value) for value in rv3d.view_location],
        "view_rotation": [float(value) for value in rv3d.view_rotation],
        "view_distance": float(rv3d.view_distance),
        "view_camera_zoom": float(rv3d.view_camera_zoom),
        "view_camera_offset": [float(value) for value in rv3d.view_camera_offset],
        "lens": float(space.lens),
    }


def apply_viewport(values: object) -> None:
    if not isinstance(values, dict):
        return
    _window, area, space, _region = find_view3d()
    if space is None:
        return
    rv3d = space.region_3d
    assignments = (
        ("view_location", values.get("view_location")),
        ("view_rotation", values.get("view_rotation")),
        ("view_camera_offset", values.get("view_camera_offset")),
    )
    for name, value in assignments:
        if isinstance(value, list):
            try:
                setattr(rv3d, name, tuple(float(item) for item in value))
            except (TypeError, ValueError):
                pass
    for owner, name, minimum, maximum in (
        (rv3d, "view_distance", 1e-6, 1e12),
        (rv3d, "view_camera_zoom", -30.0, 600.0),
        (space, "lens", 1.0, 250.0),
    ):
        try:
            setattr(
                owner, name,
                max(minimum, min(maximum, float(values.get(name, getattr(owner, name))))),
            )
        except (TypeError, ValueError):
            pass
    perspective = str(values.get("view_perspective", ""))
    if perspective in {"PERSP", "ORTHO", "CAMERA"}:
        try:
            rv3d.view_perspective = perspective
        except TypeError:
            pass
    if area is not None:
        area.tag_redraw()


def camera_region_bounds(
    scene: object,
) -> tuple[float, float, float, float] | None:
    _window, _area, space, region = find_view3d()
    camera = getattr(scene, "camera", None)
    if space is None or region is None or camera is None:
        return None
    try:
        from bpy_extras.view3d_utils import location_3d_to_region_2d

        points = []
        for corner in camera.data.view_frame(scene=scene):
            world = camera.matrix_world @ Vector(corner)
            point = location_3d_to_region_2d(region, space.region_3d, world)
            if point is not None:
                points.append(point)
        if len(points) != 4:
            return None
        left = min(point.x for point in points)
        right = max(point.x for point in points)
        bottom = min(point.y for point in points)
        top = max(point.y for point in points)
        if (
            right - left >= 1e-6 and top - bottom >= 1e-6
        ):
            return left, bottom, right, top
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return None


def default_frame(_scene: object) -> tuple[float, float, float, float]:
    return DEFAULT_FRAME


def frame_screen_bounds(
    scene: object, bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    gate = camera_region_bounds(scene)
    if gate is None:
        return None
    gate_left, gate_bottom, gate_right, gate_top = gate
    gate_width = gate_right - gate_left
    gate_height = gate_top - gate_bottom
    left, bottom, right, top = bounds
    return (
        gate_left + left * gate_width,
        gate_bottom + bottom * gate_height,
        gate_left + right * gate_width,
        gate_bottom + top * gate_height,
    )


def screen_to_camera_bounds(
    scene: object, values: object,
) -> tuple[float, float, float, float] | None:
    gate = camera_region_bounds(scene)
    if gate is None:
        return None
    try:
        left, bottom, right, top = (float(item) for item in values)
    except (TypeError, ValueError):
        return None
    gate_left, gate_bottom, gate_right, gate_top = gate
    gate_width = gate_right - gate_left
    gate_height = gate_top - gate_bottom
    if abs(gate_width) <= 1e-9 or abs(gate_height) <= 1e-9:
        return None
    left, right = sorted((left, right))
    bottom, top = sorted((bottom, top))
    return (
        (left - gate_left) / gate_width,
        (bottom - gate_bottom) / gate_height,
        (right - gate_left) / gate_width,
        (top - gate_bottom) / gate_height,
    )


def cropped_projection(
    projection: Matrix, bounds: tuple[float, float, float, float],
) -> Matrix:
    left, bottom, right, top = bounds
    scale_x = 1.0 / max(right - left, 1e-9)
    scale_y = 1.0 / max(top - bottom, 1e-9)
    translate_x = -(left + right - 1.0) * scale_x
    translate_y = -(bottom + top - 1.0) * scale_y
    crop = Matrix((
        (scale_x, 0.0, 0.0, translate_x),
        (0.0, scale_y, 0.0, translate_y),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    ))
    return crop @ projection


def render_matrices(
    scene: object, view_layer: object,
    bounds: tuple[float, float, float, float],
) -> tuple[Matrix, Matrix]:
    """Build camera matrices independent of viewport navigation."""
    camera = getattr(scene, "camera", None)
    if camera is None or getattr(camera, "type", "") != "CAMERA":
        raise RuntimeError("The Comic View has no active camera")
    render = scene.render
    projection = camera.calc_matrix_camera(
        view_layer.depsgraph,
        x=max(1, int(render.resolution_x)),
        y=max(1, int(render.resolution_y)),
        scale_x=max(1e-9, float(render.pixel_aspect_x)),
        scale_y=max(1e-9, float(render.pixel_aspect_y)),
    )
    return (
        camera.matrix_world.inverted_safe(),
        cropped_projection(projection, bounds),
    )


def enter_camera_view() -> None:
    _window, area, space, _region = find_view3d()
    if space is None or getattr(bpy.context.scene, "camera", None) is None:
        return
    try:
        space.region_3d.view_perspective = "CAMERA"
    except (AttributeError, TypeError):
        return
    if area is not None:
        area.tag_redraw()


def local_view_enabled() -> bool:
    _window, _area, space, _region = find_view3d()
    return bool(space is not None and space.local_view is not None)


def apply_local_view(
    objects: dict[str, object], enabled: bool, members: set[str],
) -> str | None:
    """Restore the bound viewport's local-view mode and membership."""
    window, area, space, region = find_view3d()
    if space is None or area is None or region is None or window is None:
        return (
            "Open a 3D View to restore Local View" if enabled else None
        )
    current = space.local_view is not None
    if enabled and not members:
        enabled = False
    view_layer = getattr(bpy.context, "view_layer", None)
    active = getattr(getattr(view_layer, "objects", None), "active", None)
    selection = {
        identifier: bool(obj.select_get())
        for identifier, obj in objects.items()
    }
    try:
        if current:
            with bpy.context.temp_override(
                window=window, area=area, region=region, space_data=space,
            ):
                bpy.ops.view3d.localview(frame_selected=False)
            current = space.local_view is not None
        if enabled:
            for identifier, obj in objects.items():
                obj.select_set(identifier in members)
            first = next(
                (obj for identifier, obj in objects.items()
                 if identifier in members), None
            )
            if first is None:
                return "Saved Local View contains no available objects"
            if view_layer is not None:
                view_layer.objects.active = first
            selected_objects = [
                obj for identifier, obj in objects.items()
                if identifier in members
            ]
            with bpy.context.temp_override(
                window=window, area=area, region=region,
                space_data=space, active_object=first, object=first,
                selected_objects=selected_objects,
                selected_editable_objects=selected_objects,
            ):
                bpy.ops.view3d.localview(frame_selected=False)
            current = space.local_view is not None
        if enabled and not current:
            return "Could not enter the saved Local View"
    except (AttributeError, RuntimeError, TypeError) as error:
        return f"Could not restore Local View: {error}"
    finally:
        for identifier, obj in objects.items():
            try:
                obj.select_set(selection.get(identifier, False))
            except (AttributeError, RuntimeError):
                pass
        if view_layer is not None and active is not None:
            try:
                view_layer.objects.active = active
            except (AttributeError, RuntimeError):
                pass
    return None


def tag_redraw() -> None:
    manager = getattr(bpy.context, "window_manager", None)
    for window in getattr(manager, "windows", ()):
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _draw_overlay() -> None:
    context = bpy.context
    if context.area is None or context.area.type != "VIEW_3D":
        return
    if not is_bound_context(context):
        return
    scene = context.scene
    settings = getattr(scene, "webtoon_comic_settings", None)
    if not camera_view_active(scene):
        return
    if (
        not _frame_edit_active
        and not bool(getattr(settings, "show_stream_frame_overlay", True))
    ):
        return
    views = getattr(scene, "webtoon_comic_views", ())
    index = int(getattr(settings, "active_index", -1))
    if not 0 <= index < len(views):
        return
    view = views[index]
    screen_bounds = frame_screen_bounds(scene, frame_bounds(view))
    if screen_bounds is None:
        return
    left, bottom, right, top = screen_bounds
    points = [
        (left, bottom), (right, bottom), (right, top), (left, top),
        (left, bottom),
    ]
    tick = 10.0
    x0, y0 = points[0]
    x1, y1 = points[2]
    ticks = [
        (x0, y0), (x0 + tick, y0), (x0, y0), (x0, y0 + tick),
        (x1, y0), (x1 - tick, y0), (x1, y0), (x1, y0 + tick),
        (x1, y1), (x1 - tick, y1), (x1, y1), (x1, y1 - tick),
        (x0, y1), (x0 + tick, y1), (x0, y1), (x0, y1 - tick),
    ]
    try:
        import blf
        import gpu
        from gpu_extras.batch import batch_for_shader

        shader = gpu.shader.from_builtin("UNIFORM_COLOR")
        batch = batch_for_shader(shader, "LINE_STRIP", {"pos": points})
        tick_batch = batch_for_shader(shader, "LINES", {"pos": ticks})
        gpu.state.blend_set("ALPHA")
        gpu.state.line_width_set(2.0)
        shader.bind()
        shader.uniform_float("color", (1.0, 0.45, 0.06, 0.95))
        batch.draw(shader)
        tick_batch.draw(shader)
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set("NONE")
        blf.position(0, points[3][0] + 6, points[3][1] + 6, 0)
        blf.size(0, 12)
        blf.color(0, 1.0, 0.55, 0.15, 1.0)
        blf.draw(0, f"{view.name}  {int(view.width)} x {int(view.height)}")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return


def register_overlay() -> None:
    global _draw_handle
    if _draw_handle is None and not bpy.app.background:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_overlay, (), "WINDOW", "POST_PIXEL"
        )


def unregister_overlay() -> None:
    global _draw_handle
    if _draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        except (ReferenceError, RuntimeError):
            pass
        _draw_handle = None
