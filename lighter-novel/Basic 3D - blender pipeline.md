a blender pipeline that allows for realtime scene visualization and drawovers in the webcomic program
 changes from blender go to the drawing program, but not vice versa (except for say, character pose data, camera, light, and object positions, camera and light parameters)
material assignments are preserved, but mappings occur to the drawing program's 3d model viewer which has its own material creation workflow.
textures, geometry, vertex color data, visibility, transforms, etc sync over
basic modifiers' resulting geometry should still be visible in the drawing program - mirror, armature, solidify, array, subdivision for instance (no geo nodes)

- data transfer occurs in a per-collection basis. the user can pick which collections transfer and which dont, for each comic frame that has a 3d scene, and which blender frame and scene they correspond to.
-each 3d view frame in the drawing program will correspond to a specific blender scene and keyframe (0,1,2, etc).
- this way, the user can set different parameters, poses, camera setups, etc for each frame and go between them easily.

in the drawing program, within each frame that has a 3d scene, the user would have orbit pan and dolly, and be able to modify camera settings, pick objects, translate rotate and scale with gizmos, set light and shadow parameters and settings. at the start, the material remap should just be to an empty material named after the assignment in blender, set to a flat color or the object's texture or vertex colors for simplicity.


this will require making a blender addon for managing the RPC along an RPC manager in the drawing program.

given the requirements I've outlined, what would be the major roadblocks in this pipeline and what would be the course of action to proceed with implementation?
