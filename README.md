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
- Linked non-destructive HSL, blur, exact outside-outline, and hue-based Posterize modifier stacks
- GPU-accelerated Halftone and Pixelate modifiers with editable colors and pre-filter blur
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
- Blender 5.2 and 4.5 LTS Comic Views as disk-published transparent image sources with
  persistent offline PNG caches

## Modifier presets

Use the dropdown immediately to the right of a modifier's collapse button.
**Load modifier preset** lists saved presets for that modifier type in the
current series. **Save modifier preset** updates the loaded preset, or asks for
a name if none is loaded. **Save as modifier preset…** saves a separate copy.
**Manage modifier presets…** lets you rename and delete presets of that type.

Presets are saved with the series and can be reused across its chapters and
assets. Loading one updates the modifier's effect settings and can be undone;
its linked targets, masks, source-object selection, name, and collapsed/muted
state stay in place. Updating or deleting a preset leaves previously applied
modifiers unchanged until another preset is loaded. Saving a chapter preserves
which preset was last loaded or saved for each modifier.

## Text color and spacing

The on-canvas text gizmos include **Change color** immediately after Italic.
It opens the floating color picker: select a text range to color just that
range, or leave the selection empty to recolor the whole object. Click Apply
to keep the color or Cancel to leave it unchanged. Colors survive typing,
undo, saving, copying objects, and export.

Drag the orange **L** handle beside **S** (size) and **K** (kerning) to change
line spacing from 0.5× to 3×. Escape cancels a drag.

## Text outlines

Select a text box or Free Text container, then choose **Modifiers → Add
Modifier → Outline**. The outline follows the letters, including wrapped and
transformed text. Text stays editable, and outlines support linked targets,
parameter masks, presets, undo/redo, assets, and export.

Live width, color, opacity, and intensity edits reuse the source silhouette and
its exact distance field. Rendering limits pixel calculations to the lettering
and its outline, with bounded caches. Carets and selection highlights are drawn
separately so they remain responsive and never enter the exported outline.
Run `python tests/benchmark_canvas_text_outlines.py` to measure canvas updates,
typing, and cache use on your machine; `tests/benchmark_text_outlines.py`
measures the outline renderer alone.

## Halftone and Pixelate

Halftone also works directly on linear, radial, and shape color gradients,
including outward fades, through the same cached GPU effect pipeline.

Choose **Modifiers → Add Modifier → Halftone** or **Pixelate** with an image,
drawing, or shape selected. Both effects work in stack order, support linked
targets, intensity masks, mute, undo/redo, saved projects, assets, export, and
Raster **Apply**. Their controls change the rendered appearance without editing
the source artwork. Transparency is processed in premultiplied color to avoid
fringes around cutouts.

