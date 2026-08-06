# 2D canvas rendering and drawing implementation

## Architectural center

Almost all canvas behavior lives in `comic_editor/ui/canvas.py`.

- `_CanvasLogic` is a large mixin containing document binding, selection, camera math, rendering, hit testing, input dispatch, every drawing tool, text editing, and transform workflows.
- `RasterCanvasWidget` combines that mixin with `QWidget`.
- `GpuCanvasWidget` combines it with `QOpenGLWidget` and requests partial updates.
- `create_canvas(settings)` probes an offscreen OpenGL 3.3 context unless the renderer is forced to `raster`. If the probe fails, or Qt is using `offscreen`/`minimal`, it creates the raster widget.
- The GPU widget is not a separate shader renderer. Both backends run the same QPainter/QImage pipeline; OpenGL accelerates presentation/compositing through the widget surface.

The entry point asks Qt for a core-profile OpenGL 3.3 surface, no multisampling, and swap interval zero. QPainter antialiasing and explicit offscreen masks/images supply the actual 2D rendering.

## Coordinate systems

The renderer moves among four important spaces:

1. **Widget/device space** — pointer coordinates and the final window image.
2. **Document space** — the fixed 1080-wide, variable-height chapter.
3. **Layer-local space** — geometry after subtracting the accumulated translations of a layer and all ancestors.
4. **Object-local space** — raster tile keys, vector strokes, vector fills, and free-object geometry relative to an object's `(x, y)` inside its parent layer.

`camera_transform()` composes:

```text
widget center
  → rotation
  → uniform zoom
  → negative document camera center
```

The transform is inverted for widget-to-document input and for computing the visible document bounding rectangle. Camera X/Y are rounded in device space after navigation so fractional presentation translations are less likely to blur the chapter.

Layer world translation is calculated by walking `parent_id` links and summing each layer's translation. Object/world conversions add that value and then the object's own `(x, y)` where applicable. Reparenting gradients explicitly offsets dormant field geometry so their world position is preserved.

## Top-level paint pipeline

`paintEvent()` performs the normal frame in this order:

1. Fill the widget outside the document with dark gray.
2. If no chapter exists, draw the empty-state message.
3. Use the static-background fast path during a raster transform preview, if available.
4. Set the camera transform and fill the chapter rectangle with its background color.
5. Clip to the chapter bounds.
6. Walk root pages in reverse hierarchy order and recursively render them.
7. Draw the selected raster/vector underlay, if enabled.
8. Draw the effective grid.
9. Draw predictive raster ink and the live vector gesture preview.
10. Draw selection controls, page-gap overlay, and shape/raster creation previews.
12. Remove the chapter clip, reset to widget coordinates, outline the chapter, and draw screen-space tablet/simplify hover overlays.

`render_preview()` uses the same recursive page/layer/object render functions but maps the complete chapter into the small navigator image. It omits editing overlays and live underlay.

## Hierarchy order and recursive layer rendering

Child lists and root-page lists are stored **frontmost first** because that matches the outliner. Rendering iterates them in reverse so QPainter draws back-to-front.

`_render_layer()` handles each kind:

- A fill layer fills its parent's effective path and returns.
- An open shape constructs a core ribbon and a wider clip ribbon. It paints the core, clips and renders ordinary children, paints the outer ring, then renders children that ignore the direct mask.
- A normal bounded layer paints its optional fill, clips ordinary children to its path, paints its inset-looking border by drawing a double-width pen inside the clip, then renders direct-mask-ignoring children above it.
- A compound layer builds one effective Boolean path, uses only the compound parent's style for the final fill/outline, recursively renders contributor contents, separately renders objects whose geometry reference is the compound, and finally renders direct-mask-ignoring children.

Layer opacity is multiplied through recursion. An object's `opacity_locked` determines whether it uses the direct parent's already-computed opacity or multiplies its own opacity as well.

Outward reversed radial/parent-shape gradients are rendered before their direct parent's artwork. Normal object traversal skips them to prevent a second draw.

## Shape path construction

`BoundGeometry` is converted to `QPainterPath` by `bound_path()` and `_single_bound_path()`.

- Ellipses have a primitive fast path based on four cubic nodes.
- Straight and cubic segments are supported in the same path.
- Additional contours become subpaths under an odd-even fill rule, preserving holes and disconnected regions.
- Vector-point rounding trims adjacent segments and inserts rounded joins.
- Unlocked Bezier rounding uses shared tangent construction for a smooth C1 join.
- De Casteljau subdivision is used when inserting/splitting curve segments.

Open shapes use `open_shape_mesh()` rather than a fixed-width QPen. The centerline is sampled, width multipliers are interpolated between nodes, normals generate left/right ribbon edges, and explicit point/square/round cap geometry closes the mesh. A zero-width core is empty, though extra outline width can still form a visible ribbon. That same core silhouette can be used as a compound operand.

