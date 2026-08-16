# Product, features, tools, and modes

## Product purpose

Webtoon Maker, whose window title is **Vertical Comic Editor**, is a native desktop drawing and composition program for vertically scrolling webcomics and hybrid web-novel pages. A chapter is always 1080 pixels wide and starts at 3240 pixels high. It grows when content crosses the bottom and can later be trimmed to a safe height.

The editor is not a conventional stack of full-canvas bitmap layers. Its primary model is a tree of freely positioned page shapes, nested shape/mask layers, and typed objects. Raster pixels, editable vector strokes, vector fills, text, and gradients can coexist beneath the same non-destructive shape hierarchy.

The application intentionally does not implement scheduling, reference libraries, music, galleries, study tracking, ratings, or a separate creative-session database. The `lighter-novel/` directory is user-authored story material, not an application subsystem.

## Main workspace

- The top project toolbar starts with an instant-popup File button containing New Series, Open Series, a dynamic Open Recent submenu, Save, and Save As. Save As atomically clones the complete owning series with a new series ID and rebinds its open series/asset tabs to the clone.
- The same toolbar contains Undo/Redo, chapter switching/creation, Trim Height, Fullscreen, and Hotkeys.
- A closable project-tab row keeps multiple series and assets open. Switching tabs restores the document, sparse tiles, dirty state, undo/redo, camera, selection, tool, hierarchy expansion, ribbon choice, and render caches without reopening from disk.
- The left sidebar contains the tool list, expandable Shapes and Drawing Selection groups, add-object actions, navigation/grid toggles, and resizable Picker/Palette color tabs.
- The ribbon contains Tool Settings and contextual Vector Tools, Raster Object Settings, and Gradient Tools pages. Selecting text opens Tool Settings and replaces its ordinary tool group with Object/Presets, Typography, and Layout groups.
- The center contains a narrow whole-chapter navigator and the interactive canvas.
- The right dock contains layer settings, the drag-reorderable hierarchy tree, page/layer creation, and deletion.
- A compact floating inspector follows eligible vector/fill/shape selections. Text, Raster, and Gradient properties live in ribbon controls instead.
- Three splitter sizes — sidebar/workspace, tools/colors, and ribbon/canvas — persist across restarts.

## Project and page workflow

- A **series** is a portable folder. It stores its chapter list, primary/secondary colors, palettes, and gradient ramp presets.
- Each series also owns an `assets/` library. An asset is a fitted asset document containing one root layer/object, its descendants and owned helper objects, sparse raster tiles, and a rendered square thumbnail.
- The always-visible **Asset Library** ribbon lists the active series' assets in a horizontal gallery. Double-click edits an asset in its own tab; dragging shows a 70%-opacity preview and creates an independent, freshly-IDed copy only on release.
- Right-clicking an outliner row offers Rename and Copy as Asset. Asset names are unique within their series and can be renamed from the gallery without changing the stable asset folder ID.
- A new **chapter** gets one 1080×1080 page, one matching drawing layer, and one empty raster object.
- Pages are root bounded layers. They may be rectangles, ellipses, or closed custom paths and may be freely positioned or overlap.
- **Add Page** uses the selected page or selected descendant as an anchor, then asks for a rectangle, circle, or custom closed shape below it. The new page is inserted immediately after the anchor page in hierarchy order.
- If lower pages occupy the requested space, they can be shifted as a group and an editable 120-pixel orange gutter is staged. Dotted edges move one adjacent page group; dragging the shaded band moves both groups. Confirm commits one undoable transaction; cancel restores the pre-operation layout.
- **Insert Page Gap** finds physically adjacent pages and inserts the same editable 120-pixel gutter without creating a page.
- Page motion can grow the chapter and rebase content away from a negative top edge. **Trim Height** refuses to cut through visible page or object bounds.
- The chapter navigator caches a small preview and offers a draggable viewport handle for scrolling long chapters.

## Layer and shape features

### Layer kinds

