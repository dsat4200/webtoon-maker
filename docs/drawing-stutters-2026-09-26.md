# Drawing stalls and queued-input evidence — 2026-09-26

## Accepted final policy and measurements

The final implementation defers exact effect work only while navigating an
unchanged document from an already completed view. Initial loading, drawing,
pen-release commits, and live edits render synchronously. This preserves
immediate real stroke updates while allowing missing navigation coverage to
finish in the background. Previously finished artwork remains visible; an
indicator distinguishes pending coverage from a complete current view.

| Warm draw–navigate–draw measure | Earlier synchronous fixes | Accepted final policy |
|---|---:|---:|
| Input queue p95 / maximum | 104.5 / 292 ms | 33.5 / 117 ms |
| Paint duration p95 / maximum | 41.4 / 536 ms | 38.6 / 119 ms |
| First press after navigation: maximum queue delay | 258 ms | 6.8 ms |

The original frozen baseline's navigation input queue p95/maximum was
3,055/3,378 ms. In the accepted run, every one of the 968 scheduled inputs
reached an exact scene, with per-input latency p95/maximum **79.4/150.7 ms**.
All four strokes committed. Predictive ink was called 185/122/155/114 times
over 94/62/79/58 paints in the four stroke periods, and 292 of those 293 paints
were exact-ready. This verifies that the improvement is not merely fast
painting of stale content. Ten exact background jobs completed without errors.

During the input phase, the longest GUI heartbeat gap was **123.7 ms**. The
whole-run maximum of about 3.47 s includes initial synchronous warmup and must
not be presented as the navigation gap. Paint duration, queued-input delay,
and scheduled-input-to-exact-frame delay are distinct measures. The latter is
computed per input; the earlier next-paint statistics below sampled the oldest
waiting input per paint and therefore have a different population.

Large navigation and startup remain separate limitations:

| Scenario | First updated frame | Complete exact view | Largest GUI heartbeat gap |
|---|---:|---:|---:|
| Warm rotated view → extreme 0.053× overview | 0.583 s | 9.704 s | 0.585 s |
| Initial rotated view, with input already arriving | 3.431 s | 3.431 s | 3.445 s |
| Initial extreme overview | 9.164 s | 9.164 s | 9.180 s |

The warm-to-extreme run warmed its starting view outside measurement, then
changed to the recorded extreme camera through the live canvas. Its first
frame retained previously completed artwork; the new complete view arrived
later. All 109 jobs finished. Initial cold loading still blocks input: the
rotated cold-input run queued events for up to 3.38 s, although all 968 inputs
and four strokes were eventually rendered. This stage does not solve cold
startup or promise uniformly smooth navigation through new complex artwork.

All four `policy-final-*` runs have identical production-source hashes, no
timeouts, no pending jobs or failures at completion, and unchanged SHA-256
hashes for all 8,685 copied project files. Each saved the finished native
frame **before** clearing retained effect, source, prepared Distort, and
projection caches. It then disabled asynchronous work and viewport culling,
rebuilt synchronously, and compared native pixels. All four comparisons had
**zero differing bytes**. Oracle reconstruction is excluded from timing.

Authoritative artifacts under `.artifacts/drawing-stutters-20260926` are
`policy-final-navigation`, `policy-final-rotated-cold-input`,
`policy-final-extreme`, and `policy-final-warm-to-extreme`. They preserve input,
frame, phase, heartbeat, readiness, source-hash and project-hash records, plus
both native oracle images. The benchmark never connected to Blender or wrote
to the original project. Settings and autosaves stayed in its sandbox.

The unrestricted asynchronous candidates were rejected. The first took 53 s
and changed 811 native bytes because CPU pattern fallbacks replaced the GPU
path; preserving the existing GPU backend fixed both problems. A later
byte-exact candidate still withheld cold drawing for 7.05 s and warm stroke
updates for about 2 s while rarely drawing predictive ink. The accepted policy
therefore restricts deferral to unchanged-document navigation. Those rejected
`async-*` artifacts remain intact for audit. Future cold-start work must retain
this visible-input and exact-pixel verification, rather than judging progress
from short placeholder paints alone.

## Earlier synchronous checkpoint and investigation

The supplied capture confirms long GUI freezes. Fast individual brush updates
do not establish responsive drawing: input waits behind expensive paints before
its handler can run. The earlier renderer benchmark did not cover this user's
actual drawing conditions sufficiently.

The final measured changes substantially reduce sustained drawing stalls. In
the saved rotated view, input-queue p95 falls from 3,143 ms to 23.6 ms, and paint
p95 falls from 294 ms to 35.0 ms. The original problem is not completely solved:
new-view painting can still pause for 536 ms, and the extreme cold view still
takes 10.46 seconds. This delivers the tested high-impact improvements within
the agreed scope, not a complete asynchronous rendering pipeline.

