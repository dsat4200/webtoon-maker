# Rewritten chapter responsiveness investigation

Status: investigation in progress. The early measurements below are diagnostic
captures, not final acceptance results.

The clean frozen-v13 six-effect Blueprint run completes and agrees with three
independently recomputed native patches. Its painted/restored exact settlement
is 28.754/33.575 seconds; v10's corresponding values were 84.252/72.955 seconds,
with all 150 checked native tiles unchanged. Most live mask contacts take about
60 ms, with one unexplained 213 ms first-drag outlier; release's first paint is
174.702 ms and binding Undo 552.273 ms. Genuine saved-stack transform paints take
about 191–224 ms; other heavy object paints reach 482 ms. A later cold-source
diagnostic also found initial transform paints without selected current pixels;
paint durations alone therefore do not prove live artwork feedback. These delays
prevent performance acceptance. Priority under cache pressure and shared Mesh
coverage are integrated. Further history, metadata, source precision, radial
and outline changes have passed their focused checks but await frozen-source
native acceptance. A new current-model Twirl preview comparison changes pixels;
its exact settling and full-editor visibility still need separate assessment.
The full-model mask-release reuse experiment was rejected, rather than retained
on timing claims. Detailed methods and limitations are preserved below.

## Test case and preservation

The supplied test project is `C:\Users\hopper\Documents\Pocket-Boyfriend Test Copy`.
The requested chapter is saved as **Chaper 1 Rewritten**, ID
`0a72f08009294aa0a3d14e6a38e22bbb`. It is 1080 × 54896 canvas pixels and contains
99 layers, 162 objects, 94 modifiers and 24 masks. Testing uses separate project
copies or direct, read-only source loading with edits held in memory. The original
project has a 14317-file SHA-256 manifest. Blender editing and live Blender
providers are excluded.

The saved chapter includes 38 image records with `blender_comic_view` sources
and seven embedded sources. Tests render their already stored image bytes;
they do not contact Blender or change those source definitions.

The machine has an Intel i5-1235U (12 logical processors), Intel Iris Xe graphics,
about 8 GB RAM, Windows 11, Python 3.14.6, and Qt/PySide 6.11.1. The native GPU
test uses a valid OpenGL 3.3 core context and display density 1.5.

## Measurement method

Native computer-use actions exercise the complete editor, including the
outliner, modifier cards, navigator and canvas. The built-in Performance Monitor
records GUI heartbeat gaps, inclusive phase durations, bounded Python stacks,
queue/cache counters and process memory. Inclusive parent and child times must
not be added. A stack sampled at the monitor's own refresh is not sufficient to
attribute the entire delay to the monitor.

The opt-in `tests/benchmark_real_project_interactions.py` harness loads the
chapter, raster tiles, masks and embedded image sources directly. It bypasses
project recovery and saving, uses private settings, and restores edits through
ordinary history. Its hidden native Qt widget measures handlers and paint work;
it does not establish physical pointer-to-display latency or include the
MainWindow inspector layout. Results record the actual widget, context, density,
harness hash and source root. Separate processes and quiet measurement windows
are used for CPU timing. cProfile and read-only py-spy dumps diagnose CPU callers;
there is no RenderDoc GPU-time claim.

Earlier harness tile comparisons force a synchronous projection recapture but
retain completed effect and tile results. They validate current publication and
model consistency, rather than independently recomputing every effect kernel.
Fresh, uncached kernel comparisons are identified separately. Palette oracles
use a separate native sampler; the synthetic spatial/pressure checks clear
derived caches before comparing established and promoted demand paths.

Raw artifacts are under `.artifacts/night-performance-20261006/`. Some early
helpers used a raster widget, omitted the final transform release signal, or
used an incomplete reparent case. Their results retain those limitations and
are not directly interchangeable with the later GPU interaction run.

## Heavy targets

| Target | Stored / active effects | Additional load |
| --- | --- | --- |
| University Bridge blueprint | 6 / 4 | 5745 × 3852 embedded source; mask; Deform, Twirl, Lens, Outline |
| room.png | 3 / 3 | Mesh Warp, masked Twirl, Kuwahara; 1080 × 1620 source |
| curles.png | 3 / 2 | Deform, Smudge, mask; muted Kuwahara |
| fallingthroughlife.png | 1 / 1 | Smudge, mask, plus two muted ancestor effects |
| Raster 38 | 3 / 3 | Two HSL effects, Outline, mask; 1533 × 4123 painted bounds |

The blueprint's two muted effects also need an all-six-active stress run. Saved
inactive effects are not counted as executed work.

## Baseline evidence

The native blueprint capture `20261006-224058-3792703c88` ran for 196.2 seconds.
Windows visibly reported an unresponsive editor during scaling. The maximum GUI
heartbeat gap was **14906 ms**. Its 46 canvas paints had a **2725 ms p95** and
**11898 ms maximum**. The selected object was the blueprint and the active tool
was Transform. The capture submitted 1994 effect jobs, completed 1352 and
discarded 642. Native stacks include effect dependency planning, distort work,
outline distance fields, gradient source projection and nested signatures.

The earlier capture `20261006-222512-14fac7215e` includes mixed human and automated
actions and multiple chapters. Its 22.38-second maximum gap corroborates a severe
problem but cannot be attributed exclusively to the automated blueprint case.

A clean hidden raster baseline rendered the blueprint's cold region in 29.4
seconds and parameter updates in roughly 14–15 seconds. The room case's cold
region took 126 seconds; the run was deliberately stopped during another long
undo. Completed operations and its incomplete status are retained in
`heavy-four-baseline-clean/results.json`. Thirty-five restored blueprint tiles
matched the exact reference byte for byte. The room result is not a completed
four-object benchmark.

## Findings and changes under validation

- Repeated recursive render signatures are now memoized within a single capture,
  with bounded entries and complete dependency/context separation. A signature
  microbenchmark fell from 115 to 24.8 ms with identical values.
- Slider drags previously invalidated broad document state and rebuilt identical
  modifier cards. Conservative affected bounds, drag markers and an inspector
  fingerprint preserve live changes while avoiding redundant work. Nonlocal,
  compound and linked mask/color dependencies keep full invalidation.
- Live projections require a fresh current-model preview, separate from exact
  tile and disk caches. Preview eligibility checks document/source/history,
  pixel contract, density and coverage. Exact base and top layers publish as one
  coherent batch. Detached/export requests retain exact rendering.
- Raster screen painting now yields pending exact work to the event loop instead
  of rendering it synchronously. An expired preparation budget still schedules
  further progress even when no worker is currently running.
- A failed or pending committed preview could force exact rendering back onto
  the GUI when the previous view had the same configuration and an older
  revision. Screen deferral now survives that failure. Regression checks cover
  both failure statuses, reentrant edits, retained publication revisions and
  later exact convergence; detached output still renders exactly.
- Exact spatial work survived the live-preview early return. A selective
  cancellation path retires obsolete exact jobs before
  fresh interactive preparation, while nonexact preview jobs remain available.
- Spatial siblings repeatedly traversed the same unfinished complete predecessor
  within a capture. Pending regions are now remembered for that graph's lifetime,
  and existing exact stage results are checked before capturing their inputs.
  The native-pixel pipeline, semantic keys and memory limits are unchanged.
- Regular square/hexagonal halftone geometry now evaluates only requested output
  rows, using the same padded source blur, lattice and arithmetic. Detached
  snapshots reduce copied input and account for actual peak arrays. Other
  patterns and unsupported masked/color cases retain their original paths.
- Regional blur dependency ranges use direct min/max calculations with unchanged
  kernel coefficients and pixel results.

These microbenchmarks and focused regressions are supporting evidence. They do
not establish that the full editor is fast enough.

The hidden MainWindow audit includes ordinary layouts, selection signals,
inspector construction, saved mask thumbnails and full history notifications,
with complete canvas painting suppressed. Its repeated clean medians were
64.4 ms for blueprint selection, 30.4 ms for room selection, 97.9 ms for full
blueprint undo, and 1.60 ms for saved-mask refresh. The two saved Posterize
constructors took 8.3 and 9.8 ms; cached statistics took about 0.8 ms. Duplicate
chapter/hierarchy refresh handlers consumed only a few milliseconds in this
case, so they were not rewritten speculatively. Raw profiles are under
`.artifacts/interaction-audit-20261006/mainwindow-refresh-profile/`.

A separately labeled stress case appended Posterize after the heavy blueprint's
saved stack. Its constructor blocked the GUI for more than 15 seconds and was
stopped by the diagnostic watchdog. The stack was full upstream distortion
sampling, with the GPU worker idle and canvas painting suppressed. Statistics
construction and refresh require deferred exact upstream work; reducing the
statistics source grid would change the sampled colors and is not the fix.

An intermediate v3 native run completed that constructor in 750.3 ms and its
exact statistics in 21.604 seconds. A separate cold Add Posterize action returned
in 1235.4 ms and completed its palette in 20.400 seconds; the palette matched the
synchronous native oracle exactly. Maximum event-loop gaps during settlement
were still 511.6 and 705.1 ms. Those are unresolved pauses, not acceptable final
results. A separate cold Add profile attributes 517 ms to ImageStore decoding
and roughly 302 ms to first-use SciPy imports. The asynchronous source and
completion paths require further work. These hidden UI probes suppress full
canvas painting and do not measure physical input latency.

The clean v4 Add probe returned in **339.5 ms**, completed its exact palette in
**17.448 seconds**, and matched the native palette oracle. Its largest event-loop
gap was still **511.8 ms**. Owning-thread instrumentation of a separate completion
diagnostic identifies a native 5399 × 5399 paint-only opacity mask: its field and
opacity application took 257.7 and 182.1 ms, respectively, under instrumentation.
Those costs are distinct from the background Outline field and must not be
inferred from aggregate multithreaded cProfile totals. The native mask grid is
preserved while its immutable worker snapshot and completion path are revised.

The clean final-v6 Add probe exited normally, preserved its source and private
project manifests, and matched the native palette exactly. No owning-thread
painted-mask field or opacity application ran during palette preparation. Its
maximum event-loop gap was **283.7 ms**, first action return **678.0 ms**, and
exact palette completion **23.427 seconds**. Masked source sampling still took
243.8 ms, and installing the modifier and rebuilding its inspector took about
232 ms. This verifies that the native opacity work moved off the GUI but still
rejects responsiveness acceptance. The longer exact settlement than the v4 run
is recorded rather than hidden. Raw results are under
`mainwindow-posterize-final-v6-add` in the interaction-audit artifacts.

The visible six-active capture also exposed repeated disk descriptor hashing.
Its bounded leaf samples attributed 41.3% to canonical hashing; a single stack
from adjacent slam.png does not establish that object as the dominant delay.
A typed, bounded descriptor-identity memo preserves the existing canonical JSON
and SHA-256 identity. It deliberately distinguishes Python-equal bool/int/float
values and signed zeros, avoids mutable semantic values, and revalidates entry
seals and dependencies on every read. Empty published indices can miss without
hashing. Fifty focused identity/disk checks passed; hash-only microbenchmarks
improved by 3.4–3.8 times. This is not yet a native paint improvement claim.

## Rejected acceptance results and unresolved work

The first native GPU after-run, `blueprint-gpu-after-saved-clean`, still measured
559–597 ms live translation paints and 601–675 ms scale paints. It later froze
for several minutes at scale commit. All exact and graphics workers were idle
while the GUI recursively assembled complete predecessor frames in TileGraph
and repeatedly entered spatial stage planning. That run was stopped and marked
incomplete. It is not a successful after result. Three read-only stack dumps,
including bounded locals, are retained. A requested sampling recording failed
and is unusable for quantitative timing.

The first bounded scale reproducer after that dependency fix,
`blueprint-gpu-scale-diagnostic-v2`, completed all eight phases. Scale commit took
37.5 ms in the handler and 356.4 ms in paint (393.9 ms combined). This run enabled
profiling and planning instrumentation, and only allowed a 0.2-second settlement
window: it proves the action returned, but does not prove exact convergence or
acceptable interaction speed. Live scale frames were still 851–948 ms. Its
nonzero shutdown status is retained. The hidden harness previously skipped the
normal final Qt quit/deferred-deletion cycle. A standalone QWidget/OpenGL-widget
matrix exited cleanly, and adding that ordinary lifecycle cycle to the harness
changed its direct native exit from `0xC0000409` to zero. Timed operations are
unchanged; production shutdown code was not modified.

A single-viewport preview experiment reduced a stable warmed paint median from
193.5 to 27.8 ms, but changed 35937 output bytes, mostly at chapter edges and
block seams; it clipped an initial letter at the chapter edge. This experiment
is opt-in and is not the production default. Native sampling equivalence must
be demonstrated before changing that presentation path.

The clean intermediate v3 GPU scale run (`blueprint-gpu-scale-clean-v3`) returned
all eight phases and exited with native status zero. Its two live scale paints
still took 441 and 430 ms; commit took 276 ms including the handler, and undo
took 495 ms. Exact settlement took 37.087 seconds, after which all 35 reference
tiles matched a synchronous recapture byte for byte. These results reject v3 as
a responsiveness acceptance result.

The next visible v3 editor pass activated the blueprint's muted Pinch/Punch and
HSL effects, exercised translation, scaling, slider history and redo, then added
and painted an HSL intensity mask. Cold navigation first showed an empty canvas
while native effects ran. A new coverage guard now requests a fresh bounded
current-model preview for an uncovered viewport; 64 focused checks cover initial
loading, unchanged-revision navigation, stale document identities, bounded
drafts, native worker deferral and later exact convergence.

The six-active mask pass became unresponsive before its Save Current action
could complete. The owned test process was stopped after retaining monitor
checkpoints and read-only stack dumps. This diagnostic capture overlaps short
correctness-test runs and is not a clean before/after benchmark. Its checkpoint
contains repeated slow exact paints, hundreds of thousands of cache lookups and
an unpublished current revision; Windows also visibly reported Not Responding.
Stack dumps include both the blueprint and adjacent slam.png work in the full
viewport. The sampled cache-descriptor canonical hashing is a measured hot path,
not evidence that the navigator alone caused the stall. The sampling recorder
fell behind and did not produce a usable output; it is excluded from quantitative
claims. The checkpoint is `20261006-235139-b39df77c0c`.

A bounded memory-only reproduction (`blueprint-gpu-six-mask-profile-v5`) also
failed at first mask-contact paint, independently of disk caching. Its actual
begin-stroke handler took 0.783 ms, but the subsequent paint exceeded the
15-second watchdog. Both effect executors and the graphics worker were idle.
The UI ran 589 native distortion calls, spending 12.20 seconds inclusive there;
mask sampling itself took only 21.6 ms. Contact requested exact work on a 29 MP
Pinch predecessor instead of the existing bounded interactive path. This failed
run has no finished-pixel or undo claim.

Mask contact now requests a fresh bounded current-model composite for image and
parent parameter masks. It captures the entire visible scene because a local
mask stroke can affect nonlocal spatial stages. It never falls through to exact
tile collection when the draft is pending or failed; immediate mask overlay and
the last coherent view retain their separate identities. Release uses normal
exact settlement. Sixty-seven focused checks cover contact, failure, current
pixels, bounded effect inputs, native release/undo equality and existing raster
ink/publication behavior. Native retiming remains required.