- **Bounded layer**: a closed rectangle, ellipse, polygon, or arbitrary multi-contour vector path. It can fill, outline, clip children, override a grid, participate in compound geometry, and contain more layers or objects.
- **Open shape layer**: an open cubic path rendered as a variable-width ribbon. Each node can scale width; the path has core color, optional outline, and point/square/round endpoint caps. It can also clip children to its stroked silhouette.
- **Fill layer**: a boundless, childless leaf that fills its direct parent's effective shape. New fill layers are inserted at the front of the parent's ordered child list and otherwise obey normal hierarchy ordering.
- **Page layer**: a root bounded layer. Page outlines are capped at 40 pixels and pages cannot become compound or ignore an ancestor mask.

### Unified shape geometry

- Rectangle and ellipse primitives have stable editable nodes. Custom paths can be open or closed and can contain additional contours for holes or disconnected islands.
- Nodes are either straight **vector** points or cubic **Bezier** points with incoming/outgoing controls.
- Bezier controls can be locked; valid locked handles are normalized as linked, symmetric tangents.
- Nodes store independent roundness and width multipliers. Rectangle roundness is editable per corner.
- Rectangle editing has **Normal** mode, which scales the complete primitive, and **Free** mode, which moves corners or attached edge pairs.
- Shape creation uses click for a vector point and drag for a Bezier point. Clicking the first point closes and confirms a path; Finish, Enter, or double-click confirms an open path.
- Adding a point to a rectangle or ellipse requires confirmation because it converts the primitive to a custom shape.
- Shape creation and Shape Edit expose screen-space S/O scrubbers for global open-stroke thickness (0-150) and outline thickness (0-100), with one integer pixel per four screen pixels. Shape Edit also exposes node, control, point-type, handle-lock, roundness, per-node width, deletion, insertion, and cap affordances. Shift can extend node selection.
- New shapes use the active secondary color for fill/core and an enabled four-pixel active-primary outline.

### Masks and compound shapes

- Child rendering is clipped without modifying the child data. Nested masks intersect naturally through recursive painter clipping.
- A child may **Ignore direct parent mask**. It is then rendered after the parent's fill/outline, outside that immediate mask, while still remaining inside higher ancestors.
- A layer can enable **Compound shape**. Its own path and descendant construction paths are combined using **Add**, **Subtract**, or **Ignore** operations.
- A compound parent supplies the visible fill, outline, visibility, and opacity. Contributor styling is construction-only.
- Open shapes contribute their core stroke silhouette to a compound.
- An ignored branch is isolated from the compound and renders normally.
- New contributors default to Add. A selected or drafted active contributor receives an Add/Subtract/Ignore cycle button; a free-shape draft is treated as open and previews its prospective Boolean result before commit.
- Eligible objects choose a geometry reference of **Direct parent** or **Closest compound**. This changes clipping and, for strict text, the layout bounds.
- **Flatten Compound** converts the current Boolean result into one editable custom multi-contour shape, preserves holes/disconnected regions and object positions, removes construction layers, and records one undo command.

## Raster drawing features

- Raster objects store only allocated 256×256 premultiplied-ARGB tiles. A blank tall chapter consumes no chapter-sized bitmap.
- **Add Raster** enters a drag-to-create frame mode. The interaction frame stays visible while painting and is not a clipping rectangle.
- Pencil and eraser work beyond the frame. Pencil strokes expand it to the union of its old area and alpha bounds plus a 24-document-pixel margin. Eraser strokes recalculate it from the remaining alpha plus that margin.
- Erasing prunes empty tiles. Erasing all pixels retains the previous interaction frame.
- Circular and square erasers are supported.
- Pencil presets independently map stylus pressure to size and opacity, with min/max/control points and pressure enable flags. Density, antialiasing, and start/end taper are applied by the raster pencil; the vector pencil reuses the pressure size/opacity channels.
- Small/medium/large pencil and eraser sizes are configurable, including which size becomes active at launch.
- Predictive ink shows the extrapolated end of an active raster stroke without committing it.
- Stylus hover shows the active brush/eraser radius and a center crosshair.
- Raster opacity may be locked to its parent layer or independently set. The object can ignore its direct parent mask, reference a compound mask, and show an editing underlay.
- Raster translation moves sparse data without resampling. Scale, rotation, or projective transforms bake pixels into a new sparse tile set.
- Transform previews use a cached static background and spatially query only relevant source tiles.

