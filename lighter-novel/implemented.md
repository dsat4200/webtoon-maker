tools
- [x] add an eyedropper icon in the color picker (with hotkey setting). for now, samples from the whole image.
- [x] new item in the color window besides picker and pallette - color history
- [x] add the option to hide transform handles while drawing to pencil and eraser tool in tool settings. 
- [x] add a transform tool for vector/raster that exposes the handles, and click-dragging or pen tap dragging in the bounds translates instead of drawing while in transform mode.
- [x] add a "reset rotation" tool that only works as a hotkey. it should keep the scale and position navigation the same, but reset rotation of the canvas.
- [x] line gradients with 2 points shouldn't have the 8 handle transform around them (its redundant)
- [x] while having point selected in point select mode, pressing delete should delete the points you have selected.
- [x] the click and drag handle outside a shape to translate it doesn't seem to work.
- [x] add an export to png button that exports the current chapter to a full size png image. (add a folder where assets and chapters are called exports. the image name should 
- [x] translating a vector by the handle on the bounds is drawing instead of translating
	- translating should also be visible live (like raster does)
	- 
	- ## more
- [x] remove the add fill tool. instead, the fill tool and add fill tool should be consolidated into one visibly. the add fill functionality (changing the background fill of a shape) should only happen if the shape is the currently active selected object.
- [x] add the ability to right click a layer and click "show as mask only". this sets the layer to only appear in masks and not visually in the canvas on its own. however, if currently selected, it should show as normal for the sake of editing. these layers should have opacity say (percent) " - Mask only" 
	- since opacity of a mask affects its alpha (0 to 1 mask value), if a mask has changes to the opacity slider on itself (or a mask on itself), that should affect its mask output contribution.
- [x] fill tool in a raster object should let you fill only what's inside the active selection (in a raster layer). if a vector layer is the current active selection, fill shouldnt be visible as an option.
- [x] vector fills are currently a little bugged. remove the vector fill code and instead, make fills like they are in clip studio paint, where a raster layer has to be the one to store the fill
- [x] https://help.clip-studio.com/en-us/manual_en/420_fill/Fill_Tool.htm
	- plan to implement all these features of the fill tool. tool settings should be in tool settings. Make sure to do so with optimal performance
	- https://help.clip-studio.com/en-us/manual_en/420_fill/Advanced_Fill.htm
	- implement the advanced fill tools too.
	- reference layers should have its interface
- [x] instead of gradients living in the tool settings gradient tools tab, "gradient" should be a tool, and its settings should be in tool settings instead of graident tools. Keep all the features though.
- [x] add a "show as mask only" button to settings in the top right of objects. gradient should show its own settings in the top right window, instead of parent shape settings. it should also show the name, ignore direct parent mask, and show underlay settings, like raster and vector do.


- [x] add a trash button to the left of the hide button in the opacity row. if multiple objects are selected, it can delete multiple. delete should prompt the user if they are sure. 
- [x] instead of "lock opacity" with a checkmark, make it a lock icon button that is either pressed (on) or non-pressed (off)


- [x] add a toggleable mask button with a mask icon to the right of the opacity slider (thats either pressed or not pressed). if pressed, the object is set to view as mask only (the feature that's currently only visible by right click)
	- instead of "mask only" text, put that show as mask only icon toggle button to the right of the opacity percent value, that's either pressed or unpressed.
- [x] change the simple checkbox on each object to a toggleable eyeball icon button.
- [x]  show as mask only button icon should be different from the use mask / toggle mask mode button. use https://iconoir.com mask-square icon.
- [x] current tool tool settings while a gradient is selected should show the settings,, instead of just showing them below the gradient
- [x] add a delete operation, where, if delete hotkey is pressed (just as a hotkey for now), if on a vector layer or raster layer, it deletes everything on that layer (without asking for confirmation)

bugs:
- [x] text move handles 
- [x] vector edit point preview in a scaled object shows the non-transformed points. also, make the actual point icons about 20 percent smaller, and make their size and transparency settable in tool settings with sliders. also include a toggle to show or hide them, and they should be hidden by default.
![[Pasted image 20260816003707.png]]
- ![[Pasted image 20260816190558.png]]
- the color wheel shows the wrong hue.

later
- multi object select support
- lasso fill support
- MODIFIER STACK?
	- for instance, smudge tool could have parameters and strokes. the strokes could be manipulated?
	- params:
		- insensity
	- modifers can be moved between objects
	- a modifier can apply to multiple objects and act as a modifier to any object type (acting as an overlay, perhaps like a shader almost?)



- [x] modifier strengths and parameters (anything with a slider that has a min and a max pretty much) have a box to the left that let you attach a "mask" to them
- [x] a mask is a tone map - black and white, 0 and 1 (and everything between, greyscale) that maps to the intensity of said parameter. 
- [x] masks are a selected list of objects, along with an extra selection of pixels, almost like a raster layer, that are added together. an object's alpha channel is what contributes to the mask, pretty much (an alpha of all its pixels). masks can later be used for other things. 
- [x] for instance, the mask of one object can be set to the opacity slider of another - effectively re-creating a "mask to x layer" option. 
- [x] add a new "masks" tab (the layer settings window should now be tab-based, with layer settings as one of the tabs). masks show as a grid with icons of each mask and their name. top row above that should be a mask new, rename, and delete option.
- [x] when a mask button is clicked, the outliner shows (with light green) which objects in the outliner are selected as contributers to that mask. if the object is linked to a saved mask, the program should switch the top right window to the masks tab and show that mask as being selected. 
- [x] while in mask select mode, selecting objects adds them to the mask. they can also be deselected
- [x] being in mask mode changes the canvas temporarily such that everything currently on it becomes 10 percent opacity, but the current mask is visible on screen in greyscale full opacity, as a preview that you can also raster draw on for that aspect of mask creation.
- [x] if no saved mask currently exists, selecting a mask from the mask tab while in mask mode sets that mask as the mask for the parameter you wanted.
- [x] any parameter that has the mask feature enabled should have a button to the left of the slider that appears "pressed" if a mask exists, but not if none exists. it should have a sort of "contrast" icon - a circle with a black side and a white side, and it should be orange if on, grey if not on.
- [x] examples of things that should support masks
	- any parameter in blur, or hsl
	- the newly introduced outline modifier
	- the opacity of any shape/vector/raster,blender comic object, etc.
		- this should apply to the opacity slider between the layer settings window and the outliner. dragging a layer into this box sets it as the mask (for easy "mask as" operations)
			- as such, clicking an object only selects it after being released. that way, you can drag into another object's opacity mask (or other places) without automatically switching out of that object.
- [x] hovering over a mask box should show the object's mask in the comic view




- [x] ## synced image support
- [x] use an image that streams itself from blender, actively reloading when refreshed every second?
- [x] use a literal actual blender window, displaying it below our window somehow.
- [x] outline modifier
	- [x] draws outlines around pixels in an object (or objects, if linked)
	- [x] has a thickness parameter
		- a mask button (circle with half filled, half not filled).
		- supports masks (like the rest do)
		- for instance, say you drew a "shape" by drawing some raster or vector strokes in a circle
		- you could then add an outline modifier and change the thickness with a mask. then, altering the original drawing would still show the outline around in real time.
	- [x] has an opacity parameter
		- do the same masking thing with the opacity parameter.
- [x] masks can be spontaneously made for a specific instance/case, or saved.
- [x] saved masks prompt you for a name and are added to the masks tab. the masks tab only shows masks for that chapter.
## prompt
- [x] i still cant drag layers to move them between layers with the stylus
- [x] dragging the eyedropper should show a circle icon above where you're clicking/dragging that displays the selected color
- [x] tool options shouln't extend further right, they should wrap to another row beneath if they're too long.
- [x] allow for selecting multiple objects. doing so lets you drag them to move multiple up and down layers. it also changes the visible tools to only ones that work with multiple objects. for now, that should only be a transform tool (doesn't appear if objects other than raster, vector are selected)
- [x] for now, multi select should only let you select rasters and vectors
- [x] add a new tab to the vertical list of options on the left. this tab is for "modifiers"
- [x] modifiers act like a shader or like a modifier in blender, or an adjustment layer. they aren't visible in the outliner - being attatched to an object. modifier settings should show an "add modifier" button that exposes a list of addable modifiers. for now, the only modifier should be hue/saturation/lightness, and blur)
    - [x] hsl should have those 3 parameters
    - [x] blur should have a strength, and 2 modes - full and focal point. full does the whole object/objects. focal point has gui handles (orange)
        - [x] handle for center, handle for end, and handle for ramp between the two) these 3 handles should appear on a line, with a circle dotted outline around the full radius. translating the center moves all the handles with it.
- [x] because modifiers are non destructive, drawing, erasing, updating blender objects, etc works like normal, and the modifier will reflect any changes correctly (non destructive and parameterized)
- [x] modifiers should have their own row/ui element like they do in blender. they appear in a stack that applies from top to bottom, like blender. they can be click dragged from their title to reorder (each modifier should have a title on top, like in blender)
- [x] modifiers should all have an "intensity" value that appears first on the list. this a horizontal slider from 0 to 100 percent that adjusts how much a modifier is applied.
- [x] the only modifiers that should appear are ones that exist in the selected object (or in all selected objects if multiple are selected)
- [x] all modifiers should also have a square chain (link) icon in the top right. when in link select mode, highlight in orange in the outliner which object or objects this modifier is attached to. selecting/deselecting objects or shapes in the outliner adds/removes those from the link state of that modifier. clicking the link button again in this mode toggles things back to normal.
- [x] when you click add modifier, it should add it to all the currently selected objects, linked. linked modifiers means that the same modifier data/parameters persist between its duplicates in other objects/shapes.
- [x] transform tool should also appear if a shape is selected. this should move the shape and everything in it (unlike manipulating the handles normally, which just edits the shape bounds and doesn't modify the objects within.
- [x] click dragging slightly outside the 8 handles of a shape should let you translate it. currently, this isn't working./
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