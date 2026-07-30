## Gradient tool / objects:
- add a gradient object type with its own creation tool
	- dropdown that lets the user create a shape fill gradient, a linear/curve gradient, or a circle/ellipsoid gradient. these are parameterized objects that are the child of a shape
	- these gradients would fill the screen, but they don't when the child of a page or shape, instead masking beneath that shape like any other child of a shape.
	- add a "gradient tools" ribbon menu that appears whenever any object that is the child of a shape is selected, or whenever a shape is selected (so pretty much all the time). 
	- if a gradient object is selected, automatically switch the ribbon to this menu.
	- in the gradient tools ribbon menu, add a column labeled "create gradient" which lets the user create a gradient as a child of the currently active shape, but at the bottom of its child heirarchy (if there exist children in that shape). there are different types of gradients that dictate how the gradient map is applied, as follows below:
	- line/curve gradient:
		- lets the user create an open shape - exact same controls as making a an open shape  in the current "add shape" tool (except without the ability to close it)
		- has a start point and an end point. the gradient transitions itself along that curve.
		- colors extend beyond the start and end, as a flat fill of their corresponding color.
	- circle/ellipsoid gradient:
		- lets the user click and drag to create a circle - same as the circle tool
		- has handles to change the origin point and radius
		- has a gizmo that lets it switch between circle and ellipse
		- if ellipse on, has 2 handles for radius, and another handle for rotation
		- gradient transition occurs from the outside edges inwards. beyond the outside of the circle is purely just the color of the end of the gradient.
	- create shape gradient:
		- has no boundary-defining gizmo (besides the gradient center mentioned later)
		- the boundary is instead defined by the parent, where the start is the borders of the parent shape and the end is the gradient center point.
	- gradient center gizmo (not part of the ribbon, instead on the canvas)
		- exists in circle/ellipsoid gradient and in shape fill gradient
		- has a gizmo that lets you move the "gradient center" which is where the gradient transitions toward. it defaults to the center of the shape and follows changes in the shape, but if this gizmo is moved manually at some point, it doesn't follow those changes. double clicking/tapping this gizmo if its "unlocked" resets it so it goes back to the centerpoint and follows like normal
	- add a "Gradient parameters" column in the ribbon that shows a rect box that is a live gradient from left to right, with handles that allow you to add and remove colors. the handle should be a box with the current color of the handle and a triangle pointing up at the gradient. clicking and dragging this handle moves it across this linear spectrum. double clicking it bring up the same color picker we use in the bottom left (make sure it also shows the primary and secondary current colors as pickable options).
		- plus button to add a new gradient point, minus to remove the selected one.
		- basically, the same controls as blender's colorRamp node.
		- this is a "gradient ramp" type of data. keep this separate, as its something we may need to call for later features.
		- since all shape types can the same gradient map, switching shape types for the current gradient should keep the same user-set gradient map
		- include a dropdown to load different gradient maps, save them, rename them, add and remove.
		- gradient ramp presets are saved in a per-series basis.
		- each gradient object has its own parameters and gradient ramp of course, so changing the values of one doesn't change any other gradient objects
			- this also means that loading a gradient preset and changing it doesn't override it until you hit save.
			- this means that even if a gradient ramp preset has changed, this change doesn't propogate to any other gradient objects that use that preset, unless the user clicked that gradient, opened the dropdown, and reselected that gradient ramp, which would load the new data. treat loading as loading those values onto the current gradient ramp.
- gradients may be used later as maps for other things, so make the structure for gradient data and creation generalizable. instead of all gradients having color fill data, make the gradient we just created a subtype of gradient that specifically does fill colors. the gradients parent structure should have all the shape creation/gizmos/ribbon thing, and type of gradient should be an attribute of that structure.
- make sure to use best practices for programming patterns when designing how gradients work.



- same thing but for speed lines


