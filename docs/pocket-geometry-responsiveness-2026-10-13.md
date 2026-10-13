# Pocket Boyfriend geometry responsiveness, October 13, 2026

The overnight investigation is complete. It began at 10:51:48 UTC and continued
past 18:54 UTC on October 13, 2026. The final measured application is immutable
iteration 34; all 332 application files match the delivered checkout.

Continuous geometry now publishes artwork during input rather than repeatedly
cancelling it until release. Ordinary tested shape/page edits first changed
artwork at 188–207 ms instead of 3.1–3.3 seconds. Larger groups first changed
at 486–1,252 ms. A cold dense hide improved from 5.6 seconds to 330 ms, and
repeated warm visibility changes had a 291 ms median. Very short dense edits
also show a fresh committed preview before the expensive native result.

The dense effect stack still has roughly one-second cold initial responses,
some longer intervals without changed artwork, and 6.9–7.8-second exact final
renders after release. High requested input rates are not sustained. The
measurements below distinguish actual artwork, handle movement, input duration
and final native completion. They include complete pixel/model equality,
225 successful repeated geometry/Undo cycles across the three endurance runs,
50 dense visibility cycles, final targeted tests and real Windows UI checks.

## Project and observation

The investigation uses a fresh, isolated full copy of Pocket Boyfriend. The
manual chapter has 109 layers, 182 objects, 108 modifiers and 31 masks, at
1080 × 82000 canvas pixels. The original project is read-only throughout this
workflow. The initial full copy contained 15500 files and 528295688 bytes;
all original/copy SHA-256 values matched at preparation. A second isolated
copy, excluding recovery directories, is used for physical Windows UI input.
It has independent application preferences and performance logs.

Immutable application snapshots and result files are under
`.artifacts/overnight-geometry-20261013-0652`. Iteration 01 preserves the dirty
checkout as it stood at the beginning of this run, including earlier fixes.
This avoids treating unrelated checkout changes as changes from this run.

Native diagnostics use the application's ordinary input handlers, scene
captures, evaluator, history and GPU canvas, with a 1042 × 937 widget. A
requested three-second, 60 Hz gesture is observed through actual framebuffer readbacks
at frame swaps. Handles and cursors are omitted from that observer so a moving
gizmo cannot count as updated artwork. The application heartbeat and readback
cost are recorded separately. These are framebuffer observations, not claims
about physical monitor scanout or stylus input-to-photon latency.
Actual input duration is recorded separately: delivering the requested input
can take longer when the event loop and rendering share CPU time.

Early diagnostic versions sampled small patches and then a horizontal band.
Those observations can miss affected artwork; their numbers must not be mixed
with full-frame measurements as if they used an identical observer. Formal
comparisons use the complete framebuffer in both builds. A changed hash proves
visible pixel changes but does not by itself prove the edited pose is correct.
Complete final pixels, model records, native reference renders, and actual UI
operations supply separate correctness evidence.

## Causes and changes

A very short drag can finish before any preview was painted. The actual
release now witnesses its focused transaction and single command push, then
may render a fresh committed provisional frame through the existing isolated
reveal lane. An intervening controller timer preserves that witness until the
first committed paint supplies coverage. No unfinished live image is relabeled
current. Real boundary/translation tests cover both never-started and active
unpainted workers, native cold tiles, full model Undo/Redo and context retirement.
For a quick dense boundary edit, iteration 27 waited 7,067 ms for visible
artwork; iteration 29 showed current provisional artwork at 670 ms. Complete
native framebuffers and models matched. This extra frame has a cost in cheap
cases: an already-fast page view changed at 139 ms without the new frame and
173 ms with it, and final native presentation took 127 versus 258 ms. It is a
responsiveness measure for expensive edits, not a universal native-render speedup.

The physical iteration 29 capture also showed repeated full checkpoint
snapshots costing up to 297 ms of thread CPU. The writer now uses a private
read-only view of immutable published contexts/events/stacks; mutable counters,
ring lists and stall/active-span shells are still captured under lock. UI and
export snapshots keep independent nested copies. Replaying the actual stopped
5.50 MB metadata with shared contexts restored and bounded phase samples
reconstructed reduced mean snapshot wall time from 19.26 ms to 0.109 ms over
100 iterations, with identical contents. This replay is not a measurement of
the whole live writer or an attribution of every recorded GUI stall. Publication,
epoch replacement, active-span isolation, writer formats and owned overhead
checks passed before further physical capture.

