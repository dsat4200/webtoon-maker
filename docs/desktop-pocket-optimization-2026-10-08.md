# Pocket Boyfriend desktop optimization and stability

This pass uses the Ryzen 7 7700X / 32 GB / RTX 5060 desktop, CPython 3.11.15,
PySide6/Qt 6.11.1, and the Windows platform. Native GPU runs independently
identify `NVIDIA GeForce RTX 5060/PCIe/SSE2`, at display density 1.5. No desktop
automation, provider connection, Blender process, or original-project save is
needed. The preceding raster-feedback fixes are included in the frozen starting
build; comparisons here measure additional work.

Three complete project copies were verified against all 13,776 original files
(357,023,852 bytes). The rewritten chapter has 106 layers, 173 objects,
103 modifiers and 29 masks. The original project and preferences are protected
by full before/after SHA-256 inventories. Raw evidence is under
`.artifacts/desktop-pocket-20261008`, with `initial-source` preserving the
starting runtime and test bytes.

## Accepted Brush changes

Brush retains the exact promoted unchanged foreground calculation, broadcasts
the original integer-to-float32 coordinate axes, and retains immutable dabs
whose computed envelopes do not change any scalar bits. Integer conversion
precedes the original half-pixel addition, including coordinates beyond 2^24.
These changes preserve original source grids, precision, RNG and materials.

Balanced 12-run CPU comparisons contain 468 accepted packets per owner/case.
Final complete source buffers and profiles match. These are engine timings,
excluding native event dispatch, presentation and physical pen latency.

| Case | Original median | Candidate median |
| --- | ---: | ---: |
| Dense 8 px, byte | 2.179 ms | 1.809 ms |
| Dense 8 px, RGBA64 | 2.467 ms | 2.059 ms |
| Dual 16 px, byte | 2.539 ms | 2.141 ms |
| Dual 16 px, RGBA64 | 2.873 ms | 2.390 ms |
| Authored color, byte | 3.056 ms | 2.823 ms |
| Authored color, RGBA64 | 3.416 ms | 3.205 ms |

RGBA64 authored-color CPU packet p95 stayed flat (8.012 to 8.011 ms). Independent
original primitives compare every emitted dab, RNG state, live native buffer,
float stroke plane, corrected replay, final pixels, and history originals.
The 328 focused Brush checks include 77 new precision regressions.
Reproduce with `tests/benchmark_brush_cpu.py`; detailed balanced samples are
in `.artifacts/realtime-contact-20261008/brush-cpu-reuse-r1.json`.

## Accepted input stability changes

Released cold contacts remain queued in their original authoring context.
Entering mask mode while Pencil waits previously invented a mask history
transaction; deferred Brush/Lasso could also reject an accepted contact after
a later mask or multiple-selection change. Actual pending mask ownership now
controls mask finalization. Deferred Brush/Lasso temporarily restore their
captured authoring context and then restore the user's current UI state.

All 297 native core checks pass, including the four new context regressions.
The new checks compare complete RGBA64 buffers, transaction labels, undo/redo,
unchanged mask pixels/revision, and preservation of current selection/mode.

## Accepted graphics queue change

The graphics owner previously held the same condition lock used by GUI release
and submission while making its GL context current and deleting retired driver
fences. Driver cleanup now happens outside that lock, entirely on the graphics
owner. Requests, close/cancel ordering, leases, and final fence drainage retain
their ordinary ownership. A held-driver regression verifies that release and
submission finish while cleanup remains deliberately blocked; its completion,
cancel, and close cases all pass. This removes a demonstrated blocking path,
but the diagnostic Pencil move spikes were not reproduced and are not attributed
to it.

## Actual saved-project verification

The isolated heavy chapter passes saved Raster 9 Pencil/Eraser/Brush edits,
exact model/pixel undo and redo, cancellation without a history entry, existing
mask painting with undo/redo, and scratch fill/copy/cut/paste with exact history.
Manual Save deliberately waits for a released cold input worker; reopen and
chapter switching preserve the captured model and native pixel bytes. The
check also verifies 7,961 unrelated files in that copy remain unchanged.
Evidence: `saved-project-stability.json` and its XML.
The same eleven actions pass again on a fresh, fully inventoried copy using
the final frozen runtime. All 702 source hashes remain unchanged, all 157
imported app modules come from that freeze, and there are no external actions,
notices, or errors. Evidence: `saved-project-stability-final.json` and its XML
and provenance file.