## Vector drawing features

- **Add Vector Drawing** creates a drawing object made of editable cubic strokes.
- The ordinary Pencil becomes a vector pencil when a Vector Drawing is active. Pressure samples are deduplicated, resampled, and fitted into cubic anchors while retaining width and opacity.
- A tap creates a pressure-aware dot. Open strokes support point, square, and round caps.
- Vector strokes render live during a gesture and remain editable afterward.
- Selecting a Vector Drawing and invoking Shape Edit maps to **Vector Edit**. It shows anchors for selected strokes; Ctrl-click extends stroke selection; dragging an anchor previews only affected stroke geometry.
- The ordinary Eraser becomes a vector eraser with three modes:
  - **Stroke** removes an entire touched stroke.
  - **Point** subtracts the swept corridor and keeps surviving cubic spans.
  - **Intersection** removes from the touched portion to the nearest centerline intersections.
- The eraser hit metric follows the circular/square eraser shape and accounts for variable stroke width.
- Vector erasing previews the complete cut from the initial press while keeping the drawing model unchanged until release, so the gesture still creates one undoable command.
- **Vector Redraw** changes point thickness or opacity. Manual Redraw maps pressure directly up to a configured maximum. Point Select mode applies Increase, Decrease, or Uniform operations to selected points first, then selected strokes, then all strokes.
- **Vector Connect** sweeps across two endpoints and joins them with a tangent bridge while retaining the first stroke's visual style.
- **Sweep Simplify** uses a screen-stable orange circular preview and a spatial anchor grid. It simplifies only covered anchors and incident cubic spans. **Apply** uses selected points, then strokes, then the whole drawing.
- Vector edits use per-stroke render revisions so an edit does not invalidate unrelated cached stroke images.

## Vector fill features

- The Fill tool traces bounded faces of the active Vector Drawing and creates owned **Vector Fill** children behind its strokes.
- A Vector Fill is not a normal layer child. Its owner drawing stores an ordered list of fill IDs; fills move with and are deleted with that drawing.
- **Normal** fill accepts a click or resampled drag through faces. Touching an existing fill can replace/update it.
- **Enclose and Fill** uses a drawn lasso and combines enclosed faces without retaining internal boundaries.
- **Close gaps** can add virtual edges up to a threshold.
- **Fill narrow areas** controls whether neck-like regions survive face preparation.
- **Area scaling** can expand or contract the result using round or rectangular behavior.
- Existing fills do not automatically reflow after line edits; the Fill tool intentionally updates only regions the artist touches again.

## Text features

- **Add Text** creates a text object from the active preset. Selecting that object switches to Text Edit; the Add action itself returns to Object Select.
- Text selection/caret drawing, drag selection, clipboard operations, keyboard editing, and IME input are implemented on the canvas.
- One Text Edit session has its own local history and commits as one document-level undo command.
- The outliner label is derived from the first 16 normalized characters of content and is not separately renamed.
- Text Tool Settings expose presets, font family, optional per-family dropdown previews, integer pixel size, bold/italic, kerning, visibility, opacity/lock, layout mode, 3×3 alignment, margin, geometry reference, and free/uniform transform mode. Layout- and compound-specific controls appear only when applicable.
- Text presets store formatting only: font family, integer pixel size, bold, italic, kerning, layout mode, 3×3 alignment, and margin. Preset sizes are normalized to 6–250.
- In Text Edit, the selected text alone receives an on-canvas `− size + B I` strip. Its size field commits on Enter/focus loss and cancels on Escape. Two orange circles on the right edge scrub size (10–100, integer) and kerning (1.0–10.0, tenths) at one step per four screen pixels; a drag becomes one undo command.
- Text drag-selection updates character highlighting live. Double-click selects a word, triple-click selects the entire box, and the selected box uses an I-beam cursor away from higher-priority controls.
- **Strict** layout wraps and clips text to the selected direct or compound shape bounds with a uniform margin. Edge-midpoint dragging edits the margin.
- **Free** layout uses a four-point projective quad. It supports the shared eight-handle transform, rotation, pivot, and 3×3 alignment within its local transformed rectangle. Dragging the dotted boundary translates the object while the interior remains available for text selection.
- A free-text drag caches the scene without the selection and rasterizes the selected text once at device-aware resolution. Pointer moves reproject that image through the live quad; commit/cancel returns to normal high-quality text layout.
- Double-clicking a transformed free text object re-enters Text Edit at the clicked position.

