"""Dedicated-context ModernGL renderer returning premultiplied QImages.

The backend contains no widget and never assumes the Qt GUI thread owns its
OpenGL context. A caller should construct and use an instance on one worker
thread (``RenderService`` does exactly that).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any

import numpy as np
from PySide6.QtGui import QImage

from .camera import normalize
from .cubemap import CubeFace, cubemap_face_size, face_gutter_tangent, face_view_matrix, required_cube_faces
from .grid import axis_lines, build_floor_grid, build_volume_grid
from .materials import DrawingMaterial, SurfaceMaterial, ToonRamp
from .mesh import AlphaMode, MeshPrimitive, SourceMaterial, compute_vertex_normals
from .primitives import floor_mesh
from .projection import FisheyeCrop, FisheyeMapping, ProjectionMode, clip_planes, perspective_matrix
from .scene import LightType, SceneData, SceneLight, SceneNode


class RendererUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RenderOptions:
    interactive: bool = False
    antialiasing: bool = False
    transparent: bool = True
    draw_floor: bool | None = None
    draw_grid: bool | None = None
    draw_volume_grid: bool | None = None
    draw_axes: bool | None = None
    selected_node_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RenderMetrics:
    elapsed_ms: float
    draw_calls: int
    triangles: int
    used_cubemap: bool
    shadow_casters: int
    draw_submit_ms: float = 0.0
    readback_ms: float = 0.0


_MESH_VERTEX_SHADER = """#version 330
in vec3 in_position;
in vec3 in_normal;
in vec4 in_color;
in vec2 in_uv;
uniform mat4 u_model;
uniform mat4 u_view_projection;
uniform mat3 u_normal_matrix;
uniform float u_outline_width;
out vec3 v_world_position;
out vec3 v_world_normal;
out vec4 v_color;
out vec2 v_uv;
void main() {
    vec3 position = in_position + in_normal * u_outline_width;
    vec4 world = u_model * vec4(position, 1.0);
    v_world_position = world.xyz;
    v_world_normal = normalize(u_normal_matrix * in_normal);
    v_color = in_color;
    v_uv = in_uv;
    gl_Position = u_view_projection * world;
}
"""

_MESH_FRAGMENT_SHADER = """#version 330
in vec3 v_world_position;
in vec3 v_world_normal;
in vec4 v_color;
in vec2 v_uv;
out vec4 frag_color;
uniform int u_outline_pass;
uniform vec4 u_outline_color;
uniform int u_surface;
uniform vec4 u_base_color;
uniform int u_toon_count;
uniform float u_toon_positions[8];
uniform vec3 u_toon_colors[8];
uniform int u_use_vertex_color;
uniform int u_use_texture;
uniform int u_double_sided;
uniform sampler2D u_base_texture;
uniform int u_alpha_mode;
uniform float u_alpha_cutoff;
uniform vec3 u_ambient;
uniform int u_light_count;
uniform int u_light_shadow_index[8];
uniform int u_light_type[8];
uniform vec3 u_light_position[8];
uniform vec3 u_light_direction[8];
uniform vec3 u_light_color[8];
uniform float u_light_energy[8];
uniform float u_light_range[8];
uniform vec2 u_light_spot_cos[8];
uniform int u_shadow_count;
uniform mat4 u_shadow_matrix[4];
uniform sampler2D u_shadow0;
uniform sampler2D u_shadow1;
uniform sampler2D u_shadow2;
uniform sampler2D u_shadow3;
uniform float u_shadow_bias;
uniform float u_shadow_opacity;

float shadow_sample(int index, vec3 world, vec3 normal, vec3 light_direction) {
    vec4 clip = u_shadow_matrix[index] * vec4(world, 1.0);
    vec3 projected = clip.xyz / max(clip.w, 1.0e-8);
    vec3 coord = projected * 0.5 + 0.5;
    if (coord.x < 0.0 || coord.x > 1.0 || coord.y < 0.0 || coord.y > 1.0 || coord.z < 0.0 || coord.z > 1.0) return 1.0;
    float sampled = index == 0 ? texture(u_shadow0, coord.xy).r :
                    index == 1 ? texture(u_shadow1, coord.xy).r :
                    index == 2 ? texture(u_shadow2, coord.xy).r : texture(u_shadow3, coord.xy).r;
    float slope = max(1.0 - dot(normal, light_direction), 0.0);
    float lit = coord.z - (u_shadow_bias * (1.0 + slope)) <= sampled ? 1.0 : 1.0 - u_shadow_opacity;
    return lit;
}

