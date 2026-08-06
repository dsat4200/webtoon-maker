## Gradient tool / objects:
- [x] add a gradient object type with its own creation tool
	- dropdown that lets the user create a shape fill gradient, a linear/curve gradient, or a circle/ellipsoid gradient. these are parameterized objects that are the child of a shape
	- these gradients would fill the screen, but they don't when the child of a page or shape, instead masking beneath that shape like any other child of a shape.
	- add a "gradient tools" ribbon menu that appears whenever any object that is the child of a shape is selected, or whenever a shape is selected (so pretty much all the time). 
	- if a gradient object is selected, automatically switch the ribbon to this menu.
	- in the gradient tools ribbon menu, add a column labeled "create gradient" which lets the user create a gradient as a child of the currently active shape, but at the bottom of its child heirarchy (if there exist children in that shape). there are different types of gradients that dictate how the gradient map is applied, as follows below:
	- [x] line/curve gradient:
		- lets the user create an open shape - exact same controls as making a an open shape  in the current "add shape" tool (except without the ability to close it)
		- has a start point and an end point. the gradient transitions itself along that curve.
		- colors extend beyond the start and end, as a flat fill of their corresponding color.
	- [x] circle/ellipsoid gradient:
		- lets the user click and drag to create a circle - same as the circle tool
		- has handles to change the origin point and radius
		- has a gizmo that lets it switch between circle and ellipse
		- if ellipse on, has 2 handles for radius, and another handle for rotation
		- gradient transition occurs from the outside edges inwards. beyond the outside of the circle is purely just the color of the end of the gradient.
	- [x] create shape gradient:
		- has no boundary-defining gizmo (besides the gradient center mentioned later)
		- the boundary is instead defined by the parent, where the start is the borders of the parent shape and the end is the gradient center point.
	- [x] gradient center gizmo (not part of the ribbon, instead on the canvas)
		- exists in circle/ellipsoid gradient and in shape fill gradient
		- has a gizmo that lets you move the "gradient center" which is where the gradient transitions toward. it defaults to the center of the shape and follows changes in the shape, but if this gizmo is moved manually at some point, it doesn't follow those changes. double clicking/tapping this gizmo if its "unlocked" resets it so it goes back to the centerpoint and follows like normal
	- [x] add a "Gradient parameters" column in the ribbon that shows a rect box that is a live gradient from left to right, with handles that allow you to add and remove colors. the handle should be a box with the current color of the handle and a triangle pointing up at the gradient. clicking and dragging this handle moves it across this linear spectrum. double clicking it bring up the same color picker we use in the bottom left (make sure it also shows the primary and secondary current colors as pickable options).
		- plus button to add a new gradient point, minus to remove the selected one.
		- basically, the same controls as blender's colorRamp node.
		- this is a "gradient ramp" type of data. keep this separate, as its something we may need to call for later features.
		- since all shape types can the same gradient map, switching shape types for the current gradient should keep the same user-set gradient map
		- include a dropdown to load different gradient maps, save them, rename them, add and remove.
		- gradient ramp presets are saved in a per-series basis.
		- each gradient object has its own parameters and gradient ramp of course, so changing the values of one doesn't change any other gradient objects
			- this also means that loading a gradient preset and changing it doesn't override it until you hit save.
			- this means that even if a gradient ramp preset has changed, this change doesn't propogate to any other gradient objects that use that preset, unless the user clicked that gradient, opened the dropdown, and reselected that gradient ramp, which would load the new data. treat loading as loading those values onto the current gradient ramp.
- [x] gradients may be used later as maps for other things, so make the structure for gradient data and creation generalizable. instead of all gradients having color fill data, make the gradient we just created a subtype of gradient that specifically does fill colors. the gradients parent structure should have all the shape creation/gizmos/ribbon thing, and type of gradient should be an attribute of that structure.
- [x] make sure to use best practices for programming patterns when designing how gradients work.

