
## thought balloons and screaming
i still need to add border types for shapes, like round bubbly ones for thought balloons or pointy ones for screaming (or just have it be one with a sharpness value and sizing parameters... hmmm

- stroke types. 


## New modifier type - stroke modifiers
- new modifier category - stroke modifiers (for now, only works on shape objects)
- changes what the appearance of a stroke is along its points (does not modify the points)
- all have a "strength" slider from 0 to 100 that lets you change how much the modifier affects the stroke
- should be stackable with other stroke modifiers, and have high performance implementation.
- should apply in order, like modifiers should.
- visible within the same stack as other modifiers
- if applied to a closed shape, should appear seamless.
- parameters of stroke modifiers should still support masking.
- only appear visible / are only compatible with shapes and vector objects.
- do not work on open shapes (only closed)
	- does not apply towards compound shapes

## Stroke modifier - scream/thought
- makes it spiky. adjustable height/width of spikes.
- roundness controls how round the spikes are. at max roundness, basically a thought bubble
- parametric!

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


## Stroke modifier - Set brush
- lets the user change the brush used for that object via a modifier.
- works on closed an open shapes too.
- custom dot toggle.
		- how to handle maps? assets? icon pack? stickers?
	- angle range - slider that controls

brushes
- airbrush support

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

- add an "asset settings" ribbon menu that only exposes when an asset is open.
- this lets you change the name of the asset
- in this asset settings menu, include a button that lets you toggle to enable repeating texture mode. this creates a root object called the repeating texture object.
	- now, instead of dragging 
	- while in repeating mode, 2 vertical tabs should appear (below the scroll window popout) that say "tile edit mode" and "source asset mode".