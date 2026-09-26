# Raster Brush tool and validation playground

The new **Brush** tool (`Shift+B`) paints Raster objects independently of the
existing Pencil. It is an initial CSP-compatible engine and importer under
active visual calibration, not a claim of complete CSP equivalence.

## Try it

For the feedback revision, double-click **`brush-playground-v2.bat`**. It uses
the separate `.artifacts/brush-playground/feedback-v2` session, preserving the
drawings in the original playground. The locally prepared sheet adds the
downloaded chain 6 and nine Flipnote presets reconstructed from this machine's
CSP materials/settings, as well as original pixel-dot, stripe and checker
examples. Local third-party assets are not bundled with the source code.

Double-click `brush-playground.bat` in the repository, or run
`python brush_playground.py`. This opens a separate editor process with its own
preferences and a saved drawing project under
`.artifacts/brush-playground/current/project`. Existing artwork and normal
editor preferences are not used by the playground.

Use **Try a brush** above the canvas to select an exercise and its drawing
target. Draw in the empty area beneath the reference stroke. The default sheet
includes pen, graphite, airbrush, watercolor, wet paint, chain, foliage,
splatter, calligraphy, dual pencil, an RGB flower stamp, and flat pixel patterns.
Wet exercises have
blue and yellow paint on the active drawing layer. `Ctrl+Z` undoes a stroke;
`Ctrl+S` saves your drawing. Reopening the launcher keeps the saved project.
`python brush_playground.py --new` makes another sheet without replacing it.
Use `--session PATH` to create or reopen a named isolated playground folder.

In the ordinary editor, select a Raster object and choose **Brush**. Its
Tool Settings contain the preset selector, size, opacity, **Brush settings**,
and **Import .sut**. Detailed settings include response-curve editing and the
secondary brush. The menu thumbnails and larger preview use the same engine
as the canvas, one shared S-shaped input/pressure template, and a fixed seed.
They refresh after edits; very large tips are scaled to fit the preview.

New playground sheets use the same drawing size for the reference stroke and
the empty pad, starting each exercise at 100% view. Fixed texture scale, blur
widths and other independent distances stay unchanged; only a size-linked
secondary tip follows the main size control. This fixes the larger woven
texture in Hazy 3's drawing pad: older sheets applied thumbnail zoom to the
sample alone. Existing sheets and saved drawings are not rewritten.

