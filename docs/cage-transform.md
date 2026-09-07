# Cage Transform

## Cage workflows

Select one or more items and choose the **Cage Transform** grid tool:

| Selection | Result |
| --- | --- |
| Raster and/or Vector Drawings | Temporary preview; OK bakes every selected drawing in one undo step; Cancel discards it. No modifier is added. |
| Images, Blender images, and/or non-page shapes | Opens Modifiers and adds one linked cage to every selected target. Images retain original bytes; shapes transform their entire subtree. |
| Drawings mixed with other types, or unsupported items | Names incompatible items in a popup, marks them red in the outliner, and changes none of the targets. |

Adding any modifier or editing its links validates the complete selection
before changing targets. Cage modifiers cannot attach directly to raster or
vector drawings. Page roots and text containers are not cage targets.

The tool's settings and modifier card share the same controls:

- Horizontal/vertical lattice points: separate sliders and number inputs,
  2–16 on each axis. Changing density resamples the current deformation.
- Smoothness: 0–100, blending bilinear and interpolating cubic cage geometry.
- Image interpolation: nearest neighbor, bilinear, or bicubic. This controls
  image resampling independently of cage smoothness.
- Free/uniform scaling and horizontal/vertical flips.
- OK/Cancel, also available below the cage and through Enter/Escape.

Click a point to select it. Shift/Ctrl-click adds or removes a point;
Shift-drag on empty space selects a box. The outer eight handles transform
the selected points, or the entire lattice when none are selected. Drag the
outer frame to translate, the upper round handle to rotate, and the gold
pivot to change the rotation/flip center. Free mode allows individual corner
deformation; uniform mode keeps proportions. Flip gizmos use the horizontal
and vertical alignment-center icons.

Modifier selection is available in **Modifiers** mode. A selected card is blue
with a blue border, and shows its handles. Leaving the mode hides handles but
retains the selection. Deselecting and reselecting an object, or switching
project tabs, restores that object's remembered modifier selection. Pending
modifier edits commit when leaving the mode; unfinished destructive tool
previews cancel when changing selection or project tabs. Undo during an active
cage edit first cancels that edit.
Explicit Save, Save As, and Export accept the current cage preview before
writing, so the saved drawing matches the output. Autosave does not accept
an unfinished destructive preview.

## Rendering and performance

`gpu_textures.py` uses a private OpenGL 3.3 context, vertex buffer, source
texture, framebuffer, and shader for cage mesh sampling. It preserves the
caller's current context. Source uploads are reused when only cage points
change. Only the latest GPU source
texture and output framebuffer are retained; individual GPU outputs are
limited to 16 million pixels and 8192 pixels on either axis.

CPU fallback inverse-rasterizes the same mesh in 128-row strips, with
premultiplied alpha throughout. Larger interactive cage requests show a small
draft and refine through the existing single-worker, 64 MiB-budgeted queue;
superseded requests are canceled. Exports and baking use full-quality sampling.
Source and output LRU caches each retain their existing 64 MiB limits.
Very large results remain subject
to the renderer's allocation limit and may take longer through CPU fallback.

The destructive vector operation adaptively refits warped curves and tangent
handles; it preserves original anchor IDs and inserts extra anchors where
needed to represent curved deformation. Raster baking prepares all target
pixel replacements before changing the document. Undo restores both model
records and pixel resources. Compound contributor geometry follows its cage.

Run `python tests/benchmark_cage.py` for an optional hardware check.
On this Windows machine on September 7, 2026, warmed cage rendering measured
about **1.8 ms at 512×512** and **4.6 ms at 1080×1080**, including framebuffer
readback. These are isolated effect timings, not a guarantee for an entire
chapter's frame rate. GPU/CPU bilinear and bicubic results differed by at most
one channel level in the test image. Nearest sampling differed at two texel
boundary pixels because hardware snaps triangle vertices to its subpixel grid.
The script also writes sample cage images under `.artifacts/cage-transform/`.

Chapter schema 24 persists cage geometry. The series schema remains 17.
Documents containing settings from the removed repeating-texture feature load
those layers as ordinary shapes, retaining their source artwork and effects.
