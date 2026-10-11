# Pocket Boyfriend raster, cage and drawing preset repairs

Measurements and validation use the private complete copy at
`.artifacts/desktop-pocket-20261008/project-copy`, with native Windows Qt.
No benchmark saves the copied project or changes the open editor's preferences.
Cage benchmark reports hash all 13,776 project files before and after; every
manifest is unchanged. The raster probes compare original chapter/series JSON
and the selected original raster PNGs before and after their in-memory edits.

## Raster presentation diagnosis

With exact scene work deliberately delayed, a newly painted source tile remained
correct while changing selection displayed the preceding exact scene. A second
contact with different prepared coverage could similarly discard the first
contact's displayed pixels. Same-configuration older viewport previews could
replace newer visible artwork. These are presentation handoff errors, rather
than lost source tile mutations.

The repair retains a bounded provisional native presentation across committed
contacts and selection handoffs until ordinary exact replacements are ready.
It uses the existing native contact composition and renderer, retains cumulative
coverage, and reconstructs a selection checkpoint without prediction. Erasure
patches replace their previous scene pixels, so old ink cannot reappear beneath
transparent erased output. History and unrelated artwork changes invalidate
this presentation. It never enters the exact projection cache or disk backing.
Rotated checkpoints reuse the existing flattened presentation format and
document clip; a translucent stroke must not become darker after selection.

## Cage diagnosis and measured scope

Selecting a drawing or making its rectangle/lasso path took approximately
0.14–1.19 ms in the isolated saved-source probe. Starting a cage instead copied
unrelated chapter owners and source pin metadata. Its live preview allocated
and warped a complete native image, including a roughly 74-million-pixel source
for Raster 17. That source exceeded the existing native allocation limit.
The saved Outline modifier also required a bounded source for the whole-scene
live demand; fixing only the nested cage kernel would not address that path.

Preparation and commit now capture only selected original raster/vector owners
with ordinary immutable ancestor/modifier/mask geometry and original source
pins. Tiling-dependent geometry retains the complete snapshot. These captures
do not replace the scene compiler's complete records or obtain a disk cache
specification. Native ownership and stale-revision checks remain active.

Positively owned legacy live cage demands allocate their source draft before
warping, using the existing 224-pixel edge and nominal 32,768-pixel policy
(integer dimension rounding permits up to 33,217 pixels). Supported affected
object/group modifier sources use the existing inverse-scale stage mapping
within that same private draft. The source/output document frames remain
complete. New draft keys and provisional status isolate these lower-resolution
temporary devices from exact artwork. Camera zoom and display density do not
increase their sampling. Exact output, commit, floating contracts, masks and
special captures retain their original sampling, source grids and precision.

| Saved-source measurement, three repeats | Before | After |
| --- | --- | --- |
| Raster 17 cage input snapshot | 173 objects, 2,150 tile pins | 1 object, 422 original tile pins |
| Raster 17 cage startup, cold | 1,380 ms | 962 ms |
| Raster 17 cage startup, warm | 612 / 662 ms | 436 / 418 ms |
| Raster 9 isolated live preview, median | 461 ms | 22 ms |
| Raster 9 temporary output | 692 × 685 | 182 × 181 |
| Raster 17 isolated live preview | Native allocation error | 285 / 295 / 329 ms, 15 × 224 source |
| Raster 17 whole-scene live demand with saved Outline | Native allocation error | 304 / 323 / 338 ms, provisional |

Reports are in `.artifacts/cage-selection-20261011`: `before.json`,
`after-focused.json`, `preview-before.json`, `preview-after.json`,
`native-scene-check.json` and `native-scene-after.json`. Reproduction scripts are
`tests/benchmark_cage_selection.py` and `tests/benchmark_cage_preview.py`.
The latter accepts `--native-scene` for a whole-scene demand; its default
isolates the preview kernel, rather than measuring an editor frame. The original
preview method is retained with the baseline evidence.

The cold Raster 17 owner-thread event pump still took 586 ms, and decoding its
422 original tiles dominated its first preview (277–322 ms). These results
do not establish real-time behavior for every saved stack. The preexisting
native 64-million-pixel allocation limit remains for exact rendering/final
baking; a very large final bake can still reject that frame.

## Pencil and eraser behavior

Both tools have independent preset libraries, illustrated selection cards,
per-preset settings and a compact Size / Manage presets strip. Management
creates, renames and deletes presets while retaining at least one. Shape,
the shared size/opacity pressure curves and their enable options, density,
antialiasing, taper and transform handles live in each preset's settings.
Settings → Pencil/Eraser owns each tool's S/M/L sizes and startup size.
The unused Current tool collapse header is hidden.

Schema 24 migrates old eraser shape into its first library once and preserves
old default full-strength erasing. Eraser strength is independent of the
foreground color. Full strength retains Clear, partial strength uses native
DestinationOut, and zero strength changes neither pixels, frame nor history.
Tiled erasing retains native source precision, including RGBA64 alpha.
Square pencil strokes do not receive a circular prediction tail.

## Verification

`tests/test_raster_presentation_continuity.py` deliberately blocks exact workers
and exercises successive chunks, changed coverage, old previews, selection,
prediction removal, erasure and history. A rotated translucent selection
checkpoint must preserve the complete presentation bytes. Its opt-in
saved-project probes load
the unchanged chapter graphs and check native source buffers, exact settlement,
undo model restoration and original file hashes.

`tests/test_cage_source_capture.py` compares complete native source/output/
presentation bytes, formats and color profiles with frozen preceding CPU
source and warp implementations. It covers nearest, bilinear and Catmull–Rom
interpolation at small and larger dimensions, legacy and floating contracts,
commit before/after a draft, cancellation, giant live demands and object/group
modifier captures. `tests/test_drawing_presets.py` covers migration, independent
libraries, shared curve options, native eraser precision, no-op pressure,
exact undo and compact layouts at 220 and 350 pixels. UI captures are in
`.artifacts/drawing-presets`.

The native Windows integration run covers 692 tests across 30 files, with no
failures or skips. The final affected feedback suite covers 256 tests across
eight files, including 16 continuity cases, both opt-in saved-project cases
and the rotated checkpoint correction, with no failures or skips. XML and
saved-source reports are under `.artifacts/pocket-fixes-20261011` as
`final-integration.xml`, `final-feedback.xml` and
`freshness-final-{plain,ordinary}.json`.