In **Brush settings → Stroke**, **Keep the same size on screen** maintains the
brush's apparent size when zooming, as described by CSP's
[screen-size option](https://help.clip-studio.com/en-us/manual_en/810_subtools/B.htm).
The view scale is captured when each stroke begins; changing zoom does not
resize a stroke already in progress. Stored sizes stay unchanged, linked
secondary sizes follow the main brush, and previews use 100% view. For
nonuniformly transformed raster objects, sizing preserves the apparent area
at the starting point; it retains the object's stretching or perspective
rather than forcing every tip into a screen-space circle.

Import files individually with **Import .sut**. Imported materials are embedded
in the preset, so the source file can move afterward. Compatibility notices
remain in the settings dialog. An unavailable active material is an import
error; it is never replaced by an arbitrary round tip or thumbnail.

For a new playground containing local brushes, repeat `--sut PATH`, for example:

```text
python brush_playground.py --sut "my-pencil.sut" --sut "my-chain.sut"
```

## Implementation

- Brush definitions and pointer samples contain no Qt canvas or vector-layer
  dependencies. Input has pressure, tilt, barrel rotation, and timestamps.
- Distance-based sampling carries spacing across input packets. A stroke seed
  controls scatter, material ordering, and random dynamics. Continuous paint
  adds elapsed-time dabs while moving or holding still. Stroke opacity and
  per-tip density are separate.
- Material tips keep full registered dimensions and padding. Alpha masks,
  gray main/sub-color tips, and authored RGB tips have distinct color behavior.
  Multiple tips, flips, thickness, angle, spray, texture, and ribbon deformation
  feed the raster backend.
- Imported color changes preserve signed HSV offsets and enabled input
  responses. Constant and pressure-only changes do not force random variation.
  Main/sub/both targeting is separate from the existing authored jitter controls.
  Secondary drawing color paints both parts of a gray material with that color;
  transparent drawing erases its black portion, following the
  [official material guide](https://tips.clip-studio.com/en-us/articles/679).
- The renderer supports two independent brush planes, blend modes, color
  pickup/transport, watercolor edges, antialiasing, and selected-region masks.
  Transparent drawing colors erase. Post-correction, percentage tapering and
  ending effects on spacing replay against the original layer at pen-up and
  remain one undo operation. Fade progresses to the selected minimum and stays
  there. Taper targets retain separate minimum values.
- Finished pixels use the existing sparse tiles, rendering, transforms,
  persistence, export, and undo. The brush library is separate from chapter
  pixels. There is no new vector-layer implementation.

## What has and has not been validated

The supplied 2018 archive contains **28 SUTs and 92 unique original raster
materials**. All were decoded. The importer follows the active Variant and
recovers actual Offscreen tile data through C2F and SQLite overflow pages. It
does not use the material thumbnail or reduced CanvasPreview as the tip.

A read-only snapshot of the installed CSP **1.12.0** database supplied additional
research fixtures: pens, pencils, dual watercolor, colored bead chain, ribbon
chains, leaves, bloodstain, rose, outline, and zippers. These are explicitly
labelled reconstructed fixtures, not native CSP exports. Local third-party
artwork remains in `.artifacts`, outside the distributed built-in presets.

The user's `vislon close` screenshot helped verify two-color material behavior:
white as main color and black as secondary produces dark filled teeth; the
opposite pair produces dark outlines and white interiors. Preserve the original
tip rather than altering its pixels to match one preview color pair.

Automated checks cover packet subdivision, deterministic randomization, repeat
order, taper, opacity accumulation, image colors/padding, tile seams, ribbons,
resource budgets, selection/transparent painting, stroke transactions, and
import corruption limits. UI captures check live previews and narrow sidebars.

The final focused check on 2026-09-26 passed **195 tests** covering the new
engine and controls plus hotkeys, settings, canvas input, color controls and
autosave. Whole-canvas and selected-area clearing during an active stroke are
covered, including undo/redo. The independently run importer checks are
recorded in the SUT investigation notes. The broader editor regression run
also encounters existing narrow-panel and text-capture failures reproduced
against the original commit; investigation details and logs are retained in
`.artifacts/brush-engine/regression-triage.md`.

The user's subsequent drawing feedback exposed latency that the original
engine-only timings missed. Profiling actual Qt tablet events and repainting
the full playground found full-tile compositing on every input packet and a
pending brush-preview stall. Publishing only changed pixels and deferring
previews during contact reduced median pen event processing from **3.70 to
1.03 ms**, and pencil from **5.90 to 1.14 ms** on the same local workload.
This measures input processing, not end-to-end hardware latency. Tests compare
partial updates against full-tile output for exact pixels, selection, erasing,
texture, dual brushes and undo snapshots. The original chain material's
registered repeat boundaries were also corrected; the new flat patterns keep
their canvas phase across overlapping strokes and tile boundaries.

Read-only inspection of the user's saved watercolor/wet-paint exercises
confirmed that strokes crossed paint on the correct raster. The original
mixing implementation either sampled only local underlying color or rapidly
overwrote its own pickup. A small per-brush pigment reservoir now loads color
from the pre-stroke raster and carries it along the path. The comparison in
`.artifacts/brush-engine/wet-pickup-after.png` shows the saved strokes alongside
a repeated patch-crossing test after the fix. This establishes working pigment
transport; the exact CSP mixing equations still require visual calibration.
The combined feedback-revision check passed **242 tests in 34.53 seconds**:
sampler, raster renderer, correction, flat patterns, partial compositing,
editor controls, importer, settings, shortcuts, input, palette and autosave.
Its log is `.artifacts/brush-engine/feedback-v2-tests.txt`.

The subsequent color and palette revision passed **279 tests in 36.22 seconds**
across that same scope plus signed-color raster tests. Its log is
`.artifacts/brush-engine/color-revision-tests.txt`. Real Leaves and Thin-chain
imports retain their entire portable definitions when the detailed settings
dialog opens and its tabs are changed. Color response distributions and the
numeric main/sub/both source enumeration still need controlled CSP comparison.

The screen-size, spray-direction and compositing revision passed **333 tests in
39.61 seconds** (`.artifacts/brush-engine/view-spray-blend-tests.txt`). Importing
embedded materials now bounds each read to the actual member size instead of
requesting the entire 128 MiB allowance; the latter caused allocation failures
in a combined run despite small source materials.

Size and opacity have sliders with editable numeric values and no stepper
buttons in the sidebar and main/secondary settings dialogs. Size uses a linear
0–200 px slider; larger sizes can still be typed. Edits snap to whole pixels
or whole opacity percentages, including rounded decimal text input. Merely
opening settings preserves an imported brush's original precision. Slider
drags update the brush and preview immediately, with the surrounding editor's
settings save/refresh deferred until release.

The downloaded `ぐりぐり水彩ぼかし` exposed a zero-fresh-paint case: both source
paint amount and density are zero. Picked-up pigment now retains its alpha
independently of the alpha contribution from fresh paint. This lets a pure
blender carry existing canvas paint without introducing the selected drawing
color. Shared previews seed a small blue paint patch for colorless blenders;
the test sheet provides blue/yellow patches and explains that these brushes
need existing paint. This fixes a no-op, not the remaining CSP mixing equation
calibration.

## Downloaded-brush validation sheet

`brush-playground-refined.bat` opens the next isolated sheet, rebuilt with
Automatic/Fixed Running-color blur import, the updated continuous-paint
scheduler, and matching sample/drawing sizes. Each sample states its size;
selecting its exercise opens at 100% zoom. Both paths change the nominal brush
size and explicitly linked secondary size together, while preserving the
source's independent texture and particle sizes. The previous
`brush-playground-custom.bat` still opens its original saved session.
Rebuilding a comparison sheet does not migrate or overwrite the user's
existing drawings or edited presets.

The SRU and Hazy3 feedback exposed a comparison-sheet inconsistency: the old
sample used a different nominal size from the drawing pad and also scaled
fixed texture dimensions. Identical settings now produce identical sample
and drawing pixels. This establishes consistency within Webtoon Maker, not
parity with CSP. SRU's automatic gap presets and Hazy3's material registration
still need controlled native comparison. No individual brush has a tuning
override; import fidelity must be corrected in shared import/rendering code.

The updated blur/continuous-paint revision passed **692 checks** after the
experimental arbitrary-angle ribbon change was withdrawn
(`.artifacts/brush-engine/refined-final-tests.txt`). Wool/knitting still show
repeat gaps with their original imported orientation; the importer reports
that limitation. No native CSP match is claimed by this validation.

`brush-playground-custom.bat` opens a separate saved sheet for **34 requested
downloaded brushes**: the screenshot families plus the five linked packs
(Hazy Days, wool/knitting, II2 Watercolor, Bibibi Soft Pen, and Flat texture).
The source materials and generated session stay under ignored `.artifacts`;
they are not redistributed with the editor. Existing playground drawings
remain in their own sessions. The exercise selector, drawing pad, color
patches for blenders, and live settings preview use the real raster engine.

II2 Watercolor 7 is held out: its recovered source enables a secondary image
tip but contains no secondary material list. Strict import reports this error
instead of inventing a tip. The two standalone wool texture images are
recorded as nonbrush pack components. Physical millimeter settings use a
300 DPI reference, retained with their original source values; interactive
imports let the user choose the conversion resolution.

Preview sizing accounts for particle coverage, enabled tilt maxima, and the
secondary brush. This prevents the 300%/800% particle sizes in two blood
brushes from clipping the entire sample. Imported definitions remain intact;
the exercise starts at a useful runtime size. Large PNGs are deduplicated in
preferences storage; see [storage and backup notes](brush-preferences-storage.md).

The combined downloaded-brush revision passed **614 checks** covering import,
rendering, dynamics, material memory, preview cancellation, UI and persistence,
plus existing shortcuts, canvas input, color ribbon and autosave integration
(`.artifacts/brush-engine/downloaded-collection-tests.txt`). The host's known
Qt TLS discovery diagnostic appeared during the run; all checks completed and
the process exited successfully. The latest isolated sidebar check also passes
with unrelated network discovery excluded. See [preview scheduling](brush-preview-scheduling.md)
for cancellation and remaining indivisible-operation limits.

## Remaining fidelity checks

**Still requiring CSP calibration:** exact hardness and anti-alias profiles,
spacing/gap enums, spray distributions, some blend/texture enum meanings and
equations, color mixing and perceptual brightness, watercolor edges, and
correction/taper behavior. Newer CSP formats/settings need more fixtures.
Unsupported source fields are retained; a successful import is not a parity
certificate. Source pressure compensation is applied before each imported
brush's pressure response; it does not change the editor's tablet preferences.
Fade uses an independent linear envelope, pending comparison with CSP.

**Known missing behavior:** CSP's alternate velocity calculation and dynamic
ribbon angle changes are retained with compatibility notices. Ribbon wet
mixing and unusual secondary-brush-only finishing effects also need work.
These combinations must not be treated as validated merely because their tip
images import. Other numerical approximations are listed in the renderer notes.

The requested direct computer-use stroke comparison is not complete. Native
screen capture failed with `SetIsBorderRequired: No such interface supported
(0x80004002)` after one recovery attempt. No blind canvas drawing was attempted.
Generated gallery images are Webtoon Maker output, not CSP output.

A fresh native-window selection and one recovery attempt on 2026-09-26
reproduced that capture error. Read-only version checks identified Windows
10 Pro build **19045** and CSP **1.12.0**. Microsoft documents
`GraphicsCaptureSession.IsBorderRequired` as introduced in build **20348**;
the unsupported capture property is therefore consistent with this host/tool
combination, rather than a brush-rendering failure. This is a supported
inference from the observed error and version requirements, not an inspection
of the capture helper's implementation. No blind native input was sent.
[Microsoft API requirements](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscapturesession.isborderrequired)

Reference-layer anti-overflow and vector operations are out of scope. The user
also explicitly approved deferring ruler snapping and erase-on-all-layers.

## Engineering references

- [CSP feature inventory and comparison ladder](csp-brush-requirements.md)
- [SUT format, verified fields, and decoding evidence](csp-sut-investigation.md)
- [Renderer behavior and numerical limitations](csp-raster-renderer-notes.md)
- [Editor integration and future-vector boundaries](brush-architecture-plan.md)
- `tests/benchmark_brush_raster.py` measures engine event/release time and
  working memory; it is not an end-to-end stylus latency measurement.

The source UI preview template is documented in `comic_editor/core/brush_preview.py`.
CSP's private template input sequence has not been recovered.
