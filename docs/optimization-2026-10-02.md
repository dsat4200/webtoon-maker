# Rendering, storage, and interaction optimization

Work started October 2, 2026. This extends the renderer service and general tile
evaluator already present at commit `33a182d`.

## Scope and quality gates

The requested work is recommendations 3–6 from the architecture review, followed
by 24 smaller profiling-led improvements and implementation of further useful
findings from primary-source optimization research.

- 3: Execute compatible effect chains with GPU-resident intermediates, reusable
  textures/programs, explicit precision rules, and safe CPU fallback. Preserve
  masks, stage rounding, coordinate phase, and exact export behavior.
- 4: Carry resolution through graph requests and caches, prioritize visible work,
  cancel obsolete requests, and separate immediate ink from delayed exact effects.
- 5: Define versioned pixel/color contracts, floating-point working buffers,
  explicit import/display/export transforms, and OpenColorIO integration without
  silently changing existing documents.
- 6: Lazy disk-backed tiles, bounded decoded caches, transactional incremental
  saves, and focused parameter/graph undo instead of full document replacement.
- Find and fix 24 additional concrete slow paths. Record their trigger, change,
  quality constraint, and verification individually.
- Research additional improvements, implement supported findings, and explain
  measured benefits and remaining limits.

All project experiments use an isolated copy of Pocket Boyfriend. Tests include
large multi-layer/multi-modifier rendering, editing, saving, reopening, recovery,
undo/redo, native Windows GPU checks, and synthetic feature cases. Existing
artwork must retain its defined pixels; provisional presentation must never be
reported as a completed exact result. Worker inputs must be detached from mutable
GUI/document state. Cache, queued work, and temporary memory need explicit limits.

## Evidence and usage

Baseline source, project copy, timing results, profiles, image comparisons, and
test reports live under `.artifacts/optimization-20261002` (ignored local data).
Account usage started at 1% of the available weekly meter. The requested work
budget is 35–45 percentage points; check the account meter after major batches
and stop before its conservative 45% total ceiling. Do useful scoped work rather
than manufacture activity to consume a quota.

## Progress

- Renderer service and tile evaluator: existing foundation inspected.
- Fresh source/project baseline: captured at `33a182d`. The current Pocket
  Boyfriend main chapter has 135 objects, 89 layers, 78 modifiers, and 17 masks.
  The isolated copy contains 4,800 files across three chapters.
- Recommendation 3: exact compatible point chains now use compiled CPU tables
  and a bounded float-texture GPU engine. A dedicated graphics owner serves
  detached jobs. Exact scalar blur pyramids now stay on the GPU through integer
  resampling and scalar blending. Regional GPU kernels and broader graph execution
  remain pending.
- Recommendation 4: concurrent detached CPU work and separate raster-contact
  previews implemented; graph resolution/mips and further scheduling remain pending.
- Recommendation 5: persisted pixel-contract and OCIO foundations, native
  precision/ICC import decoding, and float blur/outline, warp, smudge, mask,
  halftone, and object blend paths implemented.
  Remaining effect kernels and whole-canvas presentation/export integration
  remain pending. Existing documents still use their original pixels.
- Recommendation 6: lazy saved/edited tiles, bounded raster history and imported
  originals, incremental saves, and focused metadata history implemented. Further
  full-suite and real-project memory/performance verification remains in progress.
- 24 smaller fixes: all twenty-four implemented; focused pixel/storage/native
  checks pass. The fourth sharded full run passes 5,123 cases (86 skipped),
  with no failures/errors and unchanged implementation hashes.
- Research and further implementation: in progress.

### Storage and history first pass

Lazy `DiskTileMap` mappings defer PNG decoding and retain saved and edited pixels
in one 256 MiB LRU. Evicted edits retain immutable private raw files, preserving
every pixel bit and their uncommitted status. A just-borrowed tile stays resident
until a later request so a caller can paint into it. Saves
track independent resource generations per destination, replace only changed
tiles/imported images, and snapshot immutable resource files with hard links
where supported (portable copy fallback otherwise). Existing pending-marker
recovery remains in place. A derived alpha-bound index avoids decoding pixels
for content bounds on subsequent opens. Deleted backing files are retained in
a private temporary directory when metadata history can restore their objects.
Decoded imported images now have a separate 256 MiB LRU.