The subsequent physical iteration 30 capture preserved 140 writer checkpoints.
Its snapshot mean was 0.409 ms and maximum 2.745 ms, compared with 21.95 ms and
296.85 ms across 58 checkpoints in the iteration 29 capture. Those captures
differ in duration and activity, so their overall stall counts are not a matched
latency comparison. The later capture still includes a 240 ms summary-generation
outlier and a 557 ms heartbeat gap. It supports the snapshot improvement, not a
claim that all monitoring or native-event-loop pauses are resolved. The actual
private page edit, Undo, save and reopen all reached the correct artwork.

Continuous geometry input repeatedly superseded complete scene work. A render
could finish only after the user stopped, although the handles had already
moved. Bounded geometry captures now retain the actual immutable changed
records and complete original source owners for one positively identified
gesture. One active render and the latest pending render may complete during
that gesture. A displayed older preview retains its actual captured revision;
it is provisional artwork, never relabeled as current exact pixels.

When input advances the model before its queued paint, the geometry timer now
revalidates that same owner and view and dispatches the newest frozen pose.
Previously it could yield to another paint on every tick and never submit the
prepared updates during rapid input. The regression test delivers the timer
between real input and paint and verifies a current pending pose without
cancelling the active immutable render.

The owner includes document, view, camera, display density, selection, tool,
history, source versions, render configuration and dependency generation.
Changes outside that owner retire its previews. The last preview that was
actually painted can stay visible through that gesture's own release command
while the committed native render is pending. Undo, another edit, source
mutation or any context change retires this dated presentation too.

Complete original source headers can be reused across known metadata-only
captures without rescanning or decoding unrelated source tiles. New captures
retain independent edited/COW headers, native versions and original file pins.
Unknown, structural and resource edits keep ordinary invalidation. Worker
source preparation finishes once per snapshot; native input-source snapshots
share that same preparation contract.

Bounds queries reuse identical ancestor transforms within one synchronous
subtree traversal. No mapping survives that query: the next pointer edit,
source change or caller-supplied live mapping follows the ordinary calculation.
Nested projective siblings and effect bounds match the uncached calculation.

For boundary edits already classified by the existing effect/mask logic as
full-chapter damage, the input path now uses that result directly. It avoids
walking the whole child subtree twice per pointer move merely to union those
bounds with the full chapter. Finite-effect cases retain their original bounds
calculation. Focused mask/Halftone tests compare complete native projection
tiles against an independent cold scene and verify full model/pixel Undo/Redo.

Tests also exposed a cold-start inconsistency: the first ordinary source
capture could use a different draft source-frame path from a warmed gesture.
After that capture completes, the same actual-owner/source/admission guards
now select the existing bounded geometry path before evaluation. Full cold
and warmed frames match for masked HSL/HSL/Outline and attached-rig transforms.

A separate queue stall occurred when an oversized scene tried to reserve its
own nested graphics copies while already owning private capture storage.
The existing admission ledger now recognizes its positively owned capture
lease, including the active and pending leases of that exact interaction
scope. Every retained byte is still charged; foreign nonempty leases remain
foreign. Empty metadata leases consume no pixel storage. A cancelled caller
also exits a graphics-copy wait rather than waiting indefinitely.

The existing bounded transient source-frame policy is used for supported
geometry drafts, including smudge. Original source buffers are preserved.
Large raster/source footprints keep the ordinary native viewport path.
These draft results remain private to the gesture, never move into native
exact caches, and cannot enter disk recording, saved artwork or output.

Mesh candidate lookup uses smaller blocks for dense, small draft queries and
a bounded face/block intersection table. Candidate order, coordinate math,
interpolation, source grids and native output are unchanged.

The optional verified radial RGBA sampler had been disabled for the locally
installed SciPy 1.17.1. That version has now passed the held original native
oracles and is admitted alongside the previously verified 1.18.0. Unsupported
versions and input types retain the original SciPy path. The C source, binary,
ABI, radial sample counts, global grids and floating-point arithmetic were not
changed by that version-admission adjustment.

