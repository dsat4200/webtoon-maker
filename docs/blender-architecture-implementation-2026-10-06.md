# Blender architecture refactor: implementation and verification

October 6, 2026

This accompanies the [architectural analysis and acceptance plan](blender-architecture-refactor-2026-10-06.md). That document describes the original checkout and remains historical evidence. This report describes the implementation in the working tree and records its verification. The application still uses Python and Qt; the change is ownership of work, invalidation, resources and publication.

## Implemented boundaries

Input passes through a normalized tool session with ordered press, movement, release and interruption. A gesture owns its original document and target, transient state and history transaction. Drawing samples retain their timestamps, pressure and ordering. Released source operations accepted by the editor are drained before a document is retired or a dirty/save decision is made. Latest-only probes can be canceled; accepted Cut, duplicate, mask deletion and cage commits are not silently discarded.

Native source admission queues cold raster and mask samples while detached jobs prepare their original tile buffers. Ordered release publishes one history transaction after all accepted samples finish. Replay yields between bounded packet groups and retains a bounded set of native borrowers; subsequent source misses are prepared again off the input path. Active mask overlays similarly consume prepared scalar and contributor resources, with resident paint updates applied outside painting.

Focused record snapshots and `ChangeSet` notifications describe changed entities, source resources, order, metadata and old/new influence. Forward edits and undo use the same dependency propagation. Parameter controls, gradients, geometry, transforms, text, mask bindings and contributors preserve unrelated records. Mask/modifier registry changes do not rebuild drawable outliner rows. Structural changes still update the hierarchy.

`SceneSnapshotCompiler` captures versioned metadata in cooperative GUI slices and pins original source resources. It retains unchanged records and individual vector stroke generations. Worker replacement also hands off ready original image/tile buffers through the existing source LRUs, guarded by immutable source pins, native versions, document identity and color environment. Unchanged originals avoid repeated decoding; edited or replaced sources keep their newly captured pixels. `SceneKernels` is the shared implementation used by detached evaluation and the explicit reference backend. Workers consume captured inputs and never use the live canvas as their rendering owner. Publication verifies document/resource identity, revision, configuration and consumer demand.

`SceneScheduler` keeps one active evaluation and the newest requested view. Cancellation occurs between bounded native blocks. Whole edited views and their base/top phases publish coherently. The canvas consumes prepared tiles, native overview streams and transient resources. Source loading, effects, text/image preparation and custom color configuration work are detached. Huge overviews stream native finished pixels through a bounded presentation conversion; zoom and display density do not request larger derived artwork.

Eligible raster contact feedback combines a prepared prefix, original source plane, clipping and suffix with changed resident source tiles. Pencil and eraser contact can therefore appear while the ordinary exact evaluator is busy, including foreground occlusion and revealing lower artwork after erasure. Presentation patches have a 32 MiB LRU and reuse unchanged source versions. Predictive ink invalidates only patches containing its native coverage, off-screen patches are culled before composition, and patches use the existing retained GPU presenter. The ordinary typed change stream and coverage checks retain valid prepared planes across selected-pixel edits; final exact coverage retires the displayed overlay. These are provisional display resources, never exact artwork cache entries. Final settled views continue through the ordinary renderer. Native fixed-grid feedback comparisons cover negative origins, source seams, antialiased clips, parent placement, opacity, promotion and entirely erased tiles. The saved-scene proof also records nine background channel bytes differing by at most one outside the gesture footprint due to provisional transparent-pass grouping; settled exact/reference views remain identical.

Shared work admission bounds competing canvas, source, navigator, cache and device work. Its 256 MiB accounting is a conservative concurrency estimate, not an absolute process-memory cap. Oversized work runs exclusively. Other explicit LRUs and handoff budgets bound retained derived results; original pixels, open documents and history have their own ownership and backing policies.

Device images own leases, readiness and context lifetimes. Compatible point/blur graph segments retain GPU resources across operations and presentation without mandatory intermediate readback. Unsupported edges materialize on a detached owner and use the native CPU reference path. Context loss is a cache miss followed by resubmission. GPU worker retirement releases its GUI sharing-context references explicitly, fixing a reproduced delayed Qt context-lifetime crash.

Navigator, pixel probes, assets, baking and output use the same detached scene capture and evaluator. Asset copies retain the selected pixel contract and accepted publication across document switches. Baking captures only affected graph/source history and keeps native QImage formats. Cold tile deletion removes backing addresses without decoding them. Export uses the separate native export edge and a revision-pinned writer.

Manual saves, recovery, Save As and series preference publication use a serial coordinator with immutable revision-pinned captures. A save that finishes after another edit cannot mark that newer revision saved. Save As remaps the published clone's resource paths and adopts them in bounded GUI slices, retaining newer source edits. Close and document replacement settle accepted source operations before taking their saved/dirty snapshot.

