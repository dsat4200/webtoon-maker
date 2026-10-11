# RTX 5060 overnight optimization pass

This is an ongoing pass against starting commit
`8f704f4feb5cff07e3870d900f2bc09e94afc747`. The earlier drawing fixes are
already present in that commit. This pass reduces exact CPU scene work used
alongside the GPU renderer; it does not claim that every modified drawing
stack now gives real-time feedback.

The finite-filter canvas integration is retained in the checkout. Its latest
718-file source passed 2,478 native Windows checks on the RTX 5060 and all 11
core saved-project workflows on a fresh full copy. Earlier sections record
previous checkpoints; the latest retained high-rate input measurements and
remaining delays are recorded at the end.

## Retained changes

- Mesh face containment compares the two coordinate columns directly. It
  preserves the original tolerance, array-to-array NumPy promotion, face
  order, inverse matrices, barycentric arithmetic, and sampled coordinates.
- Original Kuwahara uses broadcast integer axes for scalar radii and shares
  the unchanged weight denominator between mean and variance. Variable
  radius fields keep their original dense-coordinate path. Integral-table
  construction, radius rounding, quadrant order, fractional interpolation,
  cancellation boundaries, and output conversion are unchanged.
- Shared background graphics contexts and surfaces follow their supplied
  display before native creation. Legacy helpers follow supplied surfaces;
  owned surfaces follow their created contexts. Prepared borrowed contexts
  are never recreated or reassigned. This fixes implicit primary-display
  selection, rather than selecting a GPU vendor for the user.
- Scene metadata detachment reuses exact built-in immutable scalar values
  and scalar tuples. Mutable/Qt values and scalar subclasses retain their
  ordinary independent ownership. Semantic keys, native output and bounded
  cache bookkeeping are unchanged.
- Live finite-filter raster feedback updates only the selected bounded source
  and replays complete ordinary native scene tiles. It keeps original source
  and effect grids, clipping and painter order, typed source ownership and
  existing invalidation. Changed resident pixels cross a serial worker lane;
  complete affected tiles replace the normal batch, including erased pixels.
  Existing capture pins and ready original buffers prepare the private owner
  before contact without repeated links or redundant native decoding. Ordinary
  rendering resumes when dependency, coverage, frame or byte guards fail.

Artwork cache resolution and ordinary semantic keys/invalidation are unchanged.
Transient live planes, source packets and presentation crops remain bounded
and excluded from durable exact storage. Exact document
tiles still use the original native 256px grid with 2px gutters and fixed
4×4 capture blocks. New regressions preserve independent original math and
compare raw coordinate/working-pixel bits and complete native image buffers.

## Inputs and isolation

Evidence is under `.artifacts/overnight-gpu-20261009/`. A fresh complete
Pocket Boyfriend copy contains 13,776 files / 357,023,852 bytes and initially
matches the original file hashes. The stress chapter is
`0a72f08009294aa0a3d14e6a38e22bbb`, 1080×82000 pixels, with 106 layers,
173 objects, 103 modifiers, and 29 masks. The dream-sequence visibility
change exists only in the benchmark's detached model. The full-view probe
uses y=30000, height=5284.615384615383, and the ordinary 256px margin,
covering 115 native projection tiles.

Starting runtime/tests are frozen as `baseline-source` (702 files).
`allocation-source` changes only the two production kernel files.
`final-allocation-source` adds the two permanent test/reference files
(704 files). Complete SHA manifests verify the imported source.
`screen-affinity-source` adds the four context initialization changes and
their permanent regression module (705 files). `immutable-value-source`
adds the scene ownership shortcut and its permanent regression module
(706 files). The checkout matches that complete tested source. Private
settings and complete companion brush assets
are separate from user
preferences. Test clipboard operations use a private in-process clipboard;
provider connections and child integrations are blocked in stability runs.
The user's running editor is not restarted or closed.

## Performance and native correctness

`allocation-balanced-r1.json` contains 20 alternating baseline/candidate
pairs for each of nine isolated CPU cases. Equality checks and hashing are
outside timing. All output bits matched in every sample.

| Isolated case | Baseline median | Candidate median | Less wall time |
| --- | ---: | ---: | ---: |
| Saved mesh native strip | 7.281ms | 6.201ms | 14.8% |
| Mesh full query grid | 49.513ms | 36.830ms | 25.6% |
| Mesh unaligned query grid | 39.994ms | 30.991ms | 22.5% |
| Kuwahara scalar integer | 42.922ms | 40.399ms | 5.9% |
| Kuwahara scalar fractional | 78.995ms | 72.518ms | 8.2% |
| Kuwahara variable field | 19.911ms | 19.508ms | 2.0% |

The unrelated mesh case was effectively unchanged (2.292ms versus
2.297ms); tiny mesh and small filter cases also improved. Windows thread
CPU counters are too coarse for interpreting the sub-millisecond cases.
These are isolated CPU timings, not end-to-end input latency.

`scene-balanced-pairs.json` contains three fresh-process paired full-view
runs in alternating order. Median exact evaluation was **40.522s →
38.954s**, **3.87% less wall time**. All 115 native tile records matched in
all six runs, including full pixel-buffer hashes, format, dimensions,
stride, device density, and color profile. Model bytes also matched. The
192 MiB shared image pool and 256 MiB distortion preparation cap were
unchanged. Timing workloads were serialized; no stage instrumentation was
used in this comparison.

Private differential checks additionally covered mixed float32/float64
coordinates, folded and overlapping meshes, degenerate faces, unrelated
regions, projective grids, scalar/fractional/variable filter radii, filter
iterations and explicit processing scales. Whole-renderer comparisons
covered nearest/bilinear/bicubic sampling, byte/float16/float32 native
buffers, fractional output regions, and transient downsampling.

## Rejected experiments

A private 5×3 capture shape reduced the first diagnostic full-view time
from 40.775s to 34.304s, but changed three of the 115 native tile buffers.
It was rejected. Production retains the original capture shape, native
phase, sampling grid, and memory allowance.

Cached mesh inverses and vectorized block bounds passed native equality
and the repaired isolated checks, but gave only a small full-view benefit.
They were superseded by the smaller coordinate-comparison change and are
absent from production. One initial private test run had a missing fixture
for tests outside the source tree; the fixture was corrected and the
checks were rerun. No product failure was hidden by changing assertions.

## Stability and native contact checks

`allocation-broader-r1-stability.xml` records **2,239 unique passing tests**,
zero failures/errors/skips, in 413.333s. The assembled 704-file build passed
the native Windows rendering, GPU, drawing/brush, history, cache, and
persistence matrix. Provenance reports zero provider/child-integration
attempts, wrong module origins, or changed source files.

The fresh saved-copy run passed all 11 checks: full series open; saved
raster Pencil, Eraser and Brush with exact undo/redo; cancellation rollback;
mask paint with undo/redo; fill; copy/cut; paste; saving through a released
cold input; and exact save/reopen/chapter switch. It verified 7,961 unrelated
private files and unchanged user settings. Core actions took 8.469s; pytest
including teardown took 11.51s. Those transactions mutate only the separate
`stability-final-copy`.

Native GPU contact checks confirmed **NVIDIA GeForce RTX 5060/PCIe/SSE2**
at DPR1.5 and zoom1 / zoom0.35. In each baseline and candidate run, all 40
Pencil, 40 Eraser and 40 Brush contacts appeared while held, within16.7ms,
with zero obsolete scene submissions. All **240 candidate contacts** passed;
maximum candidate contact-to-swap time was14.335ms. Candidate p95s were
5.186/5.217/10.681ms at zoom1 and6.230/6.146/11.764ms at zoom0.35
(Pencil/Eraser/Brush). Before/after use the same observer. These are
synthetic native handler-to-Qt-swap checks for simple affine sources,
excluding physical tablet-driver and display scanout latency. They do not
prove real-time feedback for arbitrary modified stacks.

### Display/GPU routing observed during testing

The first fresh native probe reported AMD integrated graphics. Qt currently
exposes only the Kamvas display; this differs from earlier RTX runs. That
probe was retained as diagnostic data and rejected as RTX evidence. A
private copy of the same Python executable/DLLs, using the same Python
libraries, was then assigned a temporary Windows high-performance GPU
preference. It reported NVIDIA4.3 /591.44 even with the current Kamvas
display layout. Every temporary preference is scoped to that private
executable, guarded, and restored in `finally`; the running editor and
shared Python executables are not assigned a preference. Restoration
journals accompany every run.