Original encoded imports also use immutable file pins and a 64 MiB byte LRU per
store. Clones and resource history share those original pins without reading or
duplicating their complete bytes. External atomic replacement/deletion cannot
change a loaded revision, and saving restores the original file bytes exactly.
Unsaved raster paint and large raster undo patches still need bounded spilling.

Clean tile revisions are pinned with immutable hard links (copy fallback),
prepared in the background and reused across recovery snapshots. Decoding reads
the pinned revision even if a later publisher replaces the original filename.
Per-chapter/asset publication locks wait for earlier readers, and saved resource
generations also check destination file identity/size/time so independent loaded
stores cannot mistake another writer's pixels for their own. Recovery adopts
derived bounds only when the live pixel revision still matches. Last-good
backups reuse unchanged files and replace only changed resources before the
pending marker is published. Regression cases include concurrent readers,
independent writers, deleted/replaced files, undo, and injected publication failure.

The initial storage/regression batch passed 88 tests; seven dedicated edge cases
also passed, including lazy eviction, in-place edits, manual/autosave sequencing,
injected publication failure, and delete/save/undo.

Initial large-chapter measurements (single runs, including profiler overhead):

| Operation | Baseline | First pass |
|---|---:|---:|
| Main chapter open, existing project without index | 973 ms | 266 ms |
| First content-bound scan, existing project | 102 ms | 873 ms |
| Metadata-only save after destination is established | 7,753 ms | 1,522 ms |
| One-dab save | 7,746 ms | 1,535 ms |
| Reopen saved chapter with index | 3,049 ms | 299 ms |

The first bounds scan still decodes older tiles, and the first save to a new
destination still copies every resource. Initial autosave measured slower
(4,475 → 6,911 ms) because durable publication now flushes individual tile files;
this remains an optimization target. Do not infer drawing latency from storage
timings. Further comparisons need consistent cache states and repeated runs.

The later sequential recovery check on all 135 objects/89 layers/78 modifiers
passed file hashes and reopened pixels. Background pin preparation took 672 ms
while the event loop ran; captures took 11.7–13.5 ms without decoding clean live
tiles. Writer times were 3,157 ms initially, 2,870 ms when creating the first
backup, 1,343 ms for one dab, and 1,248 ms for the following metadata edit.
The archived implementation measured roughly 4,267/5,212/5,362 ms in its three
phases. These are single runs, not a stable speedup ratio. Later heartbeat peaks
were about 42 ms, which still needs improvement.

Metadata history now compiles full-state callers to changed-record patches;
modifier sliders, endpoints, presets, linking, and reordering capture focused
records/attributes directly. Structural undo keeps the document and unrelated
model identities while retaining its normal UI notifications. History restore
cancels active parameter gestures explicitly. Verification is in progress.

### Scheduling and pixel foundations

Detached effect work now uses up to four CPU workers while sharing the existing
256 MiB admission budget. Oversized fallbacks keep exclusive ownership of the
pool. All running requests participate in cancellation, exact-result lookup,
and publication checks. Each queued request captures its own immutable render
policy. Shared blur and outline cache bookkeeping uses short locks; expensive
kernels run outside them, and retained arrays are read-only.