## Gradient tweaks:
- [x] add a gradient ramp preset that can't be changed or deleted but can be loaded - primary to secondary
- [x] add a "swap primary/secondary" button to the main color picker, in the same row. make it a square button with a refresh icon
- [x] shape gradient has an option that when enabled, instead of using a center inside, points the gradient outwards, with a gizmo for the distance outwards until it reaches its maximum ignoring the masking of its parent (but not its parent's parent). 
- [x] performance on shape gradients is abyssmal - any time a gradient is updated, be it by moving the center or modifying the ramp, it runs super poorly. find ways to optimize this so it runs as close to realtime as possible.
- [x] gradient colors can have transparency, but currently they aren't showing on the canvas (transparency should work)
- [x] the circle gradient gizmos have their own fill, which is blocking me from seeing the actual gradient underneath. these should not have fills and should show the gradient that's being modified.
- [x] the same happens with line/curve for some reason, and the handles aren't selectable, instead they make a point? super bugged. the line/curve creation and editing should be exactly like making a new open shape with the shape tool, with the same adding of points, gizmos, handles, etc.
- [x] in shape mode, it's not showing any option to translate the shape - this should be available.
- [x] gradient tool columns in the ribbon menu are too wide. make them about 60 percent the width they currently are.
- [x] in "create gradient" column in gradient tools ribbon, if the user has a shape selected that has a gradient, expose a "select gradient" button
- [x] linear gradient curve positions matter - they should be the path along which the gradient color transitions.
	- [x] add a gradient ribbon menu column called "gradient type parameters" that, if a linear gradient is selected, exposes the following options:
		- follow direction: 
			- parallell: the gradient transition occurs from first point to last point
			- perpindicular: starts from the line and extends outwards on one side of the line. exposes setting for the distance this ends at from the line.
		- toggle: reverse direction
			- in parallell, transition occurs from last to first
			- in perpindicular, transition occurs from max distance from line towards the line.
	- if a shape gradient is selected (circle or shape-based)
		- reverse direction:
			- in circle, transition starts at edges of circle, ends at max distance value
			- in shape, forces the gradient to ignore parent mask. shows gradient outwards up to a max distance, starting from the shape bounds (like a glow effect)
			- in this case, gradient shows in canvas as if it's below the parent (parent on top) to avoid coloring over the parent or its children

## Gradient tweaks
- in ellipse and parent shape gradient modes, in gradient type parameters add a "uniform " toggle, that, when enabled, ignores the center dot and instead transitions to a certain distance from the bounds (parent bounds in parent mode, ellipsoid/circle bounds in those.)
	- instead of an "outward" value, rename it to "distance" so we can use it for uniform in non-reversed cases
		- in this case, it acts as if "inward" (current outward behavior still should work but just when reversed is enabled)
- line/curve gradient should have ALL the gizmos and handles that the shape version has, including handle type (vector/bezier), roundness, lock bezier handles, etc.


## Bug:
after creating a filled or open shape, it sometimes locks up and wont let me edit it. below is the error message i got
KeyError: 'Error calling Python override of QOpenGLWidget::mouseMoveEvent(): x'
Error calling Python override of QOpenGLWidget::event(): Traceback (most recent call last):
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\ui\canvas.py", line 6403, in event
    return super().event(event)
           ~~~~~~~~~~~~~^^^^^^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\ui\canvas.py", line 6107, in mouseMoveEvent
    self._tool_move(event.position(), 1.0)
    ~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\ui\canvas.py", line 9869, in _tool_move
    self._update_shape_edit(point)
    ~~~~~~~~~~~~~~~~~~~~~~~^^^^^^^
  File "C:\Users\hopper\Documents\webtoon-maker\comic_editor\ui\canvas.py", line 10815, in _update_shape_edit
    dx = target.x() - float(primary_start["x"])
                            ~~~~~~~~~~~~~^^^^^
KeyError: 'Error calling Python override of QOpenGLWidget::mouseMoveEvent(): x'

New Feature: 
- object/layer outer outline
	- in drawing object:
		- appears in object settings
		- draws an integer px thickness outline around all pixels that have a drawn-on value
		- lets you set the color of the outline too. since it has an alpha channel that can control the opacity for us
test the fill tool dumbass (vector and ras


## minor bug
- start.bat doesn't install PIL or pyside6 (it should)
## new gradient type: speed lines
speed lines, includes opacity too
	- supports different shapes
		- however, in line/curve mode, a draggable gizmo lets you set which direction the lines point (on which side of the line, sliding from one to the other)
			- another gizmo is a toggle with text label - lets you switch between whether the lines start at the ends and follow the curve as they go left/right, or whether they point up/down (as if pointing "normal" to the curve). basically, perpindicular or parallel, above or below, left or right.
		- in circle/ellipse/shape, the speed lines start at the outside and move inside towards the moveable center point, like the regular version
	- works the same as a color fill gradient, but with speed lines instead.
	- has a gradient ramp control the color/opacity (since colors have alpha channel)
	- a different ramp can control the thickness transition curve (just use the greyscale of whatever gradient ramp)
	- this means 2 of the full gradient parameters columns - one called "thickness parameters" and the other called "color parameters"
- another column called "impact line parameters" lets you set the following with sliders, enter-able parameters
	- density - how dense are the lines (this also means thickness is scaled relative so you dont have overlap)
	- gap - how much space between each line (none if zero)
	- close range - margin of how many px away from their expected end point they should actually end/transition to
	- randomness distance- what is the max distance a point's actual end point be from the expected end point for transition towards (thickness only)
	- randomness scale - how different should each line's end point be from it's immediate neighbors (think of it like affecting the scale of the noise used for the randomness operation, thickness only)
	- custom center shape/line (button)
		- this option creates a child object shape of the speed line gradient. this child can be deleted but not moved.
			- asks for which shape type (square, rect, free (closed only))
		- clicking this when one exists just selects it
		- exists or not, clicking this button selects it and switches to shape edit if not already in that mode
			- this lets the user modify the shape
		- what this center shape does is, instead of the lines ending at a point, they project towards the closest point on this internal shape's outside boundary, and end once they reach it, smoothly transitioning as they usually do, following the parameters from the ribbon.
		- if deleted, reverts to point-based center
		- only if the gradient object is a line, the shape type should be an open shape.
			- if the custom center is of a different open-ness type than its parent (open / closed), then it simply shouldn't have an effect.
			- don't delete the custom center if the gradient shape changes though.
	- shape version of this speed lines effect also supports the "outwards" toggle, which ignores the custom inner shape if one exists

## New feature: Asset Library
- "assets" are like prefabs in unity. they are presets made of a layer, its parameters, and its children and their parameters and so on, in a separate "library".
- like prefabs, assets under the hood are just projects of their own, but only containing the relevant layers/objects that compose the prefab (instead of a full comic chapter or pages)
- assets exist in this library with a thumbnail (a preview of what the asset looks like) and a name. they can be renamed, and double clicking them opens them up as if they were a project, letting the user modify the preset asset and save it (which also updates its thumbnail)
- to aid in this, add project tabs (sort of like what you'd see in photoshop or clip studio paint). these "projects" can be actual projects, or assets. these project tabs should show the name of the project that's open, an x button to close it, an asterisk after the name if it hasn't been saved recently. clicking a tab should well, open that project. however, projects that already have open tabs shouldn't be fully opening - they should already be cached / in memory, etc such that they don't slow down the current project but also switching between them is fast, like in any image manipulation program.
- additionally, add a ribbon menu titled "Asset Library" that's always visible as an option
	- this assets ribbon shows assets as thumbnails with a name below, in a row, that is scrolled horizontally.
	- right clicking a layer/object in the outliner should bring up a new right click menu. one of the options should be rename, to rename the layer/object)
	- another option should be "copy as asset". this brings up a popup asking you to name the asset. clicking OK then creates a folder for that asset which contains, essentially a project of its own, but with only that object and its children as they were when copied. this should also refresh the assets ribbon viewer and show its square thumbnail, which should have been rendered as a finished image, whose view is cropped to a fitted box around the bounds of the selected object


## Misc features
- [x] add a delete layer hotkey
- [x] text size, add gizmos for increasing, decreasing by 1, direct font size field gizmo that lets you change it by typing in after clicking.
	- text size should be integer, not fractional (at least not in the tool setting or gizmo interface)
- [x] include a toggle in tool settings to display the fonts in the font list dropdown in either their own font, each, or all using the same default font.
- [x] move the stuff thats currently in the text window popup to "tool settings" ribbon menu (all of it)
- [x] include bool, italic toggle gizmos
- [x] gizmos only appear when a text object is selected, and only apply to that text item
- [x] 2 handles that let you drag - circle handles, horizontal, attached to the right hand side of the text box, orange colored like gizmos
	- these let you adjust the font size and kerning.
	- font size handle goes from 10 to 100. (snap to integer)
	- kerning handle should go from 1 to 10 (snap to 1 decimal place)
- [x] also, there's currently a bug where i cant make the font size above 99. typed in manual values should be 250 max.
- [x] also, optimize the free transform feature of text layout so that it transforms smoothly in realtime.


more text tweaks
- [x] make the text gizmos twice as large.
- [x] add the ability to select text (and a select all hotkey, which selects all text) (light orange transparent overlay on selected text). typing when text is selected deletes that selected text and types.
- [x] add an "open recent" feature (file dropdown should contain new series, open series, and open recent, save, save as)
- [x] undo doesn't let me undo a shape creation (bug)
- [x] bug during shape editing, now the canvas is just grey and i can't change or open anything

## Raster/Vector object / pencil features
psd brush support?
- add brushes to tool settings ribbon (raster/vector pencil) (with preview of a small, curved stroke segment in a square live preview icon, from zero pressure to 100, swoop curve )

smoothing support


Object features
- outline support
	- has a color, thickness.
	- no anti-aliasing


bugs
- trying to free transform a raster layer crashed the program
	- also, changes made to the 8-handle cage around the transformed raster/vector object should stay so you know how you transformed it, until you deselect the object (instead of resetting the transform bounds position)
	- also, vector objects should be able to free/uniform with 8 handles just like raster objects do.


general tweaks
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


## New feature - repeating texture mode


## shape tweaks
- for shape creation, add a "finish" button gizmo that confirms the shape.
- adding a shape to a compound shape should be additive compound by default. it should affect the shape creation preview stage too.
	- assume the shape is an open shape during free shape creation unless closed (for preview purposes)
- these gizmos should be added to shape edit / shape creation:
	- if the shape is a compound shape: show a gizmo button with text that shows the type of compounding going on (add, subtract, ignore). clicking or tapping this button cycles between them. also, these set compound types should be previewable during shape creation pre-confirmation.
	- also, include a gizmo (similar to the text size or kerning text gizmos in appearance and behavior) that alters the thickness of the stroke, and another for the thickness of the outline, of the preview shape
		- (0px to 150px total for stroke thickness)
		- (0px to 100 px for outline thickness)
- closing the shape or double clicking the last point still also confirms the shape.
- confirming the shape also sets the preview parameters as the params of the new shape of course
- clicking and dragging the translate boundary in text free transform mode isn't letting me translate the shape. the 8 handle corners and edges let me transform, but not the dotted line to translate the whole text object.
- move the file dropdown to the same row as the undo/redo/etc
- the text selection should let me double click a word to select it, triple click to select all text in that box. hovering over text in a currently selected text box should change the cursor to a text insertion icon, and clicking and dragging should select from the click point to the release point (select characters) live, like any normal text input field in any program.
- make the text gizmos a little smaller (maybe 30 percent smaller), except the kerning/font size drag handles 


## Dont forget
- object/layer outer outline
	- in drawing object:
		- appears in object settings
		- draws an integer px thickness outline around all pixels that have a drawn-on value
		- lets you set the color of the outline too. since it has an alpha channel that can control the opacity for us
test the fill tool dumbass (vector and raster drawing)
(can you free transform 8 handle a shape?)
- asset library



## more tweaks (done)
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

## more tweaks  (done)
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




## Bugs:
- [ ] now the raster pencil doesn't work. instead it just draws a dot. if the issue is that the interaction would translate, instead make the translation handle for the raster 8 handle system happen only when hovering/clickdragging around the outside of the width/height with a 20px outside margin.
- [ ] where did pencil settings go? I had a pencil settings dialogue somewhere. it had some pretty precise adjustments. is it supposed to be under tool settings? i can't select tool settings while in raster or vector pencil (this must be a bug)


## Performance issues
- pinch zoom is laggy
- improve performance for vector drawin
- sweep simplify is super laggy
- transforming in stroke select mode is slow
- shape gradient in reverse direction is very slow to update

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


## Fill types - textures
what supports fill types?
- shape fills
- gradient colors


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

![[Pasted image 20260730190248.png]]