Windows controls per-application GPU preference; NVIDIA documents that the
OS preference takes precedence over its control-panel selection.
[NVIDIA GPU-selection guidance](https://nvidia.custhelp.com/app/answers/detail/a_id/5035).
This is evidence about newly launched probe processes, not a claim about
which GPU the already-running user editor currently owns.

### Display affinity regression checks

The subsequent 705-file build passed **1,649 unique native Windows tests**
on the RTX 5060, zero failures/errors/skips, in253.856s. These include the
rendering/GPU matrix, native cache/output checks and ten new affinity cases.
Eight synthetic distinct-display checks fail against the untouched original
initialization; the prepared borrowed-context control passes there. The new
native sharing case verifies actual sharing, exact point pixels and owner
shutdown on the installed driver. Qt exposes only one physical display in
this session, so actual two-display adapter switching is not claimed.

All240 additional Pencil/Eraser/Brush contacts at DPR1.5, zoom1 /0.35,
appeared while held within16.7ms; the maximum was14.367ms. All six tool/zoom
runs had zero obsolete scene submissions. Matrix provenance records no
external attempts, wrong module origins or modified source files.

The first matrix also passed all1,649 tests, but its audit mistakenly
included generated pytest metadata in the source manifest. That guard
correctly failed when the metadata changed. The manifest was repaired to
cover the original704 source files plus the new regression module, and the
complete matrix was rerun; neither product assertions nor source checks
were relaxed. Both isolated GPU preferences were restored after their run.

The screen setup follows Qt's documented requirement to select the surface's
screen before creating its context.
[Qt context setup](https://doc.qt.io/qt-6/qopenglcontext.html#details).

## Private filtered-contact research

The modified saved raster still has no prepared live feedback. A private
composition prototype keeps the ordinary native scene before the selected
leaf, invokes the shared filtered entity kernel on that destination, and
replays the unchanged foreground through the ordinary scene kernel. The
first attempt flattened the foreground to a transparent image; it changed
25 edge pixels by one channel unit in a fractional clip case and was
rejected. Replaying the foreground avoids that additional alpha rounding.

The revised proof matched complete native buffers in108 cases with dual
HSL, a spatial intensity mask and solid antialiased Outline, including
fractional/sheared/rotated parent mappings, three source formats, partial
opacity, Pencil-like paint and erasing. It also matched four native saved
Raster38 checks (one ordinary1028px capture and one260px patch) and verified
all13,776 copied files unchanged.

A further private proof reused the existing ordinary cache-state handoff
without new keys or capacity. Its eight saved-raster outputs were exact,
but that handoff added cost instead of making the path responsive. None of
these prototypes is connected to the editor. They do not justify a live
latency claim; safe scheduling, bounded ownership and substantially faster
selected/foreground evaluation are still required before adoption.

A diagnostic phase trace narrowed the remaining cost to selected-entity
evaluation: about 343ms for a 260px native patch, while foreground replay was
about 5.5ms and mask-field evaluation about 3.5ms. These instrumented private
measurements identify work to investigate; they are not a product benchmark.

A separate private 64px native effect-block trial matched six saved-layer
outputs, including 66px contact patches. Actual editor snapshots then matched
six more outputs and adopted 44 unchanged decoded source tiles through the
existing version/pin-validated `ReadyOriginals` handoff. The 66px patch still
took about 92.5ms. Thus source decoding alone does not explain the remaining
latency. The block-size trial is not promoted, and ordinary exact captures
and effect/source grids remain unchanged.

## Immutable ownership shortcut

Profiling that private contact patch identified repeated detachment of nested
immutable cache-key leaves as a substantial part of its selected-entity cost.
A retained shortcut preserves the existing keys and mutable/Qt ownership
rules while reusing exact built-in scalar values. Three alternating fresh-process
pairs against the 705-file build completed with all 115 native records equal in
every run. Median exact evaluation was 38.578s → 37.153s, 3.69% less wall time,
with the same native capture grid and memory caps.

The 706-file candidate subsequently passed 2,275 unique native Windows tests
on the confirmed RTX 5060, with zero failures/errors/skips, in 431.897s.
Its 26 new ownership cases preserve exact scalar types and floating bits,
signed zero, mutable scalar-subclass attributes, nested container/Qt geometry
ownership, float QImage copy-on-write, and rejection of editor objects and
callbacks. Provenance records no external attempts, wrong source imports or
changed source files. The private GPU preference was restored after the matrix.

A second complete private Pocket Boyfriend transaction copy passed all 11
core-feature exercises in 8.420s. It verified 7,961 unrelated private files
unchanged and actual user settings unchanged. All 240 additional native
Pencil/Eraser/Brush contacts at DPR 1.5 and zoom 1 / 0.35 were current while held
within 16.7ms; maximum 15.397ms, zero obsolete scene submissions. These
synthetic handler-to-frame-swap checks cover the ordinary affine feedback
path, not physical tablet latency or arbitrary modified stacks. The heavy
modified-stack comparison below also passed. The exact tested scene and test
bytes were then promoted through a guarded source-manifest check. The 64px
filtered-contact prototype remains private.

## Heavy saved-stack verification and remaining latency

Both matched MainWindow runs reported the RTX 5060, accepted all 186 posted
events, and retained three history transactions (Pencil, Eraser, Brush).
Each finished native framebuffer matched its independent synchronous scene
oracle. Both runs left the 13,776-file benchmark copy unchanged.

The candidate cold exact view finished in 57.790s versus 59.359s before;
the final input's exact view took 14.336s versus 15.065s. These single paired
runs are stability evidence, not a reliable speed estimate. This modified
raster remains outside the prepared feedback gate: neither run produced
prepared current-contact frames. Input dispatch p95 was still about 349ms
under the heavy stack. Further live-feedback work remains necessary.

The later 705-file/706-file MainWindow pair also confirmed the RTX 5060,
accepted all 186 events, retained exactly three Pencil/Eraser/Brush transactions,
matched both independent final native-framebuffer oracles with zero changed
bytes, and left the benchmark copy unchanged. Cold exact view time was
57.893s → 55.237s; final-input-to-exact time 14.273s → 12.932s; posted-input
dispatch p95 was 359.199ms → 127.315ms, with maximum 423.800ms → 198.559ms.
This is one serialized stability pair, not a reliable speed estimate. Both
runs still had zero prepared current-contact frames for the modified Raster38.
Its live feedback remains unfinished.

## Bounded filtered-contact and native display proofs

### Visibility correction

A subsequent stronger check found that the saved Raster38 stroke locations
used in the component probes below were covered by other artwork. Pencil,
Eraser and Brush changed their native source buffers, but all three final scene
and framebuffer hashes were identical. Their equality checks and transaction
counts remain useful, while their timings do not prove visible drawing response.
The saved-location patch measurements likewise cannot establish visible edit
correctness. The independent 108-fixture full-capture comparison is separate
evidence. New saved-layer probes require visible changed pixels in addition to
native output equality. No filtered-contact prototype has been promoted.

Further private work evaluates only the demanded rectangle for finite HSL and
solid Outline nodes, using the same native kernels, original frames, halos,
mask sampling and precision. The region flag remains confined to the private
706-file `contact-region-source`; ordinary checkout effect blocks are unchanged.
This matched 108 same-region fixture outputs and six saved-layer outputs.

Recording ordinary native painter calls removed repeated foreground traversal.
A direct small-device replay matched the saved examples but failed a stronger
fractional fixture against a crop of its ordinary full capture: 224 changed
pixels, maximum channel difference 29. Adding a smaller composition clip also
changed eight rotated edge pixels by one unit. Both shortcuts were rejected.

The subsequent source-patch proof retains the ordinary capture's selected image
dimensions, transforms, clipping and complete composition, then crops its output.
Only the demanded filtered source pixels are replaced on their native integer
grid; halos crossing the original image edge are cropped to that existing extent.
The final boundary rerun matched all 108 fixture buffers. Four saved Raster38
paint/erase outputs also matched independent ordinary 1028px captures exactly.
The 66px source updates took 6.96/8.39ms; the 260px updates took 26.45/28.19ms.
These are private component measurements, not editor latency claims.

A private detached contact lane subsequently exercised actual Pencil, Eraser and
Brush handlers at 60Hz on the copied saved layer. It received 120 owned native
source packets, completed all 120 offscreen, retained exactly one history
transaction per tool, and matched each independent final full-capture oracle.
Worker p95 was 10.35ms, maximum 14.20ms; native buffer capture p95 was 0.19ms.
Every copied file remained unchanged. This does not separately prove undo/save
during the new worker's activity; the retained checkout's earlier 11 core tests
remain the authoritative transaction checks.

Native Windows probes then presented one bounded 1028px prepared capture through
the ordinary GPU presenter on the confirmed RTX 5060 at DPR1.5. All three tools
again matched independent final native image and framebuffer oracles, including
when queued packets were coalesced. The first harness run failed during Qt
teardown; explicitly hiding and deleting the private widget with deferred-delete
delivery fixed the harness, and subsequent native runs exited successfully.
The failure remains in `streaming-native-contact-display-r1-diagnostic.json`
and its logs; it is not counted as a passed stability run.

Frame timing is still inconsistent. For example, the retained-owner run R6
presented 107/120 contacts within 16.7ms, but Pencil p95 was 33.14ms and maximum
92.33ms. R8, using Qt's event loop, presented 101/120 within 16.7ms and still
had occasional long Pencil/Brush waits. Reusing the worker owner preserved exact
output but did not eliminate these waits. Separate wall/CPU, admission, source
update and backend-initialization timings plus Python collection observations
are diagnostic evidence; they do not establish a cause or a reliable improvement.

These probes replace the private window's document-presentation entry point
with one prepared capture. They do not cover full-view resource preparation,
production controller admission/invalidation, cancellation, context loss,
chapter switching or physical pen/display latency. No filtered-contact prototype
has been promoted. The next investigation is same-sized presentation texture
reuse and the remaining worker-to-frame delays before production integration.

The later `preservation-filtered-contact-research.json` guard again verified
all 13,776 original files by membership, size, modification time and SHA-256;
all 13,776 benchmark-copy files, actual settings, HEAD and all 706 tested
checkout/source files were unchanged. The private GPU preference was restored,
and the original editor processes remained open.

The completed preservation guard checked every original file's membership,
size, modification time and SHA-256: all 13,776 were unchanged. Actual user
settings were unchanged, the 706-file tested build matched the checkout,
and the private GPU preference was absent after testing. The original
running editor remained open. The overall overnight goal remains active.

## Visible saved-layer probes and texture-reuse decision

The corrected Raster38 probe uses a visible portion of the saved layer at
approximately (866–944, 31900–31912), rather than the covered earlier locations.
In `visible-native-contact-display-r2.json`, all 116 completed worker updates
changed native scene pixels. Pencil, Eraser and Brush each retained one history
transaction, produced different final scene/framebuffer hashes, and matched
independent ordinary full native images and GPU framebuffers exactly. All
13,776 benchmark-copy files and all 706 private source files passed their guards.
It still uses one privately prepared capture and bypasses production controller
preparation, so this remains component evidence.

Three alternating fresh-process pairs then checked the same visibly changed
inputs on the RTX 5060. All six runs matched each tool's native scene and
framebuffer hashes. Reusing same-sized owned presentation textures passed
99 native Windows cases, including 49 new changed/erased-buffer and contract
checks, the ordinary presenter tests, and borrowed device-resource tests.
However, median per-run Pencil p95 was 42.78ms → 35.93ms, Eraser p95
18.84ms → 21.29ms, and Brush p95 16.42ms → 31.74ms. The experiment did not
consistently reduce latency and has not been promoted. Original presentation
texture allocation, ownership, color conversion and budget behavior remain intact.

## Retained perspective live feedback

The ordinary production canvas reproduced a separate delay for a saved convex
perspective raster. Before this change, such rasters always failed feedback
eligibility and repeatedly submitted scene replacements during held input.
Source handlers were quick, but their current pixels appeared much later.
This is a supported configuration that can occur with the free transform frame
shown in the reference image; it does not establish the transform values or
graphics adapter in the already running user editor.

The retained change accepts finite, convex, invertible perspective mappings
only after the shared solver maps all authored corners correctly. It preserves
all nine coefficients and rejects horizon crossings before inverse source
queries and across contributing native source-tile rectangles. Current native
source images draw directly onto the existing prefix using the original layer
clip/transform, SourceOver opacity and ordinary 4×4 native capture origin.
Existing source/effect grids, formats, pixel contracts, memory limits, cache
keys and durable exact pipeline remain unchanged. Unsupported dependencies
and incomplete resource coverage keep the ordinary fallback.

The final `projective-native-contact-final-r2` comparison used fresh serialized
processes on the confirmed RTX 5060 at DPR1.5, zoom1 and zoom0.35. It exercises
the ordinary canvas/controller without replacing its presentation entry point.
All 240 candidate Pencil/Eraser/Brush contacts were observed as current pixels
while held, all used prepared feedback, all were within 16.7ms, maximum 14.626ms,
and all had zero held scene submissions. Per-tool candidate medians ranged
from 4.90ms to 8.74ms. The baseline medians ranged from 151ms to about 185ms,
with zero prepared frames. These are synthetic native handler-to-frame-swap
measurements including observation overhead. Scheduling lateness is recorded
separately and can still spike at first input; physical tablet-driver latency,
display scanout, and arbitrary modified stacks are outside this claim.

The 707-file final build passed 2,326 unique native Windows tests on the
confirmed RTX 5060 with zero failures, errors or skips in 429.238s. Provenance
records no external integration attempts, wrong source imports or changed
source files. Its 52 new cases compare complete native buffers through held
Pencil/Eraser/Brush inputs, nested and mirrored perspective, curved clips,
translucent foreground, RGBA64/RGBA32F source storage, exact undo/redo and
cancellation, and invalid solver/horizon admission. An initial comprehensive
run found one obsolete test expecting valid perspective input to remain pending;
the replacement fallback case checks the still unsupported fixed modifier-source
frame, and the entire native matrix was rerun. A preexisting fixture now fills
its transparent QImage before admission rather than relying on allocator contents.

A fresh complete `stability-projective-r2-copy` of Pocket Boyfriend passed all
11 core transactions in 8.486s, with a perspective warp applied only to its
private saved Raster9 model. Pencil/Eraser/Brush undo/redo, exact cancellation,
mask drawing, fill, copy/cut/paste, Save draining released cold input, exact
save/reopen and chapter switching all passed. The guard verified 7,961 unrelated
private files and actual settings unchanged. The first copied-project harness
completed these transactions but used a POSIX-path manifest against its Windows
allowed-edit prefixes; it incorrectly treated its intended chapter Save as
an unrelated-file change. A second fresh copy using the harness's established
Windows manifest schema passed the complete guard. No failed harness run is
counted as a successful stability run.

Only the verified projective feedback runtime and its regression bytes were
promoted through a guarded 707-file source check. The texture-reuse experiment
and finite filtered-contact replay remain private. Heavy modified Raster38 still
needs production live-feedback integration, and low-zoom views exceeding prepared
coverage still retain their existing fallback. The overnight goal remains active.

The final preservation check verified all 13,776 original Pocket Boyfriend files
with unchanged membership, sizes, modification times and SHA-256 hashes. All
13,776 benchmark-copy files and actual settings also remain unchanged. The
checkout's 707 runtime/test files match the tested final source; the private
GPU preference was restored and the original editor processes remain open.
The checkout's original HEAD is unchanged, and `git diff --check` passes.

## Bounded filtered-contact owner proof (private candidate)

`filtered-native-recipe-source` starts from the retained 707-file perspective
build and has not been promoted. Its ordinary scene capture records owned
native QPainter operations, copies the prefix before the selected raster, and
retains the selected source image's original extent. A finite dirty demand
updates that source plane, then replays the complete original composition on
the unchanged capture device. Captured texture brushes/pens, image storage,
geometry and operation metadata count toward a 64 MiB prepared cap, including
reserved space for the worker's retained updated source and scene planes.
This is transient native resource preparation, with no exact/disk admission.

The strengthened fixture gives the source a realistic 384-pixel interaction
frame and requires every drawing/erasing update to visibly change the scene.
It found a single rotated-edge pixel differing by as much as 70 units when a
parameter mask was sampled on a smaller device. Keeping source captures on
the original 256-pixel grid alone did not fix it. Sampling parameter fields
on the unchanged ordinary stage grid, then cropping those fields for finite
point-filter work, did. Masked spatial stages keep their ordinary grid. All
216 cumulative full-native-image comparisons now pass through fractional,
rotated and sheared placement, ARGB32/RGBA64/RGBA32F sources, opacity 1/0.54,
opaque/transparent backgrounds and source seams. Every update visibly changes
the independent ordinary scene. Earlier fixture comparisons without this
positive visibility check are weaker evidence.

The source-plane guard obtains the ordinary TileGraph's complete output frame
and validates that its original full demand, transform and precision mode
still produce the same extent before accepting a patch. This prevents source
growth or a different native placement from silently clipping new pixels into
an obsolete plane. The serial worker clones its selected model and source
owner off the GUI, captures only changed resident native COW handles and typed
generations, and coalesces one pending packet with its complete dirty union.
Cold source decoding is refused by packet capture. Tombstones preserve eraser
pruning, merged packet bytes have a separate 32 MiB cap, and worker retirement
releases its model, caches, images and context owner on that worker. Its drafts
carry live/contact metadata and have no persistent cache owner.

On the confirmed RTX 5060, the actual saved Raster38 passed six additional
visibly changed complete-native-scene comparisons. Updated filter/composition
work ranged from about 9.42ms to 10.38ms; cold recipe preparation took 7.339s.
The bounded prepared accounting was 51,428,540 bytes, below 64 MiB. These are
native helper timings, not input-to-frame or physical tablet latency. All
13,776 benchmark-copy files remained unchanged.

The frozen 710-file private candidate passed 396 focused native Windows tests
on the RTX 5060 in 65.749s, with zero failures, errors or skips. This includes
34 new cases for native buffers, ordinary partial-block projection tiles,
source ownership, rejected frame/configuration changes, linked dependencies,
cancellation, cold packets, pruning, coalescing and retirement. Provenance
records no external integration attempts, wrong imports or source mutations.
The original 13,776 project files, benchmark copy and actual settings remain
unchanged; the root runtime/tests still match the retained 707-file build,
the private GPU preference was restored and both original editor processes
remain open. Normal canvas integration and complete lifecycle/dirty-coverage
admission are still required before retaining this filtered path.

## Normal filtered canvas integration and cold-source profiling (private)

The frozen 712-file `filtered-canvas-source` adds the ordinary canvas/controller
integration to that foundation. Typed selected-raster changes capture only
resident native COW handles and owned generations. The worker updates its
private finite-filter source, then complete native scene images replace affected
tiles in the normal presentation batch. Erased transparency therefore removes
the old scene pixels. Every dirty requested tile, including its gutter, must
fit a prepared ordinary block and its original requested region. Missing base
tiles, changed identity/configuration, unsupported coverage and source-frame
growth resume ordinary rendering. Provisional tiles never enter exact or disk
storage. Reset, selection changes and graphics-owner changes retire the worker;
completion publication uses a queued signal, including when the ordinary scene
timer is stopped during held input.

The first normal saved-layer probe exposed a cold-owner mistake. Profiling
found 2,073 unnecessary hard-link operations in the first worker fork: about
778ms of its 792ms evaluation. The already captured scene owns revision-reader
jobs and immutable pins. Adopting those existing pins before the normal detached
copy avoids relinking unvisited files. Source ownership preparation now runs
asynchronously before contact, with ordinary render admission and no GUI wait.
The packet lane remains serial with one active and one merged pending update.
A completed future can invoke its callback on the registering GUI caller;
publication wakes explicitly return to the serial worker in that case.

The normal production GPU canvas on copied saved Raster38 passed complete native
buffer and held-to-exact framebuffer comparisons for Pencil, Eraser and Brush.
The verified unprofiled readback run confirmed RTX 5060 / NVIDIA 591.44, DPR1.5,
zoom1, and 40 synthetic 60Hz contacts per tool. All 120 center pixels visibly
changed while held and matched their eventual exact framebuffer color at an
observed presented native revision containing that input. There were zero
ordinary scene submissions and one history transaction per tool. Brush used a
contrasting color after Pencil; earlier same-color Brush runs could observe
only 29/40 changed centers and are weaker latency evidence. Source owners were
ready before the first contact. Prepared accounting stayed below 64 MiB
(51,428,540 / 48,257,020 bytes).

An additional observer check caught a framebuffer-coordinate measurement issue
at fractional DPR. The old center observer inferred height with Python rounding
of 687*1.5; the revised observer uses the actual framebuffer image height. Its
first strict eventual-color run failed before that correction and is not
counted as a pass. Earlier center timing results are superseded. The corrected
run separately records first matching exact center and fully caught-up frame
times. First matching center medians were 13.02ms Pencil, 13.09ms Eraser and 13.85ms
Brush; P95s were 46.99ms, 40.19ms and 27.16ms, with maxima 109.54ms, 43.57ms and 61.00ms.
88/120 inputs were within 16.7ms. Fully caught-up frame medians were 13.02/13.09/
13.85ms, but maxima 218.33/86.74/101.48ms: requiring the whole presentation to
catch up to the newest input overstates the first appearance of an older
contact. The observer makes no changes to normal batch contents or rendering;
additional native readback overhead applies. Physical tablet-driver/display
scanout latency is excluded. This still does not establish that every first
contact is within one frame. Cold complete scene preparation took 47.875s on
this deliberately complex visible stack.

All 79 focused checks passed offscreen in 44.154s. They include ordinary held
painting and release/undo/redo across ARGB32/RGBA64, opaque/transparent pages,
fractional/rotated placement and tile seams; cancellation; queued preparation
and evaluation retirement; identity/configuration/selection guards; complete
base and dirty-gutter coverage; camera zoom/rotation while held; and erasing
the final source followed by exact undo. The frozen integration then passed
all 2,405 native Windows tests on the confirmed RTX 5060 in 497.892s, with zero
failures, errors or skips. Provenance reports no external integration attempts,
wrong imports or mutated source bytes.

A fresh complete `stability-filtered-canvas-copy` passed all 11 core transactions
in 8.670s. The chapter has 106 layers, 173 objects, 103 modifiers and 29 masks at
1080x82000. Drawing/erasing/brush and mask history, cancellation, fill, clipboard,
Save draining cold input, exact reopen and chapter switching passed; 7,961
unrelated private files and actual settings remained unchanged. This core
transaction check disables canvas updates and does not measure live display.

The full preservation guard again verified all 13,776 original project files
and all 13,776 benchmark-copy files, plus actual settings, unchanged. Original
membership, size, modification time and SHA256 all matched. The root runtime
and tests still match the retained 707-file perspective build and original HEAD;
the integrated filtered candidate remains private while the remaining startup
and presentation tail is investigated. Both original editor processes remain
open, and the completed test runs restored their private GPU preference.


## Retained filtered integration and ready original ownership

The `filtered-canvas-ready-source` freeze is now retained in the main checkout.
The guarded promotion copied exactly ten changed/new runtime and test files;
all 712 runtime/test files match the tested freeze. It preserves the previous
allocation, display-affinity and projective-feedback work and original HEAD.
No editor process was restarted.

The private source fork now consumes `ReadyOriginals` into its independent
ordinary source LRUs after the ordinary detached copy establishes immutable
pins. This preserves native formats, profiles, raw bits and decode witnesses,
validates source pins/generations and obeys existing eviction budgets. A later
edited packet supersedes its original COW pixels. The handoff retains no second
source LRU. The prior timeline saw three redundant cold tile reads taking a
combined 2.523ms in the first Pencil update and two taking 1.521ms for Brush.
The new ownership cases verify no decode of the ready buffers, safe source-file
removal, private residency, unchanged original pixels and current edited output
across ARGB32/RGBA64/RGBA32F.

All 104 focused cases passed offscreen in 50.796s. Added production cases verify
that dirty gutters outside prepared demand and exhausted presentation capacity
resume normal held work, then release/undo/redo recover exact native pixels for
Pencil, Eraser and Brush. Destruction of a Qt consumer with an active blocked
worker retires it without waiting; delayed completion cannot revive that owner.
The complete native Windows matrix passed 2,415 tests in 509.271s, with zero
failures, errors or skips on confirmed RTX 5060 / NVIDIA 591.44. Provenance
records no external attempts, wrong imports or changed source bytes.

A new complete 13,776-file copy passed all 11 core transactions in 8.649s:
saved-raster Pencil/Eraser/Brush history, cancellation, mask history, fill,
copy/cut/paste history, Save draining cold input, exact reopen and chapter
switching. The graph has 106 layers, 173 objects, 103 modifiers and 29 masks at
1080x82000. All 7,961 unrelated private files and actual user settings stayed
unchanged. Canvas updates are disabled in this transaction check, so it is not
a live-display measurement. The first attempt reached all 11 transactions but
its final preservation check used POSIX manifest paths against Windows prefixes;
that harness failure is not counted as a pass. The corrected test used another
fresh full copy and verified the Windows-relative exclusions.

Both the prior and ready-source GPU runs independently matched complete native
worker buffers and held-to-released exact framebuffers for all three tools.
Each ran 40 synthetic 60Hz contacts on copied saved Raster38 at DPR1.5, zoom1,
with source preparation complete before contact, zero held ordinary scene
submissions and one history transaction per tool. All 13,776 benchmark files
remained unchanged. Revision-only observers add no per-frame GPU readback; they
record the first swap of a native revision containing an input. These timings
are distinct from the earlier first eventual matching-center observations:

| Revision presentation | Pencil median / P95 / max ms | Eraser median / P95 / max ms | Brush median / P95 / max ms |
| --- | --- | --- | --- |
| Prior integration | 12.68 / 88.64 / 103.69 | 13.43 / 37.57 / 53.63 | 14.23 / 88.73 / 106.62 |
| Ready-source integration | 12.27 / 58.56 / 72.49 | 12.50 / 57.14 / 81.39 | 12.87 / 87.68 / 92.39 |

All 120 input revisions were observed while held; 80/120 ready-source swaps
were within16.7ms. These small samples show ordinary medians around12–13ms,
with mixed tail changes, and do not establish a universal tail improvement or
physical tablet/display latency. Occasional presentation pauses remain even
without readback. Whole-scene cold readiness was47.746s on this deliberately
complex visible stack. Further display-lifecycle timing is a separate private
experiment; no unproven presentation-mode change has been retained.

After promotion the preservation guard verified original membership, byte size,
modification time and SHA256 for all13,776 source-project files; all13,776
benchmark files; actual settings; all712 tested checkout files; and restored
private GPU preference. Both original editor sessions remained open. The goal
remains active to investigate the remaining presentation pauses.


## Display-lifecycle and cursor-isolation diagnostics

A private alternate Qt framebuffer-discard mode was not retained. It produced
mixed Pencil/Brush tails and worse Eraser P95/max (118.63/145.29ms), while the
original mode remains installed. Timing the original paint lifecycle found
most frame composition intervals below0.4ms, but occasional paint-event spans
up to41.18ms; some included paintGL spans up to26.97ms and concurrent source
worker updates up to37.86ms. The follow-up original-mode run remained variable
(27.01/82.21/116.07ms maxima for Pencil/Eraser/Brush). These measurements do not
establish a single driver or scheduling cause. Remaining experiments are
private and leave the tested source unchanged.

The first lifecycle trace reported792 differing framebuffer components for
Brush, despite matching complete native worker output. A repeat original-mode
run and the discard-mode run matched all three held/released framebuffers.
The discrepancy was then reproduced deliberately by holding the mouse cursor
at(80,80): the synthetic8px stroke cursor changes to the configured24px hover
cursor after release. This reproduced exactly792 differing components. With
hover temporarily excluded only during both pixel captures, the complete
held/released artwork framebuffers were identical; the complete native worker
buffer also matched its independent ordinary capture. The synthetic cursor
mismatch is therefore display-overlay behavior, not changed artwork. No
production cursor or drawing behavior was changed.


A separate private native paint-event dispatch alias passed all native-buffer,
source/history and held/released framebuffer checks, but still had mixed
presentation tails (P95/max61.78/78.25ms Pencil,65.58/70.24ms Eraser,
57.02/79.22ms Brush). It was not retained: there is no established consistent
performance improvement over the tested callback. The original paint-event
route remains in the checkout. Further CPU-cycle attribution is diagnostic
only and does not alter renderer output or cache sampling.


Windows thread-cycle attribution narrows the remaining pause without identifying
its cause. In one trace, ordinary worker updates used about30.4million cycles
and7.90ms median; a53.35ms update used38.52million cycles. GUI paintGL used
7.63million cycles/2.17ms median, but a27.29ms frame used122.34million cycles;
a46.01ms paint event used206.12million cycles. This suggests active GUI/graphics
work and delayed worker execution, rather than proportionally more filter math.
Cycles are recorded directly and are not converted into precise CPU milliseconds.
A follow-up isolated the pending indicator:median0.088ms,max0.879ms; long27.14ms
Eraser frames had current contact coverage and an indicator call of0.007ms.
The indicator therefore does not account for these bursts. All three full-native
and held/released framebuffer comparisons passed in both diagnostic runs. The
next investigation targets the GPU presentation calls; no new presentation or
system setting change has been retained from these diagnostics.

## Texture creation and bounded reuse diagnostics

The installed build is PySide/Qt6.11.1 on Ryzen7 7700X (8 cores/16 threads),
about31.15GiB usable memory, and RTX5060/NVIDIA591.44. Windows also reports an
active AMD integrated adapter. Every saved-canvas run below independently
confirmed the RTX renderer; the temporary preference affects only the private
test interpreter and is restored after each run.

Lightweight GUI phase timing isolated long uploads: `display_image` peaked at
0.011ms and the native260px format conversion at0.118ms, while the original
Qt texture constructor reached26.60ms. Explicit equivalent Qt calls narrowed
this further: texture creation reached29.63ms; storage allocation stayed below
0.008ms and the data transfer below0.143ms. The original Qt implementation
creates a texture and uploads RGBA8 pixels without mipmaps in this configuration.
See [Qt6.11.1 texture implementation](https://raw.githubusercontent.com/qt/qtbase/v6.11.1/src/opengl/qopengltexture.cpp).

A separate private direct-GL candidate passed52 native presentation/device/
framebuffer checks and complete saved-layer pixel/history checks for all three
tools. It did not improve the hotspot: explicitly measured `glGenTextures`
reached25.00ms, versus0.506ms for allocation/upload and0.014ms for deletion.
This rules out the image conversion and Qt convenience overload as the cause
of the measured creation bursts. The direct upload code was not promoted.

A second private candidate retained up to32 retired CPU presentation textures,
capped at4MiB within the existing total presentation budget. It reused a texture
only after a zero-timeout GPU fence poll reported completion; pending textures
were never overwritten or waited upon, and borrowed producer textures remained
on their original ownership path. This candidate passed52 native checks. Two
saved-stack repetitions independently matched complete native worker buffers
and held/released exact framebuffers for Pencil, Eraser and Brush. Each observed
all120 input revisions, zero held ordinary scene submissions and one history
transaction per tool, with all13,776 copied files unchanged.

The pool removed the upload bursts: maximum upload0.743/0.879ms and maximum
paintGL5.54/7.40ms in the two repetitions. Overall contact-to-swap tails did not
improve consistently, so it was not promoted. Pencil/Eraser/Brush P95 values
were93.78/125.73/33.70ms and86.61/112.26/65.08ms. The remaining31–34ms paint-event
bursts occurred before `paintGL`, with active GUI CPU consumption and worker
wall delays despite near-normal worker cycle counts. Combining the private pool
with the previously rejected native paint-event alias also remained mixed
(P95 59.24/42.85/80.75ms) and was not retained.

An additional diagnostic explicitly activated the same widget context before
delegating to its unchanged Qt paint event. The activation reached29.35ms,
while the subsequent paint event stayed below8.20ms and uploads below0.509ms.
Thus the remaining pre-paint burst is in context/framebuffer activation rather
than canvas painting. All native and held/released artwork comparisons passed;
overall tails remained mixed, so this diagnostic was not installed either.

A final two-stage activation trace separated the public context activation
from the widget's framebuffer binding: context activation reached27.03ms,
while subsequent framebuffer activation stayed below0.027ms. The resulting
paint event stayed below5.55ms and uploads below0.564ms. Complete worker and
held/released native artwork comparisons again passed for all three tools;
all120 input revisions were observed while held. This identifies the second
hotspot as context activation, rather than framebuffer binding. The overall
tails remained mixed (P95 73.38/96.09/60.90ms), so no activation workaround was
retained. A change needs to address both measured hotspots and demonstrate
better end-to-end responsiveness before it can replace the verified build.

All timings here use the synthetic60Hz native-revision observer without per-frame
readback, and are not physical tablet or scanout latency. Whole native-buffer
and held/released framebuffer comparisons are separate correctness checks.
The first explicit-pointer diagnostic was aborted because this PySide binding
requires a buffer rather than the integer pointer advertised in its signature;
it is excluded from measurements. Its private process alone was stopped and
its GPU preference restoration was verified. Corrected buffer-based runs passed.
The retained checkout still matches the712-file tested filtered-ready source;
no presentation experiment or system-wide GPU setting has been installed.

## Matched presentation comparison and retained native repaint work

The new comparison observes the same boundary for both existing canvas backends:
synthetic 60 Hz input-handler entry to `paintEvent` return. It records presented
native revision metadata without a readback on each frame. This is not backing
store flush, `frameSwapped`, tablet-driver or physical scanout latency. The full
native worker buffers and final held/released widget images are checked separately.
Every completed run below observed all 120 contacts while held, zero ordinary
held scene submissions, one history transaction per tool and unchanged source
pixels on release. All 13,776 benchmark-copy files stayed unchanged.

On the ready-source baseline, forced RTX presentation had Pencil/Eraser/Brush
medians 12.15/12.87/12.91 ms and P95 27.76/57.05/118.89 ms. Existing raster
presentation had medians 15.11/15.49/15.73 ms and P95 16.09/16.93/21.34 ms.
The raster result is smoother but does not justify a blanket renderer switch.
The new source leaves the user's renderer and adapter preferences untouched.

An earlier source-packet handoff passed 110 focused cases and complete saved-layer
native/image/history checks. It started the same bounded detached worker before
repaint, after final source revision publication. It did not improve timing
consistently on either backend, so it was not retained. A separate private raster
graphics-owner experiment was also not retained. The selected saved stack made
no GPU effect requests in those capability probes; their explicit execution
assertion failed. Those probes provide no GPU-acceleration or timing claim, even
though their preceding artwork/history checks passed.

The retained change instead joins complete coherent native live tiles for the
existing raster widget painter. It copies their integer-grid cores into the
existing single 4 MiB presentation surface, preserving source bytes, color space,
one sample per document pixel and all source/effect grids. Fractional viewport
edges include a one-pixel native filtering neighbor. Unknown/mixed inputs,
other precision, tilted cameras, incomplete coverage and larger views fall back.
The provisional context uses completed worker revision and actual image keys,
so an unchanged pending frame can reuse the last coherent surface. It never
becomes an exact or durable entry. The same source also removes the second
projection invalidation after typed raster publication for Pencil, Eraser and
Brush; visual notifications still run normally.

Two clean copied-layer repetitions (`saved-coherent-mosaic-raster-r1` and `r3`)
gave medians 13.77/14.15/14.59 ms and 14.56/14.10/14.32 ms. They completed
116/120 and 113/120 updates within 16.7 ms, versus 106/120 in the matched raster
baseline. Median paint duration fell from 3.36/3.28/3.38 ms to 2.72/3.04/2.76 ms
and 2.78/2.88/2.77 ms. The live surface used 2,638,560 bytes and reused 38–40
pending frames per tool. Full native buffers and held/released widget images
matched exactly for all tools. The `r2` correctness checks also passed, but its
timing is excluded from the clean comparison because a full project copy ran
concurrently; its Brush tail was higher.

The retained 713-file source passed 158 focused checks and 2,426 native Windows
checks with zero failures, errors or skips. The native suite took 504.673 s and
verified RTX 5060, NVIDIA 591.44, GL 4.3 on Kamvas Pro 16. Source hashes, imports
and blocked external-integration guards were clean. A fresh complete project
copy passed all 11 core transaction checks in 8.595 s: Pencil/Eraser/Brush
history, cancellation, mask paint, fill, copy/cut/paste, Save draining released
cold input and exact save/reopen/chapter switching. Its graph still contained
106 layers, 173 objects, 103 modifiers and 29 masks over 1080×82000 pixels.
Those transaction checks disable painting and are separate from display timing.

A fresh interpreter with unchanged Windows preferences selected integrated AMD
graphics. A full ready-source default-adapter GPU run verified AMD Radeon
Graphics/GL 3.3/26.8.1.260806, with medians 11.40/11.79/11.97 ms and P95
20.94/13.14/13.69 ms. It passed the same native/image/history checks. This is
the new test process's adapter, not proof of either already-open editor's
context. The original editors use two different interpreter paths; neither
was restarted or inspected through computer-use control.

A final forced-RTX run of the retained candidate again preserved all artwork,
input and history checks, with medians 12.48/12.14/12.83 ms. P95 remained variable
at 111.60/49.22/61.99 ms. The raster surface does not alter GPU drawing; these
results do not establish a fix for the earlier texture/context stalls. The goal
remains active for that GPU presentation work. No driver, system-wide setting,
renderer default or original editor preference was changed.

Guarded promotion changed exactly five files, then a full preservation audit
confirmed original and benchmark-copy membership, bytes, timestamps and hashes;
all 13,776 original files and original settings are unchanged. The checkout
matches the tested 713-file source, temporary private GPU preference is restored,
and both original editors remain open. The promotion and preservation evidence
is in `live-native-mosaic-coherent-promotion.json` and
`preservation-live-native-mosaic-promoted-r1.json`.


## Measured desktop display route and automatic presentation workaround

The active Windows display-path query reports one Kamvas Pro 16 HDMI output on
AMD PCI device 164E, with the same source and target adapter LUID. Qt identifies
that same screen. The private forced-RTX contexts report RTX 5060 / NVIDIA 591.44
and therefore use a different render adapter from the active output. This routing
observation does not by itself prove the cause of the driver stalls; the matched
presentation comparison supplies the performance evidence. The diagnostic uses
read-only [QueryDisplayConfig](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-querydisplayconfig)
and [device-name queries](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ns-wingdi-displayconfig_adapter_name).
Connector value 5 is [HDMI](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/ne-wingdi-displayconfig_video_output_technology).
The measured route is saved in `active-display-routes-r1.json`.

The retained `canvas_presentation_policy.py` only changes an `auto` canvas on
this identified RTX 5060 / NVIDIA 591.44 / Qt 6.11.1 combination. It requires
one available direct HDMI path on AMD 164E, matching probe/widget/target screen
names and matching source/target adapters. It selects the existing QWidget
presenter, avoiding the measured GUI WGL texture/context waits. Other hardware,
driver/Qt versions, multi-display or unknown routes retain ordinary GPU selection.
Explicit `gpu` and `raster` choices keep their existing behavior. It reads identity
from the existing availability context and uses only read-only display APIs.
No Windows preference, driver, saved setting or source/effect/cache policy changes.

The comparison keeps the preceding common timing boundary: synthetic 60 Hz
handler entry to `paintEvent` return, at DPR 1.5 in a 954x687 widget. It excludes
physical tablet, backing-store flush and scanout latency. All four runs observed
all 120 native input revisions while held, zero ordinary held scene submissions,
one history transaction per tool, unchanged raster source bytes on release,
independent complete native worker-buffer equality and held/released widget-image
equality. All 13,776 benchmark files stayed unchanged. Pencil can finalize its
ordinary model metadata on release; model immutability during release is not
claimed. No heavy copy/test workload overlapped these measurements.

| Presenter | Median P/E/B (ms) | P95 P/E/B (ms) | Updates within 16.7 ms |
| --- | --- | --- | --- |
| Explicit RTX GPU | 12.56/12.22/12.72 | 61.58/51.82/30.63 | 81/120 |
| RTX auto, first clean run | 14.08/14.27/15.03 | 25.28/15.19/16.40 | 115/120 |
| RTX auto, second clean run | 14.19/13.98/14.54 | 16.10/15.01/16.15 | 116/120 |
| Unforced AMD auto GPU | 11.49/11.62/12.04 | 20.91/13.07/13.32 | 116/120 |

The workaround improves the long tail and the fraction inside a 60 Hz frame.
Its typical repaint is roughly 2 ms slower than RTX presentation. A few outliers
remain: the two auto runs reached maximum Pencil/Eraser/Brush times of
35.13/16.39/35.11 ms and 30.92/17.15/26.25 ms. This is a measured presentation
workaround, not a guarantee that every physical input arrives inside one frame.
The unforced AMD path remains a GPU widget and retains its earlier roughly
11–12 ms median and 116/120 timely updates.

GPU computation remains available through the existing graphics worker and
rendering pipeline. Two actual asynchronous Apply Modifier actions on 1024x1024
Raster sources used RTX segments and final materialization off the GUI thread.
Both normal and soft-light point stacks matched independent per-stage CPU
references for every native pixel; undo restored the originals and redo restored
the exact baked result, with one transaction each. The small saved filtered
stack's earlier zero-GPU-request probes are not reinterpreted as GPU acceleration.
Native tiled effects still use their ordinary CPU admission when appropriate.

The promoted 716-file source passed 2,451 native RTX checks with zero failures,
errors or skips in 509.739 s. The matrix is the retained 2,426-case suite
plus 23 presentation-selection guards and the two real GPU bake checks.
Native identity, source hashes, module origins and external-integration guards
passed. A fresh full `stability-display-route-auto-copy-r4` began with all
13,776 SHA matches and passed all 11 core workflows in 8.639 s
with automatic presentation: drawing/erasing/brush history, cancel, masks, fill,
copy/cut/paste, Save draining released cold input, exact save/reopen and chapter
switching. The graph remained 106 layers / 173 objects / 103 modifiers / 29 masks
at 1080x82000; 7,961 unrelated private files and original settings were verified.
Those transaction checks disable painting and are separate from live timing.

The original editor executable preferences were read-only queried and both had
no explicit Windows per-executable entry. Their live GL contexts remain unknown;
neither original process was restarted or controlled. The new default process
selected AMD. The actual GPU availability identity drives the policy on the next
launch, rather than assuming the already-open editor's adapter.

Guarded promotion changes exactly `canvas.py`, the new policy and its two test
modules. Runtime/tests match `display-route-presentation-source-r4-manifest.json`
(716 files), superseding the preceding 713-file checkpoint. Evidence includes
`display-route-presentation-r4-native-rtx-r1-stability.xml`, `saved-project-stability-display-route-auto-r4.json`,
the four timing labels above and `display-route-presentation-r4-promotion.json`.
The overnight goal remains active for further input/physical-display validation;
the source change takes effect on the next ordinary editor launch.


## Latest-source handoff under sustained tablet input

High-rate private QTabletEvent delivery exposed a delay missed by the preceding
60 Hz direct-handler probe. The retained worker used one active and one merged
pending packet, but collecting a completion could launch that older pending
source before current typed generations were captured. The baseline timing
trace measured median queued wait of 6.71/6.45/9.78 ms for Pencil/Eraser/Brush;
native jobs themselves took 12.15/11.84/10.81 ms, including admission.

The latest retained change captures and merges current resident native source
handles before polling. Completion-only polls publish ready work but defer
starting an older pending packet while uncaptured generations exist. The next
capture merges their values and complete dirty coverage before starting work.
There is still one active and one bounded merged pending display job. Every
received pressure sample, timestamp and order still reaches ordinary source
editing; only intermediate display work is combined. Source/effect grids,
native precision, filter/painter math, memory caps and exact/disk cache behavior
are unchanged. This change is independent of the earlier presentation policy.

The matched runs used the full 106-layer saved chapter, a ready complex filtered
Raster38 viewport, DPR 1.5, zoom 1, and a 954x687 widget. The pressure stream
delivers 240 samples/s for one or four seconds with a final release. Timing runs
were sequential without heavy copy/test work. Scope is handler entry to
paintEvent return, excluding physical driver, backing-store flush and scanout.
Both presenters use the same observer without per-frame readback.

The table reports only samples that changed native source revision. The long
Brush stroke changes pixels for 404 of 960 samples because normal brush spacing
can skip a stamp; all 960 samples still participate in ordered source replay.

| Matched stream | Previous median P/E/B (ms) | Retained median P/E/B (ms) | Previous P95 P/E/B (ms) | Retained P95 P/E/B (ms) |
| --- | --- | --- | --- | --- |
| RTX auto, 960 samples/tool | 34.02/33.60/32.41 | 25.83/26.10/24.53 | 41.20/40.81/39.41 | 33.24/32.10/33.84 |
| Default AMD GPU, 240 samples/tool | 26.04/26.49/30.55 | 23.50/23.35/25.67 | 34.32/33.44/38.80 | 29.73/28.59/33.32 |

The long RTX run improves typical changed-source latency by roughly 22–24%.
The default process selects AMD/GPU presentation and improves too. Every
comparison retained exact complete native buffers and held/released images,
unchanged source bytes at release, one transaction per tool, zero ordinary held
scene submissions, ordered pressure replay equality and all 13,776 benchmark
file hashes. These measurements do not identify the already-open editors'
actual contexts. The RTX private preference was restored after each wrapper.

A fresh matched 60 Hz comparison measured previous median/P95
14.15/14.09/14.38 / 16.06/14.94/17.26 ms,
and retained median/P95 14.37/14.42/14.24 /
17.32/15.10/17.03 ms, with 115/120 and
114/120 updates within 16.7 ms respectively. The first
candidate 60 Hz run remained 13.74/14.26/14.63 ms typical, with 112/120 timely
updates. Outliers remain: the long retained RTX Pencil run reached 51.32 ms.
The high-rate median is still above 16.7 ms on this complex filtered layer;
the overnight responsiveness goal remains active.

The 717-file source passed all 104 focused checks and 2454 native
RTX checks in 512.049 s, with zero failures, errors or skips.
Three new regressions hold a real native job, submit older pending source,
then add newer Pencil/Eraser/Brush input. They require evaluating the newest
merged source directly and exact independent native presentation. Source SHA,
module-origin and external-integration guards passed.

A fresh full stability-latest-filtered-packet-copy-r1 began with 13,776 SHA
matches and passed all 11 core workflows in 8.747 s. Its graph
remained 106 layers / 173 objects / 103 modifiers / 29 masks at 1080x82000.
Drawing/erasing/brush undo/redo, cancellation, masks, fill, copy/cut/paste,
released cold-input Save drain, exact save/reopen and chapter switch passed.
All 7,961 unrelated private files and original settings were verified.
These transaction checks disable painting and remain separate from live timing.

Guarded promotion changes only the filtered worker, its UI state and the new
test module. Root runtime/tests match latest-filtered-packet-source-manifest.json
(717 files), superseding the 716-file checkpoint. No original editor was
closed, restarted or controlled. Evidence is latest-filtered-packet-promotion.json,
the matched stream labels above, latest-filtered-packet-native-rtx-r2-stability.xml
and saved-project-stability-latest-filtered-packet-r1.json. Final preservation
audit follows this documentation update under a fresh label.


Final latest-source preservation audit completed: `preservation-latest-filtered-packet-final-r1.json` verifies
all 13,776 original files' membership, bytes, timestamps and SHA hashes, all
13,776 benchmark files, original preferences, and the 717 root/frozen source
hashes. The private RTX interpreter preference is absent and HEAD remains
8f704f4feb5cff07e3870d900f2bc09e94afc747. Both original editor processes
20568/33756 remain responding. All private test wrappers finished.


## Nested immutable metadata ownership in live filter jobs

A bounded diagnostic cProfile run collected 16 native jobs per tool and retained
all exact source, native image, release, history and ordered pressure-replay
checks. It attributed roughly 3 ms inclusive per profiled job to repeatedly
detaching deeply nested cache metadata. Profiling overhead is included; these
are attribution measurements, not clean input latency. HSL and outline work
remain important costs and retain their existing arithmetic.

The retained change proves an exact built-in tuple tree contains only None,
bool/int/float/str/bytes and exact tuples, using an iterative walk. Proven
immutable trees can be shared. The existing flat-tuple path remains first;
mutable containers, Qt values, scalar subclasses and callbacks keep their
ordinary independent copying/rejection behavior. There is no global memo or
additional retained cache. Key values, scalar bits, dependencies, storage
accounting, precision and native source/effect sampling are unchanged.

A representative micro-test reduced nested-key detachment from 0.04982 to
0.03235 ms (about 35%), with flat keys unchanged. The two clean 4-second RTX
240 Hz streams each delivered 960 samples/tool; Brush changed source revision
for 404 samples due to ordinary spacing. Changed-source median/P95 results:

| Source | Median P/E/B (ms) | P95 P/E/B (ms) |
| --- | --- | --- |
| Fresh 717-file baseline | 25.69/26.08/23.77 | 32.16/32.03/32.73 |
| Candidate first run | 22.75/23.11/21.12 | 31.03/31.13/29.75 |
| Retained repeat | 23.05/22.63/21.48 | 31.89/29.64/31.03 |

The fresh repeat improves typical handler-to-paint latency by about 10–13%.
Pencil/Eraser input-handler P95 was higher in these dense RTX streams, but the
scheduled-sample-to-paint medians also improved (29.60/29.77/27.43 to
27.70/26.61/25.27 ms). This combined boundary includes synthetic delivery
lateness and supports a 6–11% gain in that test. It still excludes physical
tablet driver, backing-store flush and scanout. Both presenters use one common
paint-return observer, with no per-frame readback. Timing runs were sequential.

The unforced AMD/GPU 240 Hz run also passed every exact/source/history/replay
check. Changed-source medians went from 23.50/23.35/25.67 to
22.88/21.70/23.50 ms;
P95 was 28.35/27.05/30.41 ms.
The clean 60 Hz direct-handler run measured median
13.29/13.57/14.25 and P95
14.30/15.41/19.58 ms. Input handlers stayed
essentially unchanged and 114/120 updates were within 16.7 ms, matching the
preceding retained run's count. Brush tails remain variable.

Every clean stream observed all delivered samples' revisions while held,
zero ordinary held scene submissions, one transaction/tool, independent
complete native-buffer equality, held/released image equality, unchanged
source bytes at release, ordered pressure-replay equality and all 13,776
benchmark file hashes. This does not imply a distinct frame per input sample.
The dense RTX repeat still has 21–23 ms typical latency and a Pencil maximum
of 50.28 ms, so full realtime responsiveness remains unproven.

All 229 focused checks and 2478 native RTX checks passed with zero
failures, errors or skips (512.343 s native). The 24 new
ownership cases cover both real pooled metadata modes, deep mutable state,
float NaN payload/signed-zero bits, QImage/NumPy copies, scalar subclasses and
rejected editor callbacks. Native module-origin/source/external guards passed.
A fresh full stability-nested-detachment-copy-r1 began with 13,776 SHA matches
and passed all 11 core workflows in 8.410 s. The graph remains
106 layers / 173 objects / 103 modifiers / 29 masks at 1080x82000;
7,961 unrelated private files and original settings were verified. Transaction
checks disable painting and remain separate from live timing.

Guarded promotion changes scene.py and the new ownership test only. Root
runtime/tests match nested-detachment-source-r2-manifest.json (718 files),
superseding 717. No sampling, filter, presentation-policy or durable-cache
contract changes. All original editors remain open; this code applies next
ordinary launch. Final original/source/preferences audit follows documentation.


Final nested-metadata preservation audit completed: `preservation-nested-detachment-final-r1.json` confirms
all 13,776 original files' membership, bytes, timestamps and SHA hashes;
all 13,776 benchmark files and original preferences; and all 718 root/frozen
source hashes. The private RTX interpreter preference is absent, HEAD remains
8f704f4feb5cff07e3870d900f2bc09e94afc747, and original editor processes
20568/33756 remain responding. All private test wrappers finished.


## HSL experiments and visible native-feedback coverage

This pass retains the 718-file nested-detachment-source-r2 checkpoint without
promoting another runtime change. All three proposed HSL arithmetic changes
were rejected. The in-place variant passed 54 exact working-pixel comparisons,
but only saved 0.8–1.9% on 256-square micro inputs. Scalar branch selection and
shared hue scratch each passed 486 comparisons across float16/32/64, masked
fields, HDR/negative colors and scalar extremes. Scalar branch selection saved
only 1.66% on the saved-size input; shared scratch made that input about 1.5%
slower. Filter arithmetic, source/effect grids, precision and cache contracts
remain unchanged. Evidence: hsl-experiments-decision.json.

A 48-call saved input census found roughly 69–76 by 68–72 float32 inputs,
finite scalar HSL settings and nearly opaque Pencil/Brush pixels. Eraser inputs
were about 92–99% visible. The existing large sparse-input shortcut therefore
does not address this workload. The refreshed 48-job native profile identifies
HSL, native image composition/copies and immutable tuple proof as remaining
costs; cProfile overhead is included and its timings are not clean latency.
The copy-type census finds primarily mask/modifier model records. Their saved
serializers normalize values, so independently owned records remain necessary.

A frozen one-file candidate used a guarded fixed membership table for exact
built-in scalar types and one tuple walk. All 229 focused checks passed, with
zero failures/errors/skips. It reduced isolated ownership checks but did not
consistently improve the full native RTX 5060 pressure stream:

| Source | Changed-source median P/E/B (ms) | P95 P/E/B (ms) |
| --- | --- | --- |
| Fresh retained 718 baseline | 22.04/24.03/21.60 | 29.48/32.45/31.11 |
| Experimental type-membership candidate | 22.94/23.11/21.18 | 31.03/30.69/30.71 |

Both sequential 960-sample/tool runs preserved all delivered source samples,
ordered pressure replay, complete independent native buffers, held/released
images, unchanged source pixels at release, one transaction/tool, zero ordinary
held scene submissions, all 13,776 benchmark hashes and all frozen source
hashes. Brush changed revision for 404 of 960 samples due to normal spacing.
The candidate remains external and rejected (immutable-types-decision.json).
These tests measure Qt handler-to-paintEvent return, excluding physical pen
driver and scanout. No broad native requalification or root promotion was
needed for the rejected candidate.

The corrected coverage census and a separate neighboring native-capture census
also pass the unchanged 240-sample/tool pressure/native/history checks and
13,776 benchmark hashes. The diagnostic chapter retains saved visibility and
enables the selected layer/ancestor: 100 of
106 layers are visible. The metadata-only 28-square
influence at x866/y31730 and y31744 is outside prepared coverage, while
y31760/31800/31900/32060/32250 is covered. This establishes a visible admission
gap, not measured crossing latency or the cause of a physical pen delay.

Only native block (0,0,31) was prepared, with 51,428,540 reserved bytes including
future source/scene progress. Its selected source is 1027x1029; prefix and
initial are full 1028x1028 native devices. A standalone ordinary neighboring
block (0,0,30) capture reserved 48,260,540 bytes.
Independent per-recipe summation is 99,689,080
bytes; 0 immutable COW storages overlap by
0 bytes. Unique images, all metadata and both
reserved progress pairs total 99,689,080 bytes (above
the existing 64 MiB cap). This is ownership evidence from two ordinary captures;
production preparation/accounting and its limits remain unchanged.

The first coverage helper stopped before contact on a missing QRectF import;
the separate corrected helper/run passed. This was diagnostic setup, with the
718-file runtime unchanged and the private preference restored in both runs.
The current retained build remains qualified by 2478 native RTX checks and
11 workflows on a fresh full Pocket Boyfriend copy from the preceding retained
checkpoint. Further work should quantify actual coverage crossings and assess
bounded shared ownership before changing preparation. Preserve original
256-pixel source/effect grids and the full native capture/composition device.
Full realtime responsiveness remains unproven; the goal stays active.


Final HSL/coverage preservation audit completed: preservation-hsl-coverage-final-r1.json
verifies all 13,776 original files' membership, bytes, timestamps and SHA hashes,
all 13,776 benchmark files, original settings, and all 718 root/frozen source
hashes. The private GPU preference is absent, HEAD is unchanged, and both
original editors (20568/33756) remain responding. All private workloads finished;
git diff --check passed. No runtime/test changes were promoted in this pass.


## Retained preview/native contact handoff guard

Actual strokes from y31810 to31730 across native boundary31744 reproduced a
paint exception on the retained 718-file build. After a prior viewport preview,
filtered feedback interpreted its ('preview', serial) key as a ProjectionAddress
and failed on int.level. The independent new regression reproduces that same
failure on the unchanged runtime. The retained 719-file source classifies native
tile batches before replacement. Only a positively owned contact with complete
validated native coverage can substitute its native basis for an earlier
viewport preview; no ready replacement leaves the supplied view intact.
Other previews and unknown/overview keys remain on ordinary rendering.

The six new cases cover actual held Pencil/Eraser/Brush contacts, native image
equality, source edits, one history transaction, undo/redo, and unowned non-native
batches. All 235 focused checks and 2,484 native RTX 5060 checks passed with zero
failures/errors/skips. A fresh complete 13,776-file Pocket Boyfriend copy passed
all 11 core workflows, including drawing/erasing/brush undo-redo, cancellation,
mask paint, fill, clipboard, save draining pending input, reopen and chapter
switching. It preserved 7,961 unrelated private files and original settings.

Clean changed-source handler-to-paint-return medians (Pencil/Eraser/Brush ms):

| Run | Prior 718-file source | Guarded 719-file source |
| --- | --- | --- |
| RTX, 960 samples/tool | 22.04/24.03/21.60 | 22.78/23.15/21.73 |
| Default, 240 samples/tool | 22.88/21.70/23.50 | 21.80/21.40/23.20 |

Both ordinary 240 Hz streams preserve every delivered source sample, ordered
pressure replay, full independent native buffers, held/released images,
unchanged source pixels at release, one transaction/tool and zero ordinary held
scene submissions. Brush changes pixels for 404/960 samples in the RTX run;
unchanged samples are excluded from these medians. These runs show no material
warm-contact slowdown from the stability guard; they do not establish physical
pen-driver or scanout latency.

The corrected crossing test passes 120 samples/tool at 60 Hz, independent current
viewport references, ordered source replay and history checks without the old
exception. It also reveals a separate severe coverage cliff: changed-source
P95 is 13.20/13.32/3.89 seconds, with 2/2/22 ordinary held scene submissions.
The held viewport and released native images differ under the existing
presentation policies, despite source equality. Initial prepared readiness
was 47.32 seconds. Full realtime responsiveness therefore remains unproven.

Native grids, complete 1028px devices, source/effect 256px grids, color/precision,
cache identities and 64 MiB prepared/32 MiB packet/crop limits remain unchanged in
the retained fix. A private 128 MiB neighboring-region experiment is isolated:
the measured 99,689,080-byte ownership sum fits it, but the extra preparation
cost, warm responsiveness and lifecycle/admission must be measured before any
promotion. This experiment is not part of the retained 719-file source.

Evidence: preview-native-feedback-promotion.json,
preview-native-feedback-focused-r1.xml,
preview-native-feedback-native-rtx-r2-stability.xml,
saved-project-stability-preview-native-feedback-r1.json,
saved-crossing-preview-native-rtx-r2.json and the comparison labels in the
promotion record. Only the two UI files and new regression module were promoted.
No original editor was closed or restarted. The goal remains active.

Final preservation-preview-native-feedback-final-r1.json verifies all 13,776
original files' membership, bytes, timestamps and SHA hashes, all 13,776 benchmark
hashes, original preferences and all 719 checkout/frozen source hashes. The
private GPU preference is restored, HEAD is unchanged, original editor
processes 20568/33756 remain responding, and git diff --check passes.


## Retained adjacent native region capacity

The 64 MiB prepared limit admitted one complete native region on the selected
complex saved raster, leaving a visible neighboring row uncovered. Crossing
that boundary resumed ordinary scene work and caused multi-second tails. The
retained 720-file source raises only filtered preparation capacity to 128 MiB,
including all captured images, metadata and reserved source/scene progress.
Both serial worker paths reserve the actual prepared sum plus their existing
snapshot workspace; scene preparation reserves the 128 MiB bound through shared
admission. Source packets and presentation crops remain capped at 32 MiB each.
Global admission and source cache budgets remain unchanged. Explicit smaller
worker budgets still reject complete ownership that exceeds them.

The same 120-sample/tool, 60 Hz RTX crossing path has the following Qt
handler-to-paint-return results (ms), excluding physical driver and scanout:

| Tool | Prior P95 | Retained P95 | Retained median | Retained maximum |
| --- | --- | --- | --- | --- |
| RASTER_PENCIL | 13199.70 | 37.90 | 16.26 | 45.80 |
| RASTER_ERASER | 13319.36 | 40.22 | 15.55 | 43.38 |
| BRUSH | 3886.71 | 26.79 | 17.24 | 42.25 |

All delivered source/pressure samples survive ordered replay. Both complete
independent native region buffers match for each tool, held and released images
match, release preserves source bytes, and each stroke remains one history
transaction. Covered contact stays active with zero ordinary held scene jobs.
Prepared reservations are 99,685,560 bytes for Pencil and 96,514,040 for
Eraser/Brush. RTX prepared readiness was
46.68 seconds versus
47.32 before in these runs. This
establishes the measured comparison, not a general startup-time guarantee.
The default-adapter crossing passes the same native/source/replay/history and
held/released guards; its timings are in the promotion record.

Ordinary changed-source medians at 240 Hz (Pencil/Eraser/Brush ms):

| Run | Prior 719-file source | Retained 720-file source |
| --- | --- | --- |
| RTX, 960 samples/tool | 22.78/23.15/21.73 | 22.26/22.08/21.28 |
| Default, 240 samples/tool | 21.80/21.40/23.20 | 22.64/21.79/23.07 |

Both ordinary streams preserve full independent native buffers, every delivered
source sample, ordered replay, held/released images, release source pixels and
one transaction per tool. These paired runs show no material warm-contact
slowdown. Brush samples that do not change source pixels are excluded from the
medians. All 13,776 benchmark hashes remain unchanged.

Four new regressions cover actual held Pencil/Eraser/Brush contacts across two
native regions, unchanged owned bases, native equality, undo/redo, preparation
and contact memory-ticket observations, and explicit smaller-budget rejection.
The first synthetic fixture used eight additional full-plane foreground HSL
filters; its focused run passed, but its independent native references exhausted
allocation capacity after the broad suite (three MemoryErrors). It was replaced
with eight fixed original PNG image planes, preserving the same actual >64 MiB
ownership boundary and all assertions while removing unnecessary filter buffers.
The candidate runtime did not change during that test repair. All 239 focused
checks and 2,488 native RTX checks then passed with zero failures/errors/skips.
A fresh complete 13,776-file Pocket Boyfriend copy passed all 11 core workflows,
including drawing/erasing/brush, cancellation, mask paint, fill, clipboard,
pending-input save, reopen and chapter switching, with 7,961 unrelated private
files and original settings preserved.

Eligibility still uses the ordinary legacy-pixel, blend, dependency and geometry
restrictions. Floating documents and unsupported effects retain ordinary paths.
Original pixels, 256px source/effect grids, full 1028px devices, color/precision,
camera-independent artwork sampling, semantic cache keys and persistence remain
unchanged. Live recipes/results never become durable exact cache entries. No
sampling exception or separate disk renderer was introduced.

The candidate process memory sample includes independent full native reference
renders and history: peak working set 3,921,481,728 bytes and peak paged memory
4,961,488,896 bytes. There is no paired baseline peak, so this is not a claim of
memory reduction or per-stroke memory use. Coverage beyond admitted regions,
cold real-input behavior, dense-stream tails and physical pen/display latency
remain incomplete. The optimization goal stays active.

Evidence: neighbor-capacity-promotion.json, neighbor-capacity-focused-r4.xml,
neighbor-capacity-native-rtx-r2-stability.xml,
saved-project-stability-neighbor-capacity-r1.json,
saved-crossing-neighbor-capacity-rtx-r1.json and
saved-crossing-neighbor-capacity-default-r1.json. Frozen r2 crossing runtime
matches retained r5 exactly; only the new regression file differs. Guarded
promotion changes three render files and that regression module.


Final preservation audit: all 13,776 original files retain their bytes,
modification times and SHA-256 hashes; all 13,776 benchmark hashes and original
settings also match. The checkout matches all 720 frozen tested source hashes,
the private GPU preference is restored, HEAD is unchanged, and both original
editor processes remain responsive. Existing editor sessions were preserved;
the retained code applies when the application is next launched.

An unresolved coverage hypothesis needs a separate measured test. Ordinary warm
Eraser/Brush preparation after history reports approximately 48.26 MB of owned
regions versus 99.69 MB for the initial Pencil preparation. SceneController
removes already-ready requests before filtered preparation consumes the remaining
requests. A neighboring exact region could therefore be omitted after feedback
is retired by undo or selection. This is code and preparation metadata evidence,
not a demonstrated post-undo crossing failure. Next reproduce a warm stroke,
release, undo, settle, then the same native boundary crossing, checking both
native regions, ordered replay, history, held/released equality and latency.
Start that investigation from the retained 720-file source. Do not change the
qualified candidate or claim that the broader coverage problem is solved.


## Retained complete visible feedback through source-only history

The previously proposed coverage problem is now reproduced: eight saved Pencil
samples at y31900, release, undo and full settlement, then the same crossing of
y31744. The old 720-file source discards its prepared owner and prepares only
missing exact tiles, omitting an already-ready neighboring native region.
Pencil/Eraser/Brush changed-source P95 becomes 12.665/12.698/3.949 seconds.
Sources, ordered replay and history stay correct, while held/released views
differ. The retained 721-file source gives filtered preparation the complete
visible native demand, while ordinary rendering still evaluates missing tiles.
It also retains eligible source-only undo/redo ownership through the same typed
native packet path. Structural/order/unrelated artwork, unsupported dependencies
and source-frame failures keep their ordinary retirement/fallback guards.

Keeping an owner introduces a real history publication hazard: a delayed
pre-undo completion can put the old stroke back on screen. On typed resource
undo/redo, the presentation floor advances to the upcoming ordinary projection
revision. Older updates finish their private owner work but cannot replace
current presentation tiles. New source samples keep their original order and
one-command transaction. Metadata-only history does not advance this floor.

Same corrected RTX crossing, 120 samples/tool at 60 Hz, Qt handler-to-paint-return ms:

| Tool | Old P95 | Retained P95 | Retained maximum |
| --- | --- | --- | --- |
| RASTER_PENCIL | 12664.90 | 25.79 | 33.91 |
| RASTER_ERASER | 12698.38 | 40.06 | 44.11 |
| BRUSH | 3949.37 | 37.81 | 50.48 |

All 120 contacts/tool are observed while held. Both independent whole 1028px native
fields match for every tool; held/released images, release source bytes, ordered
pressure replay and one history transaction match. No ordinary held scene jobs
are submitted. The default-route crossing passes the same guards; its exact
timings and identity are in the promotion record. These are editor paint-return
measurements, excluding physical driver, pen transport and scanout.

The three-cycle RTX readiness comparison measures undo-ready median
10.185s before and
6.654s retained, and warm stroke-ready
median 6.852s before versus
6.957s retained. Initial prepared
readiness is 47.729s before and
47.701s retained. This is a measured saved-view
comparison, not a general cold-start or immediate post-undo input guarantee.

Changed-source ordinary medians at 240 Hz, Pencil/Eraser/Brush ms:

| Route | Retained720 baseline | Guarded721 candidate |
| --- | --- | --- |
| RTX, 960 samples/tool | 22.49/22.83/21.43 | 23.28/23.90/23.51 |
| Default, 240 samples/tool | 22.64/21.79/23.07 | 22.74/22.77/24.37 |

The RTX pair uses a fresh sequential baseline and guarded candidate. The first
comparison with an older baseline put unguarded Brush median 23.414 ms against
21.278 ms, narrowly outside the fixed 10% admission threshold; it was not retained.
The clean pair above passes the original median/P95 thresholds without relaxing
them. Every delivered source/pressure sample survives replay, independent native
buffers and held/released images match, and each gesture remains one command.
Brush inputs that do not change the recorded native revision are excluded from
these medians. Both graphics routes preserve all 13,776 benchmark hashes.

Seven new cases cover three tools crossing after undo, retained owner identity,
two complete visible regions, reselection with no missing ordinary tiles,
source/history/redo ownership, and three held-worker history races. The race
fixture exposes the selected HSL leaf, starts with the existing padded release
frame, requires a visibly different independent native image before undo, and
excludes only pending-contact UI decorations. Both old and restored workers and
ordinary scene work are held by explicit barriers; static artwork and a resumed
contact must not resurrect old ink, cancellation preserves redo, and final
native references match. The unguarded case fails on 1,392 artwork components;
the guarded cases and final 246-case focused matrix pass. Earlier weak fixture
variants either compared the pending indicator, hid ink behind foreground PNGs,
or correctly lost source-frame eligibility; they are not race-proof evidence.

All 2,495 native RTX checks pass with zero failures/errors/skips and verified
source/module/renderer provenance. A new complete Pocket Boyfriend copy passes
all 11 core workflows: open, Pencil/Eraser/Brush undo/redo, exact cancellation,
mask painting, fill, copy/cut/paste, saving accepted pending input, and
save/reopen/chapter switching. Original preferences and 7,961 unrelated private
files remain unchanged. This core transaction check and the native visual/perf
checks are separate evidence scopes.

The first complete-visible-only experiment was rejected: it moved undo-ready
median from 10.185 s to 30.539 s. Typed owner reuse and its history floor are the
qualified result. Preparation 128 MiB, packet/crop 32 MiB limits, native grids,
source/effect sampling, color/precision, semantic cache keys and disk pipeline
remain unchanged. Live ownership/results never become durable exact entries.
No sampling exception was introduced. Cold readiness, coverage beyond admitted
regions, dense stream tails and physical pen/display latency remain incomplete.

Evidence: visible-feedback-history-promotion-r3.json,
visible-feedback-history-guarded-focused-r3.xml,
visible-feedback-history-native-rtx-r3-stability.xml,
saved-project-stability-visible-feedback-history-r3.json,
visible-history-publication-stable-baseline-r1.xml and guarded-r1.xml,
and the exact readiness/crossing/warm labels in the promotion record.


Final guarded history preservation audit: all 13,776 original files retain their
original membership, bytes, modification times and SHA-256 hashes; all 13,776
benchmark hashes and original settings also match. The checkout matches all
721 frozen tested source hashes, the private GPU preference is restored, HEAD
is unchanged, both original editors remain responsive, and git diff --check
passes. Existing sessions were preserved; retained code applies when the
application is next launched. Cold readiness and wider/physical-input latency
remain outside this completed optimization and stability pass.

## Exact Chapter 1 Rewritten input repair, 2026-10-10

This later pass reproduces the user's selected `Raster 60ststssststss`
(`06be89900eb540ee83a26514822d32ec`) in Chapter 1 Rewritten, rather than
substituting a different modified raster. The current immutable private copy
contains 13,825 files / 357,107,152 bytes. Evidence is under
`.artifacts/realtime-chapter1-20261009/`. This pass uses offscreen Qt input and
CPU presentation; its timings do not qualify physical Windows pen, GPU or
display latency. The user is handling the missing Windows GPU independently.
No driver, Windows graphics setting or hardware recovery change is part of
this pass.

The selected raster has an ordinary separable ancestor chain but a cumulative
fractional origin `(-412.81371802702984, 49996.311819029186)`. The old plain
feedback gate rejected that origin, so received native paint samples changed
source pixels while the displayed chapter remained stale. Fractional origins
now use the existing mapped native-world feedback path without rounding the
origin or changing the saved pixels. In the paired saved-leaf probe, restoring
only the old gate produced 0 visible held updates out of 114 source-changing
inputs; the repaired path produced 114/114, with no ordinary held scene
submissions, exact held/released native artwork and one undo transaction per
tool. These are controlled CPU checks, not a physical input measurement.

Cold contact preparation now retains one typed dependency owner across selected
pixel changes. A private, nonrecording scheduler prepares native prefix/source/
suffix planes progressively; the first input address and every subsequent
changed or pruned address remain in its substitution journal. It borrows only
validated completed original ownership, yields to native input between bounded
metadata slices and wakes through a queued completion signal. Large selected
maps capture a bounded native inverse-view subset through the ordinary COW/
file-pin path, retaining complete global alpha and authored frame metadata.
There is no alternate renderer, durable live result or sampling exception.

The feedback-only lane retains at most 64 MiB of native planes; ordinary
preparation keeps its 32-MiB default. Full dirty-source coverage permits a
bounded subset to remain live in a wider viewport. A production saved-chapter
1024×927 widget at zoom 0.3 retained 65 native patches / 52,728,000 bytes.
Pencil, Eraser and Brush held/released full widget and native patch comparisons
were exact; each used one bootstrap and zero ordinary held scene submissions.
First cold visible feedback was 178/159/202 ms in that run. Ready handler p95s
were 2.97/2.95/5.19 ms; these separate cold preparation from received-input
service. The ordinary renderer still handles dependencies or footprints outside
the positively proven bounded lanes.

Received Pencil/Eraser moves skip unrelated gizmo hit tests during an owned
stroke. Release retains its final endpoint and the last received positive pen
pressure. Focus/capture loss and Escape resolve the owned transaction and clear
pen/mouse suppression. The custom painting cursor follows the active input
family and hides the standard arrow during painting. Native input lifetime,
pressure/order, vector isolation and cursor checks accompany these changes.

The presentation repair gives neighboring native tiles disjoint physical pixel
ownership while retaining their existing gutters. Large transformed native
blocks use ordinary native display tile geometry, and eligible CPU replacement
retains native alpha until display composition. This fixes display seams without
altering source/effect grids, native pixel contracts or artwork cache resolution.
Large-source coverage passed 77 exact checks across all three tools, 32/64-bit
native pixels, plain/fractional/affine/projective mappings and zooms 0.3/0.57/1,
including actual cold mouse contacts and source/history preservation.

`saved-stability-roundtrip.json` records nine saved-chapter workflows in a fresh
writable clone: held input, one commit, native source undo/redo, recovery save/
reopen, manual save/reopen and lossless native PNG export/reload. All passed,
including binding a different chapter and returning, in 241.94 seconds. The
immutable baseline copy, original target chapter and settings are audited
separately. Goal wall time is not evidence of a continuous eight-hour soak;
individual runtime durations and source manifests remain the evidence.

Current evidence includes `candidate-saved-cpu.json`,
`cold-candidate-unblocked-tablet-pressure-saved-cpu.json`,
`cold-wide64-production-candidate-unblocked-wide-saved-cpu.json`,
`cold-cooperative-production-candidate-cooperative-saved-cpu.json`,
`large-source-cold-feedback-current.xml`,
`large-source-and-bootstrap-final.xml`,
`contact-admission-configuration-final.xml`, `saved-stability-roundtrip.json`
and `input-final-audit.json`. Further presentation, actual-preset and final
source-stability checks are still in progress at this checkpoint.

### Continuous trailing input: full-editor diagnosis and repair

The user's later physical report confirmed that held strokes now appeared, but
trailed throughout drawing. A separate full `MainWindow` probe therefore keeps
the real hierarchy/settings listeners, selection handles, predictive cursor and
natural Qt backing store active. The private chapter has 106 layers and 173
objects. The copied current preset is `HIMOGザラ強弱`, size 30, opacity 0.8,
antialiasing 0 and spacing 0.25; Pencil is 12 and Eraser is 28. These controlled
CPU/offscreen checks do not measure physical hardware arrival or display latency.

The typed transient publication still requested a complete widget redraw for
every received raster input. Full-editor baseline frame means were 6.16–7.14 ms,
including about 4.7 ms of projection and 1.22 ms of selection geometry. Brush
thumbnail generation also continued during Pencil/Eraser input. At high packet
rates, display work consumed the same event loop that receives pen samples.

Positively owned, stable native raster changes now queue damaged horizontal
widget bands. Full horizontal coverage deliberately preserves Qt's fractional
image sampling and dashed-guide phase; smaller arbitrary rectangle clips failed
exact retained-backing comparisons. Source pixels, dependency invalidation,
pressure, timestamps and received order still update on every sample. Software
presentation starts no earlier than six milliseconds after the previous frame
start, without restarting an already queued timer. The idle-to-contact frame
remains immediate. Brush thumbnail work yields while any native painting owner
is active or has an unfinished input predecessor.

Stable selection handles retain the original QPainter primitive commands, with
a 512-command / 8-MiB geometry budget and direct drawing fallback. They do not
retain an image, paint device or native handle. An earlier presentation-image
prototype changed antialiased pixels and was removed. Native preparation and
artwork cache sampling contracts are unchanged. Pending labels, changed document
or interaction bounds, rotation, GPU presentation and unproven ownership retain
the existing complete redraw path.

`main-window-input-screenshot-final-guarded/report.json` records all three
tools at 128/240 received samples per second in the 1024×927, zoom-0.57 view.
All twelve held/released retained-backing comparisons matched a fresh complete
widget redraw exactly. Independent native artwork, source packet order, one
commit and exact undo/redo also passed. The warm measured streams requested zero
ordinary held scene jobs and zero complete widget repaint regions. Frame means
were 2.06–2.39 ms; callback means were 0.47–0.90 ms. Every two-second 240-Hz stream
accepted all 482 packets. Corrected helper metadata separates received callback
time, actual held-stream duration and intended dispatch lateness. Five streams
completed in 2.0016–2.0038 seconds; Pencil at 240 Hz took 2.0323 seconds, with a
26.66-ms final dispatch delay. Other final delays were 0.23–3.19 ms. Maximum
newest-delivered-input to paint-end times were 4.74–7.03 ms across the six cases.
These synthesized, in-process timings are not hardware latency; occasional
dispatch jitter remains. The earlier `final-timer-recorder` helper used a
one-packet-offset dispatch reference and is retained as intermediate evidence.

`test_native_input_pending_frame.py` adds six real Qt event cases across Pencil,
Eraser and Brush with 8/16-bit native originals. Every received sample changes
source pixels while the presentation timer remains pending; final endpoint,
pressure/axes ownership, the hidden standard cursor, single commit and native
undo are verified independently of pumping the display timer. The fresh broad
source audit and a retained full-editor soak remain in progress.

The final cursor audit also found a received/admitted ownership gap: an older
transaction could set `_drawing=False` after a newer held contact had entered
its deferred journal. Modifier/transform hover tests then selected a standard
cursor. The early painting cursor guard now checks the current tool's latest
unreleased receipt-time chapter, TileStore and source-model owner as well.
Five real Qt successor cases cover Raster Pencil/Eraser, mask Pencil/Eraser and
Brush, including release and changed source/chapter/store/tool exclusions.
They and their cursor/queued-presentation neighbors passed 56 checks.

`gizmo-repaint-production-dpr1.json` and `gizmo-repaint-production-dpr1.25.json`
record 37 current production checks at each display scale, with matching before/
after source manifests: 13 actual backing-store repaint cases, 11 selection
key/lifecycle/fallback cases and 13 command ownership/budget/state cases. Solo
mode participates in the key; enabling an active Cage/Texture modifier retires
selection retention even when that ribbon change does not mutate the model.

`full-window-input-soak-final-all24/checkpoint.json` records the post-cursor-build
full-editor smoke: all 24 tool/rate/view combinations, 29 gestures, 1,914
received and accepted native packets, 24 held native oracles and 48 exact
retained-backing checks. All source/order/history/undo checks passed, with zero
ordinary held jobs or input-target write rejections. It ran 66.3 seconds,
including complete file audits; this is not an eight-hour result. Peak retained
native planes were 33,259,200 bytes, selection geometry was 16,756 bytes /
35 commands, and all individual cache budgets held. Offscreen working-set and
peak-working-set maxima were about 775/921 MB during this short multi-view pass.

The isolated harness initially blocked generated thumbnail writes in its copied
settings folder. Windows temporary-file retries then stalled an idle thumbnail
completion, after the input checks had passed. The harness now permits only
derived thumbnail output in its own output folder while protecting original and
copied input files; real thumbnail computation, listeners and painting-time
yield behavior remain active. The diagnostic trace is retained and is not
evidence of a production drawing freeze.

The first frozen-source broad run retained 650 results: 649 passed and the
zoomed-out affine budget test failed its old expectation of ordinary held
capture. Its old click could lie outside the document, and its inherited clips
covered only a 128-pixel strip. The updated fixture draws inside a visible
missing native block with valid inherited clip geometry, then proves visibly
changed exact native pixels while held, bounded 64-MiB preparation, zero
ordinary held scene submissions/captures, preserved original 16-bit formats,
one commit and exact undo. This strengthened case passed without changing
production code. `input-final-first-source.xml` and its JSON companion retain
the initial failure; a fresh full qualification follows the corrected test.

The fresh final qualification passed all 650 tests across 30 modules, with zero
failures, errors or skips, in 85.197 seconds of XML test time. All 677 Python
source hashes matched before/after testing and the later preservation inventory.
`final-input-completion-audit.json` records the XML and manifest digests. All
13,825 private files / 357,107,152 bytes still match the original baseline.
Saved original chapter files also match; observed original changes are 4,422
autosave entries and settings during concurrent activity. The audit did not
alter, restore or attribute those changes.

The authorized continuous 28,800-second full-editor run was launched at
2026-10-10 13:54:24 UTC, PID 31892, under
`full-window-input-soak-20261010-135424-14f1490a/`. Its hidden process uses below
normal priority, retained stdout/stderr, a launch source/helper manifest and
minute atomic checkpoints. It cycles all 24 tool/rate/view combinations with
128 moves per contact and periodic native/backing comparisons, cold preparation,
source/order/history and budget checks. Copied inputs remain strict; changes to
the user's original project/settings are read-only observations. The launch
is not evidence of eight completed hours. `checkpoint.json` must reach
`completed` with at least 28,800 elapsed seconds before duration is qualified.
This shared-computer background run is stability evidence, not an isolated
physical pen or GPU latency benchmark.

The retained first-cycle checkpoint at 13:55:32 UTC is `running`, 60.69 seconds,
24 completed gestures / all 24 cases, 3,120 received and accepted native packets,
zero ordinary held submissions, zero retained-backing mismatches, restored native
source and no failure. `first-cycle-proof.json` retains that checkpoint digest.

That first long-run process later stopped on its own assertion at 123.78 seconds,
after 56 gestures, while checking small-fractional Pencil at 240 Hz (ordinal 55).
Received input, independent native artwork, commit/undo/redo and released backing
were exact, but held retained backing differed from a fresh full widget redraw.
The saved PNGs and `mismatch-presentation-analysis.json` classify 548 pixels:
464 outside the repainted band on the fractional document edge at x518 retained
the idle composition shade, and 84 minor artwork/selection blending differences.
The affected current paint band already matched. This is a whole-view idle-to-live
presentation transition that bounded damage failed to refresh, not lost native
samples. PID 31892 was confirmed terminal; it has not completed eight hours.
The failed run remains intact for reproduction and a once-per-contact full-frame
transition control is being tested before any replacement long run.

The unchanged-source baseline replay also failed at 122.8 seconds on ordinal 54,
small-fractional Pencil at 128 Hz. Before retained-backing, after retained-backing,
and after fresh-widget snapshots had identical native generation, prediction,
mosaic and selection-command state. Its prepress `mosaic=None` observation was
after a detached oracle render, which deliberately discards the owner's mosaic;
that observation alone does not identify the previous idle-widget presenter.
The outside-band mismatch, same-state fresh oracle, and following full-refresh
control are the correctness evidence. Both replay artifacts remain intact.

The artifact-only full-on-press control completed at 185.69 seconds with 85
gestures, all 24 cases, 11,050 accepted ordered packets, 48 held native oracles,
96 held/released retained-backing comparisons and five cold contacts. It passed
the previously failing third-cycle cases 54/55 and fourth-cycle cases 78/79.
Every latest warm gesture used exactly one full widget paint region, followed by
67–127 bounded natural frames. Native/source/history checks remained exact and
the copied project, copied settings and source hashes stayed unchanged.
Diagnostic tracing and short streams make its dispatch timings unsuitable as
an isolated physical input-latency measurement.

The production repair queues that one complete software-widget adoption frame
after the first admitted Pencil/Eraser dab or Brush begin sample. Deferred
contacts receive it when their own first dab is applied, since their received
press repaint may already have completed while a predecessor owned the source.
Later packets retain the existing bounded-row damage and non-restarting display
timer. Packet admission, coordinates, pressure, axes, source grids, native
precision, history and exact cache contracts are unchanged. A new real retained-
backing regression explicitly declines the optional idle mosaic to exercise the
existing native fallback before live mosaic adoption; it failed all three tools
on unchanged production with 82 outside-band fractional edge pixels differing
by one display value. Actual-source repeated-view qualification is in progress;
no replacement eight-hour completion is claimed here.

The actual-source replay's direct owned-widget trace subsequently identified the
previously ambiguous idle path. At the failing small-fractional view the idle
document batch contains 35 tiles, exceeding the optional mosaic's 32-tile cap;
its owned widget paint retains no mosaic. The first held native-contact batch
contains 25 tiles and builds a 3,210,240-byte mosaic. That real presenter change
explains why untouched fractional edge pixels require a complete adoption
repaint. This trace observes actual widget draws, rather than inferring the idle
path from a detached oracle's cache clearing. The production replay passed
ordinal 54 with one initial full frame and exact held/released backing.

Current-source repeated-view qualification completed at 185.21 seconds with 81
gestures, all 24 cases, 10,530 accepted ordered samples, 55 held native oracles
and 110 held/released backing comparisons. There were no backing failures or
ordinary held submissions; the exact source, project and copied settings stayed
unchanged. Native-plane, GUI-patch, mosaic and command payload peaks remained
52,728,000 / 6,760,000 / 3,210,240 / 16,756 bytes within their existing limits.
Concurrent regression load makes this replay correctness evidence rather than
an isolated timing benchmark.

The fresh 32-module qualification passed all 677 tests with zero errors, failures
or skips in 114.10 seconds. Every tracked Python source hash stayed unchanged
during the run and at the follow-up inventory. The adoption and partial-repaint
subset separately passed 20/20 at DPR 1 and 20/20 at DPR 1.25 with stable
production hashes. `contact-adoption-proof.json` links the qualified manifest,
XML digest, density-specific proofs and production replay; it explicitly leaves
the eight-hour completion false. A separate current-source profile is checking
steady presentation costs and optional faster input rates before relaunch.

### Windows upgrade and ten-hour continuation (2026-10-10)

The user reported updating to Windows 11, authorized native computer use, and
requested ten hours of further input/workflow optimization using a Pocket
Boyfriend copy. The recorded continuation window is 2026-10-10 15:00:07 UTC
through 2026-10-11 01:00:07 UTC. Setup, wall-clock work, completed test duration,
and failed runs are recorded separately; this entry does not certify ten hours.
No driver, operating-system or security settings were changed.

Current-machine culling evidence is retained in
`current-windows-culling-qualification.json`. A process-local control disables
only the owned software paint-region optimization, using the same renderer and
immutable settings. Across 44 gestures the qualification preserves 14,872
ordered native samples and 88 held/released retained-backing comparisons.
The optimization reduces actual tile calls from 49 to about 7.12 per frame
without changing complete-batch eligibility, source sampling or native grids.
Current-machine paired mean paint times improve by approximately 13–20% across
the six Pencil/Eraser/Brush 128/240 Hz cases. The warm ABBA run contains a
29.6 ms Brush 128 Hz outlier and four intervals over 16.7 ms; these remain in
the evidence. An isolated Brush repeat improves 2.267 to 2.027 ms and has no
intervals over 16.7 ms. This is application/software presentation evidence,
not physical device or GPU scan-out latency. Earlier measurements straddle
the user's Windows upgrade and are retained as history, not causal OS proof.

`current-windows-pixel-observer-comparison.json` pairs identical settings and
input with the strict transient pixel-notification optimization. Pixel-only
changes to the single selected raster no longer rebuild its property panel or
emit unchanged outliner roles while held. Dependency and mask processing,
mixed/ancestor selections, property changes, commit, undo and redo retain their
ordinary behavior. Mean Pencil/Eraser callback times change from
1.000/0.852/0.984/0.884 ms to 0.872/0.704/0.836/0.707 ms respectively for
Pencil 128/240 Hz and Eraser 128/240 Hz, approximately 13–20% faster.
Brush callbacks are mostly neutral. Native samples, backing and history checks
remain exact; variable frame tails prevent a claim that all physical lag is gone.

Fresh current-source qualifications completed with exit code zero:
`contact-adoption-qualified-20261010-151516` passes 755 tests across 34 modules,
and `core-workflow-qualified-20261010-151516` passes 316 tests across 13 modules.
There are no errors, failures, skips or Python source changes during either
run, and the follow-up source inventory is unchanged. Their XML SHA-256 values
are respectively
`019e396b2f8a4982d21a4ff9447931785da7b0081b2186b4d50fcfb433b408ec` and
`087f57d20d0266b41f23df856114e97a2796840a6d9dd888f655ba2d00fa21e6`.
The owned-region and focused-observer subset also passes 78 tests at each of
DPR 1 and DPR 1.25, including retained physical backing and RGBA64 precision.
These synthesized-input and native-output checks are separate from visible QA.

The first private visible launch at 15:21:36 UTC selected `GpuCanvasWidget`
automatically, rather than the software canvas used by the offscreen harness.
It failed during telemetry initialization because the artifact helper directly
read an uninitialized `_raster_contact_active` field. The failure and copy are
retained in `visible-private-editor-20261010-150741-7709d946`; it did not exercise
drawing. The helper is being repaired to observe the actual renderer truthfully.
A bounded controller startup also failed before launching a test segment:
Windows' virtual-environment `python.exe` redirects to a different interpreter
PID. A non-Qt probe confirms this; actual interpreter identity must be recorded
separately from the retained launcher process before long-run qualification.
Neither setup failure modifies production code or the original project.
### Native desktop workflow checks after the Windows upgrade

On 2026-10-10 the private visible editor used the normal automatic renderer:
`GpuCanvasWidget`, NVIDIA GeForce RTX 5060, OpenGL 3.3.0, NVIDIA driver 617.42.
The copied project was `visible-private-editor-20261010-152758-c42ff54e/Pocket-Boyfriend-QA`.
The original project was not used as an editing target. The visible launcher
published the actual interpreter PID 8228 and its process creation identity,
separately from the venv redirector PID 24264.

Native UI actions on the selected Chapter 1 raster covered Pencil, Eraser,
Undo/Redo of the eraser gap, the saved textured Brush, transform movement,
free corner editing and their Undo operations. Adding the normal Blur modifier
produced strength 8; changing it to 31 and Undo returned it to 8. A further
Pencil stroke with Blur active settled correctly. Manual Save reported success,
removed the dirty tab marker, and reopening the same private folder displayed
the saved lines, eraser gap, Brush marks and Blur strength 8. Reopening starts
with no active selection, as in the ordinary application.

The short UI drags generated only three mouse packets each and lasted
8–32 ms. Native telemetry nevertheless observed one held Eraser frame and
two held Brush frames with `BlankCursor`, no remaining native input queue,
and no contact error on release. Their first held frames appeared 8.0013 ms
and 5.9625 ms after contact entry respectively. A held Pencil frame with Blur
appeared at 8.4047 ms; filtered feedback remained pending immediately after
that short contact. These are Qt frame and input measurements, not hardware
USB latency or display scanout, and do not establish sustained-stroke speed.

The separate current-source paced replay completed all 24 Pencil/Eraser/Brush
pen/mouse cases at 128/240/500/1000 packets per second, with 22,464 packets
accepted in exact order, 48 held/released retained backing checks, one commit
per contact, and successful native source/Undo/Redo restoration. The report is
`paced-input-qualified-20261010-152624/paced-input.json`, SHA-256
`03abf737083971b995bc4d356d706c1f0bb90847b1be1f16d373015de4e3ae01`.
It uses the software/offscreen full MainWindow, not the native GPU desktop.

This replay retained a significant startup outlier: the 1000 Hz pen Pencil
press reached the receiver 268.283 ms after its planned time, including
121.729 ms late posting and 146.554 ms in the posted event queue. Its worst
move queue delay was 243.806 ms; after the first second, mean queue delay was
8.144 ms and maximum 19.915 ms. Prepress CPU frames, GC and background work
were not recorded, so this evidence does not identify their cause. The native
GPU sustained replay must measure these separately before any further fix.

The quiet controller ownership repair also passed a non-Qt subprocess proof:
`owned-interpreter-probe-20261010-152923-a5517b5d/report.json`, SHA-256
`9c0514035568d7dd6e099d001d8234a91e5a7de874bc51aadb2623a561f1b42d`.
Both clean exit and guarded termination observed the actual interpreter and
redirector exit codes separately. This proves process ownership only; it is
not a completed drawing stability segment or ten-hour run.

The subsequent source-only disk audit fully decoded the saved target PNGs and
the exported chapter PNG. The export is 1080 × 82000 RGBA8, SHA-256
`5c3430fbda99e05191293c6a4b10dd05449f735772f1626b73844a9114cb64ea`.
The target's six saved tiles retain 256 × 256 native dimensions, exact index
metadata and alpha bounds. Transform fields are unchanged after Undo. Only
the test raster's pixel/interaction bounds and the deliberately added Blur
changed in the saved model. This is integrity and persistence evidence;
export pixels were not compared to an independent cold-render export.
The 18,218 original files and original settings matched the retained snapshot
at the final scan, with no additions, removals or observed read races. That is
final-state hash evidence, not an assertion about unobserved transient writes.
The audit summary is `visible-copy-disk-validation-summary-20261010.json`,
SHA-256 `fdab6f9497ac32425eb50875b79195076d3ce6ebb431a82fa11e3833734246f1`.

The first repaired-process-controller drawing segment completed 187.8001 s,
72 gestures and all 24 cases. Both the actual interpreter and redirector exited
zero; production, helper and immutable input hashes stayed stable. Its retained
controller ledger rejected the terminal report because the validator compared
the runner's integer `physical_backing_failures: 0` with an empty list. This was
a validator schema error, not an application backing-image failure. The original
failed ledger remains at `ten-hour-input-controller-20261010-154008-adb275e7`.

After that process finished, the validator was corrected to require exactly an
integer zero and nonempty retained backing-oracle records whose `equal` and
`retained_backing` fields are both true. The source-only proof accepts that
retained terminal report and rejects missing counts, nonzero counts, list/bool
counts, missing oracle records, mismatched pixels and unretained images.
`controller-backing-schema-proof-20261010.json` records the result and original
terminal/checkpoint hashes. The controller hash at this repair is
`5acd854b5d11003ea43ac80f28137b8df38c22c24e26a2d53fd0c0a580bcf1fd`.
This separate proof does not rewrite the old controller ledger or count it as a
new clean controller launch. A fresh bounded launch must verify the repair.

The subsequent validator hardening requires the complete 24-case table, positive
per-case packet/gesture/oracle counts, matching Qt/native counts, aggregate
consistency, zero ordinary held submissions and the original five explicit
memory ceilings. `controller-completed-schema-proof-20261010.json` accepts the
retained good report and rejects 110 malformed, missing, inconsistent or
over-budget reports, crediting none of their duration. The current controller
SHA-256 is `f439019fc6bcbcf2ad98b4ffc439a1271027435aeb5328ea5464e2b854c6db6c`.

### Native sustained calibration and shutdown failure

The first normal-priority Windows/GPU sustained calibration used the fresh
`visible-private-editor-20261010-154243-0f49f135` copy. All 258 Pencil pen packets
at 128 Hz reached the ordinary Qt and native handlers in order. The 196 held
GPU frames, retained-versus-fresh GPU framebuffer checks, independent native
artwork checks and one-commit Undo/Redo source/model restoration passed inside
the application. Mean input callback time was 1.0931 ms (maximum 7.1674 ms),
and mean GPU CPU frame time was 4.5418 ms (maximum 5.7151 ms). The native input
gate never queued a sample, and ordinary scene submissions remained zero while
held. These observations do not establish end-to-end hardware pen latency.

The same run had a 118.1569 ms maximum posted-event delivery delay and two
approximately 147.8 ms frame intervals. About 143 ms of each interval fell
outside the measured paint scope; callbacks inside each interval totalled only
14–15 ms. This does not establish the cause of the gaps. GPU upload and state
scopes also do not account for them, so the next measurements must include
event-loop and compositor boundaries before attributing the stalls.

The child wrote a completed in-process report but both retained OS process
handles subsequently reported access violation `0xC0000005`. The parent ledger
correctly rejected qualification. `native-terminal.json` SHA-256 is
`fc49141009c0d1b3db46c1083bc806d5b4e41b20fe6bcba34af50ad5603556d7`.
No duration or Blur prerequisite is credited to this failed run. Its wrapper
had called `QApplication.exit()` instead of the normal `MainWindow.closeEvent`
shutdown path; that difference is a diagnostic candidate, not a proven cause.
The next artifact wrapper requests ordinary window close after validation,
leaves any private-copy unsaved prompt for observed GUI Discard, and retains
an all-thread fatal trace file through interpreter teardown. Its SHA-256 is
`584357c51b88fed01da831493198aec575a7bde88241d3796393200ec8448ea7`.

The fresh rerun at `visible-private-editor-20261010-155859-bad5fb38` completed
the same 258-packet Pencil contact, all live native/backing/history checks and
all final guards. Root observed the private-copy Unsaved changes dialog and
clicked its displayed Discard button after timing ended. Ordinary shutdown
completed and both retained process handles reported exit zero. Its terminal
SHA-256 is `9cee28113092290c31211a3ca7c2c8a7e01fe706e26f5e4c1c8ef7daacf0d939`.
This is one completed calibration, not the six-tool/rate baseline. Delivery
still had a 123.4498 ms maximum; callbacks averaged 1.0152 ms and the 206 held
GPU frames averaged 4.6153 ms. Normal shutdown resolved this probe's exit
failure; one rerun does not isolate the earlier native crash's precise cause.

### Pen barrel-button contact continuity

Qt 6.11.1's [Windows pointer implementation](https://raw.githubusercontent.com/qt/qtbase/v6.11.1/src/plugins/platforms/windows/qwindowspointerhandler.cpp)
replaces LeftButton with RightButton for an in-contact barrel button. Its
[tablet dispatcher](https://raw.githubusercontent.com/qt/qtbase/v6.11.1/src/gui/kernel/qguiapplication.cpp)
can therefore generate TabletPress/TabletRelease during the same physical
contact. Canvas previously treated each as a new contact or lift, prematurely
finishing Brush or queuing a Raster successor. This source behavior also
exists in the October 3 baseline; it is not proof of the recent lag's cause.

The fix routes positive-pressure Press/Release packets with remaining buttons
through the ordinary move branch only when a pen/tool contact already owns
input and navigation is inactive. Its existing exact packet/time/axis handling
and cursor footer stay intact. NoButton and zero-pressure releases retain the
existing endpoint and history policy. Twelve warm/cold Pencil/Eraser/Brush
regressions all failed before the fix. The focused candidate run passed
146 tests, including 27 button/hover/lift cases and the existing owner, cursor,
release-only endpoint, pressure and Undo/Redo tests, in 9.08 s. XML reports are
`tablet-button-baseline.xml` and `tablet-button-candidate.xml`. Canvas SHA-256
was stable throughout that run:
`f4eac3e63ad7f753f03e89c0580432080c0f080e85c170c5619eb54c14d69c98`.

### Fresh bounded stability and six-case native GPU qualification

The hardened-controller fresh run
`ten-hour-input-controller-20261010-160403-76f8d423/segment-001-a02a65e8`
qualified 184.9412 seconds, all 24 cases, 87 gestures and 11,310 exact ordered
Qt/native packets. It retained 48 held native-art checks, six cold gestures,
zero ordinary held submissions and zero physical backing failures. Every
fixed memory ceiling passed. Both creation-checked segment handles exited
zero; production/helper hashes and immutable input manifests matched. The
terminal ledger SHA-256 is
`a62181d5e85a591d725aaede95bea0f0e09dcf75dfb24bce660c3975a2813b35`.
This certifies a synthetic offscreen MainWindow segment, not hardware latency,
native GPU timing, modifier coverage or completion of the ten-hour window.

The next fresh native Windows/GPU copy,
`visible-private-editor-20261010-160340-e76a8ac8`, completed all six plain pen
Pencil/Eraser/Brush cases at 128 and 240 Hz, each held for two seconds. All
2,220 packets reached Qt and native handlers in exact order. Held source
pixels caught up before release; retained/fresh GPU frames, independent native
artwork, one commit per contact and complete model/source Undo/Redo restoration
all passed. Every gate's maximum queued input and ordinary held submissions
were zero. Both actual interpreter 17392 and redirector 7532 exited zero after
ordinary close and root's observed private-copy Discard. Terminal SHA-256:
`cf252129f2e11ca6e9c571a3863b3291f325dac9c530fe0c54d99223894e2f92`.

| Tool | Rate | Mean callback ms | Mean GPU CPU frame ms | Maximum post-to-callback ms |
| --- | ---: | ---: | ---: | ---: |
| Pencil | 128 | 0.923 | 4.196 | 49.839 |
| Pencil | 240 | 0.600 | 2.895 | 33.798 |
| Eraser | 128 | 0.813 | 3.384 | 50.327 |
| Eraser | 240 | 0.675 | 3.033 | 38.903 |
| Brush | 128 | 0.529 | 2.505 | 37.076 |
| Brush | 240 | 0.454 | 2.268 | 43.210 |

This run is qualified stability/correctness evidence with remaining delivery
tails. It is not a matched before/after speed claim for the tablet-button fix.
The optional `--trace-loop` observer records bounded numeric dispatcher,
composition, owned cache-timer and outer QOpenGLWidget paint boundaries,
decoding only after application return. Wall and thread-CPU clocks are separate;
Windows accounting can quantize short CPU spans. Its syntax and non-Qt overflow
proof passed, and its buffers did not overflow in this run. The artifact driver
SHA-256 is `6ee65b6fb0033d471458af6657018fff4bd2e3250c39bea714c5e2e20f3312f1`.

### Remaining input-delivery tails and Blur crash discrimination

Independent analysis of the six-case trace retained 70,859 chronological rows
without overflow. Dispatcher awake signals without a preceding block are normal
on Windows; the analyzer pairs an actual block with its next awake rather than
requiring every awake to have blocked. Strict cache/outer-paint and associated
composition/swap pairs still reject malformed held traces. Seventeen malformed
trace schemas were rejected in a separate source-only proof. Analyzer SHA-256:
`7feb237ed2635bffefe0361010e91291ead2c96e88287fa85d849b63e192f2bb`.

The largest Pencil128 frame interval was 64.326 ms, including 59.520 ms outside
paintGL. Input callbacks overlapped 5.862 ms of that interval, cache ticks
0.055 ms, outer-paint boundaries 0.196 ms, actual block/awake spans 0.788 ms
and composition/swap 0.150 ms. These categories can nest and must not be added
as independent CPU time. Eraser128 and Brush240 had similar unexplained gaps.
Cache tick maxima were below 0.182 ms and composition/swap below 1.470 ms in
this run. Newest received sample to frame completion p95 was 4.13–6.23 ms.
This localizes the measured tail before admission without establishing an OS,
compositor, clipboard, background-worker or physical-device cause. Detailed
overlap summary SHA-256:
`c7730c2ae6f8b6452f960b8ef7b180e21d69a5d100f792804aab89e0ed575064`.

Fresh automated Blur8 copy `visible-private-editor-20261010-161019-b2ed65fd`
failed with actual interpreter and redirector exit `0xC000041D`; it received
no passing credit. Its last ready checkpoint does not establish whether metadata
inspection, modifier installation or contact setup had begun. The diagnostic
started with `0x8001010D` and then a corrupted all-thread traceback followed by
an access violation. CPython 3.11.15's Windows faulthandler labels first-chance
exceptions before continuing Windows exception handling. Its all-thread dump
also traverses live thread frames. This makes diagnostic interference a candidate,
not a proven cause or a reason to dismiss the failure. Terminal SHA-256:
`200d00c596906be8157c54e3ba83856be1e2082e5f4e65596adc7c7e17a1795c`.

Ordinary launcher copy `visible-private-editor-20261010-161342-37c87af8`,
which has no extra faulthandler enabled, survived real Windows UI Add Blur8,
Pencil drawing, strength 49 then Undo8, Eraser drawing then Undo, the saved
Brush, manual Save and ordinary close. Closed telemetry retained GPU/auto,
Brush, revision8, dirtyfalse and no errors; its app return marker was zero.
This was short UI workflow evidence rather than paced held-input qualification
or an independently retained operating-system teardown proof. A fresh check
of every original project file and original settings matched the protected
manifest after this run.

Two new native Windows Qt cases for Raster+Blur8 and point→Blur8→point passed
in `native-raster-blur-fallback-bc42f703`. They retain the required exact regional
CPU fallback, compare independent reference pixels, present the result through
the native GPU, and preserve source/model bytes. They do not manufacture a
DeviceImage final for Raster+Blur or qualify the full visible MainWindow.
Test-file SHA-256:
`4e49610ca46a390504b6f39fff5d60906aea7926e8ba67bf76e10ef63fb0ff4e`.

The artifact driver now has explicit diagnostic `--fatal-current-thread`;
faulthandler stays enabled through teardown with `all_threads=False`, while
the default stays True. Bounded private setup journals record before/after
metadata, modifier and selection operations with no held-contact writes.
All native input, rendering, history, source guards and retained process-exit
requirements remain unchanged. Independent source review passed. Driver SHA-256:
`5386eaa31c91a478778647fa5d1bc18577588d08aec08e5e7b193d156fc8acf0`.

### Native high-rate input and ordinary Blur observation, October 10

Fresh native GPU copy `visible-private-editor-20261010-162334-eb5fc47a`
passed six mouse streams: Pencil, Eraser and the saved Brush at 500 and 1000 Hz.
All 9,012 Qt/native packets remained ordered, accepted and complete. The cursor
stayed blank during contact; input queues and ordinary held scene submissions
were zero. Held/released retained framebuffer comparisons, independent native
artwork, one history commit and full source/model/height Undo/Redo restoration
passed. Both actual interpreter 10224 and redirector 2220 exited zero after normal
close and the observed private-copy Discard. Terminal SHA-256:
`d366b3553c9db5b9acf644201131a21ee1885fd51ecd724a66a056ce70d52392`.

| Tool | Rate | Mean callback ms | Mean GPU CPU frame ms | Maximum post-to-callback ms |
| --- | ---: | ---: | ---: | ---: |
| Pencil | 500 | 0.819 | 4.441 | 25.191 |
| Pencil | 1000 | 0.474 | 2.953 | 36.066 |
| Eraser | 500 | 0.676 | 3.284 | 41.561 |
| Eraser | 1000 | 0.580 | 3.290 | 22.745 |
| Brush | 500 | 0.331 | 2.290 | 43.155 |
| Brush | 1000 | 0.294 | 2.205 | 40.635 |

The independent trace contained 26,367 rows without overflow or missing held
pairs. Post-to-receipt p95 was 6.302–12.368 ms; newest receipt to GPU CPU frame
completion p95 was 3.172–6.207 ms. The largest 52.76–63.00 ms frame intervals
were mostly outside the measured GPU/cache/composition scopes. A source-reading
command accidentally attempted to parse an existing JSON artifact as Python and
failed immediately; its precise UTC overlap with this run is unresolved. These
measurements qualify correctness and sustained input admission, but cannot serve
as an uncontaminated matched speed comparison or establish a cause for tails.
Analysis SHA-256:
`8b5e8333ce7fb99cdd710e134e74ee0bebcbe2b0cc0632bee846267ee95416e8`.
Observation-limit note SHA-256:
`ab881b6e46bc648972a3b16d7af06dd594471670916993659aca4792b6e6a471`.

Diagnostic Blur8 copy `visible-private-editor-20261010-161912-258ab293`
survived metadata, Add Blur8 and contact setup with current-thread faulthandler.
All 257 held Qt/native packets were accepted, including their pressure, axes,
time and native positions, with the blank cursor and no queued gate packets.
Of 210 held frames, 209 reported pending artwork and none had private filtered
coverage. The final ordinary preview became current 826.935 ms after the last
native sample while the pointer was still held. This demonstrates catch-up, not
continuous current rendering. The strict covered-contact driver could not
qualify Blur because production explicitly excludes it from that lane. The
trace then overflowed while waiting; no framebuffer/artwork oracles were run.
The actual interpreter and redirector both exited 1 because qualification failed;
the app returned zero through ordinary close. No passing credit is assigned.
The first-chance `0x8001010D` diagnostic remained intact without an access
violation, which narrows diagnostic investigation without proving the earlier
crash's cause. Terminal SHA-256:
`988f3fa5313b7ddffc04112e27d7ee52e050fb0fa1b5e450a8a2d24eca24159b`.

An explicit artifact-driver `--ordinary-blur-fallback` lane now observes the
identity of the actual ordinary preview tile passed to the GPU presentation
function, which still delegates exactly once. It requires at least two increasing
edited artwork revisions drawn before the last input post, followed by a current
preview in a completed held frame and its associated swap. The ongoing criterion
permits a newer input revision to be pending while an older edited revision is
drawn; it does not claim every input event was immediately presented. Final
framebuffer, independent native pixels, release and history checks are unchanged.
Attempted history evidence is retained even if ongoing updates fail. The default
covered-contact lane still requires actual coverage and zero held submissions.
Six malformed witnesses were rejected by the source-only proof, including stale
document/revision/image, incomplete frame, wrong contact and missing last sample.
Driver SHA-256:
`0ee795609e069ed6763e48903d0d33a718d6c888d53a335e26efd61caf1f65af`.
No native run of that new observation lane is claimed here.

### Completed quiet stability segment and source-owner repair, October 10

The private quiet controller segment
`ten-hour-input-controller-20261010-162655-1765c255/segment-001-bc5966a6`
finished with 1,841.325 qualified seconds and 797 gestures covering all 24 cases.
Source restoration passed, physical backing failures were zero, and the production,
helper and input manifests stayed unchanged. Actual interpreter 6636 and redirector
9952 both exited zero, verified through retained Windows process handles. This is
paced offscreen stability evidence, not physical-device or visible GPU latency.
Each gesture checks the authored object and chapter height; the full MainWindow
driver separately checks complete chapter model restoration. This segment does
not complete the requested ten-hour continuation.

A corrected actual filtered-snapshot ownership regression reproduced
`RuntimeError('OrderedDict mutated during iteration')` while the previous filtered
consumer touched shared resident-source LRUs. The baseline control passed and the
race failed before its contact could qualify. Baseline XML SHA-256:
`a51522b4f0d228be0dd032988673d100b3563bdddc082aeeda00bdfc7b7c1a46`.
ReadyOriginals now freezes the bounded decoded-image and resident-tile entry lists
before Qt aliases/version reads can yield. Existing source pins, identities and
versions still guard adoption. This changes neither native source pixels nor
sampling, cache keys or persistence. The race and neighboring original-source,
filtered-contact and visibility suites passed 120/120 in 82.956 seconds; diff
whitespace checks passed. Candidate evidence is
`source-owner-handoff-repair-candidate.xml`.

### Four-second ordinary Blur baseline and comparison limits

Fresh full GPU copy `visible-private-editor-20261010-165830-47c3bd4f`
accepted all 513 Pencil128 held packets with the blank cursor and zero queued
native packets. It presented 464 held GPU frames, but the actual ordinary-preview
witness recorded no increasing edited revisions while input was still arriving.
Final revision520/frame477 caught up while held. Its retained framebuffer matched
fresh frame478 byte-for-byte, with owned FBO and GL-error guards intact.

The original independent artwork oracle failed: it compared 35 native document
tiles presented at camera scale0.57 against the ordinary preview's single633×1024
output grid. The two final presentation layouts differ, so this failure alone
does not establish incorrect native source/effect pixels. It remains failed;
no release, one-commit or history qualification is claimed. The independent
continuous-progress observation is also failed and is not explained away by the
layout difference. Actual interpreter22200 and redirector27812 exited1 for the
qualification failure, while ordinary application close returned0 and all original
artwork/settings/private-write guards passed. Terminal SHA-256:
`68c4b87ba45710ba36b16c122e0d256f37e1de6239b8fdf9eebaf72ccb5f59a6`.
A separate opt-in same-request native-preview oracle is under review; the original
native-tile oracle and this failed evidence remain unchanged.

Four small historical Pencil/Eraser controls with selected Blur12 and ancestor
Blur8 independently reproduced completed immutable-preview cancellation when a
new native packet arrived. Source/model, early-commit and Undo/Redo guards passed;
ten candidate cases were explicitly skipped because the scheduling draft remained
unapplied. The 1.353-second baseline XML SHA-256 is
`3c2d4ee28f9f162c5bf6715ace2854f0d710208ad38383fdb1436269e879fd72`.
Thirteen separate standard-library contracts for the optional GUI-tail observer
also passed; that observer is not integrated or runtime-qualified.

### Fresh native baselines and strict preview diagnostics, October 10

Fresh GPU copy `visible-private-editor-20261010-170902-c9f66af0` qualified all
six plain Pencil/Eraser/Brush pen streams at 128/240 Hz, with 2,220 exact ordered
Qt/native packets and unchanged native artwork, retained framebuffer and history
guards. Actual interpreter16256 and redirector18208 both exited zero through
their retained creation-checked handles. Its report SHA-256 is
`dfe67e4be2614b082c73cdede16fa02733e1cce2393e04b3f59767cc0c309c5d`.
The independent GC chronology retained all 3,466 callbacks / 1,733 collection
pairs. None overlapped the 33 recorded long whole-frame intervals or 1,975 held
paintGL scopes. This does not account for general allocation/refcount work or
GIL contention; collector thread identity and CPU were not recorded. The earlier
argparse-only source-reading mistake has unresolved UTC overlap, so this baseline
does not support a clean-isolation or matched performance claim. Evidence is
`native-baseline-gc-full-frame-intervals-c9f66af0.json` and
`native-plain-baseline-c9f66af0-provenance-priorities.json`.

The optional eight-scope GUI callback observer was subsequently integrated with
default observation disabled. Fresh copy
`visible-private-editor-20261010-172921-328e21f7` passed all six plain streams and
their ordinary artwork/framebuffer/history/source guards with observation enabled.
Its 9,037 owned GUI spans had no overflow or unfinished spans; no foreign spans
were recorded. Both actual interpreter16532 and redirector18204 exited zero.
Report SHA-256:
`f388a88e3ebed12f146166b0704c1dac0da4cf40cf23c45849227360210e9ce0`.
Callback-tail analysis remains pending at this checkpoint. Inclusive/nested
scopes and ticks crossing into validation must not be added as independent input
work. Observer overhead is included; this is not a measured speed improvement.

The strict same-request native-preview oracle passed 19 private CPU/offscreen
contracts in `strict-preview-v2-unit-20261010-171846-55151053` (XML1.449 s).
Its diagnostic-only v3 passed 24 contracts in
`strict-preview-v3-unit-20261010-173216-31b938ae` (XML1.465 s). Both runners
exited zero with no errors, failures, skips or source changes. XML SHA-256 values
are respectively
`29ffa07f3baa71f308a3887ef8bb7fc355eface82c13d55fd11fb1e7b2ebe5a2` and
`fb66cb44584477d1a6b272cc3347042f3a020d9fcf8c4322ce789991f5d8ba4c`.
These contracts validate immutable source/model/environment ownership, exact
output-request metadata and canonical native CPU comparison. They do not
qualify the saved chapter's native GPU or physical pen behavior.

The v2 full native Blur8 Pencil128 four-second probe remains failed at
`visible-private-editor-20261010-171818-1df69408`. All 513 held packets were
admitted, and retained-versus-fresh GPU backing matched; the strict independent
same-request image still differed at 12 pixels. The ongoing witness recorded
zero increasing edited preview revisions during posting. Captured transient
prediction is a source-backed candidate for the image difference, not a proven
explanation of this saved-project failure or its lack of continuous updates.
V3 compares cold canonical LIVE images with captured prediction and with only
`_predictive=None`, keeping native source/effect density1 and the original
INTERACTIVE/provisional policy. Its separate authored nonlive reference stays
EXACT. Any diagnostic explanation leaves the original equality assertion failed;
the release/history and ongoing-progress guards remain unchanged. No completed
full native v3 Blur qualification is claimed at this checkpoint.

All sustained streams above use synthetic posted Qt input. Handler receipt,
native admission, CPU paint and Qt swap milestones do not establish physical
USB/tablet scheduling or display scan-out latency. These checkpoints neither
complete the requested ten-hour continuation nor certify that modifier drawing
is continuously responsive.

### Native repaint pacing and current preview ownership, October 10, 18:26 UTC

The completed ordinary contact-preview candidate now keeps the actual immutable
older document and request on a provisional result, with positive contact/view/
configuration ownership. It does not relabel that result current exact artwork
or record it on disk. Six old test assumptions failed identically against the
archived pre-candidate modules. The updated tests add current bootstrap ownership,
actual source-byte progress, no stale worker publication, and release convergence
checks; all private worker barriers release in unconditional `finally` blocks.
The targeted 21 checks passed in `native-reuse-v3-tests-20261010-181311-e8e0b00d`
(XML2.352s), and all 244 broader checks passed in
`completed-preview-candidate-tests-20261010-181324-462350b3` (XML270.857s). These
are offscreen correctness checks, not desktop input latency measurements.

Strict preview oracle V4 separates two exact identities: captured current LIVE
provisional presentation including prediction, and authored nonlive EXACT artwork
with only prediction removed. Source/model/color/request/native sampling guards
remain intact. All 32 unit checks passed in
`strict-preview-v4-unit-20261010-175410-e497db92`. The saved-chapter Blur8 probe
`visible-private-editor-20261010-175910-77f1ad0d` passed held/released native image,
retained framebuffer, source/history and full model-height checks but failed the
unchanged requirement for two increasing edited artwork revisions during posting.
Its 514 packets were admitted in order. The earlier reader-command incident
included a short unintended PySide import in the possible held interval; retain
this run as a literal functional failure, not a controlled timing comparison.

The corrected GUI scope observer on the qualified plain saved-chapter run
`visible-private-editor-20261010-175424-bfd4ea6e` recorded 13,404 owned GUI scopes
with no overflow, foreign scope, diagnostic fault or unfinished pair. The union
of measured nested scopes accounts for136.254ms of986.110ms outside `paintGL` in
28 held intervals lasting at least16.7ms. The remaining849.856ms is outside the
observed scopes; it does not identify Windows, GPU drivers, GIL or any specific
cause. Newest receipt-to-frame p95 was3.879–6.956ms across the six saved-tool
cases; worst post-to-receipt delay remained31.970–53.134ms.

The applied GPU candidate coalesces only full-widget warm contact repaints using
a non-sliding timer bounded by6ms. Packet receipt/native admission remain direct;
cold admission immediately promotes pending repaint work. Owner/view/context
changes retire the token, and mask-mode and resize entry now retire old pacing
before the original operation. Artwork resolution and exact cache keys are
unchanged. The original hidden native test failures remain preserved. The visible
diagnostic instead reads the actual owned completed framebuffer before any grab
or update, records legacy/raw formats, and compares exact same-attachment bytes
after a fresh completed frame and swap. Resize may legitimately create and paint
a new valid token; the old token must still retire.

All 21 actual Windows GPU pacing diagnostic cases passed in
`native-gpu-pacing-diagnostic-tests-20261010-1819/pacing` (XML6.109s,
SHA-256 `3c6aaca5a2e31f5bb1fcaa172427b0d7118939e38b26488960f1a10279fc65ba`).
The first neighboring run failed four stale reference-helper signatures. Adding
the existing `replace_background` argument exposed two further helper-lifetime
failures during the subsequent detached QImage wait. The GPU-only reference is
now scoped around its actual GPU comparison, with a strict `None` assertion,
and restored before release/settlement. Reference pixels and display tolerance
did not change. All 10 neighboring native checks passed in
`native-gpu-neighbors-reference-scope-20261010-1826` (XML3.082s,
SHA-256 `e8f23675739a8d983b9608f77a5082f93e3265e4f1117c9848c90e9d34cecb68`).
Both qualified runs have actual interpreter and redirector OS exit zero, Qt app
exit zero, no callback/cleanup/final guard errors and unchanged guarded sources.
Cold-source lifecycle and actual context reparent checks, then full saved-chapter
paired timing, remain required before claiming a responsiveness improvement.

### Paired saved-chapter input and held settlement, October 10, 18:52 UTC

The first 24-case experiment, `visible-private-editor-20261010-183238-59eec07f`,
passed 13 case-level input/source/history checks before the optional event-loop
recorder exhausted its fixed capacity. Both retained OS processes returned one;
the application returned zero, and the diagnostic overflow failed the final
guard. Preserve the complete failure. Its prefix is not a qualified comparison.

The fresh run `visible-private-editor-20261010-184245-a345810f` omitted that
optional recorder and completed all 24 ordinary saved Pencil/Eraser/Brush pen
cases: two seconds per contact, 128/240 Hz, and off/on/on/off within each group.
The actual interpreter and redirector both exited zero; the application exited
zero and all source/helper/artwork/settings/runtime/final guards passed. Terminal
SHA-256 is `f1422925666391cdfdb7915a81d55b1d3fcbd006a007edb7626e85e8b3670c5c`;
its bound report hash is
`399947833bf2eab76376a2f1973b4eafc6a158a0291e1cf03c2bae8ec5b54378`.

The strict post-exit reader verifies that terminal/report binding, 24 qualified
cases, equal per-group precontact source/model/view baselines, packet/FBO/history
guards, and actual frame/swap packet associations. Analysis artifact
`native-pacing-abba-analysis-184245-a345810f.json` has SHA-256
`70b407e9b0d8927b011031085aa239573ce36d5e6a3c0108916c81f3f94d0894`.
Pooled post-to-callback p95, in ms, was:

| Tool | Rate | Pacing off | Pacing on |
| --- | ---: | ---: | ---: |
| Pencil | 128 | 31.884 | 13.030 |
| Pencil | 240 | 4.463 | 12.824 |
| Eraser | 128 | 18.962 | 16.095 |
| Eraser | 240 | 5.144 | 11.920 |
| Brush | 128 | 11.701 | 5.811 |
| Brush | 240 | 5.355 | 6.367 |

Ongoing newest-authored-post-to-Qt-swap p95 increased from 4.433 to 6.466 ms for
Brush240, from 8.080 to 8.492 ms for Pencil240, and from 6.911 to 7.276 ms for
Eraser240. These are frame-weighted observations during posting, including repeat
presentation of the latest admitted packet. Qt swaps do not establish physical
scanout or USB/tablet latency. Two contacts per mode also do not establish universal
tails or a system scheduling cause. The mixed result does not justify accepting
the six-millisecond pacing candidate as a general responsiveness improvement.

The preserved lifecycle18 run failed its strict held `wait_current`; the default
real reparent run stopped on the same strict projection-pending assertion before
the other two context modes ran. A separate actual cold Pencil/mouse diagnostic,
`native-gpu-lifecycle-diagnostic-one-20261010-1845`, preserved that failure and
reported all settle predicates true except `frame_not_pending`. The actual native
gate was current, settled and unreleased, feedback covered the full current view,
the current token was valid, damage/timer were empty, and the latest completed
frame had swapped. Desired/current/projection revisions were all eight. Diagnostic
SHA-256 is `97ab2ed00b414d9073c8a64c7062a8b45da95b94d0d4a8e5188eda93b886a454`.
Both diagnostic OS exits were one, app exit zero, callbacks/cleanup/final guards
empty and sources unchanged. It establishes the incorrect held wait assumption,
not completed lifecycle/native pixel qualification. Idle/released exact checks
and independent source/native256 authored-view/FBO/history checks remain required.

## Native three-millisecond pacing trial and lifecycle failures, October 10, 19:08 UTC

The three-millisecond warm GPU presentation trial changes only the scheduling
constant in `canvas.py:1362`. Its exact previous source is archived in
`.artifacts/realtime-chapter1-20261009/gpu-contact-three-ms-trial-applied-20261010`;
the candidate canvas SHA is `edbf83bdab76dbfb87063eb1b323383599f5a8961682d942bece969edfc9aa7b`.
It remains temporary and is not an accepted general input optimization.

The fresh full-project paired run
`visible-private-editor-20261010-190152-340c0622` completed all 24 cases. Both
creation-verified retained interpreter handles exited zero, Qt returned zero,
and every source, helper, original artwork/settings, native packet, cursor,
pixel, model and history guard passed. Report SHA:
`c6ade970d71d7b67f8556bd05256306f12311576e7c70965c82df63bca8d4ac1`;
terminal SHA: `96d7eed13503c43d77d91a770e5ff591f8ab674971db4437f4a19ada05f7fa1e`.
The strict post-terminal analyzer output SHA is
`d8d58f9beec6fb7ccafc764938815e253b7c3e8713a98a83f72468535a631fb5`.

Two two-second contacts per mode produced these pooled p95 values in ms:

| Tool/rate | Posted input to callback off/on | Newest authored native post to ongoing Qt swap off/on |
| --- | --- | --- |
| Pencil 128 Hz | 18.781 / 8.583 | 8.868 / 7.506 |
| Pencil 240 Hz | 4.047 / 6.342 | 7.963 / 7.994 |
| Eraser 128 Hz | 10.572 / 11.430 | 9.097 / 10.557 |
| Eraser 240 Hz | 8.303 / 6.779 | 6.619 / 7.101 |
| Brush 128 Hz | 8.819 / 5.563 | 6.901 / 5.057 |
| Brush 240 Hz | 4.475 / 7.439 | 4.178 / 5.337 |

These measurements again show a mixed tradeoff, including slower Brush240
delivery and older frames. They are authored Qt input and Qt swap observations,
not physical pen, USB or display scanout measurements. The eligibility source
proof's completion UTC was not captured, so its possible overlap with the run
must remain disclosed until the retained posting timestamps establish a bound.

The independently reviewed lifecycle settlement suite ran natively at
`native-gpu-lifecycle-settlement-20261010-1859`: 8 passed and 10 failed, with
both OS exits one, normal Qt return zero, no callback/cleanup/final guard error,
and unchanged production sources. XML SHA:
`2190d6f51c5315322a50123f08bb13c716e4921401aa2f26a220b10af7b5d33a`.
Six Brush waits incorrectly require `document.contact_only`, which production
intentionally defines only for Raster Pencil/Eraser. Two empty Eraser waits
need more precise actual predicate evidence. Two chapter-growth assertions
failed after authored source equality; the asset fixture is not an explanation
because `_emit_raster_dirty` has no asset-type exclusion. Those failures remain
preserved and unqualified. A concurrent standard-library reader proof/generator
ran during this correctness suite; no isolated performance claim uses it.

The default genuine reparent diagnostic at
`native-gpu-reparent-diagnostic-20261010-1903` preserved its strict wait failure.
All actual transfer predicates passed except the exact-frame-pending flag:
the new host had a valid recreated context, a completed naturally swapped
frame, retired old token and current full native contact feedback. Both retained
OS exits were one. No framebuffer oracle ran after that wait, so this is
diagnostic evidence only; native context/pixel correctness is still unqualified.

### 19:36 UTC — actual Chapter 1 phase blocker and continued native qualification

The failed strict Blur run at `visible-private-editor-20261010-191311-779bdf8f`
observed 511 real eligibility calls while input was still posted. Every call
used the actual `('base', 'top')` phase tuple, live/contact-only document and
settled native gate, and returned no ordinary contact owner. The previous
predicate accepted only `(None,)`. During posting, 510 captures were created,
303 were partially advanced and none completed or submitted a worker. This is
a concrete phase-policy blocker plus capture starvation; it does not identify
GPU hardware or Windows drivers as the cause. The strict failed diagnostic is
`actual-blur-eligibility-failed-diagnostic.json`, SHA
`924fedd3da6a6ca3f2a83536b6e917a813c0bb891c49ab0f0789e051d9de7fa7`.

At 19:34:04 UTC the two independently reviewed predicates were applied exactly,
archived at `ordinary-contact-phase-support-applied-20261010-193404-df0a0163`.
Only `(None,)` and the canonical ordered `('base', 'top')` tuple are eligible.
The worker still renders one complete native-scale scene with `request.phase`
equal to `None`; its actual phase tuple remains part of ownership. Exact,
materialized, durable, other-consumer and reversed/unknown phases retain their
original exclusions. The 22 native phase cases are still unrun at this entry.

The zero-millisecond GPU scheduling trial completed 24 actual native ABBA
contacts in `visible-private-editor-20261010-192113-efa177e4`, with both retained
OS exits zero, all native pixel/source/history/cursor/FIFO guards passing and
unchanged protected files. The strict analysis SHA is
`df3f48b695a724a72afb576a7ec083e28d52a891bb65991c6a9e47f2d821d930`.
Results are mixed: Pencil/Brush 128 Hz callback and ongoing-frame delays improve,
Eraser 128 Hz callback delay regresses, and 240 Hz results are broadly similar
with several worse tails. This remains a trial, not a universal speed claim.
A 57 ms stdlib reparent proof ran after launch but before the application's
ready marker and before the measured contacts; that overlap is preserved.

The actual held-reparent run `native-gpu-reparent-held-settlement-20261010-1937`
proved real default context recreation, one destruction, old-token retirement,
and byte-identical first transferred retained/fresh frames plus an independent
native256 authored view. It then failed the second contact's retained/fresh
comparison (2229 changed bytes, maximum error 207). Its second wait can accept
the press frame before the queued move is painted; the failure is retained and
the three-branch lifecycle is unqualified. Both retained OS exits were one,
with clean callback/cleanup/source guards. A new full-current wait is being
reviewed; no pixel assertion is waived.

The 15-case native CPU/source capture prototype run
`ordinary-contact-capture-native-20261010-1937` failed before constructing the
candidate: its test setup observed desired revision 7 but actual revision 8
after an ordinary paint. All 15 failures, both terminal OS exits one, normal
Qt return zero and clean cleanup/source guards are preserved. The test must
wait for actual current positive ownership before selecting its immutable R0;
it must not fabricate ownership or relabel older source pixels as current.

### 19:56 UTC — native qualification and remaining capture starvation

The first-paint ordering repair refreshes the document after projection
configuration advances its revision. It changes no source pixels, input order,
cache key or sampling grid. The exact applied source is
`document_projection_features.py` SHA `fc07d58b3395eb8a5010553e2e879bd65c1193f71e83d0c1e13e0e5f77e07af2`,
archived at `initial-projection-revision-applied-20261010-193859-522daeb9`.
The phase-policy native suite subsequently passed all 22 cases, both retained
OS exits zero, normal Qt return zero, clean callback/cleanup/source guards:
`ordinary-contact-phase-native-20261010-1940`, XML SHA
`803a036a129a2b7cb2bd97531ff09bfd59c183d8f43a27b5f35559b1a11bc8a8`.

The bounded R0 capture prototype passed all 15 actual native Windows CPU/source
cases at `ordinary-contact-capture-native-20261010-1945`, XML SHA
`edfa1f6b067f26053d884e43b587d45b1fb9588949eecaf8aafd60fc26bdc81e`.
These include Pencil/Eraser, cold files, full selected maps, RGBA64/P3 source
precision, honest older revisions and exact source/model Undo/Redo/Undo. Both
retained OS exits and Qt return were zero, with clean final guards. Earlier
V3/V4 failures remain preserved: the canonical reference used the wrong render
policy, Eraser incorrectly expected frame growth, and a cold fixture baseline
read a stale resident image before replacing its PNG registration. V5 evicts
that stale setup image before registering and obtaining the actual native
decoder baseline. This is source correctness evidence, not GPU/main-window
responsiveness evidence. Production integration is still unapplied here.

The corrected full-current reparent wait passed all three actual native GPU
branches: default recreation, explicit retained sharing and explicit recreation.
Each checks both contacts against fresh FBO pixels and an independent canonical
native256 authored view, with exact source/history restoration and clean
creation-verified OS exits. Evidence:
`native-gpu-reparent-held-settlement-20261010-1943`, qualification SHA
`79aa08b2bcff100a080df70de7a51792fb87ec8699e5fb46852c8c2be3588266`.
The original failed second-contact bytes remain intact; no pixel guard changed.

A fresh independent Pocket Boyfriend copy passed Pencil/Eraser/Brush at 128 and
240 Hz on the real automatic Windows RTX 5060 renderer. Evidence:
`visible-private-editor-20261010-194443-09787e80`, report SHA
`bf4f78ef4cfc6a25b30f90602f37ea733e3b1f501b2a7210c4c1065be7c60d89`,
terminal SHA `7c1c4b84190be4d5f8d0b01e5ace8086121504b71c4e8431e0286ffabd167ff7`.
Both retained OS exits and Qt return were zero. Native frame/source/model/history,
FIFO, pressure, BlankCursor and protected artwork/settings guards passed. No
other test job ran during the held input interval. Authored Qt input does not
measure physical USB pen or display scanout latency.

The phase-only strict Blur retest still failed the required two increasing
artwork revisions before the last posted move:
`visible-private-editor-20261010-194844-a6fd12b1`, report SHA
`9fb6bb0e9947ddee5e079e05a4ab420a2224e468b00e366c32f6e213467d94a4`.
Both retained OS exits were one, Qt returned zero and final guards were clean.
All 707 real contact-key calls during posting now return positive ownership
for the genuine base/top tuple. Nevertheless, 455 ordinary captures were
created during posting and none completed; 244 were replaced after partial
slices, discarding 921.875 ms of observed thread CPU. The only worker submitted
4019.186 ms after the first post, after input posting had ended, and preview
acceptance occurred at 4739.526 ms. Positive phase eligibility alone therefore
does not repair capture starvation. The exact-source-policy V4 failed-run
analysis is `phase-only-blur-scene-analysis-20261010-1955.json`, SHA
`d22ac7959d25a32e23230a2d50922179c0d77430a42518954e252ecb819a3192`.
Observer spans include overhead; these numbers are diagnostics, not a speed gain.

Dedicated first-paint V2 qualification at `initial-projection-native-20261010-1953`
passed the two idle cases, but four held P/E cases blocked the GUI before its
ordinary source-admission timer could start the worker. Their first-paint
assertions were never reached. Both terminal OS exits were one, Qt returned
zero and cleanup/source guards were clean. A separate V3 pumps actual queued
GUI admission with widget updates disabled and no scene request before the
deliberate first paint; its native qualification remains pending.

Lifecycle V3 at `gpu-contact-lifecycle-native-20261010-1955` passed 14 of 18
native GPU cases, including Brush and empty Eraser settlement. Four chapter
growth fixtures failed before drawing because their `BoundGeometry.path` call
omitted its required document nodes argument. The literal failures and XML SHA
`952dcce03f2f703aaea82c79c3e4b9577ea81011803dc230e78011a2fcc0559d`
remain preserved. Both terminal OS exits were one, Qt returned zero and all
callback/cleanup/source guards were clean. No growth outcome is claimed yet.

### 20:14 UTC — private capture integration and remaining cleanup defects

The reviewed private capture integration was applied at 20:03:52 UTC. Exact
sources: controller `516ac1ee1b561faa751f01c7face252e0598109ec783ba410f58f3545cdb2c31`,
scheduler `8ba3e4f04779a58612dfef8df2290b2d10139ca6666cfbb7b2d72b2831ceb8b7`,
new capture helper `3c36f1257655c1292a3d98540679be104b84210541827031951ed31b2416e0e1`.
Archive: `ordinary-contact-capture-integration-applied-20261010-2004`.
Immutable older source revisions retain their actual revision and copy lease;
the normal native renderer, full admission estimate, cache keys and sampling
policy remain in use. A canceled worker returns before inspecting its lease.

Actual integration qualification at
`ordinary-contact-capture-integration-native-20261010-2004` passed the nine
original capture/progress/admission/retirement cases. Both new three-epoch
Pencil/Eraser cases passed their held source, canonical preview, history and
copy-credit checks, then failed their strict final weak-lease cleanup assertion.
This is 9 pass / 2 fail, both retained OS exits one, Qt return zero, clean
callback/cleanup/source guards; XML SHA
`dc662b16e9c899d1d485152bbcebac50d374236994142334829bc6394aeff62f`.
The suspected closed-scene ownership cycle is not yet established by runtime
evidence. No forced collection, manual credit release or assertion waiver is
an acceptable repair.

Dedicated first-paint V4 passed all six real native cases at
`initial-projection-native-20261010-2001`, XML SHA
`3cb5d969aaf20c490453864d342425dfa9969b4a4b6284a7ee8dba34d3ef356b`;
both retained OS exits and Qt return zero, all guards clean. The preserved V3
failures exposed the normal release-time interaction-frame recomputation.
V4 independently checks that only that selected frame changes at release,
every native pixel stays identical, and Redo restores the full committed model.

Lifecycle V4 passed 16 / 18 at `gpu-contact-lifecycle-native-20261010-2000`.
The remaining mouse/pen Pencil chapter-growth failures are Redo sparse-map
topology mismatches. A separate actual two-case diagnostic,
`native-pencil-growth-history-diagnostic-20261010-2005`, preserves those
original failures and records only missing key `(1, 9)`: committed and command
after-image are the same native 256x256 fully transparent tile, raw SHA
`8a39d2abd3999ab73c34db2476849cddf303ce389b35826850f9a700589b4a90`.
All shared native tiles and unrelated owners match exactly. Painted pixels
survive; the new blank-tile canonicalization repair remains unapplied here.
Diagnostic XML SHA `5e6687c47813e95e805971cc7616d90b5148619eb5dd7188d5f1079364ab7a90`.

The new independent Pocket Boyfriend six-case run passed at
`visible-private-editor-20261010-200614-1c1b4356`, report SHA
`eb643c907bb3730c6ad2fcad847cb7d9f46ce3dc1248fb6bba684a3a72eaea27`,
terminal SHA `76d08864dc55a359a363c76af1b85fd72c735549e5c494f0e1bf468fdf7f7b86`.
Both retained creation-matched OS exits and Qt return zero; protected artwork,
settings, source/helper, held native frame, cursor and history guards passed.
Other agent source-reader subprocesses overlapped this run. Two mistaken
target executions (chunks `863dda`, argparse exit 2, and `ab2a51`, SyntaxError
exit 1 before body) and other reads are acknowledged; their exact UTC intervals
are unavailable. This run qualifies correctness, not isolated timing.

### 20:18 UTC — actual Blur cancellation and closed-scene ownership evidence

The integrated capture path still fails strict ongoing Blur progress in the
fresh copy `visible-private-editor-20261010-201351-41f319c0`. Both retained OS
exits are one, normal Qt return zero and final source/helper/artwork/settings
guards clean. Report SHA
`f2d1486f4509be536006045afdc61861b06e934b5328af420b9577ffaef76303`,
terminal SHA `bb66ab64af9ed47b28c2b3b53f850c1d38a13c541dcfca868742cfbb14434dfd`.
The exact-policy V5 diagnostic reader preserves failure and qualifies only its
analysis: `private-capture-blur-scene-analysis-20261010-2017.json`, SHA
`44f8e41cc005d8a47c7e3138dc47e4962cf564e92eade9a4efca0094f69de770`.
During the actual four-second posting window it observes six scheduler submits,
four worker/admission entries, three publish calls, 134 cancel delegate calls
with an active demand, and zero preview acceptance. Token state and callers
are not recorded, so this alone does not establish cancellation causality.
Private inner capture create/slice work is explicitly unobserved.

The separate actual two-node ownership diagnostic
`ordinary-contact-capture-lease-diagnostic-native-20261010-2017` preserves both
original weak-object failures before diagnostic allocation. Both retained OS
exits are one, Qt return zero, source/callback/cleanup guards clean; XML SHA
`e5c470c8fae89691ebd13f7e6ec49d5c81c835883cae5fde7a263858f05de1ba`.
After real controller retirement, worker joining and ordinary polling, Pencil
copy markers 1/5/11 and Eraser markers 18/24 remain unreleased in actual
admission. Their matching weakly observed closed EvaluatedScene objects retain
the private snapshot and both InlineResults.owner and SceneRenderBounds.canvas
back-links. Running tickets, waiters and copy credits are empty; controller
capture/snapshot/preview-snapshot references are absent. This runtime evidence
supports normal close-time internal cycle retirement. No such source repair is
applied at this checkpoint; externally owned snapshots must retain their lease.

### 20:30 UTC — native history recovery and preserved new test failures

At 20:21:58 UTC, release-time Pencil canonicalization was applied in canvas
`75f35478808d3221ce9e3271bd943c41b19676536472de725b3907e50d2b35f4`.
Only newly allocated, originally absent tile keys have their alpha metadata
invalidated and are passed through the existing empty-tile pruning operation.
Preexisting source tiles and held-input work remain unchanged. The original
GPU lifecycle suite now passes all 18 literal cases at
`gpu-contact-lifecycle-native-20261010-2023`; both retained OS exits and Qt
return zero, source/callback/cleanup guards clean. XML SHA
`04cd4c73ba55d996810255fdda1964c54bb648495cb88f73805500bfb556cfe1`,
qualification SHA `c52137247df6e419d98993702bfab92d456f214c912dd305fc3698db3215ec6c`.
This includes the previously failing mouse and pen chapter-growth Redo cases.

Additional focused release fixtures V1 and V2 each pass eight and fail eight.
Their proposed geometry creates genuine new empty padding tiles but does not
produce the asserted optimistic cached alpha false positive before release.
Those original assertions and failures are retained; neither fixture qualifies
its claimed false-positive scenario. V2 output
`pencil-new-empty-release-native-20261010-2030`, XML SHA
`e69b251102d4641488f887e2441a8857e11a39cbcb6a97dfcc44f1eaad31ec0d`,
qualification SHA `a639fe94e7c4a257799ebb8c2d61053114a2ccc187f010094949c19512e7f18d`;
both OS exits one, Qt return zero, final guards clean.

The separate original-empty-source history baseline fails both real mouse/pen
Undo topology assertions at `original-empty-source-history-native-20261010-2022`.
This is distinct from newly allocated Pencil padding: the ordinary history
setter drops an existing transparent native source tile. XML SHA
`34ae7fd288deb944d9159d025147634989f5629a62e4557b9784ad9c3b93309c`,
qualification SHA `8b6c0be3c3725e6c1ddef42229c6f39009f9ee3ba7ca9ea8a33052b6fad314e3`;
both OS exits one, Qt return zero and final guards clean. Its separate source
repair remains unapplied at this checkpoint.

At 20:25:38 UTC, normal detached-renderer close was amended to retire its own
InlineResults.owner and SceneRenderBounds.canvas back-links, including when
the existing residency-pool clear throws. Scene SHA
`6df4c37e8e218f9d74f10276f2f78dede18ce863d0746b12b8572ea627d25056`.
At `closed-private-scene-native-20261010-2026`, all 11 original integration
cases now pass, including the two final weak-lease assertions. Four additional
external-snapshot/cache-backend tests fail: the first normal Pencil/Eraser
cases still observe a live weak scene after both known back-links are absent;
the following fault cases fail their initial-empty-admission guard because
failed-case tracebacks retain their predecessors. The remaining strong owner
has not been established. No forced collection, early lease release or
assertion waiver has been used. This is 11 pass / 4 fail, both OS exits one,
Qt return zero, final guards clean. XML SHA
`847c6eba66100bd38069eb2a96530b2a4b3c212e838689651b16eaeb71ae90d4`,
qualification SHA `24466c8c274d0ef1da7f1387d635c99494e27dc27baf3f5dbdd70535ff8a961c`.

### 20:46 UTC — actual cancellation evidence and original native history recovery

The ten original GPU rendering/navigation/overlay/gradient neighbors pass after
the Pencil release and detached-scene close changes, at
`native-gpu-neighbors-after-scene-close-20261010-2033`. Both retained OS exits
and Qt return zero; source, callback and cleanup guards pass. XML SHA
`cc147f1952104493d834d90bca63d7001d37c076edc86dd183c5ab6610549c62`,
qualification SHA `465f31bb541c9814b9b3a06b7ab4faac4865bd6866e77f61a03faa7ac9bfbcc9`.

The busy-contact baseline retains 12 pass / 10 fail at
`owned-busy-contact-native-baseline-20261010-2039`. Eight cold/resident cases
never reach their actual source-worker barrier, so do not establish their
claimed busy boundary. The other two failures do establish cancellation of
the real admitted render token when a selected native revision advances before
a fresh paint request. All twelve invalid-owner retirement cases pass.
Both retained OS exits are one, Qt return zero, and final source/callback/cleanup
guards pass. XML SHA `5a75a00beb2defed8852f0c4aa397c8db1cc8717d7a8dd7f178c3ba7b8f2c41a`,
qualification SHA `abe59c3147112dac160e91632741087ff8113362a523632f2b72411dde3f034b`.
The proposed preservation change remains unapplied.

The closed-scene V3 diagnostic preserves its original weak-reference failure
before allocating diagnostics, then observes actual tile-effect closures held
by TileNode and graph/cell owners. The source graph owns an evaluator whose
closure captures the same graph, retaining scene-capturing callbacks. This is
the next targeted lifecycle repair; no forced collection or early release is
used. Output `closed-scene-owner-diagnostic-native-20261010-2045`, XML SHA
`5a65ced0e53dd51391feb4669535604208e370e34390bf68096a56d333650617`,
qualification SHA `0ff609637a1f66feded5a199a95d84fb4557ac29ff2ed9266bd9ac098bd64182`;
both OS exits one, Qt return zero, final guards clean.

At 20:45:06 UTC the transparent-original history setter repair was applied
through its exact two-source guarded installer. TileStore SHA
`3134c2dadb0f8f6ba98e1dcaa5f9959bd484106c3e6bcc601a9ecd7a10cbe02d`,
commands SHA `c2d5ba46b00b8e9f13ab877a26c3d944891ffe1042546c2f04f9908321fe4d23`.
The previously failing real mouse/pen Undo/Redo tests both pass at
`original-empty-native-history-pointer-native-20261010-2047`, with both OS
exits and Qt return zero and every final guard passing. XML SHA
`b4edbbe21b526468076922f4ab53dd2f4a6950716e2ed41b4169363ae7be26cc`,
qualification SHA `314863f402d12e68222807f1802d730882aa4fc9fa956be47e5a2b55518ad506`.
The separate 26-format/history fixture retains 18 pass / 8 fail: all eight
RAW-spill rows reach finally but call a nonexistent SnapshotBacking.close.
Those failures are retained pending a new correctly bound cleanup fixture;
their assertions reaching cleanup is not treated as a qualified pass. XML SHA
`2dfc4560269605f948cfe048ea46e6b3c47a40f2cdd532433ad0c3e99912b089`,
qualification SHA `02bbef9193c2d8d3c40c2c13db6edda8e56499b4cfdea519cade8cfecac67e28`.

Optional worker profiling was added only to artifact QA helpers at 20:37:19 UTC:
driver SHA `1396d66b71b7f36146dc1b061cb788c5168bc6dfc363b203135031d43e307513`,
profiler SHA `e8243bbf586f237513a27b69ad5e4e9450cda326ed71be7e3f5e154d2118ec7d`.
Default behavior remains unchanged; profiling changes timing and cannot
qualify latency. A fresh current-production private copy is being prepared
for plain input qualification before the first owned-worker profile.

### 20:57 UTC — isolated plain input, native history28 and actual worker profile

After the repaired test-only backing cleanup, the unchanged 26 native format,
metadata, byte and RAW-spill assertions all pass, followed by both original
mouse/pen history cases. Output
`original-empty-native-history-native-20261010-2051-v2`, qualification SHA
`106f5d377e2d83bb07bff53c83f263eb1b07c22343767a5a00308243f0f1b850`,
XML26 SHA `6e486457cdba3ace57b58d8863293d90ff9aa0a00277509d46b5339e083d67b8`,
XML2 SHA `c2c696089c7833d48300de19b81d0970b2ec36ba18601145cf7ae2c14142d455`.
Both actual interpreters and redirectors exit zero; both Qt returns zero and
all source, native admission, callback and cleanup guards pass. V1's nonexistent
close-method failures remain retained.

Fresh copied Chapter 1 Rewritten passes all six plain Pencil/Eraser/Brush
128/240Hz synthetic native pen cases using the real GpuCanvasWidget and normal
automatic renderer selection. Every team member acknowledged terminal source
processes before launch and held a full tool/read/write freeze until root
observed the actual six-case close-ready marker at20:49:14 UTC. Root's first
postlaunch reader observes that marker after held work; Computer Use follows
only then. Output `visible-private-editor-20261010-204541-bbd4f9ad`, report SHA
`0885d340a7655b796cd71dc3eeb21544b834b3749873c23ae16e1355307c168d`,
terminal SHA `7e07dafd67ddaa304e7d3e00180175d15908e03154b291d99ca87845b1ac65c9`.
Actual22320 and redirector276 both retain creation-verified OS exit zero,
Qt return zero, and original artwork/settings/private authored-disk guards pass.
Input handler means range0.43–1.30ms, CPU paintGL means2.26–4.65ms and maximum
7.24ms, with no native gate queue. Post-to-callback means1.82–3.55ms still have
33–61ms maxima. These observer-instrumented synthetic Qt timing measurements
are not physical pen/USB or display latency, or a comparative speedup claim.

The first owned worker profile on another fresh copy completes its diagnostic
with four actual jobs, no unprofiled eligible jobs, no overflow/faults, and
ordinary restoration/join. The strict original ongoing-two-edited-revisions
assertion still fails; it is not waived. Output
`visible-private-editor-20261010-205017-38afc052`, report SHA
`992204b18d3ea2f7ea9b0073df965e30def1100b19bba947ece86e9222d07abb`,
terminal SHA `079cb171db32604e7b7b314666e50ea55c3b6389f29b64dd264c3da83d2d1702`.
Actual21936 and redirector29128 both exit one after normal Qt return zero;
every final guard passes. The profile shows blur_regions.resize_region and
NumPy array conversion as substantial call costs. First job wall1194ms,
threadCPU500ms; resize_region128 calls /80 recursive calls,462ms self and863ms
cumulative; NumPy astype1913 calls,196ms self. Second job wall1303ms,
threadCPU406ms; resize_region570ms self /1001ms cumulative. cProfile changes
timing, and nested costs must not be added. Native sampling/output contracts
remain unchanged; any proposed optimization needs separate correctness and
unprofiled performance verification.

The bounded cold prerequisite diagnostic confirms an actual admitted canvas
ticket of274,728,960 bytes against the268,435,456-byte budget, with1,310,720
bytes of native copy credit. Its LIVE Contact preview retains a selected
feedback_target even though the actual scheduler returns after preview and
does not prepare feedback for LIVE documents. The unused feedback reserve is
128MiB. The retained oversized render barrier prevents cold input admission;
this distinguishes the eight earlier fixture prerequisite failures from the
two demonstrated stale-token cancellation failures. Output
`owned-busy-contact-cold-diagnostic-native-20261010-2056`, qualification SHA
`3529f9f7fe17c1f878fd5686e24c3d283db2bbf078cb806b856e0629f4f27e9e`,
XML SHA `dca8d64bba26162b2ae9bfbb6b2cdfc13317564a20b372f3ea07596056c7934e`;
actual22080 and redirector12688 both exit one, Qt return zero, final guards
clean. A narrowly scoped unused-feedback reservation repair is being drafted;
the real native snapshot/workspace budget and positive copy credit must remain.

### 21:05 UTC — combined owned-preview repair passes native22

The reviewed V5 controller/capture pair was installed at21:04:27 UTC through
the guarded two-source installer, with exact final source/helper manifests and
no rollback, cleanup or unrelated-source errors. Positive busy ownership now
preserves the real admitted immutable preview while newer native stroke
revisions arrive; invalid ownership retains the original cancellation path.
For a positively bounded owned LIVE Contact demand with a real unreleased
positive RENDER_ADMISSION CopyLease, only the unused feedback target/requests
are cleared. Native snapshot/workspace estimates, copy credit, canonical
preview request, native sampling grids and output contracts remain unchanged.

Actual CPU-native Qt regression output
`owned-busy-contact-combined-native-20261010-2105` passes all22 cases, including
the two previously failing real-token stale-revision cases, eight cold/resident
admission cases and twelve invalid-owner retirement cases. No skip, xfail,
callback, cleanup or final-guard errors. Actual3188 and redirector11604 have
creation-verified retained handles and both normal OS exits zero; Qt returns
zero. This is native correctness evidence, not a GPU or physical-input latency
measurement. Qualification SHA
`552e9a4789161a059ae620c9c1f6afc611a2562b2b07f4443bd4559c5c0ae191`;
XML SHA `ea52251147339d35bfaa74157e5496d7c778ffd1743f2458a7317c6c66b37691`.

The isolated plain-six report also rules out measured garbage collection as
the cause of its held-stroke tails:1733 paired collections overlap none of
the six move-posting windows and none of77 late move-delivery intervals of
at least16.7ms. Actual held GC flags match the existing stroke suspension and
restoration. The observer lacks thread identity/CPU measurements; no GC-policy
change or broader causal claim follows from this diagnostic.

### 21:10 UTC — synchronous graph retirement and25 native checks

The diagnosed private-scene retention cycle is now repaired at its actual
owner. A nondeferred completed tile output clears its shared graph closure
cell in finally. Deferred continuations keep the same graph; no direct lease
release, cache clearing, source mutation, sampling change or GC workaround
was added. The guarded one-source installer applied the reviewed exact
tile_effects candidate at21:06:08 UTC with complete source guards.

`nondeferred-tile-graph-retirement-native-20261010-2107` passes all15 ordinary
capture/closed-scene ownership checks and all10 native generic/deferred Blur
and prefix-reuse neighbors. The four previously failing closed-scene checks
now pass their original weak-reference and positive-copy retirement oracles,
including fault cleanup. No skipped/xfail cases or callback, cleanup and final
guard errors. Each real interpreter and redirector has a creation-verified
retained handle and normal OS exit zero; both QApplication returns are zero.
Qualification SHA
`56891970bc9686d5b988e6898cb1ae89236239779e63cddb8ad9e4156fe059a7`;
capture XML SHA
`8fc5db581659c13fe61370912475d0b443ad9dd32bac820578a7a0f27be1fa73`;
neighbor XML SHA
`4f84bb7532c76b9c94a61223227d8d6ecd0d8b079d8b782ca78e28250bc100d3`.
These are native correctness/ownership checks; their duration is not an
editor or input performance measurement.

The separate private int64 Blur-resize scratch candidate remains unapplied.
Its44 frozen-original numerical comparisons pass on this machine, checking
exact output bytes, fetch grids, immutable/strided source pixels, legacy
rounding, odd dimensions, masked/scalar Blur and detached eviction. Timing
and full-editor qualification remain outstanding.

### 21:23 UTC — current full-chapter plain6 passes; Blur error retained

Fresh independent copy `visible-private-editor-20261010-210910-9f0b2cfb`
passes all six actual MainWindow pen contacts: saved Pencil/Eraser/Brush at
128 and240Hz. All three agents acknowledged a complete tool/process/read
freeze before launch; the first root artifact reader at21:13:03 UTC observes
six validated cases awaiting ordinary close. Source/native artwork, held and
released existing-GPU-attachment, one-commit and full model/history oracles
pass. The normal private Discard closes the editor; actual14500 and
redirector18048 both have retained creation-verified OS exit zero, normal
priority32, Qt return zero and final source/helper/protected-input guards.
Report SHA `3e946af28aa7ef8c8ce67feccb03fee511e0bd261f9a7abce27efe9d942a8dc3`;
terminal SHA `e38e5852f05a6a6d13fb09a40827cef0073de2f52df0e383ee4895ebd9d97e63`.

Measured CPU paintGL maxima are under8.04ms and pointer callback maxima
under11.34ms in this single synthetic, observer-instrumented run. The first
Pencil128 case still has nine post-to-callback intervals above16.7ms, maximum
63.95ms. The other five cases have zero such intervals, maxima7.68/12.47/
6.88/6.01/9.98ms. This is not an ABBA comparison or physical input/scan-out
measurement and does not establish a universal speedup or the cause of tails.

The subsequent isolated ordinary Blur4-second Pencil128 stream in copy
`visible-private-editor-20261010-211442-0b35ad4a` fails during held input with
the existing strict RuntimeError `Ordinary native scene/input reported an
error`. It is not counted as a successful ongoing-preview test. Its producer
retires before normal root-requested private close/Discard; actual21548 and
redirector13552 both have creation-verified OS exit one, Qt return zero,
normal priority and clean final guards. Report SHA
`1546f298829d2d873ecd90ca86efd544406c8c58849d2d652c35db54428ad837`;
terminal SHA `1b68f871a6fb30bd756f4892303fa663f0442fcd4a04f00a37bb24553845a63a`.
The current driver retains only the generic scene/input failure. A NEW
failure-only diagnostic driver is being prepared to retain underlying
controller/native-input causes before close, with success/oracle behavior
and installed helpers unchanged. No cause is inferred from the missing data.

### 21:34 UTC — failure diagnostic identifies an observer signature error

The isolated diagnostic copy `visible-private-editor-20261010-212220-d5147372`
fails under the same scene-traced Blur contact. Failure-only capture, before
producer retirement or Qt teardown, retains the controller and projection
message: `TypeError: SceneController._contact_preview_key_for() got an
unexpected keyword argument 'allow_busy'`. The native input gate has no error.
Independent source checks locate the rejection in the installed scene observer:
its replacement callable accepts only the original five positional arguments.
The production method accepts keyword-only `allow_busy=False`; the new owned
busy-contact check legitimately passes `allow_busy=True`. The wrapper's copied
qualname explains why the exception names the production method. Rejection
happens before the observer body, explaining the absent raised trace rows.

This diagnostic does not indicate a production rendering regression. A narrow
observer repair will forward keyword arguments unchanged through both owned
and foreign calls, then rerun the original strict test. The failed runs remain
failed; no error or ongoing-preview requirement is waived. Diagnostic report
SHA `194eb2aa932598dbdfe43d660c1e45ed124dc2aa585213026b4b93129c67f814`;
terminal SHA `24bf1d115bcd47adcdffc178573ed560ad40f47c9b3a23a4a89e440daec6f24a`.
Actual26124 and redirector15984 both terminate with creation-verified OS exit
one at normal priority32, Qt return zero and clean final guards after normal
private close/Discard. Original artwork/settings and authored-copy disk checks
pass. The diagnostic is a correctness reproduction, not a performance result.

### 21:46 UTC — observer repaired; real Blur progress failure remains

The observer's three-line keyword forwarding repair974f31 is installed using
the reviewed V3 one-target installer. Application report SHA
`26a5f3af682fe87ac947433d692797009cd10b24e071ffd01dab891da42d293f`
has no failure/final guard errors; all production/tests remain byte-identical.
The V2 installer first stopped on a duplicate archival O_EXCL write, before
target mutation. Its partial archive is retained; a full-main disposable
regression reproduces that failure and verifies V3's write-once archive.

Fresh full-chapter Blur contact `visible-private-editor-20261010-213426-b223fef1`
now reaches the original held/released artwork, existing GPU attachment,
independent native live-preview and model/history checks without a RuntimeError.
It still fails the unchanged requirement for two increasing edited artwork
revisions during posting: the observed list is empty. Both actual30032 and
redirector9392 have retained creation-verified OS exit one at normal priority32;
Qt returns zero and all source/helper/protected-input guards pass. Report SHA
`2685eeb4adf68369adf11ee4e07fd229038c5eeb082277a3dc95f914daeaf313`;
terminal SHA `db0ada1c8c4d4d85670b128efd3de79a912207b028b996e8f6e3d5a3d611114d`.
Strict V7 diagnostic analysis SHA
`a22a1bb487c30d404b5ba63c5fdfad4a3a50cef1e287eab46f0c561819b149b3`
explicitly keeps native_run_qualified=false and preserves the literal failure.

During4.004 seconds of posting, the trace records140 active-demand cancellations,
723 positive eligibility results with settled gates, four worker entries and
three publications; no preview is accepted until posting ends. An owner tag
alone cannot prove a ContactSceneSnapshot: ordinary fallback demands can carry
that tag while failing the private type/lease preservation checks. The private
constructor's refusal reason and actual retained aliases remain unobserved.
The next diagnostic captures the existing refusal instead of weakening bounds
or assuming positive keys prove private ownership. Saved metadata counts411
records/one selected tile/51 image objects fit their fixed caps, but cannot
establish live decoded bytes or auxiliary residency.

### 21:54 UTC —35 selected native stability checks qualified across three runs

The current production bytes pass22 existing modifier-dependency, sparse raster
transform/page warp, precision/spilled history, persistence and exact-output
checks in the core child of `native-core-workflow-20261010-2152`. Its retained
actual/redirector exits are both zero, Qt returns zero and the child is strictly
qualified with no callback/cleanup/source/admission errors. Core XML SHA
`92a51a312c6e7bdb80d6383ce1de99f4e773a3afc5b5adab35345b222efff452`.

The same V1 parent then correctly remained failed because its save-suite
validator incorrectly required CanvasLogic evidence from storage-only fixtures,
despite all12 unchanged save assertions passing. That failed overall
qualification8513dc remains retained. V2 changes only that suite's required
canvas-function tuple to empty; it does not change selected tests or any
native/process/source/cleanup guard. No canvas is claimed for storage-only tests.

The separate V2 save run `native-core-save-integrity-20261010-2153` now strictly
qualifies12/12 manual/autosave chapter/asset erase/clear/move cases; qualification
SHA `25d0967418b91bb0f5a75a3d3d7fc7ede4456e084a090e42ea852d3416c37a5a`;
XML SHA `e971d87b49b2921148b3d1d7f2179d2257dfd76ead9c483f33e64116accc7160`.
Separate `native-core-export-action-20261010-2154` qualifies the real nonblocking
MainWindow export action, active-capture source ownership and normal close;
actual constructor evidence is GpuCanvasWidget/auto on Windows. Qualification
SHA `60baefd995dc71eb84a591dfee60a0ec2d226ce90ec30467a10884795118b36c`;
XML SHA `068eb6099aee0229fafc341eb7fa4fad19e659bb1a487be2fb0700a3a8328da4`.
Both latter runs have actual/redirector retained OS exits zero, Qt zero and no
callback/cleanup/final source errors. These private correctness children use
BelowNormal priority16384, not the normal-priority timed editor streams. The
qualified22 core tests were not unnecessarily repeated. These are small-fixture
correctness checks, not the requested real copied-chapter workflow or an input
latency claim; the full-chapter live Blur failure remains unresolved.

### 22:02 UTC — real contact capture refusal is memory admission

The failure-only constructor diagnostic ran on independent copy
`visible-private-editor-20261010-213652-e10231a4`, with the original four-second
128Hz Pencil/Blur inputs and unchanged ongoing, native pixel, framebuffer and
history assertions. Its first root artifact read was at22:01:33UTC, after the
held producer had stopped. All three agents acknowledged full tool/process
isolation before launch. The run still fails the literal ongoing-preview
assertion; it is diagnostic evidence, not a successful realtime test.

The restored observer records233 constructor refusals with zero faults or
overflow. The first refusal is `nonblocking native capture admission declined`;
the already computed retained charge is262144 bytes, store_bytes0, against the
unchanged134217728-byte capture limit. This disproves decoded-alias retention
overflow as the cause of that first attempt. It does not yet name the outstanding
admission ticket or prove its lifetime. No cache cap or global admission budget
has been changed in response.

Report SHA `056cdf97458c9b5b5752749f383b59de7d0337f0f741e785fe6733660483dd1a`;
terminal SHA `b2287891f4b8f7b94aa0a20ac353fad6112bbd850fe0de1be77c7dfa7fd9258f`.
Actual interpreter15604 and redirector2356 both have creation-verified retained
OS exit one at normal priority32; the QApplication returns zero after ordinary
private-copy Discard/close. Final source/artwork/settings guards are clean.

### 22:10 UTC — bounded admission retry applied; eight estimate checks pass

The strict refusal reader preserves the failed ongoing qualification and all233
refusals (analysis SHA `f4f9f5c120ca67c9ea5ff61a6ea9cf7b95619680792f513cb9033728d4fb0520`).
Chronology SHA `13b1ce16f0d42780ee99a5b03b332f17ca39e6249ca661458f753f1db23eeff3`
places every refusal within an actual admitted canvas-worker scope. The first
R11 worker enters admission about57ms after the first post; the first R15 refusal
returns about62ms afterward, before any ordinary compiler capture row. Initial
private success followed by catch-up refusal is a control-flow inference; the
trace does not directly record snapshot class or the ticket's estimated bytes.

The reviewed controller now distinguishes the specific admission refusal from
other capture failures. With no current capture and fully validated existing
private work, it retains that work and retries after polling, before dispatch.
Repeated refusal starts the existing8ms timer and does not redispatch the same
older snapshot. Metadata/cap failures retain ordinary fallback. The scheduler
also excludes the128MiB feedback preparation charge for LIVE work, whose original
renderer never prepares that feedback; native source/workspace estimates remain.
The shared256MiB budget and all native ownership/precision/sampling guards remain.

Applied controller SHA `0cb3ea13287205f711c21dc3c3a0d823695b6dfb7a2a507f26be1fc27267886d`,
capture SHA `aa6ccc68e0d815c7e27f432360bf480cce6325991f0fdbeba563f58dbda9a673`,
scheduler SHA `61e561e86aa01bc3b5855e0d0ce4dcfba54c3fd9288b54de827f67cb85edccfa`.
The full703-file source/test/helper guard matches the preceding qualified build
with exactly these three changes (before1ba76c6a; after9699974c).

`live-feedback-estimate-native-20261010-2212` passes8/8 actual native correctness
checks, including real ticket/copy headroom, original pixel equivalence, unchanged
idle/record feedback charge and rejected LIVE feedback-only work. Actual25772
and redirector18832 have matching creation-verified OS exit zero, Qt zero,
BelowNormal priority16384 and no source/callback/cleanup errors. Qualification
SHA `dc0a9953b245ac5ad623ca9cd335c3fecca64ff34250147949629715a2f79ecd`;
XML SHA `2ba411720630dd92fde19004a48c93654409a4588ed4b58edd81509bc90b1b1a`.
These checks do not qualify the complex chapter's realtime Blur behavior.

The first oversized-controller test run remains failed: seven fixtures compare
an ImageSource object directly with bytes; the last initial-worker barrier times
out. Retaining failed fixture snapshots could explain that later admission
pressure but is not yet proven. Both retained OS exits are one, Qt zero and all
source/callback/cleanup guards are clean. Qualification SHA
`58e809618c07cea89a5c4e688813cfb32546241294c6ca5e7d4a949a21817191`;
XML SHA `91da98ca2f103b3ec171eeca4aaa0b7ad8ef2d5879ca38fbbcd5716df9a305c8`.
A new test revision will correct the source-byte assertion and discard only
test-owned references after worker shutdown. No production change is inferred
from these fixture failures.

### 22:31–22:49 UTC — admission retry passes the complex chapter and workflow checks

The admission-retry production triple recorded above now passes eight actual
native oversized-worker/refusal tests, six full-chapter plain Pencil/Eraser/Brush
streams at128/240Hz, and the strict default Raster60 + Blur8 Pencil stream.
The original input driver and observational wrapper remain unchanged. During
the four-second Blur stream, five increasing edited artwork revisions
`11,13,123,218,401` are drawn and swapped while input is still posting, with
zero active-demand cancellations. Current canonical LIVE/native source, the
prediction-free authored reference, retained versus fresh GPU framebuffer,
released pixels, full model and one-commit history checks all pass. Both actual
interpreter and redirector have creation-verified zero OS exits. The strict V8
analysis uses no failed-ongoing waiver.

Evidence: `owned-contact-admission-retry-native-20261010-2223-v3` qualification
SHA `f5be7caca08ef736d69499574cd181d503d6b1984ad25447c94dc4345bca4a5f`;
plain copy `visible-private-editor-20261010-221033-01f7d40f` report
`2d0234319f3c330834557f5513f4d17635e8d367e5b5989345790f83050e7ac9`;
Blur copy `visible-private-editor-20261010-221204-e877996c` report
`dad7a087d33bc95f43a5d4ffe8c243c4271804576786f68fb2247cc50632587f`,
terminal `3cf7478396ceab6c2734e99cffaf10bfe7b484749f412b9c8839806b52d7aaab`,
strict analysis `a56f9582514870c516d8795134716e70f08c83c73f4cafdd58a3b1f89c42623b`.
All files are under `.artifacts/realtime-chapter1-20261009`.

Filtered rendering still trails: six worker scopes overlapping posting average
649.7ms, maximum842ms. Nested admission spans are not additional work to sum.
Five held updates establish progress, not60Hz artwork. The diagnostic flag
`--ordinary-blur-fallback` observes the existing production lane; it does not
disable feedback. Production eligibility already excludes Blur. Posted native
Qt packets and frameSwapped do not measure physical tablet drivers or scanout.

One focused and four complete genuine cold-input cases also pass with exact
catch-up, one commit, unchanged sources and dual zero OS exits. Four-case
qualification SHA `53db012a293ff5cdc01ee04aaae331841f5c07af29f6cf4415eb0b0ecc11802d`.
They observe an actual616,597,504-byte exclusive scene ticket against the
unchanged268,435,456-byte budget. The cold2MiB source worker waits until that
ticket retires. These cases qualify eventual catch-up without another packet,
not immediate cold admission during the deliberately held large render.

The independent ordinary UI copy `visible-private-editor-20261010-222134-459b2939`
also completes Pencil/Eraser/Brush, modifier edits, translation, Free corner
warp, rotation and Undo/Redo checks, followed by Save, actual reopen and native
PNG export. Persisted chapter/index/complete target source bytes match the
post-save observation after reopen/export. Actual timestamp export
`Chaper 1 Rewritten-20261010-184718.png` passes all chunk CRCs, full bounded
zlib/scanline decoding and1080x82000 dimensions. This verifies export integrity,
not an independent cold-render comparison of every output pixel. The normal
editor and redirector close with zero OS exits; originals and the entire
original settings tree remain invariant. Parent SHA
`b0bf39ef7f3fae745eb8139f1fe3558b9da73174f702d45f7a45c14ed92e574f`;
post-save `8cfd047618fda79da02094709ef5eb050c499b844e7a8186b36d6314dbed1e7e`;
post-reopen `8c9e595764a05bee19546a7c4efcc1c7f833023e28cacea9b6409d8f36a3f1a5`;
post-export `3d8ec33c3be91630be410d8ab926897f80097c7b05f337a3a166ad8b00794b04`.

Raw failed V1/V2 retry fixtures and stale-checkpoint saved-stage attempts remain
preserved. V3 observes the actual root compiler separately from the independent
fresh canonical reference and polls the joined, closed scheduler before dropping
test ownership. No failed result or native oracle was rewritten.

### 22:52 UTC — exact native resize allocation improvement applied

`resize_region` now reuses a private int64 multiplication scratch array and
rounds/clips its own accumulators in place. Sample order, normalized22-bit
coefficients, separate horizontal/vertical byte rounding, legacy alpha
conversions, source fetch grids, cache semantics and borrowed source arrays are
unchanged. The44 frozen exact tests cover normal/legacy, strided/read-only and
odd/thin native grids, masked/scalar fields and detached cache eviction.

A fully isolated, unprofiled64-call ABBA experiment wins both pairs in all eight
case/algorithm groups: mean wall time falls27.3–34.6%, with separate measured
allocator peaks26.2–28.9% lower. Normal633x1024→986x1596 falls120.62→78.92ms;
legacy131.24→93.63ms. This is function-level evidence, not an editor latency
claim. Report SHA `ccaf894a8a2d94d70836a689e49dc98046f21a26067f695d64296d7a9ceaf451`;
independent analysis `0bae9df560aed12e6541fe66c39ed1b448a4a66c23c0aa20aa509acb32f53c5d`.

The root exact-byte application verifies703 source/test/helper files and changes
only `comic_editor/render/blur_regions.py`, from
`2567fa6ed45753ff93eb99343cec9cc5ad2dc7d53aae46fd4e9f1d76df071387` to
`06679d08ee84914f71a1a0bc1953ed2304ac5231847899a48e1c8be6a4d0c2f8`.
Application SHA `fc2926e70e4ace5d2ac2031af2caf3170335c89fcd67b11a5dda8785b9bd2a0d`.
Fresh native integration and full-chapter qualification of this additional
change are pending; the preceding chapter evidence belongs to the prior build.

### 23:12 UTC — resize change qualified in native integration and the chapter

The additional resize change passes37 original native integration cases:
17 regional Blur,11 interactive modifier/snapshot and9 GPU point-chain
reference cases. All actual Windows Qt processes and redirectors exit0;
there are no failures, skips, callback/cleanup errors or source changes.
Qualification SHA `acb41b5072621675c9886bdf6dd3d90609c502cb3953b883f5ff6618d1efea08`.
This is native correctness evidence, not an input timing comparison.

Fresh full-chapter plain input passes all12 four-second combinations of
Pencil/Eraser/saved Brush, pen/mouse and128/240Hz. All8,856 Qt/native packets,
held cursor, native artwork/FBO, one-commit history and complete model/source
Undo/Redo/Undo checks pass. Both creation-verified processes exit0 and all
source/helper/runtime guards pass. Report SHA
`b7019dace306775a989003add820170e022545dcabe00a659ada7a94ce311339`;
terminal SHA `151e415f40a73c7a443cfe4f7e53e832aec562d2bfac2414c427208d655f3c79`.
Callback maximum9.045ms and CPU paintGL maximum12.601ms are observed synthetic
metrics. Ten held delivery receipts exceed16.7ms, with maximum51.840ms.
Producer scheduling lateness is separate. One agent was interrupted rather
than providing a fresh freeze acknowledgement before this run; there is no
fully acknowledged timing-isolation or before/after speedup claim for plain12.

An independently prepared chapter then passes strict Blur8 Pencil and Eraser
four-second pen streams at128Hz, including independent same-request native
LIVE pixel, source/model and full history checks. During actual posting,
Pencil publishes six increasing edited revisions and Eraser seven; neither
stream cancels an active scene demand. The input gate queue remains empty.
All three agents acknowledged a full tool/file/process freeze for this run.
Observed overlapping worker mean wall is546.59ms for Pencil and498.40ms for
Eraser. These unpaired chapter observations still show substantial modifier
catch-up delay; they do not establish a comparative speedup or physical pen
latency. Nested worker/admitted scope times must not be summed.
Report SHA `0ad5b816c8d892d57197ea154f2f546194ff6b9af6a05649063f0d9f1415afc5`;
dual zero-exit terminal SHA
`0b85a5b2b073542d190836965f36043af41b2f73c70310dd461500dd9b8e2f42`;
unchanged strict V8 analysis SHA
`bf3b671df60dbcbe71a14d79d1e52313f64442435855557135e1b6c529e2ef69`.
Private inner captures remain unobserved; no root-capture count is used to
infer their absence. The opt-in fallback label does not disable production
feedback, and production already excludes Blur from covered feedback.

## 2026-10-10 23:30 UTC: guarded resize arithmetic and native qualification

An owned-worker diagnostic completed on the full private chapter with seven
profiled jobs and strict native LIVE/source/history checks. Both native
processes exited normally with zero status. The profile identifies resize
arithmetic as a substantial cost: 896 calls, 1,585ms aggregate self time.
Coefficient construction accounts for 1,792 calls and 235.9ms self time.
These are instrumented, inclusive call statistics, not a latency comparison;
nested times must not be added. Exported labels can represent multiple Python
code objects. The first reader rejected that ambiguity; its failure is retained.
The revised reader retains that limitation and counts distinct jobs per label.
Qualified diagnostic analysis SHA
`48dc6663b44b1b37956e28d1d78c4a1abb72390a4a2cc92e85cb8c59678b8dab`.

Resize now uses int32 accumulators only when the actual coefficient matrix
proves every product and partial sum fits. The proof requires nonnegative
int64 coefficients, protects the row reduction from int64 overflow, and bounds
the accumulator by `2^21 + 255 * maximum_row_sum <= INT32_MAX`.
Horizontal use additionally requires actual uint8 source samples; vertical use
is proved independently after the existing uint8 intermediate conversion.
Every failed proof retains the int64 path. Ordered arithmetic, 22-bit shift,
clipping, legacy conversion, source/effect grids and cache contracts are preserved.
Production blur_regions SHA
`ea503a849d5271d7cf52e5f7bf6d3fa26eca095ea1a72052a437f78b893acb36`.

All61 CPU tests pass, including the unchanged44 native-grid tests and17
dispatch/boundary/fallback cases. An isolated ABBA experiment completes64
unprofiled calls and16 separately profiled allocator calls. All eight geometry
and algorithm groups improve in both pairs: mean wall reduction11.45–25.02%,
allocator peak reduction41.52–46.99%, with exact output and source guards.
Allocator peak is not RSS or GPU memory; quantized CPU time does not uniformly
decrease. This qualifies the resize function, not physical input or editor speed.
Independent completed-experiment analysis SHA
`0fa9f5feb1f6ad5dfde06dc4c8c0145c71175fa5edb112b62708d4be506ca1a1`.
The application verifies the entire703-file before/after source map and changes
only blur_regions. Application report SHA
`2163dde87fcba11f965f4a77bb89cbfe80436f3f5e8b1c769a87c3eca9e5b4f7`.

The post-application native Windows Qt run passes42 tests: regional17,
interactive11, GPU reference9 and native float/HDR5. All four actual processes
and redirectors exit0 with QApplication/pytest0, no skips, callback/cleanup
errors or source drift. Float/HDR tests retain their original tolerances;
the entire42-case suite is not described as byte-exact. Qualification SHA
`14152074bb322846192ebd033b455d8d0b2acd985f36d59bbfa086f5c2605156`.
Fresh full-chapter held-input qualification follows this native run.

## 2026-10-10 23:46 UTC: post-int32 full-chapter input

The new plain12 run passes all8,856 Qt/native packets across the twelve
four-second tool, input-mode and128/240Hz combinations. Both creation-matched
normal-priority processes exit0 with QApplication0 and all source/helper,
private authored-disk, original/settings and Windows runtime guards passing.
Report SHA `1a1f70ced6c9d75585faa97129a5514fc877b4f8ccecb69c9d1c3dbd53140f2b`;
terminal SHA `40de85321606fb657478f8de63f0f54c5ca5a353b1f14adeb804812a6af0911e`.
All three agents acknowledged a complete activity freeze before measurement.
Descriptive held press/move post-to-callback means range0.518–2.322ms. Four
receipts exceed16.7ms, with maximum39.283ms. These synthetic Qt measurements
do not measure physical device or display latency, and are not a paired speedup.

The fresh strict Blur8 Pencil/Eraser run also passes ongoing native-grid LIVE
updates, final held/released FBO and artwork, immutable source/full-model and
complete history restoration. All three agents acknowledged the measurement
freeze. Current edited revisions during posting are Pencil
`[11,15,138,242,321,430,433]` and Eraser `[526,601,603,667,730,792,913]`.
Neither contact cancels an active scene demand. Both processes exit0 and all
source/helper/runtime guards pass. Report SHA
`e3cb75ae430c83c066ee25934f50162186b7d7fd5c6d0b21134aba357f5917a9`;
terminal SHA `17b09015645bcf9117f87ac5b7243764a3c16ef6db1b202fb50a1e925835b586`.
Unchanged strict V8 analysis SHA
`f9a2f0d144bf1ad3ad05d826b35f6f1afb715867229714908242045fe92051e0`.
Observed overlapping worker mean wall remains546.12ms for Pencil and480.28ms
for Eraser, with maxima950.42ms and536.79ms. One Eraser scope crosses the last
post. Scope costs are inclusive and must not be added; this unpaired observation
still shows substantial modifier catch-up cost and establishes no editor speedup.

A separately instrumented500Hz pen/mouse run passes all six Pencil/Eraser/Brush
cases and6,012 Qt/native packets. Both native processes exit0 with QApplication0,
all final guards pass, and both bounded event-loop and GUI-tail observations
are complete without overflow. Report SHA
`25f67375a814e29da1dde76e38df8492d83777fe4a9dd607530fa4aa80631888`;
terminal SHA `449c3b19eaaa8f13128ecaf5a97ee7bc80deb3f5ea1b726be36f2830340c3f42`.
All three agents acknowledged the full measurement freeze. Detailed chronology
analysis follows using the independently reviewed route-specific V3 reader;
trace timing is diagnostic and does not establish physical latency or causation.

The completed V3 chronology reader has SHA
`d51f3c3ff51946e1284b3123d506392031eb6ea93b3c43f071dc7dc2e4967252`.
Root independently checked its completed status, six qualified cases and both
creation-matched exit0 records. At500Hz the pen Pencil's1,000 move receipts
average21.5035ms, p9530.4548ms and maximum37.1341ms;813 exceed16.7ms.
Pen Eraser and Brush average3.2277ms and3.5505ms with no16.7ms exceedances.
Mouse Pencil/Eraser/Brush average4.5529/4.3823/2.1860ms, with one exceedance
for Pencil and none for the other two. These are separately instrumented
diagnostic observations, not a paired editor speedup or physical device timing.
Pen pressure varies0.2–0.8 with Pencil pressure-size enabled; mouse pressure
stays1.0. They are not equal-work controls.

The worst pen Pencil move132 waits37.1341ms, with16 subsequent move posts
before receipt. Previously executing input callbacks overlap25.4303ms of that
window; native apply overlaps17.0139ms within those callbacks. Outer paint
overlaps6.9107ms. Nested spans must not be added. This supports investigating
repeated callback bookkeeping, without establishing OS, GIL or GPU causation
and without authorizing dropped input, changed pressure or changed dab spacing.

## 2026-10-11 00:02 UTC: ordinary workflow after bounded-int32 resize

The root operated the normal editor on private copy234443-9bb4b745, with no
synthetic input or lifecycle/render/save hooks. Pencil, Eraser, textured Brush,
Blur intensity/strength, translation, Free corner warping, rotation and oriented
undo/redo rendered as expected. Atomic UI drags do not prove held-input latency.
Explicit Save completed; reopening restored the three authored test strokes,
Blur48%/8px and the original orientation. The ordinary export toolbar completed.
The V5 read-only reader changes exactly three V4 literals and inverts to the
original V4 bytes/AST. Its post-reopen observation follows completed export.
Post-save SHA `39fb5c9be4f7da96d9f854692698ebf1714051bdf4474a69bf20fd3762f3508c`;
post-reopen SHA `5ba2b4c74747fe1d202baca84d2f683e7824603741179db8c9b449f1a8913294`;
post-export SHA `319f3c0a83736d100ac1533de951195dc0b31078b954e5cb55a1921d85936d63`.
Completed chapter, complete encoded target source tiles and tile index match
the post-Save persisted bytes. This is persisted equality, not an in-memory
before-Save pixel oracle or an atomic directory observation.

`Chaper 1 Rewritten-20261010-195932.png` is1080×82000 RGBA8,2,576,714bytes,
SHA `cc7c1dab2c1a59845e4c4cbb9268ad55631f325b5ab1c699b45eeb9f5ae613e7`.
Every PNG CRC and the complete354,322,000byte decompressed scanline stream
validate; this does not establish independent cold-render pixel equality.
Normal window closure yields actual28732 and redirector29912 creation-matched
exit0, app0, no errors or new rejected writes, unchanged full source/helpers,
and observationally equal entire original project/settings directory.
Owned parent SHA `19050566a60c7796441aec02ac352cbd775ea367dee8cae0ba20aee94de05cbd`.

## 2026-10-11 00:12 UTC: rejected coefficient memo and untraced500Hz input

The unapplied coefficient memo passes77 CPU tests (44 original exact native
resize/Blur contracts plus33 reuse/ownership/real-cap/concurrency controls).
JUnit SHA `bbc083162f1690c64ad8f6ef36e748df9c1f805074a3629f0b923721a7d39807`.
All three agents acknowledge a full activity freeze before the isolated original
64-call ABBA and16 separate allocator calls. An untimed independent preflight
observes32 coefficient calls,16 cold misses,14 warm hits and2 oversized warm
misses; observers restore before the timed original benchmark uses fresh modules.
Protocol SHA `ef64965c8110d1357e572edb18e85c1f49adc0d5d263c720602db83241bddead`;
timing SHA `d68e6db25622e4d24b4a9c55697e7309f7148d875a70f84afe5fcceb98dbb93f`;
strict independent analysis SHA
`38a1d87482930cdfc5851649b13a38a3f5b2371c4d14779b015dd339f7690200`.
All output/source/full-production/helper/artifact guards pass. Warm group mean
wall changes range from1.36% slower to1.83% faster, with mixed ABBA pair results.
The cache is NOT applied: this does not justify new retained memory or complexity.

A fresh ordinary-driver run removes the extra GUI/event-loop tracing, keeping
the current ea503 production, both input modes and all three tools at500Hz for
two seconds each. Copy000645-910a94c0 passes six cases and6,012 Qt/native packets,
held BlankCursor/final held and released native/FBO pixels, full model/source,
one commit and Undo/Redo/Undo restoration. All three agents acknowledge the full
measurement freeze; the first artifact read is158seconds after the conservative
post-launch clock. Normal Discard of synthetic private test edits is followed
by actual28928 and redirector5704 creation-matched normal-priority exit0,
app0 and all source/helper/private-artwork/original/settings/runtime guards.
Report SHA `fed2f1caeb7bf30ddaac0a019b8321b02dab4feb505d29a0f8637a20a8d8efaa`;
terminal SHA `29d1fc9f94d91b6d203696dd53f83d5e2c6eb3cddeb74663fb185e68edd31044`;
descriptive summary SHA
`d434f797f0351a6b94aff17aa38852bfe69f20bbe1b458a5b41b1331c706b98a`.

Pen Pencil held press/move receipts average35.2780ms, p9547.8250ms,
maximum55.0397ms, with981/1001 above16.7ms. Its callbacks average1.5874ms;
29 held CPU paint frames average6.9902ms over the2second offered stream.
Pen Eraser/Brush and mouse Pencil/Eraser/Brush receipt means are
3.1420/2.8985/4.2291/4.1082/1.9599ms, with no16.7ms exceedances.
This confirms high-rate pen Pencil backlog also occurs without the extra tracing.
It is not a paired comparison against the traced run, nor physical USB/scanout
timing; unequal pen pressure-size and mouse work remain as documented above.

### 2026-10-11 00:25 UTC — ordinary dependency publication closure

Preapply V2 independently reviewed, with actual29 CPU contracts passing against
the frozen candidate: unchanged10 transaction tests plus19 invalidation/closure
controls. Qualification SHA `645e1d2c216c2dac51090f6eac3d815897ace21ea082899bb3edb7dc70659743`;
both retained process handles confirm creation-matched OS exit0, no application
instance or UI imports, and complete source/helper guards unchanged.

The full copied chapter graph has173 objects,106 layers,103 modifiers and29 masks.
Eight ordered ABBA pairs each publish1000 prebuilt ordinary raster notifications;
full returned closure lists, complete dependency/index/generation state and model
bytes match afterward. All8 pairs improve; median publication reduction34.2396%.
Typical pair means are3.9 to2.6 microseconds per publication, so this is a small
absolute reduction in input cost and does not establish a whole-editor speedup.
Qualification SHA `d377c4d243514a33340bf93d6fd91a4c1a1a0a1cce5d7f4a77f66682eca00b76`;
both actual/redirector OS exit0 and all source guards pass. All3 agents explicitly
fully frozen before launch, with root's first artifact read162seconds after the
conservative post-launch clock. Natural GC/OS scheduling remain uncontrolled;
the report's profiling-disabled field reflects ordinary unprofiled launch.

The sole change skips the second pure affected-closure walk when no graph links
can change. Document rebinding, structural/conservative changes and link fields
retain the original old/new union; revision/generation and renderer semantics
remain intact. Atomic staged/fsynced one-target installer archives717 exact
inputs and checks ownership immediately before replacement/rollback. Only
`comic_editor/core/changes.py` changes from `eb042aee...` to
`1fa372969e620e939c72795936bf4370c245c2c84788f9c5837ff53a3caa799d`.
Install report SHA `4f2086ca87df02cfa8d690ceb900887b10b4223874f920e45b2e6e8cbfe2b91d`:
installed, committed, no rollback/cleanup errors. Actual production transaction10
tests pass without failures/errors/skips, XML SHA
`3b49b0fc948d68c66afc39ca2b699f574c7025b39cced8d5384d4dde878d88ad`.
Further native/input qualification follows; persistent geometry/tuple metadata
memos have not been installed.

### 2026-10-11 00:38 UTC — closure integration and owned input profile

The installed closure change passes all42 native integration cases in four fresh
Windows Qt applications: regional17, interactive11, GPU reference9, native float5.
Qualification SHA `cfddb11d140ee1f37d58b1ab16e0cd86da1eca4cd9bfedc31939addbbe0dd78e`.
Each actual interpreter and redirector has creation-matched OS exit0; all original
assertions, native admission, fixture teardown and full source guards pass. This
is correctness evidence, with source-only agent work continuing concurrently;
it is not an isolated performance measurement.

A separately reviewed artifact-only driver profiles1000 admitted pen Pencil
calls on a fresh independent Pocket Boyfriend / Chapter1Rewritten copy, while
retaining the original native pixel, held/released FBO, pressure, cursor, full
model, one-command and Undo/Redo assertions. All3 agents explicitly freeze before
launch; first artifact inspection is166seconds after the conservative launch
clock. No other QA application runs. The exact original admitted method delegates
once per call, and its descriptor is restored before terminal qualification.
The45 source/CPython-hook controls pass; no unowned canvas or thread is profiled.

Profile SHA `d3d60eeb83923fbbecfaca2274158956181b3e12de1ecffc3cfb8885a397e9f1`;
native report SHA `0264b754db9ff7dc56bb4c7ae7a0dfd01a0d2c2d894f02ced9725aae38153afb`;
terminal SHA `11c68c52f87a886f59e9eefa25e28c959ae328e15685ed1c2529f560cfa77819`.
Actual22964 and redirector22916 both exit0 with matching retained creation
identities after ordinary private-test Discard; app0 and full source/helper,
private artwork, original project and settings guards pass. A stale computer-use
inventory is rejected before input; resetting only its observation kernel yields
the correct private UUID window. No unrelated application receives input.

The1000 profiled admitted calls total1.365seconds. Nested cumulative costs include
projection invalidation0.229seconds (43475 capture-rectangle constructions),
3000 widget-damage calls0.267seconds, and5000 full GPU contact checks0.273seconds.
The native paint-segment calls total0.075seconds. There are202000 getattr calls
with0.141seconds self cost. These nested times must not be added. Profiler overhead,
existing driver observers and scope boundaries mean these are diagnostic costs,
not physical pointer, complete callback, display or optimization latency proof.
They direct the next candidates toward repeated invalidation/repaint bookkeeping;
input samples, native dabs, pressure and renderer/cache contracts stay intact.

### 2026-10-11 00:44 UTC — full pending GPU damage, preapply harness failure

An artifact-only five-line candidate skips redundant QRegion work after the
unchanged full current GPU-contact check, only when the frame timer is active and
the pending region is exactly one rectangle equal to the current widget. Qt's
QRegion.contains(QRect) tests overlap, so it is deliberately not used as a full
coverage witness. Partial, holey, larger, inactive, software and invalid-owner
routes keep the original path. No production canvas change is applied here.

V1 runs all31 native cases successfully: unchanged18 original GPU pacing cases
and13 real region/timer/ownership controls. The final generic fixture validator
rejects the original FrameCountGpuCanvas subclass because its literal class gate
only accepts RasterCanvasWidget/GpuCanvasWidget. Actual/redirector OS exits are1;
this run is explicitly UNQUALIFIED despite31 test assertions passing. Reported
app/pytest returns0, source guards and admission/cleanup all pass. Qualification
SHA `50f4844cb8c4fc71d160b62b37f6ab3d88adc22d566e453266bd08b5692fabd9`;
XML SHA `00a5236f3144bc3cdab23f6dcd5a2bf2a0b98b21b4727bf54207dbf083022042`.
The raw Windows COM exception diagnostic0x8001010d is retained; it does not by
itself establish an application crash. A new V2 harness requires exact registered
original subclass identity, production GPU isinstance, pinned source and actual
MRO instead of weakening the real fixture requirement. Original31 test bytes and
cleanup methods remain unchanged; V1 reports are preserved.

V2 actual rerun passes31/31 with app/pytest0 and matching retained actual24616
and redirector26088 OS exit0. Exact original subclass/source/MRO evidence, all
source/helper guards, empty native admission and all cleanup controls pass.
Qualification SHA `4562e78c540b27e5da0cff1138fb29a35bc0763b2888e49e73a789a696075c55`;
XML SHA `4ced51153a4b76570638d457edcf63b487366c7ab68e0b65f1f6c71dd6f392cf`.
These are preapply native correctness tests, with source-only agents working
concurrently; they provide no isolated input-latency or performance claim.

### 2026-10-11 00:53 UTC — queue benchmark native failure and handler conflict

Queue benchmarkV2 returns no interval/child/XML evidence. Actual26184 and
redirector8772 have matching retained creation identities and real terminal
status `0xc000041d` (3221226525); the failed qualification is preserved with SHA
`89a5f65e03522b64937c7d84e078a2f85e8556eb55bb64b7f855435c726cbb11`.
All3 agents fully freeze before launch. First artifact read is170seconds after
the conservative launch clock. Parent final production/test/helper guards remain
unchanged. No candidate canvas is installed and no timing result is claimed.
Stderr records COM0x8001010d then access violation while printing other-thread
import frames; the custom fatal.log is empty. This is an actual QA process failure,
not an accepted benchmark or evidence of its exact root cause.

Root and independent reviewers read the installed pytest built-in faulthandler
plugin. Its configure hook always enables a duplicated stderr handler with default
all_threads=True; teardown disables and restores stderr/defaultallthreads. Thus
PYTEST_DISABLE_PLUGIN_AUTOLOAD does not preserve the custom raw-FD/current-thread
handler: it only prevents external plugin autoload. Pointer/cursor/helper imports
contain no corresponding handler changes. Earlier pytest native wrappers did
enable a raw-FD handler, but their claim that it remained current-thread throughout
pytest is not established and is corrected here; actual pixel/test/terminal
evidence remains as recorded. Non-pytest owned editor drivers are unaffected by
this particular plugin conflict. The new artifact harness blocks only pytest's
handler-owning plugin with `-p no:faulthandler`, keeping its own fatal handler
enabled through actual OS exit, with explicit plugin/enabled checkpoints. This
source-backed conflict is not proof that the handler caused the native failure.

### 2026-10-11 01:07 UTC — actual queue/projection measurements and guarded application

Projection candidate `23213fca4d2799751b44f9cb98e0a0707efe268f2ebf058e05ee9bccda2bff52`
passes the unchanged13 repository projection tests plus23 geometry controls in
an actual Windows QApplication. Qualification SHA
`6db7fe5219e026f0e1368a11bbe18b21899a2f6e8e5c475bcf93dcb5a3312394`;
36-test XML SHA `f9bc15209c1b75b6f62fd16c371c71973ed9dd2addaf26913f9caa41ce3edcc0`.
Retained actual/redirector creation identities both terminate OS0; pytest/app0,
native admission, fixture cleanup and all source guards pass. Its wrapper blocks
only the built-in pytest faulthandler plugin and retains the current-thread raw-FD
handler. Original tests/helpers and production handler behavior are unchanged.

Queue benchmarkV3 qualifies with SHA
`63fc91947f7cf4335d2c19625942cf973e9ec540a8d9bdde627ecfc6b0f96f13`;
32 actual intervals SHA
`45e6f774077736a53479b24e4f44db47a2a56e3738884f3789908df8a914bce3`.
All8 ABBA pairs favor the narrow full-pending-region shortcut: median2373.9ns
saved per redundant queue call, median9.093% helper reduction. The full current
GPU owner/context/view guard still runs on every call. Actual post-contact field
inventory confirms `_mask_tile_input`, `_mask_deferred_strokes` and
`_raster_deferred_strokes` are absent; initialization of these fields is a separate
unapplied draft. No GC change, profiling, tracing or event pump occurs in timing.

Projection7-mode/4-pair ABBA qualification SHA
`2624f42c4ef5fdf6ea967f6b15d66d8e65c1cd9d6e478230ca1a0f0110aa0714`
retains112 actual rows. Warm43 median21.199us falls to5.856us (15.343us saved);
warm partial saves15.222us and128 tiles/four configurations saves47.237us.
All4 pairs favor each warmed mode. Cold43 first-capture cost rises38.264us;
negative-rectangle Qt fallback rises1.936us, full-None rises0.644us and all-invalid
rises1.043us. All corresponding4 pairs retain those regressions. The independent
stdlib raw-row/state/count/arithmetic analysis is saved with SHA
`ee57f2de4a2efab8bda39038a37c8f3b3054e203f92852396a239a87fe9833b4`.
These are function measurements, not whole-editor or physical-input speedups.

All3 source agents acknowledge a full freeze before these measurements. Root's
first artifact read occurs190seconds after the conservative queue launch clock
and167seconds after projection launch. Actual and redirector BelowNormal
creation-verified OS exits0 and complete source/helper guards are accepted before
release. The failedV2 native run remains separately recorded above.

Root applies only canvas75f→1da through the guarded747-input installer; report SHA
`b4a41457bdbabf3189966b2db14112510c955ab5478f9a4ec351e758a86629c5`.
Projection's original binding correctly rejects that prior canvas drift. A NEW
explicit binding accepts only the recorded canvas replacement, retains identical
candidate/tests/validators/evidence and guards730 inputs. Root then applies only
projection4a51→23213; report SHA
`d49ad5e3c6a7b0a7018ccd4d7616e64c040402ffd078d595ee5d13dad0a81785`.
Both installers archive exact sources, fsync staged replacement, recheck target
ownership immediately before commit/rollback, preserve foreign changes and report
installed/committed with no rollback or cleanup errors. Combined current-source
native correctness and fresh private-copy input/workflow checks remain pending.

The unchanged original input driver also records packet/native/frame/presenter
observations in its unprofiled mode. Synthetic receipt/callback times include
different observer subsets; they do not establish USB/scan-out latency or a cause
for the earlier Pencil500Hz backlog. Its5ms held tick does not scan the full model
while posting. A separate observer comparison would require a new explicit
diagnostic artifact and paired evidence; original driver/helpers remain unchanged.

### 2026-10-11 01:28 UTC — installed-source native78, plain12, high-rate and Blur checks

Current production remains changes1fa/canvas1da/projection232 plus the previously
qualified sources. A fresh independent Pocket Boyfriend copy with all13,825 files,
180 saved brush resources and private settings runs the unchanged original native
input driver. Plain12 at128/240Hz, pen and mouse, four seconds per contact, qualifies
all8,856 packets with held/released native source/FBO equality, exact pressure/order,
BlankCursor during contact, one history commit and full undo/redo/undo restoration.
Report SHA `69a80de727fc81bf129125e9ae58db2cdd03436d6d38848692168c769a14d4da`;
terminal SHA `21c470be020be234d36a0e393a84963d473789b95ab5e89247ef31cb5db69a15`.
Actual9120/redirector28996 creation-matched Normal32 OS0 and app0; final original,
settings, production/helper and Windows runtime guards pass. Mean synthetic
post→receipt spans0.553–2.152ms;1 of8,844 held receipts exceeds16.7ms (26.086ms).
CPU/Qt-swap milestones average62.25–63.25Hz during the offered streams. Summary
SHA `7c362d4460b7bccbbda151750106bfa24a8fd4ee855a6ac9404aa4fd2a9ab0c0`.
Startup records the ordinary automatic NVIDIA RTX5060/driver617.42 presentation
result, not an additional hardware/driver probe. These are observer-instrumented
synthetic Qt measurements; no paired whole-editor improvement is established.

Fresh500Hz six-case/2second contacts qualify all6,012 packets and the same pixel,
cursor, source/model/history controls. Report SHA
`1f5df74eb6ab7846551df071b2b0d379ff12f52f90c7c50d1abc1dfaaa093cb9`;
terminal SHA `31b4fe40ce43bc004b2d70fd28eae127bf04ccef717bef6dc787a8b6e9e80293`.
Actual22704/redirector16172 creation-matched Normal32 OS0/app0/full guards pass.
First pen Pencil remains slower: mean receipt10.242ms, p9519.163ms, max26.938ms,
101 of1,001 held receipts above16.7ms; mean callback1.252ms, CPU frame6.472ms and
68 held frames/34Hz over two seconds. Otherfive cases mean1.795–4.092ms, no receipts
above16.7ms and62.5–64.5Hz. Its summary SHA
`ae88bb5486847969c511beabfedf4363067c7ba46f60c19e0464883701baee96`.
The earlier35ms result is a separate unpaired run; this does not establish the
cause or a percentage gain. High-rate first Pencil backlog remains outstanding.

Combined postproduction78 native checks pass17 regional/11 interactive/9 GPU
reference/5 float-native/13 original projection/23 actual Qt geometry controls.
Qualification SHA `b862169bdc4216f01bdb0a73415940706253bc36ce9242e704f7feee07629be6`.
Every six fresh actual/redirector BelowNormal pair terminates creation-matched OS0;
app/pytest0, exact XML counts/no skips/errors, genuine fixtures, empty admission,
owned cleanup, current-thread handler/plugin guards and710 source checks pass.
Geometry tests use installed render.projection232; the UI compatibility import
reexports that same class. Literal original test bodies/helpers are unchanged.
Other source-only work/copy I/O runs concurrently: this is correctness evidence,
not an isolated performance benchmark.

Fresh Blur8 pencil/eraser128Hz/4second contacts qualify1,028 packets, with7 actual
current artwork preview revisions while input is posting in each contact, current
native-preview oracle equality and full source/model/history restoration.
Report SHA `582d410d6d8916be04b4df924caed672e719c85836f35290c72e4daf6d917917`;
terminal SHA `d99cb1b30da6210b71219f1d99fc774eeb0fdfbb795e68e49f7e2f4de00bd621`.
Actual28760/redirector10208 creation-matched Normal32 OS0/app0/full guards pass.
Mean receipt3.143/2.760ms, maximum22.144/16.815ms;8/2 tails above16.7ms. Widget
frames around63/64Hz are not modified-artwork update rates: seven revisions in
four seconds still leave background modifier calculation as a visible limit.
Summary SHA `483aa1a0c4e92aa72bb636d644b680d0573a620f3c9341c10e86617f83f2a5ae`.

All3 agents freshly acknowledge full freezes before each held-input launch;
first artifact reads occur159/157/154seconds after respective conservative
launch clocks. Root closes each exact private UUID window via normal Discard,
then accepts both terminal handles and final guards before releasing agents.
Normal current-source transform/save/reopen/export UI checks remain pending.

### 2026-10-11 01:43 UTC — ordinary editor workflow and optional-state native controls

Root uses Computer Use on the ordinary current-source editor in private copy
`visible-private-editor-20261011-012800-bb247524`. Pencil stroke, crossing erasure,
eraser Undo/Redo, the installed HIMOG textured brush, Free corner deformation
with Undo/Redo/Undo, rotation with Undo, and translation with Undo/Redo/Undo are
visually checked. Object Select translates a Raster from the outside-edge band;
an interior click selects its ordinary Pencil tool, and pivot dragging moves the
pivot. These gestures are not independent sustained-input timing evidence.

Blur8 Full is added, renders the test artwork blurred, and its addition is
undone/redone/undone. An uncommitted spinbox edit is not claimed as a separately
qualified parameter-history transaction. Ctrl+S completes with its success toast.
The private project closes and reopens through Recent, retaining the pencil gap,
brush stroke and original zigzag. Later translation checks are discarded without
another Save; the second reopen selects No for the private test autosave and
loads the explicitly saved artwork. Original project/settings are never selected.

The independently reviewed saved-stage reader observes a clean, stable persisted
baseline after that second reopen, not before the first reopen. Report SHA
`a564f4aa99d0c6f129474d6c32b9727422ae97b670e82346f620054274d321a2`;
chapter SHA `3bb11fdf74967b352a3a70cb4b6ce9ad4c587b4f13ef0958924c54bfcfe70012`.
The actual exported filename is `Chaper 1 Rewritten-20261010-213633.png`.
Full PNG CRC and bounded stream validation confirms 1080x82000 RGBA8,
2,543,245 file bytes and 354,322,000 decoded scanline bytes; PNG SHA
`9f6011ee2bfadc27dd37cbf3f11861afcb9233672d8ea6cb20c4fa88509b6ce4`.
This is export integrity/dimension evidence, not an independent cold-render pixel
comparison. The earlier reader's incorrect `Chapter` basename failure remains
preserved rather than being rewritten.

Normal editor shutdown completes. Parent SHA
`2399a29eb9cfd9681899e8e00d4bf7c3c397b8f2fda10d7aa7ed311bbda871e7`;
actual24608 and redirector16768 retain their creation identities, Normal32 and
OS0, with full source/test/helper guards, no callback/write-audit errors and the
whole original project/settings-directory manifests observationally unchanged.

Unapplied three-None V2 controls also qualify all ten genuine native tests:
six unchanged original Pencil/Eraser/Brush pen/mouse FIFO/native/FBO/history tests,
three positive-owner refusals and idle cancellation. Actual23224/redirector9072,
BelowNormal16384, both creation-matched OS0/app0/pytest0, empty admission and
source/handler/fixture guards. Qualification SHA
`2cda0d49ed99ee4a5fa5b691656c0bb5c4eadbef0d93cae0b7521ca1aa3d3cc5`;
exact ten-test XML SHA `036de011b99f15dcb78941bf202420d8c5f760775f287287eebfba56d18bfadf`.
The adapter installs the defaults after ordinary initialization; separate AST and
byte-inverse proofs pin their early constructor placement and absence of field
reads during initialization. No performance or application qualification exists
for this candidate yet. A four-contact fixed-order 500Hz Pencil diagnostic is
source-reviewed and pending actual execution; it cannot identify a specific
cache/OS cause or physical USB latency.

