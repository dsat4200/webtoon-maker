# Editing responsiveness

The September 2026 performance pass moves expensive display effects, large
fills, and recovery-file writes off the GUI thread. Small effects still render
directly. Larger effects display a bounded preview immediately and replace it
with the exact image when the worker finishes. Export and baking use exact
rendering.

## Measurements

### Text editing capture, 3 October 2026

`text slowness.json` records 70.23 seconds in a 1080 × 54896 chapter with 97
layers, 153 objects, 87 effects and 23 masks. Its 131 navigator renders consumed
42.41 seconds of inclusive wall time. Thirteen of the seventeen stalls recorded
in Text Edit had navigator stacks; the longest gap was 5.73 seconds in smudge
processing. Glyph drawing/layout accounted for only four leaf stack samples.
These sampled stacks identify callers, not native internals, and overlapping
phase totals must not be added.

Typing previously emitted an empty dirty rectangle on every change, discarded
all document tiles on session entry/commit, and emitted a hierarchy change when
committing. The navigator rebuilt the entire chapter. Spatial thumbnail stacks
also retained full-size input captures and mask fields before reducing their
output. Text now invalidates its affected frames and finite effect support;
nonlocal/mask/color dependencies retain conservative full invalidation. Text
sessions defer navigator work until commit. Compact navigator captures use the
existing staged renderer, keeping spatial rigs in document coordinates and
excluding drafts from exact disk caching.

A read-only replay of the saved chapter, with monitoring disabled and Windows
Qt's offscreen raster canvas, measured:

| Work | Before | After |
| --- | ---: | ---: |
| Text commit plus navigator refresh | 8782 ms | 74 ms |
| Navigator refresh attempted during typing | 101 ms | 0.020 ms (deferred) |
| Initial complete navigator build | 10253 ms | 4869 ms |
| Largest sampled mask field | 29,149,201 pixels | 753,424 pixels |
| Total sampled mask pixels across replay | 467,723,034 | 5,383,414 |

The replay performed one cold build, one refresh attempt while editing, then a
commit. It rendered 59 bands before and 30 afterward and submitted no exact
effect jobs. The original chapter SHA-256 remained unchanged. Runs were separate
processes; file-system caches and other machine work affect timings. These are
the navigator workload, not end-to-end GPU key-to-display latency. Initial source
decoding, unsupported/linked-mask captures and cold scene work remain measurable:
the updated first build still took about five seconds, with a 557 ms maximum band.

Regression checks compare native RGBA8 and float/HDR pixels before/after thumbnail rendering for
masked image/raster twirl, mesh, smudge, radial, cage and generic effects. Text
checks compare retained and fresh exact frames for transformed strict/free text,
ancestor blur/outlines, local/command undo, layout properties and Bounds resizing;
they also verify distant tile retention and full invalidation for nonlocal
dependencies. Reproduction and raw measurements are under
`.artifacts/text-performance-20261003/`.
The affected text, navigator, projection, masking, translation and disk-cache
suites passed 352 focused tests. Whitespace validation passed.

Representative Windows measurements from this workspace, in milliseconds.
These measure editor interaction latency; a background operation may take
longer to finish. Performance varies with artwork, hardware, and other work.

| Operation | Previous blocking work | Updated interaction |
| --- | ---: | ---: |
| HSL + blur + outline slider, with chapter navigator | 358 ms | 17–23 ms |
| Drawing through that stack, with navigator | 419 ms | 17–23 ms |
| Moving artwork with that stack | 391 ms synchronous reference | 22–28 ms |
| CPU square halftone, 1080 × 720 | 1450 ms | about 13 ms |
| CPU stippling, 1080 × 720 | 2560 ms | about 14 ms |
| 1080 × 6000 bucket fill | 1073 ms | under 1 ms submission |
| Recovery save, 128 noisy raster tiles | 1344–1703 ms | about 1 ms snapshot/submission |
| Outlined text, 850 × 340 frame, typing + full widget paint | — | about 10 ms |

During the large-fill and autosave benchmarks, a 5 ms GUI heartbeat continued
running while the workers calculated or wrote files. Measured maximum gaps
were approximately 8–11 ms for fills and 8 ms for autosave. Unchanged recovery
revisions are skipped entirely.

Populated targets were also tested: a painted 1080 × 6000 page submitted its
fill in 1.33 ms and kept the maximum heartbeat gap to 11.15 ms. A narrow fill
on a painted 1080 × 20000 page completed in 18.82 ms. Large fills display exact
completed batches while dragging and preserve one undo step for the gesture.

The CPU halftone kernel now samples repeated cell colors once per cell. Exact
CPU images in the modifier benchmark settled about 0.3–0.5 seconds after the
last edit. The GPU square/stippling path measured about 4 ms per update and
reused one source upload; its initial context and shader setup took about
0.4 seconds. The HSL kernel also avoids six full RGB temporary images: the
1080 × 720 hue case fell from 159 to 111 ms and from 131 to 53 MiB of traced
peak working memory.

## Correctness and resource handling

- Effect workers receive detached image, modifier, and mask snapshots. Newer
  edits supersede older work, and replacing a document cancels pending results.
- Temporary images are never stored as exact output or exact parent sources.
  This also applies to nested layers, stroke stacks, mirrors, tiling, and
  halftone target-color sources. Settled images are compared byte for byte with
  synchronous renders in the regression tests.
- The effect queue uses a bounded pool of four workers and combined memory admission;
  independent exact jobs can run together within that budget. Explicitly
  oversized work runs exclusively, without retaining another queued snapshot;
  large documents do not fall back to expensive work on the GUI thread. Exact
  completions and the latest completed stage have separate, byte-limited
  retention so ordinary cache eviction cannot strand a temporary preview.
  A single oversized cache image can occupy its cache exclusively.
- Offscreen generic effects are culled before source capture. The chapter
  navigator uses compact previews without queuing full-resolution work for
  every offscreen page.
- Text outlines reuse cropped silhouette-distance data and keep ordinary text
  updates at full resolution. Typing and deletion keep the text visible while
  the caret and selection are drawn separately.
- Large fills use detached tile work and short batches for reference capture.
  Undo, tolerance replay, cancellation, and stale-document checks remain part
  of the transaction. Unrelated modifier source caches survive fill changes.
- Autosave snapshots preserve raster tiles, masks, original image data, and
  asset ownership. Existing transaction and recovery backups are unchanged.
  Explicit save or close may wait for a conflicting disk transaction to finish.

The same pass fixes the fill eyedropper's hidden modal dialog, reveals and
centers inserted assets in the outliner, and allows direct selection of nested
text while editing combined shapes.

## Large documents and offscreen artwork

The canvas automatically skips layers and objects whose painted bounds are
outside the area being repainted. Scrolling brings them back into rendering;
their artwork remains loaded and editable. Bounds and ancestor transforms are
cached, and live drawing invalidates only the affected object and its parents.
Small brush updates can also skip artwork outside their dirty rectangle.

Bounds include ordinary blur and outline expansion, open-shape stroke geometry,
and children that ignore their parent's clip. Effects or transforms whose
footprint cannot be safely bounded retain the full rendering path. Active
transform previews and effect-source captures bypass culling. Exports continue
to render the complete requested area.

Vector edit commits, undo, and redo retain unrelated stroke images and drawing
indexes. Outliner rows have cached positions, and expansion state is tracked
from expand/collapse events instead of scanning the document on every reset.
Compound geometry signatures are checked only when a compound path is needed,
avoiding a document-wide geometry scan for edits that do not use one.

Representative warmed Windows measurements from this workspace:

