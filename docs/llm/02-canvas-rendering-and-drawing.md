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

Translation-output aliases restore the complete bounds promised by their stage
plan. Both mirror and radial raster paths therefore admit an alias only when
the returned image bounds equal those full bounds. Cropped results retain
their ordinary TileGraph keys and placement. The shared renderer version
`native-artwork-20261008-refactor-full-frame-translation-1` rejects older
derived entries through existing descriptor/index compatibility, including
final projection tiles. Original pixels and native kernel sampling are unchanged;
old in-memory owners require a fresh application process.

`render/cache.py::PersistentRenderCache` is an optional backing for the ordinary
projection, source, effect, tile-graph, and retained-stage cache access points.
It never traverses the document or implements a second renderer. Only an
explicit recording scope writes immutable results. QImage formats/raw bytes
and numeric array bits are compressed losslessly, including float working
pixels and retained checkpoint placement. Draft/live caches are excluded.

Cache descriptor identity serialization has a locked, bounded memo for typed,
immutable semantic keys (16 MiB and 8,192 entries). It returns the existing
canonical digest, preserving bool/int/float distinctions and signed zero.
Mutable keys and nonfinite values use the ordinary serialization path. This
memo does not bypass dependency, clear-epoch, seal or blob validation and is
shared by memory and disk access. See `tests/test_render_cache_identity.py`.

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

`ui/disk_cache.py::DiskCacheController` stages the ordinary immutable scene
capture, then submits explicit recording demands to `render/scheduler.py`.
The worker uses the same renderer, dependency keys, source/effect caches and
native tile grid as viewing. Image compression, hashing and disk reads retain
the bounded IO pool. A row becomes green only after all requested phases and
the index commit, the GUI receives that committed manifest, and ordinary blob
validation succeeds. Cancellation drains completed writes off the GUI. Cache
clears also run on a detached owner. Sibling owners merge publication under a
root-scoped lock; a clear epoch prevents queued pre-clear writes resurrecting
removed entries. Reads restore values into existing memory LRUs.
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

Canvas interaction lives in `comic_editor/ui/canvas.py` and its tool features.
`render/scene_kernels.py::SceneKernels` contains the shared scene implementation;
the canvas inherits it for explicit reference captures while detached consumers
evaluate it without a widget. Ordinary widget paints present ready resources.

### Exact CPU distortion preparation

`ui/distort_rendering.py` retains the incoming native sampling grids and
interpolation contracts. Two preparation shortcuts reduce work without changing
samples; neither derives resolution from camera zoom or display density.

Native legacy Pinch/Punch additionally plans the exact required input rectangle
before source capture. Eligibility requires unmasked bilinear transparent/white
edges, finite integer native frames and at most262,144 output pixels. The full
semantic source/effect frame and inverse mapping remain unchanged. The crop
includes every original bilinear tap and the unchanged base intersection,
including at100% intensity. Global and crop-local floor indices and float64
fraction bits must match; each kernel strip checks containment again. A crop's
prepared float RGBA must fit the existing256 MiB preparation budget. Unsupported
or uncertain cases use the original full-input path. This is source preparation,
not a new sampling grid, cache identity or disk renderer. Independent original
kernel and native-output checks are in `tests/test_native_pinch_input_roi.py`;
the saved-stack whole-buffer and separate performance pair are documented in
the integration performance ledger.

- Oversized legacy premultiplied ARGB32 sources can retain a job-local COW
  QImage and normalize only the required bilinear source crop. Eligibility is
  restricted to native scale, bilinear transparent/white edges and Twirl,
  Deform, Mesh Warp, Lens Distortion or Pinch/Punch. The full normalized source
  must exceed the existing 256 MiB preparation budget; each admitted crop must
  fit that budget. Global and crop-local floor indices and the float64 fraction
  bits must be identical before using the crop. Nonfinite/large coordinates,
  full or empty crops, floating contracts, other interpolation/edges, or any
  fraction mismatch use the original full-source sampler.
- Native legacy bilinear Lens Distortion can fill only taps whose entire
  bilinear support is outside the source with its existing constant edge value.
  Partially overlapping support still uses the original SciPy sampler. This
  requires owned float32 input, float64 coordinates, zero padding,
  `grid-constant` mode, constant fill zero/one, and at most 262,144 coordinates;
  unsupported cases retain the original path.

