# Preview cache and canvas visibility repair — October 11, 2026

## Evidence from the three captures

The user's main caching, saved-cache loading and navigator scrubbing captures
were inspected separately. Their context identifies Pocket Boyfriend's rewritten
1080×82000 chapter, with 106 layers, 173 objects and 103 modifiers. The captured
solo state was disabled and contained no entities. The main caching run lasted
643.6 seconds; its maximum GUI heartbeat gap was 1.58 seconds. The loading
capture's 6.17-second maximum gap also includes substantial theme/settings
interaction and is not attributed entirely to cache reads. Worker envelope
wall times include admission and waiting; inclusive phase totals overlap.

The main caching path constructed semantic dependency keys and source file stat
stamps in GUI status ticks. A tick deadline could not interrupt an expensive row
calculation. The previous recording demand held a heavy admission ticket across
the chapter, preventing visible scene evaluation until cancellation. Green rows
described validated saved blobs rather than a ready canvas presentation. During
scrubbing the local 256 MiB projection cache also evicted native tiles: one full
chapter phase requires approximately 414 MiB before any separate top/base phases.
These are cache ownership/scheduling problems, not evidence that the pages were
hidden. Analysis is retained in `.artifacts/cache-performance-20261011/capture-analysis.json`.

## Implemented behavior and preserved contracts

**Cache to memory** warms ordinary native exact projection, source/effect and
checkpoint values into the shared system-RAM tier. It does not force an artwork
save and does not record disk blobs. CPU QImages and numeric arrays preserve
their original bits, precision, color metadata and source/effect frames. Exact
tiles remain one sample per canvas pixel; camera zoom and display density never
increase derived artwork resolution. GPU presentation retains bounded visible
textures. Shared GPU memory uses system RAM and is not an additional independent
budget for retaining the entire project.

RAM uses the existing typed semantic descriptors and dependency validation. Its
shared LRU budget is 20% of physical RAM, capped at 8 GiB with a 256 MiB floor.
Background warming yields admission between native blocks; dependency inspection
runs off the GUI, and navigator refresh continues while warming. Green rows
require all final native tiles to be resident. A fully resident visible request
does not recapture or evaluate the scene merely because the local projection LRU
is smaller than the requested view.

Closing an explicitly warmed/restored project offers optional disk publication
after artwork Save/Discard decisions. Accepted publication is asynchronous and
cancellable, copies resident exact values through the existing recording/retain
and lossless blob paths, and performs no second render. Source alias seals are
published through ordinary fingerprint metadata, including unchanged blobs whose
source files were adopted during Save. Discarded unsaved edits skip publication.
Cancellation/failure keeps the project open. Reopening validates and restores
saved results into RAM. Drafts and live edits never become exact/durable cache
entries. Memory clears and saved disk clearing are separate menu actions.

Project close releases its resident namespaces only after canvas detachment or
switching, and only once no series/asset tab shares that project. Accepted
application close releases all project namespaces after detaching. Retired
namespace generations prevent late workers populating a reopened project.

## Measured and verified scope

- The actual saved rewritten chapter displayed an exact native tile at document
  bounds `(510, 50942, 260, 260)` in **327.5 ms while the background cache remained
  uncanceled**. Its bytes matched the independent cold pipeline; all 13,776
  source-file hashes were unchanged and no disk cache artifacts were written.
  This is a visibility/admission probe, not a whole-project warm-time result.
  Evidence: `.artifacts/cache-visibility/saved-native-visible.json`.
- On 205 native dependency requests sampled every eighth row of that same saved
  chapter, ordinary batched inspection had a **103.2 ms median**, versus **163.2
  ms** for individual inspection across three repeats. Every semantic key was
  equal and source files were unchanged. This measures dependency inspection
  only, not rendering or screen presentation. Evidence:
  `.artifacts/cache-performance-20261011/dependency-batch-after.json`.
- `tests/test_memory_cache_workflow.py` exercises the real MainWindow: warming
  writes no disk cache, accepted tab close publishes real exact values and
  retires RAM, and a new window restores identical native projection buffers
  with scene capture forbidden. Source-file hashes remain unchanged.
- `tests/test_cache_close_workflow.py` covers immediate decline, optional prompt
  cancellation, asynchronous close, the actual Cancel button, failed
  publication, source Save ordering, Discard exclusion, inactive-tab ownership,
  sequential multi-project exit, and deferred external updates. The real close/
  reopen test and all 12 UI lifecycle tests passed on native Windows Qt.
- Additional native byte/format/color, array/checkpoint, bounded RAM/LRU,
  retirement, source-seal, disk-epoch, corruption, resident canvas and background
  admission coverage is in `test_resident_render_cache.py`,
  `test_resident_cache_metadata.py`, `test_memory_cache_source_export.py`,
  `test_disk_render_cache.py`, `test_cache_visibility_admission.py`, and
  `test_render_dependency_batch.py`.

The original project and its saved artwork were not modified by investigation.
The full 82,000-pixel chapter can exceed local presentation caches and individual
effect allocations; these checks do not claim every cold effect or full-project
warm operation completes within one interactive frame.