| Workload | Previous work / culling disabled | Updated |
| --- | ---: | ---: |
| 1,001 layers + 1,000 text objects, full viewport repaint | 219 ms | 8.7 ms |
| Same scene, 35 × 35 brush dirty rectangle | 223 ms | 9.7 ms |
| Repaint after adding a vector stroke among 1,000 warmed drawings | 133 ms | 14 ms |
| Outliner rebuild, 1,000 layers + 1,000 objects | 100 ms | 1.1 ms |
| Outliner rebuild, 3,000 layers + 3,000 objects | 668 ms | 4.1 ms |
| Saved chapter, top viewport (28 layers + 21 objects) | 8.6 ms | 3.6 ms |

The synthetic viewport benchmark compares culling enabled and disabled on the
same renderer. The saved chapter was loaded read-only and compared at its top,
middle, and bottom; all three views had zero differing pixel bytes. These
measurements describe the listed operations, not a guaranteed frame rate.

The 408-case renderer regression run passed. A subsequent 88-case run covered
the final culling edge cases, hierarchy behavior, vector cache preservation,
and tiled assets. Tests compare culled images with unculled renders, including
pan recovery, zoom/rotation, escaped parent clips, wide strokes, effects,
transform previews, and export/source-capture paths.

## Undo responsiveness

Object moves and property edits that leave the hierarchy unchanged restore
only their changed raster, image, or text records. Unrelated objects and
outliner rows stay in place. Structural edits and image-source relinks continue
to use the complete restore path. Within vector drawings, undo reparses only the changed strokes and
retains the other strokes and their cached images.

Raster stroke undo and redo repaint the affected region. Raster selection
transforms retain tile patches for the changed region, including when one
lasso moves pixels across several selected drawings in a single undo step.

Representative Windows undo-handler measurements, excluding the following
repaint, in synthetic documents:

| Operation | Before | After |
| --- | ---: | ---: |
| Object move, 1,000 layers and 1,000 raster objects | 86.7 ms | 2.3 ms |
| One stroke edit, 5,000 strokes with 10 points each | 212.6 ms | 1.75 ms |

`tests/benchmark_undo.py` measures the current handlers with isolated settings
and checks restored document state. Timing varies with hardware and artwork;
large structural edits still perform more work than focused drawing edits.

## Speech bubbles and stroke effects

Compound bubbles reuse their local deformed boundary when the complete bubble
moves or scales. Source-to-root transforms are composed directly, avoiding
rounding differences that previously rebuilt attributed outlines every frame.
Uniform outlines skip geometric style attribution. Collinear samples on a
straight Scream edge become a single boundary span, while the full samples
remain available to masks, opacity and dash spacing. Boundaries with varying
outline widths or visibility retain all their style anchors.

The following Windows measurements use a 500 × 220 compound ellipse, a triangle
tail, real Segoe UI text, and Scream height 47, width 69, roundness 0. They include
MainWindow change callbacks and a synchronous full-resolution canvas repaint.
Each result is the median of five changed frames; geometry resize changes the
original ellipse each frame rather than reusing a transformed image.

| Compound effect stack | Move | Uniform resize | Geometry resize |
| --- | ---: | ---: | ---: |
| Scream | 4.7 ms | 5.6 ms | 16.8 ms |
| Scream + Outline 3 + Blur 2 | 20.9 ms | 34.7 ms | 43.0 ms |

`tests/benchmark_bubble_interactions.py` loads the Windows Segoe UI font
explicitly and verifies that hiding the text changes the rendered image.
Collinear reduction preserves the geometric boundary within 1e-8
document units; Qt can change antialiasing at that boundary when many short
segments become one long segment. Regression tests constrain those changes to
boundary pixels and preserve filled regions, tails and custom outline styles.

Noncompound stroke effects deform the incoming image as well as its boundary.
Expensive material warps use a bounded preview during manipulation, followed
by the exact full-resolution result from a background worker. A separate
500 × 220 Scream benchmark with registered Segoe UI compares that editing
preview with the same synchronous kernels:

| Noncompound interaction | Synchronous kernels | Interactive preview |
| --- | ---: | ---: |
| Move | 170.4 ms | 27.0 ms |
| Resize | 199.3 ms | 30.9 ms |

These preview timings are not full-resolution render timings. Ordinary drawing
and compound boundary rendering remain at full resolution; only expensive
effect previews use reduced intermediate images. Settled output, source
captures and exports use exact results, and temporary images are never cached
as exact output. Regression tests compare settled images with synchronous
renders, including masks, mixed stroke/effect stacks and cache pressure.

Editing a contributor inside a compound also reuses flattened source curves
and ribbon geometry. Ribbon projections run in bounded batches, boundary
matching reuses scalar coordinates, and orientation-preserving transforms avoid
repeated path polygonization. Uniform outlines can stroke the completed
boundary directly; varying widths, disabled edges and reflected sources retain
their source attribution. Cache memory estimates remain unchanged.

Compound-child gestures retain viewport culling for unrelated artwork and
reuse unrelated mirror/pattern source captures. Affected compound subtrees
remain live, including sibling images fitted to the changing compound bounds.
Final mouse/stylus release coordinates are applied before the gesture's single
undo entry is committed.

Rounded open-tail joins use exact shared cubic endpoints and a tiny interior
overlap. This prevents a near-zero crack from becoming an outlined dash inside
the tail. Winding calculations subtract a local origin so tall chapter
coordinates cannot reverse tiny join sectors through floating-point cancellation.

Complex nonuniform compound outlines can paint their raw winding coverage once
and clip it with an antialiased fill mask at the destination's actual pixel
resolution. This removes the geometric clipper's normalization from normal
canvas painting. The temporary images are limited to 32 MiB, intersected with
the destination and active clip; large exports, vector/high-depth devices and
other composition modes retain geometric clipping. Simple and uniform outlines
keep their cached geometry. This is full-resolution rendering, with small
antialiasing differences at boundaries rather than a reduced-quality draft.
Routing uses the generated outline's complexity: a short spiky boundary with
a curved contributor can still produce thousands of outline elements.
Geometric fallbacks perform their Boolean operations near a local origin so
large chapter coordinates cannot discard the interior contour and blacken the fill.

`tests/benchmark_compound_children.py` measures child moves, uniform scaling
and node edits with MainWindow callbacks and synchronous full-resolution
repaints. Its synthetic open tail has three bent anchors, tapered core widths,
varying outline widths and large local coordinates; it also covers a closed
contributor and an Outline/Blur stack. Segoe UI is loaded explicitly, and each
case verifies that its text appears in the rendered scene. Projection,
boundary matching and orientation optimizations have byte-identical render
comparisons against their original calculations.

Final Windows results from that standalone benchmark (medians of five changed
frames, without a profiler):

| Compound contributor | Move | Uniform resize | Node edit |
| --- | ---: | ---: | ---: |
| Tapered open tail, varying outline | 28.0 ms | 41.8 ms | 57.9 ms |
| Same tail + parent Outline 3 / Blur 2 | 57.8 ms | 68.8 ms | 83.6 ms |
| Closed triangle | 12.5 ms | 11.7 ms | 12.6 ms |

A separate read-only saved-chapter reproduction retained the full surrounding
artwork, real text font, large parent transforms and the actual three-node
tail. With cProfile enabled on both measurements, median changed-frame time
fell from 277.4 to 69.8 ms for moving the contributor and from 328.4 to 146.9 ms
for moving a tail node. These measurements include the scene repaint and
profiling overhead; they are not a guaranteed frame rate for arbitrary scenes.

## Linear and circular gradients