The mesh kernel also omits triangles whose destination bounds cannot cover the
requested pixels. Its triangles, tessellation and inverse mapping stay intact.
This kernel change is separate from the old regional-mesh scheduler, which has
not been ported into the detached evaluator.

Whole native-buffer, precision and fallback checks are recorded in
`tests/test_oversized_bilinear_preparation.py`, `tests/test_sparse_lens_sampling.py`
and the integration performance ledger. These shortcuts do not establish that
the integrated editor is responsive: saved Blueprint measurements still show
multi-second live artwork delays. See
`../refactor-integration-performance-2026-10-07.md` for actual measured scope,
failed probes and remaining acceptance work.

`render/pixels.py` applies the persisted `PixelContract` at explicit source,
working, display and export edges. Legacy documents retain encoded-sRGB byte
stage behavior. Floating documents import straight source channels before
premultiplication, preserving native 16-bit values and positive sub-byte alpha.
Embedded ICC profiles resolve before working-space conversion; authored UI
colors enter as sRGB. Mask coverage remains non-color scalar data. A captured
color environment owns the OCIO configuration, context and external resources,
so queued work and cache dependencies refer to the same immutable transforms.
`ui/color_resources.py` requests custom OCIO environments from a serial admitted
worker. Configuration/LUT hashing, immutable LUT capture and compilation of the
authored/display/export CPU edges stay on that worker. GUI paint, scene capture
and cache metadata lookups read only readiness. A staged scene yields until its
matching environment is ready; external config/LUT/context revisions advance a
guarded presentation ticket. Durable keys replace that runtime ticket with the
captured semantic resource signature. Pending/failed environments cannot record
exact entries or report cached rows as ready. Invalid resource diagnostics keep
their exception class, and corrected resources are detected on the detached
validation lane. Older snapshots retain their original transforms after source
files change or disappear.
Display conversion quantizes temporary presentation pixels once. Native output
uses the separate export conversion; display surfaces never replace native
source or derived working images.

`ui/scene_consumers.py` stages snapshots for isolated source statistics, Curves
pixel probes, baking and displacement snapshots. It passes only frozen scene
inputs to detached workers and retains editor callbacks on the document thread.
Curves/eyedropper probes preserve the final move/release while a cold source is
pending. Curves point edits use focused modifier snapshots for one history
transaction. Ready device tiles queue CPU availability without GUI readback.
Native float sampled colors receive their pinned display transform only at the
eyedropper edge; Curves probes retain working RGB and original coverage.

Portable brush strokes keep the legacy byte path for byte sources and new
tiles. Existing high-precision raster sources enter brush sampling through
their native channels before alpha association. Native output retains the
original format and profile, and copies only pixels under actual stroke
coverage. Untouched pixels retain their exact source bits even when a
watercolor edge requests a full-tile flush. This source edge does not change
the document color policy or brush equations for legacy artwork.

Authored brush PNG tips and textures also prepare on the admitted native-input
worker before the first dab. `core/brush_raster.prepare_brush_materials()` owns
independent compact byte pixels or lossless row blocks; it never shares the
mutable live material cache with that worker. Its pinned owner includes every
mip the existing pressure/spray/ribbon rules can select, each resized directly
from the original PNG. Adoption precedes queued input and corrected replay
retains that owner, so sampling cannot perform a cold PNG decode or resize on
the GUI. Admission estimates read only the small PNG header, without hashing
the complete resource there. Procedural brushes need no material preparation.
The material equations, registered grids, RNG sequence and legacy byte policy
are unchanged; `test_brush_material_ownership.py` compares exact final pixels
for stamp, ribbon, spray and corrected replay with primary/dual materials.

