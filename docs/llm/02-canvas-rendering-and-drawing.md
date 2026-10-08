# 2D canvas rendering and drawing implementation

## Architectural center

### Native artwork and manual disk backing

Camera zoom and display density do not add artwork samples. Exact projection
tiles use the native document grid (256px plus 2px gutters), vectors and text
source captures cap their derived density at one, and transient scene/ink
captures render into bounded native or smaller devices before presentation.
Original image/raster grids, modifier source frames, float precision, and
native sampling behavior remain intact. Grids, borders, and editing controls
remain screen-resolution overlays. See the root `AGENTS.md` constraint.

Generic modified Object and Layer subtree captures allocate their existing
dimensions in the current pixel contract. Forcing these source captures to
ARGB32 discarded native low-alpha/color samples before float stages and export;
Legacy captures still use the same ARGB32 format. This changes source storage,
not sampling density, effect grids, painter hints, or presentation resolution.

`render/cache.py::PersistentRenderCache` is an optional backing for the ordinary
projection, source, effect, tile-graph, and retained-stage cache access points.
It never traverses the document or implements a second renderer. Only an
explicit recording scope writes immutable results. QImage formats/raw bytes
and numeric array bits are compressed losslessly, including float working
pixels and retained checkpoint placement. Draft/live caches are excluded.

`ui/cache_dependencies.py` supplies stable source content fingerprints and
regional scene identities. Encoded source files are hashed asynchronously and
their stat stamps/content hashes are remembered in the cache index. Known dirty
pixel identities survive adopting an encoded backing on save; sealed file
records validate these identities across reopen and project-folder copying. Temporary
Qt image IDs and runtime document identities are absent from durable keys.
Simple raster dependencies select contributing source tiles; gutters can
conservatively invalidate an adjacent section. Modified/masked subtrees retain
their full upstream dependencies. Names/editor metadata are omitted; source
pixels, geometry, order, masks, modifiers, resolved fonts, pixel contracts,
external color configurations, renderer and library versions are dependencies.

`ui/disk_cache.py::DiskCacheController` uses the normal owning-thread render
service in short batches; image compression, source hashing and disk reads
run in the bounded IO pool. A row becomes green only after all its final tiles
and the index are committed. Reads restore values into existing memory LRUs.
Completed reads displaced by IO admission enter a separate 64 MiB handoff LRU
(one oversized completion may occupy it exclusively). This lets a restarted
capture consume its finished effect after reloading an evicted source, rather
than discarding that effect and repeating the same reads indefinitely. The
handoff retains the ordinary validated values and checkpoint metadata; clearing
or closing the backing releases it.
Failure/cancellation retains committed rows. Disk writes occur only after the
user clicks Cache to disk; normal viewing never automatically records results.

While a cache job runs the active document is read-only. Command history,
outliner edits, keyboard/mouse/tablet drawing, drop input and property controls
are locked, while camera/navigation, Cancel and chapter switching remain
available. Blender publication imports, pending clipboard paste results and
completed external image drops are deferred until unlocking. Active fill jobs
and unfinished editing previews must finish before caching starts. Chapter switches/shutdown drain already submitted
immutable disk writes and start no further rendering.

Canvas interaction and legacy scene/effect kernels live in
`comic_editor/ui/canvas.py`. Retained document-region rendering has a separate
service boundary in `comic_editor/render/`.

- `render/service.py::DocumentRenderService` owns region composition, fixed 4×4
  capture-block scheduling, explicit requests/results, and the retained tile
  cache. It imports no editor UI modules and can run with a non-widget backend.
- `render/projection.py::DocumentProjection` owns finished document tiles,
  configuration retention, resolution addressing, byte budgets, and invalidation.
  `ui/document_projection.py` preserves its previous import paths.
- Frozen `RenderDocument` and `RenderRequest` records identify document/view
  metadata, revisions, regions, scale, quality, and base/top output phases.
  `RenderResult` distinguishes exact, provisional, pending, failed, and stale
  work. Metadata snapshots do not copy all source pixels.
- `ui/scene_render_backend.py::CanvasSceneBackend` adapts requests to the existing
  scene kernels on the canvas thread, restoring temporary capture state on every
  exit. It is not a detached worker renderer.
  One capture temporarily memoizes complete object/layer/parameter dependency
  signatures in `ui/render_signatures.py`. Branch compatibility state and document
  generations remain distinct. Nested captures discard the outer memo on return;
  every new capture and ordinary direct signature call rechecks live records and
  source revisions, including edits made without model signals. This memo does
  not alter semantic artwork keys, exact pixels, or disk admission.
- Explicit `INTERACTIVE` scene requests may use the existing compact spatial
  preparation path from `ui/thumbnail_effects.py`: at most 256 pixels per source
  edge and 32768 source pixels before parameter fields and deformation setup.
  Local pixel distances and their mask endpoints scale together; world-space
  rigs keep their placement through inverse-scale stage mapping. Posterize
  simplify radius scales, while palette values and intensity endpoints do not.
  Canvas/overflow draft keys remain separate from exact source/effect and
  translation keys. Unsupported stacks, base-alpha, mask contributors,
  halftone color sources, cage source captures and tiling retain their prior
  grids. Exact canvas tiles, exports and source artwork keep native sampling.
  This is a transient sampling exception, with native-pixel and performance
  evidence in [editor-performance.md](../editor-performance.md).
  The opportunistic live compact mirror dispatch excludes Text: its established
  generic capture already follows the live free-text quad and preserves native
  outline pixels through commit, Undo and Redo. A stored mirror-source frame
  clips moving glyphs, while a bounds-only correction still differs from native
  commit pixels. Explicit spatial modifier and Navigator dispatch are unchanged.
