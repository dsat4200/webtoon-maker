# Vertical Comic Editor

A native PySide6 drawing editor for 1080-pixel-wide, vertically scrolling
webtoon and hybrid web-novel comics.

This repository is independent from Drawing SRS. It contains no sessions,
ratings, scheduling, references, music, gallery, study database, or practice
workflow.

## Current foundation

- Portable series folders and versioned chapter manifests
- Toolbar File dropdown with New/Open/Recent/Save plus atomic whole-series
  Save As cloning
- Portable per-series Asset Libraries with fitted rendered thumbnails
- Cached, closable project tabs for open series and editable assets
- Growable 1080px-wide chapters
- Editable Vector Drawing objects with pressure-sensitive cubic strokes and
  drawing-owned Vector Fill children
- Freely positioned page layers
- Drawn rectangle, circle, or custom-shape page insertion with editable gaps
- Nested rectangle, circle, and polygon bounded layers
- Non-destructive hierarchical masks
- Chapter-local reusable tone masks for opacity and modifier parameters, with
  live contributor alpha, raster paint, and a translucent blue edit overlay
- Linked non-destructive HSL, blur, and exact outside-outline modifier stacks
- Sparse 256×256 raster tiles
- Explicit, non-clipping raster interaction frames with drag-to-create
- Named pressure-curve pencil presets, independent pressure channels,
  density/taper/antialias controls, and configurable S/M/L sizes
- Live on-canvas text editing with selection-scoped size/style gizmos and
  free/projective or strict wrapped layouts
- Eight-handle free/uniform transforms for text and sparse raster objects
- Cached raster/text transform previews, fast sparse translation, and
  spatially culled projective commits
- Global formatting-only text presets
- Drag-reorderable layer/object tree
- Outliner Rename and Copy as Asset context actions
- Drag-to-place detached assets with a transparent on-canvas preview
- Named, bounded layers with non-destructive bound translation/editing
- Rounded layer masks, optional fills/borders, and boundless fill leaves
- Unified rectangle, circle, and open/closed vector-Bézier Shape paths
- Per-point width, roundness, linked controls, outlines, and endpoint caps
- Frontmost-first layer/page selection with Ctrl-click candidate menus
- User grid defaults with optional document and per-layer overrides
- Pixel-snapped pan, zoom, rotation, and aspect-preserving chapter preview navigation
- Touch navigation controlled only by Tablet Navigation mode
- Command-based undo/redo and atomic autosave recovery
- Blender 4.5 Comic Views as disk-published transparent image sources with
  persistent offline PNG caches

## Run

On Windows, run `start.bat`. It installs the required Python packages,
including PySide6, Pillow, NumPy, and SciPy, before starting the app. SciPy's
compiled Euclidean distance transform powers accurate realtime outlines.

Or run the equivalent commands manually:

```powershell
python -m pip install -r requirements.txt
python main.py
```

## Blender Comic Views prototype

The Blender integration keeps the 3D scene wholly inside Blender. A linked
Comic View is an ordinary Image Object in the editor: translation, free or
uniform projective transforms, masks, opacity, hierarchy, and compositing all
remain editor-owned. Blender only replaces its source pixels. Drawing stays on
normal raster or vector layers above the image.

To install and connect it:

1. Install Blender 4.5 LTS on Windows.
2. Run `blender_extension/webtoon_comic_views/build.ps1`, or use the already
   built `blender_extension/webtoon_comic_views-0.5.1.zip`.
3. In Blender, choose **Edit → Preferences → Get Extensions → Install from
   Disk**, select the ZIP, and enable **Webtoon Comic Views**.
4. In a 3D View, open the **Comic Views** sidebar. Create, Save, and Render views and
   copy the displayed loopback port and token.
5. In Webtoon Maker, open the **Blender Views** ribbon page, enter those values,
   connect, select a thumbnail, and choose **Add Selected View to Canvas**.

If something fails, click **Copy Logs** in Blender's Comic Views panel. It
copies a token-redacted diagnostic report with extension events, render errors,
active-view metadata, and published-file status for easy bug reports.

Blender scene edits remain working changes until **Save** stores them. Save
automatically assigns the view a private timeline frame and bakes the camera,
rig controls, and other changing channels there—manual key insertion is not
required. **Render** atomically publishes that saved revision, thumbnail,
resolution, and full PNG. A connected editor imports the finished PNG
immediately; reconnecting imports the newest existing render without asking
Blender to render again.
**Load** restores the latest Save, while **Revert** swaps it with the previous
Save without rendering. The imported frame is encoded into the comic project,
so it reopens with identical pixels when Blender or its publication cache is
unavailable. Updates preserve placement, transforms, masks, opacity, ordering,
and Undo history. When the published aspect changes, the image keeps its
displayed width and center while its height follows the new aspect. Use the
Image Object inspector to reconnect, relink by UUID, or detach the cache into a
normal embedded image. Rasterize and Copy as Asset also freeze the cached image.