`ui/tile_input.py::TileInputGate` also admits Brush and Lasso Brush native source
footprints. Received Brush packets retain their original pressure, axes, time
and order; release follows the final native packet and corrected replay in one
history transaction. Lasso packets retain their original closed preview paths,
so queued fills replace their closing edge without opacity accumulation. Cold
untouched occupancy is metadata-only worker preparation, separate from edited
native buffers. Pending cancellation discards worker publication; terminal
errors restore source pixels and frames and report one operation error. A new
gesture clears the prior error after rollback. Clear and Save drain accepted
released gestures before capturing document truth.
Cross-tool native contacts use one FIFO activation queue before changing shared
stroke fields. A released cold Brush, Pencil, mask or Lasso contact therefore
finishes its captured transaction before the next contact begins, while each
tool retains its original native packet data and source owner.

- `core/changes.py` carries typed entity fields, old/new damage, resource
  addresses, structural/order changes and draft status. `DependencyIndex`
  propagates changes through composition, modifiers and masks. Forward edits
  and undo/redo publish through the same command callback. Metadata notices
  preserve legacy signals while focused observers avoid artwork/tree rebuilds.
- `ui/tool_sessions.py` wraps begin/update/commit/cancel and ordered pointer
  samples. Raster/vector samples preserve pressure, time and stationary brush
  ticks; navigation/handle feedback may coalesce replaceable values. Brush
  interruption commits, while Escape/tool/document retirement follow each
  tool's established cancellation policy.
- `render/scene.py::SceneSnapshotCompiler` captures only changed model records
  in cooperative slices. Nested vector points yield separately; unchanged
  strokes retain their immutable generation. Sources use independent QImage
  handles or immutable file/history pins. Address/version/path-identical clean
  sources share pin jobs across captures. Visible dependencies can overtake
  unrelated pin IO. No scene evaluation or source decode occurs in capture.
- `DetachedSceneBackend` owns the captured chapter, source stores, pixel/color
  environment and pure kernels. It never reads a live canvas, command stack or
  editor callback. Snapshot-local mutable cache bookkeeping belongs to its
  evaluator. Bound semantic source/effect/vector/checkpoint LRUs survive a
  revision through validated keys, without retaining the old scene owner.
  `InlineResults` retains independent COW handles and charges each distinct
  QImage storage once across checkpoint scope aliases. Removing the last alias
  releases its charge; mutation detaches the caller's handle. The existing
  64 MiB/512-entry limits and oversized-result policy remain intact. Cache
  adoption rebuilds this ledger from actual retained storage, rather than old
  per-scope byte totals. This is memory ownership bookkeeping and does not
  change semantic keys, dependency validation, disk entries or native output.
  Native ownership/adoption cases are in `tests/test_inline_result_storage.py`;
  measured heavy-stack performance is tracked separately in
  `docs/refactor-integration-performance-2026-10-07.md`.
  `render/source_resources.py::ReadyOriginals` also transfers already decoded
  original QImage handles when the worker retires a snapshot, including after
  cancellation. Adoption requires the same document, pixel contract and color
  environment, identical encoded-image pins or raster pin/native versions.
  The replacement retains its independent ImageStore/TileResidency byte
  budgets and preserves its newly captured buffers. Transfer handles are
  consumed immediately; this adds no second source LRU or disk cache identity.
- `render/scheduler.py::SceneScheduler` has one active evaluator and one latest
  queued demand per consumer. It cancels obsolete work between native blocks
  and bounds result handoff bytes. `render/admission.py` shares working-memory
  admission across scene, GPU, navigator, fill and output consumers; expensive
  jobs serialize, small independent work can coexist, and oversized work is
  exclusive. Admission estimates do not replace the kernels' full-source
  requirements or their cache byte limits.
- `ui/scene_controller.py` advances capture outside paint, dispatches detached
  work, rejects stale document/configuration/revision results and adopts ready
  native tiles. Edited views retain their previous complete coverage until all
  replacement regions are ready. Live feedback is explicitly provisional.
  Metadata slices use 2 ms during an active original Pencil/Eraser contact with
  ready source input and valid prepared feedback coverage. Cold preparation,
  release and other tools retain 4 ms; the timer cadence remains 8 ms. This
  cooperative budget does not change captured inputs or native exact work.
  Lower-density preview evaluators have separate caches and cannot contaminate
  the retained native evaluator or durable entries.
  Ordinary Raster Pencil/Eraser contact instead captures `contact_only` demand
  metadata after validating its open native input gate and original object.
  Capture slices revalidate that ownership. These contacts reuse unchanged
  native-owner caches through the same semantic keys, sampling and dependency
  checks; private transform/mask draft caches never flow back into that owner.
  Contact results remain live and provisional, so durable reads and writes
  stay disabled. Export clears both contact and live demand metadata.
