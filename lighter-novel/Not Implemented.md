


## filled shape bugs
- sometimes drawing a shape draws like a line through the middle and doesn't close it fully.
- tweak create filled shape. straight lines and ends should be vector points. doesn't always fill the shape.


## better grids
- grid view, instead of showing each grid ccell individually, should show grid divisions of 4, like csp



## filled shapes point selection:
- allow for box, lasso select tools to exist if a custom shape is selected. these would allow the user to select and manipulate multiple points at once. (should work like it does in the vector object point selection mode)

## Cage Transform
- https://help.clip-studio.com/en-us/manual_en/360_transform/Types_of_transformations.htm#1004087
- just like mesh transformation from clip studio paint. make it a tool, and also make a modifier version with the same parameters
- the modifier tool, if used on a raster or vector, acts like just clip studio paint. it is a tool that can be used and is destructive (unless you undo.). it does not add a modifier.
- by contrast, the cage transform modifier can not be added to rasters and vectors (intended for objects that can't be traditionally drawn on, (blender shapes, image layers for now)
- if the tool is used on a blender shape or image layer, it should switch to the modifier stack, add the modifier to the stack, and select it, exposing the gizmos.
- if used on a raster/vector, no modifier.
- both expose the same settings. in the raster/vector objects they are in tool settings, in the other 2 they are in modifier settings.
	- show a grid of points that can be transformed, with OK and cancel gizmos below. 
	- additionally, show (with a margin outside) the traditional 8 handles with translate support, rotate handle, and free/uniform toggle that we've come to expect, outside that grid.
	- also include a pivot point as a gizmo.
	- include a slider in tool settings/mod. settings that lets the user change the "smoothness" of the cage transformations (interpolation kind of thing? not sure how this works behind the scenes)
	- horizontal, vertical flip should be gizmos though (align-vertical-centers as the vertical flip icon, align-horizontal-centers for the horizontal flip icon)
	- in tool/modifier settings, include the flip buttons.)
	- no need for the center of rotation option since we have a pivot point gizmo.
	- if used on an image, always keep original image since on an image its a modifier and those are non destructive. don't show this option.
	- number of horizontal and vertical lattice points should be options with a slider and inputtable with keyboard numbers to the right. vertical and horizontal should have their own rows in the UI.
	- include interpolation mode.
	- allow for multi-point selection like csp has.
	- ignore puppet warp for now (we can add that in a later pass.)
	- cage transform modifier is incompatible with vector/raster objects (only the operation is supported)
	- modifiers can now be selected. being selected makes them blueish with a blue outline (like buttons do when toggled). when a modifier is selected, it should expose gizmos. for now, no modifiers have gizmos though except for this cage transform one for now.
		- modifiers can only be selected in modifier mode. switching tabs out of modifier mode retains the selection but hides the modifier gizmos
		- deselecting the object also hides the gizmos but remembers the selection.
	- cage transformation used during a multi selection of rasters/vectors should affect any selected objects.
		- if all objects are vectors/rasters, dont create a modifier when doing this.
		- if mixed, error popup
	- adding a cage transform modifier while multiple objects are selected, if all objects are compatible with mesh transform modifier, should add the modifier to all selected, and link them. (the same should be the case for all modifiers, really - if all in a selection are compatible with the modifier and its added , they should share a linked modifier)
		- trying to add a modifier that is not compatible in a multi selection should throw an error popup.
		- trying to use modifier tool on a multi selection with incompatible objects should also throw an error popup. these should tell the user which are not compatible via text in the popup, and also highlighting those objects red in the outliner.
	- cage transform on a shape should affect the shape and all its children (without needing them to have a linked modifier)
- implement cage transform while maintaining maximum performance. performance is a top concern for cases like these.