## Synchronous checkpoint measurements

All timings below come from hidden native Windows MainWindow runs with the
copied real chapter and settings. The same generated 120 Hz input schedule is
used for each paired scenario, without waiting for rendering between moves.
Frozen baseline source remains unchanged.

| Scenario and measure | Frozen baseline | Synchronous checkpoint |
|---|---:|---:|
| Rotated static: paint median / p95 / max | 86.8 / 294 / 3,491 ms | 20.3 / 35.0 / 106 ms |
| Rotated static: input queue p95 / max | 3,143 / 3,491 ms | 23.6 / 102 ms |
| Low-zoom static: paint median / p95 / max | 58.3 / 112 / 453 ms | 18.1 / 21.3 / 115 ms |
| Low-zoom static: input queue p95 / max | 263 / 446 ms | 18.2 / 111 ms |
| Draw–navigate–draw: paint p95 / max | 650 / 3,628 ms | 41.4 / 536 ms |
| Draw–navigate–draw: input queue p95 / max | 3,055 / 3,378 ms | 104.5 / 292 ms |
| First press after navigation: maximum queue delay | 3,100 ms | 258 ms |
| Extreme 0.053× view: first cold paint | 15.53 s | 10.46 s |

Rotated static and navigation runs each dispatched all 968 inputs and committed
four real strokes; low-zoom runs dispatched all 484 inputs and committed two.
The actual camera at each stroke start matches the planned and baseline camera
to floating-point tolerance. Navigation timers coalesce intermediate updates
when the GUI is blocked: the identical schedule produced 24 camera-change
signals in the baseline and 84 in the current run, while endpoints agree.
Maximum pending tablet events during navigation fell from 272 to 36. Input
latency, in addition to paint durations, makes this backlog visible.

The final static runs execute no legacy full-scene drawing and no GUI
effect calls during the input phase. Predictive ink now paints between retained
base and promoted-artwork phases. Their p95 input-to-next-paint completion is
72.3 ms in the rotated view and 43.3 ms at low zoom. This includes the wait for
Qt painting but does not measure physical display presentation. Single-run
timings include instrumentation overhead and are not a universal frame-rate
guarantee.

The final implementation keeps predictive ink between retained base and
promoted-artwork phases; preserves unchanged effect results across selection,
tool and camera changes; reuses exact source/sampler/mesh preparation; retains
canonical native mesh-output tiles; and protects expensive shared prefixes
within the existing bounded effect cache. Mesh and MLS kernel changes retain
the original numerical results. Nested selection-menu reentry is guarded.

The cache-pressure diagnosis is verified by final cold phase counts. The same
blueprint deformation and twirl each ran twice before prefix protection, with
unchanged dependency keys after eviction. They now run exactly once: 1.457 s
for deformation and 0.511 s for twirl. Canonical mesh-output tests compare every
pixel against the full-stage crop across interpolation, edge handling,
identity/affine/projective placement, masks, intensity, and cache eviction.

Cold work remains synchronous. First paints in the final static runs take
3.48 s rotated and 5.42 s at low zoom; the extreme overview takes 10.46 s.
The final new-view maximum is still a visible half-second pause. The next
architectural priority is to prepare exact new coverage asynchronously and
retain an exact overview, with dependency-aware publication of finished
results. Existing pixels and interaction must remain available while missing
detail is computed. Keeping effect/mask intermediates on the GPU and avoiding
readback is a subsequent performance opportunity; replacing Qt or choosing
Vulkan alone would not fix these scheduling and invalidation problems.

All 8,685 input files have identical before/after SHA-256 hashes in every run.
Settings and real autosaves were sandboxed. Separate quality captures at all
three recorded cameras are byte-identical between frozen and current source;
first, settled, and unchanged-dirty frames also agree. Those quality captures
test finished scene fidelity, while targeted promoted-ink tests cover its
ordering. All four final runs used identical production source-file hashes,
completed without timeouts, and ended with zero incomplete projection tiles.
No legacy full-scene paints or effect-worker calls occurred in the final runs;
the exact new-view computations run synchronously. The synthetic trajectory
does not reproduce the missing raw pen or
touch samples, or the capture's nested selection-menu interaction sequence.

Synchronous checkpoint artifacts are `final-rotated`, `final-lowzoom`, `final-navigation`, and
`final-extreme-cold` under `.artifacts/drawing-stutters-20260926`. Each contains
settings, input/paint/phase records, summary, source-file hashes, and complete
before/after project manifests. Paired baselines are `baseline-rotated`,
`baseline-lowzoom`, `baseline-navigation`, and `baseline-extreme-cold`.
`quality-final-cameras/comparison.json` records the independent final scene
fidelity comparison. Earlier iterations remain preserved below for audit.

