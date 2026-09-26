# Brush work — paused September 26, 2026

The user explicitly requested a pause to conserve usage and work on unrelated
features. Resume only when asked. This is an unfinished checkpoint, not a
completed CSP compatibility claim. Changes remain in the working tree; no
commit or PR was created. A source snapshot is stored locally at
`.artifacts/brush-engine/paused-2026-09-26.zip` as an extra recovery point.
Do not blindly restore it over subsequent unrelated work.

## User requirements and accepted scope

- Importing a `.sut` should reproduce its intended brush automatically. Fix
  shared importer/renderer behavior; do not introduce per-brush tuning hacks.
- Raster layers, a dedicated Brush tool, shared live stroke previews, and a
  drawing playground. Keep brush geometry reusable for future vector layers.
- User approved excluding ruler snapping and erase-all-layers. Reference-layer
  dependencies are excluded. Pen-angle validation is deferred at the user's
  request; preserve imported fixed image orientation.
- Sliders plus typed input for size/opacity, without arrows. Practical size
  range approximately 0–200 px; whole pixels and whole opacity percentages.
- Preserve saved playground drawings and original imported definitions.

## Latest feedback

HIMOG01, Sketch/Pencil02, blender05, foliage06, blood08–10, Hazy18,
watercolor28/32, pen33 and Flat34 were good. SRU03 gaps differ; oil04 may not
pick up enough canvas color; blood07 is slow; Hazy19 texture size differs.
The oil screenshot has white selected and shows pale transported pigment:
mixing exists, but native strength remains uncalibrated.

## Current implementation and checks

- New shared `BrushDefinition.with_size()` changes nominal size and explicitly
  linked secondary size while preserving independent texture/particle/blur
  lengths. Playground sample and pad use the same whole-pixel nominal size.
  New exercises open at 100% zoom. Added a sample-size label, not yet visually
  checked in a rebuilt full sheet.
- SRU/Hazy screenshots exposed an inconsistent old comparison sheet. Matching
  our sample and runtime does **not** prove CSP parity. SRU automatic spacing
  still uses an approximate table; Hazy material registration needs native
  comparison. Do not describe these native discrepancies as fixed.
- Negative texture contrast previously became negative gain below -33.33%,
  unintentionally reversing luminance. Generic negative contrast now flattens
  monotonically with gain `1 + contrast`; nonnegative behavior is unchanged.
  Source values/assets/scale are preserved. **62 focused tests passed**;
  native transfer curve remains uncalibrated.
- Size/opacity controls now display and accept rounded integers; size slider
  is linear 0–200, larger typed values remain allowed. **14 UI/playground
  tests passed**, but review found two remaining edge cases (below).
- Previous blur-mode and continuous-paint revision passed **692 checks** in
  `.artifacts/brush-engine/refined-final-tests.txt`. This predates the latest
  contrast/cache/UI changes and is not the current full-suite result.
- Automatic/Fixed running blur and moving/held continuous buildup are
  implemented. Exact native blur kernel/cadence are still approximations.

## First tasks on resume

1. Fix two integer-control edge cases in `comic_editor/ui/brush_controls.py`:
   explicit typing that equals the displayed rounded value does not emit
   `valueChanged` (e.g. imported17.234 displays17; typing17 leaves17.234).
   Track user text edits and commit independently, without duplicate saves or
   rounding untouched imports. Also edited0 silently becomes core minimum0.1.
   A proposed whole-pixel edit minimum1 (size slider1–200, opacity0–100) was
   assigned but **not implemented before pause**. Preserve untouched imported
   subpixel values. Add regressions and update range documentation.
2. Finish validating the current **lossless compressed-row material cache
   prototype** in `core/brush_raster.py`. It is present in the working tree.
   It stores large compressible RGBA8 originals as independent compressed row
   blocks (<=256KiB decoded block), within the existing128MiB budget. It does
   not resize/drop source pixels. Full original reconstruction is still
   transiently needed for exact Qt mip generation.92 focused cache/pixel
   tests passed.48 additional compressed full-stroke cases were added after
   that run and remain pending combined validation.
3. Real blood07 mouse benchmark completed with exact baseline output hashes:
   warmed p95 about141.6→39.4ms, max328→84ms, total5.006→3.171s; misses32→0,
   retained cache130.8→9.4MB. Cold total worsened5.17→5.64s from compression;
   cold p95 improved150→68.7ms. These were agent-reported partial results;
   the profiler did not finish writing final JSON before a later varied-
   pressure pass ran out of host memory. **Repeat/record the remaining gate
   before claiming a finished performance fix.** Baselines exist in
   `blood-live-before.json` / `blood-live-reuse.json`. Script:
   `.artifacts/brush-engine/profile_blood_live.py`.
4. Run combined brush/SUT tests plus settings, hotkeys, canvas input performance,
   color ribbon and async autosave after production edits stabilize. Serialize
   Python jobs; host commit memory fluctuated between100MB and1.3GB spare.
   Set OPENBLAS_NUM_THREADS=1, OMP_NUM_THREADS=1, QT_TLS_BACKEND=schannel,
   PYTHONIOENCODING=utf-8. The host sometimes prints Qt TLS0xc0000139 despite
   exit0/pass; inspect actual exit and summary.
5. Build and visually check the new v6 sheet with the ignored local script
   `.artifacts/brush-engine/build_downloaded_playground.py`. It targets
   `.artifacts/brush-playground/downloaded-csp-v6`, which **does not exist at
   pause**. Review SRU/Hazy3, sample labels and controls; write actual validation
   results. Only then point `brush-playground-refined.bat` to v6.

## Saved sessions and unresolved fidelity

- `brush-playground-custom.bat` opens v4 with the user's saved drawings.
  **Never overwrite/rebuild v4.** v5 also exists and is preserved.
- `brush-playground-refined.bat` was restored to existing v5 at pause so it
  does not point at an unbuilt v6. It uses current working-tree code, including
  the pending cache prototype; it is not a frozen release.
-34 of35 requested downloaded brushes strictly import. II2 Watercolor7 has
  an enabled secondary tip whose material list is genuinely NULL. Keep it
  omitted; permission for a labeled primary-only approximation was unanswered.
- Wool/knitting arbitrary-angle ribbons still have seams. An experimental
  row-remapping distortion was rejected and reverted. Imported source angles
  are intact; do not substitute zero or claim parity from author thumbnails.
- Native CSP computer-use capture failed repeatedly with
  `SetIsBorderRequired: No such interface supported (0x80004002)` on Windows10
  build19045. No blind inputs. Do not retry without a relevant state change.
  All three tutorial videos remain unreviewed; browser transcript export
  timed out. No native-CSP matched-stroke claim is supported.
- Latest34-source scalar audit found no confirmed active dropped scalar with
  a supported exact counterpart. `ChangeRGBByDual=1` occurs only blood04
  (`🤚血`) where dual is disabled, so dormant. Active angle flag0x10 (value19)
  appears on blood01/04/05/06 primary and blood03 secondary; it currently warns
  and falls back fixed. Exact flag meaning is unverified; do not guess-map it.
- Core remaining approximations: automatic spacing, wet mixing equations,
  texture transfer/registration, arbitrary-angle ribbons and stochastic/input
  response calibration. The detailed evidence is in the other brush docs.

## Workspace care

All agents were interrupted at pause; no test/build Python process remained.
The existing user editor process was left open. Do not close user applications
to free memory without authorization. Third-party fixtures/materials remain
under ignored `.artifacts`, not redistributed. Unrelated user changes exist
under `lighter-novel/`; preserve them. Consult this note before resuming and
recheck the current working tree because unrelated work may follow this pause.
