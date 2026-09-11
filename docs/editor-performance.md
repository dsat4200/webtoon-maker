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
```

The editor and modifier benchmarks write timings and profiles to
`.artifacts/editor-performance`. The focused regressions are
`test_interactive_effects.py`, `test_interactive_patterns.py`,
`test_fill_interaction_performance.py`, `test_async_autosave.py`,
`test_color_picker_sampling.py`, and `test_outliner_reveal.py`. Existing text,
fill, stroke, pattern, masking, export, persistence, and session suites provide
additional coverage.

## Validation results

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