## Boundary for the remaining asynchronous work

The current evaluator cannot safely be submitted wholesale to a worker.
`DocumentProjectionFeatures._render_document_region` temporarily changes shared
canvas render flags, region/scale/phase state, and underlay context. Traversal
reads the live chapter, selections, gesture previews, and image stores while
updating render caches. `effect_pipeline.render_stages` also captures masks and
target layers, touches effect caches and provisional revisions, and can invoke
GPU rendering tied to a graphics context. A private output QImage does not
isolate these dependencies. Prepared Distort caches are currently GUI-owned.

The first safe boundary is after source capture and dependency resolution:
prepare immutable source pixels, copied modifier parameters, mask/color fields,
transforms and bounds on the GUI thread; run expensive CPU kernels using
worker-owned state; then return completed pixels through a queued result.
Requests need document, configuration, and content revision identifiers so the
GUI can reject obsolete results after edits, undo, or document replacement.
Publish a coherent finished view while retaining the previous complete
presentation during pending work. Independently publishing source/destination
tiles of a moved object could temporarily duplicate or erase it.

This boundary still leaves source capture and final composition on the GUI
thread; its benefit must be measured rather than assumed. Moving traversal too
requires an immutable scene evaluator. Verification must include continuous
pen input during first visits and extreme zoom-out, stale-result rejection,
cross-tile edits and undo, document replacement, and finished-frame comparisons
against the same frozen reference. No asynchronous rewrite is claimed here.

## What the supplied capture shows

`drawing-performance-20260926-085407.json` records 186.6 seconds of activity:

| Recorded measure | Evidence |
|---|---:|
| GUI heartbeat stalls | 59 |
| Longest heartbeat gap | 15.40 s |
| Longest paint | 15.23 s |
| Recent paint p95 | 74.36 ms |
| Recent brush continuation p95 | 0.89 ms |
| Longest brush continuation handler | 33.9 ms |
| Tablet moves observed | 17,995 |
| Camera changes | 214 |

Several multi-second freeze stacks enter retained projection generation,
modified images, and synchronous Distort MLS/mesh computation. Other long
stalls include outline distance computation and pattern rendering. The active
drawing raster itself has no modifiers; expensive neighboring artwork can still
be revisited when a frame or effect cache is invalidated.

The approximately 59-second `tool_press` spans overlap nested selection-candidate
dialogs. They are not an isolated measurement of a brush stamp. Phase timings
are inclusive and overlap. The capture retains 512 recent samples per phase and
only its last 3,000 general events; 86,154 earlier events were overwritten.
Heartbeat maxima and completed phase maxima remain useful even when a recent
percentile no longer includes an earlier freeze.

Measured control-refresh maxima are small (roughly 2.3 ms or less), and no
`navigator.render` phase appears. MainWindow ancillary behavior needed coverage,
but this capture does not support blaming its settings controls for the
multi-second stalls.

## Gaps in the earlier benchmark

- It used an older chapter copy with 95 objects and 31 effects. This capture's
  chapter has 96 objects, 43 effects, and 9 masks.
- Its edited raster was near y=300. The reported drawing raster is
  `9db9d8094e744ee4aaffb84e9db10f7c` (Raster 34), near y=19,500–19,900, over a
  different collection of distorted reference images.
- Its six short helper moves waited for finished rendering between operations.
  They could not measure sustained tablet input accumulating behind a stalled
  GUI thread, event-loop paint scheduling, or input-to-next-paint delays.
- It constructed Canvas directly, excluding normal MainWindow selection/tool
  synchronization, event filtering, refresh signals, and autosave behavior.
- It disabled predictive ink and the grid. The actual copied settings enable
  both, as well as pencil transform handles. The chapter also contains a
  `show_on_top` Draft object. Together, predictive ink and promoted artwork
  selected a separate full-scene rendering path during drawing.
- It did not reproduce draw–navigate–draw sequences at approximately 2.864× and
  −45° rotation, the captured 0.694× view, or the extreme 0.053× view.

The earlier warm-navigation numbers remain measurements of those specific
operations. They were insufficient evidence for real-world drawing smoothness.

## Reproducer and isolation

`tests/benchmark_drawing_stutters.py` is a manual native Windows harness. It
loads only `.artifacts/drawing-stutters-20260926/project-copy`, whose main
chapter matches the capture's object/effect/mask inventory. No autosave chapter
manifest is present for that copied chapter. The user settings snapshot supplies
the real brush sizes, pressure curve, predictive-ink, grid, and collapsed
navigator settings.