`core/pixel_contract.py` defines the validated, immutable document policy; chapter
schema 26 saves it and migrates omitted policies to the original byte behavior.
Serialized and focused metadata history reconstruct validated policies during
undo/redo. Policy/schema/persistence checks pass all 318 cases. Imported originals
can now decode their native precision and embedded profile, sharing the existing
decoded memory budget with legacy display images; all 83 import/source checks pass.
`render/pixels.py` provides legacy byte and opt-in float16/float32 working formats,
straight-color OCIO conversion with unchanged alpha, distinct import/display/
export edges, and cached CPU/GPU processor descriptions. The renderer service
carries explicit contracts. This is infrastructure, not an enabled whole-app
float-color feature: the current canvas still renders the legacy policy until all
color source/composition/effect/presentation edges are integrated. Version-two
blur levels resample premultiplied float planes, retain HDR/negative/sub-byte RGB,
and interpolate scalar/masked strengths without byte round trips. Float outline
stacks use the float kernel rather than the optimized byte-only shortcut, and
the final float stack does not clip unchanged HDR pixels. Constant preservation,
linearity, cache ownership and legacy outline/blur checks pass (92 passed,
9 native-GPU cases skipped in the offscreen test environment). Warp and cage
sampling, cached smudge checkpoints, opacity masks, regular halftone tiles, and
custom object blends now preserve float precision too. Byte-only GPU paths
decline float requests before creating a context. The spatial/legacy checks pass
all 326 cases, including active warp linearity and exact regional agreement.
Remaining source-color and whole-canvas edges still prevent enabling the feature.

### Exact point chains and graphics ownership

Unmasked brightness/contrast and RGB curves can compile final float results for
every channel-byte/alpha-byte pair. Independent channels use table lookups;
this retains every float bit across supported blend modes. Spatial stacks have
an explicit table variant retaining their byte quantization between stages.
Masks and unsupported/noncanonical inputs keep the reference kernels. Tile
nodes can bypass compatible point prefixes without removing their identities.

The native OpenGL 4.3 engine uses RGBA32F sources, tables and results under a
128 MiB GPU budget, reuses shaders/uploads, and reads back only the final result
with typed float buffers. Qt framebuffer-to-image conversion is not used,
because it quantizes float textures. Initial 1025-square measurements were
300 ms for the reference, 31 ms for warmed CPU tables and 12 ms for warmed GPU
execution; the first GPU call cost 310 ms. Cold cost must not move onto pen input.
The GUI now prepares an offscreen surface between events; the dedicated worker
creates and owns its context/programs/textures, and serves detached CPU jobs.
GUI captures use the exact CPU table. Queued snapshots have a separate 128 MiB
admission limit and cancellation, and shutdown joins the owner before destroying
the surface. Native tests cover pixel identity, rows/HDR, copy isolation,
queue admission, CPU fallback and process-exit cleanup.

### Exact GPU scalar blur

Scalar legacy blur uses integer RGBA8 textures, the reference's 22-bit bilinear
coefficients, and both byte-rounding passes. CPU-generated byte-pair tables
preserve Pillow's RGBA/RGBa conversion and scalar blend; a float lookup preserves
NumPy's normalization bits. Pyramid levels and upscales stay resident under the
same 128 MiB GPU budget as point operations. Only the final RGBA32F image is read
back. Queued blur and point requests share bounded, detached inputs. GUI requests,
float policies, masks and unsupported/oversized work retain the reference path.

Native cases cover both modes, odd and one-dimensional images, alpha, radius
thresholds and resource reuse. All float values match. One end-to-end 1920×1080
run measured normal warmed CPU/GPU 72.4/41.0 ms and legacy 93.8/39.6 ms. Changed
pixels took 48.5/51.4 ms on the GPU respectively; normal cold setup took 95.9 ms.
These include admission, detached copies and transfers; they are isolated kernel
measurements, not a whole-project speed ratio. Regional tile evaluation still
uses the CPU blur graph.

### Current verification limits

The complete test run first found stale whole-document snapshot and single-worker
assumptions plus font/paint-device setup differences. Focused repairs keep the
actual undo/redo, queued input isolation, exact-pixel and layout assertions.
The larger single-process run reached 3,301 cases before exhausting system commit
memory while another native benchmark ran. It is not a passing full-suite result.
The first six-process run completed 5,090 cases with 74 skips, no errors and one
failure in undo during export-region editing. The fixed transient-state handling
passes 181 focused history/control cases. The second six-process run retained
unchanged implementation hashes and found an imported-image value-equality
regression, now fixed. A navigator timing assertion failed in that broad run but
passed independently and with its preceding test group. The third run completed
5,122 cases and found an existing blur-cache counter assumption and a tile-write
spy that did not forward the new optional bounds argument. Preserving the counter
meaning and forwarding the spy arguments repaired both without weakening pixel
or write-count assertions. The fourth run completed all six shards: 5,037 passed,
86 skipped, zero failures/errors, and unchanged implementation hashes. Subsequent
pattern-contact and immutable source-revision changes have focused passing checks;
they require another frozen full run once the next implementation phase is stable.