Circular gradients sample distance from the first handle, using the second
handle to define the radius. Both shapes reuse a color-ramp lookup table across
handle drags and preset copies. The ramp cache holds at most 32 tables (128 KiB
of sample data at the default size); scalar and colored-image caches each retain
at most 32 entries. Color previews keep the existing maximum dimension of 256
pixels during editing and 768 when settled. Mask gradients sample every requested
pixel, processing rows in chunks of at most 262,144 pixels for normal export
widths, so coordinate temporaries do not grow with the full image height.

Representative Windows Qt offscreen measurements from the synthetic
`tests/benchmark_gradient_rendering.py` benchmark, with changing endpoints:

| Render grid | Linear median | Circular median |
| --- | ---: | ---: |
| Color, editing 256 × 192 | 2.17 ms | 1.34 ms |
| Color, settled 768 × 576 | 22.75 ms | 15.65 ms |
| Mask, full resolution 1024 × 768 | 15.76 ms | 14.91 ms |
| Mask, full resolution 2048 × 1536 | 55.14 ms | 62.59 ms |

Unchanged color renders took 0.019–0.041 ms from cache; all benchmark cases
shared one ramp table. These timings measure sampling and image construction,
not GUI gestures or saved-scene repaints, and vary with hardware and Qt/NumPy
versions. The benchmark uses only synthetic data and does not save settings.
Regression tests verify unchanged linear mask pixels across rotated and
projective mappings, circular hard stops and alpha, and cache invalidation.

## Reproduction

Run from the repository root:

```powershell
python tests/benchmark_editor_interactions.py
python tests/benchmark_editor_interactions.py --synchronous
python tests/benchmark_modifier_interactions.py --backend cpu
python tests/benchmark_modifier_interactions.py --backend auto
python tests/benchmark_fill_interactions.py
python tests/benchmark_autosave.py
python tests/benchmark_canvas_text_outlines.py
python tests/benchmark_scene_culling.py
python tests/benchmark_large_hierarchy.py
python tests/benchmark_undo.py
python tests/benchmark_bubble_interactions.py
python tests/benchmark_stroke_previews.py
python tests/benchmark_compound_children.py
python tests/benchmark_gradient_rendering.py
```

The editor and modifier benchmarks write timings and profiles to
`.artifacts/editor-performance`. The focused regressions are
`test_interactive_effects.py`, `test_interactive_patterns.py`,
`test_fill_interaction_performance.py`, `test_async_autosave.py`,
`test_color_picker_sampling.py`, and `test_outliner_reveal.py`. Existing text,
fill, stroke, pattern, masking, export, persistence, and session suites provide
additional coverage.

The scene-culling benchmark prints its timing comparison. The large-hierarchy
benchmark writes `.artifacts/large-hierarchy/timings.json`. Focused large-scene
regressions are `test_scene_culling.py`, `test_hierarchy_performance.py`,
`test_vector_edit_cache_performance.py`, and `test_outliner_reveal.py`.

## Validation results

The final compound-child change passed 629 focused tests covering attributed
outlines, rounded joins, gesture endpoints and undo/redo, viewport culling,
modifier cache dependencies, full-resolution raster clipping, masks, tiling,
and export fallbacks. The native Windows GPU editor also constructed and
remained alive for the five-second startup smoke test using Schannel.

The broad nonexternal test run passed more than 1500 cases. Its two failures
were instrumentation still watching the renderer's former entry point; those
spies were updated without weakening their assertions, and all 33 tests in
that file then passed. After the final changes, the affected renderer group
passed 361 tests with 6 platform-dependent skips, and the final text, outliner,
eyedropper, fill, autosave, and capture-integrity group passed 126 tests.
Live Blender-extension and external-editor integration were not rerun.

Cache-pressure tests reduce both ordinary scene caches to 1 KiB and repeatedly
evict them while two targets render. Both direct stacks and three-stage
pipelines converge to exact pixels without repeated completed work. CPU and
native GPU modifier benchmarks also compare settled images with synchronous
output and reported zero differing bytes.

## October 6 modifier edits and committed feedback

Modifier sliders previously cleared every document tile twice per value. The
inspector now emits one document-change notification and preserves unrelated
exact tiles when both the old and new painted bounds are known. Linked owners
are included. Compound ancestors, modified ancestors, remote mask contributors,
halftone references, and unbounded effects retain full invalidation. Contracting
effects invalidate their old halo as well as their new bounds.

A synthetic CPU run with 48 image objects, eight changed brightness values, and
a 1080 × 1800 viewport measured a median exact refresh of 81.4 ms before this
change and 32.8 ms afterward with cProfile enabled. Capture blocks fell from 48
to 8 across the run, and invalidated retained tiles fell from 45 to 1 per value.
The benchmark compares identical source/effect sampling and exact pixels. These
are synthetic renderer timings, not application input latency or a claim about
the saved chapter's frame rate. Evidence is in
`.artifacts/interaction-audit-20261006/baseline.json` and `regional.json`.

All modifier parameter drags, including mask endpoints, mark their lifetime as
transient. Release, inspector refresh/destruction, undo, and chapter replacement
clear the marker. Groove clicks on distortion sliders now snapshot before Qt
changes the value, so the initial jump and subsequent drag form one undo action.
Live captures use the existing interactive kernels and cannot enter exact disk
caches. The first live widget paint retires obsolete exact queued/running work
before preparing its preview, preserving completed prefixes and nonexact preview
jobs. Canceled futures cannot publish even if their kernels finish later.
Cheap effects can still be dominated by viewport composition; profiling
must distinguish live presentation from local exact refreshes.

After an ordinary committed edit, interactive widget presentation can retain a
fresh combined preview while exact workers finish. It is captured from the
committed model rather than relabeling the last drag image, is bounded to one
million pixels at no more than native document density, and lives outside the
exact projection cache. A dirty-only capture requires consecutive proven local
changes against a complete exact seed with coverage in every scene pass;
otherwise the visible scene is captured conservatively. Configuration, revision,
history, coverage, density, and pixel-contract guards reject stale feedback.
Visibility/configuration switches, including solo, mask-only and underlay, may
capture a fresh complete preview without an old seed. An initial viewport or a
new camera area without complete current exact coverage can likewise capture
current-model feedback while native exact workers finish. A covered current
exact frame skips this work. These fresh captures do not require an old exact
document identity; a full history restore configures its new identity first.
Only dirty patches require the old seed's strict identity, history, coverage,
and pixel-contract proof. These eligibility changes have correctness coverage,
but are not a measured cold-navigation latency acceptance result.
Live ink takes precedence. Complete exact base/top phases still publish together,
and detached captures and exports use exact rendering. Pending or failed exact
work retains current committed feedback and the existing status label.

Focused regressions are `test_modifier_edit_locality.py`,
`test_projection_interaction_preview.py`, and `test_distort_slider_history.py`.
They cover linked/remote dependencies, erased halos, fresh committed-model
normalization, actual blocked heavy workers, worker failure, compound/masked
ancestors and show-on-top, export exclusion, stale-preview rejection, and one
undo command for a real mouse groove press.

The inspector also keeps a detached fingerprint of its selected targets,
modifier records, mutable masks, linked/referenced owners and ancestry, plus
document/history and editing context. A normal selection's two notifications
and a full modifier undo's three notifications now construct one current widget
tree synchronously. Changed values, record replacement, geometry, masks and
referenced names still rebuild it. This snapshot copies metadata, without
sampling artwork or serializing the chapter. Navigator refreshes defer all live
projection gestures as well as existing mouse/tablet/text contacts, including
parameter changes driven by keyboard controls.

## October 6 bounded spatial preparation and cold sources

