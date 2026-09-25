# Modifier cache reuse — 2026-09-24

Unchanged artwork should keep its completed pixels while the user changes tools,
selects another layer, or draws elsewhere. A new draft is appropriate when an
actual rendering dependency changes.

## Causes and changes

- A small dirty repaint over a reference requested a smaller modifier output
  region, replacing the completed viewport result with a newly computed crop
  or draft. Canvas and overflow effects now plan against the stable viewport;
  the dirty rectangle only clips painting. Export and nested source-capture
  regions retain their own scopes. The initial full-preview and distant-stroke
  probes missed this path because they never changed the effect's requested
  region.
- Object signatures included an empty live-preview record merely because an
  object was selected, including images. Live brush/eraser/transform data now
  participates only when it can affect that object.
- Full serialized records included presentation metadata: names, expanded cards,
  the selected Curves graph channel, and the last selected raster child. Cache
  signatures now describe rendering dependencies separately. Stroke and tiling
  paths use the same distinction.
- Stage keys depended on recreated image identities and requested camera extents
  even when the captured source and output extent were unchanged. Stable source
  dependencies and actual stage extents allow reuse across those requests.
- Ordinary source, draft, and output LRUs compete for memory. Completed viewport
  output is retained separately, including synchronous effects and an exact
  cached capture subsequently displayed in the canvas. Generic output lookup
  checks retained results before recapturing sources or rebuilding mask fields.
  Completed stage pipelines likewise use their shared extent plan to recover
  retained output before source capture. Shared QImage storage is counted once
  across retained scopes.

Retention stays bounded: the default exact-output budget is 256 MiB with at most
512 scope records. Replacement and eviction remain possible; this is not a
persistent cache of every historical frame. Navigator drafts and unviewed export
captures do not fill the viewport exact-output budget. Drafts cannot become exact
export/source entries.

Real source pixels, live geometry, modifier parameters, linked modifier changes,
mask paint/contributors, opacity masks, clipping, and changed output regions
still invalidate their dependents. An excluded stroke target only changes the
signatures of its own affected ancestor layers.

## Actual canvas dirty-repaint proof

`test_dirty_modifier_reuse.py` uses `_ensure_scene_cache` and its real partial
redraw dispatcher. It covers Array→Curves, Pixelate, Distort, and blurred Outline
across overlapping dirty rectangles, a visible frontmost tracing stroke with
three segments and undo, and partial overflow redraws. It compares completed
reference pixels around the stroke and verifies no effect calls or worker
requests. All 12 cases pass with the viewport fix.

Disabling only `_modifier_viewport_region` in an isolated test process, so it
returns the dirty region again, produces nine failures: Array→Curves, Pixelate,
and Distort recompute in all three scenarios. Pixelate also changes previously
completed pixels in the repainted area. The ordinary blurred Image Outline
cases remain passing controls. The reproduction log is
`.artifacts/cache-stability-20260924/dirty-region-before-fix.log`. No production
files are changed for this comparison.

## Saved-source measurement

The reproducible offscreen probe is
`.artifacts/cache-stability-20260924/saved_reference_probe.py`. It loads only the
existing private project copy under `.artifacts/responsiveness-20260924`, and
edits its in-memory document without saving. The reference remains unselected
while tools, its parent selection, a distant real raster stroke, and undo change.
The probe confirms that the stroke changes actual raster tiles.

This measures the saved 1080×1080 reference image through its normal object
modifier path and parent transform, **not whole-canvas latency**. Other artwork
is omitted from the captured frame: an unrelated saved Halftone on Gradient 5
prevented a bounded whole-scene exact warm-up on the offscreen CPU renderer.
The entire chapter remains loaded for the dependency checks.

Before/after comparison uses original UI modules from commit
`f45f95be9b67fddbd1426160b886840dd3594fdd` in an artifact import directory, without
changing the checkout. Ordinary LRUs are deliberately evicted while completed
output fits comfortably inside the retained budget.

| Saved reference stack | Before, after each ordinary-cache eviction | After |
| --- | --- | --- |
| Saved Outline | Recaptured 1131×1131 source and reran the effect; median 8.86 ms | No capture, effect call, or new job; median 0.51 ms |
| Outline plus Curves added only in memory | Recaptured 1131×1131 source; median 1.46 ms | No capture, effect call, or new job; median 0.62 ms |

All ten operations in each current case preserve exact pixels. The Curves case
starts with a visibly different draft (686,927 changed image bytes), then keeps
the completed result through subsequent operations. The saved Outline-only case
already stayed visually exact before the fix, but needlessly recomputed under
cache pressure. Tool changes and distant strokes alone did not reproduce a blur
in this old saved copy.

Raw results, before/after logs, and first/exact PNGs are in
`.artifacts/cache-stability-20260924/`; baseline results are in its `baseline/`
subdirectory. Timings are local CPU capture measurements, not display latency.

## Regression strategy

- `test_dirty_modifier_reuse.py`: actual scene-cache dirty redraws, overlapping
  tracing gestures, undo, and overflow keep completed viewport effects.
- `test_modifier_cache_stability.py`: unselected reference pixels across tool,
  selection, real stroke, undo, and ordinary LRU pressure; HSL, Curves, Blur,
  synchronous Outline, and Array→Curves; exact-capture-to-viewport promotion.
- `test_modifier_cache_dependencies.py` and `test_mask_cache_dependencies.py`:
  shared/linked edits and real mask/source changes refresh affected artwork
  while unrelated targets reuse their pixels.
- `test_stage_dependency_reuse.py`: generic color/blur effects, dynamic Distort,
  pattern effects, spatial Array/Mirror/Radial Blur/Cage stages; metadata and
  camera reuse alongside real parameter, mask, extent, and export checks.
- `test_interactive_patterns.py`, `test_interactive_strokes.py`,
  `test_compound_stroke_performance.py`, and `test_tiling_cache_dependencies.py`:
  bounded retention, worker completion, sequential stacks, stroke/compound
  metadata, and tiling dependency behavior. These remain separate rendering
  paths and receive explicit checks rather than assuming generic reuse covers
  them.