- An explicitly owned Legacy widget presentation may use
  `ui/acquired_source_preview.py`: a whole-source preview of at most 256 pixels
  per edge and 32768 pixels, after the ordinary original decoder has completed.
  Camera zoom and DPR cannot increase this source representation. It shares the
  existing 256 MiB ImageStore LRU, not a separate pixel pool or decoder. A bounded
  metadata record proves actual decode/adoption, encoded identity, source stamp,
  generation and resident COW identity; history, actual/scoped precision and
  source color context remain in the preview key. Source/effect keys distinguish
  acquired input from ordinary fallback before a warm cache lookup. Missing
  provenance takes the ordinary route instead of waiting on a nonexistent job.
  Raster presentation opts in only at its owned scene-cache entry; an arbitrary
  detached interactive painter cannot opt in. A widget-owned live scene uses one
  transient INTERACTIVE viewport request, with at most one sample per canvas
  pixel and a 1 MiPixel core plus gutters, before drawing to the presentation
  surface. Native/exact tiles, exports, Float sources, Navigator, contributors,
  compound/outward/statistics, cage, halftone and tiling retain prior paths.
  All acquired and fallback preview tokens exclude exact/disk admission. This
  exception's correctness and timing evidence is in editor-performance.md;
  original cold acquisition still has to finish before the preview is available.
- Focused metadata history restores may retain completed original-image draft
  stages in the same ordinary modifier LRU. Only Legacy documents and Legacy
  stage keys qualify; fitted-to-parent images conservatively remain excluded.
  Their ordinary source identities include the complete unrounded effective
  destination quad, so subpixel parent-shape edits invalidate captured pixels
  even when their aligned source frame is unchanged. At most the newest
  64 records and `min(8 MiB, ordinary budget / 4)` survive as COW handles with
  unchanged semantic keys, after synchronous restore notifications and only
  when chapter/source stores, actual contract and document dimensions still
  match. Full replacement, source/preparation caches and worker pools retain
  their ordinary cancellation/clearing behavior. These entries remain
  provisional and cannot feed exact captures or disk recording.
- Deferred exact effect stages reject provisional upstream captures with
  `ProjectionPending` before native filtering or exact cache admission. A mask
  or modified descendant can also become provisional while parameter fields
  are sampled; that capture must retry after complete input arrives. Explicit
  synchronous captures preserve their original native path.
- `render/geometry_cache.py` reuses exact settings strings and pure bounds only
  inside owning-thread capture scopes. Each lookup snapshots all current model
  fields into typed immutable tokens, preserving unsignaled nested mutations;
  bounds and matrices remain unrounded. Cached rectangles are copied before
  return, nested scopes clear their outer memo, and worker contexts cannot
  inherit this thread-local state. It changes no artwork or durable cache key.
- `ui/source_images.py` yields cold original-image decoding only in explicitly
  deferred exact or bounded interactive scene captures. A worker runs the
  unchanged `ImageStore` decoder against an immutable encoded pin; source/store,
  history, generation and file-stamp checks precede owner-thread adoption into
  the existing decoded LRU. Header-derived peak estimates use the ordinary
  worker admission budget. Native decode handoffs survive effect-only live
  cancellation, while document replacement/full cancellation retires them.
  Native decode completions use that retained/ImageStore handoff rather than
  occupying the derived-effect LRU and evicting current compact stage prefixes.
  Temporary handoff keys cannot enter disk caches. Default source APIs and
  detached exact/export captures remain synchronous at their original grids,
  formats, precision and color profile.
- Float documents use `ImageStore.native_image()` and `render/pixels.py`'s
  explicit source color conversion before composition. Deferred float sources
  decode original pixels and convert off the owning thread, with contract/color
  configuration context in transient handoff keys. Converted sources share the
  existing 256 MiB ImageStore residency budget; source replacement/history and
  file stamps validate adoption, and oversized frames use the existing bounded
  worker handoff. Legacy documents retain their byte display-source path even
  inside a temporary float effect scope. Float native PNG export renders the
  ordinary exact scene under its pixel contract and quantizes straight RGBA16
  channels once, avoiding Qt's lossy integer premultiplied intermediate.
- `ui/document_projection_features.py` supplies explicit requests and camera
  priority/deadlines, translates results into UI publication state, and presents
  finished artwork. GPU presentation and editing overlays remain in the UI.
- Full/regional document edits invalidate the service cache. Reentrant edits,
  view changes, and document replacement cannot publish stale captured pixels.