Saved visibility hides the previously selected drafting pages. Initial occupied
benchmarks therefore did not exercise effects on active ink, despite loading the
full project. Occupied scenarios now show selected ancestors in memory, avoid
the center pivot handle, and require evidence that selected modifiers execute.
The corrected three-outline native run showed 116 ms median / 317 ms maximum pen
queue delay and 19.2 seconds to the first exact view. Separate viewport previews,
restricted to the expanded dirty composite and with outlines deferred during
contact, reduced one matched run to 41 ms median / 115 ms maximum. Its final native
image was byte-identical to the synchronous oracle, all three effects executed,
no inputs were left unpresented, and project files were unchanged. Initial exact
loading remained 18.9 seconds; graph mips/better startup scheduling are unresolved.

The preview composites through the existing layer/mask/blend renderer, replacing
affected areas with Source composition so erasing reveals underlying artwork.
It lives outside exact tile caches and remains visible while the final projection
finishes. Detached exports remain synchronous. Focused publication and projection
checks pass; native presentation/worker/kernel checks pass all 65 cases.

The taller 1533×4123 selected raster, made visible only in memory and given three
additional point effects, exercised all six selected effects. Its initial native
run took 107.7 seconds to first exact presentation and reached 675 ms maximum pen
queue delay, although final pixels were byte-identical. A profiled follow-up
exhausted available system commit memory before reaching input. Neither result
supports a general large-stack speed claim. Color-plus-outline contact captures
now admit exact regional evaluation, and allocation work below reduces their
scratch requirements. A repeat after all 24 fixes avoided the memory failure and
preserved finished pixels, but median/maximum pen queue delay remained 499/785 ms.
A full 1401×7998 background pattern was still rendering on the GUI during contact.
Its new contact draft is bounded before masks/target captures, reuses any completed
exact result first, and cannot enter the exact tile cache. The matched repeat took
98.3 seconds to the first exact frame and reduced median/maximum queue delay to
260/477 ms; maximum input paint was 484 ms. All six selected effects were observed,
the finished native frame matched every byte, no input lacked its final exact
presentation, and all 4,800 source files were unchanged. This is a useful local
improvement, but the large case is still slow. A further profile is measuring
stable tile/import revisions: signatures now avoid decoding every cold resource
and no longer change solely because a display buffer was evicted and re-decoded.
That profiled run preserved all final pixels but is not a comparable latency
measurement. Its GUI trace still spent 208 ms on four background warp drafts.
Contact warps now use a smaller draft and wait until release to enqueue full
jobs. The following unprofiled repeat measured 172/337 ms median/maximum pen
queue delay and 341 ms maximum input paint, with all six effects observed and
byte-identical final pixels. First exact loading remains 97.7 seconds and terminal
exact delivery took another 19 seconds after input began. Smaller 64-pixel contact
graph tiles now reduce overcapture around local updates. Edge tests also corrected
outline coverage outside the fixed generic frame before blur; 85 regional checks
pass, including matching the 256-pixel exact graph after release. Its native
repeat measured 87/224 ms median/maximum queued pen delay and 228 ms maximum
input paint. All six effects were observed, final pixels were byte-identical,
and all 4,800 files in the input copy were unchanged. First exact loading was
98.1 seconds; final completion was 18.8 seconds after input began. These are
short, fresh-process native runs, not an overall application speedup.

Stroke previews now check retained coverage in every compositing pass before
using a dirty-region replacement. A pan can preserve document configuration
while exposing new artwork; uncovered views require a complete preview capture.
Known transparent space outside the chapter does not force unnecessary work.
Coverage/gap/pass/pan checks and existing publication tests pass (19 cases).