Native MainWindow runs use Raster 38 at camera `(540, 32642.3076923, .13, 0)`,
955 x 927 logical viewport, 120 Hz synthetic tablet events, and the saved Brush
preset. The hidden dream page is made visible only in memory. Both starting
and Brush-candidate runs dispatch all 186 events, finish three transactions,
drain without errors, and keep every project-copy file unchanged. Their final
frame PNG hashes match, and each recorded framebuffer region matches the
independent synchronous render with zero changed bytes. Those initial captures
were cropped by one row and column: Python rounding and GL viewport both report
1432 x 1390 where Qt's actual 150%-density attachment is 1433 x 1391.

The measurement helper now queries the bound attachment dimensions without
painting. Native tests check all four true corner pixels, exact Qt capture
equality, and unchanged paint count at both small and full benchmark sizes.
The combined candidate rerun captures all 1433 x 1391 pixels. It accepts all
186 posted events, commits exactly three strokes, evaluates all three selected
effects, and reaches a nonprovisional exact frame without errors. Its full
native framebuffer equals the synchronous oracle with zero differing bytes,
and every file in the read-only project copy remains unchanged. Evidence:
`native-heavy-combined-fullframe/summary.json` and its two native PNGs.

This complex stack still has substantial delay: first exact readiness is
about 65 seconds, and a paint gap after Brush is about 11.6 seconds. Fast paint
callbacks and successful final equality do not establish live current artwork.
No prepared raster-feedback frames cover this modified target. The controlled
Brush improvement does not make all of these heavy effects realtime.
The final combined run takes 65.445 seconds to first exact readiness; its
186 input events reach the final exact frame in 14.807 seconds median and
16.361 seconds maximum. These are warm-input-to-exact timings, not a claim of
physical pen/display latency or prepared live current artwork.

## Projection test expectation correction

The first broad native run passes 1,500 of 1,504 cases. Its four failures also
occur in the frozen starting build: an older test waits for a detached preview
while a covered native contact deliberately pauses that work. The correction
changes only the test. It requires positively owned current feedback that was
actually presented, visible pixel progress on every segment, unchanged pixels
outside the stroke, and byte-identical final exact pixels after release. Ordinary
preview/exact waits retain their original requirement. All 20 cases in that
file and a 257-case native contact/input/projection group pass; a fresh combined
selection is used for final assembled validation.

Final assembled native Windows verification passes **2,104 cases**, with zero
failures, errors, or skips in 477.032 seconds. It combines the broader rendering,
GPU, cache and precision matrix with the core-feature and focused Brush groups,
without double-counting overlapping modules. Every imported app module comes
from `final-verified-source`; all 702 frozen source hashes are unchanged, with
no external-action attempts. Evidence: `final-combined-stability.xml`,
`final-combined-run-provenance.json`, and `final-combined-run-result.json`.

Final synthetic native-contact checks on the RTX 5060 also retain the preceding
live-feedback behavior: at zoom 1.0 and DPR 1.0/1.5, all 120 contacts per run
show current ink while held within 16.7 ms, with no detached scene submissions
during contact. Pencil/Eraser/Brush p95 values are 6.860/8.190/13.897 ms at DPR
1.0 and 6.945/6.352/13.407 ms at DPR 1.5. At zoom .35 and DPR 1.5, all 120
contacts likewise show current ink within 16.7 ms (p95 8.679/6.956/12.316 ms).
The complete source is restored by undo and each stroke has one transaction.
These fresh checks establish current behavior, not an incremental causal gain
from this hour. Handler-to-Qt-swap timings exclude physical tablet driver and
display scanout, and the observer adds overhead. Evidence: `final-contact-*.json`.

The additional zoom .1 probe cannot establish a per-input latency bound. Its
single-point pure-color oracle rounds Pencil's physical y=517.5 to row 518,
while the true red pixels occupy the preceding row; Brush's final red sample
likewise lies one column left. Private classification verifies all native
source centers contain expected pixels, held source bytes are unchanged by
release, and complete 1350 x 1050 held, settled and synchronous-oracle frames
are identical for all three tools. Thus the missing strict sample is a
measurement error rather than delayed/missing artwork. No threshold or frozen
harness was changed. Evidence: `extreme-contact-classification-v2.json` and
the held/settled/oracle PNG triples. Correct held artwork is established;
per-packet realtime timing at zoom .1 is not.