## thought balloons and screaming
i still need to add border types for shapes, like round bubbly ones for thought balloons or pointy ones for screaming (or just have it be one with a sharpness value and sizing parameters... hmmm

- stroke types. 


## New modifier type - stroke modifiers
- like in blender used to, add modifier button should expose a menu that has different "categories" of modifier. add a new category of modifier called "stroke modifiers".
- changes what the appearance of a stroke is along its points (does not modify the points)
- all have a "strength" slider from 0 to 100 that lets you change how much the modifier affects the stroke
- should be stackable with other stroke modifiers, and have high performance implementation.
- should apply in order, like modifiers should.
- visible within the same stack as other modifiers
- if applied to a closed shape, should appear seamless.
- parameters of stroke modifiers should still support masking.
- only appear visible / are only compatible with shapes and vector objects.

## Stroke Modifier - Wobble
- uses a non-visible random perlin noise map to make the stroke look wobbly.
- Options
	- Position - Strength of the noise effect over the point location.
- Strength - Strength of the noise effect over the point strength (opacity)
- Noise Scale - Control the noise frequency scale
- Noise Offset - Moves the noise along the strokes.
- randomize seed button - changes the seed used by the pseudo random number generator.
## Stroke Modifier - DotDash
- makes the stroke appear dotted or dashed. dashes are rectangular and follow the curve. dots do not.
- Options:
	- distance - the distance between dots/dashes
	- dash/dot toggle
	- length - only in dash mode, changes the length of dashes.
	- pattern - text-inputtable. spaces and dashes control the repeating pattern of dots/dashes where space is blank and dash is a dash/dot.
	- roundness - 0 to 100. 100 by default on a dot, but can be slid back to make it look like a square though. on dashes, controls roundness of dash corners.

## Stroke modifier - curve shape
![[Pasted image 20260821114654.png]]
handles?




## Stroke modifier - Set brush
- lets the user change the brush used for that object via a modifier.
- works on closed an open shapes too.
- custom dot toggle.
		- how to handle maps? assets? icon pack? stickers?
	- angle range - slider that controls



if applied to a closed shape, behaves differently from on an open shape / vector object.


## Raster/Vector object / pencil features
psd brush support?
- add brushes to tool settings ribbon (raster/vector pencil) (with preview of a small, curved stroke segment in a square live preview icon, from zero pressure to 100, swoop curve )
- outside of shapes can be treated as a stroke that supports psd brushes?

## review the fill tool



smoothing support for pencil/eraser



asset library folders should support dragging items from the asset library into them.
- being inside a folder should make the first element in the grid "Up" which is just the parent folder. dragging into this works too if its there.
- folders should also be possible drag between folders, moving their children with them.

rasterize should be a right click option on any object (but be hidden in, well, raster objects)


merge layers should work between raster objects and between vector objects.
multiple layer selection
non destructive erasing (Masks?)
more key commands
- redo hotkey mapper to match csp
- same key multi tool stack toggle

motion blur, smudge, gaussian blur?

gaussian blur modes
- full image
- focal point (with handle for center, handle for end, and handle for ramp between the two) these 3 handles should appear on a line, with a circle outline around the full radius

faster fill tools / layer agnostic fill

speech bubble - option to change default line and fill color
set a text preset as the default?

multiple layer select?


complex hair brushes. how do they do it?
clipping masks
- how do i want to handle them?

Object features
- outline support
	- has a color, thickness.
	- no anti-aliasing, global anti aliasing toggle?



## more tweaks
- dragging assets into folders isn't working (bug)




## New feature - repeating texture editor
- add an "asset settings" ribbon menu that only exposes when an asset is open.
- this lets you change the name of the asset
- in this asset settings menu, include a button that lets you toggle to enable repeating texture mode. this creates a root object called the repeating texture object.
	- now, instead of dragging 
	- while in repeating mode, 2 vertical tabs should appear (below the scroll window popout) that say "tile edit mode" and "source asset mode".
- https://repper.notion.site/Repper-Help-Centre-044964e7366843bcaa1ac04cdef96b59
- 
## New Feature - Image Imports
image import support
 makes a new object under the currently selected object in the same parent. images are a new "image object" type. they have by default a free transformable (or uniform) 8 handle box around, behaves the same as it would in a raster layer.
 - image objects show image object properties in the selected object properties panel (what i'm calling the raster/vector object / layer properties panel now)
 - different ways to import images
	- file > import
	- paste from clipboard (control v, changeable hotkey)
	- drag file in from file explorer
	- drag in file from web browser or any other source (like what pureref does)
- dragging operations support dragging into shapes to preview what they would look like in that shape, when released get put into the correct shape based on that (same behavior that asset library uses)
- right clicking an image and adding it to the asset library defaults the name to the original filename (but still in the same "name asset" popup window, so the user still has a chance to change the name before saving it)

## Bugs
- trying to free transform a raster layer crashed the program
	- also, changes made to the 8-handle cage around the transformed raster/vector object should stay so you know how you transformed it, until you deselect the object (instead of resetting the transform bounds position)
	- also, vector objects should be able to free/uniform with 8 handles just like raster objects do.
- trying to delete the last point in a new, unconfirmed shape crashes the program.
- Text selections, and the text cursor,  i think work how they should, but are not visible in the text field like text cursors and selections normally are.
- if gizmos or handles are off-canvas but still onscreen, they should still be visible. (currently they are not)
- in shapes, if the stroke, outline, or combine mode shape gizmos block a point handle ,it wont let me select or move that handle. make those handles have priority, and if a shape gizmo overlaps a handle, try moving it intelligently away from that handle.


## UI overhaul / tweaks
- holding control while click-dragging a corner smoothness handle in a rect object should force all 4 corners to copy smoothness of the handle being manipulated.
- move the opacity slider to a row at the top of, inside of, the object/layer outliner.
	- put the "visible" toggle there too
- the space between the top row window and layer/object outliner should be able to move up to cover up elements, but introduce a scroll bar if this happens
- move tablet navigation, reset view, and snap to grid to the top row, to the right of the hotkeys button
- use iconoir icons to replace the toolbar buttons with buttons that show an icon, and show the name of the command upon hover.
	- https://iconoir.com/
	- that includes all of the tools and options that are normally on the tool bar (except the ones I just moved)
		- add an "add page" button
- remove the "delete selected" , add page, and add layer buttons from the outliner.
- make the full page preview/scrollbar ui element instead a collapsable to the right of the drawing canvas element, that is collapsed by default.
- where the scrollbar is currently (but thinner) is where the new, smaller, tool buttons will live, in a verical column that can be click-dragged at the right side to grow it horizontally right. if large enough, it should show both the icon and tool name simultaneously. if too narrow, the buttons should stay the same height but auto width, hiding the text when not all of it can fit, and minimal width is the width that would make the icons square.
- where the tool settings vertical menu currently is, instead put the ribbon menu there but make it vertical. all the ribbon menu switching buttons should now be vertical on the right side of the new ribbon location. text should be right to left but rotated 90 degrees on this menu. of course, instead of scrolling horizontally, it should now scroll vertically.
	- this will give the canvas more room.
- remove the "layers and objects" header with the popout and x button.
- instead of having "raster object settings" and "vector object settings" be in a separate window from layer settings, just have the layer settings window switch to showing raster object or vector object settings, depending on the selected object type. 
	- this way, when eraser or raster are selected, the ribbon can default to tool settings instead of raster/vector object settings
	- remove raster/layer object settings from the ribbon, but all of their settings should instead be where layer settings are now, when a raster or vector object is selected.

## new shape creation type - free shape
- this shape is a freely drawn shape, unlike the add shape's point by point method.
	- when released from the user-drawn lasso preview, creates a filled shape with calculated bezier and vector points that best estimate the shape the user drew. this includes sharp points and bezier points, and handles and lengths for them calculated as closely as possible, with a sensitivity slider visible on screen. also, include a toggle to switch between creating a filled shape or an open bezier (or auto, which closes it if the user ends the shape close to the start point of that free shape). remember which was chosen - that will be the default the next time the user makes a new free shape.
	- preview assumes it is a free shape. however, while the end of the shape is close enough to the start, preview a fill for the shape that assumes the shape would be closed (semi transparent fill). upon release, ask the user in a popup if they want to close the shape. if they hit yes, then close it.
	- points/handles should not snap to the grid during free drawing or free drawing shape estimation.
	- enable/disable pen pressure for stroke thickness

## New feature - repeating texture mode

## New Feature - custom repeating frames
- just like godot has.


## Dont forget
- object/layer outer outline
	- in drawing object:
		- appears in object settings
		- draws an integer px thickness outline around all pixels that have a drawn-on value
		- lets you set the color of the outline too. since it has an alpha channel that can control the opacity for us
test the fill tool dumbass (vector and raster drawing)
(can you free transform 8 handle a shape?)
- asset library



## image sync tweaks
- render region box on screen
- adjustment layer - allows transforms and modifiers simultaneously
- includes uniform, free, etc


what are the fill types / textures
- repeating pattern
- brick texture
- different noise types
- screentones
	- tone size, dynamic, texture-able?
	- parameters - if it has a value from 0 to 1, it can be set to a "texture"
- support having a mask
- dynamic mask - glow around, inside an object?
	- gradient ramp support
- glow object with a child that represents the fill (but what about scale / parameter support?)



transformations
- cage transform
- puppet transform
- distort with texture
- [x] blur
- smear
- [x] hue, saturation, lightness