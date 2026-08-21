

## Blender plugin tweaks
- add a "hide stream frame overlay" button.
- stream frame should only be visible/editable while in camera view. a comic frames' stream frame should be relative to the camera that its attached to
	- as such, panning the camera while in camera view should also look like its moving the frame (since its attached to the camera, and the camera box moves when you pan like this)
	- basically, because the stream frame will be camera-relative, basic navigation like panning tilting and zooming won't affect the final output.
- i dont think its happening right now, but correct me if i'm wrong. this is how the plugin should behave:
	- what should happen a view is selected is that that view becomes the active one - this loads everything about that comic frame state - which objects are visible, their positions, rotations, scales. poses should be saved/loaded with "whole character" keying set. intensity of lights, whether a collection is active or not, etc. effectively, a comic frame should be a save/loadable scene state in and of itself (though comic frame data itself doesn't contain any actual geometry, just information on the scene's state.)
	- that way, the user can switch between editing blender frames that are used as panels in the comic more easily.
	- "update" should only render out. save can handle saving.
	- if they dont already exist, add a save and load button that purely save and load comic frame info without rendering. this will make iteration easier.
	- if it doesn't already, revert should revert to the previous iteration of the comic frame (but not render). 
	- add a toggle "always hide overlays" that, if enabled, always hides blender's overlay before rendering.
	- reminder, this is blender 4.5.5 LTS.
	- if the user is in local view (the view that hides all but certain objects), and saves a comic frame, it should save that info as well, and the render should reflect what local view shows/hides.
- what does the revert button do?





error:
![[Pasted image 20260821185526.png]]
render failed - the rendered frame size changed during streaming. this happened to me when i wasn't connected but changed the frame overlay in blender, then reconnected and tried to relink the view and it wouldn't show the new render on the old comic. help me handle this error in the code so the user can do things like that.