## Rejected changes and remaining cost

The admitted detached scene probe evaluates 115 exact native tiles. Independent
recordings compare every buffer's bytes, dimensions, format, stride, profile
and DPR, while retaining the saved chapter bytes.

Static entity settings take under 8 ms of the 42–44 second render, so settings
memoization is rejected. Most work occurs in Room mesh warps, Twirl and
Kuwahara. The two mesh IDs execute 480/176 times and consume 9.63/4.12 seconds;
Twirl consumes 2.51 seconds in 198 calls; Kuwahara consumes 5.57 seconds in
182 calls. The actual Smudge takes 2.36 seconds once. Nested timings overlap
and must not be added blindly. Windows thread-CPU counters have coarse
granularity; bounded owner-thread wall intervals supply the detailed costs.

A causal retirement trace confirms the stores are admitted and then lost.
The existing 192 MiB shared pool reaches its byte cap; its effect and retained
maps also reach their 512-alias limits. The two expensive Room stage groups
incur 512 and 106 repeated misses after byte-pressure retirement, versus two
and zero after metadata retirement. The observer retains semantic tuples and
scalar records, and its map-only control preserves key/order/byte/metadata
outcomes. All 115 native tile records and chapter bytes match the stage baseline.
Evidence: `admission-dream-baseline-instrumented.json`.

A private 384 MiB capacity experiment, through the existing budget setters and
an explicit extra 192 MiB admission reservation, also matches all 115 buffers.
Room mesh call counts fall from 480/176 to 320/134, Twirl from 198 to 148, and
Kuwahara from 182 to 142. Global byte retirements fall from 6,633 to 1,219 while
metadata retirements rise from 2,287 to 4,665. The first instrumented elapsed
sample falls from 44.480 to 36.701 seconds. This is a capacity diagnosis, not
an accepted runtime optimization or physical input benchmark. Production
budgets and preferences are unchanged. Increasing shipped retention also needs
accounting for inactive evaluators, cache transfers, and durable writer queues,
plus a matched native MainWindow test. The ordinary snapshot estimate describes
active workspace rather than all retained ownership. Evidence:
`admission-dream-private384-instrumented.json`.
Serialized repeats reproduce 44.617 seconds at 192 MiB and 37.092 seconds at
384 MiB, with identical work and retirement counts within each capacity and
full native equality in all four runs. The unchanged 512-alias limit becomes
a stronger constraint as byte pressure falls. The private summary and all
underlying records remain in `admission-dream-capacity-summary.json`.

A prototype reusing a fully assembled frame through existing bounded semantic
maps produces zero checkpoint hits and unchanged expensive call counts.
It takes 42.982 seconds versus 41.964 seconds for the instrumented stage
baseline, with identical native buffers. It is rejected; cache budgets and
sampling are unchanged. A hidden MainWindow callback probe also disproves a
large property-panel cost: 40 callbacks have a 0.060 ms median and 0.163 ms p95,
with no source decode. No property-panel skip is accepted.

The Pencil GUI profile is diagnostic only. It captures 60 continuation calls
in 145 ms total and does not reproduce the earlier long move gaps. Its run
stops on an overly narrow benchmark effect-observer assertion after pixel
validation; it is not counted as a successful end-to-end run. The harness now
records input, drain and oracle effect coverage separately, verifies one
transaction per stroke, and completes cleanup before coverage assertions.

## Test isolation correction

The first clipboard audit changed and cleared the desktop clipboard through
an existing test. Subsequent runs use a private Qt MIME clipboard throughout
teardown. The original image was recovered by its exact recorded PNG SHA-256
and restored only after the native clipboard was confirmed empty and unchanged.
Unknown extra MIME representations are not reconstructed. The recovery image
and evidence are preserved in the private artifact directory.

All artwork caches continue to use the native document grid and ordinary shared
semantic dependencies. These changes add no disk renderer, sampling exception,
durable live edit, or new color/precision policy. Save current work and restart
the editor to load the updated runtime.

Final protection checks verify all 13,776 original files by SHA-256, size and
mtime, with zero changed/added/removed files. Actual user preferences retain
their original SHA-256. The working runtime/test bytes match all 702 files in
the verified freeze. The existing editor process is left running. Evidence:
`protected-files-final.json` and `final-assembly-verification.json`.