- `render/tile_graph.py` evaluates compiled effect nodes recursively on stable
  256px addresses. Nodes declare required input rectangles or disjoint islands;
  identities contain semantic frames, transforms, source generations and the
  upstream parameter/mask prefix. `ui/tile_effects.py` supplies scene kernels,
  bounded cache adapters and lazy object/layer/raster source capture. Exact
  regional requests above 256² pixels use this path; small frames retain the
  existing kernels. Provisional captures and special-purpose contexts opt out.
  Exact stage preflight uses the existing stage keys, including full unrounded
  mapped origins, before assembling upstream pixels. Successful non-null
  preflight results enter the same graph key and retained handoff as evaluated
  tiles; cache-only requests remain read-only. The owning context is checked
  again after preflight worker adoption before pixels can enter that handoff.
  Only whole-frame graph owners receive shared-prefix protection; ordinary
  regional tiles and viewport output aliases remain in the bounded normal LRU.
  Their private assembly coverage preserves progress without treating every
  one-use tile as a shared predecessor.
  Explicitly deferred exact
  captures also retain unfinished native frame assembly privately in
  `EffectJobs`: only completed tiles are copied into that buffer, and retries
  skip its covered addresses. Mutable partials have no ordinary cache or disk
  entry and never feed workers. Full node identity, requested aligned frame,
  format/tile grid, chapter/store/history/revision and pixel contracts guard
  reuse. Partial and complete buffers share the existing retained byte/record
  limits, including conservative coverage metadata; completed COW storage
  deduplicates aliases. Cancellation/context replacement rejects old progress.
  Pure, synchronous, nonexact and cache-only graphs retain their original
  behavior. A completed predecessor can leave the retained pool after its
  caller owns a COW image; worker admission accounts for the detached source.
  No chain of ancestors is pinned indefinitely.
  When a proved distortion stage's full input/output pair cannot fit, its
  deferred request uses the existing whole-frame exact kernel and exclusive
  oversized admission, restoring regional policy on every exit. This also applies
  when its input or output native float32 RGBA preparation exceeds the unchanged
  256 MiB preparation cache and a later complete-source stage demands its entire output;
  final partial-viewport stages retain regional demand. Twirl, Deform,
  Mesh Warp, Lens Distortion and Pinch/Punch have masked native-byte equivalence
  coverage across fractional affine/projective mapping and byte/float sources.
  Radial retains its output-local 96px integration partition; Cage retains its
  GPU FBO sampling extent. Unproved stages and full outputs above the existing
  64-megapixel limit are not promoted. See [performance evidence](../editor-performance.md).
  Deferred exact CPU Twirl, Deform, Mesh Warp, Lens Distortion and Pinch/Punch
  at uniform 100% intensity with no parameter masks omit the padded blend
  image: the existing native kernel already returns the entire finished stage,
  so that image would never participate in blending. Source pixels, requested
  output grid, rig mapping, keys and precision remain unchanged. The old native
  working-memory reservation remains in place, calculated from the current Qt
  image format and aligned row stride; the 64-megapixel allocation guard also
  remains. Detached source/rig snapshots and normal context cancellation still
  protect worker publication. Live, synchronous, masked, lower-intensity,
  invalid-mapping and unproved kernels keep their original padded blend path.
  When explicitly deferred exact regional demand reaches one of those five
  proved CPU warps with native float32 source preparation above the unchanged
  256 MiB preparation-cache budget, the adapter groups work into fixed 512px
  document-grid output batches clipped to the semantic frame. Canonical graph
  addresses and exact tile keys remain 256px. Preflight and evaluation request
  the same existing native kernel, full source grid, rig mapping and batch
  target; only a completed batch is cropped into the requested tile. Copied
  crops cannot consume the full batch's retained worker handoff, so sibling
  requests can reuse it. Parameter masks keep their world mapping over that
  native batch. Regional-policy flags restore on every exit. Synchronous,
  live, navigator, provisional, unproved and over-64-megapixel paths retain
  their prior demand; cache-only requests may read completed batches but do
  not compute or admit them. Native byte/float16/float32 comparisons cover
  affine/projective placement, HDR and transparency, varying masks and the
  uniform-100% allocation shortcut. Admission and retained budgets are unchanged.
- Point operations, native single-pass Kuwahara, sharpen and solid outlines
  have finite footprints. Generic stacks retain float intermediates until their
  original final RGBA8 conversion; spatial stacks retain per-stage rounding.
  Dither and Kuwahara sampling retain full-image coordinate origins.
- `render/blur_regions.py` requests individual pyramid regions using original
  frame dimensions and Pillow-compatible fixed-point bilinear coefficients.
  Odd-size levels, legacy alpha behavior and masked-radius interpolation match
  the reference path. Its immutable, thread-safe level cache has a 32 MiB budget.
  Deferred jobs resolve source dependencies on the GUI thread, then perform
  filtering in workers without consulting canvas state. Dependency discovery
  computes the sampling-index envelope directly, preserving the padded source
  rectangles without constructing unused bilinear weights; filtering still
  uses the original coefficients and rounding.
- Integer-translated Array copies request inverse-mapped source islands.
  Qt's rotated/scaled/reflected sampling keeps a shared full frame. Pattern,
  contour, reduced-scale/repeated Kuwahara and smudge nodes also retain shared
  complete-frame dependencies; cage/mesh/radial kernels retain full source
  access while requesting bounded output. All-solid outline stacks retain their
  specialized reference path. Sharpen radius/strength masks and generic float
  stacks with strength-masked blur retain whole-field extrema decisions. See
  [the tile evaluator plan and results](../tile-evaluator-plan-2026-10-01.md).
- Regular square/hexagonal halftone crops keep the complete frame's lattice and
  their original padded source/blur samples. Analytic-AA marks compute geometry
  only for requested output pixels, avoiding discarded-margin arrays. Blob,
  liquid, line and ring marks retain their padded output neighborhood because
  their coverage uses neighboring derivatives. Native byte and float16/float32
  working results retain the reference rounding and alpha behavior. Analytic
  marks evaluate geometry in 128-row chunks while sharing the same padded
  source blur. Deferred regional jobs detach that identical padded rectangle
  with original frame/origin metadata, rather than retaining a whole frame.
  Their admission estimate covers source/blur arrays, bounded geometry, output
  conversion and scalar blending; multi-tile requests keep their original
  per-tile padding. Saved Rewritten Gradient5 parameters match native bytes
  and float16/float32 HDR working values in the regression suite. Isolated
  October 2026 probes measured 108.63/115.80/130.16 MB admission estimates above
  100.45/107.52/120.89 MB peak increases in process private committed bytes for
  byte/float16/float32.
  Cache keys, fixed source/effect grids and the 256 MiB admission budget are
  unchanged.
- Non-projection scene rendering, asset capture, and export remain reference
  paths using the existing kernels. See
  [the implementation plan](../renderer-service-plan-2026-09-29.md).

- `_CanvasLogic` is a large mixin containing document binding, selection, camera math, rendering, hit testing, input dispatch, every drawing tool, text editing, tone-mask and modifier integration, and transform workflows.
- `RasterCanvasWidget` combines that mixin with `QWidget`.
- `GpuCanvasWidget` combines it with `QOpenGLWidget` and requests partial updates.
- `create_canvas(settings)` probes an offscreen OpenGL 3.3 context unless the renderer is forced to `raster`. If the probe fails, or Qt is using `offscreen`/`minimal`, it creates the raster widget.
- Both backends use the shared QPainter/QImage scene pipeline. In Auto/GPU mode, cages additionally use an OpenGL 3.3 shader through `gpu_textures.py`; a private context retains its latest source texture/output framebuffer and restores the caller's context. Unsupported contexts or oversized outputs fall back to CPU sampling.
- `paintEvent` always ends its widget painter in `finally`, including early returns and exceptions. Deferred capture tracebacks can retain Python frames and delay painter destruction. Relying on that destruction allows an old painter to end Qt's reused OpenGL engine during a later frame, crashing smear previews or native tile presentation. `_paint_canvas_frame` draws within this explicitly bounded lifetime.
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

