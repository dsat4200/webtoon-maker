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
		- like panels kind of under the hood, but only support having linear text, always have a text layer on top, with either gradient, solid, or repeating pattern (or a combination of these) layers underneath as the background.
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

## more tweaks:
- snap to grid should apply to handles while translating them, if snap to grid is on
- add a "delete point" gizmo to the selected point. it should be a circle with an X in it.
- completing a shape or confirming a line when using the add shape tool should automatically select it and switch to shape edit tool.
- created shapes should by default have a 4px black outline, enabled.
- translating a raster layer lags, but translating its handles does not.
	- remove the undo tranform button, since we have the undo feature in the program already
- roundness on non-locked beziers doesn't look right. it should go between smooth and sharp, and clicking the gizmo should enable/disable roundness for that point.
- ![[Pasted image 20260728173737.png]]


## more tweaks
- terminology note: a gizmo is a handle that only appears when a point is selected.
- bezier roundness value still looks like shit. it has this "bubble look" when neighbors are also beziers. this should be a smoothing, ROUNDNESS value, not a "bubbliness value". fix this, even if it means under the hood roundness is calculated differently for beziers than for vectors (because it looks fine for vector points) see the attached image for help.
- make the gizmos for bezier handles larger than the other gizmos, its getting confusing.
- when i use add shape to create a filled shape, that filled shape's default fill should be white not black.
- object select should be able to select shapes if the user clicks on or near one of their borders. this should select the layer.
- when a shape is selected, it should switch to shape edit automatically.
- for some reason, smoothness shows 2 gizmos that slide in and out, but only one does anything. the one that doesn't do anything should be hidden. what the fuck is it supposed to do?
- smoothness/roundness gizmos should vanish entirely if the point selected is a locked bezier (as in, the mode where the handles move in sync as if on a continuous line)
- raster layer's width/height box should always be visible and while the raster is active.
	- in raster edit, a click drag outside the W/H should extend the box and draw, but a tap/click should be for selections.
- in tablet mode, dragging horizontally is moving in the opposite horizontal direction
- pinch zooming feels horrible. sometimes when i do it it jerks the canvas to a different position. zooming in or out always snaps and doesn't preserve how zoomed in i was before.
- layer outliner isn't letting me drag a raster layer into being the child of a page. this is a bug.
- the shapes dropdown works but it SHOULDN'T be a button. you know how the layers outliner has dropdowns for children? the shapes dropdown should look sorta like that - a plain text line with the dropdown icon that when clicked, expands. NOT like a button that glows when expanded.




## even more tweaks:
- releasing after erasing should re-calculate the width/height of the raster layer.
- this recalc (and that of expanding the width/height) should have a safety margin around the "true" calculated bound, that way it's not pushing up against the strokes you're drawing.
- changing the order of objects in the outliner shouldn't force collapse the layers like it currently does.
- settings for hotkeys should include a check that lets you "hold". if this is enabled for that hotkey, holding it switches to that tool temporarily and goes back to the tool you were on when released. simply pressing the button and releasing without holding should switch to that tool still.
	- additionally, hotkey mapping should allow modifier keys on their own, or combined with another key.
	- currently, trying to add a hotkey adds an extra. Instead, it should replace the existing one.