The subsequent visible v5 pass used a fresh private copy and exercised solo,
all-six activation, an HSL intensity-mask stroke, Save Current, a saved-mask
endpoint change and Undo, and a cage-point drag with Escape restoring that point.
Save Current completed and displayed the named mask. The cage controls remained
visible in the modifier panel; v7 subsequently confirmed that selecting Tool
Settings exits modifier editing and removes them. Initial navigation displayed
fresh coarse artwork before its
exact refinement. The editor then closed normally after saving only this copy.

This is functional and diagnostic evidence, not acceptance timing. Its monitor
capture `20261007-001724-66e064b9e4` overlaps short correctness runs. The final
capture contains 512 canvas paints, 386878 ms inclusive paint time, a 2268 ms
paint p95, and a 3329 ms maximum heartbeat gap. A read-only stack dump while
the four effect workers and GPU worker were idle found repeated modifier
validation/serialization and bounds planning on the GUI. Capture-local reuse
now checks complete typed mutable state while retaining the exact serializer
strings, unrounded geometry and copied rectangle values. Mesh bounds omit only
unused source mapping and preserve destination tessellation and native pixels.
This planning fix and the deferred native paint-only opacity output require
new quiet native measurements; successful mask saving alone does not establish
responsive editing.

The v6 read-through mask diagnostic used the real disk-cache controller and
dependency rules against an empty validated index, with all 4112 original cache
file hashes and the index unchanged. Contact completed with 1.5–2.7 ms handlers
and 154–281 ms profiled paints; it admitted no new exact jobs during contact.
Release still spent about 697 ms building projection dependency keys that could
not hit this index. Both painted and restored all-six exact waits exceeded
120 seconds. The saved four-active state converged in 41.28 seconds and all 50
native reference tiles were byte-identical. These active-stack timeouts are
explicitly unverified output, not passing exact results.

A separate short retention trace reached the 512-record limit with 390 unique
image storages and 237.7 MB retained. During one unchanged painted release it
recorded 7349 evictions caused by record pressure with no byte-pressure
evictions. Worker-result and graph aliases consume multiple records for shared
pixels; incomplete full predecessors also lose their copied tile progress on
each pending retry. The fix must preserve bounded progress rather than increase
the artwork cache budget. All partial assemblies must remain outside durable
exact caches until their complete dependency coverage is available.

Appending a modifier now retains existing inspector cards only when their
complete detached records, identities, selection/history/context, pixel
contract, mask and owner metadata are unchanged; selected owners may only append
the same new suffix. Other changes retain a full rebuild. Forty-three focused
reuse, locality and slider-history checks passed, including shared ownership,
old-card callbacks, replacement, changed dependencies and actual undo/redo
rebinding. Native Add timings after this change remain pending.

Compact live prefixes now reuse only direct original-image captures with the
existing complete source/stage semantics and explicit precision contract.
Paint-only parameter masks qualify; contributors, gradients, derived parent
sources and target-layer pattern colors do not. The separate transient key
never enters exact or durable caches. Seventy-four focused checks cover late
mask painting with unchanged warp reuse, volatile Qt source handles, source and
geometry edits, parent motion, upstream parameters/masks/endpoints, precision
changes, uncached draft/native pixel equality, contact publication and inspector
bindings. A retained Posterize panel keeps its ready statistics on append and
refreshes them after an earlier effect changes. Native timing remains pending.

## Frozen v7 diagnostic and visible editing results

The read-through native GPU six-effect run
`blueprint-gpu-six-mask-profile-v7-disk` completed with exit zero and preserved
the original model, source and cache manifests. Mask-contact handlers took
1–3 ms and profiled live paints 53–69 ms; the release handler took 1.25 ms and
its first paint 456.7 ms. These are instrumented hidden-widget timings, not
physical input latency or final acceptance. Both painted and unpainted six-effect
exact waits still exceeded 120 seconds. The saved four-effect state converged in
33.405 seconds with all 50 published tiles byte-identical on recapture.

Private predecessor assembly now made finite, monotonic progress: all 683
painted-state worker starts had unique scope/key pairs, no private assembly was
evicted, and coverage reached 1036 of 1103 reserved addresses. Ordinary retained
aliases still recorded 23569 count-pressure evictions, but they did not erase
private dependency progress. This run is therefore not evidence of renewed
worker recomputation or a deadlock. It remains a failed latency/convergence check.

Planning ran 217732 times, with approximately 58 seconds cumulative inclusive
planning time. Thirty-four worker preparations of the same 5383-square bilinear
input all missed the preparation cache and converted in 9.365 seconds combined.
Its float32 RGBA representation is 463627024 bytes, exceeding the unchanged
256 MiB preparation budget. The GUI's own RGBA conversions totaled only 13.84 ms;
large preparation costs must not be attributed to the GUI from aggregate profiles.
The preparation fix promotes already-required complete CPU stages when that
native preparation would otherwise repeat, preserving native grids and all cache
budgets. Its focused pressure/assembly batch passed 125 cases. A separate exact
plan memo passed 113 correctness cases, but its actual Blueprint metadata
comparison regressed unique native requests from 15.75 to 42.94 ms per 160 calls
and three requests per region from 45.98 to 66.50 ms. A small identical-request
gain did not justify those costs. The experiment was removed and archived;
production keeps the original stage-plan builder. Frozen native retiming is
still required.

The requested independent cold-reference child in v7 returned stale before
rendering its patches because its document was captured before configuration
advanced the fresh service revision. It has no valid native-pixel result. The
harness ordering is corrected, and mask runs now require their settled painted
snapshot rather than falling back to the unpainted state.

A clean frozen v7 MainWindow Add probe returned in 603.65 ms, reached its exact
Posterize palette in 23.402 seconds, and matched a separate native palette
sampler. Its largest GUI gap was 248.40 ms. Inspector refresh took 67.25 ms and
created one card, compared with 216.9 ms rebuilding six cards in v6. These
hidden-window probes suppress full canvas painting and still fail responsiveness
acceptance. Separate leaf instrumentation attributes the cold context request's
451.6 ms chiefly to effect bounds (448.6 ms); a cold numerical import is a
hypothesis requiring isolated verification. Native completion still performs
large image copies/fills. That attribution run overlaps native UI startup and is
excluded from clean timing comparisons.

The visible v7 pass selected the rewritten chapter, soloed the blueprint, moved
it, and verified toolbar Undo/Redo restored the expected geometry. It enabled
all six existing effects, scaled and undid the scale, added a cage modifier,
dragged a point, canceled with Escape, and left modifier editing. It restored
all layers, created a rectangle and a nested circle, enabled the rectangle's
compound flag and selected circle subtraction. This copy saved and closed
normally. Exact refinement stayed slow, and the new circle's selection/tree
refresh appeared only in a later observation. Functional actions do not establish
acceptable timing. Ctrl+Z did not undo the translation in this session; the
isolated settings preserve the user's existing Undo binding of Ctrl+X (Redo is
Ctrl+Shift+X). Toolbar history worked; the configured key still needs a native
check.

The Rectangle gesture also exposed a confirmed input-routing issue: dragging
inside a selected free image transformed it instead of creating a new layer.
Creation succeeded outside the selected image's controls. Creation tools now
leave selected-object transform controls inactive so their own drag handlers
receive the gesture. Forty-two actual pointer/model/history cases passed for
selected images, rasters and vectors, ordinary shape siblings and compound
children, while preserving ordinary Select/Transform and pencil handles. A
native check and the affected existing input suites remain required.

Cold bounds attribution also verified that importing the distortion module
unnecessarily loaded SciPy's interpolation package for the blueprint's Deform,
Twirl and Lens bounds. The interpolator import now occurs inside the unchanged
curve operation. In separate processes after MainWindow class imports, the same
saved native bounds changed from 283.36 to 28.29 ms on first use, with identical
geometry and no interpolation package loaded by that bounds call. Warm bounds
were below 2 ms in both processes. This metadata-only attribution does not
measure a complete Add action or physical input latency. The native distortion,
expanded-bound, preparation-cache and floating-spatial suites passed 415 checks
in 50.22 seconds (`lazy-curve-import-regressions.xml`).

## Frozen v8 failure and cache-adoption cause

The next frozen native run, `blueprint-gpu-six-mask-profile-v8-disk`, exited
normally after 291.44 seconds and preserved source, input/model and read-only
disk-cache manifests. Both six-effect checks again exceeded 120 seconds. The
saved four-effect state converged in 23.349 seconds with all 50 published native
tiles identical on recapture. The painted-only independent child correctly
reported unverified because no settled painted snapshot existed; it did not
substitute the saved state.

The outgoing-preparation guard did change the actual Lens demand. Nevertheless,
the identical full Lens scope/key was later computed three times, taking about
24.0, 21.4 and 22.0 seconds, plus a canceled attempt. Painted-state work included
350 starts and 323 unique scope/key pairs, so 27 extra computations are real
repeated work. Private coverage reached 1207 of 1227 addresses and 62 projection
tiles were valid, but the complete current viewport was still unpublished.
Retention telemetry shows both byte and record pressure. Its protected-storage
peak was only 76.7 MB; this does not prove that a protected Lens result was
unprotected by the shared-budget limit.

The exact preflight path supplies the more direct cause: TileGraph accepts a
completed `cached_output` result into its per-request map without calling its
ordinary cache adoption callback. A promoted full predecessor therefore does
not acquire the protected graph alias that a freshly computed result receives,
and its unprotected raw/stage entries can be evicted before downstream requests
finish. Successful exact preflight results now use the ordinary graph handoff,
existing key, admission budget and alias-retirement rules. Cache-only evaluation
remains read-only, and the private context is validated after the callback.
A bounded-pressure regression reproduces two predecessor computations before
this correction and one afterward in byte, float16 and float32, with identical
output bytes. The affected native tile, pressure, assembly and retention batch
passed 198 checks in 113.38 seconds. Real-project acceptance remains pending.

Instrumented live contact regressed in v8: the last paint took 180.9 ms and the
release paint 721.6 ms. No new exact jobs were submitted during contact, but two
earlier jobs remained active. Their possible CPU/memory contention needs the
owning-thread and worker traces; handler speed alone is not responsive feedback.
This run remains rejected as performance acceptance.

## Frozen v9 failure: protected-frame priority and source admission

The v9 primary exited normally after 303.28 seconds. Both six-effect states
again exceeded 120 seconds; the saved state converged in 32.606 seconds with
50 unchanged publication tiles. Source/input/read-only-disk guards passed, and
the independent painted reference remained unverified because the painted
state did not settle.

The new trace confirms that the preflight fix admitted the large Lens output
and retired its raw worker alias. Later, ordinary regional tile admissions
reached a shared-protection limit and unprotected that complete frame at 268.409 MB
total and 131.147 MB protected storage. Byte pressure then removed its graph
alias. The incoming allocation/shared-record count was not captured at that
unprotection, so its precise byte-versus-record trigger is unknown.
The same key/input was computed again: 23.14 and 23.18 seconds for the
two timestamped Lens jobs. The detailed retention events establish their
ordering, rather than absolute event times. This is evidence for a priority
correction within the unchanged 256 MiB/512-record pool: only complete
`shared_frame` graph owners need protection; individual tiles can use ordinary
LRU and existing private coverage. A scaled pressure regression reproduces
two prefix computations with the old policy and one with the selective policy
in all three native formats. Real-project verification is still required.

Live contact remained unacceptable (last paint 340 ms, a prior sample 655 ms,
release 729 ms). During the slow sample, the completed original Blueprint
decode entered the ordinary effect LRU as an 88.519 MB image, shrinking 111
entries/31.124 MB to one entry. That admission displaced compact image-prefix
previews; the last contact profile spent 141.9 ms inside a neighboring mesh
map. Lens completion occurred after contact and is not the immediate cause of
this flush. Original-image handoff already has bounded worker retention and
validated ImageStore adoption, so a duplicate effect-LRU alias is unnecessary.
Completions with both the original-decode scope and preview-handoff key now
bypass the effect-LRU write, keeping retained admission and guarded ImageStore
adoption. Actual decode/adoption pressure tests preserve the compact mesh prefix
without another mesh pass or owner-thread re-decode, including a tiny retained
budget and a frame above ImageStore residency. The decoder, ImageStore,
live-prefix and parallel batch passed 85 checks in 6.62 seconds. Selective graph
protection passed 84 guard/prefix/outline and 159 native tile/pressure checks.
No separate preview pool or increased cache budget was added.
The large-event telemetry itself took less than 0.2 ms
during the slow contact; clean matched timing remains required.

No OS working-set/private-memory sample was captured for v7-v9. Cache byte
counters and worker admission estimates cannot establish paging or resident
process memory.

## Clean v10: convergence and an independent painted reference

The clean v10 primary used GpuCanvasWidget on Intel Iris Xe/OpenGL 3.3 at
DPR 1.5, with all six Blueprint modifiers enabled in memory and a newly
painted HSL intensity mask. No monitor, profiler, planning/job instrumentation
or process telemetry was enabled. Mask handlers returned in 0.7-1.7 ms;
live paints took 33.2-40.7 ms, while release still took 193.1 ms. Painted,
restored-all-six and saved-state batches all settled and verified 50 native
tiles after 84.25 s, 72.96 s and 24.78 s respectively. These settlement times
remain a performance problem. Synchronous publication recaptures retained
completed caches and took 90.62 s, 90.49 s and 13.50 s; these are separate
validation costs, not release latency or independent correctness references.

A fresh separate-process synchronous reference began with empty scene,
effect, source, retained and private caches and canonical nondeferred demand.
It matched every original-format byte and metadata field for three painted
256x256 native patches, stored as 260x260 images with gutters: both stroke
endpoints and a warped edge outside the original image quad. Each patch
contained 57,335-62,075 visible nonwhite pixels. This independent comparison
covers those patches, not the full chapter. Reference capture took 35.05 s
and exited 0. The 440.93 s primary also exited 0; all model restoration,
production-source and native-input guards passed, as did the 4,112-file
read-only disk index/content guards. That disk context had zero usable
current-version entries, so this is not warm-disk performance evidence.
Physical pointer-to-display delay was not measured by the hidden harness.
Evidence is in `blueprint-gpu-six-mask-clean-v10-disk/proof-summary.json`
and its raw results under `.artifacts/night-performance-20261006`.

