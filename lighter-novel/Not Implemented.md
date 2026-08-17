- [x] ## synced image support
- [x] use an image that streams itself from blender, actively reloading when refreshed every second?
- [x] use a literal actual blender window, displaying it below our window somehow.

## prompt
- i still cant drag layers to move them between layers with the stylus
    
- dragging the eyedropper should show a circle icon above where you're clicking/dragging that displays the selected color
    
- tool options shouln't extend further right, they should wrap to another row beneath if they're too long.
    
- allow for selecting multiple objects. doing so lets you drag them to move multiple up and down layers. it also changes the visible tools to only ones that work with multiple objects. for now, that should only be a transform tool (doesn't appear if objects other than raster, vector are selected)
    
- for now, multi select should only let you select rasters and vectors
    
- add a new tab to the vertical list of options on the left. this tab is for "modifiers"
    
- modifiers act like a shader or like a modifier in blender, or an adjustment layer. they aren't visible in the outliner - being attatched to an object. modifier settings should show an "add modifier" button that exposes a list of addable modifiers. for now, the only modifier should be hue/saturation/lightness, and blur)
    
    - hsl should have those 3 parameters
        
    - blur should have a strength, and 2 modes - full and focal point. full does the whole object/objects. focal point has gui handles (orange)
        
        - handle for center, handle for end, and handle for ramp between the two) these 3 handles should appear on a line, with a circle dotted outline around the full radius. translating the center moves all the handles with it.
            
- because modifiers are non destructive, drawing, erasing, updating blender objects, etc works like normal, and the modifier will reflect any changes correctly (non destructive and parameterized)
    
- modifiers should have their own row/ui element like they do in blender. they appear in a stack that applies from top to bottom, like blender. they can be click dragged from their title to reorder (each modifier should have a title on top, like in blender)
    
- modifiers should all have an "intensity" value that appears first on the list. this a horizontal slider from 0 to 100 percent that adjusts how much a modifier is applied.
    
- the only modifiers that should appear are ones that exist in the selected object (or in all selected objects if multiple are selected)
    
- all modifiers should also have a square chain (link) icon in the top right. when in link select mode, highlight in orange in the outliner which object or objects this modifier is attached to. selecting/deselecting objects or shapes in the outliner adds/removes those from the link state of that modifier. clicking the link button again in this mode toggles things back to normal.
    
- when you click add modifier, it should add it to all the currently selected objects, linked. linked modifiers means that the same modifier data/parameters persist between its duplicates in other objects/shapes.
    
- transform tool should also appear if a shape is selected. this should move the shape and everything in it (unlike manipulating the handles normally, which just edits the shape bounds and doesn't modify the objects within.
    
- click dragging slightly outside the 8 handles of a shape should let you translate it. currently, this isn't working./


## more
- fill tool in a raster object should let you fill only what's inside the active selection.
- fill tool in a raster object should act like it does in clip studio paint.
## image sync tweaks
- render region box on screen
- adjustment layer - allows transforms and modifiers simultaneously
- includes uniform, free, etc


transformations
- cage warp
- blur
- smear
- hue, saturation, lightness



bugs:
- wont let me drag select to reorder objects with the stylus, only with the mouse


tools
- add an eyedropper icon in the color picker (with hotkey setting). for now, samples from the whole image.
- new item in the color window besides picker and pallette - color history
- add the option to hide transform handles while drawing to pencil and eraser tool in tool settings. 
- add a transform tool for vector/raster that exposes the handles, and click-dragging or pen tap dragging in the bounds translates instead of drawing while in transform mode.
- add a "reset rotation" tool that only works as a hotkey. it should keep the scale and position navigation the same, but reset rotation of the canvas.
- line gradients with 2 points shouldn't have the 8 handle transform around them (its redundant)
- while having point selected in point select mode, pressing delete should delete the points you have selected.
- the click and drag handle outside a shape to translate it doesn't seem to work.
- add an export to png button that exports the current chapter to a full size png image. (add a folder where assets and chapters are called exports. the image name should 
- translating a vector by the handle on the bounds is drawing instead of translating
	- translating should also be visible live (like raster does)

bugs:
- [ ] text move handles 
- [ ] vector edit point preview in a scaled object shows the non-transformed points. also, make the actual point icons about 20 percent smaller, and make their size and transparency settable in tool settings with sliders. also include a toggle to show or hide them, and they should be hidden by default.
![[Pasted image 20260816003707.png]]
- ![[Pasted image 20260816190558.png]]
- the color wheel shows the wrong hue.



## Raster/Vector object / pencil features
psd brush support?
- add brushes to tool settings ribbon (raster/vector pencil) (with preview of a small, curved stroke segment in a square live preview icon, from zero pressure to 100, swoop curve )
- outside of shapes can be treated as a stroke that supports psd brushes?




later
- multi object select support
- lasso fill support
- MODIFIER STACK?
	- for instance, smudge tool could have parameters and strokes. the strokes could be manipulated?
	- params:
		- insensity
	- modifers can be moved between objects
	- a modifier can apply to multiple objects and act as a modifier to any object type (acting as an overlay, perhaps like a shader almost?)

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