Halftone provides the six pattern families from
[Halftone Maker's Simple mode](https://halftonemaker.com/): square, hexagonal,
radial, line, ring, and stippling. Adjust spacing, rotation, inverse-luminance
sampling, level limits, blur, gamma, contrast, and input clamp. The **Base
resolution** and **Fit** controls define pattern units relative to the artwork;
they do not resize the object. Grid placement stays stable while panning or
cropping the viewport.

**Dots and lines** includes circles, incircles, triangles, squares, polygons
and stars, line marks, blobs, Delaunay triangles, and
liquid shapes. Relevant controls appear for the selected style, including dot
size, scale, rounding, line width, point spacing, and stippling variation.
Stippling uses deterministic procedural relaxation: **Smoothing iterations**
controls its convergence strength, and collision radii vary with source tone.
This is a realtime approximation of the reference's iterative stippling solver.
**Colors** supports two colors, editable gradients with RGB or OKLCH
interpolation, the incoming artwork's source colors, and **Target layer**.
In Target layer mode, click **Pick layer or object** and select an outline row,
including a Blender Comic View. Its latest cached render supplies the colors.
**Hue**, **Saturation**, and **Lightness** adjust its sampled colors without
changing the pattern. Colors follow canvas positions, including source-layer
transforms. Hidden source layers and objects can be used, and transparent or missing samples
use the incoming artwork's colors. Nested Target layer references use their
incoming colors to avoid feedback. Enable **Transparent background** to leave
only the marks.

Pixelate includes [Pixel Art Village's](https://pixelartvillage.com/) pixel
size (1–100), brightness, contrast, and saturation (−100–200) sliders, plus
**Pre-filter blur**. Blur is applied before sampling the pixel blocks; zero
disables it. **Reset All** restores the adjustment defaults in one undo step.

On supported OpenGL 3.3 hardware, cached textures and shader passes keep live
adjustments on the GPU, with bounded blur kernels and cached pattern geometry.
The raster renderer and unsupported drivers use a CPU fallback. Run
`python tests/benchmark_pattern_effects.py` for local GPU timing and visual
checks; it writes a contact sheet and timing data under
`.artifacts/pattern-modifiers/`.

## Array

Choose **Modifiers → Add Modifier → Array** to repeat the selected artwork
without changing its original position, rotation, or scale. **Count** is the
number of added copies (0–100). **First** repeats forward, **Last** backward,
and **Center** splits the copies around the original; an odd count places the
extra copy forward.

Drag either orange dot to set the distance and direction between repetitions.
The arrow indicates forward. The independent blue crosshair sets the rotation
and scale center. **Angle offset** adds degrees per step. **Scale offset** is
a percentage change per step: +10% produces 110%, 121%, 133.1%, and so on.
Backward steps use negative rotation and inverse scale so the original keeps
its place in the sequence. Copy centers follow the straight repetition axis.

Array supports modifier ordering, intensity and intensity masks, linked
targets, undo/redo, saved projects, assets, export, and Raster **Apply**.
Press Escape during a gizmo drag to cancel it.

## Posterize

The **Modifiers → Add Modifier → Posterize…** command asks for 1–24 colors.
Select a drawing, image, shape, or color gradient. Both Posterize variants work
directly on linear, radial, and shape gradients, including outward fades.
It samples the selected artwork and creates hue ranges with average output
colors. The circular editor shows hue around the ring and frequency as radial
bars. Drag a boundary to resize a range; neighboring handles cannot cross.
Click a color swatch (or **Color…**) to use the existing color picker. **+** splits
the selected range; **−** merges it into the preceding range. The last range is
kept. Ranges may wrap through red at 0°; gray pixels use hue 0°.

Enable **Simplify colors**, above the posterization controls, to reduce fine
grain before mapping hues. **Detail size** sets the neighborhood radius in
artwork pixels; **Color tolerance** controls how readily nearby colors blend;
**Strength** blends the simplified colors with the original. Start at 3 px,
25% tolerance, and 100% strength. Increase detail size for coarser texture, or
lower tolerance to protect more boundaries. The step uses alpha-weighted
[RGB guided filtering](https://people.csail.mit.edu/kaiming/eccv10/index.html),
preserves transparency, and updates the hue histogram without resetting the
chosen ranges or palette. It starts disabled so existing artwork is unchanged.

Posterize preserves source transparency and supports intensity, masks, linked
targets, undo/redo, saving, export, and Raster **Apply**. Large artwork uses a
bounded sample for the initial palette and live histogram; the effect itself
is rendered at the artwork's resolution.

**Posterize Value…** is the grayscale-source variant in the same menu. It
groups perceived brightness (0 = black, 255 = white) instead of hue. Its
rectangular editor shows a linear grayscale ramp and frequency bars, with a
separate strip of output colors. Every range can map to any color through the
same picker. Black and white are fixed endpoints; drag the internal boundaries
to resize ranges, or use **+** / **−** to split and merge them. Initialization
asks for a color count and seeds the output with grayscale averages.

Posterize Value includes the same **Simplify colors** controls above the range
editor. The source is converted to grayscale before smoothing and range mapping;
the output palette stays editable. Intensity, masks, linking, undo/redo, saved
projects, assets, export, and Raster **Apply** work with either variant.

## Run

On Windows, run `start.bat`. It installs the required Python packages,
including PySide6, Pillow, NumPy, and SciPy, before starting the app. SciPy's
compiled Euclidean distance transform powers accurate realtime outlines.

Or run the equivalent commands manually:

```powershell
python -m pip install -r requirements.txt
python main.py
```

## View settings, image placement, and file feedback

The **View Settings** tab sits beside **Layer Settings** and **Masks**. Its
**Overflow** slider shows artwork outside the rendered page area: 0 hides it,
1 shows it normally, and intermediate values fade it. This is an editing aid
and does not change the exported artwork. Tablet Navigation, Reset View, and
Fullscreen are also in this tab.

Enable **Export rect**, then choose **Edit export rect**. Drag inside the
rectangle to move it or drag its eight handles to resize it. **Save export
rect** leaves editing mode. The geometry and enabled state are saved with
the chapter, and editing again starts from the last rectangle. Full-page and
rectangle exports have separate remembered destinations.

Drop images from File Explorer or a browser onto the canvas to place them
at the cursor at their native size. Pasted images use the cursor position
too. The selected container becomes their parent; when a leaf is selected,
the new image becomes its sibling immediately above it. New images become
selected. Web image downloads run in the background; sources that require
website sign-in or prevent direct image downloads may need Copy Image or a
local file instead.

Right-click an outliner item for **Copy Object**, **Paste Object**, and **Duplicate Object**.
Copies retain editable descendants, masks, effects, and embedded image/raster
data and can be pasted into another chapter or project. **Ctrl+Shift+V** opens
the scrollable **Clipboard image history**, with square previews of images,
drawing selections, and copied objects. It pastes at the position captured
when the popup opened. History stays in memory for the current application
session, bounded to 30 entries and 128 MiB; oversized copies remain available
to ordinary Paste.

The title bar shows the project directory followed by its name. Successful
manual saves and exports show a floating row at the top of the canvas, with
the filename, directory, and a button to open that directory. The row replaces
the previous notification, pauses its timeout while hovered, and can be dismissed.

## Use as Blender's external image editor

Run `build-launcher.ps1` once after installing `requirements.txt`. It creates
`WebtoonMaker.exe` beside `main.py`, using the installed Python environment.
In Blender **Edit → Preferences → File Paths → Applications → Image Editor**,
choose that executable. Keep it in this folder; rebuild the launcher if Python
is moved. Launches do not reinstall packages or open a console.

In Blender's Image Editor, choose **Image → Edit Externally** on an image saved
to disk. For `C:\Art\texture.png`, Webtoon Maker opens the image at its original
pixel size, creates `C:\Art\texture\` as a portable project, and immediately
sets **Export Again** to `C:\Art\texture.png`. The embedded source retains its
original bytes; the page and document background are transparent. Image canvases
keep their dimensions instead of growing like comic chapters.

Use **Save** to retain editable layers in the project. Use **Export Again** to
write the composite back to the original image, then **Image → Reload** (`Alt+R`)
in Blender to see it. Export preserves the filename's format (PNG, JPEG, BMP,
TIFF, TGA, or WebP); JPEG flattens transparency onto white. The editor composites
in 8-bit RGBA, so this workflow is not a lossless HDR/16-bit editor. **Export As…**
can choose a different PNG destination for subsequent exports.

Opening the same image again returns to its existing project and keeps saved
layers or in-memory edits. A different image opens another project tab in the
running editor. An unrelated folder with the same name is left untouched and
reported as a conflict; images such as `texture.png` and `texture.jpg` therefore
need distinct stems or separate directories. Missing, unsupported, or corrupt
images produce an error without overwriting the source.

**File → Open Image…** uses the same workflow. You can also drop an image onto
`WebtoonMaker.exe` or pass files directly:

```powershell
.\WebtoonMaker.exe "C:\Art\texture.png"
python main.py "C:\Art\texture.png"
python main.py "C:\Art\texture\series.json"
```

The ordinary batch launchers also forward file arguments. Packed or unsaved
Blender images must first be unpacked or saved to disk before Blender can hand
them to an external editor. See the [Blender image editing manual](https://docs.blender.org/manual/en/5.2/editors/image/editing.html).

## Blender Comic Views prototype

### Create textures and include UV maps

In Blender's **Comic Views** N sidebar, expand **Textures**. Enter a name and
choose Prefix, Suffix, or Full Name. Prefix and Suffix use the active object's
name. Expand **Texture Creation Settings** for dimensions, fill, color, and
alpha. **Save File Settings** chooses either the `tex` directory beside the
saved `.blend`, or a custom directory selected with the folder button.
**Create Texture** creates the directory if needed, saves the PNG directly
there, and opens it using Blender's configured external image editor.
Existing filenames receive a numbered suffix instead of being overwritten.

Expand **Material Settings** to choose the object's material slot (slot 1 by
default). Creating a texture applies the included comic shader to that slot,
names the material after the texture, and connects the image. An existing
Comic Views preset is updated in place. **Transparency**, **Color**,
**Metallic**, and **Roughness** control the preset directly; shared materials
are made independent so changes stay on the selected object.

**Include UV Map Layer** sends the active mesh's active UV layout with the
texture. Webtoon Maker embeds it as a separate visible UV guide, preserving
the texture pixels. The guide is excluded from exports and chapter previews.
For an existing texture use **Edit Texture Externally** in the sidebar or
**Image → Edit in Webtoon Maker (with UV Map)**. Reopening refreshes changed
UVs without duplicating the guide or replacing your painted layers. The
small `.webtoon.json` file beside the texture carries the UV edges; once
imported, the guide is stored inside the portable project.

After exporting your painting from Webtoon Maker, click **Reload Texture**
to refresh the saved image in Blender and every material using it.

### Connecting Comic Views

The Blender integration keeps the 3D scene wholly inside Blender. A linked
Comic View is an ordinary Image Object in the editor: translation, free or
uniform projective transforms, masks, opacity, hierarchy, and compositing all
remain editor-owned. Blender only replaces its source pixels. Drawing stays on
normal raster or vector layers above the image.

To install and connect it:

1. Install Blender 5.2 LTS on Windows (4.5 LTS is also supported).
2. Run `blender_extension/webtoon_comic_views/build.ps1`, or use the already
   built `blender_extension/webtoon_comic_views-0.8.0.zip`.
3. In Blender, choose **Edit → Preferences → Get Extensions → Install from
   Disk**, select the ZIP, and enable **Webtoon Comic Views**.
4. In a 3D View, open the **Comic Views** sidebar. Create, Save, and Render views and
   copy the displayed loopback port and token.
5. In Webtoon Maker, open the **Blender Views** ribbon page, enter those values,
   connect, select a thumbnail, and choose **Add Selected View to Canvas**.

If something fails, click **Copy Logs** in Blender's Comic Views panel. It
copies a token-redacted diagnostic report with extension events, render errors,
active-view metadata, and published-file status for easy bug reports.

When upgrading, save your `.blend`, install the 0.8.0 ZIP
using **Install from Disk**, and restart Blender and Webtoon Maker. Existing
Comic Views, animation keys, and embedded comic images are retained. Version
0.6.1 uses Blender's Action slots and channelbags, replacing the legacy
`Action.fcurves` API removed in Blender 5.0. The editor displays the connected
Blender and extension versions when the extension reports them. The build
script prefers Blender 5.2 and accepts `-BlenderExecutable` for a custom path.

Version 0.6.1 removes repeated rig and animation-key searches from Save and
Save-and-switch. Render refreshes both Blender's list icon and the larger Comic
View preview immediately, and those previews persist when reopening the blend.

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
- `Ctrl+D`: deselect the active drawing selection
- `Ctrl+V`: paste the newest drawing-selection, outliner-object, or clipboard image buffer at the cursor
- `Ctrl+Shift+V`: open **Clipboard image history**; clicking a thumbnail pastes at the cursor position captured when the hotkey was pressed
- `Delete`: delete the selected layer or object when no field or canvas
  sub-editor owns the key

Hotkeys are editable as single simultaneous chords, including modifier-only
bindings such as `Ctrl` or `Ctrl+Shift`. Tool bindings can enable **Hold**:
a quick tap selects that tool normally, while holding for at least 200ms
switches temporarily and restores the previous tool on release.
Cut, Copy, and Paste as New Object are also available as unassigned hotkey
actions. Drawing Cut/Copy work on selected raster pixels or vector points;
Paste merges into a compatible active object, while Paste as New creates a
matching editable object immediately above the active raster/vector object.

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
page. It is inserted immediately after that page in the outliner without
changing existing page spacing. **Insert Page Gap** works anywhere in the
existing canvas: drag between two orange dotted lines to choose the exact
amount of blank vertical space, adjust either line, then confirm or cancel.
The live preview grows the canvas and moves complete layers or objects that
begin below the insertion line; content crossing the line is left intact.
Confirm commits the entire preview as one undoable edit, while Cancel or
Escape restores the exact original document.

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
resizable color window below the left tool list has Picker, Palette, and
History tabs. Primary and secondary colors are saved per series in canonical
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

HSL, Blur, Radial Blur, Outline, and Mirror modifiers remain attached to their objects rather than
appearing in the outliner. Their processed results are cached separately from
their isolated source images. Outline color, thickness, opacity, intensity,
and parameter-mask edits therefore reuse the same exact alpha distance field;
only source-alpha changes rebuild it.

**Stroke modifiers** appear under **Add Modifier** for closed shapes and vector
drawings whose strokes are closed. **Scream / Thought** adds adjustable spikes;
Roundness turns them into thought-bubble lobes. **Wobble** uses seeded noise to
vary position and opacity, with scale, offset, and a Randomize seed button.
**DotDash** adds curved dashes or fixed-orientation dots, spacing, length,
corner roundness, and repeating patterns (`-` marks and spaces).
All three have a 0–100% Strength slider and masks on every numeric setting.
They share the existing ordered, linkable stack, support undo and export, and
keep the editable source points intact. Whole repeats close around each loop.
On compound shapes, each stroke effect changes only its owner's shape and
outline. That modified shape contributes using its existing Add, Subtract, or
Ignore mode, including nested compounds. Children retain their own geometry
and artwork; the resulting compound boundary still clips their content.
Open shapes, images, and raster drawings are ineligible.

**Free Text Container** holds independent text boxes without adding a shape or
clipping boundary. Click to place its first box or drag its wrapping bounds;
Escape cancels placement. **Add Text** within the container places another box,
then enters Text Edit with the placeholder selected. The on-canvas
**Bounds / Stretch** toggle (also in Tool Settings) chooses reflow versus
pixel-wise deformation. Container Bounds resizing reflows each box independently.
Strict-to-free conversion preserves the resolved layout instead of stretching
it through stale dimensions. Select prioritizes visible text boxes, including
their whitespace, while explicit gizmos retain priority.

The **Blurs** submenu contains **Blur**, **Blur Legacy**, and **Radial Blur**.
Normal Blur fixes the premultiplied-alpha resampling error that caused colorful
distortion around transparent content. Legacy deliberately preserves that look,
and old saved Blur modifiers load as Legacy. Radial Blur averages a circular
spin around a draggable, grid-snapped center, with a 0–360° angle control and
selected-only gizmos. Angle/intensity masks, shared links, mute, undo, export,
Rasterize, and Raster Apply are supported. Radial previews run asynchronously;
large angles can take appreciably longer than a frame to finish.

**Export As…** chooses a PNG filename; **Export Again** overwrites the last
successful destination for that chapter, remembered across restarts. Without
a remembered destination it opens Export As. The timestamped **Export PNG**
action remains available. All three use the enabled export rectangle, or the
full chapter when it is disabled. Rectangle exports remember their own destination;
their first export opens the file picker even for a texture with an existing
full-image destination. Chapter backgrounds default to transparent; page fills retain their
own alpha. Legacy white chapter backgrounds migrate to transparent, while
white page fills and nonwhite backgrounds remain unchanged.

All color dialogs use the complete Picker / Palette / History controls,
including alpha, hex, copy/paste, primary/secondary wells, and canvas
eyedropper. **Apply** commits the pending field and shared-color changes;
**Cancel** discards them. Eyedropper temporarily hides the dialog and returns
to it after sampling (Escape cancels sampling and restores the previous tool).

In Shape Edit, selected custom-path points have a hollow circular outline
width handle. Drag to adjust 0–10 times the layer's **Outline px** baseline;
double-click resets to 1×. Shift-click an edge to toggle its outline without
changing the fill or inserting a point. Zero baseline hides all outlines but
retains point widths. Splitting edges interpolates widths and preserves hidden
edges; deletion joins an edge only when both replaced edges were enabled.

Click a modifier card's background/title to select it (blue border), and click
again to deselect. Only its selected, unmuted modifier's manipulation handles are shown.
Mirror retains the original above its reflection. Its orange dotted axis has
two endpoint handles and a midpoint handle; grid snapping applies to both.
Shape mirrors can independently Add/Subtract into the nearest compound, or
Ignore it. A linked Mirror shares one document-space axis across its targets.

**Modifiers → Add Modifier → Tiling** repeats a movable crop of a raster drawing,
vector drawing, image, or non-page shape. Choose Square, Hexagon, or reflected
Triangle. Set the center, side length, and rotation numerically, or select the
card and drag the center, corner, and rotation handles. Moving the tile samples
different source artwork; muting or removing Tiling restores the source view.
The orange source boundary stays visible while painting an affected drawing.

Pencil and eraser gestures wrap complete brush footprints across tile edges,
and raster fills use the same periodic boundaries, including vector reference
artwork. Wrapped vector strokes remain editable and whole-stroke erasing keeps
their seam fragments together. Selection and ordinary transforms edit source
content; selecting repeated copies and direct vector fills are not supported.

On shapes, Tiling repeats the children together, preserves their internal masks,
and clips the result to the shape, including compound holes and open-shape
silhouettes. The shape's own fill and outline stay in place. Tiled drawings and
images fill their nearest enclosing shape or page. Tiling stays first in the
modifier stack, supports intensity and masks, and allows one setup per hierarchy
branch, including muted setups. Siblings can share a linked document-space grid.
Save/reopen, assets, export, Rasterize, and Raster Apply retain the same result;
baking removes the baked tiling behavior. Chapter files now use schema 25.

Right-click an object or non-page shape and choose **Rasterize** to bake its
subtree and active effects into one embedded Image. Undo restores the graph
and resources. Contributing compound children must be baked at their containing
compound; unavailable image sources or externally required descendants block
baking. **Convert to Raster** remains the Image-to-editable-Raster action.
For Raster-only selections, each unmuted modifier card also offers **Apply**:
it bakes that stage and earlier active stages into sparse pixels, retaining
earlier muted stages, later stages, and links to other targets.

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

Each project tab remembers its own drawing selection, transform frame, pivot,
and pasted content. Returning to a tab restores that selection; switching to a
different chapter starts with no drawing selection.

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

## Cage Transform

Choose the grid icon for **Cage Transform**. On raster/vector drawings it
previews a destructive edit; **OK** applies one undoable operation and
**Cancel** restores the original. Vector strokes remain editable. On images,
Blender images, or shapes, it opens Modifiers and adds a non-destructive cage;
a shape's cage affects its children as well. Compatible multi-selections
share one cage. Mixed drawing/image selections show an error naming and
highlighting incompatible items without changing them.

Both workflows offer horizontal/vertical lattice counts, smoothness,
nearest/bilinear/bicubic image interpolation, flip buttons, and free/uniform
transforms. Shift/Ctrl-click adds points; Shift-drag selects a box. The outer
handles resize the selected points, the rotation handle rotates around the
movable gold pivot, and the outer frame translates them. Modifier cards
show a blue selection outline in **Modifiers** mode; selection is remembered
when leaving that mode or reselecting an object, with handles hidden meanwhile.

Auto/GPU rendering uses a dedicated OpenGL cage shader when
available, with a CPU fallback and bounded caches. See
[the feature and performance notes](docs/cage-transform.md)
for controls and measurements.

## Test

See [editing performance notes](docs/editor-performance.md) for measured
modifier, drawing, fill, and autosave responsiveness and reproducible benchmarks.

```powershell
python -m pytest -q
```
