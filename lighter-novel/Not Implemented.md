## New modifier - tiling modifier
- exposes a gizmo that is a shape. dropdown in modifier settings lets you change the shape (different tiling types).
	- each of these shapes is a "tile" that basically lets the user "cut" a specific part of the image layer the modifier is on (everything but shape objects for now)
	- the tile gets repeated using various methods.
	- for now, just do a triangular tile, a square tile, and hexagonal tile.
	- the tiles can be translated, scaled (uniform only for now) and rotated.
- when tile modifier is on a raster or vector layer, it causes drawing things outside of the tile to draw where they would be on the tile itself (a looping / repeated edge effect). this would make it easier to draw seamless repeating textures.
- when tiling is on
- when tile modifier is on a shape, all its children should be affected, and the looping tile drawing effect should happen to its child raster and vector objects too.
- tile modifier acts as a fill essentially, filling up to the borders of the parent shape.

## new modifier - posterize modifier
- takes the object it's on and maps all of its hues to a hue range with an axis for hue and another for how often it appears
- visible in a circular map, draft of the UI below. 
- like a coloramp in blender sort of - each hue range can map to another color (clicking each color icon opens up the expected picker we made)
- color ranges can be created and removed, and their handles can't overlap. handles move around the circle when click-dragged.
- when first added, asks how many colors to posterize to in a popup window, then automatically creates ranges based on the amount of colors. the range color selections in this case start off as simply the average color of the range found, but of course after initialization it's all user-editable.
![[Pasted image 20260907122039.png]]

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

- add an "asset settings" ribbon menu that only exposes when an asset is open.
- this lets you change the name of the asset
- in this asset settings menu, include a button that lets you toggle to enable repeating texture mode. this creates a root object called the repeating texture object.
	- now, instead of dragging 
	- while in repeating mode, 2 vertical tabs should appear (below the scroll window popout) that say "tile edit mode" and "source asset mode".