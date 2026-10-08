# Performance repair and detached-renderer integration

## Current integration status

R19 integrates the detached renderer, the accepted correctness and shutdown repairs, and **Resize Canvas** with **Top** or **Bottom** growth. Bottom remains the default. Top adds space above the comic by moving its artwork down by the added height. Root placements, document-space rigs and masks, and an explicit export crop move together; original source and mask pixels and sampling grids stay unchanged. Cancel applies no changes. Resize is one focused history command with Undo/Redo. Shrinking retains the existing behavior.

The tested freeze contains 296 runtime resources, 373 test files and four support files. The complete nonexternal Windows suite closed normally with **6,601 passed, zero failures/errors and 13 skips**. It collected 6,619 cases and selected 6,614 after five reviewed child/network deselections. The XML cases exactly match that selected set. Source, test and support inventories, actual imported origins, the seven-file fixture ledger and zero provider/child-attempt guards passed. The native process returned 0, with no deadline expiry and empty stderr after closure. Independent review passed all 26 checks. pytest.main took 2,973.657 s; process closure took 2,986.781 s. Actual application and screen DPR were 1.0. Font DPI96 was a root-declared launch control; the child did not record a font-DPI scalar.

The 13 skips require unavailable private, local or reference brush/SUT/CSP fixtures. The five deselections require an additional process or real TLS connection. The Blender-extension file, including its safe manifest check, was excluded entirely. These cases are outside the exercised suite. The focused ordinary Windows run separately passed **281 cases**, with zero failures/errors/skips, actual application and screen DPR1.5, normal process closure and empty stderr, in 104.762 s. The resize run passed **14 new cases plus the original focused-history case**, also at actual DPR1.5 with clean closure, in 6.420 s. The resize tests include meaningful complete native buffers for ordinary, affine, projective and singular-root placements, masked and unmasked, through Top growth and Undo/Redo, plus source/storage and save/load checks.

| Final release evidence | Result |
| --- | --- |
| Complete R19 suite and ordinary-density focused checks | Closed and independently verified as described above. |
| Final PRIVATE editor, ordinary Save and same-private cold reopen | Closed: ordinary R19 private editor PID11948 and fresh reopen PID15528 both returned 0, with empty late stderr, no timeout, dirty state false, passing frozen-import/provider guards and all 14,317 original project files unchanged. Physical operations were Resize Cancel, Top growth from 54,896 to 61,000, Undo/Redo, Blueprint Solo/navigation, translation and Uniform scale with Undo, Solo off, ordinary Save, cold reopen and ordinary close. The reopened status showed 1080 x 61000 and the shifted comic artwork was visible. Saved final and reopened initial/final normalized model SHA256 all equal 0369d5f90b5f52403e11733e0520403ab25b7e38b175a256e96ee6b998fb181a; the reopen changed no private project files. Density overrides were removed for ordinary Windows operation, but these actors did not record a DPR scalar. No exact per-action pixel or history reconstruction is claimed. The other five heavy targets, deeper-stack setup, mask stroke/cancel, compound and hierarchy cells were not repeated in this final session. |
| Build, commit and publication | Release delivery uses codex/refactor-integration and the tested R19 native sampler/build contract. The publication R2 auditor checks full current/frozen inventories, seven fixture transitions, eight fixed native/reference raw files, PE ABI/import/export metadata and the explicit Git pathset before commit. Independently verified commit/remote-ref receipts are retained locally after publication. The newer overnight-stable head f7340d13f6022ebfcf53b25e86500196818be58c is preserved. No private comic, capture or artifact is included in the release pathset. |

Accepted repairs preserve immutable source/color ownership, detached evaluation and focused history. They cover blur compatibility, source/transform signatures, group editing, mask Escape, current GPU framebuffer presentation and exact kernel reuse. Validated immutable pure-Raster-contact metadata permits reuse of unchanged native backgrounds; other live edits retain their private draft policy. Full contact checks run once per synchronous capture slice, with identity/revision checks at every record and ownership revalidation on the next slice. No capture deadline or native sampling budget increased. Graphics-resource and worker retirement now check actual C++ object lifetimes while retaining normal retirement and genuine valid-resource errors. The R19 complete and focused runs both closed cleanly.

Seven original test files have reviewed fixture transitions for ordinary deferred/event completion, CPU worker refinement/discard, current GPU-frame association, owned-window activation and the Resize dialog mock. All 533 original assertion ASTs remain; the other 331 incoming files retain their byte guards. Unit density controls are process-only. The separate ordinary-density runs leave production display scaling and font policy unchanged.

Heavy artwork still showed delayed appearance during the final Blueprint session, both before and after Top growth. It eventually rendered and changed under the tested transform, but this session does not establish immediate live updates or a whole-editor speedup. The late 55.663 s monitor capture contains 52 metric records, no timed phases, and mostly outer-event-loop Python samples. Its recorded revisions and presentation counters are constant. Its maximum recorded heartbeat gap was 158.981 ms. It began after the earlier blank interval and cannot measure that wait or attribute heavy kernel, worker or GPU costs; legacy zero counters do not establish absence of refactored worker activity.

The matched C2 result remains narrow: three alternating synthetic whole-kernel pairs preserved the complete output buffer and reduced median time from 14.290 s to 5.483 s (61.63%). This is not a whole-chapter or yesterday-to-today comparison. The separate bounded Smudge trace shows repeated identical semantic-key work under source/retained/effect cache pressure, including duplicate charges for effect-alias storage. The larger cache optimization is deferred from this release. Current and historical graphics drivers also differ, so unmatched historical editor observations cannot establish a speedup.

Exact artwork remains one sample per canvas pixel, preserving source/effect grids, color and Float precision. Only declared transient previews reduce working samples. Memory and disk share semantic keys, dependencies, epoch validation and the renderer; live/draft results never become durable exact entries. The investigation makes no Blender-side changes. Physical observations, native-buffer correctness, persistence and publication have separate evidence.

The sections below preserve the chronology. R14 had 74 failures; R16 had 14; R17 passed its assertions but had four later deleted-thread shutdown tracebacks. Their failures and pending requirements describe those historical snapshots and are not retrospectively relabeled as successful. Earlier six-target, deeper-stack, mask, hierarchy and compound checklists are prospective scope, not fresh R19 physical credit. The final physical row above is the authority for this release's actual coverage; wider responsive-input and heavy-cache work remains open where unmeasured or unperformed.

## Historical R17 integration status

This section records R17 before its shutdown repair. Its blocked/pending statements apply to that historical snapshot; current R19 release evidence appears above.

R17 assembles the detached-renderer integration and accepted correctness repairs in a frozen build containing 295 runtime resources, 371 test files and four support files. Its complete nonexternal Windows run finished with 6,581 passed, 13 skipped, zero assertion failures/errors and passing provenance guards. However, four post-pytest shutdown tracebacks attempted `GpuWorker.close()` on already-deleted `_GraphicsThread` wrappers. Clean release acceptance and publication remain blocked on that repair and fresh physical editor checks. The applied R17 source separately passed 261 focused checks at the normal Windows display scale (actual application and primary-screen DPR 1.5), with zero failures, errors or skips and clean shutdown. This report does not claim a clean complete R17 pass or a project-wide performance improvement.

The integration preserves immutable source ownership, detached scene evaluation and focused command history. Accepted repairs cover native blur compatibility, source/transform signatures, ordinary group editing, mask Escape routing, current GPU presentation and exact native kernel reuse. Pure brush contact carries validated immutable ownership metadata and reuses unchanged native background results; transform, mask and other live drafts keep their separate private policy. R17 validates the full contact classifier once per cooperative GUI capture slice, retaining identity/revision checks at every yielded record and revalidating ownership before each subsequent slice. This avoids repeated full-mode checks exhausting the existing 2 ms capture allowance. No capture deadline, timer, native grid or sampling budget changed.

R17 also gives the Posterize simplify checkbox its intrinsic clickable width rather than a stretched row whose center lies outside the native checkbox hit region. The original click/history tests remain unchanged. Graphics-helper shutdown now checks validity of each texture, buffer and vertex-array operand before destruction, in addition to the existing context/surface checks. Valid-resource errors still propagate, handle retirement and previous-context restoration remain in `finally`, and no broad exception handler was introduced. Two additive R17 test files exercise per-slice ownership and actual C++ resource deletion.

Six original test files have explicit reviewed fixture transitions. They wait for ordinary deferred Add, clipboard and owned-window completion, select the intended CPU-only worker-count lane, or associate a GPU capture with its actual current frame. All 404 original assertions in those files are retained, and the other 332 incoming test files retain their byte guards. Picture/layout/font unit controls use a declared nominal DPR1 / font DPI96 Windows test environment with strict actual-DPR metadata; native-density pixel controls and physical checks remain separate. These controls do not change production display scaling, font policy or native source sampling.

The earlier R14 broad run remains a failed historical result: 6,553 cases, 6,466 passed, 74 failed, 13 skipped and zero errors. R16 also failed: 6,581 cases, 6,554 passed, 14 failed, 13 skipped and zero errors in 2,733.058 s. Its 12 Raster-contact failures and two Posterize click failures were reproduced unchanged before the R17 repairs. All 14 original failed nodes passed in the R17 broad XML. The source/import/provider guards passed for these broad runs. Fresh reproductions and the explicit fixture ledger explain subsequent work; none retrospectively relabels earlier failures.

R17 collected 6,599 cases and ran 6,594 after five explicit child/network-process exclusions; the Blender-extension file remains wholly excluded, including its safe manifest check. The 13 skips require undistributed private/reference brush fixtures. The Windows unit lane used actual application/screen DPR 1.0 with the declared nominal font environment, took 2,736.153 s in pytest and 2,745.449 s through process closure, and ended with native return code 0. Actual imports, source/test/support bytes, the six-file fixture ledger and selected/excluded partition matched; provider and external-process attempts were zero. The wrapper's assertion/provenance success does not cover its four later deleted-thread shutdown errors, retained in `complete-nonexternal-integrated-r17-nominal1-r1/stderr.log` and the separate root closure record.

The R17 focused normal-scale run passed 261 cases in 68.269 s. These include the 180-case rendering/Text/brush-contact core, 68 original Raster-contact cases, three new capture-slice cases and ten actual graphics-resource deletion cases. A separate 195-case repaired-candidate run exercised Posterize, gradient controls and ordinary deferred Add alongside the contact cases; it passed in 25.110 s with empty stderr and no shutdown error. Candidate results supplement, rather than replace, verification of the applied R17 source. Provider attempts were zero and complete source/test/support guards passed.

Matched native C2 sampling evidence is limited to the synthetic whole-kernel workload: three alternating pairs preserved the complete output buffer and reduced median wall time from 14.290s to 5.483s (61.63%). This is not a measured whole-chapter, whole-project or yesterday-to-today speedup. Historical hardware, software-GL, instrumented GUI and heavy-object captures remain separately qualified below.