Repeated serialization of immutable scene settings was another measured cost.
The evaluator now retains at most 8 MiB of encoded settings for records owned
by its current frozen snapshot. It does not cache complete semantic keys:
pixels, live poses, masks, render contexts and other dependencies continue to
be checked each time. Derived preview records and a new snapshot serialize
independently. This metadata is not transferred through scene pixel caches.

A further isolated Windows compiler experiment kept the scalar arithmetic and
passed 102 native/reference cases. The next component evaluates independent
double channel pairs with SSE2 under MSVC's strict floating-point mode. Each
channel still multiplies y then x and adds corners 00, 01, 10, 11 in that order,
rounding to float32 before the unchanged angular accumulation. Native sample
counts, coordinates, source pixels, masks and cancellation cadence are unchanged.
The scalar source route remains available to other builds; unsupported delivery
retains SciPy. The verified shipped paired component passed the 102 held native
oracles again. Complete dense-project pixels/model matched in a separate run.
A 96×96 sampler check measured about 0.12 ms versus 0.38 ms for the scalar MSVC
component; that small-kernel timing is not a whole-project speed claim.

Revealing a dense shape was a separate committed-operation delay: an ordinary
native composite had to finish before the shape could appear. A typed
visibility-only reveal can now request one current provisional presentation
using a complete ordinary immutable snapshot and the same isolated evaluator.
The real document stays committed with its actual revision. The bounded
temporary source devices never acquire an exact/durable cache specification,
recording permission or native tile adoption. Normal exact rendering follows
immediately. Actual change/record/source ownership and view, selection, tool,
history and configuration guard this presentation. Small hides retain the native
path; other edits and unsupported modes retain their ordinary behavior.

Repeated dense hide/reveal testing found that always adding that presentation
could slow an already warm reveal. Iteration 31 showed warm reveals around
420–484 ms, while iteration 01 generally showed their native result around
333–389 ms. The extra preview also delayed native completion. The final
iteration 32 introduced a scheduling hint for the last two fully ready native
visibility states of an unchanged scene/view, with at most 512 model records
and 256 native addresses. A matching reveal uses the ordinary renderer directly.
Any other typed edit or controller reset clears the hint; view, configuration,
source and history changes cannot match it. No image, source snapshot, cache
descriptor or worker cache is retained or adopted by this hint. Actual cache
misses still render normally through the same semantic keys and validation.
An unchanged native document/view avoids repeated metadata scans during hover.
The cold-reveal, warm/context, empty-scene-cache fallback and recent-use bounds
checks passed alongside geometry release/publication and handoff checks
(72 cases). A subsequent 25-round dense visibility run measured a 295 ms median
for both first changed artwork and native completion, compared with 462 ms
and 742 ms in the intervening iteration 31 and 361 ms in iteration 01. The
first lifecycle round still took 1168 ms; later iteration 32 rounds were
282–312 ms. Every complete hidden/shown pixel buffer and model matched within
each build, and the whole hidden/shown results matched between builds. Retained
charged copies and running reservations returned to zero after every round.

A subsequent cold hide of that same 41-descendant shape still took 5,620 ms
to change on screen in iteration 32. Iteration 34 extends the same current
committed presentation lane to complex hides. A metadata-only traversal stops
after more than 32 reachable entities; small hides remain on the ordinary
path, and recently ready visibility states still skip the optional preview.
The identical cold-hide observer then measured 330 ms to changed artwork,
with complete final framebuffer and model equality. Native completion changed
from 5,610 to 5,856 ms after the operation, so this improves visible response
with a modest extra render cost. The 76 focused reveal/hide/context/native
reference, geometry release/publication and handoff cases passed. An initial
unhashable child-reference error was caught by those tests and fixed before
the successful immutable iteration 34 measurement; the failed iteration 33
logs and snapshot are retained.

## Final matched measurements

The final sequential full-frame comparison uses immutable iteration 34 against
the starting iteration 01, without simultaneous tests or UI captures. Each
requested three-second geometry gesture uses the same observer and native GPU canvas.

| Operation | Starting build: first changed artwork | Final build: first changed artwork | Distinct artwork states during input, starting → final |
| --- | ---: | ---: | ---: |
| Layer 55 boundary | 3130 ms | 190 ms | 0 → 46 |
| Layer 55 translation | 3326 ms | 188 ms | 0 → 26 |
| Layer 55 hide/show | 100 ms | 92 ms | 1 → 1 |
| End page boundary | 3129 ms | 207 ms | 0 → 40 |