Live canvas/overflow captures now explicitly reuse the navigator's compact
spatial preparation for supported stacks. Each source capture has an edge limit
of 256 and a 32768-pixel budget before mask fields and deformation preparation;
the original embedded/raster pixels and exact effect grids are retained.
World-space axes and centers use the existing inverse-scale mapping. Local
filter distances and their parameter-mask endpoints scale together. Posterize
and value Posterize scale only their simplify radius, preserving palette values
and unitless intensity bindings. Live radial drafts run the radial effect on
the bounded input. Unsupported stacks retain their existing complete input;
no effect is removed. Special base-alpha, mask-contributor, halftone-color,
cage-source and tiling captures do not take this path.

Source/effect/opacity draft keys and translation aliases cannot be recorded as
exact disk entries. The 121 navigator/live regression cases passed across
RGBA8, float32 sRGB, float16 linear sRGB and float32 linear sRGB, including HDR
inputs, raster/image/cage/radial stacks, ancestor effects and opacity/parameter
masks. They verify bounded parameter-field construction, unchanged saved
records, no native effect job from a live draft, and byte-identical native
output before and after the draft. Additional cases cover unsupported masked
parents, Posterize mask endpoints and durable-cache exclusion.

The clean native Intel Iris Xe GPU scale reproduction on the saved University
Bridge blueprint returned all eight phases. Its two live paints still took
441 and 430 ms, and the commit paint took 257 ms; 35 settled native tiles
matched the synchronous reference byte for byte. Exact settlement took 37.1 s.
These are intermediate renderer timings from
`.artifacts/night-performance-20261006/blueprint-gpu-scale-clean-v3/`, not
acceptable final responsiveness or a matched attribution of this one change.
The earlier fixed-block source and sampling geometry remain in use.

The same investigation found original-image decoding taking 517 ms on the GUI
in a cold Add Posterize profile. Explicitly deferred exact and bounded live
captures now queue the identical `ImageStore` decoder against an immutable
encoded source. They yield Pending, then adopt only after source, generation,
store/chapter/history and pin-stamp checks. The native pixel/ICC result and
ordinary source/effect keys are unchanged; exports retain synchronous decoding.
The existing worker budget admits a conservative header-derived decode peak,
including native float pixels, display conversion and encoded bytes. Large
sources are admitted exclusively rather than falling back onto the GUI.
Effect-only live cancellation preserves independent original-source decoding;
full cancellation still retires it. Decode handoff entries are transient and
excluded from disk, and an oversized source can reuse the existing bounded
handoff instead of decoding once per scene tile. The 31 decoder/ImageStore
checks cover blocked/canceled/failed workers, admission, replacement/restore,
relabel, history, reentrant ready signals, RGBA64/native scene equality and ICC
preservation. The intermediate native v4 Add measurement below records the
first-return improvement and the remaining owning-thread completion hitch.

## October 6 inspector and Posterize statistics

A hidden native Windows MainWindow replay on the isolated `ui-project-after`
clone measured median selection refreshes of 64.4 ms for the blueprint and
30.4 ms for the room, a 97.9 ms median full-state blueprint undo, and 1.6 ms
for saved-mask thumbnails. Full canvas paints, autosave, durable-cache binding
and provider operations were suppressed in this helper. Public selection and
history handlers, inspector cards and saved-mask rendering remained active.
The three undo notifications built one six-card inspector; card construction
accounted for 56.9–76.6 ms. Two hierarchy refreshes accounted for 1.1–1.7 ms.
These results locate synchronous UI work; they do not measure physical display
or input latency. The original project was never opened, and clone manifest
hashes remained unchanged. Raw runs are under
`.artifacts/interaction-audit-20261006/mainwindow-refresh-repeated/`.

Saved Posterize inspectors constructed in 8.3–9.8 ms in that replay. A separate
stress case appended Posterize after the blueprint's complete saved effect
prefix in memory; this is not a saved chapter modifier. Its former synchronous
constructor exceeded a 15-second profiled watchdog in native distortion
sampling. Statistics construction and automatic palette initialization now use
the same exact stage renderer and detached effect jobs. The UI displays
"Preparing colors…", keeps manual controls usable, and enables statistics-based
range splits only after a complete current histogram is available. A pending
Add Posterize action installs no placeholder palette or model record, and
cancels if selection, source prefix, document or history changes.

The sample still has the original maximum 512-pixel output dimension and area
weighting. Upstream sources and effects keep their full native grids, masks,
color and precision semantics. Statistics captures disable viewport windows,
regional source evaluation and compact previews; they use an explicit semantic
capture variant so isolated compound/outward contents cannot collide with an
ordinary source image or its translation alias. The synchronous sampler remains
the oracle. Pending/failed capture unwinds every temporary target and render
flag, publishes no partial samples, and never falls back to GUI exact filtering.
Live gestures defer histogram work until release. Retried captures and completed
statistics validate document/store identity, history, selected targets, pixel
contract and the complete upstream source signature. Prefix metadata has its
own signature scope for each temporarily truncated target.

The native v3 stress checkpoint measured the appended inspector's first return
at 750 ms and full exact statistics completion 21.6 seconds later. The actual
Add action, including a hidden native count dialog, returned in 1235 ms and
installed its exact palette after 20.4 seconds; that palette matched a warm
synchronous native oracle. Maximum measured event-loop gaps during settlement
were 512 ms and 705 ms. This checkpoint removed the many-second inline filter
stall, but still exposed a material source/completion hitch. A separate profile
located 517 ms in cold `ImageStore.image` decoding and about 302 ms in first
SciPy distortion imports. Owning-thread instrumentation recorded about 180 ms
for the blueprint's painted opacity mask and 209 ms for palette installation
and its inspector refresh. A separate 564 ms outline-distance row belongs to
worker activity, and does not identify a GUI mask stall. Profile totals include background worker activity
and must not be added. First-return and event-loop measurements are separate
from end-to-end display latency. Evidence is in
`.artifacts/interaction-audit-20261006/mainwindow-posterize-v3-constructor/`,
`mainwindow-posterize-v3-add/`, and `mainwindow-posterize-v3-add-profile/`.

A clean decoder-v4 run of the same actual Add action returned in 339.5 ms,
with its exact palette installed 17.45 seconds later. The palette again matched
the synchronous native oracle, and runtime source hashes were unchanged from
import through completion. The maximum event-loop gap was still 511.8 ms;
this is an intermediate improvement rather than responsiveness acceptance.
Its final paint-only opacity field used the unchanged 5399 × 5399 native grid,
58 sparse paint tiles, and no gradient or contributors, taking 152.9 ms on the
owning thread. The source was the complete saved blueprint prefix, whose Pinch
and HSL modifiers are muted, rather than the separate all-six-active stress
case. Full canvas painting was suppressed as in v3. The original project was
never opened, clone hashes remained unchanged, and this hidden-widget replay
does not measure physical display latency. Evidence is in
`.artifacts/interaction-audit-20261006/mainwindow-posterize-v4-add/`.

Owning-thread instrumentation in a separate v5 completion diagnostic identified
257.7 ms in the same native painted opacity field and 182.1 ms in opacity
application. Its 1002 ms event gap includes profiling overhead and is not a
clean timing comparison. Deferred exact requests can now yield a paint-only
opacity output as an ordinary detached effect job: selected immutable tile pins
and a copy-on-write source image feed the same shared paint kernel and opacity
arithmetic. Native dimensions, projective/fractional mapping, signed paint,
endpoints and pixel precision are unchanged. Unsupported contributor/gradient
fields and small fields keep the synchronous path. The normal snapshot API still
waits for recovery prefetch; this visible-dependency snapshot does not wait for
unrelated chapter pins or decode cold tiles on the document thread.

