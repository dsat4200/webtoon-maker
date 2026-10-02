# General tile evaluation

Implemented October 1, 2026. The performance measurements below compare against
the preserved working tree, including the radial changes that preceded this work.

## Goal and compatibility

Make exact viewport work depend on the requested output and each effect's spatial
footprint. Keep the current RGBA8 stage rounding, document coordinates, masks,
transforms, exports, and the radial-integration improvements already in progress.
This is recommendation 2; GPU execution and resolution policy remain separate work.

## Implementation

1. Compile the modifier stack into nodes with stable semantic frames and explicit
   required-input and evaluate callbacks. Evaluate recursively on a 256-pixel grid.
   Cache by source generation, upstream settings/masks, frame, transform, and tile.
2. Support point adjustments and native neighborhood filters using bounded halos.
   Add a frame-addressed blur pyramid with globally aligned, byte-exact bilinear
   resampling. Keep explicit shared-frame nodes for contour, pattern, reduced-scale,
   and spatial kernels that require their existing full-source behavior.
3. Capture scene sources lazily for eligible exact projection requests, so a cold
   tile does not allocate or paint the complete object/layer. Respect provisional
   masks, source revisions, and detached effect-worker jobs.
4. Verify tile seams, negative/odd coordinates, masks, parameter edits, prefix
   reuse, asynchronous completion, global fallbacks, and byte-budget eviction.
   Compare exact pixels and timing against the current working source snapshot,
   including existing uncommitted radial changes. Run rendering regressions and
   native Windows OpenGL checks before delivery.

## Evidence

The unchanged baseline is saved under
`.artifacts/tile-evaluator-20261001/baseline-source`.
Measurements and comparison results are saved beside it. Benchmarks use
synthetic artwork or an isolated project copy and do not save user projects.

## Result

`render/tile_graph.py` evaluates nodes recursively on a fixed document grid.
Each node has a semantic frame, an upstream dependency identity, an input-region
callback, and a kernel callback. Disjoint input regions are supported without
allocating the gap between them. The camera affects demand, rather than identity.

`ui/tile_effects.py` compiles existing stacks into these nodes and adapts their
caches, masks, transforms, and scene capture. Sources are captured on demand
instead of painting a complete object or layer before asking for an effect tile.
Generic stacks keep float intermediates until their existing final conversion;
spatial stacks keep their existing per-stage byte rounding. Finished requests and
unchanged prefixes remain reusable after later parameter edits.

The nested blur graph computes globally phased reduced levels and fetches the
exact source support for each resize. Its coefficients and pass rounding match
Pillow, including odd dimensions and the legacy alpha behavior. Immutable reduced
tiles use a separate thread-safe 32 MiB LRU. Deferred jobs capture their source
dependencies before submission, perform filtering in workers, and publish through
the existing revision-aware exact-result path. Repeated pending requests avoid
recopying inputs and masks.

| Effect family | Evaluation strategy |
|---|---|
| Point adjustments | Same-coordinate input tiles; stable masks and stage precision |
| Blur | Region-addressed pyramid with the original full-frame phase |
| Native single-pass Kuwahara | Neighborhood halo and global sampling coordinates |
| Unmasked-radius/strength sharpen | Four-sigma halo and original edge rule |
| Solid outline in eligible stacks | Thickness plus blur halo; transparent outside source |
| Dither in generic stacks | Original full-image lattice coordinates |
| Integer-translated Array | Disjoint inverse-mapped source islands |
| Rotated/scaled/reflected copies | Shared full frame to retain Qt sampling rounding |
| Cage, distort, radial | Explicit complete-source dependency with bounded output where supported by the existing kernel |
| Pattern, brush contour, reduced/repeated Kuwahara, smudge, simplified Posterize | Shared full-frame node |

Small frames at or below 256² pixels keep their existing kernels. Specialized
all-solid-outline stacks keep their existing path. Sharpen radius/strength masks
and generic float stacks containing strength-masked blur also retain their
whole-field algorithm decisions: a constant tile in a varying field must not
select a different filter or zero-strength rounding rule. Provisional and
special-purpose captures keep their existing behavior. GPU kernels, export,
baking, and resolution policy were not rewritten.

## Measurements

Synthetic 30,000 × 1,920 document, 768 × 512 viewport, exact synchronous scene
computation on this machine. These are one-run diagnostic timings, not
input-to-display latency or a frame-rate guarantee. Both runs use the same source
artwork, parameters, masks, and cache budgets. The baseline source snapshot is
independent of the modified checkout.

| Stack / request | Before | After | Gain |
|---|---:|---:|---:|
| Brightness → blur → hue, cold | 9,026 ms | 551 ms | 16.4× |
| Same stack, final hue edit | 9,098 ms | 81 ms | 111.9× |
| Same stack, blur edit | 9,632 ms | 614 ms | 15.7× |
| Same stack, source edit | 9,581 ms | 590 ms | 16.2× |
| Brightness → blur → sharpen → hue, cold | 17,121 ms | 742 ms | 23.1× |
| Same stack, pan | 17,344 ms | 128 ms | 135.8× |
| Same stack, final hue edit | 17,071 ms | 78 ms | 219.7× |
| Same stack, blur edit | 18,403 ms | 703 ms | 26.2× |

Repeated unchanged requests remain below 1 ms in both paths. The simple blur
stack's first pan costs 94 ms instead of 0.7 ms: its baseline had already computed
and retained the entire frame. The new evaluator computes newly exposed tiles.
This trades some warmed pan cost for bounded cold/edit work and source capture.
The larger sharpen stack's baseline cannot retain all its complete intermediates
within the configured budgets and therefore pays much more on a pan.

The same benchmark was run at 8,192 × 1,024. All **24** before/after viewport images
(cold, warm, pan, final edit, blur edit, and source edit for both stacks and sizes)
are byte-identical. Source-capture regression tests verify that individual source
images stay at or below 256² pixels and distant translated copies do not allocate
the intervening gap. The benchmark's allocation tracing covers `empty_image`
allocations; it is not a process-wide peak-memory measurement.

- [Pixel comparison and full timing data](../.artifacts/tile-evaluator-20261001/pixel-comparison.json)
- [Rendering regressions](../.artifacts/tile-evaluator-20261001/regressions.xml): 1,361 passed; nine offscreen native tests skipped.
- [Native Windows OpenGL verification](../.artifacts/tile-evaluator-20261001/native-tests.xml): 42 passed, no skips.
- [Final targeted verification](../.artifacts/tile-evaluator-20261001/final-focused-tests.xml): 140 passed, no skips.

Tests cover tile seams, negative/odd and long frames, premultiplied alpha,
variable radii, focal origins, transforms, mask/source generations, prefix reuse,
source capture, cache eviction, detached worker completion, and unchanged export
and capture behavior. Existing scene, projection, hierarchy, radial, masking,
pattern, baking, and modifier regressions also pass.

To reproduce, run `python tests/benchmark_tile_evaluator.py` and
`python tests/benchmark_tile_evaluator.py --width 30000 --height 1920`.
Add `--baseline` when the preserved source snapshot is available. Outputs stay
under `.artifacts/tile-evaluator-20261001`; no user project is opened or saved.
