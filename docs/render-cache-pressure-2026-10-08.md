# Heavy-scene cache pressure follow-up

This is continuing development after the verified stable integration checkpoint
`a36937eb04fec866b5949a93b1e55edeec9ff7aa`, published on
`codex/refactor-integration`. The cache-pressure branch remains unpublished and
is not yet accepted as the next stable build. Heavy-object responsiveness and
the full physical editing matrix remain incomplete.

## Workload and measurement boundary

The original test project is `Pocket-Boyfriend Test Copy`, saved chapter
`0a72f08009294aa0a3d14e6a38e22bbb` (Chapter 1 Rewritten). All native actors use
guarded private copies, with disconnected external providers and the complete
original 14,317-file inventory checked before and after. They do not edit the
Blender integration or its source data.

The clean rendering workload selects saved Raster38 at camera scale 0.13. The
480 by 687 logical viewport spans native document bounds
`[0, 22696.413763924884, 1080, 5284.615384615383]`. It requires all 120 exact
260 by 260 QImage tiles, including the ordinary gutters: 32,448,000 raw output
bytes. Exact tile DPR is one; actual Windows application/canvas/display DPR is
1.5. The GPU is Intel Iris Xe, OpenGL driver 32.0.101.7088.

Timing starts at the actual window show and ends after a native frameSwapped
signal for the current exact demand and scheduler idle. The separately reported
first swap is the same current exact demand, not a retained or provisional
frame. Production render methods and caches are not wrapped during clean runs.
Derived caches start fresh and the private disk backing is excluded. Original
source and operating-system filesystem cache warmth are uncontrolled. This
workload proves one viewport, not whole-chapter rendering or physical input
latency.

## Unique storage accounting, R20

The source and effect semantic maps now own independent copy-on-write QImage
handles and account once for each actual shared Qt pixel allocation. Separate
allocations with equal pixels still count separately. Each existing 64 MiB
partition and 512-record limit remains in force. Checkpoint accounting already
had this ownership behavior. The process-local Qt allocation identity is used
only in transient bookkeeping; semantic keys, source/effect grids, dependency
validation, precision and durable cache identities are unchanged.

At native Windows DPR1.5, 185 selected checks passed: 23 additive ownership and
accounting cases plus 162 existing native precision, transfer, dependency,
mask, disk, preview, Solo and cancellation cases. No failures or skips were
reported. The actual module origins and complete frozen source/test inventories
passed, and the process closed with no late stderr. This focused group does not
replace the final broad suite or physical acceptance.

| Clean pair | R19 settle seconds | R20 settle seconds | Complete native output |
| --- | ---: | ---: | --- |
| Morning baseline / evening candidate | 179.148279 | 92.113435 | Byte identical |
| Fresh evening baseline / candidate | 83.362388 | 81.015437 | Byte identical |

The baseline variation prevents a general speedup claim from the first pair.
The fresh evening pair shows the remaining delay clearly. Process working-set
measurements also vary: peak resident bytes were 2,068,217,856 versus
1,429,499,904 in the first pair and 2,111,524,864 versus 1,960,058,880 in the
second. They do not establish a fixed memory saving or a process memory bound.

Both completed pairs verify all 120 raw buffers and their native size, format,
stride, gutters, color profile, source model, actual GPU and display density.
Every original/private saved file remained unchanged. A separate R19 run failed
its 180-second readiness bound; it remains a failed observation, not a success
excluded from a timing population. Another attempt was aborted by the root
after an incorrect diagnostic-field search; that is a harness/operator failure,
not an application failure. Its evidence remains preserved. No three-pair
population conclusion is claimed.

## Repeated exact-stage work still present

A separate bounded R20 diagnostic observed the neighboring saved Smudge object
`8dcbef5384554e0092456fd0926bb05a` within the Raster38 viewport. The current
exact owner completed all 120 tiles. It performed six focused stage renders and
42 focused cache misses: six source, twelve effect, and twenty-four retained
lookups. Those stage spans total 23,771 ms inclusive wall time and 23,141 ms
thread CPU. Nested parent spans must not be added to those totals.

The input is 2407 by 2407 native ARGB32 pixels (23,174,596 bytes); the output is
2407 by 2807 (27,025,796 bytes). Earlier and current diagnostic evidence places
these results under pressure from a 44,820,792-byte gradient. The source/gradient
and retained-output/gradient pairs exceed their independent 64 MiB partitions.
Unique storage accounting alone does not prevent those distinct images from
evicting one another.