Every complete settled framebuffer and model record matched between builds.
The boundary/translation/native-final delays after release were 125, 298 and
148 ms respectively in the final build. The native hide/show result took
113 ms after the sequence. `formal-r54-summary.json` preserves the exact
timestamps, observer cost, heartbeat maxima and complete equality receipts.
The actual final-build boundary, translation and page input durations were
3.89, 3.06 and 3.57 seconds, versus about 2.99 seconds in each starting-build
case. Consecutive changed artwork arrived at median intervals of 77, 109 and
82 ms in the final build. Including initial delay and the tail to release,
the longest interval without changed artwork was 190, 188 and 207 ms; each
starting-build framebuffer remained unchanged for the whole gesture.
`final-visible-cadence-r54.json` preserves those full-frame cadence receipts.
These intervals measure visible changes, not the age or correctness of every
intermediate pose.

The final extended round (`extended-r55-summary.json`) also matched every
complete settled framebuffer and model between iterations 01 and 34:

| Operation | Starting first artwork | Final first artwork | Artwork states during input, starting → final | Final native frame after release |
| --- | ---: | ---: | ---: | ---: |
| 68-descendant boundary, 10-second request | 13679 ms | 486 ms | 0 → 49 | 3174 ms |
| Dense 41-descendant boundary | 18763 ms | 990 ms | 0 → 4 | 6927 ms |
| Dense 41-descendant translation | 19539 ms | 1074 ms | 0 → 4 | 7774 ms |
| Dense reveal | 2935 ms | 684 ms | Single operation | 3524 ms |
| 68-descendant translation | 7820 ms | 1252 ms | 0 → 5 | 4665 ms |

The dense cold boundary still had a 1,918 ms interval without changed artwork
during input. The larger translation's longest such interval was 1,252 ms,
with a 433 ms median changed-artwork interval. These heavy cold cases remain
visibly imperfect, although they no longer wait for release before showing
any movement. Native completion of the dense effect stack remains 6.9–7.8
seconds after release. The larger boundary's changed-artwork median was 186 ms
and longest interval 486 ms across its actual 9.96-second input.
`final-visible-cadence-r55.json` preserves every cadence summary.

The eight-second 120 Hz requested burst took 8.53 seconds to deliver in the
starting build and 24.40 seconds in the final build. First artwork improved
from 8,677 to 211 ms and observed artwork states from zero to 174; the final
median changed-artwork interval was 122 ms and largest gap 300 ms. The event
loop did not sustain the requested input rate. This is evidence of continuous
visible publication under pressure, not a 120 Hz or universal input-throughput
improvement.

## Earlier measurements and optimization evidence

The dense Layer 50 example has 41 descendants and a mesh/radial effect stack.
With the full-frame observer, iteration 01 showed no changed artwork during
the three-second boundary drag and first changed pixels at 19094 ms. The
intermediate iteration 16 first changed pixels at 1832 ms and displayed two distinct
artwork states before release. Its first final native artwork frame was at
10760 ms after release, compared with 16098 ms in that baseline. This remains
too slow for the initial dense-group preview and final render, and motivated
the subsequent work and final measurements above.

The first matched full-frame comparison round used iteration 01 and 16. Layer
55 has 11 descendants; the End page has 23. Times include the same actual
framebuffer observer in both builds.

| Operation | Baseline first changed artwork | Candidate first changed artwork | Distinct artwork states during input, baseline → candidate |
| --- | ---: | ---: | ---: |
| Shape boundary | 3121 ms | 205 ms | 0 → 29 |
| Shape translation | 3319 ms | 189 ms | 0 → 28 |
| Hide/unhide | 109 ms | 92 ms | 1 → 1 |
| Page boundary | 3165 ms | 317 ms | 0 → 20 |

All four pairs had identical complete final framebuffer pixels and model JSON.
Full-frame observer maxima ranged from 15–45 ms and heartbeat maxima from
82–119 ms, so these measurements are not claims of a 60 Hz input-to-photon
budget. Final native frames after release were 127 ms, 359 ms, 104 ms and
127 ms in the candidate; that last-stage time is distinct from the much larger
improvement in artwork updating during the gesture. Results and complete raw
timelines are preserved in `formal-r1-summary.json` and its result directories.