## Gradient features

- A `ColorFillGradientObject` is a direct child of a shape. A parent can own at most one gradient of each field type.
- **Line / Curve** follows an editable open cubic path. Parallel mode maps ramp position by arc length; Perpendicular mode maps signed distance on either side of the curve.
- **Circle / Ellipse** has origin, X/Y radii, rotation, optional ellipse behavior, and automatic or manual center/focal point.
- **Parent Shape** derives its scalar field from the parent boundary and chooses a stable interior center automatically unless manually overridden.
- **Reverse direction** on radial/parent-shape fields creates an outward glow beneath the parent and bypasses only the direct parent mask.
- **Uniform** on radial/parent-shape fields uses a fixed physical inward distance from the effective boundary. Reverse takes precedence and uses that distance outward.
- Gradient ramps contain at least two stable-ID ARGB stops, allow hard transitions at equal positions, support drag/add/remove/double-click color edit, and preserve dormant fields when switching field type.
- Per-series ramp presets support create/load/save/rename/remove. The built-in **Primary → Secondary** preset is read-only and resolves the current color wells when loaded.
- Gradient geometry, scalar fields, and ramp-colored images have independent bounded caches. Dragging uses a reduced grid and rebuilds full resolution on release.
- **Speed Lines** render discrete tapered manga strokes. Closed fields sample their outside boundary and converge on a movable center or compatible custom-center shape; Outwards reverses the trajectories and ignores that center.
- Line/Curve speed lines either follow offset guide curves toward a selected endpoint or project along signed local normals. Separate RGBA color and greyscale-thickness ramps combine with density, gap, close range, and deterministic neighbor-correlated endpoint variation.
- Gradient Tools is contextual: creation controls appear for a selected parent, color gradients show Field and Color groups, and speed lines show only Field, Color, Thickness, and compact Impact groups. Selecting a custom-center child retains its owner’s speed-line controls.

## Color and palette features

- Primary and secondary colors are stored per series in canonical `#AARRGGBB` form.
- The HSV/alpha picker has a hue ring, saturation/value square, alpha strip, active primary/secondary wells, swap button, and hex input/copy/paste accepting `#RRGGBB` or `#AARRGGBB`.
- New raster/vector strokes and fills use primary. New shapes use primary for outline and secondary for fill/core.
- Palettes are stored per series with stable palette and swatch IDs. Single-click applies a swatch to the active slot, double-click edits it, and context actions remove it. At least one palette is retained.
- Color, palette, and gradient-preset preferences save independently of chapter edits.

## Hierarchy, selection, and editing infrastructure

- The outliner mixes layers and objects in one frontmost-first tree. The first child row is visually frontmost; rendering walks lists in reverse to paint back-to-front.
- Vector fills appear nested beneath their owner and may only reorder within that owner.
- Drag/drop validates page-root rules, prevents layer cycles, prevents dropping into fill leaves, preserves object world placement where required, and restores the old graph if validation fails.
- Tree rebuilds preserve expanded entities and selection by stable ID.
- Object Select searches the complete chapter. Shape borders are selectable within 12 screen pixels while filled interiors remain click-through so descendants are reachable.
- Ctrl-click opens an ordered menu for overlapping candidates. Vector strokes are preferred to vector-fill interiors.
- Drawing Selection supports replace, Shift-add, and Ctrl-remove. The default Select All chord is Ctrl+A; during active canvas text editing it selects all text instead, while native text/numeric fields keep their native behavior.
- Undo/redo covers graph changes, text sessions, raster tile/frame patches, vector edits, transforms, page gaps, and hierarchy moves. The in-memory stack retains 200 commands.