Exact artwork continues to use one sample per canvas pixel. Source/effect grids, color and Float precision contracts remain intact; only declared transient previews may reduce working samples. Memory and disk retain the shared semantic keys, dependencies, epoch validation and renderer, with live/draft results excluded from durable exact entries. Original project/source data remain protected, provider traffic is blocked, and this investigation makes no Blender-side changes.

The R17 acceptance plan at that point was to repair and verify the graphics-worker shutdown failure, then perform the fresh PRIVATE editor checklist on the final source: six saved heavy targets, meaningful mask cancel/Undo, Solo/navigation, visible compound subtraction/hierarchy, normal Save and same-private cold reopen. Flatten/deletion are outside this physical session. Prior physical sessions and actor results remain historical evidence, not fresh final-source physical coverage. Cancellation and further heavy-stack optimizations are prepared separately and are not part of R17. Keep the existing detailed chronology and all failed/prospective witnesses below this front section.


## Preserved builds and scope

The investigation published `overnight-stable` at checkpoint
`87463c8e16b51918a7be57aa01f374000c880790`. The remote branch has since advanced
to the user's `f7340d13f6022ebfcf53b25e86500196818be58c` Text change; that newer
remote head does not inherit a full-suite claim from the checkpoint. The
checkpoint preserves the tested V20 build on the original `0e1b0fc` base, without the
incoming renderer or unfinished font/group-transform candidates. Its frozen
regression run passed 6,509 tests, skipped 95, and had no failures/errors. All
258 runtime resources, 359 test files and two support modules were verified
before publication, including Git's text normalization. GitHub Desktop's
authenticated publication and an independent remote-ref read confirmed the
same commit. The original working checkout's 115 changed files, index and HEAD
remained unchanged. Its separate recovery copy includes those files, binary
patches and a verified Git history bundle.

Integration uses `codex/refactor-integration`, initially based on incoming
`6c0a7f1b5737639606cea93be8869f85b2f1a702`. The incoming refactor changes 210
files. A temporary three-way text comparison found 35 overlapping tracked
files, with conflicts in 25. No primary-checkout pull, stash, reset or merge
was performed. The published stable branch is a checkpoint, not completion of
the performance objective; its known group-transform and Text/DPR issues are
documented in that branch's `docs/overnight-stable.md`.

The test project remains `Pocket-Boyfriend Test Copy`, chapter
`0a72f08009294aa0a3d14e6a38e22bbb` (displayed as `Chaper 1 Rewritten`). Tests
must use isolated copies or immutable inputs. Original comic pixels/settings
must remain untouched by this investigation. Blender/provider calls are
excluded; saved 2D image publications may be read as ordinary embedded sources.
Incoming Blender-extension changes are upstream work, not investigation edits.

## What the incoming architecture changes

The shared scene kernels now run through `SceneSnapshotCompiler`,
`DetachedSceneBackend` and `SceneScheduler`. Widget paints present prepared
resources. Typed changes and focused history replace many broad invalidation
and inspector-refresh paths. These are the integration ownership boundaries;
restoring the old GUI evaluator and source-worker plumbing would defeat them.

The measured V20 software-GL first paint still spent 112.817 ms on the GUI
thread, including 32.356 ms in three selected warp stages and 13.120 ms in
effect planning across twelve stacks. Detached evaluation should remove this
direct GUI work, but the underlying kernels still cost CPU time. The incoming
complete-scene GPU path accepts a narrow single-object Legacy point/blur scene.
The multi-object Blueprint Deform/Twirl/Lens graph is outside that eligibility.
An input acknowledgment or an older prepared view does not prove live artwork.

The incoming implementation report's measurements use another computer
(Ryzen/RTX). They are background evidence, not this laptop's acceptance. Current
hardware reports Intel Iris Xe, DPR 1.5 and driver `32.0.101.7088`; historical
Intel comparisons used `32.0.101.7026`. Any claimed improvement needs a matched
pair on the current device, with instrumentation excluded from clean timing.

## Port decisions supported by the read-only audit

- Keep immutable scene/source/color ownership, shared worker admission,
  typed `ChangeSet` and `DocumentPatch` history, save coordination and the
  resident GPU resource API from the incoming architecture.
- Port the independently verified Pillow 12.3 blur capability gate. Preserve
  unknown-version fallback and validate its new resident-resource route.
- Use one fresh shared renderer epoch. Incoming `native-artwork-4` collides
  with the supplied project's existing exact disk cache. Memory and disk must
  retain the same keys, validation and pipeline; never record drafts.
- Add the complete effective fit-parent quad to the relocated image signature.
  Typed invalidation alone cannot make an incomplete retained-stage key safe.
- Retain compatible native Lens, oversized bilinear and regional Mesh kernel
  optimizations in unchanged modules, with original grids and byte oracles.
- Adapt real slider groove-click history, deferred Posterize Add and narrow
  inspector/style guards to the new focused history and scene-consumer owners.
- Repair group commits for Free Text and transformable primary Image/Raster.
  Incoming also requests nonexistent `Text.transform_frame` during a focused
  group snapshot. Current preview bounds must follow Text through modified
  ancestors. Keep genuine pointer, one-command, Undo/Redo and fresh-pixel proof.
- Validate explicit native font policy across DPR in the shared text kernel
  and dependency owner. Preserve historical mismatches and independent native
  references; the previously tested isolated proposal is not integrated yet.
- Compare Float export precision, storage and rounding explicitly. Incoming
  and V20 address low-alpha loss independently but differ at export. Do not
  treat similar purpose as byte equivalence or overwrite stronger ownership.
- Reintroduce compact previews only if new measurements require them, through
  the detached provisional consumer. Exact/native/output grids remain intact.

## Incoming acceptance scope (historical checklist)

This table preserves the acceptance scope defined during the incoming audit. Individual rows require their own evidence; the current R19 section above records completed checks and remaining limits. V20, incoming-only and older physical results do not establish fresh final-source coverage.

| Requirement | Evidence required on the combined build |
| --- | --- |
| Basic and heavy modifier edits | Real parameter edits, Add/remove/reorder/mute, large saved stacks, current live artwork and one-command history |
| Move, scale and warp | Actual press/move/release, both primary selection orders, current/fresh preview and settled native comparisons |
| Text and modified parents | Single/multiple Free Text, Strict Text selection policy, Outline/Mirror/Radial parents, type-correct focused history |
| Mask painting/editing | Opacity and parameter masks, active heavy stack, actual changed contact pixels, release, Undo/Redo and native restoration |
| Solo and hierarchy edits | Solo/un-solo, reparent between layers/children, world placement, mask/rig ownership, complete restoration |
| Shapes and compounds | Rectangle/circle/custom path editing, Boolean Add/Subtract, flatten and Undo/Redo with current rendered geometry |
| Whole-project scrubbing | Every chapter and the full tall rewritten chapter, Navigator cancellation/recovery, no stale publication or idle rerender storm |
| Cold and cache pressure | Original-source readiness/ownership, fixed exact grids, bounded caches, cancellation, eviction and context-loss recovery |
| Native precision and persistence | RGBA8/F16/F32/16-bit, ICC/color contexts, low alpha, original encoded bytes, shared exact/disk semantics and draft exclusion |
| Save and reopen | Real private-project Save after model and pixel edits, dirty revision correctness, source backing and clean reopen equality |
| Responsive input and live updates | Queue, handler, paint, first genuinely changed current artwork, sustained current updates and final settlement reported separately |
| Regression and provenance | Appropriate full suite and real GPU cases, actual imported origins, full source/test freeze, zero Blender/provider attempts |

Each performance capture must identify hardware/backend/DPR, enabled modifier
stack, viewport and input schedule. It must distinguish source coldness, cache
warmth, draft quality, native completion and observer cost. Physical
input-to-photon latency is not established by Qt paint timestamps.

## Integration evidence ledger

Results are retained under `.artifacts/refactor-integration-20261007`. Failed
assertions, mismatches and original references remain preserved. The entries below record each stage at its own date, including failed and prospective stages. Current R19 acceptance and remaining performance limits appear above.

- Incoming-only R0: 108 tests passed, zero skips/failures/errors, in 25.188 s
  of pytest time (26.136 s including the runner). Detached ownership, typed
  changes, tool sessions, native input, focused properties and color resources
  were exercised. Actual imported files matched the full 290-file source
  manifest; all 338 test files and source files remained unchanged. Provider
  attempts: zero. See `incoming-baseline-r0/pytest.xml` and terminal proof.
- Incoming-only Windows GPU R1: 33 passed and 20 failed, zero skips/errors,
  in 29.160 s. Blur-dependent segments returned no device result or performed
  no blur draws; the incoming Pillow 12.2-only capability gate excludes the
  installed Pillow 12.3. Original assertions and failure witnesses remain
  intact. Actual frozen source/test imports and zero-provider proof passed.
  This is a failed baseline, not acceptance. See `baseline-native-gpu-r1`.
- The first GPU launcher attempt failed before importing Qt or running tests
  because its preparation had pinned an unrelated, still-changing proposal
  script. The old plan was preserved and the pins narrowed to actual baseline
  dependencies; the successful preflight and subsequent failed GPU test run
  are separate evidence.
- A separate Qt-only probe completed with no editor imports: Intel Iris Xe,
  OpenGL 3.3 core, driver 32.0.101.7088, Windows backend, DPR 1.5 and 1280×800
  logical display. It does not establish input or artwork latency.
- Renderer-port Windows R2: all 53 original GPU checks passed after the
  Pillow compatibility port. The full selected run had 85 passes and 51
  failures, zero skips/errors. Fifty new resident-reference cases stopped at
  a test setup assumption that the worker's initially empty statistics already
  contained a readback counter; they did not exercise their pixel oracles.
  The other failure exposed a missing stable optimization: an empty disk index
  still serializes/hashes a descriptor. Both failures remain in the R2 XML.
  This full-source snapshot included ports 01–03 only, before interaction/fit
  changes. Its import/source/test/provider guards passed.
- Combined R3 resident/disk checks: 110 passed, zero skips/failures/errors,
  in 15.252 s of pytest time (15.985 s including the runner). All 48 added
  radius/shape/algorithm cases now exercised their complete float-bit oracle;
  both owned resident chains exercised no intermediate readback. Empty-index
  fast misses retain incoming sibling-clear synchronization and ordinary entry
  validation. Original disk-cache recording/reopen/cancellation tests passed.
- Combined R3 interactions: 271 passed and 48 failed, zero skips/errors,
  in 86.028 s. All 48 failures occurred before pixel comparison: Mirror/Radial
  preview helpers tried reading `_model_before` from `EvaluatedScene`. Ordinary
  commit/history, Outline previews, slider groove, deferred Posterize Add and
  the selected original property/control tests passed. This failure exposed a
  missing detached preview-cache context, not a glyph-buffer mismatch.
- Saved-chapter incoming Blue R1 failed an early harness check: the normal
  editor defers an interior press until movement exceeds four widget pixels.
  R2 preserved that transition and then reproduced the same real detached
  `_model_before` failure on the saved Blueprint stack. Both runs preserved all
  14,317 original project files and changed no private input files. R2 provides
  no finite first-current-artwork latency; no before/after live speedup can be
  calculated from a failed publication. R1's initial requested viewport settled
  in 18.452 s, separately from its press-only setup failure.