The separate frozen-v10 visible Computer Use pass confirmed all-six image
translation/scaling and the configured Ctrl+X / Ctrl+Shift+X history keys.
Creating a rectangle inside the selected image produced a new layer without
moving the image. Making it compound, creating a circle child and choosing
Subtract produced a visible hole; moving the child moved the hole, and Undo
restored it. Dragging the six-effect image into that compound updated its
tree parent and clipped its visible artwork; Undo, Redo and a final Undo
restored the original hierarchy. A free rectangle corner changed visibly
and was restored by Undo. These are qualitative native feature checks, not
timing acceptance: affected automated CPU checks overlapped this UI pass.
The same native pass visited six navigator positions spanning the chapter,
including the first and final panels, and scrubbed the navigator continuously
in both directions. The final cold view initially showed a blank canvas and
then its artwork; the return visit showed a coarse preview immediately.
These screenshots establish eventual display, not exact settlement or
physical input latency. A new HSL intensity mask was attached, painted and
checked with Undo/Redo, then its stroke and binding were undone. Twirl's
Angle control changed from 209.93 to 55.86 degrees and Undo restored it, but
the composite remained visually unchanged/coarse with Preparing artwork
after about 20 seconds. Current-model preview availability needs a dedicated
capture; this is an outstanding responsiveness concern. Saving the private
copy briefly showed Windows Not Responding in a screenshot, recovered on
the next observation and cleared the dirty marker. No save duration was
captured. The editor closed normally and its process exited before the full
nonexternal suite began.

A whole supplied-project rehash after this native UI session matched all
14,317 original files, with no changed, missing or added files, including
existing render-cache files. The read-only guard took 13.30 seconds while the
regression suite was active; that duration is not a storage benchmark.
Evidence is `supplied-project-full-verification.json` in the artifact folder.

## Subsequent native-demand changes under validation

Five proved CPU warp families now share completed 512-pixel output batches
when deferred exact source preparation exceeds the existing 256 MiB cache.
The canonical 256-pixel document addresses, semantic dependency keys, original
source grids and output precision remain unchanged. Each graph child owns a
completed crop while siblings can reuse the raw batch. A separate change skips
an unused padded base only for full-strength, unmasked deferred CPU warps;
masked, partial-strength, synchronous and unsupported paths retain their base.
Focused checks include native 8-bit, float16 and float32 bytes, parameter masks,
affine/projective mappings, source/history/cancellation and bounded retention.
All 82 output-batch cases passed in the ongoing whole-suite run. No real-project
speedup is claimed until a quiet matched primary capture completes.

The interrupted whole nonexternal run also exposed a repeated-window setup slowdown:
three read-only stack samples at 68–82% show `QApplication.setStyleSheet` in
`MainWindow._build_ui`, with all four effect workers idle. This identifies the
sampled regression-run pauses, not the heavy artwork delays. An identical-style
no-op guard is now installed. The run reached 93% with 15 failure markers, but
its process and execution handle disappeared before a terminal report or JUnit
file was produced. The cause of that interruption is unknown. It is not a
completed 5,952-case result. The 15 recorded cases were reproduced separately;
the subsequent focused batch passed 302 cases with nine native OpenGL skips.
A complete final-source run is still required.

## v11 cache-context correction and source preparation

The clean v11 primary exited normally after 302.70 seconds. Painted, restored
six-effect and saved four-effect states verified 50 native tiles each after
42.265, 42.415 and 24.662 seconds. Every one of those native tile hashes matches
its v10 counterpart, and a fresh independent three-patch reference also matched
every native byte and metadata field. Source, model, native input and read-only
disk guards passed. Release painting took 461.2 ms and paint Undo 694.9 ms;
these remain unacceptable even though their handlers returned in 1.2 and 8.0 ms.

These settlement durations are not a matched isolation of output batching.
The supplied original index already declares `native-artwork-4`, with 4,323
entries. Its original SHA remains
`ebfcb69184deb94a484e1c4536fc5616a8a42f88344750d3f2bd770f10fca301`.
The v10 renderer rejected that index, while the v11 version accidentally
collided with it and admitted 4,323 entries. Neither v11 hits nor loads occurred,
but misses performed dependency-key work. The next shared renderer namespace
must reject both known earlier versions and retain the ordinary memory/disk
pipeline. Native equality evidence remains valid; the apparent timing improvement
cannot be attributed solely to batching.

The separate instrumented v11 diagnostic exited normally after 330.44 seconds.
Its release first paint spent 454.2 ms inclusively in 50 disk tile lookups, of
which 447.2 ms was projection-key work; these times overlap. Its 965.9 ms release
and 1,347.2 ms Undo are diagnostic timings, not clean latency measurements.
Undo also evaluated bounded current-model draft Deform/Mesh kernels on the GUI
thread, including roughly 101 ms in triangle mapping. That is a remaining
live-preview cost, not evidence of a synchronous native exact fallback.

The diagnostic recorded 344 conversions of the 5,383-square predecessor to a
463,627,024-byte float32 working array. All exceeded the unchanged 256 MiB
preparation cache. Their 57.070 seconds include synchronous publication validation:
56 worker conversions account for 9.108 seconds and 288 apparent GUI-thread
conversions for 47.962 seconds. This total must not be described as input delay.
The painted asynchronous phase separately used one Lens job (10.693 seconds),
28 Pinch jobs (8.091 seconds), and 95 neighboring Slam mesh-region jobs
(20.381 seconds). The latter remains a separate settlement hotspot.

An isolated native-source experiment used the diagnostic's actual Pinch input:
115,906,756 original-format bytes, SHA
`afeff89daae18a1f5dae2ed8116a580e4dfb4b06c203e231b85346e8b023e003`.
Across three alternating-order passes, six fixed 512-pixel native outputs took
1.475 seconds median with complete source preparation and 0.641 seconds with
bounded preparation of the original integer source taps. Converted pixels
dropped from 173,860,134 to 1,939,062; the same preparation cache retained
31,024,992 bytes. Every output hash and native metadata field matched. A separate
60-case native kernel comparison, fractional/boundary/invalid-coordinate checks,
conservative mode gates and COW input-isolation proof also passed. These are
isolated kernel measurements; full-editor responsiveness is still pending.

The implementation admits only native ARGB32-premultiplied, exact scale-one,
bilinear transparent/white sampling in five CPU warp families. It includes every
original floor index and adjacent tap, normalizes source pixels before the
unchanged interpolation, and verifies exact float64 coefficient bits after
integer rebasing. Unsupported modes, float contracts, oversized/empty crops and
unsafe rebasing retain the complete preparation. Numeric crops use the existing
bounded cache, and the Qt handle remains job-local. No source pixels are resized,
no new cache pool is added, and artwork keys/native output addresses stay intact.
Integration, cancellation, history, mask and final independent reference checks
must pass before this constitutes acceptance.

The production sampler was separately compared with the frozen v11 kernel using
the same raw native input. Three alternating passes again matched every checked
output hash, with median 1.571 seconds before and 0.683 seconds after. The first
combined integration run exited 0 with 309 passing progress markers; the new
sampler's standalone JUnit run recorded 75 passes before a final additional
foreign-format guard was added. Final-source joint validation remains pending.

Finite capture-local geometry tokens now retain typed float values with an
explicit signed-zero discriminator, and identify ordinary containers before
checking dataclass metadata. They remain ephemeral; the original serialized
settings and durable cache identities are unchanged. An isolated five-pass
experiment on the actual six Blueprint modifier records reduced every tested
metadata workload: hot settings 32.69 to 26.15 ms, unique settings mutation
42.21 to 35.84 ms, unique settings/bounds 67.93 to 62.67 ms, and the triple
settings/bounds workload 221.09 to 180.97 ms. Adjacent floats, signed zero,
types, mutable nested fields, nonfinite values, cycles and conservative retained
byte estimates were checked. These measurements do not establish GUI latency.

Native Float source/export proof found a correctness defect outside the supplied
legacy chapter: straight RGBA64 sources with alpha 1 became transparent through
the old ARGB32 scene edge, and straight RGBA8 color channels rounded during
integer premultiplication. Floating chapters now normalize native source
channels into owned float storage before their explicit color/composition edge,
through the existing source worker and unchanged ImageStore budget. Legacy
chapters, including temporary internal float scopes, retain their original
source route. Actual scene and full/cropped PNG export comparisons preserve the
required low-coverage colors and native source/profile bytes in float16 and
float32. The application export now uses the chapter's working format and
quantizes straight native 16-bit output once at the existing export edge.
The combined source/visibility/persistence batch passed 186 cases; 84 precision
checks passed after the final null-image preservation change.

The oversized ordinary effect alias also had a reproduced live-preview cost:
a real exact worker completion evicted an unchanged compact Twirl/Mesh prefix,
so late HSL mask Undo rebuilt it. An oversized ordinary RAM alias is now omitted
only when the exact semantic key, native format/size and Qt storage identity
already exist in bounded retained storage. The ordinary exact disk write still
runs. Graph admission precedes that ordinary alias; raw worker consumption
occurs only after successful graph admission. Rejection retains the prior
recoverable route. No quota, pool or cache limit was increased. Real native image,
mask-stroke and TilePatchCommand Undo tests now perform zero extra prefix warps
and match a forced fresh draft. The affected batch passed 318 checks.

The neighboring Slam mesh phase contained 95 job starts but only 62 distinct
semantic scope/key pairs and output rectangles; 33 starts recomputed existing
demand. Requested mesh regions now copy completed canonical tiles into the same
private EffectJobs region pool before retrying a pending dependency. Incomplete
coverage stays private and is never returned or recorded as exact. Native
8-bit/float16/float32 oracle, bound-mask, budget/count, cancellation and current
context tests passed (25 new cases, 83 combined). The observed job reduction and
clean editor latency still require the next native capture.

## Clean v12: matched cache context and verified native output

The final-source joint batch passed 241 cases in 25.75 seconds. A subsequent
injected source callback proved a precision-change race: a float32 result could
finish exact after the same chapter changed to float16 without changing source
or history. Source adoption now rechecks the actual chapter policy, and the
scene backend rejects the obsolete document contract. The two regressions and
surrounding source/scene checks passed (76 cases, 4.308 seconds).

The v12 runtime was frozen with all 255 source/resource files matching the
checkout and saved manifest. Its clean primary exited 0 after 198.643 seconds,
using the same Iris Xe/OpenGL 3.3 widget, DPR 1.5, 480x687 viewport, document
scale, source project, chapter, six active modifiers and painted mask workload.
Every instrumentation flag was off. The supplied earlier cache index was
rejected by the unique shared epoch, restoring v10's zero-entry lookup context.
There were no disk hits, loads or reads.

| State | v10 exact settle | v12 exact settle | v12 publication recapture |
| --- | ---: | ---: | ---: |
| Painted six-effect mask | 84.252 s | 30.683 s | 38.402 s |
| Restored six effects | 72.955 s | 34.415 s | 38.728 s |
| Saved four effects | 24.782 s | 16.387 s | 9.890 s |

Each state verified 50 current native tiles with zero differing bytes. All 150
full native hash and metadata records equal their v10 and v11 counterparts.
A fresh separate-process canonical reference began with empty caches and
matched all original-format bytes and metadata for the same three painted
patches; it took 14.278 seconds to capture and exited 0 after 17.483 seconds.
Model restoration, frozen/current runtime, native source inputs and all 4,112
read-only disk files/index passed their guards. This remains patch-level
independent correctness evidence, not a full-chapter independent render.

Live contact took 60.626 ms, with drags 57.017-60.485 ms. Release handler time was
0.646 ms but its first paint took 197.483 ms. Paint Undo took 0.368/349.403 ms
(handler/paint), and binding Undo 79.545/645.573 ms. These delays still reject
responsiveness acceptance. Physical pointer-to-display delay was not measured.
The separate v12 diagnostic completed with exit 0 in 199.938 seconds, without
pinning the earlier raw Pinch image. Its current native tiles, three cold
reference patches, source/runtime and read-only cache guards all passed.

## v12 attribution and the next fixes

During each mask contact, seven compact live prefixes were found and two were
created. Exact settlement then evicted all 21 initial live-prefix records under
ordinary small-region pressure: the first evicting writes were 262,144 and
67,584 bytes. These were not duplicate oversized Lens aliases. The 21 distinct
Qt pixel allocations totaled 6,370,460 bytes. The next candidate gives recent
eligible live prefixes bounded eviction priority inside the existing ordinary
64 MiB LRU, with an 8 MiB ceiling and 64-record limit. It creates no second pool
and allows exclusive large exact work to displace that priority.

The mask release did not flush new pixels. The runtime mask revision and first
16 native tile-version records matched the last contact, while the serialized
mask revision and document revision advanced. Nevertheless, the release
recaptured its interaction preview in 108.121 ms inclusively. A candidate may
reuse the already presentable provisional contact image only after independently
proving that its complete effective model, native mask pixels, source inputs,
history, pixel contract, camera and rendering configuration remain current.
Any unproven dependency or changed pixel falls back to a fresh preview. Such a
reuse remains transient and provisional; it cannot become an exact disk entry.

This contact-proof candidate was rejected before the next native freeze.
Although a pure typed model snapshot alone cost about 11.6 ms, the complete
effective-state check on the supplied chapter, including 1,983 tile versions and
backing/source stamps, cost 34.6, 27.9 and 59.6 ms in three warm probes. Proving
the frame before and after each contact would therefore add approximately
56–119 ms to a contact already costing about 60 ms. Saving a release recapture
does not justify that live-input regression. Its production hooks were removed;
the experiment remains diagnostic evidence, not an accepted optimization.

The v12 first paints were instrumented: commit 329.747 ms, paint Undo 493.807 ms,
and binding Undo 886.714 ms. Commit's fingerprint walk made 40,938 calls and
cost 52.496 ms exclusively (72.226 ms inclusively). Paint Undo included 45.109
ms exclusive mesh-triangle work, and binding Undo included shape construction,
framebuffer readback and a fresh preview. Inclusive costs overlap and must not
be added. These diagnostic timings are separate from the clean timings above.

Native bilinear preparation now consumed 1.081 seconds across 887 calls,
including synchronous verification, with 68 admissions and zero over-budget
preparations. The largest admitted working array was 41,015,808 bytes. The v11
diagnostic had 57.878 seconds across 428 preparations and 344 oversized
rejections. The increased call count reflects bounded native crops, not an
increase in derived artwork resolution. These aggregate costs are not GUI
input latency or an isolated OS memory benchmark.

Per-target mesh assembly did not eliminate overlap between different requested
rectangles. The painted phase still started 94 mesh jobs for 62 distinct
semantic scope/key/rectangle pairs (32 repeated starts); the restored phase
started 103 for 69 pairs (34 repeats), and the saved phase 90 for 68 (22).
Painted mesh computation fell from 20.381 to 13.233 seconds, but the next
candidate must share proven canonical native tile coverage across targets
inside the same context-checked EffectJobs pool. Partial full frames stay
private. A detached requested crop can leave that pool only when every native
grid address it covers is complete. The full-frame allocation and coverage
metadata must fit the unchanged pool budget and existing pixel cap; larger
frames retain the prior per-target route. Actual repeat reduction remains to
be measured in the next capture.

## Frozen v13: exact output preserved, broader interaction still slow

The v13 checkpoint contains 255 source/resource files. It adds bounded eviction
priority for compact prefixes inside the existing ordinary LRU and shares proved
native mesh coverage across requested rectangles inside the existing worker
pool. The affected joint batch passed 103 checks. Neither change adds a pool,
increases a quota, changes exact sampling, or admits partial frames to disk.
The mesh repeat-count improvement still requires a separate causal capture.