- `render/raster_feedback.py` prepares native prefix/source/suffix planes with
  the ordinary detached scene kernels for a selected simple raster leaf. The
  source grid and two-pixel gutters retain their integer document placement;
  ancestor clip paths, selected opacity, foreground borders and the ordinary
  promoted-artwork pass retain their scene order. `ui/raster_feedback.py`
  substitutes only resident edited native source tiles, including cleared or
  pruned tiles, and presents the resulting provisional contact pixels before
  detached scene work finishes. It performs no decode, scene evaluation or
  color conversion. A separate 32 MiB presentation LRU reuses composed patches
  by prepared ownership and resident native pixels; retirement releases it.
  Central source tiles retain their complete QImage revision dependencies.
  Neighbor dependencies own only the exact native strips/corners intersecting
  each source gutter. Their format, profile and active pixel bits determine
  equality, including float payloads; interior edits can therefore keep the
  same composed image and GPU texture. Unexpected formats, dimensions or DPR
  use the complete image revision. Missing sources still wait for detached
  work, and pruning replaces the dependency with transparent coverage. Crop
  bytes count toward the same presentation LRU budget. No source conversion,
  sampling-grid change or separate damage propagation is involved.
  Prediction invalidates only patches intersecting its native antialiasing
  coverage; leaving a patch removes that transient pen once. Presentation
  culls off-screen patches with native-gutter/physical-filtering margins and
  batches visible patches through the ordinary retained GPU tile presenter.
  Its bounded geometry LRU retains camera/viewport positions with document
  clipping in each entry key, so alternating exact and feedback clips can
  reuse both vertex sets without retiring one another.
  Copying the prepared native prefix preserves its exact premultiplied pixels
  before the selected source and suffix are composed in their original order.
  Covered planes survive selected-pixel-only revisions under the same typed
  invalidation guards, so exact completion does not rebuild all three planes
  on every stroke. Resource coverage is computed by the same helper used for
  worker preparation. Once exact artwork is current and contact ends, the
  provisional overlay stops; its retained source substitutions remain available
  for the next contact against that basis.
  Prepared planes also have a 32 MiB limit and never enter artwork/disk caches.
  The current eligibility policy requires legacy pixels, normal object blends,
  no selected/ancestor effects or opacity masks, bounded untransformed ancestors
  at unit opacity, integer accumulated source placement and no linked sampler
  depending on the edited source. Unsupported graphs retain the previous ready
  scene until ordinary detached preview evaluation completes. A screen-space
  contact ring and "Updating drawing…" label identify received input whose
  current pixels are unavailable, including queued cold source packets or a
  contact outside prepared coverage. This pending indication is not current
  artwork or exact scene readiness. Native reference
  tests cover first press/move/erase while that worker is blocked, curved clip
  boundaries and borders, object opacity, negative tile origins, source seams,
  foreground promotion and complete eraser pruning.
  This is a provisional presentation composition: flattening transparent
  foreground planes may add byte-stage rounding. The saved occupied-scene
  proof measured nine unchanged background bytes differing by one, with the
  actual pencil/eraser contact footprint byte-identical to the native oracle.
  Final exact tiles continue through the original fixed-block renderer and
  never adopt these presentation planes.
- A zoomed-out view larger than the native tile LRU first shows a provisional
  preview, then `render/overview.py` streams ordinary exact native blocks onto
  a bounded display surface off the GUI. Only finished native pixels are
  reduced; source/effect grids remain unchanged. The complete presentation
  publishes atomically, becomes current, and never enters an artwork/disk cache.

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
- `ui/scene_render_backend.py::CanvasSceneBackend` remains an explicit reference
  adapter for diagnostic/native-pixel comparisons. It restores temporary state
  on every exit. Production widget rendering uses `DetachedSceneBackend`.
- `ui/document_projection_features.py` supplies explicit requests and camera
  priority/deadlines, translates results into UI publication state, and presents
  finished artwork. GPU presentation and editing overlays remain in the UI.