The two preview helpers now use immutable snapshot identity/configuration and
revision for detached cache contexts. Canvas keeps its existing gesture owner
and projection revision. Preview helper caches are evaluator-local and excluded
from native cache handoff. No command/history state enters a render snapshot.
The original 48 failing pixel assertions remain unchanged and are being rerun.
Port 06 retains original native bilinear tap coefficients and allocation grids,
with full-source fallback whenever a crop cannot preserve them. Outline/Radial,
font policy and old regional-Mesh scheduler proposals remain held.

- Combined R4 Windows native interaction/kernel run: 708 passed and six failed,
  zero skips/errors, in 250.394 s. All 114 actual group-transform cases passed,
  including all 48 previously failing Mirror/Radial current-artwork comparisons.
  Existing modifier-preview and the native sparse/oversized warp/kernel cases
  also passed. The six new fit-parent cases stopped before their edit oracle
  at a nonexistent `LayerNode.touch_revision()` fixture call. The fixture now
  publishes the incoming typed bound change and checks captured new geometry;
  its six cache/native oracles remain unchanged and await a fresh run. R4's
  complete 290-source/346-test snapshot and provider/import proofs are intact.
- Combined R5 native source/fit/pixel checks: 110 passed, zero skips/failures/
  errors, in 5.917 s of pytest time (6.638 s including the runner). The six
  fit-parent cases exercised real source/stage cache handoff, unchanged aligned
  bounds, changed visible artwork and complete cache-free native equality in
  RGBA8/F16/F32, including compound parent bounds. Sixteen detached Image/Raster
  object/layer cases verified complete low-alpha working buffers, independently
  calculated RGBA64 output, PNG round-trip, sRGB profiles and unchanged original
  sources. Existing pixel/ICC/HDR/precision edge checks also passed. Arbitrary
  profiles/color-resource ownership remain separate coverage. This 290-source/
  347-test snapshot and actual-import/provider guards passed.
- Saved-chapter combined Blue R3 progressed beyond the repaired ownership
  boundary, but first current artwork failed with `KeyError: 0`. Its original
  project and private input files remained unchanged. Diagnostic R4 captured
  the complete worker traceback: `effective_preview_modifier()` reads
  `cache[0]`, while incoming `EvaluatedScene` initializes that cache as an empty
  `OrderedDict`. Its existing contract is `None` or a tuple holding the
  ownership key and modifier LRU. This affects transformed object-owned rigs,
  which the passing parent-rig cases did not exercise. The observer restored
  the original publisher and recorded no observer errors; all 14,317 original
  project files and private inputs remained unchanged. R4 was an expected
  failed diagnostic, not a passing live-artwork timing. A narrow initializer
  repair initializes the evaluator-local cache to `None`, matching the existing
  tuple consumer; no cache/history handoff or exception suppression was added.
- Saved-chapter combined Blue R5, native PID 8860: ordinary posted press and
  threshold-crossing movement produced two current-geometry/current-revision
  previews with different complete image hashes on the same 656 x 1024 grid.
  Release settled the requested exact viewport and Undo restored the model.
  First current artwork took 4742.881 ms; the second took approximately
  5170.739 ms; release settlement took 20533.418 ms. Initial exact viewport
  settlement took 18472.463 ms. Pointer handlers took 1.299–3.367 ms and posted
  event queue delay 0.042–0.148 ms. The 20 ms diagnostic heartbeat had a
  22.056 ms p95 gap and 108.862 ms maximum. This is instrumented Windows
  OpenGL 4.6/DPR 1.5 evidence from one saved four-active/six-total Blueprint
  run, not a matched historical speed comparison or acceptable performance.
  Source/provider/import/private-project checks passed, all 14,317 original
  files were unchanged and private input files were not saved or modified.
- Combined R7 object-owned rig/native checks: 52 passed and two failed out of
  54 tests, zero skips/errors. Translation/scaling and Deform/Twirl free-warp
  cases passed. Free-warp Mirror and Radial live pixels differ from an
  independently cold committed model (651 and 801 channel bytes respectively).
  The failing native full-buffer assertions are preserved for diagnosis.
  R6/R7 runtime bytes are identical; R7 corrects a new test fixture's mistaken
  assumption that `set_selection()` returns a boolean. The full original 338
  incoming test files remain byte-identical. Actual import/source/test/provider
  proofs passed, with zero provider entries.
- Ordinary full-editor Computer Use R2, native PID 8004: physical input selected
  the saved Blueprint, navigated the middle of the chapter, soloed it, moved it,
  undid the move, changed Twirl Angle from 209.93 to -43.45, undid that change,
  and restored the complete chapter view. Handles and controls changed before
  the heavy artwork caught up. The ordinary Performance Monitor recorded this
  mixed session, including UI selection and navigation; its inclusive nested
  phase times are not a matched gesture benchmark or GPU utilization capture.
  Monitoring was stopped before ordinary Save. The tab's dirty marker cleared,
  and ordinary close completed with native exit 0 and an empty issue list.
  Initial/final model hashes are identical; terminal dirty state is false.
  All 14,317 original project files, frozen runtime/tests, original settings
  and brush assets passed the preservation checks. The Blender client remained
  disconnected, its timeout inactive, and provider attempts were zero.
  Evidence: private-editor-combined-r6-cu-r2. A fresh ordinary MainWindow reopen
  (native PID 9900) loaded that same private chapter and showed its artwork.
  Its initial and final model hashes match the saved original model hash;
  ordinary close exited 0 with all preservation/provider checks passing and
  no changed private files. This proves Save/reopen of the restored model;
  persistence of a retained artwork edit is still separate pending coverage.
  Evidence: private-editor-combined-r6-cu-reopen-r1.
- Worker causal R1 preserved five real worker wall/thread-CPU envelopes, but
  its function-level cProfile capture is rejected for causal attribution:
  installed Python 3.14 cProfile callbacks also recorded GUI-thread activity.
  A separate two-thread control reproduced 25 GUI-only calls in that profile;
  a worker-local sys.setprofile control recorded none. No production optimization
  is justified by the mixed function timings. R1 artifacts remain intact;
  an additive thread-qualified, cleanup-safe diagnostic is being run separately.
- R7 free-warp bounds diagnostic reproduced the same two unchanged full-buffer
  failures, with no observer-induced rendering or altered test assertions.
  Actual import/source/test/provider checks passed and provider entries were zero.
  This diagnostic is an expected failed baseline, not acceptance.
- The stopped full-editor monitor capture was copied byte-identically with
  the native terminal proofs. Over its mixed 438.220 s recording there were
  four GUI heartbeat gaps of at least 250 ms, maximum 334.760 ms. Distort's
  1,307 calls totaled 46.029 s inclusive. Layer/object signatures were also
  substantial, but retained events cover only the final 13.287 s after 29,245
  earlier events were dropped. The retained Distort calls run on worker 6768;
  GUI samples cannot identify worker internals. Pending projection state at
  monitoring stop does not prove exact settlement. Evidence:
  private-mainwindow-monitor-r6-cu-r2-stopped-r1/extraction.md.
- Worker-local causal R3 recorded only actual worker callbacks (TID 6496),
  with zero GUI callbacks, no callback errors and restored methods/profiles.
  Its terminal evidence is nevertheless rejected: every evaluation exceeded
  the 128-category extent-metadata bound. The per-event observer also added
  substantial work, so these timings are not clean performance acceptance.
  An additive coarse original-method observer is being prepared; R3 remains
  unchanged. Evidence: blue-worker-causal-auto-r3.
- Saved Blueprint scale/free-warp R1 reached the four-active scale release,
  then failed before its independent cold native comparison. The detached
  evaluator's GUI reference path called `gpu_pattern_effects.renderer_for`,
  which assumes its owner has a QWidget `destroyed` signal. `EvaluatedScene`
  has no such signal. The native full-buffer assertions were not reached;
  this is a genuine detached lifecycle failure, not a pixel mismatch. Actual
  source/import/provider proofs passed, the original project and private input
  files remained unchanged, and native PID 11152 exited 1 after 64.158 s.
  Evidence: blue-scale-warp-auto-r1.
- Combined R8 owned-rig/group checks: 191 passed and four failed out of 195,
  zero skips/errors, pytest time 289.695 s. All original 12 object-owned rig
  cases, including both previously mismatching free warps, now match their
  complete independent cold native buffers. Affine parents, projective scale/
  warp and all three exact bounds contracts passed, as did all 114 existing
  group/Text cases and selected original detached tests. Four newly added
  projective-parent translations returned the stored rig before pixel comparison;
  their fixture/ownership policy is being checked without weakening the original
  native assertions. Full frozen source/test/import/provider proofs passed.
- Combined R9 detached GPU-helper lifecycle checks: all 12 passed, no skips/
  failures/errors, runner time 3.834 s. The real non-QObject evaluator now owns
  and explicitly retires its locally created pattern/texture helpers, preserving
  QWidget destruction connections and borrowed graphics-worker ownership.
  Native pattern and cage output buffers match independent fresh helpers;
  floating fallbacks and failure/idempotent cleanup checks passed. Provider
  attempts were zero and full source/test/import proofs passed.
- Combined R10 owned-rig checks: all 39 passed, no skips/failures/errors,
  runner time 47.618 s. The four formerly failing projective translations now
  transform their owned preview rigs exactly like commit, preserving the exact
  affine translation path. Their native full-buffer/one-command/Undo/Redo/source
  preservation assertions are unchanged. All original and expanded parent cases
  and exact bounds contracts passed with full provenance/provider checks.
- Saved Blueprint R2 on frozen R9 completed all four scale/warp operations
  using ordinary posted pointer handlers and production mute buttons. All eight
  live positions changed actual current geometry/artwork; each last live buffer
  equals the independent cold committed native reference. Releases made one own
  command and Undo restored each configured model and prior command identities.
  Both mute commands were restored at the end, matching the initial model hash.
  Source/provider checks passed; all 14,317 original files and private input
  files remained unchanged. This is the real production Canvas/SceneController/
  ModifierControls actor, not ordinary MainWindow or physical Computer Use.
  It completed in 687.262 s; exact settlement covers the requested viewport.

  | Active stack / gesture | First current artwork ms | Release exact viewport ms |
  |---|---:|---:|
  | Saved four / scale | 5525.562 | 20964.458 |
  | Saved four / free warp | 6948.653 | 20656.884 |
  | All six / scale | 15396.935 | 82501.630 |
  | All six / free warp | 15611.439 | 86834.923 |

  These are functional passes and unacceptable latency baselines, not historical
  speedup or responsiveness acceptance. Untimed independent reference evaluation
  can block the diagnostic GUI and must be excluded from input/heartbeat claims.
  A passive process snapshot during the run showed approximately one core of
  aggregate CPU use and 2.2 GB working set/3.5 GB private memory; those are process
  observations, not worker attribution or GPU utilization. Evidence:
  blue-scale-warp-auto-r2-r9.