One preliminary clean launch accidentally omitted the explicit frozen source
root after an artifact-plan overwrite. All 255 current files then equaled the
saved snapshot and its pixel/preservation checks passed, but that launch does
not prove a frozen import. It is retained with that limitation. The plan and
preflight were corrected, and the explicitly frozen clean run below was repeated.

The proper frozen v13 primary exited 0 in 210.9995 seconds with instrumentation
off, the matched Iris Xe/OpenGL viewport, zero readable earlier cache entries,
and no disk pixel reads. Painted, restored-six-effect and saved-four-effect
states settled in 28.754, 33.575 and 19.099 seconds respectively. Each verified
50 current native tiles with zero byte differences; all 150 hash/metadata
records equal v10 and v12. A separate empty-cache process matched the same three
painted native patches byte for byte, capturing in 17.460 seconds and exiting 0
after 20.141 seconds. Source, model, frozen runtime and all 4,112 read-only cache
files/index passed their guards. These are patch-level independent comparisons,
not an independently rendered full chapter.

The first mask contact painted in 23.456 ms, but its first drag took 213.356 ms;
the following drags took 56.161–62.304 ms. That first-drag outlier remains
unattributed and is included in the result. Release first paint took 174.702 ms,
paint Undo 173.789 ms, and binding Undo took 83.811 ms in the handler plus
552.273 ms to paint. Responsiveness acceptance still fails. Exact settlement,
handler work and first paint remain separate measures.

Nine guarded native GL cases recorded seven passes and two radial interaction
failures. The failures assert that angular integration must not execute on the
GUI thread and that intensity-gradient editing must reuse the full-quality
integration. The current compact preview route violated those expectations
before ordinary exact collection. A revised gate is being checked against
strict native-pixel and current-publication tests; the two failures have not
been counted as passing acceptance.

A matched cold actual MainWindow Add run returned in 186.2 ms. Its palette
completed asynchronously after 14,394.569 ms and matched the warm native oracle
(16.472 ms). The largest event-loop heartbeat gap was 133.353 ms. The palette
requires native upstream effect preparation before statistics downsampling;
this result establishes neither an instant palette nor physical input latency.
The private clone, frozen imports and before/after manifest guards passed.

The saved Blueprint broad run exited 0 after 47.245 seconds. Its 67 operations
covered selection, solo, modifier mute/parameters, pivot editing, scaling,
reparenting, opacity masks, compound subtraction, quad warping, Undo/Redo and
actual Escape cancellation. The saved model was restored and all 45 checked
current native tiles matched a synchronous recapture with completed caches
retained. That recapture is not an independent cold reference. The phases
originally labelled translation dragged the pivot rather than the artwork;
their 133–150 ms timings do not establish translation performance. Scaling
painted in 196–206 ms, parameter drags in 193–204 ms
and quad warping in 190–196 ms. Solo's first paint reached 555 ms, and several
history/hierarchy changes took approximately 240–327 ms. These delays require
further phase-specific attribution; successful restoration does not establish
responsive editing.

The all-six-active broad fixture then failed complete model restoration. A
terminal diagnostic localized the first divergence to the supposed translation
Undo: the pivot gesture had created no artwork command, so the helper undid the
earlier stress-stack activation and muted Pinch and HSL. All captured assertion
snapshots matched detached diagnostic snapshots. This identifies a benchmark
gesture/history error rather than a production Undo defect. The corrected
helper must prove its drag kind, changed artwork geometry, exactly one own
history command, and preservation of prior commands. Earlier native Computer
Use visibly moved the artwork, but supplies no clean measured latency.

The corrected all-six-active R3 run exited 0 after 143.336 seconds, with 70
required phases, frozen v13 imports, unchanged native inputs, all three
meaningful transform/Undo checks, and real translation Escape cancellation.
The complete saved model was restored. Both the active and saved states had
45 current native tiles with zero byte differences against retained-cache
synchronous recaptures. Active exact settlement took 30.696 seconds, followed
by a 59.363-second synchronous recapture; saved exact settlement took 15.089
seconds and recapture 10.634 seconds. These recaptures are not independent cold
oracles and are excluded from live-edit timing. Genuine movement first paints
took 226.733–289.432 ms, scaling 225.484–232.802 ms and free quad warping
233.075–238.136 ms. Movement commit took 10.727 ms in the handler plus 175.362
ms to paint; Undo took 63.454 plus 288.648 ms. Solo's first paint reached
556.499 ms. Correctness passes; responsiveness still fails.

The corrected saved-four rerun exited 0 after 48.177 seconds with all 67
required phases and meaningful translation/scale/warp/history guards. Movement
first paints were 190.938–193.638 ms; commit was 8.048 ms plus 119.431 ms to
paint, and Undo 54.063 plus 275.982 ms. Its 45 current native tiles matched
the retained-cache recapture with zero byte differences (13.970 seconds to
settle, 9.768 seconds to recapture). The subsequent 18-phase genuine transform
profile also passed its strict terminal/import/input/meaningfulness guards.

Raster38's next corrected GPU broad run exited 1 after 20.444 seconds. Its
individual translation/scale/warp commit and Undo guards, modifier history and
real Escape cancellation passed, but final complete model restoration failed.
No final exact or cold-patch acceptance is claimed for that run. A detached
per-phase model diagnostic is being prepared to localize the first divergence;
the cause was not attributed before that diagnostic.

R4 reproduced the failure (exit 1, 33.315 seconds) with 193 detached checkpoints,
all original assertions preserved, and unchanged imports/source inputs. The
first persistent difference appeared immediately after target selection,
before paint: the containing layer's `last_raster_id` changed to Raster38.
Every completed group and the final checkpoint contained only this remembered
selection field; there was no remaining artwork or history difference. Ordinary
selection intentionally updates that editor metadata. The fixture is being
corrected to restore prior remembered/active selection through the ordinary
selection API, with command-position checks and the complete model assertion
retained. No production change is warranted for this normal behavior.

Raster38's R5 correction passed the full strict run (exit 0, 163.510 seconds,
65 phases). Prior remembered raster IDs were restored through ordinary selection;
the active target/tool, complete model and command identities/revision passed
their guards. All 50 current native tiles had zero differences against the
retained-cache recapture (58.151 seconds settlement, 45.154 seconds recapture).
A fresh separate process then verified three nonempty artwork patches with
empty derived caches and zero native byte differences; its wall time was
39.879 seconds. These independent cold patches add kernel evidence for those
locations, not an exhaustive whole-chapter pixel proof.

The saved Room GPU broad run exited 0 after 138.594 seconds. All 67 phases and
strict preserved-source/model/history/transform/mesh-control guards passed.
The active 16-point Mesh Warp, Twirl and Kuwahara stack plus existing mask were
exercised. Its 45 current native tiles had zero differences against the
retained-cache recapture (56.488 seconds exact settlement, 55.218 seconds
recapture; no independent cold oracle). Movement first paints were
291.079–300.114 ms, scaling 289.997–299.840 ms and parameter edits
296.647–307.145 ms. Mesh-control edit took 27.760 ms plus 259.589 ms to paint;
Undo took 27.927 plus 379.563 ms. Responsiveness acceptance fails despite the
correctness pass.

A separate 18-phase instrumented transform run completed with immutable v13
imports and preservation guards. Its translation-labelled phases have the same
pivot limitation. Genuine scale first paints took about 272 ms under profiling;
native metadata fingerprints cost about 15.5 ms exclusive and 21.3 ms inclusive,
with additional attribute lookup, SciPy work and GPU readback. Scale Undo took
392 ms, including four gradient conversions and fresh geometry work. These are
profiled attribution measurements, not clean input-latency results; inclusive
times are not added together.

Later source changes are tracked as the future v14 candidate and do not alter
the immutable v13 checkpoint or its evidence. Remaining work includes broader
heavy-object GPU/raster cases, full chapter navigation, current-model Twirl
evidence, the final native Computer Use and Save pass, final-source regression
checks, fresh native acceptance, and the original full-file manifest guard.

## v14 candidate: history and semantic source isolation

Focused metadata Undo now preserves only eligible completed Legacy original-
image draft prefixes inside the same ordinary LRU. It retains their unchanged
keys as bounded COW handles after synchronous restore notifications, with
chapter/store/contract/dimension/history checks. Float and fitted images remain
excluded; normal source/preparation/worker cancellation still runs. A fitted
image correctness regression also reproduced stale source pixels after a
subpixel parent-bound change despite an unchanged aligned capture frame. Its
ordinary source signature now includes the complete unrounded destination quad.
The focused 31-case batch and 56 directly affected checks passed, including
notified ordinary/compound-parent edits in RGBA8, float16 and float32. Native
frames match cleared references, and original source bytes remain unchanged.
These correctness checks do not establish the resulting Undo latency.

A separate five-pass alternating metadata experiment validated 268 complete
typed token graphs and nine actual modifier records, including unsignaled
mutation, cycles, exact byte estimates and serializer normalization. Removing
redundant field-name/pair wrappers from ephemeral dataclass tokens reduced all
seven workload medians by 35.97–40.66 percent: repeated settings/bounds went
50.942→31.931 ms and the triple workload 163.727→98.870 ms. The candidate keeps
the same type, fixed field order, current value snapshots and conservative
memory charges; serialized semantic keys stay unchanged. This is metadata-only
evidence. The narrow integration passed 111 affected checks, zero
failures/errors/skips (47.315 seconds). It does not establish the resulting
GUI speed.

The radial preview gate's 76 offscreen checks passed after repairing one new
test fixture. They retain strict fresh-pixel, current-revision, mouse/pen worker,
mask-contact, cold-view and reentrant-cancellation assertions. Final native GL
validation of this candidate is still pending.

Actual document policy, scoped effect policy and the loaded OCIO configuration
now participate in the shared ordinary source, stage, translation and projection
identities. Processor LRUs remain bounded and key their current loaded config;
no file watcher or automatic reload was added. Default Legacy identities and
the 11-field Legacy projection configuration stay unchanged. Identity-only
inputs remain explicitly transient even when nested in a semantic alias.
The source-context batch passed 16 checks and the affected source/pixel/scene/
translation batch passed 158 checks.

A matched captured-source proof warmed a Legacy chapter under a temporary
float scope, then changed its actual policy. Frozen v13 reused different native
source pixels: four byte differences in each float16 case and ten/eight in the
two float32 cases. The v14 candidate isolates the keys and has zero native source
byte differences against a cleared reference in all four cases, while preserving
the encoded originals. Its shared renderer namespace is
`native-artwork-20261007-source-context-2`; earlier entries are read-only misses
through the existing cache pipeline. The joint namespace/source-context batch
passed 28 checks. Final native acceptance remains pending.

