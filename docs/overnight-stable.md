# Overnight stable snapshot — October 7, 2026

This branch preserves the V20 performance build before integration of the
October 6 detached-renderer refactor. It is based on `0e1b0fc` and includes the
completed performance, source-precision and cache-correctness repairs from the
overnight investigation. Experimental text/font and group-transform candidates
are excluded. The Blender extension is unchanged from the base commit.

In GitHub Desktop, fetch this repository and choose `overnight-stable` under
Current branch. Use the normal launcher, or install `requirements.txt` and run
`python main.py`. The comic project remains a separate portable folder; it is
not included in this source branch.

The frozen regression run completed on October 7 with **6,509 passed, 95
skipped, zero failures and zero errors**. Before publication, all 258 runtime
resources, 359 test files and two entry/support modules were verified against
that frozen run. Git may normalize text line endings on another checkout.
Skipped checks include hardware-dependent and private-project cases; the count
does not establish universal hardware or feature acceptance.

The strongest matched saved-stack drag comparison improved median repaint
from approximately 250 ms to 126 ms with four active modifiers. That remains
visibly slow. Separate Intel tests of the six-active stack measured mask-paint
updates around 28–31 ms. These are specific workload results, not promises for
every object or driver.

Known issues remain: multi-object transforms containing Text can fail to commit
correctly; group transforms with Image/Raster as the primary selection also
have a release-path defect. Individual object transforms are separate paths.
Native text pixels can differ between display densities/font backends, and
heavy modifier computations still delay some interactions. Repairs for those
issues are being validated separately during refactor integration.

See [the investigation](performance-investigation-2026-10-06.md) and
[performance notes](editor-performance.md) for measured improvements, exact
pixel checks and remaining limitations. This branch is a usable checkpoint,
not a claim that the larger performance repair is complete.