- Saved-four coarse worker observer R5 passed both the original native actor
  and independent observer guard. All five evaluations ran on actual worker
  TID 2164; original calls completed once, methods were restored, thread
  profiling stayed unchanged and no pixel data was retained. First/second
  current translation evaluations took 5.461/5.624 s wall and 5.141/5.391 s
  worker CPU. Release took 19.605 s wall and 18.828 s worker CPU. On release,
  623 distortion calls took 12.679 s inclusive, with sampler 5.124 s, MLS
  2.225 s and triangle mapping 1.155 s nested within them. Source lookups hit
  26/26 and preparation lookups hit 660/678; finished modifier lookups hit
  only 3/2,260. These actual lookup returns identify poor reuse of rendered
  work while source/preparation reuse is already effective. They do not prove
  that arbitrary sampled extents have identical inputs. All clocks are
  inclusive, overlap and include observer overhead; this is causal evidence,
  not clean performance acceptance. First-32 extent samples are explicitly
  partial. Evidence: blue-worker-causal-auto-r5/worker-causal-r5/
  worker-causal-summary.json; independent report SHA
  96408bf2c87e773f8010066c4bc523241311881cc4d728b856b221040c69fc41.

- Physical Computer Use on ordinary private MainWindow / frozen R10 retained
  a new Rectangle compound with Add Rectangle and Subtract Circle children,
  resized the Circle, and exercised its own Undo/Redo. Creating the shapes near
  document y=90 under the Son page legitimately clipped them: Son starts near
  y=13,543. Enabling Ignore direct parent mask displayed the actual compound
  and Subtract cut immediately. Two physical outliner reparent drags did not
  change the parent; reparent acceptance remains open. This was not an artwork
  rendering failure. On Blueprint's saved four-active/six-total stack, physical
  inputs soloed the object, painted two mask strokes, created a linear gradient,
  moved its endpoint with Undo/Redo, and saved the mask with a private name.
  A corrected drag on the narrow opacity endpoint changed actual visible
  artwork and persisted white=0.3523809523809469. The first endpoint drag
  missed the track and is not an accepted input-delay failure. Ctrl+S cleared
  dirty state and ordinary close exited 0, with full source/provider/original
  project guards passing. Initial model 5d9a2dcf39b06897c57a976dd579ace12cc5da95eb64a2b1ee842fbb9ac8ddaf
  became f67c6f44b89911a3a75d8749aef8a918ed73ceb7524ce28fbab420553e43c31f;
  saved chapter raw SHA b2307d8e99009b102971f7ace85acf105e69082e000aadd75605525b732c40b1.
  This proves retained physical edits and visible changed artwork, but the
  already-closed pre-Save framebuffer was not captured for a full-buffer cold
  equality assertion. Independent saved-artifact native verification is pending.
  Evidence: private-editor-physical-basic-r10-r1 (952.765 s session, native
  PID 13224); its mixed navigation/wait/edit duration is not a speed benchmark.
  The paint strokes advanced the mask revision, but the private before/after
  manifests show only its tile index changed, with no paint PNG byte changes:
  positive paint over existing white coverage is not a meaningful changed-paint
  pixel witness. A subsequent private clone must test a different Pencil alpha
  and retain changed mask source pixels. The gradient and opacity binding edits
  above are distinct retained edits and did change visible artwork.
- A fresh ordinary private MainWindow cold reopen retained the exact edited
  model f67c6f44b89911a3a75d8749aef8a918ed73ceb7524ce28fbab420553e43c31f
  at both startup and terminal. Physical inspection displayed the new compound
  with its cut in the first panel, selected Blueprint, and showed the named mask
  tile and saved white endpoint 35.238. Normal close exited 0 with dirty=false,
  no recovery dialog, all 14,317 original files preserved and full source/
  provider proof passing. Independent saved-artifact native full-buffer checks
  still remain separate. Evidence: private-editor-private-editor-physical-basic-r10-reopen-r1
  (native PID 2884, 57.757 s ordinary inspection session).
- Current-font native probe R10 stopped before all source/font pixel checks:
  actual Windows QScreen DPR was 1.5 while its first child required 1.0.
  Original strict actual-DPR assertion remains unchanged. Exit 1 is preserved;
  this is neither a font mismatch nor a font policy validation. The next probe
  must calibrate child-only environment scaling and retain strict actual-DPR
  checks. No OS scaling or application hinting policy was changed. Evidence:
  native-font-current-r10-r1.
- Combined R11 storage-accounting and canonical-cache checks: all 61 passed
  (31 new storage cases plus 30 cache identity cases), no skips/failures/errors,
  Windows-native runner time 5.021 s. Distinct QImage storage is charged once
  across COW aliases; adoption reconstructs the actual ledger. The existing
  64 MiB/512-entry limits, exact keys, oversized-result behavior and common
  disk pipeline remain unchanged. Full 290-source/350-test frozen provenance
  and provider checks passed. This proves bookkeeping and ownership behavior,
  not a heavy-stack speedup; matched native performance checks remain pending.
  Evidence: inline-storage-r11-native-r1.
- Preserved isolated sampling evidence is copied byte-for-byte into the
  integration artifacts, with provenance in historical-sampling-evidence-copies-r1.json.
  historical-oversized-bilinear-real-native-v11.json (SHA
  e17ff9bcca4ac0c8e281f34838065bf422fdf730e0a25925bb37884931f4f35a)
  compares six native 512-pixel targets from an actual captured 5,383-square
  stage: original median 1.475 s and eligible crop variant 0.641 s, with every
  native output hash equal and the 256 MiB gate unchanged. Normalized pixels
  fall from 173,860,134 to 1,939,062; OS peak memory was not measured.
  historical-sparse-lens-real-native-v14.json (SHA
  a8a84827b895c99a1e964aaff8ed6728282b08ac54e9795d188ece86924b9c02)
  compares the complete actual native Lens input/output: old median 12.429 s
  and eligible constant-only-tap variant 11.106 s across three interleaved
  cold-preparation passes each, with every native byte equal and source grid/
  output extent unchanged. These historical isolated kernel experiments
  support the sampling preparation shortcuts only. They do not measure current
  integrated worker/editor speed and do not resolve the heavy-stack latency.
- All-six coarse observer R3 on immutable R10 completed the four original
  gestures, all eight current-artwork/cold-reference witnesses, both real mute
  commands and complete model/history restoration. Native PID 9888 exited 1
  after 745.558 s because the observer rejected its own metadata budget:
  cumulative processed key bytes reached 1,221,338,562 above its fixed
  1,073,741,824 limit. All 10 observed original evaluations completed once on
  worker TID 12708; global method restoration/profile/observer checks passed, and semantic groups
  stayed below their 32,768 cap (18,657). Original 14,317 files and all private
  inputs remained unchanged. The complete telemetry gate failed, so its cache
  metadata and method clocks cannot justify an optimization or speed claim.
  Independent guard also rejected the capture; report SHA
  593d3ffbd0c503b0c288ee3b2cf2fe40392396d45f648db8aa59a983f030cf8e.
  Preserve this experiment unchanged. A subsequent observer must bound actual
  metadata/serialization work with immutable typed-key digest reuse, not simply
  raise or reset the failed counter. The first limit failure occurred in
  evaluation 7 / serial 21, with a secondary local metadata context-restoration
  error; evaluations 8 through 10 have no complete semantic groups. The wrapper
  elapsed 752.314 s, distinct from the actor duration. Evidence:
  blue-all-six-worker-causal-auto-r3-r10.
- Calibrated current-font R11 probe ran one separate editor-free screen
  calibration process (native DPR 1.5), followed by seven isolated original
  font/native-scene children at strictly asserted actual DPR 1.0 and 1.5.
  Each child exited 0 and source/import/provider proof passed. Final comparison
  rejected all 81 current cross-DPR buffers (47 saved Text sources, 18 styled
  Unicode/precision cases and 16 fixed full-model native blocks); all 16
  same-DPR cold native comparisons were equal. Some layouts also changed
  document height, and glyph runs/editor carets differ. This is a genuine
  display-dependent text issue under the unchanged default hinting policy,
  not the earlier environment precheck failure. Actual Segoe UI Emoji color
  fonts were exercised; requested MingLiU resolved to installed fallback fonts,
  so actual MingLiU coverage is not claimed. Existing historical buffers are
  preserved, optional historical comparisons were unequal, and no NoHinting
  candidate was applied. A fix must pass fresh complete native/layout/caret/
  precision comparisons before changing production font policy. Evidence:
  native-font-current-r11-r2/native-proof.json and dpr-calibration.json.
- R11 cross-pipeline cache checks: all 99 passed, no skips/failures/errors,
  runner time 12.783 s. The five original test families exercise detached
  snapshots, stage dependency reuse, exact reference retention, shared effect
  prefixes and GUI worker retention. Full frozen source/test/import proof and
  provider isolation passed (native PID 15496 closed). Evidence:
  inline-storage-cross-pipeline-r11-native-r1.
- Matched R10 control using the original Blue scale/warp R2 actor, with no
  additional coarse causal observer: all four gestures, eight current-artwork
  witnesses, independent cold committed full-buffer comparisons, one-command
  release and Undo/history checks passed. Native PID 16592 exited 0 after
  695.331 s; original/private inputs, full frozen provenance and provider
  isolation passed. Saved-four scale first/current publication was 6.268 s
  and released settlement 21.952 s; warp 5.537/21.046 s. All-six scale was
  14.945/83.622 s and warp 15.486/94.278 s. These are unacceptable delays.
  The fixture still includes current-artwork readbacks and untimed independent
  cold-reference work, so it is a matched instrumented actor comparison,
  not an uninstrumented physical MainWindow timing. R11's identical actor is
  completed before further source changes. Evidence:
  blue-scale-warp-clean-control-r10-r1.
- Matched R11 after accounting: all original functional/native/history guards
  passed (native PID 6712, 611.869 s). The eight actual current-artwork buffer
  hashes are identical across R10/R11, input inventories match, the actor policy
  is unchanged, and the only frozen runtime difference is render/scene.py.
  Saved-four scale first/current publication was 5.290 s, release 19.299 s;
  warp 6.060/17.928 s. All-six scale was 15.866/67.714 s and warp
  20.628/75.938 s. Released settlement improves 12.1/14.8% for saved-four
  and 19.0/19.5% for all-six in this one sequential matched pair. First live
  publication is mixed and remains unacceptably slow, including a slower
  all-six warp; this is not a responsiveness acceptance or a statistical/
  historical hardware-matched speedup. Original artwork grids, limits and
  source/style/geometry/history remain equal. Evidence:
  blue-scale-warp-clean-after-r11-r1 and blue-accounting-matched-pair-r1.json.