The bounded observer deliberately omits nonfocus semantic-key metadata. It
cannot reconstruct the entire cross-role working union or prove that a proposed
aggregate pool will fit it. Its timing is instrumented and is not substituted
for the clean measurement above.

## Monitor and empty disk-status repairs, R21

The private development checkout now contains two monitor runtime repairs and
an empty-index status repair, applied after both clean actors closed. Native
acceptance is pending.

The monitor observes actual GPU frame dispatch, cooperative capture slices,
owned canvas/navigator worker envelopes and their coarse effect/cache spans.
Worker metadata is immutable and contains no live widget reads. A locked
recorder epoch rejects spans and events crossing Stop, restart or Clear;
wrappers preserve exceptions, restore weak ownership and retain explicitly
unattributed legacy global-effect logging. CPU spans use thread CPU time;
these are not GPU duration measurements.

An ordinary disk backing with an empty committed index cannot satisfy any exact
projection tile. The status path now checks the existing clear epoch and that
empty index before traversing document dependency keys. Pending-color and clear
guards still run first. Nonempty lookup, dependency/clear/blob validation,
storage polling and manual recording use the original pipeline. No status memo,
alternative invalidation or automatic durable recording was introduced.

## Aggregate residency experiment

The private experiment lets source, effect and retained
maps share their existing total nominal allowance (three times 64 MiB) while
keeping separate semantic maps and per-map metadata limits. It must preserve
actual QImage ownership, checkpoint keys and state, null replacements, transfer,
native precision, cancellation and disk contracts. The pool is per evaluator;
it is not a process/RSS cap or a change to working-memory admission.

Keep criteria are actual reduction in repeated exact-stage work, complete native
pixel equality, guarded lifecycle tests, and repeated clean timing evidence.
Static review and prospective tests alone do not establish those criteria.

The experiment is now applied privately in R22/R23. Its 307 native Windows
DPR1.5 checks passed: all 64 additive pool cases, the complete earlier 185-case
group, and 58 unchanged outline/export cases. Independent auditing rehashed all
298 runtime files, 378 test files and four support inputs; no provider entries,
import mismatches, skips, errors or late stderr occurred. Null checkpoints retain
their replacement key/state instead of accidentally reusing old non-null pixels.

The first actual-comic aggregate run completed the same 120 tiles in 76.207419
seconds, using 75.921875 process CPU seconds. Every native buffer matches R19.
Peak resident bytes were 2,414,555,136. This one observation establishes neither
a reliable population speedup nor improved memory use. The delay remains
unacceptable. The endpoint census still lacks the focused 23,174,596-byte source
and 27,025,796-byte output; a new separate trace must establish their actual
retirement/reuse history. The original observer missed cross-role pool eviction,
so it cannot be reused as a complete causal observer without repair.

## Acceptance failures and causal corrections

The R21 monitor/disk group collected 222 cases: 216 passed and six failed, with
valid native provenance and zero late stderr. R23, after additive fixture
corrections, collected 224: 220 passed and four failed. These failed attempts
remain retained. All 63 original monitor cases and seven recorder epoch cases
passed; the failed new pointer test does not establish full pointer acceptance.

Native failures exposed an actual instrumentation gap: ordinary scoped stages
invoke `ui.interactive_effects`' independently imported stack alias. Patching
the source module alone misses that call. The next monitor candidate adds that
owned alias. Two fixture assumptions also proved wrong: TileStore paint uses
explicit opacity and overrides QColor alpha, and an ordered Dithering tile can
use its GPU path without invoking the CPU stack alias. Fixtures must exercise
and verify their intended production paths.

The unchanged navigator error-recovery case passes alone but fails after the
disk contact-cancellation cases. A separate read-only native boundary diagnostic
confirms that those tests leave QApplication's simulated LeftButton held after
Escape, including at navigator test entry. Navigator correctly postpones work
while the button is held. Matching QTest releases belong in the cancellation
fixtures; changing the production pause policy or extending the color-resource
timeout would hide the cause. Existing cancellation model/history assertions
and the navigator error/correction assertions remain required.

