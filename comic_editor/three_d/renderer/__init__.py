"""Neutral embedded renderer port.

Source math and rendering behavior were adapted from Perspective Renderer
commit d13b75fc437fad8e990c87a4e2c0d7e6bdf7e73d. This package deliberately
contains no human, character, asset-library, project, or frame-library code.
"""

from .camera import CameraState
from .cubemap import AdaptiveCubemapController, CubeFace, CubemapTarget, direction_to_face_uv, face_view_matrix
from .gizmo import GizmoMode, gizmo_axes, rotation_delta, scale_delta, trackball_delta, translation_delta
from .gltf import GltfImporter, GltfLoadError, GltfLoader, LoadedGltf, load_gltf
from .grid import axis_lines, build_floor_grid, build_volume_grid, nice_spacing
from .id_buffer import SceneIdBuffer, rasterize_scene_ids, select_region_ids
from .import_options import ImportOptions, NormalPolicy, PlacementMode, UpAxis
from .materials import DrawingMaterial, SurfaceMaterial, ToonRamp, ToonRampStop
from .mesh import MeshData, MeshPrimitive, MorphTarget, SkinData, SourceMaterial, TextureData
from .obj import ObjLoadError, ObjLoader, load_obj
from .offscreen import OffscreenRenderer, RenderMetrics, RenderOptions, RendererUnavailable
from .picking import PickHit, WorldRay, pick_region_ids, pick_scene, screen_to_world_ray
from .primitives import create_local_node, cube_mesh, cylinder_mesh, floor_mesh, surface_alignment_matrix
from .projection import FisheyeCrop, FisheyeMapping, ProjectionContext, ProjectionMode, ProjectionSettings
from .resources import ResourceMergeError, replace_object_resource
from .scene import (
    LightType,
    OverlaySettings,
    SceneCamera,
    SceneData,
    SceneLight,
    SceneNode,
    ShadowSettings,
    TransformSpace,
)

__all__ = [name for name in globals() if not name.startswith("_")]