more tweaks (check)
- [x] stylus should be able to click popup tool menu options (currently not working)
- [x] sweep simplify should also show a circle outline around the radius so you know which points you select. drawing with sweep simplify should show as if you're drawing with a circular brush, as a transparent orange overlay. the points under this selection are what is simplified when you release the pen from the canvas. (which then also turns off sweep simplify.) it shouldn't simplify the whole stroke, just those points underneath.
- [x] if you have points selected from a point selection mode, "apply" in simplify should only apply to those selected points (and their nearest neighbor if on a line segment)
- [x] change thickness/opacity should change automatically to point select mode if a selection is made.
	- change thickness/opacity amount/target should be a slider, (either 0 to 100 in opacity mode, or 0 to 40 px in thickness mode), with the value also  settable manually to the right. 
	- in opacity mode, slider should snap to integer percent values, for every 5%.
	- in thickness mode, snap to integer px values
	- remove the up,down buttons from this value since we have the slider.
	- pressure maximum value should be hidden in point select mode interaction.
	- pressure maximum value should also be a slider with manual entry to the right, hiding the up/down buttons.
- [x] the points in the stroke should still be visible during sweep simplify and after pressing use simplify, so the user can tell what's going on
- [x] removing from selection should remove the deselected points from the selection, not the whole stroke.
- [x] currently, shift to add to selection is broken because shift rotates the canvas. override this rotate hotkey when in this mode, for now.
- [x] the plus, minus should show on the cursor even when hovering, not just when actively selecting something.
- [x] ignore direct parent is currently unintentionally broken. selecting this for a shape should make it and its children appear as if they are on top of their parent shape - that's what i meant by ignoring the mask

## more tweaks (not done)
- [x] if i try selecting an object or layer in the outliner with my pen, it "sticks" and starts trying to drag. this is a bug
- [x] disabling a shape from being visible by unchecking the outliner doesn't let me re-enable it
- [x] ignore direct parent mask should allow a drawing to show itself as if it were "on top of" the shape / unaffected by its bounds (even while remaining a child). think of it like "always on top"
- [x] shift clicking to select multiple strokes or control clicking a stroke isn't modifying the selection if more than one is selected - this is a bug
- [x] if a stroke is already selected and you shift click it in stroke select mode, deselect it.
- [x] vector selection tools should show all handles for the current active shape, as blue handles.
- [x] in stroke select, you should be able to draw lasso to select multiple strokes at once (only if not hovering over a stroke already)
	- [x] stroke select should also support shift, control to add, remove from selection in both its lasso use and hover/click full stroke use case. however, in stroke mode, this should a pply to full strokes not just individual points.
	- [x] holding control or shift should show the +, - icon next to the cursor in stroke select mode as well.
- [x] selecting a page's borders should switch to shape edit mode.
- [x] the maximum for outline width for a page should be 40px. the slider's beginning and end should be adjusted accordingly.
- [x] new shapes for a page should default to a 4px thick border when created.
- [x] rotating the selected points should keep the 8 transform handles in the same place as during the rotate, even when releasing. they should only reset back when changes are made to which points are actually selected. currently releasing after a rotation recalculates the 8 handles to continue being an unrotated box around them, which is incorrect.
- [x] the handles / gizmos for new page shape creation shouldn't respect shape masking (for clarity)



## Extra tweaks
- [ ] trying to close a custom shape during page creation also isn't creating a shape or closing. as a refresher, below is the requirements for the "add page" button
	- clicking "add page" should ask the user if they want to insert a page gap if there's a page physically below the current page already.
	- then they modify the page gap, press confirm page gap in a popup window, and then ask the user what shape they want to use to form their page's boundaries (same as the current insert page tool, but add the confirm/cancel popup to both the normal insert page gap tool and this use of it.)
	- closing the shape should CREATE the page layer with the user-inputted shape as the bounds.
