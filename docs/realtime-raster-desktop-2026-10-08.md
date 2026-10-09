# Desktop raster contact latency repair

The reported raster had a saved slight rotation. Prepared contact feedback
previously rejected every off-axis mapping, so Pencil, Eraser and Brush repeatedly
captured and rendered the chapter while input continued. Fast GPU presentation
could still show stale artwork until a matching detached preview finished.

## Implementation and native correctness

Finite, invertible affine object/ancestor transforms now use prepared native
contact resources. Rotations, shears and reflections retain their original source
pixels, grids, Qt nearest-image sampler, layer transforms/clips and opacity phase.
Source images are immutable COW handles from the detached original-source owner;
GUI presentation substitutes only resident edited tiles, including pruned tiles.

Simply accepting these transforms on the previous 256-pixel feedback devices
failed a whole-buffer native oracle at a source seam. Clearing transformed tile
rectangles also gave a different edge phase, and an intermediate mapped image
introduced translucent byte rounding. The accepted off-axis path instead draws
original native tiles directly onto the prepared prefix at the ordinary exact
renderer's 4×4 block origin. This retains the Qt device/clipping scanline phase.
It omits the intermediate selected-source plane. Prefix/suffix planes plus pinned
original buffers have a combined 64 MiB cap, checked during preparation; composed
presentation keeps the existing 32 MiB LRU. Axis-aligned feedback keeps its
existing 32 MiB preparation limit. Worker admission reserves the larger payload.

Coverage validation requires the complete visible block set. A budget-truncated
resource set cannot suppress normal scene rendering indefinitely. A pending owned
cold native packet pauses obsolete scene/effect work until input is ready;
release, cancellation, invalid ownership and unavailable coverage resume guarded
ordinary rendering. Worker completion now queues immediate owner-thread
publication instead of relying only on eight-millisecond polling.

Imported Brush materials previously decoded original PNGs and built every
pressure/spray/replay mip on each pen-down. An independent per-canvas immutable
material-resource LRU now retains at most 64 MiB/eight owners. Retained PNG object
identities and complete mip/grayscale requirements guard reuse without hashing
large resources on the GUI. Cold resources still use the existing admitted worker.

Artwork remains one sample per document pixel regardless of camera zoom or
display density. Original RGBA64 pixels, profiles, native packet order, pressure,
brush dab/RNG equations, corrected replay and history transactions are preserved.
All feedback/material resources are transient. Exact output and disk caching use
the original shared renderer, dependency keys and invalidation; no live results
become durable exact entries.

## Native RTX 5060 measurements

`tests/benchmark_raster_contact_latency.py` runs the ordinary OpenGL canvas in
fresh native Windows processes, with a 1080×70000 chapter and saved −3° raster
rotation. Forty ordered synthetic contacts per tool are scheduled at 120 Hz.
It timestamps tool entry and native frame swap, then samples the existing
framebuffer to verify current red ink/white erased pixels. Source truth and
one-command release/undo are checked. It uses no desktop automation, source
project or user settings. Python is the launcher runtime, 3.11.15, with Qt 6.11.1;
the actual GL renderer reports NVIDIA GeForce RTX 5060/PCIe/SSE2.

The matched baseline is commit `d74be44`. The final observer samples only the
pending contact rectangle; early full-frame observers added substantial readback
overhead and are retained as diagnostics, not the final timing pair.

| Tool | Baseline median / p95 | Candidate median / p95 | Candidate maximum |
|---|---:|---:|---:|
| Pencil | 180.90 / 326.56 ms | 5.34 / 13.91 ms | 22.28 ms |
| Eraser | 179.77 / 326.19 ms | 5.21 / 6.38 ms | 6.65 ms |
| Brush | 184.75 / 332.37 ms | 9.85 / 13.80 ms | 14.88 ms |

All 120 candidate contacts appeared as current prepared pixels while held, with
zero held scene submissions. The baseline had no prepared current feedback and
28/34/20 held scene submissions respectively; many inputs appeared together only
after incoming motion paused. Candidate scheduled-input lateness p95 was
16.10/15.66/11.24 ms, distinct from handler-to-frame timing. These synthetic
instrumented runs do not measure physical tablet-driver latency or screen scanout
and are not a universal frame-rate guarantee.

At 0.35× zoom and native DPR 1.5, all 120 contacts again appeared as current prepared
pixels with zero held scene submissions. Pencil/Eraser/Brush p95 was
10.14/9.70/15.23 ms, with maxima 12.70/10.61/17.68 ms. A separate DPR 1.0 zoomed-out
run had Brush p95 of 32.51 ms and maximum 39.67 ms; the repair removes the old stale
scene delay but cannot claim every observed frame fits 16.7 ms. A first early
observer had a 138 ms Brush outlier that did not recur in profiling/repeats.

Independent imported-material measurements on native RGBA64 source with a
1024×1024 tip over ten contacts reduced warm first-dab median/p95 from
43.65/44.85 ms to 4.79/5.14 ms. The first cold contact still took 36.82 ms. Those
measure native dab publication before presentation. Isolated one-tile cold-source
preparation measured median 9.24→3.77 ms after immediate completion wakeup,
excluding a separate one-time GPU initialization cost.

## Verification and remaining scope

Whole-buffer regressions cover all three tools with rotation/shear/reflection,
rotated/nested ancestors, source seams, RGBA64 originals, partial opacity,
unchanged neighbor pixels, foreground ordering, full eraser pruning, exact
release and undo. Presentation guards reject GUI scene evaluation or source
decode. Coverage tests exercise four native blocks and missing prepared coverage;
cold-input tests exercise held, ready, release and cancellation transitions.
Material tests compare against uncached worker preparation, including dual
materials, spray, corrected replay, native source precision and undo/redo.
The combined native Windows/DPR 1.5 selection passed 302 checks, including
input/source ownership, material reuse, continuous/corrected Brush behavior,
feedback, scene backends, admission, live disk status and effect-release handoff.
A final 52-case affine/transform/cold-contact run also passed after the last
folded/projective eligibility guard. These selections overlap. Both receipts
have zero failures/errors/skips; syntax and whitespace checks also passed.

The implementation retains conservative fallback for perspective/collapsed
transforms, selected/ancestor modifiers or masks, floating document policies,
linked samplers and coverage beyond the bounded resources. Large original
material preparation and first graphics initialization still have cold costs.
These cases are not established as uniformly real time by the affine benchmark.

Private evidence is under `.artifacts/realtime-contact-20261008`, including
`final-baseline-gpu.json`, `final-candidate-gpu.json`,
`final-candidate-gpu-zoom035.json`, and
`final-candidate-gpu-dpr15-zoom035.json`. Final regression receipts are recorded
there separately. Earlier failed/observer captures remain available for audit.

Reproduce the native probe in a fresh process:

```powershell
python tests/benchmark_raster_contact_latency.py --out .artifacts/contact-gpu.json
$env:QT_SCALE_FACTOR = '1.5'
python tests/benchmark_raster_contact_latency.py --zoom .35 --out .artifacts/contact-gpu-dpr15.json
```