A startup profile of the six-effect copy found roughly 54 seconds of GUI work
across 1,556 region captures; the completed run had submitted 1,539 exact effect
jobs. That profiled run retained byte-identical final pixels but adds profiling
overhead, so its timing is diagnostic rather than a speed comparison.
The tile graph now discovers independent pending neighbors until its existing
worker slots are occupied; incomplete regions never enter finished caches.
All 122 graph/service/job/publication checks pass. The first native concurrency
repeat exhausted memory during the final synchronous oracle and is rejected as
a verified measurement. Prepared source/stroke caches had been bounded per
thread, multiplying their retained memory as more workers became active.
The live pool now shares one thread-safe 256 MiB preparation cache. Immutable
entries are computed outside its short locks, and a weak registry lets the last
executor thread release its storage. All 116 precision, cache, smudge, and
worker-lifetime checks pass. The next unprofiled native repeat completed first
exact loading in 84.0 seconds and settled 16.2 seconds after input started.
Queued pen delay was 90/218 ms median/maximum and maximum input paint was
222 ms. All six effects were exercised, final pixels matched every byte, and
all input files remained unchanged. The pool retained 232 MiB within its one
256 MiB preparation limit. This short real-project repeat supports improvement
over the prior 98.1-second/18.8-second run; it is not a whole-program speed claim.

Raster history uses a shared 64 MiB cache per tile store. Older immutable images
move to private raw storage rather than another independently budgeted cache per
command. Precision, alpha, image metadata, fill-replay updates and later edits
remain isolated. Private disk failure retains recoverable pixels in memory and
reports the failure instead of losing undo. This exceptional case suspends the
normal bound. The edited tile cache has the same lossless failure behavior.

An isolated Pocket Boyfriend run edited 40 existing raster tiles in the full
135-object/78-modifier chapter with deliberately reduced 4 MiB tile and history
budgets. Both stayed within those budgets. Median edit/undo/redo times were
1.35/4.89/5.28 ms; undo/redo restored exact pixels, the saved copy reopened with
unchanged existing PNG serialization semantics, and all input files stayed
unchanged. This verifies storage/history, not effect evaluation during painting.

## Smaller fixes ledger

1. **Windows TLS startup:** repeated canvas/clipboard managers previously ran
   general TLS plugin discovery, which instantiated an unused OpenSSL plugin
   and triggered the observed missing-DLL-entry diagnostic. Load the Schannel
   plugin from the installed Qt runtime directly, retain its loader, and select
   it once. Preserve native certificate verification and the fallback for unusual
   Qt builds. Fresh-process HTTPS clipboard failure handling is the regression
   check; no external network service or live clipboard is involved.
2. **Scalar blur radius maps:** ordinary uniform blur allocated image-sized
   float radius and integer level maps before reading their first value. Keep
   scalar selection scalar in full-frame and regional paths; varying masks retain
   their existing composite rounding. Against the preserved function, all values
   matched across thresholds and both algorithms. At 1920×1080 the normal warmed
   median fell 58.2 → 45.7 ms and traced peak 91.2 → 66.4 MB in one run.
3. **Repeated event type access:** the large-window profile recorded 120,029
   Qt event-type accesses across 7,411 filter calls. Read the immutable type once
   in the main filter and reuse it through the existing dispatch logic. Hotkey,
   outliner and tablet feature checks pass; no input route is removed.
4. **Repeated mapped tile vertices:** the native profile rebuilt 7,482 tile
   vertex arrays across 79 frames, spending about 203 ms in mapping. Cache up to
   4,096 geometry entries independently of image revisions. Camera, viewport,
   clipping, source crop and dimensions participate in invalidation. CPU geometry
   comparisons and native framebuffer checks pass.
5. **Single-tile assembly copies:** an exact region already supplied by one tile
   was allocated and repainted through QPainter. Return a shared COW image handle
   when its frame, size and format match; retain normal assembly for halos and
   mixed/multiple tiles. Regional/effect/render-service exact comparisons pass.
6. **Repeated driver texture-limit query:** retain the maximum texture size when
   initializing each presenter context. Context changes reinitialize it; over-size
   fallback checks remain. Native tests verify repeated draws skip the query.