Field, paint, source, output and conversion memory are included in admission;
oversized outputs run exclusively. A transient result handoff stays outside
disk caching, and current source/context/contract checks precede admission to
the existing exact semantic mask cache. Pending/failure cannot trigger an inline
fallback. Native byte equality, float16/float32, signed paint, projective mapping,
source/history/document mutation, reentrant changes, exact cancellation,
oversized admission, immutable cold-file replacement and actual Posterize
sample equality are covered by `test_deferred_opacity.py`. Before/after native
completion must be assessed separately from end-to-end display latency.

The unprofiled final-v6 Add replay completed with the exact native-oracle palette
and unchanged hashes for all 199 runtime source files and clone manifests. Its
maximum event-loop gap was 283.7 ms, compared with 511.8 ms in the decoder-v4
checkpoint. No owning-thread field or opacity kernel ran while awaiting the
palette. The largest sampled-object refresh still took 195.7 ms, and the final
inspector refresh took 216.9 ms. Cold first return was 678.0 ms; exact statistics
settled after 23.43 seconds, compared with 17.45 seconds in the earlier single
v4 run. These are individual checkpoints with different runtime revisions, not
a repeated isolated attribution or acceptable final interaction latency. The
same hidden-window/no-full-canvas limitations apply. Evidence is in
`.artifacts/interaction-audit-20261006/mainwindow-posterize-final-v6-add/`.

A subsequent clean frozen v7 actual Add replay returned in 603.6 ms and
installed the exact native-oracle palette after 23.40 seconds. Its maximum GUI
event gap was 248.4 ms. Exact append reuse constructed one new card and retained
the unchanged existing cards: the inspector refresh was 67.3 ms, compared with
216.9 ms in the v6 checkpoint. The final sampler still took 206.2 ms, including
160.6 ms in the selected source-object capture. Runtime/clone hashes remained
unchanged and the original project was never opened. This hidden-widget replay
suppressed full canvas paints and measured no physical presentation. These are
single checkpoint results; 603.6/248.4 ms remain above the interaction goal and
require separate owning-thread leaf attribution. Evidence is in
`.artifacts/interaction-audit-20261006/mainwindow-posterize-final-v7-add/`.

A separate owning-thread attribution replay found 451.6 ms in initial request
context construction, including 448.6 ms in `baking.effect_bounds`; subsequent
context checks were about 3 ms. That first bounds call can import the numerical
distortion module, but this trace did not isolate import cost. Final statistics
assembly included native 5383-square image fill (46.8 ms), 5433-square copy
(40.5 ms), fills (34.1/26.8 ms), and image composition (20.2/17.7 ms). The largest
request retry was 200.8 ms; no owning-thread opacity-field or opacity-application
kernel ran. Its exact palette matched the synchronous native oracle, and source
and clone manifests remained unchanged. A separate native UI startup overlapped
this diagnostic, so these caller measurements are not a clean speed comparison
or an acceptance result. Evidence is in
`.artifacts/interaction-audit-20261006/mainwindow-posterize-v7-attribution/`.

A working-space/precision regression also showed that an identical source and
mask could reuse the preceding uint8 placed-mask output after a float32 switch.
Both the placed-mask key and its translation alias now include the pixel
contract; memory and disk use the same amended semantic keys. The final focused
opacity/viewport/Posterize batch passed 96 cases, and the storage/history/mask/
existing Posterize batch passed 163 cases, with no live external integration.

Focused checks are `test_posterize_deferred_statistics.py` and the existing
Posterize, gradient sampling and gradient control suites. They exercise real
blocked/failing native workers, masked nested layers, exact sampled pixels and
histograms, actual count-dialog events, one add/undo transaction, source/history/
selection/document cancellation, live gesture deferral, saved-source pending
retries, and restored render state on failure. No live Blender or external
editor is involved.

The subsequent all-six-active blueprint/new painted HSL mask native UI capture
still exposed severe exact traversal overhead. Its checkpoint recorded about
446,672 cache calls and 58.5 seconds of inclusive cache work, with 396,270 misses;
10,157 GUI leaf samples ended in descriptor hashing. These counters identify
repeated semantic-key preparation, not a worker cancellation storm. Immutable
descriptor identity digests now use a typed memo bounded by 8192 entries and a
16 MiB conservative token budget, with a direct fast miss for an empty index.
Mutable public keys still hash every call, and reads still validate the current
entry and seal. No hit/miss result, dependency decision or pixel output is memoized
by this optimization. Disk identities, renderer versions and payloads are unchanged.

A separate hash-only replay of representative saved Blueprint/Slam settings
(about 8.9 KiB canonical descriptors) measured median original hash costs of
36.5/35.0 microseconds versus memo hits of 10.6/9.2 microseconds over five 3000-call
passes. This is a 3.4/3.8-fold reduction for those repeated keys, not a native
canvas latency or convergence measurement. The 50 focused descriptor/disk checks
passed, including signed zero/numeric type isolation, mutable metadata, versions,
bounded retention, concurrent access, publication after an empty miss, corrupted
seals, exact float pixels, and reopen reuse. Evidence is in
`.artifacts/night-performance-20261006/cache-identity-timings.json` and
`cache-identity-regressions.xml`. Native full-paint attribution remains required.

An instrumented all-six blueprint painted-mask release later showed that the
empty-index miss occurred too late: 50 calls to the controller's regional scene
key construction consumed 696.6 ms of a 1344.7 ms first paint even though the
validated backing had zero readable entries. `PersistentRenderCache.can_lookup`
now reflects only its current published entries/read handoff and closed state;
controller lookup/status and intermediate read-through check that hint before
constructing or scanning dependency keys. Nonrecording projection retain also
skips key construction, while warm intermediate metadata and source fingerprint
hooks remain active. Exact recording still uses the normal keys, validation and
pipeline. A pending first source hash postpones a disk write until the next
ordinary collect offers the valid memory tile again. Publication immediately
reenables normal lookup and seal checks; no availability result is memoized.

The nine new fast-miss/publication/recording/source-mutation checks and existing
disk, descriptor and interaction-preview suites passed 90 cases in 21.43 seconds
(`empty-disk-caller-final-regressions.xml`). The recorded 696.6 ms is diagnostic
attribution before this caller fix, not a measured native speedup. The same
all-six replay still failed both painted and unpainted 120-second exact waits;
its saved four-active state restored and matched all 50 native tile bytes.
Convergence and native full-paint latency must be checked separately after the
retention-progress changes. Evidence is under
`.artifacts/night-performance-20261006/blueprint-gpu-six-mask-profile-v6-disk/`.

A shorter unchanged painted-release trace isolated retention pressure. One
complete native predecessor covered 5383 × 5383 pixels across 506 tile addresses,
almost the entire 512-record retained limit before worker/pipeline aliases.
The trace recorded 7373 cross-scope pixel-alias puts and 7349 evictions caused
only by record pressure, while peak retained bytes were 237.7 MB, below the
existing 256 MiB limit. It did not finish the 30-second exact wait. Earlier
120-second traces also repeated completed semantic worker keys. Evidence is in
`.artifacts/night-performance-20261006/blueprint-gpu-six-mask-retention-v6/retention-attribution.json`.

Deferred exact tile graphs now retain native frame progress privately between
captures. A lazily allocated buffer receives each completed tile once; later
retries skip covered addresses. Partials never enter semantic caches or disk,
and never become worker inputs or presented complete frames. Assembly identity
contains the full node/prefix identity, aligned requested frame, format and tile
grid. Owning chapter/stores, history, revision and both current/document pixel
contracts guard the pool; cancel or context replacement clears it. The existing
retained byte and record limits also cover these buffers and 192 bytes reserved
per tile address. Finished COW images deduplicate ordinary aliases; consumed raw
worker aliases retire only after checkpoint admission. Complete predecessors
can be evicted once their caller owns the COW image, whose worker snapshot is
charged by the existing inflight admission. Ancestors are not pinned indefinitely.