The harness creates a real MainWindow with its normal signal connections and
event filters. Before construction, it replaces the Blender controller with a
nonconnecting signal stub. It never starts the file-launch broker or interacts
with the live editor. The window uses `WA_DontShowOnScreen` and
`WA_ShowWithoutActivating`. Settings and actual autosave jobs write only into
the run's separate sandbox; complete before/after SHA-256 manifests verify the
8,685-file input copy remains unchanged.

A producer thread posts real tablet events at 120 Hz while the GUI event loop
handles input, scheduled paints, ancillary refreshes, and autosaves normally.
There is no per-move repaint or wait-for-effects loop. The default sequence uses
four two-second strokes, alternating pencil and eraser. Tablet compression is
disabled, matching the application launcher. Generated pen coordinates,
pressure, posting/dispatch timestamps, frames, and phase details are preserved.

The new `--navigate-between-strokes` sequence schedules pan drags of 120 pixels,
15° rotations, and two Ctrl-wheel steps through the application's navigation
methods and wheel events, followed by drawing and an inverse return sequence.
It reports first-press queue delay after navigation and per-stroke frame/queue
statistics. `--cold-only --scenario extreme` separates the extreme view from
sustained input. The final measurements above include both scenarios.

The capture does not contain the raw pen path, contact timestamps, pressure
sequence, touch gestures, or full popup interaction sequence. Thus this is a
controlled reproduction using the recorded document/settings/camera conditions,
not an exact playback of the user's session. Posting lateness due to producer
scheduling/GIL contention is reported separately from time already spent in
Qt's input queue. Paint completion is not physical pen-to-screen latency.

## Initial sustained-input measurements

The frozen pre-fix source is in
`.artifacts/drawing-stutters-20260926/baseline-source/comic_editor`. The first
current-code comparison includes effect-cache preservation and spatial mesh
query improvements, before fixing the predictive-ink/promoted-artwork path.

| Static-view scenario | Measure | Frozen baseline | First current run |
|---|---|---:|---:|
| Rotated 2.864×, four strokes | Paint p95 / maximum | 294 / 3,491 ms | 105 / 270 ms |
| Rotated 2.864×, four strokes | Input queue p95 / maximum | 3,143 / 3,491 ms | 95 / 271 ms |
| Rotated 2.864×, four strokes | Median paint | 86.8 ms | 92.5 ms |
| Unrotated 0.694×, two strokes | Paint p95 / maximum | 112 / 453 ms | 67.6 / 263 ms |
| Unrotated 0.694×, two strokes | Input queue p95 / maximum | 263 / 446 ms | 64.7 / 258 ms |
| Unrotated 0.694×, two strokes | Median paint | 58.3 ms | 56.3 ms |

All 968 rotated-scenario inputs and all 484 low-zoom inputs were dispatched in
each version. Frozen rotated producer lateness peaked at 18.8 ms, while queued
inputs waited almost 3.5 seconds: this reproduces GUI backlog, not a slow input
producer. Each scenario committed the expected real strokes and submitted
sandbox autosaves. Every input-copy file hash remained unchanged.

These initial changes reduced large stalls, but the 93 ms rotated drawing median
was still unacceptable for smooth drawing. Instrumentation isolated only
19.5 ms median retained-tile collection while total painting takes 92.5 ms.
With predictive ink and any promoted artwork, `paintEvent` takes the
`promoted_ink` branch: it collects the retained scene and then draws a fresh
full-scene QImage to preserve live-ink ordering. That path escaped the old
benchmark because predictive ink was disabled. The subsequent retained-phase
repair preserves the established artwork/ink order.

The frozen low-zoom first paint takes 7.66 seconds even though its warmed
sequence's worst paint is 453 ms. The session's 14–15 second cases are not all
reproduced by static warmed drawing. Cold views, navigation, selection/solo
changes, and the extreme zoom therefore remain required verification.

Run artifacts are `baseline-rotated`, `baseline-lowzoom`, `current-rotated`, and
`current-lowzoom` under the drawing-stutters artifact directory. Current runs
also identify modifier types/IDs, owning entity names, source/output sizes,
outline distance-build counts, and GUI-versus-worker execution. Inclusive worker
effect durations must not be mistaken for GUI-blocking durations or added to
their parent paint times.

The next preserved iteration, `retained-ink-*`, removed the live-ink full-scene
fallback. Its rotated static paint p95/max was 34.4/125 ms and input queue
p95/max was 24.3/123 ms. Navigation still reached 943 ms painting and 666 ms
input queue delay, with nine repeated mesh calls using the same source in one
frame. Its extreme first paint was 11.96 s and still computed blueprint
deformation/twirl twice. Prepared-source reuse, canonical finished mesh tiles,
and protected-prefix retention produced the final results at the top of this
report. Every intermediate artifact remains intact.