### Compound paths

`layer_effective_path()` caches the Boolean result per compound layer:

1. Start with the compound layer's own operand.
2. Recursively collect visible descendant layers until another compound takes ownership of its subtree.
3. Transform each descendant operand into the compound root's local coordinates.
4. Union Add operands, union Subtract operands, and skip Ignore branches.
5. Subtract the complete subtraction path from the additions and apply odd-even fill.

The cache is cleared on document/hierarchy changes. Flattening converts QPainterPath elements back into a custom `BoundGeometry` with additional contours.

## Raster drawing and rendering

### Storage and rasterization

`TileStore` owns:

```text
object ID → (tile X, tile Y) → 256×256 QImage
```

Images use `QImage.Format_ARGB32_Premultiplied`. Tile coordinates may be negative because objects can have local pixels outside their original frame. A tile is allocated only when a dab touches it.

`paint_dab()`:

- finds every intersected tile;
- snapshots its pre-stroke image on first touch when undo capture is active;
- paints an antialiased circle or square with `CompositionMode_SourceOver`; or
- erases with `CompositionMode_Clear`.

`paint_line()` spaces interpolated dabs along each input segment. Spacing derives from brush size and preset density; opacity interpolates between the endpoints.

The canvas converts active pointer/stylus pressure through the selected preset's cached size/opacity curves. The eraser ignores pressure curves for opacity and uses its configured size. The raster pencil also applies preset density, antialiasing, and start/end taper. The vector pencil reuses the size/opacity pressure channels but does not apply those raster-only preset fields.

### Stroke transaction

At raster stroke start, the canvas records the frame and an initially empty map of pre-edit tiles. During motion it paints local-space segments, expands the interaction frame, grows the chapter when the world-space dirty region crosses the bottom, and optionally updates predictive ink. At release it:

1. paints the final dab when needed;
2. prunes touched tiles whose alpha is now empty;
3. calculates exact alpha bounds (Pillow fast path, pixel-scan fallback);
4. for pencil, expands the interaction frame to the union of its old area and padded alpha bounds; for eraser, refits it to padded remaining alpha; when fully erased, keeps the old frame;
5. snapshots after-images for touched tile keys; and
6. pushes a `TilePatchCommand` containing tile and frame before/after state.

The frame is an interaction/selection affordance only. It neither clips rendering nor removes pixels. Shape Edit can change the frame but cannot shrink it inside actual alpha content.

### Raster rendering and transforms

Normal raster rendering translates by object `(x, y)`, queries only tiles intersecting the local visible rectangle, and draws each image at `tile_index × 256`.

- Translation preview simply offsets drawing. Commit changes object position and preserves tile images.
- Projective/scale/rotation preview maps the original interaction rectangle to a destination quad with `QTransform.quadToQuad`.
- Projective commit visits destination tiles intersecting the target polygon, inverse-maps their source query, draws only relevant source tiles, and replaces the object's sparse set.
- A cached full-widget background allows the selected raster alone to redraw during handle motion.

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

The cache key includes drawing ID, stroke ID, stroke render revision, color, closure/caps, rounded render scale, and device ratio. Live selection previews use a transient token. Cache hits are reinserted to approximate recency, and the cache is capped at 384 stroke images.

Vector fills paint first, in reverse owner order, and strokes paint afterward. A fill is a closed `BoundGeometry` drawn with `fillPath()`.

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

## Vector fill computation

The Fill tool converts drawing strokes to cubic/flattened paths and invokes planar face tracing. Gap closing can introduce virtual edges up to the configured threshold. The clicked seed chooses a face; a drag is resampled so faces between sparse pointer events are still found. Enclose mode collects faces within the lasso and unions them.

Post-processing can remove narrow neck regions and can geometrically expand/contract the area with round or rectangular behavior. The prepared path is serialized as a closed `BoundGeometry` and stored in a `VectorFillObject` together with the seed/lasso and a snapshot of fill settings. It is not recomputed automatically when owner strokes later change.

## Gradient rendering

Simple non-uniform radial gradients use Qt's `QRadialGradient`. More complex line, parent-boundary, uniform-distance, and outward fields use NumPy-generated scalar images.

The general pipeline is:

1. Build a resolution-limited grid over the relevant bounds (maximum dimension 768; 256 during interactive preview).
2. Convert QPainterPath geometry to polygon segments or a raster coverage mask.
3. Compute a float scalar per grid cell: arc-length amount, signed perpendicular distance, elliptical radial distance, or distance from/to a boundary.
4. Cache geometry projections and scalar fields independently of color.
5. Build a 1024-entry premultiplied RGBA ramp lookup table.
6. Convert scalar values to LUT indices, zero pixels outside coverage, wrap the contiguous NumPy buffer in a copied `QImage`, and cache the colored result.