void main() {
    if (u_outline_pass != 0) { frag_color = u_outline_color; return; }
    vec4 base = u_base_color;
    if (u_use_vertex_color != 0) base *= v_color;
    if (u_use_texture != 0) base *= texture(u_base_texture, v_uv);
    if (u_alpha_mode == 1 && base.a < u_alpha_cutoff) discard;
    vec3 normal = normalize(v_world_normal);
    // glTF double-sided materials use the normal facing the viewer on their
    // back side.  Culling is disabled for these primitives in the draw pass;
    // gl_FrontFacing therefore tells us when the interpolated authored normal
    // needs to be reversed for lighting.
    if (u_double_sided != 0 && !gl_FrontFacing) normal = -normal;
    vec3 illumination = u_surface == 2 ? vec3(1.0) : u_ambient;
    for (int i = 0; i < 8; ++i) {
        if (i >= u_light_count) break;
        vec3 to_light;
        float attenuation = 1.0;
        if (u_light_type[i] == 0) {
            to_light = normalize(-u_light_direction[i]);
        } else {
            vec3 offset = u_light_position[i] - v_world_position;
            float distance_to_light = length(offset);
            to_light = offset / max(distance_to_light, 1.0e-6);
            attenuation = 1.0 / max(1.0, distance_to_light * distance_to_light);
            if (u_light_range[i] > 0.0) attenuation *= pow(clamp(1.0 - distance_to_light / u_light_range[i], 0.0, 1.0), 2.0);
            if (u_light_type[i] == 3) {
                float cone = dot(normalize(-u_light_direction[i]), to_light);
                attenuation *= smoothstep(u_light_spot_cos[i].x, u_light_spot_cos[i].y, cone);
            }
        }
        float diffuse = max(dot(normal, to_light), 0.0);
        int shadow_index = u_light_shadow_index[i];
        float shadow = shadow_index >= 0 && shadow_index < u_shadow_count ? shadow_sample(shadow_index, v_world_position, normal, to_light) : 1.0;
        illumination += u_light_color[i] * u_light_energy[i] * attenuation * diffuse * shadow;
    }
    if (u_surface == 1) {
        float level = max(max(illumination.r, illumination.g), illumination.b);
        vec3 toon = u_toon_colors[0];
        for (int i = 1; i < 8; ++i) {
            if (i >= u_toon_count) break;
            if (level >= u_toon_positions[i]) toon = u_toon_colors[i];
        }
        illumination = toon;
    }
    frag_color = vec4(base.rgb * illumination, base.a);
}
"""

_DEPTH_VERTEX_SHADER = """#version 330
in vec3 in_position;
uniform mat4 u_model;
uniform mat4 u_light_view_projection;
void main() { gl_Position = u_light_view_projection * u_model * vec4(in_position, 1.0); }
"""
_DEPTH_FRAGMENT_SHADER = """#version 330
void main() { }
"""

_LINE_VERTEX_SHADER = """#version 330
in vec3 in_position;
in vec4 in_color;
uniform mat4 u_view_projection;
out vec4 v_color;
void main() { v_color = in_color; gl_Position = u_view_projection * vec4(in_position, 1.0); }
"""
_LINE_FRAGMENT_SHADER = """#version 330
in vec4 v_color; out vec4 frag_color; void main() { frag_color = v_color; }
"""

_COMPOSITE_VERTEX_SHADER = """#version 330
in vec2 in_position; out vec2 v_uv;
void main() { v_uv = in_position * 0.5 + 0.5; gl_Position = vec4(in_position, 0.0, 1.0); }
"""
_COMPOSITE_FRAGMENT_SHADER = """#version 330
in vec2 v_uv; out vec4 frag_color;
uniform sampler2D u_face0; uniform sampler2D u_face1; uniform sampler2D u_face2;
uniform sampler2D u_face3; uniform sampler2D u_face4; uniform sampler2D u_face5;
uniform vec2 u_viewport_size; uniform float u_half_fov; uniform int u_mapping;
uniform int u_crop; uniform float u_gutter;
float inverse_radius(float radius) {
    if (u_mapping == 1) return 2.0 * asin(clamp(radius * 0.5, -1.0, 1.0));
    if (u_mapping == 2) return 2.0 * atan(radius * 0.5);
    if (u_mapping == 3) return asin(clamp(radius, -1.0, 1.0));
    return radius;
}
float forward_radius(float theta) {
    if (u_mapping == 1) return 2.0 * sin(theta * 0.5);
    if (u_mapping == 2) return 2.0 * tan(theta * 0.5);
    if (u_mapping == 3) return sin(theta);
    return theta;
}
vec4 sample_face(vec3 direction) {
    vec3 a = abs(direction); int face; vec2 local;
    if (a.x >= a.y && a.x >= a.z) {
        if (direction.x >= 0.0) { face=0; local=vec2(direction.z, direction.y)/a.x; }
        else { face=1; local=vec2(-direction.z, direction.y)/a.x; }
    } else if (a.y >= a.z) {
        if (direction.y >= 0.0) { face=2; local=vec2(direction.x, direction.z)/a.y; }
        else { face=3; local=vec2(direction.x, -direction.z)/a.y; }
    } else {
        if (direction.z >= 0.0) { face=4; local=vec2(-direction.x, direction.y)/a.z; }
        else { face=5; local=vec2(direction.x, direction.y)/a.z; }
    }
    vec2 uv = 0.5 + 0.5 * local / u_gutter;
    return face==0?texture(u_face0,uv):face==1?texture(u_face1,uv):face==2?texture(u_face2,uv):face==3?texture(u_face3,uv):face==4?texture(u_face4,uv):texture(u_face5,uv);
}
void main() {
    vec2 centered = (v_uv - 0.5) * u_viewport_size;
    float reference = u_crop == 0 ? min(u_viewport_size.x,u_viewport_size.y) : length(u_viewport_size);
    float normalized_radius = 2.0 * length(centered) / reference;
    if (normalized_radius > 1.0) { frag_color=vec4(0.0); return; }
    float theta = inverse_radius(normalized_radius * forward_radius(u_half_fov));
    vec2 axis = length(centered) < 1.0e-7 ? vec2(1.0,0.0) : vec2(centered.x,-centered.y)/length(centered);
    vec3 direction = vec3(sin(theta)*axis, -cos(theta));
    frag_color = sample_face(direction);
}
"""


def _matrix_bytes(matrix: np.ndarray) -> bytes:
    return np.asarray(matrix, dtype="f4").T.tobytes()


def _qimage_from_rgba(pixels: np.ndarray) -> QImage:
    # Mesh/line passes use separate-alpha blending. RGB in the framebuffer is
    # therefore already premultiplied while alpha retains its true coverage.
    values = np.asarray(pixels, dtype=np.uint8).copy()
    height, width = values.shape[:2]
    image = QImage(values.data, width, height, width * 4, QImage.Format.Format_RGBA8888_Premultiplied)
    return image.copy()


class OffscreenRenderer:
    """ModernGL 3.3 backend. Construct and call it on one thread only."""

    def __init__(self, context: Any | None = None) -> None:
        try:
            import moderngl
        except Exception as exc:
            raise RendererUnavailable("ModernGL is not installed") from exc
        self.gl = moderngl
        try:
            self.context = context or moderngl.create_standalone_context(require=330)
        except Exception as exc:
            raise RendererUnavailable(f"OpenGL 3.3 context unavailable: {exc}") from exc
        try:
            self.mesh_program = self.context.program(vertex_shader=_MESH_VERTEX_SHADER, fragment_shader=_MESH_FRAGMENT_SHADER)
            self.depth_program = self.context.program(vertex_shader=_DEPTH_VERTEX_SHADER, fragment_shader=_DEPTH_FRAGMENT_SHADER)
            self.line_program = self.context.program(vertex_shader=_LINE_VERTEX_SHADER, fragment_shader=_LINE_FRAGMENT_SHADER)
            self.composite_program = self.context.program(vertex_shader=_COMPOSITE_VERTEX_SHADER, fragment_shader=_COMPOSITE_FRAGMENT_SHADER)
        except Exception as exc:
            self.release()
            raise RendererUnavailable(f"renderer shader compilation failed: {exc}") from exc
        self.last_metrics = RenderMetrics(0.0, 0, 0, False, 0)
        self.last_warnings: tuple[str, ...] = ()

    def release(self) -> None:
        for name in ("mesh_program", "depth_program", "line_program", "composite_program"):
            resource = getattr(self, name, None)
            if resource is not None:
                try:
                    resource.release()
                except Exception:
                    pass
                setattr(self, name, None)
        context = getattr(self, "context", None)
        if context is not None:
            try:
                context.release()
            except Exception:
                pass
            self.context = None

    def _target(self, size: tuple[int, int], samples: int = 0) -> tuple[Any, Any, Any]:
        if samples:
            color = self.context.renderbuffer(size, components=4, samples=samples)
            depth = self.context.depth_renderbuffer(size, samples=samples)
        else:
            color = self.context.texture(size, 4, dtype="f1")
            depth = self.context.depth_texture(size)
        return self.context.framebuffer([color], depth), color, depth

    def _evaluated(self, scene: SceneData, node: SceneNode, primitive: MeshPrimitive) -> tuple[np.ndarray, np.ndarray]:
        mesh = scene.meshes[node.mesh_index]  # type: ignore[index]
        weights = node.morph_weights or mesh.default_morph_weights
        positions = primitive.evaluated_positions(weights)
        normals = primitive.normals.copy() if primitive.normals is not None else compute_vertex_normals(positions, primitive.indices)
        for target, weight in zip(primitive.morph_targets, weights):
            if target.normal_deltas is not None and weight:
                normals += target.normal_deltas * np.float32(weight)
        lengths = np.linalg.norm(normals, axis=1)
        normals /= np.maximum(lengths[:, None], 1e-12)
        if node.skin_index is not None and primitive.joints is not None and primitive.weights is not None:
            skin = scene.skins[node.skin_index]
            inverse_node = np.linalg.pinv(node.world_matrix)
            joint_matrices = np.asarray([inverse_node @ scene.nodes[joint].world_matrix @ bind for joint, bind in zip(skin.joint_node_ids, skin.inverse_bind_matrices)])
            homogeneous = np.column_stack((positions, np.ones(len(positions), dtype=np.float32)))
            deformed = np.zeros((len(positions), 4), dtype=np.float64)
            for influence in range(4):
                matrices = joint_matrices[primitive.joints[:, influence]]
                deformed += np.einsum("nij,nj->ni", matrices, homogeneous) * primitive.weights[:, influence, None]
            positions = deformed[:, :3].astype(np.float32)
            normals = compute_vertex_normals(positions, primitive.indices)
        return positions, normals

    def _resource(self, scene: SceneData, node: SceneNode, primitive: MeshPrimitive, program: Any) -> tuple[Any, Any, Any]:
        positions, normals = self._evaluated(scene, node, primitive)
        if program is self.depth_program:
            vertex = self.context.buffer(np.ascontiguousarray(positions, dtype=np.float32).tobytes())
            index = self.context.buffer(np.ascontiguousarray(primitive.indices, dtype=np.uint32).tobytes())
            vao = self.context.vertex_array(program, [(vertex, "3f", "in_position")], index)
            return vao, vertex, index
        colors = primitive.colors if primitive.colors is not None else np.ones((len(positions), 4), dtype=np.float32)
        texcoords = primitive.texcoords if primitive.texcoords is not None else np.zeros((len(positions), 2), dtype=np.float32)
        packed = np.ascontiguousarray(np.column_stack((positions, normals, colors, texcoords)), dtype=np.float32)
        vertex = self.context.buffer(packed.tobytes())
        index = self.context.buffer(np.ascontiguousarray(primitive.indices, dtype=np.uint32).tobytes())
        vao = self.context.vertex_array(program, [(vertex, "3f 3f 4f 2f", "in_position", "in_normal", "in_color", "in_uv")], index)
        return vao, vertex, index

    @staticmethod
    def _look_at(position: np.ndarray, target: np.ndarray, up_hint: np.ndarray = np.array([0.0, 1.0, 0.0])) -> np.ndarray:
        forward = normalize(target - position)
        if abs(float(np.dot(forward, up_hint))) > 0.98:
            up_hint = np.array([0.0, 0.0, 1.0])
        right = normalize(np.cross(forward, up_hint))
        up = np.cross(right, forward)
        result = np.identity(4)
        result[0,:3], result[1,:3], result[2,:3] = right, up, -forward
        result[0,3], result[1,3], result[2,3] = -np.dot(right, position), -np.dot(up, position), np.dot(forward, position)
        return result

    @staticmethod
    def _shadow_view_matrix(
        node: SceneNode,
        light: SceneLight,
        scene_center: np.ndarray,
        scene_radius: float,
    ) -> np.ndarray:
        """Build a shadow view that preserves the light object's -Z axis."""
        direction = normalize(-node.world_matrix[:3, 2])
        up_hint = np.asarray(node.world_matrix[:3, 1], dtype=np.float64)
        up_length = float(np.linalg.norm(up_hint))
        if up_length <= 1.0e-12:
            up_hint = np.array([0.0, 1.0, 0.0])
        else:
            up_hint /= up_length
        if light.light_type is LightType.SUN:
            position = np.asarray(scene_center, dtype=np.float64) - direction * scene_radius * 2.5
            target = np.asarray(scene_center, dtype=np.float64)
        elif light.light_type in (LightType.SPOT, LightType.RECTANGLE):
            position = node.world_origin
            target = position + direction
        else:
            raise ValueError("point lights require omnidirectional shadow maps")
        return OffscreenRenderer._look_at(position, target, up_hint)

    @staticmethod
    def _point_shadow_warning(light: SceneLight) -> str:
        return (
            f"Point light {light.name!r} cannot cast omnidirectional shadows "
            "in renderer v1; its shadow casting is disabled."
        )

    def _publish_render_warnings(
        self, scene: SceneData, warnings: tuple[str, ...],
    ) -> None:
        prefix = "Point light "
        suffix = "cannot cast omnidirectional shadows in renderer v1; its shadow casting is disabled."
        retained = tuple(
            warning for warning in scene.warnings
            if not (warning.startswith(prefix) and warning.endswith(suffix))
        )
        self.last_warnings = tuple(dict.fromkeys(warnings))
        scene.warnings = tuple(dict.fromkeys((*retained, *self.last_warnings)))

    def _shadow_maps(self, scene: SceneData) -> tuple[list[Any], list[Any], list[np.ndarray], list[int]]:
        if not scene.shadows.enabled:
            self._publish_render_warnings(scene, ())
            return [], [], [], []
        low, high = scene.bounds()
        center, radius = (low + high) * 0.5, max(float(np.linalg.norm((high-low)*0.5)), 1.0)
        framebuffers, textures, matrices, light_indices = [], [], [], []
        warnings: list[str] = []
        for light_index, (node, light) in enumerate(scene.enabled_lights):
            if not light.casts_shadow:
                continue
            if light.light_type is LightType.POINT:
                warnings.append(self._point_shadow_warning(light))
                continue
            if len(textures) >= 4:
                continue
            view = self._shadow_view_matrix(node, light, center, radius)
            if light.light_type is LightType.SUN:
                projection = np.identity(4)
                projection[0,0] = projection[1,1] = 1.0 / radius
                projection[2,2], projection[2,3] = -1.0/(radius*3.0), -0.2
            else:
                projection = perspective_matrix(math.degrees(light.spot_outer_angle*2.0) if light.light_type is LightType.SPOT else 100.0, 1.0, max(radius*.001,.001), radius*6.0)
            matrix = projection @ view
            texture = self.context.depth_texture((scene.shadows.resolution, scene.shadows.resolution))
            texture.compare_func = ""
            texture.repeat_x = texture.repeat_y = False
            framebuffer = self.context.framebuffer(depth_attachment=texture)
            framebuffer.use(); framebuffer.clear(depth=1.0)
            self.context.enable(self.gl.DEPTH_TEST)
            self.depth_program["u_light_view_projection"].write(_matrix_bytes(matrix))
            for mesh_node in scene.visible_nodes():
                self.depth_program["u_model"].write(_matrix_bytes(mesh_node.world_matrix))
                for primitive in scene.meshes[mesh_node.mesh_index].primitives:  # type: ignore[index]
                    source, _drawing = self._material(scene, primitive)
                    self._set_triangle_culling(
                        mesh_node,
                        enabled=not source.double_sided,
                    )
                    vao, vertex, index = self._resource(scene, mesh_node, primitive, self.depth_program)
                    vao.render(mode=self.gl.TRIANGLES)
                    vao.release(); vertex.release(); index.release()
            self.context.front_face = "ccw"
            self.context.cull_face = "back"
            self.context.enable(self.gl.CULL_FACE)
            framebuffers.append(framebuffer); textures.append(texture); matrices.append(matrix); light_indices.append(light_index)
        self._publish_render_warnings(scene, tuple(warnings))
        return framebuffers, textures, matrices, light_indices

    def _set_lights(self, scene: SceneData, shadow_matrices: list[np.ndarray], shadow_light_indices: list[int]) -> None:
        pairs = scene.enabled_lights
        self.mesh_program["u_light_count"].value = len(pairs)
        light_types = [0] * 8
        positions = [(0.0, 0.0, 0.0)] * 8
        directions = [(0.0, -1.0, 0.0)] * 8
        colors = [(0.0, 0.0, 0.0)] * 8
        energies = [0.0] * 8
        ranges = [0.0] * 8
        spot_cosines = [(-1.0, 1.0)] * 8
        shadow_indices = [-1] * 8
        for shadow_index, light_index in enumerate(shadow_light_indices):
            shadow_indices[light_index] = shadow_index
        for index, (node, light) in enumerate(pairs):
            type_value = {LightType.SUN:0, LightType.POINT:1, LightType.RECTANGLE:2, LightType.SPOT:3}[light.light_type]
            light_types[index] = type_value
            positions[index] = tuple(float(value) for value in node.world_origin)
            directions[index] = tuple(float(value) for value in normalize(-node.world_matrix[:3,2]))
            colors[index] = light.color
            # Blender energy is deliberately retained raw in metadata. This
            # bounded approximation keeps viewport exposure usable.
            energies[index] = min(light.energy, 10000.0) * (0.001 if light.energy > 20 else 1.0)
            ranges[index] = light.range
            spot_cosines[index] = (math.cos(light.spot_outer_angle), math.cos(light.spot_inner_angle))
        # ModernGL reflects GLSL arrays under their base names. Scalar arrays
        # accept a flat tuple; vector arrays accept a tuple of vector tuples.
        self.mesh_program["u_light_type"].value = tuple(light_types)
        self.mesh_program["u_light_position"].value = tuple(positions)
        self.mesh_program["u_light_direction"].value = tuple(directions)
        self.mesh_program["u_light_color"].value = tuple(colors)
        self.mesh_program["u_light_energy"].value = tuple(energies)
        self.mesh_program["u_light_range"].value = tuple(ranges)
        self.mesh_program["u_light_spot_cos"].value = tuple(spot_cosines)
        self.mesh_program["u_light_shadow_index"].value = tuple(shadow_indices)
        self.mesh_program["u_shadow_count"].value = len(shadow_matrices)
        padded_matrices = shadow_matrices + [np.identity(4)] * (4 - len(shadow_matrices))
        packed_matrices = np.asarray([matrix.T for matrix in padded_matrices], dtype="f4")
        self.mesh_program["u_shadow_matrix"].write(packed_matrices.tobytes())
        self.mesh_program["u_shadow_bias"].value = scene.shadows.bias
        self.mesh_program["u_shadow_opacity"].value = scene.shadows.opacity

    def _material(self, scene: SceneData, primitive: MeshPrimitive) -> tuple[SourceMaterial, DrawingMaterial | None]:
        source = scene.source_materials[primitive.material_index] if scene.source_materials else SourceMaterial("material:default")
        drawing_id = scene.material_mappings.get(source.material_id)
        return source, scene.drawing_materials.get(drawing_id) if drawing_id else None

    def _set_triangle_culling(
        self,
        node: SceneNode,
        *,
        enabled: bool,
        face: str = "back",
    ) -> None:
        """Configure culling for a primitive under an exact world transform.

        glTF triangle fronts are counter-clockwise.  A world matrix with a
        negative determinant reverses that winding, so ModernGL must classify
        clockwise triangles as fronts for that node.  This also keeps the
        front-culling inverted-hull outline on the actual back faces.
        """
        self.context.front_face = "cw" if node.determinant < 0.0 else "ccw"
        self.context.cull_face = face
        if enabled:
            self.context.enable(self.gl.CULL_FACE)
        else:
            self.context.disable(self.gl.CULL_FACE)

    def _draw_meshes(self, scene: SceneData, view_projection: np.ndarray, shadow_textures: list[Any], shadow_matrices: list[np.ndarray], shadow_light_indices: list[int], *, include_floor: bool, size: tuple[int,int], selected_ids: frozenset[str]) -> tuple[int,int]:
        self.mesh_program["u_view_projection"].write(_matrix_bytes(view_projection))
        self.mesh_program["u_ambient"].value = scene.ambient_color
        self._set_lights(scene, shadow_matrices, shadow_light_indices)
        for index in range(4):
            self.mesh_program[f"u_shadow{index}"].value = 8 + index
            if index < len(shadow_textures):
                shadow_textures[index].use(8 + index)
        draw_calls, triangles = 0, 0
        entries: list[tuple[SceneNode, Any]] = [(node, scene.meshes[node.mesh_index]) for node in scene.visible_nodes()]  # type: ignore[index]
        if include_floor:
            entries.append((SceneNode("internal:floor-node", "Floor"), floor_mesh(extent=scene.overlays.grid_extent)))
        self.context.enable(self.gl.DEPTH_TEST | self.gl.CULL_FACE | self.gl.BLEND)
        self.context.blend_func = (
            self.gl.SRC_ALPHA, self.gl.ONE_MINUS_SRC_ALPHA,
            self.gl.ONE, self.gl.ONE_MINUS_SRC_ALPHA,
        )
        for node, mesh in entries:
            self.mesh_program["u_model"].write(_matrix_bytes(node.world_matrix))
            self.mesh_program["u_normal_matrix"].write(np.asarray(np.linalg.pinv(node.world_matrix[:3,:3]).T, dtype="f4").T.tobytes())
            for primitive in mesh.primitives:
                source, drawing = self._material(scene, primitive) if node.node_id != "internal:floor-node" else (SourceMaterial("internal:floor", base_color_factor=np.array([.42,.42,.45,1],dtype=np.float32)), None)
                surface = drawing.surface if drawing else SurfaceMaterial.DIFFUSE
                base = source.base_color_factor.astype(np.float64) * np.asarray(drawing.base_color if drawing else (1,1,1,1))
                outline = drawing.outline_enabled if drawing else True
                outline_color = drawing.outline_color if drawing else (0,0,0,.8)
                thickness = drawing.outline_thickness_px if drawing else 1.5
                if node.node_id in selected_ids:
                    outline, outline_color, thickness = True, (1.0, 0.55, 0.08, 1.0), max(thickness, 3.0)
                vao, vertex, index_buffer = self._resource(scene, node, primitive, self.mesh_program) if node.node_id != "internal:floor-node" else self._plain_resource(primitive, self.mesh_program)
                if outline and thickness > 0 and node.node_id != "internal:floor-node":
                    # The silhouette is always an inverted-hull pass.  Keep
                    # front-face culling enabled even when the base surface is
                    # double sided; otherwise the expanded hull fills the
                    # object instead of tracing only its silhouette.
                    self._set_triangle_culling(node, enabled=True, face="front")
                    self.mesh_program["u_outline_pass"].value = 1
                    self.mesh_program["u_outline_color"].value = outline_color
                    self.mesh_program["u_outline_width"].value = max(thickness, .1) * max(scene.active_camera.distance, 1.0) / max(size[1], 1) * 0.6
                    vao.render(mode=self.gl.TRIANGLES); draw_calls += 1
                self._set_triangle_culling(
                    node,
                    enabled=not source.double_sided,
                    face="back",
                )
                self.mesh_program["u_outline_pass"].value = 0
                self.mesh_program["u_outline_width"].value = 0.0
                self.mesh_program["u_double_sided"].value = int(source.double_sided)
                self.mesh_program["u_surface"].value = {SurfaceMaterial.DIFFUSE:0, SurfaceMaterial.TOON:1, SurfaceMaterial.UNSHADED:2}[surface]
                self.mesh_program["u_base_color"].value = tuple(float(v) for v in base)
                ramp = drawing.toon_ramp if drawing else ToonRamp()
                ramp_positions = [stop.position for stop in ramp.stops] + [1.0] * (8 - len(ramp.stops))
                ramp_colors = [stop.color for stop in ramp.stops] + [(1.0, 1.0, 1.0)] * (8 - len(ramp.stops))
                self.mesh_program["u_toon_count"].value = len(ramp.stops)
                self.mesh_program["u_toon_positions"].value = tuple(ramp_positions)
                self.mesh_program["u_toon_colors"].value = tuple(ramp_colors)
                self.mesh_program["u_use_vertex_color"].value = int((drawing.use_vertex_color if drawing else True) and primitive.colors is not None)
                use_texture = (drawing.use_base_color_texture if drawing else True) and source.base_color_texture is not None and primitive.texcoords is not None
                self.mesh_program["u_use_texture"].value = int(use_texture)
                self.mesh_program["u_alpha_mode"].value = {AlphaMode.OPAQUE:0,AlphaMode.MASK:1,AlphaMode.BLEND:2}[source.alpha_mode]
                self.mesh_program["u_alpha_cutoff"].value = source.alpha_cutoff
                texture_resource = None
                if use_texture:
                    texture = scene.textures[source.base_color_texture]  # type: ignore[index]
                    texture_resource = self.context.texture(texture.size, 4, texture.pixels.tobytes())
                    min_filter = self.gl.NEAREST if int(texture.min_filter) in (9728, 9984, 9986) else self.gl.LINEAR
                    mag_filter = self.gl.NEAREST if int(texture.mag_filter) == 9728 else self.gl.LINEAR
                    texture_resource.filter = (min_filter, mag_filter)
                    texture_resource.repeat_x = texture.wrap_s.value != 33071
                    texture_resource.repeat_y = texture.wrap_t.value != 33071
                    texture_resource.use(0); self.mesh_program["u_base_texture"].value = 0
                vao.render(mode=self.gl.TRIANGLES); draw_calls += 1; triangles += len(primitive.indices)
                if texture_resource is not None: texture_resource.release()
                vao.release(); vertex.release(); index_buffer.release()
        self.context.front_face = "ccw"
        self.context.cull_face = "back"
        self.context.enable(self.gl.CULL_FACE)
        return draw_calls, triangles

    def _plain_resource(self, primitive: MeshPrimitive, program: Any) -> tuple[Any,Any,Any]:
        positions = primitive.positions
        normals = primitive.normals if primitive.normals is not None else compute_vertex_normals(positions, primitive.indices)
        packed = np.column_stack((positions,normals,np.ones((len(positions),4),dtype=np.float32),np.zeros((len(positions),2),dtype=np.float32))).astype(np.float32)
        vertex = self.context.buffer(packed.tobytes()); index = self.context.buffer(primitive.indices.astype(np.uint32).tobytes())
        vao = self.context.vertex_array(program, [(vertex,"3f 3f 4f 2f","in_position","in_normal","in_color","in_uv")], index)
        return vao,vertex,index

    def _draw_lines(
        self,
        scene: SceneData,
        view_projection: np.ndarray,
        draw_grid: bool,
        draw_volume_grid: bool,
        draw_axes: bool,
    ) -> int:
        groups = []
        if draw_grid:
            grid = build_floor_grid(scene.overlays.grid_extent, extent=scene.overlays.grid_extent)
            groups.append((grid.vertices, np.tile(np.array([[.45,.45,.48,.32]],dtype=np.float32),(len(grid.vertices),1))))
        if draw_volume_grid:
            volume = build_volume_grid(scene.overlays.grid_extent)
            groups.append((volume.vertices, volume.colors))
        if draw_axes:
            axes = axis_lines(scene.overlays.grid_extent)
            groups.append((axes.vertices,axes.colors))
        if not groups: return 0
        self.line_program["u_view_projection"].write(_matrix_bytes(view_projection))
        self.context.enable(self.gl.BLEND)
        self.context.blend_func = (
            self.gl.SRC_ALPHA, self.gl.ONE_MINUS_SRC_ALPHA,
            self.gl.ONE, self.gl.ONE_MINUS_SRC_ALPHA,
        )
        calls = 0
        for positions,colors in groups:
            packed=np.column_stack((positions,colors)).astype(np.float32); buffer=self.context.buffer(packed.tobytes())
            vao=self.context.vertex_array(self.line_program,[(buffer,"3f 4f","in_position","in_color")])
            vao.render(mode=self.gl.LINES); vao.release(); buffer.release(); calls+=1
        return calls

    def _read(self, framebuffer: Any, size: tuple[int,int]) -> QImage:
        raw = framebuffer.read(components=4, alignment=1)
        pixels = np.frombuffer(raw, dtype=np.uint8).reshape((size[1],size[0],4))[::-1]
        return _qimage_from_rgba(pixels)

    def render(self, scene: SceneData, size: tuple[int,int], options: RenderOptions = RenderOptions()) -> QImage:
        width,height = int(size[0]),int(size[1])
        if width <= 0 or height <= 0 or width > 16384 or height > 16384:
            raise ValueError("render dimensions must be between 1 and 16384")
        started=time.perf_counter(); draw_calls=triangles=0; readback_ms=0.0
        low,high=scene.bounds(); radius=max(float(np.linalg.norm((high-low)*.5)),1.0)
        near,far=clip_planes(scene.active_camera.distance,radius)
        include_floor=scene.overlays.floor_visible if options.draw_floor is None else options.draw_floor
        draw_grid=scene.overlays.grid_visible if options.draw_grid is None else options.draw_grid
        draw_volume_grid=(
            scene.overlays.volume_grid_visible
            if options.draw_volume_grid is None else options.draw_volume_grid
        )
        draw_axes=scene.overlays.axes_visible if options.draw_axes is None else options.draw_axes
        shadow_fbos,shadow_textures,shadow_matrices,shadow_light_indices=self._shadow_maps(scene)
        final_fbo,final_color,final_depth=self._target((width,height),4 if options.antialiasing else 0)
        resolve=None
        try:
            final_fbo.use(); final_fbo.clear(0,0,0,0,depth=1.0)
            if scene.projection.mode is ProjectionMode.FISHEYE:
                face_size=cubemap_face_size(scene.projection,(width,height),interactive=options.interactive,max_texture_size=self.context.info.get("GL_MAX_TEXTURE_SIZE",4096))
                face_resources=[]; required=set(required_cube_faces(scene.projection))
                face_projection=perspective_matrix(90.0,1.0,near,far)
                for face in CubeFace:
                    framebuffer,color,depth=self._target((face_size,face_size)); face_resources.append((framebuffer,color,depth))
                    framebuffer.use(); framebuffer.clear(0,0,0,0,depth=1.0)
                    if face in required:
                        calls,tris=self._draw_meshes(scene,face_projection@face_view_matrix(scene.active_camera,face),shadow_textures,shadow_matrices,shadow_light_indices,include_floor=include_floor,size=(face_size,face_size),selected_ids=options.selected_node_ids); draw_calls+=calls; triangles+=tris
                        draw_calls += self._draw_lines(scene, face_projection @ face_view_matrix(scene.active_camera, face), draw_grid, draw_volume_grid, draw_axes)
                final_fbo.use(); final_fbo.clear(0,0,0,0,depth=1.0)
                self.context.disable(self.gl.DEPTH_TEST | self.gl.CULL_FACE | self.gl.BLEND)
                quad=np.asarray([[-1,-1],[1,-1],[-1,1],[-1,1],[1,-1],[1,1]],dtype=np.float32); buffer=self.context.buffer(quad.tobytes()); vao=self.context.vertex_array(self.composite_program,[(buffer,"2f","in_position")])
                for index,(_fb,color,_depth) in enumerate(face_resources): color.use(index); self.composite_program[f"u_face{index}"].value=index
                self.composite_program["u_viewport_size"].value=(float(width),float(height)); self.composite_program["u_half_fov"].value=math.radians(scene.projection.effective_fisheye_fov_deg)*.5
                self.composite_program["u_mapping"].value=list(FisheyeMapping).index(scene.projection.fisheye_mapping); self.composite_program["u_crop"].value=0 if scene.projection.fisheye_crop is FisheyeCrop.CIRCULAR else 1; self.composite_program["u_gutter"].value=face_gutter_tangent(face_size)
                vao.render(mode=self.gl.TRIANGLES); draw_calls+=1; vao.release(); buffer.release()
                for framebuffer,color,depth in face_resources: framebuffer.release(); color.release(); depth.release()
            else:
                projection=scene.projection.matrix(width/height,near,far); view_projection=projection@scene.active_camera.view_matrix()
                calls,tris=self._draw_meshes(scene,view_projection,shadow_textures,shadow_matrices,shadow_light_indices,include_floor=include_floor,size=(width,height),selected_ids=options.selected_node_ids); draw_calls+=calls; triangles+=tris
                draw_calls+=self._draw_lines(scene,view_projection,draw_grid,draw_volume_grid,draw_axes)
            if options.antialiasing:
                resolve,resolve_color,resolve_depth=self._target((width,height)); self.context.copy_framebuffer(resolve,final_fbo)
                read_started=time.perf_counter(); image=self._read(resolve,(width,height)); readback_ms=(time.perf_counter()-read_started)*1000
                resolve_color.release(); resolve_depth.release()
            else:
                read_started=time.perf_counter(); image=self._read(final_fbo,(width,height)); readback_ms=(time.perf_counter()-read_started)*1000
        finally:
            if resolve is not None: resolve.release()
            final_fbo.release(); final_color.release(); final_depth.release()
            for framebuffer in shadow_fbos: framebuffer.release()
            for texture in shadow_textures: texture.release()
        elapsed_ms=(time.perf_counter()-started)*1000
        self.last_metrics=RenderMetrics(
            elapsed_ms,draw_calls,triangles,
            scene.projection.mode is ProjectionMode.FISHEYE,
            len(shadow_matrices),max(0.0,elapsed_ms-readback_ms),readback_ms,
        )
        return image
