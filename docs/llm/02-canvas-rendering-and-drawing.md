# 2D canvas rendering and drawing implementation

## Architectural center

Almost all canvas behavior lives in `comic_editor/ui/canvas.py` (about 21,000 lines).

- `_CanvasLogic` is a large mixin containing document binding, selection, camera math, rendering, hit testing, input dispatch, every drawing tool, text editing, tone-mask and modifier integration, and transform workflows.
- `RasterCanvasWidget` combines that mixin with `QWidget`.
- `GpuCanvasWidget` combines it with `QOpenGLWidget` and requests partial updates.
- `create_canvas(settings)` probes an offscreen OpenGL 3.3 context unless the renderer is forced to `raster`. If the probe fails, or Qt is using `offscreen`/`minimal`, it creates the raster widget.
- The GPU widget is not a separate shader renderer. Both backends run the same QPainter/QImage pipeline; OpenGL accelerates presentation/compositing through the widget surface.
- `_CanvasPerformanceMonitor` records per-frame and per-input timing used by the latency smoke gate.

The entry point asks Qt for a core-profile OpenGL 3.3 surface, no multisampling, and swap interval zero. QPainter antialiasing and explicit offscreen masks/images supply the actual 2D rendering.

## Coordinate systems

The renderer moves among four important spaces:

1. **Widget/device space** — pointer coordinates and the final window image.
2. **Document space** — the fixed 1080-wide, variable-height chapter.
3. **Layer-local space** — geometry after subtracting the accumulated translations of a layer and all ancestors.
4. **Object-local space** — raster tile keys, vector strokes, image pixels, and free-object geometry relative to an object's `(x, y)` inside its parent layer.

`camera_transform()` composes:

```text
widget center
  → rotation
  → uniform zoom
  → negative document camera center
```

The transform is inverted for widget-to-document input and for computing the visible document bounding rectangle. Camera X/Y are snapped in device space after navigation so fractional presentation translations are less likely to blur the chapter. Camera scale is clamped to 0.05–8.0.

Layer world translation is calculated by walking `parent_id` links and summing each layer's translation. Object/world conversions add that value and then the object's own `(x, y)` where applicable. Reparenting gradients explicitly offsets dormant field geometry so their world position is preserved.

## Top-level paint pipeline

`paintEvent()` performs the normal frame in this order:

1. Position the floating text size/bold/italic overlay for the active selection.
2. Fill the widget with dark gray `#242428`; draw the empty-state message if no chapter exists.
3. Use the static-background fast path during a raster/text transform preview, if available: draw the cached scene, then the live preview, underlay, selection, and focal-modifier handles, and return.
4. Use the live vector-eraser path when active: draw the cached background scene without the drawing, then the live replacement strokes and page-gap overlay, and return.
5. Normal path: ensure the widget-level scene cache, draw it, then under the camera transform draw predictive raster ink, the live vector gesture preview, the selected text preview during a text-edit session, and the page-gap overlay.
6. Draw the active tone-mask overlay (`#64B5F6` coverage); while it is active it suppresses selection, creation, and asset-drag previews.
7. Draw selection controls, focal-modifier handles, shape/raster creation previews, and the asset-drag preview.
8. Draw screen-space overlays: tablet hover indicator, simplify sweep indicator, and the eyedropper swatch near the pointer.

`_ensure_scene_cache()` builds the widget-cached scene (`_render_scene_cache_rect`): dark gray outside the chapter, the chapter background color, a clip to the chapter bounds, root pages rendered in reverse hierarchy order, the selected drawing underlay, the effective grid, and a camera-space outline around the chapter. The scene cache is invalidated by document/selection changes, which is what makes ordinary editing and navigation cheap.

`render_preview()` uses the same recursive page/layer/object render functions but maps the complete chapter into the small navigator image. It omits editing overlays and live underlay.

## Hierarchy order and recursive layer rendering

Child lists and root-page lists are stored **frontmost first** because that matches the outliner. Rendering iterates them in reverse so QPainter draws back-to-front.

Mask-only layers and objects are skipped during normal scene rendering (unless the pass is explicitly rendering mask contributors or the entity is being interactively previewed). `_render_layer()` handles each kind:

- A layer with modifiers or an opacity-mask binding is delegated to `_render_modified_layer()`: its subtree is rendered into an isolated image, the modifier stack and opacity mask are applied, and the result is painted back. The isolated bounds expand by `blur_strength × 3.0` for blur or by the 100-pixel outline halo when needed.
- An open shape constructs a core ribbon and a wider clip ribbon. It paints the core, clips and renders ordinary children, paints the outer ring, then renders children that ignore the direct mask.
- A normal bounded layer paints its optional fill, clips ordinary children to its path, paints its inset-looking border by drawing a double-width pen inside the clip, then renders direct-mask-ignoring children above it.
- A compound layer builds one effective Boolean path, uses only the compound parent's style for the final fill/outline, recursively renders contributor contents, separately renders objects whose geometry reference is the compound, and finally renders direct-mask-ignoring children.
- Reversed radial/parent-shape gradients are rendered before their direct parent's artwork; normal object traversal skips them to prevent a second draw.

Layer opacity is multiplied through recursion. An object's `opacity_locked` determines whether it uses the direct parent's already-computed opacity or multiplies its own opacity as well.

## Shape path construction

`BoundGeometry` is converted to `QPainterPath` by `bound_path()` and `_single_bound_path()`.

- Ellipses have a primitive fast path based on four cubic nodes.
- Straight and cubic segments are supported in the same path.
- Additional contours become subpaths under an odd-even fill rule, preserving holes and disconnected regions.
- Vector-point rounding trims adjacent segments and inserts rounded joins.
- Unlocked Bezier rounding uses shared tangent construction for a smooth C1 join.
- De Casteljau subdivision is used when inserting/splitting curve segments.

Open shapes use `open_shape_mesh()` rather than a fixed-width QPen. The centerline is sampled (up to 1024 steps), width multipliers are interpolated between nodes, normals generate left/right ribbon edges, and explicit point/square/round cap geometry closes the mesh. A zero-width core is empty, though extra outline width can still form a visible ribbon. That same core silhouette can be used as a compound operand.

### Compound paths

`layer_effective_path()` caches the Boolean result per compound layer:

1. Start with the compound layer's own operand.
2. Recursively collect visible descendant layers until another compound takes ownership of its subtree.
3. Transform each descendant operand into the compound root's local coordinates.
4. Union Add operands, union Subtract operands, and skip Ignore branches.
5. Subtract the complete subtraction path from the additions and apply odd-even fill.

The cache is cleared on document/hierarchy changes. Flattening converts QPainterPath elements back into a custom `BoundGeometry` with additional contours.

## Tone masks