The bare-QPainter proof also revealed a distinct Qt limitation: drawing a
single float source pixel through a non-antialiased rectangle path quantized
its channels. Qt 6.11.1's raster implementation has a single-pixel shortcut that
reads an 8-bit `QRgb` before filling. This is consistent with the measured loss
of alpha 1/65535. [Qt's raster source](https://raw.githubusercontent.com/qt/qtbase/v6.11.1/src/gui/painting/qpaintengine_raster.cpp)
supports that attribution. Ordinary document scene painters enable
antialiasing, so this default-QPainter reproduction is not yet proof of a
production editor/export defect. An AA-aware overload and real Image/Raster
service matrix then ran separately from performance workloads: 1,536 Qt cases
and 80 actual exact-render/export cases both exited 0. AA-on and point/native
overloads preserved the checked native bytes. The actual generic modified
Image/Raster path drew through QPointF with AA enabled; its production precision
loss occurred earlier, in an ARGB32 source capture, not Qt's shortcut. Original
RGBA64 alpha 1 became source-cache alpha 0, then exact output/export alpha 0;
the unmodified Image route preserved it. Natural Raster admission also dropped
all 12 low-alpha RGBA16/float16/float32 cases before rendering, because its alpha
bounds were measured after RGBA8 conversion. Seeded isolation cases confirmed
that unmodified native Raster transfer preserved the original precision/HDR.
All original source bytes stayed unchanged and PNG exports matched their own
roundtrips; that roundtrip property did not make the lost pixels correct.
The narrow native-alpha bounds and contract-format Object/Layer source-capture
fixes passed 61 new checks and 232 affected checks on their first runs, with
zero failures/errors/skips (2.836 and 11.317 seconds). Wide raster decoding now
retains native channels inside the existing residency budget; Legacy8 decoding
is unchanged. Version-two alpha-bound index records distinguish the corrected
metadata. Matching old eight-bit PNG records remain lazy; old sixteen-bit or
unknown headers invalidate only their cached bounds and recompute through the
same native alpha scan when needed. Stale empty and undersized old-index reopen,
source bytes, padded strides, native subnormals, export and eviction are covered.

The new 80-case actual service proof exited 0 with all statuses exact: natural
Raster drops fell from 12 to zero, all native alpha and RGBA16 export oracles
matched, and stored/original source bytes and PNG roundtrips stayed unchanged.
Modified float32 neutral HSL retains its established tiny green-channel
arithmetic rounding (one to four differing raw bytes in 20 cases); no full-byte
identity is claimed for that operation. Float16 source-oracle cases matched
their native bytes. No blanket Qt antialiasing or sampling change is justified
by this evidence. Final frozen-source native acceptance remains pending.

The compact outline draft path now passes the canvas's existing 64 MiB
outline-distance cache to the same modifier kernel. It creates no new pool,
sampling rule, durable entry or alternate renderer. A five-pass alternating
isolated A/B measured 288.467→229.510 ms for its full repeated atlas workload;
EDT evaluations fell from 81 to six. A separate native-format run measured
14–26 percent full-workload gains in ARGB32/float16/float32, with at most
0.682 ms additional cold work across its three-source corpus. These atlases
are not captured first-paint inputs from the project. All 51 native comparisons
and 51 comparisons after forced cache eviction had zero byte differences.
The production integration passed 140 checks, including 24 actual-stack
handoff, alpha/mask/parameter mutation, exact independence and draft disk
exclusion checks, with zero failures/errors/skips (14.246 seconds).

A frozen-v13 focused size trace then completed in 36.089 seconds with strict
meaningful move/scale/warp, history, source and native publication checks.
All twelve drag phases had current interactive compositions and selected
modifier observations. Blueprint source drafts were about 167×198/199;
Deform/Twirl outputs were about 178×236/237, and per-block Lens drafts about
4×272/273 or 176/177×272/273. The following outline preparation remained
compact. Thus this captured workload does not support a claim of oversized
selected Blueprint draft allocations. Four document capture blocks, repeated
surrounding scene effects and composition remain candidates for its long first
paint. This metadata trace adds overhead and is not clean latency acceptance;
worker phase labels indicate observation time, not dispatch attribution. Its
settled current native recapture retains completed effect caches, so it is not
an independent cold-kernel oracle.

The R5 frozen-v13 broad GPU runs for curles and falling then exited 0 in
33.850 and 127.343 seconds. Both passed all 65 phases and strict meaningful
transform, model/history/selection restoration and source guards. Each settled
50 native tiles with zero byte differences against its current synchronous
recapture. Curles settled in 6.345 seconds and recaptured in 1.095 seconds;
falling took 60.265 and 46.049 seconds. Those retained-cache recaptures are not
independent cold references. Curles move/scale/warp feedback still took about
431–482 ms and its modifier feedback 450–455 ms. Falling move/scale feedback
took about 193–207 ms and modifier feedback 154–190 ms, with a 256 ms warp
outlier. These runs pass functional checks but fail the responsiveness goal.

The saved Blueprint raster-widget R5 run also exited 0 (45.209 seconds), with
all requested model/history/source guards and 45 unchanged native tiles. Its
genuine move/scale/warp feedback took about 179–198 ms; the first parameter
sample was 349 ms and the next 191 ms. Exact settlement/retained recapture was
15.117/9.864 seconds. This checks the separate raster presentation path; it
does not make that path meet the live-response goal.

The explicit frozen-v13 current-model Twirl probe exited 0 in 212.304 seconds.
Production slider signals changed angle 209.93→55.86 and ordinary Undo restored
it. Current INTERACTIVE old/new captures used the same bounds, density, format
and current request/document revisions. They differed in 341299 bytes (maximum
57), including 11889 bytes in the 128-world-pixel center patch (maximum 16).
The changed-angle preview carried revision 18 while the prior complete exact
view retained revision 4; exact work remained pending after 20.088 seconds.
This demonstrates changed current-model preview pixels, not changed-angle
native exact correctness or what the earlier full editor visibly presented.
The restored 209.93 state passed all 150 painted/restored/saved native checks
and three separate uncached patches. The cold process took 15.909 seconds.

The navigator-enabled six-landmark run failed its final hard check (exit 1,
76.186 seconds): after production Escape canceled a real transform, the
navigator stayed dirty or pending beyond 15 seconds. Model/command/preview
cancellation assertions passed before that failure, and all six navigation
native exact checkpoints matched. The navigator's individual dirty/timer/build
fields were absent, so the precise failing predicate is not established.
The hidden OpenGL QWidget's ordinary grab also produced uniform images; those
files are not evidence of the visible canvas or navigator. The failed artifacts
are retained and neither the assertion nor its time limit has been weakened.

The additive navigator diagnostic reproduced that failure (exit 1, 75.456
seconds) and passed a separate failed-run evidence guard. At the deadline all
gesture, mouse, pen, wheel and live-preview flags were false. The cached chapter
was current, but a 13×671 build repeatedly stopped after its first 32-row band:
554 visual invalidations/abandonments, 28 refresh attempts and 339 polls occurred
in 15 seconds. It ended fully dirty, with an active 106 ms timer and another
effect job running. Escape emitted zero interaction-finished signals, but a
held gesture does not explain this recorded failure. Derived completion
callbacks repeatedly restart the navigator; distinguishing completion updates
from actual document edits needs current-pixel publication checks before a fix.

The curles first-paint causal run exited 0 (20.317 seconds) with all 18 genuine
move/scale phases and current native checks passing. In one instrumented steady
scale paint (580.741 ms), two radial integrations cost 185.297 ms inclusive;
1824 SciPy geometric transforms cost 145.192 ms exclusive. Those counts include
the same radial work and must not be added. Smudge's 33 sample calls cost only
10.020 ms inclusive. The radial effect belongs to the neighboring `about to
get up Copy Copy.png` image, not the selected curles image. Its circular
intensity-mask gradient excludes generic Image draft-prefix reuse, even though
the integration is independent of intensity.

A separate bounded-input diagnostic exited 0 (17.641 seconds), retained all
native/source/history guards and recorded zero dropped events. Exactly two
full identities—current native source contents/format, evaluated angle field,
nine unrounded mapping coefficients, origin/output shape and source/pixel
context—were identical across all eight real move/scale samples. Twenty native
hashes cost 0.895 ms total (0.057 ms maximum); twenty angle-field hashes cost
0.078 ms total. This supplies project evidence for a bounded transient radial
integration reuse experiment. It is not a measured implementation speedup.

The separate compact-viewport experiment has not passed acceptance and is not
integrated. Its immutable v13 baseline completed in 105.325 seconds with strict
meaningful-transform and native guards. The artifact-only candidate process
completed in 103.163 seconds, but its strict guard rejected the first translate
sample: the ordinary service returned PENDING and a null image at the current
revision. All recorded context guards passed, which does not make the missing
image acceptable. Its apparent 31–45 ms translation paints therefore are not
accepted responsiveness gains. The independent live ROI comparison failed
after 4.425 seconds because there was no current composite to compare. Both
failures are preserved. Required dependency scope is still under diagnosis;
the current-pixel checks and hard deadlines have not been weakened.

The bounded radial reuse is now integrated in the existing ordinary modifier
LRU. Only explicitly bounded, provisional, non-navigator/non-exact captures
qualify. It hashes at most 1 MiB of current native source rows and 256 KiB of
the actually evaluated angle field, and retains at most a 1 MiB float32
integration per admitted output. The unchanged semantic integration identity,
all nine unrounded mapping values, origin/shape and actual/scoped/color context
remain dependencies. The base and intensity-mask blend are evaluated afresh.
The preview namespace cannot enter exact disk admission; no new cache pool,
sampling grid or exact worker path is introduced. All 55 new correctness cases
passed (4.30 seconds process wall, 3.543 seconds JUnit), with no failures/errors/
skips. The preserved initial failure mutated a different default-argument OCIO
cache instance from the renderer's explicit-path instance; correcting that
fixture required no production repair. Actual chapter speedup remains unmeasured.

The affected radial batch also passed all 178 checks (157.979 seconds JUnit,
zero failures/errors/skips). The Navigator now receives worker completion on
a separate derived-result signal, also consumed by Posterize/source handoff
checks. During a same-model provisional thumbnail build, completion coalesces
one full follow-up without abandoning completed bands or resetting an active
timer. Actual visual/document/hierarchy edits still abandon immediately.
Chapter/store identities, projection configuration/revision, dimensions,
background, contract and history are checked before and after each band.
The 65 new/existing scheduling checks and 115 affected source/float/opacity/
Posterize/retention checks passed (3.289/6.806 seconds JUnit), with no failures,
errors or skips. Native 15-second recovery and visible UI acceptance are pending.

The additive compact pending diagnosis is terminal. The candidate reproduced
its first-drag failure in 4.573 seconds; a separate failed-run guard verified
the trace without accepting current feedback or performance. It identified the
selected Blueprint's cold original decode, already running with the same key
from gesture begin, rather than a stale revision or an unrelated neighbor.
Its 739817644-byte job reservation is a conservative decode peak estimate;
actual resident memory was not measured by this trace. Decode residency held
only 27822080 bytes and did not include Blueprint. The original four-block
run completed in 107.151 seconds with native/model/history/source guards and
bounded restored observers, but all four first-drag blocks were also PENDING:
the selected stack returned no current output in any block. This is an existing
cold-acquisition gap. Those guards do not establish first-drag selected-pixel
feedback. No hidden warming, synchronous full decode or weakened oracle was
introduced. A bounded JPEG preview acquisition remains a separate proposal.

The current v14 production snapshot is frozen with 257 non-bytecode resources,
manifest SHA-256 `fa78043cf71385c82342c070dadf159f697fddd8964bd164ed26627e421fe968`.
Its clean/native/visible-editor/full-suite acceptance is being run separately;
prior v13 timings are not evidence for this snapshot. The original historical
14317-file baseline is pinned with corroborating 10205-source/4112-cache records;
the current pin does not claim an independently recorded earlier baseline SHA.

The first v14 clean primary run completed in 186.867 seconds (native PID 9356,
exit 0, one finished event). All 18 clean/source/cache/cold guards passed; all
150 native checkpoints matched the v10 and v12 references. Painted-mask,
restored-active and saved-stack settling took 29.007, 28.984 and 15.152 seconds;
their separate synchronous verification took 38.653, 38.547 and 9.267 seconds.
The independent three-patch cold child completed in 15.846 seconds with no
native differences. Live mask scene paints were 22.984–24.204 ms, commit
125.589 ms, paint Undo 113.159 ms, binding Undo 460.411 ms and activation Undo
588.642 ms. These paints are not proof of current selected artwork on a cold
first drag, and the longer history delays remain responsiveness failures.

R1's primary helper declares the frozen root and has unchanged full manifests,
but lacks terminal actual-module origin fields. Its main and cold-child paths
prepend the requested frozen root before importing Qt/editor code; a new R2
run will nevertheless record actual origins and hashes rather than infer them
from bootstrap order. The completed R1 artifacts and pins are preserved.

The nine native graphics checks pass on v14, including radial-handle recovery
and full-quality intensity-gradient reuse (previously the two v13 failures).
The guard verified 101 imported runtime files under the frozen root and all
257 resources against the pinned manifest, with zero failed/error/skipped cases.

The cold MainWindow Add check also passes, with actual frozen import origins,
unchanged clone/source metadata and the native oracle. Posterize's real first
return took 156.264 ms; its palette completed after 14.352 seconds while the
event loop remained available (largest observed heartbeat gap 137.689 ms).
The warm synchronous oracle took 16.054 ms. Deferred availability prevents the
long palette job from blocking Add, but does not make the palette itself instant.

The visible v14 editor exercise used the private rewritten chapter and its saved
Blueprint stack, including all six enabled modifiers. Genuine interior moves,
axis scaling, free corner warp, painted HSL intensity masking, solo, temporary
rectangle/circle compound subtraction and moving Blueprint into that compound
all changed the visible artwork. Undo/Redo restored the corresponding states;
all temporary editing commands were undone before closing. Six navigator stops
covered the opening, dialogue, falling sequence, application section and ending.
A Twirl slider release initially displayed the old detailed spiral with the new
value, then displayed the new spiral on a later observation. This remains a
current-frame handoff concern, not a claim of instant response. Computer Use
call duration includes capture/automation overhead and is not input latency.

A separate built-in monitor capture, `20261007-102812-83e291bdc7/capture.json`,
was stopped and saved through the actual UI. It recorded 263 navigator band renders
(31.200 seconds inclusive), 157 canvas paints (24.600 seconds inclusive), and
92 heartbeat stalls with a largest recorded gap of 908 ms. These nested totals
must not be added. Much of the interval was idle while the selected saved room
snapshot continued showing Preparing artwork. Its canvas move visibly shifted
the artwork by 24 by 17 display pixels and Undo restored it. Monitoring adds
overhead; these figures diagnose repeated work, not clean frame acceptance.
The private UI ended with native exit 0 and a passing actual-module/resource/
settings terminal guard. Its close prompt saved only the private restored copy;
this was not the separately instrumented Save measurement.

The v14 clean chapter navigator run fails its unchanged 15-second recovery
deadline. A new R6 diagnostic also fails; its bounded observers and initial
overlap with the closing private UI exclude clean timing claims. During R6
recovery, 212 derived-ready callbacks caused 68 band refreshes and three full
followups without a single abandon/invalidate callback. The pending context
remained current (document 19, history 3, stack 5), with row 128 of 671 pending
and its timer active at the deadline. The first 64 exact observed identities
were separate Blueprint native patches (42 Deform and 22 Twirl), each ready once,
with no observed exact restart after readiness. The cap excludes later jobs;
this does not establish global cache ping-pong. It does identify irrelevant
native completions restarting provisional navigator followups. A Navigator-only
dependency filter is being evaluated while ordinary canvas/Posterize completion
notifications and real document invalidation remain required.

The monitor's terminal projection remained incomplete (document revision 108,
presented 101), with 107686 render attempts and 106129 incomplete attempts. All
92 captured heartbeat stalls occurred without an active gesture. The last 1.42
seconds alone contained eight navigator bands and five canvas paints without
input. Ordinary effect-cache misses were 86907 versus 1788 hits, with 67031576
resident bytes near its unchanged 64 MiB budget. Four repeated sampled native
Twirl jobs belonged to the second Room snapshot, ID
`69a3bc1cc3ac4300af9ae3785243b3ac`, at 1659 by 2296 pixels; the selected first
Room snapshot exercised in Computer Use was a different Mesh/Kuwahara object.
These observations support a convergence problem and repeated native work,
but the sampled trace alone does not establish all cache-key dependencies.

V14 primary R2 completed separately in 273.079 seconds with native exit 0 and
one finished event. Its original 18 guards passed, as did actual imported-module
path/hash/PID/full-resource guards in both the parent and independent cold child.
All 150 native checkpoints still match v10/v12 exactly. Painted, restored active
and saved-stack settling took 44.072, 46.139 and 22.528 seconds, with separate
synchronous verification taking 53.524, 57.321 and 13.126 seconds. The large
variation from R1 is retained; no timing improvement is attributed to adding
terminal import observations. Native correctness is established, while these
long settling times remain unacceptable for immediate final-quality feedback.

The complete Lens experiment is terminal and passes. A separate frozen native
chapter capture supplied its actual owned ARGB32 input (1105 by 1466, bounds
(-15,17555,1105,1466), SHA-256
`1eac55aa640a13f6a652b646c446114b50ea801bd35a8cd7efd4b99b80c617fb`).
All six alternating old/candidate full renders match all 115906756 native output
bytes and the independently archived Lens output hash exactly. Twelve native,
three ownership/mutation and twelve boundary/mode/nonfinite/size checks pass;
all 257 frozen resources and the source input remain unchanged.

The candidate retains original maps and every partial bilinear tap, omitting
SciPy work only where all taps have constant padding. Of 28976689 output points,
15138144 (52.24249%) have no real taps. Cold full-frame old times were 12.269,
12.429 and 12.635 seconds; candidates were 12.049, 11.106 and 10.732 seconds.
The median saving is 1.324 seconds (10.65%), with only 1.8% in the first pair.
This is a modest native-kernel improvement, not a GUI latency result. Explicit
temporary arrays peaked at 10281648 bytes; this does not measure OS process peak.
Prepared-source bytes/budgets and Float/other-effect routes remain unchanged.
Production integration is still pending its focused tests and new acceptance.

The proposed JPEG scaled-read experiment is rejected for this actual source.
Its filename ends in `.jpg`, but a read-only original header check shows PNG
magic/IHDR, 5745 by 3852 RGBA8, with the already pinned 31665964-byte file size.
Both R1 native runs retain the original first changed-drag failure (5.337 and
6.081 seconds, exit 1); the proposed proxy had zero reads or admitted entries.
The JPEG-only gate correctly excluded the PNG. Assuming a codec from its suffix
was an experiment setup mistake. No JPEG shortcut, PNG decoder replacement,
source reencoding or hidden warm-up was integrated. Cold first selected pixels
remain an unmet requirement; the existing ordinary-native decode/preview paths
are being evaluated separately.

The monitored 908/604 ms decode freezes were traced specifically to Navigator
band rendering, not the committed current-model scene capture. Navigator bypasses
the scene backend and its channel excludes both deferred and live-draft source
acquisition, so it calls the synchronous original ImageStore decoder. The first
Navigator completion-dependency filter has passed 50 new and 218 directly
affected regressions with zero failures/errors/skips and unchanged provider/
source/import guards. A separate Navigator opt-in to the existing native decode
worker is being tested. It must retain a private unfinished band on Pending and
publish nothing until that band is complete, with the same source keys, native
decoder, color contracts and bounded worker admission.

