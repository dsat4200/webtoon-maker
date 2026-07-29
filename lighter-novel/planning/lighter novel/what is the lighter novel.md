	()how to get reading to be fun again / how to make comic making easier again

simple. make an image every paragraph or so. infinite scrolling, dialogue boxes,  panels, whatever.




features:
- "characters"
	- have a name, color, sometimes a  temporary name (for funny moments), dialogue, dialog boxes, mafybe even a font or word effects, and a word bubble preset
- audio support for when you reach a certain part (unimportant)
- the ability to compile to images for webtoon consumption
- ability to "save" assets - this can be any object.
- dynamic font size support
- looping background tiling creator for simple backgrounds (and basic tiles) (tile boxes exist)
- basic gradients
- very firm grid snapping for my autistic soul.


how it works
- everything is a box. text goes in boxes, images go in boxes, panels go in boxes, etc. boxes snap strongly to a grid. as such, every image has bounds. 
	- these bounds can be expanded or shrunk. 
	- images in panels use the bounds of their parent as a mask.
	- the panel's bounds can be formed by a box, poly-shape, etc.
- text
	- "text presets" - let you choose a default size, font, etc
	- word art type text - text that follows a path / curve that you can draw at first and it'll try to match the curve you draw. if it's an arc, just a bezier with 2 ends and a point between. if it's got more bends, those are added. of course, bends are math'd out with sensitivity too for initial calculation. can be modified later by selecting the wordart and modifying.
- lenses
	- act almost like warp surfaces over top. imagine the warp tool but it applies to a specific area, and that warp can be moved over the image wherever, like a lens on a page distoring anything underneath - images, text etc.
	- mesh/cage/free transform, bulge, shrink, twist, , etc. these have their own properties but also bounds that control what part of the image they warp.
- layers
	- the layers window itself is dynamic, only showing what's on screen or very close to being on screen. panels are treated as groups
	- there can be multiple objects in a layer
	- layers can have names
- transform
	- free transform, mesh / cage transform
- objects
	- parameterized objects such as boxes, lines, gradients, etc. those are a thing.
	- dialogue boxes, closed shapes (like text boxes)
		- like panels kind of under the hood, but only support having linear text, always have a text layer on top, with either gradient, solid, or repeating (or a combination of these) layers underneath as the background.
- "vector" layers - like csp
	- different eraser types - intersect (which also creates points to make the appearance of a fill), whole line, or point-based.
	- points can be edited directly but rarely ever are
	- dynamic vector fill is also a thing - where a group of strokes joins...
		- select multiple strokes (lasso?) or maybe just a fill bucket? maybe draw a lasso near the strokes that would be the fill? to create a "dynamic shape" (like moho) which dynamically finds what the shape's points when you try to fill it. it would be and uses that space as a mask for its layer (almost as if it was a panel in and of itself). that filled space can be a layer or a group of layers. 
	- clicking a vector shape selects it, showing its bounds.
	- what if we get rid of layers entirely? and instead they are fully WSYWIG?
- edit image in external program feature. (include layer for edited image and one for the full thing?)


## Prompt 1
this is a spaced repetition drawing software. make a copy of it in a new sibling folder that strips out everything pracitice-related, and only includes the "drawing" parts - the canvas, tools (except reference point tool), eraser, grid, etc, stay.

this will be a standalone software, which uses parameter-ized objects and tools to create a hybrid vertically-scrolling webtoon / web novel style comic. for now, we are only touching the important parts we'll need to build a strong foundation for an extensible drawing program.

the "core" - what to keep
- pencil/eraser (now renamed to raster pencil/eraser)
- the canvas navigation controls

simplification / modification
- tablet mode just enables/ disables the touch / pinch / drag navigation controls.
	- remove the floating tablet mode tool bar
- in layers panel, remove up/down arrows, instead add click to drag as the main method of reorganizing layer order.
- remove sessions as a concept. remove music feature, basically only keep free drawing mode.

