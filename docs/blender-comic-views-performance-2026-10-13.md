# Comic Views Save and Render performance repair — October 13, 2026

## Reproduction

The user reported Blender blocking during Comic View Save and Render, sometimes
alongside a browser crash. A separate copy of `lanceroomportals.blend` reproduced
long saves: 3,023 objects, 2,333 Actions and 50 Comic Views, using the user's
Fourth Axis Blender 5.3.0 Alpha build `41c29746937b`.

The original .blend and running Blender were left untouched. Separate factory
startup/background processes loaded the same copy, selected its active Comic
View 50, increased the active camera lens by 0.125, saved, saved unchanged, and
exercised `_render_saved_snapshot` with only `render_active_camera` replaced by
a small dummy frame. The last measurement isolates real state capture, bake
validation, application and restoration; it is **not** total GPU render time.
The old process imported the original checkout from a `git archive HEAD` copy;
the new process imported 0.8.2. No test wrote a .blend or published into the
user's normal frame directory.

## Results

Normal wall-clock measurements in separate processes:

| Operation | Original 0.8.1 | Updated 0.8.2 |
| --- | ---: | ---: |
| Save after lens edit | 77.42 s | 18.90 s |
| Save unchanged | 7.69 s | 3.62 s |
| Render state preparation/restoration, GPU draw stubbed | 7.55 s | 3.76 s |

The original profiled edited Save took 122.17 s; the updated profiled Save took
29.67 s before the final redundant-frame-selection shortcut. Profiler overhead
is substantial, so those values are separate from normal wall-clock results.
The original rewrote 21,008 channels and called `_write_point` 669,908 times;
the update changed 1,162 curves. Identical-record reuse reduced `_add_channel`
calls from 2,557,039 to 277,855. Existing scene differences beyond the lens edit
were captured normally rather than discarded to improve timings.

A separate Blender 5.2.2 synthetic scene had 803 objects and 80 child collections.
Applying an identical snapshot fell from 0.1213 s to 0.0167 s. A seeded random
2048×2048 RGBA8 frame measured alpha conversion at 0.2169 s / 121,653,772 bytes
of traced temporary allocations before, versus 0.1366 s / 34,408,593 bytes after.
PNG encoding fell from 1.2417 s to 0.3803 s. The compressed PNG grew from
13,508,022 to 13,769,074 bytes, while decoded pixels stayed identical. These
are single-process samples, not universal latency or size guarantees.

## Causes and changes

- All views formerly retained a separate `SnapshotChannel`, key and label for
  every sample. Baking now retains one descriptor and compact integer-frame /
  float64-value arrays, and shares equal records only inside that operation.
- Every existing animated channel formerly rewrote every owned key, retaining
  rollback and handle-style objects even for equal keys. Only missing, changed
  or nonconstant keys now need those mutations. Comparisons use Blender's
  float32 F-Curve storage edge. Style ownership uses an integer set lookup with
  the original frame tolerance rather than testing every view per key.
- Unchanged visibility and optical assignments could invalidate expensive scene
  resources. Scene application now compares values first, and unchanged Local
  View membership avoids toggling out and back in. Hidden membership is preserved.
- Cached native snapshots formerly parsed and serialized all older views to
  calculate the same hash. Canonical bytes now calculate the existing marker
  directly; older/noncanonical JSON still takes the parser/normalizer path.
- Cached keys are evaluated before applying saved values, so restoration cannot
  conceal damaged animation. Verification reuses operation-local channel data.
- Render formerly captured the working scene twice, converted full-frame alpha
  using multiple full-frame temporary arrays, and retained another uncompressed
  PNG scanline image. It now shares the working snapshot, bounds alpha work to
  65,536 pixels per strip and feeds PNG scanlines in roughly 64 KiB blocks.
- Matching cached timeline selections avoid redundant `frame_set` calls. Render
  publication logs now separate rendering/restoration, encoding and publication.

## Correctness and verification

`tests/test_blender_extension.py`, `tests/test_blender_source_protocol.py` and
`tests/test_blender_image_sources.py` passed 88 checks, with
`WEBTOON_BLENDER_LIVE_TEST=1` and the Fourth Axis executable in
`BLENDER_EXECUTABLE`. The extension suite additionally exercised installed
Blender 4.5 and 5.2, including live GPU smoke tests, installation, manifest
validation, Action slots, backfill, rollback, scene restoration, driver policy,
thumbnails, Local View and publication failures. The final Action-slot and
bookkeeping checks passed again on all three builds after the last edits.

`tests/_blender_renderer_memory_probe.py` independently compares every RGB-byte /
alpha-byte combination against the old integer formula, plus asymmetric
multi-strip images. It checks PNG headers, CRCs, scanline filters and exact
decompressed RGBA bytes, rejects malformed frame lengths, and guards alpha/PNG
workspace sizes. Bookkeeping regressions verify matching keys create no point
or style mutations, canonical unchanged saves avoid repeated parsing, identical
records reuse work, changed records refresh, and failed edits restore original
keys and styles.

There is no sampling exception or new exact cache. Output dimensions, GPU
readback, native source pixels, integer alpha math, color conversion and the
memory/disk cache contracts remain unchanged. PNG compression level affects
encoded size only. Working state and publication rollback remain transactional.

Save still runs Blender RNA work on its main thread and can pause on a large
scene. These changes substantially reduce that work; they do not make Blender
operations asynchronous. The browser crash was not reproduced or diagnosed,
and full-scene GPU draw cost remains unmeasured. Reduced allocation and scene
invalidations address plausible resource pressure, but do not establish that a
browser/driver crash is fixed.

The installable package is `blender_extension/webtoon_comic_views-0.8.2.zip`.