A second matched round (iteration 01 versus 21) measured 3195 → 207 ms for
shape boundaries, 3342 → 243 ms for translation, 105 → 106 ms for visibility,
and 3147 → 246 ms for page boundaries. During the three-second input the
candidate displayed 48, 24 and 51 distinct artwork states in the boundary,
translation and page cases. Complete final pixels and model records again
matched for all four pairs. This repeat does not show a visibility speedup.

A third matched round (iteration 01 versus 24) measured 3136 → 228 ms for
shape boundaries, 3324 → 188 ms for translation, 96 → 91 ms for visibility,
and 3138 → 241 ms for page boundaries. The candidate displayed 50, 26 and 55
distinct artwork states during the three-second geometry gestures. Complete
final pixels and model records matched again.

The dense Layer 50 timer-catch-up diagnostic (iteration 20, lightweight
in-memory scheduling audit) first changed artwork at 955 ms and showed five
distinct states during the gesture. All 22 completed held previews were
accepted against the same owner; the first retained revision was 8 while
the current input had reached 36. Complete final native pixels and model
records matched the baseline. Its final render still needed about 9.6 seconds
after release. A separate 96px initial-draft experiment measured 845 ms; that
small single-run difference has not justified a production quality change.

A focused dense-shape reveal test measured 3110 ms to first changed pixels in
iteration 21. Iterations 23 and 24 first showed the current provisional shape
at 711 and 708 ms. All complete final native pixels and model records matched.
The final native frame still took approximately 3.6 seconds after the reveal;
that time includes the preliminary presentation work and is distinct from the
first visible update. Publication/retirement tests hold native work to
verify an actual current reveal is painted, then reject it after view,
selection, tool, history, source, foreign edit or subsequent hiding changes.

The extended iteration 01/25 round used the same complete-frame observer and
required complete settled pixel/model equality for all six pairs:

| Operation | Baseline first changed artwork | Candidate first changed artwork | Candidate distinct states during input |
| --- | ---: | ---: | ---: |
| Layer 55, 960-point burst | 8598 ms | 215 ms | 180 |
| Blanket group, ten-second boundary drag (68 descendants) | 13216 ms | 563 ms | 42 |
| Dense Layer 50 boundary | 22139 ms | 1072 ms | 5 |
| Dense Layer 50 translation | 19495 ms | 1082 ms | 4 |
| Dense Layer 50 reveal | 2974 ms | 683 ms | — |
| Blanket group translation | 7703 ms | 2421 ms | 5 |

The deliberately delivered 120 Hz burst was not sustained at 120 Hz: 960
points took 8.45 seconds in the baseline and 26.02 in the candidate with its
additional previews and full-frame readbacks. It proves continued publication
under backlog, not a 120 Hz input or presentation budget. The ten-second group
test took 9.96/9.98 seconds, so its comparison also covers a sustained timed
gesture. Final dense native frames still took 8.65 seconds after boundary
release and 7.84 after translation; the larger Blanket translation remains
2.42 seconds to first visible artwork and 4.69 seconds to final native output.
These are remaining limitations, not hidden inside the first-update gains.
Raw results are preserved in `extended-r30-summary.json` and its directories.

The iteration 01/27 extended comparison also passed complete settled
pixel/model equality for all six pairs. First visible updates were 246 ms
for the Layer 55 burst, 555 ms for Blanket boundary input, 1083 ms for dense
boundary input, 1039 ms for dense translation, 685 ms for dense reveal and
1161 ms for Blanket translation. Dense boundary input took 3.51 seconds,
versus 5.38 seconds in the iteration 25 comparison; Blanket translation input
took 3.23 seconds versus 4.33 seconds. These comparisons are single runs,
with short correctness tests overlapping the beginning of this round; later
isolated rounds will establish the final timings. Final dense native frames
still required 7.5–7.9 seconds. The source, commands and complete equality
receipts are in `extended-r35-summary.json`.

Physical Windows UI tests on the independent copy covered an explicitly
identified upper-left Layer 55 boundary drag, translation of its children, hiding/unhiding it, undo of
visibility and placement, and resizing the End page boundary. Screens showed
the changed artwork, including moved child content and changed page clipping.
An earlier lower polygon handle overlapped an attached Vector rig; its movement
is treated as modifier-edit evidence rather than a verified boundary edit.
Saving and reopening the private copy preserved the End page's moved top edge.
Only two page-node y coordinates changed in saved chapter data; the original
artwork source, raster, mask and embedded image files remained byte-identical.
The stopped performance capture is preserved as `candidate-ui14-final` in the
campaign's capture checkpoints. Monitoring and screenshot automation add
overhead; those UI calls are qualitative workflow evidence, not substituted
for the matched native timing measurements.