The prototype supports one Blender instance and one editor connection. It
renders the camera-relative Stream Frame from the saved camera and captured
shading with a transparent background; that frame may extend beyond the camera
gate and is not a Cycles/Eevee final render. Ordinary viewport pan, zoom, and
rotation do not affect published output.
Comic Views store panel-variable state and stable references, never mesh,
curve, texture, or other geometry data.

Navigation defaults:

- `Alt` + drag: pan
- `Shift` + drag: rotate
- `Alt+Shift` + drag: zoom toward the initial click (right in, left out)
- Mouse wheel: vertical scroll
- `Ctrl` + mouse wheel: zoom
- `P`, `E`, `F`, `S`, `T`, `B`: Pencil, Eraser, Fill, Object Select,
  Transform, Shape Edit
- `Delete`: delete the selected layer or object when no field or canvas
  sub-editor owns the key

Hotkeys are editable as single simultaneous chords, including modifier-only
bindings such as `Ctrl` or `Ctrl+Shift`. Tool bindings can enable **Hold**:
a quick tap selects that tool normally, while holding for at least 200ms
switches temporarily and restores the previous tool on release.

The Settings button beside Hotkeys opens the Grid preferences. Grid box size,
subdivisions, color, opacity, and global visibility persist per user; the
current document and individual layers can override the grid geometry/style.
`Alt+G` toggles the global canvas overlay by default, and snapping follows the
same resolved grid. The chapter navigator and exports remain grid-free.

Expand Shapes to choose Rectangle, Circle, or Shape. In Shape creation,
clicking adds vector points, dragging creates Bézier points, clicking the
first point closes the shape, and Finish, Enter, or double-click commits an
open line. Orange S/O scrubbers edit the draft's global stroke and outline.
Shape Edit exposes the same global controls plus point, curve, roundness,
width, type, lock, and end-cap gizmos using the blue/orange visual language.
Selected points also expose a
circular X gizmo for deletion. New shapes use the active secondary color for
their fill/core and an enabled 4px active-primary outline (white and black by
default). Unlocked Bézier roundness smooths through the selected point;
locked Bézier points already use their linked tangent and do not show a
roundness gizmo. Bézier controls, shape geometry, and object transforms all
use the single Snap to grid toggle.

Object Select can pick a shape by clicking within 12 screen pixels of its
border. Selecting a bounded or open shape—on the canvas or in the layer
outliner—switches directly to Shape Edit. Filled interiors remain
click-through so objects nested inside a shape stay reachable.

Enable **Compound shape** in Layer Settings to combine a shape's own path
with additive and subtractive descendant shapes. Contributing child styles
are construction-only: the compound parent supplies the final fill, outer
outline, visibility, and opacity. Set a descendant to Ignore to isolate its
branch and build nested compounds. Open strokes contribute their variable
stroke silhouette, and Shape Edit can select the individual construction
paths without displaying handles for unselected layers. Raster and text
objects can reference either their direct parent mask or the closest compound
mask; strict text also uses the selected mask's bounds. **Flatten Compound**
compiles the visible Boolean result into one editable multi-contour shape,
preserving holes, disconnected regions, ignored branches, and object
positions as one undoable operation.
New contributors default to Add. During free-shape creation, an Add/Subtract/
Ignore gizmo cycles the draft operation and the canvas previews the prospective
Boolean result before Finish or another confirmation gesture commits it.

Add Text selects the new object, enters an active Text Edit session, and
selects the complete “Text” placeholder so typing immediately replaces it.
Selecting an existing text object also enters Text Edit and opens its
Object/Presets, Typography, and Layout groups in Tool Settings. Its UI label is derived from
the first 16 normalized characters of its content. The canvas shows selected-
text size, bold, and italic controls plus right-edge size and kerning scrub
handles. Typography includes a persistent 0.5×–3.0× line-spacing multiplier,
which is also stored in text presets. Strict text wraps to its parent layer with a uniform margin; Free
text keeps its own projective transform rectangle. Both modes provide 3×3
alignment, using the free text rectangle as the alignment frame when
transformed. Drag or Shift-navigate to select text; double-click selects a word
and triple-click selects the entire box. The selection is shown in translucent
orange, and the configurable Select All command targets the active text editor
while native fields retain their own Select All. The dotted free-text boundary
translates the box while its interior retains normal I-beam editing. Double-click
inside a free text transform to return to Text Edit at that position. Raster
translations preserve their sparse tiles, while
scale/projective transforms bake into sparse tiles. Both use the program's
standard Undo and Redo commands.

