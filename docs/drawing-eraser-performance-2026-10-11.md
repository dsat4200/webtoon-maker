# Drawing and eraser latency, October 11, 2026

The requested capture was `20261011-124315-45e20cd7e3`, recorded
12:43:15–12:43:29 local time. It identifies Doodles chapter
`49354b12f0aa4f09a3522a623d520fc1`, selected raster
`572d37134a904b8ab7220c8d4febf507`. Its 1080×30000 document contains 28 layers,
75 objects, 11 modifiers, five masks and 788 sparse source tiles; the selected
raster has 18 tiles. Investigation used a private recovery copy. User project
files and the running editor were not changed.

## Causes and changes

The input handler was already inexpensive (p95 2.78 ms). Whole-chapter navigator
work held shared native-render admission for 0.7–2.4 seconds. Four later canvas
envelopes took 31–1414 ms although their admitted work took only 29–48 ms.
The ordinary preview path did not borrow the native tile cancellation token,
so superseded thumbnails kept rendering unrelated artwork during interaction.

Detached previews now check cancellation between layers, objects, source tiles
and effect stages. A navigator also abandons its provisional capture when
higher-priority visible/input work waits. It discards the partial image,
releases its reservation through normal unwinding and retries the dirty
document after the existing idle delay. Idle previews use the same renderer.

An unrelated layer with `ignore_parent_mask` rejected the selected raster's
native feedback eligibility. These siblings paint after clipped children and
the parent border. The prepared foreground now keeps that pass and its order,
independent of child-list position. Unsupported outward gradients, blends,
selected/ancestor modifiers, masks and coupled dependencies keep their guards.

Full-image verification also exposed a sampling-phase difference when a
positive axis parent quad at fractional coordinates was flattened into an
intermediate source image. Authored object/ancestor quads now retain original
source samples and the ordinary painter transform/clip hierarchy in the
existing native 4×4 feedback route. Plain translations retain compact native
patches and budgets. Cold source capture uses the same coverage policy.

Native feedback blocks additionally preserve each unchanged display crop's
image/texture identity after comparing its complete pixels, including gutters.
A local edit updates affected display tiles; gutter edits update all sampling
neighbors. The existing private presentation cache owns and bounds these bytes.

## Matched desktop measurement

`tests/benchmark_navigator_contact_latency.py` ran fresh baseline and candidate
processes against the copied recovery scene on Windows Qt 6.11.1, NVIDIA RTX
5060, with the ordinary GPU canvas. Both used the captured 951×937 viewport,
DPR 1, camera center and zoom 0.6581012199. Each tool received forty synthetic
native inputs at 120 Hz, with a real whole-chapter navigator job already
admitted before contact. Pencil/eraser size was eight source pixels at full
pressure. Original graph, artwork and modifiers were retained; test edits
existed only in memory.

| Packet to frame swap | Before median | After median | Before p95 | After p95 |
| --- | ---: | ---: | ---: | ---: |
| Pencil | 640.8 ms | 17.7 ms | 789.0 ms | 23.3 ms |
| Eraser | 677.0 ms | 15.9 ms | 821.9 ms | 18.4 ms |

All 80 candidate positions appeared while contact was held. Candidate contacts
submitted no ordinary full-scene work while held. Input-handler p95 was
1.08 ms for pencil and 0.74 ms for eraser. These are software packet-to-swap
measurements, excluding tablet drivers and display scanout; framebuffer
observation adds overhead. Baseline source preserves the earlier cache task's
uncommitted changes.

## Native correctness and regression evidence

With an ordinary scene worker deliberately blocked, held pencil and eraser
presentation each matched an independent cold native tile render across all
891087 displayed pixels, with zero differing channels. Undo restored every
original selected-source byte. Before/after hashes confirmed all 1621 copied
project files were unchanged.

`test_unmasked_sibling_raster_feedback.py` covers both child positions,
object/layer siblings, nested branches, pencil/eraser, RGBA8/RGBA64 originals,
complete buffers and source-byte undo. `test_navigator_preemption.py` covers
cancellation, admission release, partial image rejection, visible progress
and complete idle retry. `test_native_feedback_texture_reuse.py` covers local
edits, four-neighbor gutters, retained pixels, retirement and the byte cap.
Existing contact coverage, bootstrap, native transforms, preview contracts,
scene cancellation and presentation suites provide broader regression checks.