Path signatures round element coordinates to three decimals. Ramp signatures include stable stop IDs, positions, and colors. The geometry, scalar, and colored-image caches are each bounded (normally 32 entries). This separation means color/stop edits do not redo geometry work.

Reversed radial and parent-shape fields create an outward padded boundary image. They are painted before the parent shape so the parent artwork covers their inner edge. During slider/handle drags, `_gradient_preview_active` selects the smaller grid; release clears the render cache for a full-resolution rebuild.

Speed lines use a separate manga-stroke pipeline. Closed contours are sampled at density-derived arc intervals and joined to a point center, the closest compatible custom-center boundary point, or an outward destination. Perpendicular line fields sample the guide and follow its local normal; Parallel fields use signed-distance offset curves and endpoint-oriented ramp travel. Each stroke keeps independent color and thickness coordinates, with close range and deterministic neighbor-smoothed noise moving only the thickness endpoint. Safe width is capped by neighboring stroke separation minus Gap. The rasterizer produces fractional edge coverage, then the shared RGBA LUT compositor applies color and opacity.

## Text rendering and editing

Text is laid out by `QTextDocument` with a pixel-size `QFont`, absolute letter spacing, plain text, block alignment, and a fixed text width.

- Strict text obtains the selected direct or compound `QPainterPath` bounding rectangle, converts it into the object's parent coordinates, applies margin, clips to that rectangle, and vertically offsets the document for top/middle/bottom alignment.
- Free text lays the document out in an axis-aligned local rectangle, maps it into a four-point destination quad, and clips before painting.
- Selection highlighting uses `QAbstractTextDocumentLayout.PaintContext`. The caret comes from the active block layout's cursor position.
- Canvas hit testing inversely maps a click into text layout coordinates and asks the document layout for a text position.
- Pointer drag updates the character range live; Qt word selection powers double-click, and a same-object third click within the platform interval selects the full text.
- Keyboard, clipboard, and IME changes update the live object. A local text history handles in-session undo; the entire session becomes one chapter command on commit.
- Text-only canvas controls are derived from the current selection and exist only in Text Edit. The overlay edits integer size and bold/italic; two screen-space right-edge handles scrub snapped size and kerning from their drag-start values and coalesce each drag into one chapter command.
- Before any ribbon, gizmo, or transform edit, an active typing transaction is committed so document undo order matches user action order.
- A free-text transform captures a static viewport without the selected object and rasterizes that object once into a device/zoom-aware transparent image. Each move projectively maps the cached image into the preview quad and derives selection controls from that quad; release or Escape clears both caches and restores normal `QTextDocument` rendering.
- In Text Edit, only a screen-space band around the dotted free-transform boundary begins translation. Transform handles keep priority and the quad interior remains an I-beam text target.

## Hit testing and tool input

All mouse, tablet, wheel, touch, key, double-click, and IME events enter the canvas and are dispatched according to `ToolKind` plus selected entity type.

- Shape hit targets are scaled inversely with zoom so handles and border tolerance stay screen-stable.
- Free-shape previews render their real open core/outline and can inject the draft as a virtual Add/Subtract operand into an ancestor compound. Finish and operation buttons plus global S/O scrubbers are painted and hit-tested in screen space.
- Shape Edit prioritizes radius/delete/gizmo/control/node/primitive-handle/edge/insertion/interior targets.
- Object Select traverses the hierarchy front-to-back and respects visibility, opacity, ancestor masks, direct-mask escape, compound references, and page placement.
- Raster hits use the interaction frame and actual alpha behavior where required. Vector hits prefer visible stroke corridors before fill interiors.
- A raster press near but outside a frame is deferred until motion exceeds four widget pixels, preventing an edge pencil stroke from being mistaken for translation.
- Grid snapping is resolved from the selected layer's nearest override, falling back to the chapter grid.

## Navigation performance

Touch hardware may report more events than a full recursive render can sustain. The canvas therefore keeps only the newest touch packet and applies it with a zero-delay single-shot timer. Each applied packet updates the camera and live-renders the document, avoiding transformed viewport-screenshot boundaries during pan, zoom, and rotation.

## Rendering invariants and limitations

- Chapter width is always 1080; “infinite” vertical space means automatic growth, not an unbounded coordinate store.
- The whole document is not a single bitmap. Raster objects are sparse, while vectors/text/gradients/layers remain structured.
- OpenGL mode does not change document semantics or introduce a distinct shader path.
- Fills and masks are non-destructive. Moving or editing a mask can reveal existing raster/vector/text data.
- Raster interaction frames are not clips.
- Vector fills are snapshots of traced faces, not live constraints.
- Underlay is an editing-only second rendering pass and is intentionally absent from chapter preview.
- Rendering and hit testing both use the hierarchy's frontmost-first contract; code that mutates child order must preserve it.