- Isolated font-design R11/R3 candidate: seven fresh native children exited 0;
  all 81 actual-DPR 1.0/1.5 native buffers and layout/glyph/caret metadata
  compare equal, and all 16 same-DPR cold native blocks compare equal. This
  includes the 47 saved Text sources and 18 styled Unicode/ARGB32/F16/F32
  cases; original source/style/model records and native grids are preserved.
  The candidate makes two shared explicit NoHinting changes and advances the
  common cache epoch; production is still unchanged. Actual Segoe UI Emoji
  color tables were exercised, with MingLiU fallback qualified as above.
  Retained historical NoHinting source/layout comparisons match, but the three
  older native scene patches remain unequal and are preserved without rewriting
  expected buffers. This is a font determinism correction on the same native
  grid, not evidence of a speedup or a complete chapter export. Matched text
  timing and affected original regressions remain required before production
  integration. Evidence: native-font-design-r11-r3/native-proof.json (SHA
  8f0f953fe40cc959853e91c7583da65b7a18f1c9b711bda263a029afcf1361f9).
- Matched text timing R2's four source children completed, but its final
  provenance/metadata comparison failed. Independent audit found complete
  native pixels equal within policy; failures were an unimported provider
  incorrectly required to be wrapped and an unordered raw-font inventory.
  Preserve the failed run. Prospective R3 records the installed lazy provider
  finder and absent-or-wrapped state, and canonicalizes only the full font
  inventory while retaining multiplicity and strict positioned glyph, layout,
  caret and full native-byte comparisons. All original timed-method AST and
  assertions remain unchanged; 21 independent guard controls passed.
  Root preflight and four fresh sequential ABBA Windows/DPR1.5 source children
  then passed all 65 cases and all 130 within-policy native comparisons.
  This is inclusive CPU/source timing, with metadata and hashes outside the
  clocks, not whole-scene GPU/UI latency. Production font policy is unchanged
  until affected original and additive regressions pass. Evidence:
  native-text-matched-performance-r3/terminal-summary.json; failed R2 and its
  additive audit are retained unchanged.
- R3 font micro-workload has no demonstrated speed win. Across 65 first-case
  source draws, baseline ABBA totals were 328.07/248.66 ms and candidate
  293.41/282.19 ms, similar means 288.36/287.80 ms. Summed case warm medians
  were 38.83/35.21 ms versus 44.75/40.15 ms; median paired case ratio was
  1.056, with candidate lower in 22 of 65 cases. Thread CPU clocks are
  quantized at 15.625 ms, so a zero sub-millisecond reading does not mean no
  CPU work. All 65 baseline/candidate layouts match, but native appearance
  changes in 35 of 47 saved and 15 of 18 styled sources. Treat NoHinting as
  an explicit deterministic font appearance policy change, not pixel identity
  with the previous display-dependent default or a performance optimization.
- Saved private native R1 stopped before rendering: actual saved-retained
  PID 11000 exited 1 because the fixture referenced the obsolete binding field
  `white` instead of the incoming model's `white_value`. Provider wrapping,
  isolation and actual source origin checks passed. No complete ROI, saved-cold
  or baseline-cold native comparison is credited. A prospective additive actor
  correction must preserve the exact saved opacity assertion and every native
  comparison; the failed R1 remains immutable. Evidence:
  private-saved-native-r10-r1/saved-retained/results.json and terminal-proof.json.
- Isolated Legacy live-canvas operator proposal R2 was frozen from held R11,
  preserving all 350 original/current test files and four support files.
  Root Windows/PID 9004 run: 34 passed, 9 failed in 156.642 s; complete actual
  source/test provenance and zero provider attempts passed. Four support files
  remained equal after execution. Six Mesh cases fail before rendering because
  the proposed fixture retains four Deform source handles while declaring nine
  Mesh destination handles. Two radial cases fail to publish matching current
  artwork within 25 seconds; the independent original-pose reference succeeds,
  and no controller error is reported. Outline takes the full 690x562 stack
  without the promised cancellation token instead of the bounded operator
  route. These failures are retained and reject production adoption. Correct
  the fixture and actual runtime paths in a prospective isolated revision,
  preserving current/cold full-buffer, exact release and history assertions.
  The candidate's other cases include current Deform/Twirl transforms, generic
  Blur/Kuwahara, Float native fallback and cancellation of an obsolete running
  kernel. No saved-heavy responsiveness or earliest-release latency is credited.
  Evidence: live-canvas-policy-windows-r2/pytest.xml and terminal-proof.json.
- The accepted all-six R5 causal capture identifies separate live and exact
  costs. All four contact evaluations contain zero repeated same-semantic graph
  keys. Exact scale/warp release invokes six region renders, with 529/576 Lens
  keys computed six times each (3,174/3,456 Lens computations). Inclusive Lens
  time is 45.982/49.100 s. This capture uses frozen R10's per-record accounting
  and includes observer overhead; it is not R11 clean performance. It records
  neither individual eviction residence nor an independent frame-allocation
  clock. Source fetching is under 2 ms in the reported release, while complete
  upstream effect frames dominate. Exact Pinch tap-dependency pruning is a
  prospective investigation, not an implemented improvement. Evidence:
  all-six-causal-r5-analysis-r1.md and its scalar JSON (SHA
  04077aadd81a462b7cf21c64148709ef5f8cf446757712c50d5790153a9c0b77).
- The isolated font proposal's 22 additive native tests pass, but its unchanged
  affected text group fails: 265 passed, 22 failed, zero skipped/errors, native
  PID 2880, 330.069 s. Two outline tests fail editing/resting visibility; twenty
  MainWindow cases update model text but show identical captured artwork after
  typing or backspace. These are visible editor requirements, not historical
  glyph-buffer expectations. Preserve this failed run and compare the exact 22
  original cases against unchanged R11 before assigning causality. Production
  font policy remains unchanged. Evidence:
  font-policy-text-affected-r11-r1/stdout.log and its original pytest XML.
- A second ordinary physical private-editor session successfully reparents
  Layer52 from son to Page0, then Undo/Redo restores/reapplies that hierarchy.
  Actual pencil input changes mask source tiles 1_73.png and 2_73.png. Ordinary
  Save and close finish cleanly; loaded model changes from f67c6f44 to e3662e4f,
  with the reparent as its only model delta. The Blue opacity binding, gradient,
  stored six modifiers and original project remain unchanged. Mask Undo/Redo
  is not credited because an owned monitor dialog made the UIA indices
  ambiguous. Fresh saved-native ROI/source comparisons remain pending; no
  closed-editor pre-Save framebuffer equality is reconstructed. Evidence:
  private-editor-outliner-reparent-physical-r10-r1/terminal-proof.json.
- That physical session's built-in monitor capture records one 396.054 ms
  heartbeat gap with a sampled stack in disk_cache.tick, projection_key and
  _modifier_entity_settings/deepcopy. Final revision/presented revision are both
  17 with no pending work. Its 266.843 s inclusive, mixed-thread capture drops
  21,546 timeline events and contains only one input sample, so it is a path to
  investigate, not a clean first-current or input-latency measurement. Evidence:
  physical-mask-reparent-monitor-r1 (copied capture bytes SHA prefix 9640aa).
- Unchanged R11 reproduces precisely the same 22 text assertion failures in a
  fresh Windows/PID 8268 run (36.328 s, zero skipped/errors). This rejects a
  causal claim that NoHinting introduced them. Preserve both failed runs. The
  fixture drives QWidget.render before canvas.grab; a separate actual GL
  paint/frameSwap/framebuffer diagnostic is required to distinguish a test
  capture problem from visible editor behavior. Evidence:
  font-visible-failures-baseline-comparison-r1.json and
  font-failed-baseline-r11-r1/terminal-proof.json.
- Isolated live-canvas R4 passes all 60 original proposed gates on Windows
  (PID 1952, 258.628 s, zero skipped/errors/failures/provider attempts). Every
  held R11 test byte and all four support files remain equal. Tests include
  current/cold draft buffers, exact commit/Undo, running-kernel cancellation,
  unchanged default native radial bits and current radial remixes. The actual
  saved-heavy first-current and earliest-release timing pair remains pending,
  so this is correctness acceptance of an isolated proposal, not a speed claim
  or production adoption. Evidence: live-canvas-policy-windows-r4/pytest.xml,
  terminal-proof.json and independent-terminal-audit-r1.json.
- Saved physical state now passes retained versus independently cold complete
  native ROI verification in three fresh processes (13184, 9360, 3916). All
  5,529,600 Blueprint bytes match after reopening; 2,364 pixels/7,947 bytes
  change relative to the prior state. Actual mask decoding changes 920/2,113
  pixels in the two painted tiles. Compound ROI's 460,800 bytes match across
  retained, cold and prior states; its unchanged pixels are observed, not
  assumed. Stored binding/source/ICC/model/original-project guards pass. This
  proves saved native results, not a pre-Save framebuffer or exclusive paint
  causality. Evidence: physical-mask-reparent-native-r10-r2/terminal-proof.json
  (SHA f622ad25d67a9ac775cae264637dd5d060cfa1a207616e20cda4065a1094614a)
  and physical-saved-native-r5-independent-buffer-audit-r2.json.
- Pinch census R2 failed before rendering because the observer checked an
  asynchronously initializing graphics owner immediately. Preserve this
  failure and give it no native credit. Prospective R3 waits for the original
  owner's readiness during setup; observed PID15100 and unobserved cold PID7944
  then pass complete 1080x1280 native buffers and original source/ICC equality.
  All 49 observed Pinch stages use one 5383x5383 full upstream frame. Their
  conservative tap/base dependency union needs 64 of its 506 global tiles,
  covering 14.475% of its native area. This supports investigating exact ROI
  pruning, but is not crop-sampler equivalence or an implemented speedup.
  Evidence: blue-pinch-single-r11-r3/terminal-proof.json (SHA
  a161da89c2ec3892922421945d39c4f5066f8fc1abf2a0e475910d701887fd61)
  and pinch-single-census-r3-address-analysis-r1.json.
- Ordinary physical whole-chapter navigation R1 fails complete coverage: only
  three of six ordered checkpoints receive an exact current presentation. At
  the 60% stop, the actual demand has 40 required native tiles but only 35 ready
  after roughly 46 s; it remains busy/pending and is superseded by the next
  physical navigation. A complete-looking screenshot does not pass this gate.
  Individual 20%/40% demands take approximately 11.696/8.094 s to first exact
  presentation in this instrumented session. The startup top checkpoint also
  precedes physical input, so R2 adds that requirement prospectively and a
  separate low-rate diagnostic progress file. R1's failed coverage, raw
  observer and source/model/original/provider guards are preserved. This is
  neither clean latency nor an all-chapter native export comparison. Evidence:
  private-editor-navigation-nav11-r1/navigation-observer.json and its terminal
  guard; navigation-progress-r2-protocol.md describes the unrun repeat.
- Heavy Blueprint live-draft R4 baseline completes all four scale/free-warp
  operations in native PID15116, then fails its final earliest-release gate
  (943.118 s functional run). Every current/cold draft, committed/Undo native
  buffer, actual history command and complete model restoration check passes;
  original/private files and provider guards remain unchanged. All four first
  release swaps paint successfully at approximately 49-63 ms but have no
  current preview and retain an older presented revision while exact work is
  pending. First live frames take 5.242/6.152 s for the saved four effects and
  16.435/17.375 s for all six; exact settlement takes 19.139/20.002 and
  77.127/77.982 s. These are instrumented functional observations, not a clean
  matched speed ratio. Preserve the failed terminal and original strict gate.
  The stale release states support investigating a guarded, presentation-only
  handoff of the final current draft; no exact or durable cache adoption is
  permitted. Evidence: blue-live-draft-control-r11-r4/results.json and
  terminal-proof.json; release-presentation-mechanism-r11-static-r1.md.