Core concepts:
- "layers"
	- instead of traditional layers in a drawing program, layers in this program always have "bounds". these act as a mask that objects within that layer must conform to. This is to avoid layer clutter in a vertically scrolling format.
		- layer bounds are themselves parametric shapes with points. there's a box bound, a circle bound, and a a poly-shape bound (made of 3 or more points that create an enclosed shape. making the bounds should be as simple as selecting a bound type and clicking and dragging for the rectangle or circle, or clicking points for the poly-shape. regardless of bound type, there should be a "snap to grid" option, and the bounds should be modifiable post-drawing. Since they act as a mask, this should be a non-destructive action.
	- layers can contain other layers. they can also contain objects.
	- layers inherit the grid's parameters from their parent by default, but can be over-ridden.
	- "page layers" are layers that are at the root of the layer heirarchy. they also have bounds and don't act any differently so far (other than that a page layer can't be the child of another layer.)
- "objects"
	- objects must have a non-page layer. multiple objects can be on a layer.
	- objects are parametric, with GUI on-canvas controls, and there are many different types of objects
	- text objects show text. each text object can either be grid-aligned (to the vertical rows of the grid of the project) or layer-aligned (centered to the bounds of the layer that contain the text)
		- they can have a font, a size, be bold, italic, and have kerning
	- "raster" objects are where the pencil and eraser tool are used, to draw. reminder, like text, they also are masked by the bounds of their immediate parent layer, but can and often will have data that isn't visible but still exists (non-destructive is key)
	- layers and objects can be hidden if you choose to do so. objects each also have their own opacities, but by default should be locked to the parent's opacity (this locking behavior can be toggled on and off)
	- in general, an object's options appear in a floating widget right above the object vertically, when the object is selected.
	- each object, when selected, dynamically shows or hides tools in the toolbar. For instance, pencil becomes un-choosable when you have a text box selected.
		- some objects change your tool to a default tool (such as selecting a drawing layer changes you to the pencil tool). but, not all of them have this behavior.
	- objects can be selected with the new "object select" tool.
		- the object select tool can only see objects on the same layer you have selected in the layers panel (unless you have "page" checked in the tool setting, in which case it can select any object in the page layer you are working on.
- the "canvas"
	- the canvas is, in reality, simply the visible window of the full image your working on (since it's a vertically scrolling comic).
	- as such, it should snap to the nearest pixel to avoid subpixel problems.
	- to scroll the canvas, add a "scroll" bar to the left side of the screen.
		- this scrollbar should show a very low-def preview of the full "chapter" you are working on, with a box around the part you're currently editing acting as the scroll handle. we want this to be live, so using the scrollbar should be responsive and low-latency.
- "series"
	- a series contains chapters. a chapter is essentially a project you have open.
	- series have their own folder which contains these chapters
	- series have a name.
- "assets"
	- this is something we can get into later, but for now, just plan ahead when you're working on the structure you use for objects and layers. make the assumption that in the future it'll be possible to save certain layers as "assets" which can later be re-used for things like re-occurring word bubbles, visual-novel style dialogue boxes, polyshapes, raster layers as stickers, text presets that get re-used, etc.
- assume the width of a project is always 1080 pixels for now.
(note: told it to write the full plan in the broad strokes for a solid foundation, then later i'll tell it to just plan out the first steps)


## Run 2: tweaks
- clicking and dragging inside the bounds in bound edit should translate the bounds.
- left and right pan are currently inverted in tablet mode.
- layers should have names and be re-namable
- add the free/uniform transform tool that the original SRS python had, in a non-destructive when possible manner (8 handles, one per corner and one per edge, each translatable, click drag in the center to translate the full box, uniform and non-uniform scale modes, with toggleable handle grid-snapping)
	- these operations should be possible on text in a way that lets you still edit the text afterwords, and should be possible on raster layers but in this case destructively is fine, but include an undo transforms button that undoes the raster transformation to how it was pre-transformation (like drawing srs python did)
- alignment rework - text
	- clicking inside the bounds of the currently selected text object should let you enter text live on the canvas, as if typing in a normal text box in a word processing program. if doing this, hide the "text" field in the text settings popup menu. do this with a new "text edit" tool that is selected by default when a text object is selected.
	- instead of having grid and layer align, text should be movable with the transform tool in a "free" mode (either snapping or not) which has handles to free transform or uniform transform, OR text should be "strict" and be confined to the bounds of its parent layer, using those bounds for wrapping and having a margin. in this mode, there should be a 3x3 sort of grid that lets you choose how it aligns relative to its parent bounds as a box (top, middle or bottom vertical alignment, left middle or right horizontal alignment.) it should wrap to this box, still allow for return characters, and expand/contract this automatic text bound relative to the bounds of the parent layer for extra adjustment.
- the current active layer (most immediate parent layer of the current object) should have an orange dotted line for the bounds.
- for some reason, every other layer/object in the layer/object outliner has a white background instead of a black one. instead, layers should have a dark grey and objects should have a black background in this window.
- switching to object select should hide whatever object edit popup window that was on screen before.
- if an object or layer is selected in any tool unless otherwise specified (for instance, don't do this in raster pencil or eraser), single clicking a certain radius outside the bounds of the current layer (without dragging) should revert to selecting the current page and switching to object select mode. (select in page should be the default)
- while editing an object, that object should *remain* highlighted in the outliner.
- if an object is selected, clicking "bound edit" should select its most immediate layer parent and switch to bound edit.
- instead of "box bound, circle bound, polygon bound" buttons, create a "new bound" button that reveals these three options.
- in the first row, right aligned in the "text" popup editor, show a dropdown with loadable text presets the user has created (along with a default one that can still be overwritten).  there should be a save (S) button, a rename (R) button, a remove (X) button, and a (+) add button for these presets.
- add an "add text" and "add raster" button to the left side tool panel that adds those to the current active layer.
- since selection of objects will ideally mostly be done by object select mode, by default adding a new object to a layer should keep the parent layer collapsed in the outliner instead of expanded for ease of use. however, if it's already expanded, keep it so.
- instead of just greying out the "raster pencil" and "raster eraser" tools when unavailable, hide them from the toolbar entirely.



## Run 3 - more tweaks
- preview scroll bar is vertically stretched, this is incorrect.
- transforming a text object or raster object, or modifying an object's parameters while in transform mode (or any other mode), shouldn't immediately switch back to its corresponding mode after completion (currently, transforming a text object will switch back to text mode after transforming, or modifying some params of a raster layer will switch back to pencil, and more examples. this is incorrect.)
- if text is being edited currently, ignore hotkeys that use the letter keys or shift.
- selecting "add raster" should show a tooltip asking the user to do a box select - this will set the initial width and height of the raster (the blue lines can be distracting when starting to draw).
	- bound edit when raster is selected should transform it's width and height. this is separate from the transform functionality.
	- this way, clicking outside the raster width/height box of the current raster image while a raster is selected (even in pencil/eraser mode) will let the user select other objects (this way raster drawing tools aren't the exception)
	- however, if the stroke started within the width/height and it begins to extend outside the width/height, it should expand like it currently does.
- from drawing srs python, re-add all the the pencil setting options that existed previously, with the pencil settings, pressure settings, and user-creatable presets it had.
- reference drawing srs python for the transform tool's performance - it feels much slower here than it did there when scaling raster layers.
- its hard to tell what object is currently being edited in the outliner. the blue overlay that appears when an object is selected in the outliner should stay visible,  even if the user clicks away from the outliner being focused.
- - in object select mode, when making a selection, if multiple objects exist below the cursor, default to the one that has the higher layer order.
	- however, if control is held while object selection occurs, show a small popup that lets the user select which of the objects under the cursor to choose.
- if text edit is selected but no text object is currently selected, show the bounds of all current text objects on screen. from there, clicking one of them will select it.
- fill layers fill the bounds of the parent layer and don't have bounds of their own.
- layers can now have "borders" with a thickness, border color, and a vertex radius.
	- layers can also have a fill color.

## Tweaks
raster object tweaks:
- while hovering or drawing, hide the height/width of a raster layer, but if the stylus isn't hovering over, show the height/width if the raster layer is selected (or if in object select mode)
	- while you're at it, show the text bounds in object select mode too.

- tool settings should still be selectable with the stylus.
- it should be possible to make or move a raster or text layer such that it's the direct child of a page layer.

more tweaks:
- snap to grid should apply to handles while translating them, if snap to grid is on
- add a "delete point" gizmo to the selected point. it should be a circle with an X in it.
- completing a shape or confirming a line when using the add shape tool should automatically select it and switch to shape edit tool.
- created shapes should by default have a 4px black outline, enabled.
- translating a raster layer lags, but translating its handles does not.
	- remove the undo tranform button, since we have the undo feature in the program already
- roundness on non-locked beziers doesn't look right. it should go between smooth and sharp, and clicking the gizmo should enable/disable roundness for that point.
- ![[Pasted image 20260728173737.png]]

new feature: compound bounds/shapes
- since bounds are now shapes, i'll refer to them interchangably in this section.
- compound bounds - a toggle for a bound that allows it to be able to be made of a composite of the bound shapes that are its children (also layers should be able to have children / new bound should work while a layer is already selected to create a child layer)
	- can be any shape, including non-filled line/curve shapes.
	- the full compound shape (parent of the compound-contributing children) still has visible toggle and fill, and other properties.
	- each layer with bound child of a compound bound can either be additive, subtractive, or ignored by the overall shape (if ignore, its children should ignore their parent's parent compound bound too, that way nested compound bounds are possible)
	- when in shape edit mode, clicking another shape should select it, exposing its own handles (handles of other shapes on the layer not visible by default - handles of only the selected shape should be visible).
	- the full outline and fill are of the compound bound with addition and subtraction of the shapes. that means intersections shouldn't have outlines inside, only the outside the combined fill.
	- for strict text objects, they still work as before, fitting to their parent. 
		- note that text objects and raster objects can be children of the parent compound shape/bound, or any of their shape/bound children. however, include a toggle to switch between referencing the direct parent, or specifically the closest above compound bound layer parent in the heirarchy. default to just the direct parent, and hide this option if there is no compound bound parent.
	- by default, a layer isn't a compound bound (unless enabled in bound edit options)
	- if a compound bound/shape is selected and a new shape is made, it should be made as the child by default.
	- if a compound-contributing shape child is selected, and a new shape is made, it should be a sibling by default.
- include a new shape creation type - free shape
	- this bound is a freely drawn bound, unlike the marquee tool.
	- when released from the user-drawn lasso preview, creates a filled shape with calculated bezier and vector points that best estimate the shape the user drew. this includes sharp points and bezier points, and handles and lengths for them calculated as closely as possible, with a sensitivity slider visible on screen. also, include a toggle to switch between creating a filled shape or an open bezier (or auto, which closes it if the user ends the shape close to the start point of that free shape). remember which was chosen - that will be the default the next time the user makes a new free shape.
	- points/handles do not snap to the grid during creation.
- the text object popup is too big. move align stuff to a popup that appears if you press a button called "align" in the text settings popup instead.
	- additionally, if strict to parent is selected, the transform options for the text should be hidden (since they don't do anything in that case)
	- if in transform mode, double clicking inside the text bounds should re-activate text edit mode
	- move text size, bold/italic, and kerning to the same row in that order.
	- put layout setting and the align popup menu button in the same row
	- aligning should still be possible in free transform mode, just that instead the aligning should be relative to the bounds of the text object's transform rect itself.
		- instead of showing a row for name, the name of a text object should just be the first 16 characters.


## Gradient tool, colors:
- add a gradient tool that exposes handles on the canvas the user can use so set key positions of gradient controls such as 
	- radius, center, midpoint of a circular/ellipsoid gradient
	- position of the start, end and midpoint for the curve of the transition between colors of a linear gradient
- the popup tool settings for gradients should let the user create presets for and points for the gradient ramp of these gradients, along with saving, naming, etc.
	- essentially, these are the gradients from clip studio paint.
- add a a "color panel" that lets the user create a color palette with a color picker to modify colors or add colors. palettes can be saved, renamed, deleted, and selected

- layer modifiers?

most important parts first
- vector layers, raster layers, pencil and eraser
- point manipulation
- basic parameter-ized shapes (box, polyshape, circle, free)
	- the ability to create layers (which themselves have bounds) to use inside these shapes as masks/panel groups or not
- boxes/bound/polyshape panel creation - as bounds of their own layer. support having a stroke around though.
- the grid
- text objects (can be in any layer)
	- can either be center-aligned (fit to the vertical and horizontal center of the bounds of the layer they're on)
- layers 
	- have bounds
	- can have many objects within them, that mask to those bounds
	- can also themselves contain layers.
- panels (solid color bg for now)
- character 

full wizzy or not


pros:
- layers can be fucking convoluted and annoying to deal with, and i hate keeping track of them.
- it'd be more like blender if i could just click the thing.

cons:
- layers give you a lot of control that's super useful.
- more complicated to plan software this way.