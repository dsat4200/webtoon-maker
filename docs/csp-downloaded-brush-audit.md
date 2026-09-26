# Downloaded dynamic-brush audit

This audit uses the read-only installed CSP snapshot and reconstructed research fixtures, not native UI strokes. It separates code failures from uncalibrated CSP math. No user artwork was changed. Source snapshot: `.artifacts/brush-investigation/installed/EditImageTool.latest-readonly-snapshot.todb`; active Variant rows are joined through `NodeVariantID`, with non-null values overriding the Manager common variant.

## Brush identity and active settings

| Requested family | Local evidence | Behaviors that matter |
| --- | --- | --- |
| Sketching / Pencil Brush | Active/reset Variant 850/851; image material `Layer 1 Copy 2 2`, texture `Dirt 10` | Pressure + tilt size; pressure opacity; vertical thickness 65%; pen-tilt direction; fixed gap 2.6%; size 17 px. |
| HIMOGザラ強弱 | Active/reset 860/861. The catalog spelling is ザラ, not サラ. | Size combines pressure, speed and random input; pressure minimum 8%, speed minimum 45%, random minimum 68%; automatic Normal spacing; angle 45°, thickness 75%. |
| かすれ油彩■リメイク | Active/reset 856/857; parallelogram tip and noise-canvas texture | Blend with pressure-controlled density, paint amount, paint density and per-dab texture density. Paint amount 82%, paint density 84%; automatic Normal spacing. |
| ぐりぐり水彩ぼかし | Active/reset 858/859; three irregular tips and noise texture | Running color with paint amount **0** and paint density **0**; pressure/speed/random size; random angle; blur 200 and size-link flag 1; per-dab texture. |
| leaf 2 葉ブラシ | Recovered registered tool name `ghibli leaves 2` | Non-spray material brush; size responds to tilt with minimum 100% / candidate maximum 300%; pen-tilt angle plus random angle. |
| Downloaded blood family | Ten registered emoji-named tools from asset 1956272, recovered under `blood-01` through `blood-10` | Multiple pressure curves, dual brushes, random gaps, ribbons, spray and signed spray bias. These are distinct from the installed Bloodstain tool. |
| SRU | Registered `SRUカリカリ線画ペン`; portrait thumbnail matches the supplied screenshot | Dual brush; primary pressure size uses a newer dynamics header. The initial screenshot transcription was incorrect; the catalog and thumbnail establish its identity. |

Stored settings on disabled groups are not evidence of active behavior. In particular, spray fields occur on these non-spray pencil/oil/watercolor variants, and HIMOG retains texture settings without an active texture reference.

## Confirmed failure repaired: zero-density wet pickup

The requested watercolor is a pure blender: both fresh-paint controls are zero. Before the fix, `_wet_pixels` multiplied the whole transported load by paint density. Consequently, no stroke could change any pixel, even after reading opaque canvas pigment. This follows directly from the stored settings and was reproduced with a colored-patch path.

The wet stage now applies paint density only to the fresh alpha contribution. With picked-up alpha `a`, paint amount `m`, and paint density `d`, the independent approximation is `a + m*d*(1-a)`. This preserves the previous full-density behavior while retaining canvas pigment at zero density. Transparent tip coverage, zero stroke opacity, zero dab density and explicit erasure keep their existing meanings. The color mixing ratio remains independent of the alpha contribution.