- The isolated reduced-preview R4 heavy actor completes the same four real
  operations and all current/cold draft, committed/Undo native, history and
  source guards, then fails the same earliest-release assertion (PID17124,
  855.640 s). First live observations are 1.502/1.340/1.442/1.734 s, still too
  slow for responsive editing. Exact release settlement is 18.870/19.191 and
  70.969/114.831 s; the last operation is slower than the baseline. Both actors
  lack an accepted clean timing pair. Independent comparison passes 70 native
  compatibility checks, including all eight complete committed/Undo buffers
  across builds, while preserving both full-actor failures. Evidence:
  blue-live-draft-candidate-r4-r1/results.json and
  blue-live-draft-native-crossbuild-r4-independent-r2.json.
- Exact Pinch ROI proposal R2 passes 92 of 97 native cases and fails five strict
  pruning/reuse assertions. Pixel checks preceding the four pruning failures
  pass. The 768-pixel fixture needs every upstream tile, so pruning cannot
  reduce its Lens work; the disk fixture never records the exact final output
  whose reuse it requires. These are failed preconditions, not permission to
  weaken assertions or adopt the runtime proposal. Additive corrected fixtures
  and original full-coverage controls remain prospective. Evidence:
  native-pinch-roi-proposed-r2/junit.xml and its terminal proof.
- The isolated disk-status proposal passes 30 of 33 native cases, with three
  failures in full-model equality after mask cancellation. No cancellation
  difference is classified as permissible metadata: ordinary Escape explicitly
  restores the serialized mask revision. An unchanged-R11 cancellation
  diagnostic must record the actual differing fields, complete native buffers
  and history before assigning cause. All four support files remain unchanged.
  Evidence: disk-live-status-windows-r1/junit.xml and
  four-support-after-native-root.json.
- Navigation R1's monitor contains one 405.771 ms GUI heartbeat gap at the 20%
  stop, not at 60%. Its sampled timer-start stack does not establish the cause:
  observed SceneController polling is at most 0.377 ms and painting 56.231 ms,
  and surrounding phase events are missing from the retained timeline. Worker
  CPU overlaps the slow 60% demand, but this neither proves one object's
  exclusive cost nor proves GIL contention. R5 prospectively adds bounded
  wall/thread-CPU envelopes around the original GUI and worker calls without
  changing rendering, input, cache admission or the six ordered coverage gates.
  Evidence: physical-navigation-monitor-r1/analysis.md,
  physical-navigation-stall-r1/analysis.md and navigation-gui-envelope-r5-ready.json.
- Navigation R5 completes all six ordered physical checkpoints and closes
  normally (PID9392, native/wrapper exit zero). All 42 terminal guard checks
  pass, including original/private source and model/history preservation,
  provider exclusion and unconditional envelope restoration. Instrumented
  first-exact-current observations are approximately 12.496 s at 20%, 7.899 s
  at 40%, 59.093 s at the heavy 60% stop, 0.406 s at 80% and 0.293 s at the
  bottom. This is demanded-tile readiness with matching paint/swap metadata,
  not independent pixels for the whole chapter or a clean input-latency test.
  Evidence: private-editor-navigation-nav11-r5/navigation-observer.json and
  terminal proof SHA39337e5f3f17cfb684df68c9147e80e795d180218fe6c42fbd74ad41951866c6.
- Unchanged R11 reproduces the mask cancellation model difference in all six
  diagnostic cases (empty/seeded native mask with ordinary, maintenance or
  scheduler activity). The precise difference is /masks/0/revision, from zero
  to two after Escape. Each case records native/history evidence before the
  unchanged hard model assertion fails. This localizes a baseline cancellation
  defect; it does not justify allowing the difference in candidate tests.
  Evidence: mask-cancel-baseline-r1-wrapper-proof.json and the six native
  diagnostic.json files under mask-cancel-baseline-r1-native-evidence.
- The independent text capture diagnostic retains all 22 original failures
  and passes 17 evidence-integrity checks. The 20 GPU widget grabs are unchanged
  while model text and cold native exports change. Explicit grabFramebuffer
  rerenders return transparent pixels: Qt calls paintGL, whose inherited
  default clears this paintEvent-based canvas. Those extra diagnostic reads
  can disturb subsequent screen contents and cannot establish ordinary typing
  invisibility. The two Raster cases show actual glyph-pixel differences on
  entering edit mode despite identical model/native export and disabled caret;
  their presentation mismatch remains unresolved. Evidence:
  text-capture-original-r11-r2-independent-analysis.json, preserving the raw
  original failures and explicit-readback limitations.
- The mask baseline's full evidence proves more than serialized revision
  drift: empty masks retain a newly painted tile, seeded masks retain 2,068
  changed native bytes, and all six cases add a history command. Stored source
  files and unrelated Raster pixels are unchanged. MainWindow consumes Escape
  by exiting mask mode, which commits the still-active canvas stroke before
  its own cancellation handler runs. The isolated routing fix passes all eight
  native cases in PID15608, with zero provider entries and full290/352/4 inputs
  preserved. All six canceled strokes restore complete native pixels, model
  and history and produce zero commits; both completed-stroke controls create
  one command and pass exact native/model Undo/Redo. Production integration
  and its standalone test repeat remain pending. Evidence:
  mask-cancel-baseline-r1-independent-analysis.json and
  mask-escape-routing-r1-wrapper-proof.json.
- Heavy Raster38 baseline reaches its 900 s wrapper deadline (PID15340,
  elapsed900.394 s) without terminal actor results. Its last observed process
  CPU exceeds875 s and RSS reaches roughly2 GiB, but the missing phase record
  prevents attributing that work or accepting any transform/native assertions.
  Preserve this failure and its empty logs; original-project guard passes.
  A prospective repeat needs bounded phase breadcrumbs rather than another
  opaque deadline. Static inspection finds that the live shortcut excludes
  HSL, including this saved HSL/HSL-with-gradient/Outline stack. That is an
  eligibility lead, not measured proof of its cost or a speedup. Evidence:
  raster38-heavy-basic-auto-r1-r11/terminal-proof.json.
- Exact Pinch ROI now passes all101 native focused cases in PID4348 (41.047 s,
  zero failures/errors/skips/provider entries) with the corrected feasible
  pruning fixture, four complete-coverage controls and actual disk checkpoint
  reuse. The two runtime files remain byte-identical to the reviewed R2
  proposal. Fresh saved-native correctness processes1568/7968 then pass25
  independent guards and complete5,529,600 output-byte equality. A separate
  fresh performance pair15100/14792 passes26 guards on the same Intel hardware:
  one original render_region takes14.720635 s versus8.482493 s; scene-worker
  thread CPU14.046875 s versus7.9375 s. This is one descriptive sequential
  pair with empty source/derived caches after ordinary setup, not a statistical
  claim, whole-editor/input-latency result or OS-cache-cold comparison. Source,
  native grid/ICC, model/mute restoration and original/private files match.
  Portable production tests and combined integration remain pending. Evidence:
  native-pinch-roi-proposed-r5/terminal-proof.json,
  blue-pinch-roi-correctness-r2/terminal-proof.json and
  blue-pinch-roi-performance-r2/terminal-proof.json.
- The shared GPU paintGL readback hook passes all20 original typing/reflow
  cases in PID5324. Both Raster outline cases still fail their unchanged
  editing-versus-resting whole-widget equality assertions, so the22-case
  diagnostic remains failed overall. Its explicit readbacks call the shared
  presentation path exactly once; this is correctness evidence, not a clean
  typing-performance measurement. Production integration remains pending the
  independent diagnostic audit and combined regression repeat. Evidence:
  text-capture-readback-r11-r3/junit.xml and text-capture-diagnostic.json.
- Release handoff R3 passes14 of17 actual native cases in PID11140. Three
  Radial cases time out at the original25 s initial committed-render gate,
  before any gesture or handoff. Those cases provide no release correctness
  credit. The failed run and all original assertions remain intact; further
  qualification needs the unchanged R4 initial-render control, rather than a
  larger deadline. Source/test/support post-guards pass. Evidence:
  release-handoff-policy-windows-r3/pytest.xml and
  release-handoff-policy-isolated-r3 post-guard.
- HSL/Raster R2 crashes with access violation in PID2892 during a NumPy
  whole-buffer assertion. That fixture forms views from constBits on temporary
  QImages without retaining their owners. Preserve the failed native log and
  missing terminal evidence. An additive ownership-only fixture correction
  must retain each image through the unchanged complete-buffer comparisons
  and rerun all12 cases; this crash is not runtime acceptance. Evidence:
  hsl-raster-windows-r2-wrapper.log and its failed wrapper proof.
- The one-all-six Scale coarse trace completes in PID13048 (329.259 s) and
  retains the original earliest-release failure. Its separate16-check evidence
  guard passes, including whole current/cold/native comparisons, exact history
  and model restoration, source/provider/import guards and observer cleanup.
  It observes the actual first painted Scale job; inclusive instrumented
  clocks are causal leads and are not a clean interaction speed comparison.
  This is one gesture rather than the prior four-case transform run. Evidence:
  blue-live-draft-causal-r2-r1/terminal-proof.json and
  guard_blue_live_draft_causal_r3.py output for that label.
- Portable exact Pinch101 and mask cancellation8 tests now pass together:
  PID676,109 cases, zero failures/errors/skips, with full290/353/4 inputs,
  independent original-kernel ownership and provider/import guards. This
  qualifies the three source files for integration after the R11-dependent
  navigation trace completes. Evidence:
  native-pinch-mask-combined-r2/terminal-proof.json.
- Combined GPU readback and Text decoration reuse passes26/30 cases in
  PID5916, with provider0/provenance and full-source post-guards. Both original
  Raster cases now pass entry equality but fail at line228 after typing is
  committed; two additional typing/commit cases also fail. All20 original GPU
  cases and six additional decoration/fallback cases pass. This narrows the
  remaining defect to live-versus-committed presentation after text changes;
  the combined proposal is not accepted. Evidence:
  text-decoration-gpu-combined-windows-r2/pytest.xml.
- HSL/Raster R3 fixes fixture image ownership, then completes12 cases in
  PID11532. Six unmasked cases pass complete current/cold/native/Undo checks;
  all six masked cases fail first-current versus fresh same-policy preview
  pixels at line288 (approximately32-39% byte mismatches). The whole run
  remains failed, with provider0/provenance and full291/353/4 inputs held.
  The masked-gradient discrepancy must be resolved before adopting HSL draft
  eligibility. Evidence: hsl-raster-windows-r3/pytest.xml and wrapper proof.