A concurrent user-requested recovery fix retains immutable file pins for live source owners when a manual save removes recovery files. The final integration preserves that fix. Resident reads return the already-owned native image immediately; fresh scene captures adopt only completed per-address pins and never start or wait for filesystem work. Tests hold pinning during raster and mask contact, verify undo/redo and newer-edit preservation, capture after recovery cleanup, and exercise a failed batch with a later valid source.

Color boundaries explicitly distinguish original source, premultiplied working buffers, authored sRGB colors, scalar masks, display and export. Native straight 8/16-bit channels enter float storage before alpha multiplication, preserving low-alpha color precision. Material brushes also keep existing 16-bit and floating source formats, association, profiles and untouched source bytes when flushing a tile; legacy byte authoring equations are retained. Custom OCIO preparation owns captured configuration, context and external resources on an admitted serial worker. GUI requests use a readiness ticket; durable cache identity uses the captured semantic configuration/resource signature. Pending or failed environments cannot become exact disk entries.

## Sampling and persistence contracts

Exact document tiles retain one sample per canvas pixel and the existing fixed native tile grid. Source images, sparse source pixels, modifier/source frames, alpha association and precision contracts remain intact. Camera zoom and display density affect presentation and editing controls. Transient previews may use fewer samples and do not replace exact or native source data.

Disk backing retains the ordinary semantic keys, dependency validation and rendering pipeline. Only explicit cache recording admits immutable exact results. Drafts, active gestures, readiness tickets and failed captures never become durable exact entries. Corruption, incompatible versions and missing resources remain ordinary cache misses. Cache jobs and publication honor their document epochs; canceled pre-clear writes cannot resurrect cleared entries.

## Phase evidence ledger

The evidence directory is `.artifacts/blender-refactor-20261006`; sustained native workloads retain separate source and project manifests under `.artifacts/drawing-stutters-20260926`. Logs from unsuccessful intermediate checks are preserved and superseded by identified successful checks, rather than erased.

| Plan phase | Implemented boundary | Verification |
| --- | --- | --- |
| 0 Baselines and precision | Preserved original source and isolated saved project; explicit native import conversion | Pixel contract/edge suites; matched serial native comparisons below |
| 1 Edit transactions | Focused patches, typed changes, dependency generations and observer updates | Change transactions, document patches, focused property observers, mask/tree and placement/history families |
| 2 Tool sessions | Ordered normalized input and original-target gesture lifetimes | Tool session/canvas, brush, clipboard, selection, interruption and retirement tests |
| 3 Detached evaluation | Incremental immutable captures and common worker kernels | Detached scene, render service/backend/outputs, capture integrity and cold-source ownership tests |
| 4 Prepared presentation | Bounded latest-demand scheduling, coherent views, current raster feedback and ready-only auxiliary overlays | Native projection/presentation, busy-renderer contact feedback, cold mask/source gates and latency measurements below |
| 5 GPU resources | Device leases, graph segments, sharing, fallback and context retirement | Final native family: 242 passed, including 51 presentation/contact cases and six actual GPU gradient cases; no skips |
| 6 Consumers/writers | Shared evaluator; revision-pinned serial save/export/copy/bake | Assets/bakes/overlays combined: 193 passed; save/recovery/Save As/lifecycle and output gates |
| 7 Color and remaining hotspots | Native color/alpha edges, detached OCIO, focused gradient controls | Color/output/text: 221 passed; OCIO/source ownership, pixel precision and final regression coverage below |

## Performance and final verification

The complete regression run (`acceptance-tests-214243`) contains 285 test files in 29 isolated processes: 5,566 passed, 113 skipped, no failed tests. During that run, another user chat completed the recovery ownership fix described above. The final source therefore has an explicit five-file delta rather than an unchanged-source claim: three recovery files, scene capture, and the new ownership test file. Every other application/test source hash matches the complete run. The current tree contains 286 test files.

After integration, the affected 20-file scope passed 264 cases with no skips: 128 storage/save/lifecycle, 94 source/native-input and 42 consumer/output/asset cases. All 572 Python source hashes stayed identical across these checks. This verifies the current recovery delta separately from the complete run. Earlier failed radial assertions were replaced with production detached-renderer, worker ownership and native release/undo/redo checks; the subsequent complete run includes those passing replacements. Original failed logs are retained.

The frozen final native family passed 242 cases with no skips, including the 51 added presentation/contact cases and six actual GPU gradient cases (`render-native-family-recovery-final`, 36.76 seconds). It covers all 109 OpenGL cases skipped by the offscreen platform. The other four skips are optional visible Blender integration probes; this refactor does not claim to verify that separate application. Its 572 source hashes match the affected-scope freeze. Final current-source coverage, hashes and the attributed recovery delta are recorded in `.artifacts/blender-refactor-20261006/final-verification.json`.

