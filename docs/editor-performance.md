# Editing responsiveness

The September 2026 performance pass moves expensive display effects, large
fills, and recovery-file writes off the GUI thread. Small effects still render
directly. Larger effects display a bounded preview immediately and replace it
with the exact image when the worker finishes. Export and baking use exact
rendering.

## Measurements

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
- The effect queue has a normal memory budget and one worker. Explicitly
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