The v15 runtime is now frozen with 257 resources, manifest SHA-256
`87e62d2b34289ca158eaaf8de4636e28abd85bb9306b1411b3f3eeb9b5d56bf0`.
It includes the dependency filter, Navigator use of the ordinary asynchronous
source decoder, and sparse constant-padding Lens sampling. Focused Lens and
directly affected tests passed all 282 cases with no failures/errors/skips;
Navigator source tests additionally cover terminal failure, null results,
cancellation and a bounded metadata-only wait. The 15 inferred historical
failure node IDs also pass individually. These focused results do not substitute
for the still outstanding protected whole-suite terminal result.

V15 primary R2 completed in 251.426 seconds (native PID 6964, exit 0, exactly
one finished event). All 18 acceptance guards and actual parent/cold-child
module-path/hash/PID/full-resource checks passed. All 150 native checkpoints
match the v10/v12 references exactly. Painted-mask, restored-active and saved
settling took 43.083, 42.997 and 24.408 seconds; independent synchronous checks
took 45.985, 46.192 and 12.592 seconds. Original cache records remained unchanged
and the run had no disk loads/hits. Large settling delays remain unresolved.

The clean v15 Navigator still fails its unchanged 15-second recovery deadline
(101.868-second process, native PID 10556, exit 1). During that recovery it
submitted 241 more jobs and completed 243, with 3433 more projection attempts,
3417 incomplete. The six earlier native checkpoints match exactly, but the
failure occurs before the helper's final restoration payload: missing terminal
model/history restoration evidence is not treated as a pass.

A separate quiet R7 diagnostic also fails the same deadline (99.226 seconds,
native PID 12048, exit 1), with strict failure-evidence and observer-cleanup
guards passing. Of 284 recovery notifications, 237 were correctly ignored by
the Navigator filter and 47 admitted; none had unknown relevance. The 131
observed build contexts stayed at document/history/stack [19,3,5], with no
invalidate or abandon callback. It finished one full build, scheduled another,
and reached the deadline pending an original-source decode at row 0. Decoded
source residency moved from 261923760 bytes/34 records to 266521520 bytes/31
records under the unchanged 268435456-byte budget, with generation 0 throughout.
The capped identity sample contains 62 canvas tile frames and two source decode
jobs; it does not establish every later job's scope. Together the full-followup
and bounded-LRU observations support a source-completion loop: completed bands
contain no source placeholders, yet a successful native decode requests another
full pass which can decode originals evicted by the previous pass. A narrow
owned-pending-source completion fix is under review; no deadline or oracle is
being relaxed.

The acquired-source compact preview experiment R2 passes its independent pixel
and release proofs, but is not integrated into production. Both separate native
processes preserve the original 67 assertions, all three changed native commits,
Undo/restored states and the cold three-patch equality checks. The candidate's
12 actual changed selected-stack images match fresh reference owners, and all
three first release paints publish current revisions 11/22/33. Its fixed whole
source preview is 221 by 148 pixels, constructed in 0.664 ms from the ordinary
completed original decode and charged to the existing decoded-source LRU. The
initial original acquisition took 1027.766 ms, with first readiness at
1001.481 ms; this explicitly acquired experiment does not fix true-cold first
feedback. Transform Undo left source generation at 0 in these runs, correcting
the earlier assumption that every transform Undo flushes source residency.

The instrumented pair's 12 drag paints were baseline min/median/max
200.353/246.107/294.322 ms and candidate 107.558/126.824/502.851 ms. The real
502.851 ms warp spike and the ordinary 120 ms paints remain unacceptable. The
candidate process also runs additional fresh original-source reference checks,
so its 183.483-second total cannot be compared as clean performance against the
161.466-second baseline. A new matched timing pair removes reference rendering,
readback/hashing and pending diagnostic work from the timed processes while
retaining the accepted current/native provenance checks. Its results remain
pending. The previously proposed 31–45 ms goal has not been achieved.

The clean acquired-source R3 pair is now terminal and its strict comparison
passes: baseline PID 1644/166.934 seconds, candidate PID 12724/168.749 seconds,
both exit 0. All three current first releases, changed native commits,
Undo/restored states and independent cold patches pass and match. The 12-paint
median changes from 250.346 to 125.617 ms, but candidate scaling still reaches
251.200 ms. No fresh reference rendering, ROI readback/hashing, source
enumeration or Pending-stack diagnosis ran during these paints. The compact
candidate therefore gives a measured acquired-preview improvement without
meeting responsiveness targets. The original 502.851 ms R2 warp observation
remains recorded; it was not reproduced by this clean pair. A separate one-paint
profile is being captured to distinguish the remaining cost from reference-test
residency and to avoid attributing it to unchanged neighbors without evidence.

The separate R4 one-paint profile is terminal and strictly accepted (native PID
12780, 163.672-second process, exit 0), with current releases/native commits/
restored/cold/source proofs intact. Its instrumented translation paint took
157.327 ms: scene service was 148.747 ms inclusive, with 11 mirror targets and
13 stage-stack calls. The stage renderer was 90.708 ms inclusive and three
Distort calls 42.489 ms inclusive. Disjoint leading exclusive costs include
24 SciPy geometric transforms/17.797 ms, 7610 attribute lookups/11.182 ms,
three MLS evaluations/8.092 ms, 68 image draws/5.497 ms, recursive geometry
fingerprints/3.875 ms and two framebuffer readbacks/3.857 ms. Inclusive and
exclusive views are not summed. No source decoder ran in this frame; the
remaining delay is ordinary scene/effect drawing. This frame supplies no
Radial cache-miss evidence, so the separate content-key proposal remains
unintegrated.

The narrow Navigator source/capacity completion change is integrated. All 255
focused/affected cases pass (16.477 seconds, zero failures/errors/skips), with
full current-resource and actual imported-path/hash proof and zero provider
attempts. Nineteen new cases cover synchronous nested metadata restoration,
stale source/history/color/generation rejection, preserving existing required
followups, successful unrelated completion, failed/canceled sources and two
separate capacity releases without a second full build. Three spaced originals
over a two-frame decoded budget complete one build in Legacy/f16/f32, each with
three off-thread original decodes and final preview plus resident native bytes
equal to fresh ordinary references. Initial setup/fixture failures are retained;
the existing 236 cases required no runtime repair. Full native chapter acceptance
for the newly frozen build is still pending.

V16 froze 257 resources with manifest SHA-256
`2878e0bdb18d3258f51afe88696477040e1cf7961a7c8003f8bd39f7fb9e4937`.
Its clean Navigator still fails the original 15-second recovery deadline
(native PID 4684, 104.885 seconds, exit 1). R8, a separately guarded quiet
diagnostic, also fails (PID 14724, 94.877 seconds). It proves the completion
loop fix works on the actual chapter: zero recovery full-followup transitions
and zero published-followup builds, with ten matching source success callbacks
and ten successful current-source validations. Fifty-five capacity callbacks
arrived while a private source was pending, with 46 current validations. No
source success had a mismatched or absent private dependency. The build stayed
at the current context/generation 0, dirty/followup false, but reached only row
224 of 671 before waiting on another valid original. During recovery, 246 jobs
were submitted and 248 finished, with 1285 requests. This identifies remaining
worker-admission starvation: the private 120 ms retry often loses released
capacity to native canvas jobs. Diagnostic observer costs remain separate from
clean timing; missing final restoration payload is still not accepted.

The next integrated change only shortens a validated source-success or
capacity-release continuation to the next Qt turn (1 ms), retaining an already
earlier timer. Running/budget-denied sources still wait 120 ms and ordinary
unknown/stale notifications retain prior behavior. No queue, memory budget,
decoder, cache key or sampling contract changes. All 262 affected tests pass
(17.520 seconds, zero failures/errors/skips), including seven new release,
no-release, budget-denial, contact and stale-source checks. Provider, full
resource and actual-import guards pass with zero provider attempts. The v17
full-chapter native gate remains pending.

The zero-native-tap Pinch experiment is terminal with all 30 small guards passing,
including native RGB under zero alpha, Float fallthrough, coefficient boundaries,
nonfinite coordinates, COW, borrowed/unowned inputs and budgets. Its complete
5383-square result matches all 115906756 original full-SciPy reference bytes;
all 21 timed project tile outputs match that independent full oracle. The
7225-byte coarse map finds 3281 occupied blocks and skips 756742 of 1835008
requested sample points (41.24%). Seven-tile cold medians are 1.103784 seconds
ordinary versus 0.937669 candidate (15.05% lower); the captured 512-square tile
changes from 0.162751 to 0.102742 seconds (36.87% lower). Cold occupancy scans
take 15–24 ms and are included. Numeric retained preparation is 24.45 MB versus
35.26 MB. These are kernel/ROI results, not GUI or full-chapter timing.

The separate full-output candidate takes 21.76 seconds versus 13.32 seconds for
the full-prepared SciPy oracle. Those algorithms are not matched current-Strip
timing baselines; the difference nevertheless prevents broad admission without
further measurement. Production would need a requested-whole-output ROI guard,
not merely the size of each internal sampling block, plus an established owned
immutable source contract. The public kernel accepts borrowed QImages, so the
artifact's explicit ownership opt-in is not a production ownership detector.
This experiment is not integrated.

V17 freezes 257 resources (201 runtime/shader files), manifest SHA-256
`22891b742552638acd573d4168a47c83bd55ca5ea68b01bd942194f0ffc22389`.
The clean full-chapter Navigator now passes: native PID 8736, 124.082-second
process, exit 0, unchanged 37 assertions. History/Escape recovery takes
10792.858 ms, below the original 15000 ms deadline. The release-driven change
therefore resolves the measured admission delay without a new queue or budget.
All six current native position checks (300,6456,12000,27000,42000,54000) and
the final target check pass. Modifier Undo/Redo/Undo and the real Escape handler
restore whole model, remembered Raster, selection/tool and command identity,
clear the preview and finish the Navigator. The terminal guard independently
rehashes 138 actual imported files under the frozen root and verifies all 257
current/frozen resources plus native original inputs. Instrumentation is off.
The Navigator helper does not request an independent cold renderer and makes
no persistent-cache assertion; those remain separate checks. This is recovery
acceptance, not a claim that every editing paint meets its responsiveness goal.

The selected-only 128/8192 preview experiment's corrected R2 candidate also
finishes with exit 0 (PID 9200, 182.984 seconds). All twelve actual changed
widget crops equal separately decoded current-model references using the
declared temporary preview policy; all three first-release revision guards,
three changed native commits, restoration and independent cold native patches
pass. Its selected input is 84 by 100 pixels and Deform/Twirl output 78 by 104,
so the intended policy is exercised by the actual widget observer. Instrumented
paint values are 63.883–123.283 ms. These include correctness instrumentation
and are not clean timing or acceptance of the 31–45 ms target. The matched
ordinary run and an uninstrumented timing pair remain separate gates. The
failed R1, whose instance observer bypassed the class policy wrapper, remains
preserved and rejected. Neither selected128 nor the Pinch experiment is in
production at this point.

The compact256 production placement is still under review. Compared with the
experimental hook, its first draft had four material integration hazards:
missing provenance could raise Pending without a decode job; a fallible source
check could bypass flag restoration; a preview hit could ignore a changed
resident source; and generic decoded-cache insertion did not establish actual
source acquisition. The R2 proposal uses ordinary fallback, unconditional
restoration, resident COW/profile/extent validation and stamps around actual
ordinary decode or guarded worker adoption. An additional review requires
explicit ownership at the raster-cache entry, rather than treating any detached
interactive painter as widget-owned. Proposed regression tests and production
native/current-frame validation are required before acceptance.

The reviewed compact256 placement is now integrated in seven runtime files and
`tests/test_acquired_source_preview.py`; the selected128 and Pinch experiments
remain excluded. R3 made raster presentation ownership explicit. R4 distinguishes
trusted acquired source input from ordinary fallback before warm source/stage
lookups. The actual warm RenderService/HSL regression changes visible resident
pixels and a generic replacement, requires changed current output and equality
with a fresh cleared render, and proves the acquired route was active first.
An initial fixture assigned selection fields without selecting the actual image;
the ordinary selection API and explicit route/cache assertions corrected that
fixture. All earlier failed proposal XMLs remain preserved. The cold-source
test also now proves both the existing off-thread original handoff and admission
of the actual owned small representation, rather than merely a non-null image.

The final guarded proposal has 92 passes in 6.974 seconds, zero failures,
errors or skips, with actual candidate import origins and unchanged candidate
and production resources. The integrated affected set has 163 passes in 19.440
seconds, likewise zero failures/errors/skips, including source pressure,
current/release feedback, mask contact, spatial drafts and native clip paths.
These are placement/correctness gates, not native paint timing. One initial
affected invocation guessed two nonexistent test filenames and stopped before
executing tests; the successful invocation uses the actual file inventory.

V18 freezes 258 artwork/runtime resources, manifest SHA-256
`f530b74c3bba9b44cefa78a4d2b2187bf7fe905384942fcd290b9afe710b3a0b`,
with 359 test Python resources pinned separately (SHA-256
`c3f966380b0fb314ab1636056055fb520367a344a8f2ee681de80f992edbed54`).
All twelve sequential freeze/finalizer/preflight steps exit 0, with current and
frozen resources equal. The integration's fresh native current-ROI/release/cold
output proof and the full final-build suite are still pending at this checkpoint.

Read-only nonfocus-key review finds no evidence for another late cache change.
The selected acquired source token is included in every modified source key,
but excludes geometry, modifier/mask parameters, camera and document revision.
Recorded acquired-profile v15 R4 tokens stay identical throughout each
ready/drag/commit gesture and change at Undo. Eleven mirror targets and thirteen
stacks in the first-paint profile do not establish which owners missed caches;
without owner-specific hit/miss evidence, their cost is not attributed to that
token. Selection/source/history and trusted/fallback transitions can still
invalidate nonfocus keys conservatively. No optimization of that behavior is
included in V18.

V18 native GPU acceptance passes all nine cases, zero failures/errors/skips,
with 102 actual imported modules under the frozen root and all 258 resources
unchanged. The cold MainWindow Add R2 likewise exits 0 (native PID 16356,
parent PID 14072). Its actual late import proof independently rehashes 150
modules versus 113 in the initial inventory. First return is 260.069 ms,
full-quality palette completion 18325.560 ms, warm native oracle 30.299 ms,
and maximum GUI heartbeat gap 214.404 ms. Metadata verification costs
136.477 ms outside those phases. The palette remains slow despite asynchronous
completion; this is correctness/liveness acceptance, not fast-palette acceptance.