- The HSL masked discrepancy is a fixture normalization defect. The R3
  factory deserializes an unsaved mask before adding its references; ordinary
  normalization removes that unreferenced mask, and the cold model removes
  the subsequently dangling binding. Minimal model diagnosis PID16680 passes
  all seven controls. Additive R5 binds the unchanged mask and effects before
  ordinary deserialization; it retains all whole-buffer assertions. Native
  PID14232 now passes12/12 cases in39.371 s, with provider0, actual provenance
  and full291/353/4 input/post-run guards. This qualifies the HSL preview path,
  but provides neither saved Raster38 speed nor earliest-release acceptance.
  Evidence: hsl-mask-fixture-diagnosis-r5 and
  hsl-raster-windows-r5-wrapper-proof.json.
- Physical chapter navigation R6 completes all six ordered stops in PID5328,
  with47 evidence guards and11 kernel-link controls passing. At60%, one
  current exact scene worker takes53.286 s wall/48.844 s CPU; the actual swap
  follows first observed demand by53.302 s. The masked Radial Blur object
  8972a14d457548a1b40e978398fa6c7a contributes42.566 s inclusive across six
  captures; one capture takes40.838 s. This localizes the dominant object, not
  an exclusive kernel cost or equal-input duplication. Lens optimization is
  not justified by this request. Original/private files and model/history are
  preserved; provider0, native GPU/DPR1.5 and observer restoration pass.
  No whole-chapter pixel oracle or clean input-latency comparison is claimed.
  Evidence: private-editor-navigation-nav11-r6 terminal proof,
  navigation-kernel-causal-r6-compact-analysis-r1.json.
- Unchanged R4 and handoff R3 both fail the same initial Radial25 s gate in
  diagnostic R1, before gestures (PIDs11852/9656). Source post-guards pass.
  The clock exporter itself fails on a presentation-revision field that is
  absent before completion; the R1 traces provide no kernel-causal credit.
  Preserve these failures. Additive diagnostic R2 keeps the original gate and
  treats absent pending metadata as absent while sealing collected clocks.
- The accepted Pinch/Mask source port is now applied as R12: exactly three
  runtime files and three additive portable test/reference files, with full
  before/after inventories and all original350 test inputs preserved. Fresh
  ports-combined-src-r12 freezes290 source/353 test files and four support
  inputs. The accepted109-case native source bytes are unchanged by this port;
  broader combined/editor verification and integrated publication are pending.