Raster and text objects may live directly under a page or under another
container layer. Use Add Raster and drag a frame before drawing. The selected
raster frame remains visible while painting, and Shape Edit changes the frame
without scaling or cropping its pixels. Dragging beyond the frame begins a
stroke and expands it; tapping outside without dragging performs page-scoped
object or shape selection. Painted and recalculated content bounds keep a
24-document-pixel safety margin. Finishing an eraser stroke prunes empty tiles
and fits the frame to the remaining alpha bounds; Undo and Redo restore the
pixels and frame together.

Reordering or reparenting items in the layer outliner preserves expanded
pages/layers and the current selection, including across hierarchy undo/redo.

Use **Add Page** after selecting a page or one of its descendants, then draw
the new page as a rectangle, circle, or closed custom shape below the active
page. It is inserted immediately after that page in the outliner. When lower
pages already occupy the space, the editor can move them together and expose
an orange 120px gutter: drag either dotted edge to move its page group, or
drag the shaded band to move both groups. **Insert Page Gap** adds the same
editable 120px gutter in empty space between two physically adjacent pages.
Page-gap edits and page insertion are undoable, and the chapter grows with a
120px safety margin when a page group crosses its current top or bottom.

With Tablet navigation enabled, one finger pans in the finger's direction.
Two fingers pan, pinch-zoom, and twist-rotate around their centroid without
snapping or resetting the existing zoom.

## Vector drawings, fills, and colors

Use **Add Vector Drawing** to create an editable vector object. The ordinary
Pencil and Eraser are contextual: they paint sparse pixels on a Raster object
and vector strokes on a Vector Drawing. Vector pencil input is fitted to
editable cubic anchors while retaining absolute point width and opacity.
Shape Edit becomes Vector Edit for the selected drawing, showing only anchor
circles for the selected stroke; Ctrl-click extends the stroke selection.
Vector Pencil and Eraser update on the canvas from the initial press instead
of waiting for release. The vector stroke cache and visibility index keep
warmed pan/rotation responsive; live zoom reuses its starting vector detail
and performs one crisp redraw when the gesture ends. Stylus hover shows a pressure-tool circle and center
crosshair at the active S/M/L size, and stylus taps work in popup menus.

The vector Eraser supports Stroke (whole touched line), Point (only the swept
corridor), and Intersection (from the touched section to the nearest
centerline intersections). The contextual **Vector Tools** ribbon adds
pressure redraw for thickness or opacity, endpoint connection, and local
curve simplification. **Sweep Simplify** uses a translucent orange circular
preview and changes only the covered points and their adjoining curve spans.
Editable points remain visible and selected afterward. Apply affects selected
points first, then selected strokes, then every stroke when nothing is
selected. Redraw amount and pressure limits use sliders with manual numeric
entry, and selecting vector points switches Redraw to Point Select.

Fill uses the active primary color and only the active entity. On a Raster,
it performs a four-connected contiguous pixel fill inside that object's finite
interaction frame. Raster Tool Settings provide a 0–255 straight-RGBA
tolerance; transformed rasters are filled in object-local coordinates without
baking their transform. On a shape,
clicking near its border changes the outline and clicking its interior sets
the fill. On a Vector Drawing, clicking or dragging through bounded faces
creates separate Vector Fill children behind its strokes. Enclose and Fill
uses a drawn enclosure and combines the enclosed faces into one fill without
internal boundaries. Close Gaps, narrow-area handling, and optional
round/rectangular area scaling are available in Tool Settings. Existing
vector fills intentionally remain unchanged after line edits until Fill
touches that region again.

The ribbon contains Tool Settings plus a contextual Vector Tools page. The
resizable color window below the left tool list has separate Picker and
Palette tabs. Primary and secondary colors are saved per series in canonical
ARGB form. The color wheel edits either slot, and its hex row accepts, copies,
and pastes `#RRGGBB` or `#AARRGGBB`; new strokes and fills use
primary, while new shapes use primary for their enabled 4px outline and
secondary for their fill/core. Color palettes are also per series: single
click applies a swatch to the active slot, double-click edits it, and the
context menu removes it. Palette names, swatches, and active colors save
automatically.

