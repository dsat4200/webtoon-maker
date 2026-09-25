# Pocket Boyfriend interaction delays

The current saved chapter reproduced long delays despite fast brush input.
It contains 74 layers, 95 objects and 31 modifiers. All experiments loaded a
fresh isolated project copy; drawing and transform changes stayed in memory.

## Causes and changes

- The navigator synchronously refreshed the chapter after drawing updates. A
  13 × 5-pixel dirty band could process full-size halftone images and GPU
  readbacks. It now paints cached pixels during gestures, defers scene work,
  maps dirty bands back to document coordinates, and uses bounded thumbnail
  captures for supported effects. Thumbnail results cannot enter exact caches.
  Full refreshes run in short bands with an event-loop yield between them;
  another gesture pauses the remaining work. The previous complete thumbnail
  stays visible until the new one is ready.
- A tall outlined raster required an approximately 82 MiB source. This replaced
  the entire 64 MiB effect cache, repeatedly evicting other unchanged artwork.
  Interactive raster outlines now use guarded, aligned viewport windows. Full
  dependency captures and exports retain their complete source.
- Effect opacity masks expanded full images into multiple floating-point RGBA
  arrays. The mask kernel now processes bounded strips and skips floating-point
  work for fully opaque/transparent pixels. Completed mask results are cached;
  ordinary viewport masks can restrict work to the visible output.
- Transform previews disabled scene culling globally. Object and multi-object
  drags now retain affected branches and compound dependencies while culling
  unrelated artwork. Free text also follows its live transform when Show on Top
  requires normal hierarchy rendering.
- Selection hits constructed expensive outline geometry even for distant
  shapes. Conservative rejection and the existing bounded mesh cache avoid that
  work. Complex panel outlines reuse full-resolution raster clipping instead
  of repeating geometric Boolean normalization on zoom.

## Measurements

Windows/native Qt measurements on the copied current chapter, using the same
machine and scene. These measure synchronous application work, not physical
pen-to-display latency. Warm-up, first-use effects, and background machine load
can affect individual frames.

| Operation | Before | After |
| --- | ---: | ---: |
| Drawing plus navigator, median | 1,086 ms | 10.7 ms |
| Object drag, first frame | 1,185 ms | 15.4 ms |
| Object drag, median | 79.3 ms | 14.3 ms |
| Pan in the effect-heavy area, median | 346.7 ms | 18.0 ms |
| Zoom in the same area, median | 388.4 ms | 28.5 ms |
| Normal selection lookup, median | 15.3 ms | 7.0 ms |
| Shape-edit lookup, median | 33.8 ms | 2.9 ms |

The pan/zoom comparison restores the former mask and outline paths in a
separate process. Both versions settle asynchronous effects before nine camera
changes. The updated zoom run's slowest frame was 73 ms while preparing newly
needed geometry/captures; these measurements are not a guaranteed frame rate.

A separate 3072 × 2048 mask benchmark preserves every output byte: smooth masks
fell from 210 to 82 ms, sparse masks from 210 to 29 ms, and opaque masks from
216 to 9 ms. Peak NumPy working memory fell from 408 MiB to under 5 MiB.

A complete navigator refresh is now divided into 32-row batches. On this
chapter, batches took 7.4 ms median and 60.6 ms maximum, with 325 ms total work
spread across event-loop turns. The assembled thumbnail matched a single full
render byte for byte. During drawing, displaying the cached navigator took
approximately 0.1 ms.

## Correctness and reproduction

The viewport-window and opacity-mask changes matched full captures byte for
byte at four saved-chapter positions, including the expensive region. Selection
comparisons matched all 4,896 sampled decisions. Additional tests cover mask
edits, parent scaling/reflection, panning, rotated/projective placement,
offscreen-to-onscreen transforms, one-step undo/redo, exports and cache pressure.

Complex outline clipping preserves full-resolution coverage but can change
antialiasing at boundaries: at 120% zoom, the measured panel changed 94 of
800,000 pixels by one alpha level. Non-antialiased, vector, high-depth and
oversized output retain geometric clipping. The navigator remains an approximate
thumbnail; canvas and export sources remain independent.

Local evidence is in `.artifacts/responsiveness-20260924/`, including
`drawing-saved-chapter-before-timings.json`,
`drawing-saved-chapter-after-timings.json`, `drag-before.json`, `drag-after.json`,
`viewport-comparable-before.json`, `viewport-comparable-after.json`,
`viewport-pixel-comparison.json`, `hit-comparison.json`,
`plain-outline-comparison.json` and `opacity-mask-before-after.json`.
The accompanying probes require the isolated project copy in that directory.

Restart an already-running editor to load the source changes.