## Navigation and input

- Alt-drag pans, Shift-drag rotates, and Alt+Shift-drag zooms. Drag zoom keeps
  the document point beneath the initial click fixed at that screen position;
  dragging right zooms in and dragging left zooms out.
- Mouse wheel scrolls vertically; Ctrl+wheel zooms around the viewport center.
- The camera transform is centered, rotated, uniformly scaled, then translated to the document center. Camera centers are snapped in device space to reduce blur.
- High-frequency mouse and pen navigation packets are coalesced to the newest
  position per event-loop frame, with the final release position always applied.
- During drag zoom, touch pinch, and Ctrl+wheel bursts, vector strokes reuse their
  starting-scale images while layout and newly revealed content remain live. A
  crisp vector redraw follows release/touch completion or a short wheel pause.
- **Tablet navigation** enables touch navigation. One finger pans with the finger; two fingers pan, pinch, and twist around a stable centroid.
- Touch input is coalesced to one application per event-loop turn and live-renders the document under the updated camera transform.
- Stylus events are handled separately from synthesized mouse events. Popup and outliner forwarding allow buttonless stylus taps.
- The narrow preview provides whole-chapter navigation independently of camera rotation.

## Canonical canvas tools

`ToolKind` has 19 distinct values. Two extra enum names are aliases, not additional tools.

| Tool value | UI/context | Behavior and availability |
| --- | --- | --- |
| `object_select` | Object Select, default; S | Selects shape borders or objects. Ctrl-click requests an overlap menu. Selection can automatically route to Shape Edit, Text Edit, Pencil, Fill, or gradient Shape Edit according to entity type. |
| `raster_pencil` | Pencil; P | Draws sparse bitmap dabs on Raster objects or fitted cubic strokes on Vector Drawings. Requires one of those objects. |
| `raster_eraser` | Eraser; E | Clears sparse raster pixels or performs the chosen vector eraser mode. Requires a Raster or Vector Drawing. |
| `fill` | Fill; F | Changes shape outline/interior color or creates/updates owned vector fills. Requires a shape layer or active Vector Drawing/Fill owner. |
| `text_edit` | Text Edit | Selects and edits existing text on the active page; text creation is the separate Add Text action. Selecting non-text while activating it promotes selection to the active page. |
| `transform` | Transform; T | Shared free/uniform quad transformation for free text. Raster transformation is exposed through raster controls and direct on-canvas affordances rather than allowing `set_tool(TRANSFORM)` on a Raster. |
| `shape_edit` | Shape Edit; B | Edits layer paths and gradient field geometry. `BOUND_EDIT` is an alias with the same value. On a Vector Drawing, activation is remapped to `vector_edit`. |
| `vector_edit` | Contextual Shape Edit | Selects vector strokes/anchors and drags anchors. Requires an active Vector Drawing (a selected Vector Fill resolves to its owner internally). |
| `vector_redraw` | Vector Tools → Use Redraw | Pressure/manual or point-based thickness/opacity editing. Requires an active Vector Drawing. |
| `vector_connect` | Vector Tools → Use Connect | Sweeps two endpoints and joins their strokes. Requires an active Vector Drawing. |
| `vector_simplify` | Vector Tools → Sweep Simplify | Sweeps a local simplification radius over vector anchors. Requires an active Vector Drawing. |
| `draw_select_rect` | Rectangle Select | Rectangle selection of raster pixels or vector points. |
| `draw_select_lasso` | Lasso Select | Freeform selection of raster pixels or vector points. |
| `draw_select_stroke` | Stroke Select | Selects whole vector strokes. It is hidden/unavailable for Raster objects. |
| `insert_page_gap` | Insert Page Gap | Finds a physical gap boundary and stages the editable orange gutter transaction. |
| `box_bound` | Shapes → Add Rectangle | Drag-creates a rectangular layer; also serves as a page-shape choice during Add Page. |
| `circle_bound` | Shapes → Add Circle | Drag-creates an ellipse/circle layer; also serves as a page-shape choice. |
| `shape_create` | Shapes → Add Shape | Click/drag path construction for closed bounded or open shape layers. `POLYGON_BOUND` is an alias with the same value. Add Page requires this workflow to finish closed. |
| `raster_create` | Internal Add Raster state | Drag-creates a Raster interaction frame. It is entered by Add Raster rather than shown as a persistent toolbar tool. |