The right inspector uses the same dark vertical-tab treatment as the left
ribbon for Settings and Masks. Parameter and opacity mask buttons open mask
editing; right-click an assigned orange button for **Remove Mask**, click the
same button again or use **Exit Mask Mode** to commit contributor changes, and
press Escape to cancel the current contributor edit. Mask mode leaves the
normal comic visible and overlays coverage in translucent light blue. In mask
mode Pencil becomes a dedicated alpha brush: pressure maps linearly between
configurable From and To values (0 to 1 by default), or paints one constant To
value when pressure is disabled. It replaces existing mask alpha so light
pressure can lower coverage; Eraser still removes it. Both use queued tile
strokes and one Undo command per gesture.

HSL, Blur, and Outline modifiers remain attached to their objects rather than
appearing in the outliner. Their processed results are cached separately from
their isolated source images. Outline color, thickness, opacity, intensity,
and parameter-mask edits therefore reuse the same exact alpha distance field;
only source-alpha changes rebuild it.

Blur stages use a session-local 64 MiB premultiplied multiresolution pyramid
with effective radii 0, 1, 3, 7, 15, 31, 63, and 127. Strength, focal,
intensity, and mask edits reuse that pyramid; canvas preview, save, and export
all use the same result. Run `python tests/benchmark_masked_blur.py` for the
opt-in 1080p warmed benchmark.

Drag the divider beside the left sidebar to change its width, the divider
between Tools and Colors to change their heights, or the divider below the
ribbon to change the canvas/ribbon balance. These sizes are remembered across
application restarts; the narrow chapter navigator keeps its fixed width.

The Drawing Selection disclosure provides Rectangle and Lasso selection for
raster pixels, vector points, or anchors on any custom-path layer (including
open paths, pages, and additional contours). Stroke selection remains exclusive
to Vector Drawings.
Shift adds, Ctrl removes, and an unmodified gesture replaces the selection;
the configurable Select All command defaults to `Ctrl+A`. Selected content
uses eight free/uniform transform handles plus edge translation, rotation, and
a movable pivot. Custom-path transforms move anchors and their Bézier controls
together, and Delete removes the selected anchors only when every contour keeps
its required minimum topology. Those rotate/pivot affordances are shared by normal object
transforms, and free text exposes its bounds handles while Text Edit is active.

Raster and Vector Drawing inspectors, and eligible shape layers, can enable
**Ignore direct parent mask**. The complete subtree may then draw beyond that
one shape and is composited above its fill and outline while still respecting
higher ancestor masks. Strict text inside a
compound parent can independently use the parent’s main path or the full
compiled compound result.

## Gradient objects

Gradient objects are direct children of shapes and are edited through the
contextual **Gradient Tools** ribbon. A shape can own one Line/Curve, one
Circle/Ellipse, and one Parent Shape gradient; use **Select Gradient** to
select the child matching the field-type dropdown. Line gradients follow the
actual curve by arc length or extend perpendicularly from either side.
Reverse flips the ramp direction. Reversed radial and Parent Shape gradients
extend outward to their distance handle, bypass only the direct parent mask,
and render beneath the parent as an outside glow.

Radial and Parent Shape gradients also support **Uniform** inward distance:
the ramp starts at the effective boundary and reaches its final color after
the chosen physical Distance. Reverse takes precedence and uses that same
Distance outward. Hidden automatic or manual centers are preserved when
Uniform is toggled. Line/Curve gradients use the full open-path geometry
editor—including Vector/Bézier conversion, insertion, deletion, handle
locking, and roundness—without irrelevant cap or thickness controls.

Speed Lines use the same three field types but render discrete manga strokes
instead of filled distance bands. Circle/Ellipse and Parent Shape effects
sample their boundary and taper toward a movable point or compatible custom
center shape. Line/Curve effects either follow offset copies of the guide or
project along its normals. Independent RGBA color and greyscale-thickness ramps
combine with density, gap, close-range, and neighbor-smoothed endpoint
variation; Outwards reverses closed-field trajectories and ignores the custom
center.

Gradient ramps support translucent ARGB stops and reusable per-series
presets. **Primary to Secondary** is a built-in, read-only preset that copies
the current color wells when loaded. The square swap control between those
wells exchanges the active primary and secondary colors. Gradient geometry,
scalar distance fields, and ramp colors are cached independently so moving a
center or editing ramp colors updates interactively without rebuilding the
parent boundary.

## Test

```powershell
python -m pytest -q
```
