# Drawing performance monitor and eyedropper update

13 September 2026 · Non-Blender editor

The **Performance…** button beside **File**, at the top left, opens a dedicated
monitor. **Opening it does not start recording.** Enable monitoring immediately
before reproducing a slow drawing gesture, selection, transformation, or tool
change in solo mode. Click **Mark slowdown** to leave a breadcrumb, then **Stop**.
Closing the monitor or closing the editor also stops recording.
Restart an already-running editor after updating the source to load these changes.

## Automatic logs for optimization

Every enabled run creates a separate timestamped folder containing:

- `summary.md`: a concise starting point for an agent, including reproduction
  context, slow phases, tool/selection/solo transitions, user marks, freeze stacks,
  resource readings, and limitations.
- `capture.json`: all retained structured data, including phase counts, total/max
  durations, recent p50/p95 durations, active calls, events, sampled file/function/
  line locations, and dropped-sample counters.

**Open log folder** opens the current run. The normal Windows location is:

```text
%LOCALAPPDATA%\VerticalComicEditor\Vertical Comic Editor\performance-logs
```

Logs are beside application settings, outside the artwork project. Each run
checkpoints approximately every two seconds on a worker, with atomic file
replacement and a final checkpoint on stop. A crash can leave the most recent
checkpoint marked `recording`; it is still useful. A Python/native operation
holding the GIL can delay both sampling and saving until it yields. Disk errors
are shown in the panel. Existing runs are preserved; **Clear capture** finalizes
an active run before starting a new one. **Export JSON…** saves an optional extra
copy and stops capture first. Automatic files stay on disk until removed.

When asking an agent to optimize a slowdown, provide both files from the affected
run and describe the action you performed. Entity IDs connect the recorded tool,
selection, ancestry, modifier types, and solo targets to the source document.
Logs include structural metadata and source paths, but no artwork pixels,
document text, frame locals, or serialized chapters.

## What is measured

| Area | Evidence captured |
|---|---|
| Drawing and interaction | Stroke begin/move/commit, mouse/tablet event counts, handler timings, selection and transform phases |
| Tool/solo freezes | Requested and previous tools; selection and solo context before entering calls; calls still active at a checkpoint |
| GUI responsiveness | 100 ms heartbeat, gaps of at least 250 ms, and Python stack samples approximately every 20 ms |
| Rendering | Scene-cache work, dirty areas, layer/object effects, masks, signatures, compound geometry, selection overlays, navigator rendering, eyedropper |
| Cache and background pressure | Effect/source cache hits and misses, retained image bytes, raster tile counts, pending/running/completed/discarded effect jobs, autosave state |
| Resources/backend | Process CPU, GUI-thread CPU, Windows resident/peak/private memory, viewport/zoom/rotation/DPR, Qt/PySide versions, renderer, pattern-GPU availability and uploads |

Process CPU uses **100% for one logical CPU**; a separate machine-normalized
percentage divides by logical processor count. Missing readings are `null` with
reasons. Resource/context reads occur on the GUI thread about once per second;
the log worker uses detached/cached data. During a freeze, resource values may
therefore be older than the sampled stack or active call.

Timings are **inclusive wall times**. Parent and child phases overlap and must
not be added. A GPU wrapper's wall time includes preparation, driver calls and
readback; it is not GPU execution time or utilization. Python stack samples
identify native-call entry locations, not native internals. Idle event-loop
samples can appear among hot spots, so start with stalls and completed phase
durations. This is not physical pen-to-display latency measurement.

## Disabled behavior and overhead

The launcher creates the controller lazily. Until enabled, there is no sampling
thread, writer thread, active timer, event filter, phase wrapper, resource polling
or log I/O. Existing canvas input/frame timing collection is also off by default.
Stopping restores wrapped methods, disconnects added observers and removes the
filter; reopening the window does not resume a run automatically.

Recording retains at most 3,000 general events, 256 transition breadcrumbs,
256 phase groups with 512 recent durations each, 4,096 stack locations,
100 stalls and 128 active spans. Counts/totals/maxima cover the run; percentiles
cover retained recent samples. Overwrites are reported. The panel refreshes at
most once per second and displays bounded subsets; JSON retains the larger bounds.