7. **Repeated sampler state assignment:** private tile textures now retain their
   sampling mode. Set filters only for a new texture or a smooth/nearest change.
   Native tests verify repeated frames preserve pixels and both mode changes apply.
8. **Repeated vertex-buffer replacement:** mapped arrays surviving pixel-only
   edits retain their GPU buffer. Repack/upload only when vertex identity/order
   changes; clear the retained signature on context teardown. Native checks verify
   pixel edits retain the buffer while camera changes update it.
9. **Non-input event dispatch:** paint/layout/timer events entered every outliner,
   popup and hotkey helper. A retained set routes only the event types these
   handlers support through their existing logic. Other events reach Qt's original
   filter immediately; hotkey, deactivation and tablet/outliner checks pass.
10. **Duplicate outline silhouette scan:** cache misses thresholded the same alpha
    once for the hash and again for occupied bounds. Reuse the immutable boolean
    silhouette for both. Threshold/NaN behavior and final fields are unchanged.
11. **Intermediate EDT byte cast:** a boolean transparency image was converted
    into a byte image immediately before SciPy converted it for its distance
    transform. Pass the existing boolean image directly. Exact distance/outline
    checks pass. The two scan changes together measured 101.2 → 97.9 ms cold at
    1920×1080 in one run; this small difference is not a general speed guarantee.
12. **Repeated projection capture geometry:** immutable requests rebuilt world
    rectangles and gutter adjustments for every invalidation/intersection. Cache
    the exact four coordinates and return independent Qt rectangle values. This
    preserves original floating-point operations at every resolution scale.
13. **Repeated invalid-tile intersection:** each later dab rechecked geometry for
    tiles already awaiting replacement. Skip those intersections while still
    advancing the document revision and preserving old exact pixels. Regional,
    multi-configuration and asynchronous publication checks pass.
14. **Copied import redecoding:** copying a validated image into another store
    read all encoded bytes and decoded the image again. Share its immutable source
    and an available decoded COW frame; a missing frame stays lazy. Tests verify
    copying avoids decoding and later image edits remain isolated.
15. **Repeated identical imports:** an unchanged validated filename/MIME/payload
    no longer decodes and writes another private original. Keep its immutable
    source and dirty notification. Changed bytes or metadata still validate.
16. **Byte normalization scratch:** image-to-float and blur readback conversions
    previously allocated both cast and divided full frames. Divide the detached
    float buffer in place. Shared conversion keeps every float bit and never
    changes the input. At 1533×4123: 45.2 → 22.0 ms, 192.9 → 96.4 MiB traced peak.
17. **Warp output scratch:** clamp, premultiply constraints, scaling and nearest
    rounding used separate full float frames. Reuse the owned sanitized buffer
    while preserving nonfinite handling and input isolation. 314.5 → 253.0 ms,
    289.3 → 217.0 MiB traced peak on the same size.
18. **Modifier output quantization:** clip the owned scaled frame in place before
    the same truncating byte conversion. The blur-source and legacy pixel helper
    use that shared conversion. 67.7 → 56.9 ms, 192.9 → 120.6 MiB traced peak.
19. **Straight-color reconstruction:** avoid separate RGB division and an RGBA
    concatenation. Divide a contiguous RGBA buffer and restore unchanged alpha.
    A first strided-RGB attempt was slower and was replaced. Final comparison:
    133.7 → 89.4 ms, 168.8 → 102.5 MiB traced peak; transparent thresholds match.
20. **HSL RGB extrema:** chained channel maximum/minimum operations reuse their
    output rather than general reductions over strided RGB triples. Finite,
    signed-zero, nonfinite and ordinary reference cases retain exact bits.
21. **HSL result reconstruction:** allocate the final RGBA output once, write
    colors directly and retain original coverage. Avoid a separate RGB result
    and concatenation. Changes 19–21 together measured 1410.6 → 1113.6 ms for HSL
    at 1533×4123, with 530.4 → 506.3 MiB traced peak. These are isolated single-run
    medians; not a whole-project speed ratio. Tracing excludes some Qt/Pillow
    native allocations.