The Navigator opts into the ordinary original-source decode worker only during
its owned band capture. A Pending band stays private and does not advance or
publish; its 120 ms metadata-only waiter also resumes failed, null or canceled
jobs. Build identity includes source decode generation, document/history,
pixel/color configuration and the owning stores. Global derived-ready signals
keep their zero-argument interface. Their borrowed scope/key and capacity-retry
metadata exists only during synchronous emission and is restored in `finally`.
An unchanged build awaiting that exact current immutable original can resume
on its successful decode or a capacity-only retry without requesting another
full pass. Encoded identity, generation, file stamp, history and working-color
representation must still match. Existing required followups are preserved;
unrelated/unknown notifications and real edits remain conservative. This avoids
repeated full previews when the chapter's originals exceed decoded residency.
Only a validated successful source/capacity notification shortens the existing
private continuation to the next Qt turn (1 ms). A running or budget-denied
source restores the 120 ms metadata wait, and active input still defers it.
Worker admission, exclusive oversized jobs and all memory limits are unchanged.

## Hierarchy order and recursive layer rendering

Child lists and root-page lists are stored **frontmost first** because that matches the outliner. Rendering iterates them in reverse so QPainter draws back-to-front.

Mask-only layers and objects are skipped during normal scene rendering (unless the pass is explicitly rendering mask contributors or the entity is being interactively previewed). `_render_layer()` handles each kind:

- A layer with active, unmuted modifiers or an opacity-mask binding is delegated to `_render_modified_layer()`: its subtree is rendered into an isolated image, the modifier stack and opacity mask are applied, and the result is painted back. A stack whose modifiers are all muted bypasses isolation unless an opacity mask remains. The isolated bounds expand by `blur_strength × 3.0` for blur or by the 100-pixel outline halo when needed. A shape stack containing an active Outline omits only the final own-shape clip so the halo can extend outward while inherited clips remain in force.
- An open shape constructs a core ribbon and a wider clip ribbon. It paints the core, clips and renders ordinary children, paints the outer ring, then renders children that ignore the direct mask.
- A normal bounded layer paints its optional fill, clips ordinary children to its path, paints its inset-looking border by drawing a double-width pen inside the clip, then renders direct-mask-ignoring children above it.
- A compound layer builds one effective Boolean path, uses only the compound parent's style for the final fill/outline, recursively renders contributor contents, separately renders objects whose geometry reference is the compound, and finally renders direct-mask-ignoring children.
- Reversed radial/parent-shape gradients are rendered before their direct parent's artwork; normal object traversal skips them to prevent a second draw.

Layer opacity is multiplied through recursion. An object's `opacity_locked` determines whether it uses the direct parent's already-computed opacity or multiplies its own opacity as well.

## Shape path construction

`shape_contours.py` compiles `BoundGeometry` into shared fill paths and source-attributed logical edges. The canvas `bound_path()` and `_single_bound_path()` entry points delegate to this builder; rounded geometry is no longer independently reconstructed for outlines.

- Ellipses have a primitive fast path based on four cubic nodes.
- Straight and cubic segments are supported in the same path.
- Additional contours become subpaths under an odd-even fill rule, preserving holes and disconnected regions.
- Vector-point rounding trims adjacent segments and inserts rounded joins.
- Unlocked Bezier rounding uses shared tangent construction for a smooth C1 join.
- De Casteljau subdivision is used when inserting/splitting curve segments.

Open shapes use `core_mesh()` through the canvas `open_shape_mesh()` adapter. Core widths preserve their eased interpolation, now measured by arc distance on each logical edge, and retain point/square/round endpoint caps. Independent contours never connect to each other. The core, expanded child mask, outline subtraction, visual bounds, and compound operands share the same centerline. A zero-width core is empty; outline width can still form a visible ribbon.

### Compound paths

`layer_effective_path()` caches the Boolean result per compound layer:

1. Start with the compound layer's own operand.
2. Recursively collect visible descendant layers until another compound takes ownership of its subtree.
3. Transform each descendant operand into the compound root's local coordinates.
4. Union Add operands, union Subtract operands, and skip Ignore branches.
5. Subtract the complete subtraction path from the additions and apply odd-even fill.

The cache is cleared on document/hierarchy changes. Flattening converts QPainterPath elements back into a custom `BoundGeometry` with additional contours.

## Cage sampling

`core/cage.py` evaluates document-space displacement over a 2–16 point lattice,
blending bilinear and Catmull–Rom interpolation. `cage_rendering.py` builds a
shared triangle mesh and inverse-samples premultiplied pixels in bounded strips.
GPU sampling uses the same nearest/bilinear/Catmull–Rom filters. Cropped cage
outputs retain the complete incoming source, including pixels displaced from
outside the viewport. Intensity and masks blend the transformed stage with its
incoming image. CPU live work uses small drafts and the bounded latest-job queue.

`cage_features.py` manages transactions, remembered modifier selection, and
16 ms pointer coalescing with a release flush. Raster baking prepares all
targets before mutation; `cage_vectors.py` adaptively transports and refits
editable vector curves, adding anchors where necessary. Shape cages also warp
compound contributor geometry. See [limits and measured timings](../cage-transform.md).

## Tone masks

Object/group translations carry wholly owned modifier rigs and private mask
geometry through `ui/attached_translation.py`. Ownership is resolved across
opacity and parameter bindings; a shared rig/mask moves once only when all its
consumers move together. Contributor references remain live scene links, so
moving an attached mask never mutates unrelated contributor artwork.

Mask paint uses a persistent document-space `paint_offset` over its original
sparse grid. Rendering, the blue overlay, painting, erasing, lasso and wand
editing use that origin. Moving paint does not retile or resample source pixels.
Object/group previews transform value copies and cancel without model writes.