R24 adds the ordinary scoped stack-alias monitor hook and matching releases in
the two disk cancellation test families. The original cancellation and color
assertions and their timeouts remain unchanged. The same combined selection
collected 224 cases: 222 passed and two failed, with native Windows DPR1.5,
valid actual import provenance, no provider entries and zero late stderr.
The original navigator retry and the new GPU pointer test now pass, including
the pointer test's complete native output, model and source comparisons.
The two remaining additive failures identify a separate prebound effect-stage
alias missing from the monitor, and a regional test whose actual request still
covers the complete source. They remain failed acceptance, pending correction.

The separately instrumented R4 pooled capture reached its current exact
viewport after 88.531 seconds and preserved the model and source guards.
The observer nevertheless rejected its own evidence: four stored descriptors
disappeared without a recognized alias boundary. Ordinary same-key replacement
can pass an equal but distinct key object to the removal callback, whereas the
observer indexed the resident canonical key by object identity. Its four
unrecognized removals prevent complete causal acceptance. A fresh observer
must resolve the actual resident key at that callback and continue to reject
any unexplained removal. This capture is neither a clean timing pair nor an
accepted pooled eviction diagnosis.

The corrected R5 observer completed a separate instrumented viewport in
86.712 seconds. It resolved equal resident keys before cross-role removal,
recognized all four formerly unexplained replacements, and reported no unknown
removals. The focused Smudge stage ran four times, totaling 16.083 seconds of
inclusive wall time. Source/effect/retained lookups lost reusable results both
to global byte pressure and to the existing 512-entry local metadata limits.
This motivates a bounded reuse-based retention experiment; simply increasing
the allowance or claiming a benefit from endpoint occupancy would not address
both causes. This instrumented capture is not a clean speed comparison.

R25's enlarged Sharpness fixture exposed a real native rendering defect.
After an actual cropped TileGraph request, a later whole-view render differed
in 16 of 30 complete native tile buffers. The R27 causal control reproduced
that mismatch; bypassing only the focused translation-output lookup made all
30 whole native buffers agree between independent cold and reused owners.
The captured crop was 1026 by 1026, but the translation alias promised the
full 1281 by 1153 object bounds. Four later whole-view lookups reused that
crop. Source pixels and saved model data remained unchanged.

The old radial raster path unconditionally admitted this cropped result under a
full-frame translation key. The mirror path already checks that output bounds
equal the promised full bounds before admitting the alias. A matching radial
admission guard and the existing shared renderer version migration were applied
in R28. Acceptance compares against both lookup-disabled native
rendering and an independent whole original Sharpness kernel: an ordinary
cold tiled render can itself be contaminated by an earlier cropped alias.
The failed native attempts remain preserved, and no expected failures belong
in the eventual stable suite.

R26's three selected actual generic-adapter monitor/lifecycle checks passed
at native Windows DPR1.5, with full pixels/source/model comparisons and no
provider entries or late stderr. This repairs instrumentation coverage; it
does not accept the regional rendering defect or establish responsiveness.

R28 applies the radial full-bounds admission guard and advances the existing
shared renderer version. All six new native regressions pass: the whole
original Sharpness kernel, lookup-disabled cold references, cropped and repeated
requests, legitimate complete-frame translation reuse, and ordinary migration
of effect/retained/final-projection entries without deleting source data.
The original 224-check monitor/disk selection also passes in a fresh native
Windows DPR1.5 process. Complete source/tests/all four support files, actual
imports, original pixel/model assertions, provider isolation and late stderr
guards remain intact.

A separate 65-case TileGraph/Sharpness selection passes 62 and fails three
unchanged Kuwahara whole-frame comparisons. All three failures independently
reproduce against published R19 in a fresh native DPR1.5 process. They are
an existing unaccepted precision/region issue, not a reason to weaken native
pixel equality. The previous full R19 suite ran with explicitly forced DPR1.0;
that broad success does not establish native DPR1.5 coverage. The actual
cause of the Kuwahara mismatch remains under investigation.

R29/R30 tests a bounded reuse-based retention policy within the same 192 MiB
shared image allowance and 512-record role limits. R29 passes all 307 original
pool/cache cases and six radial regressions, but three new format variants
expose a pressure-fixture mistake: byte eviction removed the cold aliases before
the intended local metadata pressure. R30 adds same-allocation aliases to that
additive fixture without changing runtime or the other assertions. All 51 new
cases then pass at native Windows DPR1.5. These are correctness results, not
evidence of improved interactive performance.

