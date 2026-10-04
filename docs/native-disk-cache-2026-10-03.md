# Native artwork sampling and navigator disk cache

Expand the page navigator, drag its white start/end handles, then click
**Cache to disk**. The selected full-width interval snaps outward to 256px rows.
The chapter saves first. Editing pauses while caching; scrolling/zoom and
Cancel remain available. Green marks durable valid rows, red marks missing or
changed rows, and amber marks selected work in progress. Cancel and switching
chapters keep completed work. A subsequent manual click finishes missing rows.
Use the size dropdown to clear selected sections or all cached data.

Caches live beside the chapter in `.render-cache`. They store the same exact
source/effect/stage and final results used by memory caching, with lossless raw
working pixel compression. Original artwork stays independent. Reopening reads
valid results asynchronously into the normal bounded caches; source/mask/effect
and geometry changes select different dependency keys. There is no automatic
recording or disk eviction. Source tile gutters and shared modifier/mask
dependencies can invalidate more than the directly edited pixels.

Exact final artwork uses one sample per document pixel regardless of zoom or
display density. Transient previews may sample below native resolution. Screen
presentation and editing controls still follow device density. Original source
detail and effect sampling/precision are retained, so transforms and modifiers
can calculate new native output from those sources. Zoom alone reuses pixels.

## Local performance probe

An isolated synthetic chapter with raster dabs, Curves and Outline was opened
with empty memory caches. The same native 1080×512 region was prepared before
manual disk caching and after reopening. This measures tile preparation,
including source validation and asynchronous dependency waits, not application
startup time. The probe ran under Qt offscreen on this Windows machine.

| Preparation | Elapsed | Scene captures |
| --- | ---: | ---: |
| Uncached | 311.77 ms | 4 |
| Reopened disk cache | 60.88 ms | 0 |

The final tile bytes matched exactly (SHA-256
`d8d7770ebc0c5c3a7c1afa3607feb3d77e4df854fc5d77d554992791068bd781`).
Manual recording took 212.40 ms after the initial viewport was warmed and
stored 53 entries in 102,697 compressed bytes. These timings are illustrative;
document complexity, storage, compression and OS file caching affect results.

Tests cover exact byte/float codec round trips, async final-tile reuse without
scene traversal, intermediate reuse without source recapture, source/effect
invalidation, editing locks, cancellation/resumption, write failure, corruption
repair, and selective/full clearing. Rendering and editor regressions run with
the existing suite; GPU-only checks require an available OpenGL context.

The batched regression run finished with 5,120 passing checks, 86 skips, and
two existing straight-alpha float-import failures in `test_pixel_contract.py`.
Those conversions produce identical bytes from the unchanged HEAD code.
An intermittent Windows Qt cleanup abort occurred during large test runs;
the affected batch passed on a fresh rerun. OpenGL checks were skipped by the
offscreen platform. Cache-focused checks also cover dirty memory reuse across
save and folder copying, draggable ranges, camera/chapter controls during the
editing lock, deferred image drops, and malformed manifests.

## Completed-read starvation correction (2026-10-04)

The `20261004-122802-d776e4573a` capture recorded 9,230 tile attempts and
9,856 incomplete results over 51.69 seconds, while only 185 tiles were retained.
The source LRU alternated between 1,895,564-byte raster pixels and an
82,380,480-byte gradient source. Section 152 (document Y 38,912–39,168) could
not finish: a restarted capture loaded those sources before reaching its pending
effect, and IO admission discarded the completed effect read to make room.
That restarted the same dependency cycle without any new effect work.

On an isolated snapshot of the chapter and its disk cache, the original code
remained on section 152 after a 20-second probe (617 ticks, 4,600 tile attempts
at the last periodic sample). Each source was loaded over 120 times. With the
completed-read handoff, the entire selected interval (rows 140–172) finished in
22.64 seconds over 264 ticks. The two sources were loaded 15 and 14 times across
the interval, and section 152 advanced by the next periodic sample. These are
local Windows timings, including asynchronous IO and durable publication,
not a general latency guarantee. A subsequent reopened completed range took
3.98 seconds without scene rendering.

The correction uses the existing semantic identities and lossless decoder.
Finished displaced reads enter a 64 MiB LRU, separately bounded from in-flight
reads; one oversized completion is admitted exclusively. It does not change
source/effect sampling, artwork density, rendering, cache keys or durability.
Invalid entries and failed reads remain misses, and clear/close release the
handoff. Regression tests reproduce both blocking and nonblocking read pressure,
verify the handoff budget and cleanup, and preserve checkpoint state and float
bits. The pressure regressions fail with the original lookup implementation.

All 15 native tiles for section 152 (five tiles each for composite, base and top)
matched cold synchronous reference captures byte for byte with disk reuse
disabled. Their concatenated SHA-256 was
`438f7979926d68b1048987117431a607d156577d628083d20cde1ec12be028ab`.

Validation: all 22 disk-cache checks passed, along with the relevant projection,
render-service, retained/parallel effects, translation, tile-graph, dependency,
preview-scope and show-on-top checks. GPU-only cases were skipped offscreen.
The existing mask-only layer/blur export-isolation test failed with the same
354,158 mismatched channels using both the changed and original cache lookup;
that fixture does not attach disk backing.
