# CSP physical brush lengths

This is a read-only source audit of the installed settings snapshot and the reconstructed additional-request fixtures. It does not establish native stroke parity. The source enum is not documented publicly: **0 = pixels, 2 = millimeters** is a strongly corroborated mapping from local saved canvas settings (pixel dimensions at 0, A4 dimensions 210 × 297 at 2), combined with the official preference description. CSP allows brush and dialog length values in pixels or millimeters. [Official Preferences, Ruler/Unit](https://help.clip-studio.com/en-us/manual_en/720_preferences/Preferences.htm)

Canvas resolution is a separate input. No DPI or document-resolution field was found in the relevant `Manager`, `Node`, or `Variant` tables of either the installed snapshot or the new pen fixture. Tip-image resolution is not the destination canvas DPI. CSP exposes canvas resolution when creating a document. [Official canvas settings](https://help.clip-studio.com/en-us/manual_en/210_file/Creating_a_New_Canvas.htm)

## Fields and conversion scope

These are the nine brush-context unit columns present in both inspected schemas. Each length has its own unit; the pen's main size unit must not be applied indiscriminately to other settings.

| Source value / unit | Effective interpretation |
| --- | --- |
| `BrushSize` / `BrushSizeUnit` | Nominal brush diameter. Convert independently of screen-size locking. |
| `DualSize` / `DualSizeUnit` | Secondary nominal diameter. Use the same chosen import DPI, independently of the main brush's unit. |
| `BrushSpraySize` / `BrushSpraySizeUnit` | Convert only when `BrushSpraySizeSyncBrushSize` is false. When true, the stored number is a percentage of brush size. |
| `DualSpraySize` / `DualSpraySizeUnit` | Same rule using `DualSpraySizeSyncBrushSize`. |
| `BrushInLength` / `BrushInLengthUnit` | Length-mode starting range. Percentage mode uses `BrushInRatio` instead. Starting is inactive for Fade. |
| `BrushOutLength` / `BrushOutLengthUnit` | Length/Fade ending range in the current mapping. Percentage mode uses `BrushOutRatio` instead. The native Fade mapping remains provisional. |
| `BrushWaterEdgeRadius` / `BrushWaterEdgeRadiusUnit` | Watercolor edge width. |
| `BrushWaterEdgeBlur` / `BrushWaterEdgeBlurUnit` | Edge blurring width; active when processing after the stroke. |
| `BrushBlur` / `BrushBlurUnit` | Fixed Running-color blur width, converted into `blur_width` pixels independently. `BrushBlurLinkSize=1` selects Automatic instead; the fixed width is preserved but dormant and does not trigger a DPI prompt. |

The installed Ground brush retains `BrushSpraySizeUnit=2` with `BrushSpraySize=50` and `BrushSpraySizeSyncBrushSize=1`. This is concrete evidence that the ratio switch takes precedence over a stored unit code. Relative spacing, texture scale, dynamics curves and percentage taper values have no physical-length conversion. No `BrushIntervalUnit` or dual taper unit columns exist in these inspected schemas.

The official manual distinguishes particle size linked to brush size, length/percentage/Fade taper modes, watercolor edge width and post-stroke edge blur, and automatic/fixed Running-color blur. These distinctions explain why a global multiplier would be incorrect. [Spray and taper settings](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm), [Watercolor edge](https://help.clip-studio.com/en-us/manual_en/810_subtools/W.htm), [Ink blur](https://help.clip-studio.com/en-us/manual_en/810_subtools/I.htm)

## New pen: びびび柔ペン

Fixture: `.artifacts/brush-investigation/installed/additional-request-reconstructed/2306072-01.registered.reconstructed.sut`. Its registered-source SHA-256 is `4f92b7296d06ec57edefc9dc39f7f4dcac27ae922901d7c12f600634aea69f0e`. The fixture is reconstructed research data, not a native CSP export.

| Active field | Stored value and unit | Resolved at an assumed 300 DPI |
| --- | --- | --- |
| Size | 1.5 mm | 17.716535433 px |
| Watercolor edge | 0.1 mm | 1.181102362 px |
| Edge blur after stroke | 0.1 mm | 1.181102362 px |
| Starting length | 35 px | 35 px |
| Ending length | 42 px | 42 px |

It has no active secondary brush or spraying. Its stored Running-color blur value is dormant for this pen. Among the 19 additional-request brush fixtures, this pen is the only one with any nonzero brush-context length-unit field.

## Minimal import contract

Use an explicit import DPI input and resolve `pixels = millimeters × dpi / 25.4`. Unit 0 remains unchanged. An unknown code should remain visible as an import limitation; do not silently interpret it as millimeters. A missing unit can retain the existing pixel fallback, without claiming it is a verified native default.

Preserve the original source variant exactly. Record the DPI and each conversion's original value, unit code and resulting pixels in source provenance. If 300 DPI is chosen without a known CSP destination document, label it as an assumption. Future re-import should recompute from the original lengths, not multiply already converted values, and should use the same DPI for the primary and secondary definitions. This supplies the new pen's required brush context without introducing document-wide print settings or changing existing artwork.

The code paths that already enforce numeric limits must continue to do so after conversion. If a converted length exceeds a supported limit, report the restriction rather than allowing a different value only after save/reload. Exact physical-unit stroke appearance still requires comparison against CSP at the same document DPI and zoom.

Implemented in `core/brush_units.py` and `import_sut(..., dpi=300)`: explicit field pairs, independent secondary units, ratio/percentage bypass, active unknown-unit warnings, and `source.length_units` provenance. Interactive import asks for DPI when active millimeter lengths are present. Re-import resolves from original values; it does not repeatedly scale the current brush. The original targeted import/UI/model test group passed 144 tests.

## Running-color blur mode

The current importer preserves Automatic and Fixed separately. The observed `BrushBlurLinkSize` boolean is interpreted as 1=Automatic and 0=Fixed, consistent with its name and the two modes in the official manual; this flag interpretation is not a controlled native export. The inspected snapshot contains pixel and millimeter widths with either flag value. Variant 858, the downloaded watercolor blender, retains `BrushBlur=200`, `BrushBlurLinkSize=1`, and `BrushBlurUnit=0`.

Fixed mode resolves the saved scalar through the same explicit physical-unit conversion as other lengths. Automatic imports use a size-linked approximation and retain the fixed scalar only for switching modes. An Automatic brush with a stored millimeter width does not request DPI solely for that dormant value. Blur is active only with Running color; Blend, Smear, disabled mixing and the primary dual-brush Smear override retain the width without applying it. [Official Ink settings](https://help.clip-studio.com/en-us/manual_en/810_subtools/I.htm)

Portable presets now store `blur_mode` and `blur_width`. The older normalized `blur` value remains compatible: existing presets without the new fields keep their previous automatic-strength behavior. Choosing Automatic explicitly in the settings interface activates the normal size-linked approximation. Importer version 8 records this change. `tests/test_brush_running_blur.py` checks independent pixel/mm widths, dormant fields, raw/portable round trips, missing-mode diagnostics, actual color pickup and the local watercolor fixture; the combined blur/units/wet group passed 66 tests.

This fixes the loss of mode and units, not the remaining kernel calibration. Fixed width currently supplies the distance to four neighboring pickup samples; Automatic retains `strength × min(20, brush_size × 0.2)`. Both apply the blur response multiplier. CSP does not publish its corresponding numerical radius or sampling kernel, and import warnings retain that limitation.