- [ ]  once a custom shape type page is made, it's not letting me translate its points. it should allow this (this may be a bug)
- [x] hex row his should be between primary/secondary and below the wheel/picker
- [x] operations that let you select should let you select other pages as well, instead of just things on the same page.
- [x] instead of a transform tool for raster drawing objects, use the 8 handle system we have, and put the popup that normally appears in the tool in the ribbon instead. call that ribbon "raster object settings". include the transform type column here too "transform settings". move all the raster object settings from the popup to this new ribbon in the first column labeled "object settings"
- [x] if in a drawing select mode, tool settings in the ribbon should let the user pick either uniform or free transform. (this is working, but instead move it to vector tools ribbon in its own column called "transform settings"
- [x] like pencil/eraser modes, if in a drawing select mode and not dragging, let the user click outside the w/h of the current active drawing object to select another object.

## New Feature - show underlay
- [x] if a vector/raster drawing is selected, add a slider to the object settings floating window to change the opacity of hidden parts of the drawing (those that are being clipped by shapes or are under shapes) such that it can show those parts above temporarily so you can see what's underneath. it should be a slider.
	- [x] when doing this, opacity should reduce for the parts of the image that are still within the boundaries of the drawing, and show more visibly the drawing layer you have selected, but over top.
	- this opacity revealing effect of course is reverted when that element is no longer selected, and if that opacity effect is zero, the parts that are hidden appear hidden as normal.
	- remember the underlay setting per-object.


## New feature: Gradients

film grain? vignette is just a gradient preset

speed lines shape,like a gradient?
- has a ramp for thickness, a handle for density, set max/min thickness/opacity
- linear, radial, or shape-based like gradients do
- moveable center point.
crosshatching/speedline pencil?


## Bugs:
- [ ] now the raster pencil doesn't work. instead it just draws a dot. if the issue is that the interaction would translate, instead make the translation handle for the raster 8 handle system happen only when hovering/clickdragging around the outside of the width/height with a 20px outside margin.
## Performance issues
- pinch zoom is laggy
- make sure vector drawing is SNAPPY
- sweep simplify is super laggy
- transforming in stroke select mode is slow

## OG prompt
- add a drawing selection tools dropdown that has rect select, lasso select, and stroke select. for now, just make it work on only raster drawings or vector drawings - only allowing you to either select pixels or points. if in stroke select (which only appers on a vector drawing), hovering over should show a blue outline around the stroke you're over. clicking it selects all points on that stroke.
	- drawing selections should let you hold shift to add, control to remove, or nothing to replace. doing so should show either a plus, minus, or nothing next to the respective cursor you're on (rect, lasso, or pointer finger hand) clicking an empty spot on the canvas should deselect. when things are selected, show the 8 handles for transforming the selection, free and uniform. if the user hovers near enough to one of the 4 edges of this transform boundary, change the cursor to a move icon, clicking or dragging this should let the user translate their selection. add a rotate handle in the top middle above the top middle transform handle. add a pivot handle that defaults to the center of the selection. however, if the user modified the position of this pivot anchor (dragged it), then keep it the same even if they modify their selection. however, reset this pivot anchor the next time they deselect everything and make a new selection.
		- hovering over any of the 8 handles should turn the cursor into a pointing hand.
		- add these pivot, rotate, translate abilities to all instances of the 8 anchor transform happening, even in other tools
	- control a does "select all" which selects all on the current raster/vector drawing. this hotkey is changable in settings
- like in raster pencil, if you tap the stylus outside the width/height of the vector object (without dragging), it should instead select whatever object you clicked
- raster/vector object layers should have a toggle in their floating settings menu that allows them to ignore the shape mask (lets them do stuff like pop out of the canvas, is useful for organization). 
	- layers should also have this toggle.
	- by default though, this toggle is disabled
	- children of an object with this toggle are also affected.
	- they still respect their parent shape's parent shape though, if one exists.
- in text edit, if mode is free transform, show 8 handles to change the text bounds manually.
	- if a text object is a child of the main shape in a compound shape, and strict is on, there should be an option to strict fit to the main shape, or to strict fit to the full compound shape. the default should be to the main shape (currently its only doing it to the full compound shape)
- in text edit, trying to transform both with line handles and point handles sometimes only lets me do one or the other, this is a bug.