The unchanged warmed smoke thresholds passed on the same frozen final source: input callback p95 8 ms, frame p95 16.7 ms, commit 100 ms and vector stroke growth at most 1.25. The final results in `smoke-final-220713.log` are:

| Warm workload | Input callback p95 (ms) | Frame p95 (ms) | Commit (ms) | Stroke growth |
| --- | ---: | ---: | ---: | ---: |
| Dense vector navigation | 6.17 | 6.01 | 4.01 | 1.00 |
| Raster pencil | 4.25 | 3.67 | 5.64 | 1.05 |
| Raster eraser | 3.65 | 3.10 | 5.29 | 1.00 |
| Text transform | 2.21 | 1.94 | 2.48 | 0.95 |
| Vector eraser | 1.81 | 1.46 | 3.82 | 1.00 |
| Vector pencil | 1.06 | 0.85 | 27.26 | 1.08 |

Native measurements distinguish event queue delay, callback/paint time, provisional contact feedback and final exact convergence. Qt paint completion is not physical input-to-photon latency. The final serial comparison is `performance-final-220818`: three baseline and seven current-source workloads, with source hashes unchanged throughout and matching the final regression-delta, native and smoke checks. The baseline source is commit `13d659e9419dac80d57ae6a91a9e5f4a31bb5598`; the current source is the verified working tree. Both use the same isolated saved chapter, settings, native Qt backend, one OpenBLAS thread and two one-second strokes at 120 Hz (244 events). Hardware is Ryzen 7 7700X, 32 GiB RAM and RTX 5060 on Windows 10 Pro 19045. Other user applications remained open; available Windows commit at workload start ranged from 3.25 to 5.71 GiB, recorded per run.

All ten workloads completed, every posted input was dispatched with none queued, camera endpoints agreed within 1.1e-14, and every finished native frame/reference comparison had **zero changed bytes**. Project files remained unchanged. Navigator plus save submitted one real autosave. Values below are **p50 / p95 / p99 / maximum**, in milliseconds; queue delay starts at the producer's scheduled event time. Full posted-to-dispatch, producer, handler, heartbeat, transfer, capture and source evidence remains in the raw manifests and `performance-comparison.json`.

| Native workload | Input queue | Paint | Current contact presentation |
| --- | --- | --- | --- |
| Before: warm drawing 1 | 18.19 / 35.68 / 40.99 / 45.94 | 36.68 / 43.68 / 48.91 / 50.12 | — |
| After: warm drawing 1 | 3.41 / 9.38 / 16.37 / 22.65 | 3.19 / 5.75 / 8.25 / 8.63 | 9.57 / 17.94 / 28.84 / 47.12 |
| Before: warm drawing 2 | 19.03 / 37.65 / 40.13 / 42.84 | 37.27 / 43.27 / 45.91 / 47.68 | — |
| After: warm drawing 2 | 1.97 / 7.41 / 12.67 / 17.25 | 2.65 / 5.63 / 7.02 / 8.15 | 6.94 / 14.51 / 26.20 / 33.52 |
| Before: draw, navigate, draw | 16.98 / 34.53 / 40.77 / 43.32 | 25.31 / 40.52 / 42.33 / 45.58 | — |
| After: draw, navigate, draw | 2.10 / 6.53 / 10.52 / 11.85 | 2.52 / 5.26 / 6.73 / 11.22 | 7.13 / 16.20 / 19.83 / 20.71 |
| After: low zoom | 2.98 / 7.63 / 9.04 / 9.91 | 2.96 / 6.12 / 8.93 / 10.15 | 8.83 / 15.18 / 19.23 / 22.03 |
| After: cold source/startup | 1.86 / 9.48 / 26.41 / 40.38 | 1.23 / 1.71 / 2.67 / 6.68* | Unprepared |
| After: navigator plus autosave | 13.94 / 31.49 / 36.98 / 38.82 | 3.99 / 8.01 / 10.23 / 13.56 | 29.47 / 72.70 / 87.48 / 93.94 |

Warm drawing queue p95 fell from 35.68–37.65 ms to 7.41–9.38 ms, and paint p95 from 43.27–43.68 ms to 5.63–5.75 ms. The unchanged offscreen gates pass. The proposed eight-millisecond native warm queue target is met in one repeat, navigation and low zoom, but **not consistently in both warm repeats or under navigator/autosave load**. No universal eight-millisecond native guarantee or physical pen-latency claim is made. Native p99 and maximum values above remain part of the result.