### Tool aliases

- `BOUND_EDIT` equals `SHAPE_EDIT` (`"shape_edit"`) for compatibility with older integrations.
- `POLYGON_BOUND` equals `SHAPE_CREATE` (`"shape_create"`) for compatibility with the earlier polygon tool name.

## Other explicit modes and option sets

These are not separate `ToolKind` values but materially change behavior.

| Area | Modes/options |
| --- | --- |
| Canvas renderer | `auto`, `gpu`, `raster`. Auto/GPU use `QOpenGLWidget` only if an OpenGL 3.3 context probe succeeds; all modes share the same QPainter document renderer. |
| Text layout | `strict` shape-bound layout or `free` projective-quad layout; horizontal left/center/right and vertical top/middle/bottom alignment. |
| Transform | `free` moves corners independently/edge pairs together; `uniform` scales the entire quad. Rotation and movable pivot are available in both. |
| Rectangle edit | `normal` scales the primitive as a whole; `free` moves individual corners or attached edge pairs. |
| Raster eraser shape | circle or square. |
| Vector eraser | `stroke`, `point`, `intersection`. |
| Fill | `normal` seed/drag filling or `enclose` lasso filling; area scaling uses `round` or `rectangle`. |
| Vector redraw parameter | `thickness` or `opacity`. |
| Vector redraw interaction | `manual` pressure redraw or `point` selection/application. |
| Vector redraw operation | `increase`, `decrease`, `uniform`. |
| Drawing selection operation | replace by default, add with Shift, remove with Ctrl. |
| Gradient field | `line`, `radial`, `parent_shape`. |
| Line gradient direction | `parallel` or `perpendicular`, with optional reverse. |
| Radial/shape gradient | inward default, `uniform` fixed distance, or reversed outward; center can be automatic or manual; radial can use circular or elliptical radii. |
| Compound contribution | `add`, `subtract`, `ignore`. |
| Object geometry reference | `direct` parent or closest `compound`. |
| Opacity | locked to the direct layer or independent object opacity. |
| Mask escape | normal clipping or `ignore_parent_mask` for only the direct parent. |
| Hotkey tool binding | normal sticky selection or **Hold**: tap selects; holding at least 200 ms temporarily switches and release restores. |
| Shape endpoints | `point`, `square`, `round` caps. |
| Brush pressure | size and opacity pressure channels independently enabled/disabled, with separate response curves. |

## Default keyboard controls

- P: Pencil
- E: Eraser
- F: Fill
- S: Object Select
- T: Transform
- B: Shape Edit
- Delete: Delete Selected
- Ctrl+A: Select All
- Ctrl+S: Save
- Ctrl+Z: Undo
- Ctrl+Shift+Z: Redo
- Ctrl+0: Reset View
- Alt+G: Toggle Grid
- Alt+Return: Fullscreen (installed directly by the main window)

Vector Redraw, Connect, Simplify, the three drawing-selection tools, and Insert Page Gap have no default chord. The hotkey dialog supports single simultaneous chords, modifier-only chords, duplicate validation, clearing, and optional Hold behavior for tools. Delete Selected yields Delete to focused editors, active canvas text editing, and shape/gradient point editing.