## UI tweaks
- the text object popup is too big. move align stuff to a popup that appears if you press a button called "align" in the text settings popup instead.
	- additionally, if strict to parent is selected, the transform options for the text should be hidden (since they don't do anything in that case)
	- if in transform mode, double clicking inside the text bounds should re-activate text edit mode
	- move text size, bold/italic, and kerning to the same row in that order.
	- put layout setting and the align popup menu button in the same row
	- aligning should still be possible in free transform mode, just that instead the aligning should be relative to the bounds of the text object's transform rect itself.
		- instead of showing a row for name, the name of a text object should just be the first 16 characters.


## new feature: compound shapes (done)
- compound shapes - a toggle for a shape that allows it to be able to be made of a composite of the shapes that are its children
	- can be any shape, including non-filled line/curve shapes.
	- the full compound shape (parent of the compound-contributing children) still has visible toggle and fill, and other properties.
	- each layer with shape child of a compound shape can either be additive, subtractive, or ignored by the overall shape (if ignore, its children should ignore their parent's parent compound shape too, that way nested compound shapes are possible)
	- when in shape edit mode, clicking another shape should select it, exposing its own handles (handles of other shapes on the layer not visible by default - handles of only the selected shape should be visible).
	- the full outline and fill are of the compound shape with addition and subtraction of the shapes. that means intersections shouldn't have outlines inside, only the outside the combined fill.
	- for strict text objects, they still work as before, fitting to their parent. 
		- note that text objects and raster objects can be children of the parent compound shape, or any of their shape/shape children. however, include a toggle to switch between referencing the direct parent, or specifically the closest above compound shape layer parent in the heirarchy. default to just the direct parent, and hide this option if there is no compound shape parent.
	- by default, a layer isn't a compound shape (unless enabled in shape edit options)
	- if a compound shape is selected and a new shape is made, it should be made as the child by default.
	- if a compound-contributing shape child is selected, and a new shape is made, it should be a sibling by default.
- in the a compound shape settings, there should be a "flatten" button. This button takes all that information from the child compound shape contributors and "flattens" them all such that their children become direct children of the compound shape, and the full compound shape gets "compiled" into one shape, made of beziers and vector points and whatnot. This single flattened shape is no longer compound, but looks exactly the same. the shape would have to be calculated from the compound, with new points made where intersections were and whatnot.
	- if a child text object was previously strict positioned to one of the compound children, it's instead converted to free position, so its position is preserved.

## More shape tweaks (done):
1. stroke thickness and outline thickness should have sliders. their px values should be integers.
2. when selecting an end point of an open shape, the other two gizmos besides handles and delete freeze up or error out when selected.
3. the toggle gizmo should be labeled with text that shows what the handle type is currently for the selected handle.
4. ![[Pasted image 20260729180630.png]]
5. the round cap type is currently broken, displaying like the attached image.
6. hovering over a gizmo should tell you what it does
7. make all gizmos and handles for shapes 150 percent larger than they currently are
8. selecting a bunch of gizmo options, especially for ends, is buggy, leading to issues where i can't modify handles or shapes once it occurs. below is an example of an error i'm getting:
```
ValueError: Error calling Python override of QOpenGLWidget::mousePressEvent(): Malformed incoming Bézier handle at contour 0, point 0 (b489dc4062a24c1c81c4c2e477b206f7)
Error calling Python override of QOpenGLWidget::event(): Traceback (most recent call last):
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\ui\canvas.py", line 2819, in event
    return super().event(event)
           ~~~~~~~~~~~~~^^^^^^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\ui\canvas.py", line 2691, in mouseDoubleClickEvent
    super().mouseDoubleClickEvent(event)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\ui\canvas.py", line 2638, in mousePressEvent
    self._tool_press(event.position(), 1.0)
    ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\ui\canvas.py", line 3229, in _tool_press
    and self._begin_shape_edit(point, allow_interior=False)
        ~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\ui\canvas.py", line 3844, in _begin_shape_edit
    self._model_before = self.chapter.to_dict()
                         ~~~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\core\models.py", line 1229, in to_dict
    "layers": [layer.to_dict() for layer in self.layers.values()],
               ~~~~~~~~~~~~~^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\core\models.py", line 607, in to_dict
    "bound": self.bound.to_dict() if self.bound is not None else None,
             ~~~~~~~~~~~~~~~~~~^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\core\models.py", line 539, in to_dict
    self.validate()
    ~~~~~~~~~~~~~^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\core\models.py", line 468, in validate
    raise ValueError(
        f"Malformed incoming Bézier handle at {location}"
    )
ValueError: Error calling Python override of QOpenGLWidget::mouseDoubleClickEvent(): Error calling Python override of QOpenGLWidget::mousePressEvent(): Malformed incoming Bézier handle at contour 0, point 0 (b489dc4062a24c1c81c4c2e477b206f7)
```


## Vector object tweaks (done)
1. when i draw a vector shape, i can't see the strokes on screen. I think this is a visual bug, because shape edit still lets me select the strokes and points.
2. the boundaries between tool and scroll bar, and between ribbon and canvas should be draggable to customize width/height of these UI elements.
3. instead of being in the ribbon, color settings should be another tab of the color window in the bottom left.
	1. speaking of which, the colors section in the left doesn't currently show the color picker we made - it's just blank
	2. the boundary between this and the tool bar should also be draggable to scale up/down the windows in this column.


more tweaks (not done)
- stylus should be able to click popup menu options.
- like in raster pencil, if you tap the stylus outside the width/height of the vector object (without dragging), it should instead select whatever object you clicked
- raster/vector object layers should have a toggle in their floating settings menu that allows them to ignore the shape mask (lets them do stuff like pop out of the canvas, is useful for organization). 
	- layers should also have this toggle.
	- by default though, this toggle is disabled
	- children of an object with this toggle are also affected.
	- they still respect
- in text edit, if mode is free transform, show 8 handles to change the text bounds manually.
	- if a text object is a child of the main shape in a compound shape, and strict is on, there should be an option to strict fit to the main shape, or to strict fit to the full compound shape. the default should be to the main shape (currently its only doing it to the full compound shape)
- in text edit, trying to transform both with line handles and point handles sometimes only lets me do one or the other.
## New feature - Vector Layers (done, ultra)
- add vector layers from clip studio paint.
- https://help.clip-studio.com/en-us/manual_en/180_layers/Vector_layers.htm
- however, we only want certain core features for now.
- these are the features we want
	- vector layers - here, we'll call them "vector drawings" instead, since they are objects, not layers.
	- rename raster pencil and raster eraser to just pencil and eraser. they act as vector pencil and vector eraser if the current layer is a vector layer.
	- vector pencil
		- like a normal pencil, but after releasing, creates the vector points (like in CSP)
	- vector eraser
		- has the erase modes - stroke, point, and intersection
	- vector edit
		- appears if you click "shape edit" tool while a vector drawing is selected, but is its own tool under the hood
		- lets you select strokes in a vector drawing
		- choosing this shows circle handles for each point in the selected stroke. dragging these lets you move them. vector tools ribbon menu shows options to adjust parameters.
	- vector tools dropdown -  only shows when a vector object is active. contains the following. if i mention "tool settings" for a vector tool, those should appear in the vector tools ribbon page, in their own column labeled with what the name of what they do.
		- redraw vector thickness/opacity (not the adjust opacity feature from the webpage. this is its own thing)
			- lets you draw over strokes in the current vector drawing to use your pen pressure to set parameters for the closest points as you draw (doesn't change the position of anything though)
			- tool settings should give you options to increase, decrease by flat amounts, or set them all to uniform
			- toggle between either thickness mode or opacity mode, options stay the same but affect the select mode type.
			- toggle between point select and manual redraw mode
			- if using the vector edit tool, this redraw column of the ribbon should still be visible, and instead apply the adjustments from these tool settings to all selected strokes, or if none are selected, all strokes.
		- connect vector line tool
			- works like in clip studio paint
			- no tool settings that i can think of for now
		- simplify vector line tool
			- tool settings should let you adjust how much simplification occurs
			- again, tool settings should persist in shape mode and apply to either the selected stroke or all strokes.
			- drawing over happens like in clip studio paint (when this tool is selected)
			- 
		- don't add any other features from the webpage.
	- new fill tool
		- fills using the currently active primary color.
		- contextual. acts as a vector fill in a vector object, a shape fill that sets a shape background color or a shape outline color (if the user clicks the shape's border or near the shape's border using it)
		- fill tool in vector mode is pretty complex. refer to the following for what it should support. https://help.clip-studio.com/en-us/manual_en/420_fill/Fill_Tool.htm?rhhlterm=fill%20tool&rhsearch=fill%20tool
		- don't implement fill tools i havent explicitly mentioned below, even if they are in the CSP documentation. 
			- new object type - vector fill object
			- if a vector drawing is selected, and the user attempts to fill a space but there is no current vector fill shape underneath, create one as a child of the vector drawing, keeping the vector drawing selected
			- treat vector fills as always "up to vector path"
			- a vector fill object is essentially a vector filled shape, but as an object, that has no stroke but has a fill color. it's points / beziers are based on the vector drawing strokes around where the fill was attempted, with the "close gaps" and other options included.
			- if the user attempts to fill a space and there is a vector fill object there already, instead of making a new one, change the color of it (as fill should) and also recalculate its fill shape again based on the current vector drawing strokes around it, with the current fill settings too.
			- drag to fill multiple should work as usual
			- clicking a vector drawing stroke with the fill tool should not change its color, in case you were wondering.
			- include clip studio paint's "enclose and fill" tool (only for the currently active vector drawing object as usual)
		- for now, fill should only refer to the currently active shape or object, for simplicity.
		  
	- color palette
		- add a row window above the canvas, that acts like the ribbon from microsoft word (however, it's a long, horizontally scrolling one, if we end up adding enough features to require it). there will be "tabs" that let the user switch between different ribbons, such as one for color, and later one for other options, but just keep in mind for later that more will be added. when a tab is selected, the ribbon will change to showing its corresponding options.
			- add a "color pallete" column to the color ribbon menu that lets the user create a color palette with a color picker to modify colors or add colors. palettes can be added (+ button), removed (- button), and selected. don't have a rename button. instead, show a text field that displays the name, and clicking it lets the user enter the new name or change the current one. palettes save automatically when modified.
			- the palette should show each color as a square swatch in a grid.
				- clicking a swatch brings up a popup color picker that lets the user change this color.
				- see the attached image for what this color picker wheel should look like. additionally, include an alpha channel slider (vertical, showing to the right of the square but still inside the outer hue wheel) this alpha slider should look like a linear gradient rect where the top is the current color at 100 percent visibility and the bottom is at 0 percent opacity, with a grey and white checkered background behind said gradient. there should be a dark grey line 6px in width that marks the selected opacity, and lets the user drag it up and down this gradient slider to set the alpha of the color.
				- create this swatch in such a way that we can reference and use it later.
				- ![[Pasted image 20260729205056.png]]
			- there should be a dropdown to let the user switch between their saved palettes.
			- for now, this section should only take up bout 1/5th of the visible width of the ribbon menu (an estimate, not an actual dynamic scaling)
	- color picker in the main window
		- in its own row section under the tool picker, add the color picker wheel we described earlier. this color lets the user pick the currently active primary and secondary colors.
		- by default, these are black and white
			- as such, the new default initial colors for shape creation fills and outlines will be primary as the outline and secondary as the fill, instead of black and white.
	- ribbon menu ribbons
		- instead of a floating tool settings menu, make a "tool settings" ribbon tab that changes the ribbon menu to show contextual tool settings based on what's the current tool (currently just the pencil/eraser/fill)
		- there should be a "vector tools" ribbon that shows vector tools if a vector drawing is selected. this vector tools ribbon tab is only visible when a vector drawing is selected, and doing so switches the ribbon to this tab automatically.


## Brush size panel


## Custom brushes
- airbrush
- paint brush
- g pen vs pencil?
- blend brush / blur?


## new shape creation type - free shape
- this shape is a freely drawn shape, unlike the marquee tool.
	- when released from the user-drawn lasso preview, creates a filled shape with calculated bezier and vector points that best estimate the shape the user drew. this includes sharp points and bezier points, and handles and lengths for them calculated as closely as possible, with a sensitivity slider visible on screen. also, include a toggle to switch between creating a filled shape or an open bezier (or auto, which closes it if the user ends the shape close to the start point of that free shape). remember which was chosen - that will be the default the next time the user makes a new free shape.
	- points/handles should not snap to the grid during free drawing or free drawing shape estimation.
	- enable/disable pen pressure for stroke thickness







## Layer Modifiers
- have their own shape
- apply to either a layer and its kids, or the whole canvas (how are we attaching these)
- effects:
	- fragment shader
	- warp, twist, blur, noise maps, musgrave, stucci, voronoi, wood
	- maps that affect how much the effect is applied to the pixels below
	- brick texture?
	- can have gradient for how much they apply (radial or otherwise) or solid fill
	- film grain


impact effects
- shapes can do this




## Down the Roadmap - 3D Questions
Priorities
- fast, intuitive, performant
- easy to import, ideally with live sync from blender.
- integrates with asset library
- supports poses
- has opacity function
- supports warping / lenses, manga perspective
- intuitive popup tools for common edits
	- ex: hand one where each finger is a slider vertical for open close, but you can expose each slider for each sub-digit if you choose, can also splay
	- lets you draw across the sliders to set FAST.
- IK/ragdoll like transform mode (like cascadeur)
- floor snapping
- 3d scene importing
- simple toon shader with shadow map support?, flat shader, outline support. (convert blender shaders to glsl?)
	- color shadow map support for different scene colors, like evening night etc, instead of using colored lights for such things?
	- do some more shader research
- outline research
	- semi transparent outlines, color outlines, outlines around invidividual objects, outlines that automatically get smaller the further away from the object they are, inverted hulls.
	- 3d gizmo for distance ranges?
	- custom 3d gizmos that map to shape keys on an object! like moho smart bones!
- motionbuilder-style human and hand definitions




## Symmetry Rulers?


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