*Cold input begins after the first completed native paint, without waiting for source or exact readiness. All nine selected source tiles were evicted, the source-preparation worker ran once, and GUI source-preparation calls were zero. First native display took 234.49 ms. The table measures paints after input starts; an earlier startup paint took 90.94 ms and is preserved in the raw distribution. Prepared current-contact feedback was unavailable throughout this cold gesture, so its short paint/input times do not prove immediate visible ink. Existing held-basis cold-contact tests separately verify current native drawing and erasing when the prepared composition is eligible.

Final exact convergence is also distinct from input responsiveness. Warm complete artwork appeared 233.66 and 215.38 ms after the last dispatched event, versus 53.84 and 51.49 ms in the synchronous baseline, which performed its exact work while blocking input. Low zoom took 93.35 ms; navigation took 6.80 seconds; navigator/autosave took 3.22 seconds. Cold complete artwork took 13.19 seconds after the last event, with background jobs idle another 4.05 seconds later. The tall cold overview's first complete native artwork took 29.77 seconds from presentation start; remaining jobs became idle another 10.32 seconds later. Every workload converged with no pending inputs and exact zero-byte reference agreement. Timing joins use absolute input/frame clocks and the terminal document revision, documented in `performance-timing-clock-correction.md`; startup, complete artwork and final job retirement are reported separately.

A diagnostic profile of the earlier presentation implementation found 990 feedback patch rebuilds/uploads, of which 872 were caused solely by changed neighboring source-image tokens. Interior edits had not changed the sampled gutter pixels. The same profile found 5,994 vertex builds because switching between exact and feedback clips cleared otherwise stable screen geometry. Instrumented timings are excluded from the latency comparisons. The resulting presentation changes retain geometry under independently keyed clips and compare the exact native strips/corners contributed by neighboring tiles. Central tiles retain their full source token; missing, pruned or unsupported sources use conservative dependencies. Crops preserve native format, profile and bits, and their allocations count against the existing 32 MiB presentation budget. Source pixels, artwork keys and the renderer's sampling remain unchanged. These changes are included in the complete 214243 regression run and the final frozen native, smoke and performance checks above.

The follow-up diagnostic reduced rebuilds/uploads from 990 to 149 and transfer bytes from 267,696,000 to 40,289,600; no neighbor-only misses remained. Geometry builds dropped to nine, with 7,812 retained hits. Capture slices then correlated with about four milliseconds more median input queue delay. Ready Pencil/Eraser contact now uses a two-millisecond metadata slice under the existing target, configuration, coverage and source-readiness guards. Release, cold input and ineligible work retain four milliseconds; the eight-millisecond timer cadence stays unchanged. This changes cooperative scheduling, never input samples or evaluated pixels.

The earlier isolated presentation gate (`blender-capture-budget-warm-gate-final-2142`, before the recovery integration) dispatched all 244 events with none queued, preserved the project files and matched the finished native reference with zero differing bytes. Scheduled queue p50/p95/p99/max was 1.61/6.53/16.44/21.76 ms; paint was 2.42/4.87/5.82/8.44 ms; current contact was 6.21/10.27/30.14/43.02 ms. This diagnostic run explains the initial target result; the repeated frozen final-source measurements above establish the final reported range.

An earlier comparison (`performance-final-211246`) failed its last four workloads under Windows commit pressure, including a 1 MiB NumPy allocation failure and a DLL load reporting an undersized paging file. With our workers exited, only 4.7 GiB of commit remained. After the user closed unused Blender windows, available commit rose to 12.6 GiB and the quieter pre-presentation-fix repeat (`performance-final-211913`) passed all ten workloads. Those sources and timings are historical. The final-source ten-workload repeat above also passed after user applications were reopened; its per-workload memory records describe those conditions. Failed logs remain intact. No system-memory setting or user project was changed.

`acceptance-final.json` confirms that the final source hashes match the native, affected-scope, smoke and ten-workload checks. Compilation and whitespace checks pass. These evidence records preserve the failed intermediate runs and the attributed concurrent recovery fix.

## Scope

The legacy widget backend remains an explicit diagnostic/reference oracle; ordinary detached consumers use the shared kernels. GPU acceleration and immediate prepared feedback have eligibility rules, with CPU evaluation and explicit incomplete presentation for unsupported work. Expensive final effects may take longer than one frame. Background work removes their synchronous cost from input; it does not make their computation free.

Float source/working buffers retain precision through evaluation, existing-format editing and raw history spill. Portable raster storage remains byte/16-bit integer PNG; float/HDR raster save round trips are not enabled or claimed. Original encoded ImageStore files remain byte-preserved. The native float editing/handoff checks and the 16-bit PNG save/reopen checks verify these distinct contracts.

No language rewrite, graphics API replacement, user-project migration or automatic durable render-cache recording is introduced. Unrelated Blender extension/release work in the checkout is preserved.