Across the recorded runs, 475 distinct regression cases passed; one optional
external Pocket Boyfriend fixture was skipped. Actual Windows/OpenGL texture,
gutter, geometry, state-restoration and presentation checks passed. Three
low-zoom software-window partial-repaint checks intermittently differ by one
display color level between a screen snapshot and immediate full repaint.
The same discrepancy was reproduced in the isolated pre-change eraser check;
baseline and candidate outcomes vary with test sequence. It remains an
unresolved display-test limitation, not a waived native-pixel comparison.
Saved-scene/native buffers, source precision and undo checks above require and
achieve exact equality. `validation-summary.json` retains these distinctions.

Private evidence is in `.artifacts/drawing-eraser-20261011/`: capture analysis,
baseline/final matched 40-point probes, zero-difference pixel reports and test
logs/XML. Exact/disk descriptors, dependency validation, recording rules, color
contracts and native artwork resolution are unchanged. Draft and feedback
images remain private provisional resources.

## Evening Pocket Boyfriend captures

The additional captures were `20261011-192746-9928616a68`,
`20261011-193328-a0e58dfbb2`, and `20261011-200407-f9df07f874`.
The 1.44-second `20261011-200234-f1eaff64c5` capture contains no completed
phases and cannot establish a slowdown. The last useful capture stopped at
20:12:15 local time after 488.21 seconds. Its frozen analysis input has SHA256
`eb2c778230fab0f4eadc2b1cbe3fada5de894b9671fd647127d47a3bf18209c3`.

The 19:27 capture uses the large 1080×82000 chapter with 106 layers, 173 objects,
103 effects and 30 masks. The 19:33 capture instead uses the actual opened
2705×40572 asset document, with four layers, nine objects, four effects and two
masks. They must not be treated as the same workload. Both investigations use
separate private copies of the saved manual/recovery states. The user project
and their running editor remain untouched.

The last capture has 6,206 canvas paints: recent-512 p95 9.60 ms, but maximum
1,656.46 ms. Its 255 event-loop heartbeat gaps of at least 250 ms total
263.14 seconds, with p95 2,308.47 ms and maximum 3,286.69 ms. Seven measured
`begin_stroke` calls total only 25 ms, maximum 4.70 ms. Smooth ordinary drawing
frames therefore do not establish that the first contact or other long pauses
are fixed.

Identified interface-thread work includes saved cache index JSON during tab
binding (1.47–2.19 seconds), Blender PNG stat/read/decode (746 ms), private
snapshot pin deletion (379–501 ms), and repeated whole-polygon lasso drawing
and alpha scans (267–1,827 ms). The earlier asset capture also contains eleven
workspace-settings writes taking 292–543 ms. The 1,965 ms `begin_cage_tool`
sample is an incompatibility warning for a selected Text Object awaiting
dismissal, not measured native cage preparation.

GPU presentation has separate long tails: texture upload maximum 1,392.67 ms
and GL-state restore maximum 1,551.74 ms. Python samples identify calls into
the driver, not the reason it stalled. The capture has no historical GPU
utilization, hard-fault, system-memory or driver-queue measurements. Its
bounded last 19.4-second event timeline cannot attribute all heartbeat gaps
to background workers; most retained stalls only show the Qt event-loop
boundary. Worker envelopes and admitted spans are nested and are not summed.

### Additional changes

Contiguous document, retained contact and feedback tiles now share one ordered
native presentation boundary and GL state snapshot. Clipping, alpha, geometry
and source rectangles retain their original ordering. Compatible display
texture storage is reused only after a zero-time completion-fence poll; active
and retired allocations share the existing 256 MiB cap. Native source packets
are still applied at their received rate. An owned covered drawing contact
coalesces display work to 16 ms; cold or invalidated admission and release
promote pending work immediately. Projection request and viewport-intersection
reuse caches only immutable camera/grid metadata.

Workspace saves now submit bounded detached scalar preferences, coalesce to
one active and one latest packet, and merge into the existing encoded settings
on the writer. Full saves supersede old queued updates; close drains the latest
workspace write and performs its ordinary full save. Brush encoding and file
work no longer occur during these frequent workspace updates. Saved cache
index loading uses the existing cache IO worker and the same validation,
generation guards, aliases and ordinary restoration pipeline.

Retired private source pins and directories now enqueue cleanup on a dedicated
IO thread. Original pin owners remain alive through deletion and surviving
snapshots still read their original source bytes after authored filename
replacement. Finalizers do not enter executor submission locks, avoiding a
same-thread GC deadlock as well as synchronous file deletion. User source
paths are never cleanup targets.

