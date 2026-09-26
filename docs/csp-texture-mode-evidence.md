# CSP texture modes: source audit and corrected import order

The additional Hazy brushes exposed a pre-existing texture-mode mapping problem. It is not a new CSP 5.x enum or a second texture-mode field. All 19 additional-request schemas and the installed settings snapshot contain `TextureCompositeMode` and `DualTextureCompositeMode`; none contains `TextureCompositeMode2`.

## Primary evidence

The official legacy texture guide lists five modes in this order: Normal, Multiply, Subtract, Compare, Outline. CSP 1.10.5 added Overlay, Color dodge, Color burn, Hard mix and Height. The current web manual omits Color burn from its prose list, although the release notes explicitly include it. [Official current texture guide](https://help.clip-studio.com/en-us/manual_en/810_subtools/T.htm), [official release history, 1.10.5](https://www.clipstudio.net/en/dl/release_note/)

The installed program provides independent primary evidence. Its English resource at `C:\Program Files\CELSYS\CLIP STUDIO 1.5\CLIP STUDIO PAINT\resource\english\E79C2AC5-BC3F-4838-9E87-F49B629F84B5` contains the full ten-label sequence under the Texture mode group. Resource path `1 / 63 / 1`, consecutive string IDs **25–34**, stores the labels in exactly the order below. Resource SHA-256: `91ad749430c0102bea08d48fbad14d1fc7630a2a3365c4ffef1fe1776e332a2e`. Only a small label/provenance extraction is saved in `.artifacts/brush-investigation/installed/texture-mode-resource-evidence.json`; the source program file was read without modification.

| Source code | Portable mode | Prior import |
| --- | --- | --- |
| 0 | Normal | Subtract |
| 1 | Multiply | Multiply |
| 2 | Subtract | Compare |
| 3 | Compare | Overlay |
| 4 | Outline | Height |
| 5 | Overlay | Outline |
| 6 | Color dodge | Multiply fallback |
| 7 | Color burn | Multiply fallback |
| 8 | Hard mix | Multiply fallback |
| 9 | Height | Multiply fallback |

The **label order is directly observed**. Interpreting source codes 0–9 as that order remains a corroborated inference, not a one-setting native export or a measured stroke comparison. Raw source codes are retained and the importer states this limitation. It would be incorrect to describe the numeric enum or rendering equations as fully calibrated.

The higher codes predate the new downloads. In the installed snapshot, Watery ink (Variant 749) and Thick oil paint (771) use code 7, while Watercolor splash (627) uses code 9. New Hazy 1/2/3/5 use 7; Hazy 4 uses 2. II2 Watercolor 9 uses 9. II2 Watercolor 8 retains 8 with no active texture image, so it must not generate an active-mode warning or imply an applied texture.

## Implementation and limits

`sut_import.py` now uses the complete menu-order crosswalk for both primary and secondary textures. `brush_raster.py` adds a bounded density-space Color burn response, rather than silently using Multiply. The settings menu exposes all ten modes. Unknown numeric codes continue to use an explicit fallback warning and retain the original value.

These changes do not establish CSP's proprietary texture formulas. In particular, Normal currently shares the Multiply implementation and explicitly warns about the missing density-preserving response. Brightness, contrast, density emphasis, Outline, Height, texture interactions with wet paint and anti-aliasing still require native comparison. Existing per-tip/per-stroke, transform and texture-density behavior is preserved. The new tests cover code/label preservation, primary and secondary textures, serialization, unknown-code reporting, Color burn limits and density modulation, and deterministic rendering across tile boundaries and pointer packet sizes.

## Negative contrast must not invert the material

Hazy 3 retains `TextureContrast=-55`, brightness 30, and the explicit Invert
flag. The previous general contrast equation used gain `1 + 3 × contrast`,
which became negative below -33.33 and reversed luminance before Invert was
applied. Negative contrast now uses gain `1 + contrast`: lower values reduce
differences monotonically, reaching a flat midtone at -100. Nonnegative
contrast keeps its previous equation exactly. The source brush fields, texture
pixels and scale are unchanged; there is no Hazy-specific adjustment.

This repairs a general ordering error, not the remaining native calibration.
The [texture manual](https://help.clip-studio.com/en-us/manual_en/810_subtools/T.htm)
separates Contrast and Invert; CSP's
[tonal correction description](https://help.clip-studio.com/en-us/manual_en/390_filters/Tonal_Correction_Effects.htm)
defines lower contrast as weaker light/dark differences. That separate tool
does not prove that brush texture uses an identical transfer curve. The new
negative branch is an independent approximation pending controlled CSP
measurements. Tests check monotonicity, the flat endpoint, brightness and
inversion composition, unchanged nonnegative results, and Hazy 3 source-field
preservation. This does not establish native texture scale or registration
geometry: the playground consistency fix compares Webtoon Maker with itself.

## Color mixing on dual brushes and `BrushUseWaterColor2`

Hazy 1 stores `BrushUseWaterColor=1`, `BrushUseWaterColor2=1`, `BrushWaterColor=0`, and `UseDualBrush=1`. The source's single-brush mode value must not make this an effective Blend brush. CSP explicitly switches enabled color mixing to Smear when a dual brush is active. The installed Wet wash (Variant 773) likewise retains mode 0 even though the official guide identifies its updated behavior as Smear. [Official dual-mixing support answer](https://support.clip-studio.com/en-us/faq/articles/20210103), [official Smear guide](https://tips.clip-studio.com/en-us/articles/5515)

`BrushUseWaterColor2` is not evidence for a second independently wet brush. All populated local rows inspected have the same enabled value in both flags; some older rows omit the second flag. No mismatching pair was observed, so precedence between contradictory flags remains unknown. Preserve both raw values, keep the existing first-flag interpretation until independent evidence exists, and apply the documented dual-brush Smear constraint after resolving whether mixing is enabled. The importer now applies that effective-mode constraint.