The production compact256 R4 native proof passes (PID 3616, 186.960 seconds,
exit 0). It preserves all 67 original assertions with no source/sampler/cache/
scene-routing override. All twelve changed widget ROIs match current independent
original decodes under the production presentation policy. All three first
release paints show current revisions 11,22,33 while exact revisions 4,12,23
are still pending. Each changed native commit differs at 36 of 45 addresses;
all three complete native hash lists equal accepted historical V15 R2 results.
Restored artwork and all three independent cold patches also match. Parent
134 and cold-child 108 actual imports/PIDs, all 258 resources and project input
guards pass. Instrumented drag paints are 94–155 ms and release paints 157–190
ms; initial original acquisition takes 766 ms separately. These are correctness
observations, not a new clean timing pair.

Three earlier production-helper attempts are preserved and rejected: a wrong
observer class name, then fresh-reference acquisition ending Pending before the
selected image was reached. The accepted old reference synchronously acquired
every demanded independent original; the production adaptation initially
acquired only the selected original. R4 uses bounded ordinary worker polling
and retries outside primary timings, within the unchanged 20-second reference
deadline and without synchronous fallback. Each reference completes after two
or three cold-neighbor retries. Its selection, source/precision, owner scope
and current status are explicit; no pixel-comparison assertion is waived.

The first fullsuite attempt exits 1 before Qt import or test execution because
the ready metadata pins test-manifest bytes with SHA d3196ee5…, while the
unchanged actual frozen test-manifest bytes have the recorded c3f96638… hash.
Independent verification confirms all 359 current test files equal that manifest
and all 258 artwork/runtime files equal the freeze. Entry, wrapper, provider,
child-hook, child-accounting and child-signature pins match. The failed attempt's
log/process and original metadata are preserved; a separate additive R2 runner
must use the proven test-manifest identity. This is runner setup failure, not
completed suite acceptance or a program regression.
The additive R2 fullsuite attempt also stops before any test body executes:
collection imports the repository's separate brush playground utility, which
was absent from the frozen editor-only path. Its original exit-2 pytest result,
exit-8 terminal guard, collection-error XML and import evidence are retained.
R3 separately pins untouched root main.py and brush_playground.py, verifies
current/frozen support bytes and actual imported origins, and keeps the frozen
editor first on the import path. No test is omitted to resolve the dependency.
The R3 suite starts successfully and is still running; progress is not a
terminal pass or native-output acceptance.

The R3 fullsuite reaches a real terminal result: 6508 passes, 95 skips and
one failure in 1744.90 seconds (native/pytest exit1). The outlined variant of
text_drag_with_promoted_artwork_updates_before_one_undoable_commit leaves
its drag preview equal to the initial pixels at line245; the plain variant
passes. This is a program regression requiring diagnosis and repair, not a
runner setup failure or accepted suite. The complete XML/log/process/import
and child evidence remain immutable. All final V18 broad/timing/UI plans are
held while the focused reproduction identifies the cause. A changed runtime
will require a new freeze and fresh final-build verification.

The failed suite's provenance still passes independently: all 258 frozen
resources, 359 tests and both separately frozen support modules, parent PID 7448
with 201 actual imports, and exactly three accounted child tests. Child PIDs
13340/2768/3164 have 122/5/123 verified imports and no provider entries; parent
provider entries are also zero. The suite is rejected solely for nonzero
pytest/native return and the actual JUnit failure. Its 95 skips classify as
73 remaining hardware/backend cases, nine already-passed native GPU cases and
13 unavailable private/reference brush fixtures. There are no unidentified
hardware skip reasons. Correctness failure is not concealed by these guards.

Focused immutable V17 and V18 replays both reproduce the outlined-text failure,
so it predates the V18 acquired-source placement. Trace evidence shows an
original native-preview request with no owned compact viewport, no Pending,
and non-null provisional output. Glyphs use the current free-text quad at
150..350, while the mirror-style source bounds remain 700..900; the capture's
-700 translation clips the newly moved glyphs. Bounded live rendering routes
the supported Outline stack through mirror-target capture. Its bounds helper
excludes Text because another generic text-bounds path already handles live
quads, but this caller starts from the original frame. A narrow live free-text
bounds correction is being validated; strict/nonlive layout and native source
grids must retain their behavior.

The bounds-only causal control restores the three live previews but fails the
original commit==preview byte assertion, so it is not integrated. Keeping
outlined text on its established generic capture passes every original test
assertion. The production correction changes only canvas.py: TextObject is
excluded from opportunistic live compact mirror dispatch; explicit spatial
modifier dispatch and Navigator are unchanged. The generic text capture
already follows its live quad and uses the established cheap native solid
outline path. No new sampling, cache key, source grid or precision rule is
introduced, and the native renderer epoch stays unchanged.

The original full transform-scene-culling file passes 14 cases in 2.127 seconds;
167 affected text outline/visibility/promoted rendering/projection/acquired
source cases pass in 27.541 seconds. Both processes terminate 0 with zero
failures/errors/skips; tests are unchanged. All repros and rejected causal
controls are retained. The runtime delta from frozen V18 is exactly canvas.py,
SHA-256 857b8562a4096ba33e596d4cd31b383868fc25391d932329cc6fdea210891057.
V19 freeze is authorized after those terminal results; final verification is
still required and the earlier failed fullsuite remains rejected.

V19 freezes the corrected source: all 258 resources independently match current
and frozen bytes, manifest SHA-256 a44dc165193e200acb76d906773cb6c64df07a35ea722c717d753d64b2003ca0.
The 359 test files remain semantically identical to V18. Their newly serialized
manifest's actual raw SHA-256 is d3196ee5478866fe2bab1e9f9cbc0f3d442e194f232eb92e9dadac510e116d28;
that value is computed from the new bytes and matches the new ready/plan pins.
The separate fullsuite ready file is immutable during execution so later
native/UI metadata cannot alter its pins. Latest R3 standalone support,
fail-closed provider and exact three-child guards, six exclusions and marker
remain intact. Source/support/test/runner stdlib preflight passes, imports no
Qt and launches no tests. The fresh fullsuite is now active under label
final-nonexternal-v19-r1; no concurrent native/performance/UI run is permitted.

V19 R1 reaches a successful terminal result at 19:11 UTC: 6509 passes, 95
skips, zero failures/errors and two previously identified divide warnings in
1754.35 seconds. Native and pytest return 0. Strict terminal acceptance also
passes all 258 resource and 359 test pins, 201 actual parent module origins,
both root support modules, and the three accounted children (PIDs
10020/5736/9456, with 5/123/122 verified module origins). Parent and children
record no Blender provider entry. The full offscreen XML has 6604 complete
testcase records. Its 82 hardware/backend skips still require current native
GPU execution; 13 unavailable private/reference fixtures remain explicit.

The first clean V19 native six-effect mask check terminates 0 at 19:16 UTC
(PID 10388, 240.636 seconds). Profiling, monitor, planning/process telemetry,
live evidence and raw input capture are disabled. The three current exact
checks compare 150 native tiles with zero changed bytes, restoration matches
the historical native lists, and three independent cold-child patches match.
Actual parent and cold-child origins/PIDs, all 258 resources, model restoration,
original input/cache preservation and zero disk pixel reads pass. Live mask
paint frames take 27.516–31.042 ms; commit paint takes 141.212 ms and paint Undo
161.304 ms. Binding removal and whole-stack Undo still repaint in 682.029 and
692.306 ms. Exact settlement remains background work taking 45.413/38.612/21.197
seconds for the three states; synchronous reference recaptures are separate.
These are current production clean timings for this mask case, not a claim
that moving, scaling, warping or every history action meets the response goal.

V19 clean GPU Navigator terminates 0 in 117.938 seconds. All 37 original
assertions, six current native checkpoints at chapter y 300/6456/12000/27000/
42000/54000, final native/history/model restoration and actual frozen origins
pass. History-triggered Navigator recovery takes 12090.239 ms, below the
unchanged 15-second bound; this remains a visible preparation delay. The nine
separate native GPU canvas cases also pass without skips, with 102 verified
actual imports and all 258 resources intact.

The remaining 73 offscreen hardware skips are executed on Windows in an exact
native subset. R1 has 61 passes and 12 failures, zero errors/skips. Its failed
XML, logs and process/proof are preserved and rejected. Independent stdlib
review validates PID 6868, all 101 actual imports, 258 resources, 359 tests,
provider/subprocess zero and unchanged capture pins. Pytest return 1 becomes
native return 8 because aggregate acceptance fails; the wrapper's generic
"PID/proof failed" message does not mean the actual PID differs.

Every failure is in GPU blur: nine direct cases and three worker cases return
None with zero uploads/readbacks. GpuBlur explicitly accepts Pillow 12.2.0
only, but this environment actually has 12.3.0 (requirements permit >=10.0).
The existing unverified-resampler fallback runs before graphics access. This
is a stale compatibility boundary, not a shader failure or source-origin drift.

An isolated package changes only that predicate to explicit verified releases
12.2.0/12.3.0, retaining real installed version strings, original tests and
unknown-version fallback. Native control PID 9144 exits 0 in 15.366 seconds:
74 complete JUnit cases pass without failures/errors/skips. All 12 originally
failed cases and the unknown-version guard pass; 60 expanded cases cover 23
radii each (1380 raw float32-bit comparisons), unit/odd/511x513 shapes, both
algorithms and premultiplied/unconstrained/extreme-alpha inputs, with zero
differences. Current/original/candidate resource and test pins, fail-closed
provider and no-subprocess checks pass. Root independently rechecks the full
XML and confirms exactly one candidate resource differs, render/gpu/blur.py.

A separate alternating three-pair 1025x1025 timing control measures CPU/GPU
medians: normal cold 46.654/26.814 ms and warm-radius 37.374/14.751 ms; legacy
cold 63.065/25.666 ms and warm-radius 47.742/14.529 ms. GPU resource use is
66319148 bytes within its unchanged 67108864-byte budget. These isolated kernel
measurements do not establish whole-editor or physical input latency. Evidence
is in gpu-blur-pillow123-v19-r2/{proof,performance,process}.json and pytest.xml.
Only the verified version allowlist is approved for production integration;
sampling, fixed-point coefficients, pass rounding, color and renderer epoch
remain unchanged. Fresh integrated checks and a new final freeze are required.

The integrated allowlist passes 32 original native GPU blur, worker and point
chain cases without failures/errors/skips (PID 3460, native/pytest/wrapper 0,
5.276 seconds of pytest execution). Independent review validates actual current
module origins, all 258 resource and 359 test pins, no child process and no
Blender provider entry. The only runtime change from V19 is the verified-version
predicate and its comment in render/gpu/blur.py; the renderer epoch is unchanged.

For the timing control above, cold means a fresh CPU blur pyramid cache and
unique GPU source keys in an existing GL owner/driver session. Conversion tables
and the driver were already warm from correctness checks. Source generation and
initial float normalization were outside timing; CPU conversion/cache/build/
upscale/blend/normalization and GPU upload/build/blend/normalization/readback were
inside. Warm-radius samples reuse source levels at radii 11.25, 12.25 and 13.25.
These measurements do not represent process startup, source decoding or editor
interaction latency.

V20 freezes this exact source with 258 independently verified resources and
manifest SHA-256 d4081bd96cb52d957f8af3c3afdfa40c9acbb02eedb03bf48b1a0d8f2e3acfe4.
All 359 tests are unchanged. The independent fullsuite ready file remains
immutable during execution, including the separate main.py/brush_playground.py
support and exact three-child/provider guards. The fresh V20 R1 fullsuite began
at approximately 19:43 UTC; current native, editing, navigation and actual Save
acceptance remain pending. Earlier successful V19 results remain historical.

V20 R1 completes at 20:07:43 UTC (started 19:42:35): 6509 passes, 95 skips,
zero failures/errors and the same two known warnings in 1505.33 seconds.
Strict terminal acceptance passes 6604 complete JUnit records, 201 actual parent
origins, all 258 resources/359 tests, both standalone support modules and all
three accounted children (PIDs 13416/16052/17392, 5/123/122 actual module origins).
Parent and children enter no Blender provider. Native heavy editing, desktop
GPU skips and actual private Save still require fresh current-build acceptance.
The suite duration is not a controlled editor-performance comparison.

A separate read-only diff audit distinguishes freeze preservation from the
original checkout. Nineteen tracked test files changed during the investigation
(definitions 224 to 255; asserts 884 to 1059), with 46 additive untracked test
modules. No new skip/xfail/importorskip calls or deleted numerical assert_*
oracles were found. Two publication expectations and one radial post-drag
expectation intentionally adopt committed-widget asynchronous projection;
their strengthened checks retain old/current revision separation and final
current publication. Initial text/overlay/warm-ink fixtures now establish exact
resting artwork before comparing immediate live/cancelled output. Shared and
exclusively owned mask cases have separate strict coverage. Thus "unchanged
tests" above means unchanged during the named freeze/run, not unchanged from
the starting checkout. Original numerical pixel/native/history oracles remain.

The first V20 clean native mask run is rejected at terminal acceptance. Native
PID 13816 exits 0 in 252.330 seconds, with 150 current exact tiles and three
independent current cold patches showing zero differences, and restored model/
input/cache checks passing. However, 36 of 50 historical native pixel hashes
differ in each of the three states; tile rectangles, formats, dimensions and
strides match. Presentation also differs. This is a real hash rejection, not
just path metadata, and the existing baseline assertions are retained.

V10/V12/V14/V19 used Intel Iris Xe, GL 3.3/core profile and DPR 1.5; this V20
process used Gallium llvmpipe software rendering, GL 3.0/compatibility and DPR 1.
Independent stdlib review verifies 134 parent and 110 cold actual module origins,
both distinct PIDs (13816/5976), and all 258 current/frozen resources. This
Canvas-only helper has no provider-attempt counter; its module lists exclude
Blender/MainWindow and its loader uses stored sources, so no numeric provider
attempt claim is fabricated. The original failed proof remains immutable.

Read-only environment probes import no editor modules. Default probe PID 10428
reports a Windows `WinDisc` screen (1920x1200, DPR 1, logical DPI 96) and Mesa
3.0/llvmpipe. Explicit desktop-OpenGL probe PID 9088 reports Microsoft GDI Generic
1.1. No QT_OPENGL/scaling environment override was present. The installed Intel
driver is now 32.0.101.7088, versus the previously recorded 32.0.101.7026, and
Windows reports no current physical video mode. Desktop reconnection was
requested while independent work continues. These timings cannot be compared
as matched Intel performance. Backend and DPR contributions to the native hash
change are still unresolved; a same-software-backend scaling control is pending.

The unchanged-helper DPR control completes with native PID 11580 and cold PID
15016 in 195.551 seconds. Only QT_SCALE_FACTOR=1.5 changes in the child process;
the observed renderer remains the same Mesa/llvmpipe GL 3.0 backend. All three
states, including restored original document state, differ in 36 of 50 native
hashes from software DPR 1. Native rectangles/formats/sizes/strides are identical;
current exact recaptures, cold patches and actual parent/cold source origins
pass. Nine hashes per state also differ from historical Intel DPR 1.5. The
original baseline/presentation guards still fail and their exit 1 is preserved.
No baseline is rebased and no assertion is waived. This establishes a native
DPR-dependent byte problem in the supplied case; hardware/backend differences
remain a separate unresolved issue. Frozen cold-stage localization is pending.