If a proved spatial distortion's full input/output pair cannot coexist in that
pool, the exact deferred request uses its existing full native stage instead of
cycling partial output and input buffers. Its lookup and evaluation temporarily
disable nested regional assembly and restore state even on Pending/Failed.
Oversized workers keep the existing exclusive admission. Twirl, Deform, Mesh
Warp, Lens Distortion and Pinch/Punch were compared with their unchanged regional
pixels across fractional affine/projective placement, varying intensity masks,
transparency, HDR and byte/float16/float32 source contracts. Radial's output-local
96px blocks select integration counts, so its demand partition is preserved.
Cage's GPU FBO extent can affect vertex rounding, so it is not promoted without
native GPU evidence. The existing 64-megapixel whole-output limit also prevents
promotion. Default pure/synchronous/nonexact/cache-only graph behavior is unchanged.

The latest 35 pressure/native precision checks and 57 assembly/retention guards
passed. A dependency wider than a five-record pool completes all three 80-plus
tile stages without repeating workers; the unchanged fresh-graph path repeats
evicted first-stage work and cannot reach stage two. Separate checks cover
source/history/document/contract/cancel rejection, private-buffer accounting,
completed COW handoff under byte pressure, unexpected native formats/DPR/color
metadata, fractional frames and transparent halos. The earlier affected graph,
prefix and parallel queue batch passed 103 cases. These are correctness and
controlled progress results; native all-six convergence and UI latency remain
separate acceptance measurements. XML artifacts are
`tile-pressure-final-pytest.xml`, `tile-assembly-guards-pytest.xml` and
`tile-assembly-affected-pytest.xml` under
`.artifacts/interaction-audit-20261006/`.

The instrumented native v7 all-six replay showed monotonic progress rather than
the previous repeated-work cycle: all 683 painted-state worker starts had unique
scope/key pairs, no private assembly was evicted, and coverage grew to 1036 of
1103 reserved addresses. Nevertheless, both painted and unpainted exact waits
exceeded 120 seconds. The saved four-active state converged in 33.4 seconds and
matched all 50 published native tile bytes. Ordinary record aliases still churned;
the private checkpoints preserved dependency progress within the existing
256 MiB/512-record retained limits. This establishes progress, not acceptable
latency or successful all-six convergence.

Worker attribution then found 34 preparations of the same 5383-square bilinear
source, all cache misses, with 9365 ms aggregate conversion time. Its float32 RGBA
preparation needs 463,627,024 bytes, exceeding the unchanged 256 MiB preparation
cache even though two ARGB input/output surfaces fit the retained pool. A proved
CPU distortion now also uses its existing full native kernel when its input or
output preparation exceeds that bound and a later complete-source effect needs
its entire output. An allocation-free compile of the actual saved Blueprint
records confirms Lens expands 1105 by 1466 input pixels to 5383-square output:
its outgoing preparation exceeds the bound, and the following Pinch needs that
whole output. Lens is promoted; the later Pinch has only partial viewport demand
and remains regional. Its oversized input preparation cost is not eliminated by
this change. Other final stages with partial demand also remain regional.
The preparation budget is a shared constant, not an increased limit. Whole-stage
admission, native grids, parameter fields, color contracts, exclusions and durable
cache rules are unchanged. Evidence is in
`.artifacts/night-performance-20261006/blueprint-gpu-six-mask-profile-v7-disk/`.

The expanded pressure matrix passed 60 native-byte comparisons across both
retained-pair and preparation-overflow triggers, five CPU distortion types,
affine/projective placement, varying masks, and byte/float16/float32 contracts.
Together with actual 29-megapixel demand-decision checks and assembly/retention/
pending guards, the focused batch passed 125 cases with no failures or errors
(`.artifacts/interaction-audit-20261006/tile-preparation-pressure-pytest.xml`).
Six further expanded-Lens native crop comparisons cover the actual outgoing
pressure pattern, masks, affine/projective placement and all three source
contracts. The affected pressure and creation suites passed 240 cases in
`outgoing-pressure-creation-affected-pytest.xml`; allocation-free actual node
decisions are recorded in `actual-blueprint-pressure-nodes.json`. Native
convergence and UI latency require a subsequent frozen-runtime replay.

The instrumented v8 replay still exceeded both 120-second all-six exact waits.
The saved four-active state converged in 23.35 seconds with all 50 published
native tiles equal. The same promoted Lens worker scope/key ran three times,
with unchanged incoming source identity, taking 24.0, 21.4 and 22.0 seconds.
Review found that a completed stage preflight bypassed the ordinary graph
`put` handoff, leaving only its raw worker/pipeline aliases. A successful
non-null preflight now uses the same exact graph key, existing retained budget
and raw-alias consumption as an evaluated tile. Reentrant context changes are
rejected before adoption; Pending, Failed and null images are not admitted;
cache-only requests remain read-only. A bounded older-frame/unfinished-child
pressure regression reproduces two predecessor worker computes without the
handoff and one with it, byte-equal in byte/float16/float32 formats. This is a
controlled correctness/progress result, not a native latency acceptance claim.
Artifacts include `preflight-adoption-pressure-baseline-pytest.xml` and
`preflight-adoption-pressure-pytest.xml` under the interaction audit directory,
and `blueprint-gpu-six-mask-profile-v8-disk` under the native performance directory.

Native creation QA also found that a selected image's transform cage consumed
Rectangle creation presses inside the image. Creation tools now hide that cage
and receive their creation gestures; Select/Transform and explicitly enabled
Pencil handles retain their behavior. Forty-eight actual Qt mouse-event cases
cover selected Image/Raster/Vector interiors and handles, Rectangle/Circle/free
and drawn shapes, raster creation, ordinary shape/compound placement, selection,
and one-command undo/redo. Heavy compound creation was observed to publish late
in the old UI capture, but it did create the requested child; it is not recorded
as a missing-creation bug.

The subsequent instrumented v9 capture verified that preflight adoption worked:
the 115,906,756-byte Lens frame entered the protected graph scope, and its raw
worker alias was consumed. It later lost protection while 57 aliases shared
its pixels, then the graph alias and final pipeline alias were evicted by byte
pressure. Both all-six 120-second waits still failed; the saved state settled
in 32.61 seconds. The event did not record the incoming protected-image size or
protected-record count, so it does not distinguish the shared partition's byte
and record admission triggers. It does establish that protecting every regional
child/output alias allowed that demanded whole predecessor to lose its priority.

Graph retention now protects only whole-frame node owners. Regional tiles and
one-use viewport outputs use the existing normal LRU; their private assembly
coverage preserves progress. A production-adapter regression with a predecessor
at 90% of the unchanged shared-half budget, many child tile adoptions and a
separate viewport admission reproduces two prefix computes before this policy
and one afterward. Finite regional predecessors also hand a complete COW frame
to a waiting shared child without repeating workers. Both tests preserve native
bytes in byte/float16/float32 formats and retain the same total/shared byte and
record limits. The 84 guard/prefix cases and 159 existing native tile, pressure,
parallel and dependency cases passed; artifacts are
`tile-graph-protection-baseline-pytest.xml`, `tile-graph-protection-guards-pytest.xml`
and `tile-graph-protection-native-affected-pytest.xml` in the interaction audit directory.