- Graphics shutdown waits for the owning worker thread, then retires its
  service back-reference and shared GUI context on the application thread.
  A finished QThread must not keep either in a Python cycle until unrelated
  garbage collection; that previously invalidated native Qt image-loader state.
- Full/regional document edits invalidate the service cache. Reentrant edits,
  view changes, and document replacement cannot publish stale captured pixels.
- `render/tile_graph.py` evaluates compiled effect nodes recursively on stable
  256px addresses. Nodes declare required input rectangles or disjoint islands;
  identities contain semantic frames, transforms, source generations and the
  upstream parameter/mask prefix. `ui/tile_effects.py` supplies scene kernels,
  bounded cache adapters and lazy object/layer/raster source capture. Exact
  regional requests above 256² pixels use this path; small frames retain the
  existing kernels. Provisional captures and special-purpose contexts opt out.
- Point operations, native single-pass Kuwahara, sharpen and solid outlines
  have finite footprints. Generic stacks retain float intermediates until their
  original final RGBA8 conversion; spatial stacks retain per-stage rounding.
  Dither and Kuwahara sampling retain full-image coordinate origins.
- `render/blur_regions.py` requests individual pyramid regions using original
  frame dimensions and Pillow-compatible fixed-point bilinear coefficients.
  Odd-size levels, legacy alpha behavior and masked-radius interpolation match
  the reference path. Its immutable, thread-safe level cache has a 32 MiB budget.
  Detached consumers resolve source dependencies from their captured ownership;
  filtering never consults mutable editor state.
- Integer-translated Array copies request inverse-mapped source islands.
  Qt's rotated/scaled/reflected sampling keeps a shared full frame. Pattern,
  contour, reduced-scale/repeated Kuwahara and smudge nodes also retain shared
  complete-frame dependencies; cage/mesh/radial kernels retain full source
  access while requesting bounded output. All-solid outline stacks retain their
  specialized reference path. Sharpen radius/strength masks and generic float
  stacks with strength-masked blur retain whole-field extrema decisions. See
  [the tile evaluator plan and results](../tile-evaluator-plan-2026-10-01.md).
- Navigator, asset captures, output/export, source statistics and baking use
  the common captured scene and kernels. `render/outputs.py` composes native
  output and converts once at its declared export edge. `ui/output_jobs.py`
  captures and publishes output jobs without putting encoders on the GUI.
  `ui/scene_consumers.py` gives histogram/bake tools their own bounded consumer.
  `ui/asset_copy_jobs.py` stages Copy as Asset, then prepares owned native
  sources/thumbnail off the GUI and publishes on the existing serial save
  writer. Its source pixel contract and captured color environment survive
  extraction; a newer edit in a replaced open asset remains dirty.
  Fill reference rendering and flood work use detached source ownership and the
  existing guarded application/replay protocol.

- `_CanvasLogic` is a large mixin containing document binding, selection, camera math, rendering, hit testing, input dispatch, every drawing tool, text editing, tone-mask and modifier integration, and transform workflows.
- `RasterCanvasWidget` combines that mixin with `QWidget`.
- `GpuCanvasWidget` combines it with `QOpenGLWidget` and requests partial updates.
- GPU paint events enter `QOpenGLWidget.paintEvent`; Qt prepares the widget's
  framebuffer before `paintGL` draws the ordinary shared canvas frame.
  `paintGL` verifies the current widget context, draw framebuffer and color
  attachment, then clears that existing presentation attachment to the opaque
  canvas background. It restores scissor and indexed color-mask state before
  entering the explicitly bounded QPainter lifetime. Transparent native tiles
  must not accumulate over preserved pixels from an earlier partial update.
  This presentation clear does not bind another framebuffer, allocate derived
  artwork, alter sampling density or change native/cache output.
- `create_canvas(settings)` probes an offscreen OpenGL 3.3 context unless the renderer is forced to `raster`. If the probe fails, or Qt is using `offscreen`/`minimal`, it creates the raster widget.
- Both backends present the shared scene pipeline. Eligible point/blur chains
  retain `render/device.py::DeviceImage` textures with producer fences and
  leases. Shared-context presentation consumes ready textures without a
  readback/upload round trip. QPainter, encoders, disk recording and unshared
  raster surfaces materialize at their real CPU edge on a detached lane.
  Unsupported chains preserve exact CPU fallback. Context failure retires
  device results as cache misses; GL cleanup remains on its graphics owner.
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

