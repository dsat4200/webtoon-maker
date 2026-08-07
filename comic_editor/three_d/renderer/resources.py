"""Composition helpers for object-scoped cached GLB resources.

Blender sync publishes a reusable chapter scene plus object-scoped evaluated
resources.  A resource GLB has its own compact texture, material, mesh, and skin
index spaces, so replacing only the matching object node requires rebasing
those indexes into the chapter scene.  This module keeps that operation neutral
and independent of chapter persistence or Qt UI code.
"""

from __future__ import annotations

import copy
from dataclasses import replace

from .mesh import MeshData, MeshPrimitive, SourceMaterial
from .scene import SceneData


class ResourceMergeError(ValueError):
    """Raised when an object resource cannot be merged without ambiguity."""


def _resource_materials(
    scene: SceneData, object_id: str,
) -> tuple[SourceMaterial, ...]:
    if scene.source_materials:
        return scene.source_materials
    # SceneData permits material-less procedural scenes, where the renderer
    # supplies an implicit material at draw time.  Once the resource is merged
    # into a scene with explicit materials, make that implicit slot concrete.
    return (SourceMaterial(f"resource:{object_id}:default-material"),)


def _rebased_mesh(
    mesh: MeshData,
    *,
    material_offset: int,
    material_count: int,
) -> MeshData:
    primitives: list[MeshPrimitive] = []
    for primitive in mesh.primitives:
        source_index = int(primitive.material_index)
        if not 0 <= source_index < material_count:
            raise ResourceMergeError(
                "Object resource mesh references a missing source material"
            )
        primitives.append(replace(
            primitive,
            material_index=material_offset + source_index,
        ))
    return replace(mesh, primitives=tuple(primitives))


def replace_object_resource(
    base_scene: SceneData,
    resource_scene: SceneData,
    object_id: str,
) -> SceneData:
    """Return ``base_scene`` with one object's render resource replaced.

    The matching base node keeps its identity, transform, hierarchy, camera,
    light, and visibility state.  Its mesh, optional skin, morph weights, and
    resource extras come from ``resource_scene``.  Resource-local texture,
    material, mesh, and skin indexes are appended and rebased without mutating
    either input scene.

    A skinned resource may refer to joints already present in the base scene.
    Missing joint nodes are rejected instead of silently producing corrupt
    deformation; Blender's reusable base GLB is expected to carry that rig.
    """

    object_id = str(object_id).strip()
    if not object_id:
        raise ResourceMergeError("Object resource requires a stable object ID")
    if object_id not in base_scene.nodes:
        raise ResourceMergeError(
            f"Base scene does not contain object node {object_id!r}"
        )
    resource_node = resource_scene.nodes.get(object_id)
    if resource_node is None:
        raise ResourceMergeError(
            f"Object resource does not contain node {object_id!r}"
        )
    if resource_node.mesh_index is None:
        raise ResourceMergeError(
            f"Object resource node {object_id!r} has no mesh"
        )
    if not 0 <= resource_node.mesh_index < len(resource_scene.meshes):
        raise ResourceMergeError(
            f"Object resource node {object_id!r} references a missing mesh"
        )

    result = copy.deepcopy(base_scene)

    # Preserve the renderer's implicit-default semantics for any pre-existing
    # base primitives before introducing explicit resource materials.
    if not result.source_materials and result.meshes:
        result.source_materials = (SourceMaterial("material:default"),)

    texture_offset = len(result.textures)
    result.textures = (*result.textures, *copy.deepcopy(resource_scene.textures))

    source_materials = _resource_materials(resource_scene, object_id)
    material_offset = len(result.source_materials)
    rebased_materials = tuple(
        replace(
            material,
            base_color_texture=(
                texture_offset + material.base_color_texture
                if material.base_color_texture is not None else None
            ),
        )
        for material in copy.deepcopy(source_materials)
    )
    result.source_materials = (*result.source_materials, *rebased_materials)

    resource_mesh = resource_scene.meshes[resource_node.mesh_index]
    mesh = _rebased_mesh(
        copy.deepcopy(resource_mesh),
        material_offset=material_offset,
        material_count=len(source_materials),
    )
    mesh_index = len(result.meshes)
    result.meshes = (*result.meshes, mesh)

    skin_index: int | None = None
    if resource_node.skin_index is not None:
        if not 0 <= resource_node.skin_index < len(resource_scene.skins):
            raise ResourceMergeError(
                f"Object resource node {object_id!r} references a missing skin"
            )
        skin = copy.deepcopy(resource_scene.skins[resource_node.skin_index])
        missing = [
            joint_id for joint_id in skin.joint_node_ids
            if joint_id not in result.nodes
        ]
        if skin.skeleton_root_id and skin.skeleton_root_id not in result.nodes:
            missing.append(skin.skeleton_root_id)
        if missing:
            raise ResourceMergeError(
                "Object resource skin references nodes absent from the base "
                f"scene: {', '.join(dict.fromkeys(missing))}"
            )
        skin_index = len(result.skins)
        result.skins = (*result.skins, skin)

    target = result.nodes[object_id]
    target.mesh_index = mesh_index
    target.skin_index = skin_index
    target.morph_weights = tuple(resource_node.morph_weights)
    target.extras = {
        **target.extras,
        **copy.deepcopy(resource_node.extras),
    }
    result.warnings = tuple(dict.fromkeys(
        (*result.warnings, *resource_scene.warnings)
    ))
    result.validate()
    result.recompute_world_matrices()
    return result


__all__ = ["ResourceMergeError", "replace_object_resource"]