Lasso brush packets redraw only tiles intersecting the changed closing-edge
triangle, with the original antialias guard and pen-down originals. Full-path
fallbacks remain for unsupported changes. Every intermediate result is checked
against the old full-polygon algorithm in the fixed 39-packet regression
sequence for RGBA8, RGBA64 and float originals, including translucent painting,
transparent-color erasure, crossings and reversed edges.

Blender publication stat/read/decode, immutable original-byte pin creation and
display conversion run on one detached worker. Only guarded current chapter,
object/source owners and current publication metadata may adopt the prepared
native/display handles on the document thread. One active decoded result and
one latest metadata record per linked view bound pending work. Physical or
queued drawing and read-only cache operations defer adoption; retired contexts
cannot apply late successes or errors. Original PNG bytes, source precision,
profiles and aspect/transform behavior remain unchanged. The focused prepared
image, Blender source, halftone and protocol suites pass 93 cases.

Ctrl-selection now captures the tablet-down modifiers, with a Ctrl-only
keyboard fallback for tablet backends that omit it. Pencil and eraser use the
ordinary object/layer chooser without starting a stroke, and deferred selection
keeps the press modifiers through release. Mid-contact keyboard or barrel
changes do not start a second drawing transaction.

### Matched sustained-contact evidence

`tests/benchmark_pocket_gpu_contact_latency.py` uses the actual captured
1042×937 native GPU canvas and original Pocket Boyfriend source scene. Each
tool receives seven consecutive three-second contacts at 120 Hz, separated by
15 ms. All contacts must change source pixels and remain held until release.
Each stage, including the pencil stage before later erasure, is compared with
baseline source and full-frame pixels; exact source/model undo and unchanged
project-file hashes are also required.

The large-chapter matched pair uses the captured −0.663° camera and 256 MiB
presentation pressure. Both runs finish the same explicit setup readback
before timing. The candidate is the frozen `candidate-source-barrier` runtime
with grouped presentation and storage reuse, before the later cadence/replay
changes. This is a fair sustained-contact comparison, not a cold-start,
cadence or first-pen-contact claim.

| Outer canvas paint | Baseline p95 / max | Candidate p95 / max |
| --- | ---: | ---: |
| Large chapter pencil | 17.97 / 176.12 ms | 4.77 / 11.42 ms |
| Large chapter eraser | 23.28 / 30.59 ms | 7.58 / 21.00 ms |

Candidate packet-to-frame-swap p95/max are 6.17/13.68 ms for pencil and
9.29/26.14 ms for eraser, excluding tablet-driver delay and physical scanout.
All fourteen contact packet counts, complete source stages and frame stages
match baseline exactly. Initial-to-undo displayed frames differ by the same
249 bytes in both runs (cursor/outline presentation); source/model undo is
exact. The valid equality evidence is
`big-candidate-barrier-guarded-r2/summary.json` and
`matched-barrier-comparison.json`. Candidate presentation performs one new allocation
and 21,962 safe storage reuses, with peak allocation 268,236,800 bytes below
the 268,435,456-byte cap and zero raster fallbacks.

An earlier large candidate run ended one held contact prematurely; its pencil
stage differed by 349 bytes even though later erasure hid the difference.
That run is invalid and excluded. The guarded repeat requires uninterrupted
contacts and passes all intermediate comparisons. An asset cadence run also
showed a zero pacing-token count before its coverage guard was corrected;
its faster timings cannot be attributed to pacing. These failures are retained
in the private evidence instead of being used as positive results.

### First-contact investigation

A separate two-contact probe evicts the selected asset's eleven decoded source
buffers without flushing OS disk caches. Before the busy-storage replay change,
baseline first covered swap is 35.02 ms and candidate 119.50 ms. This does not
reproduce a one-second freeze and is negative cold-start evidence, not proof
of a fix. A second probe queues 991 presentation uploads without an event pump
or setup readback. It produces three consecutive busy-storage raster fallbacks
and a 102.03 ms first paint / 110.83 ms first covered swap, versus 18.31 ms for
the next contact. This establishes a distinct graphics-storage startup path
that the sustained-contact barrier tests deliberately exclude.

Private evening evidence, frozen baseline/candidate manifests, complete source
comparisons and test logs are in `.artifacts/drawing-latency-20261011-evening/`.
All changes preserve native artwork sampling, original source precision,
dependency validation and ordinary memory/disk cache identities. Draft/live
presentation never becomes durable exact cache data.