## Correctness evidence

Complete final framebuffer pixels and complete final model JSON matched
iteration 01 for the initial Layer 55 boundary, translation and visibility
cases and for the dense Layer 50 native result. Subsequent formal comparisons
require that same equality before they report a successful pair.

Focused tests cover source-header reuse and native version invalidation;
immutable record serialization; real older-preview publication during input;
release continuity; retirement on view, selection, tool, history, source and
foreign-edit changes; drawing-preview publication; private-copy admission and
cancellation; and preservation of ordinary source and pixel contracts.
The wider suite's first five failures were reproduced unchanged in frozen
iteration 01: two brush-error fixtures lack the fake application's icon method,
one File-menu fixture expects the old toolbar order, one anisotropic distortion
fixture expects cropped allocation despite the existing native-frame plan, and
one rotated border-patch case differs by two channel units. Those failures are
preserved in the campaign logs and are not weakened to make this run pass.
The mesh/native allocation checks passed 52 cases. The radial C/SciPy/native
reference, mask and cancellation checks passed 102 cases on the actual local
1.17.1 wheel. The broader native output, persistence and repeat-run results
are recorded below.

The first repeated-edit endurance attempt completed 22 full pixel/model/Undo
checks, then faulted inside Python's code-line lookup while the diagnostic
watchdog was printing a periodic traceback. The Windows fault address is 100
bytes after the local `PyCode_Addr2Line` export, and its last dump stops between
the worker filename and line number. This resembles the interpreter race
reported in [CPython issue 116008](https://github.com/python/cpython/issues/116008)
and [issue 158200](https://github.com/python/cpython/issues/158200); that
attribution is an inference, not a captured native stack. An unchanged iteration
25 repeats the same editing sequence without asynchronous periodic traceback
dumps. Fatal-exception logging remains enabled. The helper change does not
change editor, renderer or sampler code. The unchanged iteration 25 subsequently
completed 120 cycles over 15 minutes, with complete pixel/model equality after
every Undo and final render, no fatal traceback and no retained charged pixel
copies between cycles. First visible artwork had a 174 ms median and 239 ms
95th percentile. A dense 41-descendant translation run completed another 35
cycles over 14 minutes with the same equality checks. Its private memory rose
initially and plateaued around 2.34 GB; native completion remained roughly
eight to nine seconds. These results do not establish the crash attribution.

The final immutable iteration 34 completed 70 further real boundary-edit
cycles on the 68-descendant Blanket group over 21 minutes 27 seconds. Each
cycle requested ten seconds at 15 Hz plus a one-second in-gesture pause,
then checked complete current pixels/model and complete restored pixels/model
after Undo. All checks passed, with 46–50 changed artwork states during every
gesture. First changed artwork had a 374 ms median, 450 ms 95th percentile
and 330–504 ms range. Exact native completion after release remained
3.28–3.75 seconds. Charged retained pixel copies and running reservations
returned to zero between every cycle; zero-byte metadata leases could briefly
remain. Private memory rose from 2.170 to 2.364 GB, peaked at 2.386 GB and
fluctuated between 2.251 and 2.380 GB across the last 20 cycles. The fatal log
was empty. This bounded observed range is not a proof that every allocation
is leak-free. `final-endurance-r56-summary.json` and all 70 raw rows preserve
the actual durations, equality checks, memory and admission counters.

The remaining suite was run as 266 independent module processes, preserving
all failures and assertions. All GPU modules used the actual Windows platform
instead of Qt's context-less offscreen mode. Updated lifecycle test doubles
retain the original presentation retry assertions (40 passing cases). Cold
geometry policy, HSL and handoff checks now exercise actual owned captures
(60, 12 and 17 passing cases respectively). The predictive-ink wait timeout
and stylus Smudge-handle failure reproduced against frozen iteration 01.
Four further failed modules also reproduced against iteration 01: the stylus
vector fixture lacks `_raster_contact_active` on its uninitialized fake canvas;
seven proposed text-decoration tests inspect an unpainted presentation;
the general UI fixtures expect the old Theme/Grid tab and omit `theme_library`
on their fake settings dialog; and two view-settings expectations hard-code
`#242428` while the shipped theme uses `#232530`. Their assertions and logs are
preserved. These baseline reproductions do not make the full suite green.
The focused-grid dialog fake also failed unchanged. A second border case
reproduced the identical 47 two-unit channel differences under the shipped
Dark theme; that production presentation file is unchanged from iteration 01.
An earlier combined run faulted in Qt image decoding, while isolated module
runs completed that module. No renderer fix or established attribution is
claimed for that separate process-state fault.

The final broad isolated batches attempted all 431 test-module files against
preserved application versions 31 and 32. Reconciled results contain 8,768
cases: 8,649 passed, 100 skipped, 19 failed, with no remaining collection errors.
All 19 remaining failures belong to the baseline-reproduced categories above;
this is not a green full-suite claim. The raw first-batch XML, per-module
outputs and each failed case are retained in `final-isolated-validation-batches.json`.
The reconciled file lists every module's actual immutable application version.

Three isolated-harness dependencies were repaired without weakening assertions.
The workspace root was restored to the frozen wrapper's import path, after
which four brush-playground fidelity cases and two startup-error cases passed.
The latter module still fails its two original fake-application icon cases.
The autosave module's save-settings spy had left its private preferences file
missing; valid default preferences are now initialized in that temporary test
directory. All 40 autosave cases then passed. The stalled process was positively
identified as the private test worker before termination; its partial output
and termination receipt remain preserved. `test_live_radial_native_reference.py`
is a diagnostic helper with no collected cases, not a product-test failure.
`final-reconciled-validation.json` records these replacements and the original
raw receipts. The final affected-module run is separately pinned to version 34.
That final run passed all 325 cases across 15 focused modules, with no skips,
failures or errors. Its actual Windows GPU presentation retry run passed all
40 additional cases. These include the final cold complex-hide path, warm
visibility scheduling, real continuous/released geometry publication, native
source invalidation and admission, cancellation, HSL/rig/effect native
references, bound traversal and damage classification, strict radial samples,
and recorder/encoding isolation. XML and full output are preserved as
`final-targeted34` and `final-gpu-retry34`.

The final short-gesture repeat compared versions 27 and 34, delivering one
real dense boundary move and release in about 13 ms. First visible artwork
improved from 7,053 to 576 ms, with complete native framebuffer/model equality;
native completion was 7,040 versus 7,426 ms after release. This independently
repeats the quick-release improvement while retaining its extra exact-work
cost. `final-quick-dense-r57-summary.json` records both runs.
The final version 34 then passed 25 additional dense hide/show cycles: median
first artwork and native completion were both 291 ms (279–894 ms range).
Every hidden/shown complete native buffer and model matched both within the
run and against version 32's earlier 25-cycle reference. Running reservations
and charged copies settled to zero; the fatal log was empty. Private memory
fell from 1.996 to 1.827 GB. See `final-warm-visibility-r58-summary.json`.

Computer Use also exercised version 34's real MainWindow on the independent
UI copy: the End page's top boundary moved from screen y=193 to y=223, the
artwork reached that new boundary, and Undo restored both. Hiding the page
removed its artwork and revealing it restored the correct drawing. The saved
private copy and optional exact cache were closed normally. These screenshot
observations validate the workflow; their automation timing is not substituted
for the matched framebuffer measurements. Earlier baseline-editor changes
were also saved to their private copy after all native comparisons finished.

Exact final document tiles remain one sample per canvas pixel. Camera zoom or
display density does not raise artwork cache resolution. Draft and dated
presentation storage is process-local and cannot become durable exact cache
entries. Memory and disk still use the same semantic identities, dependency
validation, exact results and rendering pipeline.

Final SHA-256 verification found all 15,500 original project files unchanged,
with no additions, removals or changed files (`original-integrity-final.json`).
Original application preferences retain their pre-run 3,128,522-byte size and
2026-10-13 03:09:52.354025 UTC modification time; UI and tests used independent
preferences paths. This is a size/time check, not an initial/final preferences
hash comparison. The original project was never used for edits or saves.
The final build's 332 measured file hashes also match the checkout exactly
(`runtime-34-delivered-integrity.json`). All private test editors closed normally.