1. Position the floating text size/bold/italic overlay and fill the widget with
   dark gray `#242428`; draw the empty-state message without a chapter.
2. Compute native visible demand and present ready document tiles, a coherent
   previous edited view, or the explicit provisional/current overview surface.
   Scheduling only records demand; paint performs no scene/source evaluation.
3. Under the camera transform draw immediate predictive raster ink, the live
   vector gesture, text caret/selection, and page-gap overlays. Promoted ink is
   ordered within the detached scene's on-top composition when needed.
4. Draw active tone-mask coverage, selection and focal-modifier controls,
   creation/asset-drag feedback, then screen-space export, tablet hover,
   simplify and eyedropper overlays.

The old widget scene/static-transform/vector-eraser image paths remain explicit
reference helpers. A canvas with `SceneController` takes ready presentation;
its scene previews are captured as tool/model state for detached evaluation.

`_ensure_scene_cache()` is the reference widget-image helper: dark gray outside
the chapter, background, chapter clip, reversed root-page order, selected
drawing underlay, grid and document outline. Ready production presentation
preserves these visual rules while separating artwork evaluation from overlays.

`ui/navigator_jobs.py` captures and evaluates the navigator separately, pauses
during interaction, hides UV guides in its detached chapter view and publishes
a complete preview. `render_preview()` remains the explicit reference helper
for the same recursive kernels and historical navigator pixel comparisons.

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

The masks panel consumes completed 80-pixel thumbnails from `SceneConsumers`.
Its staged snapshot and the ordinary mask kernel run on the detached owner;
panel refresh never evaluates a field or decodes source tiles on the GUI.
Publication checks the document and mask list. These display previews do not
enter durable exact caches. Parameter bindings and contributors use focused
record history; saved-mask names and labels are metadata and do not invalidate
artwork. Mask/modifier registry changes retain unrelated drawable tree rows.
Deleting a painted mask captures its native tiles and alpha bounds on the
worker before one focused history transaction; Save and destructive session
transitions drain that accepted transaction. Undo restores the original native
format and pixels without re-reading the retired source store.

- Mask paint tiles are drawn as translucent `#64B5F6` coverage multiplied by `0.35 × mask` alpha in the editing overlay, with a small LRU for painted tile images composited with `CompositionMode_Plus`.
- The overlay signature recursively walks contributors (entity JSON, pixel tile cache keys, ancestors, dependent masks) with cycle guards. Document changes invalidate the overlay fully; while mask painting is active, contributor images are preserved across one gesture so the editor does not flicker.
- Mask pencil pressure maps linearly between the configured From/To alpha values (defaults 0.0 to 1.0); without pressure sensitivity the To value is constant. Mask strokes replace alpha rather than accumulating it, so they can lower existing coverage. Each gesture snapshots touched tiles once and bumps the mask revision once.
- Parameter masks bind a mask to a target through black/white endpoints. For opacity, the bound value multiplies RGB and alpha; for modifier parameters the mask maps 0–1 onto the attribute's black-to-white range.

## Modifier rendering

`comic_editor/ui/modifier_rendering.py` is the pixel engine for the non-destructive stack.