The first uninstrumented R28/R30 matched Raster38 viewport pair takes 76.184
and 102.547 seconds respectively. All 120 complete native buffers (32,448,000
bytes), output metadata, saved model, actual GPU/view/DPR, provider guards and
clean shutdown checks agree. The candidate is slower in this pair and has not
earned publication as an optimization. One pair is not a population estimate;
further causal measurements must explain the regression before retaining the
policy in a stable build.

The R30 cProfile capture closes cleanly and preserves all artwork/source guards,
but its function timing data is rejected. On the installed CPython 3.14.6,
global profiling callbacks interleave GUI and worker events through one profile
context while the external `time.thread_time` timer uses separate thread clocks.
The raw stats contain 808,396.266 positive and -808,297.375 negative self seconds;
the reported source subset of 411,800.859 seconds cannot describe the 82-second
job. Clipping or rescaling those records would invent measurements. Only the
independent outer instrumented job envelope (79.188 thread CPU / 82.042 wall
seconds) is usable. A replacement must filter the actual owner thread and
validate its accounting before any hotspot rankings receive credit.

Hidden pages already return before their normal artwork, child, and modifier
rendering; ordinary regional dependency keys also omit their branch contents.
That does not make them free to load: snapshot capture still visits their model
records and source-tile addresses and registers immutable file pins. These
metadata/file-preparation costs are separate from pixel evaluation. Any later
visibility-based preparation pruning must retain legitimate visible mask,
linked-source and halftone dependencies and immutable revision ownership.

## Local evidence inventory

Private captures stay under `.artifacts/refactor-integration-20261007`; they are
not publication payloads. Key evidence is:

- `ports-combined-src-r20-freeze-proof.json`, complete 297-runtime/374-test freeze.
- `unique-cache-r20-native15-r1/pytest.xml` and `terminal-proof.json`, native185.
- `unique-cache-r20-native185-independent-audit-r1.json`, independent guard audit.
- `raster38-cache-r19-clean-baseline-r4` and `raster38-cache-r20-clean-candidate-r2`.
- `r19-r20-clean-pair1-r1.json`, full first-pair comparison.
- `raster38-cache-r19-clean-baseline-r5` and `raster38-cache-r20-clean-candidate-r3`.
- `r19-r20-clean-pair2-r2.json`, full fresh evening comparison.
- `raster38-smudge-cache-r20-warm-r1`, separate instrumented diagnostic.
- `monitor-disk-r21-root-apply-receipt-r1.json`, exact six-file application record.
- `ports-combined-src-r21-freeze-proof.json`, complete 297-runtime/377-test freeze.
- `aggregate-pool-r22-native15-r1` and `aggregate-pool-r22-native307-independent-audit-r1.json`.
- `raster38-cache-pool-r23-clean-candidate-r1` and `r19-pool-clean-pair1-r1.json`.
- `monitor-disk-r21-native15-r1` and `monitor-disk-r23-native15-r1`, retained failed groups.
- `mouse-boundary-color-retry-r23-r1`, read-only native global-button diagnosis.
- `monitor-disk-r24-native15-r1`, retained 222-pass/two-failure native group.
- `raster38-smudge-cache-r23-warm-r4`, retained observer failure after exact settlement.
- `monitor-fixtures-r24-root-apply-receipt-r1.json`, guarded monitor and input-fixture repairs.
- `raster38-smudge-cache-r24-warm-r5`, corrected bounded pooled causal observer.
- `monitor-disk-r25-native15-r1`, retained enlarged-region and adapter failures.
- `monitor-adapter-r26-native15-r1`, three passing actual-adapter/lifecycle cases.
- `radial-crop-diagnostic-r27-native15-r1`, ordinary failure and focused-bypass pass.
- `radial-crop-diagnostic-r27-root-assessment-r1.json`, actual crop/full-frame witnesses.
- `radial-full-frame-r28-native15-r1` and `monitor-disk-r28-native15-r1`, six new and 224 original checks.
- `kuwahara-tile-reference-r19-native15-r1`, three unchanged native mismatches on published code.
- `image-segments-r29-native15-r1` and `image-segments-r30-native15-r1`, failed fixture and corrected native51.
- `r28-r30-segment-clean-pair1-r1.json`, complete uninstrumented viewport comparison.
- `raster38-cpu-profile-r30-r1`, preserved raw capture with rejected function accounting.
- `cpu-profile-r30-rejection-aggregate-r1/manifest.json`, raw accounting and matching CPython source.

Every explicit full-source assertion includes the four support inputs. Private
raw image buffers, the copied comic and capture metadata remain excluded from
Git publication.