The official guide demonstrates mixing existing pigment with paint density zero; that setting cannot mean “discard all picked-up pigment.” Its numerical equations are not published. Our formula remains an approximation, especially for stroke initialization on an empty canvas. [CELSYS drawing-tool guide](https://tips.clip-studio.com/en-us/articles/563)

QA artifacts in `.artifacts/brush-engine/`:

- `zero-density-wet-comparison.png`: identical path, source patches and seed before/after; synthetic Running brush with both fresh-paint controls zero.
- `zero-density-wet-metrics.json`: previously 0 changed pixels; afterward 9,368. A blue tail over empty canvas is RGBA `(0, .251, 1, 1)`, followed by mixed pigment after crossing yellow, `(.686, .627, .314, 1)`.
- `zero-density-wet-before.npz`: untouched baseline and original failure result, retained for comparison.

`tests/test_brush_wet_transport.py` covers zero-load pickup, transport across empty regions, pickup of a second color, partial-alpha transport, pressure-controlled paint density, preserved undo originals, transparent/no-coverage inputs, explicit erasure, packet invariance and working-cache spilling. Existing wet/color tests continue to pass.

The actual reconstructed `guriguri-watercolor-blur.current.reconstructed.sut` was then checked without changing its imported settings: size 150, its original three tip images, pressure `.8`, and seed 42. It now changes 45,627 pixels; 12,042 pixels in the empty gap receive transported pigment. Blue-tail RGBA is `(0, .255, 1, .431)` and yellow-tail RGBA is `(.984, .792, .016, .565)`. `downloaded-watercolor-blender-comparison.png` was visually inspected, and `downloaded-watercolor-blender-metrics.json` records the fixture hash and measurements. The before panel is the unchanged canvas predicted by the previous zero-alpha result. This confirms the functional fix in Webtoon Maker, not native CSP fidelity.

## Highest-priority remaining checks

### 1. Repaired: tilt maximum was discarded on the sketch pencil and foliage

`BrushSizeEffector` for Variant 850 has active mask 48 (pressure + tilt), tilt minimum 4, and final header value **257** at byte offset 40. At the time of the audit, `_dynamics` ignored that final value. Other code already treated this slot as tilt maximum for color-channel warnings; non-color channels did not report the loss. `BrushDynamics` had no maximum field and `_factor` clamped physical responses to 1, so a larger tilt scale could not survive even if imported.

Reproduction using the decoded size dynamics and size 17, at pressure 1: tilt magnitudes 0/15/30/60/90° yield sizes `.68 / 7.161 / 12.471 / 14.736 / 17`. There is no way to express the source's candidate 257% endpoint. CSP exposes minimum/maximum dynamics controls, but the raw slot's exact scale still needs a one-setting export. [Official brush customization](https://help.clip-studio.com/en-us/manual_en/240_brushes/Customizing_brush_tools.htm)

Recommended check: add an explicit tilt maximum to the portable dynamics model, preserve it in every channel, and separately cap outputs by each physical parameter's range. Verify source values 100 and 257 with full pressure and varying tilt. Do not globally allow negative size or unbounded alpha.

The exact recovered leaf2 makes the loss clearer: its size dynamics enable **tilt only**, with minimum 100 and candidate maximum 300 plus a descending curve. The current calculation `minimum + (1-minimum)*curve` collapses to 1 at every tilt, so a nominal 500 px leaf stays 500 px at 0/30/60/90°. Its defining size dynamics are therefore completely lost. This is a confirmed ignored-control failure even though the precise native interpolation still needs calibration.

This finding was subsequently fixed: `tilt_maximum` is retained and applied to the tilt response for physical parameters, with the larger factor allowed only while tilt is enabled. The leaf curve now runs from 3× upright to 1× at 90°; the Sketch curve increases with tilt. UI/importer/sampler tests cover the source records. Exact native transfer functions and combined-input behavior still need CSP comparison; the measurements above describe the pre-fix audit.

### 2. Repaired: newer SRU dynamics records were rejected

The registered SRU primary size effector has a **48-byte** header, active pressure flag 16, and total length 108. Most other dormant records have a 48-byte header and total length 48. At the time of the audit, the importer accepted only a header marker of 44, reported unsupported encoding, and imported `dynamics={}` for this brush. Its primary pressure response was absent. The decoder needed the extra header field, curve offsets and lengths checked before extending support, while preserving strict bounds validation.

This finding was subsequently fixed: both 44-byte and 48-byte headers are supported with strict bounds validation. The extra word is the velocity-graph byte length; pressure, tilt and velocity curves are now retained independently, including SRU's exact three-knot pressure curve and the new Bibibi/Hazy response records. Unknown extensions still warn. See [dynamics format evidence](sut-dynamics-formats.md). The paragraph above describes the pre-fix audit.

### 3. Repaired: active random gap inputs were ignored by the scheduler

Blood02's primary and secondary interval effectors enable only Random with minima 63% and 27%. Blood08's primary interval enables pressure + Random, with random minimum 6%. Before the fix, `_spacing` always called `_factor('spacing', randomized=False)`, so the imported random effect could not change any gap. Using Blood02's size 700 / gap 300% and decoded interval effector gave positions `0, 2100, 4200, 6300, 8400` for seeds 1 and 2 alike.

The scheduler now chooses randomness once when scheduling the next distance, retains that choice across pointer packets, and keeps deterministic spacing queries used for density normalization separate from random-state mutation. A separate seeded stream prevents particle count, tip/color choices, and density queries from changing interval randomness. `tests/test_brush_spacing_random.py` includes the exact relevant Blood02/Blood08 source effector records, gap bounds and variation, disabled-setting preservation, pressure-profile phase, primary/secondary seeds, taper/Fade, and coarse/fine packet equivalence. Random distribution and its interaction with native density-by-gap remain uncalibrated; those three source components have density-by-gap disabled.

### 4. Repaired: signed spray bias was clipped

Blood08 has an active secondary spray with `DualSprayBias=-100`. The old importer and portable model clamped this to zero. The sign is now retained, with an explicit edge-weighted branch that keeps every particle inside the brush footprint. Positive deviation preserves the previous center-weighted equation. The native radial distribution still needs calibration.

### 5. Repaired: dormant ribbon flags when spraying is active

Blood06 stores both `BrushRibbon=1` and `BrushUseSpray=1`. The importer now disables the effective ribbon option when spraying or a round tip makes it unavailable, while preserving both raw source fields and reporting the inactive flag. Native comparison is still needed; this follows the documented setting availability. [Official Stroke settings](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm)

### 6. Repaired: Running-color blur mode and source units

Variant 858 stores `BrushBlurLinkSize=1`, `BrushBlur=200`, `BrushBlurUnit=0`. The previous importer collapsed the scalar into a normalized blur strength and discarded the active mode. Importer version 8 now retains Automatic/Fixed plus a separate fixed width in pixels, with explicit millimeter/DPI conversion when needed. Automatic preserves the dormant fixed scalar; selecting Fixed uses its distance independently of brush size. Existing portable presets keep their old normalized-strength behavior.

CSP distinguishes a size-linked Automatic blur from an explicit Fixed value width. The implemented distinction now has unit, pressure-response, actual color-pickup, compatibility and UI tests. Automatic still uses the existing capped size-linked approximation, and both modes use a five-sample pickup kernel. Native comparisons over a sharp two-color boundary at multiple sizes remain necessary to calibrate the radius and kernel. [Official Ink settings](https://help.clip-studio.com/en-us/manual_en/810_subtools/I.htm)

### 7. Automatic spacing is still a coarse table

The downloaded oil and HIMOG use `BrushAutoIntervalType=2`. The importer replaces it with fixed relative spacing `.25`; it does not account for tip shape or coverage. The source's stored `BrushInterval` belongs to the inactive Fixed mode and should not simply replace this approximation. Check coverage and repeated-pattern seams at several sizes/pressures, especially the irregular oil tip. This is already disclosed by import warnings, not a newly confirmed native mismatch.

### 8. Speed response needs consistent input timing

HIMOG, the watercolor and default Bloodstain all enable speed dynamics. The current sampler uses the last packet's length/time and the channel's velocity scale (2,000 px/s by default). Stabilization modifies geometry before that speed is calculated. Exact CSP velocity scaling and combined-input equations are unknown; merely seeing pressure/random/velocity fields imported does not establish a match. Compare equal physical paths and timestamps at multiple packet rates, then compare slow/fast native strokes at fixed pressure.

### 9. Continuous buildup while moving — scheduler gap corrected

Before the fix, a synthetic size-40 brush with spacing `.5`, continuous rate 60, and a 200 px straight movement emitted 11 dabs at each tested duration (`.1`, `1`, and `4` seconds). The moving branch never scheduled elapsed-time dabs. CSP documents slower continuous strokes becoming denser. The four requested active pencil/oil/watercolor rows have Continuous spraying disabled; this gap affected other wet brushes such as installed Thick oil paint. [Official Stroke settings](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm)

Follow-up: the scheduler now adds time events during moving and stationary segments, preserves independent distance/random-gap phase, and consistently disables timed paint when primary Post correction is active. Tests cover slower raster buildup, input subdivision, secondary-only timers, final replay and unchanged noncontinuous dabs. The exact CSP cadence remains uncalibrated; long delayed packets catch up only the latest second. See [continuous scheduling and limitations](brush-continuous-scheduling.md).

## Checks already improved, still needing native comparison

Particle orientation now has independent angle, line/whole-spray/center direction and additive random variation. Ink and dual compositing use separate mappings; the prior incorrect Multiply/Add and Height/Soft-light mappings were corrected. Signed color changes, material alpha/color modes, per-dab texture and stored pressure curves are exercised by tests. These improvements do not establish native equations, texture-mode ordinals, stochastic distributions or exact brush-size normalization. Tests should use the actual requested material set and preserve the original settings before adjusting a comparison size.

The additional Hazy pack exposed a dormant-setting interaction: Hazy 1/2/3/4 and II2 Watercolor 8 retain an edge blur of 15 px while Process after brush stroke is off. The renderer previously applied that blur and its broad repaint halo anyway. Both now use zero effective blur during live edge processing, retaining the saved width for post-stroke use. A dormant millimeter blur alone no longer requests import DPI. Small renderer/import/UI regressions verify these behaviors. [Official watercolor-edge availability](https://help.clip-studio.com/en-us/manual_en/810_subtools/W.htm)

Blood01 originally exceeded the 64-Mi-pixel aggregate import budget. The importer now retains only one decoded original at a time, reuses validated PNGs for repeated references, and permits 256 Mi pixels of unique source material while retaining the 32-Mi-pixel per-image limit. Its 15 originals (265,686,571 pixels) import in roughly 2.5 seconds with a measured process peak around 200 MB and unchanged dimensions. The renderer also keeps compact byte pixels and expands sampled regions only. Its fitted preview fell from 15.37 seconds / 916 MB peak to 1.72 seconds / 403 MB peak; exact pixel-equivalence tests cover the sampler and complete strokes.