- **HSL** performs a NumPy hue/saturation/lightness round-trip with per-parameter masks.
- **Blur** evaluates a per-pixel radius from strength and the optional focal ramp. `BlurPyramidCache` (64 MiB) stores levels at radii `(0, 1, 3, 7, 15, 31, 63, 127)`, keyed by algorithm and a BLAKE2b source digest. Normal Blur keeps Pillow images in premultiplied `RGBa` through every reduction, enlargement, and blend. Legacy retains the old `RGBA` branch exactly: feeding it premultiplied data causes a second premultiplication/unpremultiplication during resizing and can produce RGB greater than alpha, explaining the colorful distortion on transparent composites.
- **Radial Blur** uses `radial_blur.py`: symmetric midpoint angular integration with premultiplied bilinear sampling, transparent out-of-source samples, and adaptive sample counts based on pixel arc length. Integration uses 96×96 tiles; zero-angle mask regions are exact identities. The complete incoming stage remains available when rendering a cropped output, so displaced centers do not lose offscreen source pixels. `effect_geometry.radial_sweep_bounds` includes angular extrema and mask endpoint angles; the pipeline adds bilinear support and preserves ancestor clipping.
- The optional Windows AMD64 RGBA sampler shares corner addresses across four channels on supported finite contiguous float32 inputs with SciPy 1.18.0. Its owned C source, binary, build contract and ABI are verified before loading. Unsupported or missing delivery retains the original SciPy path. It preserves double multiplication and corner order, float32 rounding per angular sample, native grids, sample counts, masks and cancellation cadence; it is not a sampling exception or another cache pipeline. The app never compiles or downloads code. `tools/build_radial_rgba_sampler.py` is an explicit developer build recipe. See [the integration measurements](../refactor-integration-performance-2026-10-07.md) for bit-level acceptance and matched whole-kernel timing.
- `radial_pipeline.py` caches angular integration separately from the final intensity mix. Its key includes the semantic upstream source, center, angle/angle-mask signature, full mapping, output shape and origin, but excludes intensity and its mask. Integration is retained as a float-premultiplied QImage in the existing bounded caches, so the final blend still rounds to RGBA8 only once. An intensity-gradient edit also reuses in-flight integration and blends its latest mask after completion.
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
Fractional placement changes recapture parent-space artwork; raster-local
effects retain their original grid and are placed after processing. Drafts and
live previews still cannot enter the durable exact cache.

Translation aliases admit stage outputs only when their actual bounds match
the reference plan's final placement. A tile-graph crop of a retained spatial
stage (such as Outline followed by Smudge) keeps its ordinary regional cache
identity; it must never be restored across the complete stage frame. The
`native-artwork-3` renderer identity excludes older durable aliases/projection
tiles that could contain this placement error. Sampling density is unchanged.

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

`baking.py` requests detached images/tiles before replacing graph data. Rasterize renders
one pixel per document pixel, then embeds a positioned Image. Apply keeps
Raster-local tiles and the existing mapping, removes only active prefix links,
and extends interaction bounds. Undo callbacks restore model, images, and the
affected tile sets. The worker also captures focused graph history, original
native tiles, and alpha bounds. Publication and undo retain unrelated records
and image sources without decoding old cold tiles or scanning native alpha on
the GUI. Clearing a file-backed tile map deletes addresses without reading its
values. Preview full/partial clears use Source composition before
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

Large fills and every composite reference mode capture immutable scene/source
ownership in four-millisecond GUI slices. Clean source files are pinned and
decoded by the fill worker. Cold reference tiles, including the morphology halo,
use `render.fill_references` and the ordinary detached entity kernels; the GUI
capture loop never evaluates artwork. Parent transforms/opacity, text exclusion,
and repeating-source boundary bypass match the existing reference contract.
Fill classification keeps its premultiplied byte reference image while native
source/effect sampling and precision remain unchanged. Reference rendering and
flood computation share one admitted workspace, including cancellation checks.
Result publication verifies document binding, history/source/reference versions,
selection, target model and gesture generation. Tolerance replay retains its
original reference tiles and evaluates any missing halo tiles on the same
detached worker. Async completion, rather than queue admission, creates the single
history transaction.

### Raster rendering and transforms

Normal raster rendering translates by object `(x, y)`, queries only tiles intersecting the local visible rectangle, and draws each image at `tile_index × 256`.