- R12 saved Room2 translate/scale/warp actor PID11432 reaches its unchanged
  900 s wrapper deadline (900.384 s), with no terminal actor results and empty
  logs. Original14317 and private4062 files remain unchanged; frozen inputs
  remain held. This is a failed acceptance run, not three accepted gestures.
  Late external py-spy0.4.2 reads, confined to that private PID, show the
  scene worker in Halftone/Lens and later Smudge, while GPU workers wait.
  The main stack progresses from release wait to Undo wait; stacks alone do
  not prove which gesture, object, cache identity or deadline consumed time.
  One10 s,20 Hz nonblocking window contains176 samples per five threads with
  23 reported read errors. It is descriptive, includes idle waits, and has no
  native C symbols, locals or clean timing claim. Evidence:
  room2-heavy-basic-auto-r1-r12/terminal-proof.json and
  external-py-spy-analysis-r1.json. The diagnostic tool is isolated under
  artifacts and does not alter program requirements. See the
  [py-spy documentation](https://github.com/benfred/py-spy) for profiler behavior.
- The additive initial-Radial diagnostic R2 preserves the original25 s
  failure in PID2288 while exporting valid clocks. One ordinary exact kernel
  processes a512x640 float32 RGBA source into626x728 native output, with37,768
  per-channel SciPy sampling calls. Its instrumented wall/thread-CPU clocks
  are33.390/21.734 s; sampling accounts for22.853/17.266 s nested within that
  kernel. A separate unchanged native control takes16.899/16.547 s for the
  same grid and call count. The difference prevents treating instrumented
  clocks as a clean speed comparison. Ten independent observer checks pass;
  no duplicate integration, completed initial-render gate, or candidate
  speedup is claimed. Evidence: radial-initial-control-clocks-r2 and
  radial-initial-r2-independent-observer-audit.json.
- The19-case scheduler/Radial cancellation proposal completes in PID2468:
  17 pass and two replacement cases fail an unchanged snapshot-signature
  assertion. Ordinary worker resolution converts a runtime QImage identity
  into its semantic pixel fingerprint; that representation change alone
  does not establish pixel mutation. An additive R4 test checks full old and
  new native buffers and immutable tile ownership instead. Peer review also
  requires a fresh reference with disk backing explicitly disabled, observed
  original-kernel evaluation, and publication before ordinary disk reuse.
  The runtime proposal remains byte-identical and unaccepted until all19
  corrected native cases pass. Evidence: scene-radial-cancellation-native-r2
  and radial-cancellation-r4-independent-review.json.
- Text presentation diagnosis PID9336 retains all four original failures.
  In the two original cases, live and committed native input mosaics are
  byte-identical, but separate cropped-tile drawing changes183 widget pixels
  near the logical256-pixel seam at DPR1.5. Whole-input replay matches;
  full-gutter replay still differs in120 pixels, at most two byte levels,
  and is not accepted. Two additional cases install nested draw observers,
  making the diagnostic restoration flag false before fixture teardown;
  preserve that validity failure separately from the unchanged feature
  failures. A lifecycle correction and input-only presentation experiment
  remain prospective. Evidence: text-tiles-diagnostic-windows-r12-r2.
- Cancellation R4 now passes19/19 proposed cases in PID15912,58/58 affected
  scheduler/scene/effect-region cases in PID6304, and85/85 pixel/disk-contract
  cases in PID7068. All three native runs retain provider/import/full290/355/4
  guards and the unchanged two-file runtime proposal. The replacement tests
  require ordinary publication, fresh original evaluation without disk
  backing, full old/new native buffers, then exact disk reuse. This qualifies
  the narrow cooperative Radial cancellation port; it is not an initial
  Radial kernel speedup or a full combined-editor acceptance run. Evidence:
  scene-radial-cancellation-native-r3, affected-r3 and contracts-r3.
- Saved Raster38 HSL draft Scale finishes in PID13316 after499.267 s, without
  a deadline or actor exception. Overall acceptance remains failed because
  one bounded progress-file replacement raises Windows access denied. The
  independent17-check audit preserves that failure while verifying both
  post-paint live witnesses, complete independent draft/commit/Undo native
  buffers, exact model/history restoration and provider/source/input guards.
  Actual instrumented warm/first-artwork/release/Undo waits are107.221,
  8.228,106.363 and110.670 s. The private HSL-HSL-Outline route is selected,
  using full native source and mask arrays with97x256 transient effect output;
  it remains much too slow and is not merged-R12 or physical-input acceptance.
  Evidence: raster38-hsl-draft-scale-r4-r12 and
  raster-draft-r4-r12-actual-independent-analysis.json. Prospective telemetry
  should append bounded complete records without replacing a file held by a
  concurrent Windows reader; do not repeat minutes of rendering just for IO.
- Additive Text diagnostic R3 completes in PID7416 with diagnostic validity
  passing and the same four strict feature failures retained. An input-only
  API replay in PID13664 then preserves all recorded native cores and finds
  zero changed glyph-seam pixels for a bounded joined native-core image.
  Gutter/point/clipped-whole drawing still differs; the existing overview
  sampler differs from the original QPainter presentation and is not a
  substitute. The joined-core path is prospective and needs ordinary typing,
  commit, invalidation and bounded-allocation acceptance. It performs no scene
  evaluation, source sampling, cache admission or durable writes. Evidence:
  text-tiles-diagnostic-windows-r12-r3 and text-presentation-api-windows-r2.
- Disk status validation passes all33 cases in PID6208 against R12, including
  the accepted mask cancellation behavior. The narrow change leaves pending
  status rows queued while drawing or projecting a live preview; completion,
  reads, maintenance and builds continue. The earlier three mask-baseline
  failures remain preserved. Evidence: disk-live-status-windows-r12-r2.
- Halftone regional output cropping initially passes81/84 cases in PID4832.
  Three fine-grid table-fallback cases differ by one float32 ULP and remain
  failed. The corrected proposal preserves the complete original arithmetic
  when no cell table is admitted, then crops the finished result; eligible
  table-backed circle/incircle output work can still use the requested core.
  The corrected proposal passes92 cases in PID1796,141 affected cases in
  PID16516 and58 pixel-contract cases in PID8500. Full preparation, global
  coordinates and source sampling stay unchanged. Performance acceptance is
  separate. Evidence: halftone-output-crop-windows-r4, affected-r4 and
  contracts-r4; halftone-output-crop-r3-independent-review.json.
- The shared NumPy RGBA sampler passes70 strict native cases in PID13424,
  36 affected cases in PID13940 and85 contract cases in PID7400, but is
  rejected for production on measured performance. Three alternating whole
  kernel pairs in PID16900 produce identical7,291,648 output bytes. Median
  wall time rises from16.921 to22.629 s, and median thread CPU from16.500 to
  22.000 s. This is a33.7% wall-time regression on the initialized synthetic
  control, not an editor or historical performance comparison. The original
  SciPy kernel remains in production. Evidence: radial-shared-rgba-native-r1,
  affected-r1, contracts-r1 and radial-shared-rgba-benchmark-r1.
- The first joined native-core Text candidate passes the four original
  failures in PID9680 and30 combined typing/display cases in PID17224. The
  separate43-case surface run in PID16696 has42 passes and one null-image
  fixture error before the tested operation. Review additionally finds
  unrelated Unicode label changes in the proposal. Those results remain
  preserved; neither defect is waived, and the corrected candidate must be
  rebuilt from the unchanged UTF-8 R12 source before adoption. Evidence:
  text-native-mosaic-original4-windows-r1, combined30-windows-r1 and
  mosaic43-windows-r1.
- The corrected Text R2 candidate is rebuilt from raw R12 UTF-8/CRLF bytes,
  preserving all unrelated Unicode labels and comments. Its complete73-case
  native run passes in PID5204: the30 original/decoration cases and43 bounded
  surface cases run together. All original353 test files, four supports,
  provider guards and complete source/input manifests remain held. The
  null-image helper correction preserves the original strict43 assertions.
  Evidence: text-native-mosaic-full73-windows-r2 and
  text-native-mosaic-r2-source-preservation.json.
- Halftone whole-region performance passes six alternating calls in PID16576,
  with identical262,144 output bytes, unchanged full source/ICC/cache identity
  and one worker on Windows at DPR1.5. Median wall time falls234.523 to76.838 ms
  (67.24% less); median thread CPU falls234.375 to62.500 ms. Both routes retain
  full526x526 preparation and the same cell table; nine pointwise passes use
  the requested256x256 core in the candidate. This uses synthetic native
  artwork with the saved Gradient12 settings, not that object's saved source,
  the complete ancestor pipeline, the53-second navigation stall or GUI input
  latency. Evidence: halftone-region-benchmark-r1 and
  disk-halftone-accepted-source-maps-r3.json.
- The owned optional Win64 C sampler passes83 strict cases in PID16976,
  36 affected cases in PID1940 and85 pixel/disk-contract cases in PID8400.
  The tested binary exports only the two owned ABI functions and imports no
  DLLs. It shares corner addresses across RGBA channels while preserving
  the original double multiplication, corner order and per-sample float32
  rounding. Missing, unsupported or unverified delivery uses original SciPy;
  there is no runtime compilation or download. Source grids,96-pixel blocks,
  angular count, cancellation cadence, masks and native precision remain held.
- Six matched whole Radial kernel calls in PID12408 pass full7,291,648-byte
  equality with zero candidate SciPy fallback. Median wall time falls17.119
  to11.591 s (32.29% less); median thread CPU falls16.734 to11.203 s. The source
  and output hashes match the earlier slower NumPy experiment. DLL startup is
  outside the clocks; per-call finite validation, coordinate checks, foreign
  calls and allocation are inside. This qualifies the optional sampler for
  production porting, not a saved-chapter or physical-input speed claim.
  Evidence: radial-rgba-cabi-native-r1, affected-r1, contracts-r1 and
  radial-rgba-cabi-benchmark-r1.
- Root applies the accepted cancellation, disk-status, Halftone and corrected
  Text deltas to the integration checkout, then freezes R13:290 runtime files,
  360 test/reference inputs and four supports. All353 previous test bytes are
  preserved; the original checkout and published overnight-stable branch are
  untouched. This is a combined source checkpoint requiring fresh runtime
  acceptance, not a completed full-build or saved-project verification.
  Evidence: accepted-ports-r13-r1-root-apply-journal.jsonl and
  ports-combined-src-r13-freeze-proof.json.
- The portable optional sampler passes all83 cases through the ordinary
  production module in PID10824. The same accepted C source and binary are
  shipped with verified portable metadata; no duplicated private kernel is
  used by this adapter. All293 runtime resources,362 test/reference inputs,
  four supports and the unchanged R13 checkout are checked before and after.
  No skips, provider calls or silent SciPy fallback occur in supported cases.
  This is component acceptance; assembled live-preview, text and full-editor
  acceptance remain separate. Evidence: radial-cabi-production-native-r1.
- The assembled live/HSL/handoff/C1/cancellation/user-Text candidate runs203
  cases in PID2204:199 pass and four fail, with zero errors/skips and complete
  source/input/provider guards passing. The translate Radial case still misses
  the original25-second initial-scene gate. Scale and Warp reach the release
  handoff checks but miss the same25-second committed-scene gate afterward.
  The newly ported affine-Cage Text case also changes displayed pixels merely
  by entering text edit. These failures remain required blockers; the candidate
  is not applied or published. Original cancellation assertions are retained;
  their observation seam now wraps the actual complete RGBA sampler rather
  than assuming four SciPy channel calls. Evidence:
  combined-live-handoff-text-native203-r13-r4-combined-wrapper-proof.json.
- A separate explicitly unrolled C2 sampler passes the unchanged83 production
  module cases in PID9544, using the newly compiled owned binary and a distinct
  build contract. It retains the C1 math and sample grids. Performance and
  assembled behavior remain pending; this component result does not resolve
  the four assembled failures. Evidence: radial-cabi-unrolled-native-r3.
- Six alternating C2 whole-kernel calls pass in PID15516. All7,291,648 output
  bytes match the original and the previously accepted C1 output. Median wall
  time is14.290 s for original SciPy and5.483 s for C2 (61.63% less); median
  thread CPU is13.891 and5.375 s. Source, grids and per-call work are held;
  library initialization is outside the clocks. These are matched original
  versus C2 kernel measurements, not a matched C1-versus-C2 or saved-editor
  comparison. C2 can proceed to the unchanged assembled gates. Evidence:
  radial-cabi-unrolled-benchmark-r3.
- C2 is composed into the same assembled candidate with only its four sampler
  resources changed. All three previously failed Radial Translate/Scale/Warp
  cases pass in PID16676, including unchanged25-second current-scene gates,
  owned release presentation, independent committed native bytes and Undo.
  Full295 runtime/367 test/four-support and unchanged current R13 guards pass
  after the run. Total testcase durations include several independent cold
  renders and are not single-input latency measurements. The separate affine
  Text failure and full assembled suite remain pending. Evidence:
  assembled-c2-radial-gates3-r5.
- A one-case native diagnosis in PID7760 preserves the affine Text failure.
  The two original captures differ at3,095 glyph pixels while the same eight
  native tile identities are presented, no tile uploads occur, and the editing
  overlay has no selection or visible caret. This identifies a narrow UI paint
  discriminator; storage metadata alone is not a texture-content readback.
  An isolated early return for an empty editing overlay is prepared. It leaves
  the actual caret/selection body and all native glyph rendering unchanged;
  original pixel assertions and waits remain required. Evidence:
  text-cage-onecase-r5-diagnostic-r2 and text-no-ink-overlay-proposal-r1.
- The isolated empty-overlay guard does not resolve the affine Text case:
  PID5440 still fails the unchanged enter-edit pixel equality at line28.
  This rejects the proposed overlay cause; the change is not accepted. The same
  candidate separately passes all73 existing text/typing/caret/selection/native
  mosaic cases in PID14232 with zero errors/skips and complete provenance and
  provider guards. Diagnosis now includes actual presenter inputs and borrowed
  CPU tile bytes, rather than treating cache-token equality as content proof.
  Evidence: assembled-c2-text-no-ink-one-r6 and assembled-c2-text73-r6.
- The next Text diagnostic times out after180 seconds in PID14952 before its
  observation hook installs. An actual py-spy dump places the GUI in the window
  fixture's initial processEvents and a graphics owner at shader program link.
  It produces no case XML and is invalid as pixel evidence. An unchanged fresh
  retry in PID12536 completes in5.137 seconds and validly preserves the same
  glyph mismatch. All eight CPU tile byte hashes, camera/painter transforms,
  opacity, geometry and texture metadata match; no text overlay body or raster
  feedback draws. The discrepancy remains in presentation. Separate startup
  inspection also finds global and canvas-owned worker preparation scheduled
  for the same GPU canvas; this duplication is concrete, while its connection
  to the one startup stall remains a hypothesis. Evidence:
  text-cage-onecase-r6-diagnostic-r3 and its retry1.
- A bounded ordinary exact warm trace of the private Raster38 view succeeds in
  PID11360 with the original project unchanged and zero provider entries.
  Its admitted worker runs94.823 seconds. Selected Raster38 uses regional
  rendering successfully in all10 calls and costs5.926 seconds inclusive.
  Neighboring falling-through-life Smudge and Room stacks cost29.645 and25.737
  seconds exclusive, with repeated same-sized Smudge stage outputs. This
  corrects the earlier unproved attribution to selected full-frame HSL/Outline.
  The observed repeated sizes/misses do not yet prove equal semantic keys or
  eviction pressure. No cache budget, source grid or rendering path is changed.
  Evidence: raster38-exact-owners-r6-warm-r1 and
  raster38-exact-warm-owner-analysis-r1.json.
- Additional GPU diagnostics preserve the same original Text failure. The first
  R4 run in PID2436 is rejected because an observer helper overrides a canvas
  metadata method and prevents capture association; its failed artifacts remain.
  The corrected R5 run in PID14084 has complete provenance, no observer errors,
  and verifies the actual read framebuffer attachment before bounded readback.
  For both original captures, the tile-stage framebuffer region equals the final
  returned capture byte for byte. The mismatch therefore already exists after
  ordinary GPU tile presentation, before later overlays. Actual texture filters,
  sampler binding, framebuffer encoding, sampling state and native CPU tile
  bytes are unchanged. Successive presentations of the same transparent tiles
  produce different framebuffer bytes; framebuffer lifecycle and background
  coverage now require investigation. These instrumented readbacks are neither
  performance measurements nor native export correctness evidence. Evidence:
  text-cage-onecase-r6-diagnostic-r4 and text-cage-onecase-r6-diagnostic-r5.
- Routing ordinary GPU paint events through Qt's standard framebuffer lifecycle
  alone still fails the unchanged first Text case in PID11860. A subsequent
  candidate changes only paintGL: it positively identifies the widget's current
  framebuffer/color attachment and clears that existing presentation attachment
  to the same opaque canvas background before QPainter. Sized GL vectors and
  finally-restored scissor/indexed color mask preserve surrounding graphics state.
  This candidate passes the complete original affine case in PID17104, including
  enter-edit, live typing and committed capture equality. All85 affected Text
  cases then pass in PID8836, with zero errors/skips and complete source/test,
  current-checkout and provider guards. Source pixels, derived sampling grids,
  native rendering, cache semantics and PartialUpdate remain unchanged. The full
  assembled and broader integration checks are still pending. Evidence:
  assembled-text-paint-one-r7, assembled-c2-text-clear-one-r8,
  assembled-c2-text85-r8 and text-fbo-clear-r8-independent-review.json.
- The complete assembled R8 group passes all203 cases in PID6768, with zero
  failures/errors/skips and complete runtime/test/support, current-checkout and
  provider guards. This includes the original live interaction, HSL, release
  handoff, sampler, cancellation and affine Text assertions. The accepted
  resources are applied to the integration checkout and frozen as R14
  (295 runtime resources,367 tests,four support files). Evidence:
  assembled-native203-r8-combined-wrapper-proof.json and
  ports-combined-src-r14-freeze-proof.json.
- The broader single-process nonexternal R14 suite closes in PID8568 after
  2,786.097 seconds:6,466 passed,74 failed,13 skipped,zero errors. All6,553
  selected cases are reconciled against XML; five planned child-launch cases
  are deselected and the Blender-extension file is excluded. Full current and
  frozen inventories/imports match, with zero provider or external-child
  attempts. All22 previously failed Text cases also pass in this broad run.
  The13 skips concern optional local/private brush/SUT/CSP fixtures; none is
  credited as exercised. The failed run remains a release blocker. Evidence:
  complete-nonexternal-integrated-r14-r1 and
  complete-suite-r14-failure-extraction-r1/summary.json.
- Fourteen representative original failures are rerun unchanged in a fresh
  Windows process at the actual display density1.5:one passes and13 fail.
  A first intended density1 control remains at actual1.5 and is not treated as
  a density contrast. A corrected process-only test environment produces actual
  density1 (independently checked for screen/widget/grab):ten representatives
  pass and four fail. The tests' physical capture grids/point coordinates explain
  the sampled compound, dirty reference, transformed-raster, culling, overflow,
  promoted-ink and vector failures; the GPU overlay, Posterize Add, brush-grid
  visibility and logical Curves minimum width still require investigation.
  The camera handoff case passes in both fresh processes. These are nominal
  unit-fixture controls, not a production density override or blanket waiver;
  original assertions and artwork sampling are unchanged. Evidence:
  broad-r14-representatives-native-r1, broad-r14-representatives-dpr1-r1
  (invalid density contrast) and broad-r14-representatives-dpr1-r2.
