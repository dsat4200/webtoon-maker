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