The v9 contact trace also found a different eviction: a completed original-image
decode of 88.5 MB replaced 111 derived-effect LRU entries totaling 31.1 MB with
one oversized source alias, discarding the current compact Mesh prefix. Native
source decodes now keep only their existing retained source handoff and guarded
ImageStore adoption, rather than writing this redundant derived-effect alias.
The exemption requires both the source-decode scope and source-decode-preview
key; ordinary effect completion behavior is unchanged. Actual completion and
adoption tests cover a tiny retained budget with protected old entries, an
oversized nonresident ImageStore handoff, resident adoption, and identical live
pixels with no second Mesh call or owner-thread re-decode. Stale source/history/
pin guards, failures and precision checks remain covered; 85 affected checks
passed in `source-decode-prefix-pressure-pytest.xml`. The subsequent clean v10
native run converged for the painted six-effect state in 84.25 seconds, restored
six-effect state in 72.96 seconds and saved four-effect state in 24.78 seconds.
All 50 published exact tiles matched in each state. Three independent fresh
cold native patches also matched raw bytes and metadata with empty derived
caches. Live frames were 33–41 ms and release was 193 ms, so convergence is
established while cold refinement and interaction latency still exceed the
responsive-editing goal.

The same v7 capture recorded 217,732 stage-plan calls and 58.0 seconds of
cumulative planning time across its operations. A capture-local whole-plan memo
experiment preserved native output and passed 28 focused plan guards plus the
existing geometry/region/smudge/signature checks (113 total). Nevertheless, a
quiet metadata-only comparison rejected it: with actual saved Blueprint Lens
settings, captured native bounds and a representative 6.9 KiB semantic prefix,
160 identical requests improved only from 16.01 to 14.17 ms. Unique requests
worsened from 15.75 to 42.94 ms, and three calls per unique request worsened from
45.98 to 66.50 ms. Deep current-state proof and miss bookkeeping outweighed reuse.
The experiment was removed; production stage planning retains its prior path,
keys and sampling. These microresults measure metadata, not canvas latency.
The rejected source/tests and reproducible input are archived under
`.artifacts/night-performance-20261006/stage-plan-rejected-v3.py`,
`test-capture-stage-plan-rejected.py` and `stage-plan-microcomparison.json`.

The owning-thread Add attribution also recorded a 46.8 ms transparent fill of
a 5383-square native image and a 40.5 ms copy into a 5433-square image. A
full-strength CPU warp never reads that padded blend image: its original path
returns the warped result directly when the uniform intensity is one. Deferred
exact Twirl, Deform, Mesh Warp, Lens Distortion and Pinch/Punch now omit that
unused allocation and paint, retaining the same native source, output grid,
keys, masks policy and kernel. The old working-memory admission reservation is
preserved using the current QImage format and aligned row stride rather than
assuming RGBA8. All other modes retain their original padded-base preparation.
The focused `test_deferred_distort_base.py` checks include 60 unchanged native
byte/float16/float32 comparisons across full/cropped output and fractional
affine/projective placement, HDR source values, all excluded modes, reservation
and allocation guards, detached source/rig snapshots and cancellation. The
initial 75-case pass is recorded in
`.artifacts/night-performance-20261006/deferred-distort-base-regressions.xml`.
This removes measured unused work; native Add and full-scene latency still need
their separate after-change acceptance measurements.

The v10 oversized Pinch input still exceeded the 256 MiB native preparation
cache, making each distinct regional worker repeat the same full-source float
conversion. Explicitly deferred exact partial-output work in the five proved
CPU distortion families now uses fixed 512px native document-grid output
batches, clipped to the semantic frame. The canonical graph still stores 256px
tiles and uses its existing identities; completed native batches use the same
stage pipeline and keys with their exact output target, then copied crops serve
sibling requests without retiring the full worker handoff. Masks and source
sampling grids are unchanged, and cache-only requests do not create work.
The old 58 Pinch rectangles map to 18 such batches, covering about 1.24 times
the requested output area. That is a demand-count estimate from the captured
rectangles, not a measured speedup or a new sampling grid.

The final batch check passed 229 cases, including 82 new tests with 60 native
byte/float16/float32 comparisons across all five CPU families, masked and
uniform-100% paths, fractional affine/projective placement, HDR and transparent
source pixels. Additional checks cover sibling reuse after ordinary cache
eviction, clipped edges, full handoff survival, stale source/mask/history/contract
rejection and Pending/Failed flag restoration. Its artifact is
`.artifacts/interaction-audit-20261006/native-output-batch-affected-pytest.xml`.
That process started at 06:34:11 UTC; the optional-base caller's final valid-mapping
guard landed at 06:34:33 UTC, so this targeted artifact covers its preceding
source. The full nonexternal suite started after the complete runtime froze
and is the authoritative combined-source check once its result is recorded.
Retained and preparation budgets, synchronous/export rendering, live previews,
Radial/Cage partitioning and the 64-megapixel guard remain unchanged. A fresh
native v11 run must establish the latency effect separately.

After the interrupted broad run, isolated replay recovered the actual UI
failure assertions in `isolated-ui-failures-20261007/pytest.xml` and retained
frame/revision/status observations in its `frames.json`. Six outlined or
zero-intensity MainWindow text cases and two text-outline cases compared an
initial Pending/loading presentation with finished artwork after entering text
editing. Their tests now wait, with a two-second bound, only for the initial
complete current-revision view and its viewport coverage. Immediate live
typing, caret/selection, clean export and commit comparisons remain unchanged.
The radial cancellation/no-op test also retains those assertions and now
expects the general asynchronous committed-widget policy after its drag ends.

Two strict immediate-pixel failures exposed a presentation inconsistency:
raster selection movement changed a grid-covered background channel by one,
and canceling a solid-color picker changed 95 grid bytes by one. Ordinary
interactive raster presentation used the transparent grid overlay intended to
work around the non-multisampled GL engine, while detached/resting and live
presentation painted the same grid directly. The workaround now requires an
actual QOpenGLWidget paint device; raster and detached QImage captures use
direct coverage consistently, including captures supplied by a GPU canvas.
Live GL presentation uses the same overlay path as its resting GL frame.
This changes only the screen grid, without modifying artwork sampling,
precision, semantic caches or durable entries. The original strict raster
live/commit/undo and picker-cancel assertions passed unchanged.

`ui-fixes-affected-20261007.xml` records 302 passed and nine desktop-GL cases
skipped in 48.596 seconds across the affected UI and projection files.
`projection-grid-raster-20261007.xml` adds four exact QImage comparisons across
raster/GPU canvas owners and zero/17-degree rotation. These synthetic
offscreen checks are correctness evidence, not latency measurements or native
GL acceptance. The initial 15-case replay also passed in
`isolated-ui-failures-corrected-20261007/pytest.xml`. Artifacts are under
`.artifacts/interaction-audit-20261006/`.

The broad run's 68-percent stack showed the main thread reapplying the
application stylesheet during another MainWindow construction. New windows
now skip that global operation when the already-installed stylesheet is
identical. This avoids redundant application-wide style work; its effect on
real-project startup and Add latency still requires a separate quiet run.

The v11 causal mask-Undo capture also evaluated five compact distortion stages
on the owning thread (168.012 ms inclusive), including one Mesh kernel whose
three triangle-map rows totaled 101.216 ms. A scaled-pressure actual Scene test
then reproduced eviction of the unchanged Twirl prefix: an oversized exact
worker completion flushed the ordinary 64 MiB effect LRU, and undoing a real
late HSL mask stroke recomputed the warp. Its pre-fix failure is recorded in
`live-prefix-pressure-baseline.xml`; these captures establish the cause rather
than an after-change latency result.