22. **Unused CPU blur levels:** scalar/small-radius blur no longer constructs all
    eight pyramid levels. Retain needed immutable levels and extend the prefix
    for larger later radii. Duplicate concurrent extension cannot double-count
    cache bytes; normal/legacy output matches complete pyramids.
23. **Raster alpha bounds conversion:** standard native ARGB tiles no longer
    unpremultiply/copy their RGB just to inspect alpha. Extract only coverage and
    keep the existing fallback for other formats. 10.4 → 2.18 ms, 24.1 → 6.03 MiB
    traced peak at 1533×4123; ordinary 256-pixel tiles use the same exact rule.
24. **Repeated undo bounds scans:** immutable history images retain their exact
    alpha bounds after first use. Later undo/redo restores those bounds with the
    pixels rather than rescanning every tile. Fill-replay replacements retire
    the old value and its bounds. Tests verify two scans for eight applications.

## Research

[libvips evaluation documentation](https://www.libvips.org/API/current/how-it-works.html)
describes demand-driven regions, immutable operation inputs, per-worker writable
state, and bounded concurrent sinks. These principles support filling available
workers from independent tile demands while retaining a single bounded pool of
immutable preparation results. This uses the existing evaluator and adds no
libvips dependency.

[Qt image storage documentation](https://doc.qt.io/qt-6/qimage.html) describes
implicit image sharing and typed premultiplied float formats. Private history
therefore holds COW copies while resident and records raw storage plus metadata
when spilling; it avoids PNG conversions that could quantize a working image.

[Pillow 12.2.0 resampling source](https://github.com/python-pillow/Pillow/blob/12.2.0/src/libImaging/Resample.c)
defines the tested fixed-point coefficient/rounding and pass order. The GPU blur
keeps those byte boundaries, including odd and one-dimensional sizes. Its legacy
premultiplication and [blend source](https://github.com/python-pillow/Pillow/blob/12.2.0/src/libImaging/Blend.c)
behavior are calibrated against the installed library. Future library changes
must retain an explicit compatibility gate and CPU fallback.

[Qt OpenGL context documentation](https://doc.qt.io/qt-6/qopenglcontext.html)
requires a context to be made current only in its owning thread; texture sharing
must be established before context creation. Worker execution therefore needs
its own context and detached pixel inputs, with explicit lifetime and completion
handling. Moving a live canvas renderer into a pool would violate that boundary.
The [Qt offscreen surface documentation](https://doc.qt.io/qt-6/qoffscreensurface.html)
requires creating and destroying its native surface on the GUI thread, while
allowing it to be used by an OpenGL context in another thread. The dedicated
graphics worker follows this lifetime rule and uses no shared mutable GL objects.

[NumPy thread safety](https://numpy.org/doc/stable/reference/thread_safety.html)
supports parallel low-level numeric work when workers own their arrays; shared
mutation requires synchronization. Parallel effect jobs must retain the existing
detached-input contract and impose a combined memory budget.
[NumPy's division contract](https://numpy.org/doc/stable/reference/generated/numpy.divide.html)
provides an explicit output buffer and preserves that buffer where a condition
is false. Numeric conversions therefore reuse owned storage, while straight
color starts with zeroed pixels so transparent locations keep their old behavior.

[OpenColorIO developer guidance](https://opencolorio.readthedocs.io/en/latest/guides/developing/developing.html)
describes cached CPU/GPU processors, batched pixel conversion, display/view
transforms, and shader extraction. Processor construction belongs to render
setup, rather than each pixel/tile call.

[Qt's TLS loader source](https://code.qt.io/cgit/qt/qtbase.git/tree/src/network/ssl/qtlsbackend.cpp)
shows that general discovery instantiates all backend plugins, and skips that
scan when a backend is already registered. The selected native plugin remains
owned through Qt's public `QPluginLoader` API. [Qt's backend selection contract](https://doc.qt.io/qt-6/qsslsocket.html#setActiveBackend)
requires selection before any SSL sockets or related certificate objects exist.