A synthetic 20-page, 18,000-pixel chapter probe measured mean raster move-handler
work of **0.109 ms off, 0.130 ms enabled, and 0.121 ms after stopping** across
240 moves per run. These were sequential runs with warm-up/order effects, not a
controlled overhead percentage or a guarantee for complex chapters. An injected
400 ms wait was correctly recorded with a source stack and a 482 ms heartbeat
gap (which includes the normal heartbeat interval). Monitor diagnostics themselves
add work; compare the same reproduction with recording off when assessing final
performance improvements.

## Eyedropper fix and measured effect

Previously, every pointer sample entered exact chapter rendering with scene
culling disabled. Sampling one pixel could render unrelated large halftones and
read full GPU framebuffers back to CPU memory.

Exact sampling now enables conservative culling around the requested region,
without enabling draft effects or editor overlays. Effect/mask/mirror source
captures still include their dependencies. A pointer gesture reuses up to eight
64×64 full-resolution regions, with antialiasing guards (about 136 KiB retained).
The cache resets on each gesture and document/visual/hierarchy/solo changes.
Repeated identical colors no longer re-emit color-panel updates. Public direct
sampling stays uncached so unsignaled model/tile edits remain visible.

Measurements used only the isolated **Pocket-Boyfriend-copy**, the 53,820-pixel
rewritten chapter, native Windows Qt and automatic GPU effects. Separate fresh
processes used a hidden raster canvas and included cProfile overhead. All sampled
ARGB colors matched the previous compositor.

| World pixel | Previous sample | Updated direct sample | Updated gesture sample |
|---|---:|---:|---:|
| 540, 1000 (first use) | 2,944 ms | 400 ms | 412 ms |
| 541, 1000 | 348 ms | 23 ms | 0.093 ms |
| 542, 1000 | 739 ms | 17 ms | 0.046 ms |
| 540, 27000 | 911 ms | 16 ms | 11 ms |
| 540, 50000 | 596 ms | 14 ms | 8 ms |

Five additional points within visible effect-bearing content also matched.
First gesture samples there took **48–769 ms**, with subsequent adjacent samples
at **0.025–0.084 ms**. Expensive effects that contribute to a cold region still
need their full source frame; some first samples remain around half a second.
First-use GPU initialization and geometry caches also contribute. This is the
remaining target for effect-source tiling/caching, rather than a color-accuracy
tradeoff. Tool switching's broader scene invalidation policy is unchanged.

The previous profile spent 3.045 seconds across five samples in
`QOpenGLFramebufferObject.toImage`. An offscreen CPU-fallback baseline also failed
allocating a 71.4 MiB working array for an unrelated large halftone; the successful
before/after comparisons above used native GPU effects throughout.

## Verification and supporting artifacts

Integration tests exercise real Qt mouse/tablet strokes, solo-layer hotkeys and
toolbar actions, dispatched paints, injected stalls, per-run saving, disabled
behavior, failure cleanup and lifecycle restoration. Eyedropper checks compare
normal composition for alpha, blur, outlines, mirrors, halftones, mask-only
visibility, solo changes, zoom/rotation and cache invalidation. An independent
80-pixel projective-image comparison across tile boundaries found zero mismatches.
The combined affected regression suite passed **145 tests**. After additional
destruction/failure cleanup coverage, the final monitor integration and dialog
suite passed **29 tests**. `git diff --check` found no whitespace errors.

- Original architecture and optimization report: [performance-review-2026-09-12.md](performance-review-2026-09-12.md).
- Eyedropper raw timings/profiles and detailed findings: `.artifacts/performance-review/eyedropper-*`.
- Monitor sample logs, rendered UI, overhead results and reproducible probe:
  `.artifacts/performance-monitor/`.

No original artwork project was used as a write destination. Changes here add
diagnostics and improve the eyedropper; the broader navigator, full effect-frame,
invalidation and document-history restructuring recommendations remain in the
architecture report.

The original project has changed since the earlier review's saved inventory,
and a separate editor process was already running during this work. The earlier
byte-for-byte comparison is historical, not a claim that the actively used
original is still identical today. This update's chapter probes loaded the
isolated copy; monitor tests used synthetic documents and isolated settings.
