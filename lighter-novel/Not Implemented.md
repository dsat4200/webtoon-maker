## Raster/Vector object / pencil features
psd brush support?
- add brushes to tool settings ribbon (raster/vector pencil) (with preview of a small, curved stroke segment in a square live preview icon, from zero pressure to 100, swoop curve )
- outside of shapes can be treated as a stroke that supports psd brushes?

cage transform support
select support for raster mode
smoothing support
asset library folders.
merge layers
multiple layer selection
non destructive erasing (Masks?)
more key commands
- redo hotkey mapper to match csp
- same key multi tool stack toggle

motion blur, smudge, gaussian blur?

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
## more tweaks (next)
- visibility and opacity sliders should be pinned to the top of the outliner, but below the selected object properties row.
- text opacity is currently locking and wont let me change it (bug)
	- same with gradients, images, speed lines, etc
- remove speed lines feature. i didn't think it out very well
- asset library shouldn't have a column - just put all the assets there in a grid (items wide -  however many fit, vertical with scroll bar.) and let the user scroll vertically instead of horizontally to traverse them. also include folders. let the user click an "add folder" button at the bottom of the assets window to add a folder (plus folder icon), with a delete folder button next to it (trash icon), and the name of the folder (editable text field) between the two in that row (these operations only apply to the selected folder, only one folder selectable at a time)
	- folder icon should be, well, a folder icon, fitting the square icons of the rest of the items in the asset browser but with a visible 2x2 grid of the icons of the first 4 items in the folder, with a 8px margin between them. (and the name of the folder below the folder icon as well)
	- right clicking any asset or folder should let you rename or delete them as well.
## Image mode tweaks
- bounds should live update while dragging, intsead of doing it after release
- increase transform hover/drag area to include inside of image while it's selected
- if in pencil mode, click dragging 8-handle gizmos shouldn't draw.
- any transform that has 8-handle bounds should have a text gizmo with text label that lets you toggle between uniform and free transform. (be it vector, raster, free transform text, rects, circle/ellipse, or image)
	- in those 8-handles, any area that would let you translate an object should turn the cursor into an open palm. any drag operation should be a closed hand while dragging. 
	- make the rotation pivot a circle with a crosshair in the center. if you double click it, it should snap back to whatever center it should be in, and return to following that center until it's manually moved again.
	- hovering over circle-shaped handles (such as the main 8, or points in a shape) should turn the pointer into a crosshair.
	- if there's a corresponding free/uniform toggle in a menu somewhere, like the ribbon or the selected object properties panel, for a transform mode gizmo toggle you add, remove the non-gizmo one to free up UI space.
- pasting images and importing from the dialogue works, but dragging images in from file explorer or google images does not show preview and does not import (bug)
- selecting image objects while another object is selected should work like any other object (text, shapes). currently, clicking from outside like that (with the image under the cursor) is selecting the page when it should be selecting the image.

## New feature - repeating texture editor
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





## Performance issues
- pinch zoom is laggy
- improve performance for vector drawin
- sweep simplify is super laggy
- transforming in stroke select mode is slow
- shape gradient in reverse direction is very slow to update




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

