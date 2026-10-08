# Native raster drawing responsiveness

Drawing could show old pixels while a held Pencil or Brush repeatedly restarted
a complete chapter capture. Solo and saved transforms also declined the existing
prepared raster presentation, and native input preparation froze unrelated
chapter sources before obtaining the few tile keys required by the stroke.

The input lane now freezes only the requested native source keys with the
original source/version/pin ownership. Prepared current raster presentation now
supports ordinary solo visibility and positive axis-aligned saved object and
ancestor transforms. A covered current Pencil, Brush or Eraser contact cancels
obsolete scene/effect work once and performs no chapter capture or scene
submission while held. Input packets retain their ordered native sampling.
Pen-up resumes the ordinary exact scene renderer and history transaction.

No source/effect grid, precision policy, artwork density or semantic disk cache
pipeline changes. Live feedback remains provisional and never becomes a durable
exact entry. The mapped image replacement uses the ordinary SourceOver sample
phase after clearing the previous native footprint; direct CompositionMode_Source
was measured to sample transformed edges differently and was corrected.

Native Windows regression checks cover full buffers at successive held-pen
positions on 70,000-pixel canvases, nested fractional/unequal positive scales,
all three raster tools, solo object/layer/page visibility, adjacent source tiles,
world gutters, 64-bit source tiles, partial opacity, pruning, stale publication,
release and undo. The 116-case feedback selection and 93-case input/cache
selection pass. Exact sector Kuwahara regional correctness separately passes
68 tests against the original complete-frame kernel; that correction is not a
speed claim.

A separate automated native GPU test loaded a complete private byte copy of
the user's Chapter 1 Rewritten, raised its canvas to 1080 x 70000, and soloed the
saved transformed Raster9. Pencil and Brush each produced four successive
current native source/compositor states without the pending marker, capture or
scene scheduler work at the observed held position. Final current swaps were
observed within 58.7/60.0 ms including a mandatory 50 ms observation pump; these
instrumented bounds are not clean tablet latency measurements. Ordinary exact
release and undo passed. The original project/settings, complete current/frozen
source/tests/support, actual import origins and shutdown guards passed; stderr
was empty.

Private evidence remains in `.artifacts/refactor-integration-20261007`:
`urgent-native-feedback-deferral-r3.xml`,
`urgent-input-cache-regressions-r1.xml`, `urgent-native-sector-r1.xml`, and
`raster-contact-held-r34-solo-native-drawing-deferral-r1` and
`raster-contact-held-r35-solo-native-release-ready-r1`. The release-ready repeat
also passed, observing current swaps at 58.2/60.2 ms with the same 50 ms pump.

This proves the prepared simple-raster path, not every modifier/mask/transform
graph or physical tablet driver. Unsupported graphs still require ordinary
current effect evaluation. The previously measured shared retention policy had
mixed viewport timings and does not establish an overall speed improvement.
Background admission during other native drawing paths remains a separate
priority for further work.

A broader native drawing/scene selection passed 151 of 169 cases. Eight failures
compare differently sized logical and DPR1.5 physical buffers; four held-contact
helpers wait for a new scene preview even though current prepared pixels are
already visible. Six additional physical-coordinate color assertions reproduce
unchanged against the frozen source before contact deferral. These failed results
remain recorded, without relaxing pixel equality or treating this selection as
fully passing. Native fixture corrections are separate from the urgent drawing
behavior change.
