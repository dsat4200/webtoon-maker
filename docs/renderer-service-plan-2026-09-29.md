# Renderer service boundary — 29 September 2026

## Choice

Implement recommendation 1 from the architecture review: establish a renderer
boundary independent of the canvas. It is the quickest complete architectural
change because the application already separates retained document tiles from
their presentation. Recommendations 2–5 require new effect algorithms, GPU
resource ownership, scheduling semantics, or color-management behavior;
recommendation 6 spans storage, loading, memory ownership, and undo.

## Implementation

1. Give `comic_editor.render` ownership of the document projection cache,
   region composition, and fixed-block tile scheduling.
2. Introduce immutable document/view metadata, region requests, and explicit
   quality, output phase, resolution, capture region, requested region, and
   revision. Return revision-tagged exact, provisional, pending, failed, or stale
   results. Never admit a result after its document/revision has changed.
3. Put the existing scene renderer behind a synchronous compatibility backend.
   The service must not import canvas/UI modules, inspect a widget, read editor
   flags, or own an input event loop. The backend translates an explicit request
   into the existing scene renderer's temporary state and always restores it.
4. Route retained canvas captures through the service, keeping the established
   canvas entry points for performance instrumentation. Send full and regional
   document invalidations to the service. Keep GPU presentation and editing
   overlays in the UI.
5. Preserve existing file formats, effect kernels, exact-pixel publication,
   navigation priority, frame deadlines, and live drawing behavior.

The backend is deliberately synchronous and reuses existing scene/effect
implementations. Immutable requests are not detached copies of all document
pixels. A future worker/native backend will need detached source ownership;
this change must not move the legacy canvas renderer onto a worker thread.
Non-projection scene rendering, asset capture, and export remain independent
reference paths for compatibility verification.

## Verification

- Exercise the service with a backend that has no canvas or widget: region
  composition, overflow, output phases, exact/provisional/pending/failure
  status, request validation, revision/document replacement, fixed capture
  blocks, and frame-budget yielding.
- Exercise the real compatibility backend: restore all temporary state on
  success, pending effects, failure, and unexpected exceptions; verify explicit
  request values override ambient canvas capture flags.
- Run existing projection, dependency, live ink, solo, masking, transform,
  save/export, and performance instrumentation regressions.
- Run native GPU presentation checks when available. Treat skipped driver tests
  as unverified, rather than evidence of GPU correctness.

## Completion criteria

Production retained document captures use the service; the service owns and
operates the projection cache and fixed-block region renderer without importing
the UI; edits invalidate its revision; stale work cannot enter the cache; the
compatibility backend isolates temporary legacy state; existing saved source
data and rendered artwork remain equivalent in the regression checks.

This is an architectural extraction, not a claim of faster effects, native
execution, a complete compositor graph, or asynchronous GPU computation.

## Implemented result and evidence

The service and compatibility adapter are integrated into production retained
captures. Document edits call the service's invalidation API. Existing canvas
capture entry points remain available to the performance recorder, and previous
projection imports resolve to the renderer-owned implementation.

Final verification on 29 September 2026:

- **351 regression tests passed**, zero failures/errors/skips. Coverage includes
  the new service/backend contracts, projection, revision invalidation,
  asynchronous publication, live ink, viewport culling, solo retention, effect
  dependencies, masking, transforms, promoted artwork, baking/export, save
  integrity, and asynchronous autosave.
- **28 native Windows OpenGL tests passed**, zero failures/errors/skips, covering
  real GPU presentation, retained navigation, overlays, context cleanup, and
  grid/live-ink integration.
- **18 comparisons against the pre-extraction `HEAD` renderer had zero differing
  bytes**, including synthetic raster underlay, promoted artwork, blur, HSL,
  overflow off/on, base/top/combined phases, and 0.5×/1×/1.25× rendering.
- A fresh process imported `comic_editor.render.service` without importing any
  `comic_editor.ui` module. `git diff --check` reported no whitespace errors.

Machine-readable evidence is under `.artifacts/render-service-20260929/`:
`regression-tests.xml`, `native-tests.xml`, and
`baseline-pixel-comparison.json`. All scene inputs are synthetic or pytest
temporary data; no existing user project or Blender connection is used.

The regression process emitted the same Windows Qt TLS discovery diagnostic
seen before this extraction; it completed with exit code zero and all tests
passing. No network/TLS code was changed.

The backend remains on the owning canvas thread, with an explicit rejection of
worker-thread access. Existing scene/effect kernels, export, and asset rendering
are retained behind/beside this boundary. Detached pixel snapshots and a native
or GPU graph backend remain subsequent architecture work.