A tone mask is a chapter-level grayscale field. `render_tone_mask_field()` sums per-contributor coverage (each contributor's base render, respecting ancestor visibility, clipping, and opacity) plus optional sparse mask paint tiles, clamped to 0–1 in a float32 field. Contributor images are cached in an LRU with a 64 MiB byte budget keyed by entity state, and the whole field is cached keyed by `(mask_id, size, transform, contributor signature, mask revision)`.

- Mask paint tiles are drawn as translucent `#64B5F6` coverage multiplied by `0.35 × mask` alpha in the editing overlay, with a small LRU for painted tile images composited with `CompositionMode_Plus`.
- The overlay signature recursively walks contributors (entity JSON, pixel tile cache keys, ancestors, dependent masks) with cycle guards. Document changes invalidate the overlay fully; while mask painting is active, contributor images are preserved across one gesture so the editor does not flicker.
- Mask pencil pressure maps linearly between the configured From/To alpha values (defaults 0.0 to 1.0); without pressure sensitivity the To value is constant. Mask strokes replace alpha rather than accumulating it, so they can lower existing coverage. Each gesture snapshots touched tiles once and bumps the mask revision once.
- Parameter masks bind a mask to a target through black/white endpoints. For opacity, the bound value multiplies RGB and alpha; for modifier parameters the mask maps 0–1 onto the attribute's black-to-white range.

## Modifier rendering

`comic_editor/ui/modifier_rendering.py` is the pixel engine for the non-destructive stack.

- **HSL** performs a NumPy hue/saturation/lightness round-trip with per-parameter masks.
- **Blur** evaluates a per-pixel radius from strength and the optional focal ramp. `BlurPyramidCache` (64 MiB) stores levels at radii `(0, 1, 3, 7, 15, 31, 63, 127)`, keyed by algorithm and a BLAKE2b source digest. Normal Blur keeps Pillow images in premultiplied `RGBa` through every reduction, enlargement, and blend. Legacy retains the old `RGBA` branch exactly: feeding it premultiplied data causes a second premultiplication/unpremultiplication during resizing and can produce RGB greater than alpha, explaining the colorful distortion on transparent composites.
- The Legacy byte-input GPU blur accepts explicitly verified Pillow releases
  12.2.0 and 12.3.0; unknown resamplers return to the ordinary CPU route before
  graphics access. Its integer textures, 22-bit coefficients, separable pass
  rounding, conversion/blend tables and final normalization preserve CPU
  float32 bits. Native 12.3.0 evidence covers all original blur/worker assertions
  and 1380 expanded raw-bit comparisons, with zero differences. Separate
  1025x1025 cold/warm CPU–GPU controls and unchanged resource accounting are
  recorded in `docs/performance-investigation-2026-10-06.md`. This compatibility
  entry changes no source/effect grid, floating-source policy, semantic key,
  renderer epoch or persistence route; future releases require fresh evidence.
- **Radial Blur** uses `radial_blur.py`: symmetric midpoint angular integration with premultiplied bilinear sampling, transparent out-of-source samples, and adaptive sample counts based on pixel arc length. Integration uses 96×96 tiles; zero-angle mask regions are exact identities. The complete incoming stage remains available when rendering a cropped output, so displaced centers do not lose offscreen source pixels. `effect_geometry.radial_sweep_bounds` includes angular extrema and mask endpoint angles; the pipeline adds bilinear support and preserves ancestor clipping.
- `radial_pipeline.py` caches angular integration separately from the final intensity mix. Its key includes the semantic upstream source, center, angle/angle-mask signature, full mapping, output shape and origin, but excludes intensity and its mask. Integration is retained as a float-premultiplied QImage in the existing bounded caches, so the final blend still rounds to RGBA8 only once. An intensity-gradient edit also reuses in-flight integration and blends its latest mask after completion.
- Bounded provisional canvas radial integrations may reuse the ordinary modifier LRU under an explicit preview namespace. Eligibility requires at most 1 MiB of current native input pixels, 256 KiB of evaluated angle data and 1 MiB of float32 integration output. The key hashes visible native rows and the actual evaluated angle field, and includes all nine unrounded mapping coefficients, the existing integration dependencies and actual/scoped pixel and loaded color contexts. Every call blends the current base and intensity field again; exact rendering, navigator work and durable admission exclude these entries. Native byte/half-float/float tests cover unsignaled borrowed-buffer edits, mask endpoints, mapping, source/color contexts, eviction and poisoned preview entries. The supplied chapter's diagnostic found two identical integration inputs across all eight genuine move/scale drags; hashing twenty native inputs took 0.895 ms total and twenty angle fields took 0.078 ms total. These are diagnostic hash costs, not a measured UI speedup.
- Document changes with an active Radial Blur mark their identity/revision for deferred exact projection. The previous complete view remains visible until all current tiles are ready. Radial handles and attached mask-gradient drags permit deferral during pen contact; painting and unrelated live previews retain their existing synchronous policy. Changed radial handles cancel obsolete snapshots without dropping retained outputs, no-op input avoids invalidation, Escape restores the drag snapshot, and radial cards synchronize controls without emitting parameter edits.
- Raster stacks containing an active Radial Blur use the same tile-coordinate stage renderer as Raster Apply and transform the processed result once with nearest sampling. Prefix baking records its output frame (including transparent padding) for later stages, avoiding both a transform/resampling-order mismatch and a blur-pyramid alignment change after Apply. Legacy Raster stacks without radial baking are unchanged.
- Interactive radial work uses the single-worker, 256 MiB-budgeted latest-request queue (`effect_jobs.py`), immutable inputs, cancellation checkpoints, and main-thread publication. New sampling requests replace superseded jobs; document replacement cancels them. Exact projection retains the previous complete view during pending work; the raster preview may show the incoming source without caching it as a finished effect. Exports, rendered masks, Rasterize, and Raster Apply remain synchronous at the same integration quality. Source/output LRUs retain independent 64 MiB budgets, and worker results use the existing bounded 256 MiB retained pool. One oversized exact job is admitted exclusively instead of falling back to computation on the GUI thread.
- **Outline** computes the exact outside distance field with `scipy.ndimage.distance_transform_edt` (foreground = transparent pixels), cached in a 64 MiB `OutlineDistanceCache` keyed by a zlib CRC of the float32 alpha. Coverage is `clip(thickness_field + 0.5 - distance)` multiplied by `1 - alpha` and the outline opacity, then tinted by the outline color.
- `apply_modifier_stack()` skips muted modifiers and applies active modifiers in card order; each effect is blended by its intensity through `current × (1 - mask) + effect × mask`, where the intensity mask is the intensity value modulated by any bound parameter mask.
- `apply_opacity_mask()` applies a bound opacity mask to an isolated render.

Canvas-side caches (`_modifier_render_cache`, `_modifier_source_cache`, 64 MiB each) key by layer/object signatures that include pixel cache keys, selection transform preview quads, and eraser previews. Parameter, focal-rig, intensity, and mask edits reuse the isolated source render.

Solid color overlay picker edits use a transient modifier value copy through
`effective_preview_modifier()`. The picker forwards color, alpha, hex, palette,
history and active-well changes; a 16 ms single-shot timer coalesces pending
colors without restarting on each input. Preview publication invalidates the
canvas and signals `visualChanged`, retaining unchanged source captures without
changing saved artwork or undo history. Apply retires the pending timer and
commits the latest picker color as one command; Cancel or inspector destruction
retires the preview. Document/history replacement guards reject stale edits.
The ordinary native preview path and live-preview disk-cache exclusion apply.
`tests/test_overlay_modifiers.py` checks warm cache edits, visible picker input,
preview/commit pixel equality, source reuse, cancellation, undo/redo, queued
final input and inspector deletion. On 2026-10-04, 24 warm color edits of a
1000×700 shape on a 1200×900 raster canvas at zoom 1 rendered in a median
32.83 ms (p95 37.63 ms), excluding the timer delay, with two retained source
entries. Sampling and color/precision contracts are unchanged.

`ui/translation_cache.py` supplies relative-placement aliases through the same
effect-cache accessors and optional disk backing. Aliases include source pixels,
the subtree's geometry/effects/masks, native capture phase, linear mapping,
concrete stage extents, and the pixel contract. Completed opacity-mask crops
retain the canonical upstream stage identity. Coordinate subtraction roundoff
is canonicalized at 10 decimal places, finer than existing semantic frame keys;
parameter values and pixel bits are unchanged. There is no alternate renderer.
Linked scene masks, external effect sources, projective mappings, strict text
layout and live source edits conservatively use the existing dependency path.
That eligibility check includes each descendant layer's visibility and live
geometry tokens, even when the parent's own live tuple is empty. Capture-local
signature memoization includes interactive/export mode, selection and disk
capture context, so a selected mask-only layer cannot become an ordinary exact
source or a durable projection through a parent effect.
Fractional placement changes recapture parent-space artwork; raster-local
effects retain their original grid and are placed after processing. Drafts and
live previews still cannot enter the durable exact cache.

Translation aliases admit stage outputs only when their actual bounds match
the reference plan's final placement. A tile-graph crop of a retained spatial
stage (such as Outline followed by Smudge) keeps its ordinary regional cache
identity; it must never be restored across the complete stage frame. The
`native-artwork-20261007-source-context-2` renderer identity excludes older
durable aliases/projection
tiles that could contain placement or descendant-visibility errors. Sampling
density is unchanged. The same identity also isolates earlier float imports:
straight integer RGBA samples now normalize directly into owned float32 storage
before ICC/color conversion and premultiplication. This avoids Qt's integer
premultiplied intermediate at low alpha while preserving native 8/16-bit grids,
original source bytes/profile, float16/float32 contracts and legacy behavior.

`tests/test_attached_translation.py` compares moved previews/commits against
cold native renders for focal blur, Array, Mirror, Radial Blur, deformed cages,
twirl, mesh warp and smudge, plus nested effects and painted/limited-gradient
masks. Reuse assertions count source captures, effect-parameter evaluation and
mask sampling; qualifying moves perform none. It also covers fractional raster
placement, undo/redo, mask editing, shared ownership and disk-backed exact reuse.

The offscreen CPU probe `python tests/benchmark_attached_translation.py` measures
twelve translations of a 256×192 deformed cage with parameter and opacity masks.
On 2026-10-03, enabling relative-placement reuse reduced the median full-preview
capture from 44.711 ms to 3.557 ms. Source captures/effect-field evaluations/mask
fields fell from 12/12/24 to 0/0/0. These are local CPU timings, not display
latency guarantees, and artwork density, filters and precision are unchanged.

Blur measurements on this machine (September 5, 2026; `python tests/benchmark_free_text_blurs.py`): warmed 128×128 normal Blur edits had a 0.43 ms median, one pyramid build, and 0.08 MiB cached. A 50×40 target's radial angle edits had a 25.28 ms median with one reused source stage (0.01 MiB source / 0.08 MiB results). The 128×128 radial integration measured 69.34 / 774.25 / 1596.72 ms for 15° / 180° / 360°, respectively, with a 1.45 MiB traced peak. These are full-quality computation times, not UI frame times: larger/full-circle radial jobs are asynchronous and are not claimed to fit a 16.7 ms frame budget.

Mirror uses `effect_geometry.py` for document-space reflection and stage-wise
bounds, and `effect_pipeline.py` for reusable sources, output-stage caches,
parameter masks, and baking. A trailing unmasked Mirror at full target opacity
draws the two source placements directly rather than allocating the empty gap.
Other stages propagate required output regions backward; reflection samples
the retained incoming source even when it lies outside the output region.
Raster sources use nearest sampling. Both cache families remain bounded.
Individual effect images have a 64-megapixel allocation guard.

`shape_outline.py` renders both default and edited outlines:

- Constant-width connected runs use native round stroking (a closed run has no end caps). Long uniform line runs receive bounded-error simplification for rendering only; source anchors remain unchanged.
- Variable-width edges use adaptive strips, analytical curve normals, and arc-distance width interpolation. Rounded-corner halves belong to adjacent edges. Joins/caps are generated at real boundaries, not at every tessellation sample.
- Positive winding batches all pieces. Binary-exact subpixel coordinate snapping makes shared arc/strip vertices identical, then a single normalization before clipping avoids Qt's overlapping-path intersection failure; incremental boolean unions are forbidden. Coverage is painted once, so overlap cannot double alpha. Connected variable widths get outer round-join sectors, not full endpoint disks that would create width bulges.
- Closed coverage clips inside the fill. Open coverage subtracts the core and preserves its configured outer endpoint caps; hidden-edge breaks are round. Fill/mask topology never depends on outline visibility.
- Precision follows output scale, including projective stretch, in stable power-of-two buckets. Boolean normalization/clipping runs in scaled coordinates. Regression tests compare variable-curve boundaries to a finer reference with a 0.25-output-pixel limit through 16× zoom.
- Raw source-edge hit testing is separate from painted coverage. Continuous closest-point projection replaces the old 33-sample hit test, so long or transformed hidden edges remain hittable.

`shape_outline_compound.py` splits final boundaries into attributed source spans using adaptive segment overlap/intersection endpoints and a spatial index. Adjacent spans are rejoined before stroking. Source widths/visibility survive subtraction and independent Mirror contributions; unmatched spans use the compound baseline. Attribution is cached independently of styling; it no longer uses a fixed 1.5-unit envelope.

Open compound contributors attribute their normalized core ribbon surface to its originating edge strips and half-join sectors, rather than matching the final boundary to a distant centerline. The cached surface keeps independent endpoint arc weights and outgoing-edge ownership, so hidden edges and changing outline widths propagate to both sides without rebuilding the core.

Each canvas owns one 64 MiB `OutlineCache`, with conservative key/path accounting and LRU eviction. It separately caches compiled contours, fill/core geometry, constant runs/variable edges, compound attribution, and outline results/bounds. Width edits reuse unchanged contours and nonincident stroke geometry; colors are absent from geometry keys. Width-only notifications preserve compound fill caches. Document replacement clears outline caches. Pointer packets during width drags coalesce at 16 ms; release applies its exact final position synchronously. Dirty bounds include old/new geometry, effects, ancestors, and mask dependants.

Canvas, preview/PNG, rendered-alpha masks, rasterization, and asset bounds consume this shared geometry. Preview shape antialiasing matches canvas/baking; Raster objects still explicitly disable antialiasing and retain nearest sampling.

Local offscreen Qt 6.11.1 measurements (2026-09-05; warmed medians, not CI assertions):

| Scene | Measured edit/render time | Retained outline cache after benchmark |
| --- | --- | --- |
| Reported seven-point shape, outline rebuild | 0.71 ms (previous renderer: 224–250 ms with missing edges) | 1.34 MiB |
| 100-anchor shape, full 1080×600 preview | 3.67 ms | 2.89 MiB |
| 1,000-anchor shape, full preview | 19.10 ms; above a strict 60 Hz budget | 27.22 MiB |
| Compound with 20 cutouts, full preview | 8.95 ms warm / 19.99 ms cold | 2.05 MiB |

The seven-point benchmark compiled one contour and rebuilt two incident edge strokes per edit (160 edge builds for 80 distinct widths). The 20-cutout benchmark built boundary attribution once across all style edits. Cache-budget tests exercise eviction and explicit clearing, not just a small-scene peak.

Run `python -m pytest -o addopts='' -q -s tests/test_realtime_shape_outlines.py -k benchmark` to reproduce timings, cache bytes, and rebuild counts. These figures exclude hardware/display latency and are not a guarantee for arbitrarily complex documents.

`baking.py` prepares images/tiles before replacing graph data. Rasterize renders
one pixel per document pixel, then embeds a positioned Image. Apply keeps
Raster-local tiles and the existing mapping, removes only active prefix links,
and extends interaction bounds. Undo callbacks restore model, images, and the
affected tile sets. Preview full/partial clears use Source composition before
returning to SourceOver, so transparent pages remove stale buffer pixels.

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

Transformed raster queries inverse-map visibility to the original sparse tile
grid without intersecting `interaction_rect`. That editing frame can contain
destination-space bounds after the first transform; filtering source tiles by
it hides valid artwork and makes stroke-driven frame expansion reveal stale
projection tiles in square chunks. `tests/test_raster_transform_visibility.py`
covers Free/Uniform handle commits, drawing across tile boundaries afterward,
undo/redo, save/reopen and bounded source queries. The shared disk renderer
identity is `native-artwork-20261007-source-context-2`, so incomplete or misplaced captures from older renderers
are ordinary cache misses. Original pixels and nearest sampling are preserved.

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

1. Use native density, or an explicit preview density clamped to 0.1–1, with an 8192-pixel maximum dimension. Camera zoom and device ratio do not add samples.
2. Flatten cubic spans while interpolating width and opacity.
3. Resample enough points for the current render scale.
4. Draw variable-width line segments and round joins into an 8-bit alpha mask using Lighten composition.
5. Draw explicit point/square/round end caps.
6. Fill an ARGB image with the stroke color and apply the mask through `DestinationIn`.
7. Draw the colored image into the stroke's document-space target rectangle.

The cache key includes drawing ID, stroke ID, the stroke render revision or a live-preview cache token, color, closure/caps, and rounded render scale. Live eraser and selection previews use transient per-stroke tokens (`eraser-preview`, `selection-preview`). Cache hits are reinserted for LRU recency. Each canvas session has a 64 MiB byte budget; selective invalidation updates byte accounting, and a single image larger than the budget is rendered but not retained.

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
- Full-strength affine Cage Transforms on a text's ancestors also map its editing frame, caret, selection, IME rectangle, and typography gizmos. Each lattice and the text frame are checked against the ordinary cage mapping; blended, intensity-masked, nonlinear, or singular cages retain the source editing frame. Parent-mask tests undo only that parent's cage and the outer cages, respecting clipping between stages. These are editor-only transforms: source bounds, native glyph/effect pixels, exact tiles, and durable cache identities are unchanged. A click on displayed text begins its typing session immediately and avoids capturing a source transform preview; free-box transform controls remain available in Transform mode.
- Pointer drag updates the character range live; Qt word selection powers double-click, and a same-object third click within the platform interval selects the full text.
- Keyboard, clipboard, and IME changes update the live object. A local text history handles in-session undo; the entire session becomes one chapter command on commit.
- Text-only canvas controls are derived from the current selection and exist only in Text Edit. A floating overlay edits integer size and bold/italic; two screen-space right-edge handles scrub snapped size and kerning from their drag-start values and coalesce each drag into one chapter command.
- Before any ribbon, gizmo, or transform edit, an active typing transaction is committed so document undo order matches user action order.
- `text_features.py` owns free-box placement, strict/free conversion, container frames, and Bounds resizing. Bounds previews update logical dimensions and placement together, preserving the glyph mapping and using the same `QTextDocument` as caret/selection/hit testing. Container bounds derive from child quads, and container rendering adds no path/clip. Text layout clips intersect (never replace) ancestor clips.
- Stretch, translation, and rotation may cache an unmodified box's native-density image (8192-pixel side cap). Boxes below active ancestor effects/masks use live scene rendering instead. Bounds never uses stretched image previews. A 16 ms timer coalesces layout gestures and release flushes the last pointer position into one undo command.
- In Text Edit, only a screen-space band around the dotted free-transform boundary begins translation. Transform handles keep priority and the quad interior remains an I-beam text target.
- Entering/ending a typing session changes overlays without discarding document tiles. Typing, local undo, ribbon/gizmo properties, strict margins and Bounds gestures invalidate the union of old/new clipped frames plus finite ancestor effect support. Text used as a mask/color source, spatial or unrecognized stacks, and brush-contour outlines keep full invalidation, including overflow regions. Typing commits preserve the hierarchy and one undo command.
- The navigator defers its dirty bands throughout active text sessions and box gestures. Its ordinary staged renderer uses compact source captures for Distort, cage and radial stacks, keeping world-space rigs fixed through inverse-scale stage mapping and reducing mask fields before effect preparation. Generic modified objects/layers use that same path. Explicit bounded canvas/overflow `INTERACTIVE` requests also use it under the guards described above. Unsupported stacks and mask-contributor sampling retain their prior grids; exact canvas/export/source renders remain unchanged, and these drafts are excluded from durable exact caches. See the October 2026 measurements in [editor-performance.md](../editor-performance.md).

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

Vector stroke bitmaps remain at native density across camera pan, rotation and zoom. Navigation gestures may temporarily retain an explicit preview density capped at one; release/touch completion, or 120 ms without another Ctrl+wheel event, clears that override and redraws at native density. Wheel zoom applies `1.0015^delta` per event. Alt+Shift drag zoom also preserves the initial click's document point at its original widget-space position while scale changes.

## Underlay rendering

The underlay is a live editing aid, not a separate object. While an object's underlay amount is nonzero, the scene render multiplies the object's own opacity by `1 - amount`, and `_render_selected_drawing_underlay` repaints it on top at `effective_opacity × amount`. The result is a ghost of the original at reduced opacity that the artist can trace over. It is intentionally absent from the chapter preview and navigator.

## Tablet and touch interop (Windows)

In tablet mode the canvas uses `comic_editor/ui/windows_input.py` to register the window for native touch with palm rejection (`TWF_WANTPALM`) and to set `MicrosoftTabletPenServiceProperty` with `TABLET_ENABLE_MULTITOUCHDATA` so touch navigation keeps working while the stylus hovers. `WM_TABLET_QUERYSYSTEMGESTURESTATUS` is answered to suppress system gesture handling.

## Rendering invariants and limitations

- For five CPU warps (Twirl, Deform, Mesh, Lens and Pinch), an exact scale-one
  ARGB32-premultiplied source whose normalized preparation exceeds 256 MiB may
  prepare integer native crops for bilinear transparent/white sampling. Crops
  include all original interpolation taps and are normalized before SciPy
  interpolation; exact float64 fraction bits must survive integer rebasing.
  Otherwise the unchanged full preparation is used. Float contracts, other
  interpolation/edge modes and other kernels retain their existing path.
  Immutable numeric crops share `PreparedDistortCache` and its ordinary budget;
  the const Qt input is job-local. This changes allocations, not source density,
  semantic artwork keys, native output samples or the disk renderer. The actual
  captured 5,383-square Pinch input matched all six native outputs across three
  alternating passes, with median kernel time 1.475 to 0.641 seconds; 60 smaller
  native kernel cases and boundary/COW checks also matched. Full-editor acceptance
  is tracked in [the investigation](../performance-investigation-2026-10-06.md).
- Exact Lens Distortion may skip constant-only bilinear samples from an owned
  normalized Legacy ARGB32 source at native output scale one. Coordinates stay
  on the original full source grid; a point with any real tap, including values
  immediately inside `(-1, width)` or `(-1, height)`, still uses the original
  SciPy sampler. Finite float64 strips are limited to 262144 points, with
  transparent/white constant edges only. Float/native-wide inputs, oversized
  strip samplers, other kernels and other sampling modes retain their previous
  path. No cache keys, density, budget, parameter fields, masks or intensity
  mixing change, and disk backing consumes the same ordinary exact result.
  The actual captured 1105×1466 Lens input produced the archived complete
  5383×5383 native output in all six alternating cold runs, matching all
  115906756 bytes. Median complete kernel time changed from 12.429 to 11.106
  seconds (10.65%); 12 complete native format/placement cases, three source/COW
  cases and 12 independent boundary/fallback checks passed. Floating paths were
  explicitly left unchanged. These are isolated kernel measurements; editor
  acceptance is recorded in [the investigation](../performance-investigation-2026-10-06.md).
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
