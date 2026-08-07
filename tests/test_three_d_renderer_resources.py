from __future__ import annotations

import numpy as np
import pytest

from comic_editor.three_d.renderer.mesh import (
    MeshData,
    MeshPrimitive,
    MorphTarget,
    SkinData,
    SourceMaterial,
    TextureData,
)
from comic_editor.three_d.renderer.resources import (
    ResourceMergeError,
    replace_object_resource,
)
from comic_editor.three_d.renderer.scene import SceneData, SceneNode


def _triangle(
    mesh_id: str,
    *,
    scale: float = 1.0,
    material_index: int = 0,
    morph: bool = False,
    skinned: bool = False,
) -> MeshData:
    target = MorphTarget(
        "Smile",
        np.asarray([[0.0, 0.0, 0.25]] * 3, dtype=np.float32),
    )
    primitive = MeshPrimitive(
        np.asarray([
            [0.0, 0.0, 0.0],
            [scale, 0.0, 0.0],
            [0.0, scale, 0.0],
        ], dtype=np.float32),
        np.asarray([[0, 1, 2]], dtype=np.uint32),
        material_index=material_index,
        joints=(
            np.zeros((3, 4), dtype=np.uint16) if skinned else None
        ),
        weights=(
            np.asarray([[1.0, 0.0, 0.0, 0.0]] * 3, dtype=np.float32)
            if skinned else None
        ),
        morph_targets=(target,) if morph else (),
    )
    return MeshData(mesh_id, mesh_id, (primitive,))


def test_replaces_only_target_and_rebases_texture_and_material_indexes() -> None:
    base_texture = TextureData(
        "base-texture", "Base", np.full((1, 1, 4), 127, dtype=np.uint8)
    )
    base = SceneData(
        nodes={
            "root": SceneNode(
                "root", "Root", child_ids=("target", "other")
            ),
            "target": SceneNode(
                "target", "Target", parent_id="root", mesh_index=0,
                extras={"base": True},
            ),
            "other": SceneNode(
                "other", "Other", parent_id="root", mesh_index=1,
            ),
        },
        root_node_ids=("root",),
        meshes=(_triangle("old"), _triangle("other", scale=3.0)),
        textures=(base_texture,),
        source_materials=(
            SourceMaterial("base-material", base_color_texture=0),
        ),
    )
    resource_texture = TextureData(
        "resource-texture", "Resource",
        np.asarray([[[10, 20, 30, 255]]], dtype=np.uint8),
    )
    resource = SceneData(
        nodes={
            "target": SceneNode(
                "target", "Evaluated target", mesh_index=0,
                extras={"evaluated": True},
            ),
        },
        root_node_ids=("target",),
        meshes=(_triangle("evaluated", scale=2.0),),
        textures=(resource_texture,),
        source_materials=(
            SourceMaterial("resource-material", base_color_texture=0),
        ),
        warnings=("Static modifier was evaluated.",),
    )

    merged = replace_object_resource(base, resource, "target")

    assert base.nodes["target"].mesh_index == 0  # Inputs remain immutable.
    assert merged.nodes["target"].parent_id == "root"
    assert merged.nodes["root"].child_ids == ("target", "other")
    assert merged.nodes["other"].mesh_index == 1
    assert merged.nodes["target"].mesh_index == 2
    assert merged.nodes["target"].extras == {
        "base": True, "evaluated": True,
    }
    replacement = merged.meshes[2].primitives[0]
    assert replacement.positions[1, 0] == pytest.approx(2.0)
    assert replacement.material_index == 1
    assert merged.source_materials[1].material_id == "resource-material"
    assert merged.source_materials[1].base_color_texture == 1
    assert merged.textures[1].texture_id == "resource-texture"
    assert merged.warnings == ("Static modifier was evaluated.",)


def test_rebases_skin_and_preserves_morph_targets_and_weights() -> None:
    base = SceneData(
        nodes={
            "target": SceneNode("target", "Target", mesh_index=0),
            "joint": SceneNode("joint", "Joint"),
            "unrelated": SceneNode("unrelated", "Unrelated"),
        },
        root_node_ids=("target", "joint", "unrelated"),
        meshes=(_triangle("old"),),
        source_materials=(SourceMaterial("base-material"),),
        skins=(SkinData(
            "existing-skin", "Existing", ("joint",),
            np.asarray([np.identity(4)], dtype=np.float64),
        ),),
    )
    resource = SceneData(
        nodes={
            "target": SceneNode(
                "target", "Target", mesh_index=0, skin_index=0,
                morph_weights=(0.75,),
            ),
            "joint": SceneNode("joint", "Joint"),
        },
        root_node_ids=("target", "joint"),
        meshes=(_triangle("deform", morph=True, skinned=True),),
        source_materials=(SourceMaterial("deform-material"),),
        skins=(SkinData(
            "replacement-skin", "Replacement", ("joint",),
            np.asarray([np.identity(4)], dtype=np.float64),
            "joint",
        ),),
    )

    merged = replace_object_resource(base, resource, "target")

    target = merged.nodes["target"]
    assert target.skin_index == 1
    assert target.morph_weights == (0.75,)
    assert merged.skins[1].skin_id == "replacement-skin"
    assert merged.skins[1].joint_node_ids == ("joint",)
    primitive = merged.meshes[target.mesh_index].primitives[0]
    assert primitive.morph_targets[0].name == "Smile"
    assert primitive.evaluated_positions(target.morph_weights)[0, 2] == pytest.approx(0.1875)
    assert merged.nodes["unrelated"].name == "Unrelated"


def test_rejects_missing_target_or_skin_dependency_without_partial_mutation() -> None:
    base = SceneData(
        nodes={"target": SceneNode("target", "Target", mesh_index=0)},
        root_node_ids=("target",),
        meshes=(_triangle("old"),),
        source_materials=(SourceMaterial("base-material"),),
    )
    resource = SceneData(
        nodes={"target": SceneNode(
            "target", "Target", mesh_index=0, skin_index=0,
        )},
        root_node_ids=("target",),
        meshes=(_triangle("replacement", skinned=True),),
        source_materials=(SourceMaterial("resource-material"),),
        skins=(SkinData(
            "skin", "Skin", ("missing-joint",),
            np.asarray([np.identity(4)], dtype=np.float64),
        ),),
    )

    with pytest.raises(ResourceMergeError, match="missing-joint"):
        replace_object_resource(base, resource, "target")
    assert base.nodes["target"].mesh_index == 0

    with pytest.raises(ResourceMergeError, match="does not contain"):
        replace_object_resource(base, resource, "absent")
