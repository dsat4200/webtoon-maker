## first
implement speed lines
implement asset browser, assets
do a code review, architecture pass
plan for futureproofing should a 3d engine be implemented later


@Document 3D renderer codebase
@Document webcomic codebase
i want to integrate 3d viewport shape frames with blender sync via a blender addon, into the webcomic drawing program (inside of a shape object). This will allow me to manipulate a blender scene to my liking, and have those changes propogate to the drawing program.

for now, i'd want the following from the pre-existing 3d renderer project
- the rendering engine
- capability to render fisheye, orthographic, and perspective modes
- the toon shader it uses (and normal shader) with vertex color and texture support
- the outline functionality
- basic gltf/obj imports
- pan/tilt/zoom
- the gizmo that has rotate/scale/move/trackball rotate
- the draw cube/draw cylinder functions
- light object support, light types
- camera fov support
- shadows/shadow settings
- 3d grid overlay and axis overlays
- global/local transforms

do not import
- the human functionality
- asset library functionality

the blender sync requirements:
- would include a blender plugin to aid in syncing
- blender owns the source data - espeically geometry, armatures, and collections. the drawing application owns the per-comic-frame presentation overrides (lighting, pose data, camera views or metadata, which perspective type (since blender only has 2), etc). the drawing program side has limited control - implementing controls on a case by case basis as i see fit, updating the comic frame data.
- blender mesh edits and transformations would sync to the program
- each "blender object" in the comic is associated with a specific blender "comic frame" - choosable in the blender addon.
- a webcomic chapter can only be associated with one blender file. this way, only one blender file's models/collections/assets can be cached / updated and not have to do it for every 3d frame.
- these "comic frames" contain the following:
	- collection toggles and visibility
	- any data that can be keyframed for the active collections (transforms, shape keys, poses, visibility light information, camera position rotation, scale, and approximate metadata (as much as can be mirrored from the drawing program) etc)
	-  do not contain geometry (though they may contain parameter data for simple parametric models that were made in the drawing program side.)
	- since object key-framable data and collection heirarchy/visibility is preserved, this means they should also contain light data (metadata for light information, visibility, collection assignment) 
- the drawing program can then use that comic frame data to update its own view for accuracy. in that sense, the comic frame data is a source of truth for both the blender addon interpreter and the webcomic program.
- material assignments are taken from blender for now. in the drawing program, these assignments can be modified to their corresponding drawing-side renderer assignment.
- there's "comic frame" data and there's "comic chapter blender file" data, which handles metadata for things that aren't a part of comic frame data, such as material assignments.
- 3d scenes / collections cannot be created from the drawing program side.
- if a collection didn't exist when a comic frame was made, it should be hidden by default.
- blender addon should have an "update comic frame" button that acts as a save for the comic frame data and a sync trigger simultaneously (omit true live syncing for now)
- for now, do not include modifier support. objects will appear in the drawing program as if they have their modifiers applied.


the 3d blender object (webcomic project)
- acts as a new type of layer, that has a shape (free closed, square, circle/ellipsoid) as its boundary (and an optional outline, 4px black by default)
- the layer's children are not movable or renamable. they are inside an object called "blender" that contains the relevant 3d frame objects.
- this allows us to create 2d drawings above and beneath the blender, and have those draw above or below the scene.
- has the same floor plane
- the background of a 3d object is of course transparent.
- when the 3d object is selected, that's when viewport rotation and navigation controls take over and temporarily disable the 2d viewport pan/tilt/zoom controls
- it also resets canvas rotation and zooms in to frame the 3d blender object.
- this is "3d mode".
	- 3d mode also exposes new ribbon menu items. ribbon menus include
		- view - allows adjusment/visibility toggle of axes, grid, and any other overlays
		- rendering - allows adjustment of shadows, fidelity, and other render settings relating to quality. allows enabling/disabling of anti aliasing (off by default)
		- outline settings - for now, just keep blank. will be similar to freestyle - so make sure blender's "mark freestyle edge" data is still preserved.
		- materials - allows you to adjust material assignments and mapping, with a column for creating, renaming, deleting, and modifying drawing program-side 3d materials.
		- object properties - for the kinds of properties you'd see in blender - scale, position, rotation data, object metadata in its own column (stuff like light settings, camera settings, object parameters for paremeterized objects, etc)
		- tool settings - for tool specific settings. currently only select tool really has a need for it
	- in 3d mode, the tool bar changes to a new list of 3d tools. 
	- these tools include
		- transform object - exposes the gizmos
		- add light
		- draw cube
		- draw cylinder
		- select dropdown:
			- lasso select, rect select
			- in both modes, click works as a selection too. 
			- also, support shift to add, control to remove from selection, both with cursor icon changes. this disables the pantiltzoom controls
				- only enable this feature if "enable multi select" is enabled (off by default) in ribbon menu tool settings
	- selecting a non-3d layer reverts back to 2d mode.