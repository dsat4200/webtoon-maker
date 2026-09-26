# Page transform alignment — 26 September 2026

Page translation now keeps the saved **swage** page's mesh-warped Blender image aligned with its drawn outlines during preview and after release. The final full-chapter preview and commit match in every pixel of both the native framebuffer and the overlay-free exact render.

The implementation now uses temporary transformed descendant modifier controls during a page/group preview, includes that preview in the rendered source frame, reads the live parent transform for clip rejection, and preserves page placement through model validation and persistence. Committed model data is unchanged until release. Page-height calculations and negative-page correction also account for the persisted page transform.

The causes were separate: descendant warp controls stayed in their committed world positions during preview; source bounds and an inherited clipping check used committed transforms; and validation discarded page placement on redo/reload while retaining the moved warp. The clipping issue was visible only in the native renderer's rightmost tile strip and was fixed before accepting the final run.

## Saved-project verification

`tests/benchmark_page_transform.py` loads the fresh isolated project copy, activates the same canvas transform tool used by Ctrl+T, and sends native mouse press/move/release and Escape events. The page moves by 80 × 120 document pixels. The camera follows by the same amount so preview and committed artwork can be compared at identical screen coordinates. The test covers preview, release, undo, redo, a second preview, Escape, and a separate saved/reloaded chapter.

The fixture retains all saved modifiers and masks: visible `slam.png` image `0a688546290744858bf3626dd04ceccd`, its **6 × 5** mesh with smoothness 50, HSL mask, outline, and raster `9db9d8094e744ee4aaffb84e9db10f7c` containing the drawn shoe outlines. This is the latest saved scene; no unsaved live editor state is changed. A solo-page run diagnoses the affected content; the final run includes the full chapter.

The frozen baseline's solo-page comparison found the following differing-pixel counts. Undo and Escape restored the original image. Redo cleared `transform_frame`/`transform_quad` despite retaining the moved descendant warp, confirming a model error as well as the preview error.

| Comparison | Baseline exact image | Baseline native framebuffer | Final full-chapter exact / native |
| --- | ---: | ---: | ---: |
| Preview → commit | 260,623 | 229,335 | 0 / 0 |
| Commit → redo | 513,648 | 453,589 | 0 / 0 |

Separate cross-version comparisons confirm that the untouched page and the correctly committed result preserve their baseline pixels: both exact and native comparisons have zero differing bytes. The fixes correct preview and restored state rather than changing the intended committed artwork.

The final run also verifies zero pixel differences for undo, repeated preview, and the saved/reloaded committed image. Escape restores the exact original artwork; its native framebuffer naturally differs because Escape dismisses the transform handles. Preview leaves the complete chapter model unchanged, and undo/redo/validation/deserialization/save/load preserve their expected complete models. Every painted mask tile and Raster 34 tile remains byte-identical. All native readbacks finish without pending work or renderer errors.

`final-full-scene/summary.json` and `final-validation.json` are the final results. The final visual review files are `final-full-scene/preview-native.png`, `commit-native.png`, and `redo-native.png` under `.artifacts/page-transform-20260926`.

Focused regression coverage includes modifier preview helpers, source bounds, inherited clipping, mouse and pen release, cancellation, nested mask dependencies, model persistence, and transformed-page height safety. Separate passing runs comprised 31 helper/page/dependency tests, 19 clipping tests, 46 model/warp/dependency tests, and existing suites of 105 models/assets/persistence/page tests, 79 tiling/cage tests, and 110 page-insertion/shape-path tests. These runs overlap and are not an aggregate unique-test count.

Native readbacks use the existing OpenGL framebuffer, with the normal asynchronous setting enabled. A separate synchronous render excludes selection controls for the exact image oracle. Native readiness checks distinguish pending content from a finished frame; readback does not request an extra paint. These captures establish correctness, not input-latency or performance measurements.

The HSL mask retains its existing world-coordinate behavior. Thus, an original-versus-translated RGB comparison is not the acceptance condition; **preview versus commit at the same translation** is. In the full chapter, unrelated pages also remain stationary while the camera moves.

## Safety and provenance

The source was frozen before production edits under `.artifacts/page-transform-20260926/baseline-source`. The original project was read only and copied to `.artifacts/page-transform-20260926/project-copy`; all **8,673 files (124,436,005 bytes)** matched before/after source and copy hashes. The final integrity check still matches every original and copied file. Save/load verification writes only a new directory inside its run artifacts. No Blender connection, live editor interaction, or real settings writes are used.

Artifacts retain the failed baseline and intermediate diagnoses rather than replacing them. `baseline-corrected` is the authoritative baseline: an earlier exploratory run used a stale mouse release coordinate after camera compensation. `fixed-solo` and `fixed-full-scene` isolated the native strip problem; `clip-diagnostic` confirmed the correction with identical initial, repeated, invalidated, and fresh-transform native previews.
