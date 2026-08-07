"""Internal Wavefront OBJ/MTL importer with per-object scene nodes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex
from typing import Iterable

import numpy as np
from PIL import Image

from .import_options import ImportOptions, NormalPolicy, import_model_matrix
from .mesh import AlphaMode, MeshData, MeshPrimitive, SourceMaterial, TextureData, compute_vertex_normals
from .scene import SceneData, SceneNode


class ObjLoadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Corner:
    position: int
    texcoord: int | None
    normal: int | None


@dataclass(slots=True)
class _Group:
    name: str
    material: str | None
    triangles: list[tuple[_Corner, _Corner, _Corner]]


def _index(value: str, count: int, label: str) -> int:
    try:
        raw = int(value)
    except ValueError as exc:
        raise ObjLoadError(f"invalid {label} index {value!r}") from exc
    if raw == 0:
        raise ObjLoadError(f"{label} indices are one-based")
    result = raw - 1 if raw > 0 else count + raw
    if not 0 <= result < count:
        raise ObjLoadError(f"{label} index is out of range")
    return result


def _corner(token: str, positions: int, texcoords: int, normals: int) -> _Corner:
    parts = token.split("/")
    if not parts or len(parts) > 3 or not parts[0]:
        raise ObjLoadError(f"invalid face corner {token!r}")
    return _Corner(
        _index(parts[0], positions, "position"),
        _index(parts[1], texcoords, "texture") if len(parts) > 1 and parts[1] else None,
        _index(parts[2], normals, "normal") if len(parts) > 2 and parts[2] else None,
    )


def _safe_relative(base: Path, value: str) -> Path:
    candidate = (base / value).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise ObjLoadError("OBJ dependency escapes its source directory") from exc
    return candidate


def _load_mtl(path: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    current: dict[str, object] | None = None
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as exc:
        raise ObjLoadError(f"cannot read material library {path.name!r}") from exc
    for raw in lines:
        try:
            tokens = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            continue
        if not tokens:
            continue
        keyword, values = tokens[0].lower(), tokens[1:]
        if keyword == "newmtl" and values:
            current = {"color": [1.0, 1.0, 1.0, 1.0]}
            result[" ".join(values)] = current
        elif current is not None and keyword == "kd" and len(values) >= 3:
            current["color"] = [float(values[0]), float(values[1]), float(values[2]), current["color"][3]]  # type: ignore[index]
        elif current is not None and keyword in ("d", "tr") and values:
            alpha = float(values[0])
            if keyword == "tr":
                alpha = 1.0 - alpha
            current["color"][3] = max(0.0, min(1.0, alpha))  # type: ignore[index]
        elif current is not None and keyword == "map_kd" and values:
            # Wavefront options precede the file. The final token is the useful path.
            current["texture"] = values[-1]
    return result


def load_obj(source: str | Path | bytes, options: ImportOptions | None = None, *, base_dir: str | Path | None = None) -> SceneData:
    if isinstance(source, bytes):
        text = source.decode("utf-8-sig", errors="replace")
        directory = Path(base_dir) if base_dir is not None else None
    else:
        path = Path(source)
        text, directory = path.read_text(encoding="utf-8-sig", errors="replace"), path.parent
    positions: list[tuple[float, float, float]] = []
    position_colors: list[tuple[float, float, float, float] | None] = []
    texcoords: list[tuple[float, float]] = []
    normals: list[tuple[float, float, float]] = []
    groups: list[_Group] = [_Group("Object", None, [])]
    libraries: list[str] = []
    current_name, current_material = "Object", None

    for line_number, raw in enumerate(text.splitlines(), 1):
        try:
            tokens = shlex.split(raw, comments=True, posix=True)
        except ValueError as exc:
            raise ObjLoadError(f"line {line_number}: malformed quoting") from exc
        if not tokens:
            continue
        keyword, values = tokens[0].lower(), tokens[1:]
        try:
            if keyword == "v" and len(values) >= 3:
                positions.append(tuple(float(v) for v in values[:3]))
                position_colors.append(tuple(float(v) for v in values[3:6]) + (1.0,) if len(values) >= 6 else None)
            elif keyword == "vt" and len(values) >= 2:
                texcoords.append((float(values[0]), float(values[1])))
            elif keyword == "vn" and len(values) >= 3:
                normals.append(tuple(float(v) for v in values[:3]))
            elif keyword in ("o", "g"):
                current_name = " ".join(values) or "Object"
            elif keyword == "usemtl":
                current_material = " ".join(values) if values else None
            elif keyword == "mtllib":
                libraries.extend(values)
            elif keyword == "f":
                if len(values) < 3:
                    raise ObjLoadError("face has fewer than three corners")
                corners = [_corner(value, len(positions), len(texcoords), len(normals)) for value in values]
                group = next((item for item in groups if item.name == current_name and item.material == current_material), None)
                if group is None:
                    group = _Group(current_name, current_material, [])
                    groups.append(group)
                # Blender's sync fallback emits convex polygons; deterministic fan
                # triangulation also handles ordinary triangle/quad OBJ files.
                group.triangles.extend((corners[0], corners[i], corners[i + 1]) for i in range(1, len(corners) - 1))
        except (ValueError, ObjLoadError) as exc:
            if isinstance(exc, ObjLoadError):
                raise ObjLoadError(f"line {line_number}: {exc}") from exc
            raise ObjLoadError(f"line {line_number}: invalid numeric value") from exc
    groups = [group for group in groups if group.triangles]
    if not positions or not groups:
        raise ObjLoadError("OBJ contains no renderable faces")

    mtl_data: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    if directory is not None:
        for library in libraries:
            library_path = _safe_relative(directory, library)
            if library_path.is_file():
                mtl_data.update(_load_mtl(library_path))
            else:
                warnings.append(f"Material library not found and was ignored: {library}")
    material_names: list[str | None] = []
    for group in groups:
        if group.material not in material_names:
            material_names.append(group.material)
    textures: list[TextureData] = []
    materials: list[SourceMaterial] = []
    for index, name in enumerate(material_names):
        description = mtl_data.get(name or "", {})
        color = np.asarray(description.get("color", [1,1,1,1]), dtype=np.float32)
        texture_index = None
        if description.get("texture") and directory is not None:
            texture_path = _safe_relative(directory, str(description["texture"]))
            try:
                with Image.open(texture_path) as image:
                    pixels = np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()
                texture_index = len(textures)
                textures.append(TextureData(f"obj:texture:{texture_index}", texture_path.name, pixels))
            except OSError as exc:
                raise ObjLoadError(f"cannot decode texture {texture_path.name!r}") from exc
        materials.append(SourceMaterial(f"obj:material:{index}", name or "Default", color, texture_index, AlphaMode.BLEND if color[3] < 1 else AlphaMode.OPAQUE))

    meshes: list[MeshData] = []
    nodes: dict[str, SceneNode] = {}
    root_ids: list[str] = []
    for group_index, group in enumerate(groups):
        policy = options.normals if options is not None else NormalPolicy.AUTO
        all_authored = all(corner.normal is not None for triangle in group.triangles for corner in triangle)
        authored_valid = all_authored and all(
            np.linalg.norm(normals[corner.normal]) > 1e-12  # type: ignore[index]
            for triangle in group.triangles for corner in triangle
        )
        if policy is NormalPolicy.REQUIRE_AUTHORED and not authored_valid:
            raise ObjLoadError("OBJ normals are missing or invalid")
        force_flat = policy is NormalPolicy.RECOMPUTE_FLAT
        vertex_map: dict[object, int] = {}
        out_positions, out_normals, out_texcoords, out_colors, indices = [], [], [], [], []
        has_normals = authored_valid and policy not in (NormalPolicy.RECOMPUTE_FLAT, NormalPolicy.RECOMPUTE_SMOOTH)
        has_texcoords = all(corner.texcoord is not None for triangle in group.triangles for corner in triangle)
        has_colors = any(position_colors[corner.position] is not None for triangle in group.triangles for corner in triangle)
        for face_index, triangle in enumerate(group.triangles):
            face_positions = np.asarray([positions[corner.position] for corner in triangle], dtype=np.float64)
            if float(np.linalg.norm(np.cross(face_positions[1] - face_positions[0], face_positions[2] - face_positions[0]))) <= 1e-12:
                warnings.append(f"Skipped degenerate OBJ triangle in {group.name}")
                continue
            out_triangle = []
            for corner in triangle:
                key: object = (corner, face_index) if force_flat else corner
                if key not in vertex_map:
                    vertex_map[key] = len(out_positions)
                    out_positions.append(positions[corner.position])
                    if has_normals:
                        out_normals.append(normals[corner.normal])  # type: ignore[index]
                    if has_texcoords:
                        out_texcoords.append(texcoords[corner.texcoord])  # type: ignore[index]
                    if has_colors:
                        out_colors.append(position_colors[corner.position] or (1,1,1,1))
                out_triangle.append(vertex_map[key])
            indices.append(out_triangle)
        if not indices:
            continue
        position_array = np.asarray(out_positions, dtype=np.float32)
        index_array = np.asarray(indices, dtype=np.uint32)
        normal_array = np.asarray(out_normals, dtype=np.float32) if has_normals else compute_vertex_normals(position_array, index_array)
        primitive = MeshPrimitive(position_array, index_array, material_names.index(group.material), normal_array,
            np.asarray(out_texcoords, dtype=np.float32) if has_texcoords else None,
            np.asarray(out_colors, dtype=np.float32) if has_colors else None)
        mesh_index = len(meshes)
        mesh_id, node_id = f"obj:mesh:{group_index}", f"obj:node:{group_index}"
        meshes.append(MeshData(mesh_id, group.name, (primitive,)))
        nodes[node_id] = SceneNode(node_id, group.name, mesh_index=mesh_index)
        root_ids.append(node_id)
    if not meshes:
        raise ObjLoadError("OBJ contains no non-degenerate faces")
    scene = SceneData("obj:scene", nodes, tuple(root_ids), tuple(meshes), tuple(textures), tuple(materials), warnings=tuple(warnings))
    if options is not None:
        low, high = scene.bounds()
        adjustment = import_model_matrix(low, high, options)
        for node_id in root_ids:
            scene.nodes[node_id].local_matrix = adjustment @ scene.nodes[node_id].local_matrix
        scene.recompute_world_matrices()
    return scene


class ObjLoader:
    @staticmethod
    def load(source: str | Path | bytes, options: ImportOptions | None = None, *, base_dir: str | Path | None = None) -> SceneData:
        return load_obj(source, options, base_dir=base_dir)