Exact worker results already own a retained COW handoff before their ordinary
RAM alias is inserted. The effect LRU now skips only an oversized duplicate
with the same semantic key, Qt storage identity, byte size, dimensions and
native format in that existing pool. Graph adoption admits its handoff first,
then writes the ordinary alias, and consumes the raw result only if admission
succeeds. Rejection keeps the ordinary exclusive alias and the raw handoff.
There is no extra preview pool or increased budget. The ordinary exact
disk write still runs before the RAM decision; live drafts remain excluded.
Full history restoration still clears the caches.

Twirl and Mesh each retain their unchanged compact prefix through exact
pressure and actual late-mask paint Undo, perform zero additional warp calls,
and reproduce a forced fresh draft exactly. Strict source, geometry, parent,
early-parameter, upstream-mask and endpoint changes still recompute. Three
native formats cover admitted/rejected graph handoffs, exact disk bytes and
draft exclusion; independent storage, mismatched keys/formats, cancellation
and history changes retain their original guards. The affected check completed
with 316 passes in `live-prefix-alias-affected.xml`; the two precision-contract
cases passed separately in `live-prefix-alias-contract-final.xml` after the
source working-copy fix. Artifacts are under
`.artifacts/interaction-audit-20261006/`. These checks may overlap other
correctness work and establish pixels, handoffs and bounded accounting;
quiet native-editor measurements must establish the latency effect.

The v12 native causal lifecycle capture distinguished a second pressure source.
All 21 initial compact prefix tokens, with distinct Qt storage totaling
6,370,460 bytes (6.075 MiB), survived contact but were evicted during exact
settlement by ordinary sub-budget native stage/tile writes of 262,144 or 67,584
bytes. Oversized duplicate admission did not cause these evictions. Its complete
`live-prefix-telemetry.json` is under
`.artifacts/night-performance-20261006/blueprint-gpu-six-mask-first-paint-v12-disk/`.
Two real Scene replays, Twirl and Mesh plus a late HSL mask stroke, reproduced
lost-prefix assertions under a stream of native 256px writes before the priority
change (`live-prefix-regional-pressure-baseline-v12.xml`).

The same ordinary effect LRU now gives a bounded recent subset of
`live-effect-draft-stage` entries eviction priority over ordinary native tiles.
The measured ceiling is 8 MiB, reduced to a quarter of the ordinary budget for
smaller caches, with at most 64 protected records. It remains one OrderedDict
with unchanged 64 MiB total accounting; no image pool or persistent index was
added. Excess/older prefixes remain ordinary eviction candidates. Incoming
native results always remain consumable: a large result can displace priority,
and an unretained oversized result retains its existing exclusive admission.
Ordinary gets determine recency. Semantic keys, source invalidation, native
sampling, precision, cancellation/history clear and disk admission are unchanged.

The strict current-prefix checks now cover small-region pressure before
source/geometry/parent/early-mask/parameter and precision changes. The new
quota/recency checks verify native-byte/float16/float32 storage, the actual
8 MiB ceiling, 64-record limit, scaled quarter allowance, exact progress and
ordinary byte accounting. Real Twirl/Mesh mask Undo under 32 native region
writes performs no extra unchanged warp calls and matches a forced fresh draft.
Durable recording still excludes protected drafts while retaining exact native
results, and cancelled/history-replaced workers still cannot publish stale pixels.
`live-prefix-priority-affected-v13.xml` completed 327 passes with zero
failures/errors/skips in 57.180 seconds across the affected prefix, graph,
source, worker-retention, draft and disk-cache checks. The 42 focused cases are
also recorded in `live-prefix-priority-guards-v13.xml`. These processes establish
correctness and bounded retention; they are not quiet native latency acceptance.

The acquired-source compact256 preview uses the completed ordinary source
decoder and the existing decoded LRU. The temporary source is fixed at no more
than 256 pixels per edge and 32768 pixels. Widget presentation owns the opt-in;
detached interactive calls, native output, Float, Navigator and contributor
paths preserve their source grids. Trusted decode/adoption metadata is bounded
to 64 records and contains no pixels. Missing or stale metadata uses ordinary
source acquisition, and cache keys distinguish that fallback before source or
stage reuse. Original cold decode time is reported separately, not hidden as
preview preparation.

The v15 R2 experiment passed twelve actual changed-widget ROI comparisons with
fresh independently decoded sources, three current first-release revisions,
three changed native commits, restoration and independent cold native patches.
The separate clean acquired-source timing pair had live-paint medians
250.346 ms ordinary versus 125.6165 ms compact, with a 251.2 ms compact maximum.
This halves the median but does not meet the 31–45 ms response goal. Production
placement passed 92 guarded proposal cases in 6.974 seconds, including actual
warm source/stage reuse after visible source mutation, ordinary fallback,
detached ownership, acquisition, COW, precision and painter/state restoration.
Earlier failing fixtures are retained in the investigation artifacts. Fresh
integrated native acceptance and the full final-build suite remain separate
gates; the experimental timing is not a production-build timing claim.

The integrated V18 production placement subsequently passes 163 affected
checks with zero failures/errors/skips and its strict native R4 capture.
Twelve changed widget ROIs equal fresh independent original decodes, three
first-release paints contain the current revision before exact completion,
three changed native commits match the accepted historical native hashes, and
restoration plus three cold native patches match. Actual parent/child import
origins and all 258 frozen/current resources are checked. The native proof
contains no sampler, source-cache or scene-routing substitution. Instrumented
drag paints remain 94–155 ms and releases 157–190 ms; these prove current pixels,
not a clean production speed result. Fullsuite and broad final-build timing
are still pending. See [the investigation](performance-investigation-2026-10-06.md)
for capture identities, failed helper attempts, pins and timing limits.

The subsequent full V18 suite found a real outlined-text drag failure, despite
passing the focused image checks. Its original failure and complete provenance
are preserved. The live compact mirror route captured text on its old frame,
clipping glyphs already positioned on the new quad. A bounds-only control fixed
the preview but failed commit/preview equality and was rejected. V19 keeps text
on its established generic live capture; explicit spatial modifiers and
Navigator retain their previous routing. Native sampling and the renderer epoch
are unchanged. The original 14 transform cases and 167 affected text/projection
checks pass without changing their assertions. The frozen V19 full suite and
fresh native heavy-object, GPU, navigation and real Save checks must complete
before this build has final acceptance.

V19's complete offscreen suite subsequently passes 6509 cases, with 95 explicit
skips and no failures/errors. Clean native six-effect mask drags take
27.516–31.042 ms, but binding removal and whole-stack Undo repaint in
682.029–692.306 ms. Whole-chapter Navigator history recovery takes 12.090 seconds
and passes its unchanged 15-second limit. These results cover their specific
operations; the heavy transform response goal remains unmet.

Native hardware checks also identify a stale GPU blur compatibility guard:
installed Pillow 12.3.0 was rejected before any GPU work. An isolated verified
allowlist passes 1380 raw float32-bit comparisons against the CPU reference,
including normal/legacy algorithms and odd/unit/extreme-alpha inputs. Adding
only that verified release to the production allowlist passes 32 original
native affected cases. Unknown Pillow versions still fall back. Source/effect
grids, precision, cache keys, memory budgets and renderer epoch are unchanged.
V20 freezes this integrated build; its fresh full suite, broad native editing
and actual Save acceptance are separate pending checks.

V20's fresh complete suite now passes 6509 cases with 95 explicit skips and no
failures/errors. Current native acceptance remains open: the Windows desktop
lost its Intel rendering context, and a paired same-software-backend control
also exposed native Text pixels changing with display scaling. Tests of all 47
saved texts identify Qt's Windows font-engine selection as the cause and show
that an explicit font hinting policy can make native pixels independent of
display DPR. That candidate has not been integrated. Historical native failures
remain retained; current software timings are not comparable Intel speed results.