An independent supported-text repro also fails on V20 (native PID 15596,
32.481 seconds). Moving child da20441ad7184b3baa46067438682157 of the saved
double-Outline Free Text container 124912d882784aa2b535789eaee54dc2 by -24 world
pixels vertically produces glyph pixels unequal to a fresh committed model.
The old captured input frame starts near world y 17621.833, while the live
glyph quad starts at 17602.853. Asset/baking bounds recurse through the stored
child quad while the capture painter draws the live quad. This is supported
ancestor behavior; direct Text spatial modifiers are prohibited by model
validation. Failed provenance/source/provider guards pass. An isolated candidate
supplies recursive current object-local bounds only for actual free-Text
gestures; it has not been integrated or accepted yet.

Subsequent handler review qualifies this text result: R1 directly invokes the
generic selected-Text preview method. Ordinary single-object pointer press tries
the free-Text handler first, and effect-bearing free Text uses a stored-quad live
gesture through that handler. The internal generic route mismatch is proven,
but ordinary single-child UI reachability is not. R1/R2 and the isolated candidate
remain preserved and unintegrated. Additive ordinary-pointer and reachable
multi-selection tests are required before claiming an actual UI regression or
integrating this proposed correction.

The first frozen cold localization attempt preserves a successful DPR 1 native
child (PID 10488, 16.44 seconds) but its new diagnostic wrapper incorrectly
expects successful TileBatch.error to be None rather than the actual empty
string, and repeated regional metadata exceeds its cap. That attempt remains
preserved. A new bounded R2 observer accepts successful error None/empty string
only, keeps all original 38 native helper assertions, and limits repeated
owner/stage records; this is an artifact correction, not a renderer change.

R2 terminates 0 with native PIDs 15824/13200 (15.537/15.893 seconds), the same
saved model and native mask buffers, three existing exact patch oracles,
2031 pinned supplied inputs, all 258 current/frozen resources and provider guards.
Both members reproduce their corresponding software primary native hashes.
All 799 recorded native flags have exact=True, bounded=False, vector scale 1;
thumbnail capture scale is 1 and source/effect grids and mappings match.

The first observed source-pixel difference is literal SLAM Text
304ae307b5094d84813e784aa7e231d7. Its 1007x666 ARGB32 source QImages have identical
DPR 1/DPM 3780, settings, bounds, mapping and 61px font, but hashes 64996ac9…
and 42a7ae03… at display DPR 1/1.5. QTextDocument's layout paintDevice is None.
Eight of twelve recorded text document heights vary despite matching recorded
glyph runs/positions/outlines (36px font height 167.671875 to 171.5625; 39px font
235 to 240; 50px font 240 to 244). Recorded preceding raster sources and subsequent
Blueprint source chunks match. The 2GiB hash budget bounds this observation;
unobserved later bytes are inconclusive. The next isolated control binds a
retained native metrics device before text layout/size queries. Production is
unchanged, and this observation does not yet establish the candidate fix.

Correction to the preceding glyph comparison: the initial observer queried glyph
runs before document layout/drawing, and those lists were empty. Their equality
is vacuous and provides no evidence that glyph outlines or positions match.
The observed source image bytes, document heights, settings, mapping, native
flags and image-device metadata remain valid. Further controls require nonempty
line/glyph observations after drawing.

The retained native metrics-device candidate is rejected by R3. Native children
228/6264 terminate 0 in 16.33/15.46 seconds, with all 258 isolated candidate
resources, unchanged production resources, supplied inputs and provider guards
verified. Every observed document layout uses the actual retained DPR-1,
3780-DPM QImage device. Its output nevertheless reproduces the respective
original DPR-1/DPR-1.5 results. The unchanged three-patch DPR-1 oracle passes
at DPR 1 and fails at DPR 1.5 by 26671/26503/4569 changed bytes. Document-height
and source-pixel differences remain. The candidate is not integrated.

The installed Qt 6.11.1 Windows font implementation provides a concrete cause:
PreferDefaultHinting chooses DirectWrite when the application's display DPR is
not 1, while DPR 1 normally uses the existing GDI path. Explicit No/Vertical
Hinting also chooses DirectWrite; FullHinting normally chooses GDI. MingLiU and
color-font handling have exceptions. A QFont constructed with a paint device
copies the device's logical DPI, which is 96 in both controls; the device does
not override this global engine choice. See the installed-version primary
sources [Windows font engine selection](https://raw.githubusercontent.com/qt/qtbase/v6.11.1/src/gui/text/windows/qwindowsfontdatabase.cpp)
and [QFont device constructor](https://raw.githubusercontent.com/qt/qtbase/v6.11.1/src/gui/text/qfont.cpp).
Text-only controls are testing the chapter's actual font settings and pixels
before any production policy change. Display scaling must not change native
artwork pixels, and no historical baseline has been silently replaced.

R4 text-only factors terminate 0 in native PIDs 14188/904 (3.590/3.814 seconds).
Each process draws all 47 saved Text objects under five independent factors,
using actual native one-pixel source rendering, original parents/transforms,
colors and font sizes. Post-draw observations require nonempty actual lines,
glyph indexes, positions and outline digests. The Default SLAM source reproduces
the prior native scene source hash. Supplied model, cache-empty, provider and
all 258 current/frozen resource guards pass. This is correctness localization,
not clean editor performance acceptance.

CanonicalDevice changes no old DPR-1 sources and leaves the DPR mismatch.
UseDesignMetrics still varies across DPR and changes 25 old DPR-1 texts.
PreferNoHinting makes all 47 native/layout/glyph results DPR invariant but
changes all 47 old DPR-1 sources (331402 pixels). PreferFullHinting likewise
makes all 47 native/layout/glyph results DPR invariant, preserves 45 old DPR-1
sources byte for byte, and changes 692 pixels/2267 bytes across two texts, with
maximum channel difference 23. Those two requested custom fonts resolve to
Tahoma Bold on this machine. Source, glyph and placement comparisons are being
reviewed before a production-like candidate and shared renderer epoch change.
Production remains unchanged and all failed historical comparisons are retained.

The ordinary-pointer multi-selection reproduction now fails on original V20:
native PID 12944, 21:01:29.511 to 21:02:01.636 UTC, 32.125 seconds, actual exit 1.
Pointer press chooses the real multi/translate branch; moving the two saved
Free Text children previews both quads 24 pixels upward and changes 74168 glyph
ROI bytes. Ordinary primary release clears both previews but commits neither
Text quad, changes no model, advances no revision and records zero commands.
No previous Undo is consumed. A separate ordinary reference release reproduces
the omission; the original strict pixel assertion stays failed. The observer's
source/test/provenance/provider-zero guards pass. This establishes actual UI
reachability of the missing multi-Text commit, unlike the earlier forced
single-Text route. `_commit_geometry_transform` currently commits only Raster,
VectorDrawing and Image objects in its multi branch. A combined isolated
preview-bounds/Free-Text-commit correction is being prepared; no current source
or test has been changed for it.

The font-policy review also checks the two small old-DPR-1 differences in detail:
font resolution, glyph indexes/positions/outline digests/font tables, document
and line metrics, source bounds and alpha bounding boxes are identical. The
measured differences are raster coverage changes. The long RENT text has 31
changed pixels with an opaque alpha in one result, so the evidence must not be
described as exclusively partially transparent antialias pixels. Compared with
old Default DPR 1.5, NoHinting changes 35/47 sources (120468 pixels) and FullHinting
changes all 47 (310336 pixels); neither reproduces that old policy completely.
The approved isolated candidate uses FullHinting in the shared Text document
and resolved-font dependency constructor, and a unique shared memory/disk epoch
`native-artwork-20261007-native-text-hinting-3`. Ordinary UI fonts and existing
font sizing remain unchanged. Candidate native controls and original regression
tests remain required before production integration.

R5's isolated production-like three-file candidate passes its exact control:
native PIDs 5952/8472, 16.969/16.620 seconds, both exit 0. The 16 requested native
tiles are exact, and all three unchanged old-DPR-1 native patch buffers match
at DPR 1 and 1.5 (ce41f312…/34e52925…/2efae2c0…). Actual post-draw text layout
and glyph data match. Both observers retain 1120 identity-aligned stage records
with no dropped records and no different observed native stage. The approximately
2GiB hash cap still limits statements about unobserved later stage bytes.
Original 38 helper assertion/raise nodes, supplied model/mask buffers, 2031
source inputs, current/original/candidate 258 resources and provider guards pass.

The separate candidate-source pair also passes: native PIDs 13564/13124,
1.651/1.543 seconds, both exit 0. Every complete native source buffer for all
47 chapter Text objects equals the independently recorded R4 FullHinting
buffer and equals the other display-DPR process. Actual font, line/glyph/layout,
model, source and provenance guards pass. No production source or current test
has been changed. A separate bounded Unicode/styled/wide-format/editor/cache
extension is required before integration; these diagnostic timings are not
clean editing performance measurements.

R5's wider extension fails and remains preserved (original wrapper exit 1;
native children 11376/5944 both exit 0, 1.859/1.716 seconds). Fifteen of 18 full
native buffers match across DPR. The styled Segoe UI Bold mixed Latin/emoji/
Chinese/Hebrew case differs in all three uint8/float16/float32 output contracts,
despite equal bounds, formats, grid, document/caret/hit/dependency metadata.
An initial glyph-record inequality is qualified: Qt enumerates glyph runs in
a different order, but their actual font tables, glyph indexes, positions and
outlines match when aligned semantically. Original enumeration remains recorded.
The remaining pixel difference is real: uint8 changes 13 pixels/28 bytes,
maximum channel difference 12, inside the first Latin A (bbox 13,20 to 19,36),
rather than the CJK or emoji glyphs. FullHinting is therefore not yet accepted
as a general correction. Narrow constructor/character-format/raster controls
are being prepared; production and historical oracles remain unchanged.

R6 qualifies that attribution further. All fourteen fresh native processes exit
0, and its wrapper terminates 0. Two exact-prefix DPR-1 replicas (15220/6500)
both differ from original R5 DPR 1 only on the styled case (28/73/149 bytes in
uint8/float16/float32), reproducing the original R5 DPR-1.5 buffers instead.
Both DPR-1.5 replicas (1044/10388) reproduce their original buffers. Thus the
small coverage difference can also occur between fresh processes at the same
DPR; it is not established as a DPR-only defect. Original R5 failure stays
failed and immutable. Fresh single-case Full/No/CharacterFull factors match
native bytes across DPR, while Default fails and Vertical retains small byte
differences. The fresh-case metric observation warms font resolution before
draw, so it does not prove the original cold-prefix path. Exact-order cold
replicas are required before a production decision. Actual Full fragments
inherit preference 3 despite an unset CharFormat property; explicit character
hinting has not been shown necessary. MingLiU is not installed/resolved in
these cases; actual Segoe UI Emoji COLR/CPAL coverage is present.

The independent existing-data comparison also finds that NoHinting preserves
all 47 old-Default-DPR-1.5 model bounds, native grids, document/block/line metrics,
glyph fonts/tables/indexes/positions and outlines. Twelve source buffers match
that old policy exactly; 35 differ in raster output. This contrasts with Full's
minimum change against old DPR 1, and is being considered alongside actual
cold-prefix repeatability rather than selecting policy by a convenient baseline.

The group-transform candidate now has original causal evidence beyond Text:
R4 PID 13924 exits pytest 1 with eight failures, no errors/skips, 1.440 seconds.
Every failure reaches actual intended-versus-stored group destination across
Text/Text, both mixed primary orders, Image/Image and Raster/Raster. The existing
release branch wrongly invokes the single-object commit when an Image/Raster
primary has an active group geometry target. The candidate excludes that target,
stores supported Free Text quads with established single-Text semantics, and
supplies current local Text bounds through ancestor capture recursion. Strict
Text groups show no free-transform cage and use ordinary selection dispatch.

R4's candidate model run retains 28 passes/two fixture failures: an unselected
background Free Text overlaps a strict+Raster test click and correctly wins the
unchanged Text-first selector. R5 moves only that unused background text in the
negative fixture and adds an actual topmost-hit precondition; all 69 original
assert/raise nodes remain. R5 then passes 30 model/history cases (PID 17156,
2.572 seconds) and 84 current-artwork cases (PID 11000, 97.396 seconds), with no
failures/errors/skips. Actual press/move/release matches independently committed
current artwork for move/scale/warp, Outline/Mirror/Radial ancestors, affine and
projective parents, both mixed primary orders and existing Image/Raster groups.
Logical Text size/style, exactly one own command, prior history identities,
model Undo/Redo and strict ordinary selection dispatch pass. Actual imports,
provider-zero, current/original/candidate 258 resources and unchanged 359 tests
pass. These are isolated synthetic correctness checks; saved-chapter native
and separate cold acceptance are pending and production remains unchanged.

R7's original-order cold-prefix controls terminate 0: all sixteen fresh native
children exit 0, total child elapsed 27.969 seconds. Full, No and CharacterFull
each run two replicas at each DPR with the unchanged 18-case/precision order
and no added pre-draw metrics query. All complete native buffers, semantic
glyphs, geometry/editor/dependency data are same-DPR repeatable and cross-DPR
equal. The separate diagnostic Full/QtHashSeed=0 family also passes. Every Full
replica matches original R5 DPR 1.5; the original R5 DPR-1 styled failure remains
unresolved and immutable. Explicit character hinting adds no native difference.
NoHinting is the next isolated policy proposal because it preserves all 47
historical DPR-1.5 layouts/positions/grids and uses design metrics consistently.
It requires its own native/cold/source proof and fresh shared epoch
`native-artwork-20261007-native-text-design-4` before integration.

The current V20 surrounding-paint causal probe completes with native PID 15084,
21:34:43.467 to 21:36:59.159 UTC, 135.692 seconds, actual command exit 0. All 77
original current/native/cold guards pass, with 84 actual draft evaluations.
Presentation is software Mesa/llvmpipe GL 3.0, DPR 1, actual GpuCanvasWidget;
the saved stack has four active modifiers. First paint measures 112.817ms wall
and 109.375ms thread CPU (Windows CPU clock has coarse 15.625ms ticks).
Selected Blue capture takes 47.858ms, including three Distort stages totaling
32.356ms; planning takes 13.120ms across twelve calls and seven ordinary draft
evaluations take 6.614ms. Source acquisition is 0.296ms with no decode/OCIO or
worker wait in the profile. Four running workers/pending-zero state is unchanged.
The other twelve instrumented drag paints are 74.586–98.121ms, median 80.687ms.
This establishes current GUI computation cost, not matched Intel or all-six
speed acceptance. Independent attribution/provenance review is continuing.

At 21:40:58 UTC, after the user returns, Windows again reports a physical
1920x1200/59Hz display. Read-only Qt probe PID 8416 terminates 0 with no editor
imports and confirms Intel Iris Xe GL 3.3/core, DPR 1.5, DISPLAY1, driver
32.0.101.7088. Hardware native validation can resume. The driver differs from
the historical 32.0.101.7026 capture, so subsequent matched timing pairs should
use this current environment, and earlier software results remain qualified.
