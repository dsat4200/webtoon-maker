# Smudge Modifier

Add **Distort → Smudge Modifier** to an object or layer, then activate its
modifier editing mode. Drag in an empty area of the canvas to draw a dotted,
open stroke preview. Releasing fits the gesture to a single cubic curve with
exactly two editable points.

Select a point to edit **Radius**, **Flow**, and **Strength** under **Bézier
properties**, or use the three vertical controls on the left of the canvas.
Drag a point to move it and its handle, drag a handle to reshape the curve, or
drag the curve to move the whole stroke. **Vector** collapses that point's curve
handle; switching back to **Bézier** restores the handle. Delete the selected
stroke using its canvas button, the modifier panel, or Delete. Escape cancels
an unfinished gesture. Each completed gesture is one undo step.

**Tool settings** stays outside the collapsible point-properties section.
These are defaults for new strokes: radius, flow, strength, pressure enable,
and independent pressure mappings and curves for all three values. Pressure
controls strength by default. Mouse strokes use full pressure. Existing
strokes retain the pressure settings captured when they were drawn.

Save and load named tool presets within the current project. Presets store
tool defaults and pressure curves, without copying drawn strokes. **Opacity**
blends the resulting smear with the original image independently of stroke
strength.

## Rendering and responsiveness

The renderer carries and mixes existing color along each curve in stroke
order. It uses premultiplied color and alpha, including when paint extends
outside the original object's bounds. Output-region requests preserve the
same stroke history so adjacent tiles agree.

The preparation cache shares a 96 MiB budget with other Distort preparation.
It reuses the source and completed stroke prefixes. Editing a late stroke can
reuse earlier strokes; editing an early stroke must update the strokes after
it. Opacity changes reuse the completed smear.

During curve or parameter dragging, the shared Distort preview requests at
most a 224-pixel longest side and 32,768 source pixels. Smudge additionally
caps the extended working area at a 512-pixel longest side and 262,144 pixels.
Releasing returns to the normal exact-render pipeline. The preview can look
softer than the settled result. This is a CPU smudge kernel integrated with
the existing canvas and document-projection caches, rather than a full GPU
modifier-stack rewrite.

Sharp source-color boundaries can leave fine repeated pickup texture along a
smear. The current brush preserves that texture; it is not a pixel-for-pixel
reproduction of Affinity's brush engine.

## Validation

Automated coverage includes gesture fitting and endpoints, recorded pressure,
independent pressure curves, point and handle events with mouse and pen,
cancel/undo/redo, project preset persistence, transforms and asset placement,
ordered color pickup, opacity, transparency, region agreement, cache reuse,
and cancellation without publishing incomplete stroke results.

The final focused suite passed **298 tests**, including the existing Distort,
mesh preview, gradient, model, and vector-model regressions.

An isolated native OpenGL canvas and modifier panel were exercised with a
1024 × 1024 synthetic color image and three smudge strokes. Inputs were posted
at fixed 60 Hz deadlines, independently of painting. All 248 inputs arrived;
the overall input-queue delay was 10.6 ms median and 48.0 ms at the 95th
percentile. These are measurements of this fixture, not a guarantee for every
project or stroke count.

| Action | Paint median | Paint 95th percentile | Release to exact image |
| --- | ---: | ---: | ---: |
| Edit first stroke handle | 56.2 ms | 70.2 ms | 444 ms |
| Edit last stroke handle | 26.5 ms | 32.4 ms | 182 ms |
| Edit last point's Flow gizmo | 26.9 ms | 31.5 ms | 182 ms |
| Pan the finished scene | 7.6 ms | 9.0 ms | — |

Panning invoked the smudge renderer **zero times**. The finished native
framebuffer and a fresh exact render had **zero differing bytes**, with no
pending jobs or rendering errors. A real pen-event sequence retained its
varying pressure and final release position; tool-only presets and the
chapter survived saving and reloading the synthetic project. The user's open
project was not used or restarted.

The first-stroke result is interactive but does not reach 60 frames per
second. A full-resolution release can also pause briefly. These remain
performance limits for heavier stacks.

Reproduction scripts: `tests/benchmark_smudge_rendering.py` and
`tests/benchmark_smudge_interaction.py`. The latter writes an isolated project
and evidence to `.artifacts/smudge-20260927/<label>` and requires a fresh label.
Accepted timing evidence is `native-final/summary.json`, with valid native
pixels in `native-final/finished.png` and `native-final/oracle.png`. The
`native-visual/window-opacity-50.png` screenshot combines that run's native
canvas framebuffer with its live controls and shows the selected stroke,
50% opacity, and all pressure mappings. The earlier `native` attempt was
rejected because its modifier had not been activated. The before/opacity
captures in `native-final` are also excluded: grabbing a hidden Qt window
cleared its OpenGL framebuffer. The visual rerun corrected capture ordering
and explicitly asserted that the image contained the source colors.

The behavior references were the [Affinity Smudge Brush guide](https://www.affinity.studio/help/tools-tools-smudge-brush/)
and the [provided video demonstration](https://www.youtube.com/shorts/_ATZSfzo79U),
which was visually inspected in the browser. The editable two-point strokes,
point gizmos, and nondestructive modifier controls are specific to this editor.
