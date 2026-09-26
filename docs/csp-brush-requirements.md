# CSP raster brush compatibility requirements

Research date: 2026-09-26. This is a requirements inventory, not a claim that the engine implements or matches every item. The fidelity target must record the installed CSP version as well as the exported brush: behavior has changed between versions.

The requested scope is a new **Brush** tool painting raster layers, with `.sut` import and enough fidelity for pens, pencils, paint, airbrushes, chains, foliage, splatter, and other material brushes. Vector layers are deferred. Reference-layer dependencies may be excluded. All other omissions require an explicit decision rather than silently turning an imported brush into a round stamp.

## Sources and confidence

The user's [drawing overview](https://help.clip-studio.com/en-us/manual_en/240_brushes/Drawing_and_painting.htm#1005858) establishes the intended families: Pen, Sketch, Watercolor/Thick paint, Airbrush, and Decoration. The [customization manual](https://help.clip-studio.com/en-us/manual_en/240_brushes/Customizing_brush_tools.htm) links their detailed settings and describes pressure, tilt, velocity, random dynamics, minimum/maximum effects, and editable pressure graphs.

The matrix below records documented behavior. Rendering formulas, interpolation details, random distributions, and file encodings remain empirical questions unless a separate investigation establishes them. Similar-looking sample output alone does not prove a parameter was imported correctly.

The three supplied videos were requested through the web reader:

| Video | What could be verified | Limitation |
| --- | --- | --- |
| [Eax-Xx39xYQ](https://www.youtube.com/watch?v=Eax-Xx39xYQ) | URL supplied by user | Fetch failed; no contents reviewed. |
| [P-7AI5xTXTg](https://youtu.be/P-7AI5xTXTg) | Page title: *Guide to Clip Studio Paint Brush Settings* | YouTube page shell only; no transcript or frames reviewed. |
| [O8dtV4O5wh4](https://youtu.be/O8dtV4O5wh4) | Page title: *The ULTIMATE GUIDE to CLIP STUDIO PAINT Brushes* | YouTube page shell only; no transcript or frames reviewed. |

No feature claims below are attributed to unseen video content.

A follow-up attempt in the user's Brave browser on 2026-09-26 reached a tab
for `P-7AI5xTXTg`, but page inspection timed out. The browser's dedicated
YouTube transcript export also timed out and reset its session. No transcript
file or video frames were returned; those attempts do not strengthen the
content evidence above.

## Requirements matrix, from simple to complex

All rows are in scope unless explicitly marked deferred. The comparison column is an engineering proposal, not a statement from CELSYS.

| Stage | Feature family | Documented requirements | Proposed comparison |
| --- | --- | --- | --- |
| 1 | Input dynamics | Pressure, tilt, velocity, and random sources; limits and pressure curve per supported parameter. [Customization](https://help.clip-studio.com/en-us/manual_en/240_brushes/Customizing_brush_tools.htm) | Constant input, rising/falling pressure ramps, slow/fast paths, tilt sweep. |
| 1 | Round pens and soft tips | Circle/material tips; circle hardness; size dynamics; screen-relative size; one-pixel minimum. Tip thickness has a horizontal/vertical axis. Angle may follow stylus orientation, barrel rotation, line direction, or random variation. [Brush size/tip](https://help.clip-studio.com/en-us/manual_en/810_subtools/B.htm) | Dot, straight stroke, diagonal, circle; multiple sizes and hardness values. |
| 1 | Sampling quality | None/Weak/Middle/Strong anti-aliasing; separate automatic/quality/speed preference for large brushes. [Anti-aliasing](https://help.clip-studio.com/en-us/manual_en/810_subtools/A.htm) | Subpixel diagonals, tiny dots, very large circles, export at 100%. |
| 2 | Accumulation | Brush density controls each tip; density can compensate for reduced spacing. Keep this separate from Ink opacity. [Brush tip](https://help.clip-studio.com/en-us/manual_en/810_subtools/B.htm), [Ink](https://help.clip-studio.com/en-us/manual_en/810_subtools/I.htm) | Same-stroke self-crossing versus a second stroke; repeated stationary dots. |
| 2 | Stroke placement | Fixed or adaptive interval, interval dynamics, continuous spraying during movement and stationary holds, velocity-input compatibility option, darken tip overlap. Continuous spraying conflicts with post correction. [Stroke](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm) | Equal path at varied event rates/speeds; stationary hold; no end gap. |
| 3 | Material tips | Multiple ordered images; flips can be fixed, random, or follow reversed repetition. Preserve per-tip dimensions, alpha, and registration padding. [Brush tip](https://help.clip-studio.com/en-us/manual_en/810_subtools/B.htm), [registration geometry](https://tips.clip-studio.com/en-us/articles/535) | Non-square asymmetric stamps, padded tips, tiny/large tip pair. |
| 3 | Tip color semantics | Gray/Monochrome tips map black to main color and white to sub color. Color tips preserve their authored colors. Choosing the sub color paints with sub color only; transparent painting erases the black portion. Monochrome source tips can gain anti-aliasing when transformed on a richer raster layer. [Official decoration guide](https://tips.clip-studio.com/en-us/articles/679) | Black, white, gray, RGB and transparent test pixels; swap main/sub colors. |
| 3 | Tip sequence | Repeat, Reverse, Do not repeat (hold final), Random, One time only, One random cycle. [Stroke](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm) | A/B/C-labeled tips over a long stroke and a new stroke. |
| 4 | Pencil/charcoal texture | Texture asset; density dynamics; invert; density emphasis; scale/rotation; brightness/contrast. Texture modes: Normal, Multiply, Subtract, Compare, Outline, Overlay, Color dodge, Hard mix, Height. Apply per plotted tip or per stroke; overlapping tips behave differently between these modes. [Texture](https://help.clip-studio.com/en-us/manual_en/810_subtools/T.htm) | Checker/grain texture, opacity overlaps, canvas rotation/zoom, repeated passes. |
| 5 | Spray and foliage | Particle size independent of brush footprint, optional size linking, particle-density dynamics, center deviation, particle direction distinct from tip angle. Orientation can follow line, whole spray, center, or randomness. [Spraying](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm) | Sparse scatter, dense spray, center-directed asymmetric particles, stationary hold. |
| 5 | Color variation | Tip-level and stroke-level hue/saturation/brightness variation, sub-color mixing, dynamics, main/sub/both target selection. [Color Jitter](https://help.clip-studio.com/en-us/manual_en/810_subtools/C.htm) | One continuous stroke plus repeated strokes; distinct main/sub colors. |
| 6 | Ribbons/chains | Ribbon deforms the pattern along the path and connects its top/bottom; multiple tips retain sequencing. It is unavailable with a circular tip or spray. [Stroke](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm), [official ribbon guide](https://tips.clip-studio.com/en-us/articles/681) | Straight, S, tight curve, reversal, pressure taper, alternating connected tips. |
| 6 | Rigid chains/stamps | Rigid beads can be arranged by spacing instead of ribbon deformation; unequal registered image sizes affect alignment. [Official stamp guide](https://tips.clip-studio.com/en-us/articles/680) | Beaded necklace on an arc: preserve individual shapes while matching gaps. |
| 7 | Wet color mixing | Same-layer Blend, Running color, and Smear; RGB paint amount, transparency/paint density, color stretch, and Running-color blur. Standard/Perceptual mixing and five brightness-correction levels. Newer Running-color quality differs from version 4.2 and earlier. Blend/Running restrict opacity dynamics and blending-mode choices. [Ink](https://help.clip-studio.com/en-us/manual_en/810_subtools/I.htm) | Brush from transparency through blue/yellow patches; reverse order; repeated passes. |
| 7 | Watercolor edges | Edge width, opacity, darkness; optional processing at stroke end with blur width. [Watercolor edge](https://help.clip-studio.com/en-us/manual_en/810_subtools/W.htm) | Isolated stroke, self-crossing stroke, touching strokes, soft/textured boundaries. |
| 7 | New watercolor style | Version 5.1 adds Standard/Vivid, Strength, and Vividness. Vivid changes edge color and follows drawing opacity. The official article excludes watercolor edges with dual brushes. [Version 5.1 guide](https://tips.clip-studio.com/en-us/articles/16882) | Baseline depends on installed version; never map unknown Vivid settings silently to Standard. |
| 8 | Dual brush | Secondary brush has independent tip, spray, stroke, texture, size/dynamics and anti-aliasing, optional main-size link, and RGB-versus-alpha composition. Modes include Normal, Multiply, Add (Glow), Subtract, Darken, Lighten, Screen, Overlay, Color Dodge/Burn, Linear burn, Hard Mix, Height (Linear). [Dual brush](https://help.clip-studio.com/en-us/manual_en/810_subtools/Number.htm) | Primary round + sparse secondary texture; unrelated spacing; colored secondary tip. |
| 9 | Correction | Sharp angles, stabilization, speed adjustment, post correction with speed/scale options and curve method, taper continuation after pressure release. [Correction](https://help.clip-studio.com/en-us/manual_en/810_subtools/C.htm) | Small jitter, sharp corner, quick S, pen-up tail; compare latency and final path. |
| 9 | Start/end effects | Multiple affected parameters with minima; absolute-length, percentage, or fade specification; independent start/end and speed-sensitive effect. [Starting/ending](https://help.clip-studio.com/en-us/manual_en/810_subtools/S.htm) | Short and long strokes with same brush; abrupt pen-up; fade beyond its endpoint. |
| 10 | Raster erase | A drawing tool may erase through its blending configuration, retaining its brush behavior. [Official tool basics](https://tips.clip-studio.com/en-us/articles/535) | Erase textured and RGB content with round/material/wet brushes; verify alpha. |

## Scope boundaries and decisions

The user already excluded vector-layer support. Keep imported vector-only fields as metadata where practical, but do not implement vector editing, vector erasing, vector magnets, or vector-path anti-overflow. Reference-layer anti-overflow is also excluded under the user's permission: CSP documents it as depending on designated reference lines. [Anti-overflow](https://help.clip-studio.com/en-us/manual_en/810_subtools/A.htm)

The user explicitly approved leaving ruler snapping and erase-on-all-layers
out of this brush work. Other boundaries below remain subject to the stated
scope; they must not silently disappear:

The user also asked to ignore angle controls because their pen does not
support them. Pen-angle/rotation validation is therefore deferred. Basic
stored image orientation remains necessary for chains and other material
brushes; this does not establish how CSP combines a fixed angle with line
direction. Source values remain intact.

| Boundary | Recommended interpretation | Why a decision may be needed |
| --- | --- | --- |
| Ruler/object snapping | User-approved deferral; retain imported setting. | This depends on external geometry, unlike brush-local stabilization. |
| Erase across multiple layers | User-approved deferral; support current raster layer erasure. | It changes document behavior beyond the new raster brush tool. |
| Newer-version-only rendering | Target installed CSP first; preserve newer fields with a visible notice. | Cannot visually validate a rendering mode absent from installed CSP. |
| Unsupported stylus sensor | Preserve and evaluate if samples are available; use a documented neutral fallback. | Computer-generated mouse strokes cannot validate tilt or rotation response. |
| Perceptual mixing/exact wet transport | Keep in scope; measure before claiming parity. | Public parameter descriptions do not disclose exact algorithms. |
| Paper/grain asset missing from `.sut` | Report the missing asset rather than quietly inventing a replacement. | The preset's identity can depend on that asset. |

## Architecture requirements inferred from this inventory

These are engineering recommendations, not claims about CSP internals.

1. Separate raw pointer samples, stabilized stroke geometry, parameter evaluation, stamp/ribbon generation, and raster compositing. A future vector layer can reuse geometry and brush definitions without importing the raster backend.
2. Store a versioned brush definition with original source fields and a compatibility report. Distinguish recognized/implemented, recognized/approximated, ignored-by-scope, and unknown settings. Import success and visual fidelity are separate results.
3. Preserve source assets and their full registration rectangle. A tight alpha crop changes both visible size and pattern spacing. Multiple references to one image may intentionally weight a sequence.
4. Store timestamp, pressure, tilt, azimuth/barrel rotation when available; preserve the distinction between absent pressure and an actual zero. Make mouse pressure fallback explicit.
5. Use a distance-based sampler with carried remainder and a separate time-based continuous spray schedule during movement and stationary holds. Randomness should have a reproducible stroke seed; do not make results depend on UI repaint frequency. The current scheduler's cadence and bounded catch-up behavior are described in [continuous scheduling](brush-continuous-scheduling.md).
6. Give stroke opacity, per-tip density, texture, paint loading, and secondary-brush composition distinct stages. A single alpha slider cannot represent all of them.
7. Use bounded dirty regions and tiled temporary stroke buffers. Post correction, ending-by-percentage, and deferred watercolor edges may need rerendering against the pre-stroke raster state. One stroke must remain one undo action.
8. Treat ribbon mapping as deformation of a connected strip, not simply rotation of isolated stamps. Preserve texture continuity along arc length and test pressure-changing width.
9. Wet brushes require controlled access to the active layer and explicit pickup/transport state. Prevent in-place scan order from making results depend on whether pixels are visited left-to-right or right-to-left.
10. Preserve unknown numeric/enumerated values until verified. An unrecognized value is evidence to investigate, not permission to clamp it to a convenient enum.

## Comparison ladder and acceptance evidence

Use this order to localize mismatches instead of debugging all features at once:

| Step | Fixture | What to record |
| --- | --- | --- |
| A | Hard circular pen, all dynamics off | Size, coverage, boundary profile, start/end footprint. |
| B | Soft circular brush and translucent marker | Radial falloff; tip density versus stroke opacity; same-stroke crossings. |
| C | Pencil with one texture | Grain scale/phase/contrast and pressure response. |
| D | Asymmetric material tip with visible padding | Size normalization, anchor, rotation convention, spacing. |
| E | Multi-tip rigid chain and deformed ribbon | Sequence, seam continuity, aspect ratio, flips and corner behavior. |
| F | Sparse foliage and blood-splatter spray | Tip mix, density, spatial spread, size/angle distributions, main/sub colors. |
| G | Watercolor/wet paint on known color patches | Pickup, transport distance, transparency, buildup, mixing color and edge. |
| H | Dual brush | Secondary geometry and composition with parameter toggles one at a time. |
| I | Real Asset Store brushes from several creators | Original `.sut` import report, all embedded assets, CSP settings screenshot, raster output pair. |

For each pair, record CSP version, preset identity/hash, canvas resolution and zoom, brush size units, colors, layer alpha/background, pressure/dynamics, path, duration, and any changed setting. Export pixels at native resolution; UI screenshots alone confound zoom and color management. Mouse-only comparisons establish constant-input geometry and compositing, not stylus fidelity.

Keep a controlled path set: dot, straight horizontal/diagonal, shallow arc, S, loop crossing itself, zigzag, stationary hold, and repeated passes. Add recorded pressure ramps and tilt sweeps where a real tablet is available. Vary pointer event subdivision while preserving path/time to test sampling invariance.

For deterministic cases, compare footprint bounds, mean/max alpha, stroke width profile, and aligned image error. Set tolerances after seeing baseline CSP repeatability; do not invent a universal percentage and label all brushes compatible. For random brushes, compare distributions across several strokes rather than require identical random placements. Label visual approximations and remaining discrepancies beside the example.

## User validation sheet

The final drawing exercise should expose the actual Brush tool and imported presets, not only pre-rendered thumbnails. Suggested labeled cells: pen, pencil, airbrush, watercolor, textured paint, chain, foliage, splatter, RGB stamp, and dual brush. Each cell should offer an empty area, a sample stroke, and a short instruction such as “draw an S, vary pressure, then cross the first stroke.” Wet-brush cells need colored patches on the active raster layer. Include reset/undo, brush size and main/sub color controls, and a way to import the user's own `.sut` beside the supplied examples.

Use generated or permissioned demonstration assets for the shared sheet. Keep third-party downloaded brush files local unless their redistribution permission is established; store source identity and comparison evidence without assuming a download permits bundling.