A tone mask is a chapter-level grayscale field. `render_tone_mask_field()` sums per-contributor coverage (each contributor's base render, respecting ancestor visibility, clipping, and opacity) plus optional sparse mask paint tiles, clamped to 0–1 in a float32 field. Contributor images are cached in an LRU with a 64 MiB byte budget keyed by entity state, and the whole field is cached keyed by `(mask_id, size, transform, contributor signature, mask revision)`.

- Mask paint tiles are drawn as translucent `#64B5F6` coverage multiplied by `0.35 × mask` alpha in the editing overlay, with a small LRU for painted tile images composited with `CompositionMode_Plus`.
- The overlay signature recursively walks contributors (entity JSON, pixel tile cache keys, ancestors, dependent masks) with cycle guards. Document changes invalidate the overlay fully; while mask painting is active, contributor images are preserved across one gesture so the editor does not flicker.
- Mask pencil pressure maps linearly between the configured From/To alpha values (defaults 0.0 to 1.0); without pressure sensitivity the To value is constant. Mask strokes replace alpha rather than accumulating it, so they can lower existing coverage. Each gesture snapshots touched tiles once and bumps the mask revision once.
- Parameter masks bind a mask to a target through black/white endpoints. For opacity, the bound value multiplies RGB and alpha; for modifier parameters the mask maps 0–1 onto the attribute's black-to-white range.

## Modifier rendering

`comic_editor/ui/modifier_rendering.py` is the pixel engine for the non-destructive stack.

- **HSL** performs a NumPy hue/saturation/lightness round-trip with per-parameter masks.
- **Blur** evaluates a per-pixel radius from the strength parameter and a focal ramp mask when in focal mode. A session `BlurPyramidCache` (64 MiB budget) stores premultiplied RGBA8 pyramid levels at effective radii `(0, 1, 3, 7, 15, 31, 63, 127)`, keyed by a BLAKE2b digest of the source pixels; each pixel's radius interpolates between the two neighboring levels. Scalar blur reuses the same pyramid.
- **Outline** computes the exact outside distance field with `scipy.ndimage.distance_transform_edt` (foreground = transparent pixels), cached in a 64 MiB `OutlineDistanceCache` keyed by a zlib CRC of the float32 alpha. Coverage is `clip(thickness_field + 0.5 - distance)` multiplied by `1 - alpha` and the outline opacity, then tinted by the outline color.
- `apply_modifier_stack()` applies modifiers in card order; each effect is blended by its intensity through `current × (1 - mask) + effect × mask`, where the intensity mask is the intensity value modulated by any bound parameter mask.
- `apply_opacity_mask()` applies a bound opacity mask to an isolated render.

Canvas-side caches (`_modifier_render_cache`, `_modifier_source_cache`, 64 MiB each) key by layer/object signatures that include pixel cache keys, selection transform preview quads, and eraser previews. Parameter, focal-rig, intensity, and mask edits reuse the isolated source render.

## Raster drawing and rendering

### Storage and rasterization

`TileStore` owns:

```text
object ID → (tile X, tile Y) → 256×256 QImage
```

Images use `QImage.Format_ARGB32_Premultiplied`. Tile coordinates may be negative because objects can have local pixels outside their original frame. A tile is allocated only when a dab touches it. Tone-mask paint uses the same store keyed by mask ID and persists under the chapter's `masks/` directory.

`paint_dab()`:

- finds every intersected tile;
- snapshots its pre-stroke image on first touch when undo capture is active;
- paints an antialiased circle or square with `CompositionMode_SourceOver`, or erases with `CompositionMode_Clear`; and
- batches samples per tile with one QPainter per tile, incrementally growing the alpha-bounds cache for non-erase dabs.

`paint_line()` spaces interpolated dabs along each input segment. Spacing derives from brush size and preset density; opacity interpolates between the endpoints.

The canvas converts active pointer/stylus pressure through the selected preset's cached size/opacity curves. The eraser ignores pressure curves for opacity and uses its configured size. The raster pencil also applies preset density, antialiasing, and start/end taper. The vector pencil reuses the size/opacity pressure channels but does not apply those raster-only preset fields. Mask pencil uses the mask-specific From/To mapping instead of normal pencil curves.

### Stroke transaction

At raster stroke start, the canvas records the frame and an initially empty map of pre-edit tiles. During motion it paints local-space segments, expands the interaction frame, grows the chapter when the world-space dirty region crosses the bottom, and optionally updates predictive ink. At release it:

1. paints the final dab when needed;
2. prunes touched tiles whose alpha is now empty;
3. calculates exact alpha bounds (Pillow fast path, pixel-scan fallback);
4. for pencil, expands the interaction frame to the union of its old area and padded alpha bounds; for eraser, refits it to padded remaining alpha; when fully erased, keeps the old frame; the pad is the 24-document-pixel `RASTER_FRAME_MARGIN`;
5. snapshots after-images for touched tile keys; and
6. pushes a `TilePatchCommand` containing tile and frame before/after state.

The frame is an interaction/selection affordance only. It neither clips rendering nor removes pixels. Shape Edit can change the frame but cannot shrink it inside actual alpha content.

Predictive ink extrapolates half the local delta of the active stroke into a preview tuple; it is drawn with a round-cap pen at reduced alpha (`round(110 × alphaF)`) clipped to ancestor layer paths, and never enters the tile store or undo history.

### Raster flood fill

For a selected Raster, Fill inverse-maps the pointer through its persistent quad and uses the local interaction frame as a finite domain. `TileStore` labels four-connected matching components inside each touched tile with SciPy, joins matching labels across tile edges, and allocates only components reached from the seed. Matching compares straight RGBA by maximum per-channel difference; a transparent seed ignores hidden RGB (alpha-only comparison). The changed tiles form one `TilePatchCommand`, while the persistent transform is left untouched. Fill profiles control tolerance, gap closing, narrow-area handling, area scaling, reference mode, and blend mode.

### Raster rendering and transforms

Normal raster rendering translates by object `(x, y)`, queries only tiles intersecting the local visible rectangle, and draws each image at `tile_index × 256`.

- Translation preview simply offsets drawing. Commit changes object position and preserves tile images.
- Projective/scale/rotation preview maps the original interaction rectangle to a destination quad with `QTransform.quadToQuad`; the stored `transform_frame`/`transform_quad` pair renders through the same projective transform.
- Whole-object projective re-tiling (`TileStore.projective_transform`) visits destination tiles intersecting the target polygon, inverse-maps their source query, draws only relevant source tiles, and replaces the object's sparse set.
- A cached full-widget background (the scene minus the selected object, rendered at device-pixel ratio) allows the selected raster alone to redraw during handle motion. Image objects skip the static cache so unchanged artwork above stays above.

## Vector input, geometry, and rendering

### Freehand to cubic data

The vector pencil records `FreehandSample` values containing position, pressure-derived width, and opacity. `comic_editor/core/vector_geometry.py` then provides the pipeline:

- deduplicate near-identical samples;
- resample the drag path at stable spacing;
- chord-parameterize spans;
- recursively fit cubic Bezier segments to the configured error tolerance;
- refine parameters with Newton iteration; and
- map width/opacity back to editable anchors.

The result is a `VectorStroke` of `VectorStrokePoint` anchors with optional incoming/outgoing controls. A single sample remains a dot. Gesture-time preview data is separate from the committed stroke graph.

### Stroke rendering

Each vector stroke is rasterized independently by `_vector_stroke_image()`:

1. Choose a render scale from canvas zoom and device-pixel ratio, clamped to 0.1–8 and limited to an 8192-pixel maximum dimension.
2. Flatten cubic spans while interpolating width and opacity.
3. Resample enough points for the current render scale.
4. Draw variable-width line segments and round joins into an 8-bit alpha mask using Lighten composition.
5. Draw explicit point/square/round end caps.
6. Fill an ARGB image with the stroke color and apply the mask through `DestinationIn`.
7. Draw the colored image into the stroke's document-space target rectangle.

The cache key includes drawing ID, stroke ID, the stroke render revision or a live-preview cache token, color, closure/caps, rounded render scale, and device ratio. Live eraser and selection previews use transient per-stroke tokens (`eraser-preview`, `selection-preview`). Cache hits are reinserted for LRU recency. Each canvas session has a 64 MiB byte budget; selective invalidation updates byte accounting, and a single image larger than the budget is rendered but not retained.

Each drawing also builds a lazy spatial index over 256-document-unit cells keyed by its drawing revision. Visible queries union only intersecting cells, sort their stroke indexes to retain paint order, and fall back to live geometry during edits that have not yet committed a new revision. Very large queries filter occupied cells instead of enumerating an unbounded grid.

The vector eraser keeps the committed model unchanged until release. Its live pass uses a device-pixel-aware scene image rendered without the active drawing, then composites unchanged strokes and the newest replacement strokes over that background. Affected strokes receive independent preview revisions, and the background/preview images are cleared on commit, cancel, selection/document changes, and errors. A separate 64-document-unit grid accelerates eraser stroke queries.

### Vector editing algorithms

The geometry module is Qt-independent and supplies:

- cubic evaluation, derivatives, splitting, subsegments, reversal, adaptive flattening, and arc length;
- nearest-point projection on curves, paths, and variable-width strokes;
- centerline/corridor hit testing with round or square metrics;
- corridor subtraction that splits cubics and preserves surviving outer spans;
- path/self intersections and intersection-bounded erase;
- local and whole-path Ramer-Douglas-Peucker-style simplification;
- tangent bridges and oriented endpoint connection; and
- planar face tracing, gap-closing virtual edges, narrow-area handling, and seed-face lookup.

Live vector edits invalidate only the changed stroke IDs. Structural restores clear the drawing cache. The model and undo restore paths preserve live object/stroke/point identity where possible so inspectors and selections do not retain stale references.

## Vector drawing selection

Rectangle, lasso, and stroke selection share one subsystem for Raster and Vector Drawing objects.

- Vector selections store stable stroke and point IDs.
- Raster selections copy the selected sparse pixel region and clear or retain source pixels according to the transform operation.
- Replace/Add/Remove is derived from current modifiers.
- Selected content gets a persistent four-corner frame with eight corner/edge handles, edge translation, a rotation affordance, and a movable pivot.
- Free mode edits individual corners/edge pairs. Uniform mode scales around the opposite anchor or pivot.
- The vector preview stores temporary point/control/width payloads and a revision token; commit pushes one undoable object patch.
- Select All chooses the complete active text while the canvas owns a text-edit session; otherwise it chooses all raster alpha content or all vector points/strokes as appropriate.

## Gradient rendering

Simple non-uniform radial gradients use Qt's `QRadialGradient`. More complex line, parent-boundary, uniform-distance, and outward fields use NumPy-generated scalar images.

The general pipeline is:

1. Build a resolution-limited grid over the relevant bounds (maximum dimension 768; 256 during interactive preview).
2. Convert QPainterPath geometry to polygon segments or a raster coverage mask.
3. Compute a float scalar per grid cell: arc-length amount, signed perpendicular distance, elliptical radial distance, or distance from/to a boundary.
4. Cache geometry projections and scalar fields independently of color (32 entries per cache).
5. Build a 1024-entry premultiplied RGBA ramp lookup table.
6. Convert scalar values to LUT indices, zero pixels outside coverage, wrap the contiguous NumPy buffer in a copied `QImage`, and cache the colored result.

Path signatures round element coordinates to three decimals. Ramp signatures include stable stop IDs, positions, and colors. This separation means color/stop edits do not redo geometry work.

Reversed radial and parent-shape fields create an outward padded boundary image. They are painted before the parent shape so the parent artwork covers their inner edge. During slider/handle drags, `_gradient_preview_active` selects the smaller grid; release clears the render cache for a full-resolution rebuild.

Speed lines are legacy: creation is rejected, legacy records are dropped at load, and `_render_gradient` early-returns for them. The historical manga-stroke pipeline (density, gap, close range, neighbor-correlated noise, thickness LUTs) remains in the code only as dead compatibility paths.

## Text rendering and editing

Text is laid out by `QTextDocument` with a pixel-size `QFont`, absolute letter spacing, plain text, block alignment, and a fixed text width.

- Strict text obtains the selected direct or compound `QPainterPath` bounding rectangle, converts it into the object's parent coordinates, applies margin, clips to that rectangle, and vertically offsets the document for top/middle/bottom alignment.
- Free text lays the document out in an axis-aligned local rectangle, maps it into a four-point destination quad, and clips before painting.
- Selection highlighting uses `QAbstractTextDocumentLayout.PaintContext`. The caret comes from the active block layout's cursor position.
- Canvas hit testing inversely maps a click into text layout coordinates and asks the document layout for a text position.
- Pointer drag updates the character range live; Qt word selection powers double-click, and a same-object third click within the platform interval selects the full text.
- Keyboard, clipboard, and IME changes update the live object. A local text history handles in-session undo; the entire session becomes one chapter command on commit.
- Text-only canvas controls are derived from the current selection and exist only in Text Edit. A floating overlay edits integer size and bold/italic; two screen-space right-edge handles scrub snapped size and kerning from their drag-start values and coalesce each drag into one chapter command.
- Before any ribbon, gizmo, or transform edit, an active typing transaction is committed so document undo order matches user action order.
- A free-text transform captures a static viewport without the selected object and rasterizes that object once into a device/zoom-aware transparent image (capped at 8192 pixels on a side). Each move projectively maps the cached image into the preview quad and derives selection controls from that quad; release or Escape clears both caches and restores normal `QTextDocument` rendering.
- In Text Edit, only a screen-space band around the dotted free-transform boundary begins translation. Transform handles keep priority and the quad interior remains an I-beam text target.

## Hit testing and tool input

All mouse, tablet, wheel, touch, key, double-click, and IME events enter the canvas and are dispatched according to `ToolKind` plus selected entity type.

- Shape hit targets are scaled inversely with zoom so handles and border tolerance stay screen-stable. Shape borders accept hits within 24 screen pixels of the stroked border; vector strokes need proximity within half the stroke width plus a screen-stable margin; gradient hits use ellipse ring distance.
- Free-shape previews render their real open core/outline and can inject the draft as a virtual Add/Subtract operand into an ancestor compound. Finish and operation buttons plus global S/O scrubbers are painted and hit-tested in screen space.
- Shape Edit prioritizes radius/delete/gizmo/control/node/primitive-handle/edge/insertion/interior targets.
- Object Select traverses the hierarchy front-to-back and respects visibility, opacity, ancestor masks, direct-mask escape, compound references, mask-only status, and page placement.
- Raster hits use the interaction frame and actual alpha behavior where required. Vector hits prefer visible stroke corridors before interiors.
- A raster press near but outside a frame is deferred until release, preventing an edge pencil stroke from being mistaken for translation; near-miss eraser presses expand their candidate bounds by half the eraser size.
- Grid snapping is resolved from the selected layer's nearest override, falling back to the chapter grid.

## Navigation performance

Touch hardware and high-frequency desktop pen input may report more events than a full recursive render can sustain. Touch navigation and modifier-drag mouse/pen navigation therefore keep only their newest pending packet and apply it with a zero-delay single-shot timer. Release synchronously applies the final pointer position. Each packet updates camera layout, clipping, transforms, overlays, and newly revealed content live; no viewport screenshot is stretched.

Vector stroke bitmaps are reused at unchanged scale for pan and rotation. Alt+Shift drag zoom, touch pinch, and Ctrl+wheel bursts temporarily reuse vector bitmaps from the gesture's starting scale while the rest of the scene stays live. Release/touch completion, or 120 ms without another Ctrl+wheel event, clears that override and performs one crisp final-scale redraw. Wheel zoom applies `1.0015^delta` per event. Alt+Shift drag zoom also preserves the initial click's document point at its original widget-space position while scale changes.

## Underlay rendering

The underlay is a live editing aid, not a separate object. While an object's underlay amount is nonzero, the scene render multiplies the object's own opacity by `1 - amount`, and `_render_selected_drawing_underlay` repaints it on top at `effective_opacity × amount`. The result is a ghost of the original at reduced opacity that the artist can trace over. It is intentionally absent from the chapter preview and navigator.

## Tablet and touch interop (Windows)

In tablet mode the canvas uses `comic_editor/ui/windows_input.py` to register the window for native touch with palm rejection (`TWF_WANTPALM`) and to set `MicrosoftTabletPenServiceProperty` with `TABLET_ENABLE_MULTITOUCHDATA` so touch navigation keeps working while the stylus hovers. `WM_TABLET_QUERYSYSTEMGESTURESTATUS` is answered to suppress system gesture handling.

## Rendering invariants and limitations

- Chapter width is always 1080; "infinite" vertical space means automatic growth, not an unbounded coordinate store.
- The whole document is not a single bitmap. Raster objects and tone-mask paint are sparse, while vectors/text/gradients/layers remain structured.
- OpenGL mode does not change document semantics or introduce a distinct shader path.
- Fills and masks are non-destructive. Moving or editing a mask can reveal existing raster/vector/text data.
- Raster interaction frames are not clips.
- Modifiers are isolated renders applied after the subtree paints; they never affect sibling or ancestor pixels directly.
- Tone-mask overlay, mask contributor caches, and mask paint are editing-time artifacts; masks persist as contributor lists plus sparse paint tiles.
- Legacy fill layers and vector fills are materialized into raster tiles at load and no longer exist at runtime.
- Underlay is an editing-only second rendering pass and is intentionally absent from chapter preview.
- Rendering and hit testing both use the hierarchy's frontmost-first contract; code that mutates child order must preserve it.