Transformed raster queries inverse-map visibility to the original sparse tile
grid without intersecting `interaction_rect`. That editing frame can contain
destination-space bounds after the first transform; filtering source tiles by
it hides valid artwork and makes stroke-driven frame expansion reveal stale
projection tiles in square chunks. `tests/test_raster_transform_visibility.py`
covers Free/Uniform handle commits, drawing across tile boundaries afterward,
undo/redo, save/reopen and bounded source queries. The shared disk renderer
identity is `native-artwork-3`, so incomplete or misplaced captures from older renderers
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
- Typing history captures only the edited box's text, color runs and strict margin. Text property drags capture their changed attributes; cached free-box transforms capture geometry and wholly owned attached rigs/masks. Free-box insertion captures its parent, new-record tombstones, collection order and document size. These interactions do not serialize unrelated chapter objects. Production transform previews use the detached scene instead of preparing widget-owned full-document text bitmaps.
- The navigator defers its dirty bands throughout active text sessions and box gestures. Its ordinary staged renderer also uses compact source captures for Distort, cage and radial stacks, keeping world-space rigs fixed through the inverse-scale stage mapping and reducing mask fields before effect preparation. Generic modified objects/layers use that same navigator path. Unsupported stacks and mask-contributor sampling keep their prior grids; native canvas/export/source renders remain unchanged and navigator drafts are excluded from durable exact caches. See the October 2026 measurements in [editor-performance.md](../editor-performance.md).

## Hit testing and tool input

Cold magic-wand classification, drawing Copy/Cut extraction, clipboard-history
thumbnails, and drawing-cage preparation/commit use `ui/scene_consumers.py`.
Editor scene and supplemental source arguments are captured in 4 ms slices;
clean source files are pinned through the common scene compiler, then decoded
and evaluated on its serial worker. `render/input_capture.py` uses the same
detached scene kernels and original source grids. Wand classifiers retain their
existing premultiplied byte reference contract; native artwork/source precision
remains unchanged. Drawing clipboard tiles retain their source QImage format.

Wand clicks and drawing captures are ordered actions. A queued wand click may
advance only across the preceding click's committed revision; unrelated source
changes cancel it. Cancel/tool/session changes retire matching jobs. Copy then
immediate Paste retains the requested paste position until extraction finishes;
Cut publishes one prepared history transaction before Save may capture it.
Cage preparation retains a pending press, latest absolute handle target, and
final release. Commit warps all original selected sources on the worker and
publishes one guarded history transaction. Save/export wait for that transaction
before staging source ownership. Clipboard thumbnails use the already owned
clipboard payload and never borrow a live editor during worker evaluation.

All mouse, tablet, wheel, touch, key, double-click, and IME events enter the canvas and are dispatched according to `ToolKind` plus selected entity type.

Ordinary Raster, mask paint, Brush and Lasso Brush admit cold native sources
through `ui/tile_input.py`. Each received packet owns its original samples and
authoring settings; the serial source worker prepares only its declared native
tile footprint before the existing paint kernel runs. Pressure, tablet axes,
timestamps, source grids and QImage precision are retained. Long queues replay
in approximately 4 ms slices without dropping samples. Extra source borrowers
are bounded by the tile residency budget; evicted edited tiles are re-prepared
from their original recoverable owner. Unknown cold alpha bounds at release
are scanned as metadata on the worker and adopted in bounded slices rather
than returning every untouched native image. Tile-only admission accounts for
its requested native buffers and alpha workspace, without reserving an unrelated
full-scene render workspace. Authored Brush materials enter as immutable worker
resources before the first paint packet.

Released contacts retain their original entity and source-generation guards
when the user changes selection, tool or an unrelated property. A harmless
presentation change may restart staging, but cannot silently cancel accepted
ink. Different drawing tools share one FIFO contact activation queue, so a
released cold Brush followed by a Raster contact completes as two ordered
transactions. Document replacement and explicit cancellation retire owned input
and prevent late publication. Save, Undo, Clear and close include accepted input
and queued successor contacts in their existing transaction boundaries.

The active blue mask overlay is prepared by `render/mask_overlay.py` through the
common detached mask kernel. Widget paint consumes ready QImages only. Positive
paint keeps the original split scalar-base/native-tile Plus composition; signed
subtraction uses the complete common scalar field. Changed resident native mask
tiles can update contact feedback without evaluating a cold mask in paint.
Large zoomed-out positive paint sets stream their original tiles into one bounded
display surface on the worker, preserving camera filtering and display density;
the surface is transient presentation and never a durable exact cache entry.
Prepared overlays own mask/config/source revision and camera/viewport/density
identity, so retired workers cannot publish into a different document or view.
Native artwork sampling remains one sample per document pixel. The overlay
tests compare exact blue display bytes with the original split composition,
including fractional rotation and both smoothing settings at display densities
one and two.

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
